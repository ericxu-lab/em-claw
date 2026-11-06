import json
import subprocess
from pathlib import Path
from typing import Any

import mrcfile
import numpy as np
import pytest

from relion_mcp.catalog import Catalog, CatalogError
from relion_mcp.initial_model_acceptance import (
    record_external_reference,
    require_initial_model_acceptance,
    validate_initial_model_acceptance,
)
from relion_mcp.runner import RelionRunner
from relion_mcp.settings import Settings

REFERENCE = "Reference/cryosparc_J2291_map.mrc"
PARTICLES = "External/cryosparc/particles.star"
MASK = "Masks/focus.mrc"
POSE_COLUMNS = (
    "_rlnAngleRot #2\n_rlnAngleTilt #3\n_rlnAnglePsi #4\n"
    "_rlnOriginXAngst #5\n_rlnOriginYAngst #6\n"
)


def _run_settings(project_root: Path) -> Settings:
    relion_bin = project_root / "relion-bin"
    relion_bin.mkdir(exist_ok=True)
    for executable_name in ("relion_pipeliner", "relion_image_handler", "relion_refine"):
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


def _write_mrc(
    project: Path,
    relative_path: str,
    *,
    box_size_pix: int = 32,
    pixel_size_A: float = 0.824,
) -> Path:
    path = project / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    z, y, x = np.indices((box_size_pix, box_size_pix, box_size_pix), dtype=np.float32)
    centre = (box_size_pix - 1) / 2.0
    radius_squared = (x - centre) ** 2 + (y - centre) ** 2 + (z - centre) ** 2
    data = np.exp(-radius_squared / (2.0 * (box_size_pix / 7.0) ** 2)).astype(np.float32)
    with mrcfile.new(path, overwrite=True) as handle:
        handle.set_data(data)
        handle.voxel_size = pixel_size_A
        handle.update_header_stats()
    return path


def _write_particles(
    project: Path,
    relative_path: str = PARTICLES,
    *,
    box_size_pix: int = 32,
    pixel_sizes_A: tuple[float, ...] = (0.824, 0.824),
    with_poses: bool = True,
) -> Path:
    optics_rows = "".join(
        f"{group} {box_size_pix} {pixel:.6f} 2\n"
        for group, pixel in enumerate(pixel_sizes_A, start=1)
    )
    pose_values = " 10.0 90.0 -30.0 1.5 -2.0" if with_poses else ""
    particle_rows = "".join(
        f"{index:06d}@stack.mrcs{pose_values} {1 + index % len(pixel_sizes_A)}\n"
        for index in range(1, 5)
    )
    group_column = 7 if with_poses else 2
    text = (
        "data_optics\n\nloop_\n"
        "_rlnOpticsGroup #1\n_rlnImageSize #2\n_rlnImagePixelSize #3\n"
        "_rlnImageDimensionality #4\n"
        f"{optics_rows}\n"
        "data_particles\n\nloop_\n"
        "_rlnImageName #1\n"
        f"{POSE_COLUMNS if with_poses else ''}"
        f"_rlnOpticsGroup #{group_column}\n"
        f"{particle_rows}"
    )
    path = project / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _provenance() -> dict[str, Any]:
    return {
        "source_software": "CryoSPARC v4.6.2",
        "source_job": "P362 J2291 new_local_refine",
        "rationale": "Consensus local refinement whose poses the particles carry.",
    }


def _record(
    runner: RelionRunner,
    project: Path,
    *,
    reference: str = REFERENCE,
    mode: str = "poses_from_reference_refinement",
    mask_diameter_A: float = 20.0,
) -> str:
    result = record_external_reference(
        runner.settings,
        str(project),
        reference,
        PARTICLES,
        mask_diameter_A,
        _provenance(),
        {"mode": mode},
        True,
    )
    record = result["initial_model_acceptance"]
    assert record == result["acceptance_record"]
    assert record.startswith(".relion-mcp/initial-model-acceptances/external-")
    assert (project / record).stat().st_mode & 0o222 == 0
    return record


def _fixture(tmp_path: Path) -> tuple[RelionRunner, Path]:
    project = tmp_path / "project"
    project.mkdir()
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))
    _write_mrc(project, REFERENCE)
    _write_mrc(project, MASK)
    _write_particles(project)
    return runner, project


def _render_class3d(runner: RelionRunner, *, alignment: bool, mask_diameter_A: float = 20.0):
    return runner.catalog.render_job(
        "three_d_classification",
        inputs={
            "particles": PARTICLES,
            "reference_volume": REFERENCE,
            "reference_mask": MASK,
        },
        params={
            "num_classes": 3,
            "symmetry": "C1",
            "mask_diameter_A": mask_diameter_A,
            "alignment": alignment,
        },
    )


def _forbid_pipeliner(monkeypatch, message: str) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError(message)),
    )


class _ReachedPipeliner(Exception):
    pass


def _detect_pipeliner(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        raise _ReachedPipeliner

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_record_binds_measured_geometry_provenance_and_pose_lineage(tmp_path: Path):
    runner, project = _fixture(tmp_path)

    record = _record(runner, project)
    document = json.loads((project / record).read_text(encoding="utf-8"))

    assert document["schema"] == "relion.external-reference-acceptance/v1"
    assert document["geometry"] == {
        "box_size_pix": 32,
        "pixel_size_A": 0.824,
        "optics_group_count": 2,
        "mask_diameter_A": 20.0,
    }
    assert document["reference_provenance"] == _provenance()
    assert document["pose_lineage"] == {"mode": "poses_from_reference_refinement"}
    assert document["reference_volume"]["path"] == REFERENCE
    assert len(document["classification_particles"]["sha256"]) == 64
    assert _record(runner, project) == record


def test_no_alignment_class3d_passes_gate_with_reference_poses(tmp_path: Path, monkeypatch):
    runner, project = _fixture(tmp_path)
    record = _record(runner, project)
    rendered = _render_class3d(runner, alignment=False)
    assert rendered.options_by_key["dont_skip_align"] == "No"
    _detect_pipeliner(monkeypatch)

    with pytest.raises(_ReachedPipeliner):
        runner.add_job(
            str(project),
            rendered,
            pipeline_name="default",
            alias="class3d_external_noalign",
            initial_model_acceptance=record,
            confirm=True,
        )


def test_no_alignment_class3d_rejects_reference_independent_poses(tmp_path: Path, monkeypatch):
    runner, project = _fixture(tmp_path)
    record = _record(runner, project, mode="independent_of_reference")
    rendered = _render_class3d(runner, alignment=False)
    _forbid_pipeliner(monkeypatch, "no-alignment Class3D needs reference-derived poses")

    with pytest.raises(CatalogError, match="pose_lineage_mismatch"):
        runner.add_job(
            str(project),
            rendered,
            pipeline_name="default",
            alias="class3d_external_noalign_bad_poses",
            initial_model_acceptance=record,
            confirm=True,
        )


def test_aligned_class3d_accepts_reference_independent_poses(tmp_path: Path):
    runner, project = _fixture(tmp_path)
    record = _record(runner, project, mode="independent_of_reference")

    result = require_initial_model_acceptance(
        runner.settings,
        project,
        record,
        reference_volume=REFERENCE,
        classification_particles=PARTICLES,
        mask_diameter_A=20.0,
        alignment=True,
    )

    assert result["valid"] is True
    assert result["schema"] == "relion.external-reference-acceptance/v1"


def test_gate_rejects_reference_changed_after_recording(tmp_path: Path, monkeypatch):
    runner, project = _fixture(tmp_path)
    record = _record(runner, project)
    reference = project / REFERENCE
    with mrcfile.open(reference, mode="r+") as handle:
        handle.data[0, 0, 0] += 1.0
    rendered = _render_class3d(runner, alignment=False)
    _forbid_pipeliner(monkeypatch, "a changed reference must not reach relion_pipeliner")

    with pytest.raises(CatalogError, match="reference_sha256_mismatch"):
        runner.add_job(
            str(project),
            rendered,
            pipeline_name="default",
            alias="class3d_external_tampered",
            initial_model_acceptance=record,
            confirm=True,
        )


def test_gate_rejects_mask_diameter_differing_from_record(tmp_path: Path):
    runner, project = _fixture(tmp_path)
    record = _record(runner, project)

    result = validate_initial_model_acceptance(
        runner.settings,
        str(project),
        record,
        reference_volume=REFERENCE,
        classification_particles=PARTICLES,
        mask_diameter_A=24.0,
        alignment=False,
    )

    assert result["valid"] is False
    assert {error["code"] for error in result["errors"]} == {"geometry_mismatch"}


def test_record_rejects_reference_box_mismatch(tmp_path: Path):
    runner, project = _fixture(tmp_path)
    _write_mrc(project, REFERENCE, box_size_pix=36)

    with pytest.raises(CatalogError, match="does not match the reference box 36"):
        _record(runner, project)


def test_record_rejects_any_optics_group_pixel_mismatch(tmp_path: Path):
    runner, project = _fixture(tmp_path)
    _write_particles(project, pixel_sizes_A=(0.824, 0.83))

    with pytest.raises(CatalogError, match="Optics row 2 pixel size"):
        _record(runner, project)


def test_record_rejects_reference_pose_claim_without_pose_columns(tmp_path: Path):
    runner, project = _fixture(tmp_path)
    _write_particles(project, with_poses=False)

    with pytest.raises(CatalogError, match="requires particle pose columns"):
        _record(runner, project)


def test_record_rejects_mask_wider_than_box(tmp_path: Path):
    runner, project = _fixture(tmp_path)

    with pytest.raises(CatalogError, match="exceeds the .* particle box"):
        _record(runner, project, mask_diameter_A=40.0)


def test_record_refuses_initial_model_outputs(tmp_path: Path):
    runner, project = _fixture(tmp_path)
    _write_mrc(project, "InitialModel/job009/run_it100_class001.mrc")

    with pytest.raises(CatalogError, match="use relion_record_initial_model_acceptance"):
        _record(runner, project, reference="InitialModel/job009/run_it100_class001.mrc")


def test_record_requires_provenance_fields(tmp_path: Path):
    runner, project = _fixture(tmp_path)

    with pytest.raises(CatalogError, match="provenance.source_job"):
        record_external_reference(
            runner.settings,
            str(project),
            REFERENCE,
            PARTICLES,
            20.0,
            {"source_software": "CryoSPARC", "rationale": "consensus"},
            {"mode": "poses_from_reference_refinement"},
            True,
        )


def test_hand_edited_record_fails_digest_check(tmp_path: Path):
    runner, project = _fixture(tmp_path)
    record = _record(runner, project)
    path = project / record
    document = json.loads(path.read_text(encoding="utf-8"))
    document["pose_lineage"] = {"mode": "independent_of_reference"}
    path.chmod(0o644)
    path.write_text(json.dumps(document), encoding="utf-8")

    result = validate_initial_model_acceptance(runner.settings, str(project), record)

    assert result["valid"] is False
    assert "record_sha256_mismatch" in {error["code"] for error in result["errors"]}


def test_start_only_no_alignment_class3d_discovers_external_record(tmp_path: Path, monkeypatch):
    runner, project = _fixture(tmp_path)
    record = _record(runner, project)
    job_star = project / "Class3D/job001/job.star"
    job_star.parent.mkdir(parents=True)
    job_star.write_text(
        "data_job\n\n"
        "_rlnJobTypeLabel relion.class3d\n\n"
        "data_joboptions_values\n\n"
        "loop_\n"
        "_rlnJobOptionVariable #1\n"
        "_rlnJobOptionValue #2\n"
        f"fn_img {PARTICLES}\n"
        f"fn_ref {REFERENCE}\n"
        "particle_diameter 20.0\n"
        "dont_skip_align No\n",
        encoding="utf-8",
    )
    launches: list[list[str]] = []

    class FakeProcess:
        pid = 4321

    def fake_popen(command, **kwargs):
        launches.append(command)
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    result = runner.start_jobs(
        str(project),
        ["Class3D/job001/"],
        "class3d_external_start",
        "default",
        confirm=True,
    )

    assert len(launches) == 1
    assert result["initial_model_acceptance_gates"][0]["acceptance_record"] == record
