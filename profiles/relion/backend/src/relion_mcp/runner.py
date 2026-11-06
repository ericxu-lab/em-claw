from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path, PurePosixPath
from typing import Any

import mrcfile
import numpy as np

from .catalog import (
    EXECUTABLE_SUPPORT_LEVELS,
    Catalog,
    CatalogError,
    RenderedJob,
    safe_absolute_runtime_path,
    safe_alias,
    safe_project_relative,
)
from .initial_model_acceptance import (
    find_initial_model_acceptance,
    registered_class3d_inputs,
    require_initial_model_acceptance,
)
from .initial_model_acceptance import (
    record_initial_model_acceptance as _record_initial_model_acceptance,
)
from .outputs import classify_outputs
from .settings import ConfigurationError, Settings
from .star import (
    ParticleStarSummary,
    StarFormatError,
    job_output_edges,
    parse_star_block,
    particle_star_row_summary,
    particle_star_summary,
    pipeline_processes,
    relion_metadata_loop_digest,
)


class RelionRuntimeError(RuntimeError):
    """Raised when a RELION command fails."""


class PartialPipelineAddError(RelionRuntimeError):
    """Raised when a native multi-job branch was only partially added."""

    def __init__(self, added_jobs: list[str], failed_stage: str, detail: str):
        self.added_jobs = list(added_jobs)
        self.failed_stage = failed_stage
        super().__init__(
            f"Failed while adding {failed_stage}. Jobs already added and preserved: "
            f"{self.added_jobs}. No jobs were started. Do not start this incomplete branch; "
            f"retry with a new base alias. Cause: {detail}"
        )


class SegmentStartError(RelionRuntimeError):
    """Raised when a bounded segment was registered but could not be started."""

    def __init__(self, added_jobs: list[str], schedule_name: str, detail: str):
        self.added_jobs = list(added_jobs)
        self.schedule_name = schedule_name
        super().__init__(
            f"Registered the bounded segment jobs but could not start schedule "
            f"{schedule_name!r}. Jobs were preserved and must not be added again: "
            f"{self.added_jobs}. Recover with relion_start_jobs after diagnosing the "
            f"start failure. Cause: {detail}"
        )


MAX_SELECTED_CLASSES = 256
CLASS_SELECTION_MATERIALIZATION_MODES = {"auto", "gui_equivalent", "select_joinstar"}
CURRENT_RELION_SYMMETRY_ORDER_DIGITS = 2
SYMMETRY_PREFLIGHT_MAX_ORDER = 9999
SYMMETRY_PREFLIGHT_TIMEOUT_SECONDS = 10
SYMMETRY_SEMANTIC_RULE = "C1, Cn or Dn with n >= 2, or I, O, or T"
SYMMETRY_ALIGNMENT_SCHEMA_VERSION = "relion.symmetry-reference-alignment/v2"
SYMMETRY_ALIGNMENT_EXECUTABLE = "relion_align_symmetry"
SYMMETRY_ALIGNMENT_WORKING_BOX_SIZE_PIX = 64
SYMMETRY_ALIGNMENT_ORIENTATION_SAMPLES = 400
SYMMETRY_ALIGNMENT_LOCAL_SEARCH_RANGE = 2
SYMMETRY_ALIGNMENT_LOCAL_SEARCH_STEP_DEG = 2.0
SYMMETRY_ALIGNMENT_PADDING_FACTOR = 2
SYMMETRY_ALIGNMENT_MIN_SCORING_RADIUS_SHELLS = 3
SYMMETRY_ALIGNMENT_MAX_EXTENDED_HEADER_BYTES = 16 * 1024 * 1024
SYMMETRY_ALIGNMENT_FREE_DISK_RESERVE_BYTES = 64 * 1024 * 1024
DUPLICATE_PARTICLE_REQUIRED_COLUMNS = {
    "rlnCoordinateX",
    "rlnCoordinateY",
    "rlnImageName",
    "rlnMicrographName",
    "rlnOpticsGroup",
    "rlnOriginXAngst",
    "rlnOriginYAngst",
}

_RELION_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_REFINED_ALIGNMENT_RE = re.compile(
    rf"^\s*The\s+refined\s+solution\s+is\s+"
    rf"ROT\s*=\s*(?P<rot>{_RELION_NUMBER})\s+"
    rf"TILT\s*=\s*(?P<tilt>{_RELION_NUMBER})\s+"
    rf"PSI\s*=\s*(?P<psi>{_RELION_NUMBER})\s+"
    rf"DIFF2\s*=\s*(?P<diff2>{_RELION_NUMBER})\s*$",
    re.MULTILINE,
)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _particle_row_summary_payload(
    path: Path,
    *,
    require_optics: bool = True,
    allow_unnamed_particle_block: bool = False,
) -> dict[str, Any]:
    """Validate a particle STAR and return class-independent bounded counts."""
    if not require_optics and path.stat().st_size == 0:
        return {
            "particle_count": 0,
            "columns": [],
            "optics_group_ids": None,
            "optics_group_counts": None,
            "exact_duplicate_image_name_count": 0,
            "exact_duplicate_image_name_examples": [],
            "image_name_multiset_digest": f"{0:064x}",
            "row_metadata_multiset_digest": f"{0:064x}",
            "optics_metadata_digest": None,
            "optics_block_present": False,
            "empty_native_table": True,
        }
    summary = particle_star_row_summary(
        path,
        required_columns=DUPLICATE_PARTICLE_REQUIRED_COLUMNS,
        allow_unnamed_particle_block=allow_unnamed_particle_block,
    )
    if summary.optics_group_counts is not None:
        invalid_particle_groups = sorted(
            group
            for group in summary.optics_group_counts
            if re.fullmatch(r"[1-9][0-9]*", group) is None
        )
        if invalid_particle_groups:
            raise StarFormatError(
                f"data_particles in {path} has non-canonical rlnOpticsGroup values; "
                "expected positive integers without leading zeros: "
                + ", ".join(invalid_particle_groups)
    )
    # Any optics block in the native removed-particles sidecar is itself an
    # integrity failure, so probe it tolerantly and let the caller report that
    # higher-level contract violation. Main/retained tables remain strict.
    optics = parse_star_block(path, "optics", strict_loops=require_optics)
    optics_block_present = optics is not None
    if require_optics and optics is None:
        raise StarFormatError(f"Missing data_optics block in {path}")
    if require_optics and not optics:
        raise StarFormatError(f"data_optics block in {path} is empty")
    optics_groups: list[str] | None = None
    optics_metadata_digest: str | None = None
    if require_optics and optics:
        optics_metadata_digest = relion_metadata_loop_digest(
            optics,
            block_name="optics",
        )
        raw_optics_groups = optics.get("rlnOpticsGroup")
        if isinstance(raw_optics_groups, list):
            optics_groups = [str(value) for value in raw_optics_groups]
        elif isinstance(raw_optics_groups, str) and raw_optics_groups:
            optics_groups = [raw_optics_groups]
        else:
            raise StarFormatError(f"data_optics in {path} is missing rlnOpticsGroup")
        invalid_optics_groups = sorted(
            group
            for group in optics_groups
            if re.fullmatch(r"[1-9][0-9]*", group) is None
        )
        if invalid_optics_groups:
            raise StarFormatError(
                f"data_optics in {path} has non-canonical rlnOpticsGroup values; "
                "expected positive integers without leading zeros: "
                + ", ".join(invalid_optics_groups)
            )
        if len(optics_groups) != len(set(optics_groups)):
            raise StarFormatError(
                f"data_optics in {path} has duplicate rlnOpticsGroup values"
            )
        expected_optics_groups = [
            str(index) for index in range(1, len(optics_groups) + 1)
        ]
        if optics_groups != expected_optics_groups:
            raise StarFormatError(
                f"data_optics in {path} must list contiguous rlnOpticsGroup values 1..N "
                "in row order. RELION loadSafely would otherwise renumber optics groups, "
                "changing particle metadata during duplicate removal; got "
                f"{optics_groups}, expected {expected_optics_groups}."
            )
        if summary.optics_group_counts is not None:
            missing_groups = sorted(
                set(summary.optics_group_counts).difference(optics_groups)
            )
            if missing_groups:
                raise StarFormatError(
                    f"data_particles in {path} references missing optics groups: "
                    + ", ".join(missing_groups)
                )
    return {
        "particle_count": summary.particle_count,
        "columns": list(summary.columns),
        "optics_group_ids": optics_groups,
        "optics_group_counts": summary.optics_group_counts,
        "exact_duplicate_image_name_count": summary.duplicate_image_name_count,
        "exact_duplicate_image_name_examples": list(
            summary.duplicate_image_name_examples
        ),
        "image_name_multiset_digest": summary.image_name_multiset_digest,
        "row_metadata_multiset_digest": summary.row_metadata_multiset_digest,
        "optics_metadata_digest": optics_metadata_digest,
        "optics_block_present": optics_block_present,
        "empty_native_table": False,
    }


def _assert_safe_relative_parent(
    project: Path,
    relative_path: str,
    *,
    role: str,
) -> None:
    """Reject existing non-directory or symlink components below the project."""
    cursor = project
    for part in PurePosixPath(relative_path).parent.parts:
        cursor /= part
        if not os.path.lexists(cursor):
            continue
        if cursor.is_symlink():
            raise CatalogError(f"{role} parent must not contain symbolic links: {cursor}")
        if not cursor.is_dir():
            raise CatalogError(f"{role} parent component is not a directory: {cursor}")


def _open_or_create_project_directory(
    project: Path,
    relative_directory: PurePosixPath,
    *,
    role: str,
) -> int:
    """Securely walk/create a project directory and return its verified open fd."""
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    opened_fds: list[int] = []
    try:
        opened_fds.append(os.open(project, directory_flags))
        opened_project = Path(f"/proc/self/fd/{opened_fds[0]}").resolve(strict=True)
        if opened_project != project:
            raise CatalogError(f"Project directory changed while preparing {role}.")
        for part in relative_directory.parts:
            try:
                os.mkdir(part, mode=0o755, dir_fd=opened_fds[-1])
            except FileExistsError:
                pass
            try:
                child_fd = os.open(part, directory_flags, dir_fd=opened_fds[-1])
            except OSError as exc:
                raise CatalogError(
                    f"{role} parent component is not a stable real directory: {part}"
                ) from exc
            opened_fds.append(child_fd)
        expected = project.joinpath(*relative_directory.parts)
        opened_directory = Path(
            f"/proc/self/fd/{opened_fds[-1]}"
        ).resolve(strict=True)
        if opened_directory != expected:
            raise CatalogError(f"{role} parent changed while it was being created.")
        result_fd = opened_fds.pop()
        return result_fd
    except OSError as exc:
        raise CatalogError(f"Could not prepare {role} parent directory: {exc}") from exc
    finally:
        for descriptor in reversed(opened_fds):
            os.close(descriptor)


def _mrc_volume_info(
    path: Path,
    *,
    role: str,
    max_box_size_pix: int | None = None,
) -> dict[str, Any]:
    try:
        with mrcfile.mmap(path, mode="r", permissive=False) as handle:
            if handle.data is None:
                raise CatalogError(f"{role} MRC does not contain voxel data: {path}")
            data = np.asarray(handle.data)
            if data.ndim != 3:
                raise CatalogError(f"{role} must be a three-dimensional MRC volume.")
            if data.dtype.kind != "f":
                raise CatalogError(f"{role} must contain real floating-point density values.")
            mode = int(handle.header.mode)
            if mode != 2:
                raise CatalogError(f"{role} must use RELION's floating-point MRC mode 2; got {mode}.")
            shape_zyx = tuple(int(value) for value in data.shape)
            if len(set(shape_zyx)) != 1:
                raise CatalogError(f"{role} must use a cubic grid; got {shape_zyx}.")
            if shape_zyx[0] % 2 != 0:
                raise CatalogError(f"{role} cubic box size must be even; got {shape_zyx[0]}.")
            if max_box_size_pix is not None and shape_zyx[0] > max_box_size_pix:
                raise CatalogError(
                    f"{role} box size {shape_zyx[0]} exceeds the configured symmetry-"
                    f"alignment limit of {max_box_size_pix} pixels."
                )

            voxel_size_xyz_A = tuple(
                float(getattr(handle.voxel_size, axis)) for axis in ("x", "y", "z")
            )
            if not all(math.isfinite(value) and value > 0 for value in voxel_size_xyz_A):
                raise CatalogError(f"{role} must declare a positive finite voxel size.")
            if not np.allclose(
                voxel_size_xyz_A,
                (voxel_size_xyz_A[0],) * 3,
                rtol=1e-6,
                atol=1e-6,
            ):
                raise CatalogError(
                    f"{role} must use isotropic voxel spacing; got {voxel_size_xyz_A}."
                )

            axis_mapping = tuple(
                int(getattr(handle.header, field)) for field in ("mapc", "mapr", "maps")
            )
            if axis_mapping != (1, 2, 3):
                raise CatalogError(
                    f"{role} must use canonical MRC axis mapping (1, 2, 3); got {axis_mapping}."
                )
            starts_xyz = tuple(
                int(getattr(handle.header, field))
                for field in ("nxstart", "nystart", "nzstart")
            )
            origin_xyz_A = tuple(
                float(getattr(handle.header.origin, axis)) for axis in ("x", "y", "z")
            )
            if starts_xyz != (0, 0, 0) or not np.allclose(
                origin_xyz_A,
                (0.0, 0.0, 0.0),
                rtol=0.0,
                atol=1e-6,
            ):
                raise CatalogError(
                    f"{role} must use zero MRC starts and origin because native alignment "
                    "materializes a canonical output header."
                )
            if not all(math.isfinite(value) for value in origin_xyz_A):
                raise CatalogError(f"{role} MRC origin must be finite.")

            if not bool(np.isfinite(data).all()):
                raise CatalogError(f"{role} contains non-finite density values.")
            density_min = float(np.min(data))
            density_max = float(np.max(data))
            if density_min == density_max:
                raise CatalogError(f"{role} density is constant and cannot be aligned.")
            density_mean = float(np.mean(data, dtype=np.float64))
            density_std = float(np.std(data, dtype=np.float64))
    except CatalogError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise CatalogError(f"Cannot read {role} as a valid MRC volume: {path}: {exc}") from exc

    return {
        "shape_zyx": list(shape_zyx),
        "box_size_pix": shape_zyx[0],
        "voxel_size_xyz_A": list(voxel_size_xyz_A),
        "axis_mapping_mapc_mapr_maps": list(axis_mapping),
        "starts_xyz": list(starts_xyz),
        "origin_xyz_A": list(origin_xyz_A),
        "mrc_mode": mode,
        "dtype": str(data.dtype),
        "density": {
            "min": density_min,
            "max": density_max,
            "mean": density_mean,
            "std": density_std,
        },
    }


def _parse_refined_alignment(stdout: str) -> dict[str, Any]:
    matches = list(_REFINED_ALIGNMENT_RE.finditer(stdout))
    if len(matches) != 1:
        raise RelionRuntimeError(
            "relion_align_symmetry must report exactly one refined ROT/TILT/PSI/DIFF2 "
            f"solution for an MRC input; found {len(matches)}."
        )
    values = {name: float(matches[0].group(name)) for name in ("rot", "tilt", "psi", "diff2")}
    if not all(math.isfinite(value) for value in values.values()) or values["diff2"] < 0:
        raise RelionRuntimeError(
            "relion_align_symmetry reported a non-finite or negative refined solution."
        )
    return {
        "euler_angles_deg": {
            "rot": values["rot"],
            "tilt": values["tilt"],
            "psi": values["psi"],
        },
        "diff2": values["diff2"],
        "convention": "RELION ROT/TILT/PSI as reported by relion_align_symmetry",
        "rotation_applied_to_output": True,
        "rotation_matrix_reported": False,
    }


def _safe_name(value: str, field: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
        raise CatalogError(f"{field} may contain only letters, digits, '.', '_' and '-'.")
    return value


def _safe_job(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.\-/]+", value):
        raise CatalogError(f"Unsafe RELION job name: {value}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or len(path.parts) < 2:
        raise CatalogError(f"Unsafe RELION job name: {value}")
    return value if value.endswith("/") else value + "/"


def _normalise_class_numbers(values: list[int]) -> list[int]:
    if not isinstance(values, list) or not values:
        raise CatalogError("selected_class_numbers must be a non-empty list.")
    checked: list[int] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            raise CatalogError("Every selected class number must be an integer.")
        if value < 1:
            raise CatalogError("Every selected class number must be positive.")
        checked.append(value)
    if len(checked) != len(set(checked)):
        raise CatalogError("selected_class_numbers may not contain duplicates.")
    if len(checked) > MAX_SELECTED_CLASSES:
        raise CatalogError(f"At most {MAX_SELECTED_CLASSES} classes may be selected in one call.")
    return sorted(checked)


def _derived_alias(base: str, suffix: str) -> str:
    checked_base = _safe_name(base, "alias")
    checked_suffix = _safe_name(suffix, "alias suffix")
    candidate = f"{checked_base}__{checked_suffix}"
    if len(candidate) > 80:
        digest = hashlib.sha256(checked_base.encode("utf-8")).hexdigest()[:8]
        tail = f"_{digest}__{checked_suffix}"
        prefix = checked_base[: 80 - len(tail)].rstrip("._-") or "selection"
        candidate = f"{prefix}{tail}"
    return _safe_name(candidate, "derived alias")


class RelionRunner:
    def __init__(self, catalog: Catalog, settings: Settings):
        self.catalog = catalog
        self.settings = settings

    @contextlib.contextmanager
    def _pipeline_add_lock(self, project: Path, pipeline_name: str):
        """Serialize job registration for one project/pipeline.

        ``add_job`` identifies the newly registered job by diffing the
        pipeline's process list before and after invoking
        ``relion_pipeliner``. Without serialization, two concurrent add-job
        calls against the same project/pipeline can interleave their
        before/after snapshots, causing one call to attribute the other
        call's job to itself. The lock is held across that whole
        snapshot/invoke/snapshot/diff sequence and is process-safe (not just
        thread-safe), since each MCP session runs as its own OS process.
        """
        pipeline = _safe_name(pipeline_name, "pipeline_name")
        lock_dir = self.settings.resolve_project_subpath(project, "Log")
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / f".relion_mcp_add_job_{pipeline}.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def pipeline_path(self, project: Path, pipeline_name: str | None = None) -> Path:
        selected_pipeline = (
            self.settings.pipeline_name if pipeline_name is None else pipeline_name
        )
        pipeline = _safe_name(
            selected_pipeline,
            "pipeline_name",
        )
        return self.settings.resolve_project_subpath(
            project,
            f"{pipeline}_pipeline.star",
        )

    def _validate_scratch_policy(self, rendered: RenderedJob) -> None:
        """Lexically confine an explicit scratch option to the approved base."""
        if "scratch_dir" not in rendered.options_by_key:
            return
        requested_value = safe_absolute_runtime_path(
            rendered.options_by_key["scratch_dir"],
            allow_empty=True,
        )
        if requested_value == "":
            return
        configured_value = self.settings.scratch_directory
        if configured_value is None:
            raise ConfigurationError(
                "A non-empty per-job scratch_directory requires an operator-approved "
                "RELION_SCRATCH_DIR base."
            )
        approved = PurePosixPath(safe_absolute_runtime_path(configured_value))
        requested = PurePosixPath(requested_value)
        if requested != approved and approved not in requested.parents:
            raise ConfigurationError(
                "Per-job scratch_directory must equal or be beneath the configured "
                f"RELION_SCRATCH_DIR base: {configured_value}"
            )

    def _execution_environment(self) -> dict[str, str]:
        """Return a child environment that can resolve sibling RELION programs.

        RELION's pipeliner writes job commands such as ``relion_import`` rather
        than embedding the absolute path of every executable.  Consequently,
        configuring an absolute ``RELION_PIPELINER`` is insufficient when the
        MCP process inherited a minimal PATH.  Prepend the directories of the
        configured RELION entry points while preserving the inherited PATH.
        """
        environment = os.environ.copy()
        for name in tuple(environment):
            if name.startswith(("CRYOSPARC_", "EMCLAW_CRYOSPARC_")):
                environment.pop(name, None)
        if self.settings.scratch_directory is None:
            environment.pop("RELION_SCRATCH_DIR", None)
        else:
            environment["RELION_SCRATCH_DIR"] = self.settings.scratch_directory
        inherited_parts = [part for part in environment.get("PATH", "").split(os.pathsep) if part]
        relion_bin_dirs: list[str] = []
        for configured in (
            self.settings.pipeliner_executable,
            self.settings.image_handler_executable,
            self.settings.star_handler_executable,
            self.settings.class_selector_executable,
        ):
            resolved = self.settings.resolve_executable(configured)
            if resolved:
                parent = str(Path(resolved).resolve().parent)
                if parent not in relion_bin_dirs:
                    relion_bin_dirs.append(parent)
        environment["PATH"] = os.pathsep.join(
            relion_bin_dirs + [part for part in inherited_parts if part not in relion_bin_dirs]
        )
        return environment

    def _resolve_execution_executable(
        self,
        configured: str,
        environment: dict[str, str] | None = None,
    ) -> str | None:
        execution_environment = (
            self._execution_environment() if environment is None else environment
        )
        candidate = Path(configured).expanduser()
        if candidate.is_absolute():
            return (
                str(candidate)
                if candidate.is_file() and os.access(candidate, os.X_OK)
                else None
            )
        return shutil.which(configured, path=execution_environment.get("PATH", ""))

    def environment(self, project_dir: str | None = None) -> dict[str, Any]:
        project = (
            self.settings.validate_project_path(project_dir)
            if project_dir is not None
            else None
        )
        execution_environment = self._execution_environment()
        pipeliner = self._resolve_execution_executable(
            self.settings.pipeliner_executable,
            execution_environment,
        )
        image_handler = self._resolve_execution_executable(
            self.settings.image_handler_executable,
            execution_environment,
        )
        star_handler = self._resolve_execution_executable(
            self.settings.star_handler_executable,
            execution_environment,
        )
        class_selector = self._resolve_execution_executable(
            self.settings.class_selector_executable,
            execution_environment,
        )
        align_symmetry = self._resolve_execution_executable(
            SYMMETRY_ALIGNMENT_EXECUTABLE,
            execution_environment,
        )
        refine = shutil.which("relion_refine", path=execution_environment.get("PATH", ""))
        scratch_directory = self.settings.scratch_directory
        scratch_path = Path(scratch_directory) if scratch_directory is not None else None
        scratch_exists = scratch_path.exists() if scratch_path is not None else None
        scratch_is_dir = scratch_path.is_dir() if scratch_path is not None else None
        scratch_writable = (
            bool(scratch_is_dir and os.access(scratch_path, os.W_OK | os.X_OK))
            if scratch_path is not None
            else None
        )
        pipeline_path = self.pipeline_path(project) if project is not None else None
        marker = (
            self.settings.resolve_project_subpath(project, ".gui_projectdir")
            if project is not None
            else None
        )
        version_text = None
        if pipeliner:
            for flag in ("--version", "--help"):
                try:
                    completed = subprocess.run(
                        [pipeliner, flag],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                        env=execution_environment,
                    )
                    output = (completed.stdout + "\n" + completed.stderr).strip()
                    if output:
                        version_text = output.splitlines()[0][:300]
                        break
                except (OSError, subprocess.TimeoutExpired):
                    continue
        return {
            "mode": self.settings.mode,
            "approved_roots": [str(path) for path in self.settings.project_roots],
            "expected_relion_version": self.catalog.relion_version,
            "pipeliner": pipeliner,
            "image_handler": image_handler,
            "star_handler": star_handler,
            "class_selector": class_selector,
            "align_symmetry": align_symmetry,
            "symmetry_alignment_limits": {
                "max_input_box_size_pix": (
                    self.settings.symmetry_alignment_max_box_size_px
                ),
                "minimum_scoring_radius_shells": (
                    SYMMETRY_ALIGNMENT_MIN_SCORING_RADIUS_SHELLS
                ),
            },
            "particle_class_selection_modes": {
                "preferred": "gui_equivalent" if class_selector else "select_joinstar",
                "gui_equivalent_available": class_selector is not None,
                "fallback": "select_joinstar",
            },
            "relion_refine": refine,
            "execution_path": execution_environment.get("PATH", ""),
            "scratch_directory": {
                "configured": scratch_directory,
                "exists_on_mcp_host": scratch_exists,
                "is_directory_on_mcp_host": scratch_is_dir,
                "writable_on_mcp_host": scratch_writable,
                "host_checks_are_advisory": True,
            },
            "relion_import": shutil.which(
                "relion_import", path=execution_environment.get("PATH", "")
            ),
            "ctffind": shutil.which("ctffind", path=execution_environment.get("PATH", "")),
            "topaz": self._resolve_execution_executable(
                self.settings.topaz_executable, execution_environment
            ),
            "version_probe": version_text,
            "project": str(project) if project is not None else None,
            "project_marker": bool(marker and marker.exists()),
            "pipeline_file": str(pipeline_path) if pipeline_path is not None else None,
            "pipeline_exists": bool(pipeline_path and pipeline_path.exists()),
        }

    def initialize_project(self, project_dir: str, pipeline_name: str, confirm: bool) -> dict[str, Any]:
        self.settings.assert_execution_enabled(confirm)
        project = self.settings.validate_project_path(project_dir, may_create=True)
        pipeline = _safe_name(pipeline_name, "pipeline_name")
        project.mkdir(parents=True, exist_ok=True)
        project = self.settings.validate_project_path(str(project))
        marker_raw = project / ".gui_projectdir"
        if marker_raw.is_symlink():
            raise CatalogError("The RELION project marker may not be a symbolic link.")
        if marker_raw.exists() and not marker_raw.is_file():
            raise CatalogError("The RELION project marker must be a regular file.")
        marker = self.settings.resolve_project_subpath(project, ".gui_projectdir")
        marker_created = not marker.exists()
        marker.touch(exist_ok=True)
        pipeline_path_raw = project / f"{pipeline}_pipeline.star"
        if pipeline_path_raw.is_symlink():
            if marker_created:
                marker.unlink(missing_ok=True)
            raise CatalogError("The RELION pipeline file may not be a symbolic link.")
        pipeline_path = self.pipeline_path(project, pipeline)
        pipeline_created = False
        initializer_command: list[str] | None = None
        initializer_stdout = ""
        initializer_stderr = ""
        try:
            if pipeline_path.exists() and not pipeline_path.is_file():
                raise CatalogError("The RELION pipeline path must be a regular file.")
            if not pipeline_path.exists():
                environment = self._execution_environment()
                relion = shutil.which("relion", path=environment.get("PATH", ""))
                if not relion:
                    raise RelionRuntimeError(
                        "Cannot find the RELION GUI executable needed for native project initialization."
                    )
                initializer_command = [
                    relion,
                    "--pipeline",
                    pipeline,
                    "--do_projdir",
                    "--refresh",
                    "1",
                    "--idle",
                    "0",
                ]
                if not environment.get("DISPLAY", "").strip():
                    xvfb_run = shutil.which(
                        "xvfb-run", path=environment.get("PATH", "")
                    )
                    if not xvfb_run:
                        raise RelionRuntimeError(
                            "Cannot initialize a missing RELION pipeline: DISPLAY is "
                            "not set and xvfb-run is not available. Forward a working "
                            "X11 display or install xvfb-run."
                        )
                    initializer_command = [
                        xvfb_run,
                        "-a",
                        "-e",
                        "/dev/stderr",
                        *initializer_command,
                    ]
                try:
                    completed = subprocess.run(
                        initializer_command,
                        cwd=project,
                        capture_output=True,
                        text=True,
                        timeout=min(self.settings.utility_timeout_seconds, 30),
                        check=False,
                        env=environment,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise RelionRuntimeError(
                        f"RELION native project initialization could not complete: {exc}"
                    ) from exc
                initializer_stdout = completed.stdout[-4000:]
                initializer_stderr = completed.stderr[-4000:]
                if completed.returncode != 0:
                    diagnostic = "\n".join(
                        part
                        for part in (
                            f"stdout:\n{initializer_stdout}" if initializer_stdout else "",
                            f"stderr:\n{initializer_stderr}" if initializer_stderr else "",
                        )
                        if part
                    )
                    raise RelionRuntimeError(
                        "RELION native project initialization failed with exit code "
                        f"{completed.returncode}"
                        + (f":\n{diagnostic}" if diagnostic else ".")
                    )
                if not pipeline_path.is_file():
                    raise RelionRuntimeError(
                        "RELION native project initialization completed without creating "
                        f"{pipeline_path.name}."
                    )
                pipeline_created = True
        except Exception:
            if marker_created:
                marker.unlink(missing_ok=True)
            raise

        metadata_dir_raw = project / ".relion-mcp"
        if metadata_dir_raw.is_symlink():
            raise CatalogError("The .relion-mcp metadata directory may not be a symbolic link.")
        if metadata_dir_raw.exists() and not metadata_dir_raw.is_dir():
            raise CatalogError("The .relion-mcp metadata path must be a directory.")
        metadata_dir = self.settings.resolve_project_subpath(project, ".relion-mcp")
        metadata_dir.mkdir(exist_ok=True)
        metadata_path_raw = metadata_dir / "project.json"
        if metadata_path_raw.is_symlink():
            raise CatalogError("The RELION MCP metadata file may not be a symbolic link.")
        if metadata_path_raw.exists() and not metadata_path_raw.is_file():
            raise CatalogError("The RELION MCP metadata path must be a regular file.")
        metadata_path = self.settings.resolve_project_subpath(
            project,
            ".relion-mcp/project.json",
        )
        created_at = time.time()
        if metadata_path.is_file():
            try:
                existing_record = json.loads(metadata_path.read_text(encoding="utf-8"))
                if isinstance(existing_record, dict):
                    created_at = float(existing_record.get("created_at_epoch", created_at))
            except (OSError, TypeError, ValueError):
                pass
        record = {
            "created_at_epoch": created_at,
            "pipeline_name": pipeline,
            "expected_relion_version": self.catalog.relion_version,
        }
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=metadata_dir,
            prefix=".project-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(json.dumps(record, indent=2) + "\n")
            temporary_metadata = Path(handle.name)
        try:
            temporary_metadata.replace(metadata_path)
        finally:
            temporary_metadata.unlink(missing_ok=True)
        return {
            "project_dir": str(project),
            "marker": str(marker),
            "pipeline_name": pipeline,
            "pipeline_file": str(pipeline_path),
            "pipeline_created": pipeline_created,
            "initializer_command": initializer_command,
            "initializer_stdout": initializer_stdout,
            "initializer_stderr": initializer_stderr,
            "metadata": str(metadata_path),
        }

    def render_command(
        self,
        rendered: RenderedJob,
        pipeline_name: str,
        alias: str | None,
    ) -> list[str]:
        command = [
            self.settings.pipeliner_executable,
            "--pipeline",
            _safe_name(pipeline_name, "pipeline_name"),
            "--addJob",
            rendered.relion_job_type,
            "--addJobOptions",
            rendered.option_string,
        ]
        checked_alias = safe_alias(alias)
        if checked_alias:
            command.extend(["--setJobAlias", checked_alias])
        return command

    def preflight_symmetry(
        self,
        rendered: RenderedJob,
        *,
        require_verified: bool = False,
    ) -> dict[str, Any] | None:
        """Check large semantic symmetry orders against the installed RELION build."""
        symmetry = rendered.options_by_key.get("sym_name")
        if symmetry is None:
            return None
        match = re.fullmatch(r"[CD]([1-9][0-9]*)", symmetry)
        if match is None or len(match.group(1)) <= CURRENT_RELION_SYMMETRY_ORDER_DIGITS:
            return {
                "symmetry": symmetry,
                "status": "compatible",
                "semantic_rule": SYMMETRY_SEMANTIC_RULE,
                "installed_probe": "not_required",
            }

        if len(match.group(1)) > len(str(SYMMETRY_PREFLIGHT_MAX_ORDER)):
            if not require_verified:
                return {
                    "symmetry": symmetry,
                    "status": "unverified",
                    "semantic_rule": SYMMETRY_SEMANTIC_RULE,
                    "installed_probe": "skipped",
                    "reason": "order_exceeds_safe_probe_ceiling",
                    "automatic_probe_max_order": SYMMETRY_PREFLIGHT_MAX_ORDER,
                }
            raise RelionRuntimeError(
                f"Symmetry {symmetry!r} satisfies the MCP semantic rule "
                f"({SYMMETRY_SEMANTIC_RULE}), but its order exceeds the automatic "
                f"installed-RELION probe safety ceiling of {SYMMETRY_PREFLIGHT_MAX_ORDER}. "
                "This operational ceiling is not a semantic symmetry limit. Installed "
                "compatibility remains unverified, so no job was registered."
            )

        environment = self._execution_environment()
        executable = shutil.which("relion_refine", path=environment.get("PATH", ""))
        if not executable:
            if not require_verified:
                return {
                    "symmetry": symmetry,
                    "status": "unverified",
                    "semantic_rule": SYMMETRY_SEMANTIC_RULE,
                    "installed_probe": "unavailable",
                    "reason": "relion_refine_not_found",
                }
            raise RelionRuntimeError(
                f"Symmetry preflight could not verify {symmetry!r}. It satisfies the MCP "
                f"semantic rule ({SYMMETRY_SEMANTIC_RULE}), but its order has more than "
                f"{CURRENT_RELION_SYMMETRY_ORDER_DIGITS} digits and relion_refine was not "
                "found in the configured RELION execution environment. No job was registered."
            )

        command = [executable, "--sym", symmetry, "--print_symmetry_ops"]
        timeout = max(
            1,
            min(self.settings.utility_timeout_seconds, SYMMETRY_PREFLIGHT_TIMEOUT_SECONDS),
        )
        try:
            with (
                tempfile.TemporaryDirectory(prefix="relion-symmetry-preflight-") as probe_dir,
                tempfile.TemporaryFile() as output,
            ):
                completed = subprocess.run(
                    command,
                    cwd=probe_dir,
                    stdout=subprocess.DEVNULL,
                    stderr=output,
                    timeout=timeout,
                    check=False,
                    env=environment,
                )
                output.seek(0, os.SEEK_END)
                output.seek(max(0, output.tell() - 4000))
                diagnostic = output.read().decode("utf-8", errors="replace").strip()
        except subprocess.TimeoutExpired as exc:
            if not require_verified:
                return {
                    "symmetry": symmetry,
                    "status": "unverified",
                    "semantic_rule": SYMMETRY_SEMANTIC_RULE,
                    "installed_probe": command,
                    "reason": "probe_timed_out",
                }
            raise RelionRuntimeError(
                f"Installed RELION did not complete the symmetry preflight for {symmetry!r} "
                f"within {timeout} seconds. The value satisfies the MCP semantic rule "
                f"({SYMMETRY_SEMANTIC_RULE}), but installed compatibility could not be "
                "confirmed. No job was registered."
            ) from exc
        except OSError as exc:
            if not require_verified:
                return {
                    "symmetry": symmetry,
                    "status": "unverified",
                    "semantic_rule": SYMMETRY_SEMANTIC_RULE,
                    "installed_probe": command,
                    "reason": f"probe_failed: {exc}",
                }
            raise RelionRuntimeError(
                f"Installed RELION could not run the symmetry preflight for {symmetry!r}: "
                f"{exc}. The value satisfies the MCP semantic rule "
                f"({SYMMETRY_SEMANTIC_RULE}), but no job was registered."
            ) from exc

        if completed.returncode != 0:
            rejection_detail = next(
                (
                    line.strip()
                    for line in reversed(diagnostic.splitlines())
                    if "recognize symmetry" in line.lower()
                ),
                None,
            )
            if rejection_detail is not None:
                raise RelionRuntimeError(
                    f"Installed RELION rejected symmetry {symmetry!r} during preflight "
                    f"(relion_refine exit code {completed.returncode}). The value satisfies "
                    f"the MCP semantic rule ({SYMMETRY_SEMANTIC_RULE}), but this RELION build "
                    f"does not accept that large order. No job was registered. Native diagnostic: "
                    f"{rejection_detail}"
                )
            native_detail = (
                diagnostic.splitlines()[-1].strip()
                if diagnostic
                else "no native diagnostic"
            )
            if not require_verified:
                return {
                    "symmetry": symmetry,
                    "status": "unverified",
                    "semantic_rule": SYMMETRY_SEMANTIC_RULE,
                    "installed_probe": command,
                    "reason": f"probe_exit_{completed.returncode}",
                    "native_diagnostic": native_detail,
                }
            raise RelionRuntimeError(
                f"Installed RELION symmetry preflight failed for {symmetry!r} with exit code "
                f"{completed.returncode}. The diagnostic did not identify an unsupported "
                "symmetry, so compatibility remains unverified and no job was registered. "
                f"Native diagnostic: {native_detail}"
            )

        return {
            "symmetry": symmetry,
            "status": "compatible",
            "semantic_rule": SYMMETRY_SEMANTIC_RULE,
            "installed_probe": command,
        }

    def record_initial_model_acceptance(
        self,
        project_dir: str,
        *,
        reference_volume: str,
        initial_model_particles: str,
        classification_particles: str,
        model_star: str,
        optimiser_star: str,
        data_star: str,
        evidence_artifacts: dict[str, str],
        assessment: dict[str, Any],
        geometry: dict[str, Any],
        particle_lineage: dict[str, Any],
        confirm: bool,
    ) -> dict[str, Any]:
        return _record_initial_model_acceptance(
            self.settings,
            project_dir,
            reference_volume,
            initial_model_particles,
            classification_particles,
            model_star,
            optimiser_star,
            data_star,
            evidence_artifacts,
            assessment,
            geometry,
            particle_lineage,
            confirm,
        )

    def add_job(
        self,
        project_dir: str,
        rendered: RenderedJob,
        *,
        pipeline_name: str,
        alias: str | None,
        confirm: bool,
        initial_model_acceptance: str | None = None,
    ) -> dict[str, Any]:
        if rendered.support_level not in EXECUTABLE_SUPPORT_LEVELS:
            raise CatalogError(
                f"Operation {rendered.operation} is not executable; support_level={rendered.support_level}."
            )
        self.settings.assert_execution_enabled(confirm)
        project = self.settings.validate_project_path(project_dir)
        acceptance_gate: dict[str, Any] | None = None
        is_class3d = (
            rendered.operation == "three_d_classification"
            or rendered.relion_job_type == "Class3D"
        )
        if is_class3d:
            try:
                mask_diameter_A = float(rendered.options_by_key["particle_diameter"])
                reference_volume = rendered.options_by_key["fn_ref"]
                classification_particles = rendered.options_by_key["fn_img"]
            except (KeyError, TypeError, ValueError) as exc:
                raise CatalogError(
                    "Class3D rendering is missing the reference, particles, or particle diameter required by the InitialModel acceptance gate."
                ) from exc
            acceptance_gate = require_initial_model_acceptance(
                self.settings,
                project,
                initial_model_acceptance,
                reference_volume=reference_volume,
                classification_particles=classification_particles,
                mask_diameter_A=mask_diameter_A,
                alignment=rendered.options_by_key.get("dont_skip_align", "Yes") != "No",
            )
        elif initial_model_acceptance is not None:
            raise CatalogError(
                "initial_model_acceptance is valid only for native Class3D jobs."
            )
        self._validate_scratch_policy(rendered)
        execution_environment = self._execution_environment()
        executable = self._resolve_execution_executable(
            self.settings.pipeliner_executable,
            execution_environment,
        )
        if not executable:
            raise RelionRuntimeError(f"Cannot find executable: {self.settings.pipeliner_executable}")
        symmetry_preflight = self.preflight_symmetry(rendered, require_verified=True)
        command = self.render_command(rendered, pipeline_name, alias)
        command[0] = executable

        pipeline_path = self.pipeline_path(project, pipeline_name)
        with self._pipeline_add_lock(project, pipeline_name):
            before = [row["name"] for row in pipeline_processes(pipeline_path)] if pipeline_path.exists() else []
            try:
                completed = subprocess.run(
                    command,
                    cwd=project,
                    capture_output=True,
                    text=True,
                    timeout=self.settings.add_job_timeout_seconds,
                    check=False,
                    env=execution_environment,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RelionRuntimeError(f"relion_pipeliner could not add the job: {exc}") from exc
            if completed.returncode != 0:
                raise RelionRuntimeError(
                    f"relion_pipeliner failed with exit code {completed.returncode}:\n{completed.stderr[-4000:]}"
                )
            after = [row["name"] for row in pipeline_processes(pipeline_path)] if pipeline_path.exists() else []
            added = [name for name in after if name not in before]
            if not added:
                raise RelionRuntimeError(
                    "relion_pipeliner returned success but no new pipeline process was detected."
                )
            job = added[-1]
        return {
            "project_dir": str(project),
            "pipeline_name": pipeline_name,
            "job": job,
            "command": command,
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-4000:],
            "symmetry_preflight": symmetry_preflight,
            "initial_model_acceptance_gate": acceptance_gate,
        }

    def start_jobs(
        self,
        project_dir: str,
        jobs: list[str],
        schedule_name: str,
        pipeline_name: str,
        confirm: bool,
    ) -> dict[str, Any]:
        self.settings.assert_execution_enabled(confirm)
        project = self.settings.validate_project_path(project_dir)
        execution_environment = self._execution_environment()
        executable = self._resolve_execution_executable(
            self.settings.pipeliner_executable,
            execution_environment,
        )
        if not executable:
            raise RelionRuntimeError(f"Cannot find executable: {self.settings.pipeliner_executable}")
        checked_jobs = [_safe_job(job) for job in jobs]
        if not checked_jobs:
            raise CatalogError("At least one job is required.")
        for job in checked_jobs:
            self.settings.resolve_project_subpath(project, job)
        acceptance_gates: list[dict[str, Any]] = []
        for job in checked_jobs:
            inputs = registered_class3d_inputs(self.settings, project, job)
            if inputs is None:
                continue
            gate = find_initial_model_acceptance(
                self.settings,
                project,
                reference_volume=inputs["reference_volume"],
                classification_particles=inputs["classification_particles"],
                mask_diameter_A=inputs["mask_diameter_A"],
                alignment=inputs["alignment"],
            )
            acceptance_gates.append({"job": job, **gate})
        schedule = _safe_name(schedule_name, "schedule_name")
        pipeline = _safe_name(pipeline_name, "pipeline_name")
        command = [
            executable,
            "--pipeline",
            pipeline,
            "--schedule",
            schedule,
            "--repeat",
            "1",
            "--min_wait",
            "0",
            "--RunJobs",
            " ".join(checked_jobs) + " ",
        ]
        log_dir = self.settings.resolve_project_subpath(project, "Log")
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.settings.resolve_project_subpath(
            project,
            f"Log/pipeline_{schedule}.log",
        )
        diagnostic_log_path = self.settings.resolve_project_subpath(
            project,
            f"Log/pipeline_{schedule}.diagnostic.log",
        )
        launcher = [
            sys.executable,
            str(Path(__file__).with_name("_schedule_runner.py")),
            str(project),
            schedule,
            str(diagnostic_log_path),
            "--",
            *command,
        ]
        try:
            process = subprocess.Popen(
                launcher,
                cwd=project,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=execution_environment,
            )
        except OSError as exc:
            raise RelionRuntimeError(
                f"Could not launch the RELION scheduler helper: {exc}"
            ) from exc
        return {
            "project_dir": str(project),
            "schedule_name": schedule,
            "jobs": checked_jobs,
            "pid": process.pid,
            "log": str(log_path),
            "diagnostic_log": str(diagnostic_log_path),
            "command": command,
            "initial_model_acceptance_gates": acceptance_gates,
        }

    def run_job(
        self,
        project_dir: str,
        rendered: RenderedJob,
        *,
        pipeline_name: str,
        alias: str | None,
        schedule_name: str | None,
        confirm: bool,
        initial_model_acceptance: str | None = None,
    ) -> dict[str, Any]:
        """Register and start one rendered job under one bounded confirmation."""
        self.settings.assert_execution_enabled(confirm)
        checked_alias = safe_alias(alias)
        selected_schedule = (
            _derived_alias(checked_alias or rendered.operation, "run")
            if schedule_name is None
            else schedule_name
        )
        schedule = _safe_name(selected_schedule, "schedule_name")
        added = self.add_job(
            project_dir,
            rendered,
            pipeline_name=pipeline_name,
            alias=checked_alias,
            confirm=True,
            initial_model_acceptance=initial_model_acceptance,
        )
        job_value = added.get("job")
        if not isinstance(job_value, str):
            raise RelionRuntimeError("RELION did not report the newly added job path.")
        job = _safe_job(job_value)
        try:
            started = self.start_jobs(
                project_dir,
                [job],
                schedule,
                pipeline_name,
                confirm=True,
            )
        except Exception as exc:
            raise SegmentStartError([job], schedule, str(exc)) from exc
        return {
            "operation": rendered.operation,
            "project_dir": added["project_dir"],
            "pipeline_name": pipeline_name,
            "job": job,
            "jobs_to_start": [job],
            "schedule_name": schedule,
            "pipeline_registered": True,
            "started": True,
            "add_result": added,
            "start_result": started,
        }

    def _verify_job_outputs(
        self,
        project: Path,
        job: str,
        operation: str,
        expected_particle_count: int | None,
    ) -> dict[str, Any]:
        """Verify operation-specific output edges without changing native status.

        RELION can exit successfully after an Import job matched zero raw inputs.
        The native marker and pipeline status remain useful provenance, but they
        are not sufficient evidence that the requested logical operation
        produced a usable result. This verifier deliberately remains separate
        from those native fields.
        """
        resolved_operation = self.catalog.resolve_operation(operation)
        mapping = self.catalog.mapping(resolved_operation)
        expected_result_types = [
            str(value) for value in (mapping.get("expected_results") or [])
        ]
        if expected_particle_count is not None:
            if (
                isinstance(expected_particle_count, bool)
                or not isinstance(expected_particle_count, int)
                or expected_particle_count < 1
            ):
                raise CatalogError("expected_particle_count must be a positive integer.")
            if resolved_operation != "import_particles":
                raise CatalogError(
                    "expected_particle_count is supported only for import_particles."
                )

        issues: list[dict[str, Any]] = []

        def add_issue(code: str, message: str, **details: Any) -> None:
            issue: dict[str, Any] = {"code": code, "message": message}
            issue.update(details)
            issues.append(issue)

        expected_job_type = str(mapping.get("relion_job_type") or "")
        actual_job_type = PurePosixPath(job).parts[0]
        if expected_job_type and actual_job_type.casefold() != expected_job_type.casefold():
            add_issue(
                "job_type_mismatch",
                f"Operation {resolved_operation!r} expects a {expected_job_type} job, "
                f"not {actual_job_type}.",
                expected_job_type=expected_job_type,
                actual_job_type=actual_job_type,
            )

        try:
            output_info = self.job_outputs(str(project), job)
        except (OSError, StarFormatError, ValueError) as exc:
            add_issue(
                "job_pipeline_unreadable",
                f"Could not read the job output graph: {exc}",
            )
            output_info = {
                "job_pipeline_star": str(
                    self.settings.resolve_project_subpath(
                        project,
                        f"{job}job_pipeline.star",
                    )
                ),
                "exists": False,
                "outputs": [],
            }

        all_outputs = list(output_info["outputs"])
        matched_outputs: list[dict[str, Any]] = []
        if not output_info["exists"]:
            add_issue(
                "job_pipeline_missing",
                "The job_pipeline.star output graph does not exist.",
                path=output_info["job_pipeline_star"],
            )

        def probe_output(
            candidate: dict[str, Any],
            *,
            validate_particles: bool,
        ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
            path_text = str(candidate["path"])
            result: dict[str, Any] = {
                "path": path_text,
                "result_types": list(candidate["result_types"]),
                "exists": False,
            }
            local_issues: list[dict[str, Any]] = []
            try:
                path = self.settings.resolve_project_subpath(
                    project,
                    path_text,
                    must_exist=True,
                )
            except ConfigurationError as exc:
                local_issues.append(
                    {
                        "code": "output_file_unavailable",
                        "message": f"Expected output is unavailable: {exc}",
                        "path": path_text,
                    }
                )
                return result, local_issues
            if not path.is_file():
                local_issues.append(
                    {
                        "code": "output_not_regular_file",
                        "message": "Expected output is not a regular file.",
                        "path": path_text,
                    }
                )
                return result, local_issues

            result["exists"] = True
            if not validate_particles:
                return result, local_issues
            try:
                summary = particle_star_row_summary(
                    path,
                    track_duplicate_image_names=False,
                )
            except (OSError, StarFormatError) as exc:
                local_issues.append(
                    {
                        "code": "invalid_particle_star",
                        "message": (
                            "Particle output is not a valid RELION particle STAR: "
                            f"{exc}"
                        ),
                        "path": path_text,
                    }
                )
                return result, local_issues

            result["particle_count"] = summary.particle_count
            if summary.particle_count < 1:
                local_issues.append(
                    {
                        "code": "empty_particle_table",
                        "message": "Particle output contains no data_particles rows.",
                        "path": path_text,
                        "particle_count": summary.particle_count,
                    }
                )
            if (
                expected_particle_count is not None
                and summary.particle_count != expected_particle_count
            ):
                local_issues.append(
                    {
                        "code": "particle_count_mismatch",
                        "message": (
                            "Particle output count does not match the expected count."
                        ),
                        "path": path_text,
                        "particle_count": summary.particle_count,
                        "expected_particle_count": expected_particle_count,
                    }
                )
            return result, local_issues

        if resolved_operation == "import_particles":
            expected_edge = str(PurePosixPath(job) / "particles.star")
            movie_edges = [
                str(item["path"])
                for item in all_outputs
                if "imported_movies" in item["result_types"]
                or PurePosixPath(str(item["path"])).name.casefold()
                in {"movies.star", "micrographs.star"}
            ]
            if movie_edges:
                add_issue(
                    "unexpected_movie_output_edge",
                    (
                        "Particle import registered movie or micrograph output "
                        "instead of only particles."
                    ),
                    paths=movie_edges,
                )
            particle_candidates = [
                item
                for item in all_outputs
                if str(PurePosixPath(str(item["path"]))) == expected_edge
            ]
            if not particle_candidates:
                add_issue(
                    "expected_particle_output_edge_missing",
                    f"The job output graph does not contain {expected_edge}.",
                    expected_path=expected_edge,
                )
            else:
                matched, local_issues = probe_output(
                    particle_candidates[0],
                    validate_particles=True,
                )
                matched_outputs.append(matched)
                issues.extend(local_issues)
        elif not expected_result_types:
            add_issue(
                "expected_result_contract_missing",
                f"Operation {resolved_operation!r} declares no expected result types.",
            )
        else:
            for result_type in expected_result_types:
                candidates = [
                    item for item in all_outputs if result_type in item["result_types"]
                ]
                if not candidates:
                    add_issue(
                        "expected_result_edge_missing",
                        f"The job output graph has no {result_type!r} output edge.",
                        result_type=result_type,
                    )
                    continue
                probes = [
                    probe_output(candidate, validate_particles=False)
                    for candidate in candidates
                ]
                usable = next((probe for probe in probes if not probe[1]), None)
                if usable is not None:
                    matched_outputs.append(usable[0])
                else:
                    matched_outputs.append(probes[0][0])
                    issues.extend(probes[0][1])

        particle_count = next(
            (
                int(item["particle_count"])
                for item in matched_outputs
                if "particle_count" in item
            ),
            None,
        )
        return {
            "requested_operation": operation,
            "operation": resolved_operation,
            "expected_job_type": expected_job_type or None,
            "expected_result_types": expected_result_types,
            "expected_particle_count": expected_particle_count,
            "job_pipeline_star": output_info["job_pipeline_star"],
            "all_outputs": all_outputs,
            "matched_outputs": matched_outputs,
            "particle_count": particle_count,
            "output_verified": not issues,
            "issues": issues,
        }

    def job_status(
        self,
        project_dir: str,
        job: str,
        pipeline_name: str,
        operation: str | None = None,
        expected_particle_count: int | None = None,
    ) -> dict[str, Any]:
        project = self.settings.validate_project_path(project_dir)
        checked = _safe_job(job)
        job_dir = self.settings.resolve_project_subpath(project, checked)
        marker_status = "Unknown"
        markers = {
            status: self.settings.resolve_project_subpath(project, f"{checked}{filename}")
            for status, filename in (
                ("Succeeded", "RELION_JOB_EXIT_SUCCESS"),
                ("Failed", "RELION_JOB_EXIT_FAILURE"),
                ("Aborted", "RELION_JOB_EXIT_ABORTED"),
            )
        }
        for status, marker in markers.items():
            if marker.exists():
                marker_status = status
                break
        pipeline_status = None
        pipeline_path = self.pipeline_path(project, pipeline_name)
        if pipeline_path.exists():
            target = checked
            for row in pipeline_processes(pipeline_path):
                if row["name"] == target or row["name"].rstrip("/") == target.rstrip("/"):
                    pipeline_status = row["status"]
                    break
        result = {
            "job": checked,
            "job_dir": str(job_dir),
            "exists": job_dir.exists(),
            "marker_status": marker_status,
            "pipeline_status": pipeline_status,
            "run_out": str(self.settings.resolve_project_subpath(project, f"{checked}run.out")),
            "run_err": str(self.settings.resolve_project_subpath(project, f"{checked}run.err")),
        }
        if operation is None:
            if expected_particle_count is not None:
                raise CatalogError(
                    "operation is required when expected_particle_count is provided."
                )
            return result

        verification = self._verify_job_outputs(
            project,
            checked,
            operation,
            expected_particle_count,
        )
        marker_succeeded = marker_status.casefold() == "succeeded"
        pipeline_succeeded = (
            isinstance(pipeline_status, str)
            and pipeline_status.casefold() == "succeeded"
        )
        native_succeeded = marker_succeeded and pipeline_succeeded
        output_verified = bool(verification["output_verified"])
        terminal_failure = marker_status.casefold() in {"failed", "aborted"} or (
            isinstance(pipeline_status, str)
            and pipeline_status.casefold() in {"failed", "aborted"}
        )
        if native_succeeded and output_verified:
            completion_status = "verified"
        elif terminal_failure:
            completion_status = "native_failed"
        elif native_succeeded:
            completion_status = "output_failed"
        else:
            completion_status = "pending"
        verification["native_succeeded"] = native_succeeded
        result.update(
            {
                "output_verification": verification,
                "completion_status": completion_status,
                "completion_verified": native_succeeded and output_verified,
            }
        )
        return result

    def job_outputs(self, project_dir: str, job: str) -> dict[str, Any]:
        project = self.settings.validate_project_path(project_dir)
        checked = _safe_job(job)
        star_path = self.settings.resolve_project_subpath(
            project,
            f"{checked}job_pipeline.star",
        )
        if not star_path.exists():
            return {"job": checked, "job_pipeline_star": str(star_path), "outputs": [], "exists": False}
        outputs = job_output_edges(star_path)
        return {
            "job": checked,
            "job_pipeline_star": str(star_path),
            "exists": True,
            "outputs": classify_outputs(outputs),
        }

    def tail_job_log(self, project_dir: str, job: str, stream: str, lines: int) -> dict[str, Any]:
        project = self.settings.validate_project_path(project_dir)
        checked = _safe_job(job)
        if stream not in {"out", "err"}:
            raise CatalogError("stream must be 'out' or 'err'.")
        if lines < 1 or lines > 1000:
            raise CatalogError("lines must be between 1 and 1000.")
        path = self.settings.resolve_project_subpath(project, f"{checked}run.{stream}")
        if not path.exists():
            return {"path": str(path), "exists": False, "text": ""}
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            content = deque((line.rstrip("\n") for line in handle), maxlen=lines)
        return {"path": str(path), "exists": True, "text": "\n".join(content)}

    def abort_job(self, project_dir: str, job: str, confirm: bool) -> dict[str, Any]:
        self.settings.assert_execution_enabled(confirm)
        project = self.settings.validate_project_path(project_dir)
        checked = _safe_job(job)
        job_dir = self.settings.resolve_project_subpath(project, checked, must_exist=True)
        if not job_dir.is_dir():
            raise CatalogError(f"Job directory does not exist: {job_dir}")
        marker = self.settings.resolve_project_subpath(
            project,
            f"{checked}RELION_JOB_ABORT_NOW",
        )
        created = not marker.exists()
        marker.touch(exist_ok=True)
        return {"job": checked, "abort_marker": str(marker), "created": created}

    def _prepare_native_particle_class_selection(
        self,
        project_dir: str,
        particles_star: str,
        selected_class_numbers: list[int],
        alias: str,
        pipeline_name: str,
        materialization_mode: str = "auto",
        class_source_star: str | None = None,
    ) -> dict[str, Any]:
        project = self.settings.validate_project_path(project_dir)
        checked_input = safe_project_relative(particles_star)
        try:
            input_path = self.settings.resolve_project_subpath(
                project,
                checked_input,
                must_exist=True,
            )
        except ConfigurationError as exc:
            raise CatalogError(f"Input particle STAR is unavailable: {project / checked_input}") from exc
        if not input_path.is_file():
            raise CatalogError(f"Input particle STAR is not a file: {input_path}")

        classes = _normalise_class_numbers(selected_class_numbers)
        checked_alias = _safe_name(alias, "alias")
        checked_pipeline = _safe_name(pipeline_name, "pipeline_name")
        if materialization_mode not in CLASS_SELECTION_MATERIALIZATION_MODES:
            raise CatalogError(
                "materialization_mode must be 'auto', 'gui_equivalent', or 'select_joinstar'."
            )
        class_selector = self._resolve_execution_executable(
            self.settings.class_selector_executable
        )
        selected_mode = materialization_mode
        fallback_reason: str | None = None
        if selected_mode == "auto":
            if class_selector:
                selected_mode = "gui_equivalent"
            else:
                selected_mode = "select_joinstar"
                fallback_reason = "configured_gui_equivalent_selector_not_found"
        if selected_mode == "gui_equivalent" and not class_selector:
            raise CatalogError(
                "GUI-equivalent class selection is unavailable because the configured "
                f"selector executable was not found: {self.settings.class_selector_executable!r}. "
                "Install the package console script, configure RELION_CLASS_SELECTOR, or use "
                "materialization_mode='select_joinstar'."
            )
        try:
            source_summary = particle_star_summary(input_path)
        except StarFormatError as exc:
            raise CatalogError(str(exc)) from exc
        if source_summary.duplicate_image_name_count:
            examples = ", ".join(source_summary.duplicate_image_name_examples)
            raise CatalogError(
                "Input particle STAR contains duplicate rlnImageName values"
                + (f" (examples: {examples})" if examples else "")
                + "."
            )
        missing_classes = [value for value in classes if source_summary.class_counts.get(value, 0) == 0]
        if missing_classes:
            raise CatalogError(
                f"Requested classes are absent from the input particle STAR: {missing_classes}. "
                f"Available classes: {sorted(source_summary.class_counts)}"
            )
        checked_class_source: str | None = None
        class_model_star: str | None = None
        class_averages_expected = False
        if class_source_star is not None:
            if selected_mode != "gui_equivalent":
                raise CatalogError(
                    "class_source_star is supported only by materialization_mode='gui_equivalent'."
                )
            (
                checked_class_source,
                class_model_star,
                class_averages_expected,
            ) = self._resolve_class_model_source(
                project,
                input_path,
                class_source_star,
                classes,
            )
        return {
            "project": project,
            "checked_input": checked_input,
            "classes": classes,
            "alias": checked_alias,
            "pipeline_name": checked_pipeline,
            "source_summary": source_summary,
            "materialization_mode": selected_mode,
            "requested_materialization_mode": materialization_mode,
            "materialization_fallback_reason": fallback_reason,
            "class_selector": class_selector,
            "class_source_star": checked_class_source,
            "class_model_star": class_model_star,
            "class_averages_expected": class_averages_expected,
        }

    def _resolve_class_model_source(
        self,
        project: Path,
        particles_path: Path,
        class_source_star: str,
        selected_classes: list[int],
    ) -> tuple[str, str, bool]:
        checked_source = safe_project_relative(class_source_star)
        try:
            source_path = self.settings.resolve_project_subpath(
                project,
                checked_source,
                must_exist=True,
            )
        except ConfigurationError as exc:
            raise CatalogError(f"Class source STAR is unavailable: {project / checked_source}") from exc
        if not source_path.is_file():
            raise CatalogError(f"Class source STAR is not a file: {source_path}")

        optimiser = parse_star_block(source_path, "optimiser_general")
        model_path = source_path
        checked_model = checked_source
        if optimiser is not None:
            model_value = optimiser.get("rlnModelStarFile")
            if not isinstance(model_value, str) or not model_value:
                raise CatalogError(f"Optimiser STAR lacks rlnModelStarFile: {source_path}")
            checked_model = safe_project_relative(model_value)
            try:
                model_path = self.settings.resolve_project_subpath(
                    project,
                    checked_model,
                    must_exist=True,
                )
            except ConfigurationError as exc:
                raise CatalogError(
                    f"Model STAR referenced by optimiser is unavailable: {project / checked_model}"
                ) from exc
            data_value = optimiser.get("rlnExperimentalDataStarFile")
            if isinstance(data_value, str) and data_value:
                checked_data = safe_project_relative(data_value)
                try:
                    referenced_data = self.settings.resolve_project_subpath(
                        project,
                        checked_data,
                        must_exist=True,
                    )
                except ConfigurationError as exc:
                    raise CatalogError(
                        "Particle STAR referenced by optimiser is unavailable: "
                        f"{project / checked_data}"
                    ) from exc
                if referenced_data != particles_path:
                    raise CatalogError(
                        "class_source_star and particles_star do not describe the same "
                        "classification result."
                    )

        classes = parse_star_block(model_path, "model_classes")
        if not classes or "rlnReferenceImage" not in classes:
            raise CatalogError(f"Class model lacks data_model_classes/rlnReferenceImage: {model_path}")
        references = classes["rlnReferenceImage"]
        if isinstance(references, str):
            references = [references]
        available: set[int] = set()
        selected_references: list[str] = []
        selected_set = set(selected_classes)
        for index, reference in enumerate(references):
            match = re.match(r"0*([1-9][0-9]*)@", str(reference))
            class_number = int(match.group(1)) if match else index + 1
            available.add(class_number)
            if class_number in selected_set:
                selected_references.append(str(reference))
        missing = sorted(selected_set - available)
        if missing:
            raise CatalogError(f"Requested classes are absent from class model: {missing}")
        general = parse_star_block(model_path, "model_general") or {}
        dimensionality = str(general.get("rlnReferenceDimensionality", ""))
        class_averages_expected = dimensionality == "2" or (
            not dimensionality
            and all(reference.lower().endswith(".mrcs") for reference in selected_references)
        )
        return checked_source, checked_model, class_averages_expected

    def _render_gui_equivalent_class_select(self, prepared: dict[str, Any]) -> RenderedJob:
        options = {
            "fn_exe": str(prepared["class_selector"]),
            "in_part": prepared["checked_input"],
            "param1_label": "classes",
            "param1_value": ",".join(str(value) for value in prepared["classes"]),
            "param2_label": "expected-particles",
            "param2_value": str(
                sum(
                    prepared["source_summary"].class_counts[value]
                    for value in prepared["classes"]
                )
            ),
        }
        if prepared["class_model_star"] is not None:
            options.update(
                {
                    "param3_label": "class-model",
                    "param3_value": prepared["class_model_star"],
                }
            )
        for value in options.values():
            if not re.fullmatch(r"[A-Za-z0-9_./+,:-]+", value):
                raise CatalogError(
                    "GUI-equivalent class-selection arguments may contain only letters, "
                    "digits, '_', '.', '/', '+', ',', ':', and '-'."
                )
        manifest = self.catalog.manifest_options("External")
        assignments = [
            f"{manifest[key]['gui_label']} == {value}" for key, value in options.items()
        ]
        return RenderedJob(
            requested_operation="gui_equivalent_particle_class_selection",
            operation="gui_equivalent_particle_class_selection",
            relion_job_type="External",
            support_level="schedule_mappable",
            options_by_key=options,
            option_assignments=assignments,
        )

    def _render_native_class_select(self, particles_star: str, class_number: int) -> RenderedJob:
        return self.catalog.render_job(
            "particle_selection",
            inputs={"particles": particles_star},
            params={
                "select_by_metadata_values": True,
                "selection_label": "rlnClassNumber",
                "selection_minimum": class_number,
                "selection_maximum": class_number,
            },
            include_defaults=False,
        )

    def _render_native_particle_join(self, particle_stars: list[str]) -> RenderedJob:
        inputs = {
            f"particle_star_{index}": path
            for index, path in enumerate(particle_stars, start=1)
        }
        return self.catalog.render_job(
            "join_star",
            inputs=inputs,
            params={"combine_particles": True},
            include_defaults=False,
        )

    def _native_particle_class_selection_plan(self, prepared: dict[str, Any]) -> dict[str, Any]:
        classes: list[int] = prepared["classes"]
        base_alias: str = prepared["alias"]
        pipeline_name: str = prepared["pipeline_name"]
        source: ParticleStarSummary = prepared["source_summary"]
        select_stages: list[dict[str, Any]] = []
        pending_outputs: list[str] = []
        start_order_stage_ids: list[str] = []
        aliases: list[str] = []

        for class_number in classes:
            stage_id = f"select_class_{class_number:03d}"
            stage_alias = _derived_alias(base_alias, f"class{class_number:03d}")
            rendered = self._render_native_class_select(prepared["checked_input"], class_number)
            placeholder_output = f"Select/PENDING_class{class_number:03d}/particles.star"
            select_stages.append(
                {
                    "stage_id": stage_id,
                    "class_number": class_number,
                    "expected_particle_count": source.class_counts[class_number],
                    "alias": stage_alias,
                    "job_type": rendered.relion_job_type,
                    "options_by_manifest_key": rendered.options_by_key,
                    "option_assignments": rendered.option_assignments,
                    "command": self.render_command(rendered, pipeline_name, stage_alias),
                    "expected_output": placeholder_output,
                }
            )
            aliases.append(stage_alias)
            pending_outputs.append(placeholder_output)
            start_order_stage_ids.append(stage_id)

        join_stages: list[dict[str, Any]] = []
        level = 1
        while len(pending_outputs) > 1:
            next_outputs: list[str] = []
            join_position = 0
            for offset in range(0, len(pending_outputs), 4):
                chunk = pending_outputs[offset : offset + 4]
                if len(chunk) == 1:
                    next_outputs.extend(chunk)
                    continue
                join_position += 1
                stage_id = f"join_level_{level:02d}_{join_position:02d}"
                stage_alias = _derived_alias(
                    base_alias, f"join_l{level:02d}_{join_position:02d}"
                )
                rendered = self._render_native_particle_join(chunk)
                placeholder_output = (
                    f"JoinStar/PENDING_l{level:02d}_{join_position:02d}/join_particles.star"
                )
                join_stages.append(
                    {
                        "stage_id": stage_id,
                        "level": level,
                        "position": join_position,
                        "alias": stage_alias,
                        "job_type": rendered.relion_job_type,
                        "inputs": chunk,
                        "options_by_manifest_key": rendered.options_by_key,
                        "option_assignments": rendered.option_assignments,
                        "command": self.render_command(rendered, pipeline_name, stage_alias),
                        "expected_output": placeholder_output,
                    }
                )
                aliases.append(stage_alias)
                next_outputs.append(placeholder_output)
                start_order_stage_ids.append(stage_id)
            pending_outputs = next_outputs
            level += 1

        if len(aliases) != len(set(aliases)):
            raise CatalogError("Derived class-selection job aliases collide.")
        return {
            "operation": "native_particle_class_selection",
            "execution_model": "relion_pipeline_select_then_joinstar",
            "materialization_mode": "select_joinstar",
            "requested_materialization_mode": prepared["requested_materialization_mode"],
            "materialization_fallback_reason": prepared["materialization_fallback_reason"],
            "project_dir": str(prepared["project"]),
            "pipeline_name": pipeline_name,
            "particles_star": prepared["checked_input"],
            "selected_class_numbers": classes,
            "source_particle_count": source.particle_count,
            "source_class_counts": source.class_counts,
            "selected_class_counts": {value: source.class_counts[value] for value in classes},
            "expected_particle_count": sum(source.class_counts[value] for value in classes),
            "select_stages": select_stages,
            "join_stages": join_stages,
            "planned_job_count": len(select_stages) + len(join_stages),
            "start_order_stage_ids": start_order_stage_ids,
            "expected_final_output": pending_outputs[0],
            "suggested_schedule_name": _derived_alias(base_alias, "run"),
            "pipeline_registered": False,
            "started": False,
            "next_write_tool": "relion_run_particle_class_selection",
            "advanced_add_only_tool": "relion_add_particle_class_selection_jobs",
        }

    def _gui_equivalent_particle_class_selection_plan(
        self, prepared: dict[str, Any]
    ) -> dict[str, Any]:
        rendered = self._render_gui_equivalent_class_select(prepared)
        source: ParticleStarSummary = prepared["source_summary"]
        expected_particle_count = sum(
            source.class_counts[value] for value in prepared["classes"]
        )
        stage_alias = prepared["alias"]
        stage = {
            "stage_id": "materialize_selected_classes",
            "class_numbers": prepared["classes"],
            "expected_particle_count": expected_particle_count,
            "alias": stage_alias,
            "job_type": rendered.relion_job_type,
            "options_by_manifest_key": rendered.options_by_key,
            "option_assignments": rendered.option_assignments,
            "command": self.render_command(rendered, prepared["pipeline_name"], stage_alias),
            "expected_output": "External/PENDING/particles.star",
            "expected_auxiliary_outputs": [
                "External/PENDING/backup_selection.star",
                "External/PENDING/selection_provenance.json",
                "External/PENDING/RELION_OUTPUT_NODES.star",
                *(
                    ["External/PENDING/class_averages.star"]
                    if prepared["class_averages_expected"]
                    else []
                ),
            ],
        }
        return {
            "operation": "native_particle_class_selection",
            "execution_model": "relion_external_gui_equivalent",
            "materialization_mode": "gui_equivalent",
            "requested_materialization_mode": prepared["requested_materialization_mode"],
            "materialization_fallback_reason": None,
            "project_dir": str(prepared["project"]),
            "pipeline_name": prepared["pipeline_name"],
            "particles_star": prepared["checked_input"],
            "class_source_star": prepared["class_source_star"],
            "class_model_star": prepared["class_model_star"],
            "selected_class_numbers": prepared["classes"],
            "source_particle_count": source.particle_count,
            "source_class_counts": source.class_counts,
            "selected_class_counts": {
                value: source.class_counts[value] for value in prepared["classes"]
            },
            "expected_particle_count": expected_particle_count,
            "select_stages": [stage],
            "join_stages": [],
            "planned_job_count": 1,
            "start_order_stage_ids": [stage["stage_id"]],
            "expected_final_output": stage["expected_output"],
            "expected_auxiliary_outputs": stage["expected_auxiliary_outputs"],
            "suggested_schedule_name": _derived_alias(prepared["alias"], "run"),
            "pipeline_registered": False,
            "started": False,
            "next_write_tool": "relion_run_particle_class_selection",
            "advanced_add_only_tool": "relion_add_particle_class_selection_jobs",
        }

    def plan_particle_class_selection(
        self,
        project_dir: str,
        particles_star: str,
        selected_class_numbers: list[int],
        alias: str,
        pipeline_name: str,
        materialization_mode: str = "auto",
        class_source_star: str | None = None,
    ) -> dict[str, Any]:
        """Render a one-job GUI-equivalent selection or the native fallback graph."""
        prepared = self._prepare_native_particle_class_selection(
            project_dir,
            particles_star,
            selected_class_numbers,
            alias,
            pipeline_name,
            materialization_mode,
            class_source_star,
        )
        if prepared["materialization_mode"] == "gui_equivalent":
            return self._gui_equivalent_particle_class_selection_plan(prepared)
        return self._native_particle_class_selection_plan(prepared)

    def add_particle_class_selection_jobs(
        self,
        project_dir: str,
        particles_star: str,
        selected_class_numbers: list[int],
        alias: str,
        pipeline_name: str,
        confirm: bool,
        materialization_mode: str = "auto",
        class_source_star: str | None = None,
    ) -> dict[str, Any]:
        """Add the selected exact-class materialization graph without starting it."""
        self.settings.assert_execution_enabled(confirm)
        prepared = self._prepare_native_particle_class_selection(
            project_dir,
            particles_star,
            selected_class_numbers,
            alias,
            pipeline_name,
            materialization_mode,
            class_source_star,
        )
        if prepared["materialization_mode"] == "gui_equivalent":
            plan = self._gui_equivalent_particle_class_selection_plan(prepared)
            rendered = self._render_gui_equivalent_class_select(prepared)
            result = self.add_job(
                project_dir,
                rendered,
                pipeline_name=prepared["pipeline_name"],
                alias=prepared["alias"],
                confirm=confirm,
            )
            job_value = result.get("job")
            if not isinstance(job_value, str):
                raise RelionRuntimeError("RELION did not report the newly added job path.")
            job = _safe_job(job_value)
            if PurePosixPath(job).parts[0] != "External":
                raise RelionRuntimeError(
                    f"RELION reported {job!r} for expected External job."
                )
            output = f"{job}particles.star"
            auxiliary_outputs = [
                f"{job}backup_selection.star",
                f"{job}selection_provenance.json",
                f"{job}RELION_OUTPUT_NODES.star",
                *(
                    [f"{job}class_averages.star"]
                    if prepared["class_averages_expected"]
                    else []
                ),
            ]
            entry = {
                "stage_id": "materialize_selected_classes",
                "class_numbers": prepared["classes"],
                "expected_particle_count": plan["expected_particle_count"],
                "alias": prepared["alias"],
                "job": job,
                "output": output,
                "auxiliary_outputs": auxiliary_outputs,
                "add_result": result,
            }
            return {
                "operation": "native_particle_class_selection",
                "execution_model": "relion_external_gui_equivalent",
                "materialization_mode": "gui_equivalent",
                "requested_materialization_mode": prepared["requested_materialization_mode"],
                "materialization_fallback_reason": None,
                "project_dir": str(prepared["project"]),
                "pipeline_name": prepared["pipeline_name"],
                "particles_star_input": prepared["checked_input"],
                "class_source_star": prepared["class_source_star"],
                "class_model_star": prepared["class_model_star"],
                "selected_class_numbers": prepared["classes"],
                "selected_class_counts": plan["selected_class_counts"],
                "expected_particle_count": plan["expected_particle_count"],
                "select_jobs": [entry],
                "join_jobs": [],
                "jobs_to_start": [job],
                "final_stage_id": entry["stage_id"],
                "final_job": job,
                "particles_star": output,
                "auxiliary_outputs": auxiliary_outputs,
                "suggested_schedule_name": plan["suggested_schedule_name"],
                "pipeline_registered": True,
                "started": False,
                "next_write_tool": "relion_start_jobs",
            }
        plan = self._native_particle_class_selection_plan(prepared)
        added_jobs: list[str] = []
        select_jobs: list[dict[str, Any]] = []
        join_jobs: list[dict[str, Any]] = []

        def add_stage(
            stage_id: str,
            stage_alias: str,
            rendered: RenderedJob,
            expected_job_type: str,
        ) -> tuple[str, dict[str, Any]]:
            try:
                result = self.add_job(
                    project_dir,
                    rendered,
                    pipeline_name=prepared["pipeline_name"],
                    alias=stage_alias,
                    confirm=confirm,
                )
                job_value = result.get("job")
                if not isinstance(job_value, str):
                    raise RelionRuntimeError("RELION did not report the newly added job path.")
                job = _safe_job(job_value)
                added_jobs.append(job)
                if PurePosixPath(job).parts[0] != expected_job_type:
                    raise RelionRuntimeError(
                        f"RELION reported {job!r} for expected {expected_job_type} job."
                    )
                return job, result
            except Exception as exc:
                if added_jobs:
                    raise PartialPipelineAddError(added_jobs, stage_id, str(exc)) from exc
                raise

        pending: list[dict[str, str]] = []
        for class_number in prepared["classes"]:
            stage_id = f"select_class_{class_number:03d}"
            stage_alias = _derived_alias(prepared["alias"], f"class{class_number:03d}")
            rendered = self._render_native_class_select(prepared["checked_input"], class_number)
            job, result = add_stage(stage_id, stage_alias, rendered, "Select")
            output = f"{job}particles.star"
            entry = {
                "stage_id": stage_id,
                "class_number": class_number,
                "expected_particle_count": prepared["source_summary"].class_counts[class_number],
                "alias": stage_alias,
                "job": job,
                "output": output,
                "add_result": result,
            }
            select_jobs.append(entry)
            pending.append({"stage_id": stage_id, "job": job, "output": output})

        level = 1
        while len(pending) > 1:
            next_pending: list[dict[str, str]] = []
            join_position = 0
            for offset in range(0, len(pending), 4):
                chunk = pending[offset : offset + 4]
                if len(chunk) == 1:
                    next_pending.extend(chunk)
                    continue
                join_position += 1
                stage_id = f"join_level_{level:02d}_{join_position:02d}"
                stage_alias = _derived_alias(
                    prepared["alias"], f"join_l{level:02d}_{join_position:02d}"
                )
                input_paths = [item["output"] for item in chunk]
                rendered = self._render_native_particle_join(input_paths)
                job, result = add_stage(stage_id, stage_alias, rendered, "JoinStar")
                output = f"{job}join_particles.star"
                entry = {
                    "stage_id": stage_id,
                    "level": level,
                    "position": join_position,
                    "alias": stage_alias,
                    "job": job,
                    "inputs": input_paths,
                    "output": output,
                    "add_result": result,
                }
                join_jobs.append(entry)
                next_pending.append({"stage_id": stage_id, "job": job, "output": output})
            pending = next_pending
            level += 1

        final = pending[0]
        return {
            "operation": "native_particle_class_selection",
            "execution_model": "relion_pipeline_select_then_joinstar",
            "materialization_mode": "select_joinstar",
            "requested_materialization_mode": prepared["requested_materialization_mode"],
            "materialization_fallback_reason": prepared["materialization_fallback_reason"],
            "project_dir": str(prepared["project"]),
            "pipeline_name": prepared["pipeline_name"],
            "particles_star_input": prepared["checked_input"],
            "selected_class_numbers": prepared["classes"],
            "selected_class_counts": plan["selected_class_counts"],
            "expected_particle_count": plan["expected_particle_count"],
            "select_jobs": select_jobs,
            "join_jobs": join_jobs,
            "jobs_to_start": added_jobs,
            "final_stage_id": final["stage_id"],
            "final_job": final["job"],
            "particles_star": final["output"],
            "suggested_schedule_name": plan["suggested_schedule_name"],
            "pipeline_registered": True,
            "started": False,
            "next_write_tool": "relion_start_jobs",
        }

    def run_particle_class_selection(
        self,
        project_dir: str,
        particles_star: str,
        selected_class_numbers: list[int],
        alias: str,
        pipeline_name: str,
        schedule_name: str | None,
        confirm: bool,
        materialization_mode: str = "auto",
        class_source_star: str | None = None,
    ) -> dict[str, Any]:
        """Register and start exact particle-class selection in one approved call."""
        self.settings.assert_execution_enabled(confirm)
        explicit_schedule = (
            None if schedule_name is None else _safe_name(schedule_name, "schedule_name")
        )
        added = self.add_particle_class_selection_jobs(
            project_dir,
            particles_star,
            selected_class_numbers,
            alias,
            pipeline_name,
            confirm=True,
            materialization_mode=materialization_mode,
            class_source_star=class_source_star,
        )
        jobs = [_safe_job(job) for job in added["jobs_to_start"]]
        schedule = (
            explicit_schedule
            if explicit_schedule is not None
            else _safe_name(str(added["suggested_schedule_name"]), "schedule_name")
        )
        try:
            started = self.start_jobs(
                project_dir,
                jobs,
                schedule,
                pipeline_name,
                confirm=True,
            )
        except Exception as exc:
            raise SegmentStartError(jobs, schedule, str(exc)) from exc
        result = dict(added)
        result.update(
            {
                "schedule_name": schedule,
                "started": True,
                "next_write_tool": None,
                "start_result": started,
            }
        )
        return result

    def _prepare_symmetry_alignment(
        self,
        project_dir: str,
        input_map: str,
        output_map: str,
        target_symmetry: str,
        search_max_resolution_A: float,
    ) -> tuple[
        dict[str, Any],
        Path,
        Path,
        Path,
        dict[str, str],
        str | None,
    ]:
        project = self.settings.validate_project_path(project_dir)
        checked_input = safe_project_relative(input_map)
        validated = self.catalog.validate_direct_params(
            "align_symmetry_reference",
            {
                "output_map": output_map,
                "target_symmetry": target_symmetry,
                "search_max_resolution_A": search_max_resolution_A,
            },
        )
        checked_output = str(validated["output_map"])
        target_symmetry = str(validated["target_symmetry"])
        search_max_resolution_A = float(validated["search_max_resolution_A"])
        if Path(checked_input).suffix.lower() != ".mrc":
            raise CatalogError("input_map must name an MRC file with a .mrc suffix.")
        if Path(checked_output).suffix.lower() != ".mrc":
            raise CatalogError("output_map must name an MRC file with a .mrc suffix.")
        output_relative = PurePosixPath(checked_output)
        if not output_relative.parts or output_relative.parts[0] != "ReferencePrep":
            raise CatalogError(
                "output_map must be beneath the dedicated ReferencePrep/ namespace."
            )

        try:
            input_path = self.settings.resolve_project_subpath(
                project,
                checked_input,
                must_exist=True,
            )
        except ConfigurationError as exc:
            raise CatalogError(f"Input map is unavailable: {project / checked_input}") from exc
        if not input_path.is_file():
            raise CatalogError(f"Input map is not a file: {input_path}")

        raw_output_path = project / checked_output
        if os.path.lexists(raw_output_path):
            raise CatalogError(
                "output_map already exists (including symbolic links); symmetry alignment never overwrites."
            )
        try:
            output_path = self.settings.resolve_project_subpath(project, checked_output)
        except ConfigurationError as exc:
            raise CatalogError(f"Output map is outside project_dir: {checked_output}") from exc
        if output_path == input_path:
            raise CatalogError("output_map must differ from input_map; maps are never overwritten in place.")
        raw_reference_namespace = project / "ReferencePrep"
        if raw_reference_namespace.is_symlink():
            raise CatalogError(
                "ReferencePrep must be a real project directory, not a symbolic link."
            )
        if raw_reference_namespace.exists() and not raw_reference_namespace.is_dir():
            raise CatalogError("ReferencePrep exists but is not a directory.")
        reference_namespace = raw_reference_namespace.resolve(strict=False)
        if reference_namespace not in output_path.parents:
            raise CatalogError(
                "output_map resolves outside the dedicated ReferencePrep/ namespace."
            )

        max_box_size_pix = self.settings.symmetry_alignment_max_box_size_px
        max_input_file_size_bytes = (
            1024
            + 4 * max_box_size_pix**3
            + SYMMETRY_ALIGNMENT_MAX_EXTENDED_HEADER_BYTES
        )
        try:
            input_file_size_bytes = input_path.stat().st_size
        except OSError as exc:
            raise CatalogError(f"Cannot inspect input_map size: {input_path}: {exc}") from exc
        if input_file_size_bytes > max_input_file_size_bytes:
            raise CatalogError(
                "input_map exceeds the configured symmetry-alignment file-size bound: "
                f"{input_file_size_bytes} bytes > {max_input_file_size_bytes} bytes."
            )

        input_sha256_before_validation = _file_sha256(input_path)
        input_mrc = _mrc_volume_info(
            input_path,
            role="input_map",
            max_box_size_pix=max_box_size_pix,
        )
        input_sha256 = _file_sha256(input_path)
        if input_sha256 != input_sha256_before_validation:
            raise CatalogError("input_map changed while its MRC contract was validated.")
        input_box_size = int(input_mrc["box_size_pix"])
        if input_box_size < SYMMETRY_ALIGNMENT_WORKING_BOX_SIZE_PIX:
            raise CatalogError(
                "input_map box is smaller than the fixed 64-pixel symmetry-alignment working grid."
            )
        input_angpix_A = float(input_mrc["voxel_size_xyz_A"][0])
        working_grid_nyquist_A = (
            2.0
            * input_angpix_A
            * input_box_size
            / SYMMETRY_ALIGNMENT_WORKING_BOX_SIZE_PIX
        )
        if search_max_resolution_A + 1e-9 < working_grid_nyquist_A:
            raise CatalogError(
                "search_max_resolution_A is finer than the fixed working grid permits: "
                f"use at least {working_grid_nyquist_A:.6g} A."
            )
        input_box_length_A = input_angpix_A * input_box_size
        search_fourier_radius_shells = math.ceil(
            input_box_length_A / search_max_resolution_A
        )
        if search_fourier_radius_shells < SYMMETRY_ALIGNMENT_MIN_SCORING_RADIUS_SHELLS:
            raise CatalogError(
                "search_max_resolution_A leaves fewer than the required "
                f"{SYMMETRY_ALIGNMENT_MIN_SCORING_RADIUS_SHELLS} Fourier-radius scoring "
                f"shells: use less than {input_box_length_A / 2.0:.6g} A."
            )
        minimum_free_staging_bytes = (
            2 * input_file_size_bytes + SYMMETRY_ALIGNMENT_FREE_DISK_RESERVE_BYTES
        )

        execution_environment = self._execution_environment()
        executable = self._resolve_execution_executable(
            SYMMETRY_ALIGNMENT_EXECUTABLE,
            execution_environment,
        )
        rendered_executable = executable or SYMMETRY_ALIGNMENT_EXECUTABLE
        command = [
            rendered_executable,
            "--i",
            "<private_input_snapshot.mrc>",
            "--sym",
            target_symmetry,
            "--o",
            "<private_staged_output.mrc>",
            "--box_size",
            str(SYMMETRY_ALIGNMENT_WORKING_BOX_SIZE_PIX),
            "--keep_centre",
            "--angpix",
            f"{input_angpix_A:.12g}",
            "--nr_uniform",
            str(SYMMETRY_ALIGNMENT_ORIENTATION_SAMPLES),
            "--maxres",
            f"{search_max_resolution_A:.12g}",
            "--local_search_range",
            str(SYMMETRY_ALIGNMENT_LOCAL_SEARCH_RANGE),
            "--local_search_step",
            f"{SYMMETRY_ALIGNMENT_LOCAL_SEARCH_STEP_DEG:.12g}",
            "--pad",
            str(SYMMETRY_ALIGNMENT_PADDING_FACTOR),
        ]
        limitations = [
            "Native relion_align_symmetry performs an independent stochastic search; it does not consume the relion_scan_symmetry axis or orientation matrix.",
            f"{target_symmetry} is aligned but not validated or averaged; matched C1/{target_symmetry} branch evidence remains required before symmetry is imposed.",
            "The materialized output remains full-bandwidth because --maxres limits only alignment scoring.",
            "Particle STAR orientations and origins are not transformed; this wrapper does not launch or prepare a local Refine3D continuation.",
        ]
        plan = {
            "schema_version": SYMMETRY_ALIGNMENT_SCHEMA_VERSION,
            "operation": "align_symmetry_reference",
            "status": "rendered",
            "target_symmetry": target_symmetry,
            "target_frame": "RELION standard point-group orientation",
            "symmetry_source": {
                "kind": "built_in_relion_point_group",
                "custom_definition_file_allowed": False,
                "resolution_working_directory": "private_staging_directory",
            },
            "input_map": checked_input,
            "output_map": checked_output,
            "input": {
                "sha256": input_sha256,
                "resolved_project_path": input_path.relative_to(project).as_posix(),
                "mrc": input_mrc,
            },
            "executable": {
                "name": SYMMETRY_ALIGNMENT_EXECUTABLE,
                "resolved": executable,
                "available": executable is not None,
            },
            "parameters": {
                "search_max_resolution_A": search_max_resolution_A,
                "search_working_grid_nyquist_A": working_grid_nyquist_A,
                "search_fourier_radius_shells": search_fourier_radius_shells,
                "input_box_length_A": input_box_length_A,
                "working_box_size_pix": SYMMETRY_ALIGNMENT_WORKING_BOX_SIZE_PIX,
                "uniform_orientation_samples": SYMMETRY_ALIGNMENT_ORIENTATION_SAMPLES,
                "local_search_range": SYMMETRY_ALIGNMENT_LOCAL_SEARCH_RANGE,
                "local_search_step_deg": SYMMETRY_ALIGNMENT_LOCAL_SEARCH_STEP_DEG,
                "padding_factor": SYMMETRY_ALIGNMENT_PADDING_FACTOR,
                "interpolation": "trilinear",
            },
            "resource_limits": {
                "configured_max_input_box_size_pix": max_box_size_pix,
                "configured_max_input_voxels": max_box_size_pix**3,
                "max_input_file_size_bytes": max_input_file_size_bytes,
                "input_file_size_bytes": input_file_size_bytes,
                "minimum_free_staging_bytes": minimum_free_staging_bytes,
                "free_space_preflight_required": True,
            },
            "command_template": command,
            "command_template_working_directory": "<private_staging_directory>",
            "publication": {
                "strategy": (
                    "private_temporary_directory_then_verified_dirfd_no_replace_hard_link"
                ),
                "existing_output_refused": True,
                "private_staging_directory": True,
                "private_input_snapshot": True,
                "destination_directory_fd_verified": True,
            },
            "alignment_only": True,
            "symmetry_averaging_applied": False,
            "output_lowpass_applied": False,
            "center_of_mass_recentering_applied": False,
            "rotation_center": "geometric_array_center",
            "particle_metadata_transformed": False,
            "relion_pipeline_job_registered": False,
            "relion_pipeline_job_started": False,
            "alignment_utility_executed": False,
            "stochastic_search": True,
            "direct_scan_transform_applied": False,
            "limitations": limitations,
        }
        return (
            plan,
            project,
            input_path,
            output_path,
            execution_environment,
            executable,
        )

    def render_symmetry_alignment(
        self,
        project_dir: str,
        input_map: str,
        output_map: str,
        target_symmetry: str,
        search_max_resolution_A: float,
    ) -> dict[str, Any]:
        """Render a bounded, alignment-only native point-group reference command."""
        plan, _, _, _, _, _ = self._prepare_symmetry_alignment(
            project_dir,
            input_map,
            output_map,
            target_symmetry,
            search_max_resolution_A,
        )
        return plan

    def align_symmetry_reference(
        self,
        project_dir: str,
        input_map: str,
        output_map: str,
        target_symmetry: str,
        search_max_resolution_A: float,
        confirm: bool,
    ) -> dict[str, Any]:
        """Materialize a new reference aligned to an explicit point group without averaging."""
        self.settings.assert_execution_enabled(confirm)
        (
            plan,
            project,
            input_path,
            output_path,
            execution_environment,
            executable,
        ) = self._prepare_symmetry_alignment(
            project_dir,
            input_map,
            output_map,
            target_symmetry,
            search_max_resolution_A,
        )
        if not executable:
            raise RelionRuntimeError(
                f"Cannot find executable: {SYMMETRY_ALIGNMENT_EXECUTABLE}"
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        current_output_path = self.settings.resolve_project_subpath(
            project,
            str(plan["output_map"]),
        )
        if current_output_path != output_path:
            raise CatalogError(
                "output_map parent changed during validation; no utility was executed."
            )
        raw_reference_namespace = project / "ReferencePrep"
        if raw_reference_namespace.is_symlink():
            raise CatalogError("ReferencePrep became a symbolic link during validation.")
        if os.path.lexists(output_path):
            raise CatalogError("output_map appeared before execution; no file was replaced.")
        try:
            available_free_staging_bytes = shutil.disk_usage(output_path.parent).free
        except OSError as exc:
            raise RelionRuntimeError(
                f"Could not inspect free space for symmetry-alignment staging: {exc}"
            ) from exc
        minimum_free_staging_bytes = int(
            plan["resource_limits"]["minimum_free_staging_bytes"]
        )
        if available_free_staging_bytes < minimum_free_staging_bytes:
            raise RelionRuntimeError(
                "Insufficient free space for private symmetry-alignment staging: "
                f"{available_free_staging_bytes} bytes available, "
                f"{minimum_free_staging_bytes} bytes required."
            )

        version_probe: str | None = None
        version_probe_returncode: int | None = None
        version_probe_diagnostic: str | None = None
        version_probe_command = [executable, "--version"]
        try:
            probed = subprocess.run(
                version_probe_command,
                cwd=project,
                capture_output=True,
                text=True,
                timeout=min(10, self.settings.utility_timeout_seconds),
                check=False,
                env=execution_environment,
            )
            version_probe_returncode = probed.returncode
            probe_text = (probed.stdout + "\n" + probed.stderr).strip()
            if probed.returncode == 0 and probe_text:
                version_probe = probe_text.splitlines()[0][:300]
            elif probe_text:
                version_probe_diagnostic = probe_text[-1000:]
        except (OSError, subprocess.TimeoutExpired) as exc:
            version_probe_diagnostic = str(exc)[:1000]

        staging_directory: Path | None = None
        staging_cleanup_complete = True
        staging_cleanup_warning: str | None = None
        command: list[str] | None = None
        completed: subprocess.CompletedProcess[str] | None = None
        published = False
        try:
            staging_directory = Path(
                tempfile.mkdtemp(prefix=".relion-align-", dir=output_path.parent)
            )
            staging_directory.chmod(0o700)
            input_snapshot_path = staging_directory / "input.mrc"
            staged_path = staging_directory / "aligned.mrc"
            shutil.copyfile(input_path, input_snapshot_path)
            input_snapshot_path.chmod(0o400)
            input_snapshot_sha256 = _file_sha256(input_snapshot_path)
            if input_snapshot_sha256 != plan["input"]["sha256"]:
                raise RelionRuntimeError(
                    "input_map changed while its private execution snapshot was created."
                )
            command = list(plan["command_template"])
            command[0] = executable
            command[command.index("<private_input_snapshot.mrc>")] = input_snapshot_path.name
            command[command.index("<private_staged_output.mrc>")] = staged_path.name
            try:
                completed = subprocess.run(
                    command,
                    cwd=staging_directory,
                    capture_output=True,
                    text=True,
                    timeout=self.settings.utility_timeout_seconds,
                    check=False,
                    env=execution_environment,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RelionRuntimeError(f"relion_align_symmetry could not complete: {exc}") from exc
            if completed.returncode != 0:
                raise RelionRuntimeError(
                    "relion_align_symmetry failed with exit code "
                    f"{completed.returncode}:\n{completed.stderr[-4000:]}"
                )
            if not staged_path.is_file() or staged_path.is_symlink():
                raise RelionRuntimeError(
                    "relion_align_symmetry returned success without creating a regular staged output map."
                )

            transform = _parse_refined_alignment(completed.stdout)
            output_mrc = _mrc_volume_info(
                staged_path,
                role="aligned output",
                max_box_size_pix=int(
                    plan["resource_limits"]["configured_max_input_box_size_pix"]
                ),
            )
            input_mrc = plan["input"]["mrc"]
            if output_mrc["shape_zyx"] != input_mrc["shape_zyx"]:
                raise RelionRuntimeError(
                    "relion_align_symmetry changed the full-resolution output grid shape."
                )
            if not np.allclose(
                output_mrc["voxel_size_xyz_A"],
                input_mrc["voxel_size_xyz_A"],
                rtol=1e-6,
                atol=1e-6,
            ):
                raise RelionRuntimeError(
                    "relion_align_symmetry changed the output voxel spacing."
                )
            try:
                current_input_path = self.settings.resolve_project_subpath(
                    project,
                    str(plan["input_map"]),
                    must_exist=True,
                )
            except ConfigurationError as exc:
                raise RelionRuntimeError(
                    "input_map path binding changed, became unavailable, or escaped "
                    "project_dir while relion_align_symmetry was running; output was not "
                    "published."
                ) from exc
            if current_input_path != input_path:
                raise RelionRuntimeError(
                    "input_map path binding changed while relion_align_symmetry was running; "
                    "output was not published."
                )
            try:
                input_sha256_after = _file_sha256(input_path)
                input_snapshot_sha256_after = _file_sha256(input_snapshot_path)
            except OSError as exc:
                raise RelionRuntimeError(
                    "input_map became unavailable while relion_align_symmetry was running."
                ) from exc
            if (
                input_sha256_after != plan["input"]["sha256"]
                or input_snapshot_sha256_after != plan["input"]["sha256"]
            ):
                raise RelionRuntimeError(
                    "input_map changed while relion_align_symmetry was running; output was not published."
                )
            output_sha256 = _file_sha256(staged_path)
            try:
                final_input_path = self.settings.resolve_project_subpath(
                    project,
                    str(plan["input_map"]),
                    must_exist=True,
                )
            except ConfigurationError as exc:
                raise RelionRuntimeError(
                    "input_map path binding changed, became unavailable, or escaped "
                    "project_dir before publication; output was not published."
                ) from exc
            if final_input_path != input_path:
                raise RelionRuntimeError(
                    "input_map path binding changed before publication; output was not "
                    "published."
                )
            current_output_path = self.settings.resolve_project_subpath(
                project,
                str(plan["output_map"]),
            )
            if current_output_path != output_path or raw_reference_namespace.is_symlink():
                raise CatalogError(
                    "output_map parent changed while alignment was running; output was not published."
                )
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                publication_directory_fd = os.open(output_path.parent, directory_flags)
            except OSError as exc:
                raise CatalogError(
                    "output_map parent could not be opened without following a symbolic link; "
                    "output was not published."
                ) from exc
            try:
                opened_publication_directory = Path(
                    f"/proc/self/fd/{publication_directory_fd}"
                ).resolve(strict=True)
                if opened_publication_directory != output_path.parent:
                    raise CatalogError(
                        "output_map parent changed while alignment was running; output was "
                        "not published."
                    )
                try:
                    os.link(
                        staged_path,
                        output_path.name,
                        dst_dir_fd=publication_directory_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError as exc:
                    raise CatalogError(
                        "output_map appeared while alignment was running; no file was replaced."
                    ) from exc
                except OSError as exc:
                    raise RelionRuntimeError(
                        f"Could not publish the aligned map without replacement: {exc}"
                    ) from exc
            finally:
                os.close(publication_directory_fd)
            published = True
            if not output_path.is_file() or output_path.is_symlink():
                raise RelionRuntimeError(
                    "The aligned map was not present as a regular file after publication."
                )
        finally:
            primary_failure = sys.exc_info()[1]
            if staging_directory is not None and os.path.lexists(staging_directory):
                try:
                    shutil.rmtree(staging_directory)
                except OSError as exc:
                    staging_cleanup_complete = False
                    if not published:
                        cleanup_detail = (
                            "Could not clean the private alignment staging directory: "
                            f"{exc}"
                        )
                        if primary_failure is not None:
                            primary_failure.add_note(cleanup_detail)
                        else:
                            raise RelionRuntimeError(cleanup_detail) from exc
                    staging_cleanup_warning = (
                        "The aligned output was published, but its private staging directory "
                        f"could not be removed: {exc}"
                    )

        if completed is None or command is None:  # pragma: no cover - defensive narrowing
            raise RelionRuntimeError("relion_align_symmetry did not return a process result.")
        result = dict(plan)
        result.update(
            {
                "status": "materialized",
                "command": command,
                "command_working_directory": str(staging_directory),
                "executable": {
                    **plan["executable"],
                    "resolved": executable,
                    "available": True,
                    "version_probe": {
                        "command": version_probe_command,
                        "returncode": version_probe_returncode,
                        "version": version_probe,
                        "diagnostic": version_probe_diagnostic,
                    },
                },
                "transform": transform,
                "output": {
                    "sha256": output_sha256,
                    "mrc": output_mrc,
                },
                "output_exists": True,
                "input_unchanged": True,
                "input_binding_unchanged": True,
                "physical_grid_preserved": True,
                "alignment_utility_executed": True,
                "publication": {
                    **plan["publication"],
                    "published_without_replacement": True,
                    "staging_cleanup_complete": staging_cleanup_complete,
                },
                "resource_limits": {
                    **plan["resource_limits"],
                    "available_free_staging_bytes_at_preflight": (
                        available_free_staging_bytes
                    ),
                },
                "warnings": (
                    [staging_cleanup_warning] if staging_cleanup_warning is not None else []
                ),
                "stdout": completed.stdout[-4000:],
                "stderr": completed.stderr[-4000:],
            }
        )
        return result

    def _prepare_remove_duplicate_particles(
        self,
        project_dir: str,
        particles: str,
        duplicate_distance_A: float,
        output_star: str,
        image_pixel_size_A: float | None,
    ) -> tuple[
        dict[str, Any],
        Path,
        Path,
        Path,
        Path,
        dict[str, str],
        str | None,
    ]:
        """Validate and render duplicate removal without writing project state."""
        project = self.settings.validate_project_path(project_dir)
        execution_environment = self._execution_environment()
        executable = self._resolve_execution_executable(
            self.settings.star_handler_executable,
            execution_environment,
        )
        checked_input = safe_project_relative(particles)
        try:
            input_path = self.settings.resolve_project_subpath(
                project,
                checked_input,
                must_exist=True,
            )
        except ConfigurationError as exc:
            raise CatalogError(
                f"Input particles STAR is unavailable: {project / checked_input}"
            ) from exc
        if not input_path.is_file() or input_path.is_symlink():
            raise CatalogError(
                f"Input particles STAR must be a regular, non-symbolic-link file: {input_path}"
            )

        direct_params: dict[str, Any] = {
            "duplicate_distance_A": duplicate_distance_A,
            "output_star": output_star,
        }
        if image_pixel_size_A is not None:
            direct_params["image_pixel_size_A"] = image_pixel_size_A
        validated = self.catalog.validate_direct_params(
            "remove_duplicate_particles", direct_params
        )
        duplicate_distance_A = float(validated["duplicate_distance_A"])
        checked_output = str(validated["output_star"])
        requested_image_pixel_size_A = validated.get("image_pixel_size_A")
        if requested_image_pixel_size_A is not None:
            requested_image_pixel_size_A = float(requested_image_pixel_size_A)
        output_posix = PurePosixPath(checked_output)
        checked_removed = str(
            output_posix.with_name(output_posix.stem + "_removed.star")
        )

        _assert_safe_relative_parent(project, checked_output, role="output_star")
        _assert_safe_relative_parent(
            project,
            checked_removed,
            role="removed-particles sidecar",
        )
        raw_output_path = project / checked_output
        raw_removed_path = project / checked_removed
        for raw_path, role in (
            (raw_output_path, "Output STAR"),
            (raw_removed_path, "Removed-particles sidecar"),
        ):
            if os.path.lexists(raw_path):
                raise CatalogError(
                    f"{role} already exists (including symbolic links); duplicate removal never overwrites."
                )
        try:
            output_path = self.settings.resolve_project_subpath(project, checked_output)
            removed_path = self.settings.resolve_project_subpath(project, checked_removed)
        except ConfigurationError as exc:
            raise CatalogError(
                "Output STAR or its removed-particles sidecar resolves outside project_dir."
            ) from exc
        if output_path == input_path:
            raise CatalogError(
                "output_star must differ from the input particles; STAR files are never rewritten in place."
            )
        if removed_path == input_path:
            raise CatalogError(
                "The implicit removed-particles sidecar would alias the input particles STAR."
            )
        if output_path == removed_path:  # pragma: no cover - suffix construction invariant
            raise CatalogError("Output STAR and removed-particles sidecar must be distinct.")

        try:
            input_sha256 = _file_sha256(input_path)
            input_summary = _particle_row_summary_payload(input_path)
            optics = parse_star_block(input_path, "optics", strict_loops=True)
        except (OSError, StarFormatError) as exc:
            raise CatalogError(f"Invalid input particles STAR: {exc}") from exc
        if "rlnCenteredCoordinateZAngst" in input_summary["columns"]:
            raise CatalogError(
                "Duplicate removal supports SPA particle coordinates only; "
                "rlnCenteredCoordinateZAngst marks a 3D/tomographic particle table."
            )
        if int(input_summary["particle_count"]) == 0:
            raise CatalogError(
                "Input particles STAR contains zero particle rows; duplicate removal requires "
                "at least one particle."
            )
        if not optics:
            raise CatalogError(f"Input particles STAR is missing a data_optics block: {input_path}")

        def _column_values(column: str) -> list[str] | None:
            raw = optics.get(column)
            if raw is None:
                return None
            return list(raw) if isinstance(raw, list) else [str(raw)]

        group_values = _column_values("rlnOpticsGroup")
        image_values = _column_values("rlnImagePixelSize")
        voltage_values = _column_values("rlnVoltage")
        spherical_aberration_values = _column_values("rlnSphericalAberration")
        if not group_values:
            raise CatalogError("data_optics is missing rlnOpticsGroup.")
        if not image_values:
            raise CatalogError("data_optics is missing rlnImagePixelSize.")
        if not voltage_values:
            raise CatalogError("data_optics is missing rlnVoltage.")
        if not spherical_aberration_values:
            raise CatalogError("data_optics is missing rlnSphericalAberration.")
        if len(set(group_values)) != len(group_values):
            raise CatalogError("data_optics contains duplicate rlnOpticsGroup identifiers.")
        for column in (
            "rlnImagePixelSize",
            "rlnMicrographPixelSize",
            "rlnMicrographOriginalPixelSize",
            "rlnVoltage",
            "rlnSphericalAberration",
        ):
            values = _column_values(column)
            if values is not None and len(values) != len(group_values):
                raise CatalogError(
                    f"data_optics column {column} does not contain one value per optics group."
                )

        def _optional_positive_pixel(
            column: str,
            values: list[str] | None,
            index: int,
        ) -> float | None:
            if values is None:
                return None
            raw_value = values[index]
            if raw_value in {"", ".", "?"}:
                return None
            try:
                value = float(raw_value)
            except (TypeError, ValueError) as exc:
                raise CatalogError(
                    f"Invalid {column} value {raw_value!r} for optics group {group_values[index]}."
                ) from exc
            if not math.isfinite(value) or value <= 0:
                raise CatalogError(
                    f"{column} must be positive and finite for optics group {group_values[index]}."
                )
            return value

        def _required_optics_number(
            column: str,
            values: list[str],
            index: int,
            *,
            allow_zero: bool,
        ) -> float:
            raw_value = values[index]
            try:
                value = float(raw_value)
            except (TypeError, ValueError) as exc:
                raise CatalogError(
                    f"Invalid {column} value {raw_value!r} for optics group {group_values[index]}."
                ) from exc
            if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
                qualifier = "non-negative" if allow_zero else "positive"
                raise CatalogError(
                    f"{column} must be {qualifier} and finite for optics group "
                    f"{group_values[index]}."
                )
            return value

        micrograph_values = _column_values("rlnMicrographPixelSize")
        original_values = _column_values("rlnMicrographOriginalPixelSize")
        optics_by_group: dict[str, dict[str, Any]] = {}
        for index, group in enumerate(group_values):
            optics_by_group[group] = {
                "particle_pixel_size_A": _optional_positive_pixel(
                    "rlnImagePixelSize", image_values, index
                ),
                "micrograph_pixel_size_A": _optional_positive_pixel(
                    "rlnMicrographPixelSize", micrograph_values, index
                ),
                "micrograph_original_pixel_size_A": _optional_positive_pixel(
                    "rlnMicrographOriginalPixelSize", original_values, index
                ),
                "voltage_kV": _required_optics_number(
                    "rlnVoltage", voltage_values, index, allow_zero=False
                ),
                "spherical_aberration_mm": _required_optics_number(
                    "rlnSphericalAberration",
                    spherical_aberration_values,
                    index,
                    allow_zero=True,
                ),
            }

        particle_group_counts = input_summary["optics_group_counts"]
        if particle_group_counts:
            used_groups = list(particle_group_counts)
            missing_groups = sorted(set(used_groups) - set(optics_by_group))
            if missing_groups:
                raise CatalogError(
                    "data_particles references optics groups absent from data_optics: "
                    + ", ".join(missing_groups)
                )
            group_binding = "data_particles.rlnOpticsGroup"
        else:
            used_groups = list(optics_by_group)
            group_binding = "all_optics_groups_conservative"

        missing_image_scales = [
            group
            for group in used_groups
            if optics_by_group[group]["particle_pixel_size_A"] is None
        ]
        if missing_image_scales:
            raise CatalogError(
                "Used optics groups are missing a positive rlnImagePixelSize: "
                + ", ".join(missing_image_scales)
            )

        authoritative_scales = {
            group: optics_by_group[group]["micrograph_pixel_size_A"]
            for group in used_groups
            if optics_by_group[group]["micrograph_pixel_size_A"] is not None
        }
        authoritative_values = [
            float(value) for value in authoritative_scales.values()
        ]
        if authoritative_values and any(
            not math.isclose(
                authoritative_values[0],
                value,
                rel_tol=1e-6,
                abs_tol=1e-6,
            )
            for value in authoritative_values[1:]
        ):
            details = ", ".join(
                f"{group}={value:.12g} A/pix"
                for group, value in authoritative_scales.items()
            )
            raise CatalogError(
                "Used optics groups have heterogeneous rlnMicrographPixelSize values, but "
                f"relion_star_handler accepts only one --image_angpix value: {details}. "
                "Split the particle STAR by pre-extraction micrograph pixel size."
            )

        coordinate_scales: dict[str, float | None] = {}
        coordinate_sources: dict[str, str] = {}
        ambiguous_groups: list[str] = []
        for group in used_groups:
            metadata = optics_by_group[group]
            micrograph_pixel = metadata["micrograph_pixel_size_A"]
            if micrograph_pixel is not None:
                coordinate_scales[group] = float(micrograph_pixel)
                coordinate_sources[group] = "rlnMicrographPixelSize"
            else:
                coordinate_scales[group] = None
                coordinate_sources[group] = "ambiguous_missing_rlnMicrographPixelSize"
                ambiguous_groups.append(group)

        if requested_image_pixel_size_A is not None:
            conflicting = {
                group: scale
                for group, scale in coordinate_scales.items()
                if scale is not None
                and not math.isclose(
                    requested_image_pixel_size_A,
                    scale,
                    rel_tol=1e-6,
                    abs_tol=1e-6,
                )
            }
            if conflicting:
                details = ", ".join(
                    f"{group}={scale:.12g} A/pix"
                    for group, scale in conflicting.items()
                )
                raise CatalogError(
                    f"image_pixel_size_A={requested_image_pixel_size_A:.12g} conflicts with "
                    f"the pre-extraction coordinate pixel size in used optics groups: {details}."
                )
            for group in ambiguous_groups:
                coordinate_scales[group] = requested_image_pixel_size_A
                coordinate_sources[group] = "explicit_image_pixel_size_A"
        elif ambiguous_groups:
            details = ", ".join(ambiguous_groups)
            raise CatalogError(
                "Cannot infer the pre-extraction coordinate pixel size for used optics "
                f"group(s) {details}: rlnMicrographPixelSize is absent. Equality or "
                "difference between rlnImagePixelSize and rlnMicrographOriginalPixelSize "
                "cannot distinguish motion-correction binning from extraction resampling. "
                "Pass the verified "
                "pre-extraction micrograph pixel size as image_pixel_size_A."
            )

        resolved_coordinate_scales = {
            group: float(scale)
            for group, scale in coordinate_scales.items()
            if scale is not None
        }
        coordinate_values = list(resolved_coordinate_scales.values())
        if coordinate_values and any(
            not math.isclose(
                coordinate_values[0],
                value,
                rel_tol=1e-6,
                abs_tol=1e-6,
            )
            for value in coordinate_values[1:]
        ):
            details = ", ".join(
                f"{group}={value:.12g} A/pix"
                for group, value in resolved_coordinate_scales.items()
            )
            raise CatalogError(
                "Used optics groups resolve to heterogeneous pre-extraction coordinate "
                f"pixel sizes: {details}. Split the input before duplicate removal."
            )

        particles_are_downsampled = any(
            not math.isclose(
                float(optics_by_group[group]["particle_pixel_size_A"]),
                resolved_coordinate_scales[group],
                rel_tol=1e-6,
                abs_tol=1e-6,
            )
            for group in used_groups
        )
        inferred_coordinate_scale = (
            next(iter(resolved_coordinate_scales.values()))
            if resolved_coordinate_scales
            else None
        )
        effective_image_pixel_size_A = (
            requested_image_pixel_size_A
            if requested_image_pixel_size_A is not None
            else inferred_coordinate_scale
        )
        if effective_image_pixel_size_A is None:  # pragma: no cover - validation invariant
            raise CatalogError("Could not resolve a coordinate pixel size for duplicate removal.")
        if requested_image_pixel_size_A is not None:
            image_pixel_size_source = "explicit_image_pixel_size_A"
        else:
            image_pixel_size_source = "rlnMicrographPixelSize"

        for group in used_groups:
            optics_by_group[group]["used_by_particles"] = True
            optics_by_group[group]["coordinate_pixel_size_A"] = resolved_coordinate_scales[group]
            optics_by_group[group]["coordinate_pixel_size_source"] = coordinate_sources[group]
        for group in set(optics_by_group) - set(used_groups):
            optics_by_group[group]["used_by_particles"] = False

        def _homogeneous_value(field: str) -> float | None:
            values = [
                float(optics_by_group[group][field])
                for group in used_groups
                if optics_by_group[group][field] is not None
            ]
            if len(values) != len(used_groups) or not values:
                return None
            if any(
                not math.isclose(values[0], value, rel_tol=1e-6, abs_tol=1e-6)
                for value in values[1:]
            ):
                return None
            return values[0]

        rendered_executable = executable or self.settings.star_handler_executable
        command_template = [
            rendered_executable,
            "--i",
            "<private_input_snapshot.star>",
            "--o",
            "<private_staged_output.star>",
            "--remove_duplicates",
            f"{duplicate_distance_A:.12g}",
        ]
        command_template.extend(
            ["--image_angpix", f"{effective_image_pixel_size_A:.12g}"]
        )

        limitations = [
            "This direct command does not register a pipeliner job, so the output STAR is not a JobResult; bind it explicitly as a path for the next step.",
            "Duplicate removal keeps one particle per cluster by native relion_star_handler rules; it does not choose between them on any quality criterion.",
            "Deduplicating after extraction does not undo the duplicated extraction itself; run it before any refinement that relies on half-set independence.",
        ]
        plan = {
            "operation": "remove_duplicate_particles",
            "status": "rendered",
            "input_particles": checked_input,
            "output_particles": checked_output,
            "removed_particles_star": checked_removed,
            "duplicate_distance_A": duplicate_distance_A,
            "image_pixel_size_A": effective_image_pixel_size_A,
            "requested_image_pixel_size_A": requested_image_pixel_size_A,
            "image_pixel_size_source": image_pixel_size_source,
            "particle_pixel_size_A": _homogeneous_value("particle_pixel_size_A"),
            "micrograph_pixel_size_A": _homogeneous_value("micrograph_pixel_size_A"),
            "micrograph_original_pixel_size_A": _homogeneous_value(
                "micrograph_original_pixel_size_A"
            ),
            "particles_are_downsampled": particles_are_downsampled,
            "optics_group_binding": group_binding,
            "optics_groups": optics_by_group,
            "input": {
                "sha256": input_sha256,
                "resolved_project_path": input_path.relative_to(project).as_posix(),
            },
            "input_summary": input_summary,
            "executable": {
                "name": self.settings.star_handler_executable,
                "resolved": executable,
                "available": executable is not None,
            },
            "command_template": command_template,
            "command_template_working_directory": "<private_staging_directory>",
            "publication": {
                "strategy": "private_staging_then_verified_dirfd_no_replace_hard_links",
                "output_particles": checked_output,
                "removed_particles_star": checked_removed,
                "existing_outputs_refused": True,
                "private_staging_directory": True,
                "private_input_snapshot": True,
                "destination_directory_fd_verified": True,
                "two_file_rollback_on_partial_publication": True,
            },
            "input_unchanged": True,
            "relion_pipeline_job_registered": False,
            "output_exists": False,
            "removed_output_exists": False,
            "limitations": limitations,
        }
        return (
            plan,
            project,
            input_path,
            output_path,
            removed_path,
            execution_environment,
            executable,
        )

    def render_remove_duplicate_particles(
        self,
        project_dir: str,
        particles: str,
        duplicate_distance_A: float,
        output_star: str,
        image_pixel_size_A: float | None = None,
    ) -> dict[str, Any]:
        """Render and validate native duplicate removal without writing files."""
        plan, _, _, _, _, _, _ = self._prepare_remove_duplicate_particles(
            project_dir,
            particles,
            duplicate_distance_A,
            output_star,
            image_pixel_size_A,
        )
        return plan

    def remove_duplicate_particles(
        self,
        project_dir: str,
        particles: str,
        duplicate_distance_A: float,
        output_star: str,
        image_pixel_size_A: float | None,
        confirm: bool,
    ) -> dict[str, Any]:
        self.settings.assert_execution_enabled(confirm)
        (
            plan,
            project,
            input_path,
            output_path,
            removed_path,
            execution_environment,
            executable,
        ) = self._prepare_remove_duplicate_particles(
            project_dir,
            particles,
            duplicate_distance_A,
            output_star,
            image_pixel_size_A,
        )
        if not executable:
            raise RelionRuntimeError(
                f"Cannot find executable: {self.settings.star_handler_executable}"
            )

        _assert_safe_relative_parent(
            project,
            str(plan["output_particles"]),
            role="output_star",
        )
        publication_directory_fd = _open_or_create_project_directory(
            project,
            PurePosixPath(str(plan["output_particles"])).parent,
            role="duplicate-removal output",
        )
        try:
            _assert_safe_relative_parent(
                project,
                str(plan["output_particles"]),
                role="output_star",
            )
            current_output_path = self.settings.resolve_project_subpath(
                project, str(plan["output_particles"])
            )
            current_removed_path = self.settings.resolve_project_subpath(
                project, str(plan["removed_particles_star"])
            )
            if current_output_path != output_path or current_removed_path != removed_path:
                raise CatalogError(
                    "Duplicate-removal output parent changed during validation; no utility "
                    "was executed."
                )
            if os.path.lexists(output_path) or os.path.lexists(removed_path):
                raise CatalogError(
                    "A duplicate-removal output appeared before execution; no file was replaced."
                )
            opened_publication_directory = Path(
                f"/proc/self/fd/{publication_directory_fd}"
            ).resolve(strict=True)
            if opened_publication_directory != output_path.parent:
                raise CatalogError(
                    "Duplicate-removal output parent changed during validation; no utility "
                    "was executed."
                )
        except BaseException:
            os.close(publication_directory_fd)
            raise

        staging_directory: Path | None = None
        staging_display_directory: Path | None = None
        staging_cleanup_complete = True
        staging_cleanup_warning: str | None = None
        command: list[str] | None = None
        completed: subprocess.CompletedProcess[str] | None = None
        published = False
        output_summary: dict[str, Any] | None = None
        removed_summary: dict[str, Any] | None = None
        try:
            staging_name: str | None = None
            for _ in range(100):
                candidate = f".relion-deduplicate-{secrets.token_hex(8)}"
                try:
                    os.mkdir(
                        candidate,
                        mode=0o700,
                        dir_fd=publication_directory_fd,
                    )
                except FileExistsError:
                    continue
                staging_name = candidate
                break
            if staging_name is None:  # pragma: no cover - cryptographic collision bound
                raise RelionRuntimeError(
                    "Could not allocate a private duplicate-removal staging directory."
                )
            staging_directory = (
                Path(f"/proc/self/fd/{publication_directory_fd}") / staging_name
            )
            staging_display_directory = output_path.parent / staging_name
            input_snapshot_path = staging_directory / "input.star"
            staged_output_path = staging_directory / "deduplicated.star"
            staged_removed_path = staging_directory / "deduplicated_removed.star"
            shutil.copyfile(input_path, input_snapshot_path)
            input_snapshot_path.chmod(0o400)
            if _file_sha256(input_snapshot_path) != plan["input"]["sha256"]:
                raise RelionRuntimeError(
                    "Input particles STAR changed while its private execution snapshot was created."
                )

            command = list(plan["command_template"])
            command[0] = executable
            command[command.index("<private_input_snapshot.star>")] = input_snapshot_path.name
            command[command.index("<private_staged_output.star>")] = staged_output_path.name
            try:
                completed = subprocess.run(
                    command,
                    cwd=staging_directory,
                    capture_output=True,
                    text=True,
                    timeout=self.settings.utility_timeout_seconds,
                    check=False,
                    env=execution_environment,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RelionRuntimeError(
                    f"relion_star_handler could not complete: {exc}"
                ) from exc
            if completed.returncode != 0:
                raise RelionRuntimeError(
                    "relion_star_handler failed with exit code "
                    f"{completed.returncode}:\n{completed.stderr[-4000:]}"
                )
            for staged_path, role in (
                (staged_output_path, "requested output STAR"),
                (staged_removed_path, "removed-particles sidecar"),
            ):
                if not staged_path.is_file() or staged_path.is_symlink():
                    raise RelionRuntimeError(
                        "relion_star_handler returned success without creating a regular "
                        f"staged {role}."
                    )
            try:
                output_summary = _particle_row_summary_payload(staged_output_path)
                removed_summary = _particle_row_summary_payload(
                    staged_removed_path,
                    require_optics=False,
                    allow_unnamed_particle_block=True,
                )
            except (OSError, StarFormatError) as exc:
                raise RelionRuntimeError(
                    f"relion_star_handler created an invalid particle STAR: {exc}"
                ) from exc
            if (
                output_summary["optics_metadata_digest"]
                != plan["input_summary"]["optics_metadata_digest"]
            ):
                raise RelionRuntimeError(
                    "Duplicate removal changed retained data_optics metadata; outputs "
                    "were not published."
                )
            if removed_summary["optics_block_present"]:
                raise RelionRuntimeError(
                    "Duplicate removal unexpectedly wrote data_optics metadata into the "
                    "removed-particles sidecar; outputs were not published."
                )
            input_count = int(plan["input_summary"]["particle_count"])
            output_count = int(output_summary["particle_count"])
            removed_count = int(removed_summary["particle_count"])
            if output_count < 1:
                raise RelionRuntimeError(
                    "Duplicate removal retained zero particles from a nonempty input; "
                    "outputs were not published."
                )
            if output_count + removed_count != input_count:
                raise RelionRuntimeError(
                    "Duplicate-removal outputs violate particle-count conservation: "
                    f"input={input_count}, kept={output_count}, removed={removed_count}."
                )
            modulus = 1 << 256
            input_identity = int(
                str(plan["input_summary"]["image_name_multiset_digest"]), 16
            )
            output_identity = int(str(output_summary["image_name_multiset_digest"]), 16)
            removed_identity = int(
                str(removed_summary["image_name_multiset_digest"]), 16
            )
            if (output_identity + removed_identity) % modulus != input_identity:
                raise RelionRuntimeError(
                    "Duplicate-removal outputs do not partition the input rlnImageName "
                    "multiset; outputs were not published."
                )
            input_metadata_identity = int(
                str(plan["input_summary"]["row_metadata_multiset_digest"]), 16
            )
            output_metadata_identity = int(
                str(output_summary["row_metadata_multiset_digest"]), 16
            )
            removed_metadata_identity = int(
                str(removed_summary["row_metadata_multiset_digest"]), 16
            )
            if (
                output_metadata_identity + removed_metadata_identity
            ) % modulus != input_metadata_identity:
                raise RelionRuntimeError(
                    "Duplicate-removal outputs do not partition the complete input particle-row "
                    "metadata multiset; outputs were not published."
                )

            try:
                current_input_path = self.settings.resolve_project_subpath(
                    project,
                    str(plan["input_particles"]),
                    must_exist=True,
                )
            except ConfigurationError as exc:
                raise RelionRuntimeError(
                    "Input particles path binding changed or became unavailable while "
                    "relion_star_handler was running; outputs were not published."
                ) from exc
            if current_input_path != input_path:
                raise RelionRuntimeError(
                    "Input particles path binding changed while relion_star_handler was running; "
                    "outputs were not published."
                )
            if (
                _file_sha256(input_path) != plan["input"]["sha256"]
                or _file_sha256(input_snapshot_path) != plan["input"]["sha256"]
            ):
                raise RelionRuntimeError(
                    "Input particles STAR changed while relion_star_handler was running; "
                    "outputs were not published."
                )

            _assert_safe_relative_parent(
                project,
                str(plan["output_particles"]),
                role="output_star",
            )
            current_output_path = self.settings.resolve_project_subpath(
                project, str(plan["output_particles"])
            )
            current_removed_path = self.settings.resolve_project_subpath(
                project, str(plan["removed_particles_star"])
            )
            if current_output_path != output_path or current_removed_path != removed_path:
                raise CatalogError(
                    "Duplicate-removal output parent changed while the utility was running; "
                    "outputs were not published."
                )

            linked_names: list[tuple[str, Path]] = []
            try:
                opened_publication_directory = Path(
                    f"/proc/self/fd/{publication_directory_fd}"
                ).resolve(strict=True)
                if opened_publication_directory != output_path.parent:
                    raise CatalogError(
                        "Duplicate-removal output parent changed while the utility was running; "
                        "outputs were not published."
                    )
                try:
                    for staged_path, destination_path in (
                        (staged_output_path, output_path),
                        (staged_removed_path, removed_path),
                    ):
                        os.link(
                            staged_path,
                            destination_path.name,
                            dst_dir_fd=publication_directory_fd,
                            follow_symlinks=False,
                        )
                        linked_names.append((destination_path.name, staged_path))
                except FileExistsError as exc:
                    raise CatalogError(
                        "A duplicate-removal output appeared during publication; no existing "
                        "file was replaced."
                    ) from exc
                except OSError as exc:
                    raise RelionRuntimeError(
                        f"Could not publish duplicate-removal outputs without replacement: {exc}"
                    ) from exc
            except BaseException as publication_error:
                rollback_failures: list[str] = []
                for destination_name, staged_path in reversed(linked_names):
                    try:
                        destination_stat = os.stat(
                            destination_name,
                            dir_fd=publication_directory_fd,
                            follow_symlinks=False,
                        )
                        staged_stat = staged_path.stat(follow_symlinks=False)
                        if (
                            destination_stat.st_dev == staged_stat.st_dev
                            and destination_stat.st_ino == staged_stat.st_ino
                        ):
                            os.unlink(destination_name, dir_fd=publication_directory_fd)
                        else:
                            rollback_failures.append(
                                f"{destination_name} no longer aliases its staged inode"
                            )
                    except OSError as exc:
                        rollback_failures.append(f"{destination_name}: {exc}")
                if rollback_failures:
                    publication_error.add_note(
                        "Partial-publication rollback was incomplete: "
                        + "; ".join(rollback_failures)
                    )
                raise
            published = True
        finally:
            primary_failure = sys.exc_info()[1]
            try:
                if staging_directory is not None and os.path.lexists(staging_directory):
                    try:
                        shutil.rmtree(staging_directory)
                    except OSError as exc:
                        staging_cleanup_complete = False
                        if not published:
                            cleanup_detail = (
                                "Could not clean the private duplicate-removal staging directory: "
                                f"{exc}"
                            )
                            if primary_failure is not None:
                                primary_failure.add_note(cleanup_detail)
                            else:
                                raise RelionRuntimeError(cleanup_detail) from exc
                        staging_cleanup_warning = (
                            "Duplicate-removal outputs were published, but their private staging "
                            f"directory could not be removed: {exc}"
                        )
            finally:
                os.close(publication_directory_fd)

        if completed is None or command is None or output_summary is None or removed_summary is None:
            raise RelionRuntimeError(
                "relion_star_handler did not return validated duplicate-removal outputs."
            )
        if not output_path.is_file() or output_path.is_symlink():
            raise RelionRuntimeError(
                "The deduplicated particle STAR was not a regular file after publication."
            )
        if not removed_path.is_file() or removed_path.is_symlink():
            raise RelionRuntimeError(
                "The removed-particles sidecar was not a regular file after publication."
            )

        result = dict(plan)
        result.update(
            {
                "status": "materialized",
                "command": command,
                "command_working_directory": str(staging_display_directory),
                "executable": {
                    **plan["executable"],
                    "resolved": executable,
                    "available": True,
                },
                "output_summary": output_summary,
                "removed_summary": removed_summary,
                "removed_particle_count": removed_summary["particle_count"],
                "output_exists": True,
                "removed_output_exists": True,
                "publication": {
                    **plan["publication"],
                    "published_without_replacement": True,
                    "staging_cleanup_complete": staging_cleanup_complete,
                },
                "warnings": (
                    [staging_cleanup_warning]
                    if staging_cleanup_warning is not None
                    else []
                ),
                "stdout": completed.stdout[-4000:],
                "stderr": completed.stderr[-4000:],
            }
        )
        return result

    def rescale_map(
        self,
        project_dir: str,
        input_map: str,
        input_pixel_size_A: float,
        output_pixel_size_A: float,
        new_box_size_pix: int,
        output_map: str | None,
        output_suffix: str,
        overwrite: bool,
        confirm: bool,
    ) -> dict[str, Any]:
        self.settings.assert_execution_enabled(confirm)
        project = self.settings.validate_project_path(project_dir)
        execution_environment = self._execution_environment()
        executable = self._resolve_execution_executable(
            self.settings.image_handler_executable,
            execution_environment,
        )
        if not executable:
            raise RelionRuntimeError(f"Cannot find executable: {self.settings.image_handler_executable}")
        checked_input = safe_project_relative(input_map)
        try:
            input_path = self.settings.resolve_project_subpath(
                project,
                checked_input,
                must_exist=True,
            )
        except ConfigurationError as exc:
            raise CatalogError(f"Input map is unavailable: {project / checked_input}") from exc
        if not input_path.is_file():
            raise CatalogError(f"Input map is not a file: {input_path}")
        direct_params: dict[str, Any] = {
            "input_pixel_size_A": input_pixel_size_A,
            "output_pixel_size_A": output_pixel_size_A,
            "new_box_size_pix": new_box_size_pix,
            "output_suffix": output_suffix,
        }
        if output_map is not None:
            direct_params["output_map"] = output_map
        validated = self.catalog.validate_direct_params("rescale_map", direct_params)
        input_pixel_size_A = validated["input_pixel_size_A"]
        output_pixel_size_A = validated["output_pixel_size_A"]
        new_box_size_pix = validated["new_box_size_pix"]
        output_suffix = validated["output_suffix"]
        output_map = validated.get("output_map")
        if output_map is None:
            output_path_obj = Path(checked_input)
            checked_output = str(
                output_path_obj.with_name(
                    output_path_obj.stem + output_suffix + output_path_obj.suffix
                )
            )
        else:
            checked_output = output_map
        try:
            output_path = self.settings.resolve_project_subpath(project, checked_output)
        except ConfigurationError as exc:
            raise CatalogError(f"Output map is outside project_dir: {checked_output}") from exc
        if output_path == input_path:
            raise CatalogError("output_map must differ from input_map; maps are never overwritten in place.")
        if output_path.exists() and not overwrite:
            raise CatalogError("Output map already exists; set overwrite=true to replace it.")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            executable,
            "--i",
            checked_input,
            "--angpix",
            str(input_pixel_size_A),
            "--rescale_angpix",
            str(output_pixel_size_A),
            "--new_box",
            str(new_box_size_pix),
            "--o",
            checked_output,
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=project,
                capture_output=True,
                text=True,
                timeout=self.settings.utility_timeout_seconds,
                check=False,
                env=execution_environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RelionRuntimeError(f"relion_image_handler could not complete: {exc}") from exc
        if completed.returncode != 0:
            raise RelionRuntimeError(
                f"relion_image_handler failed with exit code {completed.returncode}:\n{completed.stderr[-4000:]}"
            )
        if not output_path.is_file():
            raise RelionRuntimeError(
                "relion_image_handler returned success without creating the requested output map."
            )
        return {
            "input_map": checked_input,
            "output_map": checked_output,
            "output_exists": True,
            "command": command,
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-4000:],
        }
