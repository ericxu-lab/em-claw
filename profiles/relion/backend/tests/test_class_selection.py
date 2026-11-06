import re
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from relion_mcp.catalog import Catalog, CatalogError
from relion_mcp.runner import (
    PartialPipelineAddError,
    RelionRunner,
    RelionRuntimeError,
    SegmentStartError,
)
from relion_mcp.settings import ConfigurationError, Settings

PARTICLE_ROWS = (
    ("000001@particles.mrcs", 1, "1"),
    ("000002@particles.mrcs", 1, "1"),
    ("000003@particles.mrcs", 2, "1"),
    ("000004@particles.mrcs", 3, "1"),
    ("000005@particles.mrcs", 4, "1"),
    ("000006@particles.mrcs", 5, "1"),
    ("000007@particles.mrcs", 6, "1"),
    ("000008@particles.mrcs", 7, "1"),
    ("000009@particles.mrcs", 8, "1"),
)


def write_particles(
    path: Path,
    rows: tuple[tuple[str, int, str], ...] = PARTICLE_ROWS,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    particles = "\n".join(
        f"{image_name} {class_number} {optics_group}"
        for image_name, class_number, optics_group in rows
    )
    path.write_text(
        f"""data_optics

loop_
_rlnOpticsGroupName #1
_rlnOpticsGroup #2
opticsGroup1 1

data_particles

loop_
_rlnImageName #1
_rlnClassNumber #2
_rlnOpticsGroup #3
{particles}

""",
        encoding="utf-8",
    )


def make_runner(tmp_path: Path) -> tuple[RelionRunner, Path, Path]:
    relion_bin = tmp_path / "relion-bin"
    relion_bin.mkdir()
    for name in ("relion_pipeliner", "relion_image_handler"):
        executable = relion_bin / name
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)

    project = tmp_path / "project"
    project.mkdir()
    particles = project / "Class3D" / "job001" / "run_data.star"
    write_particles(particles)
    settings = Settings(
        mode="run",
        project_roots=(tmp_path.resolve(),),
        pipeliner_executable=str(relion_bin / "relion_pipeliner"),
        image_handler_executable=str(relion_bin / "relion_image_handler"),
        pipeline_name="default",
        add_job_timeout_seconds=90,
        utility_timeout_seconds=900,
        class_selector_executable=str(relion_bin / "missing-class-selector"),
    )
    return RelionRunner(Catalog(), settings), project, particles


def enable_gui_equivalent_selector(runner: RelionRunner, tmp_path: Path) -> Path:
    selector = tmp_path / "relion-bin" / "relion-mcp-class-select"
    selector.write_text("#!/bin/sh\n", encoding="utf-8")
    selector.chmod(0o755)
    runner.settings = replace(runner.settings, class_selector_executable=str(selector))
    return selector


def project_files(project: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(project)): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
    }


def install_fake_add_job(
    monkeypatch,
    runner: RelionRunner,
    *,
    fail_on_call: int | None = None,
) -> list[dict]:
    calls: list[dict] = []

    def fake_add_job(
        project_dir: str,
        rendered,
        *,
        pipeline_name: str,
        alias: str | None,
        confirm: bool,
    ) -> dict:
        calls.append(
            {
                "project_dir": project_dir,
                "rendered": rendered,
                "pipeline_name": pipeline_name,
                "alias": alias,
                "confirm": confirm,
            }
        )
        if fail_on_call == len(calls):
            raise RelionRuntimeError("synthetic add failure")
        job = f"{rendered.relion_job_type}/job{len(calls):03d}/"
        return {"job": job, "command": ["relion_pipeliner", "--addJob"]}

    monkeypatch.setattr(runner, "add_job", fake_add_job)
    return calls


def all_plan_aliases(plan: dict) -> list[str]:
    return [
        *(stage["alias"] for stage in plan["select_stages"]),
        *(stage["alias"] for stage in plan["join_stages"]),
    ]


def test_non_contiguous_plan_is_read_only_and_uses_native_metadata_selection(
    tmp_path: Path, monkeypatch
):
    runner, project, _ = make_runner(tmp_path)
    before = project_files(project)
    monkeypatch.setattr(
        runner,
        "add_job",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a read-only plan must not add jobs")
        ),
    )

    plan = runner.plan_particle_class_selection(
        str(project),
        "Class3D/job001/run_data.star",
        [5, 1, 3],
        "accepted_135",
        "default",
    )

    assert project_files(project) == before
    assert plan["execution_model"] == "relion_pipeline_select_then_joinstar"
    assert plan["materialization_mode"] == "select_joinstar"
    assert plan["materialization_fallback_reason"] == (
        "configured_gui_equivalent_selector_not_found"
    )
    assert plan["selected_class_numbers"] == [1, 3, 5]
    assert plan["selected_class_counts"] == {1: 2, 3: 1, 5: 1}
    assert plan["expected_particle_count"] == 4
    assert plan["planned_job_count"] == 4
    assert plan["start_order_stage_ids"] == [
        "select_class_001",
        "select_class_003",
        "select_class_005",
        "join_level_01_01",
    ]
    assert plan["pipeline_registered"] is False
    assert plan["started"] is False
    assert plan["next_write_tool"] == "relion_run_particle_class_selection"
    assert plan["advanced_add_only_tool"] == "relion_add_particle_class_selection_jobs"

    for stage, class_number in zip(plan["select_stages"], (1, 3, 5), strict=True):
        assert stage["job_type"] == "Select"
        assert stage["options_by_manifest_key"] == {
            "fn_data": "Class3D/job001/run_data.star",
            "do_select_values": "Yes",
            "select_label": "rlnClassNumber",
            "select_minval": str(class_number),
            "select_maxval": str(class_number),
        }
        assert stage["command"][3:5] == ["--addJob", "Select"]

    join = plan["join_stages"][0]
    assert join["job_type"] == "JoinStar"
    assert join["inputs"] == [
        "Select/PENDING_class001/particles.star",
        "Select/PENDING_class003/particles.star",
        "Select/PENDING_class005/particles.star",
    ]
    assert join["options_by_manifest_key"] == {
        "fn_part1": join["inputs"][0],
        "fn_part2": join["inputs"][1],
        "fn_part3": join["inputs"][2],
        "do_part": "Yes",
    }


def test_gui_equivalent_plan_uses_one_pipeline_registered_external_job(tmp_path: Path):
    runner, project, _ = make_runner(tmp_path)
    selector = enable_gui_equivalent_selector(runner, tmp_path)

    plan = runner.plan_particle_class_selection(
        str(project),
        "Class3D/job001/run_data.star",
        [5, 1, 3],
        "accepted_135",
        "default",
    )

    assert plan["execution_model"] == "relion_external_gui_equivalent"
    assert plan["materialization_mode"] == "gui_equivalent"
    assert plan["planned_job_count"] == 1
    assert plan["join_stages"] == []
    assert plan["expected_particle_count"] == 4
    stage = plan["select_stages"][0]
    assert stage["job_type"] == "External"
    assert stage["expected_output"] == "External/PENDING/particles.star"
    assert stage["options_by_manifest_key"] == {
        "fn_exe": str(selector),
        "in_part": "Class3D/job001/run_data.star",
        "param1_label": "classes",
        "param1_value": "1,3,5",
        "param2_label": "expected-particles",
        "param2_value": "4",
    }
    assert stage["command"][3:5] == ["--addJob", "External"]


def test_explicit_gui_equivalent_mode_requires_configured_selector(tmp_path: Path):
    runner, project, _ = make_runner(tmp_path)

    with pytest.raises(CatalogError, match="selector executable was not found"):
        runner.plan_particle_class_selection(
            str(project),
            "Class3D/job001/run_data.star",
            [1, 3],
            "accepted_13",
            "default",
            materialization_mode="gui_equivalent",
        )


def test_gui_equivalent_external_arguments_reject_shell_metacharacters(tmp_path: Path):
    runner, project, particles = make_runner(tmp_path)
    enable_gui_equivalent_selector(runner, tmp_path)
    unsafe = particles.with_name("run_data$(id).star")
    write_particles(unsafe)

    with pytest.raises(CatalogError, match="arguments may contain only"):
        runner.plan_particle_class_selection(
            str(project),
            "Class3D/job001/run_data$(id).star",
            [1, 3],
            "accepted_13",
            "default",
        )


def test_gui_equivalent_plan_resolves_optimiser_to_class_model(
    tmp_path: Path,
    monkeypatch,
):
    runner, project, _ = make_runner(tmp_path)
    enable_gui_equivalent_selector(runner, tmp_path)
    class_dir = project / "Class3D" / "job001"
    (class_dir / "run_model.star").write_text(
        """data_model_general

_rlnReferenceDimensionality 2

data_model_classes

loop_
_rlnReferenceImage #1
_rlnClassDistribution #2
000001@Class3D/job001/run_classes.mrcs 0.4
000003@Class3D/job001/run_classes.mrcs 0.3
000005@Class3D/job001/run_classes.mrcs 0.3

""",
        encoding="utf-8",
    )
    (class_dir / "run_optimiser.star").write_text(
        """data_optimiser_general

_rlnModelStarFile Class3D/job001/run_model.star
_rlnExperimentalDataStarFile Class3D/job001/run_data.star

""",
        encoding="utf-8",
    )

    plan = runner.plan_particle_class_selection(
        str(project),
        "Class3D/job001/run_data.star",
        [5, 1, 3],
        "accepted_135",
        "default",
        class_source_star="Class3D/job001/run_optimiser.star",
    )

    assert plan["class_source_star"] == "Class3D/job001/run_optimiser.star"
    assert plan["class_model_star"] == "Class3D/job001/run_model.star"
    assert plan["select_stages"][0]["options_by_manifest_key"]["param3_value"] == (
        "Class3D/job001/run_model.star"
    )
    assert "External/PENDING/class_averages.star" in plan["expected_auxiliary_outputs"]

    install_fake_add_job(monkeypatch, runner)
    added = runner.add_particle_class_selection_jobs(
        str(project),
        "Class3D/job001/run_data.star",
        [5, 1, 3],
        "accepted_135",
        "default",
        confirm=True,
        class_source_star="Class3D/job001/run_optimiser.star",
    )

    assert "External/job001/class_averages.star" in added["auxiliary_outputs"]


def test_gui_equivalent_3d_model_does_not_promise_class_averages(
    tmp_path: Path,
    monkeypatch,
):
    runner, project, _ = make_runner(tmp_path)
    enable_gui_equivalent_selector(runner, tmp_path)
    model_path = project / "Class3D" / "job001" / "run_model.star"
    model_path.write_text(
        """data_model_general

_rlnReferenceDimensionality 3

data_model_classes

loop_
_rlnReferenceImage #1
_rlnClassDistribution #2
Class3D/job001/run_class001.mrc 0.2
Class3D/job001/run_class002.mrc 0.2
Class3D/job001/run_class003.mrc 0.2
Class3D/job001/run_class004.mrc 0.2
Class3D/job001/run_class005.mrc 0.2

""",
        encoding="utf-8",
    )

    plan = runner.plan_particle_class_selection(
        str(project),
        "Class3D/job001/run_data.star",
        [1, 3, 5],
        "accepted_135",
        "default",
        class_source_star="Class3D/job001/run_model.star",
    )

    assert plan["select_stages"][0]["options_by_manifest_key"]["param3_value"] == (
        "Class3D/job001/run_model.star"
    )
    assert "External/PENDING/class_averages.star" not in plan["expected_auxiliary_outputs"]

    install_fake_add_job(monkeypatch, runner)
    added = runner.add_particle_class_selection_jobs(
        str(project),
        "Class3D/job001/run_data.star",
        [1, 3, 5],
        "accepted_135",
        "default",
        confirm=True,
        class_source_star="Class3D/job001/run_model.star",
    )

    assert "External/job001/class_averages.star" not in added["auxiliary_outputs"]


def test_gui_equivalent_add_registers_exactly_one_external_job(
    tmp_path: Path, monkeypatch
):
    runner, project, _ = make_runner(tmp_path)
    enable_gui_equivalent_selector(runner, tmp_path)
    calls = install_fake_add_job(monkeypatch, runner)

    added = runner.add_particle_class_selection_jobs(
        str(project),
        "Class3D/job001/run_data.star",
        [5, 1, 3],
        "accepted_135",
        "default",
        confirm=True,
    )

    assert len(calls) == 1
    assert calls[0]["rendered"].relion_job_type == "External"
    assert added["jobs_to_start"] == ["External/job001/"]
    assert added["particles_star"] == "External/job001/particles.star"
    assert added["join_jobs"] == []
    assert added["pipeline_registered"] is True


def test_adds_then_starts_jobs_in_dependency_order(tmp_path: Path, monkeypatch):
    runner, project, _ = make_runner(tmp_path)
    add_calls = install_fake_add_job(monkeypatch, runner)
    popen_calls: list[tuple[list[str], dict]] = []

    def fake_popen(command: list[str], **kwargs):
        popen_calls.append((command, kwargs))
        return SimpleNamespace(pid=4321)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    added = runner.add_particle_class_selection_jobs(
        str(project),
        "Class3D/job001/run_data.star",
        [5, 1, 3],
        "accepted_135",
        "default",
        confirm=True,
    )

    expected_jobs = [
        "Select/job001/",
        "Select/job002/",
        "Select/job003/",
        "JoinStar/job004/",
    ]
    assert [call["rendered"].relion_job_type for call in add_calls] == [
        "Select",
        "Select",
        "Select",
        "JoinStar",
    ]
    assert [call["confirm"] for call in add_calls] == [True] * 4
    join_options = add_calls[-1]["rendered"].options_by_key
    assert [join_options[f"fn_part{index}"] for index in range(1, 4)] == [
        "Select/job001/particles.star",
        "Select/job002/particles.star",
        "Select/job003/particles.star",
    ]
    assert added["jobs_to_start"] == expected_jobs
    assert added["particles_star"] == "JoinStar/job004/join_particles.star"
    assert added["pipeline_registered"] is True
    assert added["started"] is False
    assert popen_calls == []

    started = runner.start_jobs(
        str(project),
        added["jobs_to_start"],
        added["suggested_schedule_name"],
        "default",
        confirm=True,
    )

    assert started["jobs"] == expected_jobs
    assert len(popen_calls) == 1
    command, kwargs = popen_calls[0]
    assert command[command.index("--RunJobs") + 1] == " ".join(expected_jobs) + " "
    assert kwargs["cwd"] == project
    assert kwargs["start_new_session"] is True


def test_run_class_selection_registers_and_starts_graph_in_one_call(
    tmp_path: Path, monkeypatch
):
    runner, project, _ = make_runner(tmp_path)
    add_calls = install_fake_add_job(monkeypatch, runner)
    popen_calls: list[tuple[list[str], dict]] = []

    def fake_popen(command: list[str], **kwargs):
        popen_calls.append((command, kwargs))
        return SimpleNamespace(pid=4321)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    result = runner.run_particle_class_selection(
        str(project),
        "Class3D/job001/run_data.star",
        [5, 1, 3],
        "accepted_135",
        "default",
        schedule_name=None,
        confirm=True,
    )

    expected_jobs = [
        "Select/job001/",
        "Select/job002/",
        "Select/job003/",
        "JoinStar/job004/",
    ]
    assert len(add_calls) == 4
    assert result["jobs_to_start"] == expected_jobs
    assert result["schedule_name"] == "accepted_135__run"
    assert result["pipeline_registered"] is True
    assert result["started"] is True
    assert result["next_write_tool"] is None
    assert result["particles_star"] == "JoinStar/job004/join_particles.star"
    assert len(popen_calls) == 1
    assert popen_calls[0][0][popen_calls[0][0].index("--RunJobs") + 1] == (
        " ".join(expected_jobs) + " "
    )


def test_run_class_selection_reports_registered_graph_when_start_fails(
    tmp_path: Path, monkeypatch
):
    runner, project, _ = make_runner(tmp_path)
    install_fake_add_job(monkeypatch, runner)
    monkeypatch.setattr(
        runner,
        "start_jobs",
        lambda *args, **kwargs: (_ for _ in ()).throw(RelionRuntimeError("launch failed")),
    )

    with pytest.raises(SegmentStartError, match="must not be added again") as error:
        runner.run_particle_class_selection(
            str(project),
            "Class3D/job001/run_data.star",
            [1, 3],
            "accepted_13",
            "default",
            schedule_name="accepted_13_run",
            confirm=True,
        )

    assert error.value.added_jobs == [
        "Select/job001/",
        "Select/job002/",
        "JoinStar/job003/",
    ]
    assert error.value.schedule_name == "accepted_13_run"


def test_single_class_needs_only_one_select_job(tmp_path: Path, monkeypatch):
    runner, project, _ = make_runner(tmp_path)
    plan = runner.plan_particle_class_selection(
        str(project),
        "Class3D/job001/run_data.star",
        [3],
        "only_class3",
        "default",
    )
    calls = install_fake_add_job(monkeypatch, runner)

    added = runner.add_particle_class_selection_jobs(
        str(project),
        "Class3D/job001/run_data.star",
        [3],
        "only_class3",
        "default",
        confirm=True,
    )

    assert plan["planned_job_count"] == 1
    assert plan["join_stages"] == []
    assert plan["expected_final_output"] == "Select/PENDING_class003/particles.star"
    assert [call["rendered"].relion_job_type for call in calls] == ["Select"]
    assert added["join_jobs"] == []
    assert added["jobs_to_start"] == ["Select/job001/"]
    assert added["final_stage_id"] == "select_class_003"
    assert added["particles_star"] == "Select/job001/particles.star"


def test_eight_classes_use_balanced_joinstar_tree_with_at_most_four_inputs(
    tmp_path: Path, monkeypatch
):
    runner, project, _ = make_runner(tmp_path)
    selected = list(range(1, 9))
    plan = runner.plan_particle_class_selection(
        str(project),
        "Class3D/job001/run_data.star",
        selected,
        "all_eight",
        "default",
    )

    assert plan["planned_job_count"] == 11
    assert [stage["stage_id"] for stage in plan["join_stages"]] == [
        "join_level_01_01",
        "join_level_01_02",
        "join_level_02_01",
    ]
    assert [len(stage["inputs"]) for stage in plan["join_stages"]] == [4, 4, 2]
    assert all(2 <= len(stage["inputs"]) <= 4 for stage in plan["join_stages"])

    calls = install_fake_add_job(monkeypatch, runner)
    added = runner.add_particle_class_selection_jobs(
        str(project),
        "Class3D/job001/run_data.star",
        selected,
        "all_eight",
        "default",
        confirm=True,
    )

    assert [call["rendered"].relion_job_type for call in calls] == [
        *("Select" for _ in range(8)),
        "JoinStar",
        "JoinStar",
        "JoinStar",
    ]
    assert added["jobs_to_start"] == [
        *(f"Select/job{index:03d}/" for index in range(1, 9)),
        "JoinStar/job009/",
        "JoinStar/job010/",
        "JoinStar/job011/",
    ]
    assert [job["inputs"] for job in added["join_jobs"]] == [
        [f"Select/job{index:03d}/particles.star" for index in range(1, 5)],
        [f"Select/job{index:03d}/particles.star" for index in range(5, 9)],
        [
            "JoinStar/job009/join_particles.star",
            "JoinStar/job010/join_particles.star",
        ],
    ]
    assert added["particles_star"] == "JoinStar/job011/join_particles.star"


@pytest.mark.parametrize(
    "selected",
    [[], [0], [-1], [True], [1.5], [1, 1]],
)
def test_invalid_class_lists_are_rejected(tmp_path: Path, selected):
    runner, project, _ = make_runner(tmp_path)

    with pytest.raises(CatalogError):
        runner.plan_particle_class_selection(
            str(project),
            "Class3D/job001/run_data.star",
            selected,
            "invalid",
            "default",
        )


def test_absent_class_is_rejected_with_available_classes(tmp_path: Path):
    runner, project, _ = make_runner(tmp_path)

    with pytest.raises(CatalogError, match=r"absent.*\[9\].*Available classes"):
        runner.plan_particle_class_selection(
            str(project),
            "Class3D/job001/run_data.star",
            [1, 9],
            "missing",
            "default",
        )


def test_duplicate_particle_identity_is_rejected(tmp_path: Path):
    runner, project, particles = make_runner(tmp_path)
    write_particles(
        particles,
        (
            ("duplicate@particles.mrcs", 1, "1"),
            ("duplicate@particles.mrcs", 3, "1"),
        ),
    )

    with pytest.raises(CatalogError, match="duplicate rlnImageName"):
        runner.plan_particle_class_selection(
            str(project),
            "Class3D/job001/run_data.star",
            [1, 3],
            "duplicates",
            "default",
        )


def test_input_symlink_cannot_escape_project(tmp_path: Path):
    runner, project, _ = make_runner(tmp_path)
    outside = tmp_path / "outside.star"
    write_particles(outside)
    escaped = project / "Class3D" / "job002" / "run_data.star"
    escaped.parent.mkdir(parents=True)
    escaped.symlink_to(outside)

    with pytest.raises(CatalogError, match="Input particle STAR is unavailable"):
        runner.plan_particle_class_selection(
            str(project),
            "Class3D/job002/run_data.star",
            [1],
            "escaped",
            "default",
        )


def test_confirm_false_blocks_before_any_job_is_added(tmp_path: Path, monkeypatch):
    runner, project, _ = make_runner(tmp_path)
    calls = install_fake_add_job(monkeypatch, runner)

    with pytest.raises(ConfigurationError, match="confirm=true"):
        runner.add_particle_class_selection_jobs(
            str(project),
            "Class3D/job001/run_data.star",
            [1, 3],
            "not_confirmed",
            "default",
            confirm=False,
        )

    assert calls == []


def test_invalid_schedule_is_rejected_before_class_selection_registration(
    tmp_path: Path,
    monkeypatch,
):
    runner, project, _ = make_runner(tmp_path)
    calls = []

    def fake_add(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("invalid schedule must be rejected before registration")

    monkeypatch.setattr(runner, "add_particle_class_selection_jobs", fake_add)

    with pytest.raises(CatalogError, match="schedule_name"):
        runner.run_particle_class_selection(
            str(project),
            "Class3D/job001/run_data.star",
            [1, 3],
            "accepted_13",
            "default",
            schedule_name="",
            confirm=True,
        )

    assert calls == []


def test_partial_add_error_reports_preserved_jobs_and_failed_stage(
    tmp_path: Path, monkeypatch
):
    runner, project, _ = make_runner(tmp_path)
    calls = install_fake_add_job(monkeypatch, runner, fail_on_call=4)
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("adding a branch must not start it")
        ),
    )

    with pytest.raises(PartialPipelineAddError) as error:
        runner.add_particle_class_selection_jobs(
            str(project),
            "Class3D/job001/run_data.star",
            [1, 3, 5],
            "partial",
            "default",
            confirm=True,
        )

    assert [call["rendered"].relion_job_type for call in calls] == [
        "Select",
        "Select",
        "Select",
        "JoinStar",
    ]
    assert error.value.added_jobs == [
        "Select/job001/",
        "Select/job002/",
        "Select/job003/",
    ]
    assert error.value.failed_stage == "join_level_01_01"
    assert "No jobs were started" in str(error.value)
    assert "retry with a new base alias" in str(error.value)


def test_unexpected_added_job_type_is_reported_as_partial_side_effect(
    tmp_path: Path, monkeypatch
):
    runner, project, _ = make_runner(tmp_path)
    monkeypatch.setattr(
        runner,
        "add_job",
        lambda *args, **kwargs: {"job": "Import/job001/"},
    )

    with pytest.raises(PartialPipelineAddError) as error:
        runner.add_particle_class_selection_jobs(
            str(project),
            "Class3D/job001/run_data.star",
            [1],
            "wrong_native_type",
            "default",
            confirm=True,
        )

    assert error.value.added_jobs == ["Import/job001/"]
    assert error.value.failed_stage == "select_class_001"
    assert "expected Select job" in str(error.value)


def test_long_base_aliases_produce_unique_safe_derived_aliases(tmp_path: Path):
    runner, project, _ = make_runner(tmp_path)
    base_a = "a" * 79 + "x"
    base_b = "a" * 79 + "y"
    plan_a = runner.plan_particle_class_selection(
        str(project),
        "Class3D/job001/run_data.star",
        list(range(1, 9)),
        base_a,
        "default",
    )
    plan_b = runner.plan_particle_class_selection(
        str(project),
        "Class3D/job001/run_data.star",
        list(range(1, 9)),
        base_b,
        "default",
    )

    aliases_a = all_plan_aliases(plan_a)
    aliases_b = all_plan_aliases(plan_b)
    assert len(aliases_a) == len(set(aliases_a)) == 11
    assert len(aliases_b) == len(set(aliases_b)) == 11
    assert set(aliases_a).isdisjoint(aliases_b)
    assert all(len(alias) <= 80 for alias in aliases_a + aliases_b)
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+", alias) for alias in aliases_a + aliases_b)
    assert plan_a["suggested_schedule_name"] != plan_b["suggested_schedule_name"]
