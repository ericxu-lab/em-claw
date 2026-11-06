from __future__ import annotations

import base64
import json
import sys
import types
from dataclasses import replace
from pathlib import Path

import anyio
import mrcfile
import numpy as np
import pytest
from mcp import Client

from relion_mcp import server
from relion_mcp.server import mcp
from relion_mcp.settings import Settings


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_particle_preparation_tools_expose_safe_public_schemas() -> None:
    tools_by_name = {tool.name: tool for tool in await mcp.list_tools()}
    for name in (
        "relion_render_2d_classes",
        "relion_validate_2d_class_review",
        "relion_compare_map_projections",
        "relion_render_particle_boxes",
        "relion_audit_particle_selection",
        "relion_plan_micrograph_subset",
    ):
        tool = tools_by_name[name]
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.open_world_hint is False
        assert "confirm" not in tool.input_schema["properties"]

    plan = tools_by_name["relion_plan_micrograph_subset"]
    materialize = tools_by_name["relion_materialize_micrograph_subset"]
    assert materialize.annotations.read_only_hint is False
    assert materialize.annotations.destructive_hint is False
    assert materialize.annotations.idempotent_hint is False
    assert materialize.annotations.open_world_hint is False
    assert materialize.input_schema["properties"]["confirm"]["default"] is False
    assert "expected_plan_sha256" in materialize.input_schema["required"]
    for tool in (plan, materialize):
        parameter = tool.input_schema["properties"]["particles_star"]
        assert parameter["default"] is None
        assert {choice["type"] for choice in parameter["anyOf"]} == {"string", "null"}
        assert "particles_star" not in tool.input_schema["required"]
        assert {"training_count", "heldout_count", "output_name"} <= set(tool.input_schema["required"])

    classes = tools_by_name["relion_render_2d_classes"]
    assert classes.input_schema["properties"]["all_classes"]["default"] is True
    review = tools_by_name["relion_validate_2d_class_review"]
    assert review.input_schema["properties"]["rendered_pages"]["type"] == "array"
    assert "rendered_pages" in review.input_schema["required"]


@pytest.mark.anyio
async def test_projection_comparison_exposes_bounded_read_only_schema() -> None:
    tools_by_name = {tool.name: tool for tool in await mcp.list_tools()}
    tool = tools_by_name["relion_compare_map_projections"]
    properties = tool.input_schema["properties"]

    assert tool.title == "Compare map projections with RELION 2D classes"
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.open_world_hint is False
    assert tool.input_schema["required"] == [
        "project_dir",
        "map_path",
        "model_star",
        "selected_class_numbers",
        "comparison_lowpass_A",
        "particle_diameter_A",
    ]
    assert properties["selected_class_numbers"]["type"] == "array"
    assert properties["selected_class_numbers"]["items"]["type"] == "integer"
    assert properties["classes_mrcs"]["default"] is None
    assert {choice["type"] for choice in properties["classes_mrcs"]["anyOf"]} == {
        "string",
        "null",
    }
    assert properties["direction_count"]["default"] == 192
    assert properties["inplane_step_deg"]["default"] == 10.0
    assert properties["max_shift_A"]["default"] == 12.0
    assert properties["invert_class_contrast"]["default"] is False


@pytest.mark.anyio
async def test_initial_model_evidence_publisher_exposes_idempotent_write_schema() -> None:
    tools_by_name = {tool.name: tool for tool in await mcp.list_tools()}
    tool = tools_by_name["relion_publish_initial_model_evidence"]
    properties = tool.input_schema["properties"]

    assert tool.title == "Publish inspected InitialModel visual evidence"
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.idempotent_hint is True
    assert tool.annotations.open_world_hint is False
    assert tool.input_schema["required"] == [
        "project_dir",
        "evidence_kind",
        "image_sha256",
    ]
    assert properties["evidence_kind"]["enum"] == [
        "map_multiview",
        "projection_comparison",
        "late_iteration_comparison",
    ]
    assert properties["confirm"]["default"] is False


def test_initial_model_evidence_publisher_delegates_exact_cache_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {
        "status": "published",
        "artifact": ".relion-mcp/initial-model-evidence/projection_comparison.png",
    }
    observed: dict[str, object] = {}

    def fake_publish(*args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        return expected

    monkeypatch.setattr(server, "publish_visual_evidence", fake_publish)
    digest = "a" * 64

    result = server.relion_publish_initial_model_evidence(
        "/project",
        "projection_comparison",
        digest,
        confirm=True,
    )

    assert result is expected
    assert observed["args"] == (
        server.SETTINGS,
        "/project",
        "projection_comparison",
        digest,
    )
    assert observed["kwargs"] == {"confirm": True}


@pytest.mark.parametrize("particles_star", [None, "Select/job012/particles.star"])
def test_micrograph_subset_wrappers_preserve_label_source_and_plan_binding(
    monkeypatch: pytest.MonkeyPatch, particles_star: str | None
) -> None:
    calls = []
    plan_result = {"plan_sha256": "reviewed-plan", "outputs": {"training": "training.star"}}
    materialized_result = {"status": "materialized", "manifest_sha256": "manifest"}

    def fake_plan(*args, **kwargs):
        calls.append(("plan", args, kwargs))
        return plan_result

    def fake_materialize(*args, **kwargs):
        calls.append(("materialize", args, kwargs))
        return materialized_result

    monkeypatch.setattr(server, "plan_stratified_micrograph_subset", fake_plan)
    monkeypatch.setattr(server, "materialize_stratified_micrograph_subset", fake_materialize)
    request = {
        "project_dir": "/project",
        "micrographs_star": "CtfFind/job003/micrographs.star",
        "output_name": "topaz_split_r01",
        "training_count": 16,
        "heldout_count": 8,
        "seed": 23,
        "strata_columns": ["rlnOpticsGroup"],
        "numeric_bins": {"rlnDefocusU": [18000.0]},
    }
    if particles_star is not None:
        request["particles_star"] = particles_star
    plan = server.relion_plan_micrograph_subset(**request)
    result = server.relion_materialize_micrograph_subset(
        **request, expected_plan_sha256=plan["plan_sha256"], confirm=True,
    )

    assert plan is plan_result
    assert result is materialized_result
    expected_args = (server.SETTINGS, request["project_dir"], request["micrographs_star"])
    expected_kwargs = {
        key: value for key, value in request.items()
        if key not in {"project_dir", "micrographs_star"}
    }
    expected_kwargs["particles_star"] = particles_star
    assert calls == [
        ("plan", expected_args, expected_kwargs),
        ("materialize", expected_args, {
            **expected_kwargs, "expected_plan_sha256": "reviewed-plan", "confirm": True,
        }),
    ]


@pytest.fixture
def particle_transport_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = tmp_path / "project"
    classes = project / "Class2D"
    classes.mkdir(parents=True)
    with mrcfile.new(classes / "classes.mrcs") as handle:
        handle.set_data(np.arange(3 * 16 * 16, dtype=np.float32).reshape((3, 16, 16)))
    (classes / "model.star").write_text(
        "data_model_classes\n\nloop_\n_rlnReferenceImage #1\n_rlnClassScore #2\n"
        "1@classes.mrcs 0.8\n2@classes.mrcs 0.6\n3@classes.mrcs 0.01\n",
        encoding="utf-8",
    )
    (project / "particles.star").write_text(
        "data_particles\n\nloop_\n_rlnImageName #1\n"
        "3@Class2D/classes.mrcs\n1@Class2D/classes.mrcs\n2@Class2D/classes.mrcs\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "SETTINGS", replace(
        server.SETTINGS, mode="plan", project_roots=(tmp_path.resolve(),),
        visual_max_side_px=700, visual_max_panels=8,
    ))
    return project


def test_2d_multipage_image_transport_preserves_complete_review_metadata(
    particle_transport_project: Path,
) -> None:
    project = str(particle_transport_project)
    result = server.relion_render_2d_classes(
        project, "Class2D/model.star", max_classes=2, columns=2,
    )

    assert [item.type for item in result.content] == ["image", "text", "image", "text"]
    assert result.structured_content is None
    pages = []
    for image, text in zip(result.content[::2], result.content[1::2], strict=True):
        assert image.mime_type == "image/png"
        assert base64.b64decode(image.data).startswith(b"\x89PNG\r\n\x1a\n")
        pages.append(json.loads(text.text))
    assert [[row["class_number"] for row in page["rendered_classes"]] for page in pages] == [[1, 2], [3]]
    complete = server.relion_validate_2d_class_review(project, "Class2D/model.star", pages)
    assert complete["valid"] is True
    assert complete["rendered_class_numbers"] == [1, 2, 3]
    truncated = server.relion_validate_2d_class_review(project, "Class2D/model.star", pages[:1])
    assert truncated["valid"] is False
    assert truncated["missing_class_numbers"] == [3]


def test_particle_box_transport_preserves_requested_rows_and_source_pixels(
    particle_transport_project: Path,
) -> None:
    result = server.relion_render_particle_boxes(
        str(particle_transport_project), "particles.star",
        particle_numbers=[3, 1], max_particles=2, columns=2,
    )

    assert [item.type for item in result.content] == ["image", "text"]
    assert result.content[0].mime_type == "image/png"
    assert base64.b64decode(result.content[0].data).startswith(b"\x89PNG\r\n\x1a\n")
    assert result.structured_content is None
    metadata = json.loads(result.content[1].text)
    assert metadata["report_type"] == "relion_particle_boxes"
    assert metadata["sampling"] == "explicit_rows"
    rows = metadata["rendered_particles"]
    assert [row["particle_number"] for row in rows] == [3, 1]
    assert [row["stack_index"] for row in rows] == [2, 3]
    with mrcfile.open(particle_transport_project / "Class2D/classes.mrcs") as handle:
        assert [row["mean"] for row in rows] == [float(handle.data[i].mean()) for i in (1, 2)]


@pytest.mark.anyio
async def test_3d_render_tools_expose_nullable_display_lowpass() -> None:
    tools_by_name = {tool.name: tool for tool in await mcp.list_tools()}

    for name in ("relion_render_3d_classes", "relion_render_map_report"):
        parameter = tools_by_name[name].input_schema["properties"]["display_lowpass_A"]
        assert parameter["default"] is None
        assert {choice["type"] for choice in parameter["anyOf"]} == {"number", "null"}


@pytest.mark.anyio
async def test_symmetry_scan_exposes_bounded_public_schema() -> None:
    tools_by_name = {tool.name: tool for tool in await mcp.list_tools()}
    tool = tools_by_name["relion_scan_symmetry"]
    properties = tool.input_schema["properties"]

    assert tool.title == "Scan C1 half-map symmetry"
    assert tool.annotations.read_only_hint is True
    assert tool.input_schema["required"] == [
        "project_dir",
        "half_map_1",
        "half_map_2",
        "analysis_lowpass_A",
    ]
    assert properties["mask_path"]["default"] is None
    assert properties["common_max_order"]["default"] == 8
    assert properties["max_order"]["default"] == 32
    assert properties["stage2_window"]["default"] == 2
    assert properties["max_candidates"]["default"] == 5
    assert properties["focus_groups"]["default"] is None
    focus_array = next(
        choice
        for choice in properties["focus_groups"]["anyOf"]
        if choice["type"] == "array"
    )
    assert focus_array["items"]["type"] == "string"


@pytest.mark.anyio
async def test_symmetry_alignment_exposes_explicit_generic_public_schemas() -> None:
    tools_by_name = {tool.name: tool for tool in await mcp.list_tools()}
    rendered = tools_by_name["relion_render_symmetry_alignment"]
    materialized = tools_by_name["relion_align_symmetry_reference"]

    assert rendered.title == "Render RELION symmetry alignment"
    assert materialized.title == "Materialize symmetry-aligned RELION reference"
    expected_required = [
        "project_dir",
        "input_map",
        "output_map",
        "target_symmetry",
        "search_max_resolution_A",
    ]
    assert rendered.input_schema["required"] == expected_required
    assert materialized.input_schema["required"] == expected_required
    assert rendered.annotations.read_only_hint is True
    assert rendered.annotations.open_world_hint is False
    assert materialized.annotations.read_only_hint is False
    assert materialized.annotations.destructive_hint is False
    assert materialized.annotations.idempotent_hint is False
    assert materialized.annotations.open_world_hint is False
    assert materialized.input_schema["properties"]["confirm"]["default"] is False

    for tool in (rendered, materialized):
        properties = tool.input_schema["properties"]
        assert properties["target_symmetry"]["type"] == "string"
        assert properties["search_max_resolution_A"]["type"] == "number"
        assert "apply_sym" not in properties
        assert "only_rot" not in properties
        assert "overwrite" not in properties


@pytest.mark.anyio
async def test_duplicate_removal_exposes_paired_render_and_write_schemas() -> None:
    tools_by_name = {tool.name: tool for tool in await mcp.list_tools()}
    rendered = tools_by_name["relion_render_remove_duplicate_particles"]
    materialized = tools_by_name["relion_remove_duplicate_particles"]

    assert rendered.title == "Render duplicate-particle removal"
    assert materialized.title == "Remove duplicate particles"
    expected_required = [
        "project_dir",
        "particles",
        "duplicate_distance_A",
        "output_star",
    ]
    assert rendered.input_schema["required"] == expected_required
    assert materialized.input_schema["required"] == expected_required
    assert rendered.annotations.read_only_hint is True
    assert rendered.annotations.open_world_hint is False
    assert materialized.annotations.read_only_hint is False
    assert materialized.annotations.destructive_hint is False
    assert materialized.annotations.idempotent_hint is False
    assert materialized.annotations.open_world_hint is False
    assert "confirm" not in rendered.input_schema["properties"]
    assert materialized.input_schema["properties"]["confirm"]["default"] is False
    assert rendered.input_schema["properties"]["image_pixel_size_A"]["default"] is None


@pytest.mark.anyio
async def test_cryosparc_particle_import_exposes_paired_render_and_write_schemas() -> None:
    tools_by_name = {tool.name: tool for tool in await mcp.list_tools()}
    rendered = tools_by_name["relion_render_cryosparc_particle_import"]
    materialized = tools_by_name["relion_import_cryosparc_particles"]

    assert rendered.title == "Render CryoSPARC particle import"
    assert materialized.title == "Import CryoSPARC particles"
    expected_required = [
        "project_dir",
        "source_project_dir",
        "particles_cs",
    ]
    assert rendered.input_schema["required"] == expected_required
    assert materialized.input_schema["required"] == expected_required
    assert rendered.annotations.read_only_hint is True
    assert rendered.annotations.open_world_hint is False
    assert materialized.annotations.read_only_hint is False
    assert materialized.annotations.destructive_hint is False
    assert materialized.annotations.idempotent_hint is False
    assert materialized.annotations.open_world_hint is False
    assert "confirm" not in rendered.input_schema["properties"]
    assert materialized.input_schema["properties"]["confirm"]["default"] is False
    for tool in (rendered, materialized):
        properties = tool.input_schema["properties"]
        assert properties["passthrough_cs"]["default"] is None
        assert properties["export_name"]["default"] == "particles"
        assert "source_attestation" not in properties


def test_cryosparc_particle_import_render_delegates_without_confirm(monkeypatch) -> None:
    expected = {"operation": "cryosparc_particle_import", "status": "rendered"}
    observed: dict[str, object] = {}

    def fake_render(*args):
        observed["args"] = args
        return expected

    monkeypatch.setattr(server, "render_cryosparc_particle_import", fake_render)

    result = server.relion_render_cryosparc_particle_import(
        "/relion/project",
        "/cryosparc/P1",
        "J42/particles.cs",
        ["J42/J42_passthrough_particles.cs"],
        "class3d-input",
    )

    assert result is expected
    assert observed["args"] == (
        server.SETTINGS,
        "/relion/project",
        "/cryosparc/P1",
        "J42/particles.cs",
        ["J42/J42_passthrough_particles.cs"],
        "class3d-input",
    )


def test_cryosparc_particle_import_write_delegates_with_confirm(monkeypatch) -> None:
    expected = {"operation": "cryosparc_particle_import", "status": "published"}
    observed: dict[str, object] = {}

    def fake_import(*args):
        observed["args"] = args
        return expected

    monkeypatch.setattr(server, "import_cryosparc_particles", fake_import)

    result = server.relion_import_cryosparc_particles(
        "/relion/project",
        "/cryosparc/P1",
        "J42/particles.cs",
        ["J42/J42_passthrough_particles.cs"],
        "class3d-input",
        confirm=True,
    )

    assert result is expected
    assert observed["args"] == (
        server.SETTINGS,
        "/relion/project",
        "/cryosparc/P1",
        "J42/particles.cs",
        ["J42/J42_passthrough_particles.cs"],
        "class3d-input",
        True,
    )


def test_duplicate_removal_render_delegates_without_confirm(monkeypatch) -> None:
    expected = {"operation": "remove_duplicate_particles", "status": "rendered"}
    observed: dict[str, object] = {}

    def fake_render(*args):
        observed["args"] = args
        return expected

    monkeypatch.setattr(server.RUNNER, "render_remove_duplicate_particles", fake_render)

    result = server.relion_render_remove_duplicate_particles(
        "/project",
        "Extract/job007/particles.star",
        120.0,
        "Deduplicate/particles.star",
        1.31,
    )

    assert result is expected
    assert observed["args"] == (
        "/project",
        "Extract/job007/particles.star",
        120.0,
        "Deduplicate/particles.star",
        1.31,
    )


@pytest.mark.anyio
async def test_map_comparison_exposes_bounded_read_only_schema() -> None:
    tools_by_name = {tool.name: tool for tool in await mcp.list_tools()}
    tool = tools_by_name["relion_compare_maps"]
    properties = tool.input_schema["properties"]

    assert tool.title == "Align and compare RELION maps"
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.open_world_hint is False
    assert tool.input_schema["required"] == [
        "project_dir",
        "reference_map",
        "candidate_map",
        "reference_label",
        "candidate_label",
        "alignment_lowpass_A",
    ]
    assert properties["comparison_lowpasses_A"]["default"] is None
    assert properties["mask_path"]["default"] is None
    assert properties["alignment_mode"]["default"] == "rigid"
    assert properties["alignment_mode"]["enum"] == ["translation", "rigid"]
    assert properties["max_shift_A"]["default"] == 15.0
    assert properties["max_rotation_deg"]["default"] == 15.0


def test_projection_comparison_returns_image_first_and_forwards_search_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    observed: dict[str, object] = {}

    def fake_render(*args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        return server.RenderedVisual(
            png=png,
            metadata={
                "schema_version": "relion.map-projection-comparison/v1",
                "report_type": "relion_map_projection_comparison",
            },
        )

    monkeypatch.setattr(server, "render_map_projection_comparison", fake_render)
    monkeypatch.setattr(
        server, "register_visual_evidence_if_eligible", lambda *args, **kwargs: None
    )

    result = server.relion_compare_map_projections(
        "/project",
        "InitialModel/job021/run_it100_class002.mrc",
        "Class2D/job019/run_it100_model.star",
        [2, 6, 11],
        18.0,
        210.0,
        classes_mrcs="Class2D/job019/run_it100_classes.mrcs",
        direction_count=96,
        inplane_step_deg=15.0,
        max_shift_A=9.0,
        invert_class_contrast=True,
    )

    assert observed["args"] == (
        server.SETTINGS,
        "/project",
        "InitialModel/job021/run_it100_class002.mrc",
        "Class2D/job019/run_it100_model.star",
        [2, 6, 11],
        18.0,
        210.0,
    )
    assert observed["kwargs"] == {
        "classes_mrcs": "Class2D/job019/run_it100_classes.mrcs",
        "direction_count": 96,
        "inplane_step_deg": 15.0,
        "max_shift_A": 9.0,
        "invert_class_contrast": True,
    }
    assert [item.type for item in result.content] == ["image", "text"]
    assert result.content[0].mime_type == "image/png"
    assert base64.b64decode(result.content[0].data) == png
    metadata = json.loads(result.content[1].text)
    assert metadata["schema_version"] == "relion.map-projection-comparison/v1"
    assert metadata["report_type"] == "relion_map_projection_comparison"
    assert result.structured_content is None


def test_map_comparison_returns_image_first_without_structured_content(
    monkeypatch,
) -> None:
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    monkeypatch.setattr(
        server,
        "render_map_comparison",
        lambda *args, **kwargs: server.RenderedVisual(
            png=png,
            metadata={
                "schema_version": "relion.map-comparison/v1",
                "report_type": "relion_map_comparison",
            },
        ),
    )
    monkeypatch.setattr(
        server, "register_visual_evidence_if_eligible", lambda *args, **kwargs: None
    )

    result = server.relion_compare_maps(
        "/project",
        "reference.mrc",
        "candidate.mrc",
        "reference",
        "candidate",
        15.0,
    )

    assert len(result.content) == 2
    assert result.content[0].type == "image"
    assert result.content[0].mime_type == "image/png"
    assert base64.b64decode(result.content[0].data) == png
    assert result.content[1].type == "text"
    assert json.loads(result.content[1].text)["report_type"] == "relion_map_comparison"
    assert result.structured_content is None


def test_symmetry_scan_returns_image_first_without_structured_content(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    z, y, x = np.indices((16, 16, 16), dtype=np.float32)
    half_1 = np.exp(-((z - 7.0) ** 2 + (y - 8.0) ** 2 + (x - 8.0) ** 2) / 20.0)
    half_2 = np.exp(-((z - 8.0) ** 2 + (y - 8.0) ** 2 + (x - 7.0) ** 2) / 20.0)
    for name, data in (("half1.mrc", half_1), ("half2.mrc", half_2)):
        with mrcfile.new(project / name, overwrite=True) as handle:
            handle.set_data(np.asarray(data, dtype=np.float32))
            handle.voxel_size = 1.5

    module = types.ModuleType("relion_mcp.symmetry")

    class FakeSymmetryScanError(ValueError):
        pass

    observed: dict[str, object] = {}

    def fake_scan_symmetry(*args, **kwargs):
        observed.update(kwargs)
        return {
            "schema_version": "relion.symmetry-scan/v2",
            "status": "no_supported_candidate",
            "decision": "human_gate",
            "report_only": True,
            "selected_candidate": None,
            "candidates": [],
            "preprocessing": {"lowpass_before_decimation": True},
            "search": {"stage1_orders": list(range(2, 9)), "stage2_orders": []},
            "limitations": ["exploratory"],
        }

    module.SymmetryScanError = FakeSymmetryScanError
    module.SCHEMA_VERSION = "relion.symmetry-scan/v2"
    module.scan_symmetry = fake_scan_symmetry
    monkeypatch.setitem(sys.modules, "relion_mcp.symmetry", module)
    monkeypatch.setattr(
        server,
        "SETTINGS",
        Settings(
            mode="plan",
            project_roots=(tmp_path.resolve(),),
            pipeliner_executable="relion_pipeliner",
            image_handler_executable="relion_image_handler",
            pipeline_name="default",
            add_job_timeout_seconds=90,
            utility_timeout_seconds=900,
            visual_max_side_px=1400,
            visual_max_png_bytes=2_000_000,
            visual_max_panels=32,
        ),
    )

    result = server.relion_scan_symmetry(
        str(project),
        "half1.mrc",
        "half2.mrc",
        8.0,
        focus_groups=["C4"],
    )

    assert len(result.content) == 2
    assert result.content[0].type == "image"
    assert result.content[0].mime_type == "image/png"
    assert base64.b64decode(result.content[0].data).startswith(b"\x89PNG\r\n\x1a\n")
    assert result.content[1].type == "text"
    metadata = json.loads(result.content[1].text)
    assert metadata["schema_version"] == "relion.symmetry-scan/v2"
    assert metadata["report_type"] == "relion_symmetry_scan"
    assert observed["focus_groups"] == ["C4"]
    assert result.structured_content is None


@pytest.mark.anyio
async def test_smoke_tool_returns_image_first_without_structured_content() -> None:
    async def keep_event_loop_warm() -> None:
        # MCP 2.0's CPython 3.11 tracer-resync checkpoint can otherwise park
        # indefinitely when a direct-dispatch call leaves no scheduled wakeup.
        while True:
            await anyio.sleep(0.05)

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(keep_event_loop_warm)
        async with Client(mcp, raise_exceptions=True) as client:
            result = await client.call_tool("relion_visual_smoke_test", {})
        task_group.cancel_scope.cancel()

    assert result.is_error is not True
    assert len(result.content) == 2
    assert result.content[0].type == "image"
    assert result.content[0].mime_type == "image/png"
    assert base64.b64decode(result.content[0].data).startswith(b"\x89PNG\r\n\x1a\n")
    assert result.content[1].type == "text"
    assert '"report_type": "visual_smoke_test"' in result.content[1].text
    assert result.structured_content is None
