from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class ConfigurationError(ValueError):
    """Raised when the server configuration is unsafe or incomplete."""


ENVIRONMENT_DEFAULTS: dict[str, str] = {
    "RELION_MCP_MODE": "plan",
    "RELION_PROJECT_ROOTS": "",
    "RELION_CRYOSPARC_IMPORT_ROOTS": "",
    "RELION_PYEM_PYTHON": "",
    "RELION_CSPARC2STAR": "",
    "RELION_SCRATCH_DIR": "",
    "RELION_PIPELINE_NAME": "default",
    "RELION_PIPELINER": "relion_pipeliner",
    "RELION_IMAGE_HANDLER": "relion_image_handler",
    "RELION_STAR_HANDLER": "relion_star_handler",
    "RELION_CLASS_SELECTOR": "relion-mcp-class-select",
    "RELION_TOPAZ_EXECUTABLE": "topaz",
    "RELION_ADD_JOB_TIMEOUT": "90",
    "RELION_UTILITY_TIMEOUT": "900",
    "RELION_SYMMETRY_ALIGNMENT_MAX_BOX_PX": "512",
    "RELION_VISUAL_MAX_SIDE_PX": "2800",
    "RELION_VISUAL_MAX_PNG_BYTES": "6000000",
    "RELION_VISUAL_MAX_PANELS": "96",
}


def _split_roots(raw: str | None, *, variable: str = "RELION_PROJECT_ROOTS") -> tuple[Path, ...]:
    if not raw:
        return ()
    roots: list[Path] = []
    for item in raw.split(os.pathsep):
        item = item.strip()
        if not item:
            continue
        root = Path(item).expanduser()
        if not root.is_absolute():
            raise ConfigurationError(
                f"{variable} entries must be absolute paths; got {item!r}."
            )
        roots.append(root.resolve())
    return tuple(roots)


def _absolute_path_from_env(name: str) -> str:
    raw = os.getenv(name, ENVIRONMENT_DEFAULTS[name]).strip()
    if raw == "":
        return ""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ConfigurationError(f"{name} must be an absolute path; got {raw!r}.")
    return str(path)


@dataclass(frozen=True)
class CryosparcProjectIdentity:
    uid: str
    project_dir: Path
    device: int
    inode: int


def _cryosparc_live_projects_from_env() -> (
    tuple[CryosparcProjectIdentity, ...] | None
):
    variable = "EMCLAW_CRYOSPARC_PROJECT_INVENTORY_JSON"
    if variable not in os.environ:
        return None
    raw = os.environ[variable]
    if len(raw) > 64 * 1024:
        raise ConfigurationError(
            f"{variable} exceeds its transport bound."
        )
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"{variable} must be a JSON array."
        ) from exc
    if not isinstance(values, list) or len(values) > 512:
        raise ConfigurationError(
            f"{variable} must be a bounded array."
        )
    projects: list[CryosparcProjectIdentity] = []
    for value in values:
        if not isinstance(value, dict) or set(value) != {
            "uid",
            "project_dir",
            "device",
            "inode",
        }:
            raise ConfigurationError(
                f"{variable} entries must contain only uid, project_dir, device, and inode."
            )
        uid = value["uid"]
        project_dir = value["project_dir"]
        device = value["device"]
        inode = value["inode"]
        if not isinstance(uid, str) or re.fullmatch(r"P[1-9][0-9]*", uid) is None:
            raise ConfigurationError(
                "Live CryoSPARC projects must have valid project UIDs."
            )
        if not isinstance(project_dir, str):
            raise ConfigurationError(
                "Live CryoSPARC project directories must be strings."
            )
        if (
            isinstance(device, bool)
            or not isinstance(device, int)
            or device < 0
            or isinstance(inode, bool)
            or not isinstance(inode, int)
            or inode <= 0
        ):
            raise ConfigurationError(
                "Live CryoSPARC project directory identities must be non-negative integers."
            )
        path = Path(project_dir).expanduser()
        if not path.is_absolute():
            raise ConfigurationError(
                "Live CryoSPARC project directories must be absolute paths."
            )
        try:
            resolved = path.resolve(strict=True)
        except (FileNotFoundError, OSError, RuntimeError) as exc:
            raise ConfigurationError(
                "A live CryoSPARC project directory is unavailable."
            ) from exc
        if path != resolved or not resolved.is_dir():
            raise ConfigurationError(
                "Live CryoSPARC project directories must be canonical real directories."
            )
        try:
            directory_stat = resolved.stat()
        except OSError as exc:
            raise ConfigurationError(
                "A live CryoSPARC project directory identity is unavailable."
            ) from exc
        if directory_stat.st_dev != device or directory_stat.st_ino != inode:
            raise ConfigurationError(
                "A live CryoSPARC project directory changed during startup."
            )
        projects.append(
            CryosparcProjectIdentity(
                uid=uid,
                project_dir=resolved,
                device=device,
                inode=inode,
            )
        )
    if len({project.uid for project in projects}) != len(projects):
        raise ConfigurationError("Live CryoSPARC project UIDs must not be repeated.")
    if len({project.project_dir for project in projects}) != len(projects):
        raise ConfigurationError(
            "Live CryoSPARC project directories must not be repeated."
        )
    return tuple(sorted(projects, key=lambda project: project.uid))


def _positive_int_from_env(name: str) -> int:
    raw = os.getenv(name, ENVIRONMENT_DEFAULTS[name])
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a positive integer; got {raw!r}.") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be a positive integer; got {raw!r}.")
    return value


def _scratch_directory_from_env() -> str | None:
    """Return a safe execution-host path without consulting the local filesystem."""
    raw = os.getenv("RELION_SCRATCH_DIR", ENVIRONMENT_DEFAULTS["RELION_SCRATCH_DIR"])
    if raw == "":
        return None
    if not raw.strip():
        raise ConfigurationError("RELION_SCRATCH_DIR must not contain only whitespace.")
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise ConfigurationError("RELION_SCRATCH_DIR may not contain control characters.")
    if ";" in raw or " == " in raw:
        raise ConfigurationError(
            "RELION_SCRATCH_DIR may not contain RELION option delimiters."
        )
    path = PurePosixPath(raw)
    if not path.is_absolute():
        raise ConfigurationError("RELION_SCRATCH_DIR must be an absolute POSIX path.")
    if len(path.parts) <= 1:
        raise ConfigurationError("RELION_SCRATCH_DIR must not be the filesystem root.")
    if any(part in {".", ".."} for part in raw.split("/")):
        raise ConfigurationError("RELION_SCRATCH_DIR may not contain '.' or '..' path segments.")
    if path.name == "relion_volatile":
        raise ConfigurationError(
            "RELION_SCRATCH_DIR must name the parent base, not relion_volatile itself."
        )
    if not re.fullmatch(r"/[A-Za-z0-9_./+:-]+", raw):
        raise ConfigurationError(
            "RELION_SCRATCH_DIR must be an absolute POSIX path containing only "
            "letters, digits, '_', '.', '/', '+', ':', and '-'."
        )
    return raw


@dataclass(frozen=True)
class Settings:
    mode: str
    project_roots: tuple[Path, ...]
    pipeliner_executable: str
    image_handler_executable: str
    pipeline_name: str
    add_job_timeout_seconds: int
    utility_timeout_seconds: int
    star_handler_executable: str = "relion_star_handler"
    visual_max_side_px: int = 2800
    visual_max_png_bytes: int = 6_000_000
    visual_max_panels: int = 96
    class_selector_executable: str = "relion-mcp-class-select"
    symmetry_alignment_max_box_size_px: int = 512
    topaz_executable: str = "topaz"
    scratch_directory: str | None = None
    cryosparc_import_roots: tuple[Path, ...] = ()
    cryosparc_live_projects: tuple[CryosparcProjectIdentity, ...] | None = None
    pyem_python: str = ""
    csparc2star_script: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        mode = os.getenv("RELION_MCP_MODE", ENVIRONMENT_DEFAULTS["RELION_MCP_MODE"]).strip().lower()
        if mode not in {"plan", "run"}:
            raise ConfigurationError("RELION_MCP_MODE must be 'plan' or 'run'.")
        return cls(
            mode=mode,
            project_roots=_split_roots(
                os.getenv(
                    "RELION_PROJECT_ROOTS",
                    ENVIRONMENT_DEFAULTS["RELION_PROJECT_ROOTS"],
                )
            ),
            cryosparc_import_roots=_split_roots(
                os.getenv(
                    "RELION_CRYOSPARC_IMPORT_ROOTS",
                    ENVIRONMENT_DEFAULTS["RELION_CRYOSPARC_IMPORT_ROOTS"],
                ),
                variable="RELION_CRYOSPARC_IMPORT_ROOTS",
            ),
            cryosparc_live_projects=_cryosparc_live_projects_from_env(),
            pyem_python=_absolute_path_from_env("RELION_PYEM_PYTHON"),
            csparc2star_script=_absolute_path_from_env("RELION_CSPARC2STAR"),
            scratch_directory=_scratch_directory_from_env(),
            pipeliner_executable=os.getenv(
                "RELION_PIPELINER", ENVIRONMENT_DEFAULTS["RELION_PIPELINER"]
            ),
            image_handler_executable=os.getenv(
                "RELION_IMAGE_HANDLER", ENVIRONMENT_DEFAULTS["RELION_IMAGE_HANDLER"]
            ),
            pipeline_name=os.getenv(
                "RELION_PIPELINE_NAME", ENVIRONMENT_DEFAULTS["RELION_PIPELINE_NAME"]
            ),
            add_job_timeout_seconds=_positive_int_from_env("RELION_ADD_JOB_TIMEOUT"),
            utility_timeout_seconds=_positive_int_from_env("RELION_UTILITY_TIMEOUT"),
            symmetry_alignment_max_box_size_px=_positive_int_from_env(
                "RELION_SYMMETRY_ALIGNMENT_MAX_BOX_PX"
            ),
            star_handler_executable=os.getenv(
                "RELION_STAR_HANDLER", ENVIRONMENT_DEFAULTS["RELION_STAR_HANDLER"]
            ),
            visual_max_side_px=_positive_int_from_env("RELION_VISUAL_MAX_SIDE_PX"),
            visual_max_png_bytes=_positive_int_from_env("RELION_VISUAL_MAX_PNG_BYTES"),
            visual_max_panels=_positive_int_from_env("RELION_VISUAL_MAX_PANELS"),
            class_selector_executable=os.getenv(
                "RELION_CLASS_SELECTOR", ENVIRONMENT_DEFAULTS["RELION_CLASS_SELECTOR"]
            ),
            topaz_executable=os.getenv(
                "RELION_TOPAZ_EXECUTABLE", ENVIRONMENT_DEFAULTS["RELION_TOPAZ_EXECUTABLE"]
            ),
        )

    def resolve_executable(self, configured: str) -> str | None:
        candidate = Path(configured).expanduser()
        if candidate.is_absolute():
            return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None
        return shutil.which(configured)

    def assert_execution_enabled(self, confirm: bool) -> None:
        if self.mode != "run":
            raise ConfigurationError(
                "Execution is disabled. Set RELION_MCP_MODE=run after reviewing the rendered command."
            )
        if not self.project_roots:
            raise ConfigurationError(
                "Execution requires RELION_PROJECT_ROOTS to contain one or more approved project roots."
            )
        if confirm is not True:
            raise ConfigurationError("This write operation requires confirm=true.")

    def validate_project_path(self, project_dir: str, *, may_create: bool = False) -> Path:
        raw = Path(project_dir).expanduser()
        if not raw.is_absolute():
            raise ConfigurationError("project_dir must be an absolute path.")

        if not self.project_roots:
            raise ConfigurationError("RELION_PROJECT_ROOTS is required for project access.")

        if may_create and not raw.exists():
            parent = raw.parent.resolve()
            resolved = parent / raw.name
        else:
            resolved = raw.resolve()

        allowed = any(resolved == root or root in resolved.parents for root in self.project_roots)
        if not allowed:
            roots = ", ".join(str(root) for root in self.project_roots)
            raise ConfigurationError(f"Project path is outside RELION_PROJECT_ROOTS: {roots}")

        if not may_create and not resolved.is_dir():
            raise ConfigurationError(f"RELION project directory does not exist: {resolved}")
        return resolved

    def validate_cryosparc_project_path(
        self,
        project_dir: str,
    ) -> Path:
        raw = Path(project_dir).expanduser()
        if not raw.is_absolute():
            raise ConfigurationError("source_project_dir must be an absolute path.")
        try:
            resolved = raw.resolve(strict=True)
        except (FileNotFoundError, OSError, RuntimeError) as exc:
            raise ConfigurationError(
                f"CryoSPARC source project directory is unavailable: {raw}"
            ) from exc
        if not resolved.is_dir():
            raise ConfigurationError(
                f"CryoSPARC source project is not a directory: {resolved}"
            )

        if self.cryosparc_live_projects is not None:
            live_project = next(
                (
                    project
                    for project in self.cryosparc_live_projects
                    if project.project_dir == resolved
                ),
                None,
            )
            try:
                directory_stat = resolved.stat()
            except OSError as exc:
                raise ConfigurationError(
                    "CryoSPARC source project identity could not be verified."
                ) from exc
            if (
                raw != resolved
                or live_project is None
                or directory_stat.st_dev != live_project.device
                or directory_stat.st_ino != live_project.inode
            ):
                raise ConfigurationError(
                    "CryoSPARC source project is not the exact canonical directory "
                    "captured from the active CryoSPARC instance."
                )
        elif self.cryosparc_import_roots:
            allowed = any(
                resolved == root or root in resolved.parents
                for root in self.cryosparc_import_roots
            )
            if not allowed:
                roots = ", ".join(str(root) for root in self.cryosparc_import_roots)
                raise ConfigurationError(
                    "CryoSPARC source project is outside the optional "
                    f"RELION_CRYOSPARC_IMPORT_ROOTS restriction: {roots}"
                )
        else:
            raise ConfigurationError(
                "CryoSPARC source access requires the combined route's live project "
                "inventory, or an explicit RELION_CRYOSPARC_IMPORT_ROOTS restriction."
            )
        return resolved

    def resolve_project_subpath(
        self,
        project: Path,
        relative_path: str | Path,
        *,
        must_exist: bool = False,
    ) -> Path:
        """Resolve a relative path while enforcing containment beneath ``project``."""
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ConfigurationError(f"Project paths must be relative: {relative_path}")
        candidate = project / relative
        try:
            resolved = candidate.resolve(strict=must_exist)
        except FileNotFoundError as exc:
            raise ConfigurationError(f"Project path does not exist: {candidate}") from exc
        except (OSError, RuntimeError) as exc:
            raise ConfigurationError(
                f"Project path cannot be resolved safely: {candidate}: {exc}"
            ) from exc
        if resolved != project and project not in resolved.parents:
            raise ConfigurationError(f"Project path resolves outside project_dir: {relative_path}")
        return resolved
