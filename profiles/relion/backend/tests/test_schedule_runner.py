from pathlib import Path

import pytest

from relion_mcp._schedule_runner import _archive_scheduler_log, run_schedule


def test_archive_scheduler_log_moves_new_log_into_log_directory(tmp_path: Path):
    source = tmp_path / "pipeline_ctf_run.log"
    source.write_text("first run\n", encoding="utf-8")

    destination = _archive_scheduler_log(tmp_path, "ctf_run")

    assert destination == tmp_path / "Log" / source.name
    assert destination.read_text(encoding="utf-8") == "first run\n"
    assert not source.exists()


def test_archive_scheduler_log_appends_when_schedule_name_is_reused(tmp_path: Path):
    destination = tmp_path / "Log" / "pipeline_ctf_run.log"
    destination.parent.mkdir()
    destination.write_text("first run\n", encoding="utf-8")
    source = tmp_path / destination.name
    source.write_text("second run\n", encoding="utf-8")

    _archive_scheduler_log(tmp_path, "ctf_run")

    assert destination.read_text(encoding="utf-8") == "first run\nsecond run\n"
    assert not source.exists()


def test_run_schedule_archives_native_log_and_removes_empty_diagnostic(tmp_path: Path):
    executable = tmp_path / "fake_relion_pipeliner"
    executable.write_text(
        """#!/bin/sh
[ -L pipeline_ctf_run.log ] || exit 3
printf 'scheduler output\n' > pipeline_ctf_run.log
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    diagnostic = tmp_path / "Log" / "pipeline_ctf_run.diagnostic.log"

    return_code = run_schedule(tmp_path, "ctf_run", diagnostic, [str(executable)])

    assert return_code == 0
    assert (tmp_path / "Log" / "pipeline_ctf_run.log").read_text(encoding="utf-8") == (
        "scheduler output\n"
    )
    assert not (tmp_path / "pipeline_ctf_run.log").exists()
    assert not diagnostic.exists()


def test_run_schedule_keeps_native_console_diagnostics(tmp_path: Path):
    executable = tmp_path / "fake_relion_pipeliner"
    executable.write_text("#!/bin/sh\nprintf 'native error\\n' >&2\nexit 1\n", encoding="utf-8")
    executable.chmod(0o755)
    diagnostic = tmp_path / "Log" / "pipeline_ctf_run.diagnostic.log"

    return_code = run_schedule(tmp_path, "ctf_run", diagnostic, [str(executable)])

    assert return_code == 1
    assert diagnostic.read_text(encoding="utf-8") == "native error\n"


def test_run_schedule_rejects_log_directory_symlink_escape(tmp_path: Path):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / "Log").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="outside project_dir"):
        run_schedule(
            project,
            "ctf_run",
            project / "Log" / "pipeline_ctf_run.diagnostic.log",
            ["true"],
        )

    assert list(outside.iterdir()) == []
