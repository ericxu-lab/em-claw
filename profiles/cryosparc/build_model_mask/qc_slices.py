#!/usr/bin/env python
"""OpenGL-free model-in-density QC images for the autofit pipeline.

ChimeraX cannot save images on this cluster (no OpenGL, no Xvfb, on the CryoSPARC master and every compute
node), so `vision_qc.render_dock_qc` -- which drives ChimeraX `save <png>` -- always fails with
`LimitationError: Unable to save images because OpenGL rendering is not available`, and the
pipeline's --vision-qc gate can never be satisfied.

This renders the same judgement straight from the map array, the way vis_res already slices maps:
three orthogonal slabs centred on the model, each a maximum-intensity projection over +/-SLAB_A,
with the model's CA trace overlaid in red and a 50 A scale bar.

Read it as: red running along bright density = good fit; red over dark background, or sitting on
a neighbouring subunit, = bad fit.

`render_dock_qc(model_pdb=, map_mrc=, out_dir=, chimerax_exe=)` is signature-compatible with
vision_qc.render_dock_qc and returns (montage_png, log_path).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional, Tuple

import gemmi
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import mrcfile
import numpy as np
from scipy import ndimage

SLAB_A = 8.0
CORE_LEVEL = 0.99      # mask == 1 : full-weight region
EDGE_LEVEL = 0.02      # outer extent of the cosine soft edge


def _ca_positions(model_pdb: Path) -> np.ndarray:
    st = gemmi.read_structure(str(model_pdb))
    st.remove_hydrogens()
    pts: List[List[float]] = []
    for ch in st[0]:
        for r in ch:
            a = r.find_atom("CA", "*")
            if a is not None:
                pts.append([a.pos.x, a.pos.y, a.pos.z])
    return np.array(pts, dtype=np.float64)


def render_model_in_density(
    *,
    model_pdb: Path,
    map_mrc: Path,
    out_png: Path,
    title: str = "",
    slab_A: float = SLAB_A,
) -> Path:
    with mrcfile.open(str(map_mrc), permissive=True) as m:
        data = np.asarray(m.data, dtype=np.float32)          # (z, y, x)
        apix = np.array([float(m.voxel_size.x), float(m.voxel_size.y), float(m.voxel_size.z)])
        org = np.array([float(m.header.origin.x), float(m.header.origin.y), float(m.header.origin.z)])
        nstart = np.array([int(m.header.nxstart), int(m.header.nystart), int(m.header.nzstart)])
    origin_A = org + nstart * apix

    ca = _ca_positions(Path(model_pdb))
    if ca.size == 0:
        raise RuntimeError(f"no CA atoms in {model_pdb}")
    vox = (ca - origin_A) / apix                              # columns are (x, y, z)
    centre = vox.mean(axis=0)

    # `data` is indexed [z, y, x]; dropping one data axis leaves (row, column), and the matching
    # `vox` columns must be read out accordingly.
    panels = [
        ("XY slab (view down Z)", 0, 0, 1),   # drop z -> img (y, x): horiz = x, vert = y
        ("XZ slab (view down Y)", 1, 0, 2),   # drop y -> img (z, x): horiz = x, vert = z
        ("YZ slab (view down X)", 2, 1, 2),   # drop x -> img (z, y): horiz = y, vert = z
    ]
    fig, axes = plt.subplots(1, 3, figsize=(16.5, 5.8))
    for ax, (label, drop, hx, hy) in zip(axes, panels):
        axis_idx = 2 - drop                                   # vox column perpendicular to panel
        half = slab_A / apix[axis_idx]
        lo = int(max(0, round(centre[axis_idx] - half)))
        hi = int(min(data.shape[drop], round(centre[axis_idx] + half) + 1))
        sl: List[Any] = [slice(None)] * 3
        sl[drop] = slice(lo, hi)
        img = data[tuple(sl)].max(axis=drop)
        vmin, vmax = np.percentile(img, [2, 99.8])
        ax.imshow(img, cmap="gray", origin="lower", vmin=vmin, vmax=vmax)
        keep = np.abs(vox[:, axis_idx] - centre[axis_idx]) <= half
        if keep.any():
            ax.plot(vox[keep, hx], vox[keep, hy], "-", color="red", lw=0.6, alpha=0.55)
            ax.scatter(vox[keep, hx], vox[keep, hy], s=4, color="red", alpha=0.9)
        bar = 50.0 / apix[hx]
        x0, y0 = 0.06 * img.shape[1], 0.06 * img.shape[0]
        ax.plot([x0, x0 + bar], [y0, y0], "-", color="yellow", lw=3)
        ax.text(x0, y0 * 1.6, "50 A", color="yellow", fontsize=9)
        ax.set_title(f"{label}  ({int(keep.sum())} CA in slab)", fontsize=10)
        ax.set_xticks([])
        ax.set_yticks([])
    fig.suptitle(f"{title}   -   model CA trace (red) vs map density, +/-{slab_A:.0f} A slab MIP",
                 fontsize=12)
    fig.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_png), dpi=110)
    plt.close(fig)
    return out_png


def render_mask_qc(
    *,
    map_mrc: Path,
    mask_mrc: Path,
    model_pdb: Optional[Path],
    out_png: Path,
    title: str = "",
    slab_A: float = SLAB_A,
    hard_mask_mrc: Optional[Path] = None,
    density_threshold: Optional[float] = None,
) -> Path:
    """Mask QC: does the soft mask actually cover the target subunit, and how far past it?

    Same three orthogonal slabs as the dock QC, with the map in grey, the soft mask overlaid in
    cyan (its 0.5 contour drawn as a line), and the model CA trace in red if given. Read it as:
    cyan should wrap the red with a modest collar; cyan swallowing neighbouring density means the
    mask is too loose (cross-check the volume multiplier in the mask report: 2-3x the protein
    volume is healthy, >4x is masking the neighbours).
    """
    with mrcfile.open(str(map_mrc), permissive=True) as m:
        data = np.asarray(m.data, dtype=np.float32)
        apix = np.array([float(m.voxel_size.x), float(m.voxel_size.y), float(m.voxel_size.z)])
        org = np.array([float(m.header.origin.x), float(m.header.origin.y), float(m.header.origin.z)])
        nstart = np.array([int(m.header.nxstart), int(m.header.nystart), int(m.header.nzstart)])
    with mrcfile.open(str(mask_mrc), permissive=True) as m:
        msk = np.asarray(m.data, dtype=np.float32)
    hard = None
    if hard_mask_mrc is not None and Path(hard_mask_mrc).exists():
        with mrcfile.open(str(hard_mask_mrc), permissive=True) as m:
            hard = np.asarray(m.data, dtype=np.float32)
    if msk.shape != data.shape:
        raise RuntimeError(f"mask {msk.shape} and map {data.shape} are on different grids")
    origin_A = org + nstart * apix

    vox = None
    if model_pdb is not None and Path(model_pdb).exists():
        ca = _ca_positions(Path(model_pdb))
        if ca.size:
            vox = (ca - origin_A) / apix
    centre = (vox.mean(axis=0) if vox is not None
              else np.array(np.unravel_index(int(np.argmax(msk)), msk.shape))[::-1].astype(float))

    panels = [
        ("XY slab (view down Z)", 0, 0, 1),
        ("XZ slab (view down Y)", 1, 0, 2),
        ("YZ slab (view down X)", 2, 1, 2),
    ]
    # Volumes of the two mask regions: full-weight core (m ~ 1) and the cosine soft shell.
    vox_A3 = float(apix[0] * apix[1] * apix[2])
    core_vol = float((msk >= CORE_LEVEL).sum()) * vox_A3
    soft_vol = float(((msk > EDGE_LEVEL) & (msk < CORE_LEVEL)).sum()) * vox_A3

    from matplotlib.lines import Line2D
    fig, axes = plt.subplots(1, 3, figsize=(16.5, 5.8))
    for ax, (label, drop, hx, hy) in zip(axes, panels):
        axis_idx = 2 - drop
        half = slab_A / apix[axis_idx]
        lo = int(max(0, round(centre[axis_idx] - half)))
        hi = int(min(data.shape[drop], round(centre[axis_idx] + half) + 1))
        sl: List[Any] = [slice(None)] * 3
        sl[drop] = slice(lo, hi)
        img = data[tuple(sl)].max(axis=drop)
        mimg = msk[tuple(sl)].max(axis=drop)
        himg = hard[tuple(sl)].max(axis=drop) if hard is not None else None
        vmin, vmax = np.percentile(img, [2, 99.8])
        ax.imshow(img, cmap="gray", origin="lower", vmin=vmin, vmax=vmax)
        # soft shell (0 < m < 1) shaded cyan, full-weight core (m = 1) shaded magenta
        soft_layer = np.ma.masked_where(~((mimg > EDGE_LEVEL) & (mimg < CORE_LEVEL)), mimg)
        core_layer = np.ma.masked_where(mimg < CORE_LEVEL, mimg)
        ax.imshow(soft_layer, cmap=matplotlib.colors.ListedColormap(["#00e5ff"]),
                  origin="lower", alpha=0.22, vmin=0.0, vmax=1.0, zorder=2)
        ax.imshow(core_layer, cmap=matplotlib.colors.ListedColormap(["#ff2fd0"]),
                  origin="lower", alpha=0.18, vmin=0.0, vmax=1.0, zorder=2)
        ax.contour(mimg, levels=[CORE_LEVEL], colors="#ff2fd0", linewidths=1.2, zorder=5)
        ax.contour(mimg, levels=[EDGE_LEVEL], colors="#00e5ff", linewidths=1.0,
                   linestyles="dashed", zorder=5)
        if himg is not None and float(himg.max()) > 0:
            ax.contour(himg, levels=[0.5], colors="#ff9800", linewidths=1.3,
                       linestyles="dotted", zorder=6)
        if density_threshold is not None:
            # map density at the hard-mask threshold the mask builder estimated (drawn on the
            # same slab MIP as everything else, so it encloses voxels reaching it anywhere in
            # the slab). It should trace the protein envelope: much wider = threshold too low.
            # 1-px smoothing first: over a ~12-slice slab the raw MIP promotes isolated solvent
            # noise voxels above the level and the contour turns to speckle. Smoothing keeps the
            # envelope and drops single-voxel spikes.
            ax.contour(ndimage.gaussian_filter(img, 1.0), levels=[float(density_threshold)],
                       colors="#7CFC00", linewidths=1.0, zorder=4)
        if vox is not None:
            keep = np.abs(vox[:, axis_idx] - centre[axis_idx]) <= half
            if keep.any():
                ax.scatter(vox[keep, hx], vox[keep, hy], s=3, color="red", alpha=0.85)
        bar = 50.0 / apix[hx]
        x0, y0 = 0.06 * img.shape[1], 0.06 * img.shape[0]
        ax.plot([x0, x0 + bar], [y0, y0], "-", color="yellow", lw=3)
        ax.text(x0, y0 * 1.6, "50 A", color="yellow", fontsize=9)
        ax.set_title(label, fontsize=10)
        ax.set_xticks([])
        ax.set_yticks([])
    handles = [
        Line2D([0], [0], color="#ff2fd0", lw=2, label=f"mask = 1 (full weight)  {core_vol:,.0f} A^3"),
        Line2D([0], [0], color="#00e5ff", lw=2, ls="--", label=f"0 < mask < 1 (soft edge)  {soft_vol:,.0f} A^3"),
    ]
    if hard is not None:
        hard_vol = float((hard >= 0.5).sum()) * vox_A3
        handles.append(Line2D([0], [0], color="#ff9800", lw=2, ls=":",
                              label=f"hard atom mask  {hard_vol:,.0f} A^3"))
    if density_threshold is not None:
        handles.append(Line2D([0], [0], color="#7CFC00", lw=2,
                              label=f"map density at hard-mask threshold {float(density_threshold):.3f} (1 px smoothed)"))
    handles.append(Line2D([0], [0], marker="o", color="none", markerfacecolor="red",
                          markersize=5, label="model CA"))
    axes[0].legend(handles=handles, loc="upper left", fontsize=8, framealpha=0.75)
    fig.suptitle(f"{title}   -   full-weight core (magenta) + soft edge (cyan dashed) over map", fontsize=12)
    fig.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_png), dpi=110)
    plt.close(fig)
    return out_png


def render_dock_qc(
    *,
    model_pdb: Path,
    map_mrc: Path,
    out_dir: Path,
    chimerax_exe: Optional[str] = None,   # accepted and ignored: no ChimeraX/OpenGL needed
    **_: Any,
) -> Tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    png = out_dir / "model_in_density_slabs.png"
    log_path = out_dir / "qc_slices.log"
    render_model_in_density(
        model_pdb=Path(model_pdb), map_mrc=Path(map_mrc), out_png=png,
        title=f"{Path(model_pdb).name} in {Path(map_mrc).name}",
    )
    log_path.write_text(
        f"model: {model_pdb}\nmap: {map_mrc}\nimage: {png}\n"
        f"renderer: qc_slices (slab MIP; ChimeraX rendering unavailable on this cluster)\n")
    return png, log_path


# --------------------------------------------------------------------------------------------
# Standalone CLI: render a QC image for ANY map + model / map + mask, outside a pipeline run.
#
#   model in density (dock / refine check):
#     python qc_slices.py model --map J519_sharp.mrc --model docked.pdb --out qc.png
#
#   mask check (core / soft edge / hard mask / density threshold):
#     python qc_slices.py mask --map J519_sharp.mrc --mask soft_mask.mrc \
#            --hard-mask hard_atom_mask.mrc --model chainA.pdb --threshold 0.461 --out mask_qc.png
#
#   (--threshold is `threshold_estimate.estimated_density_threshold` from the mask report JSON;
#    --report can be given instead and it is read from there.)
# --------------------------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Slab-MIP QC images for cryo-EM model/mask checking "
                                             "(no OpenGL required).")
    sub = ap.add_subparsers(dest="mode", required=True)

    m = sub.add_parser("model", help="model CA trace over map density")
    m.add_argument("--map", required=True)
    m.add_argument("--model", required=True)
    m.add_argument("--out", required=True)
    m.add_argument("--title", default="")
    m.add_argument("--slab-A", type=float, default=SLAB_A)

    k = sub.add_parser("mask", help="mask core / soft edge / hard mask / density threshold over map")
    k.add_argument("--map", required=True)
    k.add_argument("--mask", required=True)
    k.add_argument("--model", default=None, help="model to overlay (optional)")
    k.add_argument("--hard-mask", default=None, help="hard atom mask MRC (optional)")
    k.add_argument("--threshold", type=float, default=None, help="hard-mask density threshold")
    k.add_argument("--report", default=None,
                   help="mask report JSON; --threshold is read from it if not given explicitly")
    k.add_argument("--out", required=True)
    k.add_argument("--title", default="")
    k.add_argument("--slab-A", type=float, default=SLAB_A)

    a = ap.parse_args(argv)
    if a.mode == "model":
        p = render_model_in_density(model_pdb=Path(a.model), map_mrc=Path(a.map),
                                    out_png=Path(a.out), title=a.title, slab_A=a.slab_A)
    else:
        thr = a.threshold
        if thr is None and a.report:
            thr = json.loads(Path(a.report).read_text()).get(
                "threshold_estimate", {}).get("estimated_density_threshold")
        p = render_mask_qc(map_mrc=Path(a.map), mask_mrc=Path(a.mask),
                           model_pdb=Path(a.model) if a.model else None,
                           hard_mask_mrc=Path(a.hard_mask) if a.hard_mask else None,
                           density_threshold=thr, out_png=Path(a.out), title=a.title,
                           slab_A=a.slab_A)
    print(f"wrote {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
