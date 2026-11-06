from __future__ import annotations

import numpy as np
import pytest
from scipy import ndimage

from relion_mcp.map_compare import (
    MapComparisonError,
    _effective_alignment_resolution_A,
    _transform_candidate,
    compare_maps,
    validate_map_comparison_resources,
)


def _asymmetric_volume(size: int = 30) -> np.ndarray:
    z, y, x = np.indices((size, size, size), dtype=np.float32)
    return np.asarray(
        np.exp(-((z - 9) ** 2 / 15 + (y - 14) ** 2 / 24 + (x - 18) ** 2 / 10))
        + 0.72
        * np.exp(-((z - 21) ** 2 / 9 + (y - 9) ** 2 / 14 + (x - 7) ** 2 / 18))
        + 0.33
        * np.exp(-((z - 17) ** 2 / 7 + (y - 22) ** 2 / 10 + (x - 22) ** 2 / 6)),
        dtype=np.float32,
    )


def test_rigid_registration_improves_fixed_cutoff_correlations() -> None:
    reference = _asymmetric_volume()
    candidate = _transform_candidate(
        reference,
        (5.0, -3.0, 4.0),
        (1.8, -1.4, 0.9),
        order=1,
        cval=0.0,
    )
    reference_original = reference.copy()
    candidate_original = candidate.copy()

    result = compare_maps(
        reference,
        candidate,
        voxel_size_A=1.5,
        alignment_lowpass_A=8.0,
        comparison_lowpasses_A=[8.0, 5.0],
        max_shift_A=7.5,
        max_rotation_deg=10.0,
    )

    assert result.metadata["status"] == "aligned"
    assert result.metadata["alignment"]["transform"]["direction"] == (
        "candidate_density_to_reference_grid"
    )
    assert result.metadata["metrics"]["fixed_transform_applied_to_every_cutoff"] is True
    assert len(result.cutoff_views) == 2
    validation = result.metadata["alignment"]["registration_validation"]
    assert validation["performed"] is True
    assert validation["pose_consensus_passed"] is True
    assert validation["held_out_generalization_passed"] is True
    assert validation["selected_transform_refit_or_replaced"] is False
    comparisons = result.metadata["metrics"]["comparisons"]
    assert [item["cutoff_resolution_A"] for item in comparisons] == [8.0, 5.0]
    for item in comparisons:
        before = item["before_alignment"]["similarity"]["pearson_correlation"]
        after = item["after_alignment"]["similarity"]["pearson_correlation"]
        assert before is not None and after is not None
        assert after > before + 0.15
        assert after > 0.99
        assert item["transform_refit"] is False
    np.testing.assert_array_equal(reference, reference_original)
    np.testing.assert_array_equal(candidate, candidate_original)


def test_translation_sign_is_recovered_from_independent_scipy_shift() -> None:
    reference = _asymmetric_volume(size=28)
    applied_zyx = np.asarray((2.0, -1.5, 1.0))
    candidate = np.asarray(
        ndimage.shift(
            reference,
            shift=applied_zyx,
            order=1,
            mode="constant",
            cval=0.0,
            prefilter=False,
        ),
        dtype=np.float32,
    )

    result = compare_maps(
        reference,
        candidate,
        voxel_size_A=1.5,
        alignment_lowpass_A=8.0,
        alignment_mode="translation",
        max_shift_A=6.0,
        max_rotation_deg=0.0,
    )

    fitted_zyx = np.asarray(
        result.metadata["alignment"]["transform"][
            "translation_full_grid_zyx_pixels"
        ]
    )
    np.testing.assert_allclose(fitted_zyx, -applied_zyx, atol=0.08)
    homogeneous = np.asarray(
        result.metadata["alignment"]["transform"][
            "homogeneous_candidate_index_xyz_to_reference_index_xyz"
        ]
    )
    np.testing.assert_allclose(homogeneous[:3, 3], (-applied_zyx)[::-1], atol=0.08)


def test_tiny_arbitrary_density_scale_is_not_treated_as_constant() -> None:
    reference = _asymmetric_volume(size=24) * np.float32(1e-8)
    candidate = np.asarray(
        ndimage.shift(
            reference,
            shift=(1.0, 0.0, 0.0),
            order=1,
            mode="constant",
            cval=0.0,
            prefilter=False,
        ),
        dtype=np.float32,
    )

    result = compare_maps(
        reference,
        candidate,
        voxel_size_A=1.5,
        alignment_lowpass_A=8.0,
        alignment_mode="translation",
        max_shift_A=5.0,
        max_rotation_deg=0.0,
    )

    assert result.metadata["status"] == "aligned"
    assert result.metadata["metrics"]["comparisons"][0]["after_alignment"][
        "similarity"
    ]["pearson_correlation"] > 0.99


def test_analysis_cutoff_keeps_filter_transition_below_decimated_nyquist() -> None:
    shape = (75, 83, 97)
    voxel_size_A = 1.5
    stride = 2
    effective_A, safe_minimum_A = _effective_alignment_resolution_A(
        shape,
        voxel_size_A,
        4.0,
        stride,
    )
    source_shell = min(1.0 / (size * voxel_size_A) for size in shape)
    coarse_nyquist = 1.0 / (2.0 * voxel_size_A * stride)

    assert effective_A == safe_minimum_A
    assert 1.0 / effective_A + source_shell <= coarse_nyquist + 1e-12


def test_resource_preflight_and_tiny_mask_fail_before_registration() -> None:
    with pytest.raises(MapComparisonError, match="voxel cap"):
        validate_map_comparison_resources((1000, 1000, 1000))

    reference = _asymmetric_volume(size=20)
    mask = np.zeros_like(reference)
    mask[10, 10, 10] = 1.0
    with pytest.raises(MapComparisonError, match="effective sample size"):
        compare_maps(
            reference,
            reference.copy(),
            voxel_size_A=1.5,
            alignment_lowpass_A=8.0,
            mask=mask,
            max_shift_A=5.0,
        )


def test_unrelated_smoothed_maps_are_not_reported_as_aligned() -> None:
    generator = np.random.default_rng(0)
    reference = ndimage.gaussian_filter(
        generator.normal(size=(20, 20, 20)).astype(np.float32),
        2.0,
    )
    candidate = ndimage.gaussian_filter(
        generator.normal(size=(20, 20, 20)).astype(np.float32),
        2.0,
    )

    result = compare_maps(
        reference,
        candidate,
        voxel_size_A=1.5,
        alignment_lowpass_A=8.0,
        alignment_mode="translation",
        max_shift_A=5.0,
        max_rotation_deg=0.0,
    )

    assert result.metadata["status"] in {
        "unstable_alignment",
        "fit_unverified",
        "ambiguous_alignment",
        "boundary_limited",
        "insufficient_overlap",
        "optimizer_stopped",
    }
    assert result.metadata["status"] != "aligned"


def test_exact_identity_is_selected_without_validation_refits() -> None:
    reference = _asymmetric_volume(size=22)

    result = compare_maps(
        reference,
        reference.copy(),
        voxel_size_A=1.5,
        alignment_lowpass_A=8.0,
        alignment_mode="translation",
        max_shift_A=5.0,
        max_rotation_deg=0.0,
    )

    assert result.metadata["status"] == "identity_optimal"
    assert result.metadata["alignment"]["optimizer"]["exact_identity_evaluated"] is True
    assert result.metadata["alignment"]["registration_validation"] == {
        "performed": False,
        "reason": "exact_canonical_voxel_identity",
        "pose_consensus_passed": True,
        "held_out_generalization_passed": True,
        "identity_validation_basis": "exact_equal_canonical_float32_voxels",
    }


def test_tiny_search_bound_cannot_force_unrelated_maps_to_identity() -> None:
    generator = np.random.default_rng(4)
    reference = ndimage.gaussian_filter(
        generator.normal(size=(20, 20, 20)).astype(np.float32),
        2.0,
    )
    candidate = ndimage.gaussian_filter(
        generator.normal(size=(20, 20, 20)).astype(np.float32),
        2.0,
    )

    with pytest.raises(MapComparisonError, match="too small to test identity"):
        compare_maps(
            reference,
            candidate,
            voxel_size_A=1.5,
            alignment_lowpass_A=8.0,
            alignment_mode="translation",
            max_shift_A=0.005,
            max_rotation_deg=0.0,
        )


def test_nonidentical_identity_candidate_cannot_override_pose_ambiguity() -> None:
    size = 48
    z, y, x = np.indices((size, size, size), dtype=np.float32)
    center = (size - 1.0) / 2.0
    radius = np.sqrt((x - center) ** 2 + (y - center) ** 2)
    theta = np.arctan2(y - center, x - center)
    reference = np.asarray(
        np.exp(-((radius - 13.0) ** 2) / (2.0 * 2.5**2))
        * np.exp(-((z - center) ** 2) / (2.0 * 5.0**2))
        * (1.0 + 0.25 * np.cos(16.0 * theta)),
        dtype=np.float32,
    )
    candidate = reference.copy()
    candidate[15, 17, 23] += np.float32(1e-3)

    result = compare_maps(
        reference,
        candidate,
        voxel_size_A=1.0,
        alignment_lowpass_A=5.0,
        alignment_mode="rigid",
        max_shift_A=6.0,
        max_rotation_deg=30.0,
    )

    assert result.metadata["alignment"]["optimizer"]["selected_solution_source"] == (
        "exact_identity"
    )
    assert result.metadata["alignment"]["exact_canonical_voxel_identity"] is False
    assert result.metadata["alignment"]["solution_stability"]["stable"] is False
    assert result.metadata["status"] == "ambiguous_alignment"


def test_translation_fit_reports_boundary_limited_instead_of_verified_alignment() -> None:
    reference = _asymmetric_volume(size=26)
    candidate = _transform_candidate(
        reference,
        (0.0, 0.0, 0.0),
        (3.0, 0.0, 0.0),
        order=1,
        cval=0.0,
    )

    result = compare_maps(
        reference,
        candidate,
        voxel_size_A=1.5,
        alignment_lowpass_A=8.0,
        alignment_mode="translation",
        max_shift_A=1.0,
        max_rotation_deg=0.0,
    )

    assert result.metadata["status"] == "boundary_limited"
    assert result.metadata["alignment"]["bounds"]["contacts"]


def test_comparison_rejects_nonfinite_constant_and_overlong_cutoff_inputs() -> None:
    reference = _asymmetric_volume(size=20)
    with pytest.raises(MapComparisonError, match="non-constant"):
        compare_maps(
            reference,
            np.ones_like(reference),
            voxel_size_A=1.5,
            alignment_lowpass_A=8.0,
        )

    nonfinite = reference.copy()
    nonfinite[0, 0, 0] = np.nan
    with pytest.raises(MapComparisonError, match="non-finite"):
        compare_maps(
            reference,
            nonfinite,
            voxel_size_A=1.5,
            alignment_lowpass_A=8.0,
        )

    with pytest.raises(MapComparisonError, match="at most six"):
        compare_maps(
            reference,
            reference,
            voxel_size_A=1.5,
            alignment_lowpass_A=8.0,
            comparison_lowpasses_A=[4, 5, 6, 7, 8, 9, 10],
            max_shift_A=5.0,
        )
