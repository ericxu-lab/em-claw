"""Deterministic, report-only point-group scanning for independent half-maps.

The routines in this module deliberately do not write maps, align a RELION
reference, or select a processing symmetry.  They produce an exploratory
shortlist whose hypotheses still require matched gold-standard refinements.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy import fft, ndimage
from scipy.spatial.transform import Rotation


class SymmetryScanError(ValueError):
    """Raised when a symmetry scan cannot be performed honestly or safely."""


SCHEMA_VERSION = "relion.symmetry-scan/v2"
ALGORITHM_VERSION = "bounded-half-map-search/3"

_MAX_INPUT_VOXELS = 160_000_000
_ANALYSIS_MAX_SIDE = 128
_ANALYSIS_MIN_SIDE = 32
_MAX_SAMPLE_POINTS = 6_000
_AXIS_COUNT = 38
_CENTER_LINE_SEARCH_MAX_PIX = 4.0
_CENTER_LINE_SEARCH_PHASES = 8
_MIN_IDENTITY_HALF_CORRELATION = 0.10
_MIN_AXIS_NULL_Z = 2.0
_MIN_ROTATIONAL_CORRELATION = 0.40
_MIN_CROSS_HALF_RETENTION = 0.40
_MIN_POLYHEDRAL_SCREEN_CORRELATION = 0.20
_MAX_OPERATOR_SET_DISAGREEMENT_DEG = 2.0


@dataclass(frozen=True)
class _PreparedMaps:
    half1: np.ndarray
    half2: np.ndarray
    weights: np.ndarray
    voxel_size_A: float
    requested_resolution_A: float
    effective_resolution_A: float
    stride: int
    particle_radius_A: float
    identifiability_max_order: int
    coordinates_xyz: np.ndarray
    coordinates_zyx: np.ndarray
    base_weights: np.ndarray
    base_half1: np.ndarray
    base_half2: np.ndarray
    identity_half_correlation: float
    rotation_center_zyx: np.ndarray | None = None
    geometric_center_zyx: np.ndarray | None = None
    joint_center_zyx: np.ndarray | None = None
    half1_center_zyx: np.ndarray | None = None
    half2_center_zyx: np.ndarray | None = None
    center_disagreement_A: float = 0.0
    center_disagreement_threshold_A: float = 0.0
    center_edge_clearance_A: float = 0.0
    center_stable: bool = True
    requested_cutoff_honored: bool = True


@dataclass(frozen=True)
class _Spectrum:
    order: int
    axes_xyz: np.ndarray
    half1: np.ndarray
    half2: np.ndarray
    cross: np.ndarray
    cross_retention: np.ndarray
    consensus_index: int
    half1_index: int
    half2_index: int
    evidence_z: float
    half1_evidence_z: float
    half2_evidence_z: float
    axis_agreement_deg: float
    identifiable: bool


def _finite_positive_float(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise SymmetryScanError(f"{name} must be a finite positive number.")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise SymmetryScanError(f"{name} must be a finite positive number.") from exc
    if not math.isfinite(result) or result <= 0:
        raise SymmetryScanError(f"{name} must be a finite positive number.")
    return result


def _bounded_integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise SymmetryScanError(f"{name} must be an integer.")
    result = int(value)
    if not minimum <= result <= maximum:
        raise SymmetryScanError(f"{name} must be between {minimum} and {maximum}.")
    return result


def _canonical_axis(axis: Sequence[float]) -> np.ndarray:
    vector = np.asarray(axis, dtype=np.float64)
    if vector.shape != (3,) or not np.all(np.isfinite(vector)):
        raise SymmetryScanError("A symmetry axis must contain three finite values.")
    norm = float(np.linalg.norm(vector))
    if norm <= np.finfo(np.float64).eps:
        raise SymmetryScanError("A symmetry axis must be non-zero.")
    vector = vector / norm
    for component in vector:
        if abs(float(component)) > 1e-12:
            if component < 0:
                vector = -vector
            break
    return vector


def axis_angle_degrees(first: Sequence[float], second: Sequence[float]) -> float:
    """Return the antipodal angle between two unoriented axes."""
    a = _canonical_axis(first)
    b = _canonical_axis(second)
    cosine = float(np.clip(abs(np.dot(a, b)), -1.0, 1.0))
    return float(np.degrees(np.arccos(cosine)))


def _rotation_angle(matrix: np.ndarray) -> float:
    cosine = float(np.clip((np.trace(matrix) - 1.0) / 2.0, -1.0, 1.0))
    return float(np.arccos(cosine))


def symmetry_operators(group: str) -> np.ndarray:
    """Return a deterministic matrix representation of Cn, Dn, T, O, or I."""
    normalized = str(group).strip().upper()
    match = re.fullmatch(r"([CD])([1-9][0-9]*)", normalized)
    if match:
        family, order_text = match.groups()
        order = int(order_text)
        if family == "C" and order < 1:
            raise SymmetryScanError("Cyclic order must be at least one.")
        if family == "D" and order < 2:
            raise SymmetryScanError("Dihedral order must be at least two.")
        scipy_group = f"{family}{order}"
    elif normalized in {"T", "O", "I"}:
        scipy_group = normalized
    else:
        raise SymmetryScanError(
            f"Unsupported point group {group!r}; expected Cn, Dn, T, O, or I."
        )
    matrices = np.asarray(Rotation.create_group(scipy_group).as_matrix(), dtype=np.float64)
    keyed = sorted(
        matrices,
        key=lambda matrix: (
            round(_rotation_angle(matrix), 12),
            *(round(float(item), 12) for item in matrix.reshape(-1)),
        ),
    )
    return np.asarray(keyed, dtype=np.float64)


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
    shell = min(
        1.0 / (size * voxel_size_A)
        for size in data.shape
    )
    low = max(0.0, cutoff - shell)
    high = cutoff + shell
    transfer = np.ones(radius.shape, dtype=np.float32)
    transfer[radius >= high] = 0.0
    transition = (radius > low) & (radius < high)
    if high > low:
        transfer[transition] = 0.5 + 0.5 * np.cos(
            np.pi * (radius[transition] - low) / (high - low)
        )
    spectrum = fft.rfftn(data)
    spectrum *= transfer
    return np.asarray(fft.irfftn(spectrum, s=data.shape), dtype=np.float32)


def _centered_resample(
    volume: np.ndarray,
    stride: int,
    *,
    order: int = 1,
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


def _adaptive_analysis_stride(
    shape: Sequence[int],
    voxel_size_A: float,
    requested_resolution_A: float,
) -> tuple[int, float, bool]:
    """Choose the cheapest bounded grid that still honors the requested cutoff.

    The upper side limit is a resource bound, not an instruction to silently
    discard requested information.  When the two constraints cannot both be
    met, the returned validity flag makes that loss explicit to the caller.
    """
    sizes = tuple(int(size) for size in shape)
    required_stride = max(1, math.ceil(max(sizes) / _ANALYSIS_MAX_SIDE))
    source_shell_A_inv = min(1.0 / (size * voxel_size_A) for size in sizes)
    requested_cutoff_A_inv = 1.0 / requested_resolution_A
    denominator = 2.0 * voxel_size_A * (
        requested_cutoff_A_inv + source_shell_A_inv
    )
    cutoff_safe_stride = max(1, math.floor(1.0 / denominator + 1e-12))
    minimum_side_stride = max(
        1,
        min(max(1, size // _ANALYSIS_MIN_SIDE) for size in sizes),
    )
    preferred_stride = min(cutoff_safe_stride, minimum_side_stride)
    stride = max(required_stride, preferred_stride)

    target_nyquist_A_inv = 1.0 / (2.0 * voxel_size_A * stride)
    maximum_cutoff_A_inv = max(
        np.finfo(np.float64).eps,
        target_nyquist_A_inv - source_shell_A_inv,
    )
    effective_resolution_A = max(
        requested_resolution_A,
        1.0 / maximum_cutoff_A_inv,
    )
    honored = effective_resolution_A <= requested_resolution_A * (1.0 + 1e-7)
    return stride, float(effective_resolution_A), bool(honored)


def _estimate_signal_center(
    volume: np.ndarray,
    mask: np.ndarray | None = None,
) -> np.ndarray:
    """Estimate a bounded signal centroid without assuming the array center.

    A lightly smoothed, solvent-referenced signed tail is used so a common map
    translation is recovered while isolated noise components contribute very
    little.  A supplied mask only soft-weights density evidence and can never
    create support on its own.
    """
    data = np.asarray(volume, dtype=np.float32)
    geometric = (np.asarray(data.shape, dtype=np.float64) - 1.0) / 2.0
    if data.ndim != 3 or min(data.shape) < 3:
        return geometric

    smoothed = np.asarray(
        ndimage.gaussian_filter(data, sigma=1.0, mode="nearest"),
        dtype=np.float64,
    )
    border_width = max(1, min(data.shape) // 10)
    border = np.zeros(data.shape, dtype=bool)
    for dimension in range(3):
        lower = [slice(None)] * 3
        upper = [slice(None)] * 3
        lower[dimension] = slice(0, border_width)
        upper[dimension] = slice(data.shape[dimension] - border_width, None)
        border[tuple(lower)] = True
        border[tuple(upper)] = True
    solvent = smoothed[border]
    background = float(np.median(solvent))
    mad = float(np.median(np.abs(solvent - background)))
    noise_scale = max(
        1.4826 * mad,
        np.finfo(np.float64).eps * max(1.0, float(np.max(np.abs(smoothed)))),
    )
    centered = smoothed - background
    positive = np.clip(centered, 0.0, None)
    negative = np.clip(-centered, 0.0, None)
    cutoff = 2.5 * noise_scale
    positive_energy = float(np.sum(positive[positive >= cutoff] ** 2))
    negative_energy = float(np.sum(negative[negative >= cutoff] ** 2))
    tail = positive if positive_energy >= negative_energy else negative
    support = tail >= cutoff
    weights = np.where(support, tail**2, 0.0)

    if np.any(support):
        labels, count = ndimage.label(support)
        if count > 1:
            component_ids = np.arange(1, count + 1)
            energies = np.asarray(
                ndimage.sum(weights, labels=labels, index=component_ids),
                dtype=np.float64,
            )
            total_energy = float(np.sum(energies))
            maximum_energy = float(np.max(energies)) if energies.size else 0.0
            keep = component_ids[
                energies >= max(0.005 * total_energy, 0.02 * maximum_energy)
            ]
            support = np.isin(labels, keep)
            weights = np.where(support, weights, 0.0)

    if mask is not None and float(np.sum(weights)) > 0.0:
        soft_mask = np.asarray(mask, dtype=np.float64)
        if soft_mask.shape == data.shape:
            weights *= 0.25 + 0.75 * np.clip(soft_mask, 0.0, 1.0)

    total = float(np.sum(weights))
    if total <= np.finfo(np.float64).eps:
        return geometric
    z, y, x = np.indices(data.shape, dtype=np.float64)
    center = np.asarray(
        [
            np.sum(weights * z) / total,
            np.sum(weights * y) / total,
            np.sum(weights * x) / total,
        ],
        dtype=np.float64,
    )
    lower = np.ones(3, dtype=np.float64)
    upper = np.maximum(lower, np.asarray(data.shape, dtype=np.float64) - 2.0)
    return np.clip(center, lower, upper)


def _spherical_window(
    shape: tuple[int, int, int],
    center_zyx: Sequence[float] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    z, y, x = np.indices(shape, dtype=np.float32)
    center = (
        (np.asarray(shape, dtype=np.float32) - 1.0) / 2.0
        if center_zyx is None
        else np.asarray(center_zyx, dtype=np.float32)
    )
    radius = np.sqrt(
        (z - center[0]) ** 2
        + (y - center[1]) ** 2
        + (x - center[2]) ** 2
    )
    edge_distances = np.minimum(center, np.asarray(shape, dtype=np.float32) - 1.0 - center)
    outer = max(2.0, float(np.min(edge_distances)))
    inner = 0.72 * outer
    window = np.ones(shape, dtype=np.float32)
    window[radius >= outer] = 0.0
    transition = (radius > inner) & (radius < outer)
    window[transition] = 0.5 + 0.5 * np.cos(
        np.pi * (radius[transition] - inner) / (outer - inner)
    )
    return window, radius


def _subtract_radial_profile(
    volume: np.ndarray,
    weights: np.ndarray,
    radius: np.ndarray,
) -> np.ndarray:
    bins = np.asarray(np.floor(radius), dtype=np.int32)
    flat_bins = bins.reshape(-1)
    flat_weights = np.asarray(weights, dtype=np.float64).reshape(-1)
    flat_volume = np.asarray(volume, dtype=np.float64).reshape(-1)
    count = int(np.max(flat_bins)) + 1
    denominator = np.bincount(flat_bins, weights=flat_weights, minlength=count)
    numerator = np.bincount(
        flat_bins,
        weights=flat_weights * flat_volume,
        minlength=count,
    )
    profile = np.divide(
        numerator,
        denominator,
        out=np.zeros_like(numerator),
        where=denominator > np.finfo(np.float64).eps,
    )
    residual = flat_volume - profile[flat_bins]
    return np.asarray(residual.reshape(volume.shape), dtype=np.float32)


def _particle_radius_A(
    average: np.ndarray,
    weights: np.ndarray,
    radius_pix: np.ndarray,
    voxel_size_A: float,
) -> float:
    energy = np.asarray(average, dtype=np.float64) ** 2 * np.asarray(weights, dtype=np.float64)
    flat_energy = energy.reshape(-1)
    total = float(np.sum(flat_energy))
    if total <= np.finfo(np.float64).eps:
        return 0.0
    order = np.argsort(radius_pix.reshape(-1), kind="stable")
    cumulative = np.cumsum(flat_energy[order])
    index = int(np.searchsorted(cumulative, 0.95 * total, side="left"))
    index = min(index, order.size - 1)
    return float(radius_pix.reshape(-1)[order[index]] * voxel_size_A)


def _identifiability_ceiling(radius_A: float, resolution_A: float, max_order: int) -> int:
    if radius_A <= 0 or resolution_A >= 2.0 * radius_A:
        return 1
    ratio = float(np.clip(resolution_A / (2.0 * radius_A), 0.0, 1.0))
    angle = math.asin(ratio)
    if angle <= 0:
        return max_order
    return max(1, min(max_order, math.floor(math.pi / angle)))


def _weighted_correlation(first: np.ndarray, second: np.ndarray, weights: np.ndarray) -> float:
    valid = (
        np.isfinite(first)
        & np.isfinite(second)
        & np.isfinite(weights)
        & (weights > np.finfo(np.float64).eps)
    )
    if int(np.count_nonzero(valid)) < 16:
        return 0.0
    x = np.asarray(first[valid], dtype=np.float64)
    y = np.asarray(second[valid], dtype=np.float64)
    w = np.asarray(weights[valid], dtype=np.float64)
    total = float(np.sum(w))
    if total <= np.finfo(np.float64).eps:
        return 0.0
    mean_x = float(np.sum(w * x) / total)
    mean_y = float(np.sum(w * y) / total)
    centered_x = x - mean_x
    centered_y = y - mean_y
    denominator = math.sqrt(
        float(np.sum(w * centered_x**2)) * float(np.sum(w * centered_y**2))
    )
    if denominator <= np.finfo(np.float64).eps:
        return 0.0
    return float(np.clip(np.sum(w * centered_x * centered_y) / denominator, -1.0, 1.0))


def _prepare_maps(
    half_map_1: np.ndarray,
    half_map_2: np.ndarray,
    mask: np.ndarray | None,
    *,
    voxel_size_A: float,
    analysis_resolution_A: float,
    max_order: int,
) -> _PreparedMaps:
    first = np.asarray(half_map_1)
    second = np.asarray(half_map_2)
    if first.ndim != 3 or second.ndim != 3:
        raise SymmetryScanError("half_map_1 and half_map_2 must be three-dimensional arrays.")
    if first.shape != second.shape:
        raise SymmetryScanError("half_map_1 and half_map_2 must have identical shapes.")
    if first.size > _MAX_INPUT_VOXELS:
        raise SymmetryScanError(
            f"Input contains {first.size:,} voxels, above the {_MAX_INPUT_VOXELS:,}-voxel cap."
        )
    if not np.all(np.isfinite(first)) or not np.all(np.isfinite(second)):
        raise SymmetryScanError("Half-maps must contain only finite voxels.")
    if np.array_equal(first, second):
        raise SymmetryScanError(
            "Half-maps must not contain identical voxel arrays."
        )
    if float(np.ptp(first)) <= 0 or float(np.ptp(second)) <= 0:
        raise SymmetryScanError("Half-maps must contain non-constant density.")
    if mask is not None:
        mask_array = np.asarray(mask)
        if mask_array.shape != first.shape:
            raise SymmetryScanError("mask must have the same shape as the half-maps.")
        if not np.all(np.isfinite(mask_array)):
            raise SymmetryScanError("mask must contain only finite values.")
        if float(np.min(mask_array)) < 0 or float(np.max(mask_array)) > 1:
            raise SymmetryScanError("mask values must lie within [0, 1].")
        if not np.any(mask_array > 0):
            raise SymmetryScanError("mask must contain non-zero support.")
        mask_array = np.asarray(mask_array, dtype=np.float32)
    else:
        mask_array = None

    stride, effective_resolution_A, requested_cutoff_honored = (
        _adaptive_analysis_stride(
            first.shape,
            voxel_size_A,
            analysis_resolution_A,
        )
    )
    filtered_first = _raised_cosine_lowpass(first, voxel_size_A, effective_resolution_A)
    filtered_second = _raised_cosine_lowpass(second, voxel_size_A, effective_resolution_A)
    filtered_first = _centered_resample(filtered_first, stride)
    filtered_second = _centered_resample(filtered_second, stride)
    analysis_voxel_A = voxel_size_A * stride
    if mask_array is not None:
        if stride > 1:
            mask_array = np.asarray(
                ndimage.gaussian_filter(
                    mask_array,
                    sigma=0.5 * stride,
                    mode="constant",
                    cval=0.0,
                ),
                dtype=np.float32,
            )
        analysis_mask = _centered_resample(mask_array, stride)
    else:
        analysis_mask = None

    average = np.asarray((filtered_first + filtered_second) / 2.0, dtype=np.float32)
    geometric_center_zyx = (
        np.asarray(filtered_first.shape, dtype=np.float64) - 1.0
    ) / 2.0
    half1_center_zyx = _estimate_signal_center(filtered_first, analysis_mask)
    half2_center_zyx = _estimate_signal_center(filtered_second, analysis_mask)
    joint_center_zyx = _estimate_signal_center(average, analysis_mask)
    independent_midpoint_zyx = (half1_center_zyx + half2_center_zyx) / 2.0
    center_disagreement_pix = float(
        np.linalg.norm(half1_center_zyx - half2_center_zyx)
    )
    center_disagreement_A = center_disagreement_pix * analysis_voxel_A
    center_disagreement_threshold_A = max(
        2.0 * analysis_voxel_A,
        0.5 * effective_resolution_A,
    )
    if (
        float(np.linalg.norm(joint_center_zyx - independent_midpoint_zyx))
        * analysis_voxel_A
        > center_disagreement_threshold_A
    ):
        rotation_center_zyx = independent_midpoint_zyx
    else:
        rotation_center_zyx = joint_center_zyx
    rotation_center_zyx = np.clip(
        rotation_center_zyx,
        np.ones(3, dtype=np.float64),
        np.asarray(filtered_first.shape, dtype=np.float64) - 2.0,
    )
    edge_clearance_pix = float(
        np.min(
            np.minimum(
                rotation_center_zyx,
                np.asarray(filtered_first.shape, dtype=np.float64)
                - 1.0
                - rotation_center_zyx,
            )
        )
    )
    center_edge_clearance_A = edge_clearance_pix * analysis_voxel_A
    center_stable = bool(
        center_disagreement_A <= center_disagreement_threshold_A
        and center_edge_clearance_A
        >= max(2.0 * analysis_voxel_A, 0.5 * effective_resolution_A)
    )

    window, radius_pix = _spherical_window(
        filtered_first.shape,
        rotation_center_zyx,
    )
    weights = window
    if analysis_mask is not None:
        weights = np.asarray(weights * np.clip(analysis_mask, 0.0, 1.0), dtype=np.float32)
    if int(np.count_nonzero(weights > 0.05)) < 64:
        raise SymmetryScanError("The spherical window and optional mask leave too little support.")

    particle_radius_A = _particle_radius_A(average, weights, radius_pix, analysis_voxel_A)
    ceiling = _identifiability_ceiling(
        particle_radius_A,
        effective_resolution_A,
        max_order,
    )
    residual_first = _subtract_radial_profile(filtered_first, weights, radius_pix)
    residual_second = _subtract_radial_profile(filtered_second, weights, radius_pix)

    z, y, x = np.indices(filtered_first.shape, dtype=np.float32)
    coordinates_zyx_all = np.column_stack(
        (z.reshape(-1), y.reshape(-1), x.reshape(-1))
    )
    coordinates_xyz_all = np.column_stack(
        (
            x.reshape(-1) - rotation_center_zyx[2],
            y.reshape(-1) - rotation_center_zyx[1],
            z.reshape(-1) - rotation_center_zyx[0],
        )
    )
    flat_weights = weights.reshape(-1)
    importance = (
        np.abs(residual_first.reshape(-1))
        + np.abs(residual_second.reshape(-1))
    ) * flat_weights
    eligible = np.flatnonzero(flat_weights > 0.05)
    if eligible.size > _MAX_SAMPLE_POINTS:
        signal_count = int(0.75 * _MAX_SAMPLE_POINTS)
        ranked = eligible[np.argsort(importance[eligible], kind="stable")]
        signal_indices = ranked[-signal_count:]
        remaining = np.setdiff1d(eligible, signal_indices, assume_unique=False)
        uniform_count = _MAX_SAMPLE_POINTS - signal_indices.size
        if remaining.size > uniform_count:
            positions = np.linspace(0, remaining.size - 1, uniform_count, dtype=np.int64)
            remaining = remaining[positions]
        selected = np.sort(np.concatenate((signal_indices, remaining)))
    else:
        selected = eligible

    coordinates_xyz = np.asarray(coordinates_xyz_all[selected], dtype=np.float64)
    coordinates_zyx = np.asarray(coordinates_zyx_all[selected], dtype=np.float64)
    base_weights = np.asarray(flat_weights[selected], dtype=np.float64)
    base_half1 = np.asarray(residual_first.reshape(-1)[selected], dtype=np.float64)
    base_half2 = np.asarray(residual_second.reshape(-1)[selected], dtype=np.float64)
    identity_half_correlation = _weighted_correlation(base_half1, base_half2, base_weights)

    return _PreparedMaps(
        half1=residual_first,
        half2=residual_second,
        weights=weights,
        voxel_size_A=analysis_voxel_A,
        requested_resolution_A=analysis_resolution_A,
        effective_resolution_A=effective_resolution_A,
        stride=stride,
        particle_radius_A=particle_radius_A,
        identifiability_max_order=ceiling,
        coordinates_xyz=coordinates_xyz,
        coordinates_zyx=coordinates_zyx,
        base_weights=base_weights,
        base_half1=base_half1,
        base_half2=base_half2,
        identity_half_correlation=identity_half_correlation,
        rotation_center_zyx=np.asarray(rotation_center_zyx, dtype=np.float64),
        geometric_center_zyx=geometric_center_zyx,
        joint_center_zyx=joint_center_zyx,
        half1_center_zyx=half1_center_zyx,
        half2_center_zyx=half2_center_zyx,
        center_disagreement_A=center_disagreement_A,
        center_disagreement_threshold_A=center_disagreement_threshold_A,
        center_edge_clearance_A=center_edge_clearance_A,
        center_stable=center_stable,
        requested_cutoff_honored=requested_cutoff_honored,
    )


def _fibonacci_axes(count: int = _AXIS_COUNT) -> np.ndarray:
    axes: list[np.ndarray] = [
        np.asarray((0.0, 0.0, 1.0)),
        np.asarray((0.0, 1.0, 0.0)),
        np.asarray((1.0, 0.0, 0.0)),
    ]
    golden_angle = math.pi * (3.0 - math.sqrt(5.0))
    for index in range(max(1, count * 2)):
        z = 1.0 - 2.0 * (index + 0.5) / (count * 2)
        radial = math.sqrt(max(0.0, 1.0 - z * z))
        angle = golden_angle * index
        axes.append(
            _canonical_axis((radial * math.cos(angle), radial * math.sin(angle), z))
        )
    unique: list[np.ndarray] = []
    for axis in axes:
        canonical = _canonical_axis(axis)
        if all(abs(float(np.dot(canonical, existing))) < 1.0 - 1e-8 for existing in unique):
            unique.append(canonical)
        if len(unique) >= count:
            break
    return np.asarray(unique, dtype=np.float64)


def _axis_alignment(axis_xyz: np.ndarray) -> np.ndarray:
    return _vector_alignment(
        np.asarray((0.0, 0.0, 1.0)),
        _canonical_axis(axis_xyz),
    )


def _vector_alignment(source_xyz: np.ndarray, target_xyz: np.ndarray) -> np.ndarray:
    source = np.asarray(source_xyz, dtype=np.float64)
    target = np.asarray(target_xyz, dtype=np.float64)
    source /= np.linalg.norm(source)
    target /= np.linalg.norm(target)
    dot = float(np.clip(np.dot(source, target), -1.0, 1.0))
    if dot > 1.0 - 1e-12:
        return np.eye(3, dtype=np.float64)
    if dot < -1.0 + 1e-12:
        seed = np.asarray((1.0, 0.0, 0.0), dtype=np.float64)
        if abs(float(np.dot(source, seed))) > 0.85:
            seed = np.asarray((0.0, 1.0, 0.0), dtype=np.float64)
        perpendicular = np.cross(source, seed)
        perpendicular /= np.linalg.norm(perpendicular)
        return Rotation.from_rotvec(perpendicular * math.pi).as_matrix()
    rotation_axis = np.cross(source, target)
    rotation_axis /= np.linalg.norm(rotation_axis)
    return Rotation.from_rotvec(rotation_axis * math.acos(dot)).as_matrix()


def _rotation_about_axis(axis_xyz: np.ndarray, angle: float) -> np.ndarray:
    axis = np.asarray(axis_xyz, dtype=np.float64)
    axis /= np.linalg.norm(axis)
    return Rotation.from_rotvec(axis * float(angle)).as_matrix()


def _prepared_rotation_center(prepared: _PreparedMaps) -> np.ndarray:
    if prepared.rotation_center_zyx is not None:
        return np.asarray(prepared.rotation_center_zyx, dtype=np.float64)
    return (np.asarray(prepared.half1.shape, dtype=np.float64) - 1.0) / 2.0


def _rotate_coordinates(
    prepared: _PreparedMaps,
    matrix: np.ndarray,
    center_zyx: Sequence[float] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    # For an active Cartesian rotation R, sampling R^-1 x corresponds to the
    # row-vector product x R.  Convert physical XYZ back to NumPy ZYX indices.
    base_center_zyx = _prepared_rotation_center(prepared)
    operation_center_zyx = (
        base_center_zyx
        if center_zyx is None
        else np.asarray(center_zyx, dtype=np.float64)
    )
    base_center_xyz = base_center_zyx[::-1]
    operation_center_xyz = operation_center_zyx[::-1]
    absolute_xyz = prepared.coordinates_xyz + base_center_xyz
    rotated_xyz = (
        (absolute_xyz - operation_center_xyz)
        @ np.asarray(matrix, dtype=np.float64)
        + operation_center_xyz
    )
    rotated_zyx = np.column_stack(
        (
            rotated_xyz[:, 2],
            rotated_xyz[:, 1],
            rotated_xyz[:, 0],
        )
    )
    sampled_weights = ndimage.map_coordinates(
        prepared.weights,
        rotated_zyx.T,
        order=1,
        mode="constant",
        cval=0.0,
        prefilter=False,
    )
    intersection = np.asarray(
        prepared.base_weights * np.clip(sampled_weights, 0.0, None),
        dtype=np.float64,
    )
    return rotated_zyx, intersection


def _sample_operation(
    prepared: _PreparedMaps,
    matrix: np.ndarray,
    center_zyx: Sequence[float] | None = None,
) -> dict[str, float]:
    rotated_zyx, weights = _rotate_coordinates(prepared, matrix, center_zyx)
    rotated_half1 = ndimage.map_coordinates(
        prepared.half1,
        rotated_zyx.T,
        order=1,
        mode="constant",
        cval=0.0,
        prefilter=False,
    )
    rotated_half2 = ndimage.map_coordinates(
        prepared.half2,
        rotated_zyx.T,
        order=1,
        mode="constant",
        cval=0.0,
        prefilter=False,
    )
    half1 = _weighted_correlation(prepared.base_half1, rotated_half1, weights)
    half2 = _weighted_correlation(prepared.base_half2, rotated_half2, weights)
    cross = 0.5 * (
        _weighted_correlation(prepared.base_half1, rotated_half2, weights)
        + _weighted_correlation(prepared.base_half2, rotated_half1, weights)
    )
    identity = prepared.identity_half_correlation
    retention = cross / identity if identity > _MIN_IDENTITY_HALF_CORRELATION else 0.0
    return {
        "half1": float(half1),
        "half2": float(half2),
        "cross": float(cross),
        "cross_retention": float(np.clip(retention, -2.0, 2.0)),
    }


def _operation_scores(
    prepared: _PreparedMaps,
    matrix: np.ndarray,
    center_zyx: Sequence[float] | None = None,
) -> dict[str, float]:
    return _sample_operation(prepared, matrix, center_zyx)


def _robust_z(value: float, distribution: np.ndarray) -> float:
    finite = np.asarray(distribution[np.isfinite(distribution)], dtype=np.float64)
    if finite.size < 3:
        return 0.0
    median = float(np.median(finite))
    mad = float(np.median(np.abs(finite - median)))
    scale = max(1.4826 * mad, 0.02)
    return float((value - median) / scale)


def _spectrum_for_order(
    prepared: _PreparedMaps,
    order: int,
    axes_xyz: np.ndarray,
) -> _Spectrum:
    half1 = np.empty(len(axes_xyz), dtype=np.float64)
    half2 = np.empty(len(axes_xyz), dtype=np.float64)
    cross = np.empty(len(axes_xyz), dtype=np.float64)
    retention = np.empty(len(axes_xyz), dtype=np.float64)
    generator = Rotation.from_rotvec(np.asarray((0.0, 0.0, 2.0 * math.pi / order))).as_matrix()
    for index, axis in enumerate(axes_xyz):
        alignment = _axis_alignment(axis)
        matrix = alignment @ generator @ alignment.T
        metrics = _operation_scores(prepared, matrix)
        half1[index] = metrics["half1"]
        half2[index] = metrics["half2"]
        cross[index] = metrics["cross"]
        retention[index] = metrics["cross_retention"]

    half1_index = int(np.argmax(half1))
    half2_index = int(np.argmax(half2))
    z_half1 = np.asarray([_robust_z(value, half1) for value in half1])
    z_half2 = np.asarray([_robust_z(value, half2) for value in half2])
    z_cross = np.asarray([_robust_z(value, retention) for value in retention])
    consensus = np.minimum(np.minimum(z_half1, z_half2), z_cross)
    consensus_index = int(np.argmax(consensus))
    return _Spectrum(
        order=order,
        axes_xyz=axes_xyz,
        half1=half1,
        half2=half2,
        cross=cross,
        cross_retention=retention,
        consensus_index=consensus_index,
        half1_index=half1_index,
        half2_index=half2_index,
        evidence_z=float(consensus[consensus_index]),
        half1_evidence_z=float(z_half1[half1_index]),
        half2_evidence_z=float(z_half2[half2_index]),
        axis_agreement_deg=axis_angle_degrees(
            axes_xyz[half1_index],
            axes_xyz[half2_index],
        ),
        identifiable=order <= prepared.identifiability_max_order,
    )


def _axis_refinement_ring(
    axis: np.ndarray,
    angle_deg: float,
    *,
    count: int = 12,
) -> np.ndarray:
    primary = _canonical_axis(axis)
    seed = np.asarray((1.0, 0.0, 0.0), dtype=np.float64)
    if abs(float(np.dot(primary, seed))) > 0.85:
        seed = np.asarray((0.0, 1.0, 0.0), dtype=np.float64)
    tangent_1 = np.cross(primary, seed)
    tangent_1 /= np.linalg.norm(tangent_1)
    tangent_2 = np.cross(primary, tangent_1)
    candidates = [primary]
    tangent = math.tan(math.radians(angle_deg))
    for phase in np.linspace(0.0, 2.0 * math.pi, count, endpoint=False):
        perturbed = primary + tangent * (
            math.cos(float(phase)) * tangent_1
            + math.sin(float(phase)) * tangent_2
        )
        candidates.append(_canonical_axis(perturbed))
    return np.asarray(candidates, dtype=np.float64)


def _refine_cyclic_axis(
    prepared: _PreparedMaps,
    order: int,
    initial_axis: np.ndarray,
    *,
    objective: str = "consensus",
    center_zyx: Sequence[float] | None = None,
) -> tuple[np.ndarray, dict[str, float]]:
    generator = Rotation.from_rotvec(np.asarray((0.0, 0.0, 2.0 * math.pi / order))).as_matrix()
    best_axis = _canonical_axis(initial_axis)
    alignment = _axis_alignment(best_axis)
    best_metrics = _operation_scores(
        prepared,
        alignment @ generator @ alignment.T,
        center_zyx,
    )
    def objective_value(metrics: dict[str, float]) -> float:
        if objective == "half1":
            return metrics["half1"]
        if objective == "half2":
            return metrics["half2"]
        if objective != "consensus":
            raise SymmetryScanError(f"Unknown axis-refinement objective {objective!r}.")
        return min(metrics["half1"], metrics["half2"], metrics["cross_retention"])

    best_value = objective_value(best_metrics)
    # Recenter each progressively finer ring on the preceding optimum.  A
    # single collection of rings around the coarse seed can stop several
    # degrees from a true arbitrary axis and depress complete-group support
    # enough to trigger a false negative.
    for angle_deg in (12.0, 6.0, 3.0, 1.0, 0.35):
        for axis in _axis_refinement_ring(best_axis, angle_deg):
            alignment = _axis_alignment(axis)
            metrics = _operation_scores(
                prepared,
                alignment @ generator @ alignment.T,
                center_zyx,
            )
            value = objective_value(metrics)
            if value > best_value:
                best_axis = axis
                best_metrics = metrics
                best_value = value
    assert best_metrics is not None
    return best_axis, best_metrics


def _cyclic_objective_value(metrics: dict[str, float], objective: str) -> float:
    if objective == "half1":
        return float(metrics["half1"])
    if objective == "half2":
        return float(metrics["half2"])
    if objective != "consensus":
        raise SymmetryScanError(f"Unknown axis-line objective {objective!r}.")
    return float(min(metrics["half1"], metrics["half2"], metrics["cross_retention"]))


def _refine_cyclic_axis_line_center(
    prepared: _PreparedMaps,
    order: int,
    axis_xyz: np.ndarray,
    initial_center_zyx: Sequence[float],
    *,
    objective: str,
) -> tuple[np.ndarray, dict[str, float]]:
    """Refine only the identifiable, transverse position of a cyclic axis."""
    axis = _canonical_axis(axis_xyz)
    seed = np.asarray((1.0, 0.0, 0.0), dtype=np.float64)
    if abs(float(np.dot(axis, seed))) > 0.85:
        seed = np.asarray((0.0, 1.0, 0.0), dtype=np.float64)
    tangent_1 = np.cross(axis, seed)
    tangent_1 /= np.linalg.norm(tangent_1)
    tangent_2 = np.cross(axis, tangent_1)
    generator = Rotation.from_rotvec(axis * (2.0 * math.pi / order)).as_matrix()
    initial_center = np.asarray(initial_center_zyx, dtype=np.float64)
    best_center = initial_center.copy()
    best_metrics = _operation_scores(prepared, generator, best_center)
    best_value = _cyclic_objective_value(best_metrics, objective)
    maximum_radius = min(
        _CENTER_LINE_SEARCH_MAX_PIX,
        max(1.0, 0.5 * prepared.effective_resolution_A / prepared.voxel_size_A),
    )
    shape = np.asarray(prepared.half1.shape, dtype=np.float64)
    for radius in (maximum_radius, maximum_radius / 2.0, maximum_radius / 4.0):
        # Evaluate one complete ring about the preceding scale's optimum.  Do
        # not mutate the ring origin while walking through its phases: that
        # turns a bounded local search into an order-dependent random walk.
        ring_center = best_center.copy()
        ring_best_center = best_center
        ring_best_metrics = best_metrics
        ring_best_value = best_value
        for phase in np.linspace(
            0.0,
            2.0 * math.pi,
            _CENTER_LINE_SEARCH_PHASES,
            endpoint=False,
        ):
            offset_xyz = radius * (
                math.cos(float(phase)) * tangent_1
                + math.sin(float(phase)) * tangent_2
            )
            candidate = ring_center + offset_xyz[::-1]
            if np.any(candidate < 1.0) or np.any(candidate > shape - 2.0):
                continue
            total_displacement_xyz = (candidate - initial_center)[::-1]
            transverse_displacement = total_displacement_xyz - (
                np.dot(total_displacement_xyz, axis) * axis
            )
            if float(np.linalg.norm(transverse_displacement)) > maximum_radius + 1e-8:
                continue
            metrics = _operation_scores(prepared, generator, candidate)
            value = _cyclic_objective_value(metrics, objective)
            if value > ring_best_value:
                ring_best_center = candidate
                ring_best_metrics = metrics
                ring_best_value = value
        best_center = ring_best_center
        best_metrics = ring_best_metrics
        best_value = ring_best_value
    return np.asarray(best_center, dtype=np.float64), best_metrics


def _axis_line_disagreement_A(
    first_axis_xyz: np.ndarray,
    first_center_zyx: Sequence[float],
    second_axis_xyz: np.ndarray,
    second_center_zyx: Sequence[float],
    voxel_size_A: float,
) -> float:
    """Return the shortest distance between two unoriented infinite axes."""
    first_axis = _canonical_axis(first_axis_xyz)
    second_axis = _canonical_axis(second_axis_xyz)
    if np.dot(first_axis, second_axis) < 0.0:
        second_axis = -second_axis
    displacement_xyz = (
        np.asarray(second_center_zyx, dtype=np.float64)
        - np.asarray(first_center_zyx, dtype=np.float64)
    )[::-1]
    normal = np.cross(first_axis, second_axis)
    normal_norm = float(np.linalg.norm(normal))
    if normal_norm <= 1e-8:
        distance_pix = float(np.linalg.norm(np.cross(displacement_xyz, first_axis)))
    else:
        distance_pix = abs(float(np.dot(displacement_xyz, normal))) / normal_norm
    return float(distance_pix * voxel_size_A)


def _nonidentity_operators(matrices: np.ndarray) -> list[np.ndarray]:
    identity = np.eye(3, dtype=np.float64)
    return [matrix for matrix in matrices if not np.allclose(matrix, identity, atol=1e-8)]


def _summarize(values: Sequence[float]) -> dict[str, float]:
    array = np.asarray(values, dtype=np.float64)
    if array.size == 0:
        return {"mean": 1.0, "minimum": 1.0, "p10": 1.0}
    return {
        "mean": float(np.mean(array)),
        "minimum": float(np.min(array)),
        "p10": float(np.percentile(array, 10.0)),
    }


def _score_oriented_group(
    prepared: _PreparedMaps,
    canonical_operators: np.ndarray,
    orientation: np.ndarray,
    center_zyx: Sequence[float] | None = None,
) -> dict[str, Any]:
    per_operation: list[dict[str, float]] = []
    for canonical in _nonidentity_operators(canonical_operators):
        matrix = orientation @ canonical @ orientation.T
        metrics = _sample_operation(prepared, matrix, center_zyx)
        per_operation.append(
            {
                "rotation_angle_deg": float(np.degrees(_rotation_angle(canonical))),
                **metrics,
            }
        )
    half1 = _summarize([item["half1"] for item in per_operation])
    half2 = _summarize([item["half2"] for item in per_operation])
    cross = _summarize([item["cross"] for item in per_operation])
    retention = _summarize([item["cross_retention"] for item in per_operation])
    complete_support = min(
        0.7 * half1["mean"] + 0.3 * half1["p10"],
        0.7 * half2["mean"] + 0.3 * half2["p10"],
        0.7 * retention["mean"] + 0.3 * retention["p10"],
    )
    return {
        "half1": half1,
        "half2": half2,
        "cross": cross,
        "cross_retention": retention,
        "complete_support": float(complete_support),
        "per_operation": per_operation,
    }


def _complete_group_replication_supported(metrics: dict[str, Any]) -> bool:
    """Return whether one fixed complete-group orientation replicates in both halves."""
    return bool(
        metrics["half1"]["mean"] >= _MIN_ROTATIONAL_CORRELATION
        and metrics["half1"]["p10"] > 0.0
        and metrics["half2"]["mean"] >= _MIN_ROTATIONAL_CORRELATION
        and metrics["half2"]["p10"] > 0.0
        and metrics["cross_retention"]["mean"] >= _MIN_CROSS_HALF_RETENTION
        and metrics["cross_retention"]["p10"] > 0.0
    )


def _replication_metrics_record(metrics: dict[str, Any]) -> dict[str, Any]:
    """Keep reciprocal complete-group diagnostics compact in the public report."""
    return {
        "half1": metrics["half1"],
        "half2": metrics["half2"],
        "cross_retention": metrics["cross_retention"],
        "complete_support": metrics["complete_support"],
        "passes_support_heuristics": _complete_group_replication_supported(metrics),
    }


def _candidate_subgroups(group: str) -> list[str]:
    if group.startswith("C"):
        order = int(group[1:])
        return [f"C{value}" for value in range(2, order) if order % value == 0]
    if group.startswith("D"):
        order = int(group[1:])
        cyclic = [f"C{value}" for value in range(2, order + 1) if order % value == 0]
        dihedral = [f"D{value}" for value in range(2, order) if order % value == 0]
        return cyclic + dihedral
    return {
        "T": ["C2", "C3"],
        "O": ["C2", "C3", "C4", "D2"],
        "I": ["C2", "C3", "C5"],
    }.get(group, [])


def _candidate_record(
    prepared: _PreparedMaps,
    *,
    group: str,
    stage: int,
    axis_xyz: np.ndarray | None,
    orientation: np.ndarray,
    group_metrics: dict[str, Any],
    evidence_z: float,
    axis_agreement_deg: float | None,
    identifiable: bool,
    generator_metrics: dict[str, float] | None,
    search_note: str,
    half_map_consistent: bool | None = None,
    symmetry_center_zyx: Sequence[float] | None = None,
) -> dict[str, Any]:
    complete = float(group_metrics["complete_support"])
    center = (
        _prepared_rotation_center(prepared)
        if symmetry_center_zyx is None
        else np.asarray(symmetry_center_zyx, dtype=np.float64)
    )
    shape = np.asarray(prepared.half1.shape, dtype=np.float64)
    center_edge_clearance_A = float(
        np.min(np.minimum(center, shape - 1.0 - center)) * prepared.voxel_size_A
    )
    center_edge_clearance_threshold_A = max(
        2.0 * prepared.voxel_size_A,
        0.5 * prepared.effective_resolution_A,
    )
    candidate_center_stable = bool(
        prepared.center_stable
        and center_edge_clearance_A >= center_edge_clearance_threshold_A
    )
    gate_results: dict[str, dict[str, Any]] = {
        "identifiable": {
            "passed": bool(identifiable),
            "actual": bool(identifiable),
            "criterion": "order <= max_testable_order",
        },
        "half_map_identity": {
            "passed": prepared.identity_half_correlation
            >= _MIN_IDENTITY_HALF_CORRELATION,
            "actual": prepared.identity_half_correlation,
            "minimum": _MIN_IDENTITY_HALF_CORRELATION,
        },
        "center_stability": {
            "passed": candidate_center_stable,
            "prepared_center_stable": prepared.center_stable,
            "candidate_edge_clearance_A": center_edge_clearance_A,
            "minimum_edge_clearance_A": center_edge_clearance_threshold_A,
        },
        "axis_null_z": {
            "passed": evidence_z >= _MIN_AXIS_NULL_Z,
            "actual": float(evidence_z),
            "minimum": _MIN_AXIS_NULL_Z,
        },
        "axis_agreement": {
            "passed": axis_agreement_deg is None or axis_agreement_deg <= 35.0,
            "actual_deg": axis_agreement_deg,
            "maximum_deg": 35.0,
            "applicable": axis_agreement_deg is not None,
        },
        "half1_complete_group_rotation": {
            "passed": group_metrics["half1"]["mean"]
            >= _MIN_ROTATIONAL_CORRELATION,
            "actual": group_metrics["half1"]["mean"],
            "minimum": _MIN_ROTATIONAL_CORRELATION,
        },
        "half2_complete_group_rotation": {
            "passed": group_metrics["half2"]["mean"]
            >= _MIN_ROTATIONAL_CORRELATION,
            "actual": group_metrics["half2"]["mean"],
            "minimum": _MIN_ROTATIONAL_CORRELATION,
        },
        "cross_half_retention": {
            "passed": group_metrics["cross_retention"]["mean"]
            >= _MIN_CROSS_HALF_RETENTION,
            "actual": group_metrics["cross_retention"]["mean"],
            "minimum": _MIN_CROSS_HALF_RETENTION,
        },
        "cross_half_lower_tail": {
            "passed": group_metrics["cross_retention"]["p10"] > 0.0,
            "actual": group_metrics["cross_retention"]["p10"],
            "exclusive_minimum": 0.0,
        },
    }
    support_failures: list[str] = []
    if not candidate_center_stable:
        support_failures.append("candidate_center_unstable_or_too_near_edge")
    if not identifiable:
        support_failures.append("order_not_identifiable_at_effective_resolution")
    if prepared.identity_half_correlation < _MIN_IDENTITY_HALF_CORRELATION:
        support_failures.append("half_map_identity_below_minimum")
    if evidence_z < _MIN_AXIS_NULL_Z:
        support_failures.append("axis_evidence_below_minimum")
    if axis_agreement_deg is not None and axis_agreement_deg > 35.0:
        support_failures.append("independent_axis_disagreement")
    if group_metrics["half1"]["mean"] < _MIN_ROTATIONAL_CORRELATION:
        support_failures.append("half1_rotation_below_minimum")
    if group_metrics["half2"]["mean"] < _MIN_ROTATIONAL_CORRELATION:
        support_failures.append("half2_rotation_below_minimum")
    if group_metrics["cross_retention"]["mean"] < _MIN_CROSS_HALF_RETENTION:
        support_failures.append("cross_half_retention_below_minimum")
    if group_metrics["cross_retention"]["p10"] <= 0.0:
        support_failures.append("cross_half_lower_tail_nonpositive")
    supported = not support_failures
    if half_map_consistent is not None:
        gate_results["reciprocal_half_map_consistency"] = {
            "passed": bool(half_map_consistent),
            "actual": bool(half_map_consistent),
        }
        if not half_map_consistent:
            support_failures.append("reciprocal_half_map_consistency_failed")
        supported = supported and half_map_consistent
    match = re.fullmatch(r"([CD])(\d+)", group)
    family = match.group(1) if match else group
    order = int(match.group(2)) if match else {"T": 12, "O": 24, "I": 60}[group]
    rank_score = complete + 0.025 * math.tanh(evidence_z / 3.0)
    preprocessing_valid = bool(
        candidate_center_stable and prepared.requested_cutoff_honored
    )
    if supported:
        support_status = "supported"
    elif not preprocessing_valid:
        support_status = "inconclusive"
    elif not identifiable or (
        prepared.identity_half_correlation < _MIN_IDENTITY_HALF_CORRELATION
    ):
        support_status = "untestable"
    else:
        support_status = "not_supported"
    return {
        "group": group,
        "abstract_group": "I" if group == "I" else group,
        "relion_convention": None,
        "family": family,
        "order": order,
        "stage": stage,
        "axis_xyz": axis_xyz.tolist() if axis_xyz is not None else None,
        "orientation_matrix_xyz": np.asarray(orientation).tolist(),
        "half1_score": group_metrics["half1"]["mean"],
        "half2_score": group_metrics["half2"]["mean"],
        "cross_half_score": group_metrics["cross_retention"]["mean"],
        "agreement_score": complete,
        "rank_score": float(rank_score),
        "half_map_consistent": supported,
        "support_status": support_status,
        "support_failures": support_failures,
        "gate_results": gate_results,
        "identifiable": identifiable,
        "center_stable": candidate_center_stable,
        "center_edge_clearance_A": center_edge_clearance_A,
        "requested_cutoff_honored": prepared.requested_cutoff_honored,
        "negative_inference_valid": preprocessing_valid,
        "symmetry_center_zyx": center.tolist(),
        "axis_agreement_deg": axis_agreement_deg,
        "evidence_z_vs_axis_null": float(evidence_z),
        "generator_metrics": generator_metrics,
        "complete_group_metrics": group_metrics,
        "subgroups": _candidate_subgroups(group),
        "search_note": search_note,
    }


def _cyclic_candidate(
    prepared: _PreparedMaps,
    spectrum: _Spectrum,
    stage: int,
) -> dict[str, Any]:
    common_center = _prepared_rotation_center(prepared)
    half1_initial_center = (
        common_center
        if prepared.half1_center_zyx is None
        else np.asarray(prepared.half1_center_zyx, dtype=np.float64)
    )
    half2_initial_center = (
        common_center
        if prepared.half2_center_zyx is None
        else np.asarray(prepared.half2_center_zyx, dtype=np.float64)
    )
    half1_axis, half1_generator = _refine_cyclic_axis(
        prepared,
        spectrum.order,
        spectrum.axes_xyz[spectrum.half1_index],
        objective="half1",
        center_zyx=half1_initial_center,
    )
    half2_axis, half2_generator = _refine_cyclic_axis(
        prepared,
        spectrum.order,
        spectrum.axes_xyz[spectrum.half2_index],
        objective="half2",
        center_zyx=half2_initial_center,
    )
    half1_center, half1_generator = _refine_cyclic_axis_line_center(
        prepared,
        spectrum.order,
        half1_axis,
        half1_initial_center,
        objective="half1",
    )
    half2_center, half2_generator = _refine_cyclic_axis_line_center(
        prepared,
        spectrum.order,
        half2_axis,
        half2_initial_center,
        objective="half2",
    )
    if np.dot(half1_axis, half2_axis) < 0:
        half2_axis = -half2_axis
    independent_axis_agreement = axis_angle_degrees(half1_axis, half2_axis)
    axis_line_agreement_A = _axis_line_disagreement_A(
        half1_axis,
        half1_center,
        half2_axis,
        half2_center,
        prepared.voxel_size_A,
    )
    axis_line_agreement_threshold_A = max(
        prepared.center_disagreement_threshold_A,
        2.0 * prepared.voxel_size_A,
        0.5 * prepared.effective_resolution_A,
    )
    operators = symmetry_operators(f"C{spectrum.order}")
    half1_orientation = _axis_alignment(half1_axis)
    half2_orientation = _axis_alignment(half2_axis)
    half1_group_metrics = _score_oriented_group(
        prepared,
        operators,
        half1_orientation,
        half1_center,
    )
    half2_group_metrics = _score_oriented_group(
        prepared,
        operators,
        half2_orientation,
        half2_center,
    )
    (
        selected_orientation_source,
        axis,
        center,
        orientation,
        metrics,
        generator_metrics,
    ) = max(
        (
            (
                "half1_independent",
                half1_axis,
                half1_center,
                half1_orientation,
                half1_group_metrics,
                half1_generator,
            ),
            (
                "half2_independent",
                half2_axis,
                half2_center,
                half2_orientation,
                half2_group_metrics,
                half2_generator,
            ),
        ),
        key=lambda item: float(item[4]["complete_support"]),
    )
    reciprocal_consistent = bool(
        independent_axis_agreement <= 35.0
        and axis_line_agreement_A <= axis_line_agreement_threshold_A
        and half1_generator["half2"] >= _MIN_ROTATIONAL_CORRELATION
        and half2_generator["half1"] >= _MIN_ROTATIONAL_CORRELATION
        and half1_generator["cross_retention"] >= _MIN_CROSS_HALF_RETENTION
        and half2_generator["cross_retention"] >= _MIN_CROSS_HALF_RETENTION
        and _complete_group_replication_supported(half1_group_metrics)
        and _complete_group_replication_supported(half2_group_metrics)
    )
    candidate = _candidate_record(
        prepared,
        group=f"C{spectrum.order}",
        stage=stage,
        axis_xyz=axis,
        orientation=orientation,
        group_metrics=metrics,
        evidence_z=min(spectrum.half1_evidence_z, spectrum.half2_evidence_z),
        axis_agreement_deg=independent_axis_agreement,
        identifiable=spectrum.identifiable,
        generator_metrics=generator_metrics,
        search_note=(
            "The axis was discovered and refined independently in each half-map, reciprocally evaluated "
            "without reoptimizing, and the better-supported fixed independent axis was used for "
            "complete-group reporting."
        ),
        half_map_consistent=reciprocal_consistent,
        symmetry_center_zyx=center,
    )
    candidate["half1_axis_xyz"] = half1_axis.tolist()
    candidate["half2_axis_xyz"] = half2_axis.tolist()
    candidate["reciprocal_generator_metrics"] = {
        "half1_optimized_axis": half1_generator,
        "half2_optimized_axis": half2_generator,
    }
    candidate["selected_orientation_source"] = selected_orientation_source
    candidate["axis_line_point_zyx"] = np.asarray(center).tolist()
    candidate["axis_line_point_is_unique"] = False
    candidate["axis_line_equivalence"] = (
        "Translations of the reported point along axis_xyz define the same cyclic axis line."
    )
    candidate["half1_axis_line_point_zyx"] = half1_center.tolist()
    candidate["half2_axis_line_point_zyx"] = half2_center.tolist()
    candidate["axis_line_agreement_A"] = axis_line_agreement_A
    candidate["axis_line_agreement_threshold_A"] = (
        axis_line_agreement_threshold_A
    )
    if axis_line_agreement_A > axis_line_agreement_threshold_A:
        candidate["support_failures"].append(
            "independent_axis_line_disagreement"
        )
    candidate["reciprocal_complete_group_metrics"] = {
        "half1_optimized_orientation": _replication_metrics_record(
            half1_group_metrics
        ),
        "half2_optimized_orientation": _replication_metrics_record(
            half2_group_metrics
        ),
    }
    return candidate


def _secondary_axis_operator(
    operators: np.ndarray,
    primary_axis_xyz: Sequence[float],
) -> np.ndarray:
    primary = _canonical_axis(primary_axis_xyz)
    best: tuple[float, float, np.ndarray] | None = None
    for matrix in _nonidentity_operators(operators):
        rotvec = Rotation.from_matrix(matrix).as_rotvec()
        if np.linalg.norm(rotvec) <= np.finfo(np.float64).eps:
            continue
        axis = _canonical_axis(rotvec)
        separation = axis_angle_degrees(axis, primary)
        if separation < 5.0:
            continue
        key = (separation, -_rotation_angle(matrix), matrix)
        if best is None or key[:2] > best[:2]:
            best = key
    if best is None:
        raise SymmetryScanError("Could not identify a non-collinear group generator.")
    return np.asarray(best[2], dtype=np.float64)


def _secondary_axis_operators(
    operators: np.ndarray,
    primary_axis_xyz: Sequence[float],
    *,
    limit: int = 6,
) -> list[np.ndarray]:
    primary = _canonical_axis(primary_axis_xyz)
    selected: list[tuple[np.ndarray, np.ndarray]] = []
    candidates: list[tuple[float, float, np.ndarray, np.ndarray]] = []
    for matrix in _nonidentity_operators(operators):
        rotvec = Rotation.from_matrix(matrix).as_rotvec()
        if np.linalg.norm(rotvec) <= np.finfo(np.float64).eps:
            continue
        axis = _canonical_axis(rotvec)
        separation = axis_angle_degrees(axis, primary)
        if separation < 5.0:
            continue
        candidates.append((separation, _rotation_angle(matrix), axis, matrix))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    for _, _, axis, matrix in candidates:
        if any(axis_angle_degrees(axis, existing) < 5.0 for existing, _ in selected):
            continue
        selected.append((axis, np.asarray(matrix, dtype=np.float64)))
        if len(selected) >= limit:
            break
    if not selected:
        raise SymmetryScanError("Could not identify non-collinear group generators.")
    return [matrix for _, matrix in selected]


def _twist_objective(
    prepared: _PreparedMaps,
    orientation: np.ndarray,
    search_operators: Sequence[np.ndarray],
    objective: str,
) -> float:
    scores: list[float] = []
    for operator in search_operators:
        metrics = _operation_scores(
            prepared,
            orientation @ operator @ orientation.T,
        )
        if objective == "half1":
            scores.append(metrics["half1"])
        elif objective == "half2":
            scores.append(metrics["half2"])
        elif objective == "consensus":
            scores.append(
                min(
                    metrics["half1"],
                    metrics["half2"],
                    metrics["cross_retention"],
                )
            )
        else:
            raise SymmetryScanError(f"Unknown twist-search objective {objective!r}.")
    return float(np.mean(scores))


def _search_twist_orientation(
    prepared: _PreparedMaps,
    *,
    canonical_axis: np.ndarray,
    physical_axis: np.ndarray,
    search_operators: Sequence[np.ndarray],
    period: float,
    objective: str,
) -> tuple[np.ndarray, list[float]]:
    best_angle = 0.0
    best_orientation = _vector_alignment(canonical_axis, physical_axis)
    best_score = -math.inf
    coarse_scores: list[float] = []
    coarse_count = 24
    step = period / coarse_count
    best_direction = np.asarray(physical_axis, dtype=np.float64)
    for direction in (best_direction, -best_direction):
        base = _vector_alignment(canonical_axis, direction)
        for angle in np.linspace(0.0, period, coarse_count, endpoint=False):
            orientation = _rotation_about_axis(direction, float(angle)) @ base
            score = _twist_objective(
                prepared,
                orientation,
                search_operators,
                objective,
            )
            coarse_scores.append(score)
            if score > best_score:
                best_angle = float(angle)
                best_direction = direction
                best_orientation = orientation
                best_score = score
    base = _vector_alignment(canonical_axis, best_direction)
    for _ in range(4):
        candidates = best_angle + step * np.linspace(-1.0, 1.0, 9)
        for angle in candidates:
            wrapped = float(angle % period)
            orientation = _rotation_about_axis(best_direction, wrapped) @ base
            score = _twist_objective(
                prepared,
                orientation,
                search_operators,
                objective,
            )
            if score > best_score:
                best_angle = wrapped
                best_orientation = orientation
                best_score = score
        step /= 3.0
    return best_orientation, [float(value) for value in coarse_scores]


def _dihedral_candidate(
    prepared: _PreparedMaps,
    spectrum: _Spectrum,
    stage: int,
) -> dict[str, Any]:
    half1_axis, _ = _refine_cyclic_axis(
        prepared,
        spectrum.order,
        spectrum.axes_xyz[spectrum.half1_index],
        objective="half1",
    )
    half2_axis, _ = _refine_cyclic_axis(
        prepared,
        spectrum.order,
        spectrum.axes_xyz[spectrum.half2_index],
        objective="half2",
    )
    if np.dot(half1_axis, half2_axis) < 0:
        half2_axis = -half2_axis
    axis_agreement = axis_angle_degrees(half1_axis, half2_axis)
    operators = symmetry_operators(f"D{spectrum.order}")
    search_operator = _secondary_axis_operator(operators, (0.0, 0.0, 1.0))
    half1_orientation, half1_twist_scores = _search_twist_orientation(
        prepared,
        canonical_axis=np.asarray((0.0, 0.0, 1.0)),
        physical_axis=half1_axis,
        search_operators=[search_operator],
        period=math.pi / spectrum.order,
        objective="half1",
    )
    half2_orientation, half2_twist_scores = _search_twist_orientation(
        prepared,
        canonical_axis=np.asarray((0.0, 0.0, 1.0)),
        physical_axis=half2_axis,
        search_operators=[search_operator],
        period=math.pi / spectrum.order,
        objective="half2",
    )
    half1_group_metrics = _score_oriented_group(
        prepared,
        operators,
        half1_orientation,
    )
    half2_group_metrics = _score_oriented_group(
        prepared,
        operators,
        half2_orientation,
    )
    operator_set_agreement = _operator_set_distance_deg(
        half1_orientation,
        half2_orientation,
        operators,
    )
    selected_orientation_source, best_orientation, best_metrics = max(
        (
            ("half1_independent", half1_orientation, half1_group_metrics),
            ("half2_independent", half2_orientation, half2_group_metrics),
        ),
        key=lambda item: float(item[2]["complete_support"]),
    )
    axis = _canonical_axis(best_orientation @ np.asarray((0.0, 0.0, 1.0)))
    is_d2 = spectrum.order == 2
    if is_d2:
        generator_metrics = None
    else:
        cyclic_generator = Rotation.from_rotvec(
            np.asarray((0.0, 0.0, 2.0 * math.pi / spectrum.order))
        ).as_matrix()
        generator_metrics = _operation_scores(
            prepared,
            best_orientation @ cyclic_generator @ best_orientation.T,
        )
    reciprocal_consistent = bool(
        (is_d2 or axis_agreement <= 35.0)
        and operator_set_agreement <= _MAX_OPERATOR_SET_DISAGREEMENT_DEG
        and _complete_group_replication_supported(half1_group_metrics)
        and _complete_group_replication_supported(half2_group_metrics)
    )

    perpendicular_twofold: list[dict[str, float]] = []
    for item, canonical in zip(
        best_metrics["per_operation"],
        _nonidentity_operators(operators),
        strict=True,
    ):
        if math.isclose(_rotation_angle(canonical), math.pi, abs_tol=1e-6):
            rotvec = Rotation.from_matrix(canonical).as_rotvec()
            if np.linalg.norm(rotvec) > 0:
                direction = rotvec / np.linalg.norm(rotvec)
                if abs(float(direction[2])) < 0.25:
                    perpendicular_twofold.append(item)
    candidate = _candidate_record(
        prepared,
        group=f"D{spectrum.order}",
        stage=stage,
        # D2 has an unordered triad rather than a distinguished principal axis.
        axis_xyz=None if is_d2 else axis,
        orientation=best_orientation,
        group_metrics=best_metrics,
        evidence_z=min(spectrum.half1_evidence_z, spectrum.half2_evidence_z),
        axis_agreement_deg=None if is_d2 else axis_agreement,
        identifiable=spectrum.identifiable,
        generator_metrics=generator_metrics,
        search_note=(
            "Each half independently located a dihedral orientation. Complete oriented operator sets "
            "were compared and reciprocally scored; D2 was treated as an unordered C2-axis frame."
            if is_d2
            else "Each half independently located its principal Cn axis and perpendicular C2 orientation; "
            "complete oriented operator-set agreement and reciprocal scoring preceded reporting."
        ),
        half_map_consistent=reciprocal_consistent,
    )
    candidate["half1_axis_xyz"] = half1_axis.tolist()
    candidate["half2_axis_xyz"] = half2_axis.tolist()
    candidate["axis_agreement_deg"] = axis_agreement
    candidate["axis_agreement_is_gating"] = not is_d2
    candidate["operator_set_agreement_deg"] = operator_set_agreement
    candidate["operator_set_agreement_threshold_deg"] = (
        _MAX_OPERATOR_SET_DISAGREEMENT_DEG
    )
    candidate["gate_results"]["operator_set_agreement"] = {
        "passed": operator_set_agreement <= _MAX_OPERATOR_SET_DISAGREEMENT_DEG,
        "actual_deg": operator_set_agreement,
        "maximum_deg": _MAX_OPERATOR_SET_DISAGREEMENT_DEG,
    }
    candidate["selected_orientation_source"] = selected_orientation_source
    candidate["reciprocal_complete_group_metrics"] = {
        "half1_optimized_orientation": _replication_metrics_record(
            half1_group_metrics
        ),
        "half2_optimized_orientation": _replication_metrics_record(
            half2_group_metrics
        ),
    }
    candidate["twist_search_scores"] = {
        "half1": half1_twist_scores,
        "half2": half2_twist_scores,
    }
    if is_d2:
        candidate["axis_frame_xyz"] = _d2_axis_frame(
            operators,
            best_orientation,
        )
    perpendicular_metrics = (
        {
            "half1": _summarize([item["half1"] for item in perpendicular_twofold]),
            "half2": _summarize([item["half2"] for item in perpendicular_twofold]),
            "cross": _summarize([item["cross"] for item in perpendicular_twofold]),
            "cross_retention": _summarize(
                [item["cross_retention"] for item in perpendicular_twofold]
            ),
        }
        if perpendicular_twofold
        else None
    )
    candidate["perpendicular_c2_complete_metrics"] = perpendicular_metrics
    perpendicular_retention = (
        None
        if perpendicular_metrics is None
        else perpendicular_metrics["cross_retention"]["mean"]
    )
    candidate["perpendicular_c2_cross_half_retention"] = perpendicular_retention
    perpendicular_supported = bool(
        perpendicular_metrics is not None
        and perpendicular_metrics["half1"]["mean"] >= _MIN_ROTATIONAL_CORRELATION
        and perpendicular_metrics["half2"]["mean"] >= _MIN_ROTATIONAL_CORRELATION
        and perpendicular_metrics["cross_retention"]["mean"]
        >= _MIN_CROSS_HALF_RETENTION
        and perpendicular_metrics["cross_retention"]["p10"] > 0.0
    )
    candidate["gate_results"]["perpendicular_c2_support"] = {
        "passed": perpendicular_supported,
        "actual_cross_half_retention": perpendicular_retention,
        "actual_half1_mean": (
            None if perpendicular_metrics is None else perpendicular_metrics["half1"]["mean"]
        ),
        "actual_half2_mean": (
            None if perpendicular_metrics is None else perpendicular_metrics["half2"]["mean"]
        ),
        "actual_cross_half_retention_p10": (
            None
            if perpendicular_metrics is None
            else perpendicular_metrics["cross_retention"]["p10"]
        ),
        "minimum_cross_half_retention": _MIN_CROSS_HALF_RETENTION,
        "minimum_half_rotational_correlation": _MIN_ROTATIONAL_CORRELATION,
        "minimum_cross_half_retention_p10_exclusive": 0.0,
    }
    if not perpendicular_supported:
        candidate["half_map_consistent"] = False
        if "perpendicular_c2_support_failed" not in candidate["support_failures"]:
            candidate["support_failures"].append("perpendicular_c2_support_failed")
        if candidate["support_status"] == "supported":
            candidate["support_status"] = "not_supported"
    return candidate


def _oriented_nonidentity_operators(
    operators: np.ndarray,
    orientation: np.ndarray,
) -> np.ndarray:
    return np.asarray(
        [
            orientation @ operator @ orientation.T
            for operator in _nonidentity_operators(operators)
        ],
        dtype=np.float64,
    )


def _operator_set_distance_deg(
    first: np.ndarray,
    second: np.ndarray,
    operators: np.ndarray,
) -> float:
    """Compare complete oriented groups without privileging a generator or axis.

    The symmetric Hausdorff distance uses rotation-geodesic distance and only
    pairs operations with the same rotation angle.  It therefore recognizes
    equivalent characteristic axes in T/O/I and permutations of D2's three
    indistinguishable two-fold axes.
    """
    first_set = _oriented_nonidentity_operators(operators, first)
    second_set = _oriented_nonidentity_operators(operators, second)
    if first_set.shape != second_set.shape or first_set.size == 0:
        raise SymmetryScanError("Cannot compare incompatible oriented operator sets.")

    first_angles = np.asarray([_rotation_angle(item) for item in first_set])
    second_angles = np.asarray([_rotation_angle(item) for item in second_set])
    distances = np.full(
        (len(first_set), len(second_set)),
        np.inf,
        dtype=np.float64,
    )
    for first_index, first_operator in enumerate(first_set):
        for second_index, second_operator in enumerate(second_set):
            if not math.isclose(
                float(first_angles[first_index]),
                float(second_angles[second_index]),
                abs_tol=1e-6,
            ):
                continue
            distances[first_index, second_index] = np.degrees(
                _rotation_angle(first_operator.T @ second_operator)
            )
    row_minimum = np.min(distances, axis=1)
    column_minimum = np.min(distances, axis=0)
    if not np.all(np.isfinite(row_minimum)) or not np.all(np.isfinite(column_minimum)):
        return math.inf
    return float(max(np.max(row_minimum), np.max(column_minimum)))


def _d2_axis_frame(
    operators: np.ndarray,
    orientation: np.ndarray,
) -> list[list[float]]:
    axes: list[np.ndarray] = []
    for operator in _nonidentity_operators(operators):
        if not math.isclose(_rotation_angle(operator), math.pi, abs_tol=1e-6):
            continue
        vector = Rotation.from_matrix(operator).as_rotvec()
        axes.append(_canonical_axis(orientation @ vector))
    axes.sort(key=lambda axis: tuple(round(float(value), 12) for value in axis))
    if len(axes) != 3:
        raise SymmetryScanError("D2 must contain an unordered frame of three C2 axes.")
    return [axis.tolist() for axis in axes]


def _principal_group_axis(
    operators: np.ndarray,
    order: int,
) -> tuple[np.ndarray, np.ndarray]:
    target_angle = 2.0 * math.pi / order
    candidates: list[tuple[float, np.ndarray, np.ndarray]] = []
    for matrix in _nonidentity_operators(operators):
        angle = _rotation_angle(matrix)
        rotvec = Rotation.from_matrix(matrix).as_rotvec()
        if np.linalg.norm(rotvec) <= np.finfo(np.float64).eps:
            continue
        candidates.append(
            (
                abs(angle - target_angle),
                _canonical_axis(rotvec),
                matrix,
            )
        )
    if not candidates:
        raise SymmetryScanError("Could not identify a principal polyhedral axis.")
    candidates.sort(key=lambda item: item[0])
    _, axis, generator = candidates[0]
    return axis, np.asarray(generator, dtype=np.float64)


def _polyhedral_candidate(
    prepared: _PreparedMaps,
    group: str,
    characteristic_spectrum: _Spectrum,
) -> dict[str, Any]:
    characteristic_order = {"T": 3, "O": 4, "I": 5}[group]
    operators = symmetry_operators(group)
    canonical_axis, _ = _principal_group_axis(operators, characteristic_order)
    secondary_operators = _secondary_axis_operators(
        operators,
        canonical_axis,
    )
    half1_axis, _ = _refine_cyclic_axis(
        prepared,
        characteristic_order,
        characteristic_spectrum.axes_xyz[characteristic_spectrum.half1_index],
        objective="half1",
    )
    half2_axis, _ = _refine_cyclic_axis(
        prepared,
        characteristic_order,
        characteristic_spectrum.axes_xyz[characteristic_spectrum.half2_index],
        objective="half2",
    )
    if np.dot(half1_axis, half2_axis) < 0:
        half2_axis = -half2_axis
    axis_agreement = axis_angle_degrees(half1_axis, half2_axis)
    period = 2.0 * math.pi / characteristic_order
    half1_orientation, half1_twist_scores = _search_twist_orientation(
        prepared,
        canonical_axis=canonical_axis,
        physical_axis=half1_axis,
        search_operators=secondary_operators,
        period=period,
        objective="half1",
    )
    half2_orientation, half2_twist_scores = _search_twist_orientation(
        prepared,
        canonical_axis=canonical_axis,
        physical_axis=half2_axis,
        search_operators=secondary_operators,
        period=period,
        objective="half2",
    )
    half1_group_metrics = _score_oriented_group(
        prepared,
        operators,
        half1_orientation,
    )
    half2_group_metrics = _score_oriented_group(
        prepared,
        operators,
        half2_orientation,
    )
    operator_set_agreement = _operator_set_distance_deg(
        half1_orientation,
        half2_orientation,
        operators,
    )
    selected_orientation_source, best_orientation, best = max(
        (
            ("half1_independent", half1_orientation, half1_group_metrics),
            ("half2_independent", half2_orientation, half2_group_metrics),
        ),
        key=lambda item: float(item[2]["complete_support"]),
    )
    reported_axis = _canonical_axis(best_orientation @ canonical_axis)
    twist_peak_z = min(
        _robust_z(
            float(np.max(half1_twist_scores)),
            np.asarray(half1_twist_scores, dtype=np.float64),
        ),
        _robust_z(
            float(np.max(half2_twist_scores)),
            np.asarray(half2_twist_scores, dtype=np.float64),
        ),
    )
    evidence_z = min(
        characteristic_spectrum.half1_evidence_z,
        characteristic_spectrum.half2_evidence_z,
    )
    reciprocal_consistent = bool(
        operator_set_agreement <= _MAX_OPERATOR_SET_DISAGREEMENT_DEG
        and _complete_group_replication_supported(half1_group_metrics)
        and _complete_group_replication_supported(half2_group_metrics)
    )
    candidate = _candidate_record(
        prepared,
        group=group,
        stage=1,
        axis_xyz=reported_axis,
        orientation=best_orientation,
        group_metrics=best,
        evidence_z=evidence_z,
        # A polyhedral group has multiple equivalent characteristic axes.  Raw
        # axis disagreement remains a diagnostic below but must not veto a
        # match between equal complete oriented operator sets.
        axis_agreement_deg=None,
        identifiable=(
            characteristic_order <= prepared.identifiability_max_order
        ),
        generator_metrics=None,
        search_note=(
            "Each half independently found a characteristic rotational axis and coarse-to-fine "
            "twist. Complete oriented operator sets, rather than arbitrarily chosen equivalent axes, "
            "were compared and reciprocally scored before reporting."
        ),
        half_map_consistent=reciprocal_consistent,
    )
    candidate["characteristic_axis_order"] = characteristic_order
    candidate["canonical_characteristic_axis_xyz"] = canonical_axis.tolist()
    candidate["half1_axis_xyz"] = half1_axis.tolist()
    candidate["half2_axis_xyz"] = half2_axis.tolist()
    candidate["axis_agreement_deg"] = axis_agreement
    candidate["axis_agreement_is_gating"] = False
    candidate["operator_set_agreement_deg"] = operator_set_agreement
    candidate["operator_set_agreement_threshold_deg"] = (
        _MAX_OPERATOR_SET_DISAGREEMENT_DEG
    )
    candidate["selected_orientation_source"] = selected_orientation_source
    candidate["reciprocal_complete_group_metrics"] = {
        "half1_optimized_orientation": _replication_metrics_record(
            half1_group_metrics
        ),
        "half2_optimized_orientation": _replication_metrics_record(
            half2_group_metrics
        ),
    }
    candidate["twist_search_scores"] = {
        "half1": half1_twist_scores,
        "half2": half2_twist_scores,
    }
    candidate["twist_peak_z_vs_sampled_twists"] = twist_peak_z
    return candidate


def _spectrum_record(spectrum: _Spectrum) -> dict[str, Any]:
    index = spectrum.consensus_index
    return {
        "order": spectrum.order,
        "identifiable": spectrum.identifiable,
        "consensus_axis_xyz": spectrum.axes_xyz[index].tolist(),
        "half1_best_axis_xyz": spectrum.axes_xyz[spectrum.half1_index].tolist(),
        "half2_best_axis_xyz": spectrum.axes_xyz[spectrum.half2_index].tolist(),
        "axis_agreement_deg": spectrum.axis_agreement_deg,
        "evidence_z_vs_axis_null": spectrum.evidence_z,
        "half1_evidence_z_vs_axis_null": spectrum.half1_evidence_z,
        "half2_evidence_z_vs_axis_null": spectrum.half2_evidence_z,
        "half1_generator_correlation": float(spectrum.half1[index]),
        "half2_generator_correlation": float(spectrum.half2[index]),
        "cross_half_generator_correlation": float(spectrum.cross[index]),
        "cross_half_generator_retention": float(spectrum.cross_retention[index]),
    }


def _high_order_small_angle_diagnostic(
    spectra: dict[int, _Spectrum],
) -> dict[str, Any]:
    """Flag a conservative order ramp caused by ever-smaller rotations."""
    orders = sorted(
        order for order, spectrum in spectra.items() if spectrum.identifiable
    )
    values = np.asarray(
        [
            min(
                float(spectra[order].half1[spectra[order].consensus_index]),
                float(spectra[order].half2[spectra[order].consensus_index]),
                float(
                    spectra[order].cross_retention[
                        spectra[order].consensus_index
                    ]
                ),
            )
            for order in orders
        ],
        dtype=np.float64,
    )
    # C2 and C3 frequently contain genuine low-order symmetry and can dominate
    # an otherwise smooth high-order ramp.  Diagnose the small-angle regime
    # from C4 upward while retaining the complete spectrum in the report.
    analysis_indices = [index for index, order in enumerate(orders) if order >= 4]
    analysis_orders = [orders[index] for index in analysis_indices]
    analysis_values = values[analysis_indices]
    assessable = len(analysis_orders) >= 6

    rho: float | None = None
    nondecreasing_fraction = 0.0
    endpoint_delta = 0.0
    support_range = 0.0
    minimum_support = 0.0
    if len(analysis_orders) >= 2:
        value_order = np.argsort(analysis_values, kind="stable")
        ranks = np.empty(len(analysis_values), dtype=np.float64)
        ranks[value_order] = np.arange(len(analysis_values), dtype=np.float64)
        correlation = np.corrcoef(
            np.arange(len(analysis_values), dtype=np.float64),
            ranks,
        )[0, 1]
        rho = float(correlation) if math.isfinite(float(correlation)) else 0.0
        differences = np.diff(analysis_values)
        nondecreasing_fraction = float(np.mean(differences >= -0.01))
        endpoint_delta = float(analysis_values[-1] - analysis_values[0])
        support_range = float(np.ptp(analysis_values))
        minimum_support = float(np.min(analysis_values))
    monotonic = bool(
        assessable
        and rho is not None
        and rho >= 0.90
        and nondecreasing_fraction >= 0.80
        and endpoint_delta >= 0.03
        and float(analysis_values[-1]) >= 0.60
    )
    high_flat_plateau = bool(
        assessable
        and minimum_support >= 0.85
        and support_range <= 0.05
    )
    small_angle_confounded = bool(monotonic or high_flat_plateau)
    return {
        "method": "high_order_spearman_order_vs_consensus_generator_support",
        "orders": orders,
        "consensus_generator_support": values.tolist(),
        "ramp_analysis_orders": analysis_orders,
        "ramp_analysis_support": analysis_values.tolist(),
        "assessable": assessable,
        "assessment_status": "assessed" if assessable else "insufficient_order_coverage",
        "minimum_orders_required": 6,
        "spearman_rho": rho,
        "nondecreasing_step_fraction": nondecreasing_fraction,
        "endpoint_delta": endpoint_delta,
        "support_range": support_range,
        "minimum_support": minimum_support,
        "monotonic_small_angle_ramp": monotonic,
        "high_flat_support_plateau": high_flat_plateau,
        "small_angle_confounded": small_angle_confounded,
    }


def _sentinel_supported(spectrum: _Spectrum, prepared: _PreparedMaps) -> bool:
    return bool(
        spectrum.identifiable
        and prepared.identity_half_correlation >= _MIN_IDENTITY_HALF_CORRELATION
        and min(spectrum.half1_evidence_z, spectrum.half2_evidence_z)
        >= _MIN_AXIS_NULL_Z
        and spectrum.axis_agreement_deg <= 35.0
        and spectrum.half1[spectrum.half1_index] >= _MIN_ROTATIONAL_CORRELATION
        and spectrum.half2[spectrum.half2_index] >= _MIN_ROTATIONAL_CORRELATION
        and spectrum.cross_retention[spectrum.half1_index]
        >= _MIN_CROSS_HALF_RETENTION
        and spectrum.cross_retention[spectrum.half2_index]
        >= _MIN_CROSS_HALF_RETENTION
    )


def _polyhedral_screen_supported(
    spectrum: _Spectrum,
    prepared: _PreparedMaps,
) -> bool:
    return bool(
        prepared.identity_half_correlation >= _MIN_IDENTITY_HALF_CORRELATION
        and min(spectrum.half1_evidence_z, spectrum.half2_evidence_z)
        >= _MIN_AXIS_NULL_Z
        and spectrum.half1[spectrum.half1_index]
        >= _MIN_POLYHEDRAL_SCREEN_CORRELATION
        and spectrum.half2[spectrum.half2_index]
        >= _MIN_POLYHEDRAL_SCREEN_CORRELATION
    )


def _select_stage2_sentinel_anchors(
    supported_sentinels: Sequence[_Spectrum],
    spectra: dict[int, _Spectrum],
    prepared: _PreparedMaps,
) -> tuple[list[_Spectrum], list[dict[str, Any]]]:
    """Select at most two Stage-2 anchors without losing composite harmonics."""
    if not supported_sentinels:
        return [], []

    supported_orders = {
        order
        for order, spectrum in spectra.items()
        if _sentinel_supported(spectrum, prepared)
    }

    def proper_supported_divisors(spectrum: _Spectrum) -> list[int]:
        return sorted(
            candidate
            for candidate in supported_orders
            if (
                2 <= candidate < spectrum.order
                and spectrum.order % candidate == 0
                and axis_angle_degrees(
                    spectra[candidate].axes_xyz[spectra[candidate].half1_index],
                    spectrum.axes_xyz[spectrum.half1_index],
                )
                <= 35.0
                and axis_angle_degrees(
                    spectra[candidate].axes_xyz[spectra[candidate].half2_index],
                    spectrum.axes_xyz[spectrum.half2_index],
                )
                <= 35.0
            )
        )

    evidence_anchor = supported_sentinels[0]
    anchors = [evidence_anchor]
    records = [
        {
            "order": evidence_anchor.order,
            "selection_reason": "evidence_peak",
            "supported_divisors": proper_supported_divisors(evidence_anchor),
        }
    ]

    harmonic_options: list[tuple[tuple[float, ...], _Spectrum, list[int]]] = []
    for spectrum in supported_sentinels[1:]:
        divisors = proper_supported_divisors(spectrum)
        if not divisors:
            continue
        divisor_strength = sum(
            min(
                min(
                    spectra[order].half1_evidence_z,
                    spectra[order].half2_evidence_z,
                ),
                6.0,
            )
            for order in divisors
        ) / math.sqrt(len(divisors))
        lcm_closure = len(divisors) >= 2 and math.lcm(*divisors) == spectrum.order
        if not lcm_closure:
            continue
        key = (
            float(len(divisors)),
            float(divisor_strength),
            float(spectrum.order),
            float(spectrum.evidence_z),
        )
        harmonic_options.append((key, spectrum, divisors))

    if harmonic_options:
        _, harmonic_anchor, divisors = max(harmonic_options, key=lambda item: item[0])
        anchors.append(harmonic_anchor)
        records.append(
            {
                "order": harmonic_anchor.order,
                "selection_reason": "harmonic_closure",
                "supported_divisors": divisors,
            }
        )
    elif len(supported_sentinels) > 1:
        secondary_evidence_anchor = supported_sentinels[1]
        anchors.append(secondary_evidence_anchor)
        records.append(
            {
                "order": secondary_evidence_anchor.order,
                "selection_reason": "evidence_peak",
                "supported_divisors": proper_supported_divisors(
                    secondary_evidence_anchor
                ),
            }
        )

    return anchors, records


def _related_supported(first: dict[str, Any], second: dict[str, Any]) -> bool:
    return (
        first["group"] in second.get("subgroups", [])
        or second["group"] in first.get("subgroups", [])
    )


def _normalize_focus_groups(
    focus_groups: Sequence[str] | None,
    maximum_order: int,
) -> tuple[str, ...]:
    if focus_groups is None:
        return ()
    if isinstance(focus_groups, (str, bytes)):
        raise SymmetryScanError("focus_groups must be a sequence of point-group names.")
    normalized: list[str] = []
    for raw_group in focus_groups:
        if not isinstance(raw_group, str) or not raw_group.strip():
            raise SymmetryScanError("focus_groups must contain non-empty strings.")
        group = raw_group.strip().upper()
        match = re.fullmatch(r"([CD])(\d+)", group)
        if match:
            family = match.group(1)
            order = int(match.group(2))
            if order < 2 or order > maximum_order:
                raise SymmetryScanError(
                    f"Focused group {group} must have order between 2 and max_order."
                )
            group = f"{family}{order}"
        elif group not in {"T", "O", "I"}:
            raise SymmetryScanError(
                f"Unsupported focused point group {raw_group!r}; expected Cn, Dn, T, O, or I."
            )
        if group not in normalized:
            normalized.append(group)
        if len(normalized) > 8:
            raise SymmetryScanError("focus_groups may contain at most eight unique groups.")
    return tuple(normalized)


def scan_symmetry(
    half_map_1: np.ndarray,
    half_map_2: np.ndarray,
    *,
    voxel_size_A: float,
    analysis_resolution_A: float,
    mask: np.ndarray | None = None,
    common_max_order: int = 8,
    max_order: int = 32,
    stage2_window: int = 2,
    max_candidates: int = 5,
    focus_groups: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Return a bounded exploratory point-group shortlist from a C1 half-map pair.

    The common scan evaluates Cn and Dn hypotheses for orders two through
    ``common_max_order`` plus T, O, and I.  A generator-only high-order
    sentinel surveys every remaining cyclic order; reproducible peaks open a
    narrow complete Cn/Dn Stage 2 scan.  Every reported candidate is evaluated
    with all non-identity group operations.
    """
    voxel = _finite_positive_float(voxel_size_A, "voxel_size_A")
    resolution = _finite_positive_float(analysis_resolution_A, "analysis_resolution_A")
    if resolution < 2.0 * voxel and not math.isclose(resolution, 2.0 * voxel, rel_tol=1e-7):
        raise SymmetryScanError(
            f"analysis_resolution_A={resolution:g} is beyond the isotropic Nyquist limit "
            f"of {2.0 * voxel:g} Angstrom."
        )
    common = _bounded_integer(common_max_order, "common_max_order", 2, 16)
    maximum = _bounded_integer(max_order, "max_order", 2, 64)
    if maximum < common:
        raise SymmetryScanError("max_order must be greater than or equal to common_max_order.")
    # A zero-width follow-up can select a neighboring harmonic while silently
    # omitting the supported prime order it represents (for example C29 next
    # to a C30 sentinel).  Keep at least one order on either side of an anchor.
    window = _bounded_integer(stage2_window, "stage2_window", 1, 8)
    candidate_limit = _bounded_integer(max_candidates, "max_candidates", 1, 8)
    focused = _normalize_focus_groups(focus_groups, maximum)
    focused_set = set(focused)

    prepared = _prepare_maps(
        half_map_1,
        half_map_2,
        mask,
        voxel_size_A=voxel,
        analysis_resolution_A=resolution,
        max_order=max(maximum, 5),
    )
    axes = _fibonacci_axes()
    spectra: dict[int, _Spectrum] = {
        order: _spectrum_for_order(prepared, order, axes)
        for order in range(2, max(maximum, 5) + 1)
    }
    high_order_diagnostic = _high_order_small_angle_diagnostic(spectra)

    candidates: list[dict[str, Any]] = []
    stage1_orders = list(range(2, common + 1))
    for order in stage1_orders:
        spectrum = spectra[order]
        candidates.append(_cyclic_candidate(prepared, spectrum, stage=1))
        candidates.append(_dihedral_candidate(prepared, spectrum, stage=1))
    polyhedral_groups_evaluated: list[str] = []
    polyhedral_groups_screened_out: list[str] = []
    for group in ("T", "O", "I"):
        characteristic_order = {"T": 3, "O": 4, "I": 5}[group]
        characteristic_spectrum = spectra[characteristic_order]
        if (
            _polyhedral_screen_supported(characteristic_spectrum, prepared)
            or group in focused_set
        ):
            candidates.append(
                _polyhedral_candidate(
                    prepared,
                    group,
                    characteristic_spectrum,
                )
            )
            polyhedral_groups_evaluated.append(group)
        else:
            polyhedral_groups_screened_out.append(group)

    sentinel_orders = list(range(common + 1, maximum + 1))
    supported_sentinels = [
        spectra[order]
        for order in sentinel_orders
        if _sentinel_supported(spectra[order], prepared)
    ]
    supported_sentinels.sort(
        key=lambda spectrum: (
            spectrum.evidence_z,
            spectrum.cross_retention[spectrum.consensus_index],
        ),
        reverse=True,
    )
    stage2_anchors, stage2_anchor_records = _select_stage2_sentinel_anchors(
        supported_sentinels,
        spectra,
        prepared,
    )
    stage2_orders_set: set[int] = set()
    for peak in stage2_anchors:
        stage2_orders_set.update(
            range(
                max(common + 1, peak.order - window),
                min(maximum, peak.order + window) + 1,
            )
        )
    focused_stage2_orders_set: set[int] = set()
    for group in focused:
        focus_match = re.fullmatch(r"[CD](\d+)", group)
        if focus_match is None:
            continue
        focus_order = int(focus_match.group(1))
        if focus_order > common:
            focused_stage2_orders_set.add(focus_order)
    focused_stage2_orders = sorted(focused_stage2_orders_set)
    stage2_orders_set.update(focused_stage2_orders)
    stage2_orders = sorted(stage2_orders_set)
    for order in stage2_orders:
        spectrum = spectra[order]
        candidates.append(_cyclic_candidate(prepared, spectrum, stage=2))
        candidates.append(_dihedral_candidate(prepared, spectrum, stage=2))

    preprocessing_valid = bool(
        prepared.center_stable and prepared.requested_cutoff_honored
    )
    high_order_control_assessable = bool(high_order_diagnostic["assessable"])
    monotonic_small_angle_ramp = bool(
        high_order_diagnostic["monotonic_small_angle_ramp"]
    )
    high_flat_support_plateau = bool(
        high_order_diagnostic["high_flat_support_plateau"]
    )
    small_angle_confounded = bool(
        high_order_diagnostic["small_angle_confounded"]
    )
    high_order_control_required = maximum >= 4
    negative_inference_valid = bool(
        preprocessing_valid
        and (
            not high_order_control_required
            or (
                high_order_control_assessable
                and not small_angle_confounded
            )
        )
    )
    for candidate in candidates:
        raw_passed = bool(candidate["half_map_consistent"])
        candidate["raw_scan_heuristics_passed"] = raw_passed
        candidate["negative_inference_valid"] = negative_inference_valid
        candidate["high_order_ramp_caveat"] = monotonic_small_angle_ramp
        candidate["high_order_plateau_caveat"] = high_flat_support_plateau
        support_confounds: list[str] = []
        finite_rotational_order = (
            candidate["family"] in {"C", "D"} and int(candidate["order"]) >= 4
        )
        if raw_passed and finite_rotational_order:
            if not high_order_control_assessable:
                support_confounds.append("high_order_control_not_assessable")
            elif monotonic_small_angle_ramp:
                support_confounds.append(
                    "finite_order_confounded_by_high_order_small_angle_ramp"
                )
            elif high_flat_support_plateau:
                support_confounds.append(
                    "finite_order_confounded_by_high_flat_support_plateau"
                )
        candidate["support_confounds"] = support_confounds
        candidate["order_specific_support_valid"] = bool(
            raw_passed and not support_confounds
        )
        if support_confounds:
            candidate["support_status"] = "inconclusive"

    # Rank raw reproducible hypotheses so confounded or deliberately focused
    # groups remain inspectable.  Keep the resolved-support collection separate
    # so neither counts nor per-group records call those hypotheses supported.
    rankable = [
        candidate
        for candidate in candidates
        if candidate["raw_scan_heuristics_passed"]
    ]
    rankable.sort(key=lambda item: float(item["rank_score"]), reverse=True)
    supported = [
        candidate
        for candidate in rankable
        if candidate["order_specific_support_valid"]
    ]
    for rank, candidate in enumerate(rankable, start=1):
        candidate["rank"] = rank
    shortlist = rankable[:candidate_limit]
    for group in focused:
        focused_candidate = next(
            (candidate for candidate in rankable if candidate["group"] == group),
            None,
        )
        if focused_candidate is None or focused_candidate in shortlist:
            continue
        replaceable = [
            candidate
            for candidate in reversed(shortlist)
            if candidate["group"] not in focused_set
        ]
        if replaceable:
            shortlist[shortlist.index(replaceable[0])] = focused_candidate
        else:
            # Multiple focused passing groups may intentionally expand the
            # presentation beyond max_candidates; the user explicitly asked
            # to retain each of them for side-by-side adjudication.
            shortlist.append(focused_candidate)
    shortlist.sort(key=lambda item: int(item["rank"]))

    rank_gap: float | None = None
    nested_leaders: bool | None = None
    if not rankable:
        if (
            prepared.identity_half_correlation < _MIN_IDENTITY_HALF_CORRELATION
            or prepared.identifiability_max_order < 2
            or not negative_inference_valid
        ):
            status = "untestable"
        else:
            status = "no_supported_candidate"
    elif not supported:
        status = "ambiguous"
    elif len(rankable) == 1:
        status = "shortlist" if negative_inference_valid else "ambiguous"
    else:
        rank_gap = float(rankable[0]["rank_score"]) - float(
            rankable[1]["rank_score"]
        )
        nested_leaders = _related_supported(rankable[0], rankable[1])
        has_confounded_hypotheses = len(rankable) > len(supported)
        status = (
            "ambiguous"
            if has_confounded_hypotheses
            or rank_gap < 0.08
            or bool(nested_leaders)
            or not negative_inference_valid
            else "shortlist"
        )

    warnings: list[str] = []
    if not prepared.requested_cutoff_honored:
        warnings.append(
            "The bounded analysis grid could not honor the requested cutoff; negative symmetry inference is inconclusive."
        )
    if not prepared.center_stable:
        warnings.append(
            "Independent half-map center estimates were unstable or too near an array edge; negative symmetry inference is inconclusive."
        )
    if high_order_control_required and not high_order_control_assessable:
        warnings.append(
            "Too few identifiable C4-and-higher orders were scanned to test for smooth-density small-angle self-similarity; finite Cn/Dn support and negative inference are inconclusive."
        )
    if monotonic_small_angle_ramp:
        warnings.append(
            "Generator support rises monotonically toward smaller rotations; finite high-order symmetry is not distinguished from smooth-blob self-similarity."
        )
    if high_flat_support_plateau:
        warnings.append(
            "Generator support remains uniformly high across C4-and-higher orders; finite rotational order is not distinguished from continuous axial self-similarity."
        )
    if mask is not None:
        warnings.append(
            "Mask-dependent periodicity is possible; repeat without the supplied mask or with a nearby loose mask."
        )
    if len(rankable) > len(shortlist):
        warnings.append(
            "The detailed raw heuristic-pass list was truncated by max_candidates; ambiguity was assessed before truncation."
        )
    if status in {"ambiguous", "inconclusive", "no_supported_candidate", "untestable"}:
        warnings.append("Default to C1 unless independent matched-branch evidence resolves the ambiguity.")
    if any(candidate["group"] == "I" for candidate in shortlist):
        warnings.append(
            "I1-I4 are coordinate conventions of one abstract icosahedral group and were not ranked separately."
        )

    candidate_by_group = {candidate["group"]: candidate for candidate in candidates}
    displayed_groups = {candidate["group"] for candidate in shortlist}

    def complete_hypothesis_record(candidate: dict[str, Any]) -> dict[str, Any]:
        group = str(candidate["group"])
        raw_passed = bool(candidate["raw_scan_heuristics_passed"])
        supported_by_scan = bool(candidate["order_specific_support_valid"])
        if supported_by_scan:
            scan_outcome = "pass"
        elif raw_passed and candidate["support_confounds"]:
            scan_outcome = "confounded"
        elif candidate["support_status"] in {"inconclusive", "untestable"}:
            scan_outcome = "untestable"
        else:
            scan_outcome = "fail"
        record: dict[str, Any] = {
            "group": group,
            "coverage": "complete_group_evaluated",
            "evaluation_level": "complete_group",
            "evaluated": True,
            "complete_group_evaluated": True,
            "focus_requested": group in focused_set,
            "scan_heuristic_outcome": scan_outcome,
            "support_status": candidate["support_status"],
            "supported": supported_by_scan,
            "raw_scan_heuristics_passed": raw_passed,
            "order_specific_support_valid": candidate[
                "order_specific_support_valid"
            ],
            "support_confounds": list(candidate["support_confounds"]),
            "support_failures": list(candidate["support_failures"]),
            "gate_results": dict(candidate["gate_results"]),
            "identifiable": bool(candidate["identifiable"]),
            "stage": candidate["stage"],
            "rank": candidate.get("rank"),
            "rank_score": candidate["rank_score"],
            "detail_state": (
                "displayed"
                if group in displayed_groups
                else "omitted_by_display_cap"
                if raw_passed
                else "compact_only"
            ),
            "half1_score": candidate["half1_score"],
            "half2_score": candidate["half2_score"],
            "cross_half_score": candidate["cross_half_score"],
            "agreement_score": candidate["agreement_score"],
            "axis_xyz": candidate["axis_xyz"],
            "symmetry_center_zyx": candidate["symmetry_center_zyx"],
            "high_order_ramp_caveat": monotonic_small_angle_ramp,
            "high_order_plateau_caveat": high_flat_support_plateau,
        }
        if group in focused_set:
            record["focused_detail"] = candidate
        return record

    hypothesis_results: dict[str, dict[str, Any]] = {}
    generator_screen_only_groups: list[str] = []
    for order in range(2, maximum + 1):
        spectrum = spectra[order]
        spectrum_index = spectrum.consensus_index
        for family in ("C", "D"):
            group = f"{family}{order}"
            candidate = candidate_by_group.get(group)
            if candidate is not None:
                hypothesis_results[group] = complete_hypothesis_record(candidate)
                continue
            generator_screen_only_groups.append(group)
            screen_passed = _sentinel_supported(spectrum, prepared)
            hypothesis_results[group] = {
                "group": group,
                "coverage": "generator_screen_only",
                "evaluation_level": "generator_screen_only",
                "evaluated": True,
                "complete_group_evaluated": False,
                "focus_requested": group in focused_set,
                "scan_heuristic_outcome": "not_evaluated",
                "support_status": (
                    "screen_passed_not_complete_scored"
                    if screen_passed
                    else "screened_out"
                ),
                "supported": False,
                "raw_scan_heuristics_passed": False,
                "order_specific_support_valid": False,
                "support_confounds": [],
                "support_failures": [
                    "complete_group_not_evaluated_by_bounded_stage2"
                    if screen_passed
                    else "generator_screen_failed"
                ],
                "identifiable": bool(spectrum.identifiable),
                "stage": "sentinel",
                "rank": None,
                "rank_score": None,
                "detail_state": "not_applicable",
                "half1_score": float(spectrum.half1[spectrum_index]),
                "half2_score": float(spectrum.half2[spectrum_index]),
                "cross_half_score": float(
                    spectrum.cross_retention[spectrum_index]
                ),
                "agreement_score": None,
                "axis_xyz": spectrum.axes_xyz[spectrum_index].tolist(),
                "symmetry_center_zyx": _prepared_rotation_center(prepared).tolist(),
                "high_order_ramp_caveat": monotonic_small_angle_ramp,
                "high_order_plateau_caveat": high_flat_support_plateau,
            }
    for group in ("T", "O", "I"):
        candidate = candidate_by_group.get(group)
        if candidate is not None:
            hypothesis_results[group] = complete_hypothesis_record(candidate)
        else:
            characteristic_order = {"T": 3, "O": 4, "I": 5}[group]
            spectrum = spectra[characteristic_order]
            index = spectrum.consensus_index
            hypothesis_results[group] = {
                "group": group,
                "coverage": "characteristic_axis_screen_only",
                "evaluation_level": "characteristic_axis_screen_only",
                "evaluated": True,
                "complete_group_evaluated": False,
                "focus_requested": group in focused_set,
                "scan_heuristic_outcome": "not_evaluated",
                "support_status": "screened_out",
                "supported": False,
                "raw_scan_heuristics_passed": False,
                "order_specific_support_valid": False,
                "support_confounds": [],
                "support_failures": ["polyhedral_characteristic_axis_screen_failed"],
                "identifiable": bool(spectrum.identifiable),
                "stage": "polyhedral_screen",
                "rank": None,
                "rank_score": None,
                "detail_state": "not_applicable",
                "half1_score": float(spectrum.half1[index]),
                "half2_score": float(spectrum.half2[index]),
                "cross_half_score": float(spectrum.cross_retention[index]),
                "agreement_score": None,
                "axis_xyz": spectrum.axes_xyz[index].tolist(),
                "symmetry_center_zyx": _prepared_rotation_center(prepared).tolist(),
                "high_order_ramp_caveat": monotonic_small_angle_ramp,
                "high_order_plateau_caveat": high_flat_support_plateau,
            }

    complete_group_evaluated_groups = [
        group
        for group, result in hypothesis_results.items()
        if result["complete_group_evaluated"]
    ]
    omitted_supported_groups = [
        candidate["group"]
        for candidate in supported
        if candidate["group"] not in displayed_groups
    ]
    omitted_raw_passing_groups = [
        candidate["group"]
        for candidate in rankable
        if candidate["group"] not in displayed_groups
    ]
    displayed_supported_groups = [
        candidate["group"]
        for candidate in shortlist
        if candidate["order_specific_support_valid"]
    ]
    result_completeness = {
        "all_stage1_groups_reported": True,
        "hypothesis_results_complete": True,
        "evaluated_groups": list(hypothesis_results),
        "complete_group_evaluated_groups": complete_group_evaluated_groups,
        "generator_screen_only_groups": generator_screen_only_groups,
        "displayed_candidate_limit": candidate_limit,
        "supported_candidate_count": len(supported),
        "displayed_supported_candidate_count": len(displayed_supported_groups),
        "displayed_supported_groups": displayed_supported_groups,
        "raw_scan_heuristic_pass_count": len(rankable),
        "displayed_raw_scan_heuristic_pass_count": len(shortlist),
        "displayed_candidate_count": len(shortlist),
        "focus_display_override_count": max(0, len(shortlist) - candidate_limit),
        "display_truncated": len(rankable) > len(shortlist),
        "omitted_supported_count": len(omitted_supported_groups),
        "omitted_supported_groups": omitted_supported_groups,
        "omitted_raw_scan_heuristic_pass_count": len(omitted_raw_passing_groups),
        "omitted_raw_scan_heuristic_pass_groups": omitted_raw_passing_groups,
        "focus_groups_all_complete_scored": all(
            group in complete_group_evaluated_groups for group in focused
        ),
    }
    confounded = [
        candidate for candidate in rankable if candidate["support_confounds"]
    ]
    if confounded and supported:
        hypothesis_outcome = "supported_and_confounded_scan_hypotheses"
    elif confounded:
        hypothesis_outcome = "raw_scan_passes_confounded"
    elif not supported:
        hypothesis_outcome = "none_passed_scan_heuristics"
    elif len(supported) == 1:
        hypothesis_outcome = "one_passed_scan_heuristics"
    else:
        hypothesis_outcome = "multiple_passed_scan_heuristics"
    if status == "ambiguous":
        ranking_outcome = "ambiguous"
    elif status == "shortlist":
        ranking_outcome = "separated"
    else:
        ranking_outcome = "not_applicable"
    if (
        prepared.identity_half_correlation < _MIN_IDENTITY_HALF_CORRELATION
        or prepared.identifiability_max_order < 2
        or not prepared.center_stable
    ):
        input_validity = "untestable"
    elif not prepared.requested_cutoff_honored:
        input_validity = "limited"
    else:
        input_validity = "valid"
    scan_summary = {
        "input_validity": input_validity,
        "hypothesis_outcome": hypothesis_outcome,
        "ranking_outcome": ranking_outcome,
        "inference_valid": preprocessing_valid,
        "negative_inference_valid": negative_inference_valid,
        "negative_inference_scope": "scan_heuristics_only",
        "center_stable": prepared.center_stable,
        "requested_cutoff_honored": prepared.requested_cutoff_honored,
        "high_order_control_assessable": high_order_control_assessable,
        "high_order_monotonic_small_angle_ramp": monotonic_small_angle_ramp,
        "high_order_flat_support_plateau": high_flat_support_plateau,
        "high_order_small_angle_confounded": small_angle_confounded,
        "evaluated_group_count": len(hypothesis_results),
        "complete_group_evaluated_count": len(complete_group_evaluated_groups),
        "supported_group_count": len(supported),
        "raw_scan_heuristic_pass_count": len(rankable),
        "confounded_group_count": len(confounded),
        "displayed_candidate_count": len(shortlist),
        "focus_groups": list(focused),
        "biological_conclusion": "unresolved",
        "processing_baseline": "C1",
        "permits_symmetry_acceptance": False,
        "permits_symmetry_rejection": False,
        "candidate_absence_is_failure": False,
        "required_confirmation": "matched_C1_and_candidate_refinement_branches",
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "status": status,
        "decision": "defer",
        "human_input_required": False,
        "report_only": True,
        "selected_candidate": None,
        "candidates": shortlist,
        "scan_summary": scan_summary,
        "result_completeness": result_completeness,
        "hypothesis_results": hypothesis_results,
        "preprocessing": {
            "requested_analysis_lowpass_A": prepared.requested_resolution_A,
            "effective_analysis_lowpass_A": prepared.effective_resolution_A,
            "requested_cutoff_honored": prepared.requested_cutoff_honored,
            "effective_to_requested_resolution_ratio": float(
                prepared.effective_resolution_A / prepared.requested_resolution_A
            ),
            "inference_valid": preprocessing_valid,
            "lowpass_type": "radial_fourier_raised_cosine",
            "lowpass_before_decimation": True,
            "analysis_stride": prepared.stride,
            "analysis_shape_zyx": list(prepared.half1.shape),
            "analysis_voxel_size_A": prepared.voxel_size_A,
            "center_preserving_resampling": True,
            "lowpass_transition_bounded_by_analysis_nyquist": True,
            "mask_antialias_before_resampling": mask is not None,
            "spherical_cosine_window": True,
            "radial_profile_subtracted": True,
            "optional_mask_applied": mask is not None,
            "sampled_voxels": int(prepared.coordinates_xyz.shape[0]),
            "symmetry_center": "independent_half_robust_signal_center",
            "center_estimation": {
                "method": "independent_half_robust_signed_density_centroid",
                "chosen_center_zyx": _prepared_rotation_center(prepared).tolist(),
                "joint_center_zyx": (
                    None
                    if prepared.joint_center_zyx is None
                    else np.asarray(prepared.joint_center_zyx).tolist()
                ),
                "geometric_center_zyx": (
                    None
                    if prepared.geometric_center_zyx is None
                    else np.asarray(prepared.geometric_center_zyx).tolist()
                ),
                "half1_center_zyx": (
                    None
                    if prepared.half1_center_zyx is None
                    else np.asarray(prepared.half1_center_zyx).tolist()
                ),
                "half2_center_zyx": (
                    None
                    if prepared.half2_center_zyx is None
                    else np.asarray(prepared.half2_center_zyx).tolist()
                ),
                "shift_from_geometric_center_zyx_pix": (
                    _prepared_rotation_center(prepared)
                    - np.asarray(prepared.geometric_center_zyx, dtype=np.float64)
                ).tolist(),
                "shift_from_geometric_center_pix": float(
                    np.linalg.norm(
                        _prepared_rotation_center(prepared)
                        - np.asarray(prepared.geometric_center_zyx, dtype=np.float64)
                    )
                ),
                "shift_from_geometric_center_A": float(
                    np.linalg.norm(
                        _prepared_rotation_center(prepared)
                        - np.asarray(prepared.geometric_center_zyx, dtype=np.float64)
                    )
                    * prepared.voxel_size_A
                ),
                "half_map_center_disagreement_pix": float(
                    prepared.center_disagreement_A / prepared.voxel_size_A
                ),
                "half_map_center_disagreement_A": prepared.center_disagreement_A,
                "maximum_stable_disagreement_A": (
                    prepared.center_disagreement_threshold_A
                ),
                "edge_clearance_pix": float(
                    prepared.center_edge_clearance_A / prepared.voxel_size_A
                ),
                "edge_clearance_A": prepared.center_edge_clearance_A,
                "stable": prepared.center_stable,
                "cyclic_axis_line_note": (
                    "Only transverse axis-line position is identifiable for Cn; translations along the axis are equivalent."
                ),
            },
        },
        "identifiability": {
            "particle_radius_A_95pct_energy": prepared.particle_radius_A,
            "criterion": "2 R sin(pi/n) >= effective_analysis_resolution_A",
            "max_testable_order": prepared.identifiability_max_order,
            "untestable_orders": [
                order
                for order in range(2, maximum + 1)
                if order > prepared.identifiability_max_order
            ],
        },
        "half_map_validation": {
            "identity_correlation_on_sampled_residual": prepared.identity_half_correlation,
            "minimum_identity_correlation": _MIN_IDENTITY_HALF_CORRELATION,
        },
        "search": {
            "axis_sampling": "deterministic_projective_fibonacci_plus_local_refinement",
            "coarse_axis_count": len(axes),
            "stage1_orders": stage1_orders,
            "stage1_groups": [
                *[f"C{order}" for order in stage1_orders],
                *[f"D{order}" for order in stage1_orders],
                "T",
                "O",
                "I",
            ],
            "sentinel_orders": sentinel_orders,
            "sentinel_kind": "cyclic_generator_only_search_trigger",
            "sentinel_supported_peaks": [item.order for item in supported_sentinels],
            "sentinel_stage2_anchors": stage2_anchor_records,
            "sentinel_stage2_anchor_limit": 2,
            "stage2_triggered": bool(stage2_orders),
            "stage2_window": window,
            "stage2_orders": stage2_orders,
            "focus_groups": list(focused),
            "focus_forced_stage2_orders": focused_stage2_orders,
            "stage2_groups": [
                group
                for order in stage2_orders
                for group in (f"C{order}", f"D{order}")
            ],
            "order_spectrum": [_spectrum_record(spectra[order]) for order in spectra],
            "high_order_diagnostic": high_order_diagnostic,
            "complete_group_scoring_for_reported_candidates": True,
            "supported_candidate_count_before_cap": len(supported),
            "raw_scan_heuristic_pass_count_before_cap": len(rankable),
            "candidate_cap_truncated": len(rankable) > len(shortlist),
            "ambiguity_top_two_groups": [
                candidate["group"] for candidate in rankable[:2]
            ],
            "ambiguity_top_two_rank_gap": rank_gap,
            "ambiguity_top_two_are_nested": nested_leaders,
            "polyhedral_orientation_search": (
                "independent characteristic-axis discovery plus coarse-to-fine twist; "
                "complete oriented operator-set equivalence"
            ),
            "polyhedral_groups_complete_scored": polyhedral_groups_evaluated,
            "polyhedral_groups_screened_out": polyhedral_groups_screened_out,
        },
        "support_heuristics": {
            "empirical_axis_null": True,
            "minimum_axis_null_z": _MIN_AXIS_NULL_Z,
            "maximum_unique_axis_disagreement_deg": 35.0,
            "unique_axis_gate_applies_to": "Cn and Dn for n > 2",
            "maximum_operator_set_disagreement_deg": (
                _MAX_OPERATOR_SET_DISAGREEMENT_DEG
            ),
            "operator_set_agreement_definition": (
                "symmetric Hausdorff rotation-geodesic distance between complete "
                "conjugated non-identity operator sets, matched by rotation angle"
            ),
            "minimum_rotational_correlation": _MIN_ROTATIONAL_CORRELATION,
            "minimum_cross_half_retention": _MIN_CROSS_HALF_RETENTION,
            "note": (
                "These bounded support heuristics identify reproducible hypotheses; they do not "
                "accept biological symmetry."
            ),
        },
        "warnings": warnings,
        "limitations": [
            "The common center is estimated from bounded density evidence; Cn axis-line refinement searches only the identifiable transverse offset.",
            "Polyhedral orientation uses characteristic-axis discovery and a bounded coarse-to-fine twist, not continuous global SO(3) optimization.",
            "The scan cannot establish stoichiometry, preserve asymmetric ligands, or choose a RELION convention.",
            "No phase-randomized control is generated; matched gold-standard refinements remain mandatory.",
        ],
        "next_step": (
            "Review the shortlist and axis/convention, then obtain explicit authorization for matched C1 and "
            "candidate-symmetry Refine3D/PostProcess branches. Compare the aligned branches for lost density, "
            "smearing, split or distorted helices, weakened ligand density, apparent partial occupancy, and "
            "collapsed conformations before accepting or rejecting the candidate symmetry."
        ),
    }
