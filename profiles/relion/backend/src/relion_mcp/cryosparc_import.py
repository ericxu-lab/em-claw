from __future__ import annotations

import ctypes
import datetime as dt
import errno
import hashlib
import json
import math
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from itertools import chain, pairwise
from pathlib import Path, PurePosixPath
from typing import Any

import mrcfile
import numpy as np

from .catalog import CatalogError, safe_alias
from .settings import Settings
from .star import (
    StarFormatError,
    _is_native_structural_data_line,
    _relion_simplify_line,
    _tokens,
    parse_star_block,
    particle_star_row_summary,
    relion_metadata_loop_digest,
)


class CryosparcImportError(RuntimeError):
    """Raised when a CryoSPARC particle import cannot be completed safely."""


SCHEMA_VERSION = "em-claw/cryosparc-particle-import/v2"
OUTPUT_BASE = PurePosixPath("External/CryoSPARC")
DISK_RESERVE_BYTES = 64 * 1024 * 1024
STAR_STAGING_BYTES_PER_PARTICLE = 1024
DIAGNOSTIC_TAIL_CHARS = 4000
RENAME_NOREPLACE = 1

_BLOB_FIELDS = ("blob/path", "blob/idx", "blob/shape", "blob/psize_A")
_CLASS3D_CTF_FIELDS = (
    "ctf/df1_A",
    "ctf/df2_A",
    "ctf/df_angle_rad",
    "ctf/accel_kv",
    "ctf/cs_mm",
)
_INSPECTED_CS_FIELDS = (
    *_BLOB_FIELDS,
    *_CLASS3D_CTF_FIELDS,
    "ctf/amp_contrast",
    "ctf/ac",
    "ctf/exp_group_id",
    "location/micrograph_path",
)
_PARTICLE_STAR_REQUIRED_COLUMNS = {
    "rlnImageName",
    "rlnOpticsGroup",
    "rlnDefocusU",
    "rlnDefocusV",
    "rlnDefocusAngle",
}
_OPTICS_STAR_REQUIRED_COLUMNS = {
    "rlnOpticsGroup",
    "rlnImageSize",
    "rlnImagePixelSize",
    "rlnImageDimensionality",
    "rlnVoltage",
    "rlnSphericalAberration",
    "rlnAmplitudeContrast",
}
_MRC_MODE_BYTES = {0: 1, 1: 2, 2: 4, 6: 2, 12: 2}


@dataclass(frozen=True)
class _FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> _FileIdentity:
        return cls(
            device=int(value.st_dev),
            inode=int(value.st_ino),
            size=int(value.st_size),
            mtime_ns=int(value.st_mtime_ns),
            ctime_ns=int(value.st_ctime_ns),
        )

    def public(self) -> dict[str, int]:
        return {
            "size_bytes": self.size,
            "mtime_ns": self.mtime_ns,
        }


@dataclass(frozen=True)
class _CsInput:
    role: str
    relative_path: str
    path: Path
    identity: _FileIdentity
    sha256: str
    row_count: int
    fields: tuple[str, ...]
    dtype: str
    uids: np.ndarray
    selected_values: dict[str, np.ndarray]

    def public(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "path": self.relative_path,
            "sha256": self.sha256,
            "row_count": self.row_count,
            "dtype": self.dtype,
            "fields": list(self.fields),
            **self.identity.public(),
        }


@dataclass(frozen=True)
class _StackBinding:
    source_relative: str
    source_path: Path
    pyem_paths: tuple[str, ...]
    destination_relative: str
    identity: _FileIdentity
    nx: int
    ny: int
    nz: int
    mode: int
    pixel_size_A: float
    first_index: int
    last_index: int
    referenced_particle_count: int

    def public(self) -> dict[str, Any]:
        return {
            "source": self.source_relative,
            "pyem_paths": list(self.pyem_paths),
            "destination": self.destination_relative,
            "size_bytes": self.identity.size,
            "shape_nzyx": [self.nz, self.ny, self.nx],
            "mrc_mode": self.mode,
            "pixel_size_A": self.pixel_size_A,
            "source_index_range_zero_based": [self.first_index, self.last_index],
            "referenced_particle_count": self.referenced_particle_count,
        }


@dataclass(frozen=True)
class _MicrographBinding:
    source_relative: str
    source_path: Path
    pyem_paths: tuple[str, ...]
    destination_relative: str
    identity: _FileIdentity
    referenced_particle_count: int

    def public(self) -> dict[str, Any]:
        return {
            "source": self.source_relative,
            "pyem_paths": list(self.pyem_paths),
            "destination": self.destination_relative,
            "size_bytes": self.identity.size,
            "referenced_particle_count": self.referenced_particle_count,
        }


@dataclass(frozen=True)
class _PyemCapability:
    configured_python: str
    configured_script: str
    python: str | None
    script: str | None
    script_sha256: str | None
    pyem_root: str | None
    issues: tuple[str, ...]

    @property
    def available(self) -> bool:
        return self.python is not None and self.script is not None and not self.issues

    def public(self) -> dict[str, Any]:
        return {
            "configured_python": self.configured_python,
            "configured_script": self.configured_script,
            "python": self.python,
            "script": self.script,
            "script_sha256": self.script_sha256,
            "pyem_root": self.pyem_root,
            "available": self.available,
            "issues": list(self.issues),
        }


@dataclass(frozen=True)
class _PreparedImport:
    project: Path
    source_project: Path
    inputs: tuple[_CsInput, ...]
    stacks: tuple[_StackBinding, ...]
    micrographs: tuple[_MicrographBinding, ...]
    pyem_path_map: dict[str, str]
    pyem_micrograph_path_map: dict[str, str]
    expected_micrograph_reference_counts: dict[str, int]
    expected_image_micrograph_multiset_digest: str | None
    expected_image_name_multiset_digest: str
    expected_group_image_digests: dict[int, str]
    source_optics_groups: dict[int, tuple[int, float, float, float, float]]
    particle_count: int
    export_name: str
    bundle_relative: str
    particles_star_relative: str
    manifest_relative: str
    bootstrap_required: bool
    project_initialized: bool
    pipeline_name: str
    capability: _PyemCapability
    alignment_warnings: tuple[str, ...]
    plan: dict[str, Any]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _identity(path: Path) -> _FileIdentity:
    try:
        value = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise CryosparcImportError(
            f"Could not inspect source file {path}: {exc}"
        ) from exc
    if not stat.S_ISREG(value.st_mode):
        raise CryosparcImportError(f"Source must be a regular file: {path}")
    return _FileIdentity.from_stat(value)


def _safe_source_relative(
    value: str, *, role: str, allow_external_marker: bool = False
) -> str:
    if not isinstance(value, str) or not value:
        raise CryosparcImportError(f"{role} must be a non-empty relative POSIX path.")
    normalized = value[1:] if allow_external_marker and value.startswith(">") else value
    if not normalized:
        raise CryosparcImportError(f"{role} is empty after its CryoSPARC path marker.")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise CryosparcImportError(f"{role} may not contain control characters.")
    if "\\" in normalized:
        raise CryosparcImportError(f"{role} must use POSIX '/' separators.")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise CryosparcImportError(
            f"{role} must stay beneath source_project_dir: {value}"
        )
    if any(part == "" for part in normalized.split("/")):
        raise CryosparcImportError(f"{role} contains an empty path component: {value}")
    return path.as_posix()


def _resolve_regular_source(
    source_project: Path,
    relative_path: str,
    *,
    role: str,
    suffixes: tuple[str, ...],
) -> Path:
    checked = _safe_source_relative(relative_path, role=role)
    if not checked.lower().endswith(suffixes):
        raise CryosparcImportError(
            f"{role} must end with one of {', '.join(suffixes)}: {relative_path}"
        )
    cursor = source_project
    for part in PurePosixPath(checked).parts:
        cursor /= part
        if cursor.is_symlink():
            raise CryosparcImportError(
                f"{role} may not traverse symbolic links: {cursor}"
            )
    try:
        resolved = (source_project / checked).resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        raise CryosparcImportError(f"{role} is unavailable: {relative_path}") from exc
    if source_project not in resolved.parents:
        raise CryosparcImportError(
            f"{role} resolves outside source_project_dir: {relative_path}"
        )
    _identity(resolved)
    return resolved


def _decode_cs_path(value: Any, *, role: str) -> str:
    if isinstance(value, (bytes, np.bytes_)):
        try:
            return bytes(value).decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise CryosparcImportError(f"{role} contains invalid UTF-8.") from exc
    if isinstance(value, (str, np.str_)):
        return str(value)
    raise CryosparcImportError(
        f"{role} must contain strings, got {type(value).__name__}."
    )


def _inspect_cs_file(
    source_project: Path,
    relative_path: str,
    *,
    role: str,
) -> _CsInput:
    path = _resolve_regular_source(
        source_project,
        relative_path,
        role=role,
        suffixes=(".cs",),
    )
    before = _identity(path)
    array: np.ndarray | None = None
    try:
        array = np.load(path, mmap_mode="r", allow_pickle=False)
        if not isinstance(array, np.ndarray):
            raise CryosparcImportError(f"{role} is not a NumPy structured array.")
        if array.ndim != 1 or array.dtype.names is None:
            raise CryosparcImportError(
                f"{role} must be a one-dimensional structured array."
            )
        if array.dtype.hasobject:
            raise CryosparcImportError(
                f"{role} may not contain object or pickled fields."
            )
        if len(array) == 0:
            raise CryosparcImportError(f"{role} contains no particles.")
        fields = tuple(str(name) for name in array.dtype.names)
        if "uid" not in fields:
            raise CryosparcImportError(f"{role} is missing required field uid.")
        uid_values = np.asarray(array["uid"])
        if not np.issubdtype(uid_values.dtype, np.integer):
            raise CryosparcImportError(f"{role} uid values must be integers.")
        if np.issubdtype(uid_values.dtype, np.signedinteger) and np.any(uid_values < 0):
            raise CryosparcImportError(f"{role} uid values must be non-negative.")
        uids = np.asarray(uid_values, dtype=np.uint64).copy()
        if np.unique(uids).size != uids.size:
            raise CryosparcImportError(f"{role} contains duplicate uid values.")
        selected_values = {
            field: np.asarray(array[field]).copy()
            for field in _INSPECTED_CS_FIELDS
            if field in fields
        }
        dtype = str(array.dtype)
    except CryosparcImportError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise CryosparcImportError(
            f"Could not load {role} with allow_pickle=False: {exc}"
        ) from exc
    finally:
        mmap = getattr(array, "_mmap", None)
        if mmap is not None:
            mmap.close()
    digest = _sha256(path)
    after = _identity(path)
    if before != after:
        raise CryosparcImportError(f"{role} changed while it was inspected.")
    return _CsInput(
        role=role,
        relative_path=_safe_source_relative(relative_path, role=role),
        path=path,
        identity=after,
        sha256=digest,
        row_count=int(uids.size),
        fields=fields,
        dtype=dtype,
        uids=uids,
        selected_values=selected_values,
    )


def _indices_for_primary(primary_uids: np.ndarray, provider: _CsInput) -> np.ndarray:
    if np.array_equal(primary_uids, provider.uids):
        return np.arange(primary_uids.size, dtype=np.int64)
    order = np.argsort(provider.uids, kind="mergesort")
    ordered = provider.uids[order]
    positions = np.searchsorted(ordered, primary_uids)
    valid = positions < ordered.size
    if not np.all(valid) or not np.array_equal(ordered[positions], primary_uids):
        missing = int(np.count_nonzero(~valid))
        if not missing:
            missing = int(np.count_nonzero(ordered[positions] != primary_uids))
        raise CryosparcImportError(
            f"{provider.role} is missing {missing} primary uid value(s); pyem's inner "
            "merge would silently drop particles."
        )
    return order[positions]


def _provider_values(
    primary: _CsInput,
    inputs: tuple[_CsInput, ...],
    field: str,
) -> np.ndarray:
    for provider in inputs:
        values = provider.selected_values.get(field)
        if values is None:
            continue
        indices = _indices_for_primary(primary.uids, provider)
        return np.asarray(values[indices])
    raise CryosparcImportError(
        f"The ordered CryoSPARC inputs are missing required field {field}."
    )


def _numeric_source_values(
    primary: _CsInput,
    inputs: tuple[_CsInput, ...],
    field: str,
    *,
    positive: bool = False,
    minimum: float | None = None,
    maximum: float | None = None,
) -> np.ndarray:
    raw = _provider_values(primary, inputs, field)
    if not np.issubdtype(raw.dtype, np.number):
        raise CryosparcImportError(f"{field} must contain numeric values.")
    try:
        values = np.asarray(raw, dtype=np.float64)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CryosparcImportError(f"{field} must contain numeric values.") from exc
    if not np.all(np.isfinite(values)):
        raise CryosparcImportError(f"{field} must contain finite values.")
    if positive and np.any(values <= 0):
        raise CryosparcImportError(f"{field} must contain positive values.")
    if minimum is not None and np.any(values < minimum):
        raise CryosparcImportError(f"{field} must be at least {minimum}.")
    if maximum is not None and np.any(values > maximum):
        raise CryosparcImportError(f"{field} must be at most {maximum}.")
    return values


def _mrc_stack_info(path: Path, *, role: str) -> tuple[dict[str, Any], _FileIdentity]:
    before = _identity(path)
    try:
        with mrcfile.open(path, mode="r", permissive=False, header_only=True) as handle:
            nx = int(handle.header.nx)
            ny = int(handle.header.ny)
            nz = int(handle.header.nz)
            mode = int(handle.header.mode)
            ispg = int(handle.header.ispg)
            nsymbt = int(handle.header.nsymbt)
            mapc = int(handle.header.mapc)
            mapr = int(handle.header.mapr)
            maps = int(handle.header.maps)
            pixel_x = float(handle.voxel_size.x)
            pixel_y = float(handle.voxel_size.y)
    except (OSError, ValueError) as exc:
        raise CryosparcImportError(
            f"Could not read {role} MRC header: {path}: {exc}"
        ) from exc
    after = _identity(path)
    if before != after:
        raise CryosparcImportError(
            f"{role} changed while its MRC header was inspected."
        )
    if nx <= 0 or ny <= 0 or nz <= 0 or nx != ny:
        raise CryosparcImportError(
            f"{role} must be a non-empty stack of square images; got ({nz}, {ny}, {nx})."
        )
    if mode != 2:
        raise CryosparcImportError(
            f"{role} must use RELION-compatible floating-point MRC mode 2; got {mode}."
        )
    if ispg != 0:
        raise CryosparcImportError(
            f"{role} must be an image stack (MRC ispg=0), not a 3D volume; got ispg={ispg}."
        )
    if (mapc, mapr, maps) != (1, 2, 3):
        raise CryosparcImportError(
            f"{role} must use canonical MRC axis mapping (1,2,3); got {(mapc, mapr, maps)}."
        )
    if not (
        math.isfinite(pixel_x)
        and math.isfinite(pixel_y)
        and pixel_x > 0
        and pixel_y > 0
        and math.isclose(pixel_x, pixel_y, rel_tol=1e-6, abs_tol=1e-6)
    ):
        raise CryosparcImportError(
            f"{role} must declare a positive, finite, isotropic XY pixel size."
        )
    bytes_per_voxel = _MRC_MODE_BYTES.get(mode)
    if bytes_per_voxel is None:  # pragma: no cover - guarded by mode check
        raise CryosparcImportError(f"Unsupported MRC mode {mode} in {role}.")
    minimum_size = 1024 + nsymbt + nx * ny * nz * bytes_per_voxel
    if after.size < minimum_size:
        raise CryosparcImportError(
            f"{role} is truncated: expected at least {minimum_size} bytes, got {after.size}."
        )
    return (
        {
            "nx": nx,
            "ny": ny,
            "nz": nz,
            "mode": mode,
            "ispg": ispg,
            "pixel_size_A": pixel_x,
        },
        after,
    )


def _safe_stack_name(source_relative: str) -> str:
    source = PurePosixPath(source_relative)
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", source.stem).strip("._-") or "particles"
    stem = stem[:64]
    suffix = hashlib.sha256(source_relative.encode("utf-8")).hexdigest()[:16]
    return f"{stem}-{suffix}.mrcs"


def _safe_micrograph_name(source_relative: str) -> str:
    source = PurePosixPath(source_relative)
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", source.stem).strip("._-") or "micrograph"
    stem = stem[:64]
    suffix = hashlib.sha256(source_relative.encode("utf-8")).hexdigest()[:16]
    return f"{stem}-{suffix}{source.suffix.lower()}"


def _pyem_capability(settings: Settings) -> _PyemCapability:
    issues: list[str] = []
    python: str | None = None
    script: str | None = None
    script_sha256: str | None = None
    pyem_root: str | None = None

    if not settings.pyem_python:
        issues.append("RELION_PYEM_PYTHON is not configured")
    else:
        configured_python = Path(settings.pyem_python)
        try:
            resolved_python = configured_python.resolve(strict=True)
        except (FileNotFoundError, OSError, RuntimeError):
            issues.append("RELION_PYEM_PYTHON is unavailable")
        else:
            if not resolved_python.is_file() or not os.access(resolved_python, os.X_OK):
                issues.append("RELION_PYEM_PYTHON is not an executable regular file")
            else:
                python = str(resolved_python)

    if not settings.csparc2star_script:
        issues.append("RELION_CSPARC2STAR is not configured")
    else:
        configured_script = Path(settings.csparc2star_script)
        if configured_script.is_symlink():
            issues.append("RELION_CSPARC2STAR may not be a symbolic link")
        else:
            try:
                resolved_script = configured_script.resolve(strict=True)
            except (FileNotFoundError, OSError, RuntimeError):
                issues.append("RELION_CSPARC2STAR is unavailable")
            else:
                if not resolved_script.is_file() or not os.access(
                    resolved_script, os.R_OK
                ):
                    issues.append("RELION_CSPARC2STAR is not a readable regular file")
                else:
                    script = str(resolved_script)
                    script_sha256 = _sha256(resolved_script)
                    pyem_root = str(resolved_script.parent)

    return _PyemCapability(
        configured_python=settings.pyem_python,
        configured_script=settings.csparc2star_script,
        python=python,
        script=script,
        script_sha256=script_sha256,
        pyem_root=pyem_root,
        issues=tuple(issues),
    )


def cryosparc_import_environment(settings: Settings) -> dict[str, Any]:
    capability = _pyem_capability(settings)
    live_projects = settings.cryosparc_live_projects
    configured_roots_available = bool(settings.cryosparc_import_roots)
    if live_projects is not None:
        authorization_available = bool(live_projects)
        authorization_policy = "live_cryosparc_project_inventory"
    else:
        authorization_available = configured_roots_available
        authorization_policy = (
            "configured_roots" if configured_roots_available else "unavailable"
        )
    return {
        "approved_source_roots": [
            str(path) for path in settings.cryosparc_import_roots
        ],
        "source_authorization": {
            "live_project_inventory_available": live_projects is not None,
            "live_project_count": (
                len(live_projects) if live_projects is not None else None
            ),
            "configured_roots_available": configured_roots_available,
            "policy": authorization_policy,
        },
        "configured_source_roots_required_only_for_standalone": True,
        "copy_only": True,
        "output_layout": "External/CryoSPARC/<export_name>/",
        "class3d_ctf_required": True,
        "project_initialization_is_explicit": True,
        "pyem": capability.public(),
        "ready": capability.available and authorization_available,
    }


def _project_pipeline_name(settings: Settings, project: Path) -> str:
    metadata = project / ".relion-mcp/project.json"
    if not metadata.exists():
        return settings.pipeline_name
    if metadata.is_symlink():
        raise CryosparcImportError(
            "RELION project metadata may not be a symbolic link."
        )
    try:
        metadata_stat = metadata.stat()
        if not stat.S_ISREG(metadata_stat.st_mode) or metadata_stat.st_size > 64 * 1024:
            raise CryosparcImportError(
                "RELION project metadata must be a small regular JSON file."
            )
        record = json.loads(metadata.read_text(encoding="utf-8"))
    except CryosparcImportError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CryosparcImportError(
            f"Could not read RELION project metadata: {metadata}: {exc}"
        ) from exc
    value = record.get("pipeline_name") if isinstance(record, dict) else None
    if not isinstance(value, str):
        raise CryosparcImportError(
            "RELION project metadata is missing a string pipeline_name."
        )
    try:
        checked = safe_alias(value)
    except CatalogError as exc:
        raise CryosparcImportError(
            f"RELION project metadata contains an unsafe pipeline_name: {exc}"
        ) from exc
    if checked is None:  # pragma: no cover - value is a string
        raise CryosparcImportError("RELION project metadata has no pipeline_name.")
    return checked


def _project_state(
    settings: Settings,
    project: Path,
) -> tuple[bool, bool, Path, Path, str]:
    exists = project.exists()
    if exists and not project.is_dir():
        raise CryosparcImportError(
            f"RELION project target is not a directory: {project}"
        )
    pipeline_name = (
        _project_pipeline_name(settings, project) if exists else settings.pipeline_name
    )
    marker = project / ".gui_projectdir"
    pipeline = project / f"{pipeline_name}_pipeline.star"
    marker_ok = marker.is_file() and not marker.is_symlink()
    pipeline_ok = pipeline.is_file() and not pipeline.is_symlink()
    initialized = exists and marker_ok and pipeline_ok
    return exists, initialized, marker, pipeline, pipeline_name


def _assert_separate_projects(project: Path, source_project: Path) -> None:
    if (
        project == source_project
        or project in source_project.parents
        or source_project in project.parents
    ):
        raise CryosparcImportError(
            "RELION project_dir and CryoSPARC source_project_dir must be separate trees."
        )


def _prepare(
    settings: Settings,
    project_dir: str,
    source_project_dir: str,
    particles_cs: str,
    passthrough_cs: list[str] | None,
    export_name: str,
) -> _PreparedImport:
    project = settings.validate_project_path(project_dir, may_create=True)
    try:
        checked_export_name = safe_alias(export_name)
    except CatalogError as exc:
        raise CryosparcImportError(f"Invalid export_name: {exc}") from exc
    if checked_export_name is None:
        raise CryosparcImportError("export_name must be provided.")
    if checked_export_name in {".", ".."}:
        raise CryosparcImportError("export_name may not be '.' or '..'.")
    if passthrough_cs is None:
        ordered_passthroughs: list[str] = []
    elif isinstance(passthrough_cs, list) and all(
        isinstance(value, str) for value in passthrough_cs
    ):
        ordered_passthroughs = list(passthrough_cs)
    else:
        raise CryosparcImportError(
            "passthrough_cs must be an ordered list of relative paths."
        )
    if len({particles_cs, *ordered_passthroughs}) != 1 + len(ordered_passthroughs):
        raise CryosparcImportError("CryoSPARC input paths must not be repeated.")

    source_project = settings.validate_cryosparc_project_path(source_project_dir)
    live_project = next(
        (
            project
            for project in (settings.cryosparc_live_projects or ())
            if project.project_dir == source_project
        ),
        None,
    )
    source_authorization = {
        "method": (
            "live_cryosparc_project_inventory"
            if settings.cryosparc_live_projects is not None
            else "configured_source_roots"
        ),
        "project_uid": live_project.uid if live_project is not None else None,
        "source_project_dir": str(source_project),
    }
    _assert_separate_projects(project, source_project)

    primary = _inspect_cs_file(source_project, particles_cs, role="particles_cs")
    passthroughs = tuple(
        _inspect_cs_file(source_project, path, role=f"passthrough_cs[{index}]")
        for index, path in enumerate(ordered_passthroughs)
    )
    inputs = (primary, *passthroughs)
    for passthrough in passthroughs:
        fields_new_to_primary = set(passthrough.fields).difference(
            {"uid", *primary.fields}
        )
        if fields_new_to_primary:
            _indices_for_primary(primary.uids, passthrough)

    effective_fields = set().union(*(set(item.fields) for item in inputs))
    missing_ctf = [
        field for field in _CLASS3D_CTF_FIELDS if field not in effective_fields
    ]
    if not ({"ctf/amp_contrast", "ctf/ac"} & effective_fields):
        missing_ctf.append("ctf/amp_contrast (or ctf/ac)")
    if missing_ctf:
        raise CryosparcImportError(
            "Class3D particle import requires CTF metadata; missing fields: "
            + ", ".join(missing_ctf)
        )

    paths = _provider_values(primary, inputs, "blob/path")
    micrograph_paths = (
        _provider_values(primary, inputs, "location/micrograph_path")
        if "location/micrograph_path" in effective_fields
        else None
    )
    indices = _provider_values(primary, inputs, "blob/idx")
    shapes = _provider_values(primary, inputs, "blob/shape")
    pixel_sizes = _numeric_source_values(
        primary,
        inputs,
        "blob/psize_A",
        positive=True,
    )
    _numeric_source_values(
        primary,
        inputs,
        "ctf/df1_A",
        positive=True,
    )
    _numeric_source_values(
        primary,
        inputs,
        "ctf/df2_A",
        positive=True,
    )
    _numeric_source_values(primary, inputs, "ctf/df_angle_rad")
    voltages = _numeric_source_values(
        primary,
        inputs,
        "ctf/accel_kv",
        positive=True,
    )
    spherical_aberrations = _numeric_source_values(
        primary,
        inputs,
        "ctf/cs_mm",
        positive=True,
    )
    amplitude_fields = [
        field for field in ("ctf/amp_contrast", "ctf/ac") if field in effective_fields
    ]
    amplitude_values = [
        _numeric_source_values(
            primary,
            inputs,
            field,
            minimum=0,
            maximum=1,
        )
        for field in amplitude_fields
    ]
    amplitudes = amplitude_values[0]
    if any(
        not np.allclose(
            amplitudes,
            candidate,
            rtol=1e-6,
            atol=1e-6,
        )
        for candidate in amplitude_values[1:]
    ):
        raise CryosparcImportError(
            "ctf/amp_contrast and ctf/ac contain conflicting values."
        )
    if "ctf/exp_group_id" in effective_fields:
        raw_optics_groups = _provider_values(
            primary,
            inputs,
            "ctf/exp_group_id",
        )
        if not np.issubdtype(raw_optics_groups.dtype, np.integer):
            raise CryosparcImportError(
                "ctf/exp_group_id must contain non-negative integer values."
            )
        if np.issubdtype(raw_optics_groups.dtype, np.signedinteger) and np.any(
            raw_optics_groups < 0
        ):
            raise CryosparcImportError(
                "ctf/exp_group_id must contain non-negative integer values."
            )
        maximum_group = np.iinfo(np.int64).max - 1
        if np.any(raw_optics_groups > maximum_group):
            raise CryosparcImportError("ctf/exp_group_id values are too large.")
        # CryoSPARC stores zero-based experiment groups. pyem deliberately adds
        # one when mapping them to RELION's positive, one-based optics groups.
        optics_groups = np.asarray(raw_optics_groups, dtype=np.int64) + 1
    else:
        optics_groups = np.ones(primary.row_count, dtype=np.int64)
    if not np.issubdtype(indices.dtype, np.integer):
        raise CryosparcImportError(
            "blob/idx must contain integer zero-based stack indices."
        )
    if shapes.ndim != 2 or shapes.shape != (primary.row_count, 2):
        raise CryosparcImportError(
            "blob/shape must contain exactly two dimensions per particle."
        )

    stack_usage: dict[str, dict[str, Any]] = {}
    micrograph_usage: dict[str, dict[str, Any]] = {}
    expected_micrograph_source_by_image_reference: dict[str, str] = {}
    expected_image_name_sum = 0
    expected_group_image_sums: dict[int, int] = {}
    source_optics_groups: dict[int, tuple[int, float, float, float, float]] = {}
    digest_modulus = 1 << 256
    for row in range(primary.row_count):
        raw_path = _decode_cs_path(paths[row], role="blob/path")
        source_relative = _safe_source_relative(
            raw_path,
            role="blob/path",
            allow_external_marker=True,
        )
        index = int(indices[row])
        if index < 0:
            raise CryosparcImportError(
                "blob/idx values must be zero-based and non-negative."
            )
        try:
            shape = (int(shapes[row][0]), int(shapes[row][1]))
        except (TypeError, ValueError, OverflowError) as exc:
            raise CryosparcImportError(
                "blob/shape values must be integer image dimensions."
            ) from exc
        if shape[0] <= 0 or shape[1] <= 0 or shape[0] != shape[1]:
            raise CryosparcImportError(
                f"blob/shape must describe square non-empty images; got {shape}."
            )
        try:
            pixel_size = float(pixel_sizes[row])
        except (TypeError, ValueError, OverflowError) as exc:
            raise CryosparcImportError("blob/psize_A values must be numeric.") from exc
        if not math.isfinite(pixel_size) or pixel_size <= 0:
            raise CryosparcImportError(
                "blob/psize_A values must be positive and finite."
            )
        usage = stack_usage.setdefault(
            source_relative,
            {
                "raw_paths": set(),
                "first_index": index,
                "last_index": index,
                "count": 0,
                "indices": [],
                "shape": shape,
                "pixel_size_A": pixel_size,
            },
        )
        if usage["shape"] != shape:
            raise CryosparcImportError(
                f"Particles referencing {source_relative} declare inconsistent blob/shape values."
            )
        if not math.isclose(
            float(usage["pixel_size_A"]), pixel_size, rel_tol=1e-6, abs_tol=1e-6
        ):
            raise CryosparcImportError(
                f"Particles referencing {source_relative} declare inconsistent blob/psize_A values."
            )
        usage["raw_paths"].add(raw_path)
        usage["first_index"] = min(int(usage["first_index"]), index)
        usage["last_index"] = max(int(usage["last_index"]), index)
        usage["count"] = int(usage["count"]) + 1
        usage["indices"].append(index)
        source_micrograph_relative: str | None = None
        if micrograph_paths is not None:
            raw_micrograph_path = _decode_cs_path(
                micrograph_paths[row],
                role="location/micrograph_path",
            )
            source_micrograph_relative = _safe_source_relative(
                raw_micrograph_path,
                role="location/micrograph_path",
                allow_external_marker=True,
            )
            micrograph = micrograph_usage.setdefault(
                source_micrograph_relative,
                {"raw_paths": set(), "count": 0},
            )
            micrograph["raw_paths"].add(raw_micrograph_path)
            micrograph["count"] = int(micrograph["count"]) + 1
        reference = f"{index + 1:06d}@{raw_path}"
        if source_micrograph_relative is not None:
            expected_micrograph_source_by_image_reference[reference] = (
                source_micrograph_relative
            )
        optics_group = int(optics_groups[row])
        source_optics = (
            shape[0],
            pixel_size,
            float(voltages[row]),
            float(spherical_aberrations[row]),
            float(amplitudes[row]),
        )
        prior_optics = source_optics_groups.setdefault(optics_group, source_optics)
        if prior_optics[0] != source_optics[0] or any(
            not math.isclose(left, right, rel_tol=1e-6, abs_tol=1e-6)
            for left, right in zip(prior_optics[1:], source_optics[1:])
        ):
            raise CryosparcImportError(
                "Particles normalized to RELION optics group "
                f"{optics_group} (CryoSPARC ctf/exp_group_id {optics_group - 1}) "
                "contain inconsistent box, pixel, voltage, Cs, or "
                "amplitude-contrast metadata."
            )
        reference_digest = int.from_bytes(
            hashlib.sha256(reference.encode("utf-8")).digest(),
            "big",
        )
        expected_image_name_sum = (
            expected_image_name_sum + reference_digest
        ) % digest_modulus
        expected_group_image_sums[optics_group] = (
            expected_group_image_sums.get(optics_group, 0) + reference_digest
        ) % digest_modulus

    bundle_relative = (OUTPUT_BASE / checked_export_name).as_posix()
    particles_star_relative = (
        PurePosixPath(bundle_relative) / "particles.star"
    ).as_posix()
    manifest_relative = (
        PurePosixPath(bundle_relative) / "conversion_manifest.json"
    ).as_posix()
    stacks: list[_StackBinding] = []
    pyem_path_map: dict[str, str] = {}
    destination_names: set[str] = set()
    for source_relative, usage in sorted(stack_usage.items()):
        ordered_indices = sorted(int(value) for value in usage.pop("indices"))
        if any(
            current == previous
            for previous, current in pairwise(ordered_indices)
        ):
            raise CryosparcImportError(
                "CryoSPARC particles contain duplicate stack image references for "
                f"{source_relative}; duplicate particles are not safe Class3D input."
            )
        source_path = _resolve_regular_source(
            source_project,
            source_relative,
            role="particle stack",
            suffixes=(".mrc", ".mrcs"),
        )
        header, identity = _mrc_stack_info(source_path, role="particle stack")
        if int(usage["last_index"]) >= int(header["nz"]):
            raise CryosparcImportError(
                f"blob/idx {usage['last_index']} is outside stack {source_relative} "
                f"with {header['nz']} images."
            )
        declared_shape = tuple(int(value) for value in usage["shape"])
        if declared_shape != (int(header["ny"]), int(header["nx"])):
            raise CryosparcImportError(
                f"blob/shape {declared_shape} does not match MRC stack dimensions "
                f"{(header['ny'], header['nx'])} for {source_relative}."
            )
        if not math.isclose(
            float(usage["pixel_size_A"]),
            float(header["pixel_size_A"]),
            rel_tol=1e-5,
            abs_tol=1e-5,
        ):
            raise CryosparcImportError(
                f"blob/psize_A {usage['pixel_size_A']} does not match MRC header pixel "
                f"size {header['pixel_size_A']} for {source_relative}."
            )
        destination_name = _safe_stack_name(source_relative)
        if (
            destination_name in destination_names
        ):  # pragma: no cover - SHA collision bound
            raise CryosparcImportError(
                "Two source stacks produced the same destination name."
            )
        destination_names.add(destination_name)
        destination_relative = (
            PurePosixPath(bundle_relative) / "stacks" / destination_name
        ).as_posix()
        raw_paths = tuple(sorted(str(value) for value in usage["raw_paths"]))
        for raw_path in raw_paths:
            pyem_path_map[raw_path] = destination_relative
        stacks.append(
            _StackBinding(
                source_relative=source_relative,
                source_path=source_path,
                pyem_paths=raw_paths,
                destination_relative=destination_relative,
                identity=identity,
                nx=int(header["nx"]),
                ny=int(header["ny"]),
                nz=int(header["nz"]),
                mode=int(header["mode"]),
                pixel_size_A=float(header["pixel_size_A"]),
                first_index=int(usage["first_index"]),
                last_index=int(usage["last_index"]),
                referenced_particle_count=int(usage["count"]),
            )
        )

    micrographs: list[_MicrographBinding] = []
    pyem_micrograph_path_map: dict[str, str] = {}
    expected_micrograph_reference_counts: dict[str, int] = {}
    micrograph_destination_names: set[str] = set()
    for source_relative, usage in sorted(micrograph_usage.items()):
        source_path = _resolve_regular_source(
            source_project,
            source_relative,
            role="micrograph",
            suffixes=(".mrc", ".mrcs"),
        )
        identity = _identity(source_path)
        destination_name = _safe_micrograph_name(source_relative)
        if destination_name in micrograph_destination_names:
            raise CryosparcImportError(
                "Two source micrographs produced the same destination name."
            )
        micrograph_destination_names.add(destination_name)
        destination_relative = (
            PurePosixPath(bundle_relative) / "micrographs" / destination_name
        ).as_posix()
        raw_paths = tuple(sorted(str(value) for value in usage["raw_paths"]))
        for pyem_path in {*raw_paths, source_relative}:
            prior = pyem_micrograph_path_map.setdefault(
                pyem_path,
                destination_relative,
            )
            if prior != destination_relative:  # pragma: no cover - normalized collision
                raise CryosparcImportError(
                    f"Conflicting micrograph mapping for pyem path {pyem_path}."
                )
        reference_count = int(usage["count"])
        expected_micrograph_reference_counts[destination_relative] = reference_count
        micrographs.append(
            _MicrographBinding(
                source_relative=source_relative,
                source_path=source_path,
                pyem_paths=raw_paths,
                destination_relative=destination_relative,
                identity=identity,
                referenced_particle_count=reference_count,
            )
        )
    micrograph_destination_by_source = {
        micrograph.source_relative: micrograph.destination_relative
        for micrograph in micrographs
    }
    expected_image_micrograph_sum = 0
    for (
        image_reference,
        source_relative,
    ) in expected_micrograph_source_by_image_reference.items():
        destination_relative = micrograph_destination_by_source[source_relative]
        assignment = f"{image_reference}\0{destination_relative}"
        expected_image_micrograph_sum = (
            expected_image_micrograph_sum
            + int.from_bytes(
                hashlib.sha256(assignment.encode("utf-8")).digest(),
                "big",
            )
        ) % digest_modulus
    expected_image_micrograph_multiset_digest = (
        f"{expected_image_micrograph_sum:064x}" if micrographs else None
    )

    project_exists, project_initialized, marker, pipeline, pipeline_name = (
        _project_state(
            settings,
            project,
        )
    )
    if project_exists:
        cursor = project
        for part in OUTPUT_BASE.parts:
            cursor /= part
            if os.path.lexists(cursor):
                if cursor.is_symlink():
                    raise CryosparcImportError(
                        f"CryoSPARC import output parent may not be a symbolic link: {cursor}"
                    )
                if not cursor.is_dir():
                    raise CryosparcImportError(
                        f"CryoSPARC import output parent is not a directory: {cursor}"
                    )
        destination = project / bundle_relative
        if os.path.lexists(destination):
            raise CryosparcImportError(
                f"CryoSPARC import bundle already exists and will not be replaced: {destination}"
            )

    capability = _pyem_capability(settings)
    command_template = [
        capability.python or "<RELION_PYEM_PYTHON>",
        capability.script or "<RELION_CSPARC2STAR>",
        "inputs/000-particles.cs",
        *[f"inputs/{index:03d}-passthrough.cs" for index in range(1, len(inputs))],
        "raw-particles.star",
        "--loglevel",
        "INFO",
    ]
    alignment_warnings: list[str] = []
    pyem_alignment_fields = {
        "alignments3D/pose",
        "alignments3D/shift",
        "alignments3D/class_posterior",
    }
    if not pyem_alignment_fields.issubset(effective_fields):
        alignment_warnings.append(
            "No pyem-mappable CryoSPARC 3D pose/shift/class-posterior set was found. "
            "The import remains valid for Class3D, but it will not carry a complete "
            "prior 3D alignment."
        )
    total_stack_bytes = sum(stack.identity.size for stack in stacks)
    total_micrograph_bytes = sum(micrograph.identity.size for micrograph in micrographs)
    import_handoff = {
        "render_tool": "relion_render_job",
        "run_tool": "relion_run_job",
        "operation": "import_particles",
        "inputs": {"particles": particles_star_relative},
        "params": {"import_type": "Particles STAR file (.star)"},
        "pipeline_name": pipeline_name,
    }
    plan: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "planned",
        "purpose": "RELION Class3D particle input",
        "project_dir": str(project),
        "source_project_dir": str(source_project),
        "source_authorization": source_authorization,
        "project_exists": project_exists,
        "project_initialized": project_initialized,
        "bootstrap_required": not project_initialized,
        "bootstrap": {
            "tool": "relion_initialize_project",
            "arguments": {
                "project_dir": str(project),
                "pipeline_name": pipeline_name,
                "confirm": True,
            },
            "marker": str(marker),
            "pipeline_file": str(pipeline),
        },
        "execution_ready": project_initialized and capability.available,
        "inputs": [item.public() for item in inputs],
        "passthrough_order": [item.relative_path for item in passthroughs],
        "particle_count": primary.row_count,
        "stacks": [stack.public() for stack in stacks],
        "stack_count": len(stacks),
        "stack_bytes_to_copy": total_stack_bytes,
        "micrographs": [micrograph.public() for micrograph in micrographs],
        "micrograph_count": len(micrographs),
        "micrograph_bytes_to_copy": total_micrograph_bytes,
        "total_bytes_to_copy": total_stack_bytes + total_micrograph_bytes,
        "copy_mode": "copy",
        "micrograph_path_policy": {
            "source_field": "location/micrograph_path",
            "pyem_column": "rlnMicrographName",
            "mode": "copy_and_rewrite" if micrographs else "not_present",
            "coverage_required": bool(micrographs),
            "output_paths_project_relative": True,
        },
        "outputs": {
            "bundle": bundle_relative,
            "particles_star": particles_star_relative,
            "micrographs": [
                micrograph.destination_relative for micrograph in micrographs
            ],
            "manifest": manifest_relative,
        },
        "pyem": {
            **capability.public(),
            "command_template": command_template,
            "working_directory": "<private same-filesystem staging directory>",
            "forbidden_options": [
                "--relion2",
                "--stack-path",
                "--class",
                "--minphic",
                "--transform",
                "--swapxy",
                "--invertx",
                "--inverty",
            ],
        },
        "class3d": {
            "ctf_metadata_required": True,
            "source_optics_groups": [
                {
                    "cryosparc_exp_group_id": group - 1,
                    "rlnOpticsGroup": group,
                    "rlnImageSize": values[0],
                    "rlnImagePixelSize": values[1],
                    "rlnVoltage": values[2],
                    "rlnSphericalAberration": values[3],
                    "rlnAmplitudeContrast": values[4],
                }
                for group, values in sorted(source_optics_groups.items())
            ],
            "complete_3d_alignment_present": not alignment_warnings,
            "warnings": alignment_warnings,
        },
        "publication": {
            "strategy": "private_staging_then_no_replace_directory_publish",
            "methods": [
                "renameat2_noreplace",
                "exclusive_destination_reservation_then_rename",
            ],
            "existing_bundle_refused": True,
            "source_stacks_copied": True,
            "source_micrographs_copied": bool(micrographs),
            "source_mutation_allowed": False,
            "relion_pipeline_job_registered": False,
        },
        "import_job_handoff": import_handoff,
    }
    digest_payload = json.dumps(plan, sort_keys=True, separators=(",", ":"))
    plan["plan_sha256"] = hashlib.sha256(digest_payload.encode("utf-8")).hexdigest()
    if settings.validate_cryosparc_project_path(source_project_dir) != source_project:
        raise CryosparcImportError(
            "CryoSPARC source project changed while the import was being prepared."
        )
    return _PreparedImport(
        project=project,
        source_project=source_project,
        inputs=inputs,
        stacks=tuple(stacks),
        micrographs=tuple(micrographs),
        pyem_path_map=pyem_path_map,
        pyem_micrograph_path_map=pyem_micrograph_path_map,
        expected_micrograph_reference_counts=expected_micrograph_reference_counts,
        expected_image_micrograph_multiset_digest=(
            expected_image_micrograph_multiset_digest
        ),
        expected_image_name_multiset_digest=f"{expected_image_name_sum:064x}",
        expected_group_image_digests={
            group: f"{value:064x}"
            for group, value in sorted(expected_group_image_sums.items())
        },
        source_optics_groups=source_optics_groups,
        particle_count=primary.row_count,
        export_name=checked_export_name,
        bundle_relative=bundle_relative,
        particles_star_relative=particles_star_relative,
        manifest_relative=manifest_relative,
        bootstrap_required=not project_initialized,
        project_initialized=project_initialized,
        pipeline_name=pipeline_name,
        capability=capability,
        alignment_warnings=tuple(alignment_warnings),
        plan=plan,
    )


def render_cryosparc_particle_import(
    settings: Settings,
    project_dir: str,
    source_project_dir: str,
    particles_cs: str,
    passthrough_cs: list[str] | None = None,
    export_name: str = "particles",
) -> dict[str, Any]:
    return _prepare(
        settings,
        project_dir,
        source_project_dir,
        particles_cs,
        passthrough_cs,
        export_name,
    ).plan


def _open_or_create_project_directory(
    project: Path,
    relative_directory: PurePosixPath,
    *,
    role: str,
) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    opened: list[int] = []
    try:
        opened.append(os.open(project, flags))
        if Path(f"/proc/self/fd/{opened[0]}").resolve(strict=True) != project:
            raise CryosparcImportError(
                f"RELION project changed while preparing {role}."
            )
        for part in relative_directory.parts:
            try:
                os.mkdir(part, mode=0o755, dir_fd=opened[-1])
            except FileExistsError:
                pass
            try:
                child = os.open(part, flags, dir_fd=opened[-1])
            except OSError as exc:
                raise CryosparcImportError(
                    f"{role} component is not a stable real directory: {part}"
                ) from exc
            opened.append(child)
        expected = project.joinpath(*relative_directory.parts)
        actual = Path(f"/proc/self/fd/{opened[-1]}").resolve(strict=True)
        if actual != expected:
            raise CryosparcImportError(f"{role} changed while it was created.")
        return opened.pop()
    except OSError as exc:
        raise CryosparcImportError(f"Could not prepare {role}: {exc}") from exc
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


def _copy_regular_file(
    source: Path,
    destination: Path,
    *,
    expected_identity: _FileIdentity,
) -> tuple[str, int]:
    read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    source_fd: int | None = None
    destination_fd: int | None = None
    digest = hashlib.sha256()
    copied = 0
    try:
        source_fd = os.open(source, read_flags)
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode):
            raise CryosparcImportError(
                f"Source changed into a non-regular file: {source}"
            )
        if _FileIdentity.from_stat(before) != expected_identity:
            raise CryosparcImportError(
                f"Source changed after rendering the import plan: {source}"
            )
        destination_fd = os.open(destination, write_flags, 0o600)
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                view = view[written:]
            copied += len(chunk)
        os.fsync(destination_fd)
        after = os.fstat(source_fd)
        if _FileIdentity.from_stat(after) != expected_identity:
            raise CryosparcImportError(f"Source changed while it was copied: {source}")
    except OSError as exc:
        raise CryosparcImportError(
            f"Could not copy source file {source}: {exc}"
        ) from exc
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        if source_fd is not None:
            os.close(source_fd)
    if copied != expected_identity.size:
        raise CryosparcImportError(
            f"Copied byte count changed for {source}: expected {expected_identity.size}, got {copied}."
        )
    return digest.hexdigest(), copied


def _star_token(value: Any) -> str:
    token = str(value)
    if not token or any(
        ord(character) < 32 or ord(character) == 127 for character in token
    ):
        raise CryosparcImportError(
            "pyem emitted an empty or control-containing STAR value."
        )
    if not any(character.isspace() for character in token) and token[0] not in {
        "#",
        "'",
        '"',
    }:
        return token
    if "'" not in token:
        return f"'{token}'"
    if '"' not in token:
        return f'"{token}"'
    raise CryosparcImportError(
        "pyem emitted a STAR value containing both quote styles."
    )


def _write_star_blocks(
    path: Path, blocks: tuple[tuple[str, dict[str, Any]], ...]
) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        for block_name, block in blocks:
            columns = list(block)
            values = [block[column] for column in columns]
            if not columns or any(
                not isinstance(column_values, list) for column_values in values
            ):
                raise CryosparcImportError(
                    f"data_{block_name} must contain exactly one loop."
                )
            row_counts = {len(column_values) for column_values in values}
            if len(row_counts) != 1:
                raise CryosparcImportError(
                    f"data_{block_name} columns have unequal lengths."
                )
            row_count = next(iter(row_counts))
            handle.write(f"\ndata_{block_name}\n\nloop_\n")
            for index, column in enumerate(columns, start=1):
                handle.write(f"_{column} #{index}\n")
            for row in range(row_count):
                handle.write(
                    " ".join(
                        _star_token(column_values[row]) for column_values in values
                    )
                    + "\n"
                )
            handle.write("\n")


def _finite_column(
    block: dict[str, Any], column: str, *, positive: bool = False
) -> None:
    values = block[column]
    if not isinstance(values, list):
        raise CryosparcImportError(f"STAR column {column} is not a loop column.")
    for raw in values:
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise CryosparcImportError(
                f"STAR column {column} must be numeric."
            ) from exc
        if not math.isfinite(value) or (positive and value <= 0):
            qualifier = "positive finite" if positive else "finite"
            raise CryosparcImportError(
                f"STAR column {column} must contain {qualifier} values."
            )


def _positive_integer_column(block: dict[str, Any], column: str) -> list[int]:
    values = block[column]
    if not isinstance(values, list):
        raise CryosparcImportError(f"STAR column {column} is not a loop column.")
    result: list[int] = []
    for raw in values:
        try:
            numeric = float(raw)
        except (TypeError, ValueError) as exc:
            raise CryosparcImportError(
                f"STAR column {column} must contain positive integers."
            ) from exc
        if not math.isfinite(numeric) or numeric < 1 or not numeric.is_integer():
            raise CryosparcImportError(
                f"STAR column {column} must contain positive integers."
            )
        result.append(int(numeric))
    return result


def _positive_integer_token(raw: str, column: str) -> int:
    try:
        numeric = float(raw)
    except (TypeError, ValueError) as exc:
        raise CryosparcImportError(
            f"STAR column {column} must contain positive integers."
        ) from exc
    if not math.isfinite(numeric) or numeric < 1 or not numeric.is_integer():
        raise CryosparcImportError(
            f"STAR column {column} must contain positive integers."
        )
    return int(numeric)


def _positive_finite_token(raw: str, column: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise CryosparcImportError(
            f"STAR column {column} must contain positive finite values."
        ) from exc
    if not math.isfinite(value) or value <= 0:
        raise CryosparcImportError(
            f"STAR column {column} must contain positive finite values."
        )
    return value


def _rewrite_particle_rows(
    raw_star: Path,
    output_star: Path,
    prepared: _PreparedImport,
    columns: tuple[str, ...],
    optics_rows: dict[int, tuple[int, float]],
) -> dict[str, int]:
    image_index = columns.index("rlnImageName")
    micrograph_index = (
        columns.index("rlnMicrographName") if "rlnMicrographName" in columns else None
    )
    optics_index = columns.index("rlnOpticsGroup")
    defocus_u_index = columns.index("rlnDefocusU")
    defocus_v_index = columns.index("rlnDefocusV")
    random_subset_index = (
        columns.index("rlnRandomSubset") if "rlnRandomSubset" in columns else None
    )
    reference_pattern = re.compile(r"^([0-9]+)@(.+)$")
    stacks_by_destination = {
        stack.destination_relative: stack for stack in prepared.stacks
    }
    observed_group_image_sums: dict[int, int] = {}
    observed_micrograph_reference_counts: dict[str, int] = {}
    observed_image_micrograph_sum = 0
    digest_modulus = 1 << 256

    try:
        source = raw_star.open("r", encoding="utf-8", errors="strict")
    except OSError as exc:
        raise CryosparcImportError(f"Could not reopen pyem STAR output: {exc}") from exc
    try:
        iterator = iter(source)
        for line in iterator:
            if _is_native_structural_data_line(line, "data_particles"):
                break
        else:  # pragma: no cover - the preceding streaming summary checks this
            raise CryosparcImportError("pyem output lost its data_particles block.")

        for line in iterator:
            if _relion_simplify_line(line) == "loop_":
                break
        else:  # pragma: no cover - the preceding streaming summary checks this
            raise CryosparcImportError("pyem output lost its particle loop.")

        observed_columns: list[str] = []
        first_row: str | None = None
        for line in iterator:
            simplified = _relion_simplify_line(line)
            if not simplified or simplified.startswith(("#", ";")):
                continue
            if simplified.startswith("_"):
                observed_columns.append(simplified.split(" ", 1)[0][1:])
                continue
            first_row = line
            break
        if tuple(observed_columns) != columns:
            raise CryosparcImportError(
                "pyem particle columns changed between validation and rewrite."
            )

        try:
            destination = output_star.open("a", encoding="utf-8", newline="\n")
        except OSError as exc:
            raise CryosparcImportError(
                f"Could not create rewritten particle STAR: {exc}"
            ) from exc
        with destination:
            destination.write("\ndata_particles\n\nloop_\n")
            for index, column in enumerate(columns, start=1):
                destination.write(f"_{column} #{index}\n")
            row_count = 0
            row_lines = [first_row] if first_row is not None else []
            for line in chain(row_lines, iterator):
                simplified = _relion_simplify_line(line)
                if not simplified:
                    if row_count:
                        break
                    continue
                row = _tokens(line)
                if len(row) != len(columns):
                    raise CryosparcImportError(
                        "pyem particle row changed between validation and rewrite."
                    )
                raw_reference = row[image_index]
                match = reference_pattern.fullmatch(raw_reference)
                if match is None:
                    raise CryosparcImportError(
                        f"Invalid pyem rlnImageName reference: {raw_reference!r}."
                    )
                image_number = _positive_integer_token(
                    match.group(1),
                    "rlnImageName stack index",
                )
                source_path = match.group(2)
                destination_relative = prepared.pyem_path_map.get(source_path)
                if destination_relative is None:
                    raise CryosparcImportError(
                        f"pyem emitted an unplanned particle-stack path: {source_path}"
                    )
                stack = stacks_by_destination[destination_relative]
                if image_number > stack.nz:
                    raise CryosparcImportError(
                        f"pyem emitted image {image_number} outside stack {source_path}."
                    )
                group = _positive_integer_token(
                    row[optics_index],
                    "rlnOpticsGroup",
                )
                if group not in optics_rows:
                    raise CryosparcImportError(
                        f"data_particles references undefined optics group {group}."
                    )
                reference_digest = int.from_bytes(
                    hashlib.sha256(raw_reference.encode("utf-8")).digest(),
                    "big",
                )
                observed_group_image_sums[group] = (
                    observed_group_image_sums.get(group, 0) + reference_digest
                ) % digest_modulus
                optics_size, optics_pixel_size = optics_rows[group]
                if optics_size != stack.nx:
                    raise CryosparcImportError(
                        "pyem optics-group image size does not match the referenced MRC "
                        f"stack: group={group}, STAR={optics_size}, MRC={stack.nx}."
                    )
                if not math.isclose(
                    optics_pixel_size,
                    stack.pixel_size_A,
                    rel_tol=1e-5,
                    abs_tol=1e-5,
                ):
                    raise CryosparcImportError(
                        "pyem optics-group pixel size does not match the referenced MRC "
                        f"stack: group={group}, STAR={optics_pixel_size}, "
                        f"MRC={stack.pixel_size_A}."
                    )
                _positive_finite_token(row[defocus_u_index], "rlnDefocusU")
                _positive_finite_token(row[defocus_v_index], "rlnDefocusV")
                if random_subset_index is not None:
                    subset = _positive_integer_token(
                        row[random_subset_index],
                        "rlnRandomSubset",
                    )
                    if subset not in {1, 2}:
                        raise CryosparcImportError(
                            "STAR column rlnRandomSubset may contain only 1 or 2."
                        )
                row[image_index] = f"{image_number:06d}@{destination_relative}"
                if micrograph_index is not None:
                    raw_micrograph_path = row[micrograph_index]
                    micrograph_destination = prepared.pyem_micrograph_path_map.get(
                        raw_micrograph_path
                    )
                    if micrograph_destination is None:
                        raise CryosparcImportError(
                            "pyem emitted an unplanned rlnMicrographName path: "
                            f"{raw_micrograph_path}"
                        )
                    row[micrograph_index] = micrograph_destination
                    observed_micrograph_reference_counts[micrograph_destination] = (
                        observed_micrograph_reference_counts.get(
                            micrograph_destination,
                            0,
                        )
                        + 1
                    )
                    assignment = f"{raw_reference}\0{micrograph_destination}"
                    observed_image_micrograph_sum = (
                        observed_image_micrograph_sum
                        + int.from_bytes(
                            hashlib.sha256(assignment.encode("utf-8")).digest(),
                            "big",
                        )
                    ) % digest_modulus
                destination.write(" ".join(_star_token(value) for value in row) + "\n")
                row_count += 1
            destination.write("\n")
        observed_group_image_digests = {
            group: f"{value:064x}"
            for group, value in sorted(observed_group_image_sums.items())
        }
        if observed_group_image_digests != prepared.expected_group_image_digests:
            raise CryosparcImportError(
                "pyem changed particle assignments among the normalized RELION "
                "optics groups."
            )
        if (
            observed_micrograph_reference_counts
            != prepared.expected_micrograph_reference_counts
        ):
            raise CryosparcImportError(
                "pyem rlnMicrographName references do not provide exact particle "
                "coverage for the CryoSPARC location/micrograph_path values."
            )
        observed_image_micrograph_multiset_digest = (
            f"{observed_image_micrograph_sum:064x}" if prepared.micrographs else None
        )
        if (
            observed_image_micrograph_multiset_digest
            != prepared.expected_image_micrograph_multiset_digest
        ):
            raise CryosparcImportError(
                "pyem changed rlnMicrographName assignments among the particle "
                "image references."
            )
    except UnicodeDecodeError as exc:
        raise CryosparcImportError(
            "pyem STAR output changed into invalid UTF-8."
        ) from exc
    finally:
        source.close()
    return dict(sorted(observed_micrograph_reference_counts.items()))


def _streamed_particle_metadata_digest(summary: Any) -> str:
    payload = (
        f"data_particles\0{summary.particle_count}\0"
        + "\0".join(sorted(summary.columns))
        + f"\0{summary.row_metadata_multiset_digest}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _rewrite_and_validate_star(
    raw_star: Path,
    output_star: Path,
    prepared: _PreparedImport,
) -> dict[str, Any]:
    try:
        optics = parse_star_block(raw_star, "optics", strict_loops=True)
        raw_summary = particle_star_row_summary(
            raw_star,
            required_columns=_PARTICLE_STAR_REQUIRED_COLUMNS,
            track_duplicate_image_names=False,
        )
    except (OSError, StarFormatError) as exc:
        raise CryosparcImportError(
            f"pyem created an invalid modern RELION STAR: {exc}"
        ) from exc
    if optics is None:
        raise CryosparcImportError(
            "pyem output must contain data_optics and data_particles."
        )
    missing_optics = sorted(_OPTICS_STAR_REQUIRED_COLUMNS.difference(optics))
    if missing_optics:
        raise CryosparcImportError(
            "pyem output is not Class3D-ready; missing data_optics columns: "
            + ", ".join(missing_optics)
        )
    if raw_summary.particle_count != prepared.particle_count:
        raise CryosparcImportError(
            "pyem changed the particle count: "
            f"expected {prepared.particle_count}, got {raw_summary.particle_count}."
        )
    if (
        raw_summary.image_name_multiset_digest
        != prepared.expected_image_name_multiset_digest
    ):
        raise CryosparcImportError(
            "pyem output image references do not match the CryoSPARC blob/path and blob/idx "
            "multiset."
        )
    micrograph_column_present = "rlnMicrographName" in raw_summary.columns
    if prepared.micrographs and not micrograph_column_present:
        raise CryosparcImportError(
            "CryoSPARC location/micrograph_path metadata was provided, but pyem did "
            "not emit rlnMicrographName."
        )
    if micrograph_column_present and not prepared.micrographs:
        raise CryosparcImportError(
            "pyem emitted rlnMicrographName without validated CryoSPARC "
            "location/micrograph_path source coverage."
        )

    try:
        relion_metadata_loop_digest(optics, block_name="optics")
    except StarFormatError as exc:
        raise CryosparcImportError(
            f"pyem output contains unsafe RELION metadata: {exc}"
        ) from exc
    for column in (
        "rlnImagePixelSize",
        "rlnImageSize",
        "rlnVoltage",
        "rlnSphericalAberration",
    ):
        _finite_column(optics, column, positive=True)
    _finite_column(optics, "rlnAmplitudeContrast")
    for raw in optics["rlnAmplitudeContrast"]:
        if float(raw) < 0 or float(raw) > 1:
            raise CryosparcImportError(
                "STAR column rlnAmplitudeContrast must stay between zero and one."
            )
    optics_groups = optics["rlnOpticsGroup"]
    if not isinstance(optics_groups, list):
        raise CryosparcImportError("rlnOpticsGroup must be a loop column.")
    normalized_optics_groups = _positive_integer_column(optics, "rlnOpticsGroup")
    defined_groups = set(normalized_optics_groups)
    if len(defined_groups) != len(normalized_optics_groups):
        raise CryosparcImportError(
            "data_optics contains duplicate rlnOpticsGroup values."
        )
    image_sizes = _positive_integer_column(optics, "rlnImageSize")
    image_dimensions = _positive_integer_column(optics, "rlnImageDimensionality")
    if any(value != 2 for value in image_dimensions):
        raise CryosparcImportError(
            "Class3D particle optics must declare rlnImageDimensionality=2."
        )
    image_pixel_sizes = [float(value) for value in optics["rlnImagePixelSize"]]
    optics_rows = {
        group: (image_sizes[row], image_pixel_sizes[row])
        for row, group in enumerate(normalized_optics_groups)
    }
    source_group_ids = set(prepared.source_optics_groups)
    if defined_groups != source_group_ids:
        raise CryosparcImportError(
            "pyem output optics groups do not match the one-based groups normalized "
            "from CryoSPARC ctf/exp_group_id values: "
            f"source={sorted(source_group_ids)}, output={sorted(defined_groups)}."
        )
    voltage_values = [float(value) for value in optics["rlnVoltage"]]
    spherical_aberration_values = [
        float(value) for value in optics["rlnSphericalAberration"]
    ]
    amplitude_contrast_values = [
        float(value) for value in optics["rlnAmplitudeContrast"]
    ]
    output_row_by_group = {
        group: row for row, group in enumerate(normalized_optics_groups)
    }
    for group, source_values in prepared.source_optics_groups.items():
        row = output_row_by_group[group]
        output_values = (
            image_sizes[row],
            image_pixel_sizes[row],
            voltage_values[row],
            spherical_aberration_values[row],
            amplitude_contrast_values[row],
        )
        if output_values[0] != source_values[0] or any(
            not math.isclose(left, right, rel_tol=1e-5, abs_tol=1e-5)
            for left, right in zip(output_values[1:], source_values[1:])
        ):
            raise CryosparcImportError(
                "pyem optics metadata does not match the CryoSPARC source for group "
                f"{group}: source={source_values}, output={output_values}."
            )

    alignment_columns = {"rlnAngleRot", "rlnAngleTilt", "rlnAnglePsi"}
    present_alignment = alignment_columns.intersection(raw_summary.columns)
    warnings = list(prepared.alignment_warnings)
    if present_alignment and present_alignment != alignment_columns:
        raise CryosparcImportError(
            "pyem output contains only part of the RELION 3D Euler-angle triplet."
        )
    origin_columns = {"rlnOriginXAngst", "rlnOriginYAngst"}
    present_origins = origin_columns.intersection(raw_summary.columns)
    if present_origins and present_origins != origin_columns:
        raise CryosparcImportError("pyem output contains only one XY origin column.")
    if present_alignment != alignment_columns and not warnings:
        warnings.append(
            "pyem did not emit a complete RELION 3D Euler-angle triplet. Class3D can "
            "start without alignment priors, so no angles were synthesized."
        )

    _write_star_blocks(output_star, (("optics", optics),))
    micrograph_reference_counts = _rewrite_particle_rows(
        raw_star,
        output_star,
        prepared,
        raw_summary.columns,
        optics_rows,
    )
    try:
        summary = particle_star_row_summary(
            output_star,
            required_columns=_PARTICLE_STAR_REQUIRED_COLUMNS,
            track_duplicate_image_names=False,
        )
        strict_optics = parse_star_block(output_star, "optics", strict_loops=True)
    except (OSError, StarFormatError) as exc:
        raise CryosparcImportError(
            f"Rewritten RELION particle STAR failed validation: {exc}"
        ) from exc
    if strict_optics is None:
        raise CryosparcImportError("Rewritten STAR lost its optics block.")
    return {
        "particle_count": summary.particle_count,
        "particle_columns": list(summary.columns),
        "optics_group_counts": summary.optics_group_counts,
        "optics_metadata_sha256": relion_metadata_loop_digest(
            strict_optics, block_name="optics"
        ),
        "particle_metadata_sha256": _streamed_particle_metadata_digest(summary),
        "micrograph_paths_rewritten": bool(prepared.micrographs),
        "micrograph_count": len(prepared.micrographs),
        "micrograph_reference_counts": micrograph_reference_counts,
        "image_micrograph_multiset_digest": (
            prepared.expected_image_micrograph_multiset_digest
        ),
        "complete_3d_alignment_present": alignment_columns.issubset(summary.columns),
        "warnings": warnings,
    }


def _pyem_environment(
    capability: _PyemCapability,
    staging_path: Path,
) -> tuple[dict[str, str], Path]:
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith(("CRYOSPARC_", "EMCLAW_CRYOSPARC_")):
            environment.pop(name, None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    if capability.pyem_root is None or capability.script_sha256 is None:
        raise CryosparcImportError("pyem capability is incomplete.")
    environment["PYTHONPATH"] = capability.pyem_root
    cache = staging_path / ".numba-cache"
    try:
        cache.mkdir(mode=0o700)
    except OSError as exc:
        raise CryosparcImportError(
            f"Could not create the private pyem Numba cache: {exc}"
        ) from exc
    if (
        cache.is_symlink()
        or not cache.is_dir()
        or not os.access(cache, os.W_OK | os.X_OK)
    ):
        raise CryosparcImportError(
            f"pyem Numba cache is not a writable real directory: {cache}"
        )
    environment["NUMBA_CACHE_DIR"] = str(cache)
    return environment, cache


def _renameat2_noreplace_errno(
    parent_fd: int, source_name: str, destination_name: str
) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    arguments = (
        parent_fd,
        os.fsencode(source_name),
        parent_fd,
        os.fsencode(destination_name),
        RENAME_NOREPLACE,
    )
    if renameat2 is not None:
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        ctypes.set_errno(0)
        result = renameat2(*arguments)
    else:
        # CentOS 7's glibc predates the wrapper even when its kernel supports
        # renameat2. Try the known architecture-specific syscall before the
        # reservation-based portable fallback below.
        syscall_numbers = {
            "x86_64": 316,
            "amd64": 316,
            "aarch64": 276,
            "arm64": 276,
            "i386": 353,
            "i686": 353,
        }
        syscall_number = syscall_numbers.get(platform.machine().lower())
        syscall = getattr(libc, "syscall", None)
        if syscall_number is None or syscall is None:
            return errno.ENOSYS
        syscall.restype = ctypes.c_long
        ctypes.set_errno(0)
        result = int(syscall(syscall_number, *arguments))
    if result == 0:
        return 0
    return ctypes.get_errno() or errno.EIO


def _rename_directory_over_exclusive_reservation(
    parent_fd: int, source_name: str, destination_name: str
) -> str:
    """Publish on filesystems that reject ``RENAME_NOREPLACE``.

    An exclusive destination-directory creation first establishes ownership of
    the final name. A normal same-directory rename may then replace only that
    empty reservation atomically. Existing destinations are never removed or
    renamed. A process crash can leave an empty mode-000 reservation, which is
    intentionally fail-closed rather than risking an overwrite.
    """

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    reservation_fd: int | None = None
    reservation_device_inode: tuple[int, int] | None = None
    published = False
    try:
        try:
            os.mkdir(destination_name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError as exc:
            raise CryosparcImportError(
                "The CryoSPARC import destination appeared during publication; "
                "it was not replaced."
            ) from exc
        reservation_fd = os.open(destination_name, flags, dir_fd=parent_fd)
        reserved = os.fstat(reservation_fd)
        if not stat.S_ISDIR(
            reserved.st_mode
        ):  # pragma: no cover - O_DIRECTORY guards this
            raise CryosparcImportError(
                "The CryoSPARC import destination reservation is not a directory."
            )
        reservation_device_inode = (int(reserved.st_dev), int(reserved.st_ino))
        os.fchmod(reservation_fd, 0)
        current = os.stat(destination_name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(current.st_mode)
            or (int(current.st_dev), int(current.st_ino)) != reservation_device_inode
            or os.listdir(reservation_fd)
        ):
            raise CryosparcImportError(
                "The CryoSPARC import destination reservation changed before publication."
            )
        os.rename(
            source_name,
            destination_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        published = True
        return "exclusive_destination_reservation_then_rename"
    except CryosparcImportError:
        raise
    except OSError as exc:
        raise CryosparcImportError(
            f"Could not publish over the exclusive destination reservation: {exc}"
        ) from exc
    finally:
        primary_failure = sys.exc_info()[1]
        if not published and reservation_device_inode is not None:
            try:
                current = os.stat(
                    destination_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if (
                    stat.S_ISDIR(current.st_mode)
                    and (int(current.st_dev), int(current.st_ino))
                    == reservation_device_inode
                ):
                    os.rmdir(destination_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except OSError as exc:
                detail = (
                    "Could not clean the failed CryoSPARC destination reservation: "
                    f"{exc}"
                )
                if primary_failure is not None:
                    primary_failure.add_note(detail)
                else:  # pragma: no cover - cleanup is reached with a primary failure
                    raise CryosparcImportError(detail) from exc
        if reservation_fd is not None:
            os.close(reservation_fd)


def _rename_directory_noreplace(
    parent_fd: int, source_name: str, destination_name: str
) -> str:
    code = _renameat2_noreplace_errno(parent_fd, source_name, destination_name)
    if code == 0:
        return "renameat2_noreplace"
    if code in {errno.EEXIST, errno.ENOTEMPTY}:
        raise CryosparcImportError(
            "The CryoSPARC import destination appeared during publication; it was not replaced."
        )
    if code in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
        return _rename_directory_over_exclusive_reservation(
            parent_fd,
            source_name,
            destination_name,
        )
    raise CryosparcImportError(
        f"Could not atomically publish the CryoSPARC import bundle: {os.strerror(code)}"
    )


def _fsync_regular_file(path: Path, *, role: str) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise CryosparcImportError(f"{role} is not a regular file: {path}")
        os.fsync(descriptor)
    except OSError as exc:
        raise CryosparcImportError(f"Could not durably flush {role}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _fsync_directory(path: Path, *, role: str) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        os.fsync(descriptor)
    except OSError as exc:
        raise CryosparcImportError(f"Could not durably flush {role}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def import_cryosparc_particles(
    settings: Settings,
    project_dir: str,
    source_project_dir: str,
    particles_cs: str,
    passthrough_cs: list[str] | None = None,
    export_name: str = "particles",
    confirm: bool = False,
) -> dict[str, Any]:
    settings.assert_execution_enabled(confirm)
    prepared = _prepare(
        settings,
        project_dir,
        source_project_dir,
        particles_cs,
        passthrough_cs,
        export_name,
    )
    if not prepared.project_initialized:
        raise CryosparcImportError(
            "RELION project is not initialized. Call relion_initialize_project with the "
            "bootstrap arguments from the rendered import plan before running pyem."
        )
    if not prepared.capability.available:
        raise CryosparcImportError(
            "pyem is not ready: " + "; ".join(prepared.capability.issues)
        )
    assert prepared.capability.python is not None
    assert prepared.capability.script is not None
    assert prepared.capability.script_sha256 is not None

    parent_fd = _open_or_create_project_directory(
        prepared.project,
        OUTPUT_BASE,
        role="CryoSPARC import output parent",
    )
    staging_name: str | None = None
    staging_path: Path | None = None
    published = False
    completed: subprocess.CompletedProcess[str] | None = None
    command: list[str] | None = None
    output_summary: dict[str, Any] | None = None
    manifest: dict[str, Any] | None = None
    publication_method: str | None = None
    try:
        try:
            os.stat(prepared.export_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise CryosparcImportError(
                "CryoSPARC import bundle already exists and will not be replaced."
            )
        filesystem = os.fstatvfs(parent_fd)
        available_bytes = int(filesystem.f_bavail * filesystem.f_frsize)
        required_bytes = (
            sum(stack.identity.size for stack in prepared.stacks)
            + sum(micrograph.identity.size for micrograph in prepared.micrographs)
            + sum(item.identity.size for item in prepared.inputs)
            + prepared.particle_count * STAR_STAGING_BYTES_PER_PARTICLE
            + DISK_RESERVE_BYTES
        )
        if available_bytes < required_bytes:
            raise CryosparcImportError(
                "Insufficient free space for particle stacks, micrographs, CS snapshots, "
                f"estimated STAR staging, and safety reserve: required={required_bytes}, "
                f"available={available_bytes}."
            )
        for _ in range(100):
            candidate = f".cryosparc-import-{secrets.token_hex(8)}"
            try:
                os.mkdir(candidate, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
            staging_name = candidate
            break
        if staging_name is None:  # pragma: no cover - cryptographic collision bound
            raise CryosparcImportError(
                "Could not allocate a private conversion directory."
            )
        descriptor_staging_path = Path(f"/proc/self/fd/{parent_fd}") / staging_name
        staging_path = prepared.project.joinpath(*OUTPUT_BASE.parts, staging_name)
        if descriptor_staging_path.resolve(strict=True) != staging_path.resolve(
            strict=True
        ):
            raise CryosparcImportError(
                "CryoSPARC import staging changed while it was created."
            )
        inputs_dir = staging_path / "inputs"
        stacks_dir = staging_path / "stacks"
        micrographs_dir = staging_path / "micrographs"
        inputs_dir.mkdir(mode=0o700)
        stacks_dir.mkdir(mode=0o755)
        if prepared.micrographs:
            micrographs_dir.mkdir(mode=0o755)

        snapshot_names = ["000-particles.cs"] + [
            f"{index:03d}-passthrough.cs" for index in range(1, len(prepared.inputs))
        ]
        for item, name in zip(prepared.inputs, snapshot_names, strict=True):
            digest, _ = _copy_regular_file(
                item.path,
                inputs_dir / name,
                expected_identity=item.identity,
            )
            if digest != item.sha256:
                raise CryosparcImportError(
                    f"{item.role} content changed while its private snapshot was created."
                )
        environment, numba_cache = _pyem_environment(
            prepared.capability,
            staging_path,
        )
        command = [
            prepared.capability.python,
            prepared.capability.script,
            *[f"inputs/{name}" for name in snapshot_names],
            "raw-particles.star",
            "--loglevel",
            "INFO",
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=staging_path,
                capture_output=True,
                text=True,
                timeout=settings.utility_timeout_seconds,
                check=False,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CryosparcImportError(
                f"csparc2star.py could not complete: {exc}"
            ) from exc
        if completed.returncode != 0:
            diagnostics = "\n".join(
                value
                for value in (
                    completed.stdout[-DIAGNOSTIC_TAIL_CHARS:],
                    completed.stderr[-DIAGNOSTIC_TAIL_CHARS:],
                )
                if value
            )
            raise CryosparcImportError(
                f"csparc2star.py failed with exit code {completed.returncode}"
                + (f":\n{diagnostics}" if diagnostics else ".")
            )
        shutil.rmtree(numba_cache)
        raw_star = staging_path / "raw-particles.star"
        if raw_star.is_symlink() or not raw_star.is_file():
            raise CryosparcImportError(
                "csparc2star.py returned success without creating a regular raw-particles.star."
            )
        raw_star_size = _identity(raw_star).size
        filesystem = os.fstatvfs(parent_fd)
        remaining_bytes = int(filesystem.f_bavail * filesystem.f_frsize)
        remaining_required = (
            sum(stack.identity.size for stack in prepared.stacks)
            + sum(micrograph.identity.size for micrograph in prepared.micrographs)
            + max(raw_star_size * 2, prepared.particle_count * 512)
            + DISK_RESERVE_BYTES
        )
        if remaining_bytes < remaining_required:
            raise CryosparcImportError(
                "Insufficient free space after pyem conversion for the rewritten STAR, "
                f"particle stacks, micrographs, and safety reserve: "
                f"required={remaining_required}, "
                f"available={remaining_bytes}."
            )
        final_star = staging_path / "particles.star"
        output_summary = _rewrite_and_validate_star(raw_star, final_star, prepared)

        stack_records: list[dict[str, Any]] = []
        for stack in prepared.stacks:
            destination_name = PurePosixPath(stack.destination_relative).name
            destination = stacks_dir / destination_name
            digest, copied = _copy_regular_file(
                stack.source_path,
                destination,
                expected_identity=stack.identity,
            )
            copied_header, copied_identity = _mrc_stack_info(
                destination,
                role="copied particle stack",
            )
            if (
                copied != stack.identity.size
                or copied_identity.size != stack.identity.size
                or int(copied_header["nx"]) != stack.nx
                or int(copied_header["ny"]) != stack.ny
                or int(copied_header["nz"]) != stack.nz
                or int(copied_header["mode"]) != stack.mode
                or not math.isclose(
                    float(copied_header["pixel_size_A"]),
                    stack.pixel_size_A,
                    rel_tol=1e-6,
                    abs_tol=1e-6,
                )
            ):
                raise CryosparcImportError(
                    f"Copied stack failed byte/header preservation: {stack.source_relative}"
                )
            stack_records.append(
                {
                    **stack.public(),
                    "source_sha256": digest,
                    "output_sha256": digest,
                }
            )
            destination.chmod(0o444)

        micrograph_records: list[dict[str, Any]] = []
        for micrograph in prepared.micrographs:
            destination_name = PurePosixPath(micrograph.destination_relative).name
            destination = micrographs_dir / destination_name
            digest, copied = _copy_regular_file(
                micrograph.source_path,
                destination,
                expected_identity=micrograph.identity,
            )
            copied_identity = _identity(destination)
            if (
                copied != micrograph.identity.size
                or copied_identity.size != micrograph.identity.size
            ):
                raise CryosparcImportError(
                    "Copied micrograph failed byte preservation: "
                    f"{micrograph.source_relative}"
                )
            micrograph_records.append(
                {
                    **micrograph.public(),
                    "source_sha256": digest,
                    "output_sha256": digest,
                }
            )
            destination.chmod(0o444)

        for item in prepared.inputs:
            current = _resolve_regular_source(
                prepared.source_project,
                item.relative_path,
                role=item.role,
                suffixes=(".cs",),
            )
            if (
                current != item.path
                or _identity(current) != item.identity
                or _sha256(current) != item.sha256
            ):
                raise CryosparcImportError(
                    f"{item.role} changed before the import bundle could be published."
                )
        for stack in prepared.stacks:
            current = _resolve_regular_source(
                prepared.source_project,
                stack.source_relative,
                role="particle stack",
                suffixes=(".mrc", ".mrcs"),
            )
            if current != stack.source_path or _identity(current) != stack.identity:
                raise CryosparcImportError(
                    f"Particle stack changed before publication: {stack.source_relative}"
                )
        for micrograph in prepared.micrographs:
            current = _resolve_regular_source(
                prepared.source_project,
                micrograph.source_relative,
                role="micrograph",
                suffixes=(".mrc", ".mrcs"),
            )
            if (
                current != micrograph.source_path
                or _identity(current) != micrograph.identity
            ):
                raise CryosparcImportError(
                    "Micrograph changed before publication: "
                    f"{micrograph.source_relative}"
                )
        if (
            _sha256(Path(prepared.capability.script))
            != prepared.capability.script_sha256
        ):
            raise CryosparcImportError(
                "csparc2star.py changed while the conversion was running."
            )
        _, project_still_initialized, _, _, current_pipeline_name = _project_state(
            settings,
            prepared.project,
        )
        if (
            not project_still_initialized
            or current_pipeline_name != prepared.pipeline_name
        ):
            raise CryosparcImportError(
                "RELION project initialization changed while the conversion was running; "
                "the bundle was not published."
            )

        raw_star_sha256 = _sha256(raw_star)
        final_star_sha256 = _sha256(final_star)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "created_at_utc": dt.datetime.now(dt.UTC).isoformat(),
            "purpose": "RELION Class3D particle input",
            "plan_sha256": prepared.plan["plan_sha256"],
            "project_dir": str(prepared.project),
            "source_project_dir": str(prepared.source_project),
            "source_authorization": prepared.plan["source_authorization"],
            "inputs": [item.public() for item in prepared.inputs],
            "particle_count": prepared.particle_count,
            "pyem": {
                "python": prepared.capability.python,
                "script": prepared.capability.script,
                "script_sha256": prepared.capability.script_sha256,
                "argv": command,
                "working_directory": "private staging directory (not published)",
                "returncode": completed.returncode,
                "stdout_tail": completed.stdout[-DIAGNOSTIC_TAIL_CHARS:],
                "stderr_tail": completed.stderr[-DIAGNOSTIC_TAIL_CHARS:],
                "environment_policy": {
                    "PYTHONPATH": prepared.capability.pyem_root,
                    "PYTHONNOUSERSITE": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "NUMBA_CACHE_DIR": "private staging directory (removed)",
                    "cryosparc_credentials_removed": True,
                },
            },
            "stacks": stack_records,
            "micrographs": micrograph_records,
            "outputs": {
                "particles_star": prepared.particles_star_relative,
                "particles_star_sha256": final_star_sha256,
                "micrographs": [record["destination"] for record in micrograph_records],
                "conversion_manifest": prepared.manifest_relative,
                "raw_pyem_star_sha256": raw_star_sha256,
            },
            "validation": output_summary,
            "publication": prepared.plan["publication"],
            "import_job_handoff": prepared.plan["import_job_handoff"],
            "source_files_modified": False,
            "relion_pipeline_job_registered": False,
        }
        manifest_path = staging_path / "conversion_manifest.json"
        with manifest_path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
            handle.write("\n")
        manifest_path.chmod(0o444)
        final_star.chmod(0o444)
        _fsync_regular_file(final_star, role="rewritten particles STAR")
        _fsync_regular_file(manifest_path, role="conversion manifest")
        raw_star.unlink()
        shutil.rmtree(inputs_dir)
        _fsync_directory(stacks_dir, role="copied stack directory")
        if prepared.micrographs:
            _fsync_directory(
                micrographs_dir,
                role="copied micrograph directory",
            )
        staging_path.chmod(0o755)
        _fsync_directory(staging_path, role="conversion staging directory")
        os.fsync(parent_fd)

        publication_method = _rename_directory_noreplace(
            parent_fd,
            staging_name,
            prepared.export_name,
        )
        os.fsync(parent_fd)
        published = True
    finally:
        primary_failure = sys.exc_info()[1]
        try:
            if (
                not published
                and staging_path is not None
                and os.path.lexists(staging_path)
            ):
                try:
                    shutil.rmtree(staging_path)
                except OSError as exc:
                    detail = f"Could not clean private CryoSPARC import staging: {exc}"
                    if primary_failure is not None:
                        primary_failure.add_note(detail)
                    else:
                        raise CryosparcImportError(detail) from exc
        finally:
            os.close(parent_fd)

    if (
        completed is None
        or command is None
        or output_summary is None
        or manifest is None
        or publication_method is None
    ):
        raise CryosparcImportError(
            "CryoSPARC particle import did not produce validated outputs."
        )
    bundle = prepared.project / prepared.bundle_relative
    particles_star_path = prepared.project / prepared.particles_star_relative
    manifest_path = prepared.project / prepared.manifest_relative
    if bundle.is_symlink() or not bundle.is_dir():
        raise CryosparcImportError(
            "Published CryoSPARC import bundle is not a real directory."
        )
    if particles_star_path.is_symlink() or not particles_star_path.is_file():
        raise CryosparcImportError("Published particles.star is not a regular file.")
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise CryosparcImportError(
            "Published conversion manifest is not a regular file."
        )
    for micrograph in prepared.micrographs:
        published_micrograph = prepared.project / micrograph.destination_relative
        if (
            published_micrograph.is_symlink()
            or not published_micrograph.is_file()
            or _identity(published_micrograph).size != micrograph.identity.size
        ):
            raise CryosparcImportError(
                "Published micrograph is not a size-preserved regular file: "
                f"{micrograph.destination_relative}"
            )

    result = dict(prepared.plan)
    result.update(
        {
            "status": "materialized",
            "execution_ready": True,
            "output_exists": True,
            "command": command,
            "command_working_directory": "private staging directory (removed)",
            "output_summary": output_summary,
            "manifest_sha256": _sha256(manifest_path),
            "publication": {
                **prepared.plan["publication"],
                "method": publication_method,
                "published_without_replacement": True,
            },
        }
    )
    return result
