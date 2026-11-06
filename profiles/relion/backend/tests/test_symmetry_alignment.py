from __future__ import annotations

import hashlib
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import mrcfile
import numpy as np
import pytest

from relion_mcp import server
from relion_mcp.catalog import Catalog, CatalogError
from relion_mcp.runner import RelionRunner, RelionRuntimeError
from relion_mcp.settings import Settings


def _run_settings(root: Path, *, max_box_size_px: int = 512) -> Settings:
    relion_bin = root / "relion-bin"
    relion_bin.mkdir(exist_ok=True)
    for name in (
        "relion_pipeliner",
        "relion_image_handler",
        "relion_align_symmetry",
    ):
        executable = relion_bin / name
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
    return Settings(
        mode="run",
        project_roots=(root.resolve(),),
        pipeliner_executable=str(relion_bin / "relion_pipeliner"),
        image_handler_executable=str(relion_bin / "relion_image_handler"),
        pipeline_name="default",
        add_job_timeout_seconds=90,
        utility_timeout_seconds=900,
        symmetry_alignment_max_box_size_px=max_box_size_px,
    )


def _write_test_map(path: Path, *, size: int = 64, voxel_size_A: float = 1.5) -> None:
    z, y, x = np.indices((size, size, size), dtype=np.float32)
    center = (size - 1.0) / 2.0
    data = np.exp(
        -(
            ((x - (center + 5.0)) / 7.0) ** 2
            + ((y - (center - 4.0)) / 9.0) ** 2
            + ((z - (center + 3.0)) / 11.0) ** 2
        )
        / 2.0
    )
    data += 0.4 * np.exp(
        -(
            ((x - (center - 12.0)) / 4.0) ** 2
            + ((y - (center + 9.0)) / 5.0) ** 2
            + ((z - (center - 7.0)) / 6.0) ** 2
        )
        / 2.0
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with mrcfile.new(path, overwrite=True) as handle:
        handle.set_data(np.asarray(data, dtype=np.float32))
        handle.voxel_size = voxel_size_A


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _project_files(project: Path) -> dict[str, bytes]:
    return {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
    }


def _resolved_path(cwd: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else cwd / path


def _assert_alignment_argv(
    command: list[str],
    *,
    executable: str,
    input_map: str,
    output_map: str,
    target_symmetry: str,
) -> None:
    assert command == [
        executable,
        "--i",
        input_map,
        "--sym",
        target_symmetry,
        "--o",
        output_map,
        "--box_size",
        "64",
        "--keep_centre",
        "--angpix",
        "1.5",
        "--nr_uniform",
        "400",
        "--maxres",
        "20",
        "--local_search_range",
        "2",
        "--local_search_step",
        "2",
        "--pad",
        "2",
    ]
    assert "--apply_sym" not in command
    assert "--only_rot" not in command
    assert "--NN" not in command


def _assert_alignment_contracts(
    result: dict[str, object],
    *,
    target_symmetry: str,
) -> None:
    assert result["schema_version"] == "relion.symmetry-reference-alignment/v2"
    assert result["operation"] == "align_symmetry_reference"
    assert result["target_symmetry"] == target_symmetry
    assert result["target_frame"] == "RELION standard point-group orientation"
    assert result["symmetry_source"] == {
        "kind": "built_in_relion_point_group",
        "custom_definition_file_allowed": False,
        "resolution_working_directory": "private_staging_directory",
    }
    assert result["command_template_working_directory"] == "<private_staging_directory>"
    if result["status"] == "rendered":
        assert "command_working_directory" not in result
    assert result["parameters"] == {
        "search_max_resolution_A": 20.0,
        "search_working_grid_nyquist_A": 3.0,
        "search_fourier_radius_shells": 5,
        "input_box_length_A": 96.0,
        "working_box_size_pix": 64,
        "uniform_orientation_samples": 400,
        "local_search_range": 2,
        "local_search_step_deg": 2.0,
        "padding_factor": 2,
        "interpolation": "trilinear",
    }
    publication = {
        "strategy": (
            "private_temporary_directory_then_verified_dirfd_no_replace_hard_link"
        ),
        "existing_output_refused": True,
        "private_staging_directory": True,
        "private_input_snapshot": True,
        "destination_directory_fd_verified": True,
    }
    if result["status"] == "materialized":
        publication.update(
            {
                "published_without_replacement": True,
                "staging_cleanup_complete": True,
            }
        )
    assert result["publication"] == publication
    resource_limits = result["resource_limits"]
    assert resource_limits["configured_max_input_box_size_pix"] == 512
    assert resource_limits["configured_max_input_voxels"] == 512**3
    assert resource_limits["input_file_size_bytes"] > 0
    assert resource_limits["max_input_file_size_bytes"] > resource_limits[
        "input_file_size_bytes"
    ]
    assert resource_limits["minimum_free_staging_bytes"] > 2 * resource_limits[
        "input_file_size_bytes"
    ]
    assert resource_limits["free_space_preflight_required"] is True
    if result["status"] == "materialized":
        assert resource_limits["available_free_staging_bytes_at_preflight"] >= (
            resource_limits["minimum_free_staging_bytes"]
        )
        assert result["input_binding_unchanged"] is True
    assert result["alignment_only"] is True
    assert result["symmetry_averaging_applied"] is False
    assert result["output_lowpass_applied"] is False
    assert result["center_of_mass_recentering_applied"] is False
    assert result["rotation_center"] == "geometric_array_center"
    assert result["particle_metadata_transformed"] is False
    assert result["relion_pipeline_job_registered"] is False
    assert result["relion_pipeline_job_started"] is False
    assert result["alignment_utility_executed"] is (result["status"] == "materialized")
    assert result["stochastic_search"] is True
    assert result["direct_scan_transform_applied"] is False
    assert result["limitations"]


def test_render_symmetry_alignment_is_generic_and_read_only(tmp_path: Path) -> None:
    project = tmp_path / "project"
    input_map = project / "Maps" / "input.mrc"
    _write_test_map(input_map)
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))
    before = _project_files(project)

    result = runner.render_symmetry_alignment(
        str(project),
        "Maps/input.mrc",
        "ReferencePrep/aligned_c4.mrc",
        "C4",
        20.0,
    )

    assert _project_files(project) == before
    assert result["input_map"] == "Maps/input.mrc"
    assert result["input"]["resolved_project_path"] == "Maps/input.mrc"
    assert result["output_map"] == "ReferencePrep/aligned_c4.mrc"
    assert result["status"] == "rendered"
    assert result["executable"] == {
        "name": "relion_align_symmetry",
        "resolved": str(tmp_path / "relion-bin" / "relion_align_symmetry"),
        "available": True,
    }
    assert "command" not in result
    _assert_alignment_contracts(result, target_symmetry="C4")
    _assert_alignment_argv(
        result["command_template"],
        executable=str(tmp_path / "relion-bin" / "relion_align_symmetry"),
        input_map="<private_input_snapshot.mrc>",
        output_map="<private_staged_output.mrc>",
        target_symmetry="C4",
    )


@pytest.mark.parametrize("target_symmetry", ["D4", "O", "I3"])
def test_render_symmetry_alignment_carries_non_c4_target_in_typed_contract(
    tmp_path: Path,
    target_symmetry: str,
) -> None:
    project = tmp_path / "project"
    _write_test_map(project / "Maps" / "input.mrc")
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))

    result = runner.render_symmetry_alignment(
        str(project),
        "Maps/input.mrc",
        f"ReferencePrep/aligned_{target_symmetry.lower()}.mrc",
        target_symmetry,
        20.0,
    )

    _assert_alignment_contracts(result, target_symmetry=target_symmetry)
    assert result["command_template"][result["command_template"].index("--sym") + 1] == (
        target_symmetry
    )


def test_align_symmetry_reference_stages_validated_output_and_parses_transform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    input_path = project / "Maps" / "input.mrc"
    output_path = project / "ReferencePrep" / "aligned_c4.mrc"
    _write_test_map(input_path)
    input_hash = _sha256(input_path)
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))
    alignment_commands: list[list[str]] = []
    staging_directories: list[Path] = []

    def fake_run(command, **kwargs):
        if "--version" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                "RELION version: 5.0.1-test\n",
                "",
            )
        alignment_commands.append(command)
        execution_cwd = Path(kwargs["cwd"])
        assert execution_cwd != project
        assert execution_cwd.parent == project / "ReferencePrep"
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["timeout"] == 900
        assert kwargs["check"] is False
        assert str(tmp_path / "relion-bin") in kwargs["env"]["PATH"].split(":")
        staged_arg = command[command.index("--o") + 1]
        snapshot_arg = command[command.index("--i") + 1]
        staged_path = _resolved_path(execution_cwd, staged_arg)
        snapshot_path = _resolved_path(execution_cwd, snapshot_arg)
        assert staged_path != output_path
        assert project in staged_path.resolve().parents
        assert staged_path.name == "aligned.mrc"
        assert snapshot_path.name == "input.mrc"
        assert snapshot_path.parent == staged_path.parent
        assert stat.S_IMODE(snapshot_path.stat().st_mode) == 0o400
        assert _sha256(snapshot_path) == input_hash
        assert staged_path.parent.parent == project / "ReferencePrep"
        assert staged_path.parent.name.startswith(".relion-align-")
        assert stat.S_IMODE(staged_path.parent.stat().st_mode) == 0o700
        staging_directories.append(staged_path.parent)
        shutil.copyfile(snapshot_path, staged_path)
        stdout = (
            "The refined solution is ROT = 12.5 TILT = 33 PSI = -4.25 "
            "DIFF2= 0.0123\n"
            f"The aligned map has been written to {staged_arg}\n"
        )
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = runner.align_symmetry_reference(
        str(project),
        "Maps/input.mrc",
        "ReferencePrep/aligned_c4.mrc",
        "C4",
        20.0,
        confirm=True,
    )

    assert len(alignment_commands) == 1
    executed = alignment_commands[0]
    staged_arg = executed[executed.index("--o") + 1]
    snapshot_arg = executed[executed.index("--i") + 1]
    assert staged_arg == "aligned.mrc"
    assert snapshot_arg == "input.mrc"
    _assert_alignment_argv(
        executed,
        executable=str(tmp_path / "relion-bin" / "relion_align_symmetry"),
        input_map=snapshot_arg,
        output_map=staged_arg,
        target_symmetry="C4",
    )
    assert result["command"] == executed
    assert result["command_template"][result["command_template"].index("--o") + 1] == (
        "<private_staged_output.mrc>"
    )
    assert result["command_template"][result["command_template"].index("--i") + 1] == (
        "<private_input_snapshot.mrc>"
    )
    assert result["input_map"] == "Maps/input.mrc"
    assert result["output_map"] == "ReferencePrep/aligned_c4.mrc"
    assert result["output_exists"] is True
    assert output_path.is_file()
    assert _sha256(input_path) == input_hash
    assert result["status"] == "materialized"
    assert result["input"]["sha256"] == input_hash
    assert result["input"]["resolved_project_path"] == "Maps/input.mrc"
    assert result["output"]["sha256"] == _sha256(output_path)
    assert result["transform"] == {
        "euler_angles_deg": {
            "rot": 12.5,
            "tilt": 33.0,
            "psi": -4.25,
        },
        "diff2": 0.0123,
        "convention": "RELION ROT/TILT/PSI as reported by relion_align_symmetry",
        "rotation_applied_to_output": True,
        "rotation_matrix_reported": False,
    }
    assert result["physical_grid_preserved"] is True
    assert result["input"]["mrc"] == result["output"]["mrc"]
    assert result["input_unchanged"] is True
    assert result["warnings"] == []
    _assert_alignment_contracts(result, target_symmetry="C4")
    assert {path.name for path in (project / "Maps").iterdir()} == {"input.mrc"}
    assert {path.name for path in (project / "ReferencePrep").iterdir()} == {
        "aligned_c4.mrc"
    }
    assert len(staging_directories) == 1
    assert result["command_working_directory"] == str(staging_directories[0])
    assert not staging_directories[0].exists()

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("an existing output must be rejected before execution")
        ),
    )
    with pytest.raises(CatalogError, match="already exists"):
        runner.align_symmetry_reference(
            str(project),
            "Maps/input.mrc",
            "ReferencePrep/aligned_c4.mrc",
            "C4",
            20.0,
            confirm=True,
        )


def test_align_symmetry_reference_private_cwd_prevents_custom_file_shadowing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    input_path = project / "Maps" / "input.mrc"
    output_path = project / "ReferencePrep" / "shadow_safe_c4.mrc"
    shadow_path = project / "C4"
    _write_test_map(input_path)
    shadow_content = "rot_axis 2 1 0 0\n"
    shadow_path.write_text(shadow_content, encoding="utf-8")
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))
    execution_directories: list[Path] = []

    def fake_run(command, **kwargs):
        if "--version" in command:
            assert kwargs["cwd"] == project
            return subprocess.CompletedProcess(command, 0, "RELION 5.0.1\n", "")
        execution_cwd = Path(kwargs["cwd"])
        execution_directories.append(execution_cwd)
        assert execution_cwd != project
        assert not (execution_cwd / "C4").exists()
        assert shadow_path.is_file()
        assert command[command.index("--sym") + 1] == "C4"
        assert command[command.index("--i") + 1] == "input.mrc"
        assert command[command.index("--o") + 1] == "aligned.mrc"
        shutil.copyfile(execution_cwd / "input.mrc", execution_cwd / "aligned.mrc")
        return subprocess.CompletedProcess(
            command,
            0,
            "The refined solution is ROT = 1 TILT = 2 PSI = 3 DIFF2= 0.5\n",
            "",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = runner.align_symmetry_reference(
        str(project),
        "Maps/input.mrc",
        "ReferencePrep/shadow_safe_c4.mrc",
        "C4",
        20.0,
        confirm=True,
    )

    assert result["target_symmetry"] == "C4"
    assert result["command_working_directory"] == str(execution_directories[0])
    assert not execution_directories[0].exists()
    assert output_path.is_file()
    assert _sha256(output_path) == _sha256(input_path)
    assert shadow_path.read_text(encoding="utf-8") == shadow_content


@pytest.mark.parametrize(
    ("input_map", "output_map", "target_symmetry", "cutoff", "message"),
    [
        ("Maps/input.mrc", "Maps/aligned.mrc", "C4", 20.0, "dedicated ReferencePrep"),
        ("Maps/input.mrc", "../aligned.mrc", "C4", 20.0, "project-relative"),
        ("Maps/input.mrc", "ReferencePrep/aligned.mrc", "C4", 0.0, "at least"),
        (
            "Maps/input.mrc",
            "ReferencePrep/aligned.mrc",
            "C4",
            48.0,
            "Fourier-radius",
        ),
        ("Maps/missing.mrc", "ReferencePrep/aligned.mrc", "C4", 20.0, "unavailable"),
        ("Maps/input.mrc", "ReferencePrep/aligned.mrc", "C1", 20.0, "alignment symmetry"),
        ("Maps/input.mrc", "ReferencePrep/aligned.mrc", "C65", 20.0, "bounded limit"),
        ("Maps/input.mrc", "ReferencePrep/aligned.mrc", "../../C4", 20.0, "alignment symmetry"),
    ],
)
def test_render_symmetry_alignment_rejects_invalid_requests_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    input_map: str,
    output_map: str,
    target_symmetry: str,
    cutoff: float,
    message: str,
) -> None:
    project = tmp_path / "project"
    _write_test_map(project / "Maps" / "input.mrc")
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("invalid alignment requests must not execute")
        ),
    )

    with pytest.raises((CatalogError, RelionRuntimeError), match=message):
        runner.render_symmetry_alignment(
            str(project),
            input_map,
            output_map,
            target_symmetry,
            cutoff,
        )


def test_render_symmetry_alignment_enforces_configured_input_box_limit(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    _write_test_map(project / "Maps" / "input.mrc", size=64)
    runner = RelionRunner(
        Catalog(),
        _run_settings(tmp_path, max_box_size_px=63),
    )

    with pytest.raises(CatalogError, match="configured symmetry-alignment limit"):
        runner.render_symmetry_alignment(
            str(project),
            "Maps/input.mrc",
            "ReferencePrep/aligned.mrc",
            "C4",
            20.0,
        )


def test_align_symmetry_reference_checks_staging_free_space_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    _write_test_map(project / "Maps" / "input.mrc")
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))
    monkeypatch.setattr(shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("insufficient disk space must be rejected before execution")
        ),
    )

    with pytest.raises(RelionRuntimeError, match="Insufficient free space"):
        runner.align_symmetry_reference(
            str(project),
            "Maps/input.mrc",
            "ReferencePrep/aligned.mrc",
            "C4",
            20.0,
            confirm=True,
        )

    assert list((project / "ReferencePrep").iterdir()) == []


def test_render_symmetry_alignment_confines_outputs_to_real_referenceprep_namespace(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    _write_test_map(project / "Maps" / "input.mrc")
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))

    with pytest.raises(CatalogError, match="dedicated ReferencePrep"):
        runner.render_symmetry_alignment(
            str(project),
            "Maps/input.mrc",
            "Refine3D/job001/aligned_c4.mrc",
            "C4",
            20.0,
        )

    outside = tmp_path / "outside-reference-prep"
    outside.mkdir()
    (project / "ReferencePrep").symlink_to(outside, target_is_directory=True)
    with pytest.raises(CatalogError, match="outside project_dir|real project directory"):
        runner.render_symmetry_alignment(
            str(project),
            "Maps/input.mrc",
            "ReferencePrep/aligned_c4.mrc",
            "C4",
            20.0,
        )

    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    ("failure_mode", "message"),
    [
        ("anisotropic_sampling", "isotropic voxel spacing"),
        ("noncanonical_axes", "canonical MRC axis mapping"),
        ("nonzero_origin", "zero MRC starts and origin"),
        ("nonfinite_density", "non-finite density"),
    ],
)
def test_render_symmetry_alignment_rejects_unsafe_mrc_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
    message: str,
) -> None:
    project = tmp_path / "project"
    input_path = project / "Maps" / "input.mrc"
    _write_test_map(input_path)
    with mrcfile.open(input_path, mode="r+") as handle:
        if failure_mode == "anisotropic_sampling":
            handle.voxel_size = (1.5, 1.6, 1.5)
        elif failure_mode == "noncanonical_axes":
            handle.header.mapc = 2
            handle.header.mapr = 1
        elif failure_mode == "nonzero_origin":
            handle.header.origin.x = 1.5
        elif failure_mode == "nonfinite_density":
            handle.data[0, 0, 0] = np.nan
        handle.flush()
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("unsafe MRC input must be rejected before execution")
        ),
    )

    with pytest.raises(CatalogError, match=message):
        runner.render_symmetry_alignment(
            str(project),
            "Maps/input.mrc",
            "ReferencePrep/aligned_c4.mrc",
            "C4",
            20.0,
        )


def test_align_symmetry_reference_cleans_staging_after_process_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    input_path = project / "Maps" / "input.mrc"
    output_path = project / "ReferencePrep" / "aligned_c4.mrc"
    _write_test_map(input_path)
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))
    staging_directories: list[Path] = []

    def fake_run(command, **kwargs):
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, "RELION 5.0.1\n", "")
        staged_path = _resolved_path(
            kwargs["cwd"],
            command[command.index("--o") + 1],
        )
        assert staged_path.parent.name.startswith(".relion-align-")
        assert stat.S_IMODE(staged_path.parent.stat().st_mode) == 0o700
        staging_directories.append(staged_path.parent)
        staged_path.write_bytes(b"partial output")
        return subprocess.CompletedProcess(command, 3, "", "alignment failed")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RelionRuntimeError, match="exit code 3"):
        runner.align_symmetry_reference(
            str(project),
            "Maps/input.mrc",
            "ReferencePrep/aligned_c4.mrc",
            "C4",
            20.0,
            confirm=True,
        )

    assert not output_path.exists()
    assert {path.name for path in (project / "Maps").iterdir()} == {"input.mrc"}
    assert list((project / "ReferencePrep").iterdir()) == []
    assert len(staging_directories) == 1
    assert not staging_directories[0].exists()


@pytest.mark.parametrize(
    "failure_mode",
    ["missing_output", "invalid_mrc", "grid_mismatch", "missing_transform"],
)
def test_align_symmetry_reference_rejects_invalid_staged_results_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    project = tmp_path / "project"
    input_path = project / "Maps" / "input.mrc"
    output_path = project / "ReferencePrep" / "aligned_c4.mrc"
    _write_test_map(input_path)
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))

    def fake_run(command, **kwargs):
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, "RELION 5.0.1\n", "")
        staged_path = _resolved_path(
            kwargs["cwd"],
            command[command.index("--o") + 1],
        )
        if failure_mode == "invalid_mrc":
            staged_path.write_bytes(b"not an MRC")
        elif failure_mode == "grid_mismatch":
            _write_test_map(staged_path, size=32)
        elif failure_mode != "missing_output":
            shutil.copyfile(input_path, staged_path)
        stdout = (
            "done\n"
            if failure_mode == "missing_transform"
            else "The refined solution is ROT = 1 TILT = 2 PSI = 3 DIFF2= 0.5\n"
        )
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises((CatalogError, RelionRuntimeError)):
        runner.align_symmetry_reference(
            str(project),
            "Maps/input.mrc",
            "ReferencePrep/aligned_c4.mrc",
            "C4",
            20.0,
            confirm=True,
        )

    assert not output_path.exists()
    assert {path.name for path in (project / "Maps").iterdir()} == {"input.mrc"}
    assert list((project / "ReferencePrep").iterdir()) == []


def test_align_symmetry_reference_refuses_publish_when_input_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    input_path = project / "Maps" / "input.mrc"
    output_path = project / "ReferencePrep" / "aligned_c4.mrc"
    _write_test_map(input_path)
    input_hash = _sha256(input_path)
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))
    staging_directories: list[Path] = []

    def fake_run(command, **kwargs):
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, "RELION 5.0.1\n", "")
        staged_path = _resolved_path(
            kwargs["cwd"],
            command[command.index("--o") + 1],
        )
        staging_directories.append(staged_path.parent)
        shutil.copyfile(input_path, staged_path)
        with mrcfile.open(input_path, mode="r+") as handle:
            handle.data[0, 0, 0] += 1.0
            handle.flush()
        return subprocess.CompletedProcess(
            command,
            0,
            "The refined solution is ROT = 1 TILT = 2 PSI = 3 DIFF2= 0.5\n",
            "",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RelionRuntimeError, match="input_map changed"):
        runner.align_symmetry_reference(
            str(project),
            "Maps/input.mrc",
            "ReferencePrep/aligned_c4.mrc",
            "C4",
            20.0,
            confirm=True,
        )

    assert _sha256(input_path) != input_hash
    assert not output_path.exists()
    assert list((project / "ReferencePrep").iterdir()) == []
    assert len(staging_directories) == 1
    assert not staging_directories[0].exists()


def test_align_symmetry_reference_refuses_publish_if_input_symlink_is_retargeted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    source_path = project / "Maps" / "source.mrc"
    input_link = project / "Maps" / "current.mrc"
    outside_path = tmp_path / "outside-project.mrc"
    output_path = project / "ReferencePrep" / "aligned_c4.mrc"
    _write_test_map(source_path)
    _write_test_map(outside_path)
    with mrcfile.open(outside_path, mode="r+") as handle:
        handle.data[0, 0, 0] += 3.0
        handle.flush()
    input_link.symlink_to(source_path)
    source_hash = _sha256(source_path)
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))
    staging_directories: list[Path] = []

    def fake_run(command, **kwargs):
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, "RELION 5.0.1\n", "")
        snapshot_path = _resolved_path(
            kwargs["cwd"],
            command[command.index("--i") + 1],
        )
        staged_path = _resolved_path(
            kwargs["cwd"],
            command[command.index("--o") + 1],
        )
        staging_directories.append(staged_path.parent)
        assert _sha256(snapshot_path) == source_hash
        input_link.unlink()
        input_link.symlink_to(outside_path)
        shutil.copyfile(snapshot_path, staged_path)
        return subprocess.CompletedProcess(
            command,
            0,
            "The refined solution is ROT = 1 TILT = 2 PSI = 3 DIFF2= 0.5\n",
            "",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RelionRuntimeError, match="path binding changed"):
        runner.align_symmetry_reference(
            str(project),
            "Maps/current.mrc",
            "ReferencePrep/aligned_c4.mrc",
            "C4",
            20.0,
            confirm=True,
        )

    assert not output_path.exists()
    assert input_link.resolve() == outside_path
    assert list((project / "ReferencePrep").iterdir()) == []
    assert len(staging_directories) == 1
    assert not staging_directories[0].exists()


def test_align_symmetry_reference_preserves_destination_that_appears_during_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    input_path = project / "Maps" / "input.mrc"
    output_path = project / "ReferencePrep" / "aligned_c4.mrc"
    _write_test_map(input_path)
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))
    raced_destination = b"created by a concurrent writer"
    staging_directories: list[Path] = []

    def fake_run(command, **kwargs):
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, "RELION 5.0.1\n", "")
        staged_path = _resolved_path(
            kwargs["cwd"],
            command[command.index("--o") + 1],
        )
        staging_directories.append(staged_path.parent)
        shutil.copyfile(input_path, staged_path)
        output_path.write_bytes(raced_destination)
        return subprocess.CompletedProcess(
            command,
            0,
            "The refined solution is ROT = 1 TILT = 2 PSI = 3 DIFF2= 0.5\n",
            "",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(CatalogError, match="appeared while alignment was running"):
        runner.align_symmetry_reference(
            str(project),
            "Maps/input.mrc",
            "ReferencePrep/aligned_c4.mrc",
            "C4",
            20.0,
            confirm=True,
        )

    assert output_path.read_bytes() == raced_destination
    assert {path.name for path in (project / "ReferencePrep").iterdir()} == {
        "aligned_c4.mrc"
    }
    assert len(staging_directories) == 1
    assert not staging_directories[0].exists()


def test_symmetry_alignment_server_tools_forward_the_bounded_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, tuple[object, ...]]] = []

    def fake_render(*args):
        observed.append(("render", args))
        return {"operation": "align_symmetry_reference"}

    def fake_align(*args):
        observed.append(("align", args))
        return {"operation": "align_symmetry_reference", "output_exists": True}

    monkeypatch.setattr(server.RUNNER, "render_symmetry_alignment", fake_render)
    monkeypatch.setattr(server.RUNNER, "align_symmetry_reference", fake_align)

    rendered = server.relion_render_symmetry_alignment(
        "/approved/project",
        "Maps/input.mrc",
        "ReferencePrep/aligned.mrc",
        "D4",
        20.0,
    )
    aligned = server.relion_align_symmetry_reference(
        "/approved/project",
        "Maps/input.mrc",
        "ReferencePrep/aligned.mrc",
        "D4",
        20.0,
        confirm=True,
    )

    assert rendered["operation"] == "align_symmetry_reference"
    assert aligned["output_exists"] is True
    assert observed == [
        (
            "render",
            (
                "/approved/project",
                "Maps/input.mrc",
                "ReferencePrep/aligned.mrc",
                "D4",
                20.0,
            ),
        ),
        (
            "align",
            (
                "/approved/project",
                "Maps/input.mrc",
                "ReferencePrep/aligned.mrc",
                "D4",
                20.0,
                True,
            ),
        ),
    ]
