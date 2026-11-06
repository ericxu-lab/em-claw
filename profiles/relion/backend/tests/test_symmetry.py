from __future__ import annotations

import numpy as np
import pytest
from scipy import ndimage
from scipy.spatial.transform import Rotation

import relion_mcp.symmetry as symmetry_module
from relion_mcp.symmetry import (
    SymmetryScanError,
    axis_angle_degrees,
    scan_symmetry,
    symmetry_operators,
)


def _cyclic_half_maps(
    order: int,
    *,
    size: int = 32,
    radius: float = 8.0,
    noise: float = 0.08,
) -> tuple[np.ndarray, np.ndarray]:
    z, y, x = np.indices((size, size, size), dtype=np.float32)
    center = (size - 1) / 2.0
    signal = np.zeros((size, size, size), dtype=np.float32)
    for index in range(order):
        angle = 2.0 * np.pi * index / order
        x0 = center + radius * np.cos(angle)
        y0 = center + radius * np.sin(angle)
        signal += np.exp(
            -(
                ((x - x0) / 1.5) ** 2
                + ((y - y0) / 2.0) ** 2
                + ((z - (center + 2.0)) / 1.7) ** 2
            )
            / 2.0
        )
        signal += 0.5 * np.exp(
            -(
                ((x - (center + 0.62 * radius * np.cos(angle + 0.17))) / 1.1) ** 2
                + ((y - (center + 0.62 * radius * np.sin(angle + 0.17))) / 1.2) ** 2
                + ((z - (center - 3.0)) / 1.1) ** 2
            )
            / 2.0
        )
    random = np.random.default_rng(100 + order)
    return (
        np.asarray(signal + noise * random.standard_normal(signal.shape), dtype=np.float32),
        np.asarray(signal + noise * random.standard_normal(signal.shape), dtype=np.float32),
    )


def _translated_c4_half_maps(
    shift_zyx: tuple[float, float, float],
    *,
    size: int = 40,
) -> tuple[np.ndarray, np.ndarray]:
    """Return independent noisy C4 halves translated together on one grid."""
    half1, half2 = _cyclic_half_maps(
        4,
        size=size,
        radius=9.0,
        noise=0.035,
    )
    return tuple(
        np.asarray(
            ndimage.shift(
                half,
                shift=shift_zyx,
                order=1,
                mode="constant",
                cval=0.0,
                prefilter=False,
            ),
            dtype=np.float32,
        )
        for half in (half1, half2)
    )


def _smooth_triaxial_half_maps(
    *,
    size: int = 36,
    noise: float = 0.012,
) -> tuple[np.ndarray, np.ndarray]:
    """Make a smooth C2 ellipsoid whose small-angle score rises with order."""
    z, y, x = np.indices((size, size, size), dtype=np.float32)
    center = (size - 1) / 2.0
    signal = np.exp(
        -(
            ((x - center) / 8.0) ** 2
            + ((y - center) / 5.0) ** 2
            + ((z - center) / 10.0) ** 2
        )
        / 2.0
    )
    random = np.random.default_rng(20260908)
    return (
        np.asarray(signal + noise * random.standard_normal(signal.shape), dtype=np.float32),
        np.asarray(signal + noise * random.standard_normal(signal.shape), dtype=np.float32),
    )


def _smooth_axisymmetric_half_maps(
    *,
    size: int = 36,
    noise: float = 0.006,
) -> tuple[np.ndarray, np.ndarray]:
    """Make a continuous axial-symmetry plateau with no finite rotational order."""
    z, y, x = np.indices((size, size, size), dtype=np.float32)
    center = (size - 1) / 2.0
    signal = np.exp(
        -(
            ((x - center) / 5.0) ** 2
            + ((y - center) / 5.0) ** 2
            + ((z - center) / 10.0) ** 2
        )
        / 2.0
    )
    random = np.random.default_rng(20260909)
    return (
        np.asarray(signal + noise * random.standard_normal(signal.shape), dtype=np.float32),
        np.asarray(signal + noise * random.standard_normal(signal.shape), dtype=np.float32),
    )


def _c29_two_ring_half_maps() -> tuple[np.ndarray, np.ndarray]:
    size = 40
    order = 29
    z, y, x = np.indices((size, size, size), dtype=np.float32)
    center = (size - 1) / 2.0
    signal = np.zeros((size, size, size), dtype=np.float32)
    rings = (
        (14.0, 2.0, 1.0, 0.58, 0.0),
        (10.0, -3.0, 0.45, 0.50, 0.07),
    )
    for radius, z_offset, amplitude, sigma, phase in rings:
        for index in range(order):
            angle = 2.0 * np.pi * index / order + phase
            x0 = center + radius * np.cos(angle)
            y0 = center + radius * np.sin(angle)
            z0 = center + z_offset
            signal += amplitude * np.exp(
                -(
                    (x - x0) ** 2
                    + (y - y0) ** 2
                    + (z - z0) ** 2
                )
                / (2.0 * sigma**2)
            )
    random = np.random.default_rng(29)
    return (
        np.asarray(signal + 0.012 * random.standard_normal(signal.shape), dtype=np.float32),
        np.asarray(signal + 0.012 * random.standard_normal(signal.shape), dtype=np.float32),
    )


def _rotated_group_half_maps(
    group: str,
    *,
    size: int,
    radius: float,
    noise: float,
) -> tuple[np.ndarray, np.ndarray]:
    z, y, x = np.indices((size, size, size), dtype=np.float32)
    center = (size - 1) / 2.0
    overall = Rotation.from_euler("zyx", [37.0, 23.0, -19.0], degrees=True).as_matrix()
    anchor = np.asarray((radius, 0.23 * radius, 0.14 * radius), dtype=np.float64)
    anchor *= radius / np.linalg.norm(anchor)
    signal = np.zeros((size, size, size), dtype=np.float32)
    sigma = 0.8
    for operator in symmetry_operators(group):
        position = overall @ operator @ anchor
        signal += np.exp(
            -(
                ((x - (center + position[0])) / sigma) ** 2
                + ((y - (center + position[1])) / sigma) ** 2
                + ((z - (center + position[2])) / sigma) ** 2
            )
            / 2.0
        )
    random = np.random.default_rng(300 + len(symmetry_operators(group)))
    return (
        np.asarray(signal + noise * random.standard_normal(signal.shape), dtype=np.float32),
        np.asarray(signal + noise * random.standard_normal(signal.shape), dtype=np.float32),
    )


def _randomly_oriented_exact_group_half_maps(
    group: str,
    *,
    seed: int,
    orientation_seed: int | None = None,
    size: int,
    radius: float,
    noise: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Make a generic point-group orbit with independent half-map noise.

    Isotropic Gaussian orbit points make the noiseless map invariant under the
    requested group.  The random global orientation is deliberately unrelated
    to the Cartesian sampling frame.  Equivalent principal axes therefore
    have equal population-level scores, and independent half-map noise may
    make either half choose a different representative of the same axis set.
    """
    random = np.random.default_rng(seed)
    z, y, x = np.indices((size, size, size), dtype=np.float32)
    center = (size - 1) / 2.0
    # Keep orientation and independent noise streams separate so ``seed`` is a
    # stable regression identifier for the half-map perturbation.
    orientation_random = np.random.default_rng(
        seed if orientation_seed is None else orientation_seed
    )
    overall = Rotation.random(random_state=orientation_random).as_matrix()
    operators = symmetry_operators(group)
    anchors = (
        (np.asarray((1.0, 0.37, 0.19), dtype=np.float64), 1.0, 0.85),
        (np.asarray((0.31, -0.73, 0.61), dtype=np.float64), 0.57, 1.05),
    )
    signal = np.zeros((size, size, size), dtype=np.float32)
    for raw_anchor, amplitude, sigma in anchors:
        anchor = radius * raw_anchor / np.linalg.norm(raw_anchor)
        for operator in operators:
            position = overall @ operator @ anchor
            signal += amplitude * np.exp(
                -(
                    (x - (center + position[0])) ** 2
                    + (y - (center + position[1])) ** 2
                    + (z - (center + position[2])) ** 2
                )
                / (2.0 * sigma**2)
            )
    return (
        np.asarray(signal + noise * random.standard_normal(signal.shape), dtype=np.float32),
        np.asarray(signal + noise * random.standard_normal(signal.shape), dtype=np.float32),
    )


def _octahedral_equivalent_axis_half_maps(
    *,
    seed: int = 36,
    size: int = 28,
    noise: float = 0.10,
) -> tuple[np.ndarray, np.ndarray]:
    """Make exact O density whose halves select equivalent orthogonal C4 axes."""
    z, y, x = np.indices((size, size, size), dtype=np.float32)
    center = (size - 1) / 2.0
    orientation = Rotation.random(
        random_state=np.random.default_rng(seed)
    ).as_matrix()
    signal = np.zeros((size, size, size), dtype=np.float32)
    anchors = (
        (np.asarray((6.0, 1.7, 2.5), dtype=np.float64), 1.0, 1.0),
        (np.asarray((3.0, -3.0, 4.0), dtype=np.float64), 0.55, 0.85),
    )
    for anchor, amplitude, sigma in anchors:
        for operator in symmetry_operators("O"):
            position = orientation @ operator @ orientation.T @ anchor
            signal += amplitude * np.exp(
                -(
                    (x - (center + position[0])) ** 2
                    + (y - (center + position[1])) ** 2
                    + (z - (center + position[2])) ** 2
                )
                / (2.0 * sigma**2)
            )
    random = np.random.default_rng(1000 + seed)
    return (
        np.asarray(signal + noise * random.standard_normal(signal.shape), dtype=np.float32),
        np.asarray(signal + noise * random.standard_normal(signal.shape), dtype=np.float32),
    )


def test_operator_counts_and_axis_sign_canonicalization() -> None:
    assert len(symmetry_operators("C5")) == 5
    assert len(symmetry_operators("D4")) == 8
    assert len(symmetry_operators("T")) == 12
    assert len(symmetry_operators("O")) == 24
    assert len(symmetry_operators("I")) == 60
    assert axis_angle_degrees([1.0, 2.0, 3.0], [-1.0, -2.0, -3.0]) == pytest.approx(0.0)


def test_common_scan_recovers_c5_and_never_selects_it() -> None:
    half1, half2 = _rotated_group_half_maps(
        "C5",
        size=34,
        radius=9.0,
        noise=0.04,
    )
    result = scan_symmetry(
        half1,
        half2,
        voxel_size_A=1.5,
        analysis_resolution_A=6.0,
        common_max_order=8,
        max_order=9,
        max_candidates=5,
    )

    c5 = next(candidate for candidate in result["candidates"] if candidate["group"] == "C5")
    assert c5["half_map_consistent"] is True
    # Without an operator-count bonus, nearly tied simpler fits can precede
    # the exact group. The scan must expose that ambiguity, not select from it.
    assert result["status"] == "ambiguous"
    assert result["hypothesis_results"]["C5"]["scan_heuristic_outcome"] == "pass"
    assert result["selected_candidate"] is None
    assert result["decision"] == "defer"
    assert result["human_input_required"] is False
    assert result["report_only"] is True
    assert result["search"]["complete_group_scoring_for_reported_candidates"] is True


def test_translated_noisy_c4_is_recovered_by_bounded_center_search() -> None:
    shift_zyx = np.asarray((0.0, -3.0, 4.0))
    half1, half2 = _translated_c4_half_maps(tuple(shift_zyx))

    result = scan_symmetry(
        half1,
        half2,
        voxel_size_A=1.5,
        analysis_resolution_A=6.0,
        common_max_order=4,
        max_order=4,
        max_candidates=2,
        focus_groups=["C4"],
    )

    center = result["preprocessing"]["center_estimation"]
    observed_shift = np.asarray(center["shift_from_geometric_center_zyx_pix"])
    assert center["method"] == "independent_half_robust_signed_density_centroid"
    assert center["stable"] is True
    assert observed_shift[1:] == pytest.approx(shift_zyx[1:], abs=1.0)
    assert center["half_map_center_disagreement_pix"] <= 1.0
    c4 = next(candidate for candidate in result["candidates"] if candidate["group"] == "C4")
    assert c4["half_map_consistent"] is True
    assert c4["raw_scan_heuristics_passed"] is True
    assert c4["support_status"] == "inconclusive"
    assert c4["order_specific_support_valid"] is False
    assert c4["support_confounds"] == ["high_order_control_not_assessable"]
    assert c4["center_stable"] is True
    assert result["search"]["high_order_diagnostic"]["assessable"] is False
    assert result["scan_summary"]["negative_inference_valid"] is False


def test_cyclic_axis_line_distance_is_invariant_to_representative_points() -> None:
    first_axis = np.asarray((0.0, 0.0, 1.0))
    second_axis = Rotation.from_euler("y", 20.0, degrees=True).apply(first_axis)
    first_point_zyx = (30.0 * first_axis)[::-1]
    second_point_zyx = (-25.0 * second_axis)[::-1]

    assert symmetry_module._axis_line_disagreement_A(
        first_axis,
        first_point_zyx,
        second_axis,
        second_point_zyx,
        1.0,
    ) == pytest.approx(0.0, abs=1e-10)


def test_cyclic_center_refinement_stays_inside_total_bound(monkeypatch) -> None:
    shape = (24, 24, 24)
    volume = np.zeros(shape, dtype=np.float32)
    prepared = symmetry_module._PreparedMaps(
        half1=volume,
        half2=volume,
        weights=np.ones(shape, dtype=np.float32),
        voxel_size_A=1.0,
        requested_resolution_A=8.0,
        effective_resolution_A=8.0,
        stride=1,
        particle_radius_A=8.0,
        identifiability_max_order=8,
        coordinates_xyz=np.zeros((1, 3), dtype=np.float64),
        coordinates_zyx=np.zeros((1, 3), dtype=np.float64),
        base_weights=np.ones(1, dtype=np.float64),
        base_half1=np.ones(1, dtype=np.float64),
        base_half2=np.ones(1, dtype=np.float64),
        identity_half_correlation=1.0,
        rotation_center_zyx=np.asarray((12.0, 12.0, 12.0)),
    )

    def monotone_scores(_prepared, _matrix, center_zyx=None):
        center = np.asarray(center_zyx, dtype=np.float64)
        score = float(center[1] + center[2])
        return {
            "half1": score,
            "half2": score,
            "cross": score,
            "cross_retention": score,
        }

    monkeypatch.setattr(symmetry_module, "_operation_scores", monotone_scores)
    initial = np.asarray((12.0, 12.0, 12.0))
    refined, _ = symmetry_module._refine_cyclic_axis_line_center(
        prepared,
        4,
        np.asarray((0.0, 0.0, 1.0)),
        initial,
        objective="half1",
    )

    displacement_xyz = (refined - initial)[::-1]
    transverse = displacement_xyz.copy()
    transverse[2] = 0.0
    assert np.linalg.norm(transverse) <= symmetry_module._CENTER_LINE_SEARCH_MAX_PIX + 1e-8

    good_summary = {"mean": 0.9, "minimum": 0.9, "p10": 0.9}
    near_edge = symmetry_module._candidate_record(
        prepared,
        group="C4",
        stage=1,
        axis_xyz=np.asarray((0.0, 0.0, 1.0)),
        orientation=np.eye(3),
        group_metrics={
            "half1": dict(good_summary),
            "half2": dict(good_summary),
            "cross": dict(good_summary),
            "cross_retention": dict(good_summary),
            "complete_support": 0.9,
            "per_operation": [],
        },
        evidence_z=4.0,
        axis_agreement_deg=0.0,
        identifiable=True,
        generator_metrics=None,
        search_note="unit test",
        half_map_consistent=True,
        symmetry_center_zyx=np.asarray((1.1, 12.0, 12.0)),
    )
    assert near_edge["center_stable"] is False
    assert near_edge["support_status"] == "inconclusive"
    assert "candidate_center_unstable_or_too_near_edge" in near_edge["support_failures"]


def test_disagreeing_half_map_centers_cannot_produce_a_negative_verdict() -> None:
    first_pair = _translated_c4_half_maps((0.0, -4.0, 4.0))
    second_pair = _translated_c4_half_maps((0.0, 4.0, -4.0))

    result = scan_symmetry(
        first_pair[0],
        second_pair[1],
        voxel_size_A=1.5,
        analysis_resolution_A=6.0,
        common_max_order=4,
        max_order=4,
        max_candidates=2,
        focus_groups=["C4"],
    )

    center = result["preprocessing"]["center_estimation"]
    assert center["stable"] is False
    assert center["half_map_center_disagreement_A"] > center["maximum_stable_disagreement_A"]
    assert result["status"] != "no_supported_candidate"
    assert result["decision"] == "defer"
    assert result["human_input_required"] is False
    assert result["scan_summary"]["negative_inference_valid"] is False
    assert result["hypothesis_results"]["C4"]["support_status"] == "inconclusive"


def test_effective_cutoff_is_computed_reported_and_used(monkeypatch) -> None:
    size = 42
    voxel_size_A = 1.5
    requested_resolution_A = 4.0
    stride = 2
    expected_effective_A = 1.0 / (
        1.0 / (2.0 * voxel_size_A * stride)
        - 1.0 / (size * voxel_size_A)
    )
    half1, half2 = _cyclic_half_maps(2, size=size, radius=10.0, noise=0.03)
    observed_cutoffs: list[float] = []
    original_lowpass = symmetry_module._raised_cosine_lowpass
    monkeypatch.setattr(symmetry_module, "_ANALYSIS_MAX_SIDE", 40)

    def record_lowpass(
        volume: np.ndarray,
        voxel_size: float,
        resolution: float,
    ) -> np.ndarray:
        observed_cutoffs.append(resolution)
        return original_lowpass(volume, voxel_size, resolution)

    monkeypatch.setattr(symmetry_module, "_raised_cosine_lowpass", record_lowpass)
    result = scan_symmetry(
        half1,
        half2,
        voxel_size_A=voxel_size_A,
        analysis_resolution_A=requested_resolution_A,
        common_max_order=2,
        max_order=2,
        max_candidates=2,
        focus_groups=["C2"],
    )

    preprocessing = result["preprocessing"]
    assert preprocessing["requested_analysis_lowpass_A"] == requested_resolution_A
    assert preprocessing["effective_analysis_lowpass_A"] == pytest.approx(
        expected_effective_A
    )
    assert preprocessing["analysis_stride"] == stride
    assert preprocessing["analysis_voxel_size_A"] == voxel_size_A * stride
    assert preprocessing["requested_cutoff_honored"] is False
    assert preprocessing["effective_to_requested_resolution_ratio"] == pytest.approx(
        expected_effective_A / requested_resolution_A
    )
    assert len(observed_cutoffs) >= 2
    assert observed_cutoffs == pytest.approx(
        [expected_effective_A] * len(observed_cutoffs)
    )
    assert any(
        "could not honor the requested cutoff" in warning.lower()
        and "inconclusive" in warning.lower()
        for warning in result["warnings"]
    )


@pytest.mark.parametrize(
    ("group", "size", "radius", "noise"),
    (("T", 32, 8.0, 0.025), ("O", 36, 9.0, 0.025), ("I", 40, 12.0, 0.018)),
)
def test_arbitrarily_oriented_polyhedral_groups_are_scanned(
    group: str,
    size: int,
    radius: float,
    noise: float,
) -> None:
    half1, half2 = _rotated_group_half_maps(
        group,
        size=size,
        radius=radius,
        noise=noise,
    )
    result = scan_symmetry(
        half1,
        half2,
        voxel_size_A=1.5,
        analysis_resolution_A=4.5,
        common_max_order=5,
        max_order=5,
        max_candidates=5,
    )

    hypothesis = result["hypothesis_results"][group]
    assert hypothesis["scan_heuristic_outcome"] == "pass"
    assert hypothesis["supported"] is True
    assert result["status"] == "ambiguous"
    assert group in result["search"]["polyhedral_groups_complete_scored"]


def test_o_replication_accepts_symmetry_equivalent_half_map_axes() -> None:
    half1, half2 = _octahedral_equivalent_axis_half_maps()
    result = scan_symmetry(
        half1,
        half2,
        voxel_size_A=1.5,
        analysis_resolution_A=6.0,
        common_max_order=5,
        max_order=5,
        max_candidates=8,
    )

    octahedral = next(
        candidate for candidate in result["candidates"] if candidate["group"] == "O"
    )
    assert octahedral["half_map_consistent"] is True
    # The halves choose orthogonal C4 representatives of the same O group.
    # Their complete oriented operator sets nevertheless coincide.
    assert octahedral["axis_agreement_deg"] == pytest.approx(90.0, abs=2.0)
    assert octahedral["axis_agreement_is_gating"] is False
    assert octahedral["operator_set_agreement_deg"] <= 2.0
    for metrics in octahedral["reciprocal_complete_group_metrics"].values():
        assert metrics["complete_support"] > 0.8
        assert metrics["cross_retention"]["p10"] > 0.0


def test_d2_replication_treats_twofold_axes_as_an_unordered_frame() -> None:
    half1, half2 = _randomly_oriented_exact_group_half_maps(
        "D2",
        seed=54,
        orientation_seed=38,
        size=30,
        radius=8.0,
        noise=0.04,
    )
    result = scan_symmetry(
        half1,
        half2,
        voxel_size_A=1.5,
        analysis_resolution_A=5.5,
        common_max_order=2,
        max_order=2,
        max_candidates=4,
    )

    dihedral = next(
        candidate for candidate in result["candidates"] if candidate["group"] == "D2"
    )
    assert dihedral["half_map_consistent"] is True
    assert dihedral["axis_xyz"] is None
    assert len(dihedral["axis_frame_xyz"]) == 3
    frame = np.asarray(dihedral["axis_frame_xyz"])
    assert frame @ frame.T == pytest.approx(np.eye(3), abs=1e-6)
    assert dihedral["axis_agreement_is_gating"] is False
    assert dihedral["operator_set_agreement_deg"] <= 2.0
    # D2 has no distinguished principal axis: its three orthogonal C2 axes are
    # an unordered frame.  Different representatives in the two halves are a
    # valid group-level match, not biological disagreement.
    assert axis_angle_degrees(
        dihedral["half1_axis_xyz"],
        dihedral["half2_axis_xyz"],
    ) > 35.0


def test_prime_high_order_sentinel_opens_stage2_for_c11() -> None:
    half1, half2 = _cyclic_half_maps(11, size=36, radius=11.0, noise=0.05)
    result = scan_symmetry(
        half1,
        half2,
        voxel_size_A=1.5,
        analysis_resolution_A=5.0,
        common_max_order=8,
        max_order=12,
        stage2_window=1,
        max_candidates=5,
    )

    assert 11 in result["search"]["sentinel_supported_peaks"]
    assert 11 in result["search"]["stage2_orders"]
    assert result["candidates"][0]["group"] == "C11"
    assert result["candidates"][0]["stage"] == 2


def test_smooth_anisotropic_blob_reports_small_angle_ramp_without_unique_order() -> None:
    half1, half2 = _smooth_triaxial_half_maps()
    result = scan_symmetry(
        half1,
        half2,
        voxel_size_A=1.5,
        analysis_resolution_A=6.0,
        common_max_order=4,
        max_order=16,
        stage2_window=1,
        max_candidates=3,
        focus_groups=["C16"],
    )

    diagnostic = result["search"]["high_order_diagnostic"]
    assert diagnostic["method"] == (
        "high_order_spearman_order_vs_consensus_generator_support"
    )
    assert diagnostic["orders"] == list(range(2, 17))
    assert diagnostic["ramp_analysis_orders"] == list(range(4, 17))
    assert diagnostic["spearman_rho"] >= 0.90
    assert diagnostic["endpoint_delta"] >= 0.03
    assert diagnostic["assessable"] is True
    assert diagnostic["monotonic_small_angle_ramp"] is True
    assert result["hypothesis_results"]["C4"]["scan_heuristic_outcome"] == (
        "confounded"
    )
    assert result["hypothesis_results"]["C4"]["supported"] is False
    assert result["status"] == "ambiguous"
    assert result["selected_candidate"] is None
    assert result["scan_summary"]["biological_conclusion"] == "unresolved"
    assert result["scan_summary"]["permits_symmetry_acceptance"] is False
    assert result["scan_summary"]["permits_symmetry_rejection"] is False


def test_continuous_axial_symmetry_plateau_confounds_finite_c_and_d_orders() -> None:
    half1, half2 = _smooth_axisymmetric_half_maps()
    result = scan_symmetry(
        half1,
        half2,
        voxel_size_A=1.5,
        analysis_resolution_A=6.0,
        common_max_order=4,
        max_order=12,
        stage2_window=1,
        max_candidates=4,
        focus_groups=["C4", "D4"],
    )

    diagnostic = result["search"]["high_order_diagnostic"]
    assert diagnostic["assessable"] is True
    assert diagnostic["high_flat_support_plateau"] is True
    assert diagnostic["small_angle_confounded"] is True
    for group in ("C4", "D4"):
        hypothesis = result["hypothesis_results"][group]
        assert hypothesis["raw_scan_heuristics_passed"] is True
        assert hypothesis["scan_heuristic_outcome"] == "confounded"
        assert hypothesis["support_status"] == "inconclusive"
        assert hypothesis["supported"] is False
    omitted = result["result_completeness"][
        "omitted_raw_scan_heuristic_pass_groups"
    ]
    assert omitted
    assert all(
        result["hypothesis_results"][group]["detail_state"]
        == "omitted_by_display_cap"
        for group in omitted
    )


def test_stage2_neighbor_of_c30_recovers_exact_prime_c29() -> None:
    half1, half2 = _c29_two_ring_half_maps()
    result = scan_symmetry(
        half1,
        half2,
        voxel_size_A=1.0,
        analysis_resolution_A=2.5,
        common_max_order=8,
        max_order=30,
        stage2_window=2,
        max_candidates=8,
    )

    assert 29 in result["search"]["stage2_orders"]
    c29 = next(
        candidate for candidate in result["candidates"] if candidate["group"] == "C29"
    )
    assert c29["stage"] == 2
    assert c29["half_map_consistent"] is True
    assert c29["selected_orientation_source"] in {
        "half1_independent",
        "half2_independent",
    }
    for metrics in c29["reciprocal_complete_group_metrics"].values():
        assert metrics["half1"]["mean"] >= 0.4
        assert metrics["half2"]["mean"] >= 0.4
        assert metrics["cross_retention"]["mean"] >= 0.4
        assert metrics["cross_retention"]["p10"] > 0.0
    assert "noise_excess_asymmetry_fraction" not in c29
    assert "noise_normalized_asymmetry" not in c29


def test_stage2_selector_reserves_an_anchor_for_supported_c30_harmonic_closure() -> None:
    scalar = np.zeros(1, dtype=np.float32)
    coordinates = np.zeros((1, 3), dtype=np.float64)
    prepared = symmetry_module._PreparedMaps(
        half1=scalar,
        half2=scalar,
        weights=scalar,
        voxel_size_A=1.0,
        requested_resolution_A=5.0,
        effective_resolution_A=5.0,
        stride=1,
        particle_radius_A=100.0,
        identifiability_max_order=64,
        coordinates_xyz=coordinates,
        coordinates_zyx=coordinates,
        base_weights=scalar,
        base_half1=scalar,
        base_half2=scalar,
        identity_half_correlation=0.9,
    )

    sentinel_rank = [
        14,
        15,
        9,
        10,
        11,
        12,
        13,
        16,
        17,
        18,
        19,
        20,
        21,
        22,
        23,
        24,
        25,
        26,
        30,
        27,
        28,
        29,
    ]
    evidence_by_order = {
        order: float(len(sentinel_rank) - rank)
        for rank, order in enumerate(sentinel_rank)
    }
    c30_closure = {2, 3, 5, 6, 10, 15, 30}

    def spectrum(order: int) -> symmetry_module._Spectrum:
        independent_z = 6.0 if order in c30_closure else 2.1
        return symmetry_module._Spectrum(
            order=order,
            axes_xyz=np.asarray([[0.0, 0.0, 1.0]], dtype=np.float64),
            half1=np.asarray([0.9]),
            half2=np.asarray([0.9]),
            cross=np.asarray([0.9]),
            cross_retention=np.asarray([0.9]),
            consensus_index=0,
            half1_index=0,
            half2_index=0,
            evidence_z=evidence_by_order.get(order, 1.0),
            half1_evidence_z=independent_z,
            half2_evidence_z=independent_z,
            axis_agreement_deg=0.0,
            identifiable=True,
        )

    spectra = {order: spectrum(order) for order in range(2, 31)}
    supported_sentinels = [spectra[order] for order in sentinel_rank]
    assert supported_sentinels.index(spectra[30]) + 1 == 19

    anchors, provenance = symmetry_module._select_stage2_sentinel_anchors(
        supported_sentinels,
        spectra,
        prepared,
    )

    assert len(anchors) <= 2
    assert [anchor.order for anchor in anchors] == [14, 30]
    assert provenance == [
        {
            "order": 14,
            "selection_reason": "evidence_peak",
            "supported_divisors": [2, 7],
        },
        {
            "order": 30,
            "selection_reason": "harmonic_closure",
            "supported_divisors": [2, 3, 5, 6, 10, 15],
        },
    ]


def test_candidate_rank_does_not_reward_operator_count() -> None:
    scalar = np.ones(16, dtype=np.float32)
    coordinates = np.zeros((16, 3), dtype=np.float64)
    prepared = symmetry_module._PreparedMaps(
        half1=scalar,
        half2=scalar,
        weights=scalar,
        voxel_size_A=1.0,
        requested_resolution_A=5.0,
        effective_resolution_A=5.0,
        stride=1,
        particle_radius_A=20.0,
        identifiability_max_order=16,
        coordinates_xyz=coordinates,
        coordinates_zyx=coordinates,
        base_weights=scalar,
        base_half1=scalar,
        base_half2=scalar,
        identity_half_correlation=0.9,
    )

    def group_metrics(operation_count: int) -> dict[str, object]:
        summary = {"mean": 0.8, "minimum": 0.8, "p10": 0.8}
        return {
            "half1": dict(summary),
            "half2": dict(summary),
            "cross": dict(summary),
            "cross_retention": dict(summary),
            "complete_support": 0.8,
            "per_operation": [{} for _ in range(operation_count)],
        }

    common = {
        "prepared": prepared,
        "stage": 1,
        "axis_xyz": np.asarray((0.0, 0.0, 1.0)),
        "orientation": np.eye(3),
        "evidence_z": 4.0,
        "axis_agreement_deg": 0.0,
        "identifiable": True,
        "generator_metrics": None,
        "search_note": "rank regression",
        "half_map_consistent": True,
    }
    c2 = symmetry_module._candidate_record(
        group="C2",
        group_metrics=group_metrics(1),
        **common,
    )
    c8 = symmetry_module._candidate_record(
        group="C8",
        group_metrics=group_metrics(7),
        **common,
    )

    assert c2["half_map_consistent"] is True
    assert c8["half_map_consistent"] is True
    assert c8["rank_score"] == pytest.approx(c2["rank_score"])


def test_complete_dihedral_scoring_distinguishes_d4_from_c4_subgroup() -> None:
    size = 30
    z, y, x = np.indices((size, size, size), dtype=np.float32)
    center = (size - 1) / 2.0
    anchor = np.asarray((8.0, 1.7, 3.2))
    overall = Rotation.from_euler("zyx", [31.0, -27.0, 18.0], degrees=True).as_matrix()
    signal = np.zeros((size, size, size), dtype=np.float32)
    for operator in symmetry_operators("D4"):
        position = overall @ operator @ anchor
        signal += np.exp(
            -(
                ((x - (center + position[0])) / 1.2) ** 2
                + ((y - (center + position[1])) / 1.7) ** 2
                + ((z - (center + position[2])) / 0.9) ** 2
            )
            / 2.0
        )
    random = np.random.default_rng(42)
    half1 = signal + 0.04 * random.standard_normal(signal.shape)
    half2 = signal + 0.04 * random.standard_normal(signal.shape)
    result = scan_symmetry(
        half1,
        half2,
        voxel_size_A=1.5,
        analysis_resolution_A=6.0,
        common_max_order=4,
        max_order=9,
        max_candidates=5,
    )

    groups = [candidate["group"] for candidate in result["candidates"]]
    assert {"D4", "C4"}.issubset(groups)
    assert result["status"] == "ambiguous"
    d4 = next(candidate for candidate in result["candidates"] if candidate["group"] == "D4")
    assert d4["perpendicular_c2_cross_half_retention"] > 0.5
    perpendicular_gate = d4["gate_results"]["perpendicular_c2_support"]
    assert perpendicular_gate["actual_half1_mean"] > 0.5
    assert perpendicular_gate["actual_half2_mean"] > 0.5
    assert perpendicular_gate["actual_cross_half_retention_p10"] > 0.0
    assert perpendicular_gate["minimum_cross_half_retention_p10_exclusive"] == 0.0
    assert all(
        record["support_status"] != "supported"
        for record in result["hypothesis_results"].values()
        if record["scan_heuristic_outcome"] == "fail"
    )

    capped = scan_symmetry(
        half1,
        half2,
        voxel_size_A=1.5,
        analysis_resolution_A=6.0,
        common_max_order=4,
        max_order=9,
        max_candidates=1,
    )
    assert len(capped["candidates"]) == 1
    assert capped["status"] == "ambiguous"
    assert capped["search"]["candidate_cap_truncated"] is True
    assert capped["search"]["ambiguity_top_two_are_nested"] is True
    completeness = capped["result_completeness"]
    assert completeness["displayed_candidate_limit"] == 1
    assert completeness["supported_candidate_count"] > completeness[
        "displayed_supported_candidate_count"
    ]
    assert completeness["display_truncated"] is True
    assert capped["hypothesis_results"]["C4"]["support_status"] == "supported"
    assert capped["hypothesis_results"]["C4"]["scan_heuristic_outcome"] == "pass"
    assert capped["hypothesis_results"]["C4"]["rank"] > 1
    compact_d4_gate = capped["hypothesis_results"]["D4"]["gate_results"][
        "perpendicular_c2_support"
    ]
    assert compact_d4_gate["actual_half1_mean"] > 0.5
    assert compact_d4_gate["actual_half2_mean"] > 0.5
    assert compact_d4_gate["actual_cross_half_retention_p10"] > 0.0

    focused = scan_symmetry(
        half1,
        half2,
        voxel_size_A=1.5,
        analysis_resolution_A=6.0,
        common_max_order=4,
        max_order=9,
        max_candidates=1,
        focus_groups=["C4", "D4"],
    )
    assert {candidate["group"] for candidate in focused["candidates"]} == {
        "C4",
        "D4",
    }
    assert len(focused["candidates"]) == 2
    assert focused["hypothesis_results"]["C4"]["support_status"] == "supported"
    assert focused["hypothesis_results"]["D4"]["support_status"] == "supported"
    assert focused["hypothesis_results"]["C4"]["rank"] == capped[
        "hypothesis_results"
    ]["C4"]["rank"]
    assert focused["status"] == capped["status"] == "ambiguous"


def test_weak_half_map_pair_is_not_forced_to_a_candidate() -> None:
    random = np.random.default_rng(7)
    half1 = random.standard_normal((18, 18, 18)).astype(np.float32)
    half2 = random.standard_normal((18, 18, 18)).astype(np.float32)
    result = scan_symmetry(
        half1,
        half2,
        voxel_size_A=1.5,
        analysis_resolution_A=8.0,
        common_max_order=2,
        max_order=2,
        max_candidates=2,
    )

    assert result["status"] in {"untestable", "no_supported_candidate"}
    assert result["candidates"] == []


def test_shared_asymmetric_signal_is_not_order_specific_support_without_ramp_control() -> None:
    size = 24
    z, y, x = np.indices((size, size, size), dtype=np.float32)
    center = (size - 1) / 2.0
    signal = np.exp(
        -(
            ((x - (center + 4.0)) / 2.0) ** 2
            + ((y - (center - 3.0)) / 3.0) ** 2
            + ((z - (center + 2.0)) / 2.0) ** 2
        )
        / 2.0
    )
    signal += 0.5 * np.exp(
        -(
            ((x - (center - 5.0)) / 1.5) ** 2
            + ((y - (center + 2.0)) / 2.0) ** 2
            + ((z - (center - 4.0)) / 2.5) ** 2
        )
        / 2.0
    )
    random = np.random.default_rng(77)
    result = scan_symmetry(
        signal + 0.1 * random.standard_normal(signal.shape),
        signal + 0.1 * random.standard_normal(signal.shape),
        voxel_size_A=1.5,
        analysis_resolution_A=8.0,
        common_max_order=4,
        max_order=4,
        max_candidates=5,
    )

    assert result["algorithm_version"] == "bounded-half-map-search/3"
    assert result["status"] == "ambiguous"
    assert result["decision"] == "defer"
    assert result["human_input_required"] is False
    assert result["report_only"] is True
    assert result["selected_candidate"] is None
    c4 = next(candidate for candidate in result["candidates"] if candidate["group"] == "C4")
    assert c4["half_map_consistent"] is True
    assert c4["raw_scan_heuristics_passed"] is True
    assert c4["support_status"] == "inconclusive"
    assert c4["order_specific_support_valid"] is False
    assert result["search"]["high_order_diagnostic"]["assessable"] is False
    assert result["scan_summary"]["negative_inference_valid"] is False
    assert "noise_excess_asymmetry_fraction" not in c4
    assert "noise_normalized_asymmetry" not in c4
    assert "signal_asymmetry_energy" not in c4["complete_group_metrics"]
    assert "half_difference_asymmetry_energy" not in c4["complete_group_metrics"]
    assert "maximum_noise_excess_asymmetry_fraction" not in result["support_heuristics"]
    assert "advisory_metrics" not in result
    assert all("asymmetry fraction" not in warning for warning in result["warnings"])


def test_focus_groups_are_canonicalized() -> None:
    assert symmetry_module._normalize_focus_groups(
        [" c04 ", "D002", "t", "C4"],
        8,
    ) == ("C4", "D2", "T")


def test_parameter_and_input_caps_are_enforced() -> None:
    half1, half2 = _cyclic_half_maps(3, size=16, radius=4.0, noise=0.02)
    with pytest.raises(SymmetryScanError, match="max_order"):
        scan_symmetry(
            half1,
            half2,
            voxel_size_A=1.5,
            analysis_resolution_A=8.0,
            max_order=65,
        )
    with pytest.raises(SymmetryScanError, match="stage2_window"):
        scan_symmetry(
            half1,
            half2,
            voxel_size_A=1.5,
            analysis_resolution_A=8.0,
            common_max_order=2,
            max_order=3,
            stage2_window=0,
        )
    with pytest.raises(SymmetryScanError, match="identical shapes"):
        scan_symmetry(
            half1,
            half2[:-1],
            voxel_size_A=1.5,
            analysis_resolution_A=8.0,
        )
    with pytest.raises(SymmetryScanError, match="identical voxel arrays"):
        scan_symmetry(
            half1,
            half1.copy(),
            voxel_size_A=1.5,
            analysis_resolution_A=8.0,
        )
    with pytest.raises(SymmetryScanError, match="Nyquist"):
        scan_symmetry(
            half1,
            half2,
            voxel_size_A=1.5,
            analysis_resolution_A=2.0,
        )
