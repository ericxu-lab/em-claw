"""
DBRV ARM B — direction-conditioned matcher for rescuing rare views from REJECTED 2D class averages.

The point of ARM B (see projects/<proj>/runs/dbrv_design.md §"ARM B", memory [[dbrv_rare_view_rescue]]):
a rare view was rejected at 2D BECAUSE it is weak — so the question is never "is this a good class?"
(the ranker already said no) but "does this class match a projection IN THE MISSING CONE?".
`reference_select_2D` scores class averages against the WHOLE volume, so it cannot answer that on its
own; this module scores each rejected class average against the EMPTY-CONE projections ONLY — the
direct analogue of what made ARM A work (filtering picks to the empty-cone template indices).

Usage (from a cryosparc-tools session, e.g. inside subC):

    from dbrv.rareview_match import load_template_stack, match_classes, write_report, contact_sheet

    proj  = cs.find_project('P5')
    cone  = load_template_stack(proj, 'J566', 'templates', keep_idx=[1,4,31,32,34,35,38])
    rej   = load_template_stack(proj, 'J342', 'templates_excluded')
    res   = match_classes(rej, cone, lowpass_A=15.0, particle_diam_A=175.0)
    write_report(res, rej, cone, 'runs/armB/J342_match.csv')
    contact_sheet(rej, cone, res, 'runs/armB/J342_sheet.png', top_n=12)

Everything here is read-only with respect to CryoSPARC — it reads output .mrc stacks and writes
CSV/PNG. It never creates or queues a job.

MODEL-BIAS NOTE: both sides are lowpassed before matching (default 15 A) and the score is only ever
used to RANK candidates that a blind visual read has to corroborate. A high score alone is not
evidence — see the two-criterion rule in dbrv_design.md §"ARM B" step B2.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np


# ---------------------------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------------------------

@dataclass
class TemplateStack:
    """A stack of 2D images (class averages or map projections) pulled from a CryoSPARC output."""
    job_uid: str
    group: str
    images: np.ndarray            # (N, H, W) float32
    psize_A: float
    class_idx: np.ndarray         # original index of each image within its job's output
    n_particles: np.ndarray | None = None   # particles per class, when the dataset carries it
    extra: dict = field(default_factory=dict)

    def __len__(self):
        return len(self.images)

    def fov_A(self):
        return self.images.shape[-1] * self.psize_A


def _first_field(dset, candidates):
    for c in candidates:
        if c in dset.fields():
            return c
    return None


def class_counts(proj, job_uid):
    """{original class_idx: num_particles} for a 2D job, from its summary stats.

    CryoSPARC keeps per-class counts in the job doc's `latest_summary_stats.all_class_info`, NOT in
    the template dataset. External (snowflake) select jobs carry empty stats — pass the twin/parent
    job via `counts_job=` in that case (e.g. the blob-r1 pool J342 is external; its counts live on
    the placeholder J341, and any select_2D's stats list ALL classes of its parent class_2D).
    """
    out = {}
    for g in proj.find_job(job_uid).doc.get('output_result_groups', []):
        for c in (g.get('latest_summary_stats') or {}).get('all_class_info', []):
            if 'class_idx' in c and 'num_particles_total' in c:
                out[int(c['class_idx'])] = int(c['num_particles_total'])
    return out


def load_template_stack(proj, job_uid, group, keep_idx=None, counts_job=None):
    """Load a `template`-type output group (class averages or create_templates projections).

    proj       : cryosparc.project.Project
    job_uid    : e.g. 'J342'
    group      : e.g. 'templates_excluded', 'templates_selected', 'templates', 'class_averages'
    keep_idx   : optional list of ORIGINAL class/template indices to keep (e.g. the empty-cone subset)
    counts_job : job to read per-class particle counts from, when `job_uid` has none of its own

    NOTE `class_idx` on the returned stack is the ORIGINAL class index (`blob/idx`) — the number the
    CryoSPARC UI shows and the one `queue_select2d(..., keep_blob_idx=[...])` expects. It is NOT the
    row position within the excluded group; for J342 the excluded rows are classes 0,3,4,5,6,9,...
    """
    import mrcfile

    job = proj.find_job(job_uid)
    dset = job.load_output(group)

    path_f = _first_field(dset, ['blob/path', 'templates/path'])
    idx_f = _first_field(dset, ['blob/idx', 'templates/idx'])
    ps_f = _first_field(dset, ['blob/psize_A', 'templates/psize_A'])
    n_f = _first_field(dset, ['class/num_particles_tot', 'class/num_particles',
                              'templates/num_particles'])
    if path_f is None or idx_f is None:
        raise RuntimeError(f"{job_uid}/{group}: no blob path/idx fields; got {list(dset.fields())[:20]}")

    paths = np.array(dset[path_f]).astype(str)
    idxs = np.array(dset[idx_f]).astype(int)
    psize = float(np.array(dset[ps_f])[0]) if ps_f else float('nan')
    npart = np.array(dset[n_f]).astype(int) if n_f else None

    # ORIGINAL class/template index — blob/idx, the number the UI shows and select_2D expects
    orig = idxs.copy()
    if keep_idx is not None:
        keep = np.isin(orig, np.asarray(keep_idx))
        paths, idxs, orig = paths[keep], idxs[keep], orig[keep]
        npart = npart[keep] if npart is not None else None

    if npart is None:
        lut = class_counts(proj, counts_job or job_uid)
        if lut:
            npart = np.array([lut.get(int(i), -1) for i in orig], dtype=int)

    imgs = []
    cache = {}
    for p, i in zip(paths, idxs):
        abspath = os.path.join(proj.dir(), p)
        if abspath not in cache:
            with mrcfile.open(abspath, permissive=True) as m:
                cache[abspath] = np.asarray(m.data, dtype=np.float32)
        arr = cache[abspath]
        imgs.append(arr[i] if arr.ndim == 3 else arr)
    imgs = np.stack(imgs).astype(np.float32)

    return TemplateStack(job_uid=job_uid, group=group, images=imgs, psize_A=psize,
                         class_idx=orig, n_particles=npart)


# ---------------------------------------------------------------------------------------------
# preprocessing
# ---------------------------------------------------------------------------------------------

def _resample_to(images, psize_from, psize_to, box_to):
    """Resample a stack to a target pixel size and box (Fourier crop/pad, then centre crop/pad)."""
    from scipy.ndimage import zoom

    factor = psize_from / psize_to
    if abs(factor - 1.0) > 1e-3:
        images = np.stack([zoom(im, factor, order=1) for im in images]).astype(np.float32)

    n = images.shape[-1]
    if n == box_to:
        return images
    if n > box_to:                      # centre crop
        s = (n - box_to) // 2
        return images[:, s:s + box_to, s:s + box_to]
    pad = box_to - n                    # centre pad
    a = pad // 2
    return np.pad(images, ((0, 0), (a, pad - a), (a, pad - a)), mode='constant')


def _lowpass(images, psize_A, res_A):
    """Soft (Gaussian) Fourier lowpass at res_A."""
    if not res_A:
        return images
    n = images.shape[-1]
    fy = np.fft.fftfreq(n, d=psize_A)[:, None]
    fx = np.fft.rfftfreq(n, d=psize_A)[None, :]
    f = np.sqrt(fy ** 2 + fx ** 2)
    fc = 1.0 / float(res_A)
    filt = np.exp(-(f / fc) ** 2 / 2.0).astype(np.float32)
    F = np.fft.rfft2(images, axes=(-2, -1)) * filt
    return np.fft.irfft2(F, s=(n, n), axes=(-2, -1)).astype(np.float32)


def _circular_mask(n, diam_px, soft_px=4.0):
    y, x = np.mgrid[:n, :n]
    r = np.sqrt((y - n / 2.0) ** 2 + (x - n / 2.0) ** 2)
    r0 = diam_px / 2.0
    m = np.clip((r0 + soft_px - r) / soft_px, 0.0, 1.0)
    return m.astype(np.float32)


def _normalize(images, mask):
    """Zero-mean / unit-variance inside the mask, then apply the mask."""
    w = mask.sum()
    mu = (images * mask).sum(axis=(-2, -1), keepdims=True) / w
    d = (images - mu) * mask
    sd = np.sqrt((d ** 2).sum(axis=(-2, -1), keepdims=True) / w) + 1e-8
    return (d / sd).astype(np.float32)


# ---------------------------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------------------------

@dataclass
class MatchResult:
    scores: np.ndarray            # (n_class, n_proj) best CC over in-plane rotation + small shift
    best_proj: np.ndarray         # (n_class,) index INTO the projection stack
    best_score: np.ndarray        # (n_class,)
    best_angle: np.ndarray        # (n_class,) in-plane angle (deg) of the best match
    work_psize_A: float
    work_box: int
    params: dict


def match_classes(classes: TemplateStack, projections: TemplateStack,
                  lowpass_A=15.0, particle_diam_A=175.0, n_rot=72, max_shift_A=20.0,
                  work_psize_A=None):
    """Score every class average against every projection: max normalised CC over in-plane rotation
    and a small translation search. Returns a MatchResult.

    Both stacks are brought to a common pixel size / box and lowpassed identically, so the score is
    a shape match, not a resolution match — which is what we want: a genuine rare view is a correct
    SHAPE at poor resolution.
    """
    from scipy.ndimage import rotate

    ps = float(work_psize_A or max(classes.psize_A, projections.psize_A, 2.0))
    fov = min(classes.fov_A(), projections.fov_A())
    box = int(round(fov / ps)) // 2 * 2

    A = _resample_to(classes.images, classes.psize_A, ps, box)
    B = _resample_to(projections.images, projections.psize_A, ps, box)

    mask = _circular_mask(box, min(particle_diam_A / ps, box * 0.9))
    A = _normalize(_lowpass(A, ps, lowpass_A), mask)
    B = _lowpass(B, ps, lowpass_A)

    # pre-rotate the projections once
    angles = np.linspace(0, 360, int(n_rot), endpoint=False)
    Brot = np.empty((len(B), len(angles), box, box), dtype=np.float32)
    for j, b in enumerate(B):
        for k, ang in enumerate(angles):
            Brot[j, k] = rotate(b, ang, reshape=False, order=1, mode='constant')
    Brot = _normalize(Brot.reshape(-1, box, box), mask).reshape(len(B), len(angles), box, box)

    # FFT cross-correlation, max within a small shift radius
    shift_px = max(1, int(round(max_shift_A / ps)))
    yy, xx = np.mgrid[:box, :box]
    dy = np.minimum(yy, box - yy)
    dx = np.minimum(xx, box - xx)
    shift_ok = (np.sqrt(dy ** 2 + dx ** 2) <= shift_px)

    FA = np.fft.rfft2(A, axes=(-2, -1))
    npix = mask.sum()

    n_cls, n_proj = len(A), len(B)
    scores = np.zeros((n_cls, n_proj), dtype=np.float32)
    best_ang = np.zeros((n_cls, n_proj), dtype=np.float32)
    for j in range(n_proj):
        FB = np.fft.rfft2(Brot[j], axes=(-2, -1))                      # (n_rot, box, box//2+1)
        # (n_cls, n_rot) best CC
        cc = np.fft.irfft2(FA[:, None] * np.conj(FB)[None, :], s=(box, box), axes=(-2, -1)) / npix
        cc = np.where(shift_ok, cc, -np.inf)
        per_rot = cc.max(axis=(-2, -1))                                # (n_cls, n_rot)
        k = per_rot.argmax(axis=1)
        scores[:, j] = per_rot[np.arange(n_cls), k]
        best_ang[:, j] = angles[k]

    bp = scores.argmax(axis=1)
    return MatchResult(scores=scores, best_proj=bp, best_score=scores[np.arange(n_cls), bp],
                       best_angle=best_ang[np.arange(n_cls), bp], work_psize_A=ps, work_box=box,
                       params=dict(lowpass_A=lowpass_A, particle_diam_A=particle_diam_A,
                                   n_rot=n_rot, max_shift_A=max_shift_A))


def cone_margin(res: MatchResult, projections: TemplateStack, cone_idx):
    """THE discriminator for ARM B.

    Matching a class average against the empty-cone projections ALONE is not enough: at low
    resolution almost everything correlates with almost everything (a featureless blob is a junk
    magnet — ARM A lost 4 of its 13 end-on templates to exactly this). What identifies a rare view is
    that it matches a cone projection BETTER THAN ANY OTHER VIEW of the same map.

    So: score against the FULL projection stack, then per class report
        best_cone   - best CC over projections inside the empty cone
        best_other  - best CC over every other projection
        margin      = best_cone - best_other       (>0 means the class really is a cone view)

    Returns a dict of arrays. `cone_idx` = ORIGINAL template indices (as shown in the CryoSPARC UI)
    that make up the empty cone.
    """
    in_cone = np.isin(np.asarray(projections.class_idx), np.asarray(cone_idx))
    if not in_cone.any():
        raise ValueError(f"none of cone_idx={list(cone_idx)} present in {projections.job_uid}"
                         f"/{projections.group} (has {list(projections.class_idx)})")
    if in_cone.all():
        raise ValueError("cone_idx covers the WHOLE projection stack — pass the full stack of "
                         "projections so the out-of-cone comparison is meaningful")

    s_cone = res.scores[:, in_cone]
    s_other = res.scores[:, ~in_cone]
    best_cone = s_cone.max(axis=1)
    best_other = s_other.max(axis=1)
    which = np.asarray(projections.class_idx)[in_cone][s_cone.argmax(axis=1)]
    which_other = np.asarray(projections.class_idx)[~in_cone][s_other.argmax(axis=1)]
    return dict(best_cone=best_cone, best_other=best_other, margin=best_cone - best_other,
                best_cone_idx=which, best_other_idx=which_other, in_cone_mask=in_cone)

# ---------------------------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------------------------

def write_report(res: MatchResult, classes: TemplateStack, projections: TemplateStack, out_csv,
                 score_percentile=None, cone=None, min_margin=0.0):
    """CSV of per-class results. Returns the rows as a list of dicts.

    Pass `cone` = the dict from cone_margin(...) to get the direction-conditioned columns and to make
    `candidate` mean "matches the empty cone better than any other view" (margin >= min_margin) —
    which is the ARM B criterion. Without `cone` it falls back to a plain score percentile.

    Either way `candidate` is a SUGGESTION: the selection rule is agreement between this ranking and
    an independent BLIND visual read (dbrv_design.md §"ARM B" B2).
    """
    os.makedirs(os.path.dirname(os.path.abspath(out_csv)), exist_ok=True)
    thr = np.percentile(res.best_score, score_percentile) if score_percentile else None

    rows = []
    for i in range(len(classes)):
        row = dict(
            job=classes.job_uid, group=classes.group,
            class_idx=int(classes.class_idx[i]),
            n_particles=int(classes.n_particles[i]) if classes.n_particles is not None else -1,
            best_proj_idx=int(projections.class_idx[res.best_proj[i]]),
            best_score=float(res.best_score[i]),
            best_inplane_deg=float(res.best_angle[i]),
        )
        if cone is not None:
            row.update(best_cone=float(cone['best_cone'][i]),
                       best_cone_idx=int(cone['best_cone_idx'][i]),
                       best_other=float(cone['best_other'][i]),
                       best_other_idx=int(cone['best_other_idx'][i]),
                       margin=float(cone['margin'][i]),
                       candidate=bool(cone['margin'][i] >= min_margin))
        else:
            row['candidate'] = bool(thr is not None and res.best_score[i] >= thr)
        rows.append(row)
    rows.sort(key=lambda r: -(r['margin'] if cone is not None else r['best_score']))

    import csv
    with open(out_csv, 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return rows


def contact_sheet(classes: TemplateStack, projections: TemplateStack, res: MatchResult, out_png,
                  top_n=16, title=None, cone=None):
    """Render the top-scoring rejected classes next to the projection each one matched.

    This is the image an agent LOOKS at for the blind visual read. Left column = the rejected class
    average, right column = its best-matching empty-cone projection (rotated into register).
    """
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from scipy.ndimage import rotate

    # rank by the ARM B criterion (cone margin) when available, else by raw score
    rank_by = cone['margin'] if cone is not None else res.best_score
    order = np.argsort(-rank_by)[:top_n]
    ncol = 4
    nrow = int(np.ceil(len(order) / (ncol // 2)))
    fig, axes = plt.subplots(nrow, ncol, figsize=(2.2 * ncol, 2.4 * nrow))
    axes = np.atleast_2d(axes)
    for ax in axes.ravel():
        ax.axis('off')

    for n, i in enumerate(order):
        r, c = divmod(n, ncol // 2)
        # show the best-matching CONE projection when doing the ARM B read, else the global best
        if cone is not None:
            j = int(np.where(np.asarray(projections.class_idx) == cone['best_cone_idx'][i])[0][0])
        else:
            j = int(res.best_proj[i])
        a = axes[r, 2 * c]
        b = axes[r, 2 * c + 1]
        a.imshow(classes.images[i], cmap='gray')
        lbl = f"cls {classes.class_idx[i]}"
        if classes.n_particles is not None:
            lbl += f"  n={classes.n_particles[i]:,}"
        a.set_title(f"{classes.job_uid} {lbl}", fontsize=7)
        b.imshow(rotate(projections.images[j], -res.scores_angle(i, j) if hasattr(res, 'scores_angle')
                        else -res.best_angle[i], reshape=False, order=1), cmap='gray')
        cap = (f"proj {projections.class_idx[j]}  cc={cone['best_cone'][i]:.3f}  "
               f"margin={cone['margin'][i]:+.3f}") if cone is not None else \
              f"proj {projections.class_idx[j]}  cc={res.best_score[i]:.3f}"
        b.set_title(cap, fontsize=7)
        a.axis('off')
        b.axis('off')

    fig.suptitle(title or f"ARM B candidates — {classes.job_uid}/{classes.group} "
                          f"vs {projections.job_uid} empty-cone projections", fontsize=9)
    fig.tight_layout()
    os.makedirs(os.path.dirname(os.path.abspath(out_png)), exist_ok=True)
    fig.savefig(out_png, dpi=150)
    plt.close(fig)
    return out_png


def render_stack(stack: TemplateStack, out_png, title=None, ncol=8):
    """Plain grid render of a stack (used to LOOK at the empty-cone projections = 'learn the view',
    and to eyeball a whole round's rejected class averages)."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    n = len(stack)
    nrow = int(np.ceil(n / ncol))
    fig, axes = plt.subplots(nrow, ncol, figsize=(1.6 * ncol, 1.8 * nrow))
    axes = np.atleast_2d(axes)
    for ax in axes.ravel():
        ax.axis('off')
    for i in range(n):
        ax = axes[i // ncol, i % ncol]
        ax.imshow(stack.images[i], cmap='gray')
        lbl = f"{stack.class_idx[i]}"
        if stack.n_particles is not None:
            lbl += f"\n{stack.n_particles[i]:,}"
        ax.set_title(lbl, fontsize=6)
        ax.axis('off')
    fig.suptitle(title or f"{stack.job_uid}/{stack.group}", fontsize=9)
    fig.tight_layout()
    os.makedirs(os.path.dirname(os.path.abspath(out_png)), exist_ok=True)
    fig.savefig(out_png, dpi=150)
    plt.close(fig)
    return out_png


# =================================================================================================
# HARVEST — walk every 2D round of every picking branch and collect the rejected classes.
# Added after ARM B v2: these three data hazards each cost a debugging cycle on hTA1-Gs and are the
# difference between "the harvest is empty" and "the harvest is silently WRONG".
# =================================================================================================

_COUNT_CACHE = {}


def particle_counts_by_class(proj, class2d_uid):
    """{class_idx: n_particles} counted from the class_2D's OWN particles.

    `class_counts()` reads `latest_summary_stats.all_class_info`, which is simply ABSENT on
    `class_2D_new` and on external select jobs in this instance (verified: 0 entries for every job
    tried). Counting `alignments2D/class` is authoritative and always available. Cached per job.
    """
    if class2d_uid in _COUNT_CACHE:
        return _COUNT_CACHE[class2d_uid]

    out = {}
    # Prefer the on-disk .cs: load_output() round-trips through the master and is far too slow for
    # multi-million-particle 2D jobs (12 rounds timed out at >2 min); the file reads in ~1 s.
    try:
        import os
        from cryosparc.dataset import Dataset
        jdir = os.path.join(proj.dir(), class2d_uid)
        cands = [f for f in sorted(os.listdir(jdir))
                 if f.endswith('_particles.cs') and 'passthrough' not in f]
        if cands:
            d = Dataset.load(os.path.join(jdir, cands[-1]))
            if 'alignments2D/class' in d.fields():
                cls = np.asarray(d['alignments2D/class']).astype(int)
                out = {int(k): int(v) for k, v in zip(*np.unique(cls, return_counts=True))}
    except Exception:
        out = {}

    if not out:                                   # fall back to the API
        try:
            job = proj.find_job(class2d_uid)
            names = [g['name'] for g in job.doc.get('output_result_groups', [])]
            grp = 'particles' if 'particles' in names else (names[0] if names else None)
            if grp:
                d = job.load_output(grp, slots=['alignments2D'])
                cls = np.asarray(d['alignments2D/class']).astype(int)
                out = {int(k): int(v) for k, v in zip(*np.unique(cls, return_counts=True))}
        except Exception:
            out = {}

    _COUNT_CACHE[class2d_uid] = out
    return out


def rejected_pool(proj, class2d_uid, select_uid, counts_job=None):
    """The rejected classes of ONE 2D round, with the hazards handled.

    Returns dict(stack=TemplateStack | None, class_idx=[...], n_particles=int, source=str, note=str).

    Hazard 1 — NEVER MATERIALISED, not absent. An EXTERNAL select job may have saved only
    `particles_selected`; its `particles_excluded` then reports 0 rows. That is not "nothing was
    rejected" — the branch simply never wrote it. Rebuild as parent minus kept (this recovered a
    7.4M-particle pool on hTA1-Gs that had been written off as absent).

    Hazard 2 — RE-NUMBERED LABELS. `select_2D` preserves `blob/idx` as the original class label, but
    `reference_select_2D` RE-NUMBERS it locally. Acting on those numbers materialises the WRONG
    classes. We therefore always report labels taken from the PARENT class_2D's frame.

    Hazard 3 — MISSING COUNTS. External select jobs carry empty summary stats; pass `counts_job=`
    (the twin/placeholder or the parent class_2D) so per-class particle counts resolve.
    """
    sel = proj.find_job(select_uid)
    groups = {g['name'] for g in sel.doc.get('output_result_groups', [])}
    counts = particle_counts_by_class(proj, class2d_uid)      # authoritative, from the particles

    def _n(idx):
        return int(sum(counts.get(int(i), 0) for i in idx))

    # kept classes: prefer the templates, fall back to the particles' own class labels ------------
    kept_idx = []
    for grp in ('templates_selected',):
        if grp in groups:
            try:
                ks = load_template_stack(proj, select_uid, grp, counts_job=counts_job or class2d_uid)
                kept_idx = sorted(int(i) for i in ks.class_idx)
            except Exception:
                kept_idx = []
    if not kept_idx and 'particles_selected' in groups:       # Hazard 2 fallback
        try:
            d = sel.load_output('particles_selected', slots=['alignments2D'])
            kept_idx = sorted(int(i) for i in np.unique(np.asarray(d['alignments2D/class'])))
        except Exception:
            kept_idx = []

    if 'templates_excluded' in groups:
        try:
            st = load_template_stack(proj, select_uid, 'templates_excluded',
                                     counts_job=counts_job or class2d_uid)
            if len(st):
                idx = sorted(int(i) for i in st.class_idx)
                return dict(stack=st, class_idx=idx, n_particles=_n(idx),
                            source=f'{select_uid}/templates_excluded', note='')
        except Exception:
            pass  # fall through to the rebuild

    # ---- Hazard 1: rebuild the rejected side from the PARENT minus the kept classes -------------
    parent = load_template_stack(proj, class2d_uid, 'class_averages', counts_job=class2d_uid)
    all_idx = sorted(int(i) for i in parent.class_idx)
    rej_idx = [i for i in all_idx if i not in set(kept_idx)]
    if not rej_idx:
        return dict(stack=None, class_idx=[], n_particles=0,
                    source=f'{class2d_uid} (rebuilt)', note='no rejected classes')
    st = load_template_stack(proj, class2d_uid, 'class_averages', keep_idx=rej_idx,
                             counts_job=class2d_uid)
    return dict(stack=st, class_idx=rej_idx, n_particles=_n(rej_idx),
                source=f'{class2d_uid}/class_averages minus kept({len(kept_idx)})',
                note='REBUILT — the select job never materialised particles_excluded')


def harvest_rejects(proj, branches, verbose=True):
    """Walk every round of every branch and collect the rejected classes.

    `branches`: {branch_name: [(class2d_uid, select_uid), ...]}  — rounds in order. Each round's
    rejects are DISJOINT from the others, so every round contributes; do not harvest only the last.

    Returns {branch: [round_dict, ...]} using `rejected_pool` per round.
    """
    out = {}
    for br, rounds in branches.items():
        out[br] = []
        for c2d, sel in rounds:
            try:
                r = rejected_pool(proj, c2d, sel)
            except Exception as e:
                r = dict(stack=None, class_idx=[], n_particles=0,
                         source=f'{c2d}/{sel}', note=f'FAILED: {e!r}'[:160])
            r.update(branch=br, class2d=c2d, select=sel)
            out[br].append(r)
            if verbose:
                flag = f'  <- {r["note"]}' if r['note'] else ''
                print(f'  {br:10s} {c2d}->{sel}: {len(r["class_idx"]):3d} classes, '
                      f'{r["n_particles"]:>9,} particles  [{r["source"]}]{flag}')
    if verbose:
        tot = sum(r['n_particles'] for v in out.values() for r in v)
        ncl = sum(len(r['class_idx']) for v in out.values() for r in v)
        print(f'  TOTAL harvested: {ncl} rejected classes, {tot:,} particles')
    return out
