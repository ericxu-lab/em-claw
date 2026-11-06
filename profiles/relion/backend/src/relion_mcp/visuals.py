from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Literal

import mrcfile
import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.collections import EllipseCollection
from matplotlib.figure import Figure
from matplotlib.patches import Circle
from PIL import Image as PILImage
from scipy import fft, ndimage
from scipy.spatial import cKDTree

from .map_compare import SCHEMA_VERSION as MAP_COMPARISON_SCHEMA_VERSION
from .map_compare import (
    MapComparisonError,
    compare_maps,
    validate_map_comparison_resources,
)
from .projection_compare import (
    MAX_CLASSES as MAX_PROJECTION_COMPARISON_CLASSES,
)
from .projection_compare import SCHEMA_VERSION as PROJECTION_COMPARISON_SCHEMA_VERSION
from .projection_compare import (
    ProjectionComparisonError,
    compare_map_projections,
)
from .settings import ConfigurationError, Settings
from .star import parse_star


class VisualError(ValueError):
    """Raised when a RELION visual report cannot be rendered safely."""


@dataclass(frozen=True)
class RenderedVisual:
    """A PNG plus concise metadata that should accompany it as TextContent."""

    png: bytes
    metadata: dict[str, Any]


_SYMMETRY_SCAN_MAX_ESTIMATED_BYTES = 768 * 1024 * 1024
_MAP_COMPARISON_MAX_INPUT_FILE_BYTES = 512 * 1024 * 1024
_VOLUME_RENDER_MAX_INPUT_FILE_BYTES = 512 * 1024 * 1024
_PROJECTION_COMPARISON_MAX_STACK_FILE_BYTES = 512 * 1024 * 1024
_PROJECTION_COMPARISON_MAX_CLASS_SIDE = 1024


def _check_volume_render_size(path: Path, label: str) -> None:
    """Reject an oversized source volume before it is fully read into RAM.

    Unlike a plain downsampled slice view, a display low-pass filter and a
    stack/record load both materialize the entire array in memory before any
    shrinking happens, so the size must be bounded before that read rather
    than after.
    """
    file_bytes = path.stat().st_size
    if file_bytes > _VOLUME_RENDER_MAX_INPUT_FILE_BYTES:
        raise VisualError(
            f"{label} is {file_bytes:,} bytes, above the "
            f"{_VOLUME_RENDER_MAX_INPUT_FILE_BYTES:,}-byte input-file cap for full-volume rendering."
        )


@dataclass(frozen=True)
class _MrcInfo:
    path: str
    shape: tuple[int, ...]
    dtype: str
    voxel_size_A: tuple[float, float, float] | None
    origin_A: tuple[float, float, float]
    axis_mapping: tuple[int, int, int]
    start_xyz: tuple[int, int, int] = (0, 0, 0)
    start_column_row_section: tuple[int, int, int] = (0, 0, 0)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _numeric_column(block: dict[str, Any], key: str, length: int) -> list[float | None]:
    values = _as_list(block.get(key))
    result: list[float | None] = []
    for index in range(length):
        result.append(_as_float(values[index]) if index < len(values) else None)
    return result


def _find_block(
    blocks: dict[str, dict[str, Any]],
    *,
    required: Sequence[str] = (),
    any_of: Sequence[str] = (),
) -> tuple[str, dict[str, Any]]:
    for name, block in blocks.items():
        if all(key in block for key in required) and (not any_of or any(key in block for key in any_of)):
            return name, block
    requirement = ", ".join(required) or "one of " + ", ".join(any_of)
    raise VisualError(f"No STAR block contains the required column(s): {requirement}.")


def _project_file(
    settings: Settings,
    project_dir: str,
    raw_path: str,
    *,
    base_dir: Path | None = None,
    suffixes: Sequence[str] = (),
) -> Path:
    project = settings.validate_project_path(project_dir)
    supplied = Path(raw_path).expanduser()

    candidates: list[Path]
    if supplied.is_absolute():
        candidates = [supplied]
    else:
        candidates = [project / supplied]
        if base_dir is not None:
            alternate = base_dir / supplied
            if alternate != candidates[0]:
                candidates.append(alternate)

    missing: list[str] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError:
            missing.append(str(candidate))
            continue
        if not (resolved == project or project in resolved.parents):
            raise ConfigurationError(f"Visual input escapes the approved RELION project: {raw_path}")
        if not resolved.is_file():
            raise VisualError(f"Visual input is not a file: {resolved}")
        if suffixes and resolved.suffix.lower() not in {item.lower() for item in suffixes}:
            allowed = ", ".join(suffixes)
            raise VisualError(f"Expected one of [{allowed}], got {resolved.name!r}.")
        return resolved

    locations = ", ".join(missing) if missing else raw_path
    raise VisualError(f"Visual input does not exist: {locations}")


def _relative(project: Path, path: Path) -> str:
    try:
        return path.relative_to(project).as_posix()
    except ValueError:
        return str(path)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_voxel_sha256(volume: np.ndarray) -> str:
    """Hash finite canonical Z/Y/X voxel values independently of MRC headers."""
    data = np.asarray(volume)
    if data.ndim != 3:
        raise VisualError("Canonical voxel hashing requires a 3D array.")
    digest = hashlib.sha256()
    digest.update(json.dumps(list(data.shape), separators=(",", ":")).encode("ascii"))
    digest.update(b"\0float32-little-endian\0")
    for section in data:
        plane = np.ascontiguousarray(section, dtype="<f4")
        digest.update(memoryview(plane).cast("B"))
    return digest.hexdigest()


def _mrc_info(handle: mrcfile.mrcfile.MrcFile, path: Path) -> _MrcInfo:
    voxel: tuple[float, float, float] | None = None
    try:
        values = (
            float(handle.voxel_size.x),
            float(handle.voxel_size.y),
            float(handle.voxel_size.z),
        )
        if all(math.isfinite(item) and item > 0 for item in values):
            voxel = values
    except (AttributeError, TypeError, ValueError):
        voxel = None
    try:
        origin = (
            float(handle.header.origin.x),
            float(handle.header.origin.y),
            float(handle.header.origin.z),
        )
    except (AttributeError, TypeError, ValueError):
        origin = (0.0, 0.0, 0.0)
    try:
        axis_mapping = (
            int(handle.header.mapc),
            int(handle.header.mapr),
            int(handle.header.maps),
        )
    except (AttributeError, TypeError, ValueError):
        axis_mapping = (1, 2, 3)
    try:
        start_column_row_section = (
            int(handle.header.nxstart),
            int(handle.header.nystart),
            int(handle.header.nzstart),
        )
    except (AttributeError, TypeError, ValueError):
        start_column_row_section = (0, 0, 0)
    if sorted(axis_mapping) == [1, 2, 3]:
        physical_start = [0, 0, 0]
        for start, physical_axis in zip(
            start_column_row_section,
            axis_mapping,
            strict=True,
        ):
            physical_start[physical_axis - 1] = start
        start_xyz = tuple(physical_start)
    else:
        # Invalid mappings are rejected by consumers that require a physical
        # grid. Preserve the raw values here rather than inventing a remap.
        start_xyz = start_column_row_section
    return _MrcInfo(
        path=str(path),
        shape=tuple(int(item) for item in handle.data.shape),
        dtype=str(handle.data.dtype),
        voxel_size_A=voxel,
        origin_A=origin,
        axis_mapping=axis_mapping,
        start_xyz=start_xyz,
        start_column_row_section=start_column_row_section,
    )


def _assert_same_grid(reference: _MrcInfo, candidate: _MrcInfo, context: str) -> None:
    """Reject comparisons between volumes that do not share a physical MRC grid."""
    mismatches: list[str] = []
    if reference.shape != candidate.shape:
        mismatches.append(f"shape {reference.shape} != {candidate.shape}")
    if reference.voxel_size_A is None or candidate.voxel_size_A is None:
        mismatches.append("positive voxel size is missing")
    elif not np.allclose(reference.voxel_size_A, candidate.voxel_size_A, rtol=1e-6, atol=1e-6):
        mismatches.append(
            f"voxel size {reference.voxel_size_A} != {candidate.voxel_size_A}"
        )
    if not np.allclose(reference.origin_A, candidate.origin_A, rtol=1e-6, atol=1e-6):
        mismatches.append(f"origin {reference.origin_A} != {candidate.origin_A}")
    if reference.axis_mapping != candidate.axis_mapping:
        mismatches.append(
            f"axis mapping {reference.axis_mapping} != {candidate.axis_mapping}"
        )
    if reference.start_xyz != candidate.start_xyz:
        mismatches.append(
            f"start indices {reference.start_xyz} != {candidate.start_xyz}"
        )
    if mismatches:
        raise VisualError(f"{context} requires identical physical grids: {'; '.join(mismatches)}.")


def _data_axis_voxel_size_A(info: _MrcInfo) -> tuple[float, float, float]:
    """Return physical sampling for NumPy's section, row, column axes."""
    if info.voxel_size_A is None:
        raise VisualError(
            "An Angstrom-defined display filter requires positive MRC voxel size metadata."
        )
    if sorted(info.axis_mapping) != [1, 2, 3]:
        raise VisualError(
            "An Angstrom-defined display filter requires MRC mapc/mapr/maps to be a permutation of 1, 2, 3."
        )
    mapc, mapr, maps = info.axis_mapping
    voxel_xyz = info.voxel_size_A
    return (
        voxel_xyz[maps - 1],
        voxel_xyz[mapr - 1],
        voxel_xyz[mapc - 1],
    )


def _apply_display_lowpass(
    volume: np.ndarray,
    info: _MrcInfo,
    display_lowpass_A: float | None,
    *,
    filter_edge_width_shells: int = 2,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Apply a read-only physical Fourier low-pass for visual evidence.

    The raised-cosine edge follows RELION's two-shell low-pass convention, but
    uses the MRC physical grid directly so non-canonical axis mappings and
    anisotropic sampling retain an honest Angstrom definition.
    """
    data = np.asarray(volume)
    metadata: dict[str, Any] = {
        "applied": False,
        "display_only": True,
        "source_map_modified": False,
        "raw_source_preserved": True,
        "applied_before_downsampling": False,
        "cutoff_resolution_A": None,
    }
    if display_lowpass_A is None:
        return data, metadata
    if isinstance(display_lowpass_A, bool):
        raise VisualError("display_lowpass_A must be a finite positive Angstrom value.")
    try:
        cutoff_A = float(display_lowpass_A)
    except (TypeError, ValueError) as exc:
        raise VisualError(
            "display_lowpass_A must be a finite positive Angstrom value."
        ) from exc
    if not math.isfinite(cutoff_A) or cutoff_A <= 0:
        raise VisualError("display_lowpass_A must be a finite positive Angstrom value.")
    if filter_edge_width_shells <= 0 or filter_edge_width_shells % 2:
        raise VisualError(
            "The display low-pass edge width must be a positive even shell count."
        )

    spacing_zyx = _data_axis_voxel_size_A(info)
    isotropic_nyquist_A = 2.0 * max(spacing_zyx)
    if cutoff_A < isotropic_nyquist_A and not math.isclose(
        cutoff_A,
        isotropic_nyquist_A,
        rel_tol=1e-7,
        abs_tol=1e-7,
    ):
        raise VisualError(
            f"display_lowpass_A={cutoff_A:g} Å is beyond the map's isotropic Nyquist limit "
            f"of {isotropic_nyquist_A:g} Å."
        )

    finite = np.isfinite(data)
    if not np.any(finite):
        raise VisualError(
            "An Angstrom-defined display filter requires at least one finite map voxel."
        )
    fill_value = float(np.mean(np.asarray(data[finite], dtype=np.float64)))
    prepared = np.asarray(np.where(finite, data, fill_value), dtype=np.float32)

    frequency_steps_A_inv = tuple(
        1.0 / (size * spacing)
        for size, spacing in zip(prepared.shape, spacing_zyx, strict=True)
    )
    shell_spacing_A_inv = min(frequency_steps_A_inv)
    cutoff_frequency_A_inv = 1.0 / cutoff_A
    edge_half_width_A_inv = filter_edge_width_shells * shell_spacing_A_inv / 2.0
    edge_low_A_inv = max(0.0, cutoff_frequency_A_inv - edge_half_width_A_inv)
    edge_high_A_inv = cutoff_frequency_A_inv + edge_half_width_A_inv

    frequencies_z = np.asarray(
        fft.fftfreq(prepared.shape[0], d=spacing_zyx[0]),
        dtype=np.float32,
    )
    frequencies_y = np.asarray(
        fft.fftfreq(prepared.shape[1], d=spacing_zyx[1]),
        dtype=np.float32,
    )
    frequencies_x = np.asarray(
        fft.rfftfreq(prepared.shape[2], d=spacing_zyx[2]),
        dtype=np.float32,
    )
    radial_frequency = np.sqrt(
        frequencies_z[:, None, None] ** 2
        + frequencies_y[None, :, None] ** 2
        + frequencies_x[None, None, :] ** 2
    )
    transfer = np.ones(radial_frequency.shape, dtype=np.float32)
    transfer[radial_frequency >= edge_high_A_inv] = 0.0
    transition = (radial_frequency > edge_low_A_inv) & (
        radial_frequency < edge_high_A_inv
    )
    transition_width_A_inv = edge_high_A_inv - edge_low_A_inv
    transfer[transition] = 0.5 + 0.5 * np.cos(
        np.pi * (radial_frequency[transition] - edge_low_A_inv) / transition_width_A_inv
    )

    spectrum = fft.rfftn(prepared)
    spectrum *= transfer
    filtered = np.asarray(
        fft.irfftn(spectrum, s=prepared.shape),
        dtype=np.float32,
    )
    metadata.update(
        {
            "applied": True,
            "type": "radial_fourier_raised_cosine_lowpass",
            "cutoff_resolution_A": cutoff_A,
            "cutoff_spatial_frequency_A_inv": cutoff_frequency_A_inv,
            "filter_edge_width_fourier_shells": filter_edge_width_shells,
            "frequency_shell_basis_A_inv": shell_spacing_A_inv,
            "transition_edge_low_A_inv": edge_low_A_inv,
            "transition_edge_high_A_inv": edge_high_A_inv,
            "source_voxel_size_A_xyz": list(info.voxel_size_A or ()),
            "data_axis_voxel_size_A_zyx": list(spacing_zyx),
            "source_axis_mapping": list(info.axis_mapping),
            "full_resolution_shape_zyx": list(prepared.shape),
            "isotropic_nyquist_resolution_A": isotropic_nyquist_A,
            "nonfinite_voxels_filled_with_finite_mean": int(
                np.size(data) - np.sum(finite)
            ),
            "applied_before_downsampling": True,
            "reference_bias_experiment": False,
        }
    )
    return filtered, metadata


def _apply_2d_display_lowpass(
    image: np.ndarray,
    pixel_size_A: float,
    display_lowpass_A: float,
    *,
    filter_edge_width_shells: int = 2,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Apply the map renderer's physical raised-cosine low-pass in two dimensions."""
    data = np.asarray(image)
    if data.ndim != 2:
        raise VisualError(f"A 2D comparison filter requires a 2D image; got {data.shape}.")
    if isinstance(pixel_size_A, bool) or not math.isfinite(float(pixel_size_A)) or pixel_size_A <= 0:
        raise VisualError("2D class comparison requires a finite positive physical pixel size.")
    if (
        isinstance(display_lowpass_A, bool)
        or not math.isfinite(float(display_lowpass_A))
        or display_lowpass_A <= 0
    ):
        raise VisualError("comparison_lowpass_A must be a finite positive Angstrom value.")
    if filter_edge_width_shells <= 0 or filter_edge_width_shells % 2:
        raise VisualError("The comparison low-pass edge width must be a positive even shell count.")
    pixel = float(pixel_size_A)
    cutoff_A = float(display_lowpass_A)
    nyquist_A = 2.0 * pixel
    if cutoff_A < nyquist_A and not math.isclose(
        cutoff_A, nyquist_A, rel_tol=1e-7, abs_tol=1e-7
    ):
        raise VisualError(
            f"comparison_lowpass_A={cutoff_A:g} A is beyond the class averages' "
            f"{nyquist_A:g} A Nyquist limit."
        )
    finite = np.isfinite(data)
    if not np.any(finite):
        raise VisualError("A comparison filter requires finite class-average pixels.")
    fill = float(np.mean(np.asarray(data[finite], dtype=np.float64)))
    prepared = np.asarray(np.where(finite, data, fill), dtype=np.float32)
    frequency_steps_A_inv = (
        1.0 / (prepared.shape[0] * pixel),
        1.0 / (prepared.shape[1] * pixel),
    )
    shell_spacing_A_inv = min(frequency_steps_A_inv)
    cutoff_frequency_A_inv = 1.0 / cutoff_A
    edge_half_width_A_inv = filter_edge_width_shells * shell_spacing_A_inv / 2.0
    edge_low_A_inv = max(0.0, cutoff_frequency_A_inv - edge_half_width_A_inv)
    edge_high_A_inv = cutoff_frequency_A_inv + edge_half_width_A_inv
    frequency_y = np.asarray(
        fft.fftfreq(prepared.shape[0], d=pixel), dtype=np.float32
    )
    frequency_x = np.asarray(
        fft.rfftfreq(prepared.shape[1], d=pixel), dtype=np.float32
    )
    radial_frequency = np.sqrt(
        frequency_y[:, None] ** 2 + frequency_x[None, :] ** 2
    )
    transfer = np.ones(radial_frequency.shape, dtype=np.float32)
    transfer[radial_frequency >= edge_high_A_inv] = 0.0
    transition = (radial_frequency > edge_low_A_inv) & (
        radial_frequency < edge_high_A_inv
    )
    transition_width_A_inv = edge_high_A_inv - edge_low_A_inv
    transfer[transition] = 0.5 + 0.5 * np.cos(
        np.pi
        * (radial_frequency[transition] - edge_low_A_inv)
        / transition_width_A_inv
    )
    spectrum = fft.rfft2(prepared)
    spectrum *= transfer
    filtered = np.asarray(
        fft.irfft2(spectrum, s=prepared.shape), dtype=np.float32
    )
    return filtered, {
        "applied": True,
        "display_only": True,
        "source_modified": False,
        "raw_source_preserved": True,
        "type": "radial_fourier_raised_cosine_lowpass",
        "cutoff_resolution_A": cutoff_A,
        "cutoff_spatial_frequency_A_inv": cutoff_frequency_A_inv,
        "filter_edge_width_fourier_shells": filter_edge_width_shells,
        "frequency_shell_basis_A_inv": shell_spacing_A_inv,
        "transition_edge_low_A_inv": edge_low_A_inv,
        "transition_edge_high_A_inv": edge_high_A_inv,
        "source_pixel_size_A_xy": [pixel, pixel],
        "source_shape_yx": list(prepared.shape),
        "isotropic_nyquist_resolution_A": nyquist_A,
        "nonfinite_pixels_filled_with_finite_mean": int(
            np.size(data) - np.sum(finite)
        ),
        "reference_bias_experiment": False,
    }


def _sample_finite(array: np.ndarray, *, target: int = 1_000_000) -> np.ndarray:
    flat = np.asarray(array).reshape(-1)
    stride = max(1, flat.size // max(1, target))
    sampled = np.asarray(flat[::stride], dtype=np.float64)
    return sampled[np.isfinite(sampled)]


def _display_limits(
    array: np.ndarray,
    low_percentile: float,
    high_percentile: float,
    *,
    symmetric: bool = False,
) -> tuple[float, float]:
    if not (0 <= low_percentile < high_percentile <= 100):
        raise VisualError("Display percentiles must satisfy 0 <= low < high <= 100.")
    finite = _sample_finite(array)
    if finite.size == 0:
        return 0.0, 1.0
    low, high = np.percentile(finite, [low_percentile, high_percentile])
    low = float(low)
    high = float(high)
    if symmetric:
        extent = max(abs(low), abs(high), np.finfo(float).eps)
        return -extent, extent
    if not math.isfinite(low) or not math.isfinite(high) or high <= low:
        center = float(np.mean(finite))
        spread = float(np.std(finite)) or 1.0
        return center - spread, center + spread
    return low, high


def _downsample_2d(array: np.ndarray, max_side: int = 900) -> tuple[np.ndarray, tuple[int, int]]:
    image = np.asarray(array)
    if image.ndim != 2:
        raise VisualError(f"Expected a 2D image, got shape {image.shape}.")
    original = (int(image.shape[0]), int(image.shape[1]))
    stride = max(1, math.ceil(max(original) / max_side))
    return np.asarray(image[::stride, ::stride], dtype=np.float32), original


def _downsample_volume(volume: np.ndarray, max_side: int = 192) -> np.ndarray:
    data = np.asarray(volume)
    if data.ndim != 3:
        raise VisualError(f"Expected a 3D volume, got shape {data.shape}.")
    stride = max(1, math.ceil(max(data.shape) / max_side))
    return np.asarray(data[::stride, ::stride, ::stride], dtype=np.float32)


def _encode_figure(
    figure: Figure,
    *,
    max_side_px: int,
    max_png_bytes: int,
) -> tuple[bytes, int, int]:
    buffer = BytesIO()
    FigureCanvasAgg(figure)
    figure.savefig(buffer, format="png", dpi=120, bbox_inches="tight", pad_inches=0.12)
    figure.clear()

    image = PILImage.open(BytesIO(buffer.getvalue())).convert("RGB")
    if max(image.size) > max_side_px:
        image.thumbnail((max_side_px, max_side_px), PILImage.Resampling.LANCZOS)

    def save_png(current: PILImage.Image) -> bytes:
        output = BytesIO()
        current.save(output, format="PNG", optimize=True, compress_level=9)
        return output.getvalue()

    encoded = save_png(image)
    while len(encoded) > max_png_bytes and max(image.size) > 900:
        width = max(1, int(image.width * 0.84))
        height = max(1, int(image.height * 0.84))
        image = image.resize((width, height), PILImage.Resampling.LANCZOS)
        encoded = save_png(image)

    if len(encoded) > max_png_bytes:
        # Palette quantisation is a final bounded-output fallback. It preserves labels,
        # contours, and gross image morphology while preventing enormous base64 payloads.
        encoded = save_png(image.quantize(colors=256).convert("RGB"))

    if len(encoded) > max_png_bytes:
        raise VisualError(
            f"Rendered PNG is {len(encoded):,} bytes, above the configured limit "
            f"of {max_png_bytes:,}; request fewer panels or a smaller image."
        )
    return encoded, image.width, image.height


def _finish(
    figure: Figure,
    metadata: dict[str, Any],
    settings: Settings,
) -> RenderedVisual:
    png, width, height = _encode_figure(
        figure,
        max_side_px=settings.visual_max_side_px,
        max_png_bytes=settings.visual_max_png_bytes,
    )
    final = dict(metadata)
    final["image"] = {
        "mime_type": "image/png",
        "width_px": width,
        "height_px": height,
        "png_bytes": len(png),
        "sha256": hashlib.sha256(png).hexdigest(),
    }
    return RenderedVisual(png=png, metadata=final)


def _parse_image_reference(reference: str) -> tuple[int | None, str]:
    if "@" not in reference:
        return None, reference
    prefix, path = reference.split("@", 1)
    try:
        index = int(prefix)
    except ValueError as exc:
        raise VisualError(f"Invalid RELION image reference {reference!r}.") from exc
    if index < 1:
        raise VisualError(f"RELION image references are one-based: {reference!r}.")
    return index - 1, path


def _metric_text(value: float | None, *, suffix: str = "", precision: int = 3) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{precision}g}{suffix}"


def _sort_indices(
    count: int,
    sort_by: Literal["index", "occupancy", "resolution", "score"],
    occupancy: Sequence[float | None],
    resolution: Sequence[float | None],
    score: Sequence[float | None],
) -> list[int]:
    indices = list(range(count))
    if sort_by == "index":
        return indices

    values = {
        "occupancy": occupancy,
        "resolution": resolution,
        "score": score,
    }[sort_by]

    def key(index: int) -> tuple[int, float, int]:
        value = values[index] if index < len(values) else None
        if value is None:
            return (1, 0.0, index)
        if sort_by == "resolution":
            return (0, float(value), index)
        return (0, -float(value), index)

    return sorted(indices, key=key)


def render_smoke_test(
    settings: Settings,
    *,
    challenge_id: str,
    labels: Mapping[str, str],
) -> RenderedVisual:
    """Return a randomized visual challenge that proves image pixels reach the model."""
    required = ("top_left", "top_right", "bottom_left", "bottom_right")
    missing = [key for key in required if key not in labels]
    if missing:
        raise VisualError(f"Smoke-test labels are missing positions: {missing}")

    figure = Figure(figsize=(8, 5), constrained_layout=True)
    axis = figure.add_subplot(1, 1, 1)
    image = np.zeros((400, 640, 3), dtype=np.uint8)
    image[:200, :320] = (235, 235, 235)
    image[:200, 320:] = (80, 80, 80)
    image[200:, :320] = (145, 145, 145)
    image[200:, 320:] = (200, 200, 200)
    axis.imshow(image)
    panels = [
        (160, 100, "TOP LEFT", labels["top_left"], "black"),
        (480, 100, "TOP RIGHT", labels["top_right"], "white"),
        (160, 300, "BOTTOM LEFT", labels["bottom_left"], "black"),
        (480, 300, "BOTTOM RIGHT", labels["bottom_right"], "black"),
    ]
    for x, y, position, token, color in panels:
        axis.text(
            x,
            y,
            f"{position}\n{token}",
            ha="center",
            va="center",
            fontsize=18,
            weight="bold",
            color=color,
        )
    axis.set_title("RELION MCP randomized image-content challenge", fontsize=16)
    axis.axis("off")
    return _finish(
        figure,
        {
            "report_type": "visual_smoke_test",
            "challenge_id": challenge_id,
            "required_response_keys": list(required),
            "verification_prompt": (
                "Read the four random tokens from the attached image, map them to the required response keys, "
                "then call relion_verify_visual_smoke_test. The tokens are not present in this metadata."
            ),
        },
        settings,
    )


def render_2d_classes(
    settings: Settings,
    project_dir: str,
    model_star: str,
    *,
    classes_mrcs: str | None = None,
    selected_class_numbers: Sequence[int] | None = None,
    max_classes: int = 64,
    columns: int = 8,
    sort_by: Literal["index", "occupancy", "resolution", "score"] = "index",
    low_percentile: float = 1.0,
    high_percentile: float = 99.0,
) -> RenderedVisual:
    project = settings.validate_project_path(project_dir)
    star_path = _project_file(settings, project_dir, model_star, suffixes=(".star",))
    model_sha256 = _file_sha256(star_path)
    blocks = parse_star(star_path)
    block_name, block = _find_block(blocks, any_of=("rlnReferenceImage", "rlnClassNumber"))

    references = _as_list(block.get("rlnReferenceImage"))
    explicit_stack: Path | None = None
    if classes_mrcs is not None:
        explicit_stack = _project_file(
            settings,
            project_dir,
            classes_mrcs,
            base_dir=star_path.parent,
            suffixes=(".mrcs", ".mrc"),
        )

    if references:
        count = len(references)
    elif explicit_stack is not None:
        with mrcfile.mmap(explicit_stack, permissive=True, mode="r") as handle:
            count = int(handle.data.shape[0]) if handle.data.ndim == 3 else 1
        references = [f"{index + 1}@{_relative(project, explicit_stack)}" for index in range(count)]
    else:
        raise VisualError("The model STAR has no rlnReferenceImage column; provide classes_mrcs explicitly.")

    if count < 1:
        raise VisualError("No 2D classes were found.")
    if not (1 <= max_classes <= settings.visual_max_panels):
        raise VisualError(f"max_classes must be between 1 and {settings.visual_max_panels}.")
    if not (1 <= columns <= 16):
        raise VisualError("columns must be between 1 and 16.")

    occupancy = _numeric_column(block, "rlnClassDistribution", count)
    resolution = _numeric_column(block, "rlnEstimatedResolution", count)
    score_keys = ("rlnClassScore", "rlnClassRankerScore", "rlnClassScore2D")
    score_key = next((key for key in score_keys if key in block), score_keys[0])
    score = _numeric_column(block, score_key, count)

    indices = _sort_indices(count, sort_by, occupancy, resolution, score)
    if selected_class_numbers is not None:
        if not selected_class_numbers or any(type(number) is not int for number in selected_class_numbers):
            raise VisualError("Supply a nonempty list of integer class numbers.")
        requested = {number - 1 for number in selected_class_numbers}
        invalid = sorted(number + 1 for number in requested if number < 0 or number >= count)
        if invalid:
            raise VisualError(f"Unknown 2D class number(s): {invalid}")
        indices = [index for index in indices if index in requested]
    candidate_indices = indices
    indices = indices[:max_classes]
    if not indices:
        raise VisualError("No classes remain after filtering.")

    effective_columns = min(columns, len(indices))
    rows = math.ceil(len(indices) / effective_columns)
    figure = Figure(figsize=(2.15 * effective_columns, 2.35 * rows + 0.6), constrained_layout=True)
    axes = figure.subplots(rows, effective_columns, squeeze=False)
    records: list[dict[str, Any]] = []
    source_hashes: dict[str, str] = {}

    with ExitStack() as stack:
        handles: dict[Path, mrcfile.mrcfile.MrcFile] = {}
        for panel, class_index in enumerate(indices):
            axis = axes[panel // effective_columns][panel % effective_columns]
            ref_index, ref_path_raw = _parse_image_reference(references[class_index])
            stack_path = explicit_stack or _project_file(
                settings,
                project_dir,
                ref_path_raw,
                base_dir=star_path.parent,
                suffixes=(".mrcs", ".mrc"),
            )
            if stack_path not in handles:
                handles[stack_path] = stack.enter_context(mrcfile.mmap(stack_path, permissive=True, mode="r"))
                source_hashes[_relative(project, stack_path)] = _file_sha256(stack_path)
            handle = handles[stack_path]
            data = handle.data
            if data.ndim == 2:
                if ref_index not in {None, 0}:
                    raise VisualError(
                        f"Class {class_index + 1} requests image {ref_index + 1}, but "
                        f"{stack_path.name} is a single 2D image."
                    )
                image = data
                resolved_index = 0
            elif data.ndim == 3:
                resolved_index = ref_index if ref_index is not None else class_index
                if resolved_index < 0 or resolved_index >= data.shape[0]:
                    raise VisualError(
                        f"Class {class_index + 1} references image {resolved_index + 1}, but "
                        f"{stack_path.name} contains {data.shape[0]} images."
                    )
                image = data[resolved_index]
            else:
                raise VisualError(f"Expected a 2D class stack, got shape {data.shape} in {stack_path}.")

            displayed, original_shape = _downsample_2d(image, max_side=420)
            vmin, vmax = _display_limits(displayed, low_percentile, high_percentile)
            axis.imshow(displayed, cmap="gray", origin="upper", vmin=vmin, vmax=vmax)
            title = (
                f"Class {class_index + 1:03d}\n"
                f"p={_metric_text(occupancy[class_index])}  "
                f"res={_metric_text(resolution[class_index], suffix=' Å')}\n"
                f"score={_metric_text(score[class_index])}"
            )
            axis.set_title(title, fontsize=8)
            axis.axis("off")
            records.append(
                {
                    "class_number": class_index + 1,
                    "reference": references[class_index],
                    "stack": _relative(project, stack_path),
                    "stack_index": resolved_index + 1,
                    "shape_yx": list(original_shape),
                    "occupancy": occupancy[class_index],
                    "estimated_resolution_A": resolution[class_index],
                    "score": score[class_index],
                }
            )

    if _file_sha256(star_path) != model_sha256 or any(
        _file_sha256(path) != source_hashes[_relative(project, path)] for path in handles
    ):
        raise VisualError("Class source changed during rendering; render the completed job again.")
    for panel in range(len(indices), rows * effective_columns):
        axes[panel // effective_columns][panel % effective_columns].axis("off")
    figure.suptitle(
        f"RELION 2D classes — {star_path.name} — sorted by {sort_by}",
        fontsize=14,
    )
    return _finish(
        figure,
        {
            "report_type": "relion_2d_classes",
            "project_dir": str(project),
            "model_star": _relative(project, star_path),
            "model_sha256": model_sha256,
            "source_files": source_hashes,
            "star_block": block_name,
            "score_column": score_key if score_key in block else None,
            "total_classes": count,
            "rendered_classes": records,
            "coverage": {
                "rendered_class_numbers": [index + 1 for index in indices],
                "missing_class_numbers": [index + 1 for index in range(count) if index not in indices],
                "requested_class_numbers": [index + 1 for index in candidate_indices],
                "complete": len(indices) == count,
                "note": "Rendered coverage records available images, not a scientific review or acceptance.",
            },
            "display": {"low_percentile": low_percentile, "high_percentile": high_percentile},
            "scientific_note": (
                "Use the attached class-average pixels together with occupancy, ranking, retained view diversity, "
                "and downstream validation. Do not select from scalar metrics alone."
            ),
        },
        settings,
    )


def render_2d_class_pages(
    settings: Settings,
    project_dir: str,
    model_star: str,
    **kwargs: Any,
) -> list[RenderedVisual]:
    """Render every requested class in bounded sheets, preserving sort order."""
    first = render_2d_classes(settings, project_dir, model_star, **kwargs)
    requested = first.metadata["coverage"]["requested_class_numbers"]
    page_size = kwargs.get("max_classes", 64)
    page_count = math.ceil(len(requested) / page_size)
    if page_count > 16:
        raise VisualError("More than 16 class sheets requested; use explicit class subsets and validate combined coverage.")
    pages = [first]
    source_hashes = dict(first.metadata["source_files"])
    for start in range(page_size, len(requested), page_size):
        page_kwargs = {**kwargs, "selected_class_numbers": requested[start:start + page_size]}
        page = render_2d_classes(settings, project_dir, model_star, **page_kwargs)
        if page.metadata["model_sha256"] != first.metadata["model_sha256"]:
            raise VisualError("Class model changed while rendering pages; render the completed job again.")
        for path, fingerprint in page.metadata["source_files"].items():
            if path in source_hashes and source_hashes[path] != fingerprint:
                raise VisualError("Class stack changed while rendering pages; render the completed job again.")
            source_hashes[path] = fingerprint
        pages.append(page)
    if any(
        _file_sha256(_project_file(settings, project_dir, path, suffixes=(".mrcs", ".mrc"))) != fingerprint
        for path, fingerprint in source_hashes.items()
    ):
        raise VisualError("Class stack changed while rendering pages; render the completed job again.")
    seen = sorted(record["class_number"] for page in pages for record in page.metadata["rendered_classes"])
    for index, page in enumerate(pages):
        page.metadata["pagination"] = {
            "page": index + 1, "page_count": len(pages),
            "combined_rendered_class_numbers": seen,
            "complete": len(seen) == first.metadata["total_classes"],
        }
    return pages


def validate_2d_class_review(
    settings: Settings,
    project_dir: str,
    model_star: str,
    rendered_pages: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Check source-bound rendering coverage; do not claim images were inspected."""
    project = settings.validate_project_path(project_dir)
    source = _project_file(settings, project_dir, model_star, suffixes=(".star",))
    fingerprint = _file_sha256(source)
    _, block = _find_block(parse_star(source), required=("rlnReferenceImage",))
    references = _as_list(block["rlnReferenceImage"])
    count = len(references)
    seen: set[int] = set()
    issues: list[str] = []
    source_hashes: dict[str, str] = {}
    stack_dimensions: dict[str, int] = {}
    for page in rendered_pages:
        if (page.get("report_type") != "relion_2d_classes"
                or page.get("model_sha256") != fingerprint
                or page.get("model_star") != _relative(project, source)
                or page.get("project_dir") != str(project)):
            issues.append("Page does not match this project and current class model.")
            continue
        files = page.get("source_files")
        records = page.get("rendered_classes")
        if not isinstance(files, dict) or not files or not isinstance(records, list):
            issues.append("Page is missing class records or source fingerprints.")
            continue
        page_valid = True
        for path, expected_hash in files.items():
            actual = _project_file(settings, project_dir, path, suffixes=(".mrcs", ".mrc"))
            if path not in source_hashes:
                source_hashes[path] = _file_sha256(actual)
                with mrcfile.mmap(actual, permissive=True, mode="r") as handle:
                    stack_dimensions[path] = handle.data.ndim
            if source_hashes[path] != expected_hash:
                issues.append(f"Class stack changed since rendering: {path}")
                page_valid = False
        for record in records:
            number = record.get("class_number") if isinstance(record, dict) else None
            if type(number) is not int or not 1 <= number <= count:
                issues.append("Page contains an invalid class number.")
            elif page_valid:
                if record.get("stack") not in files:
                    issues.append(f"Class {number} has no source fingerprint.")
                elif record.get("reference") != references[number - 1]:
                    issues.append(f"Class {number} reference does not match the model.")
                else:
                    image_index, reference_path = _parse_image_reference(references[number - 1])
                    expected_stack = _project_file(
                        settings, project_dir, reference_path,
                        base_dir=source.parent, suffixes=(".mrcs", ".mrc"),
                    )
                    stack_name = record["stack"]
                    expected_index = (image_index + 1 if image_index is not None else
                                      1 if stack_dimensions[stack_name] == 2 else number)
                    if stack_name != _relative(project, expected_stack) or record.get("stack_index") != expected_index:
                        issues.append(f"Class {number} rendered stack or image index does not match the model.")
                    else:
                        seen.add(number)
    if _file_sha256(source) != fingerprint or any(
        _file_sha256(_project_file(settings, project_dir, path, suffixes=(".mrcs", ".mrc"))) != fingerprint
        for path, fingerprint in source_hashes.items()
    ):
        issues.append("A class source changed during coverage validation; render it again.")
    missing = sorted(set(range(1, count + 1)) - seen)
    return {
        "report_type": "relion_2d_class_review_coverage", "model_star": _relative(project, source),
        "model_sha256": fingerprint, "total_classes": count,
        "rendered_class_numbers": sorted(seen), "missing_class_numbers": missing,
        "valid": bool(count) and not missing and not issues, "issues": issues,
        "scientific_note": "Coverage validates supplied rendering provenance only. Inspect every image and record scientific decisions separately.",
    }


def _class_stack_pixel_size_A(
    handle: mrcfile.mrcfile.MrcFile,
    *,
    stack_path: Path,
) -> float:
    """Return class-image XY sampling, deliberately ignoring the MRCS stack axis."""
    try:
        pixel_x = float(handle.voxel_size.x)
        pixel_y = float(handle.voxel_size.y)
    except (AttributeError, TypeError, ValueError) as exc:
        raise VisualError(
            f"2D class stack {stack_path.name} has no usable XY pixel-size metadata."
        ) from exc
    if (
        not math.isfinite(pixel_x)
        or not math.isfinite(pixel_y)
        or pixel_x <= 0
        or pixel_y <= 0
        or not math.isclose(pixel_x, pixel_y, rel_tol=1e-5, abs_tol=1e-5)
    ):
        raise VisualError(
            f"2D class stack {stack_path.name} requires finite isotropic XY sampling; "
            f"got ({pixel_x!r}, {pixel_y!r}) A."
        )
    return pixel_x


def _model_general_sampling(
    blocks: Mapping[str, Mapping[str, Any]],
) -> tuple[str | None, float | None, int | None]:
    """Read optional RELION model-level sampling declarations fail-closed."""
    for block_name, block in blocks.items():
        if "rlnPixelSize" not in block and "rlnOriginalImageSize" not in block:
            continue
        pixel_size_A: float | None = None
        box_size_pix: int | None = None
        if "rlnPixelSize" in block:
            values = _as_list(block["rlnPixelSize"])
            try:
                pixel_size_A = float(values[0])
            except (IndexError, TypeError, ValueError) as exc:
                raise VisualError("The class model has an invalid rlnPixelSize.") from exc
            if not math.isfinite(pixel_size_A) or pixel_size_A <= 0:
                raise VisualError("The class model has an invalid rlnPixelSize.")
        if "rlnOriginalImageSize" in block:
            values = _as_list(block["rlnOriginalImageSize"])
            try:
                declared_box = float(values[0])
            except (IndexError, TypeError, ValueError) as exc:
                raise VisualError(
                    "The class model has an invalid rlnOriginalImageSize."
                ) from exc
            if (
                not math.isfinite(declared_box)
                or declared_box <= 0
                or not declared_box.is_integer()
            ):
                raise VisualError(
                    "The class model has an invalid rlnOriginalImageSize."
                )
            box_size_pix = int(declared_box)
        return block_name, pixel_size_A, box_size_pix
    return None, None, None


def _projection_overlay(class_image: np.ndarray, projection: np.ndarray) -> np.ndarray:
    """Encode class pixels as magenta and projection pixels as green (agreement is white)."""
    class_display = np.clip((np.asarray(class_image) + 3.0) / 6.0, 0.0, 1.0)
    projection_display = np.clip((np.asarray(projection) + 3.0) / 6.0, 0.0, 1.0)
    return np.stack(
        (class_display, projection_display, class_display), axis=-1
    ).astype(np.float32)


def render_map_projection_comparison(
    settings: Settings,
    project_dir: str,
    map_path: str,
    model_star: str,
    selected_class_numbers: Sequence[int],
    comparison_lowpass_A: float,
    particle_diameter_A: float,
    *,
    classes_mrcs: str | None = None,
    direction_count: int = 192,
    inplane_step_deg: float = 10.0,
    max_shift_A: float = 12.0,
    invert_class_contrast: bool = False,
) -> RenderedVisual:
    """Render orientation-matched map projections beside explicit 2D class averages."""
    project = settings.validate_project_path(project_dir)
    source_map = _project_file(
        settings, project_dir, map_path, suffixes=(".mrc", ".mrcs")
    )
    source_model = _project_file(
        settings, project_dir, model_star, suffixes=(".star",)
    )
    if (
        not isinstance(selected_class_numbers, Sequence)
        or isinstance(selected_class_numbers, (str, bytes))
        or not selected_class_numbers
    ):
        raise VisualError("Supply a nonempty list of explicit 2D class numbers.")
    if any(type(number) is not int for number in selected_class_numbers):
        raise VisualError("Selected 2D class numbers must be integers.")
    if len(set(selected_class_numbers)) != len(selected_class_numbers):
        raise VisualError("Selected 2D class numbers must be unique.")
    if len(selected_class_numbers) > MAX_PROJECTION_COMPARISON_CLASSES:
        raise VisualError(
            f"Compare at most {MAX_PROJECTION_COMPARISON_CLASSES} classes per projection report."
        )

    map_sha256 = _file_sha256(source_map)
    model_sha256 = _file_sha256(source_model)
    blocks = parse_star(source_model)
    block_name, class_block = _find_block(blocks, required=("rlnReferenceImage",))
    general_block_name, model_pixel_size_A, model_box_size_pix = (
        _model_general_sampling(blocks)
    )
    references = _as_list(class_block.get("rlnReferenceImage"))
    class_count = len(references)
    invalid = sorted(
        number
        for number in selected_class_numbers
        if number < 1 or number > class_count
    )
    if invalid:
        raise VisualError(f"Unknown 2D class number(s): {invalid}")
    selected_indices = [number - 1 for number in selected_class_numbers]
    occupancy = _numeric_column(class_block, "rlnClassDistribution", class_count)
    resolution = _numeric_column(class_block, "rlnEstimatedResolution", class_count)

    explicit_stack = (
        _project_file(
            settings,
            project_dir,
            classes_mrcs,
            base_dir=source_model.parent,
            suffixes=(".mrcs", ".mrc"),
        )
        if classes_mrcs is not None
        else None
    )
    class_records: list[dict[str, Any]] = []
    class_images: list[np.ndarray] = []
    stack_hashes: dict[str, str] = {}
    stack_pixel_sizes: dict[Path, float] = {}
    stack_shapes: dict[Path, tuple[int, ...]] = {}
    filter_metadata: dict[str, Any] | None = None
    with ExitStack() as stack:
        handles: dict[Path, mrcfile.mrcfile.MrcFile] = {}
        for class_index in selected_indices:
            reference_index, reference_path = _parse_image_reference(
                references[class_index]
            )
            stack_path = explicit_stack or _project_file(
                settings,
                project_dir,
                reference_path,
                base_dir=source_model.parent,
                suffixes=(".mrcs", ".mrc"),
            )
            if stack_path not in handles:
                stack_file_bytes = stack_path.stat().st_size
                if stack_file_bytes > _PROJECTION_COMPARISON_MAX_STACK_FILE_BYTES:
                    raise VisualError(
                        f"Projection-comparison class stack {stack_path.name} is "
                        f"{stack_file_bytes:,} bytes, above the "
                        f"{_PROJECTION_COMPARISON_MAX_STACK_FILE_BYTES:,}-byte cap."
                    )
                handles[stack_path] = stack.enter_context(
                    mrcfile.mmap(stack_path, permissive=True, mode="r")
                )
                relative_stack = _relative(project, stack_path)
                stack_hashes[relative_stack] = _file_sha256(stack_path)
                stack_pixel_sizes[stack_path] = _class_stack_pixel_size_A(
                    handles[stack_path], stack_path=stack_path
                )
                stack_shapes[stack_path] = tuple(
                    int(item) for item in handles[stack_path].data.shape
                )
            handle = handles[stack_path]
            data = handle.data
            if data.ndim == 2:
                if reference_index not in {None, 0}:
                    raise VisualError(
                        f"Class {class_index + 1} requests image {reference_index + 1}, "
                        f"but {stack_path.name} is a single 2D image."
                    )
                resolved_index = 0
                pixels = np.asarray(data, dtype=np.float32)
            elif data.ndim == 3:
                resolved_index = (
                    reference_index if reference_index is not None else class_index
                )
                if not 0 <= resolved_index < data.shape[0]:
                    raise VisualError(
                        f"Class {class_index + 1} references image {resolved_index + 1}, "
                        f"but {stack_path.name} contains {data.shape[0]} images."
                    )
                pixels = np.asarray(data[resolved_index], dtype=np.float32)
            else:
                raise VisualError(
                    f"Expected a 2D class stack, got shape {data.shape} in {stack_path}."
                )
            if pixels.ndim != 2 or pixels.shape[0] != pixels.shape[1]:
                raise VisualError(
                    f"Class {class_index + 1} must be a square 2D image; got {pixels.shape}."
                )
            if pixels.shape[0] > _PROJECTION_COMPARISON_MAX_CLASS_SIDE:
                raise VisualError(
                    f"Class {class_index + 1} side length {pixels.shape[0]} exceeds the "
                    f"{_PROJECTION_COMPARISON_MAX_CLASS_SIDE}-pixel comparison cap."
                )
            if not np.all(np.isfinite(pixels)) or float(np.max(pixels)) <= float(
                np.min(pixels)
            ):
                raise VisualError(
                    f"Class {class_index + 1} contains non-finite or constant pixels."
                )
            filtered, item_filter = _apply_2d_display_lowpass(
                pixels,
                stack_pixel_sizes[stack_path],
                comparison_lowpass_A,
            )
            if filter_metadata is None:
                filter_metadata = item_filter
            class_images.append(filtered)
            class_records.append(
                {
                    "class_number": class_index + 1,
                    "reference": references[class_index],
                    "stack": _relative(project, stack_path),
                    "stack_index": resolved_index + 1,
                    "source_shape_yx": list(pixels.shape),
                    "pixel_size_A_xy": [
                        stack_pixel_sizes[stack_path],
                        stack_pixel_sizes[stack_path],
                    ],
                    "field_of_view_A_xy": [
                        pixels.shape[1] * stack_pixel_sizes[stack_path],
                        pixels.shape[0] * stack_pixel_sizes[stack_path],
                    ],
                    "occupancy": occupancy[class_index],
                    "estimated_resolution_A": resolution[class_index],
                }
            )

        if len({round(value, 7) for value in stack_pixel_sizes.values()}) != 1:
            raise VisualError("Selected class stacks do not share one physical XY pixel size.")
        if len({tuple(image.shape) for image in class_images}) != 1:
            raise VisualError("Selected class averages do not share one image shape.")
        if model_pixel_size_A is not None and any(
            not math.isclose(
                value, model_pixel_size_A, rel_tol=1e-4, abs_tol=1e-5
            )
            for value in stack_pixel_sizes.values()
        ):
            raise VisualError(
                "The class stack XY sampling disagrees with the model STAR rlnPixelSize."
            )
        if model_box_size_pix is not None and any(
            image.shape != (model_box_size_pix, model_box_size_pix)
            for image in class_images
        ):
            raise VisualError(
                "The class stack image shape disagrees with the model STAR rlnOriginalImageSize."
            )
        if _file_sha256(source_model) != model_sha256 or any(
            _file_sha256(path) != stack_hashes[_relative(project, path)]
            for path in handles
        ):
            raise VisualError(
                "A 2D class source changed during projection comparison; render it again."
            )

    _check_volume_render_size(source_map, f"Projection-comparison map {source_map.name}")
    with mrcfile.mmap(source_map, permissive=True, mode="r") as handle:
        if handle.data.ndim != 3:
            raise VisualError(
                f"Projection comparison requires one 3D map; got {handle.data.shape}."
            )
        map_volume = np.asarray(handle.data, dtype=np.float32)
        map_info = _mrc_info(handle, source_map)
    map_pixel_size_A = _validate_symmetry_scan_grid(
        map_info, "Map projection comparison"
    )
    if not np.all(np.isfinite(map_volume)) or float(np.max(map_volume)) <= float(
        np.min(map_volume)
    ):
        raise VisualError("Projection-comparison map contains non-finite or constant density.")
    filtered_map, map_filter = _apply_display_lowpass(
        map_volume, map_info, comparison_lowpass_A
    )
    canonical_map, transpose_axes = _canonical_physical_zyx(filtered_map, map_info)
    class_pixel_size_A = next(iter(stack_pixel_sizes.values()))
    try:
        comparison = compare_map_projections(
            canonical_map,
            np.stack(class_images).astype(np.float32),
            map_pixel_size_A=map_pixel_size_A,
            class_pixel_size_A=class_pixel_size_A,
            comparison_lowpass_A=comparison_lowpass_A,
            particle_diameter_A=particle_diameter_A,
            direction_count=direction_count,
            inplane_step_deg=inplane_step_deg,
            max_shift_A=max_shift_A,
            invert_class_contrast=invert_class_contrast,
        )
    except ProjectionComparisonError as exc:
        raise VisualError(str(exc)) from exc

    if _file_sha256(source_map) != map_sha256:
        raise VisualError(
            "The map source changed during projection comparison; render it again."
        )
    if _file_sha256(source_model) != model_sha256 or any(
        _file_sha256(path) != stack_hashes[_relative(project, path)]
        for path in stack_pixel_sizes
    ):
        raise VisualError(
            "A 2D class source changed during projection comparison; render it again."
        )
    rows = len(comparison.matches)
    figure = Figure(figsize=(12.8, 3.0 * rows + 0.9), constrained_layout=True)
    axes = figure.subplots(rows, 4, squeeze=False)
    output_matches: list[dict[str, Any]] = []
    for row, (match, record) in enumerate(
        zip(comparison.matches, class_records, strict=True)
    ):
        panels = (
            (match.class_image, "2D class average", "gray", -3.0, 3.0),
            (
                match.matched_projection,
                "best synthetic projection",
                "gray",
                -3.0,
                3.0,
            ),
            (
                _projection_overlay(match.class_image, match.matched_projection),
                "overlay: class magenta / projection green",
                None,
                None,
                None,
            ),
            (
                match.signed_difference,
                "signed residual: class − projection",
                "coolwarm",
                -3.0,
                3.0,
            ),
        )
        for column, (image, title, cmap, vmin, vmax) in enumerate(panels):
            axes[row][column].imshow(
                image,
                cmap=cmap,
                origin="upper",
                vmin=vmin,
                vmax=vmax,
            )
            axes[row][column].add_patch(
                Circle(
                    ((comparison.work_box - 1) / 2.0,) * 2,
                    radius=particle_diameter_A
                    / (2.0 * comparison.work_pixel_size_A),
                    fill=False,
                    color="cyan",
                    linewidth=0.55,
                    alpha=0.8,
                )
            )
            axes[row][column].set_title(title, fontsize=8)
            axes[row][column].axis("off")
        axes[row][0].text(
            -0.05,
            0.5,
            f"STAR row {record['class_number']:03d}\n"
            f"source image {record['stack_index']:03d}\n"
            f"p={_metric_text(record['occupancy'])}\n"
            f"res={_metric_text(record['estimated_resolution_A'], suffix=' Å')}",
            transform=axes[row][0].transAxes,
            ha="right",
            va="center",
            fontsize=8,
        )
        axes[row][1].text(
            0.5,
            -0.04,
            f"dir xyz=({match.direction_xyz[0]:+.2f}, "
            f"{match.direction_xyz[1]:+.2f}, {match.direction_xyz[2]:+.2f})  "
            f"in-plane={match.inplane_rotation_deg:.1f}°  "
            f"shift y/x=({match.shift_yx_A[0]:+.1f}, {match.shift_yx_A[1]:+.1f}) Å\n"
            f"NCC={match.displayed_ncc:.3f}  "
            f"runner-up margin={_metric_text(match.runner_up_margin)}",
            transform=axes[row][1].transAxes,
            ha="center",
            va="top",
            fontsize=7,
        )
        output_matches.append(
            {
                **record,
                "best_direction_index": match.direction_index,
                "best_direction_xyz": list(match.direction_xyz),
                "inplane_rotation_deg": match.inplane_rotation_deg,
                "shift_yx_pix": list(match.shift_yx_pix),
                "shift_yx_A": list(match.shift_yx_A),
                "search_ncc": match.search_ncc,
                "displayed_pair_ncc": match.displayed_ncc,
                "runner_up_ncc": match.runner_up_ncc,
                "runner_up_margin": match.runner_up_margin,
                "runner_up_direction_xyz": (
                    list(match.runner_up_direction_xyz)
                    if match.runner_up_direction_xyz is not None
                    else None
                ),
            }
        )

    pairwise_directions: list[dict[str, Any]] = []
    for left in range(len(comparison.matches)):
        for right in range(left + 1, len(comparison.matches)):
            left_direction = np.asarray(comparison.matches[left].direction_xyz)
            right_direction = np.asarray(comparison.matches[right].direction_xyz)
            separation = math.degrees(
                math.acos(
                    float(
                        np.clip(np.dot(left_direction, right_direction), -1.0, 1.0)
                    )
                )
            )
            pairwise_directions.append(
                {
                    "class_a": class_records[left]["class_number"],
                    "class_b": class_records[right]["class_number"],
                    "best_viewing_direction_separation_deg": separation,
                }
            )

    analysis_payload = {
        "schema_version": PROJECTION_COMPARISON_SCHEMA_VERSION,
        "map_sha256": map_sha256,
        "model_sha256": model_sha256,
        "stack_sha256": stack_hashes,
        "selected_class_numbers": list(selected_class_numbers),
        "comparison_lowpass_A": float(comparison_lowpass_A),
        "particle_diameter_A": float(particle_diameter_A),
        "direction_count": comparison.direction_count,
        "inplane_step_deg": comparison.actual_inplane_step_deg,
        "max_shift_A": comparison.searched_max_shift_A,
        "invert_class_contrast": bool(invert_class_contrast),
        "matches": [
            {
                "class_number": item["class_number"],
                "direction_xyz": item["best_direction_xyz"],
                "inplane_rotation_deg": item["inplane_rotation_deg"],
                "shift_yx_pix": item["shift_yx_pix"],
                "displayed_pair_ncc": item["displayed_pair_ncc"],
            }
            for item in output_matches
        ],
    }
    analysis_id = hashlib.sha256(
        json.dumps(
            analysis_payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()
    figure.suptitle(
        f"RELION map projections vs 2D classes — {comparison_lowpass_A:g} Å common low-pass — "
        f"{comparison.work_pixel_size_A:.3g} Å/px analysis grid",
        fontsize=13,
    )
    assert filter_metadata is not None
    return _finish(
        figure,
        {
            "schema_version": PROJECTION_COMPARISON_SCHEMA_VERSION,
            "report_type": "relion_map_projection_comparison",
            "analysis_id": analysis_id,
            "project_dir": str(project),
            "map": {
                "path": _relative(project, source_map),
                "sha256": map_sha256,
                "source_shape_section_row_column": list(map_info.shape),
                "canonical_shape_zyx": list(canonical_map.shape),
                "voxel_size_A_xyz": list(map_info.voxel_size_A or ()),
                "axis_mapping_mapc_mapr_maps": list(map_info.axis_mapping),
                "canonical_transpose_axes": list(transpose_axes),
            },
            "class_model": {
                "path": _relative(project, source_model),
                "sha256": model_sha256,
                "star_block": block_name,
                "general_star_block": general_block_name,
                "total_rows": class_count,
                "declared_pixel_size_A": model_pixel_size_A,
                "declared_original_image_size_pix": model_box_size_pix,
            },
            "class_stacks": [
                {
                    "path": path,
                    "sha256": fingerprint,
                    "shape_stack_y_x": list(
                        stack_shapes[
                            _project_file(
                                settings,
                                project_dir,
                                path,
                                suffixes=(".mrcs", ".mrc"),
                            )
                        ]
                    ),
                    "sampling_note": "Only MRC X/Y sampling describes 2D class pixels; the stack-axis Z sampling is ignored.",
                }
                for path, fingerprint in sorted(stack_hashes.items())
            ],
            "physical_compatibility": {
                "validated": True,
                "map_pixel_size_A": map_pixel_size_A,
                "class_pixel_size_A": class_pixel_size_A,
                "common_field_of_view_A": comparison.common_field_of_view_A,
                "source_box_size_pix": int(canonical_map.shape[0]),
                "analysis_box_size_pix": comparison.work_box,
                "analysis_pixel_size_A": comparison.work_pixel_size_A,
                "centering": "shared geometric box center plus bounded fitted 2D translation",
                "contrast_convention": (
                    "class averages inverted relative to map"
                    if invert_class_contrast
                    else "class averages use the same sign as map density"
                ),
            },
            "common_filter": {
                "map": map_filter,
                "class_averages": filter_metadata,
            },
            "search": {
                "viewing_directions": "deterministic cardinal-plus-Fibonacci full sphere",
                "direction_count": comparison.direction_count,
                "approximate_direction_spacing_deg": comparison.approximate_direction_spacing_deg,
                "requested_inplane_step_deg": comparison.requested_inplane_step_deg,
                "actual_inplane_step_deg": comparison.actual_inplane_step_deg,
                "inplane_rotation_count": comparison.inplane_rotation_count,
                "requested_max_shift_A": comparison.requested_max_shift_A,
                "searched_max_shift_pix": comparison.searched_max_shift_pix,
                "searched_max_shift_A": comparison.searched_max_shift_A,
                "particle_diameter_A": comparison.particle_diameter_A,
                "mask_soft_edge_A": comparison.mask_soft_edge_A,
                "runner_up_direction_exclusion_deg": comparison.runner_up_exclusion_deg,
                "interpolation": "linear",
                "score": "masked normalized cross-correlation after common filtering",
                "orientation_note": "Reported vectors describe the renderer's physical XYZ viewing directions; they are not RELION Euler angles.",
            },
            "resource_bounds": {
                "maximum_selected_classes": MAX_PROJECTION_COMPARISON_CLASSES,
                "maximum_class_side_pix": _PROJECTION_COMPARISON_MAX_CLASS_SIDE,
                "maximum_class_stack_file_bytes": _PROJECTION_COMPARISON_MAX_STACK_FILE_BYTES,
                "maximum_map_file_bytes": _VOLUME_RENDER_MAX_INPUT_FILE_BYTES,
            },
            "matches": output_matches,
            "pairwise_best_direction_separations": pairwise_directions,
            "acceptance_use": {
                "selected_class_count": len(output_matches),
                "minimum_required_distinct_2d_appearances": 3,
                "count_requirement_met": len(output_matches) >= 3,
                "automatic_match_verdict": False,
                "note": "The caller must directly inspect the pixels and establish that at least three selected classes are credible, distinct appearances from the seed population. Class number, NCC, or fitted direction alone does not establish a match.",
            },
            "scientific_note": (
                "This is an orientation-matched synthetic projection comparison, not the central-slab view from a map report. "
                "Both sides use one recorded physical low-pass, field of view, centering convention, mask, and explicit contrast convention. "
                "A high correlation has no universal acceptance threshold, does not establish biological identity or absolute hand, and remains part of one morphology evidence channel."
            ),
        },
        settings,
    )


def render_particle_boxes(
    settings: Settings,
    project_dir: str,
    particles_star: str,
    *,
    particle_numbers: Sequence[int] | None = None,
    max_particles: int = 24,
    columns: int = 6,
    low_percentile: float = 1.0,
    high_percentile: float = 99.0,
) -> RenderedVisual:
    """Inspect actual stored extraction boxes without aligning or normalizing them."""
    project = settings.validate_project_path(project_dir)
    source = _project_file(settings, project_dir, particles_star, suffixes=(".star",))
    particles_sha256 = _file_sha256(source)
    _, block = _find_block(parse_star(source), required=("rlnImageName",))
    references = _as_list(block["rlnImageName"])
    if not references:
        raise VisualError("The particle STAR has no images.")
    if not 1 <= max_particles <= min(settings.visual_max_panels, 96) or not 1 <= columns <= 16:
        raise VisualError("Invalid particle panel count or columns.")
    if particle_numbers is None:
        indices = np.linspace(0, len(references) - 1, min(max_particles, len(references)), dtype=int).tolist()
    else:
        if not particle_numbers or len(particle_numbers) > max_particles:
            raise VisualError("Supply between one and max_particles particle row numbers.")
        if any(type(number) is not int or not 1 <= number <= len(references) for number in particle_numbers):
            raise VisualError("Particle row numbers must be valid one-based integers.")
        if len(set(particle_numbers)) != len(particle_numbers):
            raise VisualError("Particle row numbers must be unique.")
        indices = [number - 1 for number in particle_numbers]
    ncols = min(columns, len(indices))
    nrows = math.ceil(len(indices) / ncols)
    figure = Figure(figsize=(2.6 * ncols, 2.8 * nrows + 0.6), constrained_layout=True)
    axes = figure.subplots(nrows, ncols, squeeze=False)
    records = []
    source_stats = {}
    with ExitStack() as stack:
        handles: dict[Path, Any] = {}
        for panel, row in enumerate(indices):
            image_index, path = _parse_image_reference(references[row])
            resolved = _project_file(settings, project_dir, path, base_dir=source.parent, suffixes=(".mrcs", ".mrc"))
            if resolved not in handles:
                info = resolved.stat()
                source_stats[resolved] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                handles[resolved] = stack.enter_context(mrcfile.mmap(resolved, permissive=True, mode="r"))
            data = handles[resolved].data
            if image_index is None:
                if data.ndim != 2:
                    raise VisualError("A particle stack reference requires a one-based image index.")
                image_index = 0
            if data.ndim == 2 and image_index == 0:
                pixels = data
            elif data.ndim == 3 and 0 <= image_index < len(data):
                pixels = data[image_index]
            else:
                raise VisualError(f"Invalid particle image reference: {references[row]}")
            if pixels.size > 4096 * 4096 or not np.isfinite(pixels).all():
                raise VisualError("Particle box is oversized or contains non-finite pixels.")
            values = np.array(pixels, dtype=np.float32, copy=True)
            displayed, shape = _downsample_2d(values, max_side=420)
            vmin, vmax = _display_limits(displayed, low_percentile, high_percentile)
            axis = axes.flat[panel]
            axis.imshow(displayed, cmap="gray", origin="upper", vmin=vmin, vmax=vmax)
            axis.set_title(f"Particle row {row + 1}\n{shape[1]} × {shape[0]} px", fontsize=9)
            axis.axis("off")
            records.append({
                "particle_number": row + 1, "image_name": references[row],
                "stack": _relative(project, resolved), "stack_index": image_index + 1,
                "shape_yx": list(shape),
                "pixels_sha256": hashlib.sha256(np.ascontiguousarray(values, dtype="<f4").tobytes()).hexdigest(),
                "mean": float(values.mean()), "stddev": float(values.std()),
                "minimum": float(values.min()), "maximum": float(values.max()),
                "display_limits": [float(vmin), float(vmax)],
            })
    if _file_sha256(source) != particles_sha256:
        raise VisualError("Particle STAR changed during rendering; render the completed extraction again.")
    for path, before in source_stats.items():
        info = path.stat()
        after = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if before != after:
            raise VisualError("Particle stack changed during rendering; render the completed extraction again.")
    for axis in axes.flat[len(indices):]:
        axis.axis("off")
    figure.suptitle(f"Stored RELION particle boxes — {source.name}")
    return _finish(figure, {
        "report_type": "relion_particle_boxes", "project_dir": str(project),
        "particles_star": _relative(project, source), "particles_sha256": particles_sha256,
        "total_particles": len(references), "rendered_particles": records,
        "sampling": "explicit_rows" if particle_numbers is not None else "evenly_spaced_rows",
        "display": {"low_percentile": low_percentile, "high_percentile": high_percentile},
        "scientific_note": "Pixels are read from the supplied stack without re-centering, inversion or normalization. Extraction may already have normalized them. Per-panel display scaling is for inspection; this row sample is not a dataset-wide quality estimate.",
    }, settings)


def _coordinate_rows(
    path: Path,
    *,
    settings: Settings,
    project_dir: str,
    micrograph_path: Path,
) -> tuple[np.ndarray, np.ndarray | None]:
    blocks = parse_star(path)
    _, block = _find_block(blocks, required=("rlnCoordinateX", "rlnCoordinateY"))
    x_values = _as_list(block.get("rlnCoordinateX"))
    y_values = _as_list(block.get("rlnCoordinateY"))
    if len(x_values) != len(y_values):
        raise VisualError(
            f"Coordinate columns have different lengths in {path}: "
            f"X={len(x_values)}, Y={len(y_values)}."
        )
    count = len(x_values)
    micrograph_values = _as_list(block.get("rlnMicrographName"))
    matching_rows: set[int] | None = None
    if "rlnMicrographName" in block:
        if len(micrograph_values) != count:
            raise VisualError(
                f"rlnMicrographName has {len(micrograph_values)} rows; expected {count} in {path}."
            )
        resolved_micrographs: dict[str, Path] = {}
        matching_rows = set()
        for index, name in enumerate(micrograph_values):
            if name not in resolved_micrographs:
                resolved_micrographs[name] = _project_file(
                    settings,
                    project_dir,
                    name,
                    base_dir=path.parent,
                    suffixes=(".mrc", ".mrcs"),
                )
            if resolved_micrographs[name] == micrograph_path:
                matching_rows.add(index)
    coordinates: list[tuple[float, float]] = []
    fom_values = _as_list(block.get("rlnAutopickFigureOfMerit"))
    foms: list[float] = []
    if fom_values and len(fom_values) != count:
        raise VisualError(
            f"rlnAutopickFigureOfMerit has {len(fom_values)} rows; expected {count} in {path}."
        )
    has_complete_fom = len(fom_values) == count and count > 0
    for index in range(count):
        if matching_rows is not None and index not in matching_rows:
            continue
        x = _as_float(x_values[index])
        y = _as_float(y_values[index])
        if x is None or y is None:
            raise VisualError(f"Coordinate row {index + 1} is not numeric in {path}.")
        coordinates.append((x, y))
        if has_complete_fom:
            value = _as_float(fom_values[index])
            if value is None:
                raise VisualError(f"FOM row {index + 1} is not numeric in {path}.")
            foms.append(value)
    coords = np.asarray(coordinates, dtype=np.float64).reshape((-1, 2))
    return coords, np.asarray(foms, dtype=np.float64) if has_complete_fom else None


def _pairs_from_index_star(
    settings: Settings,
    project_dir: str,
    index_star: Path,
) -> list[tuple[Path, Path]]:
    blocks = parse_star(index_star)
    coordinate_columns = (
        "rlnMicrographCoordinates",
        "rlnMicrographCoordinateFile",
        "rlnCoordinateFile",
    )
    _, block = _find_block(blocks, required=("rlnMicrographName",), any_of=coordinate_columns)
    micrographs = _as_list(block.get("rlnMicrographName"))
    coordinate_key = next(key for key in coordinate_columns if key in block)
    coordinates = _as_list(block.get(coordinate_key))
    if len(micrographs) != len(coordinates):
        raise VisualError(
            f"Micrograph and coordinate-file columns have different lengths in {index_star}: "
            f"micrographs={len(micrographs)}, coordinates={len(coordinates)}."
        )
    count = len(micrographs)
    result: list[tuple[Path, Path]] = []
    for index in range(count):
        result.append(
            (
                _project_file(
                    settings,
                    project_dir,
                    micrographs[index],
                    base_dir=index_star.parent,
                    suffixes=(".mrc", ".mrcs"),
                ),
                _project_file(
                    settings,
                    project_dir,
                    coordinates[index],
                    base_dir=index_star.parent,
                    suffixes=(".star",),
                ),
            )
        )
    return result


def _stratified_indices(values: Sequence[int], limit: int) -> list[int]:
    if len(values) <= limit:
        return list(range(len(values)))
    order = np.argsort(np.asarray(values, dtype=np.int64), kind="stable")
    positions = np.linspace(0, len(order) - 1, num=limit)
    selected = sorted({int(order[round(position)]) for position in positions})
    # Rounding can collapse positions for small arrays; fill deterministically.
    for index in range(len(values)):
        if len(selected) >= limit:
            break
        if index not in selected:
            selected.append(index)
    return sorted(selected[:limit])


def render_pick_overlays(
    settings: Settings,
    project_dir: str,
    *,
    pairs: Sequence[dict[str, str]] | None = None,
    coordinate_index_star: str | None = None,
    particle_diameter_pix: float,
    fom_threshold: float | None = None,
    max_micrographs: int = 9,
    columns: int = 3,
    low_percentile: float = 1.0,
    high_percentile: float = 99.0,
) -> RenderedVisual:
    project = settings.validate_project_path(project_dir)
    if particle_diameter_pix <= 0:
        raise VisualError("particle_diameter_pix must be positive.")
    if not (1 <= max_micrographs <= min(settings.visual_max_panels, 24)):
        raise VisualError("max_micrographs must be between 1 and 24.")
    if not (1 <= columns <= 6):
        raise VisualError("columns must be between 1 and 6.")
    if bool(pairs) == bool(coordinate_index_star):
        raise VisualError("Provide exactly one of pairs or coordinate_index_star.")

    resolved_pairs: list[tuple[Path, Path]] = []
    index_path: Path | None = None
    if coordinate_index_star:
        index_path = _project_file(settings, project_dir, coordinate_index_star, suffixes=(".star",))
        resolved_pairs = _pairs_from_index_star(settings, project_dir, index_path)
    else:
        for pair in pairs or []:
            micrograph = pair.get("micrograph")
            coordinates = pair.get("coordinates")
            if not micrograph or not coordinates:
                raise VisualError("Every pair must contain 'micrograph' and 'coordinates'.")
            resolved_pairs.append(
                (
                    _project_file(settings, project_dir, micrograph, suffixes=(".mrc", ".mrcs")),
                    _project_file(settings, project_dir, coordinates, suffixes=(".star",)),
                )
            )
    if not resolved_pairs:
        raise VisualError("No micrograph/coordinate pairs were supplied.")

    cached_coords: list[tuple[np.ndarray, np.ndarray | None]] = []
    counts: list[int] = []
    for micrograph_path, coordinate_path in resolved_pairs:
        coords, foms = _coordinate_rows(
            coordinate_path,
            settings=settings,
            project_dir=project_dir,
            micrograph_path=micrograph_path,
        )
        cached_coords.append((coords, foms))
        counts.append(int(coords.shape[0]))
    selected_indices = _stratified_indices(counts, max_micrographs)
    effective_columns = min(columns, len(selected_indices))
    rows = math.ceil(len(selected_indices) / effective_columns)
    figure = Figure(figsize=(5.0 * effective_columns, 4.5 * rows + 0.6), constrained_layout=True)
    axes = figure.subplots(rows, effective_columns, squeeze=False)
    records: list[dict[str, Any]] = []

    for panel, pair_index in enumerate(selected_indices):
        axis = axes[panel // effective_columns][panel % effective_columns]
        micrograph_path, coordinate_path = resolved_pairs[pair_index]
        coords, foms = cached_coords[pair_index]
        with mrcfile.mmap(micrograph_path, permissive=True, mode="r") as handle:
            data = handle.data
            if data.ndim == 2:
                micrograph = data
            elif data.ndim == 3 and data.shape[0] == 1:
                micrograph = data[0]
            else:
                raise VisualError(f"Expected one 2D micrograph, got shape {data.shape} in {micrograph_path}.")
            displayed, original_shape = _downsample_2d(micrograph, max_side=1100)

        vmin, vmax = _display_limits(displayed, low_percentile, high_percentile)
        height, width = original_shape
        axis.imshow(
            displayed,
            cmap="gray",
            origin="upper",
            extent=(0, width, height, 0),
            vmin=vmin,
            vmax=vmax,
        )

        finite_mask = np.ones(coords.shape[0], dtype=bool)
        if foms is not None:
            finite_mask = np.isfinite(foms)
        if fom_threshold is None or foms is None:
            accepted = np.ones(coords.shape[0], dtype=bool)
        else:
            accepted = finite_mask & (foms >= fom_threshold)
        rejected = ~accepted

        def add_ellipses(
            mask: np.ndarray,
            edgecolor: str,
            alpha: float,
            *,
            panel_coordinates: np.ndarray = coords,
            panel_axis: Any = axis,
        ) -> None:
            if not np.any(mask):
                return
            collection = EllipseCollection(
                widths=np.full(int(mask.sum()), particle_diameter_pix),
                heights=np.full(int(mask.sum()), particle_diameter_pix),
                angles=np.zeros(int(mask.sum())),
                units="xy",
                offsets=panel_coordinates[mask],
                transOffset=panel_axis.transData,
                facecolors="none",
                edgecolors=edgecolor,
                linewidths=0.55,
                alpha=alpha,
            )
            panel_axis.add_collection(collection)

        add_ellipses(rejected, "red", 0.55)
        add_ellipses(accepted, "lime", 0.8)
        accepted_count = int(np.sum(accepted))
        total_count = int(coords.shape[0])
        title = f"{micrograph_path.name}\naccepted {accepted_count}/{total_count}"
        if foms is not None and np.any(np.isfinite(foms)):
            finite_fom = foms[np.isfinite(foms)]
            title += f"  FOM [{np.min(finite_fom):.2g}, {np.max(finite_fom):.2g}]"
        axis.set_title(title, fontsize=9)
        axis.set_xlim(0, width)
        axis.set_ylim(height, 0)
        axis.set_aspect("equal")
        axis.axis("off")

        nearest: dict[str, float | None] = {"median_pix": None, "p05_pix": None}
        accepted_coords = coords[accepted]
        if accepted_coords.shape[0] >= 2:
            distances, _ = cKDTree(accepted_coords).query(accepted_coords, k=2)
            nearest_values = distances[:, 1]
            nearest = {
                "median_pix": float(np.median(nearest_values)),
                "p05_pix": float(np.percentile(nearest_values, 5)),
            }
        edge_distance = np.minimum.reduce(
            [coords[:, 0], width - coords[:, 0], coords[:, 1], height - coords[:, 1]]
        ) if total_count else np.asarray([], dtype=float)

        records.append(
            {
                "micrograph": _relative(project, micrograph_path),
                "coordinates": _relative(project, coordinate_path),
                "shape_yx": [height, width],
                "total_picks": total_count,
                "accepted_picks": accepted_count,
                "rejected_picks": total_count - accepted_count,
                "fom_available": foms is not None,
                "fom_threshold": fom_threshold,
                "nearest_neighbour": nearest,
                "fraction_with_center_within_one_radius_of_edge": (
                    float(np.mean(edge_distance < particle_diameter_pix / 2)) if edge_distance.size else 0.0
                ),
            }
        )

    for panel in range(len(selected_indices), rows * effective_columns):
        axes[panel // effective_columns][panel % effective_columns].axis("off")
    figure.suptitle(
        "RELION/Topaz pick overlays — green accepted, red below threshold",
        fontsize=14,
    )
    return _finish(
        figure,
        {
            "report_type": "relion_pick_overlays",
            "project_dir": str(project),
            "coordinate_index_star": _relative(project, index_path) if index_path else None,
            "pair_count": len(resolved_pairs),
            "sampling": "stratified by total pick count",
            "rendered_micrographs": records,
            "particle_diameter_pix": particle_diameter_pix,
            "fom_threshold": fom_threshold,
            "coordinate_convention": "RELION pixel coordinates with (0,0) at the upper-left of the micrograph.",
            "scientific_note": (
                "Assess centering, systematic misses, contamination/edge bias, duplicates, and coverage. "
                "Accept Topaz picks only when overlay evidence agrees with statistics and downstream 2D classes."
            ),
        },
        settings,
    )


def _class_map_records(
    settings: Settings,
    project_dir: str,
    model_star: Path,
    explicit_maps: Sequence[str] | None,
) -> tuple[str | None, list[dict[str, Any]]]:
    blocks = parse_star(model_star)
    if explicit_maps:
        try:
            block_name, block = _find_block(
                blocks,
                any_of=(
                    "rlnReferenceImage",
                    "rlnClassDistribution",
                    "rlnEstimatedResolution",
                ),
            )
        except VisualError:
            block_name, block = None, {}
        references = _as_list(block.get("rlnReferenceImage"))
        metadata_count = max(
            len(explicit_maps),
            len(references),
            len(_as_list(block.get("rlnClassDistribution"))),
            len(_as_list(block.get("rlnEstimatedResolution"))),
        )
        occupancy = _numeric_column(block, "rlnClassDistribution", metadata_count)
        resolution = _numeric_column(block, "rlnEstimatedResolution", metadata_count)
        return block_name, [
            {
                "class_number": index + 1,
                "map_path": _project_file(
                    settings,
                    project_dir,
                    path,
                    base_dir=model_star.parent,
                    suffixes=(".mrc", ".mrcs"),
                ),
                "reference": references[index] if index < len(references) else path,
                "explicit_map": path,
                "occupancy": occupancy[index],
                "resolution_A": resolution[index],
            }
            for index, path in enumerate(explicit_maps)
        ]

    block_name, block = _find_block(blocks, required=("rlnReferenceImage",))
    references = _as_list(block.get("rlnReferenceImage"))
    count = len(references)
    occupancy = _numeric_column(block, "rlnClassDistribution", count)
    resolution = _numeric_column(block, "rlnEstimatedResolution", count)
    records: list[dict[str, Any]] = []
    for index, reference in enumerate(references):
        stack_index, raw_path = _parse_image_reference(reference)
        path = _project_file(
            settings,
            project_dir,
            raw_path,
            base_dir=model_star.parent,
            suffixes=(".mrc", ".mrcs"),
        )
        if stack_index is not None:
            # A 3D reference in a stack is legal but uncommon; retain the index for loading.
            reference_index = stack_index
        else:
            reference_index = None
        records.append(
            {
                "class_number": index + 1,
                "map_path": path,
                "reference": reference,
                "reference_index": reference_index,
                "occupancy": occupancy[index],
                "resolution_A": resolution[index],
            }
        )
    if not records:
        raise VisualError("No class maps were found in the model STAR.")
    return block_name, records


def _read_volume_from_record(record: dict[str, Any]) -> tuple[np.ndarray, _MrcInfo]:
    path = Path(record["map_path"])
    _check_volume_render_size(path, f"3D class map {path.name}")
    with mrcfile.mmap(path, permissive=True, mode="r") as handle:
        data = handle.data
        reference_index = record.get("reference_index")
        if data.ndim == 3:
            if reference_index not in {None, 0}:
                raise VisualError(
                    f"Reference requests volume {reference_index + 1}, but {path.name} "
                    "contains one 3D map."
                )
            volume = np.asarray(data, dtype=np.float32)
        elif data.ndim == 4 and reference_index is not None:
            if reference_index < 0 or reference_index >= data.shape[0]:
                raise VisualError(f"3D stack index is outside {path.name}.")
            volume = np.asarray(data[reference_index], dtype=np.float32)
        else:
            raise VisualError(f"Expected a 3D map, got shape {data.shape} in {path}.")
        raw_info = _mrc_info(handle, path)
        info = _MrcInfo(
            path=raw_info.path,
            shape=tuple(int(item) for item in volume.shape),
            dtype=raw_info.dtype,
            voxel_size_A=raw_info.voxel_size_A,
            origin_A=raw_info.origin_A,
            axis_mapping=raw_info.axis_mapping,
            start_xyz=raw_info.start_xyz,
            start_column_row_section=raw_info.start_column_row_section,
        )
    return volume, info


def _orthogonal_slices(volume: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    z, y, x = (dimension // 2 for dimension in volume.shape)
    xy = np.asarray(volume[z, :, :])
    xz = np.asarray(volume[:, y, :])
    yz = np.asarray(volume[:, :, x])
    return xy, xz, yz


def _central_slab_projection(
    volume: np.ndarray,
    axis: int,
    *,
    fraction: float = 0.25,
    threshold: float | None = None,
) -> tuple[np.ndarray, np.ndarray | None, tuple[int, int]]:
    """Average a central slab and optionally project threshold occupancy.

    A central-slab mean retains internal morphology without allowing one bright
    solvent speckle anywhere in the box to dominate the image, as a raw maximum
    projection can.  The occupancy projection separately shows how consistently
    above-threshold density extends through the slab.
    """
    data = np.asarray(volume)
    if data.ndim != 3 or axis not in {0, 1, 2}:
        raise VisualError("Central-slab projection requires a 3D volume and axis 0, 1, or 2.")
    if not (0 < fraction <= 1):
        raise VisualError("Central-slab fraction must satisfy 0 < fraction <= 1.")
    width = max(1, min(data.shape[axis], math.ceil(data.shape[axis] * fraction)))
    start = (data.shape[axis] - width) // 2
    stop = start + width
    selection = [slice(None)] * 3
    selection[axis] = slice(start, stop)
    slab = np.asarray(data[tuple(selection)], dtype=np.float32)
    finite = np.isfinite(slab)
    finite_count = np.sum(finite, axis=axis)
    density_sum = np.sum(np.where(finite, slab, 0.0), axis=axis, dtype=np.float64)
    density = np.divide(
        density_sum,
        finite_count,
        out=np.zeros_like(density_sum, dtype=np.float64),
        where=finite_count > 0,
    ).astype(np.float32)
    occupancy = None
    if threshold is not None:
        above = finite & (slab >= threshold)
        occupancy = np.divide(
            np.sum(above, axis=axis),
            finite_count,
            out=np.zeros_like(density_sum, dtype=np.float64),
            where=finite_count > 0,
        ).astype(np.float32)
    return density, occupancy, (start, stop)


def _thresholded_overlap_metrics(
    reference: np.ndarray,
    candidate: np.ndarray,
    *,
    threshold_sigma: float = 2.0,
) -> dict[str, Any]:
    finite = np.isfinite(reference) & np.isfinite(candidate)
    if not np.any(finite):
        return {
            "jaccard": None,
            "dice": None,
            "similarity_within_threshold_union": {
                "pearson_correlation": None,
                "zscore_rms_difference": None,
            },
        }
    reference_mask = finite & (_zscore(reference) >= threshold_sigma)
    candidate_mask = finite & (_zscore(candidate) >= threshold_sigma)
    intersection = int(np.sum(reference_mask & candidate_mask))
    union_mask = reference_mask | candidate_mask
    union = int(np.sum(union_mask))
    total = int(np.sum(reference_mask) + np.sum(candidate_mask))
    return {
        "jaccard": float(intersection / union) if union else None,
        "dice": float(2 * intersection / total) if total else None,
        "union_fraction_of_finite_voxels": float(union / np.sum(finite)),
        "similarity_within_threshold_union": (
            _map_similarity(reference[union_mask], candidate[union_mask])
            if union
            else {"pearson_correlation": None, "zscore_rms_difference": None}
        ),
    }


def render_3d_classes(
    settings: Settings,
    project_dir: str,
    model_star: str,
    *,
    class_maps: Sequence[str] | None = None,
    selected_class_numbers: Sequence[int] | None = None,
    max_classes: int = 8,
    sort_by: Literal["index", "occupancy", "resolution"] = "index",
    include_mip: bool = True,
    display_lowpass_A: float | None = None,
    low_percentile: float = 1.0,
    high_percentile: float = 99.5,
) -> RenderedVisual:
    project = settings.validate_project_path(project_dir)
    star_path = _project_file(settings, project_dir, model_star, suffixes=(".star",))
    block_name, records = _class_map_records(settings, project_dir, star_path, class_maps)
    count = len(records)
    if not (1 <= max_classes <= min(settings.visual_max_panels, 16)):
        raise VisualError("max_classes must be between 1 and 16.")

    occupancy = [record.get("occupancy") for record in records]
    resolution = [record.get("resolution_A") for record in records]
    indices = _sort_indices(count, sort_by, occupancy, resolution, [None] * count)
    if selected_class_numbers:
        requested = {number - 1 for number in selected_class_numbers}
        invalid = sorted(number + 1 for number in requested if number < 0 or number >= count)
        if invalid:
            raise VisualError(f"Unknown 3D class number(s): {invalid}")
        indices = [index for index in indices if index in requested]
    indices = indices[:max_classes]
    if not indices:
        raise VisualError("No 3D classes remain after filtering.")

    loaded: list[dict[str, Any]] = []
    reference_info: _MrcInfo | None = None
    display_lowpass: dict[str, Any] | None = None
    for index in indices:
        record = records[index]
        volume, info = _read_volume_from_record(record)
        if reference_info is None:
            reference_info = info
        else:
            _assert_same_grid(reference_info, info, "3D class comparison")
        raw_compact = _downsample_volume(volume, max_side=220)
        display_volume, item_lowpass = _apply_display_lowpass(
            volume,
            info,
            display_lowpass_A,
        )
        compact = (
            _downsample_volume(display_volume, max_side=220)
            if item_lowpass["applied"]
            else raw_compact
        )
        if display_lowpass is None:
            display_lowpass = item_lowpass
        metrics = _map_metrics(compact, threshold_sigma=2.0)
        projection, occupancy_projection, slab_bounds = _central_slab_projection(
            compact,
            0,
            fraction=0.25,
            threshold=float(metrics["threshold_value"]),
        )
        loaded.append(
            {
                "record": record,
                "volume_shape": tuple(int(item) for item in volume.shape),
                "info": info,
                "raw_compact": raw_compact,
                "compact": compact,
                "metrics": metrics,
                "projection": projection,
                "occupancy_projection": occupancy_projection,
                "slab_bounds": slab_bounds,
            }
        )

    assert display_lowpass is not None

    shared_samples = np.concatenate([_sample_finite(item["compact"]) for item in loaded])
    vmin, vmax = _display_limits(shared_samples, low_percentile, high_percentile)
    projection_samples = np.concatenate(
        [_sample_finite(item["projection"]) for item in loaded]
    )
    projection_vmin, projection_vmax = _display_limits(
        projection_samples,
        low_percentile,
        high_percentile,
    )

    common_filter_sigma = 1.0
    common_filtered: list[np.ndarray] = []
    for item in loaded:
        compact = item["raw_compact"]
        finite = np.isfinite(compact)
        fill = float(np.mean(compact[finite])) if np.any(finite) else 0.0
        common_filtered.append(
            np.asarray(
                ndimage.gaussian_filter(
                    np.where(finite, compact, fill),
                    sigma=common_filter_sigma,
                    mode="nearest",
                ),
                dtype=np.float32,
            )
        )

    pairwise_comparisons: list[dict[str, Any]] = []
    for left in range(len(loaded)):
        for right in range(left + 1, len(loaded)):
            left_record = loaded[left]["record"]
            right_record = loaded[right]["record"]
            raw_similarity = _map_similarity(
                loaded[left]["raw_compact"],
                loaded[right]["raw_compact"],
            )
            filtered_similarity = _map_similarity(
                common_filtered[left],
                common_filtered[right],
            )
            comparison: dict[str, Any] = {
                "class_a": left_record["class_number"],
                "class_b": right_record["class_number"],
                "pearson_correlation": raw_similarity["pearson_correlation"],
                "zscore_rms_difference": raw_similarity["zscore_rms_difference"],
                "same_frame_unfiltered_similarity": raw_similarity,
                "common_gaussian_filter": {
                    "sigma_downsampled_voxels": common_filter_sigma,
                    "similarity": filtered_similarity,
                    "thresholded_overlap_at_2_sigma": _thresholded_overlap_metrics(
                        common_filtered[left],
                        common_filtered[right],
                        threshold_sigma=2.0,
                    ),
                },
            }
            if display_lowpass["applied"]:
                physical_similarity = _map_similarity(
                    loaded[left]["compact"],
                    loaded[right]["compact"],
                )
                comparison["common_physical_lowpass"] = {
                    "cutoff_resolution_A": display_lowpass["cutoff_resolution_A"],
                    "filter_type": display_lowpass["type"],
                    "similarity": physical_similarity,
                    "thresholded_overlap_at_2_sigma": _thresholded_overlap_metrics(
                        loaded[left]["compact"],
                        loaded[right]["compact"],
                        threshold_sigma=2.0,
                    ),
                }
            pairwise_comparisons.append(comparison)

    columns = 4 if include_mip else 3
    figure = Figure(figsize=(3.5 * columns, 3.05 * len(indices) + 0.8), constrained_layout=True)
    axes = figure.subplots(len(indices), columns, squeeze=False)
    output_records: list[dict[str, Any]] = []

    for row, item in enumerate(loaded):
        record = item["record"]
        info = item["info"]
        compact = item["compact"]
        metrics = item["metrics"]
        slices = _orthogonal_slices(compact)
        names = ("XY central slice", "XZ central slice", "YZ central slice")
        for column, (name, image) in enumerate(zip(names, slices, strict=True)):
            axes[row][column].imshow(image, cmap="gray", origin="lower", vmin=vmin, vmax=vmax)
            axes[row][column].set_title(name, fontsize=9)
            axes[row][column].axis("off")
        if include_mip:
            projection = item["projection"]
            occupancy_projection = item["occupancy_projection"]
            axes[row][3].imshow(
                projection,
                cmap="gray",
                origin="lower",
                vmin=projection_vmin,
                vmax=projection_vmax,
            )
            if (
                occupancy_projection is not None
                and np.nanmin(occupancy_projection) <= 0.1 <= np.nanmax(occupancy_projection)
            ):
                axes[row][3].contour(
                    occupancy_projection,
                    levels=[0.1],
                    colors="tab:orange",
                    linewidths=0.65,
                )
            axes[row][3].set_title("Z central-slab mean + 10% occupancy", fontsize=9)
            axes[row][3].axis("off")

        label = (
            f"Class {record['class_number']:03d} — p={_metric_text(record.get('occupancy'))} — "
            f"res={_metric_text(record.get('resolution_A'), suffix=' Å')}\n"
            f"{_relative(project, Path(record['map_path']))}"
        )
        axes[row][0].text(
            -0.04,
            0.5,
            label,
            transform=axes[row][0].transAxes,
            ha="right",
            va="center",
            fontsize=8,
            rotation=90,
        )
        output_records.append(
            {
                "class_number": record["class_number"],
                "map": _relative(project, Path(record["map_path"])),
                "reference": record.get("reference"),
                "occupancy": record.get("occupancy"),
                "estimated_resolution_A": record.get("resolution_A"),
                "shape_zyx": list(item["volume_shape"]),
                "voxel_size_A_xyz": list(info.voxel_size_A) if info.voxel_size_A else None,
                "origin_A_xyz": list(info.origin_A),
                "axis_mapping": list(info.axis_mapping),
                "sample_mean": metrics["sample_mean"],
                "sample_std": metrics["sample_std"],
                "metrics_on_downsampled_volume": metrics,
                "metrics_volume": (
                    "display_lowpass" if display_lowpass["applied"] else "raw_source"
                ),
            }
        )

    view_label = (
        f"{display_lowpass['cutoff_resolution_A']:g} Å display low-pass"
        if display_lowpass["applied"]
        else "raw map view"
    )
    figure.suptitle(
        f"RELION 3D classes — {star_path.name} — {view_label} — "
        f"physical grid validated — sorted by {sort_by}",
        fontsize=14,
    )
    return _finish(
        figure,
        {
            "report_type": "relion_3d_classes",
            "project_dir": str(project),
            "model_star": _relative(project, star_path),
            "star_block": block_name,
            "total_classes": count,
            "rendered_classes": output_records,
            "same_physical_grid_validated": True,
            "pairwise_comparisons": pairwise_comparisons,
            "display": {
                "low_percentile": low_percentile,
                "high_percentile": high_percentile,
                "normalization_scope": "shared_across_rendered_classes",
                "density_limits": {"vmin": vmin, "vmax": vmax},
                "projection_limits": {"vmin": projection_vmin, "vmax": projection_vmax},
                "lowpass": display_lowpass,
                "projection": {
                    "enabled": include_mip,
                    "type": "central_slab_mean_with_thresholded_occupancy_contour",
                    "axis": "z",
                    "slab_fraction": 0.25,
                    "occupancy_threshold_sigma": 2.0,
                    "occupancy_contour_fraction": 0.1,
                    "raw_maximum_projection_used": False,
                },
            },
            "scientific_note": (
                "Rendered maps have identical shape, voxel size, origin, start indices, and axis mapping. Shared normalization and "
                "central-slab projections make class appearance comparable. A raw component count includes tiny "
                "speckles and is not, by itself, evidence of fragmentation; use dominant-component and coverage "
                "metrics. Pairwise map differences support structural distinctness, not a biological assignment. "
                "Any requested Angstrom-defined low-pass is display-only, leaves the source maps unchanged, and "
                "does not by itself test reference bias."
            ),
        },
        settings,
    )


def _map_header_record(
    project: Path,
    path: Path,
    info: _MrcInfo,
    *,
    sha256: str | None = None,
    canonical_voxel_sha256: str | None = None,
) -> dict[str, Any]:
    record = {
        "path": _relative(project, path),
        "sha256": sha256 or _file_sha256(path),
        "shape_section_row_column": list(info.shape),
        "voxel_size_A_xyz": list(info.voxel_size_A or ()),
        "origin_A_xyz": list(info.origin_A),
        "start_xyz": list(info.start_xyz),
        "start_column_row_section": list(info.start_column_row_section),
        "axis_mapping_mapc_mapr_maps": list(info.axis_mapping),
    }
    if canonical_voxel_sha256 is not None:
        record["canonical_voxel_sha256"] = canonical_voxel_sha256
    return record


def _map_comparison_mrc_preflight(
    path: Path,
    label: str,
) -> tuple[_MrcInfo, float, tuple[int, int, int], tuple[int, int, int]]:
    file_bytes = path.stat().st_size
    if file_bytes > _MAP_COMPARISON_MAX_INPUT_FILE_BYTES:
        raise VisualError(
            f"Map comparison {label} is {file_bytes:,} bytes, above the "
            f"{_MAP_COMPARISON_MAX_INPUT_FILE_BYTES:,}-byte input-file cap."
        )
    with mrcfile.mmap(path, permissive=True, mode="r") as handle:
        if handle.data.ndim != 3:
            raise VisualError(
                f"Map comparison {label} must be a 3D MRC volume; got {handle.data.shape}."
            )
        if not np.issubdtype(handle.data.dtype, np.number) or np.issubdtype(
            handle.data.dtype,
            np.complexfloating,
        ):
            raise VisualError(
                f"Map comparison {label} must contain real-valued density; "
                f"got {handle.data.dtype}."
            )
        info = _mrc_info(handle, path)
    sampling_A = _validate_symmetry_scan_grid(info, f"Map comparison {label}")
    transpose = _canonical_physical_transpose(info)
    canonical_shape = tuple(info.shape[axis] for axis in transpose)
    try:
        validate_map_comparison_resources(canonical_shape)
    except MapComparisonError as exc:
        raise VisualError(str(exc)) from exc
    return info, sampling_A, transpose, canonical_shape


def _comparison_contour(
    axis: Any,
    image: np.ndarray,
    *,
    color: str,
    threshold: float | None,
    linewidth: float,
) -> None:
    if threshold is None or not math.isfinite(threshold):
        return
    finite = np.isfinite(image)
    if not np.any(finite):
        return
    values = np.asarray(image[finite], dtype=np.float64)
    minimum = float(np.min(values))
    maximum = float(np.max(values))
    if minimum < threshold < maximum:
        axis.contour(
            image,
            levels=[threshold],
            colors=color,
            linewidths=linewidth,
        )


def _weighted_zscore_for_display(
    array: np.ndarray,
    weights: np.ndarray,
) -> np.ndarray:
    data = np.asarray(array, dtype=np.float32)
    metric_weights = np.asarray(weights, dtype=np.float32)
    valid = np.isfinite(data) & np.isfinite(metric_weights) & (metric_weights > 1e-6)
    output = np.full(data.shape, np.nan, dtype=np.float32)
    if not np.any(valid):
        return output
    values = np.asarray(data[valid], dtype=np.float64)
    selected_weights = np.asarray(metric_weights[valid], dtype=np.float64)
    total = float(np.sum(selected_weights))
    mean = float(np.sum(selected_weights * values) / total)
    variance = float(np.sum(selected_weights * (values - mean) ** 2) / total)
    if variance <= np.finfo(np.float64).tiny:
        output[valid] = np.asarray(values - mean, dtype=np.float32)
    else:
        output[valid] = np.asarray(
            (values - mean) / math.sqrt(variance),
            dtype=np.float32,
        )
    return output


def render_map_comparison(
    settings: Settings,
    project_dir: str,
    reference_map: str,
    candidate_map: str,
    reference_label: str,
    candidate_label: str,
    alignment_lowpass_A: float,
    *,
    comparison_lowpasses_A: Sequence[float] | None = None,
    mask_path: str | None = None,
    alignment_mode: Literal["translation", "rigid"] = "rigid",
    max_shift_A: float = 15.0,
    max_rotation_deg: float = 15.0,
    threshold_sigma: float = 2.0,
    low_percentile: float = 0.5,
    high_percentile: float = 99.5,
) -> RenderedVisual:
    """Register one related map locally and compare it on the reference grid."""
    project = settings.validate_project_path(project_dir)
    labels = (str(reference_label).strip(), str(candidate_label).strip())
    if not all(labels):
        raise VisualError("reference_label and candidate_label must be non-empty.")
    if labels[0] == labels[1]:
        raise VisualError("reference_label and candidate_label must be different.")

    reference_path = _project_file(
        settings, project_dir, reference_map, suffixes=(".mrc", ".mrcs")
    )
    candidate_path = _project_file(
        settings, project_dir, candidate_map, suffixes=(".mrc", ".mrcs")
    )
    if reference_path == candidate_path or reference_path.samefile(candidate_path):
        raise VisualError("reference_map and candidate_map must be different files.")
    mask_resolved = (
        _project_file(settings, project_dir, mask_path, suffixes=(".mrc", ".mrcs"))
        if mask_path is not None
        else None
    )

    paths = (reference_path, candidate_path)
    preflight = [
        _map_comparison_mrc_preflight(path, label)
        for path, label in zip(paths, labels, strict=True)
    ]
    infos = [item[0] for item in preflight]
    samplings_A = [item[1] for item in preflight]
    transposes = [item[2] for item in preflight]
    canonical_shapes = [item[3] for item in preflight]
    if not math.isclose(
        samplings_A[0],
        samplings_A[1],
        rel_tol=1e-6,
        abs_tol=1e-6,
    ):
        raise VisualError(
            "Map comparison requires matching isotropic voxel sampling; "
            f"got {samplings_A[0]:g} Å and {samplings_A[1]:g} Å."
        )
    if canonical_shapes[0] != canonical_shapes[1]:
        raise VisualError(
            "Map comparison requires identical canonical physical shapes; "
            f"got {canonical_shapes[0]} and {canonical_shapes[1]}."
        )

    mask_volume = None
    mask_info = None
    mask_transpose = None
    mask_sha256 = None
    mask_preflight = None
    if mask_resolved is not None:
        mask_preflight = _map_comparison_mrc_preflight(mask_resolved, "mask")
        mask_info, _, mask_transpose, _ = mask_preflight
        _assert_same_grid(infos[0], mask_info, "Map comparison mask")

    snapshot_paths = paths + ((mask_resolved,) if mask_resolved is not None else ())
    snapshot_hashes = {path: _file_sha256(path) for path in snapshot_paths}

    volumes: list[np.ndarray] = []
    for path, info, transpose in zip(paths, infos, transposes, strict=True):
        with mrcfile.mmap(path, permissive=True, mode="r") as handle:
            source = np.array(handle.data, dtype=np.float32, copy=True)
        volumes.append(np.asarray(np.transpose(source, axes=transpose), dtype=np.float32))
    if mask_resolved is not None and mask_info is not None and mask_transpose is not None:
        with mrcfile.mmap(mask_resolved, permissive=True, mode="r") as handle:
            source_mask = np.array(handle.data, dtype=np.float32, copy=True)
        mask_volume = np.asarray(
            np.transpose(source_mask, axes=mask_transpose), dtype=np.float32
        )
        mask_sha256 = snapshot_hashes[mask_resolved]

    for path, expected_sha256 in snapshot_hashes.items():
        if _file_sha256(path) != expected_sha256:
            raise VisualError(
                f"Map comparison input changed while its density snapshot was read: {path}."
            )

    postflight = [
        _map_comparison_mrc_preflight(path, label)
        for path, label in zip(paths, labels, strict=True)
    ]
    if postflight != preflight:
        raise VisualError(
            "Map comparison header/grid metadata changed while the density snapshot was read."
        )
    if mask_resolved is not None:
        mask_postflight = _map_comparison_mrc_preflight(mask_resolved, "mask")
        if mask_postflight != mask_preflight:
            raise VisualError(
                "Map comparison mask metadata changed while its density snapshot was read."
            )

    canonical_voxel_hashes = [_canonical_voxel_sha256(volume) for volume in volumes]
    mask_canonical_voxel_sha256 = (
        _canonical_voxel_sha256(mask_volume) if mask_volume is not None else None
    )
    reference_sha256 = snapshot_hashes[reference_path]
    candidate_sha256 = snapshot_hashes[candidate_path]

    try:
        comparison = compare_maps(
            volumes[0],
            volumes[1],
            voxel_size_A=float(infos[0].voxel_size_A[0]),  # validated above
            alignment_lowpass_A=alignment_lowpass_A,
            comparison_lowpasses_A=comparison_lowpasses_A,
            mask=mask_volume,
            alignment_mode=alignment_mode,
            max_shift_A=max_shift_A,
            max_rotation_deg=max_rotation_deg,
            threshold_sigma=threshold_sigma,
        )
    except MapComparisonError as exc:
        raise VisualError(str(exc)) from exc

    for path, expected_sha256 in snapshot_hashes.items():
        if _file_sha256(path) != expected_sha256:
            raise VisualError(
                f"Map comparison input changed while its copied density was analyzed: {path}."
            )

    compact_reference = _downsample_volume(comparison.reference_filtered, max_side=192)
    compact_before = _downsample_volume(comparison.candidate_before, max_side=192)
    compact_after = _downsample_volume(comparison.candidate_after, max_side=192)
    compact_weights = _downsample_volume(comparison.metric_weights, max_side=192)
    reference_slices = _orthogonal_slices(compact_reference)
    before_slices = _orthogonal_slices(compact_before)
    after_slices = _orthogonal_slices(compact_after)
    weight_slices = _orthogonal_slices(compact_weights)
    before_difference = (
        _weighted_zscore_for_display(before_slices[0], weight_slices[0])
        - _weighted_zscore_for_display(reference_slices[0], weight_slices[0])
    )
    after_difference = (
        _weighted_zscore_for_display(after_slices[0], weight_slices[0])
        - _weighted_zscore_for_display(reference_slices[0], weight_slices[0])
    )

    comparisons = comparison.metadata["metrics"]["comparisons"]
    extra_rows: list[dict[str, Any]] = []
    for view, metric in zip(comparison.cutoff_views[1:], comparisons[1:], strict=True):
        before_difference_xy = (
            _weighted_zscore_for_display(view.candidate_before_xy, view.metric_weights_xy)
            - _weighted_zscore_for_display(view.reference_xy, view.metric_weights_xy)
        )
        after_difference_xy = (
            _weighted_zscore_for_display(view.candidate_after_xy, view.metric_weights_xy)
            - _weighted_zscore_for_display(view.reference_xy, view.metric_weights_xy)
        )
        extra_rows.append(
            {
                "view": view,
                "metric": metric,
                "before_difference": before_difference_xy,
                "after_difference": after_difference_xy,
            }
        )

    density_arrays = [compact_reference, compact_before, compact_after]
    difference_arrays = [before_difference, after_difference]
    for row in extra_rows:
        view = row["view"]
        density_arrays.extend(
            [view.reference_xy, view.candidate_before_xy, view.candidate_after_xy]
        )
        difference_arrays.extend(
            [row["before_difference"], row["after_difference"]]
        )
    vmin, vmax = _display_limits(
        np.concatenate([item.reshape(-1) for item in density_arrays]),
        low_percentile,
        high_percentile,
    )
    difference_limits = _display_limits(
        np.concatenate([item.reshape(-1) for item in difference_arrays]),
        1.0,
        99.0,
        symmetric=True,
    )

    figure_rows = 3 + len(extra_rows)
    figure = Figure(
        figsize=(15, 10 + 2.7 * len(extra_rows)),
        constrained_layout=True,
    )
    axes = figure.subplots(figure_rows, 4, squeeze=False)
    slice_names = ("XY", "XZ", "YZ")
    for column, (name, image) in enumerate(
        zip(slice_names, reference_slices, strict=True)
    ):
        axes[0][column].imshow(
            image, cmap="gray", origin="lower", vmin=vmin, vmax=vmax
        )
        axes[0][column].set_title(f"{labels[0]} reference: {name}")
        axes[0][column].axis("off")

    primary_metrics = comparisons[0]
    for row, (row_label, slices, overlap) in enumerate(
        (
            (
                "before alignment",
                before_slices,
                primary_metrics["before_alignment"]["threshold_overlap"],
            ),
            (
                "after alignment",
                after_slices,
                primary_metrics["after_alignment"]["threshold_overlap"],
            ),
        ),
        start=1,
    ):
        for column, (name, image, reference_image) in enumerate(
            zip(slice_names, slices, reference_slices, strict=True)
        ):
            axes[row][column].imshow(
                image, cmap="gray", origin="lower", vmin=vmin, vmax=vmax
            )
            _comparison_contour(
                axes[row][column],
                reference_image,
                color="tab:blue",
                threshold=overlap["reference_threshold"],
                linewidth=0.8,
            )
            _comparison_contour(
                axes[row][column],
                image,
                color="tab:orange",
                threshold=overlap["candidate_threshold"],
                linewidth=0.65,
            )
            axes[row][column].set_title(
                f"{labels[1]} {row_label}: {name}\n"
                "blue/orange=weighted 3D metric thresholds"
            )
            axes[row][column].axis("off")

    axes[0][3].axis("off")
    alignment_record = comparison.metadata["alignment"]
    transform = alignment_record["transform"]
    before_correlation = primary_metrics["before_alignment"]["similarity"][
        "pearson_correlation"
    ]
    after_correlation = primary_metrics["after_alignment"]["similarity"][
        "pearson_correlation"
    ]
    summary = [
        f"Status: {comparison.metadata['status']}",
        f"Alignment cutoff: {alignment_record['effective_alignment_lowpass_A']:g} Å",
        f"Pearson before → after: {_metric_text(before_correlation)} → {_metric_text(after_correlation)}",
        "Rotation XYZ (deg): "
        + ", ".join(f"{item:.3g}" for item in transform["rotation_euler_xyz_degrees"]),
        "Centered-grid translation XYZ (Å): "
        + ", ".join(
            f"{item:.3g}"
            for item in transform["translation_centered_grid_xyz_A"]
        ),
        f"Valid support: {alignment_record['valid_support_fraction']:.3f}",
        "",
        "One transform was fitted once and frozen",
        "for every requested comparison cutoff.",
        "No same/distinct relation is inferred.",
    ]
    axes[0][3].text(0.02, 0.98, "\n".join(summary), ha="left", va="top", fontsize=10)
    for row, (title, difference) in enumerate(
        (
            (f"z-score XY difference before vs {labels[0]}", before_difference),
            (f"z-score XY difference after vs {labels[0]}", after_difference),
        ),
        start=1,
    ):
        axes[row][3].imshow(
            difference,
            cmap="coolwarm",
            origin="lower",
            vmin=difference_limits[0],
            vmax=difference_limits[1],
        )
        axes[row][3].set_title(title)
        axes[row][3].axis("off")

    for row_index, row_record in enumerate(extra_rows, start=3):
        view = row_record["view"]
        metric = row_record["metric"]
        after_overlap = metric["after_alignment"]["threshold_overlap"]
        before_overlap = metric["before_alignment"]["threshold_overlap"]
        for column, (title, image) in enumerate(
            (
                (f"{labels[0]} reference", view.reference_xy),
                (f"{labels[1]} before", view.candidate_before_xy),
                (f"{labels[1]} after", view.candidate_after_xy),
            )
        ):
            axes[row_index][column].imshow(
                image,
                cmap="gray",
                origin="lower",
                vmin=vmin,
                vmax=vmax,
            )
            if column > 0:
                overlap = before_overlap if column == 1 else after_overlap
                _comparison_contour(
                    axes[row_index][column],
                    view.reference_xy,
                    color="tab:blue",
                    threshold=overlap["reference_threshold"],
                    linewidth=0.8,
                )
                _comparison_contour(
                    axes[row_index][column],
                    image,
                    color="tab:orange",
                    threshold=overlap["candidate_threshold"],
                    linewidth=0.65,
                )
            axes[row_index][column].set_title(
                f"{view.cutoff_resolution_A:g} Å fixed-transform XY — {title}"
            )
            axes[row_index][column].axis("off")
        axes[row_index][3].imshow(
            row_record["after_difference"],
            cmap="coolwarm",
            origin="lower",
            vmin=difference_limits[0],
            vmax=difference_limits[1],
        )
        axes[row_index][3].set_title(
            f"{view.cutoff_resolution_A:g} Å support-weighted z-score difference after"
        )
        axes[row_index][3].axis("off")

    figure.suptitle(
        f"Local density registration — {labels[1]} → {labels[0]} — "
        f"{comparison.metadata['status']}",
        fontsize=14,
    )

    reference_record = _map_header_record(
        project,
        reference_path,
        infos[0],
        sha256=reference_sha256,
        canonical_voxel_sha256=canonical_voxel_hashes[0],
    )
    candidate_record = _map_header_record(
        project,
        candidate_path,
        infos[1],
        sha256=candidate_sha256,
        canonical_voxel_sha256=canonical_voxel_hashes[1],
    )
    evidence_root_payload = "\0".join(
        sorted(canonical_voxel_hashes)
    ).encode("ascii")
    evidence_root_id = "map_pixels_pair_sha256:" + hashlib.sha256(
        evidence_root_payload
    ).hexdigest()
    analysis_payload = {
        "algorithm_version": comparison.metadata["algorithm_version"],
        "reference_canonical_voxel_sha256": canonical_voxel_hashes[0],
        "candidate_canonical_voxel_sha256": canonical_voxel_hashes[1],
        "mask_canonical_voxel_sha256": mask_canonical_voxel_sha256,
        "canonical_shape_zyx": list(volumes[0].shape),
        "isotropic_voxel_size_A": float(infos[0].voxel_size_A[0]),
        "analysis_stride": alignment_record["analysis_stride"],
        "analysis_shape_zyx": alignment_record["analysis_shape_zyx"],
        "analysis_voxel_size_A": alignment_record["analysis_voxel_size_A"],
        "analysis_alias_safe_minimum_lowpass_A": alignment_record[
            "analysis_alias_safe_minimum_lowpass_A"
        ],
        "transform_sha256": comparison.metadata["alignment"]["transform"]["sha256"],
        "alignment_mode": alignment_mode,
        "requested_alignment_lowpass_A": alignment_lowpass_A,
        "effective_alignment_lowpass_A": alignment_record[
            "effective_alignment_lowpass_A"
        ],
        "comparison_lowpasses_A": [
            item["cutoff_resolution_A"] for item in comparisons
        ],
        "max_shift_A": max_shift_A,
        "max_rotation_deg": max_rotation_deg,
        "threshold_sigma": threshold_sigma,
    }
    analysis_id = "map_comparison_analysis_sha256:" + hashlib.sha256(
        json.dumps(
            analysis_payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    metadata = dict(comparison.metadata)
    metadata.update(
        {
            "schema_version": MAP_COMPARISON_SCHEMA_VERSION,
            "report_type": "relion_map_comparison",
            "project_dir": str(project),
            "reference": {"label": labels[0], **reference_record},
            "candidate": {"label": labels[1], **candidate_record},
            "mask": (
                {
                    **_map_header_record(
                        project,
                        mask_resolved,
                        mask_info,
                        sha256=mask_sha256,
                        canonical_voxel_sha256=mask_canonical_voxel_sha256,
                    ),
                    "transpose_source_to_canonical_zyx": list(mask_transpose or ()),
                }
                if mask_resolved is not None and mask_info is not None
                else None
            ),
            "physical_grid": {
                "canonical_shape_zyx": list(volumes[0].shape),
                "isotropic_voxel_size_A": float(infos[0].voxel_size_A[0]),
                "matching_sampling_and_canonical_shape_validated": True,
                "identical_header_frame_required": False,
                "reference_to_canonical_transpose": list(transposes[0]),
                "candidate_to_canonical_transpose": list(transposes[1]),
                "header_origin_matches": bool(
                    np.allclose(infos[0].origin_A, infos[1].origin_A)
                ),
                "header_start_matches": infos[0].start_xyz == infos[1].start_xyz,
                "axis_mapping_matches": infos[0].axis_mapping == infos[1].axis_mapping,
                "header_differences_are_recorded_not_treated_as_density_alignment": True,
            },
            "evidence": {
                "root_source": evidence_root_id,
                "analysis_id": analysis_id,
                "root_basis": "unordered_pair_of_canonical_zyx_float32_voxel_hashes",
                "canonical_voxel_content_identical": bool(
                    canonical_voxel_hashes[0] == canonical_voxel_hashes[1]
                ),
                "whole_file_hashes_are_artifact_provenance_not_evidence_roots": True,
                "independence_note": (
                    "All pre/post metrics, requested low-passes, contours, differences, "
                    "and transforms from this map pair are correlated derivatives of one "
                    "canonical map_pixels evidence root. Origin/start-only header clones "
                    "deduplicate to the same root; axis mapping changes canonical physical "
                    "voxel order. The root does not prove experimental independence."
                ),
            },
            "display": {
                "low_percentile": low_percentile,
                "high_percentile": high_percentile,
                "shared_density_limits": {"vmin": vmin, "vmax": vmax},
                "shared_symmetric_difference_limits": {
                    "vmin": difference_limits[0],
                    "vmax": difference_limits[1],
                },
                "metric_threshold_sigma": threshold_sigma,
                "contours_use_weighted_3d_metric_threshold_values": True,
                "differences_masked_to_metric_support": True,
                "rendered_cutoff_resolutions_A": [
                    view.cutoff_resolution_A for view in comparison.cutoff_views
                ],
                "fixed_transform_cutoff_views": len(comparison.cutoff_views),
            },
            "provenance": {
                "whole_file_sha256_semantics": "stable_input_snapshot_artifact_hash",
                "source_snapshot_verified_before_and_after_analysis": True,
                "source_maps_modified": False,
                "project_files_created": False,
                "aligned_map_materialized": False,
                "report_only": True,
            },
            "scientific_note": (
                "This tool registers related density maps and reports measurements; it does "
                "not infer same/distinct biology or union benefit. A boundary-limited, "
                "insufficient-overlap, optimizer-stopped, ambiguous, unstable, or "
                "non-generalizing fit is unassessable. Different starting references do "
                "not prevent descriptive final-map comparison, but they remain a causal "
                "confound for particle-set or union-effect claims."
            ),
        }
    )
    return _finish(figure, metadata, settings)


def _surface_points(
    compact: np.ndarray,
    threshold: float,
    *,
    max_points: int = 18_000,
) -> tuple[np.ndarray, np.ndarray]:
    binary = np.isfinite(compact) & (compact >= threshold)
    if not np.any(binary):
        return np.empty((0, 3), dtype=float), np.empty((0,), dtype=float)
    surface = binary & ~ndimage.binary_erosion(binary)
    points = np.argwhere(surface)
    values = compact[surface]
    if points.shape[0] > max_points:
        stride = math.ceil(points.shape[0] / max_points)
        points = points[::stride]
        values = values[::stride]
    return points, np.asarray(values)


def _map_metrics(compact: np.ndarray, threshold_sigma: float) -> dict[str, Any]:
    finite = _sample_finite(compact)
    if finite.size == 0:
        raise VisualError("The volume contains no finite density values.")
    mean = float(np.mean(finite))
    std = float(np.std(finite))
    threshold = mean + threshold_sigma * std
    finite_mask = np.isfinite(compact)
    binary = finite_mask & (compact >= threshold)
    connectivity = ndimage.generate_binary_structure(rank=3, connectivity=3)
    labels, components = ndimage.label(binary, structure=connectivity)
    component_voxels = np.bincount(labels[binary], minlength=components + 1)[1:]
    thresholded_voxels = int(np.sum(component_voxels))

    if components:
        voxel_order = np.argsort(component_voxels)[::-1]
        voxel_counts_descending = component_voxels[voxel_order]
        voxel_fractions_descending = voxel_counts_descending / thresholded_voxels
        voxel_coverage = np.cumsum(voxel_fractions_descending)
        components_covering_95_percent = int(np.searchsorted(voxel_coverage, 0.95) + 1)

        excess_density = np.asarray(compact[binary], dtype=np.float64) - threshold
        component_density = np.bincount(
            labels[binary],
            weights=excess_density,
            minlength=components + 1,
        )[1:]
        total_excess_density = float(np.sum(component_density))
        if total_excess_density > 0:
            density_fractions_descending = np.sort(component_density)[::-1] / total_excess_density
            density_coverage = np.cumsum(density_fractions_descending)
            density_components_covering_95_percent: int | None = int(
                np.searchsorted(density_coverage, 0.95) + 1
            )
            largest_density_fraction: float | None = float(density_fractions_descending[0])
        else:
            density_components_covering_95_percent = None
            largest_density_fraction = None

        significant_minimum_voxels = min(
            thresholded_voxels,
            max(8, math.ceil(0.01 * thresholded_voxels)),
        )
        significant_components = int(np.sum(component_voxels >= significant_minimum_voxels))
        largest_voxel_count: int | None = int(voxel_counts_descending[0])
        largest_voxel_fraction: float | None = float(voxel_fractions_descending[0])
        top_component_voxel_fractions = [
            float(item) for item in voxel_fractions_descending[:10]
        ]
    else:
        components_covering_95_percent = 0
        density_components_covering_95_percent = 0
        largest_density_fraction = None
        significant_minimum_voxels = 0
        significant_components = 0
        largest_voxel_count = None
        largest_voxel_fraction = None
        top_component_voxel_fractions = []

    finite_voxels = int(np.sum(finite_mask))
    zero_fraction = (
        float(np.sum((compact == 0) & finite_mask) / finite_voxels)
        if finite_voxels
        else None
    )
    return {
        "sample_mean": mean,
        "sample_std": std,
        "finite_fraction": float(np.mean(finite_mask)),
        "zero_fraction": zero_fraction,
        "threshold_sigma": threshold_sigma,
        "threshold_value": threshold,
        "fraction_above_threshold": float(np.mean(binary)),
        "thresholded_voxel_count": thresholded_voxels,
        "connected_components_at_threshold": int(components),
        "raw_connected_components_at_threshold": int(components),
        "component_connectivity": 26,
        "largest_component_voxel_count": largest_voxel_count,
        "largest_component_fraction": largest_voxel_fraction,
        "largest_component_fraction_of_thresholded_voxels": largest_voxel_fraction,
        "largest_component_fraction_of_thresholded_density": largest_density_fraction,
        "component_density_measure": "sum of (density - threshold) over thresholded voxels",
        "components_covering_95_percent": components_covering_95_percent,
        "components_covering_95_percent_of_thresholded_voxels": components_covering_95_percent,
        "components_covering_95_percent_of_thresholded_density": (
            density_components_covering_95_percent
        ),
        "significant_components_at_threshold": significant_components,
        "significant_connected_components_at_threshold": significant_components,
        "significant_component_minimum_voxels": significant_minimum_voxels,
        "significant_component_minimum_fraction": 0.01,
        "top_component_fractions_of_thresholded_voxels": top_component_voxel_fractions,
        "component_count_interpretation": (
            "The raw connected-component count includes every above-threshold speckle; assess "
            "fragmentation from dominant-component fractions, coverage, and significant components."
        ),
        "sample_percentiles": {
            str(percentile): float(np.percentile(finite, percentile))
            for percentile in (0.5, 1, 5, 50, 95, 99, 99.5)
        },
    }


def render_map_report(
    settings: Settings,
    project_dir: str,
    map_path: str,
    *,
    threshold_sigma: float = 2.0,
    display_lowpass_A: float | None = None,
    low_percentile: float = 0.5,
    high_percentile: float = 99.5,
) -> RenderedVisual:
    project = settings.validate_project_path(project_dir)
    resolved = _project_file(settings, project_dir, map_path, suffixes=(".mrc", ".mrcs"))
    if display_lowpass_A is not None:
        _check_volume_render_size(resolved, f"Map report source {resolved.name}")
    with mrcfile.mmap(resolved, permissive=True, mode="r") as handle:
        if handle.data.ndim != 3:
            raise VisualError(f"Expected a 3D map, got shape {handle.data.shape} in {resolved}.")
        info = _mrc_info(handle, resolved)
        display_volume, display_lowpass = _apply_display_lowpass(
            handle.data,
            info,
            display_lowpass_A,
        )
        compact = _downsample_volume(display_volume, max_side=224)

    metrics = _map_metrics(compact, threshold_sigma)
    vmin, vmax = _display_limits(compact, low_percentile, high_percentile)
    slices = _orthogonal_slices(compact)
    threshold = float(metrics["threshold_value"])
    projection_records = tuple(
        _central_slab_projection(
            compact,
            axis,
            fraction=0.25,
            threshold=threshold,
        )
        for axis in (0, 1, 2)
    )
    projection_samples = np.concatenate(
        [_sample_finite(projection) for projection, _, _ in projection_records]
    )
    projection_vmin, projection_vmax = _display_limits(
        projection_samples,
        low_percentile,
        high_percentile,
    )
    figure = Figure(figsize=(14, 10), constrained_layout=True)
    axes = figure.subplots(2, 4, squeeze=False)
    names = ("XY central", "XZ central", "YZ central")
    for column, (name, image) in enumerate(zip(names, slices, strict=True)):
        axes[0][column].imshow(image, cmap="gray", origin="lower", vmin=vmin, vmax=vmax)
        if np.nanmin(image) <= threshold <= np.nanmax(image):
            axes[0][column].contour(image, levels=[threshold], linewidths=0.6)
        axes[0][column].set_title(f"{name} slice + threshold contour")
        axes[0][column].axis("off")

    surface_axis = axes[0][3]
    surface_axis.remove()
    surface_axis_3d = figure.add_subplot(2, 4, 4, projection="3d")
    points, values = _surface_points(compact, threshold)
    if points.size:
        surface_axis_3d.scatter(
            points[:, 2],
            points[:, 1],
            points[:, 0],
            c=values,
            cmap="gray",
            s=0.35,
            linewidths=0,
            alpha=0.7,
        )
        surface_axis_3d.view_init(elev=22, azim=-55)
    surface_axis_3d.set_title("Threshold surface point rendering")
    surface_axis_3d.set_axis_off()

    projection_names = (
        "Z central-slab mean + 10% occupancy",
        "Y central-slab mean + 10% occupancy",
        "X central-slab mean + 10% occupancy",
    )
    for column, (name, projection_record) in enumerate(
        zip(projection_names, projection_records, strict=True)
    ):
        image, occupancy_projection, _ = projection_record
        axes[1][column].imshow(
            image,
            cmap="gray",
            origin="lower",
            vmin=projection_vmin,
            vmax=projection_vmax,
        )
        if (
            occupancy_projection is not None
            and np.nanmin(occupancy_projection) <= 0.1 <= np.nanmax(occupancy_projection)
        ):
            axes[1][column].contour(
                occupancy_projection,
                levels=[0.1],
                colors="tab:orange",
                linewidths=0.65,
            )
        axes[1][column].set_title(name)
        axes[1][column].axis("off")

    finite = _sample_finite(compact)
    axes[1][3].hist(finite, bins=120, log=True)
    axes[1][3].axvline(threshold, linestyle="--", label=f"mean + {threshold_sigma:g}σ")
    axes[1][3].legend(fontsize=8)
    axes[1][3].set_title("Sampled voxel-value distribution")
    axes[1][3].set_xlabel("density")
    axes[1][3].set_ylabel("count (log)")

    view_label = (
        f"{display_lowpass['cutoff_resolution_A']:g} Å display low-pass"
        if display_lowpass["applied"]
        else "raw map view"
    )
    figure.suptitle(
        f"RELION map report — {_relative(project, resolved)} — {view_label}",
        fontsize=14,
    )
    return _finish(
        figure,
        {
            "report_type": "relion_map_report",
            "project_dir": str(project),
            "map": _relative(project, resolved),
            "shape_zyx": list(info.shape),
            "dtype": info.dtype,
            "voxel_size_A_xyz": list(info.voxel_size_A) if info.voxel_size_A else None,
            "metrics_on_downsampled_volume": metrics,
            "metrics_volume": (
                "display_lowpass" if display_lowpass["applied"] else "raw_source"
            ),
            "display": {
                "low_percentile": low_percentile,
                "high_percentile": high_percentile,
                "lowpass": display_lowpass,
                "projection": {
                    "type": "central_slab_mean_with_thresholded_occupancy_contour",
                    "slab_fraction": 0.25,
                    "occupancy_threshold_sigma": threshold_sigma,
                    "occupancy_contour_fraction": 0.1,
                    "raw_maximum_projection_used": False,
                },
            },
            "scientific_note": (
                "This report provides central slices, central-slab mean and threshold-occupancy projections, and an "
                "approximate threshold-surface view. The raw component count includes tiny speckles and is not by "
                "itself evidence of fragmentation; use dominant-component and coverage metrics. Map comparison still "
                "requires a shared physical grid, common filtering/contouring, and independent biological evidence. "
                "Any requested Angstrom-defined low-pass is display-only, leaves the source map unchanged, and does "
                "not by itself test reference bias."
            ),
        },
        settings,
    )


def render_mask_report(
    settings: Settings,
    project_dir: str,
    map_path: str,
    mask_path: str,
    *,
    density_threshold_sigma: float = 2.0,
    low_percentile: float = 0.5,
    high_percentile: float = 99.5,
) -> RenderedVisual:
    project = settings.validate_project_path(project_dir)
    resolved_map = _project_file(settings, project_dir, map_path, suffixes=(".mrc", ".mrcs"))
    resolved_mask = _project_file(settings, project_dir, mask_path, suffixes=(".mrc", ".mrcs"))
    with mrcfile.mmap(resolved_map, permissive=True, mode="r") as map_handle, mrcfile.mmap(
        resolved_mask, permissive=True, mode="r"
    ) as mask_handle:
        if map_handle.data.ndim != 3 or mask_handle.data.ndim != 3:
            raise VisualError("Map and mask must both be 3D MRC volumes.")
        if tuple(map_handle.data.shape) != tuple(mask_handle.data.shape):
            raise VisualError(
                f"Map/mask grids differ: {map_handle.data.shape} versus {mask_handle.data.shape}."
            )
        map_info = _mrc_info(map_handle, resolved_map)
        mask_info = _mrc_info(mask_handle, resolved_mask)
        _assert_same_grid(map_info, mask_info, "Map/mask overlay")
        compact_map = _downsample_volume(map_handle.data, max_side=224)
        compact_mask = _downsample_volume(mask_handle.data, max_side=224)

    if compact_map.shape != compact_mask.shape:
        common = tuple(min(a, b) for a, b in zip(compact_map.shape, compact_mask.shape, strict=True))
        compact_map = compact_map[: common[0], : common[1], : common[2]]
        compact_mask = compact_mask[: common[0], : common[1], : common[2]]

    map_finite = _sample_finite(compact_map)
    if map_finite.size == 0:
        raise VisualError("The map contains no finite density values.")
    mean = float(np.mean(map_finite))
    std = float(np.std(map_finite))
    density_threshold = mean + density_threshold_sigma * std
    credible_density = np.isfinite(compact_map) & (compact_map >= density_threshold)
    mask_binary = np.isfinite(compact_mask) & (compact_mask >= 0.5)
    soft_edge = np.isfinite(compact_mask) & (compact_mask > 0.05) & (compact_mask < 0.95)
    _, components = ndimage.label(mask_binary)
    touches_boundary = bool(
        np.any(mask_binary[0])
        or np.any(mask_binary[-1])
        or np.any(mask_binary[:, 0])
        or np.any(mask_binary[:, -1])
        or np.any(mask_binary[:, :, 0])
        or np.any(mask_binary[:, :, -1])
    )
    clipped_fraction = (
        float(np.sum(credible_density & ~mask_binary) / np.sum(credible_density))
        if np.any(credible_density)
        else 0.0
    )
    boundary_distance = ndimage.distance_transform_edt(mask_binary)
    credible_inside = credible_density & mask_binary
    minimum_clearance = float(np.min(boundary_distance[credible_inside])) if np.any(credible_inside) else None

    vmin, vmax = _display_limits(compact_map, low_percentile, high_percentile)
    map_slices = _orthogonal_slices(compact_map)
    mask_slices = _orthogonal_slices(compact_mask)
    figure = Figure(figsize=(14, 8), constrained_layout=True)
    axes = figure.subplots(2, 3, squeeze=False)
    names = ("XY", "XZ", "YZ")
    for column, (name, map_slice, mask_slice) in enumerate(
        zip(names, map_slices, mask_slices, strict=True)
    ):
        axes[0][column].imshow(map_slice, cmap="gray", origin="lower", vmin=vmin, vmax=vmax)
        if np.nanmin(mask_slice) <= 0.5 <= np.nanmax(mask_slice):
            axes[0][column].contour(mask_slice, levels=[0.5], linewidths=1.0)
        axes[0][column].set_title(f"{name}: map with mask=0.5 contour")
        axes[0][column].axis("off")

        axes[1][column].imshow(mask_slice, cmap="gray", origin="lower", vmin=0, vmax=1)
        if np.nanmin(map_slice) <= density_threshold <= np.nanmax(map_slice):
            axes[1][column].contour(map_slice, levels=[density_threshold], linewidths=0.8)
        axes[1][column].set_title(f"{name}: mask with credible-density contour")
        axes[1][column].axis("off")

    figure.suptitle(
        f"RELION mask report — map {resolved_map.name} — mask {resolved_mask.name}",
        fontsize=14,
    )
    metrics = {
        "mask_fraction_at_0_5": float(np.mean(mask_binary)),
        "soft_edge_fraction_0_05_to_0_95": float(np.mean(soft_edge)),
        "connected_components_at_0_5": int(components),
        "mask_touches_box_boundary": touches_boundary,
        "density_threshold_sigma": density_threshold_sigma,
        "density_threshold_value": density_threshold,
        "credible_density_fraction_outside_mask": clipped_fraction,
        "minimum_credible_density_clearance_downsampled_voxels": minimum_clearance,
    }
    return _finish(
        figure,
        {
            "report_type": "relion_mask_report",
            "project_dir": str(project),
            "map": _relative(project, resolved_map),
            "mask": _relative(project, resolved_mask),
            "shape_zyx": list(map_info.shape),
            "voxel_size_A_xyz": list(map_info.voxel_size_A) if map_info.voxel_size_A else None,
            "metrics_on_downsampled_volume": metrics,
            "scientific_note": (
                "Inspect for clipped credible density, tight high-frequency contours, disconnected regions, and inadequate "
                "soft edges. Validate the mask with masked, unmasked, and phase-randomised FSC curves."
            ),
        },
        settings,
    )


def _fsc_resolution_at_threshold(frequency: np.ndarray, fsc: np.ndarray, threshold: float) -> float | None:
    finite = np.isfinite(frequency) & np.isfinite(fsc) & (frequency > 0)
    x = frequency[finite]
    y = fsc[finite]
    if x.size < 2:
        return None
    order = np.argsort(x)
    x = x[order]
    y = y[order]
    above = np.where(y >= threshold)[0]
    if above.size == 0:
        return None
    index = int(above[-1])
    frequency_value = float(x[index])
    if index + 1 < x.size and y[index + 1] < threshold and y[index] != y[index + 1]:
        fraction = (threshold - y[index]) / (y[index + 1] - y[index])
        frequency_value = float(x[index] + fraction * (x[index + 1] - x[index]))
    return 1.0 / frequency_value if frequency_value > 0 else None


def render_fsc_report(
    settings: Settings,
    project_dir: str,
    postprocess_star: str,
) -> RenderedVisual:
    project = settings.validate_project_path(project_dir)
    star_path = _project_file(settings, project_dir, postprocess_star, suffixes=(".star",))
    blocks = parse_star(star_path)
    curve_columns = (
        "rlnFourierShellCorrelationCorrected",
        "rlnFourierShellCorrelationMaskedMaps",
        "rlnFourierShellCorrelationUnmaskedMaps",
        "rlnFourierShellCorrelationPhaseRandomizedMaskedMaps",
    )
    block_name, block = _find_block(blocks, any_of=curve_columns)

    if "rlnResolution" in block:
        frequency = np.asarray([float(item) for item in _as_list(block["rlnResolution"])], dtype=float)
        x_source = "rlnResolution"
    elif "rlnAngstromResolution" in block:
        angstrom = np.asarray([float(item) for item in _as_list(block["rlnAngstromResolution"])], dtype=float)
        frequency = np.divide(1.0, angstrom, out=np.full_like(angstrom, np.nan), where=angstrom > 0)
        x_source = "1/rlnAngstromResolution"
    else:
        raise VisualError("The FSC STAR block has no rlnResolution or rlnAngstromResolution column.")

    labels = {
        "rlnFourierShellCorrelationCorrected": "corrected",
        "rlnFourierShellCorrelationMaskedMaps": "masked",
        "rlnFourierShellCorrelationUnmaskedMaps": "unmasked",
        "rlnFourierShellCorrelationPhaseRandomizedMaskedMaps": "phase-randomised masked",
    }
    curves: dict[str, np.ndarray] = {}
    for key in curve_columns:
        if key in block:
            values = np.asarray([float(item) for item in _as_list(block[key])], dtype=float)
            if values.size != frequency.size:
                raise VisualError(
                    f"FSC column {key} has {values.size} rows; expected {frequency.size}."
                )
            curves[labels[key]] = values
    if not curves:
        raise VisualError("No recognised FSC curves were found.")

    final_resolution = None
    for candidate_block in blocks.values():
        if "rlnFinalResolution" in candidate_block:
            final_resolution = _as_float(candidate_block["rlnFinalResolution"])
            if final_resolution is not None:
                break

    figure = Figure(figsize=(10, 6.5), constrained_layout=True)
    axis = figure.add_subplot(1, 1, 1)
    for name, values in curves.items():
        axis.plot(frequency, values, label=name)
    axis.axhline(0.143, linestyle="--", linewidth=1.0, label="FSC=0.143")
    axis.axhline(0.5, linestyle=":", linewidth=0.8, label="FSC=0.5")
    if final_resolution and final_resolution > 0:
        axis.axvline(1.0 / final_resolution, linestyle="-.", linewidth=1.0, label=f"reported {final_resolution:.2f} Å")
    axis.set_xlabel("spatial frequency (1/Å)")
    axis.set_ylabel("FSC")
    axis.set_ylim(-0.1, 1.05)
    axis.grid(True, alpha=0.25)
    axis.legend(fontsize=9)
    axis.set_title(f"RELION FSC report — {star_path.name}")

    threshold_resolutions: dict[str, dict[str, float | None]] = {}
    for name, values in curves.items():
        threshold_resolutions[name] = {
            "resolution_at_0_143_A": _fsc_resolution_at_threshold(frequency, values, 0.143),
            "resolution_at_0_5_A": _fsc_resolution_at_threshold(frequency, values, 0.5),
        }
    phase_name = "phase-randomised masked"
    phase_at_reported = None
    if final_resolution and phase_name in curves and frequency.size:
        index = int(np.nanargmin(np.abs(frequency - (1.0 / final_resolution))))
        phase_at_reported = float(curves[phase_name][index])

    return _finish(
        figure,
        {
            "report_type": "relion_fsc_report",
            "project_dir": str(project),
            "postprocess_star": _relative(project, star_path),
            "star_block": block_name,
            "frequency_source": x_source,
            "points": int(frequency.size),
            "curves": list(curves),
            "reported_final_resolution_A": final_resolution,
            "threshold_estimates": threshold_resolutions,
            "phase_randomised_fsc_near_reported_resolution": phase_at_reported,
            "scientific_note": (
                "Compare corrected, masked, unmasked, and phase-randomised behaviour. A smaller nominal resolution "
                "number is not sufficient evidence of improvement when masking artefacts or visual deterioration appear."
            ),
        },
        settings,
    )


def _zscore(array: np.ndarray) -> np.ndarray:
    finite = np.isfinite(array)
    if not np.any(finite):
        return np.zeros_like(array, dtype=np.float32)
    mean = float(np.mean(array[finite]))
    std = float(np.std(array[finite]))
    if std <= np.finfo(float).eps:
        return np.asarray(array - mean, dtype=np.float32)
    return np.asarray((array - mean) / std, dtype=np.float32)


def _map_similarity(reference: np.ndarray, candidate: np.ndarray) -> dict[str, float | None]:
    finite = np.isfinite(reference) & np.isfinite(candidate)
    if np.sum(finite) < 2:
        return {"pearson_correlation": None, "zscore_rms_difference": None}
    x = np.asarray(reference[finite], dtype=np.float64)
    y = np.asarray(candidate[finite], dtype=np.float64)
    x_std = float(np.std(x))
    y_std = float(np.std(y))
    correlation = None
    if x_std > 0 and y_std > 0:
        correlation = float(np.corrcoef(x, y)[0, 1])
    xz = (x - np.mean(x)) / (x_std or 1.0)
    yz = (y - np.mean(y)) / (y_std or 1.0)
    return {
        "pearson_correlation": correlation,
        "zscore_rms_difference": float(np.sqrt(np.mean((xz - yz) ** 2))),
    }


def _validate_symmetry_scan_grid(info: _MrcInfo, label: str) -> float:
    if info.voxel_size_A is None:
        raise VisualError(f"{label} requires positive MRC voxel size metadata.")
    if sorted(info.axis_mapping) != [1, 2, 3]:
        raise VisualError(
            f"{label} requires MRC mapc/mapr/maps to be a permutation of 1, 2, 3."
        )
    voxel = np.asarray(info.voxel_size_A, dtype=np.float64)
    if not np.all(np.isfinite(voxel)) or np.any(voxel <= 0):
        raise VisualError(f"{label} requires finite positive MRC voxel sampling.")
    if not np.allclose(voxel, voxel[0], rtol=1e-6, atol=1e-6):
        raise VisualError(
            f"{label} requires isotropic voxel sampling; got {tuple(float(item) for item in voxel)} Å."
        )
    if not np.all(np.isfinite(np.asarray(info.origin_A, dtype=np.float64))):
        raise VisualError(f"{label} has non-finite MRC origin metadata.")
    return float(voxel[0])


def _canonical_physical_transpose(info: _MrcInfo) -> tuple[int, int, int]:
    """Return the source-axis transpose into physical Z/Y/X order."""
    mapc, mapr, maps = info.axis_mapping
    source_physical_axes = (maps, mapr, mapc)
    return tuple(source_physical_axes.index(axis) for axis in (3, 2, 1))


def _canonical_physical_zyx(
    volume: np.ndarray,
    info: _MrcInfo,
) -> tuple[np.ndarray, tuple[int, int, int]]:
    """Transpose MRC section/row/column axes into physical Z/Y/X order."""
    transpose_axes = _canonical_physical_transpose(info)
    return np.transpose(volume, axes=transpose_axes), transpose_axes


def _validate_symmetry_scan_volume(volume: np.ndarray, label: str) -> dict[str, float]:
    if not np.all(np.isfinite(volume)):
        raise VisualError(f"{label} contains non-finite voxels.")
    minimum = float(np.min(volume))
    maximum = float(np.max(volume))
    if maximum <= minimum:
        raise VisualError(f"{label} must contain non-constant density.")
    return {"minimum": minimum, "maximum": maximum}


def _validate_scan_integer(
    value: int,
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise VisualError(f"{name} must be an integer.")
    if not minimum <= value <= maximum:
        raise VisualError(f"{name} must be between {minimum} and {maximum}.")
    return value


def _symmetry_candidate_label(candidate: Mapping[str, Any], index: int) -> str:
    value = candidate.get("group")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return f"candidate {index + 1}"


def _symmetry_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 0 else None


def _symmetry_string_list(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [
        item.strip()
        for item in value
        if isinstance(item, str) and item.strip()
    ]


def _symmetry_hypothesis_records(value: Any) -> dict[str, dict[str, Any]]:
    """Normalize v2/vNext per-group diagnostics for safe presentation."""
    records: dict[str, dict[str, Any]] = {}
    if isinstance(value, Mapping):
        items = value.items()
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        items = ((None, item) for item in value)
    else:
        return records
    for key, value_record in items:
        if not isinstance(value_record, Mapping):
            continue
        record = dict(value_record)
        raw_group = record.get("group", key)
        if not isinstance(raw_group, str) or not raw_group.strip():
            continue
        group = raw_group.strip()
        record.setdefault("group", group)
        records[group] = record
    return records


def _symmetry_report_contract(
    scan_result: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    *,
    requested_analysis_lowpass_A: float,
    effective_analysis_lowpass_A: float,
    focus_groups: Sequence[str],
    candidate_limit: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]], dict[str, Any]]:
    """Expose the core contract prominently, with a narrow legacy fallback."""
    raw_summary = scan_result.get("scan_summary")
    scan_summary = dict(raw_summary) if isinstance(raw_summary, Mapping) else {}
    raw_completeness = scan_result.get("result_completeness")
    completeness = (
        dict(raw_completeness) if isinstance(raw_completeness, Mapping) else {}
    )
    search_value = scan_result.get("search")
    search = search_value if isinstance(search_value, Mapping) else {}
    hypotheses = _symmetry_hypothesis_records(scan_result.get("hypothesis_results"))
    displayed_groups: set[str] = set()
    for candidate in candidates:
        group = candidate.get("group")
        if not isinstance(group, str) or not group.strip():
            continue
        group = group.strip()
        displayed_groups.add(group)
        hypotheses.setdefault(
            group,
            {
                "group": group,
                "coverage": "detailed_candidate_returned",
                "support_status": candidate.get("support_status", "unknown"),
                "scan_heuristic_outcome": candidate.get(
                    "scan_heuristic_outcome",
                    "pass" if candidate.get("half_map_consistent") else "unknown",
                ),
                "reporting_state": "displayed",
            },
        )
    for group in focus_groups:
        hypotheses.setdefault(
            group,
            {
                "group": group,
                "coverage": "unknown_from_core_output",
                "support_status": "unknown",
                "reporting_state": "not_reported",
                "absence_is_failure": False,
            },
        )

    displayed_supported_count = _symmetry_nonnegative_int(
        completeness.get("displayed_supported_candidate_count")
    )
    if displayed_supported_count is None:
        displayed_supported_count = len(candidates)
    supported_count = _symmetry_nonnegative_int(
        completeness.get("supported_candidate_count")
    )
    detailed_shown_count = _symmetry_nonnegative_int(
        completeness.get("displayed_candidate_count")
    )
    if detailed_shown_count is None:
        detailed_shown_count = len(candidates)
    raw_passed_count = _symmetry_nonnegative_int(
        completeness.get("raw_scan_heuristic_pass_count")
    )
    status = str(scan_result.get("status", "untestable"))
    if supported_count is None:
        supported_count = _symmetry_nonnegative_int(
            search.get("supported_candidate_count_before_cap")
        )
    if raw_passed_count is None:
        raw_passed_count = _symmetry_nonnegative_int(
            search.get("raw_scan_heuristic_pass_count_before_cap")
        )
    if raw_passed_count is None:
        raw_passed_count = supported_count
    raw_truncated = completeness.get(
        "display_truncated", search.get("candidate_cap_truncated")
    )
    details_truncated = raw_truncated if isinstance(raw_truncated, bool) else False
    if supported_count is None:
        if status in {"no_supported_candidate", "untestable", "inconclusive"}:
            supported_count = 0
        elif not details_truncated:
            supported_count = displayed_supported_count
    if raw_passed_count is None and not details_truncated:
        raw_passed_count = detailed_shown_count
    omitted_count = _symmetry_nonnegative_int(
        completeness.get("omitted_raw_scan_heuristic_pass_count")
    )
    if omitted_count is None:
        omitted_count = (
            max(0, raw_passed_count - detailed_shown_count)
            if raw_passed_count is not None
            else None
        )
    if omitted_count:
        details_truncated = True

    omitted_groups = _symmetry_string_list(
        completeness.get(
            "omitted_raw_scan_heuristic_pass_groups",
            completeness.get("omitted_supported_groups"),
        )
    )
    if not omitted_groups:
        for group, record in hypotheses.items():
            if (
                record.get("support_status") == "supported"
                and group not in displayed_groups
            ):
                omitted_groups.append(group)

    focus_diagnostics: dict[str, dict[str, Any]] = {}
    for group in focus_groups:
        record = hypotheses.get(group, {})
        focus_diagnostics[group] = {
            "scan_heuristic_outcome": record.get(
                "scan_heuristic_outcome", record.get("outcome", "unknown")
            ),
            "support_confounds": _symmetry_string_list(
                record.get("support_confounds")
            ),
            "support_failures": _symmetry_string_list(
                record.get("support_failures")
            ),
        }

    high_order_value = search.get("high_order_diagnostic")
    high_order_diagnostic = (
        high_order_value if isinstance(high_order_value, Mapping) else {}
    )
    if high_order_diagnostic:
        if high_order_diagnostic.get("assessable") is not True:
            high_order_state = "unavailable"
        elif high_order_diagnostic.get("monotonic_small_angle_ramp") is True:
            high_order_state = "ramp_confounded"
        elif high_order_diagnostic.get("high_flat_support_plateau") is True:
            high_order_state = "plateau_confounded"
        else:
            high_order_state = "clear"
    else:
        high_order_state = "unknown"

    preprocessing_value = scan_result.get("preprocessing")
    preprocessing = (
        preprocessing_value if isinstance(preprocessing_value, Mapping) else {}
    )
    raw_center = preprocessing.get("center_estimation")
    center_estimation = dict(raw_center) if isinstance(raw_center, Mapping) else {}
    center_stable = center_estimation.get("stable")
    center_validity = (
        "stable"
        if center_stable is True
        else "unstable"
        if center_stable is False
        else "not_validated"
    )
    center = {
        "mode": center_estimation.get(
            "method", preprocessing.get("symmetry_center", "geometric_array_center")
        ),
        "validity": center_validity,
        "stable": center_stable if isinstance(center_stable, bool) else None,
        "shift_from_geometric_center_A": center_estimation.get(
            "shift_from_geometric_center_A"
        ),
        "half_map_center_disagreement_A": center_estimation.get(
            "half_map_center_disagreement_A"
        ),
        "negative_symmetry_claims_permitted": False,
    }

    if status == "ambiguous":
        headline = "AMBIGUOUS SCAN HEURISTICS"
        ranking_outcome = "ambiguous"
    elif status == "inconclusive":
        headline = "SCAN INCONCLUSIVE"
        ranking_outcome = "not_applicable"
    elif status == "untestable":
        headline = "SCAN UNTESTABLE"
        ranking_outcome = "not_applicable"
    elif status == "no_supported_candidate":
        headline = "NO HYPOTHESIS PASSED SCAN HEURISTICS"
        ranking_outcome = "not_applicable"
    else:
        headline = "EXPLORATORY SCAN SHORTLIST"
        ranking_outcome = "separated"

    scan_summary.setdefault("ranking_outcome", ranking_outcome)
    scan_summary.update(
        {
            "biological_conclusion": "unresolved",
            "processing_baseline": "C1",
            "permits_symmetry_acceptance": False,
            "permits_symmetry_rejection": False,
            "candidate_absence_is_failure": False,
            "required_confirmation": "matched_C1_and_candidate_refinement_branches",
        }
    )
    completeness.setdefault("displayed_candidate_limit", candidate_limit)
    completeness.setdefault(
        "displayed_supported_candidate_count", displayed_supported_count
    )
    completeness.setdefault("supported_candidate_count", supported_count)
    completeness.setdefault("displayed_candidate_count", detailed_shown_count)
    completeness.setdefault("raw_scan_heuristic_pass_count", raw_passed_count)
    completeness.setdefault("display_truncated", details_truncated)
    completeness.setdefault("omitted_supported_groups", omitted_groups)

    raw_count_text = (
        str(raw_passed_count)
        if raw_passed_count is not None
        else f"at least {detailed_shown_count}"
    )
    omitted_text = (
        str(omitted_count) if omitted_count is not None else "unknown"
    )
    interpretation = {
        "headline": headline,
        "summary_text": (
            f"{headline}: {raw_count_text} raw hypothesis/hypotheses passed reproducibility "
            f"heuristics; {supported_count if supported_count is not None else 'unknown'} have "
            f"order-specific support; {detailed_shown_count} detailed and {omitted_text} omitted. "
            "Biological symmetry is "
            "unresolved; this scan cannot accept or reject it. Absence from the "
            "capped candidates array is never a failure verdict."
        ),
        "hypotheses_passing_scan_heuristics": raw_passed_count,
        "raw_hypotheses_passing_scan_heuristics": raw_passed_count,
        "order_specific_supported_hypotheses": supported_count,
        "detailed_candidates_shown": detailed_shown_count,
        "detailed_candidates_omitted": omitted_count,
        "omitted_passing_groups": omitted_groups,
        "focus_groups": list(focus_groups),
        "focus_diagnostics": focus_diagnostics,
        "high_order_control": {
            "state": high_order_state,
            "assessable": high_order_diagnostic.get("assessable"),
            "monotonic_small_angle_ramp": high_order_diagnostic.get(
                "monotonic_small_angle_ramp"
            ),
            "high_flat_support_plateau": high_order_diagnostic.get(
                "high_flat_support_plateau"
            ),
        },
        "requested_analysis_lowpass_A": requested_analysis_lowpass_A,
        "effective_analysis_lowpass_A": effective_analysis_lowpass_A,
        "requested_cutoff_honored": preprocessing.get("requested_cutoff_honored"),
        "negative_inference_valid": scan_summary.get("negative_inference_valid"),
        "center": center,
        "candidate_absence_is_failure": False,
        "biological_conclusion": "unresolved",
        "permits_symmetry_acceptance": False,
        "permits_symmetry_rejection": False,
    }
    return scan_summary, completeness, hypotheses, interpretation


def _symmetry_scan_figure(
    half_map_1: np.ndarray,
    half_map_2: np.ndarray,
    candidates: Sequence[Mapping[str, Any]],
    *,
    requested_analysis_lowpass_A: float,
    effective_analysis_lowpass_A: float,
    status: str,
    search: Mapping[str, Any] | None,
    interpretation: Mapping[str, Any],
    hypothesis_results: Mapping[str, Mapping[str, Any]],
) -> Figure:
    display_1 = _downsample_volume(half_map_1, max_side=192)
    display_2 = _downsample_volume(half_map_2, max_side=192)
    display_average = np.asarray((display_1 + display_2) / 2.0, dtype=np.float32)
    combined = np.concatenate(
        [display_1.reshape(-1), display_2.reshape(-1), display_average.reshape(-1)]
    )
    vmin, vmax = _display_limits(combined, 0.5, 99.5)

    figure = Figure(figsize=(14, 8.5), constrained_layout=True)
    axes = figure.subplots(2, 3, squeeze=False)
    for axis, (title, volume) in zip(
        axes[0],
        (
            ("Half-map 1: raw central XY", display_1),
            ("Half-map 2: raw central XY", display_2),
            ("Raw half-map average: central XY", display_average),
        ),
        strict=True,
    ):
        axis.imshow(
            _orthogonal_slices(volume)[0],
            cmap="gray",
            origin="lower",
            vmin=vmin,
            vmax=vmax,
        )
        axis.set_title(title)
        axis.axis("off")

    labels = [
        _symmetry_candidate_label(candidate, index)
        for index, candidate in enumerate(candidates)
    ]
    positions = np.arange(len(labels), dtype=np.float64)
    score_keys = (
        ("half1_score", "half 1", "#4c78a8"),
        ("half2_score", "half 2", "#f58518"),
        ("cross_half_score", "cross-half", "#54a24b"),
    )
    width = 0.24
    plotted = False
    for offset, (key, label, color) in zip((-width, 0.0, width), score_keys, strict=True):
        values = [_as_float(candidate.get(key)) for candidate in candidates]
        if any(value is not None for value in values):
            axes[1][0].bar(
                positions + offset,
                [0.0 if value is None else value for value in values],
                width=width,
                label=label,
                color=color,
            )
            plotted = True
    axes[1][0].set_title("Detailed raw reproducibility passes")
    axes[1][0].set_xticks(positions, labels, rotation=35, ha="right")
    axes[1][0].axhline(0.0, color="black", linewidth=0.6)
    if plotted:
        axes[1][0].legend(fontsize=8)
    elif not candidates:
        axes[1][0].text(
            0.5,
            0.5,
            "No detailed passing hypothesis shown.\n"
            "This is not a biological symmetry rejection.",
            ha="center",
            va="center",
        )

    agreement = [_as_float(candidate.get("agreement_score")) for candidate in candidates]
    half_map_consistent = [
        bool(candidate.get("half_map_consistent")) for candidate in candidates
    ]
    for index, (label, value, is_consistent) in enumerate(
        zip(labels, agreement, half_map_consistent, strict=True)
    ):
        if value is None:
            continue
        axes[1][1].scatter(
            index,
            value,
            color="#54a24b" if is_consistent else "#e45756",
            marker="o" if is_consistent else "x",
            s=55,
        )
    axes[1][1].set_title("Raw half-map replication (not order-specific support)")
    axes[1][1].set_xticks(positions, labels, rotation=35, ha="right")
    axes[1][1].axhline(0.0, color="black", linewidth=0.6)

    axes[1][2].axis("off")
    passing_count = interpretation.get(
        "raw_hypotheses_passing_scan_heuristics",
        interpretation.get("hypotheses_passing_scan_heuristics"),
    )
    supported_count = interpretation.get("order_specific_supported_hypotheses")
    shown_count = interpretation.get("detailed_candidates_shown", len(candidates))
    omitted_count = interpretation.get("detailed_candidates_omitted")
    passing_text = "unknown" if passing_count is None else str(passing_count)
    supported_text = "unknown" if supported_count is None else str(supported_count)
    omitted_text = "unknown" if omitted_count is None else str(omitted_count)
    omitted_groups = _symmetry_string_list(
        interpretation.get("omitted_passing_groups")
    )
    center_value = interpretation.get("center")
    center = center_value if isinstance(center_value, Mapping) else {}
    center_validity = str(center.get("validity", "not_validated")).replace("_", " ")
    center_mode = str(center.get("mode", "geometric_array_center")).replace("_", " ")
    summary_lines = [
        str(interpretation.get("headline", f"SCAN STATUS: {status}")),
        f"Legacy status: {status}",
        f"Raw reproducibility passes: {passing_text}",
        f"Order-specific support: {supported_text}",
        f"Detailed: {shown_count} shown; {omitted_text} omitted",
        f"Requested low-pass: {requested_analysis_lowpass_A:g} Å",
        f"Effective low-pass: {effective_analysis_lowpass_A:g} Å",
        f"Center: {center_mode}; validity {center_validity}",
    ]
    if omitted_groups:
        shown_groups = ", ".join(omitted_groups[:6])
        if len(omitted_groups) > 6:
            shown_groups += ", …"
        summary_lines.append(f"Omitted raw-pass groups: {shown_groups}")
    high_order_control = interpretation.get("high_order_control")
    if isinstance(high_order_control, Mapping):
        high_order_state = str(high_order_control.get("state", "unknown"))
        if high_order_state != "unknown":
            summary_lines.append(
                f"High-order control: {high_order_state.replace('_', ' ')}"
            )
    focus_groups = _symmetry_string_list(interpretation.get("focus_groups"))
    if focus_groups:
        focus_diagnostics = interpretation.get("focus_diagnostics")
        focus_diagnostics = (
            focus_diagnostics if isinstance(focus_diagnostics, Mapping) else {}
        )
        reason_labels = {
            "high_order_control_not_assessable": "high-order control unavailable",
            "finite_order_confounded_by_high_order_small_angle_ramp": "high-order ramp",
            "finite_order_confounded_by_high_flat_support_plateau": "high-order plateau",
            "perpendicular_c2_support_failed": "perpendicular-C2 gate failed",
        }
        for group in focus_groups:
            record = hypothesis_results.get(group, {})
            outcome = record.get(
                "scan_heuristic_outcome",
                record.get("outcome", "unknown"),
            )
            detail = focus_diagnostics.get(group, {})
            detail = detail if isinstance(detail, Mapping) else {}
            reasons = _symmetry_string_list(detail.get("support_confounds"))
            if not reasons:
                reasons = _symmetry_string_list(detail.get("support_failures"))
            reason_text = (
                f" [{reason_labels.get(reasons[0], reasons[0].replace('_', ' '))}]"
                if reasons
                else ""
            )
            summary_lines.append(
                f"Focus raw {group}: {str(outcome).replace('_', ' ')}{reason_text}"
            )
    if search:
        stage_1 = search.get("stage1_orders") or search.get("stage_1_orders")
        stage_2 = search.get("stage2_orders") or search.get("stage_2_orders")
        if stage_1 is not None:
            summary_lines.append(f"Stage 1 orders: {stage_1}")
        if stage_2 is not None:
            summary_lines.append(f"Stage 2 orders: {stage_2}")
    summary_lines.extend(
        [
            "",
            "Biological symmetry: UNRESOLVED.",
            "This scan cannot accept or reject symmetry.",
            "Absence from candidates is never failure.",
            "Confirm with matched C1/symmetry branches.",
        ]
    )
    axes[1][2].text(
        0.02,
        0.98,
        "\n".join(summary_lines),
        ha="left",
        va="top",
        fontsize=8.2 if len(focus_groups) > 4 else 9.5,
        bbox={
            "boxstyle": "round,pad=0.5",
            "facecolor": "#fff4df",
            "edgecolor": "#d17a00",
        },
    )

    figure.suptitle(
        "C1 half-map symmetry scan — exploratory scan heuristics only",
        fontsize=14,
    )
    return figure


def render_symmetry_scan(
    settings: Settings,
    project_dir: str,
    half_map_1: str,
    half_map_2: str,
    analysis_lowpass_A: float,
    *,
    mask_path: str | None = None,
    common_max_order: int = 8,
    max_order: int = 32,
    stage2_window: int = 2,
    max_candidates: int = 5,
    focus_groups: list[str] | None = None,
) -> RenderedVisual:
    """Scan a C1 half-map pair and report non-decisive heuristic evidence."""
    project = settings.validate_project_path(project_dir)
    path_1 = _project_file(settings, project_dir, half_map_1, suffixes=(".mrc", ".mrcs"))
    path_2 = _project_file(settings, project_dir, half_map_2, suffixes=(".mrc", ".mrcs"))
    if path_1 == path_2 or path_1.samefile(path_2):
        raise VisualError(
            "half_map_1 and half_map_2 must have different filesystem identities."
        )
    half_map_1_sha256 = _file_sha256(path_1)
    half_map_2_sha256 = _file_sha256(path_2)
    if half_map_1_sha256 == half_map_2_sha256:
        raise VisualError(
            "half_map_1 and half_map_2 have identical file content; independent-half "
            "provenance cannot be evaluated."
        )
    mask_resolved = (
        _project_file(settings, project_dir, mask_path, suffixes=(".mrc", ".mrcs"))
        if mask_path is not None
        else None
    )

    if isinstance(analysis_lowpass_A, bool):
        raise VisualError("analysis_lowpass_A must be a finite positive Angstrom value.")
    try:
        lowpass_A = float(analysis_lowpass_A)
    except (TypeError, ValueError) as exc:
        raise VisualError("analysis_lowpass_A must be a finite positive Angstrom value.") from exc
    if not math.isfinite(lowpass_A) or lowpass_A <= 0:
        raise VisualError("analysis_lowpass_A must be a finite positive Angstrom value.")

    common_order = _validate_scan_integer(
        common_max_order,
        "common_max_order",
        minimum=2,
        maximum=16,
    )
    maximum_order = _validate_scan_integer(max_order, "max_order", minimum=2, maximum=64)
    if maximum_order < common_order:
        raise VisualError("max_order must be greater than or equal to common_max_order.")
    stage_2_window = _validate_scan_integer(stage2_window, "stage2_window", minimum=1, maximum=8)
    candidate_limit = _validate_scan_integer(
        max_candidates,
        "max_candidates",
        minimum=1,
        maximum=min(8, settings.visual_max_panels),
    )

    paths = [path_1, path_2] + ([mask_resolved] if mask_resolved is not None else [])
    infos: list[_MrcInfo] = []
    for index, path in enumerate(paths):
        with mrcfile.mmap(path, permissive=True, mode="r") as handle:
            if handle.data.ndim != 3:
                label = "mask" if index == 2 else f"half-map {index + 1}"
                raise VisualError(
                    f"Symmetry scan {label} must be a 3D MRC volume; "
                    f"got {handle.data.shape}."
                )
            if not np.issubdtype(handle.data.dtype, np.number) or np.issubdtype(
                handle.data.dtype,
                np.complexfloating,
            ):
                label = "mask" if index == 2 else f"half-map {index + 1}"
                raise VisualError(
                    f"Symmetry scan {label} must contain real-valued density; "
                    f"got {handle.data.dtype}."
                )
            infos.append(_mrc_info(handle, path))

    voxel_size_A = _validate_symmetry_scan_grid(infos[0], "half_map_1")
    _validate_symmetry_scan_grid(infos[1], "half_map_2")
    _assert_same_grid(infos[0], infos[1], "Symmetry scan half-maps")
    if mask_resolved is not None:
        _validate_symmetry_scan_grid(infos[2], "mask_path")
        _assert_same_grid(infos[0], infos[2], "Symmetry scan mask")
    nyquist_A = 2.0 * voxel_size_A
    if lowpass_A < nyquist_A and not math.isclose(lowpass_A, nyquist_A, rel_tol=1e-7, abs_tol=1e-7):
        raise VisualError(
            f"analysis_lowpass_A={lowpass_A:g} Å is beyond the map's isotropic Nyquist limit "
            f"of {nyquist_A:g} Å."
        )

    float32_bytes = sum(math.prod(info.shape) * np.dtype(np.float32).itemsize for info in infos)
    estimated_bytes = float32_bytes * 4
    if estimated_bytes > _SYMMETRY_SCAN_MAX_ESTIMATED_BYTES:
        raise VisualError(
            "Symmetry scan input exceeds the bounded analysis-memory estimate: "
            f"{estimated_bytes:,} bytes > {_SYMMETRY_SCAN_MAX_ESTIMATED_BYTES:,} bytes."
        )

    volumes: list[np.ndarray] = []
    source_ranges: list[dict[str, float]] = []
    transposes: list[tuple[int, int, int]] = []
    for index, (path, info) in enumerate(zip(paths, infos, strict=True)):
        with mrcfile.mmap(path, permissive=True, mode="r") as handle:
            source = np.array(handle.data, dtype=np.float32, copy=True)
        label = "mask_path" if index == 2 else f"half_map_{index + 1}"
        if index < 2:
            source_ranges.append(_validate_symmetry_scan_volume(source, label))
        elif not np.all(np.isfinite(source)):
            raise VisualError("mask_path contains non-finite voxels.")
        canonical, transpose_axes = _canonical_physical_zyx(source, info)
        volumes.append(canonical)
        transposes.append(transpose_axes)

    if np.array_equal(volumes[0], volumes[1]):
        raise VisualError(
            "half_map_1 and half_map_2 contain identical voxel arrays; independent-half "
            "provenance cannot be evaluated."
        )

    mask = volumes[2] if mask_resolved is not None else None
    mask_metadata: dict[str, Any] | None = None
    if mask is not None:
        mask_minimum = float(np.min(mask))
        mask_maximum = float(np.max(mask))
        if mask_minimum < 0.0 or mask_maximum > 1.0:
            raise VisualError("mask_path values must lie within [0, 1].")
        support_voxels = int(np.count_nonzero(mask > np.finfo(np.float32).eps))
        if support_voxels == 0:
            raise VisualError("mask_path must contain non-zero support.")
        mask_metadata = {
            "path": _relative(project, mask_resolved),
            "sha256": _file_sha256(mask_resolved),
            "minimum": mask_minimum,
            "maximum": mask_maximum,
            "support_voxels": support_voxels,
        }

    # Delayed import keeps image rendering and MCP discovery independent of the
    # numerical core while still exposing one typed public tool.
    from .symmetry import SCHEMA_VERSION, SymmetryScanError, scan_symmetry

    scan_kwargs: dict[str, Any] = {
        "mask": mask,
        "voxel_size_A": voxel_size_A,
        "analysis_resolution_A": lowpass_A,
        "common_max_order": common_order,
        "max_order": maximum_order,
        "stage2_window": stage_2_window,
        "max_candidates": candidate_limit,
    }
    if focus_groups is not None:
        scan_kwargs["focus_groups"] = focus_groups
    try:
        scan_result = scan_symmetry(volumes[0], volumes[1], **scan_kwargs)
    except SymmetryScanError as exc:
        raise VisualError(f"Symmetry scan could not be completed: {exc}") from exc
    if not isinstance(scan_result, Mapping):
        raise VisualError("Symmetry scan core returned an invalid non-object result.")
    core_schema_version = scan_result.get("schema_version")
    if core_schema_version != SCHEMA_VERSION:
        raise VisualError(
            "Symmetry scan core returned an inconsistent schema version: "
            f"{core_schema_version!r}; expected {SCHEMA_VERSION!r}."
        )
    raw_candidates = scan_result.get("candidates", [])
    if not isinstance(raw_candidates, Sequence) or isinstance(raw_candidates, (str, bytes)):
        raise VisualError("Symmetry scan core returned an invalid candidates collection.")
    candidates: list[Mapping[str, Any]] = []
    for index, candidate in enumerate(raw_candidates):
        if not isinstance(candidate, Mapping):
            raise VisualError(f"Symmetry scan candidate {index + 1} is not an object.")
        candidates.append(candidate)

    preprocessing = scan_result.get("preprocessing")
    preprocessing_record = (
        preprocessing if isinstance(preprocessing, Mapping) else {}
    )
    center_value = preprocessing_record.get("center_estimation")
    center_record = center_value if isinstance(center_value, Mapping) else {}
    canonical_shape_zyx = volumes[0].shape
    geometric_source_center_zyx = (
        np.asarray(canonical_shape_zyx, dtype=np.float64) - 1.0
    ) / 2.0
    raw_analysis_shape = preprocessing_record.get("analysis_shape_zyx")
    if (
        isinstance(raw_analysis_shape, Sequence)
        and not isinstance(raw_analysis_shape, (str, bytes))
        and len(raw_analysis_shape) == 3
    ):
        try:
            analysis_shape_zyx = np.asarray(raw_analysis_shape, dtype=np.float64)
        except (TypeError, ValueError):
            analysis_shape_zyx = np.asarray(canonical_shape_zyx, dtype=np.float64)
    else:
        analysis_shape_zyx = np.asarray(canonical_shape_zyx, dtype=np.float64)
    geometric_analysis_center_zyx = (analysis_shape_zyx - 1.0) / 2.0
    analysis_stride = _symmetry_nonnegative_int(
        preprocessing_record.get("analysis_stride")
    )
    if not analysis_stride:
        ratios = np.asarray(canonical_shape_zyx, dtype=np.float64) / np.maximum(
            analysis_shape_zyx,
            1.0,
        )
        analysis_stride = max(1, round(float(np.median(ratios))))

    def analysis_center_to_source_xyz(value: Any) -> list[float]:
        if (
            isinstance(value, Sequence)
            and not isinstance(value, (str, bytes))
            and len(value) == 3
        ):
            try:
                analysis_center = np.asarray(value, dtype=np.float64)
            except (TypeError, ValueError):
                analysis_center = geometric_analysis_center_zyx
            if not np.all(np.isfinite(analysis_center)):
                analysis_center = geometric_analysis_center_zyx
        else:
            analysis_center = geometric_analysis_center_zyx
        source_center_zyx = geometric_source_center_zyx + analysis_stride * (
            analysis_center - geometric_analysis_center_zyx
        )
        return source_center_zyx[::-1].tolist()

    common_center_zyx = center_record.get("chosen_center_zyx")
    source_center_index_xyz = analysis_center_to_source_xyz(common_center_zyx)

    def center_physical_records(source_index_xyz: Sequence[float]) -> tuple[list[float], list[float]]:
        origin_values = [
            float(origin + index * sampling)
            for origin, index, sampling in zip(
                infos[0].origin_A,
                source_index_xyz,
                infos[0].voxel_size_A or (),
                strict=True,
            )
        ]
        start_values = [
            float((start + index) * sampling)
            for start, index, sampling in zip(
                infos[0].start_xyz,
                source_index_xyz,
                infos[0].voxel_size_A or (),
                strict=True,
            )
        ]
        return origin_values, start_values

    (
        source_center_A_from_header_origin_xyz,
        source_center_A_from_header_start_xyz,
    ) = center_physical_records(source_center_index_xyz)
    enriched_candidates: list[Mapping[str, Any]] = []
    for candidate in candidates:
        record = dict(candidate)
        candidate_source_center_xyz = analysis_center_to_source_xyz(
            record.get("symmetry_center_zyx", common_center_zyx)
        )
        (
            candidate_center_A_from_origin_xyz,
            candidate_center_A_from_start_xyz,
        ) = center_physical_records(candidate_source_center_xyz)

        def axis_line(
            direction: Sequence[Any],
            source_center_xyz: Sequence[float],
            center_A_from_origin_xyz: Sequence[float],
            center_A_from_start_xyz: Sequence[float],
            axis_record: Mapping[str, Any],
        ) -> dict[str, Any]:
            return {
                "direction_physical_xyz": list(direction),
                "point_source_index_xyz": list(source_center_xyz),
                "point_A_from_mrc_header_origin_xyz": list(center_A_from_origin_xyz),
                "point_A_from_mrc_header_start_xyz": list(center_A_from_start_xyz),
                "center_assumption": center_record.get(
                    "method", "geometric_array_center"
                ),
                "center_stable": center_record.get("stable"),
                "point_unique_for_cyclic_axis": not bool(
                    axis_record.get("axis_line_point_is_unique") is False
                ),
                "directly_executable_relion_transform": False,
            }

        axis = record.get("axis_xyz")
        if (
            isinstance(axis, (Sequence, np.ndarray))
            and not isinstance(axis, (str, bytes))
            and len(axis) == 3
        ):
            record["axis_line"] = axis_line(
                axis,
                candidate_source_center_xyz,
                candidate_center_A_from_origin_xyz,
                candidate_center_A_from_start_xyz,
                record,
            )
        axis_frame = record.get("axis_frame_xyz")
        if (
            isinstance(axis_frame, (Sequence, np.ndarray))
            and not isinstance(axis_frame, (str, bytes))
            and len(axis_frame) == 3
            and all(
                isinstance(direction, (Sequence, np.ndarray))
                and not isinstance(direction, (str, bytes))
                and len(direction) == 3
                for direction in axis_frame
            )
        ):
            record["axis_frame_lines"] = [
                axis_line(
                    direction,
                    candidate_source_center_xyz,
                    candidate_center_A_from_origin_xyz,
                    candidate_center_A_from_start_xyz,
                    record,
                )
                for direction in axis_frame
            ]
        enriched_candidates.append(record)
    candidates = enriched_candidates

    status = str(scan_result.get("status", "untestable"))
    effective_lowpass_A = _as_float(
        preprocessing_record.get("effective_analysis_lowpass_A")
    )
    if effective_lowpass_A is None:
        effective_lowpass_A = lowpass_A
    search = scan_result.get("search")
    search_record = search if isinstance(search, Mapping) else None
    raw_scan_summary = scan_result.get("scan_summary")
    normalized_core_focus = (
        raw_scan_summary.get("focus_groups")
        if isinstance(raw_scan_summary, Mapping)
        else None
    )
    if (
        isinstance(normalized_core_focus, Sequence)
        and not isinstance(normalized_core_focus, (str, bytes))
    ):
        requested_focus_groups = [str(group) for group in normalized_core_focus]
    else:
        def normalize_focus_label(value: Any) -> str:
            label = str(value).strip().upper()
            if len(label) > 1 and label[0] in {"C", "D"} and label[1:].isdigit():
                return f"{label[0]}{int(label[1:])}"
            return label

        requested_focus_groups = list(
            dict.fromkeys(
                normalize_focus_label(group)
                for group in (focus_groups or [])
                if str(group).strip()
            )
        )
    (
        scan_summary,
        result_completeness,
        hypothesis_results,
        agent_interpretation,
    ) = _symmetry_report_contract(
        scan_result,
        candidates,
        requested_analysis_lowpass_A=lowpass_A,
        effective_analysis_lowpass_A=effective_lowpass_A,
        focus_groups=requested_focus_groups,
        candidate_limit=candidate_limit,
    )
    figure = _symmetry_scan_figure(
        volumes[0],
        volumes[1],
        candidates,
        requested_analysis_lowpass_A=lowpass_A,
        effective_analysis_lowpass_A=effective_lowpass_A,
        status=status,
        search=search_record,
        interpretation=agent_interpretation,
        hypothesis_results=hypothesis_results,
    )

    metadata = dict(scan_result)
    metadata.update(
        {
            "schema_version": SCHEMA_VERSION,
            "report_type": "relion_symmetry_scan",
            "project_dir": str(project),
            "agent_interpretation": agent_interpretation,
            "status": status,
            "decision": "defer",
            "human_input_required": False,
            "report_only": True,
            "selected_candidate": None,
            "candidates": list(candidates),
            "scan_summary": scan_summary,
            "result_completeness": result_completeness,
            "hypothesis_results": hypothesis_results,
            "inputs": {
                "half_map_1": _relative(project, path_1),
                "half_map_2": _relative(project, path_2),
                "half_map_1_sha256": half_map_1_sha256,
                "half_map_2_sha256": half_map_2_sha256,
                "mask": mask_metadata,
                "independent_half_maps_declared_by_caller": True,
                "random_half_assignment_provenance_validated": False,
                "different_file_identity_validated": True,
                "different_content_hash_validated": True,
                "different_voxel_arrays_validated": True,
                "source_density_ranges": source_ranges,
            },
            "physical_grid": {
                "shape_section_row_column": list(infos[0].shape),
                # Keep the legacy key's full-input semantics for API compatibility;
                # preprocessing.analysis_shape_zyx records the reduced scan grid.
                "analysis_shape_zyx": list(volumes[0].shape),
                "canonical_input_shape_zyx": list(volumes[0].shape),
                "voxel_size_A_xyz": list(infos[0].voxel_size_A or ()),
                "origin_A_xyz": list(infos[0].origin_A),
                "start_xyz": list(infos[0].start_xyz),
                "start_column_row_section": list(
                    infos[0].start_column_row_section
                ),
                "axis_mapping_mapc_mapr_maps": list(infos[0].axis_mapping),
                "identical_grid_validated": True,
                "isotropic_sampling_validated": True,
            },
            "coordinate_frame": {
                "source_numpy_axes": ["section", "row", "column"],
                "source_numpy_axis_physical_ids": [
                    infos[0].axis_mapping[2],
                    infos[0].axis_mapping[1],
                    infos[0].axis_mapping[0],
                ],
                "analysis_numpy_axis_order": ["physical_Z", "physical_Y", "physical_X"],
                "transpose_source_to_analysis_zyx": list(transposes[0]),
                "reported_axis_vector_order": ["physical_X", "physical_Y", "physical_Z"],
                "symmetry_center": preprocessing_record.get(
                    "symmetry_center", "geometric_center_of_analysis_array"
                ),
                "center_estimation": dict(center_record),
                "source_center_index_xyz": source_center_index_xyz,
                "geometric_source_center_index_xyz": (
                    geometric_source_center_zyx[::-1].tolist()
                ),
                "source_center_A_from_mrc_header_origin_xyz": (
                    source_center_A_from_header_origin_xyz
                ),
                "source_center_A_from_mrc_header_start_xyz": (
                    source_center_A_from_header_start_xyz
                ),
                "mrc_origin_interpretation": (
                    "Both header-origin and header-start interpretations are reported; "
                    "the estimated source-space point is diagnostic and is not an "
                    "executable RELION alignment transform."
                ),
            },
            "analysis_contract": {
                "requested_analysis_lowpass_A": lowpass_A,
                "effective_analysis_lowpass_A": effective_lowpass_A,
                "isotropic_nyquist_A": nyquist_A,
                "operation_order": [
                    "canonicalize_mrc_axes_to_physical_zyx",
                    "common_physical_lowpass_at_full_input_grid",
                    "center_preserving_bounded_analysis_resampling",
                    "symmetry_scoring",
                ],
                "common_lowpass_before_decimation": True,
                "filter_and_resampling_delegated_to_numerical_core": True,
                "estimated_working_bytes": estimated_bytes,
                "estimated_working_bytes_limit": _SYMMETRY_SCAN_MAX_ESTIMATED_BYTES,
                "source_maps_modified": False,
                "project_files_created": False,
            },
            "requested_search": {
                "common_max_order": common_order,
                "max_order": maximum_order,
                "stage2_window": stage_2_window,
                "max_candidates": candidate_limit,
                "focus_groups": requested_focus_groups,
            },
            "core_schema_version": core_schema_version,
            "icosahedral_convention_note": (
                "Icosahedral evidence denotes the abstract I group; this scan does not distinguish "
                "or select RELION I1, I2, I3, or I4 coordinate conventions."
            ),
            "next_confirmation_tool": "relion_compare_symmetry",
            "scientific_note": (
                "This read-only scan reports exploratory scan heuristics. Absence from the capped "
                "candidates array is never a failure verdict, biological symmetry remains unresolved, "
                "and the scan cannot accept or reject it. "
                "The deferred conclusion does not request user input: continue supported C1 processing "
                "or obtain matched diagnostic evidence within the processing task. "
                "Confirm a candidate with matched C1 and symmetry-constrained Refine3D/PostProcess branches, "
                "independent FSC and half-map evidence, existing local-resolution evidence when available, "
                "and inspection for erased, smeared, distorted, weakened, partially occupied, or "
                "conformationally averaged density. Defer a new LocalRes job until the final branch is "
                "selected unless the user explicitly requests it."
            ),
        }
    )
    return _finish(figure, metadata, settings)


def render_symmetry_comparison(
    settings: Settings,
    project_dir: str,
    candidate_maps: dict[str, str],
    *,
    reference_label: str | None = None,
    low_percentile: float = 0.5,
    high_percentile: float = 99.5,
) -> RenderedVisual:
    project = settings.validate_project_path(project_dir)
    if not (2 <= len(candidate_maps) <= 8):
        raise VisualError("Provide between 2 and 8 candidate maps.")
    if any(not label.strip() for label in candidate_maps):
        raise VisualError("Every candidate map must have a non-empty label.")
    labels = list(candidate_maps)
    reference_label = reference_label or labels[0]
    if reference_label not in candidate_maps:
        raise VisualError(f"reference_label {reference_label!r} is not in candidate_maps.")

    volumes: dict[str, np.ndarray] = {}
    paths: dict[str, Path] = {}
    infos: dict[str, _MrcInfo] = {}
    for label, raw_path in candidate_maps.items():
        path = _project_file(settings, project_dir, raw_path, suffixes=(".mrc", ".mrcs"))
        paths[label] = path
        with mrcfile.mmap(path, permissive=True, mode="r") as handle:
            if handle.data.ndim != 3:
                raise VisualError(f"Candidate {label!r} is not a 3D map: {handle.data.shape}.")
            infos[label] = _mrc_info(handle, path)
            volumes[label] = _downsample_volume(handle.data, max_side=192)
    reference_info = infos[reference_label]
    for label in labels:
        _assert_same_grid(reference_info, infos[label], "Symmetry comparison")

    reference = volumes[reference_label]
    reference_z = _zscore(reference)
    figure = Figure(figsize=(14, 3.1 * len(labels) + 0.8), constrained_layout=True)
    axes = figure.subplots(len(labels), 4, squeeze=False)
    records: list[dict[str, Any]] = []

    for row, label in enumerate(labels):
        volume = volumes[label]
        vmin, vmax = _display_limits(volume, low_percentile, high_percentile)
        xy, xz, yz = _orthogonal_slices(volume)
        for column, (name, image) in enumerate(zip(("XY", "XZ", "YZ"), (xy, xz, yz), strict=True)):
            axes[row][column].imshow(image, cmap="gray", origin="lower", vmin=vmin, vmax=vmax)
            axes[row][column].set_title(f"{label}: {name}")
            axes[row][column].axis("off")
        difference = _zscore(volume) - reference_z
        difference_slice = _orthogonal_slices(difference)[0]
        dmin, dmax = _display_limits(difference_slice, 1.0, 99.0, symmetric=True)
        axes[row][3].imshow(difference_slice, cmap="coolwarm", origin="lower", vmin=dmin, vmax=dmax)
        axes[row][3].set_title(f"z-score XY difference vs {reference_label}")
        axes[row][3].axis("off")
        records.append(
            {
                "label": label,
                "map": _relative(project, paths[label]),
                "shape_zyx": list(infos[label].shape),
                "voxel_size_A_xyz": list(infos[label].voxel_size_A or ()),
                "origin_A_xyz": list(infos[label].origin_A),
                "axis_mapping": list(infos[label].axis_mapping),
                "comparison_to_reference": _map_similarity(reference, volume),
            }
        )

    figure.suptitle(
        f"Candidate symmetry-branch map comparison — reference {reference_label} — same frame required",
        fontsize=14,
    )
    return _finish(
        figure,
        {
            "report_type": "relion_symmetry_comparison",
            "project_dir": str(project),
            "reference_label": reference_label,
            "candidates": records,
            "comparison_scope": "global_central_slice_triage_overview",
            "display_contract": {
                "per_map_percentile_scaling": True,
                "matched_absolute_contours": False,
                "local_feature_acceptance_requires_follow_up": True,
            },
            "required_inspection": [
                "density_present_in_C1_disappears",
                "side_chains_or_domains_are_smeared",
                "helices_are_split_or_distorted",
                "ligand_or_cofactor_density_is_weakened",
                "occupancy_appears_fractional",
                "distinct_conformations_are_averaged_together",
            ],
            "scientific_note": (
                "This tool compares already generated, aligned candidate maps; it does not infer or impose symmetry. "
                "Its autoscaled central slices are a triage overview, not a sufficient local-feature acceptance test. "
                "High-confidence symmetry requires matched C1 and symmetry-constrained refinements, common filtering "
                "and contour logic, independent FSC and half-map evidence, existing local-resolution evidence when "
                "available, and no reproducible symmetry damage. Defer a new LocalRes job until the final branch is "
                "selected unless the user explicitly requests it."
            ),
        },
        settings,
    )


def _crop_volume(
    volume: np.ndarray,
    center_zyx: Sequence[int] | None,
    box_size_pix: int | None,
) -> tuple[np.ndarray, dict[str, Any]]:
    if center_zyx is None and box_size_pix is None:
        return volume, {"center_zyx": None, "box_size_pix": None}
    if center_zyx is None or box_size_pix is None:
        raise VisualError("center_zyx and box_size_pix must be provided together.")
    if len(center_zyx) != 3 or box_size_pix < 8:
        raise VisualError("center_zyx must have three integers and box_size_pix must be at least 8.")
    center = [int(item) for item in center_zyx]
    half = box_size_pix // 2
    slices = []
    for axis, (coordinate, size) in enumerate(zip(center, volume.shape, strict=True)):
        start = max(0, coordinate - half)
        stop = min(size, start + box_size_pix)
        start = max(0, stop - box_size_pix)
        if start >= stop:
            raise VisualError(f"Crop center is outside axis {axis}.")
        slices.append(slice(start, stop))
    cropped = np.asarray(volume[tuple(slices)], dtype=np.float32)
    return cropped, {
        "center_zyx": center,
        "box_size_pix": box_size_pix,
        "actual_bounds_zyx": [[item.start, item.stop] for item in slices],
    }


def render_hand_comparison(
    settings: Settings,
    project_dir: str,
    map_path: str,
    *,
    opposite_hand_map: str | None = None,
    mirror_axis: Literal["x", "y", "z"] = "x",
    center_zyx: Sequence[int] | None = None,
    box_size_pix: int | None = None,
    threshold_sigma: float = 2.0,
    low_percentile: float = 0.5,
    high_percentile: float = 99.5,
) -> RenderedVisual:
    project = settings.validate_project_path(project_dir)
    original_path = _project_file(settings, project_dir, map_path, suffixes=(".mrc", ".mrcs"))
    _check_volume_render_size(original_path, f"Hand comparison map {original_path.name}")
    with mrcfile.mmap(original_path, permissive=True, mode="r") as handle:
        if handle.data.ndim != 3:
            raise VisualError("Hand comparison requires a 3D map.")
        original_full = np.asarray(handle.data, dtype=np.float32)
        info = _mrc_info(handle, original_path)

    if opposite_hand_map:
        opposite_path = _project_file(settings, project_dir, opposite_hand_map, suffixes=(".mrc", ".mrcs"))
        _check_volume_render_size(opposite_path, f"Hand comparison map {opposite_path.name}")
        with mrcfile.mmap(opposite_path, permissive=True, mode="r") as handle:
            opposite_info = _mrc_info(handle, opposite_path)
            _assert_same_grid(info, opposite_info, "Hand comparison")
            opposite_full = np.asarray(handle.data, dtype=np.float32)
        generated = False
    else:
        opposite_path = None
        generated = True

    original, crop_metadata = _crop_volume(original_full, center_zyx, box_size_pix)
    if generated:
        axis_index = {"z": 0, "y": 1, "x": 2}[mirror_axis]
        opposite = np.flip(original, axis=axis_index).copy()
    else:
        opposite, _ = _crop_volume(opposite_full, center_zyx, box_size_pix)
    original = _downsample_volume(original, max_side=192)
    opposite = _downsample_volume(opposite, max_side=192)
    combined = np.concatenate([original.reshape(-1), opposite.reshape(-1)])
    vmin, vmax = _display_limits(combined, low_percentile, high_percentile)

    figure = Figure(figsize=(14, 7.5), constrained_layout=True)
    axes = figure.subplots(2, 4, squeeze=False)
    labels = ("original", "opposite hand")
    for row, (label, volume) in enumerate(zip(labels, (original, opposite), strict=True)):
        slices = _orthogonal_slices(volume)
        for column, (name, image) in enumerate(zip(("XY", "XZ", "YZ"), slices, strict=True)):
            axes[row][column].imshow(image, cmap="gray", origin="lower", vmin=vmin, vmax=vmax)
            axes[row][column].set_title(f"{label}: {name}")
            axes[row][column].axis("off")
        metrics = _map_metrics(volume, threshold_sigma)
        points, values = _surface_points(volume, float(metrics["threshold_value"]), max_points=12_000)
        axes[row][3].remove()
        surface = figure.add_subplot(2, 4, row * 4 + 4, projection="3d")
        if points.size:
            surface.scatter(
                points[:, 2],
                points[:, 1],
                points[:, 0],
                c=values,
                cmap="gray",
                s=0.45,
                linewidths=0,
                alpha=0.72,
            )
        surface.view_init(elev=22, azim=-55)
        surface.set_title(f"{label}: threshold surface")
        surface.set_axis_off()

    figure.suptitle(
        "Map handedness comparison — visual evidence alone is not absolute-hand evidence",
        fontsize=14,
    )
    return _finish(
        figure,
        {
            "report_type": "relion_hand_comparison",
            "project_dir": str(project),
            "original_map": _relative(project, original_path),
            "opposite_hand_map": _relative(project, opposite_path) if opposite_path else None,
            "generated_by_axis_reflection": generated,
            "mirror_axis": mirror_axis if generated else None,
            "shape_zyx": list(info.shape),
            "voxel_size_A_xyz": list(info.voxel_size_A) if info.voxel_size_A else None,
            "crop": crop_metadata,
            "scientific_note": (
                "Do not assign absolute hand from overall shape, FSC, likelihood, or low-resolution appearance. "
                "A high-confidence preference requires independent chiral evidence such as resolved helices, robust "
                "model fitting/building and stereochemistry, tilt data, tomography, or another experimental constraint."
            ),
        },
        settings,
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, np.ndarray):
        return _json_safe(value.tolist())
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def metadata_text(rendered: RenderedVisual) -> str:
    """Serialize compact model-visible metadata for a trailing MCP TextContent block."""
    return json.dumps(_json_safe(rendered.metadata), indent=2, sort_keys=True, allow_nan=False)
