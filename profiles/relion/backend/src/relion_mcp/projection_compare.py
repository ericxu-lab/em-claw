from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import ndimage

SCHEMA_VERSION = "relion.map-projection-comparison/v1"
MAX_CLASSES = 12
MIN_DIRECTION_COUNT = 24
MAX_DIRECTION_COUNT = 512
MIN_INPLANE_STEP_DEG = 5.0
MAX_INPLANE_STEP_DEG = 45.0
MAX_WORK_BOX = 64


class ProjectionComparisonError(ValueError):
    """Raised when a bounded map-to-class comparison is not assessable."""


@dataclass(frozen=True)
class ProjectionMatch:
    class_index: int
    direction_index: int
    direction_xyz: tuple[float, float, float]
    inplane_rotation_deg: float
    shift_yx_pix: tuple[int, int]
    shift_yx_A: tuple[float, float]
    search_ncc: float
    displayed_ncc: float
    runner_up_ncc: float | None
    runner_up_margin: float | None
    runner_up_direction_xyz: tuple[float, float, float] | None
    class_image: np.ndarray
    matched_projection: np.ndarray
    signed_difference: np.ndarray


@dataclass(frozen=True)
class ProjectionComparisonResult:
    matches: tuple[ProjectionMatch, ...]
    work_box: int
    work_pixel_size_A: float
    common_field_of_view_A: float
    particle_diameter_A: float
    mask_soft_edge_A: float
    direction_count: int
    approximate_direction_spacing_deg: float
    requested_inplane_step_deg: float
    actual_inplane_step_deg: float
    inplane_rotation_count: int
    requested_max_shift_A: float
    searched_max_shift_pix: int
    searched_max_shift_A: float
    runner_up_exclusion_deg: float


def _finite_positive(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ProjectionComparisonError(f"{name} must be a finite positive number.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ProjectionComparisonError(
            f"{name} must be a finite positive number."
        ) from exc
    if not math.isfinite(number) or number <= 0:
        raise ProjectionComparisonError(f"{name} must be a finite positive number.")
    return number


def _bounded_integer(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProjectionComparisonError(f"{name} must be an integer.")
    if not minimum <= value <= maximum:
        raise ProjectionComparisonError(
            f"{name} must be between {minimum} and {maximum}."
        )
    return value


def _validate_array(array: np.ndarray, name: str, ndim: int) -> np.ndarray:
    result = np.asarray(array, dtype=np.float32)
    if result.ndim != ndim:
        raise ProjectionComparisonError(
            f"{name} must be {ndim}-dimensional; got shape {result.shape}."
        )
    if result.size == 0:
        raise ProjectionComparisonError(f"{name} must be nonempty.")
    if not np.all(np.isfinite(result)):
        raise ProjectionComparisonError(f"{name} contains non-finite values.")
    if float(np.max(result)) <= float(np.min(result)):
        raise ProjectionComparisonError(f"{name} must contain non-constant signal.")
    return result


def _centered_resample(
    array: np.ndarray,
    *,
    source_pixel_size_A: float,
    target_pixel_size_A: float,
    output_shape: tuple[int, ...],
) -> np.ndarray:
    scale = target_pixel_size_A / source_pixel_size_A
    matrix = np.eye(array.ndim, dtype=np.float64) * scale
    source_center = (np.asarray(array.shape, dtype=np.float64) - 1.0) / 2.0
    output_center = (np.asarray(output_shape, dtype=np.float64) - 1.0) / 2.0
    offset = source_center - matrix @ output_center
    return np.asarray(
        ndimage.affine_transform(
            array,
            matrix,
            offset=offset,
            output_shape=output_shape,
            order=1,
            mode="constant",
            cval=0.0,
            prefilter=False,
        ),
        dtype=np.float32,
    )


def _soft_radial_mask(
    shape: tuple[int, ...],
    *,
    pixel_size_A: float,
    diameter_A: float,
    soft_edge_A: float,
) -> np.ndarray:
    coordinates = np.indices(shape, dtype=np.float32)
    center = (np.asarray(shape, dtype=np.float32) - 1.0) / 2.0
    radius_pix = np.zeros(shape, dtype=np.float32)
    for axis in range(len(shape)):
        radius_pix += (coordinates[axis] - center[axis]) ** 2
    radius_A = np.sqrt(radius_pix) * pixel_size_A
    radius_limit_A = diameter_A / 2.0
    inner_A = max(0.0, radius_limit_A - soft_edge_A)
    mask = np.ones(shape, dtype=np.float32)
    mask[radius_A >= radius_limit_A] = 0.0
    transition = (radius_A > inner_A) & (radius_A < radius_limit_A)
    if np.any(transition):
        phase = (radius_A[transition] - inner_A) / max(soft_edge_A, np.finfo(float).eps)
        mask[transition] = 0.5 + 0.5 * np.cos(np.pi * phase)
    return mask


def _outer_background(array: np.ndarray) -> float:
    shape = array.shape
    coordinates = np.indices(shape, dtype=np.float32)
    center = (np.asarray(shape, dtype=np.float32) - 1.0) / 2.0
    radius_squared = np.zeros(shape, dtype=np.float32)
    for axis in range(array.ndim):
        radius_squared += (
            (coordinates[axis] - center[axis]) / max(center[axis], 1.0)
        ) ** 2
    outer = array[radius_squared >= 0.78**2]
    return float(np.median(outer)) if outer.size else float(np.median(array))


def _normalise_stack(images: np.ndarray, mask: np.ndarray) -> np.ndarray:
    values = np.asarray(images, dtype=np.float32)
    if values.ndim != 3:
        raise ProjectionComparisonError(
            f"Image normalization requires an N/Y/X stack; got {values.shape}."
        )
    weight = float(np.sum(mask, dtype=np.float64))
    if weight <= 1.0:
        raise ProjectionComparisonError(
            "The comparison mask contains too little support."
        )
    means = (
        np.sum(values * mask, axis=(-2, -1), keepdims=True, dtype=np.float64) / weight
    )
    centered = (values - means) * mask
    rms = np.sqrt(
        np.sum(centered**2, axis=(-2, -1), keepdims=True, dtype=np.float64) / weight
    )
    if np.any(~np.isfinite(rms)) or np.any(rms <= 1e-8):
        raise ProjectionComparisonError(
            "A class average or synthetic projection has no varying signal inside the comparison mask."
        )
    return np.asarray(centered / rms, dtype=np.float32)


def _fibonacci_sphere(count: int) -> np.ndarray:
    """Return deterministic viewing directions over the full sphere.

    Both signs of each cardinal direction are included explicitly. Opposite
    viewing directions can produce mirror-related projections of an asymmetric
    density, so treating plane normals as unoriented would miss valid views.
    """
    cardinal = np.asarray(
        (
            (0.0, 0.0, 1.0),
            (0.0, 0.0, -1.0),
            (1.0, 0.0, 0.0),
            (-1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (0.0, -1.0, 0.0),
        ),
        dtype=np.float64,
    )
    generated_count = count + 8
    index = np.arange(generated_count, dtype=np.float64) + 0.5
    z = 1.0 - 2.0 * index / generated_count
    radius = np.sqrt(np.clip(1.0 - z**2, 0.0, 1.0))
    phi = np.pi * (1.0 + math.sqrt(5.0)) * index
    generated = np.stack(
        (radius * np.cos(phi), radius * np.sin(phi), z),
        axis=1,
    )
    selected: list[np.ndarray] = [item for item in cardinal]
    for candidate in generated:
        if any(float(np.dot(candidate, prior)) > 1.0 - 1e-8 for prior in selected):
            continue
        selected.append(candidate)
        if len(selected) == count:
            break
    return np.asarray(selected, dtype=np.float64)


def _projection_basis_zyx(direction_xyz: np.ndarray) -> np.ndarray:
    direction = np.asarray(direction_xyz, dtype=np.float64)
    direction /= np.linalg.norm(direction)
    helper = np.asarray((0.0, 0.0, 1.0), dtype=np.float64)
    if abs(float(np.dot(direction, helper))) > 0.9:
        helper = np.asarray((0.0, 1.0, 0.0), dtype=np.float64)
    image_x = np.cross(helper, direction)
    image_x /= np.linalg.norm(image_x)
    image_y = np.cross(direction, image_x)
    image_y /= np.linalg.norm(image_y)
    # scipy.ndimage uses NumPy Z/Y/X coordinates. Each column says where one
    # output axis (view-Z, image-Y, image-X) samples the source volume.
    return np.column_stack((direction[::-1], image_y[::-1], image_x[::-1]))


def _project_volume(volume: np.ndarray, direction_xyz: np.ndarray) -> np.ndarray:
    matrix = _projection_basis_zyx(direction_xyz)
    center = (np.asarray(volume.shape, dtype=np.float64) - 1.0) / 2.0
    rotated = ndimage.affine_transform(
        volume,
        matrix,
        offset=center - matrix @ center,
        output_shape=volume.shape,
        order=1,
        mode="constant",
        cval=0.0,
        prefilter=False,
    )
    return np.asarray(np.sum(rotated, axis=0, dtype=np.float64), dtype=np.float32)


def _shift_coordinates(
    shape: tuple[int, int], maximum: int
) -> tuple[np.ndarray, np.ndarray]:
    yy, xx = np.indices(shape)
    dy = np.where(yy <= shape[0] // 2, yy, yy - shape[0])
    dx = np.where(xx <= shape[1] // 2, xx, xx - shape[1])
    allowed = (dy**2 + dx**2) <= maximum**2
    return allowed, np.stack((dy, dx), axis=-1)


def _direction_separation_deg(left: np.ndarray, right: np.ndarray) -> float:
    cosine = float(np.clip(np.dot(left, right), -1.0, 1.0))
    return math.degrees(math.acos(cosine))


def compare_map_projections(
    volume_zyx: np.ndarray,
    class_images: np.ndarray,
    *,
    map_pixel_size_A: float,
    class_pixel_size_A: float,
    comparison_lowpass_A: float,
    particle_diameter_A: float,
    direction_count: int = 192,
    inplane_step_deg: float = 10.0,
    max_shift_A: float = 12.0,
    invert_class_contrast: bool = False,
) -> ProjectionComparisonResult:
    """Match filtered 2D classes to projections of one filtered 3D map.

    Both arrays must already have the same physical low-pass applied. This
    routine validates a shared physical grid, downsamples both sides together,
    searches bounded full-sphere viewing directions, in-plane rotations, and
    translations, then returns
    display-ready normalized pairs. Scores are diagnostics, never pass/fail
    criteria for InitialModel acceptance.
    """
    raw_classes = np.asarray(class_images)
    if raw_classes.ndim == 3 and not 1 <= raw_classes.shape[0] <= MAX_CLASSES:
        raise ProjectionComparisonError(
            f"Compare between 1 and {MAX_CLASSES} explicitly selected classes per call."
        )
    volume = _validate_array(volume_zyx, "volume_zyx", 3)
    classes = _validate_array(raw_classes, "class_images", 3)
    if len(set(volume.shape)) != 1:
        raise ProjectionComparisonError(
            f"The map must use a cubic grid; got shape {volume.shape}."
        )
    if classes.shape[1] != classes.shape[2]:
        raise ProjectionComparisonError(
            f"Class averages must be square; got shape {classes.shape[1:]}."
        )

    map_pixel = _finite_positive(map_pixel_size_A, "map_pixel_size_A")
    class_pixel = _finite_positive(class_pixel_size_A, "class_pixel_size_A")
    lowpass = _finite_positive(comparison_lowpass_A, "comparison_lowpass_A")
    diameter = _finite_positive(particle_diameter_A, "particle_diameter_A")
    directions_requested = _bounded_integer(
        direction_count,
        "direction_count",
        MIN_DIRECTION_COUNT,
        MAX_DIRECTION_COUNT,
    )
    step = _finite_positive(inplane_step_deg, "inplane_step_deg")
    if not MIN_INPLANE_STEP_DEG <= step <= MAX_INPLANE_STEP_DEG:
        raise ProjectionComparisonError(
            f"inplane_step_deg must be between {MIN_INPLANE_STEP_DEG:g} and "
            f"{MAX_INPLANE_STEP_DEG:g} degrees."
        )
    if isinstance(max_shift_A, bool):
        raise ProjectionComparisonError(
            "max_shift_A must be a finite nonnegative number."
        )
    try:
        maximum_shift_A = float(max_shift_A)
    except (TypeError, ValueError) as exc:
        raise ProjectionComparisonError(
            "max_shift_A must be a finite nonnegative number."
        ) from exc
    if not math.isfinite(maximum_shift_A) or maximum_shift_A < 0:
        raise ProjectionComparisonError(
            "max_shift_A must be a finite nonnegative number."
        )
    if type(invert_class_contrast) is not bool:
        raise ProjectionComparisonError("invert_class_contrast must be a boolean.")

    map_fov = volume.shape[0] * map_pixel
    class_fov = classes.shape[1] * class_pixel
    sampling_tolerance = max(1e-5, map_pixel * 1e-4)
    fov_tolerance = max(map_pixel, class_pixel) * 0.51
    if not math.isclose(
        map_pixel, class_pixel, rel_tol=1e-4, abs_tol=sampling_tolerance
    ):
        raise ProjectionComparisonError(
            "Map and class averages must have matching physical pixel sizes before comparison: "
            f"{map_pixel:g} A versus {class_pixel:g} A."
        )
    if not math.isclose(map_fov, class_fov, rel_tol=1e-4, abs_tol=fov_tolerance):
        raise ProjectionComparisonError(
            "Map and class averages must have matching physical fields of view before comparison: "
            f"{map_fov:g} A versus {class_fov:g} A."
        )
    common_fov = min(map_fov, class_fov)
    if diameter >= common_fov:
        raise ProjectionComparisonError(
            "particle_diameter_A must be smaller than the shared physical field of view."
        )
    if maximum_shift_A > (common_fov - diameter) / 2.0:
        raise ProjectionComparisonError(
            "max_shift_A exceeds the available solvent margin around particle_diameter_A."
        )

    work_box = min(int(volume.shape[0]), MAX_WORK_BOX)
    if work_box % 2:
        work_box -= 1
    if work_box < 24:
        raise ProjectionComparisonError(
            "The common map/class grid is too small for bounded projection comparison."
        )
    work_pixel = common_fov / work_box
    if lowpass < 2.0 * work_pixel and not math.isclose(
        lowpass, 2.0 * work_pixel, rel_tol=1e-7, abs_tol=1e-7
    ):
        raise ProjectionComparisonError(
            f"comparison_lowpass_A={lowpass:g} A is beyond the {2.0 * work_pixel:g} A "
            "Nyquist limit of the bounded common analysis grid; use a coarser low-pass."
        )

    volume_background = _outer_background(volume)
    prepared_volume = np.asarray(volume - volume_background, dtype=np.float32)
    class_backgrounds = np.asarray(
        [_outer_background(image) for image in classes], dtype=np.float32
    )
    prepared_classes = classes - class_backgrounds[:, None, None]
    if invert_class_contrast:
        prepared_classes = -prepared_classes

    volume_work = _centered_resample(
        prepared_volume,
        source_pixel_size_A=map_pixel,
        target_pixel_size_A=work_pixel,
        output_shape=(work_box, work_box, work_box),
    )
    classes_work = np.stack(
        [
            _centered_resample(
                image,
                source_pixel_size_A=class_pixel,
                target_pixel_size_A=work_pixel,
                output_shape=(work_box, work_box),
            )
            for image in prepared_classes
        ]
    ).astype(np.float32)

    soft_edge_A = max(2.0 * work_pixel, lowpass / 4.0)
    mask_3d = _soft_radial_mask(
        volume_work.shape,
        pixel_size_A=work_pixel,
        diameter_A=diameter,
        soft_edge_A=soft_edge_A,
    )
    mask_2d = _soft_radial_mask(
        classes_work.shape[1:],
        pixel_size_A=work_pixel,
        diameter_A=diameter,
        soft_edge_A=soft_edge_A,
    )
    volume_work *= mask_3d
    classes_normalized = _normalise_stack(classes_work, mask_2d)

    directions = _fibonacci_sphere(directions_requested)
    projection_bank = np.stack(
        [_project_volume(volume_work, direction) for direction in directions]
    ).astype(np.float32)
    projection_bank = _normalise_stack(projection_bank, mask_2d)

    rotation_count = max(1, math.ceil(360.0 / step))
    angles = np.arange(rotation_count, dtype=np.float64) * (360.0 / rotation_count)
    actual_step = 360.0 / rotation_count
    maximum_shift_pix = math.floor(maximum_shift_A / work_pixel + 1e-8)
    maximum_shift_pix = min(maximum_shift_pix, work_box // 8)
    allowed_shifts, shift_coordinates = _shift_coordinates(
        (work_box, work_box), maximum_shift_pix
    )
    mask_weight = float(np.sum(mask_2d, dtype=np.float64))
    class_fft = np.fft.rfft2(classes_normalized, axes=(-2, -1))

    class_count = classes_normalized.shape[0]
    direction_scores = np.full(
        (class_count, len(directions)), -np.inf, dtype=np.float32
    )
    best_angles = np.zeros((class_count, len(directions)), dtype=np.float32)
    best_shifts = np.zeros((class_count, len(directions), 2), dtype=np.int16)
    for direction_index, projection in enumerate(projection_bank):
        rotated = np.stack(
            [
                ndimage.rotate(
                    projection,
                    float(angle),
                    reshape=False,
                    order=1,
                    mode="constant",
                    cval=0.0,
                    prefilter=False,
                )
                for angle in angles
            ]
        ).astype(np.float32)
        rotated = _normalise_stack(rotated, mask_2d)
        projection_fft = np.fft.rfft2(rotated, axes=(-2, -1))
        correlation = (
            np.fft.irfft2(
                class_fft[:, None, :, :] * np.conj(projection_fft)[None, :, :, :],
                s=(work_box, work_box),
                axes=(-2, -1),
            )
            / mask_weight
        )
        correlation[..., ~allowed_shifts] = -np.inf
        flattened = correlation.reshape(class_count, -1)
        flat_indices = np.argmax(flattened, axis=1)
        values = flattened[np.arange(class_count), flat_indices]
        rotation_indices, shift_flat = np.divmod(flat_indices, work_box * work_box)
        shift_y, shift_x = np.divmod(shift_flat, work_box)
        direction_scores[:, direction_index] = np.asarray(values, dtype=np.float32)
        best_angles[:, direction_index] = angles[rotation_indices]
        best_shifts[:, direction_index] = shift_coordinates[shift_y, shift_x]

    spacing = math.degrees(math.sqrt(4.0 * math.pi / len(directions)))
    runner_up_exclusion = max(15.0, 1.5 * spacing)
    matches: list[ProjectionMatch] = []
    for class_index in range(class_count):
        direction_index = int(np.argmax(direction_scores[class_index]))
        search_score = float(direction_scores[class_index, direction_index])
        angle = float(best_angles[class_index, direction_index])
        shift = tuple(int(item) for item in best_shifts[class_index, direction_index])
        rotated = ndimage.rotate(
            projection_bank[direction_index],
            angle,
            reshape=False,
            order=1,
            mode="constant",
            cval=0.0,
            prefilter=False,
        )
        aligned = ndimage.shift(
            rotated,
            shift=shift,
            order=1,
            mode="constant",
            cval=0.0,
            prefilter=False,
        )
        aligned_normalized = _normalise_stack(aligned[None, :, :], mask_2d)[0]
        displayed_score = float(
            np.sum(
                classes_normalized[class_index] * aligned_normalized,
                dtype=np.float64,
            )
            / mask_weight
        )

        eligible = np.asarray(
            [
                _direction_separation_deg(directions[direction_index], candidate)
                >= runner_up_exclusion
                for candidate in directions
            ],
            dtype=bool,
        )
        runner_index: int | None = None
        runner_score: float | None = None
        if np.any(eligible):
            eligible_indices = np.flatnonzero(eligible)
            runner_index = int(
                eligible_indices[
                    np.argmax(direction_scores[class_index, eligible_indices])
                ]
            )
            runner_score = float(direction_scores[class_index, runner_index])
        direction = tuple(float(item) for item in directions[direction_index])
        runner_direction = (
            tuple(float(item) for item in directions[runner_index])
            if runner_index is not None
            else None
        )
        matches.append(
            ProjectionMatch(
                class_index=class_index,
                direction_index=direction_index,
                direction_xyz=direction,
                inplane_rotation_deg=angle,
                shift_yx_pix=shift,
                shift_yx_A=(shift[0] * work_pixel, shift[1] * work_pixel),
                search_ncc=search_score,
                displayed_ncc=displayed_score,
                runner_up_ncc=runner_score,
                runner_up_margin=(
                    search_score - runner_score if runner_score is not None else None
                ),
                runner_up_direction_xyz=runner_direction,
                class_image=classes_normalized[class_index],
                matched_projection=aligned_normalized,
                signed_difference=np.asarray(
                    classes_normalized[class_index] - aligned_normalized,
                    dtype=np.float32,
                ),
            )
        )

    return ProjectionComparisonResult(
        matches=tuple(matches),
        work_box=work_box,
        work_pixel_size_A=work_pixel,
        common_field_of_view_A=common_fov,
        particle_diameter_A=diameter,
        mask_soft_edge_A=soft_edge_A,
        direction_count=len(directions),
        approximate_direction_spacing_deg=spacing,
        requested_inplane_step_deg=step,
        actual_inplane_step_deg=actual_step,
        inplane_rotation_count=rotation_count,
        requested_max_shift_A=maximum_shift_A,
        searched_max_shift_pix=maximum_shift_pix,
        searched_max_shift_A=maximum_shift_pix * work_pixel,
        runner_up_exclusion_deg=runner_up_exclusion,
    )
