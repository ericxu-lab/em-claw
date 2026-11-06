import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import mrcfile
import numpy as np
import pytest
from PIL import Image

from relion_mcp.catalog import Catalog, CatalogError
from relion_mcp.initial_model_acceptance import record_initial_model_acceptance
from relion_mcp.runner import RelionRunner
from relion_mcp.settings import Settings


def _run_settings(project_root: Path) -> Settings:
    relion_bin = project_root / "relion-bin"
    relion_bin.mkdir(exist_ok=True)
    for executable_name in (
        "relion_pipeliner",
        "relion_image_handler",
        "relion_refine",
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


def _write_bytes(project: Path, relative_path: str, payload: bytes) -> Path:
    path = project / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _write_text(project: Path, relative_path: str, payload: str) -> Path:
    path = project / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return path


def _write_mrc(
    project: Path,
    relative_path: str,
    *,
    box_size_pix: int = 32,
    pixel_size_A: float = 1.0,
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


def _write_png(project: Path, relative_path: str, *, variant: int) -> Path:
    path = project / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    y, x = np.indices((16, 16), dtype=np.uint16)
    pixels = np.stack(
        (
            (x * (11 + variant) + y * 3 + variant * 17) % 256,
            (y * (13 + variant) + x * 5 + variant * 29) % 256,
            ((x + y) * (7 + variant) + variant * 41) % 256,
        ),
        axis=-1,
    ).astype(np.uint8)
    Image.fromarray(pixels).save(path, format="PNG")
    return path


def _model_star(reference_volume: str) -> str:
    return (
        "data_model_general\n\n"
        "_rlnReferenceDimensionality 3\n\n"
        "data_model_classes\n\n"
        "loop_\n"
        "_rlnReferenceImage #1\n"
        f"{reference_volume}\n"
    )


def _initial_model_fixture(project: Path) -> dict[str, Any]:
    seed_particles = "Select/job008/particles.star"
    reference = "InitialModel/job009/run_it100_class001.mrc"
    model = "InitialModel/job009/run_it100_model.star"
    optimiser = "InitialModel/job009/run_it100_optimiser.star"
    data = "InitialModel/job009/run_it100_data.star"

    _write_text(project, seed_particles, "data_particles\n\nloop_\n_rlnImageName #1\n000001@stack.mrcs\n")
    _write_mrc(project, reference, box_size_pix=32, pixel_size_A=1.0)
    _write_text(project, model, _model_star(reference))
    _write_text(
        project,
        optimiser,
        "data_optimiser_general\n\n"
        "_rlnOutputRootName InitialModel/job009/run\n"
        "_rlnModelStarFile InitialModel/job009/run_it100_model.star\n"
        "_rlnExperimentalDataStarFile InitialModel/job009/run_it100_data.star\n"
        "_rlnCurrentIteration 100\n"
        "_rlnNumberOfIterations 100\n",
    )
    _write_text(project, data, "data_particles\n\nloop_\n_rlnImageName #1\n000001@stack.mrcs\n")
    _write_text(
        project,
        "InitialModel/job009/job.star",
        "data_job\n\n"
        "_rlnJobTypeLabel relion.initialmodel\n\n"
        "data_joboptions_values\n\n"
        "loop_\n"
        "_rlnJobOptionVariable #1\n"
        "_rlnJobOptionValue #2\n"
        f"fn_img {seed_particles}\n",
    )
    _write_bytes(project, "InitialModel/job009/RELION_JOB_EXIT_SUCCESS", b"")

    evidence = {
        "map_multiview": "Reports/initial_model_multiview.png",
        "projection_comparison": "Reports/initial_model_projection_comparison.png",
        "late_iteration_comparison": "Reports/initial_model_late_iterations.png",
    }
    for variant, path in enumerate(evidence.values(), start=1):
        _write_png(project, path, variant=variant)

    return {
        "reference_volume": reference,
        "initial_model_particles": seed_particles,
        "classification_particles": seed_particles,
        "model_star": model,
        "optimiser_star": optimiser,
        "data_star": data,
        "evidence_artifacts": evidence,
        "assessment": {
            "seed_status": "SEED_READY",
            "reference_utility": "credible",
            "confidence": "high",
            "decision": "auto_accept",
            "next_action": "advance_primary",
            "approval_flag": True,
            "visual_access": "direct",
            "distinct_2d_views_matched": 3,
            "late_iteration_stable": True,
            "multiple_contours_checked": True,
            "boundary_clearance_pass": True,
            "clipping_detected": False,
            "major_artifact_detected": False,
            "independent_root_sources": [
                "map_pixels",
                "late_iteration_convergence",
            ],
            "critical_gaps": [],
            "rationale": "Three distinct views match and the envelope is stable and unclipped.",
        },
        "geometry": {
            "expected_particle_extent_A": [12.0, 18.0],
            "mask_diameter_A": 20.0,
            "box_size_pix": 32,
            "pixel_size_A": 1.0,
            "boundary_clearance_A": 5.0,
        },
        "particle_lineage": {"mode": "same_particles"},
    }


def _record_acceptance(
    runner: RelionRunner,
    project: Path,
    fixture: dict[str, Any],
) -> str:
    result = record_initial_model_acceptance(
        runner.settings,
        str(project),
        fixture["reference_volume"],
        fixture["initial_model_particles"],
        fixture["classification_particles"],
        fixture["model_star"],
        fixture["optimiser_star"],
        fixture["data_star"],
        fixture["evidence_artifacts"],
        fixture["assessment"],
        fixture["geometry"],
        fixture["particle_lineage"],
        True,
    )
    record = result["initial_model_acceptance"]
    assert isinstance(record, str)
    assert record.startswith(".relion-mcp/initial-model-acceptances/acceptance-")
    assert record.endswith(".json")
    document = json.loads((project / record).read_text(encoding="utf-8"))
    assert (project / record).stat().st_mode & 0o222 == 0
    assert set(document["initial_model_artifacts"]) == {
        "model_star",
        "optimiser_star",
        "data_star",
        "job_star",
        "success_marker",
    }
    for artifact in document["initial_model_artifacts"].values():
        assert len(artifact["sha256"]) == 64
    return record


def _render_class3d(
    runner: RelionRunner,
    fixture: dict[str, Any],
    *,
    particles: str | None = None,
):
    return runner.catalog.render_job(
        "three_d_classification",
        inputs={
            "particles": particles or fixture["classification_particles"],
            "reference_volume": fixture["reference_volume"],
        },
        params={
            "num_classes": 4,
            "symmetry": "C1",
            "mask_diameter_A": fixture["geometry"]["mask_diameter_A"],
        },
    )


def _write_registered_class3d_job(
    project: Path,
    fixture: dict[str, Any],
    job: str = "Class3D/job010",
) -> None:
    _write_text(
        project,
        f"{job}/job.star",
        "data_job\n\n"
        "_rlnJobTypeLabel relion.class3d\n\n"
        "data_joboptions_values\n\n"
        "loop_\n"
        "_rlnJobOptionVariable #1\n"
        "_rlnJobOptionValue #2\n"
        f"fn_img {fixture['classification_particles']}\n"
        f"fn_ref {fixture['reference_volume']}\n"
        f"particle_diameter {fixture['geometry']['mask_diameter_A']}\n",
    )


def test_class3d_add_job_fails_closed_without_acceptance_before_subprocess(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    project.mkdir()
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))
    fixture = _initial_model_fixture(project)
    rendered = _render_class3d(runner, fixture)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Class3D must be rejected before relion_pipeliner runs")
        ),
    )

    with pytest.raises(
        CatalogError,
        match="Initial-model acceptance gate blocked Class3D.*acceptance_record_missing",
    ):
        runner.add_job(
            str(project),
            rendered,
            pipeline_name="default",
            alias="class3d_without_acceptance",
            initial_model_acceptance=None,
            confirm=True,
        )


def test_class3d_add_job_uses_native_job_type_for_gate_even_if_operation_name_differs(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    project.mkdir()
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))
    fixture = _initial_model_fixture(project)
    rendered = replace(
        _render_class3d(runner, fixture),
        requested_operation="future_class3d_alias",
        operation="future_class3d_alias",
    )
    assert rendered.relion_job_type == "Class3D"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("native Class3D jobs must be gated before relion_pipeliner runs")
        ),
    )

    with pytest.raises(
        CatalogError,
        match="Initial-model acceptance gate blocked Class3D.*acceptance_record_missing",
    ):
        runner.add_job(
            str(project),
            rendered,
            pipeline_name="default",
            alias="class3d_native_type_gate",
            initial_model_acceptance=None,
            confirm=True,
        )


def test_class3d_run_job_rejects_reference_hash_mismatch_before_registration(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    project.mkdir()
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))
    fixture = _initial_model_fixture(project)
    record = _record_acceptance(runner, project, fixture)
    (project / fixture["reference_volume"]).write_bytes(b"reference-changed-after-acceptance")
    rendered = _render_class3d(runner, fixture)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a hash-mismatched Class3D job must not reach relion_pipeliner")
        ),
    )

    with pytest.raises(
        CatalogError,
        match="Initial-model acceptance gate blocked Class3D.*reference_sha256_mismatch",
    ):
        runner.run_job(
            str(project),
            rendered,
            pipeline_name="default",
            alias="class3d_changed_reference",
            schedule_name=None,
            initial_model_acceptance=record,
            confirm=True,
        )


def test_class3d_run_job_rejects_particle_lineage_path_mismatch_even_for_same_bytes(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    project.mkdir()
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))
    fixture = _initial_model_fixture(project)
    record = _record_acceptance(runner, project, fixture)
    alternate_particles = "Select/job010/particles.star"
    _write_bytes(
        project,
        alternate_particles,
        (project / fixture["classification_particles"]).read_bytes(),
    )
    rendered = _render_class3d(runner, fixture, particles=alternate_particles)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a particle-lineage mismatch must not reach relion_pipeliner")
        ),
    )

    with pytest.raises(
        CatalogError,
        match="Initial-model acceptance gate blocked Class3D.*particle_lineage_mismatch",
    ):
        runner.run_job(
            str(project),
            rendered,
            pipeline_name="default",
            alias="class3d_wrong_particles",
            schedule_name=None,
            initial_model_acceptance=record,
            confirm=True,
        )


def test_class3d_run_job_rejects_classification_particle_hash_mismatch(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    project.mkdir()
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))
    fixture = _initial_model_fixture(project)
    record = _record_acceptance(runner, project, fixture)
    (project / fixture["classification_particles"]).write_text(
        "data_particles\n\nloop_\n_rlnImageName #1\n000002@changed.mrcs\n",
        encoding="utf-8",
    )
    rendered = _render_class3d(runner, fixture)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("changed Class3D particles must not reach relion_pipeliner")
        ),
    )

    with pytest.raises(
        CatalogError,
        match="Initial-model acceptance gate blocked Class3D.*particles_sha256_mismatch",
    ):
        runner.run_job(
            str(project),
            rendered,
            pipeline_name="default",
            alias="class3d_changed_particles",
            schedule_name=None,
            initial_model_acceptance=record,
            confirm=True,
        )


def test_acceptance_with_critical_gaps_cannot_authorize_class3d(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))
    fixture = _initial_model_fixture(project)
    fixture["assessment"]["critical_gaps"] = ["projection agreement remains unresolved"]

    with pytest.raises(CatalogError, match="critical_gaps"):
        _record_acceptance(runner, project, fixture)

    acceptance_dir = project / ".relion-mcp" / "initial-model-acceptances"
    assert not acceptance_dir.exists() or list(acceptance_dir.iterdir()) == []


def test_acceptance_rejects_malformed_reference_mrc(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))
    fixture = _initial_model_fixture(project)
    (project / fixture["reference_volume"]).write_bytes(b"not-an-mrc-volume")

    with pytest.raises(CatalogError, match=r"(?i)mrc"):
        _record_acceptance(runner, project, fixture)


def test_acceptance_rejects_non_png_evidence_even_with_png_suffix(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))
    fixture = _initial_model_fixture(project)
    evidence_path = fixture["evidence_artifacts"]["map_multiview"]
    (project / evidence_path).write_bytes(b"this is not a PNG")

    with pytest.raises(CatalogError, match=r"(?i)png"):
        _record_acceptance(runner, project, fixture)


def test_acceptance_rejects_duplicate_evidence_content_at_distinct_paths(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))
    fixture = _initial_model_fixture(project)
    first = project / fixture["evidence_artifacts"]["map_multiview"]
    duplicate = project / fixture["evidence_artifacts"]["projection_comparison"]
    duplicate.write_bytes(first.read_bytes())

    with pytest.raises(
        CatalogError,
        match=r"(?i)(distinct|duplicate).*(sha|hash|content)",
    ):
        _record_acceptance(runner, project, fixture)


def test_acceptance_requires_two_independent_root_sources(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))
    fixture = _initial_model_fixture(project)
    fixture["assessment"]["independent_root_sources"] = ["map_pixels"]

    with pytest.raises(CatalogError, match=r"(?i)independent.*root"):
        _record_acceptance(runner, project, fixture)


@pytest.mark.parametrize(
    ("geometry_key", "mismatched_value"),
    (("box_size_pix", 64), ("pixel_size_A", 1.25)),
)
def test_acceptance_rejects_geometry_mismatch_to_reference_mrc_header(
    tmp_path: Path,
    geometry_key: str,
    mismatched_value: float,
):
    project = tmp_path / "project"
    project.mkdir()
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))
    fixture = _initial_model_fixture(project)
    fixture["geometry"][geometry_key] = mismatched_value

    with pytest.raises(CatalogError, match=r"(?i)(geometry|box|pixel|voxel)"):
        _record_acceptance(runner, project, fixture)


def test_acceptance_rejects_reference_not_listed_in_model_star(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))
    fixture = _initial_model_fixture(project)
    _write_text(
        project,
        fixture["model_star"],
        _model_star("InitialModel/job009/run_it100_class002.mrc"),
    )

    with pytest.raises(CatalogError, match=r"(?i)(model|reference).*(class|map|list)"):
        _record_acceptance(runner, project, fixture)


def test_valid_hash_bound_acceptance_allows_class3d_run(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    project.mkdir()
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))
    fixture = _initial_model_fixture(project)
    record = _record_acceptance(runner, project, fixture)
    rendered = _render_class3d(runner, fixture)
    calls: list[tuple[str, str | None]] = []

    def fake_add_job(*args, **kwargs):
        calls.append(("add", kwargs.get("initial_model_acceptance")))
        return {
            "project_dir": str(project),
            "pipeline_name": "default",
            "job": "Class3D/job010/",
        }

    def fake_start_jobs(*args, **kwargs):
        calls.append(("start", None))
        return {"pid": 1234, "jobs": ["Class3D/job010/"]}

    monkeypatch.setattr(runner, "add_job", fake_add_job)
    monkeypatch.setattr(runner, "start_jobs", fake_start_jobs)

    result = runner.run_job(
        str(project),
        rendered,
        pipeline_name="default",
        alias="class3d_accepted",
        schedule_name=None,
        initial_model_acceptance=record,
        confirm=True,
    )

    assert calls == [("add", record), ("start", None)]
    assert result["started"] is True
    assert result["job"] == "Class3D/job010/"


def test_start_only_class3d_revalidates_acceptance_before_scheduler_subprocess(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    project.mkdir()
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))
    fixture = _initial_model_fixture(project)
    _write_registered_class3d_job(project, fixture)
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("an unaccepted registered Class3D job must not start")
        ),
    )

    with pytest.raises(
        CatalogError,
        match="Initial-model acceptance gate blocked Class3D.*acceptance_record_missing",
    ):
        runner.start_jobs(
            str(project),
            ["Class3D/job010/"],
            "class3d_start_only",
            "default",
            confirm=True,
        )


def test_start_only_class3d_auto_resolves_and_revalidates_valid_acceptance(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    project.mkdir()
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))
    fixture = _initial_model_fixture(project)
    record = _record_acceptance(runner, project, fixture)
    _write_registered_class3d_job(project, fixture)
    launches: list[list[str]] = []

    class FakeProcess:
        pid = 4321

    def fake_popen(command, **kwargs):
        launches.append(command)
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    result = runner.start_jobs(
        str(project),
        ["Class3D/job010/"],
        "class3d_start_only",
        "default",
        confirm=True,
    )

    assert len(launches) == 1
    assert result["pid"] == 4321
    assert result["initial_model_acceptance_gates"][0]["acceptance_record"] == record


def test_non_class3d_run_does_not_require_initial_model_acceptance(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "project"
    project.mkdir()
    runner = RelionRunner(Catalog(), _run_settings(tmp_path))
    rendered = runner.catalog.render_job(
        "ctf_estimation",
        inputs={"micrographs": "MotionCorr/job002/corrected_micrographs.star"},
        params={},
    )
    calls: list[str] = []

    def fake_add_job(*args, **kwargs):
        calls.append("add")
        return {
            "project_dir": str(project),
            "pipeline_name": "default",
            "job": "CtfFind/job003/",
        }

    def fake_start_jobs(*args, **kwargs):
        calls.append("start")
        return {"pid": 1234, "jobs": ["CtfFind/job003/"]}

    monkeypatch.setattr(runner, "add_job", fake_add_job)
    monkeypatch.setattr(runner, "start_jobs", fake_start_jobs)

    result = runner.run_job(
        str(project),
        rendered,
        pipeline_name="default",
        alias="ctf",
        schedule_name=None,
        initial_model_acceptance=None,
        confirm=True,
    )

    assert calls == ["add", "start"]
    assert result["started"] is True
