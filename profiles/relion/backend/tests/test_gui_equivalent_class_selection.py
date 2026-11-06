import json
from pathlib import Path

import pytest

from relion_mcp.class_select import (
    CLASS_AVERAGES_NODE_TYPE,
    PARTICLE_NODE_TYPE,
    ClassSelectionError,
    main,
    materialize_gui_equivalent_selection,
)
from relion_mcp.star import StarFormatError, parse_star_block, particle_star_summary


def write_particles(path: Path, *, duplicate: bool = False) -> None:
    second_image = "000001@particles.mrcs" if duplicate else "000002@particles.mrcs"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""# version 50001

data_optics

loop_
_rlnOpticsGroupName #1
_rlnOpticsGroup #2
opticsGroup1 1

data_particles

loop_
_rlnImageName #1
_rlnClassNumber #2
_rlnOpticsGroup #3
000001@particles.mrcs 1 1
{second_image} 2 1
000003@particles.mrcs 3 1
000004@particles.mrcs 1 1
000005@particles.mrcs 5 1

""",
        encoding="utf-8",
    )


def write_model(path: Path) -> None:
    path.write_text(
        """# version 50001

data_model_general

_rlnReferenceDimensionality 2

data_model_classes

loop_
_rlnReferenceImage #1
_rlnClassDistribution #2
000001@Class2D/job001/run_classes.mrcs 0.3
000002@Class2D/job001/run_classes.mrcs 0.2
000003@Class2D/job001/run_classes.mrcs 0.2
000004@Class2D/job001/run_classes.mrcs 0.1
000005@Class2D/job001/run_classes.mrcs 0.2
000006@Class2D/job001/run_classes.mrcs 0.0

""",
        encoding="utf-8",
    )


def make_project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    particles = project / "Class2D" / "job001" / "run_data.star"
    write_particles(particles)
    write_model(particles.with_name("run_model.star"))
    output = project / "External" / "job001"
    output.mkdir(parents=True)
    return project, output


def test_materialization_matches_gui_selection_semantics(tmp_path: Path):
    project, output = make_project(tmp_path)

    result = materialize_gui_equivalent_selection(
        project_dir=project,
        output_dir="External/job001/",
        particles_star="Class2D/job001/run_data.star",
        selected_classes=(1, 3, 5),
        expected_particles=4,
        class_model_star="Class2D/job001/run_model.star",
    )

    summary = particle_star_summary(output / "particles.star")
    assert summary.particle_count == 4
    assert summary.class_counts == {1: 2, 3: 1, 5: 1}
    particles = parse_star_block(output / "particles.star", "particles")
    assert particles is not None
    assert particles["rlnImageName"] == [
        "000001@particles.mrcs",
        "000003@particles.mrcs",
        "000004@particles.mrcs",
        "000005@particles.mrcs",
    ]
    optics = parse_star_block(output / "particles.star", "optics")
    assert optics == {"rlnOpticsGroupName": ["opticsGroup1"], "rlnOpticsGroup": ["1"]}

    backup = parse_star_block(output / "backup_selection.star", "")
    assert backup == {"rlnSelected": ["1", "0", "1", "0", "1", "0"]}
    class_averages = parse_star_block(output / "class_averages.star", "")
    assert class_averages is not None
    assert class_averages["rlnReferenceImage"] == [
        "000001@Class2D/job001/run_classes.mrcs",
        "000003@Class2D/job001/run_classes.mrcs",
        "000005@Class2D/job001/run_classes.mrcs",
    ]
    nodes = parse_star_block(output / "RELION_OUTPUT_NODES.star", "output_nodes")
    assert nodes == {
        "rlnPipeLineNodeName": [
            "External/job001/particles.star",
            "External/job001/class_averages.star",
        ],
        "rlnPipeLineNodeTypeLabel": [PARTICLE_NODE_TYPE, CLASS_AVERAGES_NODE_TYPE],
    }
    provenance = json.loads((output / "selection_provenance.json").read_text())
    assert provenance["selected_class_numbers"] == [1, 3, 5]
    assert provenance["selected_particle_count"] == 4
    assert provenance["preserves_source_particle_order"] is True
    assert (output / "RELION_JOB_EXIT_SUCCESS").is_file()
    assert not (output / "RELION_JOB_EXIT_FAILURE").exists()
    assert result["class_averages_written"] is True


def test_materialization_refuses_to_overwrite_outputs(tmp_path: Path):
    project, output = make_project(tmp_path)
    (output / "particles.star").write_text("preserve me\n", encoding="utf-8")

    with pytest.raises(ClassSelectionError, match="Refusing to overwrite"):
        materialize_gui_equivalent_selection(
            project_dir=project,
            output_dir="External/job001/",
            particles_star="Class2D/job001/run_data.star",
            selected_classes=(1,),
        )

    assert (output / "particles.star").read_text(encoding="utf-8") == "preserve me\n"


def test_materialization_rejects_changed_count_before_publishing(tmp_path: Path):
    project, output = make_project(tmp_path)

    with pytest.raises(ClassSelectionError, match="changed between planning and execution"):
        materialize_gui_equivalent_selection(
            project_dir=project,
            output_dir="External/job001/",
            particles_star="Class2D/job001/run_data.star",
            selected_classes=(1, 3, 5),
            expected_particles=999,
        )

    assert not (output / "particles.star").exists()
    assert not (output / "RELION_JOB_EXIT_SUCCESS").exists()


def test_materialization_rejects_duplicate_particle_identity(tmp_path: Path):
    project, output = make_project(tmp_path)
    write_particles(project / "Class2D" / "job001" / "run_data.star", duplicate=True)

    with pytest.raises(StarFormatError, match="duplicate rlnImageName"):
        materialize_gui_equivalent_selection(
            project_dir=project,
            output_dir="External/job001/",
            particles_star="Class2D/job001/run_data.star",
            selected_classes=(1, 3, 5),
        )

    assert not (output / "particles.star").exists()


def test_cli_does_not_write_failure_markers_to_an_invalid_output_path(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    monkeypatch.chdir(project)

    with pytest.raises(SystemExit) as error:
        main(
            [
                "--o",
                "../outside",
                "--in_parts",
                "missing.star",
                "--classes",
                "invalid",
            ]
        )

    assert error.value.code == 1
    assert not (outside / "selection_error.txt").exists()
    assert not (outside / "RELION_JOB_EXIT_FAILURE").exists()


def test_cli_writes_failure_markers_to_a_valid_output_path(
    tmp_path: Path,
    monkeypatch,
):
    project, output = make_project(tmp_path)
    monkeypatch.chdir(project)

    with pytest.raises(SystemExit) as error:
        main(
            [
                "--o",
                "External/job001/",
                "--in_parts",
                "missing.star",
                "--classes",
                "invalid",
            ]
        )

    assert error.value.code == 1
    assert "Invalid selected class number" in (output / "selection_error.txt").read_text(
        encoding="utf-8"
    )
    assert (output / "RELION_JOB_EXIT_FAILURE").is_file()


@pytest.mark.parametrize(
    "target_name",
    ["selection_error.txt", "RELION_JOB_EXIT_FAILURE"],
)
def test_cli_does_not_follow_symlinked_failure_targets(
    tmp_path: Path,
    monkeypatch,
    target_name: str,
):
    project, output = make_project(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("sentinel\n", encoding="utf-8")
    (output / target_name).symlink_to(outside)
    monkeypatch.chdir(project)

    with pytest.raises(SystemExit) as error:
        main(
            [
                "--o",
                "External/job001/",
                "--in_parts",
                "missing.star",
                "--classes",
                "invalid",
            ]
        )

    assert error.value.code == 1
    assert outside.read_text(encoding="utf-8") == "sentinel\n"
    other_target = {
        "selection_error.txt": "RELION_JOB_EXIT_FAILURE",
        "RELION_JOB_EXIT_FAILURE": "selection_error.txt",
    }[target_name]
    assert not (output / other_target).exists()
