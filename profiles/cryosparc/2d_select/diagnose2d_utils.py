"""
diagnose2d_utils.py — 2D health diagnosis of the particle set about to enter 3D (the PRE-3D GATE).

WHY
  In phase 1 the first ab-initio is expensive, and it fails for reasons ALREADY VISIBLE in the 2D
  class averages the particles were selected from — a box or 2D mask that clips the density (or one
  so loose it drags the neighbours in), a set that is still dirty, too few particles, or a view
  distribution too narrow to reconstruct. Running 3D on such a set burns hours and the failure is
  then mis-diagnosed as a 3D problem.

  So: before subC wires the first 3D job, it diagnoses the SELECTED 2D averages that describe the
  stack going forward, and reports to agentM, who decides what happens to the plan.

MULTI-BRANCH BY CONSTRUCTION
  The set feeding 3D is usually NOT one class_2D's output. It is a merge (+ `remove_duplicate_
  particles`) of several branches — blob + template + topaz picks, a re-pick merged with an older
  stack, several rounds of `select_2D` / `reference_select_2D`. Diagnosing only one of them misses
  the branch that is actually broken. So this tool takes the job that FEEDS 3D, walks the particle
  lineage up through every merge/dedup/restack, and finds EVERY selection branch behind it. Each
  branch is measured on its own class averages (box, mask and pixel size can differ per branch!),
  each branch's real contribution to the final set is measured by uid intersection, and the checks
  are then run on the pooled dataset with the per-branch detail kept.

THE FOUR CHECKS
  1. box_and_mask       — is the box and the 2D circular mask appropriate for the WHITE particle
                          density? TOO TIGHT: a component/lobe is smeared, runs to the box edge, or
                          is CUT by the mask. TOO LOOSE: a small particle in a large empty field,
                          and/or neighbouring-particle signal contaminating the particle edge.
                          Reports the particle's MIN and MAX diameter and compares both against the
                          size actually used so far (base_datainfo `particle_size_A` and the picker
                          diameters / extract box found in the lineage).
  2. cleanliness        — is the set CLEAN enough and a 3D candidate: do the dominant views show
                          good secondary-structure detail (judged against the extraction NYQUIST —
                          a stack binned to 6 A/px cannot show better than 12 A, which is a binning
                          limit, not dirt), and how much particle mass still sits in junk-like
                          classes?
  3. particle_count     — ADVISORY. A few thousand particles can start a 3D analysis, especially at
                          high symmetry; this check is a REMINDER to enrich, not a gate. It never
                          halts on its own.
  4. orientation_coverage — is the set confined to too narrow a range of views?

METHOD — numbers + a VISION call, same contract as `vis_res_utils` / `select2d_utils`
  This module MEASURES and RENDERS; the perceptual part (is that lobe smeared? is that class junk?
  is that a genuinely different view?) is a VISION CALL by the calling agent, which has the Read
  tool. `assess_2d()` combines both into one verdict and the exact string subC returns.

USAGE (subC)
    import sys; sys.path.insert(0, "2d_select")   # run from the CryoSPARC profile directory
    from diagnose2d_utils import diagnose_2d, assess_2d, build_prompt, decisions_log_block

    ev = diagnose_2d(proj, "J440",                 # the job whose particles feed the first 3D job
                     datainfo_path="projects/<name>/base_datainfo.yaml")
    print(build_prompt(ev))
    # READ ev['panel_pngs'] (one per branch), ev['views_png'], ev['profile_png'] — then:
    rep = assess_2d(ev,
        box_verdict="ok",            # 'ok' | 'too_small' | 'too_large'
        clean_verdict="clean",       # 'clean' | 'some_junk' | 'dirty'
        secondary_structure=True,
        vision_res_A=9.0,
        distinct_views_seen=6,
        notes="...")
    print(rep['subc_return'])

AGENTM DECISION (rep['status'] -> rep['agentM_action'])
  HEALTHY          -> 1. healthy / little problem — let the plan run on, no additional action.
  MINOR            -> 2. maybe a problem — run the 3D try and see the result, carrying the caveat.
  MORE_2D_CLEANING -> 3. particle count is sufficient but the set is NOT clean — run more
                        2D classify -> select rounds (and/or CryoRanker) before 3D.
  SERIOUS          -> 4. halt subC's 3D try — re-run with the corrected parameter; label the
                        original branch negative, or keep it positive and open a corrected branch.
  ASK_HUMAN        -> 5. ambiguous / contradictory — ask the human to inspect and instruct.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import mrcfile
from PIL import Image, ImageDraw, ImageFont

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

_FONT_PATH = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'

# Optional module-level default path to the ACTIVE project's base_datainfo.yaml (same convention as
# select2d_utils.DATAINFO_PATH): set it once per session so bare diagnose_2d(proj, uid) calls pick
# up particle_size_A / particle_character / symmetry without threading the path through every call.
DATAINFO_PATH: str | Path | None = None

SELECT_TYPES = ('select_2D', 'reference_select_2D')
CLASS2D_TYPES = ('class_2D', 'class_2D_new')
# Job types the particle lineage is walked THROUGH on the way up to a selection.
WALK_THROUGH = ('remove_duplicate_particles', 'particle_sets', 'restack_particles', 'exposure_sets',
                'extract_micrographs_multi', 'extract_micrographs_cpu_parallel', 'downsample_particles',
                'sym_expand', 'ctf_refine_global', 'reassign_particles_to_micrographs',
                'homo_abinit', 'hetero_refine', 'nonuniform_refine_new', 'nonuniform_refine',
                'homo_refine_new', 'new_local_refine', 'class_3D', 'snowflake', 'junk_detector_v1',
                'topaz_extract', 'blob_picker_gpu', 'template_picker_gpu', 'class_2D', 'class_2D_new')


# --------------------------------------------------------------------------- #
# Thresholds — every numeric cut lives here so it is visible and tunable.       #
# --------------------------------------------------------------------------- #
THRESHOLDS = {
    # 1. box + mask.  box_ratio = box_A / measured particle MAX diameter.
    'box_ratio_tight':         1.30,   # < this -> tight (SERIOUS only with clipping evidence)
    'box_ratio_minor_small':   1.50,   # < this -> fix at the full-size re-extract
    'box_ratio_minor_large':   2.50,   # > this -> generous: wasted box + neighbour risk
    'box_ratio_serious_large': 3.50,   # > this -> particle is a dot; neighbours inside the window
    'edge_signal_frac':        0.05,   # signal mass beyond 0.85R -> the box is cutting it
    'edge_contact_frac':       0.30,   # fraction of classes with signal at the very box edge
    'mask_cut_frac':           0.03,   # signal mass OUTSIDE the 2D mask radius -> the mask CUTS
    # mask_ratio = class2D mask diameter / measured particle MAX diameter
    'mask_ratio_serious_small': 1.00,  # mask smaller than the particle
    'mask_ratio_minor_small':   1.15,
    'mask_ratio_minor_large':   1.60,  # only counted when the mask was EXPLICITLY set (see below)
    'mask_ratio_serious_large': 2.20,
    'declared_size_mismatch':   0.40,  # |measured-used|/used above this -> picking diameter suspect

    # 2. cleanliness / 3D candidacy
    'res_minor_A':   12.0,             # particle-weighted median class resolution
    'res_serious_A': 18.0,
    'res_vs_nyquist_saturated': 1.15,  # res <= this x Nyquist -> BINNING-limited, not dirt
    'ess_minor':      5.0,             # mean effective #classes assigned (ECA); 1 = confident
    'ess_serious':   10.0,
    'junk_mass_minor':   0.25,         # particle mass in junk-like classes
    'junk_mass_serious': 0.35,
    # NOTE on what counts as "junk-like": only STRUCTURAL indicators (odd size, low peak SNR, signal
    # at the box edge). Class RESOLUTION is deliberately NOT one of them — it is reported on its own
    # in the resolution check instead. Resolution-based junk flags proved unusable both ways: keyed
    # to the BEST class they flag legitimate rare/low-count views (26% "junk" on a P1 set that
    # reached 2.71 A), and keyed to the MEDIAN they are circular (if half the set is junk the median
    # IS junk-level, so nothing is flagged — it silently cleared the three dirtiest sets in the
    # history). The vision `clean_verdict` is the deciding vote on cleanliness; these numbers only
    # flag structural junk that is junk in any dataset.
    'junk_size_lo':      0.60,         # class d_major outside [lo,hi] x population median -> junk
    'junk_size_hi':      1.60,
    'junk_peak_snr':     4.0,          # class peak below this x background sigma -> junk-like

    # 3. particle count — ADVISORY ONLY, symmetry-aware (effective = n x symmetry order).
    'n_enrich_reminder': 20_000,       # below -> remind to enrich (still OK)
    'n_low':              5_000,       # below -> MINOR + strong enrich reminder. Never SERIOUS.

    # 4. orientation coverage (particle mass over ROTATION-INVARIANT view groups)
    'top_view_frac_serious': 0.70,
    'top_view_frac_minor':   0.50,
    'eff_views_serious': 2.0,
    'eff_views_minor':   3.5,
    'view_corr_same':    0.82,         # descriptor correlation above which two classes = same view

    # misc
    'size_spread_ratio': 1.50,         # d_p90/d_p50 -> aggregates still in the set
    'untraced_frac':     0.10,         # this much of the final set not behind any select -> flag
}


RUBRIC_2D = """\
2D HEALTH RUBRIC — what to look for in the rendered panels. Use the SCALE BAR, and use what the
sample IS (its name/components from base_datainfo.yaml particle_character).

1. BOX + MASK vs the white particle density
   Rings on every panel: YELLOW = measured particle extent (15%-of-peak contour), CYAN = the 2D
   circular mask, WHITE = 0.85 x box radius (the standard windowing radius).
   • TOO TIGHT (box and/or mask): the density touches or crosses the cyan/white ring; a lobe or
     domain is SMEARED, streaked or truncated at the boundary; the average looks "pressed" into
     the circle; a component that should be there (micelle belt, Fab, G protein, ARD) is CUT OFF
     by the mask. Delocalized CTF signal is being cut -> aliasing.
   • TOO LOOSE: the particle is a small blob with a wide empty margin inside the mask, and/or
     fragments of NEIGHBOURING particles appear around it, contaminating the particle edge and
     the alignment.
   • Healthy: the whole particle (incl. any micelle/nanodisc belt) inside the yellow ring with a
     clear solvent margin before the cyan ring; box ~1.5-2x the longest dimension.
   The prompt gives the MIN and MAX diameter measured from the averages next to the size ACTUALLY
   USED so far (particle_size_A and the picker diameters / extract box in the lineage). A measured
   size that does not match what the sample should be — or what was used — is itself the finding:
   say what the corrected diameter/box should be.

2. CLEANLINESS — is this set CLEAN enough to reconstruct?
   Judge the DOMINANT (most populated) views, against the extraction NYQUIST given in the prompt.
   • Clean: sharp outlines, internal features — helix striations/rods, domain lobes, a resolved
     micelle boundary, TM bundles down the axis; one consistent particle identity across classes.
   • Some junk: mostly good, but a few classes are smeared / featureless white blobs / oversized
     aggregates / edge or ice artefacts / a different species.
   • Dirty: junk classes carry a large share of the particles, or the classes show more than one
     kind of object, or the "particle" has no consistent shape.
   • A stack binned so Nyquist is e.g. 12 A CANNOT show detail finer than ~12 A. Do NOT call that
     dirt or a failure — it is a binning limit; say so and judge the shape instead.

3. PARTICLE COUNT — advisory. A few thousand particles CAN start a 3D analysis (especially at high
   symmetry); the number here is a REMINDER about whether the set needs enriching, not a gate.

4. ORIENTATION COVERAGE — count the GENUINELY DIFFERENT views (not in-plane rotations of one view,
   which look identical rotated). Well-covered = top/side/tilted projections with different
   outlines. Severe preferred orientation = essentially one view family holding most of the
   particles -> the 3D map will be smeared/elongated along the missing direction. Fewer distinct
   views are needed at high symmetry.

MULTI-BRANCH: the panels come one per SELECTION BRANCH. Judge each branch, and say which branch is
the bad one — box/mask and pixel size can differ between branches, and one dirty branch can be
dropped without discarding the rest.

WHEN YOU CANNOT TELL: pass unsure=True to assess_2d rather than guessing — that routes the call to
the human instead of committing hours of GPU time on a coin flip."""


def _font(size: int):
    try:
        return ImageFont.truetype(_FONT_PATH, size)
    except Exception:
        return ImageFont.load_default()


def _wpct(vals, weights, q: float) -> float:
    """Weighted percentile (q in [0,1]) — every aggregate here is particle-weighted."""
    vals = np.asarray(vals, dtype=float)
    weights = np.asarray(weights, dtype=float)
    if vals.size == 0:
        return float('nan')
    o = np.argsort(vals)
    cw = np.cumsum(weights[o]) / max(weights.sum(), 1e-9)
    return float(vals[o][min(int(np.searchsorted(cw, q)), len(vals) - 1)])


# --------------------------------------------------------------------------- #
# Measurements on a single class average                                        #
# --------------------------------------------------------------------------- #

def _radial_profile(img: np.ndarray, center, n_bins: int):
    """Azimuthally-averaged profile p(r) in `n_bins` rings out to half the box."""
    h, w = img.shape
    yy, xx = np.mgrid[0:h, 0:w]
    rr = np.hypot(yy - center[0], xx - center[1])
    r_max = min(h, w) / 2.0
    bins = np.linspace(0, r_max, n_bins + 1)
    idx = np.clip(np.digitize(rr.ravel(), bins) - 1, 0, n_bins - 1)
    cnt = np.bincount(idx, minlength=n_bins).astype(float)
    prof = np.bincount(idx, weights=img.ravel(), minlength=n_bins) / np.maximum(cnt, 1)
    return 0.5 * (bins[1:] + bins[:-1]), prof


def class_metrics(img: np.ndarray, psize_A: float,
                  mask_px: Optional[float] = None,
                  peak_frac: float = 0.15,
                  bg_annulus: tuple = (0.85, 1.0)) -> dict:
    """
    Measure ONE class average. All sizes in Angstrom.

    The particle is the `peak_frac`-of-peak contour (default 15%) above a background estimated in
    the outer annulus (0.85-1.0 R), the region CryoSPARC's real-space window leaves as solvent.
    Four size numbers, because they answer different questions:
      d_max_A   : 2 x the furthest above-threshold pixel from centre — the longest dimension in
                  this projection. This is what the BOX must fit.
      d_major_A / d_minor_A : full extent along the principal axes of the signal mass — the
                  particle's long and short dimension in this view. Pooled over the classes these
                  give the dataset's MAX and MIN diameter.
      d_bulk_A  : 2 x the radius holding 90% of the positive mass — where the body of the density
                  is. d_max >> d_bulk means a thin protrusion, a smear or a streak.

    Also: `mask_cut_frac` (positive signal mass OUTSIDE the 2D mask radius — the direct measure of
    "the mask is cutting the particle"), `edge_signal_frac` (mass beyond 0.85R), `edge_contact`,
    `empty_frac` (share of the box that is solvent), `com_offset_A`, `peak_snr`.
    """
    img = np.asarray(img, dtype=np.float32)
    h, w = img.shape
    c = ((h - 1) / 2.0, (w - 1) / 2.0)
    R = min(h, w) / 2.0

    yy, xx = np.mgrid[0:h, 0:w]
    rr = np.hypot(yy - c[0], xx - c[1])

    ann = (rr >= bg_annulus[0] * R) & (rr <= bg_annulus[1] * R)
    if ann.sum() < 16:
        ann = rr >= 0.85 * R
    bg = float(np.median(img[ann]))
    bg_sd = float(np.median(np.abs(img[ann] - bg)) * 1.4826) or 1e-6

    sig = img - bg
    inner = rr < 0.8 * R
    peak = float(np.percentile(sig[inner], 99.5)) if inner.any() else float(sig.max())
    peak = max(peak, 1e-6)
    mask = (sig > peak_frac * peak) & (rr <= R)

    d_major = d_minor = r_max_px = r_bulk_px = 0.0
    com = c
    if mask.any():
        r_max_px = float(rr[mask].max())
        pos = np.where(mask, sig, 0.0)
        order = np.argsort(rr.ravel())
        cum = np.cumsum(pos.ravel()[order])
        total = cum[-1] if cum[-1] > 0 else 1.0
        r_bulk_px = float(rr.ravel()[order][int(np.searchsorted(cum, 0.90 * total))])
        m = pos.sum()
        if m > 0:
            com = ((pos * yy).sum() / m, (pos * xx).sum() / m)
        # principal axes of the thresholded signal -> long / short dimension of THIS view
        ys, xs = np.where(mask)
        pts = np.stack([ys - ys.mean(), xs - xs.mean()])
        if pts.shape[1] >= 3:
            covm = np.cov(pts)
            _, vecs = np.linalg.eigh(covm)
            proj = vecs.T @ pts                       # rows: minor axis, major axis
            d_minor = float(proj[0].max() - proj[0].min())
            d_major = float(proj[1].max() - proj[1].min())
            if d_minor > d_major:
                d_minor, d_major = d_major, d_minor

    pos_all = np.clip(sig, 0, None)
    tot = float(pos_all[rr <= R].sum()) or 1.0
    edge_signal_frac = float(pos_all[(rr > 0.85 * R) & (rr <= R)].sum() / tot)
    mask_cut_frac = (float(pos_all[(rr > mask_px / 2.0) & (rr <= R)].sum() / tot)
                     if mask_px else None)

    r_c, prof = _radial_profile(img, c, n_bins=max(32, int(R)))
    prof_s = prof - bg
    outer = prof_s[r_c > 0.95 * R]
    edge_contact = bool(outer.size and outer.max() > peak_frac * peak)

    return {
        'd_max_A':   2.0 * r_max_px * psize_A,
        'd_major_A': d_major * psize_A,
        'd_minor_A': d_minor * psize_A,
        'd_bulk_A':  2.0 * r_bulk_px * psize_A,
        'r_max_px': r_max_px,
        'edge_signal_frac': round(edge_signal_frac, 4),
        'mask_cut_frac': round(mask_cut_frac, 4) if mask_cut_frac is not None else None,
        'edge_contact': edge_contact,
        'empty_frac': float(1.0 - mask.sum() / max((rr <= R).sum(), 1)),
        'com_offset_A': float(np.hypot(com[0] - c[0], com[1] - c[1]) * psize_A),
        'peak_snr': float(peak / bg_sd),
        'profile': {'r_A': (r_c * psize_A).tolist(), 'p': prof_s.tolist()},
    }


# --------------------------------------------------------------------------- #
# View grouping: which class averages are the SAME view?                        #
# --------------------------------------------------------------------------- #

def _polar_resample(img: np.ndarray, r_max_px: float, n_r: int = 32, n_theta: int = 64):
    """Bilinear polar resampling about the box centre out to `r_max_px` (no scipy)."""
    h, w = img.shape
    cy, cx = (h - 1) / 2.0, (w - 1) / 2.0
    r_max_px = float(np.clip(r_max_px, 4.0, min(h, w) / 2.0 - 1))
    r = np.linspace(r_max_px / n_r, r_max_px, n_r)
    th = np.linspace(0, 2 * np.pi, n_theta, endpoint=False)
    R, TH = np.meshgrid(r, th, indexing='ij')
    y = cy + R * np.sin(TH)
    x = cx + R * np.cos(TH)
    y0 = np.clip(np.floor(y).astype(int), 0, h - 2)
    x0 = np.clip(np.floor(x).astype(int), 0, w - 2)
    fy, fx = y - y0, x - x0
    return (img[y0, x0] * (1 - fy) * (1 - fx) + img[y0 + 1, x0] * fy * (1 - fx)
            + img[y0, x0 + 1] * (1 - fy) * fx + img[y0 + 1, x0 + 1] * fy * fx)


def view_descriptor(img: np.ndarray, psize_A: float, r_max_A: float,
                    n_harm: int = 13, w_dc: float = 0.35) -> np.ndarray:
    """
    A descriptor of a class average INVARIANT to in-plane rotation: resample to polar coordinates,
    then take the magnitude of the FFT along the angular axis (a rotation is a shift in theta,
    which changes only the phase). Sampling runs to a fixed PHYSICAL radius `r_max_A`, so classes
    from branches with different pixel sizes / boxes stay comparable.

    The k=0 harmonic is just the radial profile, near-identical for every class of one dataset, and
    it swamps the comparison (measured: all pairwise correlations collapse into 0.96-0.99, nothing
    separates). So the ANGULAR harmonics k=1..n_harm are normalised on their own and the radial
    profile is added back with a small weight `w_dc` — enough to keep size sensitivity, not enough
    to dominate. Calibrated on a 50-class set: this separates round top views, elongated side views
    and two-blob aggregates into distinct groups at THRESHOLDS['view_corr_same'].

    CAVEAT (state it when reporting): the magnitude spectrum is also invariant to MIRRORING, so
    mirror-related classes — genuinely different projection directions — merge into one group. The
    view count is a LOWER BOUND, i.e. it errs toward flagging preferred orientation. The vision
    call is the deciding vote on view diversity; this number only corroborates.
    """
    img = np.asarray(img, dtype=np.float32)
    img = img - np.median(img)
    img = img / (img.std() or 1.0)
    pol = _polar_resample(img, r_max_A / max(psize_A, 1e-6))
    F = np.abs(np.fft.rfft(pol, axis=1))
    ang = F[:, 1:n_harm].ravel()
    ang = ang / (np.linalg.norm(ang) or 1.0)
    dc = F[:, 0]
    dc = dc / (np.linalg.norm(dc) or 1.0)
    d = np.concatenate([ang, w_dc * dc])
    return d / (np.linalg.norm(d) or 1.0)


def group_views(descriptors, counts, corr_same: float = None) -> dict:
    """
    Greedy grouping of class averages into distinct VIEWS by descriptor correlation, seeded from
    the most populated class down. Returns particle mass per group.

    `eff_views` = exp(Shannon entropy) of the particle mass over groups — the EFFECTIVE number of
    views, which is what matters for a reconstruction (10 groups where one holds 95% of the
    particles is effectively one view).
    """
    corr_same = THRESHOLDS['view_corr_same'] if corr_same is None else corr_same
    desc = np.stack(list(descriptors))
    corr = desc @ desc.T
    counts = np.asarray(counts, dtype=float)

    labels = -np.ones(len(desc), dtype=int)
    reps: list[int] = []
    for i in np.argsort(-counts):
        for g, rep in enumerate(reps):
            if corr[i, rep] >= corr_same:
                labels[i] = g
                break
        else:
            labels[i] = len(reps)
            reps.append(int(i))

    groups = []
    for g, rep in enumerate(reps):
        rows = np.where(labels == g)[0]
        groups.append({'rep': int(rep), 'members': rows.tolist(),
                       'n_particles': int(counts[rows].sum())})
    groups.sort(key=lambda x: -x['n_particles'])

    tot = counts.sum() or 1.0
    p = np.array([g['n_particles'] for g in groups], dtype=float) / tot
    p = p[p > 0]
    eff_views = float(np.exp(-(p * np.log(p)).sum())) if p.size else 0.0
    pc = counts / tot
    pc = pc[pc > 0]
    return {'groups': groups, 'labels': labels.tolist(), 'n_groups': len(groups),
            'top_view_frac': float(groups[0]['n_particles'] / tot) if groups else 1.0,
            'eff_views': eff_views,
            'eff_classes': float(np.exp(-(pc * np.log(pc)).sum())) if pc.size else 0.0}


# --------------------------------------------------------------------------- #
# Lineage: find EVERY selection branch behind the set that feeds 3D             #
# --------------------------------------------------------------------------- #

def _doc(proj, uid: str) -> dict:
    return proj.find_job(uid).doc


def _particle_parents(doc: dict) -> list[tuple[str, str]]:
    """(parent_job_uid, parent_group_name) for every PARTICLE input connection."""
    out = []
    for g in doc.get('input_slot_groups', []) or []:
        if g.get('type') not in ('particle', None) and 'particle' not in (g.get('name') or ''):
            continue
        for c in g.get('connections', []) or []:
            if c.get('job_uid'):
                out.append((c['job_uid'], c.get('group_name')))
    return out


def _has_output_group(doc: dict, name: str) -> bool:
    return any(r.get('group_name') == name for r in (doc.get('output_results') or []))


def _output_cs_path(proj, doc: dict, group: str) -> Optional[Path]:
    """Path to the non-passthrough .cs of one output group."""
    for r in doc.get('output_results') or []:
        if r.get('group_name') == group and not r.get('passthrough'):
            mf = r.get('metafiles') or []
            if mf:
                return Path(proj.dir()) / mf[-1]
    return None


def _is_select(proj, doc: dict) -> bool:
    """
    A 2D SELECTION step: `select_2D` / `reference_select_2D`, or the external-job select fallback
    (`snowflake`) that select2d_utils creates when the interactive job OOMs on the master.

    An external job only counts when a class_2D is actually wired into it — external jobs are used
    for other particle filters too (e.g. a correlation cut), and treating one of those as a 2D
    selection would terminate the walk early and leave its particles untraced instead of following
    them up to the real 2D selection above.
    """
    t = doc.get('job_type')
    if t in SELECT_TYPES:
        return True
    if t == 'snowflake' and _has_output_group(doc, 'particles_selected'):
        return _class2d_of_doc(proj, doc) is not None
    return False


def _class2d_of_doc(proj, doc: dict) -> Optional[str]:
    """The class_2D job wired into this job, if any."""
    for g in doc.get('input_slot_groups', []) or []:
        for c in g.get('connections', []) or []:
            pj = c.get('job_uid')
            if not pj:
                continue
            try:
                if _doc(proj, pj).get('job_type') in CLASS2D_TYPES:
                    return pj
            except Exception:
                continue
    return None


def trace_branches(proj, source_job_uid: str, max_depth: int = 16, verbose: bool = True) -> list[dict]:
    """
    Walk the PARTICLE lineage up from `source_job_uid` (the job feeding the first 3D job) and
    return every SELECTION BRANCH behind it — merges, dedups, restacks and re-extractions are
    walked through, so a set merged from blob + template + topaz picks yields three branches.

    Each path stops at the FIRST selection it meets going up: that is the selection that actually
    defines what went forward on that path (earlier rounds upstream of it are superseded). A path
    that reaches a class_2D with no selection above it is returned with `no_selection=True`; a path
    that reaches an extract/import with no 2D at all is returned as `unselected`.
    """
    branches, seen = [], set()
    frontier = [(source_job_uid, 0)]
    while frontier:
        uid, depth = frontier.pop(0)
        if uid in seen or depth > max_depth:
            continue
        seen.add(uid)
        try:
            doc = _doc(proj, uid)
        except Exception as e:
            if verbose:
                print(f'[trace] cannot read {uid}: {e!r}')
            continue
        t = doc.get('job_type')
        if _is_select(proj, doc):
            branches.append({'select_job': uid, 'select_type': t,
                             'select_title': (doc.get('title') or '')[:60],
                             'select_params': _params(doc), 'depth': depth,
                             'no_selection': False})
            continue                                     # superseded upstream — stop this path
        parents = _particle_parents(doc)
        if not parents:
            if t in CLASS2D_TYPES:
                branches.append({'select_job': None, 'select_type': None, 'select_title': '',
                                 'class2d_job': uid, 'depth': depth, 'no_selection': True})
            continue
        if t in CLASS2D_TYPES and depth > 0:
            # particles taken straight out of a class_2D without any select_2D
            branches.append({'select_job': None, 'select_type': None, 'select_title': '',
                             'class2d_job': uid, 'depth': depth, 'no_selection': True})
            continue
        for pj, _ in parents:
            frontier.append((pj, depth + 1))
    if verbose:
        print(f'[trace] {source_job_uid}: {len(branches)} selection branch(es) -> '
              f'{[b.get("select_job") or b.get("class2d_job") for b in branches]}')
    return branches


def _params(doc: dict) -> dict:
    return {k: (v.get('value') if isinstance(v, dict) else v)
            for k, v in (doc.get('params_spec') or {}).items()}


def _class2d_of_select(proj, select_uid: str) -> Optional[str]:
    """The class_2D job whose averages a selection job selected from."""
    return _class2d_of_doc(proj, _doc(proj, select_uid))


def _upstream_sizes(proj, start_uid: str, max_depth: int = 12) -> dict:
    """
    Walk further up from a class_2D and collect the particle SIZES actually used so far — any
    upstream parameter whose name mentions a diameter or a box size, with its job. Read
    generically (rather than per job type) so it survives picker/extract naming differences.
    Returns {'diameters': [{job, type, param, value}], 'boxes': [...]}.
    """
    diam, boxes, seen, frontier = [], [], set(), [(start_uid, 0)]
    while frontier:
        uid, d = frontier.pop(0)
        if uid in seen or d > max_depth:
            continue
        seen.add(uid)
        try:
            doc = _doc(proj, uid)
        except Exception:
            continue
        t = doc.get('job_type')
        for k, v in _params(doc).items():
            if v is None:
                continue
            if 'diameter' in k and 'mask' not in k and isinstance(v, (int, float)):
                diam.append({'job': uid, 'type': t, 'param': k, 'value': v})
            elif 'box_size' in k and isinstance(v, (int, float)):
                boxes.append({'job': uid, 'type': t, 'param': k, 'value': v})
        for pj, _ in _particle_parents(doc):
            frontier.append((pj, d + 1))
        for g in doc.get('input_slot_groups', []) or []:      # follow micrographs too (pickers)
            if (g.get('name') or '') in ('micrographs', 'exposures'):
                for c in g.get('connections', []) or []:
                    if c.get('job_uid'):
                        frontier.append((c['job_uid'], d + 1))
    return {'diameters': diam, 'boxes': boxes}


def _load_fields(path: Path, fields: list[str]):
    """Field-limited .cs load (a multi-million-particle stack is GBs with every field)."""
    from cryosparc.dataset import Dataset
    try:
        return Dataset.load(str(path), fields=fields)
    except Exception:
        return Dataset.load(str(path))


# --------------------------------------------------------------------------- #
# Rendering                                                                     #
# --------------------------------------------------------------------------- #

def _thumb(img: np.ndarray, scale: int) -> Image.Image:
    lo, hi = np.percentile(img, [0.5, 99.7])
    hi = max(hi, lo + 1e-6)
    arr = (np.clip((img - lo) / (hi - lo), 0, 1) * 255).astype('uint8')
    im = Image.fromarray(arr, mode='L').convert('RGB')
    return im.resize((img.shape[1] * scale, img.shape[0] * scale), Image.NEAREST)


def _ring(dr, ox, oy, cxy, r_px, scale, color, width=2, dash=False):
    if not r_px or r_px <= 0:
        return
    x0, y0 = ox + (cxy - r_px) * scale, oy + (cxy - r_px) * scale
    x1, y1 = ox + (cxy + r_px) * scale, oy + (cxy + r_px) * scale
    if not dash:
        dr.ellipse([x0, y0, x1, y1], outline=color, width=width)
        return
    for k in range(0, 360, 12):
        dr.arc([x0, y0, x1, y1], k, k + 6, fill=color, width=width)


def _render_branch_panels(data, rows, meta, psize_A, mask_px, title, out_png,
                          scale=3, ncols=8, scalebar_A=50.0) -> Path:
    h, w = data.shape[1:]
    fs = max(11, int(w * scale / 26))
    pad = 2 * fs + 10
    head = fs + 12
    cellw, cellh = w * scale, h * scale + pad
    nrows = (len(rows) + ncols - 1) // ncols
    sheet = Image.new('RGB', (ncols * cellw, nrows * cellh + head + 18), (18, 18, 18))
    dr = ImageDraw.Draw(sheet)
    font = _font(fs)
    dr.text((8, 5), title, fill=(255, 255, 255), font=_font(fs + 2))
    cxy = (w - 1) / 2.0
    for i, row in enumerate(rows):
        m = meta[row]
        ox, oy = (i % ncols) * cellw, (i // ncols) * cellh + pad + head
        sheet.paste(_thumb(data[row], scale), (ox, oy))
        _ring(dr, ox, oy, cxy, m['r_max_px'], scale, (255, 220, 0), 2)
        if mask_px:
            _ring(dr, ox, oy, cxy, mask_px / 2.0, scale, (0, 220, 255), 2, dash=True)
        _ring(dr, ox, oy, cxy, 0.85 * w / 2.0, scale, (235, 235, 235), 1, dash=True)
        col = (210, 90, 90) if m.get('junk_like') else (60, 210, 100)
        dr.rectangle([ox, oy, ox + w * scale - 1, oy + h * scale - 1], outline=col, width=2)
        n = m['n_particles']
        ns = f'{n/1000:.1f}k' if n >= 1000 else str(n)
        eca = f" ECA{m['ess']:.1f}" if m.get('ess') is not None else ''
        res = f"  {m['res_A']:.0f}A" if np.isfinite(m['res_A']) else ''
        dr.text((ox + 3, oy - pad + 3),
                f"#{m['blob_idx']} view{m['view_group']}  {ns}{res}{eca}", fill=col, font=font)
        dr.text((ox + 3, oy - pad + fs + 6),
                f"{m['d_minor_A']:.0f}x{m['d_major_A']:.0f}A"
                + (f" cut={m['mask_cut_frac']*100:.0f}%" if m.get('mask_cut_frac') else '')
                + f" edge={m['edge_signal_frac']*100:.0f}%",
                fill=(255, 220, 0), font=font)
    bar = int(round(scalebar_A / psize_A * scale))
    if 0 < bar < sheet.width:
        y = sheet.height - 14
        dr.rectangle([10, y, 10 + bar, y + 6], fill=(255, 255, 0))
        dr.text((10 + bar + 8, y - 4), f'{scalebar_A:g} A', fill=(255, 255, 0), font=font)
    sheet.save(out_png)
    return Path(out_png)


def _render_views(pool, view, out_png, cell=200) -> Path:
    """One representative class per view group across ALL branches, biggest group first."""
    groups = view['groups']
    fs = max(11, cell // 16)
    pad = 3 * fs + 8
    sheet = Image.new('RGB', (max(1, len(groups)) * cell, cell + pad), (18, 18, 18))
    dr = ImageDraw.Draw(sheet)
    font = _font(fs)
    tot = sum(g['n_particles'] for g in groups) or 1
    for i, g in enumerate(groups):
        e = pool[g['rep']]
        im = _thumb(e['img'], 1).resize((cell, cell), Image.NEAREST)
        ox = i * cell
        sheet.paste(im, (ox, pad))
        frac = g['n_particles'] / tot
        col = (255, 120, 60) if frac > 0.5 else (60, 210, 100)
        dr.rectangle([ox, pad, ox + cell - 1, pad + cell - 1], outline=col, width=2)
        dr.text((ox + 3, 3), f"view {i}  {frac*100:.0f}%", fill=col, font=font)
        dr.text((ox + 3, fs + 5), f"{g['n_particles']:,} ptcl", fill=col, font=font)
        dr.text((ox + 3, 2 * fs + 6), f"{e['branch']} #{e['blob_idx']}", fill=col, font=font)
    sheet.save(out_png)
    return Path(out_png)


def _render_profile(branches, d_min_A, d_max_A, out_png) -> Path:
    """Particle-weighted mean radial profile per branch, with mask / box / measured markers."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(8.0, 4.4))
    colors = plt.cm.tab10.colors
    for bi, b in enumerate(branches):
        rows = b['sel_rows']
        if not rows:
            continue
        wsum, tot, r = None, 0.0, None
        for row in rows:
            m = b['meta'][row]
            r = np.array(m['profile']['r_A'])
            p = np.array(m['profile']['p'])
            p = p / (np.max(p) or 1.0)
            n = float(m['n_particles'])
            wsum = p * n if wsum is None else wsum + p * n
            tot += n
        if wsum is None or not tot:
            continue
        c = colors[bi % len(colors)]
        ax.plot(r, wsum / tot, lw=2.0, color=c, label=f"{b['label']} ({tot:,.0f} ptcl)")
        if b['mask_A']:
            ax.axvline(b['mask_A'] / 2, color=c, lw=1.1, ls='--')
        ax.axvline(b['box_A'] / 2, color=c, lw=1.1, ls=':')
    ax.axvline(d_max_A / 2, color='k', lw=2, label=f'measured MAX diameter {d_max_A:.0f} A')
    ax.axvline(d_min_A / 2, color='0.45', lw=2, ls='-.', label=f'measured MIN diameter {d_min_A:.0f} A')
    ax.set_xlabel('radius from centre (A)')
    ax.set_ylabel('azimuthally-averaged signal (peak-normalised)')
    ax.set_title('Radial profile per branch  (dashed = that branch\'s 2D mask, dotted = its box edge)')
    ax.legend(fontsize=7)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(str(out_png), dpi=150)
    plt.close(fig)
    return Path(out_png)


# --------------------------------------------------------------------------- #
# Config / symmetry                                                             #
# --------------------------------------------------------------------------- #

def _load_datainfo(datainfo_path=None) -> dict:
    path = datainfo_path or DATAINFO_PATH
    if not path or not Path(path).exists():
        if path:
            print(f'[diagnose2d] WARNING: base_datainfo.yaml not found at {path}')
        return {}
    try:
        import yaml
        return yaml.safe_load(open(path)) or {}
    except Exception as e:
        print(f'[diagnose2d] WARNING: could not read {path}: {e!r}')
        return {}


def symmetry_order(cfg: dict, symmetry: Optional[str] = None) -> tuple[int, str]:
    """
    Point-group order from an explicit `symmetry` or whatever base_datainfo says
    (`symmetry` field, else a C#/D#/T/O/I token in `particle_character`). Unknown -> 1 (C1).
    Used only to scale the particle-count REMINDER: at high symmetry fewer particles go further.
    """
    import re
    src = symmetry or cfg.get('symmetry') or ''
    if not src:
        src = str(cfg.get('particle_character') or '')
    m = re.search(r'\b([CD])(\d+)\b', src, re.I)
    if m:
        n = int(m.group(2))
        return (n if m.group(1).upper() == 'C' else 2 * n), f'{m.group(1).upper()}{n}'
    for tok, order in (('icos', 60), ('\\bI\\b', 60), ('octa', 24), ('\\bO\\b', 24),
                       ('tetra', 12), ('\\bT\\b', 12)):
        if re.search(tok, src, re.I):
            return order, tok.strip('\\b')
    return 1, 'C1 (assumed)'


def _mask_diameter_A(doc: dict, box_pix: int, psize_A: float) -> tuple[Optional[float], str]:
    """
    The 2D circular mask actually in force. `class2D_window_inner_A` (or `window_inner_A` on
    reference_select_2D) when set; otherwise CryoSPARC windows at `prepare_window_inner_radius`
    (default 0.85) of the box radius. Returns (diameter_A, source).
    """
    p = _params(doc)
    if p.get('class2D_window') is False:
        return None, 'class2D_window=False (no circular mask)'
    inner = p.get('class2D_window_inner_A') or p.get('window_inner_A')
    if inner:
        return float(inner), 'class2D_window_inner_A (set)'
    frac = p.get('prepare_window_inner_radius') or 0.85
    return float(frac) * box_pix * psize_A, f'default window radius {frac} x box'


# --------------------------------------------------------------------------- #
# The evidence builder                                                          #
# --------------------------------------------------------------------------- #

def diagnose_2d(proj, source_job_uid: str,
                source_group: Optional[str] = None,
                out_dir: Optional[str | Path] = None,
                datainfo_path: Optional[str | Path] = None,
                symmetry: Optional[str] = None,
                scalebar_A: float = 50.0,
                max_branches: int = 12,
                verbose: bool = True) -> dict:
    """
    Measure + render the 2D health evidence for the particle set about to enter 3D.

    Args
      source_job_uid : the job whose particles feed the FIRST 3D job — usually the final
                       `remove_duplicate_particles` / merge / `select_2D`. Every selection branch
                       behind it is traced and diagnosed; per-branch contribution to the final set
                       is measured by uid intersection.
      source_group   : which output group carries those particles (auto-detected if None).
      datainfo_path  : the ACTIVE project's base_datainfo.yaml (particle_size_A, particle_character,
                       symmetry). Falls back to module DATAINFO_PATH.
      symmetry       : override the symmetry used for the particle-count reminder (e.g. 'C2','D7').

    Returns the evidence dict consumed by build_prompt() and assess_2d().
    """
    from select2d_utils import _final_class_files

    cfg = _load_datainfo(datainfo_path)
    sym_order, sym_name = symmetry_order(cfg, symmetry)

    src_doc = _doc(proj, source_job_uid)
    if source_group is None:
        for cand in ('particles_selected', 'particles_kept', 'particles', 'split_0', 'particles_all_classes'):
            if _has_output_group(src_doc, cand):
                source_group = cand
                break
    if source_group is None:
        raise ValueError(f'{source_job_uid}: could not find a particle output group; pass source_group=')
    src_cs = _output_cs_path(proj, src_doc, source_group)
    if src_cs is None or not src_cs.exists():
        raise FileNotFoundError(f'{source_job_uid}/{source_group}: no .cs on disk ({src_cs})')
    final_uids = np.asarray(_load_fields(src_cs, ['uid'])['uid'])
    n_final = int(len(final_uids))
    final_set = set(final_uids.tolist())
    if verbose:
        print(f'[diagnose2d] final set {source_job_uid}/{source_group}: {n_final:,} particles')

    raw = trace_branches(proj, source_job_uid, verbose=verbose)[:max_branches]
    if not raw:
        raise ValueError(f'{source_job_uid}: no 2D selection branch found upstream — the particles '
                         f'never went through a 2D classification/selection, so there is nothing to '
                         f'diagnose. Run a class_2D + select first.')

    out_dir = Path(out_dir) if out_dir else Path(proj.dir()) / source_job_uid / 'diagnose_2d'
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        # relion_runs is SHARED across projects and job UIDs are only unique WITHIN a
        # project (the counter is project-wide, not per-workspace), so this fallback
        # MUST carry the project prefix — a bare `J49_diagnose_2d` is written by P1,
        # P3 and P5 alike. Same fix as select2d_utils.run_dir_name.
        out_dir = _HERE / 'relion_runs' / f'{proj.uid}_{source_job_uid}_diagnose_2d'
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f'[diagnose2d] project dir not writable — evidence in {out_dir}')

    branches, pool, panel_pngs = [], [], []
    for bi, b in enumerate(raw):
        sel_uid = b.get('select_job')
        c2d = b.get('class2d_job') or (_class2d_of_select(proj, sel_uid) if sel_uid else None)
        if not c2d:
            print(f'[diagnose2d] branch {bi}: no class_2D behind {sel_uid} — skipped')
            continue
        c2d_doc = _doc(proj, c2d)

        # kept classes + per-class counts, taken from the SELECTION's own output (works for
        # select_2D, reference_select_2D and the external-job fallback alike)
        if sel_uid:
            sel_doc = _doc(proj, sel_uid)
            sel_cs = _output_cs_path(proj, sel_doc, 'particles_selected')
        else:
            sel_doc, sel_cs = c2d_doc, _output_cs_path(proj, c2d_doc, 'particles')
        if sel_cs is None or not sel_cs.exists():
            print(f'[diagnose2d] branch {bi}: no selected-particle .cs for {sel_uid or c2d} — skipped')
            continue
        want = ['uid', 'alignments2D/class', 'alignments2D/class_ess', 'alignments2D/class_posterior']
        d_sel = _load_fields(sel_cs, want)
        fields = set(d_sel.fields())
        uids = np.asarray(d_sel['uid'])
        cls = np.asarray(d_sel['alignments2D/class']).astype(int)
        ess = np.asarray(d_sel['alignments2D/class_ess'], float) if 'alignments2D/class_ess' in fields else None
        post = np.asarray(d_sel['alignments2D/class_posterior'], float) if 'alignments2D/class_posterior' in fields else None

        in_final = np.fromiter((int(u) in final_set for u in uids), bool, len(uids))
        n_contrib = int(in_final.sum())
        if n_contrib == 0 and verbose:
            print(f'[diagnose2d] branch {bi} ({sel_uid or c2d}): contributes 0 particles to the '
                  f'final set (superseded by a later selection) — kept in the report, weight 0')

        counts, ess_by, post_by = {}, {}, {}
        for c in np.unique(cls[in_final]) if n_contrib else np.unique(cls):
            m = (cls == c) & (in_final if n_contrib else np.ones_like(in_final))
            counts[int(c)] = int(m.sum())
            if ess is not None:
                ess_by[int(c)] = float(ess[m].mean())
            if post is not None:
                post_by[int(c)] = float(post[m].mean())

        cs_path, mrc_path = _final_class_files(proj, c2d)
        with mrcfile.open(str(mrc_path), permissive=True) as m:
            data = m.data.astype(np.float32).copy()
        ca = np.load(cs_path, allow_pickle=True)
        blob_idx = ca['blob/idx'].astype(int)
        res_A = ca['blob/res_A'].astype(float)
        psize_A = float(ca['blob/psize_A'][0])
        box_pix = int(data.shape[-1])
        mask_A, mask_source = _mask_diameter_A(c2d_doc, box_pix, psize_A)
        mask_px = (mask_A / psize_A) if mask_A else None

        meta = []
        for row in range(len(data)):
            bidx = int(blob_idx[row])
            cm = class_metrics(data[row], psize_A, mask_px=mask_px)
            cm.update({'row': row, 'blob_idx': bidx, 'res_A': float(res_A[row]),
                       'n_particles': int(counts.get(bidx, 0)),
                       'ess': ess_by.get(bidx), 'posterior': post_by.get(bidx),
                       'view_group': -1, 'junk_like': False})
            meta.append(cm)
        sel_rows = sorted([m['row'] for m in meta if m['n_particles'] > 0],
                          key=lambda r: -meta[r]['n_particles'])
        if not sel_rows:
            print(f'[diagnose2d] branch {bi} ({sel_uid or c2d}): no class carries particles — skipped')
            continue

        label = f'B{len(branches)}:{sel_uid or c2d}'
        branches.append({
            'idx': len(branches), 'label': label,
            'select_job': sel_uid, 'select_type': b.get('select_type'),
            'select_title': b.get('select_title', ''), 'select_params': b.get('select_params', {}),
            'no_selection': b.get('no_selection', False),
            'class2d_job': c2d, 'class2d_title': (c2d_doc.get('title') or '')[:60],
            'class2d_params': _params(c2d_doc),
            'box_pix': box_pix, 'psize_A': psize_A, 'box_A': box_pix * psize_A,
            'nyquist_A': 2.0 * psize_A, 'mask_A': mask_A, 'mask_source': mask_source,
            'mask_px': mask_px,
            'n_selected_in_branch': int(len(uids)), 'n_contributed': n_contrib,
            'frac_of_final': n_contrib / max(n_final, 1),
            'kept_blob_idx': sorted(counts), 'n_classes': len(sel_rows),
            'data': data, 'meta': meta, 'sel_rows': sel_rows,
            'sizes_used': _upstream_sizes(proj, c2d),
        })

    if not branches:
        raise ValueError(f'{source_job_uid}: traced {len(raw)} branch(es) but none could be measured')

    # ---- dataset-level size, cleanliness, resolution -----------------------------
    allw, all_major, all_minor, all_dmax, all_res, all_ess, all_edge, all_cut, all_snr = ([] for _ in range(9))
    for b in branches:
        for row in b['sel_rows']:
            m = b['meta'][row]
            allw.append(m['n_particles'])
            all_major.append(m['d_major_A']); all_minor.append(m['d_minor_A'])
            all_dmax.append(m['d_max_A']); all_res.append(m['res_A']); all_snr.append(m['peak_snr'])
            all_edge.append(m['edge_signal_frac'])
            all_cut.append(m['mask_cut_frac'] if m['mask_cut_frac'] is not None else 0.0)
            all_ess.append(m['ess'])
    allw = np.array(allw, float)
    # MAX diameter: weighted p75 of the per-class longest extent — above the typical view, below
    # the aggregate tail. MIN diameter: weighted p25 of the per-class SHORT axis — the narrowest
    # view's narrow dimension. Together they bracket the particle.
    d_max_A = _wpct(all_dmax, allw, 0.75)
    d_max_p50, d_max_p90 = _wpct(all_dmax, allw, 0.50), _wpct(all_dmax, allw, 0.90)
    d_min_A = _wpct(all_minor, allw, 0.25)
    d_major_med = _wpct(all_major, allw, 0.50)

    # junk-like classes: objective flags only; the vision call is the deciding vote on cleanliness
    res_best = float(np.nanmin(all_res)) if np.isfinite(all_res).any() else float('nan')
    res_med_for_junk = _wpct(all_res, allw, 0.50)
    junk_mass, junk_classes = 0.0, []
    for b in branches:
        for row in b['sel_rows']:
            m = b['meta'][row]
            why = []
            if d_major_med > 0 and not (THRESHOLDS['junk_size_lo'] * d_major_med <= m['d_major_A']
                                        <= THRESHOLDS['junk_size_hi'] * d_major_med):
                why.append(f"size {m['d_major_A']:.0f}A vs median {d_major_med:.0f}A")
            if m['peak_snr'] < THRESHOLDS['junk_peak_snr']:
                why.append(f"peak SNR {m['peak_snr']:.1f}")
            if m['edge_signal_frac'] > THRESHOLDS['edge_signal_frac']:
                why.append(f"edge signal {m['edge_signal_frac']:.0%}")
            if why:
                m['junk_like'] = True
                m['junk_why'] = '; '.join(why)
                junk_mass += m['n_particles']
                junk_classes.append({'branch': b['label'], 'blob_idx': m['blob_idx'],
                                     'n': m['n_particles'], 'why': m['junk_why']})
    n_pool = float(allw.sum()) or 1.0
    junk_mass_frac = junk_mass / n_pool

    res_median = _wpct(all_res, allw, 0.50)
    nyq = max(b['nyquist_A'] for b in branches)          # coarsest branch limits what is visible
    ess_pairs = [(e, w) for e, w in zip(all_ess, allw) if e is not None]
    ess_mean = (float(np.average([e for e, _ in ess_pairs], weights=[w for _, w in ess_pairs]))
                if ess_pairs else None)

    # ---- pooled view grouping across ALL branches (common physical sampling) ------
    r_max_A = 0.62 * d_max_A
    for b in branches:
        for row in b['sel_rows']:
            m = b['meta'][row]
            pool.append({'branch': b['label'], 'blob_idx': m['blob_idx'],
                         'n': m['n_particles'], 'img': b['data'][row],
                         'desc': view_descriptor(b['data'][row], b['psize_A'], r_max_A),
                         'b': b['idx'], 'row': row})
    view = group_views([p['desc'] for p in pool], [p['n'] for p in pool])
    for gi, g in enumerate(view['groups']):
        for k in g['members']:
            branches[pool[k]['b']]['meta'][pool[k]['row']]['view_group'] = gi

    # ---- render ------------------------------------------------------------------
    for b in branches:
        t = (f"{b['label']}  {b['select_type'] or 'no select'} <- class2D {b['class2d_job']}  |  "
             f"{b['n_contributed']:,} of {n_final:,} final ({b['frac_of_final']:.0%})  |  "
             f"box {b['box_pix']}px x {b['psize_A']:.2f}A = {b['box_A']:.0f}A, "
             f"mask {b['mask_A'] and round(b['mask_A'])}A, Nyq {b['nyquist_A']:.1f}A")
        p = _render_branch_panels(b['data'], b['sel_rows'], b['meta'], b['psize_A'], b['mask_px'],
                                  t, out_dir / f"panels_{b['label'].replace(':', '_')}.png",
                                  scalebar_A=scalebar_A)
        b['panel_png'] = str(p)
        panel_pngs.append(str(p))
    views_png = _render_views(pool, view, out_dir / 'views.png')
    profile_png = _render_profile(branches, d_min_A, d_max_A, out_dir / 'radial_profile.png')

    # ---- sizes used so far, for the comparison ------------------------------------
    used = {'particle_size_A': cfg.get('particle_size_A'), 'diameters': [], 'boxes': []}
    for b in branches:
        for d in b['sizes_used']['diameters']:
            if d not in used['diameters']:
                used['diameters'].append(d)
        for bx in b['sizes_used']['boxes']:
            if bx not in used['boxes']:
                used['boxes'].append(bx)

    traced = sum(b['n_contributed'] for b in branches)
    ev = {
        'source_job': source_job_uid, 'source_group': source_group,
        'n_final': n_final, 'n_traced': traced,
        'untraced_frac': round(1.0 - traced / max(n_final, 1), 3),
        'n_branches': len(branches),
        'branches': [{k: v for k, v in b.items() if k not in ('data', 'meta', 'sel_rows')}
                     | {'classes': [{k: v for k, v in b['meta'][r].items() if k != 'profile'}
                                    for r in b['sel_rows']]}
                     for b in branches],
        'particle': {
            'd_max_A': round(d_max_A, 1), 'd_min_A': round(d_min_A, 1),
            'd_max_p50_A': round(d_max_p50, 1), 'd_max_p90_A': round(d_max_p90, 1),
            'size_spread_ratio': round(d_max_p90 / max(d_max_p50, 1e-6), 2),
            'aspect_ratio': round(d_max_A / max(d_min_A, 1e-6), 2),
            'character': cfg.get('particle_character'),
        },
        'used_sizes': used,
        'geometry': {
            'per_branch': [{'label': b['label'], 'box_A': round(b['box_A'], 1),
                            'box_ratio': round(b['box_A'] / max(d_max_A, 1e-6), 2),
                            'mask_A': b['mask_A'] and round(b['mask_A'], 1),
                            'mask_ratio': (round(b['mask_A'] / max(d_max_A, 1e-6), 2)
                                           if b['mask_A'] else None),
                            'mask_source': b['mask_source'],
                            'edge_signal_frac': round(float(np.average(
                                [b['meta'][r]['edge_signal_frac'] for r in b['sel_rows']],
                                weights=[b['meta'][r]['n_particles'] for r in b['sel_rows']])), 4),
                            'mask_cut_frac': (round(float(np.average(
                                [b['meta'][r]['mask_cut_frac'] or 0.0 for r in b['sel_rows']],
                                weights=[b['meta'][r]['n_particles'] for r in b['sel_rows']])), 4)
                                if b['mask_A'] else None),
                            'edge_contact_frac': round(float(np.average(
                                [1.0 if b['meta'][r]['edge_contact'] else 0.0 for r in b['sel_rows']],
                                weights=[b['meta'][r]['n_particles'] for r in b['sel_rows']])), 3),
                            } for b in branches],
        },
        'cleanliness': {
            'junk_mass_frac': round(junk_mass_frac, 3),
            'n_junk_classes': len(junk_classes),
            'junk_classes': sorted(junk_classes, key=lambda x: -x['n'])[:12],
        },
        'resolution': {
            'best_A': round(res_best, 2) if np.isfinite(res_best) else None,
            'weighted_median_A': round(res_median, 2),
            'nyquist_A': round(nyq, 2),
            'saturated_at_nyquist': bool(res_median <= THRESHOLDS['res_vs_nyquist_saturated'] * nyq),
        },
        'ess': {'weighted_mean': round(ess_mean, 2) if ess_mean is not None else None},
        'counts': {'n_final': n_final, 'symmetry': sym_name, 'symmetry_order': sym_order,
                   'n_effective': int(n_final * sym_order),
                   'n_classes_total': sum(b['n_classes'] for b in branches)},
        'views': {'n_groups': view['n_groups'], 'top_view_frac': round(view['top_view_frac'], 3),
                  'eff_views': round(view['eff_views'], 2),
                  'eff_classes': round(view['eff_classes'], 2),
                  'group_fracs': [round(g['n_particles'] / n_pool, 3) for g in view['groups']]},
        'panel_pngs': panel_pngs, 'views_png': str(views_png), 'profile_png': str(profile_png),
        'out_dir': str(out_dir),
    }
    if verbose:
        g0 = ev['geometry']['per_branch']
        print(f"[diagnose2d] {n_final:,} particles from {len(branches)} branch(es); "
              f"particle {d_min_A:.0f}-{d_max_A:.0f} A; "
              f"box_ratio {[x['box_ratio'] for x in g0]}; junk mass {junk_mass_frac:.0%}; "
              f"res(med) {res_median:.1f}/Nyq {nyq:.1f} A; views {view['n_groups']} "
              f"(eff {view['eff_views']:.2f}, top {view['top_view_frac']:.0%})")
        print('[diagnose2d] READ: ' + '\n              '.join(panel_pngs + [str(views_png), str(profile_png)]))
    return ev


def build_prompt(ev: dict) -> str:
    """The instruction the agent follows while looking at the rendered figures."""
    p, r, v, c = ev['particle'], ev['resolution'], ev['views'], ev['counts']
    sat = ' (SATURATED at Nyquist -> binning-limited, NOT dirt)' if r['saturated_at_nyquist'] else ''
    lines = [RUBRIC_2D, '', f"MEASURED for the set feeding 3D "
             f"({ev['source_job']}/{ev['source_group']}, {ev['n_final']:,} particles from "
             f"{ev['n_branches']} branch(es)):",
             f"  sample            : {p['character']!r}",
             f"  particle diameter : MIN {p['d_min_A']} A  -  MAX {p['d_max_A']} A "
             f"(aspect {p['aspect_ratio']}x; max p50 {p['d_max_p50_A']} / p90 {p['d_max_p90_A']})",
             f"  size USED so far  : base_datainfo particle_size_A = {ev['used_sizes']['particle_size_A']}"]
    for d in ev['used_sizes']['diameters'][:6]:
        lines.append(f"                      {d['job']} {d['type']}.{d['param']} = {d['value']}")
    for b in ev['used_sizes']['boxes'][:4]:
        lines.append(f"                      {b['job']} {b['type']}.{b['param']} = {b['value']} px")
    lines.append('  per branch:')
    for g, b in zip(ev['geometry']['per_branch'], ev['branches']):
        lines.append(f"    {g['label']:<16} {b['n_contributed']:>9,} ptcl ({b['frac_of_final']:.0%}) "
                     f"| box {g['box_A']:.0f} A = {g['box_ratio']}x particle | mask {g['mask_A']} A "
                     f"= {g['mask_ratio']}x [{g['mask_source']}] | beyond mask {g['mask_cut_frac']} "
                     f"| beyond 0.85R {g['edge_signal_frac']:.1%} | Nyq {b['nyquist_A']:.1f} A "
                     f"| {b['n_classes']} classes")
    if ev['untraced_frac'] > 0.005:
        lines.append(f"    {'(untraced)':<16} {ev['n_final']-ev['n_traced']:>9,} ptcl "
                     f"({ev['untraced_frac']:.0%}) not behind any traced selection")
    lines += [
        f"  cleanliness       : {ev['cleanliness']['junk_mass_frac']:.0%} of the particle mass sits "
        f"in {ev['cleanliness']['n_junk_classes']} junk-like classes (objective flags: bad res / odd "
        f"size / low SNR / edge signal) — RED borders on the panels",
        f"  class resolution  : best {r['best_A']} A, particle-weighted median "
        f"{r['weighted_median_A']} A, Nyquist {r['nyquist_A']} A{sat}",
        f"  ECA (mean effective classes/particle, 1 = confident): {ev['ess']['weighted_mean']}",
        f"  particles         : {c['n_final']:,}  (symmetry {c['symmetry']} -> effective "
        f"{c['n_effective']:,} asymmetric units)",
        f"  view groups       : {v['n_groups']} distinct (rotation-invariant), effective "
        f"{v['eff_views']}, largest holds {v['top_view_frac']:.0%}; fractions {v['group_fracs']}",
        '', 'READ:']
    for i, png in enumerate(ev['panel_pngs']):
        lines.append(f'  {i+1}. {png}   (branch panels: rings = measured extent / mask / 0.85R)')
    lines += [f"  {len(ev['panel_pngs'])+1}. {ev['views_png']}   (one representative per view group)",
              f"  {len(ev['panel_pngs'])+2}. {ev['profile_png']}   (radial profile per branch vs mask/box)",
              '', 'Then call assess_2d(ev, box_verdict=..., clean_verdict=..., '
              'secondary_structure=..., vision_res_A=..., distinct_views_seen=..., unsure=..., '
              'notes=...) and report its `report_text` to agentM.']
    return '\n'.join(lines)


# --------------------------------------------------------------------------- #
# The decision rule                                                             #
# --------------------------------------------------------------------------- #

_RANK = {'OK': 0, 'MINOR': 1, 'SERIOUS': 2}
_ACTIONS = {
    'HEALTHY': (1, 'Healthy / little problem — let the plan run on, no additional action.'),
    'MINOR': (2, 'Maybe a problem — run the 3D try and see the result, carrying the caveat; if the '
                 '3D result is poor, this is the first thing to revisit.'),
    'MORE_2D_CLEANING': (3, 'Particle COUNT is sufficient but the set is NOT clean enough — run '
                            'more 2D classify -> select rounds (and/or CryoRanker) to purify it '
                            'before the 3D try, rather than reconstructing from junk.'),
    'SERIOUS': (4, 'HALT subC\'s 3D try — re-run with the corrected parameter. Either label the '
                   'original branch NEGATIVE from the offending job, or keep it positive and open '
                   'a corrected branch alongside it.'),
    'ASK_HUMAN': (5, 'Ambiguous / contradictory evidence — ask the human to inspect the panels and '
                     'instruct before spending GPU time on the 3D try.'),
}


def _group_msgs(pairs: list[tuple[str, str]], n_branches: int) -> list[str]:
    """Collapse identical per-branch findings: 'all 3 branches: ...' instead of one line each."""
    order, byline = [], {}
    for lab, msg in pairs:
        if msg not in byline:
            byline[msg] = []
            order.append(msg)
        byline[msg].append(lab)
    out = []
    for msg in order:
        labs = byline[msg]
        who = (f'all {n_branches} branches' if len(labs) == n_branches and n_branches > 1
               else ', '.join(labs))
        out.append(f'{who}: {msg}' if who else msg)
    return out


def _parse_size_A(value):
    """Parse base_datainfo `particle_size_A` into (lo, hi) in Angstrom, or None.

    The field is free-form in practice: users write a single number (`140`) OR a RANGE
    (`"100-160"`), which is legitimate when the particle is elongated or the size is not yet
    pinned down. `float()` crashes on the range form, which took out the whole box check on
    the AT1R project (particle_size_A: "100-160") until it was worked around in-memory.

    Returns (lo, hi) with lo == hi for a scalar, so callers can use `hi` wherever they used to
    use the scalar. Returns None for empty/unparseable input rather than raising — a missing
    declared size must not stop the measured-diameter checks from running.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return (v, v)
    s = str(value).strip().replace('A', '').replace('Å', '').strip()
    if not s:
        return None
    m = re.match(r'^\s*([0-9]*\.?[0-9]+)\s*(?:-|–|to|~)\s*([0-9]*\.?[0-9]+)\s*$', s)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return (min(lo, hi), max(lo, hi))
    try:
        v = float(s)
        return (v, v)
    except ValueError:
        return None


def _check_box(ev, box_verdict):
    """1. box size + 2D mask vs the white particle density, per branch."""
    T, p = THRESHOLDS, ev['particle']
    v, pairs, why = 'OK', [], []

    def add(lab, msg):
        pairs.append((lab, msg))

    for g in ev['geometry']['per_branch']:
        br, mr, lab = g['box_ratio'], g['mask_ratio'], g['label']
        # A mask that CUTS is measured directly: positive signal mass outside the mask radius.
        if g['mask_cut_frac'] is not None and g['mask_cut_frac'] > T['mask_cut_frac']:
            v = 'SERIOUS'
            add(lab, f"{g['mask_cut_frac']:.1%} of the signal lies OUTSIDE the 2D mask "
                       f"({g['mask_A']} A) — the mask is CUTTING the particle "
                       f"({p['d_max_A']} A across).")
        # Clipping by the box needs evidence at the boundary, not just a small ratio: the size
        # estimate can be inflated by aggregates, and a false SERIOUS costs a re-plan cycle.
        clipping = (g['edge_signal_frac'] > T['edge_signal_frac']
                    or g['edge_contact_frac'] > T['edge_contact_frac'])
        if g['edge_signal_frac'] > T['edge_signal_frac'] or (br < T['box_ratio_tight'] and clipping):
            v = 'SERIOUS'
            add(lab, f"box {g['box_A']:.0f} A is {br}x the particle and "
                       f"{g['edge_signal_frac']:.1%} of the signal sits beyond 0.85R "
                       f"({g['edge_contact_frac']:.0%} of classes touch the edge) — density is being "
                       f"CLIPPED (CTF delocalization cut, aliasing). Box TOO TIGHT.")
        elif br < T['box_ratio_tight']:
            v = max(v, 'MINOR', key=lambda s: _RANK[s])
            add(lab, f"box is only {br}x the particle (healthy 1.5-2.5x) but no signal "
                       f"reaches the boundary ({g['edge_signal_frac']:.1%} beyond 0.85R), so nothing "
                       f"is clipped yet — enlarge box_size_pix at the full-size re-extract.")
        elif br < T['box_ratio_minor_small']:
            v = max(v, 'MINOR', key=lambda s: _RANK[s])
            add(lab, f"box is tight ({br}x) — usable now, set a larger box at the "
                       f"full-size re-extract.")
        elif br > T['box_ratio_serious_large']:
            v = 'SERIOUS'
            add(lab, f"box is {br}x the particle — a small dot in a large noisy field; "
                       f"neighbouring particles fall inside the window and contaminate the "
                       f"averages. Box TOO LOOSE.")
        elif br > T['box_ratio_minor_large']:
            v = max(v, 'MINOR', key=lambda s: _RANK[s])
            add(lab, f"box is generous ({br}x) — wasted compute and some neighbour risk.")

        # The mask verdict. A LOOSE mask is only an independent finding when the plan explicitly
        # SET it: CryoSPARC's default is 0.85 x box, so on any healthy box (1.5-2.5x particle) the
        # default is 1.3-2.1x by construction — flagging that fires on nearly every job and merely
        # restates the box ratio (measured: 7 of 21 historical sets, all with healthy boxes).
        mask_is_default = '(set)' not in (g['mask_source'] or '')
        if mr is None:
            if 'class2D_window=False' in (g['mask_source'] or ''):
                add(lab, f"2D ran with NO circular mask (class2D_window=False) — nothing "
                           f"excluded neighbouring-particle signal; judge contamination on the images.")
        elif mr < T['mask_ratio_serious_small']:
            v = 'SERIOUS'
            add(lab, f"the 2D mask ({g['mask_A']} A) is SMALLER than the particle "
                       f"({p['d_max_A']} A) — it is cutting the density.")
        elif mr < T['mask_ratio_minor_small']:
            v = max(v, 'MINOR', key=lambda s: _RANK[s])
            add(lab, f"the mask ({g['mask_A']} A) barely clears the particle ({mr}x) — no "
                       f"room for a soft edge.")
        elif mr > T['mask_ratio_serious_large'] and not mask_is_default:
            v = 'SERIOUS'
            add(lab, f"the mask was SET at {mr}x the particle — it admits neighbouring "
                       f"particles into every average.")
        elif mr > T['mask_ratio_minor_large'] and not mask_is_default:
            v = max(v, 'MINOR', key=lambda s: _RANK[s])
            add(lab, f"the mask was SET loose ({mr}x) — ~1.2-1.4x is tighter without clipping.")

    why.extend(_group_msgs(pairs, len(ev['geometry']['per_branch'])))

    if p.get('size_spread_ratio', 1) > T['size_spread_ratio']:
        why.append(f"NOTE: class extents spread {p['size_spread_ratio']}x (p50 {p['d_max_p50_A']} A "
                   f"-> p90 {p['d_max_p90_A']} A) — aggregates/multi-particle classes are still in "
                   f"the set, so the size numbers read high.")

    # Measured MIN/MAX diameter vs what has actually been USED so far.
    # A picker's `diameter` / `diameter_max` are a WINDOW, not a single size — the minimum is meant
    # to sit below the particle. So the question is whether that window BRACKETS the measured
    # particle, not whether either end equals it. (Comparing a blob picker's min diameter against
    # the measured MAX flagged half the history spuriously.) A single declared size
    # (`particle_size_A`, a template/topaz `diameter`) is compared to the measured MAX with the
    # standard tolerance.
    used, tol = ev['used_sizes'], T['declared_size_mismatch']
    by_job: dict[str, dict] = {}
    for d in used.get('diameters', []):
        e = by_job.setdefault(d['job'], {'type': d['type'], 'min': None, 'max': None})
        if 'max' in d['param']:
            e['max'] = float(d['value'])
        else:
            e['min'] = float(d['value'])
    refs, bad = [], []
    _rng = _parse_size_A(used.get('particle_size_A'))
    if _rng:
        lo, hi = _rng
        # Compare the measured MAX diameter against the UPPER end of a declared range: the lower
        # end describes the particle's narrow axis and is not what d_max_A measures.
        val = hi
        label = f'{lo:g}' if lo == hi else f'{lo:g}-{hi:g}'
        refs.append(f'base_datainfo particle_size_A={label}')
        if abs(p['d_max_A'] - val) / max(val, 1e-6) > tol:
            bad.append(f'particle_size_A={label} A is {abs(p["d_max_A"]-val)/val:.0%} off the '
                       f'measured MAX diameter')
    for job, e in list(by_job.items())[:6]:
        if e['min'] is not None and e['max'] is not None:
            refs.append(f"{job} {e['type']} window {e['min']:g}-{e['max']:g} A")
            # Only the UPPER end is checked. A picker's minimum diameter applies to the apparent
            # blob and is meant to sit below the particle, so "min > the particle's narrow
            # dimension" fires on any elongated particle and means nothing (it flagged three
            # historical sets with healthy view coverage). A particle LARGER than the window's top,
            # though, is genuinely being mis-picked.
            if p['d_max_A'] > 1.2 * e['max']:
                bad.append(f"{job}'s picking window tops out at {e['max']:g} A but the particle "
                           f"measures {p['d_max_A']:.0f} A across — it was picking too small")
        else:
            val = e['min'] if e['min'] is not None else e['max']
            refs.append(f"{job} {e['type']} diameter={val:g} A")
            if val and abs(p['d_max_A'] - val) / max(val, 1e-6) > tol:
                bad.append(f"{job} used diameter {val:g} A vs the measured MAX {p['d_max_A']:.0f} A")
    if refs:
        why.append(f"SIZE: measured MIN {p['d_min_A']} A / MAX {p['d_max_A']} A "
                   f"(aspect {p['aspect_ratio']}x) vs used [" + '; '.join(refs) + '].')
    if bad:
        v = max(v, 'MINOR', key=lambda s: _RANK[s])
        why.append('MISMATCH: ' + '; '.join(bad) + f". Correct the picking diameter to ~"
                   f"{p['d_max_A']:.0f} A (box ~{1.8*p['d_max_A']:.0f} A = "
                   f"{int(round(1.8*p['d_max_A']/max(ev['branches'][0]['psize_A'],1e-6)))} px at "
                   f"{ev['branches'][0]['psize_A']:.2f} A/px) and record it in base_datainfo.yaml.")

    conflict = False
    if box_verdict in ('too_small', 'too_large'):
        conflict = (v == 'OK')
        v = 'SERIOUS'
        why.append(f"VISION: box/mask judged TOO {box_verdict.split('_')[1].upper()} on the panels "
                   f"(smeared/clipped/mask-cut lobe, or empty field / neighbour fragments inside "
                   f"the mask) — this is the deciding vote.")
    elif box_verdict == 'ok' and v == 'SERIOUS':
        conflict = True
        why.append('VISION: the panels look fine despite the ratios — CONFLICT with the numbers.')
    if not why:
        why.append('box and mask appropriate on every branch; no signal at the boundary.')
    return {'verdict': v, 'reason': ' '.join(why), 'conflict': conflict,
            'numbers': {'per_branch': ev['geometry']['per_branch'],
                        'd_min_A': p['d_min_A'], 'd_max_A': p['d_max_A']}}


def _check_clean(ev, clean_verdict, secondary_structure, vision_res_A):
    """2. is the set CLEAN enough and a 3D candidate?"""
    T, r = THRESHOLDS, ev['resolution']
    med, nyq, jm = r['weighted_median_A'], r['nyquist_A'], ev['cleanliness']['junk_mass_frac']
    v, why = 'OK', []

    if r['saturated_at_nyquist']:
        why.append(f"class resolution (median {med} A) is at the extraction Nyquist ({nyq} A) — the "
                   f"estimate is BINNING-limited and says nothing about cleanliness; judge on the "
                   f"images (and re-extract less binned to see more).")
    elif med > T['res_serious_A']:
        v = 'SERIOUS'
        why.append(f"particle-weighted median class resolution {med} A (best {r['best_A']} A) with "
                   f"Nyquist at {nyq} A — the particles did NOT align to a usable level.")
    elif med > T['res_minor_A']:
        v = 'MINOR'
        why.append(f"median class resolution {med} A (best {r['best_A']} A, Nyquist {nyq} A) — "
                   f"marginal; a 3D try may work but expect a low-resolution start.")
    else:
        why.append(f"median class resolution {med} A (best {r['best_A']} A, Nyquist {nyq} A) — "
                   f"the particles aligned well.")

    if jm > T['junk_mass_serious']:
        v = max(v, 'MINOR', key=lambda s: _RANK[s])
        why.append(f"{jm:.0%} of the particle mass is in junk-like classes "
                   f"({ev['cleanliness']['n_junk_classes']} of them) — the set is NOT clean; more "
                   f"2D cleaning is indicated before 3D.")
    elif jm > T['junk_mass_minor']:
        v = max(v, 'MINOR', key=lambda s: _RANK[s])
        why.append(f"{jm:.0%} of the particle mass is in junk-like classes — some junk survived "
                   f"the selection.")

    ess = ev['ess']['weighted_mean']
    if ess is not None:
        if ess > T['ess_serious']:
            v = 'SERIOUS'
            why.append(f"mean ECA {ess} — particles are spread over many classes, i.e. not "
                       f"confidently assigned (low SNR / junk-dominated).")
        elif ess > T['ess_minor'] and v == 'OK':
            v = 'MINOR'
            why.append(f"mean ECA {ess} — assignments are soft; classes are partly mixtures.")

    conflict, dirty = False, False
    if secondary_structure is False:
        conflict = (v == 'OK')
        v = 'SERIOUS'
        why.append('VISION: the dominant views show NO secondary structure / no internal detail — '
                   'not a 3D candidate regardless of the reported class resolution.')
    elif secondary_structure is True and v == 'SERIOUS' and not r['saturated_at_nyquist']:
        conflict = True
        why.append('VISION: the dominant views DO show secondary structure — CONFLICT with the '
                   'numeric resolution/ECA.')
    if clean_verdict == 'dirty':
        dirty = True
        v = max(v, 'MINOR', key=lambda s: _RANK[s])
        why.append('VISION: the set is DIRTY (junk classes carry a large share, or more than one '
                   'kind of object is present) — clean further before reconstructing.')
    elif clean_verdict == 'some_junk':
        dirty = True
        v = max(v, 'MINOR', key=lambda s: _RANK[s])
        why.append('VISION: SOME JUNK survives the selection — another cleaning round is worthwhile.')
    elif clean_verdict == 'clean' and jm > T['junk_mass_serious']:
        why.append('VISION: judged clean despite the junk-mass flags (the objective flags catch '
                   'legitimate rare/odd views too) — the eye decides.')
    elif clean_verdict == 'clean':
        why.append('VISION: the set is clean.')
    if vision_res_A is not None:
        why.append(f'VISION: detail level eyeballed at ~{vision_res_A:g} A.')
    return {'verdict': v, 'reason': ' '.join(why), 'conflict': conflict, 'dirty': dirty,
            'numbers': {'res_median_A': med, 'res_best_A': r['best_A'], 'nyquist_A': nyq,
                        'junk_mass_frac': jm, 'ess': ess, 'vision_res_A': vision_res_A}}


def _check_count(ev):
    """3. particle count — ADVISORY reminder about enriching. Never SERIOUS on its own."""
    T, c = THRESHOLDS, ev['counts']
    n, ne, sym = c['n_final'], c['n_effective'], c['symmetry']
    v, why, enrich = 'OK', [], False
    symtxt = (f' ({n:,} x {sym} = {ne:,} asymmetric units)' if c['symmetry_order'] > 1 else '')
    if ne < T['n_low']:
        v, enrich = 'MINOR', True
        why.append(f"{n:,} particles{symtxt} — thin. A few thousand CAN start an ab-initio "
                   f"(especially at high symmetry), so this does not block the 3D try, but treat it "
                   f"as a REMINDER: enrich the pool (more pickers/diameters/topaz, more "
                   f"micrographs, recombine cleaned branches + dedup) before pushing resolution.")
    elif ne < T['n_enrich_reminder']:
        enrich = True
        why.append(f"{n:,} particles{symtxt} — enough to try 3D, but below the "
                   f"{T['n_enrich_reminder']:,} that makes a refine comfortable: REMINDER to enrich "
                   f"the pool in parallel.")
    else:
        why.append(f"{n:,} particles{symtxt} — sufficient for a 3D try.")
    if ev['untraced_frac'] > T['untraced_frac']:
        why.append(f"NOTE: {ev['untraced_frac']:.0%} of the final set is not behind any traced 2D "
                   f"selection — those particles were never 2D-screened, or the lineage walk hit a "
                   f"job type it does not follow. Check where they came from.")
    return {'verdict': v, 'reason': ' '.join(why), 'enrich': enrich,
            'numbers': {'n_final': n, 'n_effective': ne, 'symmetry': sym,
                        'untraced_frac': ev['untraced_frac']}}


def _check_views(ev, distinct_views_seen):
    """4. is the set confined to too narrow a range of views?"""
    T, v_ = THRESHOLDS, ev['views']
    top, eff = v_['top_view_frac'], v_['eff_views']
    v, why = 'OK', []
    # The NUMBERS alone never reach SERIOUS: 2D class similarity is a weak proxy for 3D angular
    # coverage (different directions of a globular particle give similar outlines, and this
    # phase-/mirror-blind descriptor merges them). Measured on 21 historical pre-3D sets, a
    # numeric-only "severe preferred orientation" was WRONG 3 times out of 5 (those sets reached
    # 2.59 / 2.86 / 3.02 A). So concentrated mass caps at MINOR and the VISION call decides.
    if top > T['top_view_frac_serious'] or eff < T['eff_views_serious']:
        v = 'MINOR'
        why.append(f"{top:.0%} of particles sit in ONE view group and only {eff} effective views "
                   f"({v_['n_groups']} groups) — that LOOKS like severe preferred orientation, and "
                   f"a 3D map from it would be smeared along the missing direction. Numbers alone "
                   f"do not settle it: confirm on the panels, and check cFAR / ELONGATED after the "
                   f"refine.")
    elif top > T['top_view_frac_minor'] or eff < T['eff_views_minor']:
        v = 'MINOR'
        why.append(f"view distribution is skewed: largest group {top:.0%}, {eff} effective views — "
                   f"workable but watch for anisotropy after the refine.")
    else:
        why.append(f"{v_['n_groups']} view groups, {eff} effective, largest {top:.0%} — reasonable "
                   f"orientation coverage.")
    if ev['counts']['symmetry_order'] > 1:
        why.append(f"(symmetry {ev['counts']['symmetry']} — fewer distinct views are needed than "
                   f"for C1.)")

    conflict = False
    if distinct_views_seen is not None:
        why.append(f'VISION: {distinct_views_seen} genuinely different views counted on the panels.')
        if distinct_views_seen <= 2:
            conflict = (v == 'OK')
            v = 'SERIOUS'
            why.append('VISION: essentially a single view family — the deciding vote; the set is '
                       'NOT reconstructable as it stands (re-pick for rare views, tilt, or rescue '
                       'the missing cone).')
        elif distinct_views_seen >= 5 and v == 'MINOR':
            v = 'OK'
            why.append('VISION clears it: several genuinely distinct views are present, so the '
                       'concentrated mass is not a coverage failure (the descriptor merges '
                       'mirror-related views, so it UNDER-counts by construction).')
    return {'verdict': v, 'reason': ' '.join(why), 'conflict': conflict,
            'numbers': {'n_groups': v_['n_groups'], 'top_view_frac': top, 'eff_views': eff,
                        'distinct_views_seen': distinct_views_seen}}


def assess_2d(ev: dict,
              box_verdict: Optional[str] = None,
              clean_verdict: Optional[str] = None,
              secondary_structure: Optional[bool] = None,
              vision_res_A: Optional[float] = None,
              distinct_views_seen: Optional[int] = None,
              unsure: bool = False,
              notes: str = '') -> dict:
    """
    Combine the measured numbers with the agent's VISION call into one verdict + the action agentM
    should take.

    Vision args (all optional — omit one and that check runs on numbers alone):
      box_verdict         : 'ok' | 'too_small' | 'too_large' — your read of the white density
                            against the drawn rings and the scale bar, knowing what the sample is.
      clean_verdict       : 'clean' | 'some_junk' | 'dirty' — how much junk survives the selection.
      secondary_structure : do the DOMINANT views show secondary structure / internal detail?
      vision_res_A        : detail level you eyeball in the best views (A), recorded for the log.
      distinct_views_seen : how many genuinely different views you count (not in-plane rotations).
      unsure              : True when you cannot decide — routes to ASK_HUMAN instead of a guess.

    Returns {status, agentM_action{code,text}, checks{...}, flags[], summary, report_text,
             report_path, subc_return, notes}
      status : 'HEALTHY' | 'MINOR' | 'MORE_2D_CLEANING' | 'SERIOUS' | 'ASK_HUMAN'
    """
    if box_verdict not in (None, 'ok', 'too_small', 'too_large'):
        raise ValueError("box_verdict must be None | 'ok' | 'too_small' | 'too_large'")
    if clean_verdict not in (None, 'clean', 'some_junk', 'dirty'):
        raise ValueError("clean_verdict must be None | 'clean' | 'some_junk' | 'dirty'")

    checks = {
        'box_and_mask': _check_box(ev, box_verdict),
        'cleanliness': _check_clean(ev, clean_verdict, secondary_structure, vision_res_A),
        'particle_count': _check_count(ev),
        'orientation_coverage': _check_views(ev, distinct_views_seen),
    }
    worst = max(checks.values(), key=lambda c: _RANK[c['verdict']])['verdict']
    status = {'OK': 'HEALTHY', 'MINOR': 'MINOR', 'SERIOUS': 'SERIOUS'}[worst]

    # "Enough particles but not clean" is its own outcome: more 2D cleaning, not a re-plan and not
    # a blind 3D try. It outranks MINOR but never overrides a SERIOUS finding (a clipped box has to
    # be fixed first — cleaning a badly-boxed stack does not help).
    dirty = checks['cleanliness'].get('dirty') or \
        ev['cleanliness']['junk_mass_frac'] > THRESHOLDS['junk_mass_serious']
    count_ok = not checks['particle_count'].get('enrich')
    if status != 'SERIOUS' and dirty and count_ok:
        status = 'MORE_2D_CLEANING'

    # A SERIOUS call where the numbers and the eye disagree is not a decision to automate.
    # `unsure` escalates too — but NOT over an already-SERIOUS finding, where halting and
    # re-planning is right regardless; the flag still records the open question.
    conflicts = [k for k, c in checks.items() if c.get('conflict') and c['verdict'] == 'SERIOUS']
    if conflicts or (unsure and status != 'SERIOUS'):
        status = 'ASK_HUMAN'

    flags = [f'{k.upper()}_{c["verdict"]}' for k, c in checks.items() if c['verdict'] != 'OK']
    if checks['particle_count'].get('enrich'):
        flags.append('ENRICH_PARTICLES')
    if ev['untraced_frac'] > THRESHOLDS['untraced_frac']:
        flags.append('UNTRACED_PARTICLES')
    if conflicts:
        flags.append('NUMBERS_VS_VISION_CONFLICT:' + ','.join(conflicts))
    if unsure:
        flags.append('VISION_UNSURE')

    code, action_text = _ACTIONS[status]
    p, v = ev['particle'], ev['views']
    summary = (f"{ev['source_job']}/{ev['source_group']}: {ev['n_final']:,} particles from "
               f"{ev['n_branches']} branch(es) ({ev['counts']['symmetry']}); particle "
               f"{p['d_min_A']}-{p['d_max_A']} A; box_ratio "
               f"{[g['box_ratio'] for g in ev['geometry']['per_branch']]}; junk mass "
               f"{ev['cleanliness']['junk_mass_frac']:.0%}; class res median "
               f"{ev['resolution']['weighted_median_A']} A (Nyq {ev['resolution']['nyquist_A']} A); "
               f"{v['n_groups']} view groups (eff {v['eff_views']}, top {v['top_view_frac']:.0%})")

    lines = [f'2D HEALTH DIAGNOSIS (pre-3D gate) — {ev["source_job"]} — STATUS: {status}',
             f'agentM action {code}: {action_text}', '', summary, '']
    for g, b in zip(ev['geometry']['per_branch'], ev['branches']):
        lines.append(f"  {g['label']:<16} {b['n_contributed']:>9,} ptcl ({b['frac_of_final']:.0%}) "
                     f"<- {b['select_type'] or 'no select'} / class2D {b['class2d_job']} "
                     f"| box {g['box_A']:.0f}A={g['box_ratio']}x | mask {g['mask_A']}A | "
                     f"{b['n_classes']} classes")
    lines.append('')
    for k, c in checks.items():
        lines.append(f'[{c["verdict"]:<7}] {k}: {c["reason"]}')
    lines += ['', 'Evidence: ' + ' | '.join(ev['panel_pngs'] + [ev['views_png'], ev['profile_png']])]
    if notes:
        lines += ['', f'Notes: {notes}']
    report_text = '\n'.join(lines)

    report_path = None
    try:
        report_path = Path(ev['out_dir']) / 'diagnosis.md'
        report_path.write_text(report_text + '\n')
    except Exception as e:
        print(f'[diagnose2d] could not write the report file: {e!r}')
    where = f' Full report: {report_path}' if report_path else ''

    if status == 'SERIOUS':
        bad = '; '.join(f'{k} — {checks[k]["reason"].split(". ")[0].rstrip(".")}.'
                        for k, c in checks.items() if c['verdict'] == 'SERIOUS')
        subc_return = (f'HALT — REPORT: 2D health diagnosis on {ev["source_job"]} before the first '
                       f'3D job — SERIOUS: {bad} | {summary} |{where}')
    elif status == 'ASK_HUMAN':
        subc_return = (f'HALT — REPORT: 2D health diagnosis on {ev["source_job"]} is AMBIGUOUS '
                       f'({", ".join(flags)}) — the numbers and the visual read disagree, or the '
                       f'call could not be made. agentM should ask the human to inspect '
                       f'{ev["panel_pngs"][0]}. | {summary} |{where}')
    elif status == 'MORE_2D_CLEANING':
        subc_return = (f'HALT — REPORT: 2D health diagnosis on {ev["source_job"]} — MORE 2D CLEANING '
                       f'NEEDED: the count is sufficient ({ev["n_final"]:,}) but the set is not '
                       f'clean ({checks["cleanliness"]["reason"].split(". ")[0].rstrip(".")}). '
                       f'agentM: add 2D classify -> select rounds (and/or CryoRanker) before 3D. '
                       f'| {summary} |{where}')
    else:
        subc_return = (f'DIAGNOSE-2D {status} ({", ".join(flags) if flags else "no flags"}): '
                       f'{summary} — proceeding to 3D'
                       + (' with the caveat above.' if status == 'MINOR' else '.') + where)

    print(report_text)
    return {'status': status, 'agentM_action': {'code': code, 'text': action_text},
            'checks': checks, 'flags': flags, 'summary': summary, 'report_text': report_text,
            'report_path': str(report_path) if report_path else None,
            'subc_return': subc_return, 'notes': notes}


def decisions_log_block(ev: dict, rep: dict) -> str:
    """A ready-to-append block for `runs/subC_decisions.md` (subC's context is wiped after)."""
    br = '\n'.join(
        f"  - {g['label']} {b['select_type'] or 'no select'} <- class2D {b['class2d_job']}: "
        f"{b['n_contributed']:,} ptcl ({b['frac_of_final']:.0%}), box {g['box_A']:.0f} A "
        f"({g['box_ratio']}x), mask {g['mask_A']} A [{g['mask_source']}], {b['n_classes']} classes"
        for g, b in zip(ev['geometry']['per_branch'], ev['branches']))
    return f"""
### 2D health diagnosis — {ev['source_job']}/{ev['source_group']} (pre-3D gate)
- Status: **{rep['status']}** -> agentM action {rep['agentM_action']['code']}
- Set diagnosed: {ev['n_final']:,} particles, {ev['n_branches']} branch(es), untraced {ev['untraced_frac']:.0%}
{br}
- Particle: MIN {ev['particle']['d_min_A']} A / MAX {ev['particle']['d_max_A']} A (aspect {ev['particle']['aspect_ratio']}x);
  used so far: particle_size_A={ev['used_sizes']['particle_size_A']}, """ + \
        ', '.join(f"{d['job']}.{d['param']}={d['value']}" for d in ev['used_sizes']['diameters'][:4]) + f"""
- Cleanliness: junk mass {ev['cleanliness']['junk_mass_frac']:.0%} in {ev['cleanliness']['n_junk_classes']} classes;
  class res best {ev['resolution']['best_A']} / median {ev['resolution']['weighted_median_A']} A, Nyquist {ev['resolution']['nyquist_A']} A, ECA {ev['ess']['weighted_mean']}
- Count: {ev['counts']['n_final']:,} ({ev['counts']['symmetry']} -> {ev['counts']['n_effective']:,} ASU)
- Views: {ev['views']['n_groups']} groups, effective {ev['views']['eff_views']}, top {ev['views']['top_view_frac']:.0%}
- Flags: {', '.join(rep['flags']) or 'none'}
- Per-check: """ + ' '.join(f"{k}={c['verdict']}" for k, c in rep['checks'].items()) + f"""
- Evidence: {', '.join(ev['panel_pngs'] + [ev['views_png'], ev['profile_png']])}
- Notes: {rep['notes'] or '-'}
"""


# --------------------------------------------------------------------------- #
# Offline: measure + render straight from a class-average stack (no CryoSPARC)  #
# --------------------------------------------------------------------------- #

def diagnose_from_stack(mrcs_path: str | Path, psize_A: Optional[float] = None,
                        counts: Optional[list] = None, out_dir: Optional[str | Path] = None,
                        mask_A: Optional[float] = None) -> dict:
    """Measurements + renders from a bare class-average stack — for testing the tool, or for
    averages that came from elsewhere. Counts default to equal weighting."""
    mrcs_path = Path(mrcs_path)
    with mrcfile.open(str(mrcs_path), permissive=True) as m:
        data = m.data.astype(np.float32).copy()
        if psize_A is None:
            psize_A = float(m.voxel_size.x) or 1.0
    box_pix = int(data.shape[-1])
    counts = list(counts) if counts is not None else [1] * len(data)
    mask_px = (mask_A / psize_A) if mask_A else None

    meta = []
    for row in range(len(data)):
        cm = class_metrics(data[row], psize_A, mask_px=mask_px)
        cm.update({'row': row, 'blob_idx': row, 'res_A': float('nan'),
                   'n_particles': int(counts[row]), 'ess': None, 'posterior': None,
                   'view_group': -1, 'junk_like': False})
        meta.append(cm)
    rows = sorted(range(len(data)), key=lambda r: -meta[r]['n_particles'])
    cnt = np.array([meta[r]['n_particles'] for r in rows], float)
    d_max_A = _wpct([meta[r]['d_max_A'] for r in rows], cnt, 0.75)
    d_min_A = _wpct([meta[r]['d_minor_A'] for r in rows], cnt, 0.25)

    r_max_A = 0.62 * d_max_A
    view = group_views([view_descriptor(data[r], psize_A, r_max_A) for r in rows], cnt)
    for gi, g in enumerate(view['groups']):
        for k in g['members']:
            meta[rows[k]]['view_group'] = gi
    pool = [{'branch': 'B0', 'blob_idx': meta[r]['blob_idx'], 'n': meta[r]['n_particles'],
             'img': data[r]} for r in rows]

    out_dir = Path(out_dir or mrcs_path.parent / 'diagnose_2d')
    out_dir.mkdir(parents=True, exist_ok=True)
    panel = _render_branch_panels(data, rows, meta, psize_A, mask_px,
                                  f'{mrcs_path.name}  {box_pix}px x {psize_A:.2f}A',
                                  out_dir / 'panels.png')
    views_png = _render_views(pool, view, out_dir / 'views.png')
    prof = _render_profile([{'label': 'B0', 'sel_rows': rows, 'meta': meta, 'mask_A': mask_A,
                             'box_A': box_pix * psize_A}], d_min_A, d_max_A,
                           out_dir / 'radial_profile.png')
    return {'psize_A': psize_A, 'box_pix': box_pix,
            'd_min_A': round(d_min_A, 1), 'd_max_A': round(d_max_A, 1),
            'box_ratio': round(box_pix * psize_A / max(d_max_A, 1e-6), 2),
            'views': {'n_groups': view['n_groups'], 'eff_views': round(view['eff_views'], 2),
                      'top_view_frac': round(view['top_view_frac'], 3)},
            'classes': [{k: v for k, v in meta[r].items() if k != 'profile'} for r in rows],
            'panel_png': str(panel), 'views_png': str(views_png), 'profile_png': str(prof),
            'out_dir': str(out_dir)}


if __name__ == '__main__':
    import json
    if len(sys.argv) < 2:
        print('usage: python diagnose2d_utils.py <class_averages.mrcs> [psize_A] [out_dir]')
        raise SystemExit(2)
    ps = float(sys.argv[2]) if len(sys.argv) > 2 else None
    od = sys.argv[3] if len(sys.argv) > 3 else None
    out = diagnose_from_stack(sys.argv[1], psize_A=ps, out_dir=od)
    print(json.dumps({k: v for k, v in out.items() if k != 'classes'}, indent=2))
    print(f'\n{len(out["classes"])} classes measured; READ {out["panel_png"]}')
