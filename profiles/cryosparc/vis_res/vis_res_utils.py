"""
vis_res_utils.py — vision-based resolution sanity check for a 3D map (.mrc).

WHY
  Gold-standard FSC can report a FALSE high resolution — most often when a
  reconstruction failed to converge to an interpretable map (noise/junk aligned
  into a "consistent" but meaningless density, masking artefacts, over-tight
  masks). The number then makes no physical sense. This tool cross-checks the
  FSC number against what the density ACTUALLY shows, the same way a human would
  open the map and look for secondary structure.

METHOD
  0. CROP to the signal box first (every panel: slices, projections, surface). A refine
     box is mostly solvent, and an uncropped render puts the particle at ~6 px/A where a
     side-chain-decorated helix and a featureless 5 A rod look identical. Cropping is not
     binning — the voxel grid is untouched, you just stop drawing empty space.
  1. Render the evidence panel: 6 central SLICES (2 per axis, through the signal)
     + 3 PROJECTIONS (one per axis). Everything at the native voxel grid — NO
     BINNING, NO slab averaging, integer NEAREST upscale only; either would
     low-pass the map and make you UNDER-estimate the resolution.
       SLICES      = the RESOLUTION read (secondary structure vs the 10 A bar).
       PROJECTIONS = the ENVELOPE / ANISOTROPY read (is it a bounded particle, is
                     it smeared along a direction). A projection is a full-axis
                     mean, so it carries NO resolution information — never quote
                     an Angstrom number off it.
  2. VISION CALL (done by the agent, like the select2d guard): read the panel,
     focus on the part that carries signal, judge whether SECONDARY STRUCTURE is
     visible, then estimate a resolution in Angstrom. It is normal for some slices
     to carry little/no signal — density does not always fill the box.
  3. If NO secondary structure is visible anywhere, the map is not interpretable
     as protein / nucleic acid -> this is a FAILED reconstruction and the FSC
     resolution is meaningless (status = FAILURE).
  4. THE SURFACE, via surface_panel(): crop -> auto_display_levels() scans two orders of
     enclosed volume and returns the TWO levels bracketing the readable window (the
     connectivity knee) -> UNSHARP and SHARP rendered side by side at the SAME enclosed
     volume, every view carrying a SCALE BAR. Read the unsharp column for the FOLD
     (continuous tubes = helices resolved) and the sharp column for the DETAIL (knobs on
     those tubes = side chains). MEASURE against the bar: ~5 A tubes + 2-3 A knobs +
     ~10 A spacing -> 3-3.5 A; ~8 A smooth tubes, no knobs -> ~5 A.
  5. If the vision estimate is far from the FSC (corrected) value (|delta| over the
     two-tier tolerance: 1 A when FSC < 4 A, else 2 A), assess() does NOT decide —
     it returns NEEDS_SURFACE_CHECK. Run render_map_surface() (iso-surface level
     sweep on the unsharpened map) and re-call with surface_checked=True and an
     explicit fsc_trustworthy. Slices systematically under-read a 3-4 A map, so
     this second look is what separates a genuine map from a false high resolution
     (-> SUSPICIOUS_RESOLUTION). Where no surface can be rendered, re-call with
     surface_unavailable=True: the heuristic decides and the verdict is flagged.

USAGE (agent / subC)
    from vis_res_utils import render_map_slices, build_prompt, assess, RUBRIC

    r = render_map_slices('/path/Pxx/Jyy/Jyy_volume_map_sharp.mrc')
    # -> read r['contact_sheet'] (6 slices + 3 projections) with the Read tool,
    #    apply RUBRIC / build_prompt(), then call assess(...) with your judgement:

    v = assess(secondary_structure=True, vision_estimate_A=6.5, fsc_res_A=3.1,
               notes='only blobby tubes, no side chains -> ~6-7 A')
    if v['next_action'] == 'render_map_surface':          # gap over tolerance
        s = render_map_surface(mrc)                       # read s['surface_sheet']
        v = assess(secondary_structure=True, vision_estimate_A=6.5, fsc_res_A=3.1,
                   surface_checked=True, fsc_trustworthy=False,
                   notes='surface: fragmented, no rods -> FSC is fake-high')
    print(v['status'], v['flags'])

The vision step is performed by the calling Claude agent (it has the Read tool) —
this module only prepares the evidence and encodes the decision rule. That mirrors
select2d_utils, where "Claude reads the rendered panels".

THE SECONDARY AXIS — FSC CURVE HEALTH
  fsc_curve_health(proj, 'Jxx') / compare_fsc_curve_health(proj, [uids])
      The vision check above is PRIMARY: it asks whether the density looks like its
      number. Curve health is the second look that must accompany every reported
      resolution: the 0.143 crossing says what the map CLAIMS, the SHAPE of the
      curve says whether the claim is honest and WHICH artefact is behind it when
      it is not (monotonicity/rebound, all four columns + mask_effect_A, the mask
      divergence point, slope at the crossing, tail plateau, the 0.5-vs-0.143 gap).
      It is EXPLANATORY — it never overrides vis_res and never ranks arms alone.
      ⚠ Every FSC-derived quantity is confounded by ITERATION DEPTH; compare arms
      only through compare_fsc_curve_health(), and quote the iteration always.

SCOPE — FOR A LOCAL REFINE, JUDGE AND REPORT THE MASK REGION (added 2026-09-07)
  In a `new_local_refine` ONLY the masked region is refined; everything outside it is
  unrefined carry-over from the parent map. So a SINGLE WHOLE-MAP resolution number for a
  locally refined map is a CATEGORY ERROR, and the mask-inflated FSC is a statement about
  the MASKED REGION — which makes a vision estimate of that same region the only
  like-for-like comparison. Pass the refinement's mask:

      r = render_map_slices(map_mrc, mask='<local_refine_mask>.mrc',
                            mask_label='receptor+BRIL')      # slices -> the REGION
      s = surface_panel(map_mrc, out_dir, mask='<local_refine_mask>.mrc',
                        mask_label='receptor+BRIL')          # SURFACE -> the REGION
      v = assess(..., region=r['region'])                    # verdict -> REGION-SCOPED

  The SURFACE half was whole-box until 2026-09-24, which mattered because the surface is the
  deciding evidence for a 3-4 A map: an unmasked read judged a focused refine partly on density
  the job never refined, and auto_display_levels chose its levels on that same carry-over. With
  a mask, surface_panel crops to the region and flags REGION_SCOPED, exactly like the slices.
  The --ladder/--pair search paths of surface_judge.py remain whole-box by design.

  The slices are then cropped to the region (the mask takes precedence over the inferred
  resolution-bearing core), the projections still keep the full envelope, the >=10 px/A
  floor still holds, and the verdict carries the region's identity, volume and the fraction
  of the crop it covers, plus flag REGION_SCOPED. If the region does not fit the pixel
  budget only its most ordered part is shown and that is flagged REGION_PARTIALLY_SHOWN.
  Asking for a whole-map number on a local refine (assess(..., locally_refined=True))
  raises LOCAL_REFINE_REGION_ONLY. With NO mask the behaviour is unchanged.

THE PRE-FILTER GUARD — IS THE MAP-DOMAIN EVIDENCE EVEN USABLE? (added 2026-09-07)
  MEASURED: a CryoSPARC output volume is hard-truncated in Fourier space at the job's OWN
  reported resolution (J238 edge 4.25 vs claim 4.37 · J548 3.67 vs 3.65 · J216 10.02 vs
  13.17 · J2105 2.62 vs 2.70), while the HALF-MAPS are unfiltered to Nyquist. When the edge
  COINCIDES with the claim, any map-domain resolution evidence from that file is CIRCULAR —
  the software already put the answer there. assess(..., prefilter_edge_A=...) or
  assess(..., mrc_path=...) raises MAP_PREFILTERED_AT_CLAIM to say so; use the half-maps for
  a resolution measurement. It is a cheap guard (one FFT), never a ranking instrument, and it
  never changes a status.

TWO OTHER ENTRY POINTS
  compare_volumes(paths, out_dir, labels=...)
      QUALIFY SEVERAL VOLUMES AGAINST EACH OTHER (ab-initio classes, competing
      refinements). render_map_slices normalises each panel independently, which makes
      the map with more particles look better purely because of its own stretch —
      useless for ranking. compare_volumes puts every map on ONE shared window in
      sigma-above-solvent and renders the SAME two view types as render_map_slices —
      native SLICES (slab_vox=1, no averaging) + a PROJECTION row — plus COUNT-ROBUST
      metrics (fixed enclosed volume -> spatial coherence) so ranking is not pure
      eyeball. slab_vox=3 is opt-in for very noisy volumes and reads SOFT. No OpenGL.

  render_map_surface(mrc, ...)
      Iso-surface level sweep — the reliable check for a borderline 3-4 A map.
      Needs ChimeraX WITH working OpenGL. Nothing site-specific is hard-coded: the
      executable comes from `model_mask_chimerax` in base_datainfo.yaml, and if that
      ChimeraX cannot render where the agent runs, `vis_res_render_host` /
      `vis_res_render_ld_path` in the same yaml redirect the render to a host that
      can. Set vis_res_utils.DATAINFO_PATH to the active project's yaml first, and
      give out_dir on SHARED storage when a render host is used.

CAUTION — sigma is meaningless on a SOLVENT-FLATTENED map. A refinement's output
`volume_map.mrc` is often masked (a large fraction of voxels exactly 0), which
collapses the solvent std, so "mean + N*std" levels all enclose the same mask volume.
On such maps drive the level by TARGET ENCLOSED VOLUME instead (a packed protein is
~0.81 Da/A^3, so ~62,000 A^3 per 50 kDa). The half-maps are normally NOT masked, which
is why an unmasked half-map FSC is the honest, mask-proof resolution floor.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import numpy as np
import mrcfile
from PIL import Image, ImageDraw, ImageFont

_FONT_PATH = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'


def _font(size: int) -> ImageFont.ImageFont:
    """A legible bold font that scales with the panel; falls back to PIL default."""
    try:
        return ImageFont.truetype(_FONT_PATH, size)
    except Exception:
        return ImageFont.load_default()


# RENDER-SCALE TARGET (was a hard floor until 2026-09-25; human directive: a render that
# reaches the RECONSTRUCTION'S OWN sampling is enough, since beyond 1/voxel_A px/A the render
# only magnifies and adds no information). Kept as a READABILITY target, not a gate: on too
# few pixels the eye confuses a side-chain knob with a bump — J1078, a real 3.0 A map, was
# read as ~5 A three times. render_map_slices / surface_panel aim for it and FLAG when the
# render lands below it (`PX_PER_A_BELOW_TARGET`); assess() no longer refuses a verdict for it.
PX_PER_A_FLOOR = 10.0


# Feature -> resolution rubric the vision call uses to turn "what I see" into Angstrom.
RUBRIC = """\
SECONDARY-STRUCTURE / RESOLUTION RUBRIC (judge feature SIZE against the 10 A scale bar):
  • > ~10 A   : only a smooth blob / envelope; no internal features. NOT interpretable.
  • ~8-10 A   : separated rod-like densities (alpha-helix barrels) but no grooves.
  • ~5-8 A    : clear helical rods AND beta-sheet slabs; the SECONDARY-STRUCTURE PATH is
                followable (which rod connects to which slab) -- but NOT the backbone itself.
  • ~4-5 A    : beta-strand separation (~4.8 A) starts to resolve. Backbone becomes
                followable toward the good end of this band.
  • ~3.5-4 A  : bumps for large side chains; helix PITCH (5.4 A groove) starts to show.
  • ~2.5-3.5 A: side-chain density clear and mostly assignable; aromatic rings read as
                FLAT DISCS whose overall SHAPE is recognisable (Phe vs Leu callable).
  • < ~2 A    : HOLES in aromatic rings (the ring's inner cavity is only ~2.4-2.8 A across,
                so a central depression cannot appear until resolution is well past it).
                At ~1.5 A and better, individual atoms.
  ⚠ CORRECTED 2026-09-05 (human directive). The old rubric put "holes in aromatic rings" at
    "< ~3 A" and "helix pitch visible" at "~4-5 A" -- BOTH were roughly 1 A too optimistic.
    The aromatic-hole error is the dangerous one: it invites the inverse inference
    "no holes therefore worse than 3 A", which is worthless because NO map at 3-4 A shows
    aromatic holes even when flawless. ⛔ ABSENCE OF AROMATIC HOLES IS NOT EVIDENCE ABOUT A
    MAP COARSER THAN ~2 A -- do not cite it. (It was cited on AT1R J2105 at ~4 A, where it
    carried no information; the load-bearing evidence there was DETACHED specks in the sharp
    column, which is a genuine noise signature at any resolution.)
  ⚠ Local order beats the nominal number: a rigid, well-ordered aromatic in a 2.3 A map can
    hint at a hole while a mobile one in a 1.8 A map shows none. And OVER-SHARPENING CARVES
    FAKE HOLES into any blob -- a hole only counts if the UNSHARPENED map supports it.
INTERPRETABILITY GATE:
  If NONE of the 6 slices shows any secondary structure (only noise, shells, or a
  featureless blob) the map is NOT interpretable as protein/nucleic acid -> FAILURE,
  and the FSC number is meaningless regardless of how good it looks.
NOTES:
  • Some slices may carry little signal (density does not fill the box) — that is OK;
    judge from the slices that DO show the particle, focusing on the signal region.
  • The slices are at NATIVE sampling (no binning); fine striations are real, not noise
    upsampling artefacts — but a regular speckle covering the whole box is noise.
  • DO NOT use "how many helices separate at a global contour" as a resolution criterion.
    That measures CONNECTIVITY, not resolution, and it is badly wrong for a membrane
    protein in a DETERGENT MICELLE: detergent overlaps protein in density MAGNITUDE (it
    differs in TEXTURE), so one global level that separates helices in one region floods
    another. Helices can be fully resolved and still never separate at any single level.
    Judge resolution from feature SIZE against the scale bar, and cross-check against an
    UNMASKED half-map FSC — which no mask or contour choice can inflate.
  • Tell protein from detergent by local TEXTURE (local std / local mean), not density:
    ordered protein ~0.7, disordered detergent ~0.3 in the same map.
  • A DETERGENT MICELLE IS AN INTERNAL NEGATIVE CONTROL. Detergent is disordered: it cannot
    carry secondary structure at any resolution. So if the micelle wears the SAME rod-like /
    worm-like texture as the protein (in a slice or on a surface), that texture is amplified
    NOISE, not structure -- whatever the FSC says. Look at the belt before you believe the
    bundle."""


# ----------------------------------------------------------------------------- #
# 1-2. Slice + render the map                                                    #
# ----------------------------------------------------------------------------- #

def _voxel_size_A(mrc) -> float:
    """Voxel size in Angstrom (fall back to 1.0 if the header is unset)."""
    try:
        vs = float(mrc.voxel_size.x)
        if vs > 0:
            return vs
    except Exception:
        pass
    return 1.0


def _signal_bbox(vol: np.ndarray, sigma: float = 2.0) -> tuple[slice, slice, slice]:
    """
    Per-axis bounding box of the signal (voxels > mean + sigma*std), so the 6
    slices are placed THROUGH the particle rather than through empty solvent.
    Falls back to the whole box if no voxel passes the threshold.
    """
    thr = vol.mean() + sigma * vol.std()
    mask = vol > thr
    if not mask.any():
        return tuple(slice(0, n) for n in vol.shape)  # type: ignore
    out = []
    for ax in range(3):
        proj = mask.any(axis=tuple(i for i in range(3) if i != ax))
        idx = np.where(proj)[0]
        out.append(slice(int(idx[0]), int(idx[-1]) + 1))
    return tuple(out)  # type: ignore


def _slice_positions(bb: slice, n_full: int, n_per_axis: int) -> list[int]:
    """n_per_axis evenly spaced positions inside the signal bbox along one axis."""
    lo, hi = bb.start, bb.stop - 1
    if hi <= lo:
        lo, hi = 0, n_full - 1
    # interior fractions, e.g. n=2 -> [1/3, 2/3]
    fracs = [(k + 1) / (n_per_axis + 1) for k in range(n_per_axis)]
    return [int(round(lo + f * (hi - lo))) for f in fracs]


def _take_slice(vol: np.ndarray, axis: int, idx: int) -> np.ndarray:
    """2D slice; oriented so the image is (rows=down, cols=right) consistently."""
    if axis == 0:      # Z slice -> (Y, X)
        return vol[idx, :, :]
    if axis == 1:      # Y slice -> (Z, X)
        return vol[:, idx, :]
    return vol[:, :, idx]  # X slice -> (Z, Y)


def _to_uint8(sl: np.ndarray, p_lo: float = 0.5, p_hi: float = 99.8) -> np.ndarray:
    """Percentile-clip contrast stretch to 8-bit (per-slice)."""
    finite = sl[np.isfinite(sl)]
    if finite.size == 0:
        return np.zeros(sl.shape, np.uint8)
    lo, hi = np.percentile(finite, [p_lo, p_hi])
    if hi <= lo:
        hi = lo + 1.0
    out = np.clip((sl - lo) / (hi - lo), 0, 1)
    return (out * 255).astype(np.uint8)


def _panel(sl: np.ndarray, voxel_A: float, scale: int, scalebar_A: float,
           label: str, cell: int) -> Image.Image:
    """Render one slice: NEAREST upscale (no smoothing), scale bar, label, padded to `cell`."""
    img = Image.fromarray(_to_uint8(sl), mode='L').convert('RGB')
    img = img.resize((sl.shape[1] * scale, sl.shape[0] * scale), Image.NEAREST)

    # pad onto a fixed-size dark canvas so the contact sheet is a clean grid
    canvas = Image.new('RGB', (cell, cell), (15, 15, 15))
    ox = (cell - img.width) // 2
    oy = (cell - img.height) // 2
    canvas.paste(img, (max(ox, 0), max(oy, 0)))
    dr = ImageDraw.Draw(canvas)
    fs = max(12, cell // 28)            # font scales with panel
    font = _font(fs)

    # scale bar (physical length): pixels = scalebar_A / voxel_A * scale
    bar_px = int(round(scalebar_A / voxel_A * scale))
    bh = max(4, fs // 3)
    bx0, by = 12, cell - fs - 14
    if 0 < bar_px < cell - 24:
        dr.rectangle([bx0, by, bx0 + bar_px, by + bh], fill=(255, 255, 0))
        dr.text((bx0, by - fs - 2), f'{scalebar_A:g} A', fill=(255, 255, 0), font=font)
    dr.text((8, 6), label, fill=(0, 255, 120), font=font)
    return canvas


def render_map_slices(mrc_path: str | Path,
                      out_dir: Optional[str | Path] = None,
                      n_per_axis: int = 2,
                      scale: Optional[int] = None,
                      scalebar_A: float = 10.0,
                      signal_sigma: float = 2.0,
                      projections: bool = True,
                      prefer_unsharpened: bool = True,
                      crop: bool = True,
                      crop_pad_A: float = 8.0,
                      px_per_A_target: float = PX_PER_A_FLOOR,
                      max_sheet_px: int = 1400,
                      mask=None,
                      mask_label: Optional[str] = None,
                      mask_threshold: float = 0.5) -> dict:
    """
    Slice a map 2x per axis (6 central slices through the signal) and, by default,
    add ONE PROJECTION per axis. Everything is rendered at NATIVE sampling: no
    binning, no slab averaging, integer NEAREST upscale only.

    The two rows answer DIFFERENT questions and must not be mixed up:
      SLICES (6)      -> the RESOLUTION read. A single voxel plane, untouched: any
                         averaging (a slab) or binning is a low-pass filter and makes
                         you UNDER-read the resolution. Judge secondary structure here.
      PROJECTIONS (3) -> the ENVELOPE / ANISOTROPY read. A full-axis mean, so it is
                         deliberately averaged and carries NO resolution information;
                         it shows whether the density is a bounded, compact particle
                         and whether it is smeared along a direction (preferred
                         orientation). Never estimate Angstrom from a projection.

    prefer_unsharpened=True (the DEFAULT) reads the `..._volume_map.mrc` sibling. In a flat
    slice, sharpening buys nothing and costs a lot: it amplifies high frequencies INCLUDING
    noise, and CryoSPARC derives the B-factor from the REPORTED resolution — so a map whose
    FSC is already over-claimed is sharpened into a speckle that reads as texture. The
    unsharpened density is what you judge feature SIZE on. (The SURFACE is the opposite case:
    there the sharpened map is the one to render — see render_map_surface.) Pass
    prefer_unsharpened=False to force the sharpened map; sharpening_report() quantifies how
    much the choice matters for a given job.

    mask / mask_label / mask_threshold (ADDED 2026-09-07) — FOR A LOCAL REFINE, JUDGE THE
    REGION THAT WAS ACTUALLY REFINED. Pass the refinement's mask (path to .mrc or an array on
    the SAME grid) and the SLICES are cropped to that region instead of to the inferred
    resolution-bearing core; the PROJECTIONS still keep the full signal envelope, and the
    >=10 px/A floor still holds. The result then carries `region` (name, volume, fraction of
    the crop covered by mask > threshold) and `scope='region'`, and the estimate must be
    reported as belonging to THAT REGION, not to the whole map — outside the mask a local
    refine leaves unrefined carry-over, so a single whole-map number is a category error.
    With mask=None the behaviour is EXACTLY the 2026-09-05 path (resolution-bearing bbox).

    Returns dict:
      {mrc, voxel_size_A, box, n_slices, slices:[{axis,index,png}...],
       projections:[{axis,png}...], contact_sheet, scalebar_A}
      (+ region, scope when a mask is supplied)
    The caller (agent) then READS contact_sheet and applies RUBRIC / build_prompt().
    """
    mrc_path = Path(mrc_path)
    if prefer_unsharpened:
        swapped = unsharpened_sibling(mrc_path)
        if swapped != mrc_path:
            print(f'[render_map_slices] reading the UNSHARPENED map: {swapped.name}')
            mrc_path = swapped
    out_dir = Path(out_dir) if out_dir else mrc_path.parent / f'{mrc_path.stem}_vis_res'
    out_dir.mkdir(parents=True, exist_ok=True)

    with mrcfile.open(str(mrc_path), permissive=True) as m:
        vol = m.data.astype(np.float32).copy()
        voxel_A = _voxel_size_A(m)
    if vol.ndim != 3:
        raise ValueError(f'expected a 3D map, got shape {vol.shape}')
    # ------------------------------------------------------------------ #
    # TWO DIFFERENT CROPS, because the two rows answer different questions.
    #   SLICES      -> the RESOLUTION read  -> crop to the RESOLUTION-BEARING core,
    #                  as tight as it takes to reach the px/A floor.
    #   PROJECTIONS -> the ENVELOPE read    -> the FULL signal box. Cropping these to the
    #                  core would destroy the very evidence they exist for (is the particle
    #                  bounded? is it smeared along a direction?).
    # Before the 2026-09-05 repair BOTH used crop_to_signal(), so the slices were rendered
    # over the whole micelle at 3.6 px/A. See PX_PER_A_FLOOR above.
    # ------------------------------------------------------------------ #
    full_shape = vol.shape
    vol_env = vol                                     # envelope (projections)
    if crop:
        vol_env, _ = crop_to_signal(vol, voxel_A, pad_A=crop_pad_A)

    flags: list[str] = []
    region = None
    mask_arr = None
    if mask is not None:
        mask_arr, mask_src = load_mask(mask, shape=vol.shape)
    if crop:
        # The pixel budget SETS the crop, so the floor is met by construction: at the
        # integer upscale that reaches px_per_A_target, a panel may be at most
        # (max_sheet_px-4)/scale voxels across.
        want = _scale_for_px_per_A(voxel_A, px_per_A_target)
        budget = max(8, (max_sheet_px - 4) // want)
        if mask_arr is not None:
            # ⭐ THE MASK TAKES PRECEDENCE: the refined region is a known input, not a guess.
            chosen, how = mask_region_bbox(vol, mask_arr, voxel_A, threshold=mask_threshold,
                                           pad_A=crop_pad_A, max_box_vox=budget)
            vol_res = vol[chosen].copy()
            if mask_label:
                rname = mask_label
            elif mask_src != '<ndarray>':
                rname = Path(mask_src).stem
            else:
                rname = 'mask region'
            region = region_report(mask_arr, chosen, voxel_A, threshold=mask_threshold,
                                   name=rname, source=mask_src)
            region.update(how)
            flags.append('REGION_SCOPED')
            if region['region_frac_in_crop'] < 0.90:
                flags.append('REGION_PARTIALLY_SHOWN')
                print(f'[render_map_slices] *** REGION_PARTIALLY_SHOWN: the region does not '
                      f'fit the {px_per_A_target:g} px/A pixel budget, so the slices show only '
                      f'{region["region_frac_in_crop"]*100:.1f}% of it (the most ordered part). '
                      f'The estimate describes THAT PART, not the whole region — say so, or '
                      f'render the region in pieces.')
            print(f'[render_map_slices] ⭐ REGION-SCOPED read: SLICES cropped to the MASK '
                  f'REGION "{region["name"]}" ({how["how"]}): {full_shape} -> {vol_res.shape} '
                  f'({max(vol_res.shape)*voxel_A:.0f} A box, native grid, no binning); the '
                  f'crop is {region["crop_frac_covered_by_mask"]*100:.1f}% covered by '
                  f'mask>{mask_threshold:g}, region volume '
                  f'{region["region_volume_A3"]:.0f} A^3.  PROJECTIONS keep the '
                  f'{max(vol_env.shape)*voxel_A:.0f} A signal envelope.  ⚠ The resolution '
                  f'estimate read off these slices applies to THIS REGION ONLY.')
        else:
            chosen = resolution_bearing_bbox(vol, voxel_A, pad_A=crop_pad_A,
                                             max_box_vox=budget)
            vol_res = vol[chosen].copy()
            print(f'[render_map_slices] SLICES cropped to the RESOLUTION-BEARING core: '
                  f'{full_shape} -> {vol_res.shape} ({max(vol_res.shape)*voxel_A:.0f} A box, '
                  f'native grid, no binning)   PROJECTIONS keep the {max(vol_env.shape)*voxel_A:.0f} A '
                  f'signal envelope')
    else:
        vol_res = vol

    box_max = max(vol_res.shape)
    if scale is None:
        # px/A FLOOR, not a pixel count: integer NEAREST upscale that reaches the target.
        scale = _scale_for_px_per_A(voxel_A, px_per_A_target)
        if box_max * scale + 4 > max_sheet_px:
            # cannot have both -> keep the FLOOR and let the sheet be split (below),
            # never silently drop the scale, which is what produced the under-read.
            pass
    ppa = px_per_A_report(box_max, voxel_A, scale, floor=px_per_A_target)
    flags += ppa['flags']
    if not ppa['meets_floor']:
        print(f'[render_map_slices] *** PX_PER_A_FLOOR_NOT_MET: {ppa["px_per_A"]} px/A '
              f'< {px_per_A_target} px/A. A knob and a bump are the same pixels here — '
              f'the read is NOT valid; pass this px_per_A into assess(), which will refuse '
              f'a verdict rather than under-read the map.')
    else:
        print(f'[render_map_slices] slices at {ppa["px_per_A"]} px/A '
              f'(floor {px_per_A_target}) — scale x{scale} NEAREST, voxel {voxel_A:.3f} A')

    bbox = _signal_bbox(vol_res, sigma=signal_sigma)
    cell = box_max * scale + 4  # square cell big enough for any axis' slice

    axis_names = ['Z', 'Y', 'X']
    slices_meta = []
    panels = []
    for axis in range(3):
        positions = _slice_positions(bbox[axis], vol_res.shape[axis], n_per_axis)
        for k, idx in enumerate(positions):
            sl = _take_slice(vol_res, axis, idx)
            label = (f'{axis_names[axis]}[{idx}] SLICE  {ppa["px_per_A"]:.1f}px/A'
                     f'{"" if ppa["meets_floor"] else "  ** BELOW FLOOR **"}'
                     f'{"" if region is None else "  REGION: " + region["name"]}')
            panel = _panel(sl, voxel_A, scale, scalebar_A, label, cell)
            png = out_dir / f'slice_{axis_names[axis]}_{k+1}_idx{idx}.png'
            panel.save(png)
            panels.append(panel)
            slices_meta.append({'axis': axis_names[axis], 'index': idx, 'png': str(png)})

    # one projection per axis: full-axis MEAN over the ENVELOPE box (no resolution content,
    # so its scale is chosen to fit, not to meet the floor)
    proj_meta, proj_panels = [], []
    if projections:
        pscale = max(1, min(scale, (max_sheet_px - 4) // max(max(vol_env.shape), 1)))
        pcell = max(vol_env.shape) * pscale + 4
        for axis in range(3):
            pr = vol_env.mean(axis=axis)   # same (rows, cols) convention as _take_slice
            label = f'{axis_names[axis]} PROJ  envelope, NOT resolution'
            panel = _panel(pr, voxel_A, pscale, scalebar_A, label, pcell)
            png = out_dir / f'proj_{axis_names[axis]}.png'
            panel.save(png)
            proj_panels.append(panel.resize((cell, cell), Image.NEAREST))
            proj_meta.append({'axis': axis_names[axis], 'png': str(png)})

    # contact sheet: one ROW per axis = [slice_1 ... slice_n | projection of that axis]
    ncols = n_per_axis + (1 if projections else 0)
    sheet = Image.new('RGB', (ncols * cell, 3 * cell), (15, 15, 15))
    for i, panel in enumerate(panels):
        r, c = i // n_per_axis, i % n_per_axis
        sheet.paste(panel, (c * cell, r * cell))
    for r, panel in enumerate(proj_panels):
        sheet.paste(panel, (n_per_axis * cell, r * cell))
    contact = out_dir / 'contact_sheet.png'
    sheet.save(contact)

    # ---- READ SHEETS: the px/A that is COMPUTED must be the px/A that is JUDGED. ------
    # A 3x3 contact sheet of 700 px cells is ~2100 px and any image-reading client
    # downsamples it -- which silently throws the floor away again, one step further
    # downstream. So also emit sheets that are <= max_sheet_px on the long side at the
    # NATIVE scale. READ THESE, not the contact sheet, whenever they exist.
    per = max(1, max_sheet_px // cell)
    read_sheets = []
    if per * cell < ncols * cell or 3 * cell > max_sheet_px:
        grp = per * per
        for gi in range(0, len(panels), grp):
            chunk = panels[gi:gi + grp]
            rows = int(np.ceil(len(chunk) / per))
            im = Image.new('RGB', (per * cell, rows * cell), (15, 15, 15))
            for i, p in enumerate(chunk):
                im.paste(p, ((i % per) * cell, (i // per) * cell))
            pth = out_dir / f'read_sheet_slices_{gi//grp}.png'
            im.save(pth)
            read_sheets.append(str(pth))
        if proj_panels:
            im = Image.new('RGB', (per * cell, int(np.ceil(3 / per)) * cell), (15, 15, 15))
            for i, p in enumerate(proj_panels):
                im.paste(p, ((i % per) * cell, (i // per) * cell))
            pth = out_dir / 'read_sheet_projections.png'
            im.save(pth)
            read_sheets.append(str(pth))

    out = {
        'mrc': str(mrc_path),
        'voxel_size_A': voxel_A,
        'box': list(vol_res.shape),
        'envelope_box': list(vol_env.shape),
        'crop_box_A': round(box_max * voxel_A, 1),
        'scale': scale,
        'px_per_A': ppa['px_per_A'],
        'px_per_A_floor': px_per_A_target,
        'meets_px_per_A_floor': ppa['meets_floor'],
        'flags': flags,
        'n_slices': len(slices_meta),
        'slices': slices_meta,
        'projections': proj_meta,
        'contact_sheet': str(contact),
        'read_sheets': read_sheets,
        'scalebar_A': scalebar_A,
        'out_dir': str(out_dir),
    }
    # keys added ONLY in the region-scoped path, so the no-mask return is byte-for-byte
    # what it was before 2026-09-07.
    if region is not None:
        out['region'] = region
        out['scope'] = 'region'
        out['scope_note'] = region['scope_note']
    return out


# ----------------------------------------------------------------------------- #
# Surface (iso-surface) render via headless ChimeraX                             #
# ----------------------------------------------------------------------------- #
# 2D slices systematically UNDER-read a 3-4 A map: a true α-helix shows as a
# featureless light band in a flat slice but as an unmistakable rod on a 3D
# iso-surface. So when the slice read is ambiguous (e.g. SUSPICIOUS on a refine),
# render the surface and look for helical rods / β-sheet slabs before trusting or
# rejecting the FSC. Needs ChimeraX (path in base_datainfo.yaml `model_mask_chimerax`).

_CHIMERAX_DEFAULT = None   # resolved from base_datainfo.yaml -- see surface_config()

# Set this once per session to the ACTIVE project's yaml, e.g.
#   vis_res_utils.DATAINFO_PATH = 'projects/<name>/base_datainfo.yaml'
# (same convention as select2d_utils.DATAINFO_PATH).
DATAINFO_PATH: Optional[str] = None


def surface_config(datainfo_path: Optional[str | Path] = None) -> dict:
    """
    Resolve the surface-render environment.

    Nothing cluster-specific is hard-coded, and MACHINE facts are kept OUT of
    base_datainfo.yaml (which describes the SAMPLE). Split:

      base_datainfo.yaml  model_mask_chimerax : the chimerax executable (required)
      environment         VIS_RES_RENDER_HOST : host to ssh to for rendering, if the
                                                local host has no working OpenGL
      environment         VIS_RES_RENDER_LD_PATH : LD_LIBRARY_PATH prepended there

    Set the two env vars once per machine (CryoSPARC .env / job script), e.g. when the
    master cannot render but a GPU node can:
        export VIS_RES_RENDER_HOST=YOUR_GPU_RENDER_HOST
        export VIS_RES_RENDER_LD_PATH=$HOME/cxlibfix
    Legacy `vis_res_render_host` / `vis_res_render_ld_path` yaml keys are still read
    if present, but the environment wins.

    Returns {'chimerax','host','ld_path','source'} with None for anything unset.
    """
    import os
    path = datainfo_path or DATAINFO_PATH
    cfg = {'chimerax': None,
           'host': os.environ.get('VIS_RES_RENDER_HOST') or None,
           'ld_path': os.environ.get('VIS_RES_RENDER_LD_PATH') or None,
           'source': str(path) if path else None}
    if not path:
        return cfg
    p = Path(path)
    if not p.exists():
        return cfg
    for line in p.read_text(errors='replace').splitlines():
        line = line.split('#', 1)[0].strip()          # strip trailing comments
        if ':' not in line:
            continue
        k, v = line.split(':', 1)
        k, v = k.strip(), v.strip()
        if not v:
            continue
        if k == 'model_mask_chimerax':
            cfg['chimerax'] = v
        elif k == 'vis_res_render_host' and not cfg['host']:
            cfg['host'] = v
        elif k == 'vis_res_render_ld_path' and not cfg['ld_path']:
            cfg['ld_path'] = v
    return cfg


def unsharpened_sibling(mrc_path: str | Path) -> Path:
    """
    Map a `..._volume_map_sharp.mrc` path to its unsharpened `..._volume_map.mrc`
    sibling when that file exists, else return the path unchanged. The sharpened map
    fragments into high-frequency speckle on an iso-surface; the unsharpened map gives
    a clean, connected surface where helical rods are legible.
    """
    p = Path(mrc_path)
    if p.name.endswith('_volume_map_sharp.mrc'):
        sib = p.with_name(p.name.replace('_volume_map_sharp.mrc', '_volume_map.mrc'))
        if sib.exists():
            return sib
    return p


def volume_for_mass_kDa(mass_kDa: float) -> float:
    """Expected enclosed volume (A^3) of packed protein: ~0.81 Da/A^3 -> ~1235 A^3/kDa."""
    return mass_kDa * 1000.0 / 0.81


def level_for_enclosed_volume(vol: np.ndarray, voxel_A: float, target_A3: float) -> float:
    """
    The absolute contour level that encloses `target_A3` of volume.

    THIS is how you make surfaces of DIFFERENT maps comparable. A sigma level does not:
    sigma rises with particle count and collapses on a solvent-flattened (masked) map, so
    'mean + 5 sigma' encloses a different amount of matter in every map -- the better-
    populated class looks fatter for a reason that has nothing to do with map quality.
    Equal ENCLOSED VOLUME (from the known molecular mass, see volume_for_mass_kDa) puts
    every map on the same amount of matter, so what differs is shape and connectivity.
    """
    k = int(round(target_A3 / (voxel_A ** 3)))
    k = int(np.clip(k, 1, vol.size - 1))
    flat = vol.ravel()
    return float(np.partition(flat, flat.size - k)[flat.size - k])


def crop_to_signal(vol: np.ndarray, voxel_A: float, sigma: float = 2.0,
                   pad_A: float = 8.0) -> tuple:
    """
    Cut the box down to the SIGNAL (voxels > mean + sigma*std) plus a pad.

    Why every panel is cropped now: a refine box is mostly solvent, so an uncropped render
    spends its pixels on nothing and the particle ends up at ~6 px/A, where a side-chain-
    decorated helix and a featureless 5 A rod look identical. Cropping is NOT binning --
    the voxel grid is untouched, you just stop drawing empty space.
    Returns (subvolume, slices).
    """
    bb = _signal_bbox(vol, sigma=sigma)
    pad = max(1, int(round(pad_A / max(voxel_A, 1e-6))))
    sl = tuple(slice(max(0, b.start - pad), min(n, b.stop + pad))
               for b, n in zip(bb, vol.shape))
    return vol[sl].copy(), sl


# --------------------------------------------------------------------------------- #
# THE px/A FLOOR, and the crop that makes it reachable                               #
# --------------------------------------------------------------------------------- #
# ⚠ REPAIRED 2026-09-05. The two faults this replaces, both measured on AT1R J2105:
#   (1) crop_to_signal() crops to the SIGNAL ENVELOPE -- for a membrane protein that is
#       the whole detergent micelle (163 A here) while the resolution-bearing 7-TM bundle
#       is only ~40-50 A. The pixels went on disordered detergent.
#   (2) the render scale was `scale = max(1, round(600 / box_max))` -- a fixed PIXEL COUNT
#       for the whole box, so the achieved px/A fell out of the box size by accident and
#       NOTHING checked it. J2105 rendered at 3.6 px/A (slices) and ~6.4 px/A (surface),
#       both far below the >=10 px/A the module's own docstrings demand, and it under-read
#       a ~4 A map as ~8.5 A. The knowledge was in the file; the ENFORCEMENT was not.
# The rule now has a number, a mechanism, and a LOUD failure: see px_per_A_report().

def texture_map(vol: np.ndarray, voxel_A: float, hp_A: float = 6.0,
                win_A: float = 8.0) -> np.ndarray:
    """
    Local HIGH-FREQUENCY energy: |vol - lowpass(vol)| smoothed over a `win_A` window.

    This is the quantity the RUBRIC already names as the protein/detergent discriminator
    ("ordered protein ~0.7, disordered detergent ~0.3 in the same map"): ordered protein
    carries fine structure, a micelle/flexible periphery carries amplitude but no texture.
    Amplitude alone cannot tell them apart -- which is exactly why cropping on amplitude
    (crop_to_signal) put the render on the micelle.
    """
    from scipy import ndimage as _nd
    s_hp = max(hp_A / max(voxel_A, 1e-6) / 2.0, 0.8)
    hp = vol - _nd.gaussian_filter(vol, s_hp)
    return _nd.uniform_filter(np.abs(hp), size=max(3, int(round(win_A / max(voxel_A, 1e-6)))))


def order_score(vol: np.ndarray, voxel_A: float, hp_A: float = 6.0,
                win_A: float = 8.0, dens_q: float = 0.90) -> tuple:
    """
    ORDER (not amplitude): local high-frequency energy DIVIDED BY local amplitude, evaluated
    only where there is density. Returns (score, density_mask).

    Amplitude alone cannot find the resolution-bearing region -- a detergent micelle is
    bright AND featureless, so an amplitude crop (crop_to_signal) lands on it. On AT1R J2105
    a raw-texture crop still scored only 0.113 mean over the receptor model mask, i.e. it
    was mostly OFF the protein; dividing by local amplitude fixes that, and it is the same
    quantity the RUBRIC already names ("ordered protein ~0.7, disordered detergent ~0.3").
    """
    from scipy import ndimage as _nd
    s_hp = max(hp_A / max(voxel_A, 1e-6) / 2.0, 0.8)
    win = max(3, int(round(win_A / max(voxel_A, 1e-6))))
    lp = _nd.gaussian_filter(vol, s_hp)
    amp = _nd.uniform_filter(np.abs(lp), size=win)
    tex = _nd.uniform_filter(np.abs(vol - lp), size=win)
    dens = amp > float(np.quantile(amp, dens_q))
    if dens.sum() < 64:
        dens = amp > float(np.quantile(amp, 0.75))
    sc = np.zeros_like(tex)
    sc[dens] = tex[dens] / (amp[dens] + 1e-12)
    return sc, dens


def resolution_bearing_bbox(vol: np.ndarray, voxel_A: float, quantile: float = 0.90,
                            pad_A: float = 6.0, min_box_A: float = 24.0,
                            max_box_vox: Optional[int] = None) -> tuple:
    """
    Bounding box of the ORDERED, resolution-bearing core -- NOT of the signal envelope.

    Data-derived, no sample assumptions:
      1. score every voxel by ORDER (order_score: high-frequency energy / local amplitude);
      2. keep the top-`quantile` ordered voxels inside the density, largest connected
         component only (one hot speckle must not define the box);
      3. if `max_box_vox` is given (the pixel budget at the px/A floor), return a box of at
         most that size CENTRED on the order-weighted centroid -- so the pixels land on the
         most ordered density and the floor is met by construction, rather than by luck.
    Degrades gracefully: a 13 A map has no ordered core, so the largest component is simply
    its densest lobe -- still the right place to look, and the verdict is unchanged.
    """
    from scipy import ndimage as _nd
    sc, dens = order_score(vol, voxel_A)
    vals = sc[dens]
    if vals.size < 8:
        return _signal_bbox(vol)
    m = sc > float(np.quantile(vals, quantile))
    lab, n = _nd.label(m)
    if n > 1:
        m = lab == (int(np.argmax(np.bincount(lab.ravel())[1:])) + 1)
    if m.sum() < 8:
        return _signal_bbox(vol)
    idx = np.argwhere(m)
    lo, hi = idx.min(0), idx.max(0) + 1
    pad = max(1, int(round(pad_A / max(voxel_A, 1e-6))))
    need = int(round(min_box_A / max(voxel_A, 1e-6)))
    if max_box_vox is not None:
        # TIGHTEST cube holding `order_mass_frac` of the ordered signal, capped by the
        # pixel budget. Concentrated order (a membrane protein's TM core inside a big
        # micelle) gives a small box; uniformly ordered density (a soluble tetramer)
        # grows to the budget. No sample-specific constant either way.
        w = sc * m
        ctr = _nd.center_of_mass(w) if w.sum() > 0 else [c / 2 for c in vol.shape]
        ctr = np.asarray(ctr)
        gz, gy, gx = np.ogrid[0:vol.shape[0], 0:vol.shape[1], 0:vol.shape[2]]
        cheb = np.maximum(np.maximum(np.abs(gz - ctr[0]), np.abs(gy - ctr[1])),
                          np.abs(gx - ctr[2]))
        tot = float(w.sum())
        half = int(max_box_vox) // 2
        if tot > 0:
            order_mass_frac = 0.80
            for h in range(need // 2, int(max_box_vox) // 2 + 1):
                if float(w[cheb <= h].sum()) / tot >= order_mass_frac:
                    half = h
                    break
        want_all = min(2 * half + 2 * pad, int(max_box_vox))
        out = []
        for c, n_ in zip(ctr, vol.shape):
            want = min(max(want_all, need), n_)
            s0 = max(0, min(int(round(c - want / 2)), n_ - want))
            out.append(slice(s0, s0 + want))
        return tuple(out)
    out = []
    for a, b, n_ in zip(lo, hi, vol.shape):
        a, b = max(0, int(a) - pad), min(n_, int(b) + pad)
        if b - a < need:                      # never crop below a readable minimum
            c = (a + b) // 2
            a, b = max(0, c - need // 2), min(n_, c + need // 2)
        out.append(slice(a, b))
    return tuple(out)


# --------------------------------------------------------------------------------- #
# THE MASK REGION — for a LOCAL REFINE, judge and report the region that was refined  #
# --------------------------------------------------------------------------------- #
# ⭐ ADDED 2026-09-07 (human directive). In a `new_local_refine` ONLY the masked region is
# refined; everything outside is unrefined carry-over from the consensus map. Therefore:
#   * a SINGLE WHOLE-MAP resolution number for a locally refined map is a CATEGORY ERROR,
#     and this tool was emitting one;
#   * the mask-inflated FSC describes the MASKED REGION, so a vision estimate of THAT SAME
#     region is the like-for-like comparison the over-claim check needs (cross-kind
#     comparisons have burned this project repeatedly);
#   * it is where the science is (receptor / pocket / ligand), and phase 3+ requires the
#     local/target-region resolution to be reported SEPARATELY from the global figure.
# In an automated pipeline the mask is a KNOWN INPUT — the tool should be TOLD the region
# rather than infer it. With no mask supplied nothing here runs and the behaviour is the
# 2026-09-05 resolution-bearing-bbox path, unchanged.

def load_mask(mask, shape: Optional[tuple] = None) -> tuple:
    """
    Read a mask (path to .mrc, or an ndarray) and check it lives on the map's grid.

    Returns (mask_array_float32, source_str). FAILS LOUDLY on a shape mismatch: a mask on a
    different grid silently crops the WRONG region, which is precisely the silent-wrong-answer
    failure this module exists to prevent.
    """
    if isinstance(mask, np.ndarray):
        arr, src = mask.astype(np.float32), '<ndarray>'
    else:
        p = Path(mask)
        with mrcfile.open(str(p), permissive=True) as m:
            arr = m.data.astype(np.float32).copy()
        src = str(p)
    if arr.ndim != 3:
        raise ValueError(f'mask must be a 3D volume, got shape {arr.shape} ({src})')
    if shape is not None and tuple(arr.shape) != tuple(shape):
        raise ValueError(
            f'MASK GRID MISMATCH: mask {arr.shape} vs map {tuple(shape)} ({src}). '
            f'Re-import/resample the mask onto the map grid — cropping to a mask from a '
            f'different box would judge the WRONG region and report it as the right one.')
    return arr, src


def mask_region_bbox(vol: np.ndarray, mask: np.ndarray, voxel_A: float,
                     threshold: float = 0.5, pad_A: float = 6.0,
                     max_box_vox: Optional[int] = None) -> tuple:
    """
    Crop box for the MASKED REGION. The mask TAKES PRECEDENCE over resolution_bearing_bbox():
    the region is a known input, not something to be inferred.

      * mask bbox (mask > threshold) + pad, clipped to the box;
      * if that does not fit the pixel budget at the px/A floor, fall back to
        resolution_bearing_bbox() run on the MASK-RESTRICTED volume (the ordered-core search
        is then confined to the region) and capped by the budget — so the floor is still met
        by construction and the pixels still land inside the region.
    Returns (slices, info) with info describing what fixed the box.
    """
    m = mask > threshold
    if not m.any():
        raise ValueError(f'mask has no voxel above threshold {threshold} — nothing to judge')
    idx = np.argwhere(m)
    lo, hi = idx.min(0), idx.max(0) + 1
    pad = max(1, int(round(pad_A / max(voxel_A, 1e-6))))
    sl = tuple(slice(max(0, int(a) - pad), min(n, int(b) + pad))
               for a, b, n in zip(lo, hi, vol.shape))
    extent = max(s.stop - s.start for s in sl)
    how = 'mask_bbox'
    if max_box_vox is not None and extent > int(max_box_vox):
        # region too big for the floor -> tighten INSIDE the region, never outside it
        sl = resolution_bearing_bbox(vol * np.clip(mask, 0.0, 1.0), voxel_A,
                                     pad_A=pad_A, max_box_vox=int(max_box_vox))
        how = 'mask_bbox_exceeds_px_budget->resolution_bearing_bbox_inside_mask'
    info = {'how': how,
            'mask_bbox_extent_vox': [int(x) for x in (hi - lo)],
            'mask_bbox_extent_A': [round(float(x) * voxel_A, 1) for x in (hi - lo)]}
    return sl, info


def region_report(mask: np.ndarray, sl: tuple, voxel_A: float,
                  threshold: float = 0.5,
                  name: str = 'mask region',
                  source: str = '') -> dict:
    """
    Identity + size of the region being judged, so a caller cannot mistake the SCOPE of the
    estimate: what it is, how big it is, and how much of the rendered crop it actually covers.
    """
    m = mask > threshold
    sub = m[sl]
    vox_A3 = voxel_A ** 3
    tot = max(float(m.sum()), 1.0)
    return {
        'name': name,
        'mask_source': source,
        'mask_threshold': threshold,
        'region_volume_A3': round(float(m.sum()) * vox_A3, 1),
        'region_volume_in_crop_A3': round(float(sub.sum()) * vox_A3, 1),
        # how much of the REGION the rendered crop actually shows. < 1 means the region did
        # not fit the pixel budget at the px/A floor and only a PART of it is being judged --
        # that must be said out loud (flag REGION_PARTIALLY_SHOWN), never assumed away.
        'region_frac_in_crop': round(float(sub.sum()) / tot, 4),
        'crop_frac_covered_by_mask': round(float(sub.mean()), 4),
        'crop_box_vox': [int(s.stop - s.start) for s in sl],
        'crop_box_A': [round((s.stop - s.start) * voxel_A, 1) for s in sl],
        'scope': 'region',
        'scope_note': (f'This estimate applies to the region "{name}" ONLY '
                       f'(the volume covered by the supplied mask). It is NOT a whole-map '
                       f'number: outside the mask a local refine leaves unrefined carry-over.'),
    }


def px_per_A_report(box_vox: int, voxel_A: float, scale: int,
                    floor: float = PX_PER_A_FLOOR) -> dict:
    """px/A actually achieved by an integer NEAREST upscale, and whether it clears the floor."""
    ppa = scale / max(voxel_A, 1e-9)
    ok = ppa >= floor - 1e-9
    return {'px_per_A': round(ppa, 2), 'floor': floor, 'meets_floor': bool(ok),
            'panel_px': box_vox * scale,
            'flags': [] if ok else ['PX_PER_A_FLOOR_NOT_MET']}


def _scale_for_px_per_A(voxel_A: float, target: float = PX_PER_A_FLOOR) -> int:
    """Smallest INTEGER upscale that reaches `target` px/A (integer => still no binning)."""
    return max(1, int(np.ceil(target * voxel_A - 1e-9)))


def _write_mrc(vol: np.ndarray, voxel_A: float, path: str | Path) -> Path:
    path = Path(path)
    with mrcfile.new(str(path), overwrite=True) as m:
        m.set_data(np.ascontiguousarray(vol, dtype=np.float32))
        m.voxel_size = voxel_A
    return path


def auto_display_levels(vol: np.ndarray, voxel_A: float,
                        n_scan: int = 22, collapse: float = 0.70, keep: float = 0.80,
                        fuller_ratio: float = 1.7) -> dict:
    """
    Pick the TWO display levels automatically: wide scan, then the readable window.

    The by-eye search (bloated blob -> readable -> coral -> dust) is a CONNECTIVITY curve
    and can be measured without rendering. Scanning by ENCLOSED VOLUME (not by level value,
    which is not comparable between maps) and tracking the fraction of enclosed volume in
    the largest connected component:

        J1078   67k A^3 0.92 | 38k 0.92 | 28k 0.93 | 22k 0.94 | 18k 0.95 | 14k 0.59 | 9k 0.14
                                                              ^ readable ^   ^ COLLAPSE

    The window ENDS at the collapse, so the informative levels are the last ones before it.
    `finer` = the smallest enclosed volume still holding together (frac >= `keep`), i.e.
    maximum detail on one body; `fuller` = `fuller_ratio`x that volume, the same density
    with the envelope intact. Rendering these two costs 2 renders instead of an 8-row
    ladder, and the choice is reproducible instead of an impression.

    Returns {levels: [fuller, finer], chosen: [row, row], table: [...], collapse: row|None}.
    """
    from scipy import ndimage
    pos = vol[vol > 0]
    if pos.size == 0:
        raise ValueError('map has no positive density')
    vox_vol = voxel_A ** 3
    v0 = float((vol > float(np.percentile(pos, 50))).sum()) * vox_vol
    targets = np.geomspace(v0, max(v0 * 0.02, vox_vol * 100), n_scan)

    table = []
    for tv in targets:
        lv = level_for_enclosed_volume(vol, voxel_A, tv)
        mask = vol > lv
        occ = int(mask.sum())
        if occ < 50:
            break
        lab, n = ndimage.label(mask)
        sizes = np.bincount(lab.ravel())[1:]
        table.append({'level': float(lv), 'enclosed_A3': occ * vox_vol, 'n_frag': int(n),
                      'frac_in_largest': float(sizes.max() / occ)})
    if len(table) < 3:
        raise ValueError('level scan found too few usable levels')

    coll = next((r for r in table if r['frac_in_largest'] < collapse), None)
    holding = [r for r in table if r['frac_in_largest'] >= keep
               and (coll is None or r['enclosed_A3'] > coll['enclosed_A3'])]
    finer = holding[-1] if holding else table[len(table) // 2]
    want = finer['enclosed_A3'] * fuller_ratio
    fuller = min(table, key=lambda r: abs(r['enclosed_A3'] - want))
    if fuller['level'] >= finer['level']:
        fuller = table[max(0, table.index(finer) - 2)]
    return {'levels': [fuller['level'], finer['level']], 'chosen': [fuller, finer],
            'table': table, 'collapse': coll}


def render_zoom_crop(mrc_path: str | Path,
                     out_dir: str | Path,
                     level: float,
                     cube_A: float = 64.0,
                     centre_voxel=None,
                     size: int = 1500,
                     scalebar_A: float = 10.0,
                     **kw) -> dict:
    """
    Cut a CUBE out of the map around its densest region and render that, big, with the bar.

    WHY THIS IS NOT OPTIONAL for a 3-4 A map. A whole-particle render puts a ~250 A box in
    ~1500 px = ~6 px/A, and at that scale a helix decorated with side chains and a featureless
    5 A rod look the SAME ("chunky worms"). Cropping to ~64 A gives ~23 px/A, where the
    discriminating features are unmistakable. Measured against the 10 A bar:
        tube diameter ~5 A + knobs of 2-3 A on it + ~10 A helix-helix spacing  -> 3-3.5 A
        tube diameter ~8 A, smooth, no knobs                                    -> ~5 A
    (hTA1/zll calibration: J1078 read as the first, J1032 as the second -- and judging them
    whole-particle had put BOTH at ~5 A.)

    Returns the render dict plus {crop_mrc, cube_A, centre_voxel}.
    """
    from scipy import ndimage
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    with mrcfile.open(str(mrc_path), permissive=True) as m:
        vol = m.data.astype(np.float32).copy()
        vx = _voxel_size_A(m)
    if centre_voxel is None:
        lab, n = ndimage.label(vol > level)
        if n == 0:
            centre_voxel = [c // 2 for c in vol.shape]
        else:
            sizes = ndimage.sum(np.ones_like(lab), lab, range(1, n + 1))
            big = int(np.argmax(sizes)) + 1
            centre_voxel = [int(round(c)) for c in ndimage.center_of_mass(vol, lab, big)]
    half = int(round((cube_A / 2.0) / vx))
    sl = tuple(slice(max(0, c - half), min(n_, c + half))
               for c, n_ in zip(centre_voxel, vol.shape))
    sub = vol[sl].copy()
    crop = out / f'{Path(mrc_path).stem}_crop.mrc'
    with mrcfile.new(str(crop), overwrite=True) as m:
        m.set_data(sub)
        m.voxel_size = vx
    print(f'[render_zoom_crop] {sub.shape[0]*vx:.0f} A cube at voxel {centre_voxel} '
          f'-> {size/(sub.shape[0]*vx):.1f} px/A (whole box was {max(vol.shape)*vx:.0f} A)')
    r = render_map_surface(crop, out_dir=out / 'render', level=level, size=size,
                           scalebar_A=scalebar_A, prefer_unsharpened=False, **kw)
    r.update({'crop_mrc': str(crop), 'cube_A': sub.shape[0] * vx, 'centre_voxel': centre_voxel})
    return r


def render_surface_pair(mrc_path: str | Path,
                        out_dir: str | Path,
                        target_volumes_A3=None,
                        mass_kDa: Optional[float] = None,
                        volume_ladder: tuple = (0.30, 0.22, 0.16),
                        size: int = 900,
                        **kw) -> dict:
    """
    Render the UNSHARPENED and SHARPENED map SIDE BY SIDE at MATCHED enclosed volumes, in
    one sheet, for the resolution read.

    WHY BOTH. They answer different halves of the question and neither does both:
      UNSHARP -> the FOLD. Smooth continuous tubes, so you can see whether helices are
                 resolved and follow the backbone. Side chains are smoothed away.
      SHARP   -> the DETAIL. The same tubes plus the high-frequency content: side-chain
                 knobs, helix pitch. Also amplified noise, which is why you read it next to
                 the unsharp shape rather than alone.
    Matching by ENCLOSED VOLUME (not by sigma, not by absolute level) is what makes the two
    columns comparable: sharpening changes the histogram, so the same sigma encloses
    different amounts of matter in the two maps.

    WORKFLOW (as used on hTA1 J548):
      1. ladder the UNSHARPENED map first (render_map_surface(..., prefer_unsharpened=True))
         and pick the level range where the fold reads best -- not too bloated, not yet
         breaking up;
      2. pass those volumes here as `volume_ladder` multiples (or `target_volumes_A3`);
      3. read the paired sheet: fold from the left panel of each pair, side chains from the
         right, then estimate the resolution from BOTH.

    Returns {sheet, unsharp, sharp, levels_unsharp, levels_sharp, target_volumes_A3}.
    """
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    if target_volumes_A3 is None:
        if mass_kDa is None:
            raise ValueError('give target_volumes_A3=[...] or mass_kDa=<complex mass>')
        base = volume_for_mass_kDa(mass_kDa)
        target_volumes_A3 = [m * base for m in volume_ladder]
    target_volumes_A3 = [float(t) for t in target_volumes_A3]

    u = render_map_surface(mrc_path, out_dir=out / 'unsharp', target_volume_A3=target_volumes_A3,
                           size=size, prefer_unsharpened=True, **kw)
    sh = render_map_surface(mrc_path, out_dir=out / 'sharp', target_volume_A3=target_volumes_A3,
                            size=size, prefer_unsharpened=False, **kw)

    views = ('front', 'side', 'top')
    def _pick(res, lv, view):
        for v in res['views']:
            if v['level'] == lv and v['view'] == view and Path(v['png']).exists():
                return v['png']
        return None

    w, h = Image.open(_pick(u, u['levels'][0], 'front')).size
    hdr = max(26, h // 26)
    sheet = Image.new('RGB', (6 * w, len(target_volumes_A3) * (h + hdr)), (255, 255, 255))
    dr = ImageDraw.Draw(sheet)
    font = _font(max(15, w // 46))
    for ri, tv in enumerate(target_volumes_A3):
        y0 = ri * (h + hdr)
        for vi, view in enumerate(views):
            for ci, (tag, res) in enumerate((('UNSHARP', u), ('SHARP', sh))):
                col = 2 * vi + ci
                png = _pick(res, res['levels'][ri], view)
                if png:
                    sheet.paste(Image.open(png).convert('RGB'), (col * w, y0 + hdr))
                dr.text((col * w + 8, y0 + 4),
                        f'{tag} {view}  lvl={res["levels"][ri]:g}  ({tv/1000:.0f}k A^3)',
                        fill=(0, 0, 0), font=font)
        dr.line([(0, y0), (6 * w, y0)], fill=(120, 120, 120), width=2)
    sp = out / 'surface_pair_sheet.png'
    sheet.save(sp)
    print(f'[render_surface_pair] {sp}  (rows = enclosed volume, cols = unsharp|sharp x front/side/top)')
    return {'sheet': str(sp), 'unsharp': u, 'sharp': sh,
            'levels_unsharp': u['levels'], 'levels_sharp': sh['levels'],
            'target_volumes_A3': target_volumes_A3}


def surface_panel(mrc_path: str | Path,
                  out_dir: str | Path,
                  crop: bool = True,
                  crop_pad_A: float = 8.0,
                  zoom_cube_A: Optional[float] = None,
                  px_per_A_target: Optional[float] = PX_PER_A_FLOOR,
                  size: int = 1200,
                  scalebar_A: float = 10.0,
                  levels: Optional[list] = None,
                  mask=None,
                  mask_label: Optional[str] = None,
                  mask_threshold: float = 0.5,
                  **kw) -> dict:
    """
    The surface read, as ONE call, doing the four things the J1078/J548 tests proved matter:

      1. CROP to the signal box (both maps, identical crop) -- so pixels go on the particle,
         not on solvent. Native grid; cropping is not binning.
      2. WIDE-then-FINE level search, done numerically (auto_display_levels): scan two orders
         of enclosed volume, track connectivity, and return the TWO levels that bracket the
         readable window -- a fuller one (envelope whole) and a finer one (maximum detail
         still on one body). No 8-row ladder to eyeball.
      3. UNSHARP and SHARP side by side at the SAME enclosed volume, in one sheet: the
         unsharpened panel shows whether the FOLD is real (continuous tubes), and only then
         do the sharpened panel's knobs count as SIDE CHAINS rather than amplified noise.
      4. A SCALE BAR in every view (screen-space, from the fixed orthographic field width),
         because feature SIZE is the measurement: ~5 A tubes + 2-3 A knobs + ~10 A spacing
         is a 3-3.5 A map; ~8 A smooth tubes with no knobs is ~5 A.

    Returns {sheet, levels_unsharp, levels_sharp, enclosed_A3, crop_box_A, px_per_A, scan}.
    """
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    sharp_p = Path(mrc_path)
    unsharp_p = unsharpened_sibling(sharp_p)

    def _load(p):
        with mrcfile.open(str(p), permissive=True) as m:
            return m.data.astype(np.float32).copy(), _voxel_size_A(m)

    vu, vx = _load(unsharp_p)
    vs, _ = _load(sharp_p)
    flags: list[str] = []
    region = None
    mask_arr = mask_src = None
    if mask is not None:
        mask_arr, mask_src = load_mask(mask, shape=vu.shape)
    if zoom_cube_A:
        # ZOOM: a cube around the densest body instead of the whole signal box. Needed for
        # the FINE read -- a whole particle at ~5 px/A cannot separate a side-chain knob
        # from a bump, while a ~64 A cube at ~12-20 px/A can.
        from scipy import ndimage
        probe = float(np.percentile(vu[vu > 0], 92))
        lab, n = ndimage.label(vu > probe)
        if n:
            sizes = np.bincount(lab.ravel())[1:]
            ctr = [int(round(c)) for c in
                   ndimage.center_of_mass(vu, lab, int(np.argmax(sizes)) + 1)]
        else:
            ctr = [c // 2 for c in vu.shape]
        half = int(round((zoom_cube_A / 2.0) / vx))
        sl = tuple(slice(max(0, c - half), min(n_, c + half)) for c, n_ in zip(ctr, vu.shape))
        vu, vs = vu[sl].copy(), vs[sl].copy()
        print(f'[surface_panel] ZOOM cube -> {vu.shape} ({vu.shape[0]*vx:.0f} A) at voxel {ctr}')
    elif crop:                                # identical crop for both maps
        if mask_arr is not None:
            # ⭐ ADDED 2026-09-24. THE MASK TAKES PRECEDENCE, exactly as in render_map_slices.
            # In a local refine only the masked region was refined; everything outside it is
            # unrefined carry-over, so judging the whole box UNDER-READS the refinement that
            # was actually done. Cropping here also fixes the level search: auto_display_levels
            # runs on the CROPPED volume below, so the display levels are normalised inside
            # the region instead of being spent on out-of-mask smear.
            sl, how = mask_region_bbox(vu, mask_arr, vx, threshold=mask_threshold,
                                       pad_A=crop_pad_A)
            vu, vs = vu[sl].copy(), vs[sl].copy()
            rname = (mask_label or (Path(mask_src).stem if mask_src != '<ndarray>'
                                    else 'mask region'))
            region = region_report(mask_arr, sl, vx, threshold=mask_threshold,
                                   name=rname, source=mask_src)
            region.update(how)
            flags.append('REGION_SCOPED')
            print(f'[surface_panel] REGION_SCOPED -> cropped to "{rname}" {vu.shape} '
                  f'({max(vu.shape)*vx:.0f} A box); the read describes THAT REGION only')
            if region['region_frac_in_crop'] < 0.90:
                flags.append('REGION_PARTIALLY_SHOWN')
                print(f'[surface_panel] *** REGION_PARTIALLY_SHOWN: only '
                      f'{region["region_frac_in_crop"]*100:.1f}% of the region is in the crop')
        else:
            # REPAIRED 2026-09-05: crop to the RESOLUTION-BEARING core, not the signal envelope.
            # crop_to_signal() keeps the whole detergent micelle, which on AT1R J2105 put the
            # surface at ~6.4 px/A -- below the module's own >=10 px/A floor.
            sl = resolution_bearing_bbox(vu, vx, pad_A=crop_pad_A)
            vu, vs = vu[sl].copy(), vs[sl].copy()
            print(f'[surface_panel] cropped to the RESOLUTION-BEARING core -> {vu.shape} '
                  f'({max(vu.shape)*vx:.0f} A box)')
    cu = _write_mrc(vu, vx, out / 'crop_unsharp.mrc')
    cs = _write_mrc(vs, vx, out / 'crop_sharp.mrc')

    scan = None
    if levels is None:                        # wide scan -> the two display levels
        scan = auto_display_levels(vu, vx)
        levels = scan['levels']
        for tag, row in zip(('fuller', 'finer'), scan['chosen']):
            print(f"[surface_panel] {tag}: level {row['level']:.4f} "
                  f"({row['enclosed_A3']/1000:.0f}k A^3, connectivity "
                  f"{row['frac_in_largest']:.2f}, {row['n_frag']} frags)")
    enclosed = [float((vu > lv).sum()) * vx ** 3 for lv in levels]
    # same ENCLOSED VOLUME on the sharpened map (sharpening changes the histogram)
    levels_sharp = [level_for_enclosed_volume(vs, vx, e) for e in enclosed]

    fw = max(vu.shape) * vx * 1.15
    if px_per_A_target:
        # Resolution of the RENDER, not of the map: below ~10 px/A a side-chain knob and a
        # bump are the same handful of pixels. Raising the pixel count is the right fix --
        # cropping deeper into a large particle just shows a cut face, not a surface.
        size = int(round(px_per_A_target * fw))
        print(f'[surface_panel] px/A target {px_per_A_target} -> render size {size} px')
    ru = _render_levels(cu, out / 'unsharp', levels, size, scalebar_A, fw, kw)
    rs = _render_levels(cs, out / 'sharp', levels_sharp, size, scalebar_A, fw, kw)

    sheet = _compose_pair_sheet(ru, rs, levels, levels_sharp, enclosed, out, scalebar_A)
    ppa = round(size / fw, 3)
    floor_met = ppa >= PX_PER_A_FLOOR - 1e-9
    fl = list(flags) if floor_met else list(flags) + ['PX_PER_A_FLOOR_NOT_MET']
    if not floor_met:
        print(f'[surface_panel] *** PX_PER_A_FLOOR_NOT_MET: {ppa} px/A < {PX_PER_A_FLOOR} — '
              f'do NOT read a resolution off this panel.')
    else:
        print(f'[surface_panel] surface at {ppa} px/A (floor {PX_PER_A_FLOOR})')
    out_d = {'sheet': str(sheet), 'levels_unsharp': levels, 'levels_sharp': levels_sharp,
             'enclosed_A3': [round(e) for e in enclosed], 'crop_box_A': round(vu.shape[0] * vx, 1),
             'px_per_A': ppa, 'meets_px_per_A_floor': 'PX_PER_A_FLOOR_NOT_MET' not in fl,
             'flags': fl, 'scan': scan, 'unsharp': ru, 'sharp': rs}
    # keys added ONLY in the region-scoped path, so the no-mask return is unchanged
    if region is not None:
        out_d['region'] = region
        out_d['scope'] = 'region'
        out_d['scope_note'] = region['scope_note']
    return out_d


def _render_levels(mrc, out_dir, levels, size, scalebar_A, field_width_A, kw):
    """render_map_surface once per level, into one directory (keeps the ChimeraX calls few)."""
    outs = []
    for i, lv in enumerate(levels):
        r = render_map_surface(mrc, out_dir=Path(out_dir) / f'L{i}', level=lv, size=size,
                               scalebar_A=scalebar_A, field_width_A=field_width_A,
                               prefer_unsharpened=False, **kw)
        outs.append(r)
    return outs


def _compose_pair_sheet(ru, rs, levels_u, levels_s, enclosed, out, scalebar_A):
    """rows = display level, cols = UNSHARP|SHARP x front/side/top."""
    def _pick(res, view):
        for v in res['views']:
            if v['view'] == view and Path(v['png']).exists():
                return v['png']
        return None
    first = _pick(ru[0], 'front')
    w, h = Image.open(first).size
    hdr = max(26, h // 26)
    sheet = Image.new('RGB', (6 * w, len(levels_u) * (h + hdr)), (255, 255, 255))
    dr = ImageDraw.Draw(sheet)
    font = _font(max(15, w // 46))
    for ri in range(len(levels_u)):
        y0 = ri * (h + hdr)
        for vi, view in enumerate(('front', 'side', 'top')):
            for ci, (tag, res, lv) in enumerate((('UNSHARP', ru[ri], levels_u[ri]),
                                                 ('SHARP', rs[ri], levels_s[ri]))):
                png = _pick(res, view)
                if png:
                    sheet.paste(Image.open(png).convert('RGB'), ((2 * vi + ci) * w, y0 + hdr))
                dr.text(((2 * vi + ci) * w + 8, y0 + 4),
                        f'{tag} {view}  lvl={lv:.4f}  ({enclosed[ri]/1000:.0f}k A^3)  bar={scalebar_A:g}A',
                        fill=(0, 0, 0), font=font)
        dr.line([(0, y0), (6 * w, y0)], fill=(120, 120, 120), width=2)
    p = Path(out) / 'surface_panel.png'
    sheet.save(p)
    print(f'[surface_panel] {p}  (rows = display level, cols = unsharp|sharp x front/side/top)')
    return p


def sharpening_report(mrc_path: str | Path) -> dict:
    """
    How much did sharpening change this map? Compare `..._volume_map_sharp.mrc` with its
    unsharpened sibling.

    Returns {cc, hf_ratio, solvent_speckle_pct_sharp, solvent_speckle_pct_unsharp, advice}.

    WHY IT MATTERS FOR THE RESOLUTION READ: CryoSPARC picks the sharpening B-factor from the
    REPORTED resolution, so an over-claimed FSC produces an over-sharpened map -- the fake
    resolution and the speckled density are the SAME artefact seen twice. A low cc (map
    substantially restructured) with a large hf_ratio is the signature; read the UNSHARPENED
    map for the slice/projection panel in that case, and treat the FSC with suspicion.
    Empirical anchors: beta-gal J238 (honest 4.37 A) cc 0.99 / hf 1.3x; hTA1 J548 (FSC 3.65 A,
    SUSPICIOUS) cc 0.61 / hf 2.5x; beta-gal J216 (13 A) cc 1.00 -- nothing to sharpen.
    """
    from scipy import ndimage
    sharp = Path(mrc_path)
    unsharp = unsharpened_sibling(sharp)
    if unsharp == sharp:
        return {'cc': None, 'hf_ratio': None, 'advice': 'no unsharpened sibling found'}

    def _read(p):
        with mrcfile.open(str(p), permissive=True) as m:
            return m.data.astype(np.float32).copy()

    def _speckle(v):
        z = _solvent_z(v)
        idx = np.ogrid[tuple(slice(0, k) for k in z.shape)]
        c = [(k - 1) / 2.0 for k in z.shape]
        r = np.sqrt(sum((i - ci) ** 2 for i, ci in zip(idx, c)))
        solv = z[r > 0.85 * (min(z.shape) / 2.0)]
        hf = float((z - ndimage.uniform_filter(z, size=3)).std())
        return 100.0 * float((solv > 3).mean()), hf

    vs, vu = _read(sharp), _read(unsharp)
    cc = float(np.corrcoef(vs.ravel(), vu.ravel())[0, 1])
    sp_s, hf_s = _speckle(vs)
    sp_u, hf_u = _speckle(vu)
    ratio = hf_s / (hf_u or 1e-9)
    if cc >= 0.95:
        advice = 'sharpening is mild -- either map reads the same; use the sharp deliverable.'
    elif cc >= 0.8:
        advice = 'moderate sharpening -- prefer the unsharpened map for the slice read.'
    else:
        advice = ('sharpening RESTRUCTURED the map -- read the UNSHARPENED map, and treat the '
                  'reported FSC as suspect: the B-factor came from that same number.')
    return {'cc': round(cc, 4), 'hf_ratio': round(ratio, 2),
            'solvent_speckle_pct_sharp': round(sp_s, 2),
            'solvent_speckle_pct_unsharp': round(sp_u, 2),
            'sharp': str(sharp), 'unsharp': str(unsharp), 'advice': advice}


def render_map_surface(mrc_path: str | Path,
                       out_dir: Optional[str | Path] = None,
                       level: Optional[float] = None,
                       sigmas: tuple = (2.0, 3.0, 4.0, 5.0, 6.0, 8.0),
                       target_volume_A3: Optional[float] = None,
                       mass_kDa: Optional[float] = None,
                       volume_ladder: tuple = (1.5, 1.0, 0.7, 0.5, 0.35, 0.25, 0.15),
                       chimerax: Optional[str] = _CHIMERAX_DEFAULT,
                       size: int = 760,
                       bg: str = 'white',
                       prefer_unsharpened: bool = False,
                       timeout_s: int = 360,
                       datainfo_path: Optional[str | Path] = None,
                       render_host: Optional[str] = None,
                       render_ld_path: Optional[str] = None,
                       scalebar_A: float = 10.0,
                       field_width_A: Optional[float] = None) -> dict:
    """
    Iso-surface render of a map from 3 orthogonal views (front/side/top), via
    headless ChimeraX.

    SCOPE: this is the SECOND LOOK for the resolution verdict on ONE map -- it answers
    'does the density show the features the FSC requires'. It is NOT the volume-ranking
    tool: sigma levels are not comparable between maps (sigma rises with particle count
    and collapses on a masked map). To compare maps on a surface, pass
    mass_kDa=<complex mass> (or target_volume_A3) so every map is rendered at the SAME
    enclosed volume; otherwise rank with compare_volumes(), which is shared-window and
    count-robust by construction.

    SHARPENED map by default (prefer_unsharpened=False) -- the OPPOSITE of the slice panel.
    A surface shows the shape of one iso-level, so the high-frequency detail sharpening
    restores is exactly what turns a smooth tube into a helix with visible pitch and side-
    chain bumps. Sharpening's cost (amplified noise) shows up as a mesh/crust at LOW levels,
    which is a LEVEL problem, not a map problem -- that is what the ladder is for.
    GO DEEP BEFORE YOU DOUBT THE MAP. On hTA1 J548 the sharp surface was a uniform
    vermiculate crust from 1.2x down to 0.5x of molecular volume, and only at ~0.18x
    (8.6 sigma) did it resolve into backbone tubes decorated with SIDE-CHAIN knobs -- which
    the unsharpened map at the SAME enclosed volume smooths away entirely. Reading the
    unsharpened surface there COSTS the side-chain evidence and produces a false SUSPICIOUS
    verdict (it did, until unmasked_halfmap_fsc() settled it: 3.94 A floor vs 3.65 A
    reported). Only fall back to prefer_unsharpened=True after the DEEP rows have also
    failed, and confirm with unmasked_halfmap_fsc() before flagging the FSC.

    THRESHOLD LADDER. The contour level is the single biggest lever on what you see, so
    this always renders a ladder of levels, one row each, cheapest-to-read first:
      mass_kDa / target_volume_A3 -> a VOLUME ladder: `volume_ladder` multiples of the
                     molecular volume (default 1.5x .. 0.15x). Physically anchored and
                     comparable across maps -- prefer this whenever the mass is known.
                     The ladder spans a WIDE range on purpose. A soluble map shows its
                     structure near 1.2x-0.6x, but a MEMBRANE protein needs the deep rows
                     (~0.3x-0.15x): the mass anchor counts protein only, so the detergent
                     micelle inflates every level above that, and an over-sharpened map
                     buries the helices under a noise crust until you cut well past it.
                     hTA1 J548 resolved its 7TM bundle into separated tubes only at
                     0.30x-0.16x, and only on the UNSHARPENED map.
      neither         -> a SIGMA ladder: one row per entry in `sigmas` (level = mean +
                     sigma*std), default 2..8 sigma. Use when the mass is unknown; NOT
                     comparable between maps.
      level=<v>       -> a single absolute level (one row).
    READ THE LADDER TOP-DOWN and pick the row where the envelope first stops being
    inflated but has not yet broken up: too low = bloated blob plus noise specks; too high
    = the map disintegrates into fragments. Judge secondary structure on THAT row (and note
    that a membrane protein's micelle inflates the low rows, since a mass-anchored target
    counts protein only).
      level=<v>   -> a single absolute level (one row).
    Returns {surface_sheet, views[{level,view,png}], levels, out_dir}. Read
    `surface_sheet` (rows=level, cols=front/side/top) and judge secondary structure
    there — it is the reliable check for a borderline 3-4 A map.
    """
    mrc_path = Path(mrc_path)
    if prefer_unsharpened:
        swapped = unsharpened_sibling(mrc_path)
        if swapped != mrc_path:
            print(f'[render_map_surface] using unsharpened sibling for surface: {swapped.name}')
            mrc_path = swapped
    # absolute paths throughout: a remote render host does not share our cwd
    mrc_path = mrc_path.resolve()
    out_dir = (Path(out_dir) if out_dir else mrc_path.parent / f'{mrc_path.stem}_vis_surf').resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    with mrcfile.open(str(mrc_path), permissive=True) as m:
        vol = m.data.astype(np.float32)
        mu, sd = float(vol.mean()), float(vol.std())
    if mass_kDa is not None and target_volume_A3 is None:
        target_volume_A3 = volume_for_mass_kDa(mass_kDa)
    if target_volume_A3 is not None and np.isscalar(target_volume_A3):
        # a single target becomes a LADDER around it, so one call always spans levels
        target_volume_A3 = [m * float(target_volume_A3) for m in volume_ladder]
    if level is not None:
        levels = [level]
    elif target_volume_A3 is not None:
        # comparable-across-maps levels: same ENCLOSED VOLUME, not the same sigma.
        # A sequence gives a mass-anchored SWEEP (e.g. 1.5x / 1.0x / 0.6x the molecular
        # volume) -- the right sweep when sigma is compressed by solvent flattening.
        with mrcfile.open(str(mrc_path), permissive=True) as m:
            vx = _voxel_size_A(m)
        targets = [float(t) for t in np.atleast_1d(target_volume_A3)]
        levels = []
        for t in targets:
            lv = level_for_enclosed_volume(vol, vx, t)
            print(f'[render_map_surface] level {lv:.5f} encloses {t:.0f} A^3 '
                  f'(= {(lv - mu) / (sd or 1e-9):.1f} sigma here) -- use the SAME target volume '
                  f'for every map you intend to compare')
            levels.append(round(lv, 5))
    else:
        levels = [round(mu + s * sd, 5) for s in sigmas]

    # one ChimeraX script renders every (level x view) without reopening the map.
    # A surface with no scale is unreadable: the SAME map reads as "chunky blobs" at
    # whole-particle framing and as "helices with side chains" zoomed in, because nothing on
    # screen says how many Angstrom a bump is. So the camera is pinned to an ORTHOGRAPHIC
    # projection of KNOWN field width -> constant A/pixel -> a true screen-space scale bar,
    # and renders of different maps/levels/zooms become directly comparable.
    #
    # ChimeraX 1.3 offscreen dies on `camera ortho` (its OffScreenRenderingContext has no
    # .stereo attribute, which the camera command reads). Fixed here by setting that missing
    # attribute on the context CLASS from inside ChimeraX, then switching the camera in
    # Python -- `fieldWidth` is also not a 1.3 command keyword, it is a camera property.
    with mrcfile.open(str(mrc_path), permissive=True) as _m:
        _box_A = max(_m.data.shape) * _voxel_size_A(_m)
    fw = float(field_width_A) if field_width_A else _box_A * 1.15
    px_per_A = size / fw

    setfw = out_dir / '_setfw.py'
    setfw.write_text(
        'import sys\n'
        'from chimerax.core.commands import run\n'
        'ctx = session.main_view.render.opengl_context\n'
        "if not hasattr(ctx, 'stereo'):\n"
        '    try:\n'
        '        type(ctx).stereo = False\n'
        '    except Exception:\n'
        '        ctx.stereo = False\n'
        'cam = session.main_view.camera\n'
        "if cam.name != 'orthographic':\n"
        "    run(session, 'camera ortho')\n"
        '    cam = session.main_view.camera\n'
        'cam.field_width = float(sys.argv[1])\n')

    lines = [f'open {mrc_path}', 'lighting soft', 'lighting shadows false',
             'graphics silhouettes true width 1.2', f'set bgColor {bg}',
             'volume #1 style surface', 'color #1 #b9b9d0']
    views_meta = []
    for li, lv in enumerate(levels):
        f = out_dir / f'L{li}_front.png'; s = out_dir / f'L{li}_side.png'; t = out_dir / f'L{li}_top.png'
        cap = f'width {size} height {size} supersample 3'
        # re-frame (`view`) after every turn: without it the camera keeps the FRONT
        # framing, so an off-centre or elongated map gets clipped in side/top and reads
        # as "fragmented" when it is only badly framed.
        fix = f'runscript {setfw} {fw:.4f}'      # re-assert ortho + field width per view
        lines += [f'volume #1 level {lv}', 'view orient', 'view', fix, f'save {f} {cap}',
                  'turn y 90', 'view', fix, f'save {s} {cap}',
                  'turn y -90', 'turn x 90', 'view', fix, f'save {t} {cap}', 'turn x -90']
        tag = f'{lv:g}'
        if target_volume_A3 is not None:
            tv = float(np.atleast_1d(target_volume_A3)[li])
            tag = f'{lv:g}  ({tv/1000:.0f}k A^3'
            tag += f' = {volume_ladder[li]:g}x mol.vol)' if li < len(volume_ladder) else ')'
        for v, p in (('front', f), ('side', s), ('top', t)):
            views_meta.append({'level': lv, 'view': v, 'png': str(p), 'tag': tag})
    lines.append('exit')
    cxc = out_dir / 'render.cxc'
    cxc.write_text('\n'.join(lines) + '\n')

    import shlex
    import subprocess
    cfg = surface_config(datainfo_path)
    # explicit args beat the environment: VIS_RES_RENDER_* usually live in ~/.bashrc, which a
    # NON-INTERACTIVE shell never sources, so a script that relies on them silently loses the
    # render host and dies with "OpenGL rendering is not available".
    if render_host:
        cfg['host'] = render_host
    if render_ld_path:
        cfg['ld_path'] = render_ld_path
    exe = chimerax or cfg['chimerax']
    if not exe:
        raise RuntimeError(
            'No ChimeraX path. Set vis_res_utils.DATAINFO_PATH to the project yaml, or pass '
            'chimerax=..., or add `model_mask_chimerax:` to base_datainfo.yaml.')

    # `--cmd "open x.cxc"` -- NOT `--script x.cxc`: --script makes ChimeraX parse the
    # .cxc as PYTHON and it dies with a SyntaxError on the first ChimeraX command.
    inner = f'{shlex.quote(exe)} --offscreen --nogui --exit --cmd {shlex.quote(f"open {cxc}")}'
    if cfg['ld_path']:
        inner = f'export LD_LIBRARY_PATH={shlex.quote(cfg["ld_path"])}:$LD_LIBRARY_PATH; ' + inner
    if cfg['host']:
        # The render host must SEE the script and be able to write the PNGs, so out_dir
        # has to be on shared storage. A local scratch dir (/tmp/...) silently fails with
        # a bare "No such file/path" from ChimeraX -- check up front instead.
        probe = subprocess.run(
            ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=no', cfg['host'],
             f'test -f {shlex.quote(str(cxc))}'], capture_output=True, text=True, timeout=60)
        if probe.returncode != 0:
            raise RuntimeError(
                f'out_dir is not visible on render host {cfg["host"]}: {out_dir}\n'
                'When vis_res_render_host is set, out_dir MUST be on shared storage '
                '(e.g. inside the project directory), not local scratch like /tmp.')
    cmd = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=no',
           cfg['host'], inner] if cfg['host'] else ['bash', '-lc', inner]
    proc = subprocess.run(cmd, timeout=timeout_s, capture_output=True, text=True)

    # montage: rows = level, cols = front/side/top.
    rendered = [v for v in views_meta if Path(v['png']).exists()]
    if not rendered:
        tail = ((proc.stdout or '') + (proc.stderr or ''))[-900:]
        hint = ''
        if 'OpenGL rendering is not available' in tail or 'Offscreen rendering is not available' in tail:
            hint = (
                '\nHINT: ChimeraX ran but cannot RENDER on this host -- its bundled OSMesa needs a '
                'mesa LLVM library that is missing here. This is a SITE issue, not a code one: point '
                '`vis_res_render_host` at a node that has the library (and `vis_res_render_ld_path` '
                'at a dir holding a correctly-NAMED symlink to it) in base_datainfo.yaml. '
                'Compute-only ChimeraX still works everywhere. Otherwise fall back to '
                'compare_volumes(), which needs no OpenGL.')
        raise RuntimeError(
            f'ChimeraX produced no images (exe={exe}, host={cfg["host"]}, cxc={cxc}).{hint}\n'
            f'--- last output ---\n{tail}')
    # true screen-space scale bar: A/pixel is fixed by the orthographic field width
    bar_px = int(round(scalebar_A * px_per_A))
    for v in rendered:
        im = Image.open(v['png']).convert('RGB')
        dr0 = ImageDraw.Draw(im)
        fs0 = max(14, im.width // 48)
        bh = max(5, fs0 // 3)
        x0, y0 = int(im.width * 0.05), int(im.height * 0.94)
        if 0 < bar_px < im.width * 0.85:
            dr0.rectangle([x0 - 8, y0 - fs0 - 12, x0 + bar_px + 8, y0 + bh + 6],
                          fill=(255, 255, 255))          # keep the bar legible over density
            dr0.rectangle([x0, y0, x0 + bar_px, y0 + bh], fill=(20, 20, 20))
            dr0.text((x0, y0 - fs0 - 8), f'{scalebar_A:g} A', fill=(20, 20, 20), font=_font(fs0))
        im.save(v['png'])

    w, h = Image.open(rendered[0]['png']).size
    font = _font(max(14, w // 36))
    sheet = Image.new('RGB', (3 * w, len(levels) * h), (255, 255, 255))
    dr = ImageDraw.Draw(sheet)
    for v in rendered:
        li = levels.index(v['level']); ci = ('front', 'side', 'top').index(v['view'])
        im = Image.open(v['png']).convert('RGB')
        sheet.paste(im, (ci * w, li * h))
        dr.text((ci * w + 8, li * h + 6),
                f"level={v.get('tag', v['level'])}  {v['view']}", fill=(0, 0, 0), font=font)
    surf = out_dir / 'surface_sheet.png'
    sheet.save(surf)

    return {'mrc': str(mrc_path), 'surface_sheet': str(surf), 'views': views_meta,
            'levels': levels, 'out_dir': str(out_dir),
            'scalebar_A': scalebar_A, 'box_A': round(_box_A, 1),
            'field_width_A': round(fw, 2), 'px_per_A': round(px_per_A, 3)}


# ----------------------------------------------------------------------------------
# compare_volumes -- QUALIFY several volumes against EACH OTHER (ab-initio classes,
# competing refinements). Four things render_map_slices alone gets wrong for that job:
#
#  1. SHARED intensity scaling. Per-panel percentile normalisation makes every map
#     look equally contrasty, so the class with more particles looks better purely
#     because its own normalisation stretched further. Maps are put on ONE window in
#     sigma-above-solvent, so contrast differences are real.
#  2. NATIVE single slices by DEFAULT (slab_vox=1): no averaging, no binning, so the
#     slice row is readable for RESOLUTION on the same terms as render_map_slices().
#     slab_vox=3 is available for ranking very noisy volumes -- it buys SNR at the cost
#     of a ~3-voxel low-pass, so a slab CANNOT confirm a resolution finer than itself.
#  3. PROJECTIONS as a second row -- the envelope / "is this even a real particle"
#     read. A class that looks like noise in slices can be a perfectly bounded
#     particle in projection.
#  4. COUNT-ROBUST METRICS so ranking is not pure eyeball. Contrast in sigma units
#     RISES with particle count, so it cannot rank classes fairly. Instead fix the
#     ENCLOSED VOLUME (top-N voxels) and measure the SPATIAL COHERENCE of that fixed
#     volume: a well-aligned map puts it in one compact connected blob, a poorly
#     aligned one scatters it into fragments.
#
# WINDOWS ARE PER VIEW TYPE, not global: a projection averages ~box slices, so its
# noise falls by ~sqrt(box) while its signal mean does not. Reusing the slice window
# (or scaling it by a fudge factor) saturates projections to solid white and destroys
# exactly the differences you are trying to read.
#
# NOTE none of this needs OpenGL -- use it when render_map_surface() cannot run.
# ----------------------------------------------------------------------------------

def _solvent_z(a: np.ndarray, r_frac: float = 0.85) -> np.ndarray:
    """Normalise by the mean/std of the OUTER shell (solvent) of a 2D or 3D array."""
    idx = np.ogrid[tuple(slice(0, n) for n in a.shape)]
    c = [(n - 1) / 2.0 for n in a.shape]
    r2 = sum((i - ci) ** 2 for i, ci in zip(idx, c))
    r = np.sqrt(r2)
    solvent = a[r > r_frac * (min(a.shape) / 2.0)]
    if solvent.size == 0:
        solvent = a
    sd = float(solvent.std()) or 1e-6
    return (a - float(solvent.mean())) / sd


def volume_metrics(vol: np.ndarray, voxel_A: float,
                   occupancy_frac: float = 0.03,
                   min_piece_A3: float = 600.0) -> dict:
    """
    Count-robust quality metrics at a FIXED enclosed volume (top `occupancy_frac` of
    the box). Returns n_frag / frac_in_largest / fill_ratio / Rg_A (all count-robust)
    plus mean_sigma / peak_sigma (which RISE with particle count -- reference only,
    do NOT rank on them).
    """
    from scipy import ndimage
    z = _solvent_z(vol)
    vox = voxel_A ** 3
    k = max(int(occupancy_frac * z.size), 1)
    thr = float(np.partition(z.ravel(), z.size - k)[z.size - k])
    mask = z >= thr
    lab, n_lab = ndimage.label(mask)
    if n_lab == 0:
        return {'n_frag': 0, 'frac_in_largest': 0.0, 'fill_ratio': 0.0, 'Rg_A': 0.0,
                'thr_sigma': thr, 'mean_sigma': 0.0, 'peak_sigma': float(z.max())}
    sizes = np.bincount(lab.ravel())[1:]
    big = int(sizes.max())
    largest = lab == (1 + int(np.argmax(sizes)))
    idx = np.argwhere(largest).astype(float)
    bb = idx.max(axis=0) - idx.min(axis=0) + 1
    com = idx.mean(axis=0)
    return {
        'n_frag': int((sizes * vox > min_piece_A3).sum()),
        'frac_in_largest': big / float(k),
        'fill_ratio': big / float(np.prod(bb)),
        'Rg_A': float(np.sqrt(((idx - com) ** 2).sum(axis=1).mean())) * voxel_A,
        'thr_sigma': thr,
        'mean_sigma': float(z[largest].mean()),
        'peak_sigma': float(z.max()),
    }


def compare_volumes(mrc_paths, out_dir: str | Path,
                    labels: Optional[list] = None,
                    slab_vox: int = 1,
                    projections: bool = True,
                    scale: int = 4,
                    scalebar_A: float = 10.0,
                    occupancy_frac: float = 0.03) -> dict:
    """
    Render several maps on ONE shared intensity scale (per view type) as central
    SLICES + PROJECTIONS, and compute count-robust metrics for each.

        r = compare_volumes([v0, v1, v2, v3], out_dir='cmp', labels=['c0','c1','c2','c3'])
        # read r['sheets'][i] with the Read tool; rank on r['metrics'][i]

    `slab_vox=1` (the DEFAULT) means a single NATIVE voxel plane -- no averaging, no
    binning -- so the top row can be read for resolution on the same terms as
    render_map_slices(). Set slab_vox=3 ONLY when you are ranking noisy low-count
    volumes and want the SNR: a 3-voxel slab is a ~3-voxel low-pass, which biases the
    resolution read soft (it cannot confirm a resolution finer than the slab it
    averaged over). The projection row is a full-axis mean either way: envelope and
    anisotropy only, never Angstrom.

    Returns {sheets:[png...], metrics:[{...}], windows:{...}, out_dir}.
    """
    from scipy import ndimage
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    paths = [Path(p) for p in mrc_paths]
    labels = list(labels) if labels else [p.stem for p in paths]

    vols = []
    for p in paths:
        with mrcfile.open(str(p), permissive=True) as m:
            vols.append({'z': _solvent_z(m.data.astype(np.float32)),
                         'vx': _voxel_size_A(m), 'path': p})

    # build every 2D view first, each normalised by ITS OWN solvent sigma
    h = max(slab_vox // 2, 0)
    for v in vols:
        z = v['z']; n = z.shape[0]
        hot = z > 3.0
        com = ndimage.center_of_mass(hot) if hot.any() else (n / 2,) * 3
        ctr = [int(round(c)) for c in com]
        views = {'slab': [], 'proj': []}
        for axis, ax in ((0, 'Z'), (1, 'Y'), (2, 'X')):
            i = int(np.clip(ctr[axis], h, z.shape[axis] - 1 - h))
            sl = [slice(None)] * 3
            sl[axis] = slice(i - h, i + h + 1) if slab_vox > 1 else i
            arr = z[tuple(sl)]
            if slab_vox > 1:
                arr = arr.mean(axis=axis)
            tag = (f'{ax} SLICE native (no bin/avg)' if slab_vox <= 1 else
                   f'{ax} slab {slab_vox}vox ({slab_vox*v["vx"]:.1f}A avg - reads SOFT)')
            views['slab'].append((tag, _solvent_z(arr)))
            if projections:
                views['proj'].append((f'{ax} PROJECTION envelope only', _solvent_z(z.mean(axis=axis))))
        v['views'] = views

    rows = ['slab'] + (['proj'] if projections else [])
    windows = {r: max(float(np.percentile(im, 99.0)) for v in vols for _, im in v['views'][r])
               for r in rows}

    sheets, metrics = [], []
    for v, lab in zip(vols, labels):
        panels = []
        for r in rows:
            vmax = windows[r]
            for name, im in v['views'][r]:
                a = np.clip((im + 1.0) / (vmax + 1.0), 0, 1)
                pim = Image.fromarray((a * 255).astype(np.uint8), 'L').convert('RGB')
                pim = pim.resize((pim.width * scale, pim.height * scale), Image.NEAREST)
                dr = ImageDraw.Draw(pim)
                f = _font(max(13, pim.width // 46))
                dr.text((8, 6), name, fill=(0, 255, 120), font=f)
                bar = int(round(scalebar_A / v['vx'] * scale))
                if 0 < bar < pim.width - 24:
                    y0 = pim.height - 16
                    dr.rectangle([12, y0, 12 + bar, y0 + 4], fill=(255, 255, 0))
                    dr.text((12, y0 - 20), f'{scalebar_A:g} A', fill=(255, 255, 0), font=f)
                panels.append(pim)
        pw = max(p.width for p in panels); ph = max(p.height for p in panels)
        sheet = Image.new('RGB', (pw * 3, ph * len(rows) + 30), (12, 12, 14))
        for i, p in enumerate(panels):
            sheet.paste(p, ((i % 3) * pw, 30 + (i // 3) * ph))
        ImageDraw.Draw(sheet).text(
            (10, 8), f'{lab}   SHARED window per row  '
                     f'{"slice" if slab_vox <= 1 else f"slab{slab_vox}"}=[-1,{windows["slab"]:.0f}]sigma',
            fill=(255, 255, 255), font=_font(16))
        sp = out / f'{lab}_cmp.png'
        sheet.save(sp); sheets.append(str(sp))
        m = volume_metrics(v['z'], v['vx'], occupancy_frac=occupancy_frac)
        m['label'] = lab; m['mrc'] = str(v['path'])
        metrics.append(m)

    print(f'{"label":14s} {"n_frag":>7s} {"frac_largest":>13s} {"fill":>6s} {"Rg_A":>7s}'
          f'   (count-robust; rank on these)')
    for m in sorted(metrics, key=lambda x: -x['frac_in_largest']):
        print(f'{m["label"]:14s} {m["n_frag"]:7d} {m["frac_in_largest"]:13.3f} '
              f'{m["fill_ratio"]:6.3f} {m["Rg_A"]:7.1f}')
    return {'sheets': sheets, 'metrics': metrics, 'windows': windows, 'out_dir': str(out)}


# NOTE: a hand-rolled half-map FSC used to live here. Removed on purpose: the refine job
# already stores EVERY curve (fsc_nomask / loosemask / tightmask / noisesub) in the same
# gridfs asset -- read them with fsc_resolution_from_job()['res_by_column'] and compare
# nomask vs corrected ('mask_effect_A') to ask whether a number is mask-inflated.

def build_prompt(fsc_res_A: Optional[float] = None, region=None) -> str:
    """
    The instruction the agent follows while looking at the rendered slices.

    `region` (the dict from render_map_slices(..., mask=...)['region'], or a name) states the
    SCOPE up front: for a local refine the slices show the REFINED REGION only, and the
    estimate belongs to that region — not to the whole map.
    """
    fsc_line = (f'\nFSC (corrected) reports {fsc_res_A:.2f} A — judge whether the density '
                f'actually supports that.' if fsc_res_A is not None else '')
    if region is not None:
        rn = region if isinstance(region, str) else region.get('name', 'mask region')
        rv = '' if isinstance(region, str) else \
            f' (region volume {region.get("region_volume_A3", 0):.0f} A^3)'
        region_line = (f'\n⭐ SCOPE: these slices are cropped to the REGION "{rn}"{rv} — the '
                       f'volume covered by the refinement mask. Judge THAT region and report '
                       f'the estimate as belonging to it; it is NOT a whole-map number '
                       f'(outside the mask a local refine leaves unrefined carry-over).')
    else:
        region_line = ''
    return (
        'You are judging the resolution of a 3D cryo-EM map from 6 central slices '
        '(2 per axis, native sampling, 10 A scale bar shown).\n'
        f'{RUBRIC}{fsc_line}{region_line}\n\n'
        'Report: (1) is secondary structure visible anywhere? (2) your resolution '
        'estimate in A with the evidence; (3) if it differs from the FSC by > 1 A, '
        'state whether the FSC looks trustworthy.'
    )


# ----------------------------------------------------------------------------- #
# 3-4. Decision rule                                                             #
# ----------------------------------------------------------------------------- #

# ----------------------------------------------------------------------------- #
# THE PRE-FILTER GUARD — is the map-domain evidence CIRCULAR?                     #
# ----------------------------------------------------------------------------- #
# ⭐ ADDED 2026-09-07. MEASURED (runs/p3_lowpass/s4_trunc.json): a CryoSPARC output volume
# is HARD-TRUNCATED in Fourier space at the job's OWN reported resolution --
#     J238 edge 4.25 vs claim 4.37 · J548 3.67 vs 3.65 · J216 10.02 vs 13.17 ·
#     J2105 2.62 vs 2.70 -- while the HALF-MAPS are unfiltered to Nyquist (1.78 / 2.16 /
#     1.78 / 1.65 A).
# => When the edge COINCIDES with the claim, ANY map-domain resolution evidence from that
# file is CIRCULAR: the software already put the answer into the file, so a "the map
# degrades right about here" reading can only rediscover the number it was meant to audit.
# The flag is a WARNING THAT SUCH EVIDENCE IS UNAVAILABLE and the HALF-MAPS must be used
# instead. It is a guard, not an instrument: one FFT, one comparison, no calibration.
# It NEVER changes a status -- vision still decides; it only labels the evidence.

def radial_power(vol: np.ndarray, voxel_A: float, nbin: Optional[int] = None) -> tuple:
    """Radially averaged power spectrum; returns (res_A, power, count), coarse -> fine."""
    F = np.fft.rfftn(vol)
    P = (F.real ** 2 + F.imag ** 2)
    del F
    kz = np.fft.fftfreq(vol.shape[0], d=voxel_A)
    ky = np.fft.fftfreq(vol.shape[1], d=voxel_A)
    kx = np.fft.rfftfreq(vol.shape[2], d=voxel_A)
    K = np.sqrt(kz[:, None, None] ** 2 + ky[None, :, None] ** 2 + kx[None, None, :] ** 2)
    kmax = 1.0 / (2 * voxel_A)
    nb = nbin or vol.shape[0] // 2
    idx = np.clip((K / kmax * nb).astype(np.int32), 0, nb - 1)
    s = np.bincount(idx.ravel(), weights=P.ravel(), minlength=nb)
    c = np.bincount(idx.ravel(), minlength=nb).astype(np.float64)
    p = s / np.maximum(c, 1)
    kc = (np.arange(nb) + 0.5) / nb * kmax
    with np.errstate(divide='ignore'):
        res = 1.0 / kc
    return res, p, c


def fourier_edge_A(vol_or_path, voxel_A: Optional[float] = None,
                   floor_frac: float = 1e-8,
                   max_voxels: int = 64_000_000) -> Optional[float]:
    """
    The map's TRUNCATION EDGE: the FINEST resolution whose radially averaged power still
    exceeds `floor_frac` of the spectral peak. For a CryoSPARC output volume this is the
    low-pass the job applied to itself. Returns Angstrom, or None if it cannot be measured
    (box too large for a cheap FFT — this stays a guard, not an instrument).
    """
    if isinstance(vol_or_path, np.ndarray):
        vol = vol_or_path.astype(np.float32)
        if voxel_A is None:
            raise ValueError('voxel_A is required when passing an array')
    else:
        with mrcfile.open(str(vol_or_path), permissive=True) as m:
            vol = m.data.astype(np.float32).copy()
            voxel_A = _voxel_size_A(m) if voxel_A is None else voxel_A
    if vol.size > max_voxels:
        print(f'[fourier_edge_A] box {vol.shape} over the {max_voxels} voxel budget — '
              f'skipping the pre-filter guard (it is a cheap guard, not an instrument)')
        return None
    res, p, c = radial_power(vol, float(voxel_A))
    m = np.isfinite(res) & (res > 0) & (c > 0)
    res, p = res[m], p[m]
    if p.size == 0 or p.max() <= 0:
        return None
    alive = p > p.max() * floor_frac
    return float(res[alive].min()) if alive.any() else None


def prefilter_check(claim_A: Optional[float],
                    edge_A: Optional[float] = None,
                    vol_or_path=None,
                    voxel_A: Optional[float] = None,
                    tol_frac: float = 0.15) -> dict:
    """
    Does the map's Fourier edge COINCIDE with the claimed resolution?

    Returns {edge_A, claim_A, ratio, tol_frac, band_A, prefiltered(bool), note}.
    `prefiltered` is True when edge/claim lies inside 1 +- tol_frac, i.e. the file carries no
    information past the number it is being audited against -> map-domain resolution evidence
    is CIRCULAR and the half-maps (unfiltered to Nyquist) must be used instead.
    An edge well FINER than the claim (J216: 10.02 vs 13.17) does NOT fire: there the file
    still holds information at scales finer than the claim, so a look at the density is a
    genuine, non-circular test.
    """
    if edge_A is None and vol_or_path is not None:
        edge_A = fourier_edge_A(vol_or_path, voxel_A)
    out = {'edge_A': None if edge_A is None else round(float(edge_A), 3),
           'claim_A': claim_A, 'ratio': None, 'tol_frac': tol_frac,
           'band_A': None, 'prefiltered': False, 'note': ''}
    if edge_A is None or claim_A is None or claim_A <= 0:
        out['note'] = 'not evaluated (no edge measurement or no claimed resolution)'
        return out
    ratio = float(edge_A) / float(claim_A)
    out['ratio'] = round(ratio, 3)
    out['band_A'] = [round(claim_A * (1 - tol_frac), 2), round(claim_A * (1 + tol_frac), 2)]
    out['prefiltered'] = bool(abs(ratio - 1.0) <= tol_frac)
    out['note'] = (
        (f'The map file is band-limited at {edge_A:.2f} A, which COINCIDES with the claimed '
         f'{claim_A:.2f} A (ratio {ratio:.3f}, within +-{tol_frac:.0%}). Any map-domain '
         f'resolution evidence from this file is CIRCULAR — the reconstruction software '
         f'already low-passed the volume at its own answer. Use the HALF-MAPS (unfiltered to '
         f'Nyquist) for any resolution measurement; the density read here can still say '
         f'whether the map is INTERPRETABLE, and whether it looks WORSE than the claim.')
        if out['prefiltered'] else
        (f'Map band-limited at {edge_A:.2f} A vs claim {claim_A:.2f} A (ratio {ratio:.3f}) — '
         f'outside the +-{tol_frac:.0%} coincidence band, so the file still carries '
         f'information away from the claimed number and the density read is not circular.'))
    return out


def gap_threshold_A(fsc_res_A: float,
                    boundary_A: float = 4.0,
                    fine_A: float = 1.0,
                    coarse_A: float = 2.0) -> float:
    """
    Two-tier tolerance for the vision-vs-FSC gap, keyed on the FSC value:
      FSC < 4 A  -> 1 A  (at high resolution a 1 A overclaim is already suspicious)
      FSC >= 4 A -> 2 A  (at low resolution the eyeball estimate is coarser, so allow more)
    """
    return fine_A if fsc_res_A < boundary_A else coarse_A


def assess(secondary_structure: bool,
           vision_estimate_A: Optional[float],
           fsc_res_A: Optional[float],
           fsc_trustworthy: Optional[bool] = None,
           delta_threshold_A: Optional[float] = None,
           notes: str = '',
           surface_checked: bool = False,
           surface_unavailable: bool = False,
           px_per_A: Optional[float] = None,
           px_per_A_floor: float = PX_PER_A_FLOOR,
           region=None,
           locally_refined: bool = False,
           prefilter_edge_A: Optional[float] = None,
           mrc_path=None,
           prefilter_tol_frac: float = 0.15) -> dict:
    """
    Combine the vision judgement with the FSC number into a verdict.

    ⭐ ADDED 2026-09-07 — SCOPE and the PRE-FILTER GUARD. Both are ANNOTATIONS: they never
    change the status, they label what the verdict is ABOUT and whether its evidence is
    usable. With all four new arguments left at their defaults the returned dict is exactly
    what it was before (the no-mask safety property).

      region        : the region this estimate belongs to — the dict from
                      render_map_slices(...)['region'], or a plain name string. Supplying it
                      makes the verdict REGION-SCOPED: status gains the flag REGION_SCOPED,
                      the recommendation is prefixed with the region's identity/volume/
                      coverage, and 'scope'=='region'. Use it for every local refine, where a
                      single whole-map number is a category error (only the masked region was
                      refined; outside it is unrefined carry-over).
      locally_refined : True with NO region supplied -> flag LOCAL_REFINE_REGION_ONLY, so a
                      caller cannot mistake a whole-map number for a statement about the
                      whole map.
      prefilter_edge_A / mrc_path / prefilter_tol_frac : the pre-filter guard. Pass a measured
                      Fourier edge, or a map path to measure one (fourier_edge_A). If the edge
                      COINCIDES with fsc_res_A the verdict gains MAP_PREFILTERED_AT_CLAIM: the
                      map-domain evidence is circular and the half-maps must be used.

    Args
      secondary_structure : did the vision call SEE helices/sheets/backbone anywhere?
      vision_estimate_A   : eyeballed resolution (A); may be None if pure FAILURE.
      fsc_res_A           : corrected gold-standard FSC resolution from the refine job.
      fsc_trustworthy     : the agent's call AFTER the surface second look, when the two
                            estimates disagree by more than the tolerance. Left None with a
                            gap over tolerance, assess() does NOT guess: it returns
                            status='NEEDS_SURFACE_CHECK' / next_action='render_map_surface'.
      surface_checked     : set True when you have read a surface_sheet; then an explicit
                            fsc_trustworthy is expected (None falls back to the heuristic and
                            is flagged HEURISTIC_ONLY).
      surface_unavailable : set True when no surface can be rendered here (e.g. ChimeraX
                            missing). The heuristic decides and the verdict carries the
                            SURFACE_UNAVAILABLE flag so the record stays honest.
      delta_threshold_A   : gap tolerance (A). Default None -> two-tier `gap_threshold_A`
                            (1 A if FSC < 4 A, else 2 A). Pass a number to force a fixed one.
      notes               : free-text evidence to carry into the record.

    Returns dict: {status, flags[], delta_A, vision_estimate_A, fsc_res_A,
                   interpretable, next_action, recommendation, notes}

    status   : 'FAILURE' | 'INTERPRETABLE' | 'NEEDS_SURFACE_CHECK' | 'SUSPICIOUS' |
               'CONSISTENT' | 'FSC_CONSERVATIVE'
    flags    : e.g. ['NOT_INTERPRETABLE'], ['SECOND_LOOK_REQUIRED'],
               ['SUSPICIOUS_RESOLUTION', 'SURFACE_UNAVAILABLE']

    When `fsc_res_A is None` (e.g. ab-initio / any job that reports no gold-standard FSC),
    there is nothing to cross-check, so the verdict is the INTERPRETABILITY gate ONLY:
    secondary structure present -> 'INTERPRETABLE' (meaningful map, carry it forward);
    absent -> 'FAILURE' (junk). No resolution number is judged in this case.
    """
    d = _assess_core(secondary_structure, vision_estimate_A, fsc_res_A,
                     fsc_trustworthy=fsc_trustworthy,
                     delta_threshold_A=delta_threshold_A, notes=notes,
                     surface_checked=surface_checked,
                     surface_unavailable=surface_unavailable,
                     px_per_A=px_per_A, px_per_A_floor=px_per_A_floor)

    # ---- SCOPE. What is this verdict ABOUT? -------------------------------------- #
    if region is not None:
        reg = {'name': region} if isinstance(region, str) else dict(region)
        d['flags'] = list(d['flags']) + ['REGION_SCOPED']
        d['scope'] = 'region'
        d['region'] = reg
        vol_txt = ('' if reg.get('region_volume_A3') is None else
                   f', region volume {reg["region_volume_A3"]:.0f} A^3')
        cov_txt = ('' if reg.get('crop_frac_covered_by_mask') is None else
                   f', {reg["crop_frac_covered_by_mask"]*100:.1f}% of the judged crop covered '
                   f'by mask>{reg.get("mask_threshold", 0.5):g}')
        if reg.get('region_frac_in_crop') is not None and reg['region_frac_in_crop'] < 0.90:
            d['flags'] = list(d['flags']) + ['REGION_PARTIALLY_SHOWN']
            cov_txt += (f'; ONLY {reg["region_frac_in_crop"]*100:.1f}% OF THE REGION IS IN THE '
                        f'RENDERED CROP (it did not fit the px/A budget), so the estimate '
                        f'describes that part of the region')
        d['recommendation'] = (
            f'REGION "{reg.get("name", "mask region")}" ONLY{vol_txt}{cov_txt}. '
            f'This estimate applies to that region and NOT to the whole map — in a local '
            f'refine only the masked region is refined; everything outside is unrefined '
            f'carry-over, so a single whole-map number would be a category error. '
            + d['recommendation'])
    elif locally_refined:
        d['flags'] = list(d['flags']) + ['LOCAL_REFINE_REGION_ONLY']
        d['scope'] = 'whole_map_but_locally_refined'
        d['recommendation'] = (
            'LOCAL_REFINE_REGION_ONLY — a WHOLE-MAP number was requested for a map produced '
            'by a LOCAL refine, where only the masked region was refined (outside it is '
            'unrefined carry-over) and the FSC is a statement about that region. Re-run '
            'render_map_slices(..., mask=<the refinement mask>) and re-assess with '
            'region=<result["region"]> to get a like-for-like, region-scoped estimate. '
            + d['recommendation'])

    # ---- THE PRE-FILTER GUARD. Is the map-domain evidence usable at all? ---------- #
    if prefilter_edge_A is not None or mrc_path is not None:
        pf = prefilter_check(fsc_res_A, edge_A=prefilter_edge_A, vol_or_path=mrc_path,
                             tol_frac=prefilter_tol_frac)
        d['prefilter'] = pf
        if pf['prefiltered']:
            d['flags'] = list(d['flags']) + ['MAP_PREFILTERED_AT_CLAIM']
            d['recommendation'] = d['recommendation'] + ' ⚠ MAP_PREFILTERED_AT_CLAIM: ' + pf['note']
    return d


def _assess_core(secondary_structure: bool,
                 vision_estimate_A: Optional[float],
                 fsc_res_A: Optional[float],
                 fsc_trustworthy: Optional[bool] = None,
                 delta_threshold_A: Optional[float] = None,
                 notes: str = '',
                 surface_checked: bool = False,
                 surface_unavailable: bool = False,
                 px_per_A: Optional[float] = None,
                 px_per_A_floor: float = PX_PER_A_FLOOR) -> dict:
    """The verdict logic exactly as it stood on 2026-09-05 — see assess() for the docs.
    Kept as a separate function so the 2026-09-07 SCOPE / PRE-FILTER annotations are pure
    additions layered on top and cannot perturb the decision rule itself."""
    flags: list[str] = []

    # 0. RENDER SCALE — A WARNING, NOT A GATE (changed 2026-09-25, human directive:
    #    "去掉这个规则,截图达到重构的 pixel/A 就够了").
    #    Rendering beyond the map's OWN sampling (1/voxel_A px/A) adds magnification, not
    #    information, so a render that reaches it is sufficient in principle. `px_per_A_floor`
    #    is kept only as a READABILITY target: on too few pixels the eye confuses a side-chain
    #    knob with a bump (measured: J1078, a real 3.0 A map, read as ~5 A three times; J548
    #    3.65 A read as ~5.5 A; AT1R J2105 ~4 A read as ~8.5 A). So flag it and carry on —
    #    the verdict is no longer refused.
    if px_per_A is not None and px_per_A < px_per_A_floor - 1e-9 \
            and secondary_structure and vision_estimate_A is not None:
        flags.append('PX_PER_A_BELOW_TARGET')

    # 3. interpretability gate — no secondary structure => failed reconstruction.
    #    (this is the WHOLE judgement when there is no FSC to compare against.)
    if not secondary_structure:
        flags.append('NOT_INTERPRETABLE')
        meaningless = ('the FSC resolution is meaningless; do not report it'
                       if fsc_res_A is not None else 'the map is junk')
        return {
            'status': 'FAILURE',
            'flags': flags,
            'next_action': None,
            'delta_A': None,
            'vision_estimate_A': vision_estimate_A,
            'fsc_res_A': fsc_res_A,
            'interpretable': False,
            'recommendation': ('No secondary structure in any slice — the map is not '
                               f'interpretable as protein/nucleic acid. Treat this '
                               f'reconstruction as FAILED ({meaningless}).'),
            'notes': notes,
        }

    # No FSC to compare against (ab-initio etc.): interpretability is the only verdict.
    if fsc_res_A is None:
        return {
            'status': 'INTERPRETABLE',
            'flags': flags,
            'next_action': None,
            'delta_A': None,
            'vision_estimate_A': vision_estimate_A,
            'fsc_res_A': None,
            'interpretable': True,
            'recommendation': ('Secondary structure visible — the map is a meaningful '
                               'protein/nucleic-acid density; carry it forward. No FSC was '
                               'reported, so no resolution number is validated here '
                               '(rely on the downstream refine\'s FSC, cross-checked then).'),
            'notes': notes,
        }

    if vision_estimate_A is None:
        return {
            'status': 'INTERPRETABLE',
            'flags': flags,
            'next_action': None,
            'delta_A': None,
            'vision_estimate_A': None,
            'fsc_res_A': fsc_res_A,
            'interpretable': True,
            'recommendation': ('Secondary structure visible (interpretable) but no visual '
                               'resolution estimate given; report the FSC value as-is.'),
            'notes': notes,
        }

    delta = vision_estimate_A - fsc_res_A  # >0 => vision sees WORSE than FSC claims

    # two-tier tolerance keyed on the FSC value, unless a fixed override is given.
    if delta_threshold_A is None:
        delta_threshold_A = gap_threshold_A(fsc_res_A)

    # 4. within tolerance -> consistent.
    if abs(delta) <= delta_threshold_A:
        return {
            'status': 'CONSISTENT',
            'flags': flags,
            'next_action': None,
            'delta_A': round(delta, 2),
            'vision_estimate_A': vision_estimate_A,
            'fsc_res_A': fsc_res_A,
            'interpretable': True,
            'recommendation': (f'Vision (~{vision_estimate_A:.1f} A) agrees with FSC '
                               f'({fsc_res_A:.2f} A) within {delta_threshold_A:g} A.'),
            'notes': notes,
        }

    # gap > threshold -> SECOND LOOK on the iso-surface, THEN the agent decides.
    # Flat slices systematically UNDER-read a 3-4 A map (a helix is a bland band in a
    # slice but an unmistakable rod on a surface), so the commonest cause of a gap is
    # the slice read itself. Guessing here manufactures false SUSPICIOUS verdicts.
    if fsc_trustworthy is None and not surface_checked and not surface_unavailable:
        flags.append('SECOND_LOOK_REQUIRED')
        return {
            'status': 'NEEDS_SURFACE_CHECK',
            'flags': flags,
            'delta_A': round(delta, 2),
            'vision_estimate_A': vision_estimate_A,
            'fsc_res_A': fsc_res_A,
            'interpretable': True,
            'next_action': 'render_map_surface',
            'recommendation': (
                f'Vision (~{vision_estimate_A:.1f} A) and FSC ({fsc_res_A:.2f} A) differ by '
                f'{delta:+.1f} A, more than the {delta_threshold_A:g} A tolerance — NOT decided yet. '
                f'Run render_map_surface(mrc) (iso-surface level sweep, 3 views, unsharpened map), '
                f'read surface_sheet, then call assess(..., surface_checked=True, '
                f'fsc_trustworthy=True/False). If the surface cannot be rendered here, re-call with '
                f'surface_unavailable=True to fall back to the heuristic — the verdict is then '
                f'flagged as unconfirmed.'),
            'notes': notes,
        }

    if fsc_trustworthy is None:
        # No explicit call after the look (or no surface available): heuristic fallback.
        # FSC claiming MORE resolution than the density shows is the dangerous case.
        fsc_trustworthy = delta <= 0
        flags.append('SURFACE_UNAVAILABLE' if surface_unavailable else 'HEURISTIC_ONLY')

    if not fsc_trustworthy:
        flags.append('SUSPICIOUS_RESOLUTION')
        return {
            'status': 'SUSPICIOUS',
            'flags': flags,
            'next_action': None,
            'delta_A': round(delta, 2),
            'vision_estimate_A': vision_estimate_A,
            'fsc_res_A': fsc_res_A,
            'interpretable': True,
            'recommendation': (f'FSC reports {fsc_res_A:.2f} A but the density only '
                               f'supports ~{vision_estimate_A:.1f} A — likely a FALSE '
                               f'high resolution (mask/overfitting/alignment artefact). '
                               f'Flag SUSPICIOUS_RESOLUTION; trust the visual estimate '
                               f'and investigate the refinement (mask, FSC curve).'),
            'notes': notes,
        }

    # large gap but FSC judged sound (e.g. FSC is the conservative lower-res number).
    return {
        'status': 'FSC_CONSERVATIVE',
        'flags': flags,
        'next_action': None,
        'delta_A': round(delta, 2),
        'vision_estimate_A': vision_estimate_A,
        'fsc_res_A': fsc_res_A,
        'interpretable': True,
        'recommendation': (f'Vision and FSC differ by {delta:+.1f} A but the FSC was '
                           f'judged sound on re-examination; report the FSC value and '
                           f'note the visual discrepancy.'),
        'notes': notes,
    }


# ----------------------------------------------------------------------------- #
# Where the FSC record lives: pull the corrected resolution from a refine job     #
# ----------------------------------------------------------------------------- #
# The job DOC does NOT carry the resolution. CryoSPARC stores the gold-standard FSC
# curve as gridfs assets named `J###_fsc_iteration_NNN.txt` (and, when the FSC mask
# is auto-tightened, `..._after_fsc_mask_auto_tightening.txt`). Each is a table:
#   wave_number  fsc_nomask  fsc_loosemask  fsc_tightmask  fsc_noisesub_raw  fsc_noisesub_true  fsc_noisesub
# The CORRECTED (phase-randomisation / noise-substituted) curve is `fsc_noisesub`.
# Resolution = (box_px * voxel_A) / wave_number at the FSC=0.143 crossing.

_FSC_CORRECTED_PREF = ('fsc_noisesub', 'fsc_corrected', 'fsc_tightmask',
                       'fsc_loosemask', 'fsc_nomask')


def _box_voxel_from_job(proj, job_uid: str) -> tuple[int, float]:
    """Box (px) and voxel (A) from any volume map .mrc the job wrote on disk."""
    jdir = Path(proj.dir()) / job_uid
    cand = sorted(jdir.glob('*_volume_map.mrc')) or sorted(jdir.glob('*volume*.mrc'))
    if not cand:
        raise FileNotFoundError(f'no volume map .mrc in {jdir}')
    with mrcfile.open(str(cand[0]), permissive=True) as m:
        return int(m.data.shape[0]), _voxel_size_A(m)


def _wn_at_level(wave_number: np.ndarray, fsc: np.ndarray,
                 level: float = 0.143) -> Optional[float]:
    """Linear-interpolated WAVE NUMBER of the first FSC down-crossing of `level`."""
    below = np.where(fsc < level)[0]
    below = below[below > 0]
    if below.size == 0:
        return None
    i = int(below[0])
    x0, x1, y0, y1 = wave_number[i - 1], wave_number[i], fsc[i - 1], fsc[i]
    if y1 == y0:
        return None
    wc = x0 + (level - y0) * (x1 - x0) / (y1 - y0)
    return float(wc) if wc > 0 else None


def _res_at_level(wave_number: np.ndarray, fsc: np.ndarray, box_A: float,
                  level: float = 0.143) -> Optional[float]:
    """Linear-interpolated resolution (A) at the first FSC down-crossing of `level`."""
    wc = _wn_at_level(wave_number, fsc, level)
    return float(box_A / wc) if wc else None


def _res_at_0143(wave_number: np.ndarray, fsc: np.ndarray, box_A: float) -> Optional[float]:
    """Linear-interpolated resolution (A) at the first FSC=0.143 down-crossing."""
    return _res_at_level(wave_number, fsc, box_A, 0.143)


def fsc_resolution_from_job(proj, job_uid: str) -> Optional[dict]:
    """
    Read the corrected gold-standard FSC resolution (A) for a refine job, from its
    gridfs FSC `.txt` asset (the resolution is NOT in the job doc). Picks the final
    iteration, prefers the auto-tightened curve, and reports the corrected
    (`fsc_noisesub`) 0.143 crossing.

    Returns {res_A, column, asset, iteration, box, voxel_A, res_by_column, mask_effect_A}
    or None (never raises) — if None, read the FSC from the job's streamlog/plot and pass
    `fsc_res_A` by hand.

    `res_by_column` gives the 0.143 crossing of EVERY curve the job stored — `fsc_nomask`,
    `fsc_loosemask`, `fsc_tightmask`, `fsc_noisesub` — and `mask_effect_A` is
    nomask - corrected, i.e. how much the mask bought. Use these to ask whether a number is
    mask-inflated; do NOT recompute an FSC from the half-maps by hand (the job's curves are
    the reference, and a hand-rolled mask-free FSC is biased by solvent and by design
    against local refines).
    """
    try:
        job = proj.find_job(job_uid)
        assets = job.list_assets()
        txts = [a for a in assets
                if a.get('filename', '').endswith('.txt') and 'fsc' in a['filename'].lower()]
        if not txts:
            return None

        def rank(a):
            fn = a['filename']
            it = re.search(r'iteration_(\d+)', fn)
            return (int(it.group(1)) if it else -1,
                    'after_fsc_mask_auto_tightening' in fn)  # iteration first, then auto-tight
        best = max(txts, key=rank)

        import io
        buf = io.BytesIO()
        job.download_asset(best['_id'], buf)
        buf.seek(0)
        lines = [l for l in buf.read().decode('latin-1').splitlines() if l.strip()]
        hdr = lines[0].split()
        rows = []
        for l in lines[1:]:
            try:
                rows.append([float(x) for x in l.split()])
            except ValueError:
                continue
        rows = np.array(rows)
        wn = rows[:, 0]

        col, colname = None, None
        for name in _FSC_CORRECTED_PREF:
            if name in hdr:
                c = hdr.index(name)
                if np.isfinite(rows[:, c]).any():
                    col, colname = c, name
                    break
        if col is None:
            col, colname = rows.shape[1] - 1, (hdr[-1] if hdr else '?')

        box, vox = _box_voxel_from_job(proj, job_uid)
        res = _res_at_0143(wn, rows[:, col], box * vox)
        if res is None:
            return None
        # every curve in the SAME file: no-mask / loose / tight / noise-substituted.
        # The job already computed these -- never hand-roll a half-map FSC to second-guess it.
        by_col = {}
        for ci, name in enumerate(hdr):
            if ci == 0 or not np.isfinite(rows[:, ci]).any():
                continue
            r = _res_at_0143(wn, rows[:, ci], box * vox)
            if r is not None:
                by_col[name] = round(r, 2)
        it = re.search(r'iteration_(\d+)', best['filename'])
        return {'res_A': round(res, 2), 'column': colname, 'asset': best['filename'],
                'iteration': int(it.group(1)) if it else None, 'box': box,
                'voxel_A': round(vox, 4), 'res_by_column': by_col,
                'mask_effect_A': (round(by_col['fsc_nomask'] - res, 2)
                                  if 'fsc_nomask' in by_col else None)}
    except Exception as e:
        print(f'[fsc_resolution_from_job] could not read FSC for {job_uid}: {e!r}')
        return None


# --------------------------------------------------------------------------- #
# FSC CURVE HEALTH — the SECONDARY axis of the resolution check                 #
# --------------------------------------------------------------------------- #
# vis_res is the PRIMARY instrument: it asks "does the density look like its
# number?". Curve health is the SECONDARY one: the 0.143 crossing says what the
# map CLAIMS, the SHAPE of the curve says whether the claim is honest and, when
# it is not, WHICH artefact is behind it (over-masking, a shared half-map
# artefact, heterogeneity, an unconverged refine). A scalar mismatch never names
# the mechanism; the curve does.
#
# ⚠ THE ITERATION TRAP (measured 2026-09-03, AT1R — it cost a retracted finding).
# `..._after_fsc_mask_auto_tightening` AND the `fsc_noisesub` column exist ONLY
# at the FINAL iteration, and the rebound amplitude grows strongly with
# iteration depth: one job spanned -0.014 -> +0.173 across its OWN iterations,
# more than the entire between-job spread that was being read as biology. At
# matched depth the ranking REVERSED. So: never compare ANY FSC-derived quantity
# across jobs at unmatched depth — use compare_fsc_curve_health(), which reads
# every arm at the deepest COMMON iteration on a column that exists at every
# iteration (`fsc_nomask`).

# THRESHOLDS — calibrated 2026-09-03 on 18 completed refines across P1/P3/P5/P6
# (nonuniform / homo / local, 3.0-7.3 A), each at its OWN final iteration:
#   rebound (fsc_nomask, restricted to shells above the 0.143 crossing):
#       0.018 0.037 0.041 0.046 0.046 0.052 0.052 0.054 0.076 0.076 0.090 0.094
#       0.098 0.102 0.130 0.131 0.134 0.171 0.173     -> a CONTINUUM, no clean gap
#   tail mean beyond the crossing: |x| <= 0.005 everywhere (0.05 never fires)
#   0.5 -> 0.143 span: 4.3 - 27.6 shells (the 1.5-shell cliff never fires)
# So the rebound cut is a GRADED heuristic, not a discovered boundary — always
# report the number and the iteration, never the verdict alone.
#
# ⛔ MEASURED NEGATIVE — the mask DIVERGENCE POINT does not discriminate.
# Over the same 18 maps the masked-vs-unmasked separation (>0.05) lands anywhere
# from 7.5 to 19.2 A with no relation to map quality: P3 J54/J50 (3.85 A, ordinary)
# diverge at 19.2 A while P1 J67/J60 (3.05 A) diverge at 8.4 A. An earlier version
# of this module flagged EARLY_MASK_DIVERGENCE on a 2x-the-crossing rule and it
# fired on EVERY good map. The divergence point is therefore REPORTED as context
# and NEVER flagged — the same retraction that `mask_effect_A` already carries.
_FSC_HEALTH_THRESH = {
    'rebound_mild':      0.03,   # trough -> later peak, above the 0.143 crossing
    'rebound_strong':    0.10,
    'tail_plateau':      0.05,   # mean FSC beyond 1.15x the crossing wave number
    'mask_diverge':      0.05,   # |masked - nomask| counted as "separated" (reported only)
    'cliff_shells':      1.5,    # 0.5 -> 0.143 inside this many shells
    'wide_gap_x':        1.6,    # res(0.5) / res(0.143)
    'depth_growth_x':    2.0,    # final rebound vs its value at mid-depth
}


def _fsc_assets(proj, job_uid: str) -> tuple:
    """(job, [{filename, iteration, tightened, _id}, ...]) for a job's FSC .txt assets."""
    job = proj.find_job(job_uid)
    out = []
    for a in job.list_assets():
        fn = a.get('filename', '')
        if not (fn.endswith('.txt') and 'fsc' in fn.lower()):
            continue
        m = re.search(r'iteration_(\d+)', fn)
        out.append({'_id': a['_id'], 'filename': fn,
                    'iteration': int(m.group(1)) if m else -1,
                    'tightened': 'after_fsc_mask_auto_tightening' in fn})
    return job, out


def read_fsc_table(proj, job_uid: str, iteration: Optional[int] = None,
                   tightened: Optional[bool] = None) -> Optional[dict]:
    """
    Read one FSC .txt asset of a job as ARRAYS (the crossing values alone cannot
    answer monotonicity / divergence / slope / tail).

    iteration : None -> the deepest iteration present; else that exact iteration.
    tightened : None -> prefer the auto-tightened file when one exists at that
                iteration; True/False -> require/forbid it. Remember the
                tightened file exists ONLY at the final iteration, so a matched-
                depth comparison must pass tightened=False.

    Returns {filename, iteration, tightened, box, voxel_A, box_A, wave_number,
             columns{name: array}, res_A{name: float}} or None (never raises).
    """
    try:
        job, assets = _fsc_assets(proj, job_uid)
        if not assets:
            return None
        if iteration is not None:
            assets = [a for a in assets if a['iteration'] == iteration]
        if tightened is not None:
            assets = [a for a in assets if a['tightened'] is bool(tightened)]
        if not assets:
            return None
        best = max(assets, key=lambda a: (a['iteration'], a['tightened']))

        import io
        buf = io.BytesIO()
        job.download_asset(best['_id'], buf)
        buf.seek(0)
        lines = [l for l in buf.read().decode('latin-1').splitlines() if l.strip()]
        hdr = lines[0].split()
        rows = []
        for l in lines[1:]:
            try:
                rows.append([float(x) for x in l.split()])
            except ValueError:
                continue
        rows = np.array(rows)
        if rows.ndim != 2 or rows.shape[0] < 4:
            return None

        box, vox = _box_voxel_from_job(proj, job_uid)
        box_A = box * vox
        wn = rows[:, 0]
        cols, res = {}, {}
        for ci, name in enumerate(hdr):
            if ci == 0 or ci >= rows.shape[1]:
                continue
            y = rows[:, ci]
            if not np.isfinite(y).any():
                continue                      # fsc_noisesub_true is often all-NaN
            cols[name] = y
            r = _res_at_0143(wn, y, box_A)
            if r is not None:
                res[name] = round(r, 2)
        return {'filename': best['filename'], 'iteration': best['iteration'],
                'tightened': best['tightened'], 'box': box,
                'voxel_A': round(vox, 4), 'box_A': box_A,
                'wave_number': wn, 'columns': cols, 'res_A': res}
    except Exception as e:
        print(f'[read_fsc_table] could not read FSC table for {job_uid}: {e!r}')
        return None


def _rebound(wn_f: np.ndarray, y_f: np.ndarray, box_A: float) -> dict:
    """
    Largest trough -> later peak on an FSC curve, measured ONLY on the shells
    ABOVE the 0.143 crossing. Past the crossing the curve is noise wobbling about
    zero and a "rebound" there is meaningless — unrestricted, that scored a +0.052
    rebound between FSC -0.036 and +0.016 on P5 J81.
    """
    wc = _wn_at_level(wn_f, y_f, 0.143)
    if wc:
        sel = wn_f <= wc
        wn_f, y_f = wn_f[sel], y_f[sel]
    out = {'delta': 0.0, 'trough_fsc': None, 'trough_res_A': None,
           'peak_fsc': None, 'peak_res_A': None}
    start = int(np.argmax(y_f < 0.9)) if (y_f < 0.9).any() else 0  # skip the low-res plateau
    if y_f.size - start < 3:
        return out
    seg_y, seg_wn = y_f[start:], wn_f[start:]
    d = seg_y - np.minimum.accumulate(seg_y)
    k = int(np.argmax(d))
    if d[k] <= 0:
        return out
    j = int(np.argmin(seg_y[:k + 1]))
    return {'delta': round(float(d[k]), 4),
            'trough_fsc': round(float(seg_y[j]), 4),
            'trough_res_A': round(float(box_A / seg_wn[j]), 2) if seg_wn[j] > 0 else None,
            'peak_fsc': round(float(seg_y[k]), 4),
            'peak_res_A': round(float(box_A / seg_wn[k]), 2) if seg_wn[k] > 0 else None}


def rebound_trajectory(proj, job_uid: str, column: str = 'fsc_nomask') -> list:
    """
    The rebound amplitude at EVERY iteration of one job, on the untightened file
    (the only apples-to-apples series). This is what makes the depth confound
    visible instead of guessable: one AT1R job spanned -0.014 -> +0.173 across its
    own iterations, more than the entire between-job spread being read as biology.

    Returns [(iteration, delta), ...] ascending; [] if unavailable.
    """
    try:
        _, assets = _fsc_assets(proj, job_uid)
        its = sorted({a['iteration'] for a in assets
                      if a['iteration'] >= 0 and not a['tightened']})
        traj = []
        for it in its:
            t = read_fsc_table(proj, job_uid, iteration=it, tightened=False)
            if not t or column not in t['columns']:
                continue
            y = np.asarray(t['columns'][column], float)
            ok = np.isfinite(y)
            traj.append((it, _rebound(t['wave_number'][ok], y[ok], t['box_A'])['delta']))
        return traj
    except Exception as e:
        print(f'[rebound_trajectory] {job_uid}: {e!r}')
        return []


def fsc_curve_health(proj, job_uid: str, iteration: Optional[int] = None,
                     column: str = 'fsc_nomask', tightened: Optional[bool] = None,
                     trajectory: bool = True, verbose: bool = True) -> Optional[dict]:
    """
    SECONDARY resolution check: the SHAPE of the FSC curve, not its crossing.

    Default column is `fsc_nomask` because it exists at EVERY iteration (so
    curves are comparable across jobs) and carries no mask work. Pass
    column='fsc_noisesub' only when reading a single map's final iteration.

    Measures, in the order of the checklist:
      1. MONOTONICITY  — the largest trough -> later rebound on the curve. A
         rebound means two components decorrelating at different scales, or a
         shared artefact correlating the half-maps.
      2. CROSSINGS     — 0.143 for every stored column + mask_effect_A.
      3. DIVERGENCE    — the resolution at which the masked curve separates from
         the unmasked one. Early (low-res) separation = the mask is doing work
         where it should not.
      4. SLOPE         — how many shells the curve takes from 0.5 to 0.143. A
         near-vertical cliff is characteristic of a mask/noise artefact.
      5. TAIL          — the mean FSC beyond the crossing. A non-zero plateau
         means the half-maps share correlated noise.
      6. 0.5-vs-0.143 GAP — an unusually wide gap points at heterogeneity or
         anisotropy rather than a clean resolution limit.
    Ice-resolution bumps (~3.66 / 3.44 / 2.67 A) are NOT automated: they need the
    power spectrum for context, so check those by eye when a bump shows up here.

    Returns {verdict: HEALTHY|MINOR|UNHEALTHY, flags[], iteration, column,
             res_A{}, mask_effect_A, rebound{}, divergence{}, slope{}, tail{},
             summary} or None. Thresholds are HEURISTIC — report the NUMBERS,
             not just the verdict.
    """
    t = read_fsc_table(proj, job_uid, iteration=iteration, tightened=tightened)
    if t is None:
        print(f'[fsc_curve_health] no FSC table for {job_uid}')
        return None
    if column not in t['columns']:
        column = next(iter(t['columns']), None)
        if column is None:
            return None

    wn, box_A = t['wave_number'], t['box_A']
    y = np.asarray(t['columns'][column], dtype=float)
    ok = np.isfinite(y)
    wn_f, y_f = wn[ok], y[ok]
    flags, th = [], _FSC_HEALTH_THRESH

    # ---- 1. monotonicity: worst trough -> later rebound ----------------------
    reb = _rebound(wn_f, y_f, box_A)
    if reb['delta'] >= th['rebound_strong']:
        flags.append('NON_MONOTONIC')
    elif reb['delta'] >= th['rebound_mild']:
        flags.append('MILD_REBOUND')

    # ---- 2. crossings of every column ---------------------------------------
    res_by_col = dict(t['res_A'])
    corrected = next((c for c in _FSC_CORRECTED_PREF if c in res_by_col), None)
    mask_effect = (round(res_by_col['fsc_nomask'] - res_by_col[corrected], 2)
                   if corrected and 'fsc_nomask' in res_by_col else None)

    # ---- 3. masked-vs-unmasked divergence point (REPORTED, never flagged) ----
    # See the measured negative above: this lands 7.5-19.2 A on ordinary maps and
    # carries no quality signal on its own. Read it together with mask_effect_A
    # and the rebound; do not rank arms on it.
    div = {'res_A': None, 'nomask_fsc_at_div': None, 'masked_column': None}
    masked = next((c for c in ('fsc_tightmask', 'fsc_loosemask') if c in t['columns']), None)
    res_c = res_by_col.get(column)
    if masked and 'fsc_nomask' in t['columns']:
        nm = np.asarray(t['columns']['fsc_nomask'], float)
        m = np.asarray(t['columns'][masked], float) - nm
        good = np.isfinite(m) & (wn > 0)
        idx = np.where(good & (m > th['mask_diverge']))[0]
        if idx.size:
            i0 = int(idx[0])
            div = {'res_A': round(float(box_A / wn[i0]), 2), 'masked_column': masked,
                   'nomask_fsc_at_div': round(float(nm[i0]), 3)}

    # ---- 4. slope at the crossing (shells from 0.5 to 0.143) -----------------
    wn_05, wn_0143 = _wn_at_level(wn_f, y_f, 0.5), _wn_at_level(wn_f, y_f, 0.143)
    slope = {'shells_0p5_to_0p143': None, 'res_at_0p5_A': None}
    if wn_05 and wn_0143:
        slope = {'shells_0p5_to_0p143': round(float(wn_0143 - wn_05), 2),
                 'res_at_0p5_A': round(float(box_A / wn_05), 2)}
        if wn_0143 - wn_05 <= th['cliff_shells']:
            flags.append('CLIFF_AT_CROSSING')

    # ---- 5. tail beyond the crossing ----------------------------------------
    tail = {'mean': None, 'max': None, 'n_shells': 0}
    if wn_0143:
        sel = wn_f > 1.15 * wn_0143
        if sel.sum() >= 3:
            tail = {'mean': round(float(np.mean(y_f[sel])), 4),
                    'max': round(float(np.max(y_f[sel])), 4),
                    'n_shells': int(sel.sum())}
            if tail['mean'] >= th['tail_plateau']:
                flags.append('NONZERO_TAIL')

    # ---- 6. 0.5-vs-0.143 gap -------------------------------------------------
    gap_x = None
    if slope['res_at_0p5_A'] and res_c:
        gap_x = round(slope['res_at_0p5_A'] / res_c, 2)
        if gap_x >= th['wide_gap_x']:
            flags.append('WIDE_0P5_TO_0P143_GAP')

    # ---- 7. is the rebound a property of the MAP, or of the DEPTH? -----------
    traj = rebound_trajectory(proj, job_uid, column=column) if trajectory else []
    if len(traj) >= 3 and reb['delta'] >= th['rebound_mild']:
        mid = traj[len(traj) // 2][1]
        if mid > 0 and traj[-1][1] >= th['depth_growth_x'] * mid:
            flags.append('REBOUND_GROWS_WITH_DEPTH')

    strong = {'NON_MONOTONIC', 'NONZERO_TAIL', 'CLIFF_AT_CROSSING'}
    verdict = ('UNHEALTHY' if strong & set(flags) else
               'MINOR' if flags else 'HEALTHY')
    summary = (f"{job_uid} it={t['iteration']}{'+tight' if t['tightened'] else ''} "
               f"[{column}] {verdict} {flags} | res@0.143={res_c} A "
               f"mask_effect={mask_effect} A | rebound {reb['delta']:+.3f} "
               f"(trough {reb['trough_fsc']}@{reb['trough_res_A']} A -> "
               f"{reb['peak_fsc']}@{reb['peak_res_A']} A) | diverge@{div['res_A']} A "
               f"(reported, not scored) | 0.5->0.143 {slope['shells_0p5_to_0p143']} shells "
               f"(gap {gap_x}x) | tail mean={tail['mean']}"
               + (f" | rebound by iteration {[(i, round(d, 3)) for i, d in traj]}" if traj else ''))
    out = {'job': job_uid, 'verdict': verdict, 'flags': flags,
           'iteration': t['iteration'], 'tightened': t['tightened'],
           'column': column, 'asset': t['filename'], 'res_A': res_by_col,
           'res_at_0143_A': res_c, 'mask_effect_A': mask_effect,
           'rebound': reb, 'rebound_trajectory': traj, 'divergence': div,
           'slope': slope, 'tail': tail,
           'gap_0p5_over_0p143_x': gap_x, 'summary': summary,
           'thresholds': dict(th)}
    if verbose:
        print(summary)
        print('  [SECONDARY axis — vis_res stays primary. The verdict is a graded '
              'heuristic on a continuum: report the NUMBERS and the iteration. '
              'Cross-job comparison ONLY via compare_fsc_curve_health().]')
    return out


def compare_fsc_curve_health(proj, job_uids, column: str = 'fsc_nomask',
                             verbose: bool = True) -> dict:
    """
    Curve health for several jobs read at the DEEPEST COMMON iteration, on a
    column that exists at every iteration, with the auto-tightened file excluded
    — the only way these numbers are comparable across arms.

    Returns {'iteration': n, 'per_job': {uid: health}, 'final': {uid: health}}
    ('final' = each job's own final iteration, so the depth effect stays visible;
    report BOTH where it matters).
    """
    depths = {}
    for u in job_uids:
        _, assets = _fsc_assets(proj, u)
        its = [a['iteration'] for a in assets if a['iteration'] >= 0 and not a['tightened']]
        if its:
            depths[u] = max(its)
    if not depths:
        print('[compare_fsc_curve_health] no untightened FSC assets found')
        return {'iteration': None, 'per_job': {}, 'final': {}}
    common = min(depths.values())
    if verbose:
        print(f'[compare_fsc_curve_health] depths {depths} -> matched iteration {common}')
    per_job = {u: fsc_curve_health(proj, u, iteration=common, column=column,
                                   tightened=False, trajectory=False, verbose=verbose)
               for u in depths}
    final = {u: fsc_curve_health(proj, u, iteration=None, column=column,
                                 tightened=None, trajectory=False, verbose=verbose)
             for u in depths}
    return {'iteration': common, 'per_job': per_job, 'final': final}


# Back-compat thin wrapper: just the float.
def corrected_fsc_resolution(proj, job_uid: str) -> Optional[float]:
    r = fsc_resolution_from_job(proj, job_uid)
    return r['res_A'] if r else None


if __name__ == '__main__':
    import json, sys
    if len(sys.argv) < 2:
        print('usage: python vis_res_utils.py <map.mrc> [out_dir]')
        raise SystemExit(2)
    res = render_map_slices(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    print(json.dumps(res, indent=2))
    print('\nNow READ the contact sheet and apply build_prompt():\n')
    print(build_prompt())
