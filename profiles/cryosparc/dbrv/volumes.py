"""Volume comparison for DBRV — decide which hetero classes are the SAME structure and combine them.

Step 6 of ARM B v2. This exists because of a specific, expensive mistake: after a K=3 hetero junk-sink
(good map + two self-lowpass decoys), it is tempting to read "this class seeded from a decoy" as
"this class is junk". On hTA1-Gs the class seeded from the 20 A decoy was the SAME structure as the
one seeded from the sharp map — cross-FSC 4.72 A, real-space CC 0.93 — because a self-lowpass decoy
is the same shape only blurrier, so real low-SNR particles prefer it. Treating it as junk would have
discarded 81,782 legitimately rescued particles and reported particle loss as 52.7% instead of 10.6%.

    from dbrv.volumes import compare_classes
    cmp = compare_classes(proj, 'J1190', n_classes=3)
    cmp['combine']        # -> [[0, 1], [2]]   groups that agree
"""

from __future__ import annotations

import os
import numpy as np


def load_class_volumes(proj, job_uid, n_classes=None, slot='map'):
    """{class_idx: volume} for a hetero_refine / class_3D job, plus the pixel size."""
    import mrcfile

    job = proj.find_job(job_uid)
    names = [g['name'] for g in job.doc.get('output_result_groups', [])]
    idxs = sorted(int(n.rsplit('_', 1)[1]) for n in names
                  if n.startswith('volume_class_') and n.rsplit('_', 1)[1].isdigit())
    if n_classes is not None:
        idxs = idxs[:n_classes]
    vols, psize = {}, None
    for k in idxs:
        d = job.load_output(f'volume_class_{k}')
        path = os.path.join(proj.dir(), str(np.array(d[f'{slot}/path'])[0]))
        psize = float(np.array(d[f'{slot}/psize_A'])[0])
        with mrcfile.open(path, permissive=True) as m:
            vols[k] = np.asarray(m.data, dtype=np.float32)
    return vols, psize


def cross_fsc(a, b, psize_A):
    """Shell-wise FSC between two maps. A SIMILARITY measure between classes — NOT a resolution
    estimate (these are not independent half-sets). Returns the curve."""
    n = a.shape[0]
    A, B = np.fft.fftn(a), np.fft.fftn(b)
    f = np.fft.fftfreq(n)
    R = np.sqrt(f[:, None, None] ** 2 + f[None, :, None] ** 2 + f[None, None, :] ** 2)
    shell = np.clip((R * n).astype(int), 0, n // 2)
    num = np.bincount(shell.ravel(), (A * np.conj(B)).real.ravel(), n // 2 + 1)
    d1 = np.bincount(shell.ravel(), (np.abs(A) ** 2).ravel(), n // 2 + 1)
    d2 = np.bincount(shell.ravel(), (np.abs(B) ** 2).ravel(), n // 2 + 1)
    with np.errstate(invalid='ignore', divide='ignore'):
        c = num / np.sqrt(d1 * d2)
    return np.nan_to_num(c)


def fsc_res(curve, n, psize_A, thr=0.143):
    for i in range(1, len(curve)):
        if curve[i] < thr:
            return n * psize_A / max(i, 1)
    return 2 * psize_A


def compare_classes(proj, job_uid, n_classes=None, same_fsc_A=None, same_cc=0.85, verbose=True):
    """Which hetero classes are the same structure? Returns pairwise metrics + `combine` groups.

    `same_fsc_A`: two classes agree if their cross-FSC(0.143) is FINER than this. Defaults to 4x the
    pixel size — on hTA1-Gs (1.069 A/px binned to 2.138) that is ~8.5 A, and the agreeing pair came in
    at 4.72 A while the junk class sat at ~13 A, so the separation was wide and unambiguous.
    `same_cc`: real-space correlation floor (the agreeing pair was 0.93, junk 0.80).

    BOTH criteria must hold. If a decision is marginal the function says so — inspect the slices
    rather than trusting the threshold.
    """
    vols, psize = load_class_volumes(proj, job_uid, n_classes)
    ks = sorted(vols)
    if len(ks) < 2:
        return dict(psize=psize, pairs={}, combine=[[k] for k in ks], marginal=[])
    n = vols[ks[0]].shape[0]
    if same_fsc_A is None:
        same_fsc_A = 4.0 * psize

    pairs, adj, marginal = {}, {k: set() for k in ks}, []
    for i, a in enumerate(ks):
        for b in ks[i + 1:]:
            c = cross_fsc(vols[a], vols[b], psize)
            r = fsc_res(c, n, psize)
            cc = float(np.corrcoef(vols[a].ravel(), vols[b].ravel())[0, 1])
            same = (r < same_fsc_A) and (cc >= same_cc)
            pairs[(a, b)] = dict(fsc_A=r, cc=cc, same=same)
            if same:
                adj[a].add(b); adj[b].add(a)
            if abs(r - same_fsc_A) < 0.25 * same_fsc_A or abs(cc - same_cc) < 0.05:
                marginal.append((a, b))
            if verbose:
                print(f"  class_{a} vs class_{b}: FSC0.143 = {r:6.2f} A   CC = {cc:.4f}   "
                      f"{'SAME -> combine' if same else 'different'}")

    seen, groups = set(), []
    for k in ks:                                  # connected components
        if k in seen:
            continue
        stack, comp = [k], []
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x); comp.append(x)
            stack.extend(adj[x] - seen)
        groups.append(sorted(comp))

    if verbose:
        print(f"  -> combine groups: {groups}"
              + (f"   [MARGINAL pairs {marginal} — inspect the slices]" if marginal else ""))
    return dict(psize=psize, pairs=pairs, combine=groups, marginal=marginal, volumes=vols)


def render_classes(proj, job_uid, out_png, n_classes=None, title=None):
    """Slice montage of every class volume — the eyeball half of the same/different call."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    vols, psize = load_class_volumes(proj, job_uid, n_classes)
    ks = sorted(vols)
    n = vols[ks[0]].shape[0]
    fig, axes = plt.subplots(len(ks), 6, figsize=(15, 2.7 * len(ks)))
    axes = np.atleast_2d(axes)
    for r, k in enumerate(ks):
        v = vols[k]
        sl = [v[n // 3], v[n // 2], v[2 * n // 3], v[:, n // 2, :], v[:, :, n // 2], v[:, :, 2 * n // 3]]
        lbl = ['Z 1/3', 'Z 1/2', 'Z 2/3', 'Y 1/2', 'X 1/2', 'X 2/3']
        for c in range(6):
            axes[r, c].imshow(sl[c], cmap='gray')
            axes[r, c].set_title(f'class_{k}  {lbl[c]}', fontsize=8)
            axes[r, c].axis('off')
    fig.suptitle(title or f'{job_uid} class volumes', fontsize=11)
    fig.tight_layout()
    os.makedirs(os.path.dirname(os.path.abspath(out_png)), exist_ok=True)
    fig.savefig(out_png, dpi=140)
    plt.close(fig)
    return out_png
