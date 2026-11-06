from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path


def _safe_schedule_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
        raise ValueError("Schedule names may contain only letters, digits, '.', '_' and '-'.")
    return value


def _contained_path(project: Path, path: Path) -> Path:
    resolved = path.resolve(strict=False)
    if resolved != project and project not in resolved.parents:
        raise ValueError(f"Scheduler path resolves outside project_dir: {path}")
    return resolved


def _archive_scheduler_log(project: Path, schedule_name: str) -> Path:
    """Move RELION's fixed-name scheduler log out of the project root."""
    project = project.resolve(strict=True)
    schedule_name = _safe_schedule_name(schedule_name)
    source = project / f"pipeline_{schedule_name}.log"
    destination = _contained_path(project, project / "Log" / source.name)
    if source.is_symlink():
        source.unlink()
        return destination
    if not source.is_file():
        return destination

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        with source.open("rb") as source_handle, destination.open("ab") as destination_handle:
            shutil.copyfileobj(source_handle, destination_handle)
        source.unlink()
    else:
        source.replace(destination)
    return destination


def _prepare_scheduler_log(project: Path, schedule_name: str) -> Path:
    """Point RELION's fixed root path at the permanent log directory."""
    source = project / f"pipeline_{schedule_name}.log"
    destination = _archive_scheduler_log(project, schedule_name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.symlink_to(Path("Log") / source.name)
    return destination


def run_schedule(
    project: Path,
    schedule_name: str,
    diagnostic_log: Path,
    command: list[str],
) -> int:
    """Run one native scheduler and archive the files it leaves behind."""
    project = project.resolve(strict=True)
    schedule_name = _safe_schedule_name(schedule_name)
    diagnostic_log = _contained_path(project, diagnostic_log)
    diagnostic_log.parent.mkdir(parents=True, exist_ok=True)
    initial_diagnostic_size = diagnostic_log.stat().st_size if diagnostic_log.exists() else 0
    return_code = 1
    with diagnostic_log.open("ab", buffering=0) as output:
        try:
            _prepare_scheduler_log(project, schedule_name)
            completed = subprocess.run(
                command,
                cwd=project,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                check=False,
            )
            return_code = completed.returncode
        except OSError as exc:
            message = f"Unable to launch the RELION schedule command: {exc}\n"
            output.write(message.encode("utf-8", errors="replace"))
        finally:
            _archive_scheduler_log(project, schedule_name)

    if diagnostic_log.stat().st_size == initial_diagnostic_size == 0:
        diagnostic_log.unlink()
    return return_code


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        separator = arguments.index("--")
    except ValueError:
        print("Missing '--' before the RELION command.", file=sys.stderr)
        return 2
    if separator != 3 or len(arguments) == 4:
        print(
            "Usage: _schedule_runner.py PROJECT SCHEDULE DIAGNOSTIC_LOG -- COMMAND ...",
            file=sys.stderr,
        )
        return 2

    project = Path(arguments[0]).resolve()
    schedule_name = arguments[1]
    diagnostic_log = Path(arguments[2]).resolve()
    try:
        return run_schedule(project, schedule_name, diagnostic_log, arguments[4:])
    except (OSError, ValueError) as exc:
        print(f"Cannot start RELION schedule: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
