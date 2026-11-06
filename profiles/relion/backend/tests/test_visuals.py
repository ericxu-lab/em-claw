from __future__ import annotations

import json
import os
import shutil
import sys
import types
from pathlib import Path

import mrcfile
import numpy as np
import pytest
from PIL import Image
from scipy import ndimage

import relion_mcp.visuals as visuals_module
from relion_mcp.projection_compare import _project_volume
from relion_mcp.settings import Settings
from relion_mcp.visuals import (
    VisualError,
    metadata_text,
    render_2d_classes,
    render_3d_classes,
    render_fsc_report,
    render_hand_comparison,
    render_map_comparison,
    render_map_projection_comparison,
    render_map_report,
    render_mask_report,
    render_pick_overlays,
    render_smoke_test,
    render_symmetry_comparison,
    render_symmetry_scan,
)


def _settings(root: Path) -> Settings:
    return Settings(
        mode="plan",
        project_roots=(root.resolve(),),
        pipeliner_executable="relion_pipeliner",
        image_handler_executable="relion_image_handler",
        pipeline_name="default",
        add_job_timeout_seconds=90,
        utility_timeout_seconds=900,
        visual_max_side_px=1400,
        visual_max_png_bytes=2_000_000,
        visual_max_panels=32,
    )


def _write_mrc(
    path: Path,
    data: np.ndarray,
    voxel_size: float | tuple[float, float, float] = 1.5,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with mrcfile.new(path, overwrite=True) as handle:
        handle.set_data(np.asarray(data, dtype=np.float32))
        handle.voxel_size = voxel_size


def _gaussian_volume(size: int = 48, shift: tuple[float, float, float] = (0, 0, 0)) -> np.ndarray:
    z, y, x = np.indices((size, size, size), dtype=np.float32)
    center = (size - 1) / 2
    z0, y0, x0 = (center + value for value in shift)
    first = np.exp(-(((z - z0) / 7.0) ** 2 + ((y - y0) / 9.0) ** 2 + ((x - x0) / 11.0) ** 2) / 2)
    second = 0.62 * np.exp(
        -(((z - (z0 - 7)) / 4.0) ** 2 + ((y - (y0 + 6)) / 5.0) ** 2 + ((x - (x0 + 8)) / 6.0) ** 2) / 2
    )
    return np.asarray(first + second, dtype=np.float32)


def _assert_png(rendered, report_type: str) -> None:
    assert rendered.png.startswith(b"\x89PNG\r\n\x1a\n")
    assert rendered.metadata["report_type"] == report_type
    assert rendered.metadata["image"]["png_bytes"] == len(rendered.png)
    assert len(rendered.metadata["image"]["sha256"]) == 64
    with Image.open(__import__("io").BytesIO(rendered.png)) as image:
        assert image.width > 100
        assert image.height > 100
    parsed = json.loads(metadata_text(rendered))
    assert parsed["report_type"] == report_type


def test_display_lowpass_is_physical_and_preserves_the_input_array() -> None:
    size = 32
    voxel_size_A = 1.5
    x = np.arange(size, dtype=np.float32)
    low_mode = np.sin(2 * np.pi * 2 * x / size)
    high_mode = np.sin(2 * np.pi * 10 * x / size)
    volume = np.broadcast_to(low_mode + high_mode, (size, size, size)).copy()
    original = volume.copy()
    info = visuals_module._MrcInfo(
        path="synthetic.mrc",
        shape=volume.shape,
        dtype="float32",
        voxel_size_A=(voxel_size_A, voxel_size_A, voxel_size_A),
        origin_A=(0.0, 0.0, 0.0),
        axis_mapping=(1, 2, 3),
    )

    filtered, metadata = visuals_module._apply_display_lowpass(volume, info, 10.0)

    raw_amplitude = np.abs(np.fft.rfft(volume[0, 0]))
    filtered_amplitude = np.abs(np.fft.rfft(filtered[0, 0]))
    assert filtered_amplitude[2] / raw_amplitude[2] > 0.95
    assert filtered_amplitude[10] / raw_amplitude[10] < 0.01
    assert filtered.shape == volume.shape
    assert filtered.dtype == np.float32
    np.testing.assert_array_equal(volume, original)
    assert metadata["applied"] is True
    assert metadata["cutoff_resolution_A"] == 10.0
    assert metadata["filter_edge_width_fourier_shells"] == 2
    assert metadata["applied_before_downsampling"] is True
    assert metadata["display_only"] is True
    assert metadata["source_map_modified"] is False


def test_mrc_start_indices_are_remapped_to_physical_xyz(tmp_path: Path) -> None:
    path = tmp_path / "noncanonical.mrc"
    _write_mrc(path, np.zeros((5, 6, 7), dtype=np.float32), voxel_size=2.0)
    with mrcfile.open(path, mode="r+") as handle:
        handle.header.mapc = 2
        handle.header.mapr = 3
        handle.header.maps = 1
        handle.header.nxstart = 10
        handle.header.nystart = 20
        handle.header.nzstart = 30
    with mrcfile.open(path, mode="r") as handle:
        info = visuals_module._mrc_info(handle, path)

    assert info.axis_mapping == (2, 3, 1)
    assert info.start_column_row_section == (10, 20, 30)
    assert info.start_xyz == (30, 10, 20)


def test_display_lowpass_rejects_invalid_cutoffs_and_missing_sampling(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    volume = _gaussian_volume(size=24)
    _write_mrc(project / "Maps/valid.mrc", volume, voxel_size=1.5)
    settings = _settings(tmp_path)

    for invalid in (True, 0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(VisualError, match="finite positive Angstrom"):
            render_map_report(
                settings,
                str(project),
                "Maps/valid.mrc",
                display_lowpass_A=invalid,
            )

    with pytest.raises(VisualError, match="Nyquist"):
        render_map_report(
            settings,
            str(project),
            "Maps/valid.mrc",
            display_lowpass_A=2.5,
        )

    missing_sampling = project / "Maps/missing-sampling.mrc"
    with mrcfile.new(missing_sampling, overwrite=True) as handle:
        handle.set_data(volume)
    with pytest.raises(VisualError, match="voxel size metadata"):
        render_map_report(
            settings,
            str(project),
            "Maps/missing-sampling.mrc",
            display_lowpass_A=12.0,
        )


def test_visual_smoke_test_returns_png(tmp_path: Path) -> None:
    rendered = render_smoke_test(
        _settings(tmp_path),
        challenge_id="test-challenge",
        labels={
            "top_left": "ALPHA234",
            "top_right": "BRAVO567",
            "bottom_left": "CHARLIE8",
            "bottom_right": "DELTA999",
        },
    )
    _assert_png(rendered, "visual_smoke_test")
    assert rendered.metadata["challenge_id"] == "test-challenge"
    serialized = metadata_text(rendered)
    assert "ALPHA234" not in serialized


def test_2d_classes_and_pick_overlays(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    settings = _settings(tmp_path)

    size = 64
    yy, xx = np.indices((size, size), dtype=np.float32)
    stack = []
    for index in range(4):
        angle = index * np.pi / 7
        x0 = size / 2 + 3 * np.cos(angle)
        y0 = size / 2 + 3 * np.sin(angle)
        image = np.exp(-(((xx - x0) / (6 + index)) ** 2 + ((yy - y0) / (12 - index)) ** 2) / 2)
        image += 0.3 * np.exp(-(((xx - (x0 + 10)) / 4) ** 2 + ((yy - (y0 - 7)) / 4) ** 2) / 2)
        stack.append(image)
    _write_mrc(project / "Class2D/classes.mrcs", np.stack(stack))
    (project / "Class2D/run_model.star").write_text(
        """data_model_classes\n\nloop_\n_rlnReferenceImage #1\n_rlnClassDistribution #2\n_rlnEstimatedResolution #3\n_rlnClassRankerScore #4\n1@classes.mrcs 0.35 7.2 0.91\n2@classes.mrcs 0.30 7.8 0.84\n3@classes.mrcs 0.22 9.1 0.65\n4@classes.mrcs 0.13 12.4 0.31\n\n""",
        encoding="utf-8",
    )
    rendered = render_2d_classes(
        settings,
        str(project),
        "Class2D/run_model.star",
        max_classes=4,
        columns=2,
        sort_by="score",
    )
    _assert_png(rendered, "relion_2d_classes")
    assert [row["class_number"] for row in rendered.metadata["rendered_classes"]] == [1, 2, 3, 4]

    rng = np.random.default_rng(7)
    micro_y, micro_x = np.indices((256, 256), dtype=np.float32)
    micrograph = rng.normal(size=(256, 256)).astype(np.float32)
    micrograph += 2.2 * np.exp(-((micro_x - 128) ** 2 + (micro_y - 128) ** 2) / (2 * 28**2))
    _write_mrc(project / "Micrographs/mic001.mrc", micrograph)
    (project / "AutoPick/mic001.star").parent.mkdir(parents=True, exist_ok=True)
    (project / "AutoPick/mic001.star").write_text(
        """data_particles\n\nloop_\n_rlnCoordinateX #1\n_rlnCoordinateY #2\n_rlnAutopickFigureOfMerit #3\n55 60 0.80\n118 130 0.62\n190 180 0.20\n230 24 0.75\n\n""",
        encoding="utf-8",
    )
    rendered = render_pick_overlays(
        settings,
        str(project),
        pairs=[{"micrograph": "Micrographs/mic001.mrc", "coordinates": "AutoPick/mic001.star"}],
        particle_diameter_pix=42,
        fom_threshold=0.5,
        max_micrographs=1,
        columns=1,
    )
    _assert_png(rendered, "relion_pick_overlays")
    record = rendered.metadata["rendered_micrographs"][0]
    assert record["accepted_picks"] == 3
    assert record["rejected_picks"] == 1


def test_map_projection_comparison_preserves_noncontiguous_class_provenance(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    settings = _settings(tmp_path)
    pixel_size_A = 1.5
    volume = _gaussian_volume(size=32)
    map_path = project / "InitialModel/run_it100_class002.mrc"
    stack_path = project / "Class2D/run_it100_classes.mrcs"
    model_path = project / "Class2D/run_it100_model.star"
    _write_mrc(map_path, volume, voxel_size=pixel_size_A)

    class_stack = np.zeros((5, 32, 32), dtype=np.float32)
    class_stack[1] = _project_volume(
        volume, np.asarray((0.0, 1.0, 0.0), dtype=np.float64)
    )
    class_stack[3] = _project_volume(
        volume, np.asarray((0.0, 0.0, 1.0), dtype=np.float64)
    )
    class_stack[4] = _project_volume(
        volume, np.asarray((-1.0, 0.0, 0.0), dtype=np.float64)
    )
    # MRCS Z describes the stack axis, not image sampling. A deliberately
    # different value protects against regressing to 3D isotropy validation.
    _write_mrc(stack_path, class_stack, voxel_size=(pixel_size_A, pixel_size_A, 7.25))
    model_path.write_text(
        """data_model_general

_rlnOriginalImageSize 32
_rlnPixelSize 1.5

data_model_classes

loop_
_rlnReferenceImage #1
_rlnClassDistribution #2
_rlnEstimatedResolution #3
4@run_it100_classes.mrcs 0.40 7.1
2@run_it100_classes.mrcs 0.35 7.8
5@run_it100_classes.mrcs 0.25 8.4

""",
        encoding="utf-8",
    )
    source_bytes = {
        path: path.read_bytes() for path in (map_path, stack_path, model_path)
    }

    rendered = render_map_projection_comparison(
        settings,
        str(project),
        "InitialModel/run_it100_class002.mrc",
        "Class2D/run_it100_model.star",
        [3, 1],
        6.0,
        33.0,
        direction_count=24,
        inplane_step_deg=30.0,
        max_shift_A=6.0,
    )

    _assert_png(rendered, "relion_map_projection_comparison")
    metadata = rendered.metadata
    assert metadata["schema_version"] == "relion.map-projection-comparison/v1"
    assert len(metadata["analysis_id"]) == 64
    assert metadata["map"]["path"] == "InitialModel/run_it100_class002.mrc"
    assert len(metadata["map"]["sha256"]) == 64
    assert metadata["class_model"]["path"] == "Class2D/run_it100_model.star"
    assert metadata["class_model"]["total_rows"] == 3
    assert metadata["class_model"]["general_star_block"] == "model_general"
    assert metadata["class_model"]["declared_pixel_size_A"] == pixel_size_A
    assert metadata["class_model"]["declared_original_image_size_pix"] == 32
    assert metadata["class_stacks"] == [
        {
            "path": "Class2D/run_it100_classes.mrcs",
            "sha256": metadata["class_stacks"][0]["sha256"],
            "shape_stack_y_x": [5, 32, 32],
            "sampling_note": (
                "Only MRC X/Y sampling describes 2D class pixels; the stack-axis "
                "Z sampling is ignored."
            ),
        }
    ]
    physical = metadata["physical_compatibility"]
    assert physical["validated"] is True
    assert physical["map_pixel_size_A"] == pytest.approx(pixel_size_A)
    assert physical["class_pixel_size_A"] == pytest.approx(pixel_size_A)
    assert physical["common_field_of_view_A"] == pytest.approx(48.0)
    assert physical["analysis_box_size_pix"] == 32
    assert metadata["common_filter"]["map"]["cutoff_resolution_A"] == 6.0
    assert metadata["common_filter"]["class_averages"]["cutoff_resolution_A"] == 6.0
    assert metadata["search"]["direction_count"] == 24
    assert metadata["search"]["inplane_rotation_count"] == 12
    assert metadata["search"]["searched_max_shift_pix"] == 4
    assert metadata["resource_bounds"] == {
        "maximum_selected_classes": 12,
        "maximum_class_side_pix": 1024,
        "maximum_class_stack_file_bytes": 512 * 1024 * 1024,
        "maximum_map_file_bytes": 512 * 1024 * 1024,
    }
    matches = metadata["matches"]
    assert [match["class_number"] for match in matches] == [3, 1]
    assert [match["reference"] for match in matches] == [
        "5@run_it100_classes.mrcs",
        "4@run_it100_classes.mrcs",
    ]
    assert [match["stack_index"] for match in matches] == [5, 4]
    assert all(len(match["best_direction_xyz"]) == 3 for match in matches)
    assert all(match["displayed_pair_ncc"] > 0.8 for match in matches)
    pairwise = metadata["pairwise_best_direction_separations"]
    assert len(pairwise) == 1
    assert set(pairwise[0]) == {
        "class_a",
        "class_b",
        "best_viewing_direction_separation_deg",
    }
    acceptance = metadata["acceptance_use"]
    assert acceptance["selected_class_count"] == 2
    assert acceptance["minimum_required_distinct_2d_appearances"] == 3
    assert acceptance["count_requirement_met"] is False
    assert acceptance["automatic_match_verdict"] is False
    assert "orientation-matched synthetic projection" in metadata["scientific_note"]
    assert {path: path.read_bytes() for path in source_bytes} == source_bytes


def test_pick_overlay_rejects_mismatched_coordinate_columns(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write_mrc(project / "Micrographs/mic001.mrc", np.zeros((32, 32), dtype=np.float32))
    coordinates = project / "AutoPick/mic001.star"
    coordinates.parent.mkdir()
    coordinates.write_text(
        """data_particles

loop_
_rlnCoordinateX #1
5
12
_rlnCoordinateY 8
""",
        encoding="utf-8",
    )

    with pytest.raises(VisualError, match="Coordinate columns have different lengths"):
        render_pick_overlays(
            _settings(tmp_path),
            str(project),
            pairs=[
                {
                    "micrograph": "Micrographs/mic001.mrc",
                    "coordinates": "AutoPick/mic001.star",
                }
            ],
            particle_diameter_pix=12,
            max_micrographs=1,
            columns=1,
        )


def test_3d_map_mask_symmetry_and_hand_reports(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    settings = _settings(tmp_path)

    volume_a = _gaussian_volume()
    volume_b = _gaussian_volume(shift=(1.0, -1.0, 1.5))
    _write_mrc(project / "Class3D/class001.mrc", volume_a)
    _write_mrc(project / "Class3D/class002.mrc", volume_b)
    (project / "Class3D/run_model.star").write_text(
        """data_model_classes\n\nloop_\n_rlnReferenceImage #1\n_rlnClassDistribution #2\n_rlnEstimatedResolution #3\nclass001.mrc 0.64 8.2\nclass002.mrc 0.36 10.5\n\n""",
        encoding="utf-8",
    )

    mask = np.clip((volume_a - 0.035) / 0.12, 0.0, 1.0)
    mask = ndimage.gaussian_filter(mask, sigma=1.5)
    _write_mrc(project / "Mask/mask.mrc", mask)
    source_bytes = (project / "Class3D/class001.mrc").read_bytes()
    project_files = sorted(
        path.relative_to(project) for path in project.rglob("*") if path.is_file()
    )

    rendered = render_3d_classes(settings, str(project), "Class3D/run_model.star", max_classes=2)
    _assert_png(rendered, "relion_3d_classes")
    assert len(rendered.metadata["rendered_classes"]) == 2
    assert rendered.metadata["same_physical_grid_validated"] is True
    assert rendered.metadata["display"]["normalization_scope"] == "shared_across_rendered_classes"
    assert rendered.metadata["display"]["lowpass"]["applied"] is False
    assert rendered.metadata["display"]["projection"]["raw_maximum_projection_used"] is False
    assert len(rendered.metadata["pairwise_comparisons"]) == 1

    rendered = render_map_report(settings, str(project), "Class3D/class001.mrc")
    _assert_png(rendered, "relion_map_report")
    assert rendered.metadata["display"]["lowpass"]["applied"] is False
    assert rendered.metadata["display"]["projection"]["raw_maximum_projection_used"] is False

    rendered = render_3d_classes(
        settings,
        str(project),
        "Class3D/run_model.star",
        max_classes=2,
        display_lowpass_A=12.0,
    )
    _assert_png(rendered, "relion_3d_classes")
    lowpass = rendered.metadata["display"]["lowpass"]
    assert lowpass["applied"] is True
    assert lowpass["cutoff_resolution_A"] == 12.0
    assert lowpass["applied_before_downsampling"] is True
    assert all(
        record["metrics_volume"] == "display_lowpass"
        for record in rendered.metadata["rendered_classes"]
    )
    comparison = rendered.metadata["pairwise_comparisons"][0]
    assert comparison["common_physical_lowpass"]["cutoff_resolution_A"] == 12.0
    assert comparison["same_frame_unfiltered_similarity"] == {
        "pearson_correlation": comparison["pearson_correlation"],
        "zscore_rms_difference": comparison["zscore_rms_difference"],
    }

    rendered = render_map_report(
        settings,
        str(project),
        "Class3D/class001.mrc",
        display_lowpass_A=12.0,
    )
    _assert_png(rendered, "relion_map_report")
    assert rendered.metadata["metrics_volume"] == "display_lowpass"
    assert rendered.metadata["display"]["lowpass"]["cutoff_resolution_A"] == 12.0
    assert (project / "Class3D/class001.mrc").read_bytes() == source_bytes
    assert (
        sorted(path.relative_to(project) for path in project.rglob("*") if path.is_file())
        == project_files
    )

    rendered = render_mask_report(
        settings,
        str(project),
        "Class3D/class001.mrc",
        "Mask/mask.mrc",
    )
    _assert_png(rendered, "relion_mask_report")
    assert rendered.metadata["metrics_on_downsampled_volume"]["credible_density_fraction_outside_mask"] < 0.2

    rendered = render_symmetry_comparison(
        settings,
        str(project),
        {"C1": "Class3D/class001.mrc", "C2": "Class3D/class002.mrc"},
        reference_label="C1",
    )
    _assert_png(rendered, "relion_symmetry_comparison")
    assert len(rendered.metadata["candidates"]) == 2
    assert rendered.metadata["comparison_scope"] == "global_central_slice_triage_overview"
    assert rendered.metadata["display_contract"]["matched_absolute_contours"] is False
    assert rendered.metadata["required_inspection"] == [
        "density_present_in_C1_disappears",
        "side_chains_or_domains_are_smeared",
        "helices_are_split_or_distorted",
        "ligand_or_cofactor_density_is_weakened",
        "occupancy_appears_fractional",
        "distinct_conformations_are_averaged_together",
    ]

    rendered = render_hand_comparison(
        settings,
        str(project),
        "Class3D/class001.mrc",
        mirror_axis="x",
    )
    _assert_png(rendered, "relion_hand_comparison")
    assert rendered.metadata["generated_by_axis_reflection"] is True


def test_volume_comparisons_reject_physical_grid_mismatch(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    volume = _gaussian_volume(size=24)
    _write_mrc(project / "Maps/reference.mrc", volume, voxel_size=1.5)
    _write_mrc(project / "Maps/mismatch.mrc", volume, voxel_size=2.0)
    (project / "Maps/classes.star").write_text(
        """data_model_classes

loop_
_rlnReferenceImage #1
reference.mrc
mismatch.mrc

""",
        encoding="utf-8",
    )
    settings = _settings(tmp_path)

    comparisons = (
        lambda: render_3d_classes(
            settings,
            str(project),
            "Maps/classes.star",
            max_classes=2,
        ),
        lambda: render_mask_report(
            settings,
            str(project),
            "Maps/reference.mrc",
            "Maps/mismatch.mrc",
        ),
        lambda: render_symmetry_comparison(
            settings,
            str(project),
            {"C1": "Maps/reference.mrc", "C2": "Maps/mismatch.mrc"},
        ),
        lambda: render_hand_comparison(
            settings,
            str(project),
            "Maps/reference.mrc",
            opposite_hand_map="Maps/mismatch.mrc",
        ),
    )
    for comparison in comparisons:
        with pytest.raises(VisualError, match="identical physical grids"):
            comparison()


def _install_fake_symmetry_core(
    monkeypatch,
    observed: dict[str, object],
    *,
    result_schema_version: str = "relion.symmetry-scan/v2",
) -> None:
    module = types.ModuleType("relion_mcp.symmetry")

    class FakeSymmetryScanError(ValueError):
        pass

    def fake_scan_symmetry(half_1, half_2, **kwargs):
        observed["half_1"] = np.asarray(half_1).copy()
        observed["half_2"] = np.asarray(half_2).copy()
        observed["kwargs"] = kwargs
        return {
            "schema_version": result_schema_version,
            "status": "shortlist",
            "decision": "human_gate",
            "report_only": True,
            "selected_candidate": None,
            "candidates": [
                {
                    "group": "C5",
                    "family": "C",
                    "order": 5,
                    "stage": 1,
                    "axis_xyz": np.asarray([0.0, 0.0, 1.0]),
                    "frame_xyz": np.eye(3),
                    "half1_score": 0.81,
                    "half2_score": 0.79,
                    "cross_half_score": 0.76,
                    "agreement_score": 0.93,
                    "half_map_consistent": True,
                    "subgroups": [],
                    "rank": 1,
                },
                {
                    "group": "D2",
                    "family": "D",
                    "order": 2,
                    "stage": 1,
                    "axis_xyz": None,
                    "axis_frame_xyz": np.eye(3),
                    "orientation_matrix_xyz": np.eye(3),
                    "half1_score": 0.78,
                    "half2_score": 0.77,
                    "cross_half_score": 0.74,
                    "agreement_score": 0.90,
                    "half_map_consistent": True,
                    "subgroups": ["C2"],
                    "rank": 2,
                },
            ],
            "preprocessing": {
                "requested_analysis_lowpass_A": kwargs["analysis_resolution_A"],
                "effective_analysis_lowpass_A": 10.0,
                "lowpass_before_decimation": True,
                "analysis_shape_zyx": [12, 12, 12],
            },
            "search": {"stage1_orders": [2, 3, 4, 5, 6, 7, 8], "stage2_orders": []},
            "limitations": ["exploratory"],
        }

    module.SymmetryScanError = FakeSymmetryScanError
    module.SCHEMA_VERSION = "relion.symmetry-scan/v2"
    module.scan_symmetry = fake_scan_symmetry
    monkeypatch.setitem(sys.modules, "relion_mcp.symmetry", module)


def test_ambiguous_capped_symmetry_figure_never_implies_rejection() -> None:
    volumes = (
        np.zeros((12, 12, 12), dtype=np.float32),
        np.ones((12, 12, 12), dtype=np.float32),
    )
    candidates = [
        {
            "group": group,
            "half1_score": 0.7,
            "half2_score": 0.7,
            "cross_half_score": 0.7,
            "agreement_score": 0.7,
            "half_map_consistent": True,
        }
        for group in ("C32", "C31", "C30", "C29", "C28")
    ]
    focus_groups = ["C4", "D4", "C5", "D5", "C6", "D6", "C7", "D7"]
    focus_diagnostics = {
        group: {
            "support_confounds": (
                ["finite_order_confounded_by_high_flat_support_plateau"]
                if group == "C4"
                else []
            ),
            "support_failures": (
                ["perpendicular_c2_support_failed"] if group == "D4" else []
            ),
        }
        for group in focus_groups
    }
    interpretation = {
        "headline": "AMBIGUOUS SCAN HEURISTICS",
        "hypotheses_passing_scan_heuristics": 13,
        "detailed_candidates_shown": 5,
        "detailed_candidates_omitted": 8,
        "omitted_passing_groups": ["C4"],
        "focus_groups": focus_groups,
        "focus_diagnostics": focus_diagnostics,
        "high_order_control": {"state": "plateau_confounded"},
        "center": {
            "mode": "geometric_array_center",
            "validity": "not_validated",
        },
    }

    figure = visuals_module._symmetry_scan_figure(
        *volumes,
        candidates,
        requested_analysis_lowpass_A=12.0,
        effective_analysis_lowpass_A=25.3,
        status="ambiguous",
        search={"stage1_orders": list(range(2, 9)), "stage2_orders": [29, 30, 31, 32]},
        interpretation=interpretation,
        hypothesis_results={
            group: {
                "scan_heuristic_outcome": (
                    "confounded" if group == "C4" else "fail" if group == "D4" else "untestable"
                )
            }
            for group in focus_groups
        },
    )
    rendered_text = "\n".join(
        text.get_text()
        for axis in figure.axes
        for text in axis.texts
    )

    assert "Raw reproducibility passes: 13" in rendered_text
    assert "Order-specific support: unknown" in rendered_text
    assert "Detailed: 5 shown; 8 omitted" in rendered_text
    assert "High-order control: plateau confounded" in rendered_text
    assert "Focus raw C4: confounded [high-order plateau]" in rendered_text
    assert "Focus raw D4: fail [perpendicular-C2 gate failed]" in rendered_text
    assert all(f"Focus raw {group}:" in rendered_text for group in focus_groups)
    assert figure.axes[3].get_title() == "Detailed raw reproducibility passes"
    assert "Biological symmetry: UNRESOLVED." in rendered_text
    assert "This scan cannot accept or reject symmetry." in rendered_text
    assert "Absence from candidates is never failure." in rendered_text
    assert "No supported candidate" not in rendered_text


def test_inconclusive_symmetry_contract_is_not_rendered_as_a_shortlist() -> None:
    summary, _, _, interpretation = visuals_module._symmetry_report_contract(
        {"status": "inconclusive", "candidates": []},
        [],
        requested_analysis_lowpass_A=10.0,
        effective_analysis_lowpass_A=10.0,
        focus_groups=[],
        candidate_limit=5,
    )

    assert interpretation["headline"] == "SCAN INCONCLUSIVE"
    assert summary["ranking_outcome"] == "not_applicable"


def test_symmetry_contract_separates_raw_passes_from_order_specific_support() -> None:
    candidates = [
        {"group": "C4", "support_status": "inconclusive"},
        {"group": "C2", "support_status": "supported"},
    ]
    _, _, _, interpretation = visuals_module._symmetry_report_contract(
        {
            "status": "ambiguous",
            "candidates": candidates,
            "search": {
                "high_order_diagnostic": {
                    "assessable": True,
                    "monotonic_small_angle_ramp": False,
                    "high_flat_support_plateau": True,
                }
            },
            "hypothesis_results": {
                "C4": {
                    "group": "C4",
                    "scan_heuristic_outcome": "confounded",
                    "support_confounds": [
                        "finite_order_confounded_by_high_flat_support_plateau"
                    ],
                    "support_failures": [],
                }
            },
            "result_completeness": {
                "raw_scan_heuristic_pass_count": 14,
                "supported_candidate_count": 4,
                "displayed_candidate_count": 2,
                "displayed_supported_candidate_count": 1,
                "omitted_raw_scan_heuristic_pass_count": 12,
            },
        },
        candidates,
        requested_analysis_lowpass_A=10.0,
        effective_analysis_lowpass_A=10.0,
        focus_groups=["C4"],
        candidate_limit=5,
    )

    assert interpretation["raw_hypotheses_passing_scan_heuristics"] == 14
    assert interpretation["order_specific_supported_hypotheses"] == 4
    assert interpretation["detailed_candidates_shown"] == 2
    assert interpretation["detailed_candidates_omitted"] == 12
    assert interpretation["high_order_control"]["state"] == "plateau_confounded"
    assert interpretation["focus_diagnostics"]["C4"]["support_confounds"] == [
        "finite_order_confounded_by_high_flat_support_plateau"
    ]


def test_symmetry_contract_uses_core_precedence_when_ramp_and_plateau_overlap() -> None:
    _, _, _, interpretation = visuals_module._symmetry_report_contract(
        {
            "status": "ambiguous",
            "search": {
                "high_order_diagnostic": {
                    "assessable": True,
                    "monotonic_small_angle_ramp": True,
                    "high_flat_support_plateau": True,
                }
            },
        },
        [],
        requested_analysis_lowpass_A=10.0,
        effective_analysis_lowpass_A=10.0,
        focus_groups=[],
        candidate_limit=5,
    )

    assert interpretation["high_order_control"]["state"] == "ramp_confounded"


def test_symmetry_scan_renders_versioned_read_only_report(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    half_1 = _gaussian_volume(size=24)
    half_2 = _gaussian_volume(size=24, shift=(0.2, -0.1, 0.15))
    mask = np.clip(half_1 / float(np.max(half_1)), 0.0, 1.0)
    _write_mrc(project / "Refine3D/half1.mrc", half_1)
    _write_mrc(project / "Refine3D/half2.mrc", half_2)
    _write_mrc(project / "Mask/mask.mrc", mask)
    before_files = sorted(
        path.relative_to(project) for path in project.rglob("*") if path.is_file()
    )
    before_bytes = {
        path: (project / path).read_bytes()
        for path in before_files
    }
    observed: dict[str, object] = {}
    _install_fake_symmetry_core(monkeypatch, observed)

    rendered = render_symmetry_scan(
        _settings(tmp_path),
        str(project),
        "Refine3D/half1.mrc",
        "Refine3D/half2.mrc",
        8.0,
        mask_path="Mask/mask.mrc",
        common_max_order=8,
        max_order=32,
        stage2_window=2,
        max_candidates=5,
        focus_groups=["c05"],
    )

    _assert_png(rendered, "relion_symmetry_scan")
    metadata = rendered.metadata
    assert metadata["schema_version"] == "relion.symmetry-scan/v2"
    assert metadata["status"] == "shortlist"
    assert metadata["decision"] == "defer"
    assert metadata["human_input_required"] is False
    assert metadata["report_only"] is True
    assert metadata["selected_candidate"] is None
    assert metadata["candidates"][0]["group"] == "C5"
    assert len(metadata["candidates"][1]["axis_frame_lines"]) == 3
    assert all(
        line["directly_executable_relion_transform"] is False
        for line in metadata["candidates"][1]["axis_frame_lines"]
    )
    assert metadata["inputs"]["half_map_1"] == "Refine3D/half1.mrc"
    assert len(metadata["inputs"]["half_map_1_sha256"]) == 64
    assert len(metadata["inputs"]["half_map_2_sha256"]) == 64
    assert len(metadata["inputs"]["mask"]["sha256"]) == 64
    assert metadata["physical_grid"]["identical_grid_validated"] is True
    assert metadata["physical_grid"]["canonical_input_shape_zyx"] == [24, 24, 24]
    assert metadata["physical_grid"]["analysis_shape_zyx"] == [24, 24, 24]
    assert metadata["preprocessing"]["analysis_shape_zyx"] == [12, 12, 12]
    assert metadata["coordinate_frame"]["analysis_numpy_axis_order"] == [
        "physical_Z",
        "physical_Y",
        "physical_X",
    ]
    contract = metadata["analysis_contract"]
    assert contract["requested_analysis_lowpass_A"] == 8.0
    assert contract["effective_analysis_lowpass_A"] == 10.0
    assert contract["common_lowpass_before_decimation"] is True
    assert contract["source_maps_modified"] is False
    assert observed["kwargs"]["analysis_resolution_A"] == 8.0
    assert observed["kwargs"]["stage2_window"] == 2
    assert observed["kwargs"]["focus_groups"] == ["c05"]
    assert metadata["agent_interpretation"]["focus_groups"] == ["C5"]
    assert "C5" in metadata["hypothesis_results"]
    assert "c5" not in metadata["hypothesis_results"]
    assert np.asarray(observed["half_1"]).shape == half_1.shape
    assert json.loads(metadata_text(rendered))["candidates"][0]["axis_xyz"] == [0.0, 0.0, 1.0]
    after_files = sorted(
        path.relative_to(project) for path in project.rglob("*") if path.is_file()
    )
    assert after_files == before_files
    assert all((project / path).read_bytes() == before_bytes[path] for path in before_files)


def test_symmetry_scan_rejects_inconsistent_core_schema(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    half_1 = _gaussian_volume(size=16)
    half_2 = _gaussian_volume(size=16, shift=(0.2, -0.1, 0.15))
    _write_mrc(project / "Maps/half1.mrc", half_1)
    _write_mrc(project / "Maps/half2.mrc", half_2)
    _install_fake_symmetry_core(
        monkeypatch,
        {},
        result_schema_version="relion.symmetry-scan/unexpected",
    )

    with pytest.raises(VisualError, match="inconsistent schema version"):
        render_symmetry_scan(
            _settings(tmp_path),
            str(project),
            "Maps/half1.mrc",
            "Maps/half2.mrc",
            8.0,
        )


def test_symmetry_scan_binds_real_numerical_core(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    half_1 = _gaussian_volume(size=12)
    half_2 = np.asarray(half_1 + 0.02 * np.roll(half_1, 1, axis=0), dtype=np.float32)
    _write_mrc(project / "Maps/half1.mrc", half_1)
    _write_mrc(project / "Maps/half2.mrc", half_2)

    rendered = render_symmetry_scan(
        _settings(tmp_path),
        str(project),
        "Maps/half1.mrc",
        "Maps/half2.mrc",
        8.0,
        common_max_order=2,
        max_order=2,
        stage2_window=1,
        max_candidates=2,
    )

    _assert_png(rendered, "relion_symmetry_scan")
    assert rendered.metadata["core_schema_version"] == "relion.symmetry-scan/v2"
    assert rendered.metadata["preprocessing"]["lowpass_before_decimation"] is True
    assert rendered.metadata["search"]["stage1_orders"] == [2]
    assert rendered.metadata["search"]["stage2_orders"] == []


def test_symmetry_scan_rejects_unsafe_or_invalid_inputs(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    valid = _gaussian_volume(size=16)
    constant = np.ones((16, 16, 16), dtype=np.float32)
    nonfinite = valid.copy()
    nonfinite[0, 0, 0] = np.nan
    bad_mask = np.full((16, 16, 16), 1.2, dtype=np.float32)
    _write_mrc(project / "Maps/valid1.mrc", valid)
    _write_mrc(project / "Maps/valid2.mrc", valid + 0.01 * np.roll(valid, 1, axis=0))
    _write_mrc(project / "Maps/constant.mrc", constant)
    with pytest.warns(RuntimeWarning, match="contains NaN values"):
        _write_mrc(project / "Maps/nonfinite.mrc", nonfinite)
    _write_mrc(project / "Maps/anisotropic.mrc", valid, voxel_size=(1.5, 1.5, 1.8))
    _write_mrc(project / "Maps/start-mismatch.mrc", valid + 0.02 * np.roll(valid, 1, axis=1))
    with mrcfile.open(project / "Maps/start-mismatch.mrc", mode="r+") as handle:
        handle.header.nxstart = 1
    _write_mrc(project / "Mask/bad.mrc", bad_mask)
    with mrcfile.new(project / "Maps/complex.mrc", overwrite=True) as handle:
        handle.set_data(
            np.asarray(valid + 1j * np.roll(valid, 1, axis=0), dtype=np.complex64)
        )
        handle.voxel_size = 1.5
    os.link(project / "Maps/valid1.mrc", project / "Maps/hardlink.mrc")
    shutil.copyfile(project / "Maps/valid1.mrc", project / "Maps/copy.mrc")
    settings = _settings(tmp_path)

    with pytest.raises(VisualError, match="different filesystem identities"):
        render_symmetry_scan(settings, str(project), "Maps/valid1.mrc", "Maps/valid1.mrc", 8.0)
    with pytest.raises(VisualError, match="different filesystem identities"):
        render_symmetry_scan(
            settings,
            str(project),
            "Maps/valid1.mrc",
            "Maps/hardlink.mrc",
            8.0,
        )
    with pytest.raises(VisualError, match="identical file content"):
        render_symmetry_scan(
            settings,
            str(project),
            "Maps/valid1.mrc",
            "Maps/copy.mrc",
            8.0,
        )
    with pytest.raises(VisualError, match="isotropic voxel sampling"):
        render_symmetry_scan(
            settings,
            str(project),
            "Maps/valid1.mrc",
            "Maps/anisotropic.mrc",
            8.0,
        )
    with pytest.raises(VisualError, match="start indices"):
        render_symmetry_scan(
            settings,
            str(project),
            "Maps/valid1.mrc",
            "Maps/start-mismatch.mrc",
            8.0,
        )
    with pytest.raises(VisualError, match="non-constant"):
        render_symmetry_scan(
            settings,
            str(project),
            "Maps/valid1.mrc",
            "Maps/constant.mrc",
            8.0,
        )
    with pytest.raises(VisualError, match="non-finite"):
        render_symmetry_scan(
            settings,
            str(project),
            "Maps/valid1.mrc",
            "Maps/nonfinite.mrc",
            8.0,
        )
    with pytest.raises(VisualError, match="real-valued density"):
        render_symmetry_scan(
            settings,
            str(project),
            "Maps/valid1.mrc",
            "Maps/complex.mrc",
            8.0,
        )
    with pytest.raises(VisualError, match=r"within \[0, 1\]"):
        render_symmetry_scan(
            settings,
            str(project),
            "Maps/valid1.mrc",
            "Maps/valid2.mrc",
            8.0,
            mask_path="Mask/bad.mrc",
        )


def test_symmetry_scan_enforces_analysis_memory_cap(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    volume = _gaussian_volume(size=16)
    _write_mrc(project / "Maps/half1.mrc", volume)
    _write_mrc(project / "Maps/half2.mrc", np.roll(volume, 1, axis=0))
    monkeypatch.setattr(visuals_module, "_SYMMETRY_SCAN_MAX_ESTIMATED_BYTES", 1)

    with pytest.raises(VisualError, match="analysis-memory estimate"):
        render_symmetry_scan(
            _settings(tmp_path),
            str(project),
            "Maps/half1.mrc",
            "Maps/half2.mrc",
            8.0,
        )


def test_map_metrics_separate_a_dominant_object_from_tiny_speckles() -> None:
    volume = np.zeros((40, 40, 40), dtype=np.float32)
    volume[14:26, 14:26, 14:26] = 7.0
    speckles = [
        (2, y, x)
        for y, x in (
            (2, 2),
            (2, 6),
            (2, 10),
            (2, 30),
            (2, 34),
            (2, 38),
            (6, 2),
            (6, 6),
            (6, 10),
            (6, 30),
            (6, 34),
            (6, 38),
            (10, 2),
            (10, 6),
            (10, 10),
        )
    ]
    for coordinate in speckles:
        volume[coordinate] = 7.0

    metrics = visuals_module._map_metrics(volume, threshold_sigma=2.0)

    assert metrics["component_connectivity"] == 26
    assert metrics["connected_components_at_threshold"] == 1 + len(speckles)
    assert metrics["largest_component_fraction"] > 0.99
    assert metrics["largest_component_fraction_of_thresholded_density"] > 0.99
    assert metrics["components_covering_95_percent"] == 1
    assert metrics["significant_components_at_threshold"] == 1
    assert metrics["zero_fraction"] > 0.95


def test_map_metrics_use_corner_connectivity() -> None:
    volume = np.zeros((8, 8, 8), dtype=np.float32)
    volume[3, 3, 3] = 10.0
    volume[4, 4, 4] = 10.0

    metrics = visuals_module._map_metrics(volume, threshold_sigma=2.0)

    assert metrics["connected_components_at_threshold"] == 1
    assert metrics["largest_component_fraction"] == 1.0


def test_explicit_3d_class_maps_preserve_model_star_metadata(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    volume_a = _gaussian_volume(size=24)
    volume_b = _gaussian_volume(size=24, shift=(1.0, -1.0, 1.5))
    _write_mrc(project / "Class3D/class001.mrc", volume_a)
    _write_mrc(project / "Class3D/class002.mrc", volume_b)
    (project / "Class3D/run_model.star").write_text(
        """data_model_classes

loop_
_rlnReferenceImage #1
_rlnClassDistribution #2
_rlnEstimatedResolution #3
missing-class001.mrc 0.61 9.2
missing-class002.mrc 0.39 11.4

""",
        encoding="utf-8",
    )

    rendered = render_3d_classes(
        _settings(tmp_path),
        str(project),
        "Class3D/run_model.star",
        class_maps=["Class3D/class001.mrc", "Class3D/class002.mrc"],
        max_classes=2,
    )

    _assert_png(rendered, "relion_3d_classes")
    assert rendered.metadata["star_block"] == "model_classes"
    records = rendered.metadata["rendered_classes"]
    assert [record["occupancy"] for record in records] == [0.61, 0.39]
    assert [record["estimated_resolution_A"] for record in records] == [9.2, 11.4]
    assert records[0]["reference"] == "missing-class001.mrc"
    assert records[0]["metrics_on_downsampled_volume"]["component_connectivity"] == 26
    comparison = rendered.metadata["pairwise_comparisons"][0]
    assert comparison["class_a"] == 1
    assert comparison["class_b"] == 2
    assert comparison["pearson_correlation"] is not None
    assert comparison["common_gaussian_filter"]["similarity"]["pearson_correlation"] is not None


def test_map_comparison_aligns_in_memory_and_records_one_evidence_root(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    reference = _gaussian_volume(size=30)
    candidate = ndimage.shift(
        reference,
        shift=(2.0, -1.5, 1.0),
        order=1,
        mode="constant",
        cval=0.0,
        prefilter=False,
    )
    reference_path = project / "Refine3D/reference.mrc"
    candidate_path = project / "Refine3D/candidate.mrc"
    _write_mrc(reference_path, reference)
    _write_mrc(candidate_path, candidate)
    with mrcfile.open(candidate_path, mode="r+") as handle:
        handle.header.origin.x = 4.5
    source_bytes = {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
    }

    rendered = render_map_comparison(
        _settings(tmp_path),
        str(project),
        "Refine3D/reference.mrc",
        "Refine3D/candidate.mrc",
        "Class002 refined",
        "Class003 refined",
        8.0,
        comparison_lowpasses_A=[8.0, 5.0],
        alignment_mode="translation",
        max_shift_A=7.5,
    )

    _assert_png(rendered, "relion_map_comparison")
    assert rendered.metadata["schema_version"] == "relion.map-comparison/v1"
    assert rendered.metadata["status"] == "aligned"
    assert rendered.metadata["physical_grid"]["header_origin_matches"] is False
    assert rendered.metadata["physical_grid"]["identical_header_frame_required"] is False
    assert rendered.metadata["provenance"]["source_maps_modified"] is False
    assert rendered.metadata["provenance"]["project_files_created"] is False
    assert rendered.metadata["provenance"]["aligned_map_materialized"] is False
    assert rendered.metadata["provenance"]["report_only"] is True
    assert rendered.metadata["provenance"][
        "source_snapshot_verified_before_and_after_analysis"
    ] is True
    comparisons = rendered.metadata["metrics"]["comparisons"]
    before = comparisons[0]["before_alignment"]["similarity"]["pearson_correlation"]
    after = comparisons[0]["after_alignment"]["similarity"]["pearson_correlation"]
    assert before is not None and after is not None and after > before
    assert rendered.metadata["evidence"]["root_source"].startswith(
        "map_pixels_pair_sha256:"
    )
    assert rendered.metadata["evidence"]["analysis_id"].startswith(
        "map_comparison_analysis_sha256:"
    )
    assert rendered.metadata["display"]["rendered_cutoff_resolutions_A"] == [8.0, 5.0]
    assert rendered.metadata["reference"]["canonical_voxel_sha256"]
    assert rendered.metadata["candidate"]["canonical_voxel_sha256"]
    assert "translation_centered_grid_xyz_A" in rendered.metadata["alignment"][
        "transform"
    ]
    assert "translation_physical_xyz_A" not in rendered.metadata["alignment"][
        "transform"
    ]
    assert "correlated derivatives" in rendered.metadata["evidence"]["independence_note"]
    assert source_bytes == {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
    }


def test_map_comparison_pixel_root_ignores_header_only_changes(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    reference = _gaussian_volume(size=24)
    candidate = ndimage.shift(
        reference,
        shift=(1.0, -1.0, 0.5),
        order=1,
        mode="constant",
        cval=0.0,
        prefilter=False,
    )
    _write_mrc(project / "Maps/reference.mrc", reference)
    _write_mrc(project / "Maps/candidate.mrc", candidate)
    _write_mrc(project / "Maps/header_clone.mrc", candidate)
    with mrcfile.open(project / "Maps/header_clone.mrc", mode="r+") as handle:
        handle.header.origin.x = 12.0
        handle.header.nxstart = 7

    first = render_map_comparison(
        _settings(tmp_path),
        str(project),
        "Maps/reference.mrc",
        "Maps/candidate.mrc",
        "reference",
        "candidate",
        8.0,
        alignment_mode="translation",
        max_shift_A=5.0,
        max_rotation_deg=0.0,
    )
    clone = render_map_comparison(
        _settings(tmp_path),
        str(project),
        "Maps/reference.mrc",
        "Maps/header_clone.mrc",
        "reference",
        "header clone",
        8.0,
        alignment_mode="translation",
        max_shift_A=5.0,
        max_rotation_deg=0.0,
    )

    assert first.metadata["candidate"]["sha256"] != clone.metadata["candidate"]["sha256"]
    assert (
        first.metadata["candidate"]["canonical_voxel_sha256"]
        == clone.metadata["candidate"]["canonical_voxel_sha256"]
    )
    assert first.metadata["evidence"]["root_source"] == clone.metadata["evidence"][
        "root_source"
    ]
    assert first.metadata["evidence"]["analysis_id"] == clone.metadata["evidence"][
        "analysis_id"
    ]


def test_map_comparison_rejects_complex_mrc_before_float_conversion(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    reference = _gaussian_volume(size=16)
    _write_mrc(project / "Maps/reference.mrc", reference)
    complex_path = project / "Maps/complex.mrc"
    complex_path.parent.mkdir(parents=True, exist_ok=True)
    with mrcfile.new(complex_path, overwrite=True) as handle:
        handle.set_data(np.asarray(reference + 1j * reference, dtype=np.complex64))
        handle.voxel_size = 1.5

    with pytest.raises(VisualError, match="real-valued density"):
        render_map_comparison(
            _settings(tmp_path),
            str(project),
            "Maps/reference.mrc",
            "Maps/complex.mrc",
            "reference",
            "complex",
            8.0,
            alignment_mode="translation",
            max_shift_A=4.0,
            max_rotation_deg=0.0,
        )


def test_map_comparison_analysis_id_includes_physical_sampling(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    density = _gaussian_volume(size=24)
    for sampling, directory in ((1.0, "one_A"), (2.0, "two_A")):
        _write_mrc(project / directory / "reference.mrc", density, sampling)
        _write_mrc(project / directory / "candidate.mrc", density, sampling)

    reports = [
        render_map_comparison(
            _settings(tmp_path),
            str(project),
            f"{directory}/reference.mrc",
            f"{directory}/candidate.mrc",
            "reference",
            "candidate",
            8.0,
            comparison_lowpasses_A=[6.0],
            alignment_mode="translation",
            max_shift_A=5.0,
            max_rotation_deg=0.0,
        )
        for directory in ("one_A", "two_A")
    ]

    assert reports[0].metadata["evidence"]["root_source"] == reports[1].metadata[
        "evidence"
    ]["root_source"]
    assert reports[0].metadata["evidence"]["analysis_id"] != reports[1].metadata[
        "evidence"
    ]["analysis_id"]


def test_generated_hand_reflects_the_selected_off_center_crop(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    volume = np.arange(16**3, dtype=np.float32).reshape((16, 16, 16))
    _write_mrc(project / "Maps/asymmetric.mrc", volume)
    observed: list[np.ndarray] = []
    real_downsample = visuals_module._downsample_volume

    def capture_downsample(data: np.ndarray, max_side: int = 192) -> np.ndarray:
        observed.append(np.asarray(data).copy())
        return real_downsample(data, max_side=max_side)

    monkeypatch.setattr(visuals_module, "_downsample_volume", capture_downsample)

    rendered = render_hand_comparison(
        _settings(tmp_path),
        str(project),
        "Maps/asymmetric.mrc",
        mirror_axis="x",
        center_zyx=[8, 8, 4],
        box_size_pix=8,
    )

    _assert_png(rendered, "relion_hand_comparison")
    assert len(observed) == 2
    np.testing.assert_array_equal(observed[1], np.flip(observed[0], axis=2))


def test_fsc_report(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    settings = _settings(tmp_path)
    frequencies = np.linspace(0.01, 0.34, 40)
    corrected = np.exp(-((frequencies / 0.20) ** 3))
    masked = np.clip(corrected + 0.04, -0.1, 1.0)
    unmasked = np.clip(corrected - 0.06, -0.1, 1.0)
    phase = np.clip(0.02 * np.cos(frequencies * 60), -0.05, 0.05)
    lines = [
        "data_general",
        "",
        "_rlnFinalResolution 4.1",
        "",
        "data_fsc",
        "",
        "loop_",
        "_rlnResolution #1",
        "_rlnFourierShellCorrelationCorrected #2",
        "_rlnFourierShellCorrelationMaskedMaps #3",
        "_rlnFourierShellCorrelationUnmaskedMaps #4",
        "_rlnFourierShellCorrelationPhaseRandomizedMaskedMaps #5",
    ]
    lines.extend(
        f"{frequency:.6f} {a:.6f} {b:.6f} {c:.6f} {d:.6f}"
        for frequency, a, b, c, d in zip(frequencies, corrected, masked, unmasked, phase, strict=True)
    )
    (project / "PostProcess").mkdir()
    (project / "PostProcess/postprocess.star").write_text("\n".join(lines) + "\n", encoding="utf-8")

    rendered = render_fsc_report(settings, str(project), "PostProcess/postprocess.star")
    _assert_png(rendered, "relion_fsc_report")
    assert rendered.metadata["reported_final_resolution_A"] == 4.1
    assert rendered.metadata["curves"] == [
        "corrected",
        "masked",
        "unmasked",
        "phase-randomised masked",
    ]
    assert rendered.metadata["threshold_estimates"]["corrected"] == pytest.approx(
        {
            "resolution_at_0_143_A": 4.0036554551,
            "resolution_at_0_5_A": 5.64969234345,
        }
    )
    assert rendered.metadata["phase_randomised_fsc_near_reported_resolution"] == pytest.approx(
        -0.012548
    )
    scientific_note = rendered.metadata["scientific_note"]
    assert "not sufficient evidence of improvement" in scientific_note
    assert "masking artefacts" in scientific_note
    assert "visual deterioration" in scientific_note
