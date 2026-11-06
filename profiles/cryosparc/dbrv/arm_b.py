"""DBRV ARM B v2 — driver.

Recovers viewing directions that were PICKED and then DISCARDED during 2D cleaning.
(ARM A is the other option: re-pick the micrographs with empty-cone templates, recovering views that
were never picked at all. The two are complementary — spatial overlap of their rescued particles was
1.4-1.9% on hTA1-Gs — but ARM B is preferred: it is far cheaper, needing no 7k-micrograph re-pick.
⭐ ARM A NOW HAS ITS OWN RECORD: see ARM_A_PIPELINE.md, written 2026-09-22. Until then it had none —
no driver, no doc, only scattered mentions in one project's design notes — so the first full Arm A
pilot was run on improvised terms. It carries the PILOT GATE: ~300 micrographs, novelty measured
against the PRE-CLEANING pools with a random-position null and DENSITY-MATCHED before any arm-to-arm
comparison, an identity leg requiring the ABUNDANT view to be present, and 2D cleaning WITH the seed
before any verdict. Two Arm A attempts on jyf1 died of one measured cause: at the 20 A picking lowpass
the face-on projection has NO correlation advantage anywhere, not even at true face-on positions —
a noise-free template does not rescue a view whose projection carries no contrast advantage.)

    1. project the volume along its under-populated directions   → what the missing views look like
    2. match rejected class averages, every round, every branch   → candidate rare-view classes
    3. mix candidates with a pose-labelled rare-view SEED         → ⛔ NOT OPTIONAL — see below
    4. 2D classify, then select (refsel defaults + corr filter)   → ⛔ JUNK ONLY, NEVER DIRECTION
    5. merge into the clean stack, dedup                          → the working stack
    6. 3D purify (hetero junk-sink), cross-FSC, COMBINE agreeing  → recovered particles
    7. confirm: refined-elevation census of the survivors         → the only real direction measure

Steps 1, 2, 6 and 7 are fully automated here. Steps 3-5 queue CryoSPARC jobs, so they take explicit
parameters and are meant to be driven step by step (notebook-style, as subC runs them) rather than
fired blind — each has a decision point that wants a human or agent eye.

GOVERNING RESULT, measured on hTA1-Gs and not to be re-litigated per project: **2D cannot identify
viewing direction.** Seed enrichment, held-out validation, dense-projection direction matching and
reference-selection scores were each measured to carry no direction signal — even when handed 70,894
ground-truth rare-view particles as a seed. Do not select rare views in 2D. Keep every view, remove
only junk, and let the 3D classification in step 6 vet direction.

🔁 CORROBORATED INDEPENDENTLY 2026-09-22 (jyf1), by a method that had NOT read this paragraph: a
directional-margin 2D selector that CLEARED EVERY NEGATIVE CONTROL (blur 0.000, overlap 0/40, kept-side
0/23, noise p95 +0.016) then passed its cone gate at 1,065x the measured replicate floor — 31,743
particles, 21x the sham — AND WAS STILL JUNK: resolution along the target direction did not improve
(6.68 -> 6.86 A). A 2D direction statistic can clear all of its own controls and still measure nothing.

⛔ STEP 3'S SEED IS NOT OPTIONAL, AND STEP 4 DEPENDS ON IT. The tight `sobel_max=1.8` cut (keeps
27-34%) is safe ONLY because the seed keeps rare-view exemplars in the mixture. Seed absent => that
same cut removes the target view. This is the resolution of the apparent conflict with the standing
note that "sobel_max over-cuts rare views": both are true, conditional on the seed.

⛔ DO NOT ISSUE AN ARM VERDICT until that arm has been 2D-cleaned IN ROUNDS, merged with the FULL
UNDRAINED clean stack, and junk-drained in 3D. jyf1 closed both arms before any of that and had to
withdraw both closures.

See ARM_B_v2_PIPELINE.md — its header block now carries these four hazards and the two code bugs
fixed 2026-09-22 (`project_map` summed the wrong axis; `viewing_directions` defaulted to a convention
that silently returns fake isotropy).
"""

from __future__ import annotations

import os
import numpy as np

from .rare_view_projections import (rare_and_abundant, render, viewing_directions,
                                    elevation_census, _rodrigues)
from .rareview_match import (harvest_rejects, match_classes, cone_margin, write_report,
                             contact_sheet, TemplateStack)
from .refselect_scores import class_scores, filter_by_corr
from .particles import build_seed, save_particles, merge_dedup, net_new
from .volumes import compare_classes, render_classes


# =================================================================================================
# step 1 — what do the missing views look like?
# =================================================================================================

def step1_projections(proj, refine_uid, out_dir, particle_diam_A=None, lowpass_A=15.0,
                      rare_frac=0.05, n_bins=384, verbose=True):
    """Rare + abundant projections of the refined map, junk-magnet projections removed.

    Renders both sets — LOOK AT THEM. A featureless rare projection matches nearly every class at
    cc 0.84-0.91 regardless of view and will poison step 2; `rare_and_abundant` drops them
    automatically but the render is the check that the automation was right.
    """
    os.makedirs(out_dir, exist_ok=True)
    r = rare_and_abundant(proj, refine_uid, n_bins=n_bins, rare_frac=rare_frac,
                          lowpass_A=lowpass_A, particle_diam_A=particle_diam_A, verbose=verbose)
    render(r['rare_imgs'], f'{out_dir}/rare_projections.png', psize_A=r['psize'],
           title=f'{refine_uid} — RARE (under-populated) directions')
    render(r['abundant_imgs'], f'{out_dir}/abundant_projections.png', psize_A=r['psize'],
           title=f'{refine_uid} — abundant directions (the contrast set)')
    cen, _ = elevation_census(r['v'])
    r['census'] = cen
    if verbose:
        print(f"  entry occupancy: >45deg "
              f"{sum(v for k, v in cen.items() if k.split('-')[0] not in ('0', '20')):.2f}%")
    return r


# =================================================================================================
# step 2 — which rejected classes are candidates?
# =================================================================================================

def step2_candidates(proj, branches, projections, out_dir, particle_diam_A,
                     lowpass_A=15.0, n_rot=36, verbose=True):
    """Harvest every round's rejects and rank each class on the rare-vs-other CC MARGIN.

    Scoring against the rare projections ALONE does not work — at 15 A everything correlates with
    everything (0.89-0.94 across the board, no ranking). The margin asks the right question:
    "does this class look more like the missing view than like any view we already have?"

    Margins are SMALL by nature (neighbouring views resemble each other), so this returns a RANKING,
    never a threshold. Pair it with a BLIND visual read recorded BEFORE the scores are opened, drop
    featureless blobs regardless of score, and let the blind read win where they disagree.
    """
    os.makedirs(out_dir, exist_ok=True)
    pool = harvest_rejects(proj, branches, verbose=verbose)
    stack = TemplateStack(images=projections['images'], psize_A=projections['psize'],
                          class_idx=np.arange(len(projections['images'])), n_particles=None,
                          job_uid='projections', group='rare+abundant')
    out = {}
    for br, rounds in pool.items():
        for r in rounds:
            if r['stack'] is None or not len(r['stack']):
                continue
            key = f"{br}_{r['select']}"
            res = match_classes(r['stack'], stack, lowpass_A=lowpass_A,
                                particle_diam_A=particle_diam_A, n_rot=n_rot)
            cm = cone_margin(res, stack, projections['cone_idx'])
            write_report(res, r['stack'], stack, f'{out_dir}/{key}.csv', cone=cm)
            contact_sheet(r['stack'], stack, res, f'{out_dir}/{key}_sheet.png', cone=cm)
            out[key] = dict(round=r, result=res, margin=cm)
            if verbose:
                m = np.asarray(cm['margin'] if isinstance(cm, dict) else cm)
                print(f'  {key}: {len(r["class_idx"])} classes · margin '
                      f'{m.min():+.3f}..{m.max():+.3f} · {int((m > 0).sum())} with margin>0')
    return out


# =================================================================================================
# steps 3-5 — job-queueing (drive these explicitly)
# =================================================================================================

def step3_seed(cs, proj, ws, refine_uid, elev_min=50.0, control_max=20.0, n_max=None, verbose=True):
    """Pose-labelled rare-view SEED + the abundant-view CONTROL.

    The control is NOT optional. It is the only thing that distinguishes "the rare view genuinely
    does not separate" from "the pipeline is broken": on hTA1-Gs the control seed segregated at
    0.41-0.70 while the rare seed sat at 0.075-0.105, which is what made the negative believable.
    """
    seed = build_seed(cs, proj, ws, refine_uid, elev_min=elev_min, n_max=n_max,
                      title='DBRV seed (rare views, refined elevation)', verbose=verbose)
    ctrl = build_seed(cs, proj, ws, refine_uid, elev_min=0.0, elev_max=control_max,
                      n_max=n_max or seed['n'],
                      title='DBRV CONTROL seed (abundant views)', seed=1, verbose=verbose)
    return dict(seed=seed, control=ctrl)


REFSEL_DEFAULTS_NOTE = """
    reference_select_2D MUST run at schema defaults:
        select_mode = "thresholds on Sobel scores"   sobel_max = 1.8
        alpha_min   = 0.8                            res_filter = 20
    Set ONLY `window_inner_A` (its default is null and the job FAILS without it).
    Loosening sobel_max / using 'thresholds only' keeps 95-99% — a no-op that re-admits the whole
    discarded pool (how v1 failed) — AND changes the report format so the corr filter cannot run.
"""


def step4_select(cs, proj, ws, class2d_uid, volume_uid, window_inner_A, corr_max=1.1,
                 lane=None, hostname=None, wait=True, verbose=True):
    """reference_select_2D at DEFAULTS, then drop the bright-blob classes (corr > corr_max)."""
    job = proj.create_job(ws.uid, 'reference_select_2D',
                          title=f'DBRV refsel defaults {class2d_uid} vs {volume_uid}')
    job.connect('particles', class2d_uid, 'particles')
    job.connect('templates', class2d_uid, 'class_averages')
    job.connect('volume', volume_uid, 'volume')
    job.set_param('window_inner_A', window_inner_A)      # everything else stays at default
    job.queue(lane=lane, hostname=hostname)
    if wait:
        job.wait_for_done()
    if verbose:
        try:
            print(f'  {job.uid}: kept {len(job.load_output("particles_selected")):,} particles / '
                  f'{len(job.load_output("templates_selected"))} classes')
        except Exception:
            pass
    filt = filter_by_corr(cs, proj, ws, job.uid, corr_max=corr_max, verbose=verbose)
    return dict(select_uid=job.uid, **filt)


def step5_merge(cs, proj, ws, branch_sources, clean_uid, clean_group='particles',
                min_dist_A=60, micrographs=None, lane=None, hostname=None, verbose=True):
    """Union the branches, then merge into the clean stack. Reports NET-NEW and replacement losses."""
    a = merge_dedup(cs, proj, ws, branch_sources, min_dist_A=min_dist_A, shift_key='none',
                    lane=lane, hostname=hostname, micrographs=micrographs,
                    title='DBRV branch union+dedup', verbose=verbose)
    b = merge_dedup(cs, proj, ws, [(a['uid'], 'particles_kept'), (clean_uid, clean_group)],
                    min_dist_A=min_dist_A, shift_key='none', lane=lane, hostname=hostname,
                    micrographs=micrographs, title='DBRV merge into clean stack', verbose=verbose)
    nn = net_new(proj, b['uid'], clean_uid, baseline_group=clean_group, verbose=verbose)
    return dict(branches=a, merged=b, net_new=nn)


# =================================================================================================
# steps 6-7 — 3D purify, combine, confirm
# =================================================================================================

def step6_combine(proj, hetero_uid, out_dir=None, verbose=True):
    """Cross-FSC the hetero classes and report which to COMBINE.

    Do NOT read "seeded from a decoy" as "junk". A self-lowpass decoy is the same shape only blurrier,
    so real low-SNR particles prefer it: on hTA1-Gs the 20 A-decoy class matched the sharp-seeded one
    at FSC 4.72 A / CC 0.93, and discarding it would have thrown away 81,782 rescued particles and
    reported particle loss as 52.7% instead of the true 10.6%.
    """
    cmp = compare_classes(proj, hetero_uid, verbose=verbose)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        render_classes(proj, hetero_uid, f'{out_dir}/hetero_classes.png',
                       title=f'{hetero_uid} — combine {cmp["combine"]}')
    if verbose and cmp['marginal']:
        print('  ⚠ marginal pairs present — inspect the slice montage before trusting the grouping')
    return cmp


def step7_confirm(proj, hetero_uid, class_group, baseline_uid, is_new=None, verbose=True):
    """Refined-elevation census of the surviving rescued particles vs the baseline stack.

    This is the ONLY direction measurement in the pipeline that is a measurement rather than an
    inference from 2D — every 2D-side estimate returned null while this showed 50.27% >45deg against
    a 17.53% baseline (2.9x) on hTA1-Gs.
    """
    d = proj.find_job(hetero_uid).load_output(class_group, slots=['blob', 'alignments3D'])
    R = _rodrigues(np.array(d['alignments3D/pose'], dtype=np.float64))
    v = R[:, 2, :]
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    v[v[:, 2] < 0] *= -1.0
    elev = np.degrees(np.arcsin(np.clip(np.abs(v[:, 2]), 0, 1)))

    if is_new is None:
        base = proj.find_job(baseline_uid).load_output('particles', slots=['blob'])
        ub = set(np.asarray(base['uid']).tolist())
        is_new = np.array([u not in ub for u in np.asarray(d['uid']).tolist()])

    bv, _ = viewing_directions(proj, baseline_uid)
    be = np.degrees(np.arcsin(np.clip(np.abs(bv[:, 2]), 0, 1)))
    out = {}
    for lab, e in [('baseline', be), ('all survivors', elev),
                   ('rescued survivors', elev[is_new]), ('baseline-origin', elev[~is_new])]:
        out[lab] = dict(n=int(len(e)), gt45=float(100 * (e >= 45).mean()),
                        gt75=float(100 * (e >= 75).mean()))
        if verbose:
            print(f'  {lab:20s} n={len(e):>9,}  >45deg {out[lab]["gt45"]:6.2f}%  '
                  f'>75deg {out[lab]["gt75"]:5.2f}%')
    r, b = out['rescued survivors'], out['baseline']
    if verbose and b['gt45'] > 0:
        print(f'  -> rescued particles are {r["gt45"] / b["gt45"]:.1f}x enriched >45deg, '
              f'{r["gt75"] / max(b["gt75"], 1e-9):.1f}x >75deg')
    return out
