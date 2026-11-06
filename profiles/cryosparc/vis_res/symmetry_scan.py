#!/usr/bin/env python
"""
symmetry_scan.py — point-group scan of a CryoSPARC C1 refinement, using the RELION backend's scanner.

WHY THIS EXISTS
  Symmetry is a hypothesis to TEST, not a setting to guess. This helper runs the same
  deterministic half-map scan that the RELION MCP exposes as `relion_scan_symmetry`
  (profiles/relion/backend/src/relion_mcp/symmetry.py) on the two independent half-maps
  of a CryoSPARC C1 refine job, so both backends judge symmetry with one method.
  It is the FIRST symmetry tool to use; `phenix.map_symmetry` is a fallback (no half-maps,
  or an inconclusive scan) or a second opinion, never the primary evidence.

  The scan is EXPLORATORY and cannot accept or reject biological symmetry:
    - `candidates` is a capped shortlist; a group missing from it is NOT a rejection.
      Read `scan_summary`, `result_completeness` and `hypothesis_results`.
    - Keep C1 as the baseline. For C4 and higher, a missing, monotonic, or uniformly high
      high-order control makes a raw pass confounded, not order-specific support.
    - Dn also needs its perpendicular C2 operations to pass.
    - The decision comes from MATCHED refinements: the same particles refined in C1 and in
      the candidate group, compared on FSC and density.

USAGE (from the CryoSPARC profile directory)
    ../../libexec/em-claw-cryosparc-python vis_res/symmetry_scan.py \
        --project P5 --job J548 --lowpass-A 8 --out projects/<name>/runs/symmetry
    # optional: --mask <refine mask .mrc>  --focus C2 D2  --max-candidates 5

    --lowpass-A is the analysis resolution: well below the FSC resolution (e.g. 6-10 A for a
    3-4 A map), so only the reproducible fold is compared. It must be no finer than Nyquist.

OUTPUT
    <out>/symmetry_scan_<job>.png   the RELION scan report — READ it
    <out>/symmetry_scan_<job>.json  full metadata (candidates, hypothesis_results, limitations)
    stdout: the decision, candidates and where to look.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

import mrcfile
import numpy as np

_PROFILE = Path(__file__).resolve().parent.parent
_RELION_SRC = _PROFILE.parent / "relion" / "backend" / "src"
sys.path.insert(0, str(_RELION_SRC))
sys.path.insert(0, str(_PROFILE))

from relion_mcp.settings import Settings  # noqa: E402
from relion_mcp.visuals import _SYMMETRY_SCAN_MAX_ESTIMATED_BYTES, render_symmetry_scan  # noqa: E402

# The RELION wrapper estimates 4 float32 working copies per input volume.
_BYTES_PER_VOXEL = 4 * 4


def half_maps_from_job(proj, job_uid: str) -> tuple[Path, Path, float]:
    """Absolute half-map paths and pixel size of a refine job's final volume."""
    job = proj.find_job(job_uid)
    project_dir = Path(str(proj.dir()))
    try:
        volume = job.load_output("volume")
        half_a = project_dir / str(np.array(volume["map_half_A/path"])[0])
        half_b = project_dir / str(np.array(volume["map_half_B/path"])[0])
        psize = float(np.array(volume["map_half_A/psize_A"])[0])
        return half_a, half_b, psize
    except Exception:
        pass
    job_dir = project_dir / job_uid
    found_a = sorted(job_dir.glob("*_volume_map_half_A.mrc"))
    found_b = sorted(job_dir.glob("*_volume_map_half_B.mrc"))
    if not found_a or not found_b:
        raise SystemExit(f"{job_uid}: no half-maps found (is it a refine job with a 'volume' output?)")
    with mrcfile.open(found_a[-1], permissive=True, header_only=True) as handle:
        psize = float(handle.voxel_size.x)
    return found_a[-1], found_b[-1], psize


def _box(path: Path) -> int:
    with mrcfile.open(path, permissive=True, header_only=True) as handle:
        nx, ny, nz = int(handle.header.nx), int(handle.header.ny), int(handle.header.nz)
    if not nx == ny == nz:
        raise SystemExit(f"{path.name}: only cubic maps are supported, got {nx}x{ny}x{nz}")
    return nx


def fourier_crop(volume: np.ndarray, box: int) -> np.ndarray:
    """Band-limit a cubic map to a smaller even box, keeping the physical extent."""
    size = volume.shape[0]
    spectrum = np.fft.fftshift(np.fft.fftn(volume))
    start = size // 2 - box // 2
    cropped = spectrum[start:start + box, start:start + box, start:start + box]
    result = np.fft.ifftn(np.fft.ifftshift(cropped)).real * (box / size) ** 3
    return np.ascontiguousarray(result, dtype=np.float32)


def _write(path: Path, volume: np.ndarray, voxel_A: float) -> None:
    with mrcfile.new(path, overwrite=True) as handle:
        handle.set_data(volume.astype(np.float32))
        handle.voxel_size = voxel_A


def stage_inputs(half_a: Path, half_b: Path, mask: Path | None, voxel_A: float,
                 lowpass_A: float, out_dir: Path) -> tuple[Path, Path, Path, Path | None, dict]:
    """Return (root, half_a, half_b, mask, binning); bin only when RELION's memory bound requires it."""
    size = _box(half_a)
    if _box(half_b) != size or (mask is not None and _box(mask) != size):
        raise SystemExit("half-maps and mask must share one grid")
    count = 3 if mask is not None else 2
    limit_box = int((_SYMMETRY_SCAN_MAX_ESTIMATED_BYTES / (count * _BYTES_PER_VOXEL)) ** (1 / 3))
    limit_box -= limit_box % 2
    if size <= limit_box:
        root = Path(os.path.commonpath([half_a.parent, half_b.parent]))
        if mask is not None and root not in mask.parents:
            # The scan reads every input from one root; copy an outside mask beside the outputs.
            staged = out_dir / "inputs"
            staged.mkdir(parents=True, exist_ok=True)
            for source, name in ((half_a, "half_A.mrc"), (half_b, "half_B.mrc"), (mask, "mask.mrc")):
                shutil.copyfile(source, staged / name)
            return (staged, staged / "half_A.mrc", staged / "half_B.mrc", staged / "mask.mrc",
                    {"binned": False, "box": size, "voxel_A": voxel_A})
        return root, half_a, half_b, mask, {"binned": False, "box": size, "voxel_A": voxel_A}
    binned_voxel = voxel_A * size / limit_box
    if 2 * binned_voxel > lowpass_A:
        raise SystemExit(
            f"box {size} must be binned to {limit_box} ({binned_voxel:.3f} A/px) to fit the scan's "
            f"memory bound; --lowpass-A {lowpass_A:g} is finer than that Nyquist "
            f"({2 * binned_voxel:.3f} A). Use a coarser --lowpass-A."
        )
    staged = out_dir / "inputs"
    staged.mkdir(parents=True, exist_ok=True)
    outputs = []
    for source, name in ((half_a, "half_A.mrc"), (half_b, "half_B.mrc")):
        with mrcfile.open(source, permissive=True) as handle:
            volume = np.asarray(handle.data, dtype=np.float32)
        target = staged / name
        _write(target, fourier_crop(volume, limit_box), binned_voxel)
        outputs.append(target)
    staged_mask = None
    if mask is not None:
        from scipy import ndimage
        with mrcfile.open(mask, permissive=True) as handle:
            volume = np.asarray(handle.data, dtype=np.float32)
        resampled = np.clip(ndimage.zoom(volume, limit_box / size, order=1), 0.0, 1.0)
        staged_mask = staged / "mask.mrc"
        _write(staged_mask, resampled, binned_voxel)
    binning = {"binned": True, "source_box": size, "box": limit_box,
               "source_voxel_A": voxel_A, "voxel_A": binned_voxel,
               "reason": "Fourier-cropped to fit the RELION scan memory bound"}
    return staged, outputs[0], outputs[1], staged_mask, binning


def scan(half_a: Path, half_b: Path, *, voxel_A: float, lowpass_A: float, out_dir: Path,
         label: str, mask: Path | None = None, focus_groups: list[str] | None = None,
         max_candidates: int = 5, common_max_order: int = 8, max_order: int = 32) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    root, a, b, m, binning = stage_inputs(half_a.resolve(), half_b.resolve(),
                                          mask.resolve() if mask else None,
                                          voxel_A, lowpass_A, out_dir.resolve())
    settings = Settings(
        mode="plan",
        project_roots=(root,),
        pipeliner_executable="relion_pipeliner",
        image_handler_executable="relion_image_handler",
        pipeline_name="default",
        add_job_timeout_seconds=90,
        utility_timeout_seconds=900,
    )
    rendered = render_symmetry_scan(
        settings, str(root), str(a), str(b), lowpass_A,
        mask_path=str(m) if m else None,
        common_max_order=common_max_order, max_order=max_order,
        max_candidates=max_candidates, focus_groups=focus_groups,
    )
    png = out_dir / f"symmetry_scan_{label}.png"
    png.write_bytes(rendered.png)
    metadata = dict(rendered.metadata)
    metadata["emclaw_inputs"] = {
        "half_map_A": str(half_a), "half_map_B": str(half_b),
        "mask": str(mask) if mask else None, "analysis_lowpass_A": lowpass_A, **binning,
    }
    (out_dir / f"symmetry_scan_{label}.json").write_text(json.dumps(metadata, indent=2, default=str))
    metadata["emclaw_report_png"] = str(png)
    return metadata


def summary(metadata: dict) -> str:
    scan_result = metadata.get("scan", metadata)
    candidates = scan_result.get("candidates") or []
    lines = [
        f"report: {metadata.get('emclaw_report_png')}  (READ this image)",
        f"decision: {scan_result.get('decision')}",
        "candidates (capped shortlist, absence is not rejection): "
        + (", ".join(str(c.get("group")) for c in candidates) or "none"),
    ]
    for key in ("scan_summary", "result_completeness"):
        if key in scan_result:
            lines.append(f"{key}: {json.dumps(scan_result[key], default=str)[:600]}")
    if metadata["emclaw_inputs"].get("binned"):
        b = metadata["emclaw_inputs"]
        lines.append(f"binned {b['source_box']} -> {b['box']} px ({b['voxel_A']:.3f} A/px) for memory")
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", help="CryoSPARC project UID, e.g. P5")
    ap.add_argument("--job", help="C1 refine job UID whose half-maps to scan, e.g. J548")
    ap.add_argument("--half-a", type=Path, help="explicit half-map A (instead of --project/--job)")
    ap.add_argument("--half-b", type=Path, help="explicit half-map B")
    ap.add_argument("--voxel-A", type=float, help="pixel size for explicit half-maps (default: MRC header)")
    ap.add_argument("--lowpass-A", type=float, required=True, help="analysis resolution in Angstrom")
    ap.add_argument("--mask", type=Path, default=None, help="optional soft mask on the half-map grid")
    ap.add_argument("--focus", nargs="*", default=None, help="groups that must be evaluated, e.g. C2 D2")
    ap.add_argument("--max-candidates", type=int, default=5)
    ap.add_argument("--common-max-order", type=int, default=8)
    ap.add_argument("--max-order", type=int, default=32)
    ap.add_argument("--out", type=Path, required=True, help="output directory, e.g. projects/<name>/runs/symmetry")
    a = ap.parse_args()

    if a.half_a and a.half_b:
        half_a, half_b = a.half_a, a.half_b
        if a.voxel_A:
            voxel = a.voxel_A
        else:
            with mrcfile.open(half_a, permissive=True, header_only=True) as handle:
                voxel = float(handle.voxel_size.x)
        label = half_a.stem
    elif a.project and a.job:
        from cs_connect import get_cs
        half_a, half_b, voxel = half_maps_from_job(get_cs().find_project(a.project), a.job)
        label = f"{a.project}_{a.job}"
    else:
        ap.error("give --project and --job, or --half-a and --half-b")
    result = scan(half_a, half_b, voxel_A=voxel, lowpass_A=a.lowpass_A, out_dir=a.out, label=label,
                  mask=a.mask, focus_groups=a.focus, max_candidates=a.max_candidates,
                  common_max_order=a.common_max_order, max_order=a.max_order)
    print(summary(result))
