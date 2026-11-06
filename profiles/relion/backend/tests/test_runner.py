import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from relion_mcp import server
from relion_mcp.catalog import Catalog, CatalogError
from relion_mcp.runner import (
    RelionRunner,
    RelionRuntimeError,
    SegmentStartError,
    _safe_job,
)
from relion_mcp.settings import ConfigurationError, Settings
from relion_mcp.star import parse_star

_DEFAULT_MICROGRAPH_PIXEL = object()


def run_settings(project_root: Path) -> Settings:
    relion_bin = project_root / "relion-bin"
    relion_bin.mkdir(exist_ok=True)
    for executable_name in (
        "relion_pipeliner",
        "relion_image_handler",
        "relion_refine",
        "relion",
        "xvfb-run",
    ):
        executable = relion_bin / executable_name
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
    return Settings(
        mode="run",
        project_roots=(project_root.resolve(),),
        pipeliner_executable=str(relion_bin / "relion_pipeliner"),
        image_handler_executable=str(relion_bin / "relion_image_handler"),
        pipeline_name="default",
        add_job_timeout_seconds=90,
        utility_timeout_seconds=900,
    )


def test_initialize_project_uses_xvfb_when_display_is_unset(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    runner = RelionRunner(Catalog(), run_settings(tmp_path))
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        (project / "default_pipeline.star").write_text(
            "data_pipeline_general\n\n_rlnPipeLineJobCounter 1\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "native init ok", "")

    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = runner.initialize_project(str(project), "default", confirm=True)

    pipeline = project / "default_pipeline.star"
    assert result["pipeline_file"] == str(pipeline)
    assert result["pipeline_created"] is True
    assert result["initializer_stdout"] == "native init ok"
    assert (project / ".gui_projectdir").is_file()
    assert parse_star(pipeline)["pipeline_general"]["rlnPipeLineJobCounter"] == "1"
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert Path(command[0]).name == "xvfb-run"
    assert command[1:5] == [
        "-a",
        "-e",
        "/dev/stderr",
        str(tmp_path / "relion-bin" / "relion"),
    ]
    assert command[5:] == [
        "--pipeline",
        "default",
        "--do_projdir",
        "--refresh",
        "1",
        "--idle",
        "0",
    ]
    assert kwargs["cwd"] == project
    assert kwargs["timeout"] == 30


def test_initialize_project_uses_inherited_display_without_xvfb(
    tmp_path: Path, monkeypatch
):
    project = tmp_path / "project"
    settings = run_settings(tmp_path)
    (tmp_path / "relion-bin" / "xvfb-run").unlink()
    runner = RelionRunner(Catalog(), settings)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        (project / "default_pipeline.star").write_text(
            "data_pipeline_general\n\n_rlnPipeLineJobCounter 1\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "native init ok", "")

    monkeypatch.setenv("DISPLAY", "localhost:10.0")
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = runner.initialize_project(str(project), "default", confirm=True)

    assert result["pipeline_created"] is True
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == [
        str(tmp_path / "relion-bin" / "relion"),
        "--pipeline",
        "default",
        "--do_projdir",
        "--refresh",
        "1",
        "--idle",
        "0",
    ]
    assert kwargs["env"]["DISPLAY"] == "localhost:10.0"


def test_initialize_project_requires_display_or_xvfb_for_missing_pipeline(
    tmp_path: Path, monkeypatch
):
    project = tmp_path / "project"
    settings = run_settings(tmp_path)
    (tmp_path / "relion-bin" / "xvfb-run").unlink()
    runner = RelionRunner(Catalog(), settings)
    monkeypatch.delenv("DISPLAY", raising=False)

    with pytest.raises(RelionRuntimeError, match="DISPLAY.*xvfb-run"):
        runner.initialize_project(str(project), "default", confirm=True)

    assert not (project / ".gui_projectdir").exists()
    assert not (project / ".relion-mcp").exists()


def test_initialize_project_preserves_existing_pipeline(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    runner = RelionRunner(Catalog(), run_settings(tmp_path))
    project.mkdir()
    pipeline = project / "default_pipeline.star"
    original = "data_pipeline_general\n\n_rlnPipeLineJobCounter 7\n# preserve-existing\n"
    pipeline.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("RELION should not run")),
    )

    result = runner.initialize_project(str(project), "default", confirm=True)

    assert result["pipeline_created"] is False
    assert result["initializer_command"] is None
    assert pipeline.read_text(encoding="utf-8") == original


def test_initialize_project_reports_native_relion_failure(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    runner = RelionRunner(Catalog(), run_settings(tmp_path))
    monkeypatch.setenv("DISPLAY", "localhost:10.0")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", "display failed"),
    )

    try:
        runner.initialize_project(str(project), "default", confirm=True)
    except RelionRuntimeError as exc:
        assert "native project initialization failed" in str(exc)
        assert "display failed" in str(exc)
    else:
        raise AssertionError("Native RELION initialization failure was not reported")

    assert not (project / ".gui_projectdir").exists()
    assert not (project / ".relion-mcp").exists()


def test_initialize_project_preserves_original_creation_time(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "default_pipeline.star").write_text(
        "data_pipeline_general\n\n_rlnPipeLineJobCounter 1\n",
        encoding="utf-8",
    )
    runner = RelionRunner(Catalog(), run_settings(tmp_path))
    timestamps = iter((100.0, 200.0))
    monkeypatch.setattr("relion_mcp.runner.time.time", lambda: next(timestamps))

    runner.initialize_project(str(project), "default", confirm=True)
    first = json.loads((project / ".relion-mcp" / "project.json").read_text(encoding="utf-8"))
    runner.initialize_project(str(project), "default", confirm=True)
    second = json.loads((project / ".relion-mcp" / "project.json").read_text(encoding="utf-8"))

    assert first["created_at_epoch"] == 100.0
    assert second["created_at_epoch"] == first["created_at_epoch"]


def test_initialize_project_replaces_non_object_metadata(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "default_pipeline.star").write_text(
        "data_pipeline_general\n\n_rlnPipeLineJobCounter 1\n",
        encoding="utf-8",
    )
    metadata_dir = project / ".relion-mcp"
    metadata_dir.mkdir()
    metadata_path = metadata_dir / "project.json"
    metadata_path.write_text("[]\n", encoding="utf-8")
    runner = RelionRunner(Catalog(), run_settings(tmp_path))

    runner.initialize_project(str(project), "default", confirm=True)

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert isinstance(metadata, dict)
    assert isinstance(metadata["created_at_epoch"], float)
    assert metadata["pipeline_name"] == "default"


def test_initialize_project_rejects_symlinked_pipeline(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    unrelated = tmp_path / "unrelated.star"
    unrelated.write_text("not a RELION pipeline\n", encoding="utf-8")
    (project / "default_pipeline.star").symlink_to(unrelated)
    runner = RelionRunner(Catalog(), run_settings(tmp_path))

    with pytest.raises(CatalogError, match="pipeline file may not be a symbolic link"):
        runner.initialize_project(str(project), "default", confirm=True)

    assert not (project / ".gui_projectdir").exists()
    assert unrelated.read_text(encoding="utf-8") == "not a RELION pipeline\n"


def test_render_command_uses_argv_not_shell():
    settings = Settings(
        mode="plan",
        project_roots=(),
        pipeliner_executable="relion_pipeliner",
        image_handler_executable="relion_image_handler",
        pipeline_name="default",
        add_job_timeout_seconds=90,
        utility_timeout_seconds=900,
    )
    catalog = Catalog()
    runner = RelionRunner(catalog, settings)
    rendered = catalog.render_job(
        "ctf_estimation",
        inputs={"micrographs": "MotionCorr/job002/corrected_micrographs.star"},
        params={},
    )
    command = runner.render_command(rendered, "default", "ctf")
    assert isinstance(command, list)
    assert command[:5] == ["relion_pipeliner", "--pipeline", "default", "--addJob", "CtfFind"]
    assert ";" in command[6]


@pytest.mark.parametrize(
    "scratch_directory",
    ["/operator/scratch", "/operator/scratch/run-001"],
)
def test_per_job_scratch_policy_accepts_approved_base_or_descendant(
    tmp_path: Path, scratch_directory: str
):
    settings = replace(run_settings(tmp_path), scratch_directory="/operator/scratch")
    runner = RelionRunner(Catalog(), settings)
    rendered = runner.catalog.render_job(
        "two_d_classification",
        inputs={"particles": "Extract/job007/particles.star"},
        params={"num_classes": 50, "mask_diameter_A": 200, "scratch_directory": scratch_directory},
    )

    runner._validate_scratch_policy(rendered)


def test_per_job_scratch_policy_allows_explicit_disable_without_global_base(tmp_path: Path):
    runner = RelionRunner(Catalog(), run_settings(tmp_path))
    rendered = runner.catalog.render_job(
        "two_d_classification",
        inputs={"particles": "Extract/job007/particles.star"},
        params={"num_classes": 50, "mask_diameter_A": 200, "scratch_directory": ""},
    )

    runner._validate_scratch_policy(rendered)


def test_add_job_rejects_nonempty_scratch_without_global_base_before_relion(
    tmp_path: Path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    runner = RelionRunner(Catalog(), run_settings(tmp_path))
    rendered = runner.catalog.render_job(
        "two_d_classification",
        inputs={"particles": "Extract/job007/particles.star"},
        params={"num_classes": 50, "mask_diameter_A": 200, "scratch_directory": "/unapproved/scratch"},
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("RELION must not run for an unapproved scratch path")
        ),
    )

    with pytest.raises(ConfigurationError, match="requires.*RELION_SCRATCH_DIR"):
        runner.add_job(
            str(project),
            rendered,
            pipeline_name="default",
            alias="class2d",
            confirm=True,
        )


def test_per_job_scratch_policy_rejects_path_outside_global_base(tmp_path: Path):
    settings = replace(run_settings(tmp_path), scratch_directory="/operator/scratch")
    runner = RelionRunner(Catalog(), settings)
    rendered = runner.catalog.render_job(
        "two_d_classification",
        inputs={"particles": "Extract/job007/particles.star"},
        params={"num_classes": 50, "mask_diameter_A": 200, "scratch_directory": "/operator/scratch-cousin"},
    )

    with pytest.raises(ConfigurationError, match="equal or be beneath"):
        runner._validate_scratch_policy(rendered)


def test_render_job_rejects_empty_pipeline_name():
    with pytest.raises(CatalogError, match="pipeline_name"):
        server.relion_render_job(
            "ctf_estimation",
            inputs={"micrographs": "MotionCorr/job002/corrected_micrographs.star"},
            pipeline_name="",
        )


def test_small_symmetry_order_does_not_need_installed_probe(tmp_path: Path, monkeypatch):
    runner = RelionRunner(Catalog(), run_settings(tmp_path))
    rendered = runner.catalog.render_job(
        "initial_model",
        inputs={"particles": "Select/job008/particles.star"},
        params={"num_classes": 1, "mask_diameter_A": 200, "symmetry": "C99"},
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("one- and two-digit orders should not run a native probe")
        ),
    )

    result = runner.preflight_symmetry(rendered)

    assert result is not None
    assert result["status"] == "compatible"
    assert result["installed_probe"] == "not_required"


def test_installed_relion_can_accept_large_semantic_symmetry(tmp_path: Path, monkeypatch):
    runner = RelionRunner(Catalog(), run_settings(tmp_path))
    rendered = runner.catalog.render_job(
        "initial_model",
        inputs={"particles": "Select/job008/particles.star"},
        params={"num_classes": 1, "mask_diameter_A": 200, "symmetry": "D100"},
    )

    def accept_probe(command, **kwargs):
        assert Path(command[0]).name == "relion_refine"
        assert command[1:] == ["--sym", "D100", "--print_symmetry_ops"]
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", accept_probe)

    result = runner.preflight_symmetry(rendered)

    assert result is not None
    assert result["status"] == "compatible"
    assert result["installed_probe"][1:] == ["--sym", "D100", "--print_symmetry_ops"]


def test_large_symmetry_requires_an_installed_compatibility_probe(tmp_path: Path):
    runner = RelionRunner(Catalog(), run_settings(tmp_path))
    (tmp_path / "relion-bin" / "relion_refine").unlink()
    rendered = runner.catalog.render_job(
        "initial_model",
        inputs={"particles": "Select/job008/particles.star"},
        params={"num_classes": 1, "mask_diameter_A": 200, "symmetry": "C100"},
    )

    result = runner.preflight_symmetry(rendered)

    assert result is not None
    assert result["status"] == "unverified"
    assert result["reason"] == "relion_refine_not_found"

    with pytest.raises(RelionRuntimeError, match="relion_refine was not found") as error:
        runner.preflight_symmetry(rendered, require_verified=True)

    assert "satisfies the MCP semantic rule" in str(error.value)
    assert "No job was registered" in str(error.value)


def test_add_job_rejects_large_order_before_pipeline_registration(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    runner = RelionRunner(Catalog(), run_settings(tmp_path))
    rendered = runner.catalog.render_job(
        "initial_model",
        inputs={"particles": "Select/job008/particles.star"},
        params={"num_classes": 1, "mask_diameter_A": 200, "symmetry": "C100"},
    )
    calls = []

    def reject_probe(command, **kwargs):
        calls.append(command)
        assert Path(command[0]).name == "relion_refine"
        assert kwargs["stdout"] == subprocess.DEVNULL
        kwargs["stderr"].write(b"ERROR: do not recognize symmetry group C100\n")
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(subprocess, "run", reject_probe)

    with pytest.raises(RelionRuntimeError, match="satisfies the MCP semantic rule") as error:
        runner.add_job(
            str(project),
            rendered,
            pipeline_name="default",
            alias="large_symmetry",
            confirm=True,
        )

    assert calls == [
        [
            str(tmp_path / "relion-bin" / "relion_refine"),
            "--sym",
            "C100",
            "--print_symmetry_ops",
        ]
    ]
    assert "Installed RELION rejected symmetry 'C100' during preflight" in str(error.value)
    assert "No job was registered" in str(error.value)
    assert "do not recognize symmetry group C100" in str(error.value)


def test_extreme_order_is_semantic_but_not_automatically_probed(tmp_path: Path):
    runner = RelionRunner(Catalog(), run_settings(tmp_path))
    rendered = runner.catalog.render_job(
        "initial_model",
        inputs={"particles": "Select/job008/particles.star"},
        params={"num_classes": 1, "mask_diameter_A": 200, "symmetry": "C10000"},
    )

    result = runner.preflight_symmetry(rendered)

    assert result is not None
    assert result["status"] == "unverified"
    assert result["reason"] == "order_exceeds_safe_probe_ceiling"
    with pytest.raises(RelionRuntimeError, match="not a semantic symmetry limit"):
        runner.preflight_symmetry(rendered, require_verified=True)


def test_unrelated_native_probe_failure_is_not_misreported_as_rejection(
    tmp_path: Path, monkeypatch
):
    runner = RelionRunner(Catalog(), run_settings(tmp_path))
    rendered = runner.catalog.render_job(
        "initial_model",
        inputs={"particles": "Select/job008/particles.star"},
        params={"num_classes": 1, "mask_diameter_A": 200, "symmetry": "D100"},
    )

    def fail_probe(command, **kwargs):
        kwargs["stderr"].write(b"shared library could not be loaded\n")
        return subprocess.CompletedProcess(command, 127)

    monkeypatch.setattr(subprocess, "run", fail_probe)

    result = runner.preflight_symmetry(rendered)

    assert result is not None
    assert result["status"] == "unverified"
    assert result["reason"] == "probe_exit_127"
    with pytest.raises(RelionRuntimeError, match="did not identify an unsupported symmetry"):
        runner.preflight_symmetry(rendered, require_verified=True)


def test_run_job_registers_and_starts_with_one_public_confirmation(
    tmp_path: Path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    runner = RelionRunner(Catalog(), run_settings(tmp_path))
    rendered = runner.catalog.render_job(
        "ctf_estimation",
        inputs={"micrographs": "MotionCorr/job002/corrected_micrographs.star"},
        params={},
    )
    calls: list[tuple[str, bool]] = []

    def fake_add_job(*args, confirm: bool, **kwargs):
        calls.append(("add", confirm))
        return {
            "project_dir": str(project),
            "pipeline_name": "default",
            "job": "CtfFind/job003/",
        }

    def fake_start_jobs(*args, confirm: bool, **kwargs):
        calls.append(("start", confirm))
        return {"pid": 1234, "jobs": ["CtfFind/job003/"]}

    monkeypatch.setattr(runner, "add_job", fake_add_job)
    monkeypatch.setattr(runner, "start_jobs", fake_start_jobs)

    result = runner.run_job(
        str(project),
        rendered,
        pipeline_name="default",
        alias="ctf_estimation",
        schedule_name=None,
        confirm=True,
    )

    assert calls == [("add", True), ("start", True)]
    assert result["job"] == "CtfFind/job003/"
    assert result["schedule_name"] == "ctf_estimation__run"
    assert result["pipeline_registered"] is True
    assert result["started"] is True


def test_start_jobs_archives_native_scheduler_log_after_process_exits(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    runner = RelionRunner(Catalog(), run_settings(tmp_path))
    pipeliner = tmp_path / "relion-bin" / "relion_pipeliner"
    pipeliner.write_text(
        """#!/bin/sh
while [ "$#" -gt 0 ]; do
    if [ "$1" = "--schedule" ]; then
        schedule="$2"
        break
    fi
    shift
done
[ -L "pipeline_${schedule}.log" ] || exit 3
printf 'native scheduler log\n' > "pipeline_${schedule}.log"
""",
        encoding="utf-8",
    )

    result = runner.start_jobs(
        str(project),
        ["CtfFind/job003/"],
        "ctf_estimation_run",
        "default",
        confirm=True,
    )
    _, wait_status = os.waitpid(result["pid"], 0)

    assert os.waitstatus_to_exitcode(wait_status) == 0
    assert Path(result["log"]).read_text(encoding="utf-8") == "native scheduler log\n"
    assert not (project / "pipeline_ctf_estimation_run.log").exists()
    assert not Path(result["diagnostic_log"]).exists()


def test_start_jobs_reports_launcher_oserror(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    runner = RelionRunner(Catalog(), run_settings(tmp_path))
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("process limit")),
    )

    with pytest.raises(
        RelionRuntimeError,
        match="Could not launch the RELION scheduler helper: process limit",
    ):
        runner.start_jobs(
            str(project),
            ["CtfFind/job003/"],
            "ctf_estimation_run",
            "default",
            confirm=True,
        )


def test_run_job_false_confirmation_blocks_before_registration(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    runner = RelionRunner(Catalog(), run_settings(tmp_path))
    rendered = runner.catalog.render_job(
        "ctf_estimation",
        inputs={"micrographs": "MotionCorr/job002/corrected_micrographs.star"},
        params={},
    )
    monkeypatch.setattr(
        runner,
        "add_job",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("an unconfirmed segment must not register jobs")
        ),
    )

    with pytest.raises(ConfigurationError, match="confirm=true"):
        runner.run_job(
            str(project),
            rendered,
            pipeline_name="default",
            alias="ctf_estimation",
            schedule_name=None,
            confirm=False,
        )


def test_run_job_start_failure_preserves_registered_job_for_recovery(
    tmp_path: Path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    runner = RelionRunner(Catalog(), run_settings(tmp_path))
    rendered = runner.catalog.render_job(
        "ctf_estimation",
        inputs={"micrographs": "MotionCorr/job002/corrected_micrographs.star"},
        params={},
    )
    monkeypatch.setattr(
        runner,
        "add_job",
        lambda *args, **kwargs: {
            "project_dir": str(project),
            "pipeline_name": "default",
            "job": "CtfFind/job001/",
        },
    )
    monkeypatch.setattr(
        runner,
        "start_jobs",
        lambda *args, **kwargs: (_ for _ in ()).throw(RelionRuntimeError("launch failed")),
    )

    with pytest.raises(SegmentStartError, match="must not be added again") as error:
        runner.run_job(
            str(project),
            rendered,
            pipeline_name="default",
            alias="ctf_estimation",
            schedule_name="import_run",
            confirm=True,
        )

    assert error.value.added_jobs == ["CtfFind/job001/"]
    assert error.value.schedule_name == "import_run"


def test_rejects_absolute_or_shallow_job_paths():
    for unsafe in ("/etc/passwd", "job001", "../Import/job001"):
        try:
            _safe_job(unsafe)
        except CatalogError:
            pass
        else:
            raise AssertionError(f"Unsafe job path was accepted: {unsafe}")


def test_accepts_normal_relion_job_path():
    assert _safe_job("Import/job001") == "Import/job001/"


def test_job_apis_reject_subtree_symlink_escape(tmp_path: Path):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    (outside / "job001").mkdir(parents=True)
    (outside / "job001" / "run.err").write_text("outside\n", encoding="utf-8")
    (project / "Select").symlink_to(outside, target_is_directory=True)
    runner = RelionRunner(Catalog(), run_settings(tmp_path))

    actions = (
        lambda: runner.job_status(str(project), "Select/job001/", "default"),
        lambda: runner.job_outputs(str(project), "Select/job001/"),
        lambda: runner.tail_job_log(str(project), "Select/job001/", "err", 10),
        lambda: runner.abort_job(str(project), "Select/job001/", confirm=True),
    )
    for action in actions:
        with pytest.raises(ConfigurationError, match="outside project_dir"):
            action()

    assert not (outside / "job001" / "RELION_JOB_ABORT_NOW").exists()


def test_start_jobs_rejects_log_directory_symlink_escape(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / "Log").symlink_to(outside, target_is_directory=True)
    runner = RelionRunner(Catalog(), run_settings(tmp_path))
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("the scheduler launcher must not start")
        ),
    )

    with pytest.raises(ConfigurationError, match="outside project_dir"):
        runner.start_jobs(
            str(project),
            ["CtfFind/job001/"],
            "ctf_run",
            "default",
            confirm=True,
        )

    assert list(outside.iterdir()) == []


def test_execution_environment_prepends_configured_relion_bin(
    tmp_path: Path, monkeypatch
):
    relion_bin = tmp_path / "relion-bin"
    relion_bin.mkdir()
    pipeliner = relion_bin / "relion_pipeliner"
    image_handler = relion_bin / "relion_image_handler"
    relion_import = relion_bin / "relion_import"
    for executable in (pipeliner, image_handler, relion_import):
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)

    inherited_path = os.pathsep.join(("/usr/local/bin", "/usr/bin", "/bin"))
    monkeypatch.setenv("PATH", inherited_path)
    monkeypatch.setenv("CRYOSPARC_PASSWORD", "must-not-reach-relion-jobs")
    monkeypatch.setenv(
        "EMCLAW_CRYOSPARC_PROJECT_INVENTORY_JSON",
        '[{"uid":"P1","project_dir":"/cryosparc/P1","device":1,"inode":2}]',
    )
    settings = Settings(
        mode="plan",
        project_roots=(),
        pipeliner_executable=str(pipeliner),
        image_handler_executable="relion_image_handler",
        pipeline_name="default",
        add_job_timeout_seconds=90,
        utility_timeout_seconds=900,
    )
    runner = RelionRunner(Catalog(), settings)

    environment = runner._execution_environment()

    assert "CRYOSPARC_PASSWORD" not in environment
    assert "EMCLAW_CRYOSPARC_PROJECT_INVENTORY_JSON" not in environment
    path_parts = environment["PATH"].split(os.pathsep)
    assert path_parts[0] == str(relion_bin)
    assert path_parts[1:] == inherited_path.split(os.pathsep)
    assert runner._resolve_execution_executable(
        settings.image_handler_executable,
        environment,
    ) == str(image_handler)
    reported = runner.environment()
    assert reported["image_handler"] == str(image_handler)
    assert reported["symmetry_alignment_limits"] == {
        "max_input_box_size_pix": 512,
        "minimum_scoring_radius_shells": 3,
    }


def test_execution_environment_sets_configured_scratch_and_reports_host_status(
    tmp_path: Path, monkeypatch
):
    scratch_directory = tmp_path / "scratch"
    scratch_directory.mkdir()
    settings = replace(
        run_settings(tmp_path),
        scratch_directory=str(scratch_directory),
    )
    runner = RelionRunner(Catalog(), settings)
    monkeypatch.setenv("RELION_SCRATCH_DIR", "/ambient/value")

    environment = runner._execution_environment()
    reported = runner.environment()["scratch_directory"]

    assert environment["RELION_SCRATCH_DIR"] == str(scratch_directory)
    assert reported == {
        "configured": str(scratch_directory),
        "exists_on_mcp_host": True,
        "is_directory_on_mcp_host": True,
        "writable_on_mcp_host": True,
        "host_checks_are_advisory": True,
    }


def test_execution_environment_clears_unconfigured_ambient_scratch(
    tmp_path: Path, monkeypatch
):
    runner = RelionRunner(Catalog(), run_settings(tmp_path))
    monkeypatch.setenv("RELION_SCRATCH_DIR", "/ambient/value")

    environment = runner._execution_environment()
    reported = runner.environment()["scratch_directory"]

    assert "RELION_SCRATCH_DIR" not in environment
    assert reported == {
        "configured": None,
        "exists_on_mcp_host": None,
        "is_directory_on_mcp_host": None,
        "writable_on_mcp_host": None,
        "host_checks_are_advisory": True,
    }


def test_environment_reports_configured_topaz_executable(tmp_path: Path):
    topaz = tmp_path / "external" / "site-topaz"
    topaz.parent.mkdir()
    topaz.write_text("#!/bin/sh\n", encoding="utf-8")
    topaz.chmod(0o755)
    settings = replace(run_settings(tmp_path), topaz_executable=str(topaz))
    runner = RelionRunner(Catalog(), settings)

    reported = runner.environment()

    assert reported["topaz"] == str(topaz)
    assert str(topaz.parent) not in reported["execution_path"].split(os.pathsep)


def test_execution_executable_expands_user_directory(tmp_path: Path, monkeypatch):
    executable = tmp_path / "bin" / "relion_pipeliner"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = RelionRunner(Catalog(), run_settings(tmp_path))

    assert runner._resolve_execution_executable("~/bin/relion_pipeliner") == str(executable)


def test_environment_rejects_empty_project_path(tmp_path: Path):
    runner = RelionRunner(Catalog(), run_settings(tmp_path))

    with pytest.raises(ConfigurationError, match="project_dir must be an absolute path"):
        runner.environment("")


def test_rescale_map_resolves_bare_handler_from_execution_path(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    input_map = project / "Refine3D" / "map.mrc"
    input_map.parent.mkdir(parents=True)
    input_map.write_bytes(b"input-map")
    settings = replace(
        run_settings(tmp_path),
        image_handler_executable="relion_image_handler",
    )
    runner = RelionRunner(Catalog(), settings)
    monkeypatch.setenv("PATH", "")

    def fake_run(command, **kwargs):
        output = kwargs["cwd"] / command[command.index("--o") + 1]
        output.write_bytes(b"output-map")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = runner.rescale_map(
        str(project),
        "Refine3D/map.mrc",
        3.54,
        1.244,
        256,
        None,
        "_box256",
        overwrite=False,
        confirm=True,
    )

    assert result["command"][0] == str(tmp_path / "relion-bin" / "relion_image_handler")
    assert result["output_map"] == "Refine3D/map_box256.mrc"


def test_job_status_reads_native_relion_exit_markers(tmp_path: Path):
    project = tmp_path / "project"
    job_dir = project / "Select" / "job001"
    job_dir.mkdir(parents=True)
    runner = RelionRunner(Catalog(), run_settings(tmp_path))

    for marker, expected in (
        ("RELION_JOB_EXIT_SUCCESS", "Succeeded"),
        ("RELION_JOB_EXIT_FAILURE", "Failed"),
        ("RELION_JOB_EXIT_ABORTED", "Aborted"),
    ):
        for existing in job_dir.glob("RELION_JOB_EXIT_*"):
            existing.unlink()
        (job_dir / marker).touch()
        status = runner.job_status(str(project), "Select/job001/", "default")
        assert status["marker_status"] == expected
        assert "output_verification" not in status


def _write_completed_import_status_fixture(
    project: Path,
    *,
    output_edge: str,
    output_contents: str | None,
    pipeline_status: str = "Succeeded",
) -> None:
    job_dir = project / "Import" / "job001"
    job_dir.mkdir(parents=True)
    (job_dir / "RELION_JOB_EXIT_SUCCESS").touch()
    (project / "default_pipeline.star").write_text(
        "data_pipeline_processes\n\n"
        "loop_\n"
        "_rlnPipeLineProcessName #1\n"
        "_rlnPipeLineProcessStatusLabel #2\n"
        f"Import/job001/ {pipeline_status}\n\n",
        encoding="utf-8",
    )
    (job_dir / "job_pipeline.star").write_text(
        "data_pipeline_output_edges\n\n"
        "loop_\n"
        "_rlnPipeLineEdgeProcess #1\n"
        "_rlnPipeLineEdgeToNode #2\n"
        f"Import/job001/ {output_edge}\n\n",
        encoding="utf-8",
    )
    if output_contents is not None:
        output_path = project / output_edge
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output_contents, encoding="utf-8")


_TWO_PARTICLE_STAR = """data_particles

loop_
_rlnImageName #1
000001@particles.mrcs
000002@particles.mrcs

"""

_EMPTY_PARTICLE_STAR = """data_particles

loop_
_rlnImageName #1

"""


def test_job_status_verifies_completed_particle_import(tmp_path: Path):
    project = tmp_path / "project"
    _write_completed_import_status_fixture(
        project,
        output_edge="Import/job001/particles.star",
        output_contents=_TWO_PARTICLE_STAR,
    )
    runner = RelionRunner(Catalog(), run_settings(tmp_path))

    status = runner.job_status(
        str(project),
        "Import/job001/",
        "default",
        operation="import_particles",
        expected_particle_count=2,
    )

    assert status["marker_status"] == "Succeeded"
    assert status["pipeline_status"] == "Succeeded"
    assert status["completion_status"] == "verified"
    assert status["completion_verified"] is True
    verification = status["output_verification"]
    assert verification["native_succeeded"] is True
    assert verification["output_verified"] is True
    assert verification["particle_count"] == 2
    assert verification["issues"] == []


def test_job_status_rejects_successful_movie_mode_as_particle_import(tmp_path: Path):
    project = tmp_path / "project"
    _write_completed_import_status_fixture(
        project,
        output_edge="Import/job001/movies.star",
        output_contents="data_movies\n\n",
    )
    runner = RelionRunner(Catalog(), run_settings(tmp_path))

    status = runner.job_status(
        str(project),
        "Import/job001/",
        "default",
        operation="import_particles",
        expected_particle_count=5545,
    )

    assert status["marker_status"] == "Succeeded"
    assert status["pipeline_status"] == "Succeeded"
    assert status["completion_status"] == "output_failed"
    assert status["completion_verified"] is False
    verification = status["output_verification"]
    assert verification["output_verified"] is False
    assert {issue["code"] for issue in verification["issues"]} == {
        "unexpected_movie_output_edge",
        "expected_particle_output_edge_missing",
    }


@pytest.mark.parametrize(
    ("output_contents", "expected_particle_count", "issue_code"),
    (
        (None, None, "output_file_unavailable"),
        (_EMPTY_PARTICLE_STAR, None, "empty_particle_table"),
        (_TWO_PARTICLE_STAR, 3, "particle_count_mismatch"),
    ),
    ids=("missing-file", "empty-table", "count-mismatch"),
)
def test_job_status_rejects_invalid_particle_import_outputs(
    tmp_path: Path,
    output_contents: str | None,
    expected_particle_count: int | None,
    issue_code: str,
):
    project = tmp_path / "project"
    _write_completed_import_status_fixture(
        project,
        output_edge="Import/job001/particles.star",
        output_contents=output_contents,
    )
    runner = RelionRunner(Catalog(), run_settings(tmp_path))

    status = runner.job_status(
        str(project),
        "Import/job001/",
        "default",
        operation="import_particles",
        expected_particle_count=expected_particle_count,
    )

    assert status["completion_status"] == "output_failed"
    assert status["completion_verified"] is False
    verification = status["output_verification"]
    assert verification["output_verified"] is False
    assert issue_code in {issue["code"] for issue in verification["issues"]}


def test_job_status_reports_pending_until_both_native_statuses_succeed(tmp_path: Path):
    project = tmp_path / "project"
    _write_completed_import_status_fixture(
        project,
        output_edge="Import/job001/particles.star",
        output_contents=_TWO_PARTICLE_STAR,
        pipeline_status="Running",
    )
    runner = RelionRunner(Catalog(), run_settings(tmp_path))

    status = runner.job_status(
        str(project),
        "Import/job001/",
        "default",
        operation="import_particles",
    )

    assert status["output_verification"]["output_verified"] is True
    assert status["output_verification"]["native_succeeded"] is False
    assert status["completion_status"] == "pending"
    assert status["completion_verified"] is False


def test_job_status_server_forwards_output_verification_arguments(monkeypatch):
    calls = []

    def fake_job_status(
        project_dir,
        job,
        pipeline_name,
        operation=None,
        expected_particle_count=None,
    ):
        calls.append(
            (
                project_dir,
                job,
                pipeline_name,
                operation,
                expected_particle_count,
            )
        )
        return {"ok": True}

    monkeypatch.setattr(server.RUNNER, "job_status", fake_job_status)

    result = server.relion_job_status(
        "/project",
        "Import/job001/",
        "named",
        "import_particles",
        5545,
    )

    assert result == {"ok": True}
    assert calls == [
        (
            "/project",
            "Import/job001/",
            "named",
            "import_particles",
            5545,
        )
    ]


def test_rescale_map_rejects_in_place_output(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    input_map = project / "Refine3D" / "map.mrc"
    input_map.parent.mkdir(parents=True)
    input_map.write_bytes(b"input-map")
    runner = RelionRunner(Catalog(), run_settings(tmp_path))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("relion_image_handler must not run")
        ),
    )

    with pytest.raises(CatalogError, match="must differ from input_map"):
        runner.rescale_map(
            str(project),
            "Refine3D/map.mrc",
            3.54,
            1.244,
            256,
            "Refine3D/map.mrc",
            "_box256",
            overwrite=True,
            confirm=True,
        )


def test_rescale_map_rejects_success_without_output(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    input_map = project / "Refine3D" / "map.mrc"
    input_map.parent.mkdir(parents=True)
    input_map.write_bytes(b"input-map")
    runner = RelionRunner(Catalog(), run_settings(tmp_path))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )

    with pytest.raises(RelionRuntimeError, match="without creating the requested output map"):
        runner.rescale_map(
            str(project),
            "Refine3D/map.mrc",
            3.54,
            1.244,
            256,
            "Refine3D/map_box256.mrc",
            "_box256",
            overwrite=False,
            confirm=True,
        )


def _write_particles_star_table(
    path: Path,
    optics_rows: list[tuple[str, float | str, float | str | None, float | str | None]],
    particle_rows: list[tuple[str, str]] | None = None,
) -> None:
    if particle_rows is None:
        particle_rows = [
            ("000001@Extract/job001/mic.mrcs", optics_rows[0][0]),
            ("000002@Extract/job001/mic.mrcs", optics_rows[0][0]),
        ]
    path.parent.mkdir(parents=True, exist_ok=True)
    include_micrograph = any(row[2] is not None for row in optics_rows)
    include_original = any(row[3] is not None for row in optics_rows)
    optics_columns = [
        "_rlnOpticsGroup #1",
        "_rlnImagePixelSize #2",
        "_rlnVoltage #3",
        "_rlnSphericalAberration #4",
    ]
    if include_micrograph:
        optics_columns.append(f"_rlnMicrographPixelSize #{len(optics_columns) + 1}")
    if include_original:
        optics_columns.append(
            f"_rlnMicrographOriginalPixelSize #{len(optics_columns) + 1}"
        )
    optics_lines = []
    for group, image_pixel, micrograph_pixel, original_pixel in optics_rows:
        values = [group, str(image_pixel), "300.0", "2.7"]
        if include_micrograph:
            values.append("." if micrograph_pixel is None else str(micrograph_pixel))
        if include_original:
            values.append("." if original_pixel is None else str(original_pixel))
        optics_lines.append(" ".join(values))
    particle_lines = [
        f"{100 + index * 800}.0 {100 + index * 800}.0 {image_name} "
        f"Micrographs/mic{index + 1}.mrc {group} 0.0 0.0"
        for index, (image_name, group) in enumerate(particle_rows)
    ]
    path.write_text(
        "\ndata_optics\n\nloop_\n"
        + "\n".join(optics_columns)
        + "\n"
        + "\n".join(optics_lines)
        + "\n\ndata_particles\n\nloop_\n"
        "_rlnCoordinateX #1\n"
        "_rlnCoordinateY #2\n"
        "_rlnImageName #3\n"
        "_rlnMicrographName #4\n"
        "_rlnOpticsGroup #5\n"
        "_rlnOriginXAngst #6\n"
        "_rlnOriginYAngst #7\n"
        + "\n".join(particle_lines)
        + "\n",
        encoding="utf-8",
    )


def _write_particles_star(
    path: Path,
    image_pixel: float | str,
    original_pixel: float | str | None,
    *,
    micrograph_pixel: float | str | None | object = _DEFAULT_MICROGRAPH_PIXEL,
    particle_rows: list[tuple[str, str]] | None = None,
) -> None:
    if micrograph_pixel is _DEFAULT_MICROGRAPH_PIXEL:
        micrograph_pixel = image_pixel
    _write_particles_star_table(
        path,
        [("1", image_pixel, micrograph_pixel, original_pixel)],
        particle_rows,
    )


def _particle_loop_layout(path: Path) -> tuple[list[str], list[str], int, int]:
    """Return fixture lines, particle columns, and the contiguous row slice."""
    lines = path.read_text(encoding="utf-8").splitlines()
    block_index = (
        lines.index("data_particles")
        if "data_particles" in lines
        else lines.index("data_")
    )
    loop_index = lines.index("loop_", block_index + 1)
    columns: list[str] = []
    cursor = loop_index + 1
    while cursor < len(lines):
        stripped = lines[cursor].strip()
        if not stripped or stripped.startswith("#"):
            cursor += 1
            continue
        if stripped.startswith("_"):
            columns.append(stripped.split()[0][1:])
            cursor += 1
            continue
        break
    row_start = cursor
    while cursor < len(lines):
        stripped = lines[cursor].strip()
        if not stripped or stripped.startswith(("data_", "loop_", "_")):
            break
        cursor += 1
    return lines, columns, row_start, cursor


def _replace_first_particle_value(path: Path, column: str, value: str) -> None:
    lines, columns, row_start, _ = _particle_loop_layout(path)
    row = lines[row_start].split()
    row[columns.index(column)] = value
    lines[row_start] = " ".join(row)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _append_particle_column(path: Path, column: str, values: list[str]) -> None:
    lines, columns, row_start, row_end = _particle_loop_layout(path)
    if row_end - row_start != len(values):
        raise AssertionError("one fixture value is required per particle row")
    lines.insert(row_start, f"_{column} #{len(columns) + 1}")
    for offset, value in enumerate(values, start=1):
        lines[row_start + offset] += f" {value}"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_removed_particles_star(
    path: Path,
    particle_rows: list[tuple[str, str]],
    *,
    block_name: str = "",
) -> None:
    """Model RELION's removed table, including legacy unnamed ``data_`` blocks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        f"{100 + (int(image_name.split('@', 1)[0]) - 1) * 800}.0 "
        f"{100 + (int(image_name.split('@', 1)[0]) - 1) * 800}.0 {image_name} "
        f"Micrographs/mic{int(image_name.split('@', 1)[0])}.mrc {group} 0.0 0.0"
        for image_name, group in particle_rows
    ]
    path.write_text(
        f"data_{block_name}\n\nloop_\n"
        "_rlnCoordinateX #1\n"
        "_rlnCoordinateY #2\n"
        "_rlnImageName #3\n"
        "_rlnMicrographName #4\n"
        "_rlnOpticsGroup #5\n"
        "_rlnOriginXAngst #6\n"
        "_rlnOriginYAngst #7\n"
        + "\n".join(rows)
        + "\n",
        encoding="utf-8",
    )


def _dedup_settings(project_root: Path):
    settings = run_settings(project_root)
    star_handler = project_root / "relion-bin" / "relion_star_handler"
    star_handler.write_text("#!/bin/sh\n", encoding="utf-8")
    star_handler.chmod(0o755)
    return replace(settings, star_handler_executable=str(star_handler))


def test_remove_duplicate_particles_refuses_ambiguous_missing_micrograph_pixel_size(
    tmp_path: Path,
):
    project = tmp_path / "project"
    _write_particles_star(
        project / "Extract" / "job001" / "particles.star",
        5.24,
        1.31,
        micrograph_pixel=None,
    )
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    with pytest.raises(CatalogError) as excinfo:
        runner.remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
            None,
            confirm=True,
        )

    message = str(excinfo.value)
    assert "Cannot infer the pre-extraction coordinate pixel size" in message
    assert "motion-correction binning" in message
    assert "image_pixel_size_A=1.31" not in message
    assert not (project / "Deduplicate").exists()


def test_render_remove_duplicates_requires_explicit_scale_even_when_image_equals_original(
    tmp_path: Path,
):
    project = tmp_path / "project"
    _write_particles_star(
        project / "Extract/job001/particles.star",
        1.31,
        1.31,
        micrograph_pixel=None,
    )
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    with pytest.raises(CatalogError, match="Equality or difference"):
        runner.render_remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
        )


def test_remove_duplicate_particles_passes_image_angpix_and_reports_removed_star(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    _write_particles_star(
        project / "Extract" / "job001" / "particles.star",
        5.24,
        1.31,
        micrograph_pixel=None,
    )
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    def fake_run(command, **kwargs):
        output = kwargs["cwd"] / command[command.index("--o") + 1]
        _write_particles_star(
            output,
            5.24,
            1.31,
            micrograph_pixel=None,
            particle_rows=[("000001@Extract/job001/mic.mrcs", "1")],
        )
        _write_removed_particles_star(
            output.with_name(output.stem + "_removed.star"),
            [("000002@Extract/job001/mic.mrcs", "1")],
            block_name="particles",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = runner.remove_duplicate_particles(
        str(project),
        "Extract/job001/particles.star",
        200.0,
        "Deduplicate/union.star",
        1.31,
        confirm=True,
    )

    assert result["command"][-2:] == ["--image_angpix", "1.31"]
    assert "--remove_duplicates" in result["command"]
    assert result["particles_are_downsampled"] is True
    assert result["removed_particles_star"] == "Deduplicate/union_removed.star"
    assert result["input_summary"]["particle_count"] == 2
    assert result["output_summary"]["particle_count"] == 1
    assert result["removed_summary"]["particle_count"] == 1
    assert result["removed_particle_count"] == 1
    assert result["publication"]["published_without_replacement"] is True
    assert result["relion_pipeline_job_registered"] is False


def test_remove_duplicates_accepts_native_unnamed_removed_sidecar(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    _write_particles_star(project / "Extract/job001/particles.star", 1.31, 1.31)
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))
    staged_sidecar_bytes: list[bytes] = []

    def fake_run(command, **kwargs):
        output = kwargs["cwd"] / command[command.index("--o") + 1]
        _write_particles_star(
            output,
            1.31,
            1.31,
            particle_rows=[("000001@Extract/job001/mic.mrcs", "1")],
        )
        _write_removed_particles_star(
            output.with_name(output.stem + "_removed.star"),
            [("000002@Extract/job001/mic.mrcs", "1")],
        )
        staged_sidecar_bytes.append(
            output.with_name(output.stem + "_removed.star").read_bytes()
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = runner.remove_duplicate_particles(
        str(project),
        "Extract/job001/particles.star",
        30.0,
        "Deduplicate/union.star",
        None,
        confirm=True,
    )

    assert result["removed_particle_count"] == 1
    assert result["removed_summary"]["optics_block_present"] is False
    assert result["removed_summary"]["empty_native_table"] is False
    assert result["publication"]["published_without_replacement"] is True
    assert result["publication"]["staging_cleanup_complete"] is True
    assert result["relion_pipeline_job_registered"] is False
    assert (project / "Deduplicate/union.star").is_file()
    removed = project / "Deduplicate/union_removed.star"
    assert removed.read_bytes() == staged_sidecar_bytes[0]
    assert b"data_particles" not in removed.read_bytes()
    assert b"data_\n\nloop_" in removed.read_bytes()
    assert not list((project / "Deduplicate").glob(".relion-deduplicate-*"))


def test_remove_duplicate_particles_refuses_to_replace_an_existing_output(
    tmp_path: Path,
):
    project = tmp_path / "project"
    _write_particles_star(project / "Extract" / "job001" / "particles.star", 1.31, 1.31)
    existing = project / "Deduplicate" / "union.star"
    _write_particles_star(existing, 1.31, 1.31)
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    with pytest.raises(CatalogError, match="already exists"):
        runner.remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
            None,
            confirm=True,
        )


def test_render_remove_duplicates_uses_micrograph_pixel_not_original_pixel(
    tmp_path: Path,
):
    project = tmp_path / "project"
    _write_particles_star(
        project / "Extract" / "job001" / "particles.star",
        5.24,
        1.31,
        micrograph_pixel=2.62,
    )
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    plan = runner.render_remove_duplicate_particles(
        str(project),
        "Extract/job001/particles.star",
        200.0,
        "Deduplicate/union.star",
    )

    assert plan["status"] == "rendered"
    assert plan["micrograph_pixel_size_A"] == 2.62
    assert plan["micrograph_original_pixel_size_A"] == 1.31
    assert plan["image_pixel_size_A"] == 2.62
    assert plan["image_pixel_size_source"] == "rlnMicrographPixelSize"
    assert plan["command_template"][-2:] == ["--image_angpix", "2.62"]
    assert plan["removed_particles_star"] == "Deduplicate/union_removed.star"
    assert not (project / "Deduplicate").exists()


def test_render_remove_duplicates_does_not_treat_motioncorr_binning_as_extraction_rescaling(
    tmp_path: Path,
):
    project = tmp_path / "project"
    _write_particles_star(
        project / "Extract" / "job001" / "particles.star",
        2.62,
        1.31,
        micrograph_pixel=2.62,
    )
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    plan = runner.render_remove_duplicate_particles(
        str(project),
        "Extract/job001/particles.star",
        200.0,
        "Deduplicate/union.star",
    )

    assert plan["particles_are_downsampled"] is False
    assert plan["image_pixel_size_A"] == 2.62
    assert plan["command_template"][-2:] == ["--image_angpix", "2.62"]


def test_render_remove_duplicates_rejects_explicit_scale_conflicting_with_metadata(
    tmp_path: Path,
):
    project = tmp_path / "project"
    _write_particles_star(
        project / "Extract" / "job001" / "particles.star",
        5.24,
        1.31,
        micrograph_pixel=2.62,
    )
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    with pytest.raises(CatalogError, match="conflicts with the pre-extraction"):
        runner.render_remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
            1.31,
        )


def test_render_remove_duplicates_rejects_heterogeneous_used_optics_groups(
    tmp_path: Path,
):
    project = tmp_path / "project"
    _write_particles_star_table(
        project / "Extract" / "job001" / "particles.star",
        [
            ("1", 5.24, 2.62, 1.31),
            ("2", 4.00, 2.00, 1.00),
        ],
        [
            ("000001@Extract/job001/mic.mrcs", "1"),
            ("000002@Extract/job001/mic.mrcs", "2"),
        ],
    )
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    with pytest.raises(CatalogError, match="heterogeneous rlnMicrographPixelSize"):
        runner.render_remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
        )


def test_render_remove_duplicates_ignores_unreferenced_heterogeneous_optics_group(
    tmp_path: Path,
):
    project = tmp_path / "project"
    _write_particles_star_table(
        project / "Extract" / "job001" / "particles.star",
        [
            ("1", 5.24, 2.62, 1.31),
            ("2", 4.00, 2.00, 1.00),
        ],
        [("000001@Extract/job001/mic.mrcs", "1")],
    )
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    plan = runner.render_remove_duplicate_particles(
        str(project),
        "Extract/job001/particles.star",
        200.0,
        "Deduplicate/union.star",
    )

    assert plan["image_pixel_size_A"] == 2.62
    assert plan["optics_groups"]["1"]["used_by_particles"] is True
    assert plan["optics_groups"]["2"]["used_by_particles"] is False


def test_render_remove_duplicates_passes_used_scale_when_first_optics_group_is_unused(
    tmp_path: Path,
):
    project = tmp_path / "project"
    _write_particles_star_table(
        project / "Extract" / "job001" / "particles.star",
        [
            ("1", 1.00, 1.00, 1.00),
            ("2", 2.00, 2.00, 1.00),
        ],
        [("000001@Extract/job001/mic.mrcs", "2")],
    )
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    plan = runner.render_remove_duplicate_particles(
        str(project),
        "Extract/job001/particles.star",
        200.0,
        "Deduplicate/union.star",
    )

    assert plan["particles_are_downsampled"] is False
    assert plan["image_pixel_size_A"] == 2.0
    assert plan["command_template"][-2:] == ["--image_angpix", "2"]


def test_render_remove_duplicates_rejects_missing_used_particle_pixel_size(
    tmp_path: Path,
):
    project = tmp_path / "project"
    _write_particles_star(
        project / "Extract" / "job001" / "particles.star",
        ".",
        1.31,
        micrograph_pixel=1.31,
    )
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    with pytest.raises(
        CatalogError,
        match="does not safely parse STAR null marker.*rlnImagePixelSize",
    ):
        runner.render_remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
        )


def test_render_remove_duplicates_rejects_missing_native_coordinate_column(
    tmp_path: Path,
):
    project = tmp_path / "project"
    particles = project / "Extract/job001/particles.star"
    _write_particles_star(particles, 1.31, 1.31)
    particles.write_text(
        particles.read_text(encoding="utf-8").replace(
            "_rlnOriginYAngst #7",
            "_rlnRandomSubset #7",
        ),
        encoding="utf-8",
    )
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    with pytest.raises(CatalogError, match="rlnOriginYAngst"):
        runner.render_remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
        )


def test_render_remove_duplicates_rejects_tomographic_particle_table(
    tmp_path: Path,
):
    project = tmp_path / "project"
    particles = project / "Extract/job001/particles.star"
    _write_particles_star(particles, 1.31, 1.31)
    particles.write_text(
        particles.read_text(encoding="utf-8")
        .replace(
            "_rlnOriginYAngst #7\n",
            "_rlnOriginYAngst #7\n_rlnCenteredCoordinateZAngst #8\n",
        )
        .replace(" 0.0 0.0\n", " 0.0 0.0 0.0\n"),
        encoding="utf-8",
    )
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    with pytest.raises(CatalogError, match="SPA particle coordinates only"):
        runner.render_remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
        )


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("rlnCoordinateX", "?"),
        ("rlnCoordinateY", "text"),
        ("rlnOriginXAngst", "nan"),
        ("rlnOriginYAngst", "inf"),
    ],
)
def test_render_remove_duplicates_rejects_nonfinite_or_nonnumeric_particle_metadata(
    tmp_path: Path,
    column: str,
    value: str,
):
    project = tmp_path / "project"
    particles = project / "Extract/job001/particles.star"
    _write_particles_star(particles, 1.31, 1.31)
    _replace_first_particle_value(particles, column, value)
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    with pytest.raises(CatalogError, match=rf"{column} must be finite numeric metadata"):
        runner.render_remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
        )


@pytest.mark.parametrize(
    ("column", "marker"),
    [
        ("rlnImageName", "."),
        ("rlnImageName", "?"),
        ("rlnMicrographName", "."),
        ("rlnMicrographName", "?"),
    ],
)
def test_render_remove_duplicates_rejects_null_particle_names(
    tmp_path: Path,
    column: str,
    marker: str,
):
    project = tmp_path / "project"
    particles = project / "Extract/job001/particles.star"
    _write_particles_star(particles, 1.31, 1.31)
    _replace_first_particle_value(particles, column, marker)
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    with pytest.raises(CatalogError, match=rf"Missing {column}"):
        runner.render_remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
        )


@pytest.mark.parametrize("layout", ["split", "multiple"])
def test_render_remove_duplicates_rejects_nonphysical_particle_row_layout(
    tmp_path: Path,
    layout: str,
):
    project = tmp_path / "project"
    particles = project / "Extract/job001/particles.star"
    _write_particles_star(particles, 1.31, 1.31)
    lines, _, row_start, row_end = _particle_loop_layout(particles)
    rows = lines[row_start:row_end]
    if layout == "split":
        values = rows[0].split()
        replacement = [" ".join(values[:3]), " ".join(values[3:]), *rows[1:]]
    else:
        replacement = [rows[0] + " " + rows[1]]
    lines[row_start:row_end] = replacement
    particles.write_text("\n".join(lines) + "\n", encoding="utf-8")
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    with pytest.raises(CatalogError, match="exactly one complete particle row"):
        runner.render_remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
        )


@pytest.mark.parametrize(
    "replacement",
    [
        "1 1.31 300.0\n2.7 1.31 1.31",
        "1 1.31 300.0 2.7 1.31",
        "1 1.31 300.0 2.7 1.31 1.31 extra",
    ],
    ids=["split", "truncated", "oversized"],
)
def test_render_remove_duplicates_rejects_malformed_physical_optics_rows(
    tmp_path: Path,
    replacement: str,
):
    project = tmp_path / "project"
    particles = project / "Extract/job001/particles.star"
    _write_particles_star(particles, 1.31, 1.31)
    optics_row = "1 1.31 300.0 2.7 1.31 1.31"
    contents = particles.read_text(encoding="utf-8")
    assert contents.count(optics_row) == 1
    particles.write_text(contents.replace(optics_row, replacement), encoding="utf-8")
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    with pytest.raises(
        CatalogError,
        match="Strict data_optics loop rows require exactly 6 values",
    ):
        runner.render_remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
        )


@pytest.mark.parametrize("block", ["particles", "optics"])
def test_render_remove_duplicates_rejects_interrow_comments(
    tmp_path: Path,
    block: str,
):
    project = tmp_path / "project"
    particles = project / "Extract/job001/particles.star"
    _write_particles_star(particles, 1.31, 1.31)
    if block == "particles":
        lines, _, row_start, _ = _particle_loop_layout(particles)
        lines.insert(row_start + 1, "# comment after particle data began")
        particles.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        optics_row = "1 1.31 300.0 2.7 1.31 1.31"
        contents = particles.read_text(encoding="utf-8")
        particles.write_text(
            contents.replace(
                optics_row,
                optics_row + "\n# comment after optics data began",
            ),
            encoding="utf-8",
        )
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    with pytest.raises(CatalogError, match="full-line comments after .* rows begin"):
        runner.render_remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
        )


def test_render_remove_duplicates_rejects_crlf_star_input(tmp_path: Path):
    project = tmp_path / "project"
    particles = project / "Extract/job001/particles.star"
    _write_particles_star(particles, 1.31, 1.31)
    particles.write_bytes(particles.read_bytes().replace(b"\n", b"\r\n"))
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    with pytest.raises(CatalogError, match="CRLF line endings"):
        runner.render_remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
        )


def test_render_remove_duplicates_rejects_bare_cr_star_input(tmp_path: Path):
    project = tmp_path / "project"
    particles = project / "Extract/job001/particles.star"
    _write_particles_star(particles, 1.31, 1.31)
    particles.write_bytes(particles.read_bytes().replace(b"\n", b"\r"))
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    with pytest.raises(CatalogError, match="bare carriage returns"):
        runner.render_remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
        )


def test_render_remove_duplicates_rejects_invalid_utf8_star_input(tmp_path: Path):
    project = tmp_path / "project"
    particles = project / "Extract/job001/particles.star"
    _write_particles_star(particles, 1.31, 1.31)
    particles.write_bytes(particles.read_bytes().replace(b"mic.mrcs", b"mic\x80.mrcs"))
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    with pytest.raises(CatalogError, match="Invalid UTF-8"):
        runner.render_remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
        )


@pytest.mark.parametrize("block", ["particles", "optics"])
def test_render_remove_duplicates_rejects_duplicate_column_labels(
    tmp_path: Path,
    block: str,
):
    project = tmp_path / "project"
    particles = project / "Extract/job001/particles.star"
    _write_particles_star(particles, 1.31, 1.31)
    if block == "particles":
        old_header = "_rlnOriginYAngst #7"
        duplicate_header = "_rlnOriginXAngst #7"
    else:
        old_header = "_rlnSphericalAberration #4"
        duplicate_header = "_rlnVoltage #4"
    contents = particles.read_text(encoding="utf-8")
    particles.write_text(
        contents.replace(old_header, duplicate_header),
        encoding="utf-8",
    )
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    with pytest.raises(CatalogError, match="duplicate column labels"):
        runner.render_remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
        )


@pytest.mark.parametrize(
    ("block", "label"),
    [
        ("particles", "rlnComment"),
        ("particles", "rlnSortedIndex"),
        ("optics", "rlnComment"),
        ("optics", "rlnSortedIndex"),
    ],
)
def test_render_remove_duplicates_rejects_relion_writer_special_loop_columns(
    tmp_path: Path,
    block: str,
    label: str,
):
    project = tmp_path / "project"
    particles = project / "Extract/job001/particles.star"
    _write_particles_star(particles, 1.31, 1.31)
    if block == "particles":
        old_header = "_rlnOriginYAngst #7"
        replacement = f"_{label} #7"
    else:
        old_header = "_rlnSphericalAberration #4"
        replacement = f"_{label} #4"
    contents = particles.read_text(encoding="utf-8")
    particles.write_text(
        contents.replace(old_header, replacement),
        encoding="utf-8",
    )
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    with pytest.raises(
        CatalogError,
        match=rf"does not write as ordinary loop values: {label}",
    ):
        runner.render_remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
        )


@pytest.mark.parametrize(
    ("header", "replacement", "missing"),
    [
        ("_rlnOpticsGroup #5", "_rlnRandomSubset #5", "rlnOpticsGroup"),
        ("_rlnVoltage #3", "_rlnAmplitudeContrast #3", "rlnVoltage"),
        (
            "_rlnSphericalAberration #4",
            "_rlnAmplitudeContrast #4",
            "rlnSphericalAberration",
        ),
    ],
)
def test_render_remove_duplicates_rejects_missing_observation_model_metadata(
    tmp_path: Path,
    header: str,
    replacement: str,
    missing: str,
):
    project = tmp_path / "project"
    particles = project / "Extract/job001/particles.star"
    _write_particles_star(particles, 1.31, 1.31)
    particles.write_text(
        particles.read_text(encoding="utf-8").replace(header, replacement),
        encoding="utf-8",
    )
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    with pytest.raises(CatalogError, match=missing):
        runner.render_remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
        )


@pytest.mark.parametrize("group", ["0", "01", "foo"])
def test_render_remove_duplicates_rejects_noncanonical_optics_group(
    tmp_path: Path,
    group: str,
):
    project = tmp_path / "project"
    _write_particles_star_table(
        project / "Extract/job001/particles.star",
        [(group, 1.31, 1.31, 1.31)],
        [("000001@Extract/job001/mic.mrcs", group)],
    )
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    with pytest.raises(CatalogError, match="non-canonical rlnOpticsGroup"):
        runner.render_remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
        )


@pytest.mark.parametrize("groups", [("2", "1"), ("1", "3")])
def test_render_remove_duplicates_rejects_optics_groups_native_would_renumber(
    tmp_path: Path,
    groups: tuple[str, str],
):
    project = tmp_path / "project"
    _write_particles_star_table(
        project / "Extract/job001/particles.star",
        [
            (groups[0], 1.31, 1.31, 1.31),
            (groups[1], 1.31, 1.31, 1.31),
        ],
        [
            ("000001@Extract/job001/mic.mrcs", groups[0]),
            ("000002@Extract/job001/mic.mrcs", groups[1]),
        ],
    )
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    with pytest.raises(
        CatalogError,
        match="RELION loadSafely would otherwise renumber optics groups",
    ):
        runner.render_remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
        )


def test_render_remove_duplicates_rejects_zero_particle_input(tmp_path: Path):
    project = tmp_path / "project"
    _write_particles_star(
        project / "Extract/job001/particles.star",
        1.31,
        1.31,
        particle_rows=[],
    )
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    with pytest.raises(CatalogError, match="zero particle rows"):
        runner.render_remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
        )


def test_render_remove_duplicates_rejects_empty_optics_block(tmp_path: Path):
    project = tmp_path / "project"
    particles = project / "Extract/job001/particles.star"
    _write_particles_star(particles, 1.31, 1.31)
    contents = particles.read_text(encoding="utf-8")
    particles.write_text(
        "data_optics\n\n# comment-only block\n\n"
        + contents[contents.index("data_particles") :],
        encoding="utf-8",
    )
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    with pytest.raises(CatalogError, match="data_optics block.*is empty"):
        runner.render_remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
        )


def test_remove_duplicates_refuses_existing_implicit_removed_sidecar(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    _write_particles_star(project / "Extract/job001/particles.star", 1.31, 1.31)
    sidecar = project / "Deduplicate" / "union_removed.star"
    _write_particles_star(sidecar, 1.31, 1.31)
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("relion_star_handler must not run")
        ),
    )

    with pytest.raises(CatalogError, match="Removed-particles sidecar already exists"):
        runner.remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
            None,
            confirm=True,
        )


def test_remove_duplicates_refuses_symlinked_implicit_removed_sidecar(
    tmp_path: Path,
):
    project = tmp_path / "project"
    _write_particles_star(project / "Extract/job001/particles.star", 1.31, 1.31)
    sidecar = project / "Deduplicate" / "union_removed.star"
    sidecar.parent.mkdir(parents=True)
    sidecar.symlink_to(tmp_path / "outside.star")
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    with pytest.raises(CatalogError, match="including symbolic links"):
        runner.render_remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
        )


def test_remove_duplicates_rolls_back_first_link_on_sidecar_publication_race(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    _write_particles_star(project / "Extract/job001/particles.star", 1.31, 1.31)
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))
    raced_sidecar = project / "Deduplicate" / "union_removed.star"

    def fake_run(command, **kwargs):
        output = kwargs["cwd"] / command[command.index("--o") + 1]
        _write_particles_star(
            output,
            1.31,
            1.31,
            particle_rows=[("000001@Extract/job001/mic.mrcs", "1")],
        )
        _write_removed_particles_star(
            output.with_name(output.stem + "_removed.star"),
            [("000002@Extract/job001/mic.mrcs", "1")],
        )
        raced_sidecar.write_text("concurrent writer", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(CatalogError, match="appeared during publication"):
        runner.remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
            None,
            confirm=True,
        )

    assert not (project / "Deduplicate" / "union.star").exists()
    assert raced_sidecar.read_text(encoding="utf-8") == "concurrent writer"
    assert not list((project / "Deduplicate").glob(".relion-deduplicate-*"))


def test_remove_duplicates_staging_is_anchored_against_parent_symlink_race(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    _write_particles_star(project / "Extract/job001/particles.star", 1.31, 1.31)
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))
    moved_parent = project / "Deduplicate-moved"
    outside = tmp_path / "outside"
    outside.mkdir()

    def fake_run(command, **kwargs):
        staging = Path(kwargs["cwd"])
        output = staging / command[command.index("--o") + 1]
        _write_particles_star(
            output,
            1.31,
            1.31,
            particle_rows=[("000001@Extract/job001/mic.mrcs", "1")],
        )
        _write_removed_particles_star(
            output.with_name(output.stem + "_removed.star"),
            [("000002@Extract/job001/mic.mrcs", "1")],
        )
        (project / "Deduplicate").rename(moved_parent)
        (project / "Deduplicate").symlink_to(outside, target_is_directory=True)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(CatalogError, match="parent must not contain symbolic links"):
        runner.remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
            None,
            confirm=True,
        )

    assert list(outside.iterdir()) == []
    assert not list(moved_parent.glob(".relion-deduplicate-*"))


def test_remove_duplicates_rejects_invalid_generated_removed_star(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    _write_particles_star(project / "Extract/job001/particles.star", 1.31, 1.31)
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    def fake_run(command, **kwargs):
        output = kwargs["cwd"] / command[command.index("--o") + 1]
        _write_particles_star(
            output,
            1.31,
            1.31,
            particle_rows=[("000001@Extract/job001/mic.mrcs", "1")],
        )
        output.with_name(output.stem + "_removed.star").write_text(
            "data_\n\nloop_\n_rlnCoordinateX #1\n100\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RelionRuntimeError, match="invalid particle STAR"):
        runner.remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
            None,
            confirm=True,
        )

    assert not (project / "Deduplicate" / "union.star").exists()
    assert not (project / "Deduplicate" / "union_removed.star").exists()
    assert not list((project / "Deduplicate").glob(".relion-deduplicate-*"))


def test_remove_duplicates_accepts_native_empty_removed_sidecar(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    _write_particles_star(project / "Extract/job001/particles.star", 1.31, 1.31)
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    def fake_run(command, **kwargs):
        output = kwargs["cwd"] / command[command.index("--o") + 1]
        _write_particles_star(output, 1.31, 1.31)
        output.with_name(output.stem + "_removed.star").write_bytes(b"")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = runner.remove_duplicate_particles(
        str(project),
        "Extract/job001/particles.star",
        200.0,
        "Deduplicate/union.star",
        None,
        confirm=True,
    )

    assert result["removed_particle_count"] == 0
    assert result["removed_summary"]["empty_native_table"] is True
    assert (project / "Deduplicate" / "union_removed.star").stat().st_size == 0


def test_remove_duplicates_rejects_identity_nonpartition_with_conserved_counts(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    _write_particles_star(project / "Extract/job001/particles.star", 1.31, 1.31)
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    def fake_run(command, **kwargs):
        output = kwargs["cwd"] / command[command.index("--o") + 1]
        duplicate_row = [("000001@Extract/job001/mic.mrcs", "1")]
        _write_particles_star(
            output,
            1.31,
            1.31,
            particle_rows=duplicate_row,
        )
        _write_removed_particles_star(
            output.with_name(output.stem + "_removed.star"),
            duplicate_row,
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RelionRuntimeError, match="do not partition"):
        runner.remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
            None,
            confirm=True,
        )

    assert not (project / "Deduplicate" / "union.star").exists()
    assert not (project / "Deduplicate" / "union_removed.star").exists()


def test_remove_duplicates_rejects_zero_retained_particles(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    _write_particles_star(project / "Extract/job001/particles.star", 1.31, 1.31)
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    def fake_run(command, **kwargs):
        output = kwargs["cwd"] / command[command.index("--o") + 1]
        _write_particles_star(output, 1.31, 1.31, particle_rows=[])
        _write_removed_particles_star(
            output.with_name(output.stem + "_removed.star"),
            [
                ("000001@Extract/job001/mic.mrcs", "1"),
                ("000002@Extract/job001/mic.mrcs", "1"),
            ],
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RelionRuntimeError, match="retained zero particles"):
        runner.remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
            None,
            confirm=True,
        )

    assert not (project / "Deduplicate" / "union.star").exists()
    assert not (project / "Deduplicate" / "union_removed.star").exists()


def test_remove_duplicates_rejects_mutated_particle_row_metadata(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    _write_particles_star(project / "Extract/job001/particles.star", 1.31, 1.31)
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    def fake_run(command, **kwargs):
        output = kwargs["cwd"] / command[command.index("--o") + 1]
        _write_particles_star(
            output,
            1.31,
            1.31,
            particle_rows=[("000001@Extract/job001/mic.mrcs", "1")],
        )
        output.write_text(
            output.read_text(encoding="utf-8").replace(
                "100.0 100.0 000001@",
                "101.0 100.0 000001@",
            ),
            encoding="utf-8",
        )
        _write_removed_particles_star(
            output.with_name(output.stem + "_removed.star"),
            [("000002@Extract/job001/mic.mrcs", "1")],
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RelionRuntimeError, match="complete input particle-row metadata"):
        runner.remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
            None,
            confirm=True,
        )

    assert not (project / "Deduplicate" / "union.star").exists()
    assert not (project / "Deduplicate" / "union_removed.star").exists()


def test_remove_duplicates_does_not_normalize_numeric_looking_string_metadata(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    particles = project / "Extract/job001/particles.star"
    _write_particles_star(particles, 1.31, 1.31)
    _replace_first_particle_value(particles, "rlnMicrographName", "100")
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    def fake_run(command, **kwargs):
        output = kwargs["cwd"] / command[command.index("--o") + 1]
        _write_particles_star(
            output,
            1.31,
            1.31,
            particle_rows=[("000001@Extract/job001/mic.mrcs", "1")],
        )
        _replace_first_particle_value(
            output,
            "rlnMicrographName",
            "100.000000",
        )
        _write_removed_particles_star(
            output.with_name(output.stem + "_removed.star"),
            [("000002@Extract/job001/mic.mrcs", "1")],
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RelionRuntimeError, match="complete input particle-row metadata"):
        runner.remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
            None,
            confirm=True,
        )

    assert not (project / "Deduplicate" / "union.star").exists()
    assert not (project / "Deduplicate" / "union_removed.star").exists()


def test_remove_duplicates_accepts_relion_vector_and_alias_reformatting(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    particles = project / "Extract/job001/particles.star"
    _write_particles_star(particles, 1.31, 1.31)
    _append_particle_column(
        particles,
        "rlnOddZernike",
        ["[1.00000000000,2.50000000000]", "[3.00000000000,-4.25000000000]"],
    )
    _append_particle_column(
        particles,
        "rlnKullbackLeibnerDivergence",
        ["1.25", "2.5"],
    )
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    def fake_run(command, **kwargs):
        output = kwargs["cwd"] / command[command.index("--o") + 1]
        _write_particles_star(
            output,
            1.31,
            1.31,
            particle_rows=[("000001@Extract/job001/mic.mrcs", "1")],
        )
        _append_particle_column(output, "rlnOddZernike", ["[1,2.5]"])
        _append_particle_column(
            output,
            "rlnKullbackLeiblerDivergence",
            ["1.250000"],
        )
        removed = output.with_name(output.stem + "_removed.star")
        _write_removed_particles_star(
            removed,
            [("000002@Extract/job001/mic.mrcs", "1")],
        )
        _append_particle_column(removed, "rlnOddZernike", ["[3,-4.25]"])
        _append_particle_column(
            removed,
            "rlnKullbackLeiblerDivergence",
            ["2.500000"],
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = runner.remove_duplicate_particles(
        str(project),
        "Extract/job001/particles.star",
        200.0,
        "Deduplicate/union.star",
        None,
        confirm=True,
    )

    assert result["output_summary"]["particle_count"] == 1
    assert result["removed_particle_count"] == 1


def test_remove_duplicates_rejects_mutated_retained_optics_metadata(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    particles = project / "Extract/job001/particles.star"
    _write_particles_star(particles, 1.31, 1.31)
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    def fake_run(command, **kwargs):
        output = kwargs["cwd"] / command[command.index("--o") + 1]
        _write_particles_star(
            output,
            1.31,
            1.31,
            particle_rows=[("000001@Extract/job001/mic.mrcs", "1")],
        )
        output.write_text(
            output.read_text(encoding="utf-8").replace(
                "1 1.31 300.0 2.7 1.31 1.31",
                "1 1.31 200.0 2.7 1.31 1.31",
            ),
            encoding="utf-8",
        )
        _write_removed_particles_star(
            output.with_name(output.stem + "_removed.star"),
            [("000002@Extract/job001/mic.mrcs", "1")],
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RelionRuntimeError, match="changed retained data_optics metadata"):
        runner.remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
            None,
            confirm=True,
        )

    assert not (project / "Deduplicate" / "union.star").exists()
    assert not (project / "Deduplicate" / "union_removed.star").exists()


def test_remove_duplicates_rejects_optics_in_removed_sidecar(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    _write_particles_star(project / "Extract/job001/particles.star", 1.31, 1.31)
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    def fake_run(command, **kwargs):
        output = kwargs["cwd"] / command[command.index("--o") + 1]
        _write_particles_star(
            output,
            1.31,
            1.31,
            particle_rows=[("000001@Extract/job001/mic.mrcs", "1")],
        )
        _write_particles_star(
            output.with_name(output.stem + "_removed.star"),
            1.31,
            1.31,
            particle_rows=[("000002@Extract/job001/mic.mrcs", "1")],
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RelionRuntimeError, match="unexpectedly wrote data_optics"):
        runner.remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
            None,
            confirm=True,
        )

    assert not (project / "Deduplicate" / "union.star").exists()
    assert not (project / "Deduplicate" / "union_removed.star").exists()


def test_remove_duplicates_rejects_empty_optics_block_in_removed_sidecar(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    _write_particles_star(project / "Extract/job001/particles.star", 1.31, 1.31)
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    def fake_run(command, **kwargs):
        output = kwargs["cwd"] / command[command.index("--o") + 1]
        _write_particles_star(
            output,
            1.31,
            1.31,
            particle_rows=[("000001@Extract/job001/mic.mrcs", "1")],
        )
        removed = output.with_name(output.stem + "_removed.star")
        _write_removed_particles_star(
            removed,
            [("000002@Extract/job001/mic.mrcs", "1")],
        )
        removed.write_text(
            "data_optics\n\n# comment-only block\n\n"
            + removed.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RelionRuntimeError, match="unexpectedly wrote data_optics"):
        runner.remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
            None,
            confirm=True,
        )

    assert not (project / "Deduplicate" / "union.star").exists()
    assert not (project / "Deduplicate" / "union_removed.star").exists()


def test_remove_duplicates_rejects_nonconserving_generated_counts(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    _write_particles_star(project / "Extract/job001/particles.star", 1.31, 1.31)
    runner = RelionRunner(Catalog(), _dedup_settings(tmp_path))

    def fake_run(command, **kwargs):
        output = kwargs["cwd"] / command[command.index("--o") + 1]
        _write_particles_star(output, 1.31, 1.31)
        _write_removed_particles_star(
            output.with_name(output.stem + "_removed.star"),
            [("000003@Extract/job001/mic.mrcs", "1")],
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RelionRuntimeError, match="particle-count conservation"):
        runner.remove_duplicate_particles(
            str(project),
            "Extract/job001/particles.star",
            200.0,
            "Deduplicate/union.star",
            None,
            confirm=True,
        )

    assert not (project / "Deduplicate" / "union.star").exists()
    assert not (project / "Deduplicate" / "union_removed.star").exists()
