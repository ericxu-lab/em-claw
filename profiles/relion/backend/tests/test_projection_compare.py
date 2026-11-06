from __future__ import annotations

import numpy as np
import pytest
from scipy import ndimage

from relion_mcp.projection_compare import (
    MAX_CLASSES,
    MAX_DIRECTION_COUNT,
    MIN_DIRECTION_COUNT,
    ProjectionComparisonError,
    _project_volume,
    compare_map_projections,
)


def _asymmetric_volume(size: int = 32) -> np.ndarray:
    """Return a compact chiral-looking density with distinguishable axis views."""
    z, y, x = np.indices((size, size, size), dtype=np.float32)
    center = (size - 1) / 2.0
    volume = np.zeros((size, size, size), dtype=np.float32)
    peaks = (
        ((-5.0, -3.0, -2.0), 1.0),
        ((-1.0, 4.0, -3.0), 0.8),
        ((3.0, -2.0, 4.0), 0.6),
        ((5.0, 3.0, 1.0), 0.4),
    )
    for (offset_z, offset_y, offset_x), amplitude in peaks:
        radius_squared = (
            (z - (center + offset_z)) ** 2
            + (y - (center + offset_y)) ** 2
            + (x - (center + offset_x)) ** 2
        )
        volume += amplitude * np.exp(-radius_squared / (2.0 * 0.65**2))
    return volume


def _comparison_kwargs() -> dict[str, object]:
    return {
        "map_pixel_size_A": 1.0,
        "class_pixel_size_A": 1.0,
        "comparison_lowpass_A": 4.0,
        "particle_diameter_A": 22.0,
        "direction_count": MIN_DIRECTION_COUNT,
        "inplane_step_deg": 30.0,
        "max_shift_A": 4.0,
    }


def test_cardinal_projection_basis_has_explicit_opposite_views() -> None:
    volume = _asymmetric_volume()

    expected = {
        (0.0, 0.0, 1.0): np.sum(volume, axis=0),
        (0.0, 0.0, -1.0): np.sum(volume, axis=0)[:, ::-1],
        (1.0, 0.0, 0.0): np.sum(volume, axis=2),
        (-1.0, 0.0, 0.0): np.sum(volume, axis=2)[:, ::-1],
        (0.0, 1.0, 0.0): np.sum(volume, axis=1)[:, ::-1],
        (0.0, -1.0, 0.0): np.sum(volume, axis=1),
    }

    for direction, projection in expected.items():
        np.testing.assert_allclose(
            _project_volume(volume, np.asarray(direction, dtype=np.float64)),
            projection,
            rtol=1e-6,
            atol=1e-6,
        )

    assert not np.allclose(expected[(1.0, 0.0, 0.0)], expected[(-1.0, 0.0, 0.0)])


def test_recovers_known_axis_rotation_and_shift_without_mutating_inputs() -> None:
    volume = _asymmetric_volume()
    specifications = (
        ((0.0, 0.0, 1.0), 0.0, (0, 0)),
        ((0.0, 1.0, 0.0), 60.0, (-2, 1)),
        ((-1.0, 0.0, 0.0), 90.0, (2, -3)),
    )
    classes = []
    for direction, angle, shift in specifications:
        projection = _project_volume(volume, np.asarray(direction, dtype=np.float64))
        rotated = ndimage.rotate(
            projection,
            angle,
            reshape=False,
            order=1,
            mode="constant",
            cval=0.0,
            prefilter=False,
        )
        classes.append(
            ndimage.shift(
                rotated,
                shift=shift,
                order=1,
                mode="constant",
                cval=0.0,
                prefilter=False,
            )
        )
    class_stack = np.asarray(classes, dtype=np.float32)
    original_volume = volume.copy()
    original_classes = class_stack.copy()

    result = compare_map_projections(volume, class_stack, **_comparison_kwargs())

    np.testing.assert_array_equal(volume, original_volume)
    np.testing.assert_array_equal(class_stack, original_classes)
    assert result.work_box == 32
    assert result.work_pixel_size_A == pytest.approx(1.0)
    assert result.direction_count == MIN_DIRECTION_COUNT
    assert result.actual_inplane_step_deg == pytest.approx(30.0)
    assert result.searched_max_shift_pix == 4
    assert result.searched_max_shift_A == pytest.approx(4.0)
    for match, (direction, angle, shift) in zip(
        result.matches, specifications, strict=True
    ):
        assert match.direction_xyz == direction
        assert match.inplane_rotation_deg == pytest.approx(angle)
        assert match.shift_yx_pix == shift
        assert match.shift_yx_A == pytest.approx(shift)
        assert match.search_ncc > 0.90
        assert match.displayed_ncc > 0.98
        assert match.runner_up_ncc is not None
        assert match.runner_up_margin is not None
        assert match.runner_up_margin > 0.05
        np.testing.assert_allclose(
            match.signed_difference,
            match.class_image - match.matched_projection,
            rtol=0.0,
            atol=1e-6,
        )

    # The third class is a mirror-distinct negative-X view. Recovering +X here
    # would mean opposite viewing directions had incorrectly been collapsed.
    assert result.matches[2].direction_xyz == (-1.0, 0.0, 0.0)


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        (
            {"direction_count": MIN_DIRECTION_COUNT - 1},
            "direction_count must be between",
        ),
        (
            {"direction_count": MAX_DIRECTION_COUNT + 1},
            "direction_count must be between",
        ),
        ({"direction_count": True}, "direction_count must be an integer"),
        ({"inplane_step_deg": 4.9}, "inplane_step_deg must be between"),
        ({"inplane_step_deg": 45.1}, "inplane_step_deg must be between"),
        ({"max_shift_A": -0.1}, "max_shift_A must be a finite nonnegative"),
        ({"invert_class_contrast": 1}, "invert_class_contrast must be a boolean"),
        ({"max_shift_A": 5.1}, "max_shift_A exceeds the available solvent margin"),
        ({"particle_diameter_A": 32.0}, "particle_diameter_A must be smaller"),
        ({"comparison_lowpass_A": 1.9}, "Nyquist limit"),
    ),
)
def test_search_and_physical_bounds_are_rejected(
    overrides: dict[str, object], message: str
) -> None:
    volume = _asymmetric_volume()
    classes = np.sum(volume, axis=0, keepdims=True)
    kwargs = _comparison_kwargs()
    kwargs.update(overrides)

    with pytest.raises(ProjectionComparisonError, match=message):
        compare_map_projections(volume, classes, **kwargs)


def test_rejects_incompatible_sampling_field_of_view_and_class_count() -> None:
    volume = _asymmetric_volume()
    projection = np.sum(volume, axis=0, keepdims=True)

    with pytest.raises(
        ProjectionComparisonError, match="matching physical pixel sizes"
    ):
        compare_map_projections(
            volume,
            projection,
            **{**_comparison_kwargs(), "class_pixel_size_A": 1.1},
        )
    with pytest.raises(
        ProjectionComparisonError, match="matching physical fields of view"
    ):
        compare_map_projections(
            volume,
            projection[:, 1:-1, 1:-1],
            **_comparison_kwargs(),
        )
    with pytest.raises(ProjectionComparisonError, match=f"between 1 and {MAX_CLASSES}"):
        compare_map_projections(
            volume,
            np.repeat(projection, MAX_CLASSES + 1, axis=0),
            **_comparison_kwargs(),
        )
    with pytest.raises(ProjectionComparisonError, match=f"between 1 and {MAX_CLASSES}"):
        compare_map_projections(
            volume,
            np.empty((0, volume.shape[1], volume.shape[2]), dtype=np.float32),
            **_comparison_kwargs(),
        )


@pytest.mark.parametrize("kind", ("constant", "nonfinite"))
def test_rejects_unassessable_pixel_arrays(kind: str) -> None:
    volume = _asymmetric_volume()
    classes = np.sum(volume, axis=0, keepdims=True)
    if kind == "constant":
        classes = np.ones_like(classes)
        message = "class_images must contain non-constant signal"
    else:
        classes = classes.copy()
        classes[0, 0, 0] = np.nan
        message = "class_images contains non-finite values"

    with pytest.raises(ProjectionComparisonError, match=message):
        compare_map_projections(volume, classes, **_comparison_kwargs())
