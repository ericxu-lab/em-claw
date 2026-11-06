"""
DBRV step 1 — find the RARE viewing directions FROM THE REFINEMENT, and project the map along them.

Not "take a generic create_templates set and guess which ones look end-on". Instead:
  1. read the refinement's per-particle `alignments3D/pose` (axis-angle) -> viewing direction per particle
  2. bin those directions on a near-uniform hemisphere grid -> the measured orientation distribution
  3. the UNDER-POPULATED bins ARE the rare views (data-driven; no template guessing)
  4. project the refined map along exactly those directions -> "this is what the missing views look like"

The projections from (4) are the reference for going back through the 2D selections.

Usage:
    from dbrv.rare_view_projections import viewing_directions, sphere_bins, rare_directions, project_map
    v, R = viewing_directions(proj, 'J548')
    b = sphere_bins(v, n_bins=384)
    rare = rare_directions(b, frac=0.05)
    imgs, psize = project_map(proj, 'J548', rare['dirs'], lowpass_A=12.0)
"""

from __future__ import annotations

import os
import numpy as np


# ---------------------------------------------------------------------------------------------
# 1. viewing directions from the refinement
# ---------------------------------------------------------------------------------------------

def _rodrigues(rvec):
    """(N,3) axis-angle -> (N,3,3) rotation matrices."""
    theta = np.linalg.norm(rvec, axis=1)
    k = np.zeros_like(rvec)
    nz = theta > 1e-12
    k[nz] = rvec[nz] / theta[nz, None]
    K = np.zeros((len(rvec), 3, 3), dtype=np.float64)
    K[:, 0, 1], K[:, 0, 2] = -k[:, 2], k[:, 1]
    K[:, 1, 0], K[:, 1, 2] = k[:, 2], -k[:, 0]
    K[:, 2, 0], K[:, 2, 1] = -k[:, 1], k[:, 0]
    I = np.broadcast_to(np.eye(3), (len(rvec), 3, 3))
    s = np.sin(theta)[:, None, None]
    c = (1.0 - np.cos(theta))[:, None, None]
    return I + s * K + c * (K @ K)


def viewing_directions(proj, job_uid, transpose=False, fold_hemisphere=True):
    """Per-particle unit viewing direction from a refinement's alignments3D.

    ⚠⚠ DEFAULT CHANGED 2026-09-22 (jyf1): `transpose` now defaults to **False** — the viewing
    direction is `R[:, :, 2]`, the third **COLUMN**. The old `transpose=True` default (`R[:, 2, :]`,
    third ROW) is WRONG for CryoSPARC `alignments3D` and **silently returns FAKE ISOTROPY**.
    Measured on jyf1 J2634: ROW reads eff/N **0.9173** (azimuth histogram flat 2.59-2.86 %, i.e.
    "isotropic") while COLUMN reads **0.3560** (bimodal 0.57 -> 9.78 %, one great-circle band),
    matching the independently known effective-views of 1.54. A 2.6x error that INVERTS the
    conclusion — and it does not raise, it just reports isotropy.

    🔒 **DO NOT verify this with `elevation_census` — it CANNOT detect the bug.** Both conventions
    compute |v.z| = R[2,2], the one matrix element a transpose leaves fixed, so the census is
    BYTE-IDENTICAL under both (jyf1: 42.63/39.93/10.85/5.30/0.88/0.42 % either way). The earlier
    version of this docstring recommended exactly that check; it passes a transposed reading.
    **Verify on an AZIMUTHAL statistic instead** — effective views, azimuthal occupancy, or any
    quantity using R[0,2]/R[1,2]. End-to-end check that settles it: a 20 deg cone about the map's
    measured abundant axis should be strongly OVER-occupied (jyf1: 27.491 % = 4.56x uniform) and
    about the rare axis strongly DEPLETED (0.770 % = 0.13x). Under the wrong convention the two
    read indistinguishably (7.396 % vs 7.411 %).
    `fold_hemisphere`: a projection direction and its antipode give the same view up to an in-plane
    flip, so directions are folded to z >= 0.
    """
    d = proj.find_job(job_uid).load_output('particles', slots=['alignments3D'])
    R = _rodrigues(np.array(d['alignments3D/pose'], dtype=np.float64))
    v = (R[:, 2, :] if transpose else R[:, :, 2])       # R^T @ e_z is the 3rd ROW of R
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    if fold_hemisphere:
        v[v[:, 2] < 0] *= -1.0
    return v, R


def elevation_census(v, axis=(0, 0, 1), edges=(20, 45, 60, 75, 80)):
    """Fraction of views by elevation (angle away from the plane perpendicular to `axis`).

    Sanity check: for hTA1-Gs/J548 the known census is equator-dominated (~92% low elevation) with
    ~0.78% above 80 deg. If this function does not reproduce that, the pose convention is wrong.
    """
    a = np.asarray(axis, dtype=float)
    a /= np.linalg.norm(a)
    elev = np.degrees(np.arcsin(np.clip(np.abs(v @ a), 0, 1)))
    out, lo = {}, 0.0
    for e in edges:
        out[f'{lo:g}-{e:g}'] = float((( elev >= lo) & (elev < e)).mean() * 100)
        lo = e
    out[f'>{lo:g}'] = float((elev >= lo).mean() * 100)
    return out, elev


# ---------------------------------------------------------------------------------------------
# 2-3. bin the sphere, find the under-populated directions
# ---------------------------------------------------------------------------------------------

def fibonacci_hemisphere(n):
    """~uniform directions on the z>=0 hemisphere."""
    i = np.arange(n) + 0.5
    z = i / n                                  # (0,1]
    r = np.sqrt(np.clip(1 - z ** 2, 0, 1))
    phi = np.pi * (1 + 5 ** 0.5) * i
    return np.stack([r * np.cos(phi), r * np.sin(phi), z], axis=1)


def sphere_bins(v, n_bins=384, chunk=200_000):
    """Assign each viewing direction to its nearest bin centre; return centres + counts."""
    C = fibonacci_hemisphere(n_bins)
    counts = np.zeros(n_bins, dtype=np.int64)
    for s in range(0, len(v), chunk):
        counts += np.bincount((v[s:s + chunk] @ C.T).argmax(axis=1), minlength=n_bins)
    # solid angle per bin is ~equal, so count IS density
    return dict(centres=C, counts=counts, n=len(v),
                elev=np.degrees(np.arcsin(np.clip(C[:, 2], 0, 1))))


def rare_directions(bins, frac=0.05, min_count=None):
    """The most UNDER-POPULATED bins = the rare views.

    `frac`: take the lowest-density `frac` of bins. `min_count`: alternatively, every bin below an
    absolute count. Returns the directions plus their occupancy relative to the mean bin.
    """
    counts, C = bins['counts'], bins['centres']
    order = np.argsort(counts)
    sel = order[:max(1, int(round(frac * len(counts))))] if min_count is None \
        else np.where(counts < min_count)[0]
    mean = counts.mean()
    return dict(idx=sel, dirs=C[sel], counts=counts[sel],
                occupancy=counts[sel] / mean, elev=bins['elev'][sel], mean_count=mean)


# ---------------------------------------------------------------------------------------------
# 4. project the map along given directions
# ---------------------------------------------------------------------------------------------

def _rot_to_z(d):
    """Rotation matrix taking unit vector d to +z."""
    d = np.asarray(d, dtype=float)
    d = d / np.linalg.norm(d)
    z = np.array([0.0, 0.0, 1.0])
    v = np.cross(d, z)
    c = float(d @ z)
    if np.linalg.norm(v) < 1e-9:
        return np.eye(3) if c > 0 else np.diag([1.0, -1.0, -1.0])
    K = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + K + K @ K * (1.0 / (1.0 + c))


def load_map(proj, job_uid, slot='map'):
    import mrcfile
    vol = proj.find_job(job_uid).load_output('volume')
    path = os.path.join(proj.dir(), str(np.array(vol[f'{slot}/path'])[0]))
    psize = float(np.array(vol[f'{slot}/psize_A'])[0])
    with mrcfile.open(path, permissive=True) as m:
        return np.asarray(m.data, dtype=np.float32), psize


def project_map(proj, job_uid, dirs, lowpass_A=None, slot='map', vol=None, psize=None):
    """Project the map along each direction (sum along the view axis after rotating it to z)."""
    from scipy.ndimage import affine_transform

    if vol is None:
        vol, psize = load_map(proj, job_uid, slot)
    if lowpass_A:
        n = vol.shape[-1]
        f = np.sqrt(sum(np.fft.fftfreq(n, d=psize).reshape(s) ** 2
                        for s in [(-1, 1, 1), (1, -1, 1)]) +
                    np.fft.rfftfreq(n, d=psize).reshape(1, 1, -1) ** 2)
        vol = np.fft.irfftn(np.fft.rfftn(vol) * np.exp(-(f / (1.0 / lowpass_A)) ** 2 / 2.0),
                            s=vol.shape).astype(np.float32)

    c = np.array(vol.shape) / 2.0
    out = []
    for d in np.atleast_2d(dirs):
        # ⚠⚠ TWO BUGS FIXED HERE 2026-09-22 (jyf1). BOTH were INTERMITTENTLY INVISIBLE — for
        # near-cardinal directions the old code landed on the right axis by accident, i.e. it gave
        # the RIGHT ANSWER FOR THE WRONG REASON, which is why it survived three projects.
        #
        # (1) AXIS. scipy's convention is `input = matrix @ output`, so output axis k traverses
        #     COLUMN k of `matrix` = column k of `M.T` = ROW k of M. `_rot_to_z` gives `M @ d = z`,
        #     so `d` is ROW 2 of M. The old `.sum(axis=0)` therefore integrated along row 0 —
        #     a direction ORTHOGONAL to the one requested. Correct axis is 2.
        #     Proven on a synthetic 12x20x36 box: e0 gave 20x12 (correct 20x36); e1 gave 36x20
        #     (correct 12x36); e2 gave 20x36 (correct 12x20).
        # (2) FRAME. `alignments3D` directions are PHYSICAL (x,y,z); an MRC array is (z,y,x).
        #     `d` must be REVERSED into array order before `_rot_to_z`.
        #
        # GROUND TRUTH that settles both (keep this test): the ABUNDANT projection must reproduce
        # the dataset's measured dominant 2D class. jyf1: 57.5 x 128.6 A, aspect 2.24, vs the
        # measured 57-69 x 113-136 A, aspect 1.77-2.39. ✅ Unfixed it read aspect 1.72 (too short);
        # fixed-but-unreversed read the RARE view at aspect 1.86, i.e. INVERTED.
        M = _rot_to_z(np.asarray(d, dtype=np.float64)[::-1])   # physical (x,y,z) -> array (z,y,x)
        out.append(affine_transform(vol, M.T, offset=c - M.T @ c, order=1).sum(axis=2))
    return np.stack(out).astype(np.float32), psize


# ---------------------------------------------------------------------------------------------
# 5. junk-magnet guard + one-call entry point
# ---------------------------------------------------------------------------------------------

def featureless(images, psize_A, particle_diam_A=None, detail_A=25.0, thresh=0.45):
    """Flag projections with no internal density variation — the JUNK MAGNETS.

    An empty-cone projection is the map's WEAKEST (no measured data along that direction), so it can
    come out as a smooth blob that correlates with nearly every class average regardless of view.
    Arm A lost 4 of 13 templates to this, and Arm B v1's whole gate failed on one such projection
    matching everything at cc 0.84-0.91. This must be checked BEFORE the projections are used.

    Score = in-mask power above `detail_A` / total in-mask power. Real projections carry secondary
    structure and score high; smooth blobs score near zero. Returns (is_featureless, score).

    ⚠ `thresh` IS NOT SCALE-FREE. The score depends on the map's pixel size, its box, the applied
    lowpass and `detail_A`, so 0.45 (calibrated on hTA1-Gs) does NOT transfer. Measured on
    zll_20260904/J2163 (0.73 A/px, box 400, 15 A lowpass): rare 0.293-0.366 and ABUNDANT 0.288-0.367
    — i.e. 115/115 projections "featureless", including the best-measured directions, while both sets
    are visibly crisp. Never use the absolute threshold alone; calibrate against the abundant contrast
    set, as `rare_and_abundant` now does.
    """
    from scipy.ndimage import gaussian_filter

    imgs = np.atleast_3d(np.asarray(images, dtype=np.float32))
    if imgs.shape[0] != len(np.atleast_1d(images)):        # (H,W,N) -> (N,H,W)
        imgs = np.moveaxis(imgs, -1, 0)
    n = imgs.shape[-1]
    if particle_diam_A:
        yy, xx = np.mgrid[:n, :n] - (n - 1) / 2.0
        mask = (yy ** 2 + xx ** 2) <= (0.5 * particle_diam_A / psize_A) ** 2
    else:
        mask = np.ones((n, n), dtype=bool)

    sigma_px = max(detail_A / (2.355 * psize_A), 0.6)
    scores = np.empty(len(imgs), dtype=np.float32)
    for i, im in enumerate(imgs):
        im = im - im[mask].mean()
        hi = im - gaussian_filter(im, sigma_px)            # everything finer than detail_A
        tot = float(np.sqrt((im[mask] ** 2).mean()))
        scores[i] = 0.0 if tot < 1e-12 else float(np.sqrt((hi[mask] ** 2).mean()) / tot)
    return scores < thresh, scores


def rare_and_abundant(proj, job_uid, n_bins=384, rare_frac=0.05, abundant_frac=0.25,
                      lowpass_A=15.0, particle_diam_A=None, drop_featureless=True, verbose=True):
    """Step 1 in one call: refinement -> rare + abundant directions -> projections, junk-magnets gone.

    Returns a dict with `rare_dirs`/`rare_imgs`, `abundant_dirs`/`abundant_imgs`, the stacked
    `images` + `cone_idx` (indices of the rare columns, for `rareview_match.cone_margin`), `psize`,
    the occupancy `bins`, and `dropped` (indices removed as featureless).

    Score candidates against the FULL stack and rank on the rare-vs-other MARGIN — scoring against
    the rare projections alone gives 0.89-0.94 for everything and no ranking at all.
    """
    v, _ = viewing_directions(proj, job_uid)
    bins = sphere_bins(v, n_bins=n_bins)
    rare = rare_directions(bins, frac=rare_frac)

    counts, C = bins['counts'], bins['centres']
    n_ab = max(1, int(round(abundant_frac * len(counts))))
    ab_idx = np.argsort(counts)[::-1][:n_ab]

    vol, psize = load_map(proj, job_uid)
    rare_imgs, _ = project_map(proj, job_uid, rare['dirs'], lowpass_A=lowpass_A, vol=vol, psize=psize)
    ab_imgs, _ = project_map(proj, job_uid, C[ab_idx], lowpass_A=lowpass_A, vol=vol, psize=psize)

    dropped = np.array([], dtype=int)
    if drop_featureless:
        bad, sc = featureless(rare_imgs, psize, particle_diam_A)
        _, sc_ab = featureless(ab_imgs, psize, particle_diam_A)
        # The absolute `thresh` is pixel-size / lowpass dependent (see `featureless`). The ABUNDANT
        # directions are the best-measured ones by construction, so if they flunk it too the
        # threshold is mis-calibrated for this map, not a junk-magnet finding. Fall back to a
        # RELATIVE criterion: a junk magnet is a rare projection materially SMOOTHER than the
        # abundant contrast set.
        if bad.mean() > 0.5 and (sc_ab < 0.45).mean() > 0.5:
            cut = min(float(np.percentile(sc_ab, 5)), 0.6 * float(np.median(sc_ab)))
            if verbose:
                print(f'  ⚠ absolute featureless threshold mis-calibrated for this map: it flags '
                      f'{100*bad.mean():.0f}% of RARE and {100*(sc_ab<0.45).mean():.0f}% of ABUNDANT '
                      f'projections (rare {sc.min():.3f}-{sc.max():.3f}, abundant '
                      f'{sc_ab.min():.3f}-{sc_ab.max():.3f}). Switching to the RELATIVE criterion '
                      f'(cut {cut:.3f} = abundant p5 / 0.6x median). LOOK AT THE RENDER.')
            bad = sc < cut
        dropped = np.where(bad)[0]
        if verbose and len(dropped):
            print(f'  dropping {len(dropped)}/{len(rare_imgs)} featureless rare projections '
                  f'(scores {sc[dropped].min():.3f}-{sc[dropped].max():.3f}) — junk magnets')
        keep = ~bad
        rare_imgs, rare_dirs = rare_imgs[keep], rare['dirs'][keep]
    else:
        rare_dirs = rare['dirs']

    if verbose:
        cen, _ = elevation_census(v)
        print(f'  {job_uid}: {len(v):,} particles · census ' +
              ' '.join(f'{k}:{x:.2f}%' for k, x in cen.items()))
        print(f'  {len(rare_imgs)} rare + {len(ab_imgs)} abundant projections @ {lowpass_A} A lowpass')

    return dict(rare_dirs=rare_dirs, rare_imgs=rare_imgs,
                abundant_dirs=C[ab_idx], abundant_imgs=ab_imgs,
                images=np.concatenate([rare_imgs, ab_imgs]),
                cone_idx=np.arange(len(rare_imgs)),
                psize=psize, bins=bins, dropped=dropped, v=v)


def render(images, out_png, labels=None, title=None, ncol=6, psize_A=None, scalebar_A=100.0):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    n = len(images)
    if n == 0:
        # Used to die inside matplotlib with "Number of rows must be a positive integer, not 0",
        # which hides the real cause (the featureless guard dropped every projection).
        print(f'  ⚠ render({out_png}): EMPTY image stack — nothing to draw. '
              f'If this came from step 1, the featureless guard dropped every rare projection; '
              f'check its calibration against the abundant set before believing it.')
        return None
    nrow = max(1, int(np.ceil(n / ncol)))
    ncol = min(ncol, n)
    fig, axes = plt.subplots(nrow, ncol, figsize=(2.1 * ncol, 2.35 * nrow))
    axes = np.atleast_2d(axes)
    for ax in axes.ravel():
        ax.axis('off')
    for i in range(n):
        ax = axes[i // ncol, i % ncol]
        ax.imshow(images[i], cmap='gray')
        if labels is not None:
            ax.set_title(labels[i], fontsize=7)
        if psize_A:
            px = scalebar_A / psize_A
            x0, y0 = 0.06 * images[i].shape[1], 0.93 * images[i].shape[0]
            ax.plot([x0, x0 + px], [y0, y0], lw=2.5, color='w')
        ax.axis('off')
    fig.suptitle(title or 'projections', fontsize=10)
    fig.tight_layout()
    os.makedirs(os.path.dirname(os.path.abspath(out_png)), exist_ok=True)
    fig.savefig(out_png, dpi=150)
    plt.close(fig)
    return out_png
