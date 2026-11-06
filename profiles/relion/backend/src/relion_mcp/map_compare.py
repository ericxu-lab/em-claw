"""Deterministic, read-only local registration for related cryo-EM maps.

The numerical core is deliberately independent of MRC paths and MCP transport.
It estimates one candidate-to-reference transform at a declared physical
low-pass and then freezes that transform for every requested comparison
low-pass.  It does not infer a biological relation or write an aligned map.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from scipy import fft, ndimage, optimize
from scipy.spatial.transform import Rotation


class MapComparisonError(ValueError):
    """Raised when maps cannot be registered honestly or within bounded resources."""


SCHEMA_VERSION = "relion.map-comparison/v1"
ALGORITHM_VERSION = "bounded-local-rigid/3"

_ANALYSIS_MAX_SIDE = 64
_MAX_INPUT_VOXELS = 64_000_000
_MAX_ESTIMATED_BYTES = 1_500 * 1024 * 1024
_MAX_FUNCTION_EVALUATIONS = 1_500
_MAX_OPTIMIZER_STARTS = 3
_MIN_VALID_SUPPORT_FRACTION = 0.90
_BOUND_TOLERANCE_FRACTION = 0.02
_MIN_EFFECTIVE_SUPPORT_VOXELS = 64
_MIN_EFFECTIVE_SUPPORT_FRACTION = 0.001
_MIN_NUMERICAL_OBJECTIVE_GAIN = 1e-4
_STABILITY_FOLDS = 5
_MAX_STABILITY_EVALUATIONS_PER_FOLD = 400
_STABILITY_OPTIMIZER_FTOL = 1e-5


@dataclass(frozen=True)
class MapComparisonCutoffView:
    """Bounded central-plane evidence generated with the frozen transform."""

    cutoff_resolution_A: float
    reference_xy: np.ndarray
    candidate_before_xy: np.ndarray
    candidate_after_xy: np.ndarray
    metric_weights_xy: np.ndarray


@dataclass(frozen=True)
class MapComparisonResult:
    """Registered alignment-filter maps plus JSON-shaped quantitative results."""

    reference_filtered: np.ndarray
    candidate_before: np.ndarray
    candidate_after: np.ndarray
    metric_weights: np.ndarray
    cutoff_views: tuple[MapComparisonCutoffView, ...]
    metadata: dict[str, Any]


def _finite_positive(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise MapComparisonError(f"{name} must be a finite positive number.")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise MapComparisonError(f"{name} must be a finite positive number.") from exc
    if not math.isfinite(result) or result <= 0:
        raise MapComparisonError(f"{name} must be a finite positive number.")
    return result


def _finite_nonnegative(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise MapComparisonError(f"{name} must be a finite non-negative number.")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise MapComparisonError(
            f"{name} must be a finite non-negative number."
        ) from exc
    if not math.isfinite(result) or result < 0:
        raise MapComparisonError(f"{name} must be a finite non-negative number.")
    return result


def _validate_volume(volume: np.ndarray, name: str) -> np.ndarray:
    data = np.asarray(volume)
    if data.ndim != 3:
        raise MapComparisonError(f"{name} must be a three-dimensional array.")
    if data.size > _MAX_INPUT_VOXELS:
        raise MapComparisonError(
            f"{name} contains {data.size:,} voxels, above the "
            f"{_MAX_INPUT_VOXELS:,}-voxel cap."
        )
    if not np.issubdtype(data.dtype, np.number) or np.issubdtype(
        data.dtype, np.complexfloating
    ):
        raise MapComparisonError(f"{name} must contain real-valued density.")
    if not np.all(np.isfinite(data)):
        raise MapComparisonError(f"{name} contains non-finite voxels.")
    if float(np.max(data)) <= float(np.min(data)):
        raise MapComparisonError(f"{name} must contain non-constant density.")
    return np.asarray(data, dtype=np.float32)


def validate_map_comparison_resources(
    shape: Sequence[int],
) -> dict[str, int]:
    """Validate a prospective canonical grid before callers allocate full arrays."""
    if len(shape) != 3 or any(
        isinstance(item, bool) or not isinstance(item, (int, np.integer))
        for item in shape
    ):
        raise MapComparisonError("Map shape must contain three integer dimensions.")
    canonical_shape = tuple(int(item) for item in shape)
    if min(canonical_shape) < 8:
        raise MapComparisonError("Every map dimension must contain at least eight voxels.")
    voxels = math.prod(canonical_shape)
    if voxels > _MAX_INPUT_VOXELS:
        raise MapComparisonError(
            f"Each map contains {voxels:,} voxels, above the "
            f"{_MAX_INPUT_VOXELS:,}-voxel cap."
        )
    estimated_bytes = voxels * 56
    if estimated_bytes > _MAX_ESTIMATED_BYTES:
        raise MapComparisonError(
            "Map comparison exceeds the bounded working-memory estimate: "
            f"{estimated_bytes:,} bytes > {_MAX_ESTIMATED_BYTES:,} bytes."
        )
    return {
        "voxels_per_map": voxels,
        "estimated_working_bytes": estimated_bytes,
        "estimated_working_bytes_limit": _MAX_ESTIMATED_BYTES,
        "input_voxel_cap_per_map": _MAX_INPUT_VOXELS,
    }


def _validate_mask(mask: np.ndarray | None, shape: tuple[int, int, int]) -> np.ndarray | None:
    if mask is None:
        return None
    data = np.asarray(mask)
    if data.shape != shape:
        raise MapComparisonError("mask must have the same canonical shape as both maps.")
    if not np.all(np.isfinite(data)):
        raise MapComparisonError("mask contains non-finite voxels.")
    minimum = float(np.min(data))
    maximum = float(np.max(data))
    if minimum < 0 or maximum > 1:
        raise MapComparisonError("mask values must lie within [0, 1].")
    if not np.any(data > 0):
        raise MapComparisonError("mask must contain non-zero support.")
    return np.asarray(data, dtype=np.float32)


def _raised_cosine_lowpass(
    volume: np.ndarray,
    voxel_size_A: float,
    resolution_A: float,
) -> np.ndarray:
    data = np.asarray(volume, dtype=np.float32)
    frequencies_z = fft.fftfreq(data.shape[0], d=voxel_size_A)
    frequencies_y = fft.fftfreq(data.shape[1], d=voxel_size_A)
    frequencies_x = fft.rfftfreq(data.shape[2], d=voxel_size_A)
    radius = np.sqrt(
        frequencies_z[:, None, None] ** 2
        + frequencies_y[None, :, None] ** 2
        + frequencies_x[None, None, :] ** 2
    )
    cutoff = 1.0 / resolution_A
    shell = min(1.0 / (size * voxel_size_A) for size in data.shape)
    edge_low = max(0.0, cutoff - shell)
    edge_high = cutoff + shell
    transfer = np.ones(radius.shape, dtype=np.float32)
    transfer[radius >= edge_high] = 0.0
    transition = (radius > edge_low) & (radius < edge_high)
    if edge_high > edge_low:
        transfer[transition] = 0.5 + 0.5 * np.cos(
            np.pi * (radius[transition] - edge_low) / (edge_high - edge_low)
        )
    spectrum = fft.rfftn(data)
    spectrum *= transfer
    return np.asarray(fft.irfftn(spectrum, s=data.shape), dtype=np.float32)


def _centered_resample(
    volume: np.ndarray,
    stride: int,
    *,
    order: int,
) -> np.ndarray:
    if stride == 1:
        return np.asarray(volume, dtype=np.float32)
    source = np.asarray(volume, dtype=np.float32)
    output_shape = tuple(math.ceil(size / stride) for size in source.shape)
    source_center = (np.asarray(source.shape, dtype=np.float64) - 1.0) / 2.0
    output_center = (np.asarray(output_shape, dtype=np.float64) - 1.0) / 2.0
    offset = source_center - stride * output_center
    return np.asarray(
        ndimage.affine_transform(
            source,
            matrix=np.eye(3, dtype=np.float64) * stride,
            offset=offset,
            output_shape=output_shape,
            order=order,
            mode="constant",
            cval=0.0,
            prefilter=False,
        ),
        dtype=np.float32,
    )


def _spherical_window(shape: tuple[int, int, int]) -> np.ndarray:
    coordinates = np.indices(shape, dtype=np.float32)
    center = (np.asarray(shape, dtype=np.float32) - 1.0) / 2.0
    radius = np.sqrt(sum((coordinates[axis] - center[axis]) ** 2 for axis in range(3)))
    outer = max(2.0, float(np.min(center)))
    inner = 0.88 * outer
    window = np.ones(shape, dtype=np.float32)
    window[radius >= outer] = 0.0
    transition = (radius > inner) & (radius < outer)
    window[transition] = 0.5 + 0.5 * np.cos(
        np.pi * (radius[transition] - inner) / (outer - inner)
    )
    return window


def _weighted_mean_std(array: np.ndarray, weights: np.ndarray) -> tuple[float, float]:
    valid = np.isfinite(array) & np.isfinite(weights) & (weights > 1e-6)
    if int(np.count_nonzero(valid)) < 16:
        return 0.0, 0.0
    values = np.asarray(array[valid], dtype=np.float64)
    selected_weights = np.asarray(weights[valid], dtype=np.float64)
    total = float(np.sum(selected_weights))
    if total <= np.finfo(np.float64).eps:
        return 0.0, 0.0
    mean = float(np.sum(selected_weights * values) / total)
    variance = float(np.sum(selected_weights * (values - mean) ** 2) / total)
    return mean, math.sqrt(max(0.0, variance))


def _standardize(array: np.ndarray, weights: np.ndarray) -> np.ndarray:
    mean, std = _weighted_mean_std(array, weights)
    if std <= np.finfo(np.float64).tiny:
        raise MapComparisonError("Alignment support contains insufficient density variance.")
    return np.asarray((array - mean) / std, dtype=np.float32)


def _weighted_similarity(
    reference: np.ndarray,
    candidate: np.ndarray,
    weights: np.ndarray,
) -> dict[str, float | int | None]:
    valid = (
        np.isfinite(reference)
        & np.isfinite(candidate)
        & np.isfinite(weights)
        & (weights > 1e-6)
    )
    count = int(np.count_nonzero(valid))
    if count < 16:
        return {
            "pearson_correlation": None,
            "zscore_rms_difference": None,
            "support_voxels": count,
            "support_weight": 0.0,
        }
    x = np.asarray(reference[valid], dtype=np.float64)
    y = np.asarray(candidate[valid], dtype=np.float64)
    w = np.asarray(weights[valid], dtype=np.float64)
    total = float(np.sum(w))
    mean_x = float(np.sum(w * x) / total)
    mean_y = float(np.sum(w * y) / total)
    centered_x = x - mean_x
    centered_y = y - mean_y
    variance_x = float(np.sum(w * centered_x**2) / total)
    variance_y = float(np.sum(w * centered_y**2) / total)
    denominator = math.sqrt(variance_x * variance_y)
    if denominator <= np.finfo(np.float64).tiny:
        correlation = None
        rms = None
    else:
        correlation = float(
            np.clip(np.sum(w * centered_x * centered_y) / total / denominator, -1.0, 1.0)
        )
        standardized_x = centered_x / math.sqrt(variance_x)
        standardized_y = centered_y / math.sqrt(variance_y)
        rms = float(np.sqrt(np.sum(w * (standardized_x - standardized_y) ** 2) / total))
    return {
        "pearson_correlation": correlation,
        "zscore_rms_difference": rms,
        "support_voxels": count,
        "support_weight": total,
    }


def _threshold_overlap(
    reference: np.ndarray,
    candidate: np.ndarray,
    weights: np.ndarray,
    threshold_sigma: float,
) -> dict[str, float | None]:
    mean_reference, std_reference = _weighted_mean_std(reference, weights)
    mean_candidate, std_candidate = _weighted_mean_std(candidate, weights)
    reference_threshold = mean_reference + threshold_sigma * std_reference
    candidate_threshold = mean_candidate + threshold_sigma * std_candidate
    if std_reference <= 0 or std_candidate <= 0:
        return {
            "dice": None,
            "jaccard": None,
            "weighted_union": 0.0,
            "reference_threshold": reference_threshold,
            "candidate_threshold": candidate_threshold,
        }
    support = weights > 1e-6
    reference_mask = support & (reference >= reference_threshold)
    candidate_mask = support & (candidate >= candidate_threshold)
    intersection = float(np.sum(weights[reference_mask & candidate_mask], dtype=np.float64))
    union = float(np.sum(weights[reference_mask | candidate_mask], dtype=np.float64))
    total = float(
        np.sum(weights[reference_mask], dtype=np.float64)
        + np.sum(weights[candidate_mask], dtype=np.float64)
    )
    return {
        "dice": float(2.0 * intersection / total) if total > 0 else None,
        "jaccard": float(intersection / union) if union > 0 else None,
        "weighted_union": union,
        "reference_threshold": reference_threshold,
        "candidate_threshold": candidate_threshold,
    }


def _effective_sample_size(weights: np.ndarray) -> float:
    selected = np.asarray(weights[weights > 1e-6], dtype=np.float64)
    if selected.size == 0:
        return 0.0
    total = float(np.sum(selected))
    squared = float(np.sum(selected**2))
    return total * total / squared if squared > np.finfo(np.float64).tiny else 0.0


def _compact_xy(array: np.ndarray, *, max_side: int = 192) -> np.ndarray:
    """Return a bounded, anti-aliased central XY plane for report rendering."""
    plane = np.asarray(array[array.shape[0] // 2], dtype=np.float32)
    stride = max(1, math.ceil(max(plane.shape) / max_side))
    if stride > 1:
        plane = ndimage.gaussian_filter(
            plane,
            sigma=0.5 * stride,
            mode="nearest",
        )
    return np.asarray(plane[::stride, ::stride], dtype=np.float32)


def _rotation_matrix_zyx(angles_xyz_deg: Sequence[float]) -> np.ndarray:
    matrix_xyz = Rotation.from_euler("xyz", angles_xyz_deg, degrees=True).as_matrix()
    xyz_to_zyx = np.asarray(
        ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0), (1.0, 0.0, 0.0)),
        dtype=np.float64,
    )
    return xyz_to_zyx @ matrix_xyz @ xyz_to_zyx


def _transform_candidate(
    candidate: np.ndarray,
    angles_xyz_deg: Sequence[float],
    translation_zyx_pix: Sequence[float],
    *,
    order: int,
    cval: float,
) -> np.ndarray:
    """Apply a forward candidate-to-reference transform about the array center."""
    rotation_zyx = _rotation_matrix_zyx(angles_xyz_deg)
    inverse = rotation_zyx.T
    center = (np.asarray(candidate.shape, dtype=np.float64) - 1.0) / 2.0
    translation = np.asarray(translation_zyx_pix, dtype=np.float64)
    offset = center - inverse @ (center + translation)
    return np.asarray(
        ndimage.affine_transform(
            np.asarray(candidate, dtype=np.float32),
            matrix=inverse,
            offset=offset,
            output_shape=candidate.shape,
            order=order,
            mode="constant",
            cval=float(cval),
            prefilter=order > 1,
        ),
        dtype=np.float32,
    )


def _phase_translation(
    reference: np.ndarray,
    candidate: np.ndarray,
    weights: np.ndarray,
    max_shift_pix: float,
) -> np.ndarray:
    weighted_reference = np.asarray(reference * weights, dtype=np.float32)
    weighted_candidate = np.asarray(candidate * weights, dtype=np.float32)
    reference_spectrum = fft.rfftn(weighted_reference)
    candidate_spectrum = fft.rfftn(weighted_candidate)
    cross = reference_spectrum * np.conj(candidate_spectrum)
    magnitude = np.abs(cross)
    cross = np.divide(
        cross,
        magnitude,
        out=np.zeros_like(cross),
        where=magnitude > np.finfo(np.float32).eps,
    )
    correlation = np.abs(fft.irfftn(cross, s=reference.shape))
    peak = np.asarray(np.unravel_index(int(np.argmax(correlation)), correlation.shape))
    shift = peak.astype(np.float64)
    for axis, size in enumerate(reference.shape):
        if shift[axis] > size // 2:
            shift[axis] -= size
        index = int(peak[axis])
        before_index = [int(item) for item in peak]
        after_index = [int(item) for item in peak]
        before_index[axis] = (index - 1) % size
        after_index[axis] = (index + 1) % size
        before = float(correlation[tuple(before_index)])
        center = float(correlation[tuple(peak)])
        after = float(correlation[tuple(after_index)])
        denominator = before - 2.0 * center + after
        if abs(denominator) > np.finfo(np.float64).eps:
            delta = 0.5 * (before - after) / denominator
            shift[axis] += float(np.clip(delta, -0.5, 0.5))
    return np.clip(shift, -max_shift_pix, max_shift_pix)


def _boundary_value(volume: np.ndarray) -> float:
    faces = np.concatenate(
        [
            volume[0].reshape(-1),
            volume[-1].reshape(-1),
            volume[:, 0, :].reshape(-1),
            volume[:, -1, :].reshape(-1),
            volume[:, :, 0].reshape(-1),
            volume[:, :, -1].reshape(-1),
        ]
    )
    return float(np.median(np.asarray(faces, dtype=np.float64)))


def _fit_transform(
    reference: np.ndarray,
    candidate: np.ndarray,
    weights: np.ndarray,
    *,
    alignment_mode: Literal["translation", "rigid"],
    max_rotation_deg: float,
    max_shift_pix: float,
    registration_resolution_pix: float,
) -> dict[str, Any]:
    """Fit from several deterministic starts and audit solution stability."""
    reference_standard = _standardize(reference, weights)
    candidate_standard = _standardize(candidate, weights)

    if alignment_mode == "rigid":
        half_rotation = max_rotation_deg / 2.0
        rotation_seeds = [np.zeros(3, dtype=np.float64)]
        for axis in range(3):
            for sign in (-1.0, 1.0):
                seed = np.zeros(3, dtype=np.float64)
                seed[axis] = sign * half_rotation
                rotation_seeds.append(seed)
    else:
        rotation_seeds = [np.zeros(3, dtype=np.float64)]

    def score(angles: np.ndarray, shift: np.ndarray) -> float:
        moved = _transform_candidate(
            candidate_standard,
            angles,
            shift,
            order=1,
            cval=0.0,
        )
        similarity = _weighted_similarity(reference_standard, moved, weights)
        value = similarity["pearson_correlation"]
        return -float(value) if value is not None else 1.0

    seeded: list[dict[str, Any]] = []
    for angles in rotation_seeds:
        rotated = _transform_candidate(
            candidate_standard,
            angles,
            (0.0, 0.0, 0.0),
            order=1,
            cval=0.0,
        )
        shift = _phase_translation(
            reference_standard,
            rotated,
            weights,
            max_shift_pix,
        )
        seeded.append(
            {
                "correlation": -score(angles, shift),
                "angles_xyz_deg": np.asarray(angles, dtype=np.float64),
                "translation_zyx_pix": np.asarray(shift, dtype=np.float64),
            }
        )

    selected_starts = sorted(
        seeded,
        key=lambda item: float(item["correlation"]),
        reverse=True,
    )[:_MAX_OPTIMIZER_STARTS]
    evaluations_per_start = max(
        1,
        _MAX_FUNCTION_EVALUATIONS // len(selected_starts),
    )
    bounds = (
        [(-max_shift_pix, max_shift_pix)] * 3
        if alignment_mode == "translation"
        else [(-max_rotation_deg, max_rotation_deg)] * 3
        + [(-max_shift_pix, max_shift_pix)] * 3
    )

    def unpack(parameters: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if alignment_mode == "translation":
            return np.zeros(3, dtype=np.float64), np.asarray(
                parameters,
                dtype=np.float64,
            )
        return (
            np.asarray(parameters[:3], dtype=np.float64),
            np.asarray(parameters[3:], dtype=np.float64),
        )

    def objective(parameters: np.ndarray) -> float:
        angles, shift = unpack(parameters)
        return score(angles, shift)

    solutions: list[dict[str, Any]] = []
    total_function_evaluations = 0
    for start_rank, start in enumerate(selected_starts, start=1):
        initial = (
            np.asarray(start["translation_zyx_pix"], dtype=np.float64)
            if alignment_mode == "translation"
            else np.concatenate(
                (
                    np.asarray(start["angles_xyz_deg"], dtype=np.float64),
                    np.asarray(start["translation_zyx_pix"], dtype=np.float64),
                )
            )
        )
        result = optimize.minimize(
            objective,
            initial,
            method="Powell",
            bounds=bounds,
            options={
                "maxiter": 100,
                "maxfev": evaluations_per_start,
                "xtol": 1e-2,
                "ftol": 1e-5,
            },
        )
        total_function_evaluations += int(result.nfev)
        optimized = np.asarray(result.x, dtype=np.float64)
        optimized_correlation = -float(objective(optimized))
        seed_correlation = float(start["correlation"])
        if optimized_correlation >= seed_correlation:
            chosen_parameters = optimized
            chosen_correlation = optimized_correlation
            source = "powell"
            converged = bool(result.success)
        else:
            chosen_parameters = initial
            chosen_correlation = seed_correlation
            source = "phase_translation_seed_fallback"
            converged = False
        angles, shift = unpack(chosen_parameters)
        solutions.append(
            {
                "angles_xyz_deg": angles,
                "translation_zyx_pix": shift,
                "correlation": chosen_correlation,
                "source": source,
                "converged": converged,
                "optimizer_reported_success": bool(result.success),
                "optimizer_message": str(result.message),
                "function_evaluations": int(result.nfev),
                "start_rank": start_rank,
            }
        )

    identity_angles = np.zeros(3, dtype=np.float64)
    identity_shift = np.zeros(3, dtype=np.float64)
    identity_score = -score(identity_angles, identity_shift)
    identity_solution = {
        "angles_xyz_deg": identity_angles,
        "translation_zyx_pix": identity_shift,
        "correlation": identity_score,
        "source": "exact_identity",
        "converged": True,
        "optimizer_reported_success": True,
        "optimizer_message": "Exact identity was evaluated explicitly.",
        "function_evaluations": 0,
        "start_rank": 0,
    }
    solutions.append(identity_solution)
    best = max(solutions, key=lambda item: float(item["correlation"]))
    if float(best["correlation"]) - identity_score <= _MIN_NUMERICAL_OBJECTIVE_GAIN:
        best = identity_solution

    angles = np.asarray(best["angles_xyz_deg"], dtype=np.float64)
    shift = np.asarray(best["translation_zyx_pix"], dtype=np.float64)
    final_score = float(best["correlation"])
    objective_gain = final_score - identity_score

    competitive_tolerance = max(
        5e-4,
        min(0.01, 0.05 * max(0.0, objective_gain)),
    )
    translation_agreement_tolerance_pix = max(
        0.5,
        0.5 * registration_resolution_pix,
    )
    scoring_radius_pix = max(1.0, (min(reference.shape) - 1.0) / 2.0)
    rotation_agreement_tolerance_deg = max(
        0.5,
        math.degrees(
            math.atan2(
                0.5 * registration_resolution_pix,
                scoring_radius_pix,
            )
        ),
    )
    competitive: list[dict[str, Any]] = []
    best_rotation = Rotation.from_euler("xyz", angles, degrees=True)
    for alternative in solutions:
        if alternative is best:
            continue
        correlation_gap = final_score - float(alternative["correlation"])
        if correlation_gap > competitive_tolerance:
            continue
        alternative_angles = np.asarray(
            alternative["angles_xyz_deg"],
            dtype=np.float64,
        )
        alternative_rotation = Rotation.from_euler(
            "xyz",
            alternative_angles,
            degrees=True,
        )
        rotation_separation_deg = float(
            (best_rotation.inv() * alternative_rotation).magnitude() * 180.0 / np.pi
        )
        translation_separation_pix = float(
            np.linalg.norm(
                shift
                - np.asarray(
                    alternative["translation_zyx_pix"],
                    dtype=np.float64,
                )
            )
        )
        competitive.append(
            {
                "source": alternative["source"],
                "correlation_gap": correlation_gap,
                "rotation_separation_deg": rotation_separation_deg,
                "translation_separation_analysis_pixels": (
                    translation_separation_pix
                ),
                "materially_different_transform": bool(
                    rotation_separation_deg > rotation_agreement_tolerance_deg
                    or translation_separation_pix
                    > translation_agreement_tolerance_pix
                ),
            }
        )
    solution_stable = not any(
        item["materially_different_transform"] for item in competitive
    )

    bound_contacts: list[str] = []
    names = (
        ["rotation_x_deg", "rotation_y_deg", "rotation_z_deg"]
        if alignment_mode == "rigid"
        else []
    ) + ["translation_z_pix", "translation_y_pix", "translation_x_pix"]
    values = list(angles if alignment_mode == "rigid" else ()) + list(shift)
    limits = ([max_rotation_deg] * 3 if alignment_mode == "rigid" else []) + [
        max_shift_pix
    ] * 3
    for name, value, limit in zip(names, values, limits, strict=True):
        tolerance = max(1e-3, _BOUND_TOLERANCE_FRACTION * limit)
        if limit > 0 and abs(abs(float(value)) - limit) <= tolerance:
            bound_contacts.append(name)

    return {
        "angles_xyz_deg": angles,
        "translation_zyx_pix": shift,
        "identity_objective_correlation": identity_score,
        "final_objective_correlation": final_score,
        "objective_gain": objective_gain,
        "identity_selected": best is identity_solution,
        "optimizer_success": bool(best["converged"]),
        "optimizer_message": str(best["optimizer_message"]),
        "selected_solution_source": str(best["source"]),
        "function_evaluations": total_function_evaluations,
        "bound_contacts": bound_contacts,
        "seed_count": len(rotation_seeds),
        "optimizer_start_count": len(selected_starts),
        "solution_stable": solution_stable,
        "competitive_solutions": competitive,
        "competitive_correlation_tolerance": competitive_tolerance,
        "rotation_agreement_tolerance_deg": rotation_agreement_tolerance_deg,
        "translation_agreement_tolerance_analysis_pixels": (
            translation_agreement_tolerance_pix
        ),
        "minimum_numerical_objective_gain": _MIN_NUMERICAL_OBJECTIVE_GAIN,
    }


def _assess_transform_repeatability(
    reference: np.ndarray,
    candidate: np.ndarray,
    weights: np.ndarray,
    *,
    selected_angles_xyz_deg: np.ndarray,
    selected_translation_zyx_pix: np.ndarray,
    alignment_mode: Literal["translation", "rigid"],
    max_rotation_deg: float,
    max_shift_pix: float,
    registration_resolution_pix: float,
    analysis_sampling_A: float,
) -> dict[str, Any]:
    """Audit one selected transform with deterministic held-out spatial folds."""
    block_size = max(2, round(registration_resolution_pix))
    z_blocks = np.arange(reference.shape[0], dtype=np.int16)[:, None, None] // block_size
    y_blocks = np.arange(reference.shape[1], dtype=np.int16)[None, :, None] // block_size
    x_blocks = np.arange(reference.shape[2], dtype=np.int16)[None, None, :] // block_size
    fold_ids = np.asarray(
        (z_blocks + 2 * y_blocks + 3 * x_blocks) % _STABILITY_FOLDS,
        dtype=np.uint8,
    )
    y_block_count = math.ceil(reference.shape[1] / block_size)
    x_block_count = math.ceil(reference.shape[2] / block_size)
    spatial_block_ids = np.asarray(
        z_blocks * y_block_count * x_block_count
        + y_blocks * x_block_count
        + x_blocks,
        dtype=np.int32,
    )
    parameter_count = 3 if alignment_mode == "translation" else 6
    minimum_supported_resolution_blocks = 2 * (parameter_count + 1)
    rotation_patterns = np.asarray(
        (
            (0.0, 0.0, 0.0),
            (1.0, -1.0, 0.0),
            (-1.0, 0.0, 1.0),
            (0.0, 1.0, -1.0),
            (1.0, 1.0, -1.0),
        ),
        dtype=np.float64,
    )
    shift_patterns = np.asarray(
        (
            (0.0, 0.0, 0.0),
            (1.0, 0.0, -1.0),
            (0.0, 1.0, -1.0),
            (-1.0, 1.0, 0.0),
            (-1.0, 0.0, 1.0),
        ),
        dtype=np.float64,
    )
    scoring_radius_pix = max(1.0, (min(reference.shape) - 1.0) / 2.0)
    rotation_perturbation_deg = min(
        max_rotation_deg / 4.0,
        math.degrees(math.atan2(0.5, scoring_radius_pix)),
    )
    translation_perturbation_pix = 0.5
    bounds = (
        [(-max_shift_pix, max_shift_pix)] * 3
        if alignment_mode == "translation"
        else [(-max_rotation_deg, max_rotation_deg)] * 3
        + [(-max_shift_pix, max_shift_pix)] * 3
    )

    def unpack(parameters: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if alignment_mode == "translation":
            return np.zeros(3, dtype=np.float64), np.asarray(
                parameters,
                dtype=np.float64,
            )
        return (
            np.asarray(parameters[:3], dtype=np.float64),
            np.asarray(parameters[3:], dtype=np.float64),
        )

    def pose_displacement_A(
        first_angles: np.ndarray,
        first_shift: np.ndarray,
        second_angles: np.ndarray,
        second_shift: np.ndarray,
    ) -> float:
        delta_rotation = _rotation_matrix_zyx(first_angles) - _rotation_matrix_zyx(
            second_angles
        )
        delta_translation = np.asarray(first_shift - second_shift, dtype=np.float64)
        centered = [
            np.arange(size, dtype=np.float32) - (size - 1.0) / 2.0
            for size in reference.shape
        ]
        displacement_squared = np.zeros(reference.shape, dtype=np.float32)
        for output_axis in range(3):
            component = (
                delta_rotation[output_axis, 0] * centered[0][:, None, None]
                + delta_rotation[output_axis, 1] * centered[1][None, :, None]
                + delta_rotation[output_axis, 2] * centered[2][None, None, :]
                + delta_translation[output_axis]
            )
            displacement_squared += np.asarray(component**2, dtype=np.float32)
        denominator = float(np.sum(weights, dtype=np.float64))
        mean_squared = float(
            np.sum(weights * displacement_squared, dtype=np.float64) / denominator
        )
        return math.sqrt(max(0.0, mean_squared)) * analysis_sampling_A

    probes: list[dict[str, Any]] = []
    poses: list[tuple[np.ndarray, np.ndarray] | None] = []
    held_out_fisher_gains: list[float] = []
    total_function_evaluations = 0
    for fold in range(_STABILITY_FOLDS):
        train_weights = np.asarray(weights * (fold_ids != fold), dtype=np.float32)
        held_weights = np.asarray(weights * (fold_ids == fold), dtype=np.float32)
        held_supported_blocks = int(
            np.unique(
                spatial_block_ids[
                    (fold_ids == fold) & (held_weights > 1e-6)
                ]
            ).size
        )
        record: dict[str, Any] = {
            "fold": fold,
            "train_effective_sample_size": _effective_sample_size(train_weights),
            "held_out_effective_sample_size": _effective_sample_size(held_weights),
            "held_out_supported_resolution_blocks": held_supported_blocks,
        }
        if held_supported_blocks < minimum_supported_resolution_blocks:
            record.update(
                {
                    "valid": False,
                    "failure": (
                        "The held-out fold contains too few supported resolution "
                        "blocks for pose validation."
                    ),
                }
            )
            probes.append(record)
            poses.append(None)
            continue
        try:
            reference_standard = _standardize(reference, train_weights)
            candidate_standard = _standardize(candidate, train_weights)
        except MapComparisonError as exc:
            record.update({"valid": False, "failure": str(exc)})
            probes.append(record)
            poses.append(None)
            continue

        start_angles = np.asarray(selected_angles_xyz_deg, dtype=np.float64)
        if alignment_mode == "rigid":
            start_angles = np.clip(
                start_angles + rotation_patterns[fold] * rotation_perturbation_deg,
                -max_rotation_deg,
                max_rotation_deg,
            )
        start_shift = np.clip(
            np.asarray(selected_translation_zyx_pix, dtype=np.float64)
            + shift_patterns[fold] * translation_perturbation_pix,
            -max_shift_pix,
            max_shift_pix,
        )
        initial = (
            start_shift
            if alignment_mode == "translation"
            else np.concatenate((start_angles, start_shift))
        )

        def objective(
            parameters: np.ndarray,
            reference_for_fold: np.ndarray = reference_standard,
            candidate_for_fold: np.ndarray = candidate_standard,
            weights_for_fold: np.ndarray = train_weights,
        ) -> float:
            angles, shift = unpack(parameters)
            moved = _transform_candidate(
                candidate_for_fold,
                angles,
                shift,
                order=1,
                cval=0.0,
            )
            similarity = _weighted_similarity(
                reference_for_fold,
                moved,
                weights_for_fold,
            )["pearson_correlation"]
            return -float(similarity) if similarity is not None else 1.0

        result = optimize.minimize(
            objective,
            initial,
            method="Powell",
            bounds=bounds,
            options={
                "maxiter": 100,
                "maxfev": _MAX_STABILITY_EVALUATIONS_PER_FOLD,
                "xtol": 1e-2,
                "ftol": _STABILITY_OPTIMIZER_FTOL,
            },
        )
        total_function_evaluations += int(result.nfev)
        optimized = np.asarray(result.x, dtype=np.float64)
        chosen = optimized if objective(optimized) <= objective(initial) else initial
        angles, shift = unpack(chosen)
        if not np.all(np.isfinite(chosen)):
            record.update(
                {
                    "valid": False,
                    "failure": "The stability optimizer returned non-finite parameters.",
                }
            )
            probes.append(record)
            poses.append(None)
            continue

        bound_contacts: list[str] = []
        names = (
            ["rotation_x_deg", "rotation_y_deg", "rotation_z_deg"]
            if alignment_mode == "rigid"
            else []
        ) + ["translation_z_pix", "translation_y_pix", "translation_x_pix"]
        values = list(angles if alignment_mode == "rigid" else ()) + list(shift)
        limits = (
            [max_rotation_deg] * 3 if alignment_mode == "rigid" else []
        ) + [max_shift_pix] * 3
        for name, value, limit in zip(names, values, limits, strict=True):
            tolerance = max(1e-3, _BOUND_TOLERANCE_FRACTION * limit)
            if limit > 0 and abs(abs(float(value)) - limit) <= tolerance:
                bound_contacts.append(name)

        transformed_support = np.clip(
            _transform_candidate(
                np.ones(reference.shape, dtype=np.float32),
                angles,
                shift,
                order=1,
                cval=0.0,
            ),
            0.0,
            1.0,
        )
        support_denominator = float(np.sum(weights, dtype=np.float64))
        overlap_fraction = float(
            np.sum(weights * transformed_support, dtype=np.float64)
            / support_denominator
        )
        held_common_weights = np.asarray(
            held_weights * transformed_support,
            dtype=np.float32,
        )
        moved_candidate = _transform_candidate(
            candidate,
            angles,
            shift,
            order=1,
            cval=_boundary_value(candidate),
        )
        identity_correlation = _weighted_similarity(
            reference,
            candidate,
            held_common_weights,
        )["pearson_correlation"]
        moved_correlation = _weighted_similarity(
            reference,
            moved_candidate,
            held_common_weights,
        )["pearson_correlation"]
        fisher_gain: float | None = None
        if identity_correlation is not None and moved_correlation is not None:
            fisher_gain = float(
                np.arctanh(np.clip(moved_correlation, -0.999999, 0.999999))
                - np.arctanh(np.clip(identity_correlation, -0.999999, 0.999999))
            )
            held_out_fisher_gains.append(fisher_gain)

        displacement_A = pose_displacement_A(
            angles,
            shift,
            selected_angles_xyz_deg,
            selected_translation_zyx_pix,
        )
        hard_failure = bool(
            bound_contacts
            or overlap_fraction < _MIN_VALID_SUPPORT_FRACTION
            or fisher_gain is None
        )
        record.update(
            {
                "valid": not hard_failure,
                "optimizer_reported_success": bool(result.success),
                "optimizer_message": str(result.message),
                "function_evaluations": int(result.nfev),
                "rotation_euler_xyz_degrees": angles.tolist(),
                "translation_analysis_zyx_pixels": shift.tolist(),
                "bound_contacts": bound_contacts,
                "valid_support_fraction": overlap_fraction,
                "displacement_from_selected_transform_A": displacement_A,
                "held_out_identity_correlation": identity_correlation,
                "held_out_transformed_correlation": moved_correlation,
                "held_out_fisher_z_gain": fisher_gain,
            }
        )
        probes.append(record)
        poses.append((angles, shift))

    displacement_tolerance_A = min(
        0.5 * analysis_sampling_A,
        max(
            0.25 * analysis_sampling_A,
            registration_resolution_pix * analysis_sampling_A / 20.0,
        ),
    )
    within_tolerance = sum(
        bool(
            record.get("valid")
            and record.get("displacement_from_selected_transform_A", math.inf)
            <= displacement_tolerance_A
        )
        for record in probes
    )
    valid_pose_indices = [index for index, pose in enumerate(poses) if pose is not None]
    medoid_index: int | None = None
    maximum_medoid_spread_A: float | None = None
    if valid_pose_indices:
        pairwise = np.zeros((len(valid_pose_indices), len(valid_pose_indices)))
        for row, first_index in enumerate(valid_pose_indices):
            first = poses[first_index]
            assert first is not None
            for column in range(row + 1, len(valid_pose_indices)):
                second_index = valid_pose_indices[column]
                second = poses[second_index]
                assert second is not None
                distance = pose_displacement_A(first[0], first[1], second[0], second[1])
                pairwise[row, column] = distance
                pairwise[column, row] = distance
        local_medoid = int(np.argmin(np.median(pairwise, axis=1)))
        medoid_index = valid_pose_indices[local_medoid]
        maximum_medoid_spread_A = float(np.max(pairwise[local_medoid]))

    pose_consensus_passed = bool(
        sum(bool(record.get("valid")) for record in probes) >= _STABILITY_FOLDS - 1
        and within_tolerance >= _STABILITY_FOLDS - 1
        and maximum_medoid_spread_A is not None
        and maximum_medoid_spread_A <= 2.0 * displacement_tolerance_A
    )
    generalization_epsilon = max(
        1e-4,
        10.0 * _STABILITY_OPTIMIZER_FTOL,
    )
    robust_gain_lower_bound: float | None = None
    if len(held_out_fisher_gains) == _STABILITY_FOLDS:
        gains = np.asarray(held_out_fisher_gains, dtype=np.float64)
        median_gain = float(np.median(gains))
        mad_gain = float(np.median(np.abs(gains - median_gain)))
        robust_gain_lower_bound = median_gain - 2.0 * 1.4826 * mad_gain
    generalization_passed = bool(
        len(held_out_fisher_gains) == _STABILITY_FOLDS
        and all(gain > generalization_epsilon for gain in held_out_fisher_gains)
        and robust_gain_lower_bound is not None
        and robust_gain_lower_bound > generalization_epsilon
    )
    return {
        "method": "five_fold_spatial_block_jackknife_local_pose_probes/v1",
        "selected_transform_refit_or_replaced": False,
        "fold_count": _STABILITY_FOLDS,
        "block_size_analysis_pixels": block_size,
        "minimum_supported_resolution_blocks_per_held_out_fold": (
            minimum_supported_resolution_blocks
        ),
        "rotation_start_perturbation_deg": rotation_perturbation_deg,
        "translation_start_perturbation_analysis_pixels": (
            translation_perturbation_pix
        ),
        "maximum_function_evaluations_per_fold": (
            _MAX_STABILITY_EVALUATIONS_PER_FOLD
        ),
        "total_function_evaluations": total_function_evaluations,
        "displacement_tolerance_A": displacement_tolerance_A,
        "folds_within_displacement_tolerance": within_tolerance,
        "medoid_fold": medoid_index,
        "maximum_medoid_spread_A": maximum_medoid_spread_A,
        "pose_consensus_passed": pose_consensus_passed,
        "held_out_fisher_z_gain_epsilon": generalization_epsilon,
        "robust_held_out_fisher_z_gain_lower_bound": robust_gain_lower_bound,
        "held_out_generalization_passed": generalization_passed,
        "probes": probes,
    }


def _metric_record(
    reference: np.ndarray,
    before: np.ndarray,
    after: np.ndarray,
    weights: np.ndarray,
    threshold_sigma: float,
) -> dict[str, Any]:
    return {
        "before_alignment": {
            "similarity": _weighted_similarity(reference, before, weights),
            "threshold_overlap": _threshold_overlap(
                reference, before, weights, threshold_sigma
            ),
        },
        "after_alignment": {
            "similarity": _weighted_similarity(reference, after, weights),
            "threshold_overlap": _threshold_overlap(
                reference, after, weights, threshold_sigma
            ),
        },
    }


def _effective_alignment_resolution_A(
    shape: Sequence[int],
    voxel_size_A: float,
    requested_resolution_A: float,
    stride: int,
) -> tuple[float, float]:
    """Return an anti-alias-safe cutoff for center-preserving decimation."""
    if stride == 1:
        return requested_resolution_A, 2.0 * voxel_size_A
    coarse_nyquist_frequency = 1.0 / (2.0 * voxel_size_A * stride)
    source_shell_frequency = min(
        1.0 / (int(size) * voxel_size_A) for size in shape
    )
    safe_cutoff_frequency = coarse_nyquist_frequency - source_shell_frequency
    if safe_cutoff_frequency <= 0:
        raise MapComparisonError(
            "The bounded analysis grid cannot retain one anti-aliased Fourier shell."
        )
    safe_resolution_A = 1.0 / safe_cutoff_frequency
    return max(requested_resolution_A, safe_resolution_A), safe_resolution_A


def compare_maps(
    reference_map: np.ndarray,
    candidate_map: np.ndarray,
    *,
    voxel_size_A: float,
    alignment_lowpass_A: float,
    comparison_lowpasses_A: Sequence[float] | None = None,
    mask: np.ndarray | None = None,
    alignment_mode: Literal["translation", "rigid"] = "rigid",
    max_shift_A: float = 15.0,
    max_rotation_deg: float = 15.0,
    threshold_sigma: float = 2.0,
) -> MapComparisonResult:
    """Fit one local transform and compare both maps at fixed physical cutoffs."""
    reference = _validate_volume(reference_map, "reference_map")
    candidate = _validate_volume(candidate_map, "candidate_map")
    if reference.shape != candidate.shape:
        raise MapComparisonError(
            f"Maps must have identical canonical shapes; got {reference.shape} and {candidate.shape}."
        )
    resource_contract = validate_map_comparison_resources(reference.shape)
    validated_mask = _validate_mask(mask, reference.shape)
    sampling_A = _finite_positive(voxel_size_A, "voxel_size_A")
    requested_alignment_A = _finite_positive(
        alignment_lowpass_A, "alignment_lowpass_A"
    )
    nyquist_A = 2.0 * sampling_A
    if requested_alignment_A < nyquist_A and not math.isclose(
        requested_alignment_A, nyquist_A, rel_tol=1e-7, abs_tol=1e-7
    ):
        raise MapComparisonError(
            f"alignment_lowpass_A={requested_alignment_A:g} Å is beyond the "
            f"map Nyquist limit of {nyquist_A:g} Å."
        )
    if alignment_mode not in {"translation", "rigid"}:
        raise MapComparisonError("alignment_mode must be 'translation' or 'rigid'.")
    shift_limit_A = _finite_nonnegative(max_shift_A, "max_shift_A")
    rotation_limit_deg = _finite_nonnegative(max_rotation_deg, "max_rotation_deg")
    if shift_limit_A <= 0:
        raise MapComparisonError("max_shift_A must be greater than zero.")
    minimum_shift_limit_A = 0.5 * sampling_A
    if shift_limit_A < minimum_shift_limit_A:
        raise MapComparisonError(
            "max_shift_A is too small to test identity meaningfully; require at least "
            f"half a source voxel ({minimum_shift_limit_A:g} Å)."
        )
    if shift_limit_A > min(reference.shape) * sampling_A / 4.0:
        raise MapComparisonError(
            "max_shift_A must not exceed one quarter of the shortest map extent."
        )
    if rotation_limit_deg > 30.0:
        raise MapComparisonError(
            "max_rotation_deg must be at most 30 for this local rigid-registration tool."
        )
    if alignment_mode == "rigid" and rotation_limit_deg <= 0:
        raise MapComparisonError(
            "max_rotation_deg must be greater than zero for rigid alignment."
        )
    threshold = _finite_positive(threshold_sigma, "threshold_sigma")
    if threshold > 5.0:
        raise MapComparisonError("threshold_sigma must be at most 5.")

    requested_comparisons = list(comparison_lowpasses_A or ())
    if len(requested_comparisons) > 6:
        raise MapComparisonError("comparison_lowpasses_A accepts at most six cutoffs.")
    comparison_cutoffs: list[float] = []
    for index, value in enumerate(requested_comparisons):
        cutoff = _finite_positive(value, f"comparison_lowpasses_A[{index}]")
        if cutoff < nyquist_A and not math.isclose(
            cutoff, nyquist_A, rel_tol=1e-7, abs_tol=1e-7
        ):
            raise MapComparisonError(
                f"comparison_lowpasses_A[{index}]={cutoff:g} Å is beyond the "
                f"map Nyquist limit of {nyquist_A:g} Å."
            )
        if not any(math.isclose(cutoff, item, rel_tol=1e-9, abs_tol=1e-9) for item in comparison_cutoffs):
            comparison_cutoffs.append(cutoff)

    stride = max(1, math.ceil(max(reference.shape) / _ANALYSIS_MAX_SIDE))
    analysis_sampling_A = sampling_A * stride
    effective_alignment_A, alias_safe_minimum_A = _effective_alignment_resolution_A(
        reference.shape,
        sampling_A,
        requested_alignment_A,
        stride,
    )
    shortest_extent_A = min(reference.shape) * sampling_A
    if effective_alignment_A > shortest_extent_A / 3.0:
        raise MapComparisonError(
            "The effective alignment low-pass retains fewer than three Fourier-radius "
            "scoring shells on the shortest map axis."
        )
    reference_alignment = _raised_cosine_lowpass(
        reference, sampling_A, effective_alignment_A
    )
    candidate_alignment = _raised_cosine_lowpass(
        candidate, sampling_A, effective_alignment_A
    )
    reference_analysis = _centered_resample(reference_alignment, stride, order=1)
    candidate_analysis = _centered_resample(candidate_alignment, stride, order=1)
    scoring_radius_analysis_pix = max(
        1.0,
        (min(reference_analysis.shape) - 1.0) / 2.0,
    )
    minimum_rotation_limit_deg = math.degrees(
        math.atan2(0.5, scoring_radius_analysis_pix)
    )
    if (
        alignment_mode == "rigid"
        and rotation_limit_deg < minimum_rotation_limit_deg
    ):
        raise MapComparisonError(
            "max_rotation_deg is too small to test rigid identity meaningfully; "
            f"require at least {minimum_rotation_limit_deg:g} degrees on the "
            "bounded analysis grid."
        )
    window_analysis = _spherical_window(reference_analysis.shape)
    if validated_mask is not None:
        mask_for_analysis = (
            _raised_cosine_lowpass(
                validated_mask,
                sampling_A,
                alias_safe_minimum_A,
            )
            if stride > 1
            else validated_mask
        )
        mask_analysis = np.clip(
            _centered_resample(mask_for_analysis, stride, order=1),
            0.0,
            1.0,
        )
        weights_analysis = np.asarray(window_analysis * mask_analysis, dtype=np.float32)
        support_type = "caller_mask_times_soft_spherical_edge_taper"
    else:
        weights_analysis = window_analysis
        support_type = "soft_spherical_edge_taper"
    analysis_support_voxels = int(np.count_nonzero(weights_analysis > 1e-6))
    analysis_effective_sample_size = _effective_sample_size(weights_analysis)
    minimum_effective_sample_size = max(
        _MIN_EFFECTIVE_SUPPORT_VOXELS,
        math.ceil(_MIN_EFFECTIVE_SUPPORT_FRACTION * weights_analysis.size),
    )
    if analysis_effective_sample_size < minimum_effective_sample_size:
        raise MapComparisonError(
            "Alignment support is too small or concentrated for a six-parameter fit: "
            f"effective sample size {analysis_effective_sample_size:.1f} < "
            f"{minimum_effective_sample_size}."
        )

    fitted = _fit_transform(
        reference_analysis,
        candidate_analysis,
        weights_analysis,
        alignment_mode=alignment_mode,
        max_rotation_deg=rotation_limit_deg,
        max_shift_pix=shift_limit_A / analysis_sampling_A,
        registration_resolution_pix=effective_alignment_A / analysis_sampling_A,
    )
    angles_xyz_deg = np.asarray(fitted["angles_xyz_deg"], dtype=np.float64)
    translation_analysis_zyx = np.asarray(
        fitted["translation_zyx_pix"], dtype=np.float64
    )
    translation_full_zyx = translation_analysis_zyx * stride

    candidate_after_alignment = _transform_candidate(
        candidate_alignment,
        angles_xyz_deg,
        translation_full_zyx,
        order=1,
        cval=_boundary_value(candidate_alignment),
    )
    valid_support = np.clip(
        _transform_candidate(
            np.ones(reference.shape, dtype=np.float32),
            angles_xyz_deg,
            translation_full_zyx,
            order=1,
            cval=0.0,
        ),
        0.0,
        1.0,
    )
    full_window = _spherical_window(reference.shape)
    full_weights = np.asarray(full_window * valid_support, dtype=np.float32)
    if validated_mask is not None:
        full_weights *= validated_mask
    base_support = full_window if validated_mask is None else full_window * validated_mask
    support_denominator = float(np.sum(base_support, dtype=np.float64))
    valid_support_fraction = (
        float(np.sum(full_weights, dtype=np.float64) / support_denominator)
        if support_denominator > 0
        else 0.0
    )

    exact_canonical_voxel_identity = bool(np.array_equal(reference, candidate))
    registration_validation: dict[str, Any] = {
        "performed": False,
        "reason": "base_fit_not_eligible_for_repeatability_probes",
    }
    if fitted["identity_selected"] and exact_canonical_voxel_identity:
        registration_validation = {
            "performed": False,
            "reason": "exact_canonical_voxel_identity",
            "pose_consensus_passed": True,
            "held_out_generalization_passed": True,
            "identity_validation_basis": "exact_equal_canonical_float32_voxels",
        }
    elif (
        valid_support_fraction >= _MIN_VALID_SUPPORT_FRACTION
        and not fitted["bound_contacts"]
        and fitted["optimizer_success"]
        and fitted["solution_stable"]
    ):
        registration_validation = {
            "performed": True,
            **_assess_transform_repeatability(
                reference_analysis,
                candidate_analysis,
                weights_analysis,
                selected_angles_xyz_deg=angles_xyz_deg,
                selected_translation_zyx_pix=translation_analysis_zyx,
                alignment_mode=alignment_mode,
                max_rotation_deg=rotation_limit_deg,
                max_shift_pix=shift_limit_A / analysis_sampling_A,
                registration_resolution_pix=(
                    effective_alignment_A / analysis_sampling_A
                ),
                analysis_sampling_A=analysis_sampling_A,
            ),
        }

    if valid_support_fraction < _MIN_VALID_SUPPORT_FRACTION:
        status = "insufficient_overlap"
    elif fitted["bound_contacts"]:
        status = "boundary_limited"
    elif not fitted["optimizer_success"]:
        status = "optimizer_stopped"
    elif fitted["identity_selected"] and exact_canonical_voxel_identity:
        status = "identity_optimal"
    elif not fitted["solution_stable"]:
        status = "ambiguous_alignment"
    elif not registration_validation.get("pose_consensus_passed", False):
        status = "unstable_alignment"
    elif fitted["identity_selected"]:
        status = "identity_optimal"
    elif not registration_validation.get("held_out_generalization_passed", False):
        status = "fit_unverified"
    else:
        status = "aligned"

    metric_cutoffs = [effective_alignment_A]
    for cutoff in comparison_cutoffs:
        if not any(math.isclose(cutoff, item, rel_tol=1e-9, abs_tol=1e-9) for item in metric_cutoffs):
            metric_cutoffs.append(cutoff)
    metrics: list[dict[str, Any]] = []
    cutoff_views: list[MapComparisonCutoffView] = []
    for cutoff in metric_cutoffs:
        if math.isclose(cutoff, effective_alignment_A, rel_tol=1e-9, abs_tol=1e-9):
            reference_filtered = reference_alignment
            candidate_filtered = candidate_alignment
            candidate_aligned = candidate_after_alignment
        else:
            reference_filtered = _raised_cosine_lowpass(reference, sampling_A, cutoff)
            candidate_filtered = _raised_cosine_lowpass(candidate, sampling_A, cutoff)
            candidate_aligned = _transform_candidate(
                candidate_filtered,
                angles_xyz_deg,
                translation_full_zyx,
                order=1,
                cval=_boundary_value(candidate_filtered),
            )
        record = {
            "cutoff_resolution_A": cutoff,
            "is_alignment_objective_cutoff": math.isclose(
                cutoff, effective_alignment_A, rel_tol=1e-9, abs_tol=1e-9
            ),
            "transform_refit": False,
        }
        record.update(
            _metric_record(
                reference_filtered,
                candidate_filtered,
                candidate_aligned,
                full_weights,
                threshold,
            )
        )
        metrics.append(record)
        cutoff_views.append(
            MapComparisonCutoffView(
                cutoff_resolution_A=cutoff,
                reference_xy=_compact_xy(reference_filtered),
                candidate_before_xy=_compact_xy(candidate_filtered),
                candidate_after_xy=_compact_xy(candidate_aligned),
                metric_weights_xy=_compact_xy(full_weights),
            )
        )

    rotation_matrix_xyz = Rotation.from_euler(
        "xyz", angles_xyz_deg, degrees=True
    ).as_matrix()
    translation_full_xyz = translation_full_zyx[::-1]
    translation_centered_grid_xyz_A = translation_full_xyz * sampling_A
    center_zyx = (np.asarray(reference.shape, dtype=np.float64) - 1.0) / 2.0
    center_xyz = center_zyx[::-1]
    index_offset_xyz = (
        center_xyz
        - rotation_matrix_xyz @ center_xyz
        + translation_full_xyz
    )
    homogeneous_index_transform = np.eye(4, dtype=np.float64)
    homogeneous_index_transform[:3, :3] = rotation_matrix_xyz
    homogeneous_index_transform[:3, 3] = index_offset_xyz
    transform_payload = np.concatenate(
        (rotation_matrix_xyz.reshape(-1), translation_centered_grid_xyz_A)
    ).astype("<f8", copy=False)
    transform_sha256 = hashlib.sha256(transform_payload.tobytes()).hexdigest()

    metadata = {
        "schema_version": SCHEMA_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "status": status,
        "alignment": {
            "mode": alignment_mode,
            "scope": "bounded_local_registration_not_global_SO3",
            "moving_to_fixed": "candidate_to_reference",
            "requested_alignment_lowpass_A": requested_alignment_A,
            "effective_alignment_lowpass_A": effective_alignment_A,
            "lowpass_raised_for_analysis_nyquist": not math.isclose(
                requested_alignment_A,
                effective_alignment_A,
                rel_tol=1e-9,
                abs_tol=1e-9,
            ),
            "analysis_stride": stride,
            "analysis_shape_zyx": list(reference_analysis.shape),
            "analysis_voxel_size_A": analysis_sampling_A,
            "analysis_alias_safe_minimum_lowpass_A": alias_safe_minimum_A,
            "objective": "weighted_mean_subtracted_pearson",
            "objective_support": support_type,
            "identity_objective_correlation": fitted[
                "identity_objective_correlation"
            ],
            "final_objective_correlation": fitted["final_objective_correlation"],
            "objective_gain": fitted["objective_gain"],
            "transform": {
                "rotation_euler_convention": "active_extrinsic_xyz_degrees",
                "rotation_euler_xyz_degrees": angles_xyz_deg.tolist(),
                "rotation_matrix_canonical_grid_xyz": rotation_matrix_xyz.tolist(),
                "translation_centered_grid_xyz_A": (
                    translation_centered_grid_xyz_A.tolist()
                ),
                "translation_full_grid_zyx_pixels": translation_full_zyx.tolist(),
                "translation_analysis_zyx_pixels": translation_analysis_zyx.tolist(),
                "reference_geometric_center_index_xyz": center_xyz.tolist(),
                "candidate_geometric_center_index_xyz": center_xyz.tolist(),
                "homogeneous_candidate_index_xyz_to_reference_index_xyz": (
                    homogeneous_index_transform.tolist()
                ),
                "rotation_center": "canonical_geometric_array_center",
                "direction": "candidate_density_to_reference_grid",
                "mrc_origin_and_start_applied": False,
                "sha256": transform_sha256,
            },
            "bounds": {
                "max_rotation_per_euler_axis_deg": (
                    rotation_limit_deg if alignment_mode == "rigid" else 0.0
                ),
                "max_translation_per_axis_A": shift_limit_A,
                "minimum_meaningful_translation_bound_A": minimum_shift_limit_A,
                "minimum_meaningful_rigid_rotation_bound_deg": (
                    minimum_rotation_limit_deg
                    if alignment_mode == "rigid"
                    else None
                ),
                "contacts": fitted["bound_contacts"],
            },
            "optimizer": {
                "method": "deterministic_multistart_phase_translation_then_Powell",
                "seed_count": fitted["seed_count"],
                "optimized_start_count": fitted["optimizer_start_count"],
                "function_evaluations": fitted["function_evaluations"],
                "maximum_function_evaluations": _MAX_FUNCTION_EVALUATIONS,
                "reported_success": fitted["optimizer_success"],
                "message": fitted["optimizer_message"],
                "selected_solution_source": fitted["selected_solution_source"],
                "exact_identity_evaluated": True,
                "minimum_numerical_objective_gain": fitted[
                    "minimum_numerical_objective_gain"
                ],
            },
            "solution_stability": {
                "local_competitive_solution_agreement_assessed": True,
                "stable": fitted["solution_stable"],
                "competitive_correlation_tolerance": fitted[
                    "competitive_correlation_tolerance"
                ],
                "rotation_agreement_tolerance_deg": fitted[
                    "rotation_agreement_tolerance_deg"
                ],
                "translation_agreement_tolerance_analysis_pixels": fitted[
                    "translation_agreement_tolerance_analysis_pixels"
                ],
                "competitive_solutions": fitted["competitive_solutions"],
                "exact_voxel_identity_overrides_pose_ambiguity": bool(
                    fitted["identity_selected"] and exact_canonical_voxel_identity
                ),
            },
            "registration_validation": registration_validation,
            "interpolation": {
                "method": "scipy_ndimage_affine_transform",
                "order": 1,
                "boundary_mode": "constant_at_moving_map_boundary_median",
            },
            "valid_support_fraction": valid_support_fraction,
            "minimum_valid_support_fraction": _MIN_VALID_SUPPORT_FRACTION,
            "analysis_support_voxels": analysis_support_voxels,
            "analysis_effective_sample_size": analysis_effective_sample_size,
            "minimum_analysis_effective_sample_size": minimum_effective_sample_size,
            "mask_anti_aliased_before_analysis_resampling": bool(
                validated_mask is not None and stride > 1
            ),
            "orientation_uniqueness_assessed": False,
            "exact_canonical_voxel_identity": exact_canonical_voxel_identity,
        },
        "metrics": {
            "support": support_type,
            "same_valid_support_used_before_and_after": True,
            "threshold_sigma": threshold,
            "fixed_transform_applied_to_every_cutoff": True,
            "comparisons": metrics,
        },
        "resource_contract": resource_contract,
        "limitations": [
            "The search is local and may not recover an arbitrary starting orientation.",
            "Handedness and symmetry-equivalent transform ambiguity are not resolved.",
            "Fitting and scoring the same map pair can increase correlation and is not independent validation.",
            "Interpolation affects the moved map; primary interpretation should use declared common physical low-passes.",
            "Correlation and overlap do not by themselves establish same, distinct, biological identity, or union benefit.",
        ],
    }
    return MapComparisonResult(
        reference_filtered=reference_alignment,
        candidate_before=candidate_alignment,
        candidate_after=candidate_after_alignment,
        metric_weights=full_weights,
        cutoff_views=tuple(cutoff_views),
        metadata=metadata,
    )
