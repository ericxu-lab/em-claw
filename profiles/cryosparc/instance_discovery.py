"""Safe, read-only discovery of projects and jobs on the configured CryoSPARC instance."""

from __future__ import annotations

import json
import math
import os
import re
import sys
from pathlib import Path, PurePath
from typing import Any

_PROJECT_UID = re.compile(r"P[1-9][0-9]*")
_JOB_UID = re.compile(r"J[1-9][0-9]*")
_MAX_CS_FILES = 256
_MAX_DIRECTORY_ENTRIES = 4096
_MAX_OUTPUT_GROUPS = 64
_MAX_METAFILES_PER_GROUP = 16
_MAX_HANDOFF_CS_FILES = 512
_MAX_LIVE_PROJECTS = 512
_MAX_PROJECT_INVENTORY_CHARS = 64 * 1024
_MAX_TEXT_CHARS = 512
_MAX_PATH_CHARS = 1024


class CryosparcDiscoveryError(ValueError):
    """Raised when live metadata cannot identify a safe local project/job tree."""


def _validate_uid(value: Any, *, role: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise CryosparcDiscoveryError(f"{role} must use CryoSPARC UID form {pattern.pattern!r}.")
    return value


def _document(value: Any, *, role: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    document = getattr(value, "doc", None)
    if isinstance(document, dict):
        return document
    raise CryosparcDiscoveryError(f"CryoSPARC returned no readable {role} document.")


def _required_string(document: dict[str, Any], key: str, *, role: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CryosparcDiscoveryError(f"CryoSPARC {role} has no usable {key}.")
    return value


def _optional_text(value: Any, *, maximum: int = _MAX_TEXT_CHARS) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:maximum]


def _bounded_num_items(value: Any) -> int | float | list[int | float] | None:
    def scalar(item: Any) -> int | float | None:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        if isinstance(item, float) and not math.isfinite(item):
            return None
        return item

    direct = scalar(value)
    if direct is not None:
        return direct
    if not isinstance(value, list) or len(value) > 32:
        return None
    result: list[int | float] = []
    for item in value:
        checked = scalar(item)
        if checked is None:
            return None
        result.append(checked)
    return result


def _api_directory(value: Any, document: dict[str, Any], key: str) -> Path:
    resolver = getattr(value, "dir", None)
    if callable(resolver):
        try:
            raw_value = resolver()
        except Exception as exc:
            raise CryosparcDiscoveryError(
                f"CryoSPARC could not resolve the authoritative {key}: {type(exc).__name__}"
            ) from exc
    else:
        raw_value = _required_string(document, key, role=key.removesuffix("_dir"))
    if not isinstance(raw_value, (str, os.PathLike, PurePath)) or not str(
        raw_value
    ).strip():
        raise CryosparcDiscoveryError(f"CryoSPARC returned no usable {key}.")
    return Path(raw_value).expanduser()


def _resolve_project_directory(
    value: Any,
    document: dict[str, Any],
    project_uid: str,
) -> Path:
    raw = _api_directory(value, document, "project_dir")
    if not raw.is_absolute():
        raise CryosparcDiscoveryError("CryoSPARC project_dir must be absolute.")
    try:
        resolved = raw.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        raise CryosparcDiscoveryError(
            f"The live {project_uid} project directory is unavailable on this host: {raw}"
        ) from exc
    if not resolved.is_dir():
        raise CryosparcDiscoveryError(
            f"The live {project_uid} project path is not a directory: {resolved}"
        )
    return resolved


def _resolve_job_directory(
    project: Path,
    value: Any,
    document: dict[str, Any],
    job_uid: str,
) -> tuple[str, Path]:
    resolver = getattr(value, "dir", None)
    if callable(resolver):
        raw = _api_directory(value, document, "job_dir")
        candidate = raw if raw.is_absolute() else project / raw
    else:
        raw_value = document.get("job_dir", job_uid)
        if not isinstance(raw_value, str) or not raw_value.strip():
            raw_value = job_uid
        raw = Path(raw_value)
        candidate = project / raw
    if ".." in raw.parts:
        raise CryosparcDiscoveryError("CryoSPARC job_dir must stay beneath project_dir.")
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        raise CryosparcDiscoveryError(
            f"The live {job_uid} job directory is unavailable beneath {project}."
        ) from exc
    if project not in resolved.parents or not resolved.is_dir():
        raise CryosparcDiscoveryError("CryoSPARC job_dir resolves outside project_dir.")
    return resolved.relative_to(project).as_posix(), resolved


def _safe_metafiles(project: Path, raw_metafiles: Any) -> tuple[list[str], bool]:
    if not isinstance(raw_metafiles, list):
        return [], False
    result: list[str] = []
    for raw in raw_metafiles[:_MAX_METAFILES_PER_GROUP]:
        if not isinstance(raw, str) or len(raw) > _MAX_PATH_CHARS:
            continue
        relative = Path(raw)
        if relative.suffix != ".cs":
            continue
        if relative.is_absolute() or ".." in relative.parts:
            raise CryosparcDiscoveryError(
                "CryoSPARC output metadata contains a path outside project_dir."
            )
        candidate = project / relative
        try:
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError, RuntimeError):
            continue
        if (
            project not in resolved.parents
            or candidate.is_symlink()
            or not resolved.is_file()
        ):
            raise CryosparcDiscoveryError(
                "CryoSPARC output metadata contains an unsafe CS file."
            )
        result.append(resolved.relative_to(project).as_posix())
    return list(dict.fromkeys(result)), len(raw_metafiles) > _MAX_METAFILES_PER_GROUP


def _output_groups(
    document: dict[str, Any], project: Path
) -> tuple[list[dict[str, Any]], bool, bool]:
    raw_groups = document.get("output_results", [])
    if not isinstance(raw_groups, list):
        return [], False, False
    groups: list[dict[str, Any]] = []
    metafiles_truncated = False
    for raw in raw_groups[:_MAX_OUTPUT_GROUPS]:
        if not isinstance(raw, dict):
            continue
        group: dict[str, Any] = {}
        for key in ("group_name", "type", "name"):
            checked = _optional_text(raw.get(key))
            if checked is not None:
                group[key] = checked
        num_items = _bounded_num_items(raw.get("num_items"))
        if num_items is not None:
            group["num_items"] = num_items
        if isinstance(raw.get("passthrough"), bool):
            group["passthrough"] = raw["passthrough"]
        metafiles, group_metafiles_truncated = _safe_metafiles(
            project,
            raw.get("metafiles"),
        )
        metafiles_truncated = metafiles_truncated or group_metafiles_truncated
        if metafiles:
            group["metafiles"] = metafiles
        groups.append(group)
    return groups, len(raw_groups) > _MAX_OUTPUT_GROUPS, metafiles_truncated


def _candidate_cs_files(
    project: Path, job: Path
) -> tuple[list[dict[str, Any]], bool]:
    candidates: list[dict[str, Any]] = []
    try:
        entries = os.scandir(job)
    except OSError as exc:
        raise CryosparcDiscoveryError(f"Could not inspect CryoSPARC job directory: {job}") from exc
    truncated = False
    with entries:
        for entry_index, entry in enumerate(entries):
            if entry_index >= _MAX_DIRECTORY_ENTRIES:
                truncated = True
                break
            if not entry.name.endswith(".cs") or entry.is_symlink():
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                size_bytes = entry.stat(follow_symlinks=False).st_size
            except OSError as exc:
                raise CryosparcDiscoveryError(
                    f"Could not stat CryoSPARC CS file: {entry.name}"
                ) from exc
            path = Path(entry.path)
            candidates.append(
                {
                    "path": path.relative_to(project).as_posix(),
                    "size_bytes": size_bytes,
                }
            )
            if len(candidates) >= _MAX_CS_FILES:
                truncated = True
                break
    candidates.sort(key=lambda item: item["path"])
    return candidates, truncated


def list_live_projects(cs: Any) -> list[dict[str, str | int]]:
    """Return a bounded UID/path inventory from the authenticated instance."""

    try:
        projects = cs.cli.list_projects()
    except Exception as exc:
        raise CryosparcDiscoveryError(
            "The configured CryoSPARC instance could not list projects: "
            f"{type(exc).__name__}"
        ) from exc
    if not isinstance(projects, list):
        raise CryosparcDiscoveryError(
            "CryoSPARC returned no readable live-project inventory."
        )
    if len(projects) > _MAX_LIVE_PROJECTS:
        raise CryosparcDiscoveryError(
            "The live-project inventory exceeds the configured safety bound."
        )
    inventory: list[dict[str, str | int]] = []
    observed_uids: set[str] = set()
    observed_directories: set[Path] = set()
    for record in projects:
        document = _document(record, role="project inventory")
        project_uid = _validate_uid(
            document.get("uid"),
            role="project_uid",
            pattern=_PROJECT_UID,
        )
        if project_uid in observed_uids:
            raise CryosparcDiscoveryError(
                "CryoSPARC returned a repeated live project UID."
            )
        try:
            project_value = cs.find_project(project_uid)
        except Exception as exc:
            raise CryosparcDiscoveryError(
                "The configured CryoSPARC instance could not resolve a listed project: "
                f"{type(exc).__name__}"
            ) from exc
        project_document = _document(project_value, role="project")
        observed_uid = _required_string(project_document, "uid", role="project")
        if observed_uid != project_uid:
            raise CryosparcDiscoveryError(
                "CryoSPARC returned a different project for a listed project UID."
            )
        raw = _api_directory(project_value, project_document, "project_dir")
        if not raw.is_absolute():
            raise CryosparcDiscoveryError("CryoSPARC project_dir must be absolute.")
        try:
            resolved = raw.resolve(strict=True)
        except FileNotFoundError:
            # CryoSPARC may retain records for storage that is no longer mounted
            # on this host. Such projects cannot be imported here, so omit them.
            continue
        except (OSError, RuntimeError) as exc:
            raise CryosparcDiscoveryError(
                f"The live {project_uid} project directory could not be resolved safely."
            ) from exc
        if not resolved.is_dir():
            continue
        if resolved in observed_directories:
            raise CryosparcDiscoveryError(
                "CryoSPARC returned one directory for multiple live project UIDs."
            )
        observed_uids.add(project_uid)
        observed_directories.add(resolved)
        try:
            directory_stat = resolved.stat()
        except OSError as exc:
            raise CryosparcDiscoveryError(
                f"The live {project_uid} project directory could not be inspected safely."
            ) from exc
        inventory.append(
            {
                "uid": project_uid,
                "project_dir": str(resolved),
                "device": int(directory_stat.st_dev),
                "inode": int(directory_stat.st_ino),
            }
        )
    result = sorted(inventory, key=lambda item: item["uid"])
    encoded = json.dumps(result, separators=(",", ":"))
    if len(encoded) > _MAX_PROJECT_INVENTORY_CHARS:
        raise CryosparcDiscoveryError(
            "The live-project inventory exceeds the environment transport bound."
        )
    return result


def inspect_project_job(
    cs: Any,
    project_uid: str,
    job_uid: str,
) -> dict[str, Any]:
    """Resolve one project/job through the authenticated live CryoSPARC API."""

    checked_project_uid = _validate_uid(
        project_uid,
        role="project_uid",
        pattern=_PROJECT_UID,
    )
    checked_job_uid = _validate_uid(job_uid, role="job_uid", pattern=_JOB_UID)

    try:
        project_value = cs.find_project(checked_project_uid)
    except Exception as exc:
        raise CryosparcDiscoveryError(
            f"The configured CryoSPARC instance could not resolve project {checked_project_uid}: "
            f"{type(exc).__name__}"
        ) from exc
    project_document = _document(project_value, role="project")
    observed_project_uid = _required_string(project_document, "uid", role="project")
    if observed_project_uid != checked_project_uid:
        raise CryosparcDiscoveryError(
            f"CryoSPARC returned project {observed_project_uid!r} for {checked_project_uid}."
        )
    project_directory = _resolve_project_directory(
        project_value,
        project_document,
        checked_project_uid,
    )

    try:
        job_value = cs.find_job(checked_project_uid, checked_job_uid)
    except Exception as exc:
        raise CryosparcDiscoveryError(
            f"The configured CryoSPARC instance could not resolve job "
            f"{checked_project_uid}/{checked_job_uid}: {type(exc).__name__}"
        ) from exc
    job_document = _document(job_value, role="job")
    observed_job_uid = _required_string(job_document, "uid", role="job")
    if observed_job_uid != checked_job_uid:
        raise CryosparcDiscoveryError(
            f"CryoSPARC returned job {observed_job_uid!r} for {checked_job_uid}."
        )
    observed_job_project = job_document.get("project_uid")
    if observed_job_project is not None and observed_job_project != checked_project_uid:
        raise CryosparcDiscoveryError(
            "CryoSPARC returned a job belonging to a different project."
        )
    job_relative, job_directory = _resolve_job_directory(
        project_directory,
        job_value,
        job_document,
        checked_job_uid,
    )

    output_groups, output_groups_truncated, output_metafiles_truncated = _output_groups(
        job_document,
        project_directory,
    )
    cs_files, cs_files_truncated = _candidate_cs_files(
        project_directory,
        job_directory,
    )
    all_allowed_cs_files = list(
        dict.fromkeys(
            [
                metafile
                for group in output_groups
                for metafile in group.get("metafiles", [])
            ]
            + [item["path"] for item in cs_files]
        )
    )
    allowed_cs_files = all_allowed_cs_files[:_MAX_HANDOFF_CS_FILES]

    return {
        "connection_source": "active_cryosparc_environment",
        "source_project_dir": str(project_directory),
        "project": {
            "uid": checked_project_uid,
            "title": _optional_text(project_document.get("title")),
            "project_dir": str(project_directory),
        },
        "job": {
            "uid": checked_job_uid,
            "job_type": _optional_text(job_document.get("job_type")),
            "status": _optional_text(job_document.get("status")),
            "title": _optional_text(job_document.get("title")),
            "job_dir": job_relative,
            "output_groups": output_groups,
            "output_groups_truncated": output_groups_truncated,
            "output_metafiles_truncated": output_metafiles_truncated,
            "cs_files": cs_files,
            "cs_files_truncated": cs_files_truncated,
        },
        "relion_handoff": {
            "source_project_dir": str(project_directory),
            "project_uid": checked_project_uid,
            "job_uid": checked_job_uid,
            "allowed_cs_files": allowed_cs_files,
            "allowed_cs_files_truncated": (
                output_groups_truncated
                or output_metafiles_truncated
                or cs_files_truncated
                or len(all_allowed_cs_files) > len(allowed_cs_files)
            ),
        },
    }


def _main(argv: list[str]) -> int:
    if argv != ["--list-projects"]:
        print("usage: instance_discovery.py --list-projects", file=sys.stderr)
        return 64
    try:
        from cryosparc.tools import CryoSPARC

        client = CryoSPARC(
            license=os.environ["CRYOSPARC_LICENSE_ID"],
            host=os.environ["CRYOSPARC_MASTER_HOSTNAME"],
            base_port=int(os.environ["CRYOSPARC_BASE_PORT"]),
            email=os.environ["CRYOSPARC_EMAIL"],
            password=os.environ["CRYOSPARC_PASSWORD"],
        )
        inventory = list_live_projects(client)
    except Exception as exc:  # noqa: BLE001 - emit only the exception type
        print(f"CryoSPARC project discovery failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(json.dumps(inventory, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
