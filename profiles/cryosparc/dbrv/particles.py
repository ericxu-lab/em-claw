"""Particle-set plumbing for DBRV — the operations that were re-hand-rolled in ~30 one-off scripts.

Every function here encodes a hazard that cost real time on hTA1-Gs:
  * chunked external-job saves with verify+retry     (master command_vis dies on big single writes)
  * seed construction by REFINED elevation           (ground-truth rare views, not shape guesses)
  * guarded merge/dedup                              (shift_key + error_field silently corrupt sets)
"""

from __future__ import annotations

import time
import numpy as np


# -------------------------------------------------------------------------------------------------
# saving particle subsets back into CryoSPARC
# -------------------------------------------------------------------------------------------------

def save_particles(cs, proj, ws, dataset, title, parent_uid=None, parent_group='particles',
                   chunk=20000, max_retries=6, verbose=True):
    """Materialise a particle subset as external job(s). Returns the list of job UIDs.

    Chunked with save-then-reload verification and exponential backoff. The shared master's
    `command_vis` OOMs or resets on multi-hundred-thousand-row single writes; chunk size 20k is the
    healthy-master value (drop to ~2k when it is degraded). `refresh=False` on save_output reduces
    master-side summary rebuilds.
    """
    prefixes = sorted({f.split('/')[0] for f in dataset.fields() if '/' in f})
    uids, n = [], len(dataset)
    for start in range(0, n, chunk):
        end = min(start + chunk, n)
        piece = dataset.slice(start, end)
        for attempt in range(max_retries):
            ext = None
            try:
                ext = cs.create_external_job(proj.uid, ws.uid,
                                             title=f'{title} [{start}:{end}) of {n}')
                if parent_uid:
                    ext.connect('particles', parent_uid, parent_group)
                ext.add_output(type='particle', name='particles_selected', slots=prefixes)
                ext.start('running')
                try:
                    ext.save_output('particles_selected', piece, refresh=False)
                except TypeError:                          # older cryosparc-tools
                    ext.save_output('particles_selected', piece)
                except Exception as e:
                    if verbose:
                        print(f'    save raised {e!r}; verifying by reload…', flush=True)
                # `refresh=False` above leaves this handle's doc STALE, so `load_output` below
                # raises TypeError("does not have any results for output ...") even though the save
                # succeeded. That TypeError escapes the guard above, so every retry failed and the
                # whole call died after creating `max_retries` orphan jobs (cryosparc-tools 4.7.1,
                # first real use of this function, 2026-09-08). Refresh before verifying.
                try:
                    ext.refresh()
                except Exception:
                    pass
                got = ext.load_output('particles_selected')
                assert len(got) == len(piece), f'expected {len(piece)} got {len(got)}'
                ext.stop(error=False)
                uids.append(ext.uid)
                if verbose:
                    print(f'    OK -> {ext.uid}  ({len(piece):,} particles)', flush=True)
                break
            except Exception as e:
                if verbose:
                    print(f'    attempt {attempt + 1}/{max_retries} FAILED: {e!r}'[:160], flush=True)
                if ext is not None:
                    try:
                        ext.kill()
                    except Exception:
                        pass
                time.sleep(15 * (attempt + 1))
        else:
            raise RuntimeError(f'could not save [{start}:{end}) after {max_retries} attempts')
    return uids


# -------------------------------------------------------------------------------------------------
# the pose-labelled seed  (step 3)
# -------------------------------------------------------------------------------------------------

def elevation_of(dataset, axis=(0, 0, 1)):
    """Per-particle elevation (deg) from `alignments3D/pose`. Requires a REFINED set."""
    from .rare_view_projections import _rodrigues
    R = _rodrigues(np.array(dataset['alignments3D/pose'], dtype=np.float64))
    v = R[:, 2, :]
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    a = np.asarray(axis, float); a /= np.linalg.norm(a)
    return np.degrees(np.arcsin(np.clip(np.abs(v @ a), 0, 1)))


def build_seed(cs, proj, ws, refine_uid, elev_min=50.0, elev_max=None, n_max=None,
               title='DBRV seed', seed=0, verbose=True):
    """Particles whose REFINED elevation is in [elev_min, elev_max) — ground-truth rare views.

    This is what v2 added over v1: a rare view needs same-view exemplars to nucleate a 2D class, and
    these are selected by their actual 3D poses rather than by any 2D shape guess.

    Build the abundant-view CONTROL the same way (`elev_min=0, elev_max=20`, matched `n_max`). The
    control is not optional — on hTA1-Gs it is the only reason we could tell that the rare seed's
    failure to segregate was a real result rather than a broken pipeline.
    """
    # `ctf` is NOT optional: the seed gets unioned with the candidates and 2D-classified, and
    # class_2D_new fails outright with "CTF parameters must be connected to perform CTF correction"
    # if any input group lacks it. Loading only blob+alignments3D silently produced a CTF-less seed
    # (measured 2026-09-08 on zll_20260904: J2213/J2214 failed on launch). `alignments2D` is kept so
    # the doc's shift_key='alignments2D' union is possible.
    d = proj.find_job(refine_uid).load_output(
        'particles', slots=['blob', 'ctf', 'alignments2D', 'alignments3D'])
    elev = elevation_of(d)
    m = elev >= elev_min
    if elev_max is not None:
        m &= elev < elev_max
    idx = np.flatnonzero(m)
    if n_max is not None and len(idx) > n_max:
        idx = np.sort(np.random.default_rng(seed).choice(idx, size=n_max, replace=False))
    sub = d.take(idx)
    if verbose:
        rng = f'{elev_min:g}-{elev_max:g}' if elev_max is not None else f'>{elev_min:g}'
        print(f'  seed {rng} deg: {len(sub):,} of {len(d):,} particles '
              f'(median elev {np.median(elev[idx]):.1f} deg)')
    uids = save_particles(cs, proj, ws, sub, title, parent_uid=refine_uid, verbose=verbose)
    return dict(job_uids=uids, n=len(sub), elev=elev[idx], indices=idx)


# -------------------------------------------------------------------------------------------------
# guarded merge / dedup  (step 5)
# -------------------------------------------------------------------------------------------------

def merge_dedup(cs, proj, ws, sources, min_dist_A=60, shift_key='none', lane=None, hostname=None,
                title='DBRV merge+dedup', micrographs=None, wait=True, verbose=True):
    """Union + dedup several particle groups. `sources` = [(job_uid, group_name), ...].

    `shift_key='none'` is the DEFAULT HERE ON PURPOSE. CryoSPARC defaults to 'alignments2D', which
    adds each particle's 2D shift to its location before comparing. When the two halves have
    different shift provenance — e.g. one re-extracted with `alignments3D` recentering, the other
    carrying fresh 2D shifts — the same physical particle is displaced in one half and not the other
    by tens of Angstroms at box 256, i.e. comparable to min_dist_A, and true duplicates are MISSED.
    Pass shift_key='alignments2D' only when every source shares one provenance.

    The `particles` input accepts UNLIMITED connections (a 144-way union ran fine), so this doubles
    as the union idiom: index-disjoint sources should report ~0 rejects, and a nonzero count on
    supposedly disjoint inputs means something upstream is wrong.

    ⚠ NOT guarded here: `error_field` defaults to `pick_stats/ncc_score` with `use_neither=False`, so
    among duplicates CryoSPARC keeps the HIGHER-SCORING copy. A rescued particle can therefore
    REPLACE its curated twin and discard that twin's refined pose (1.4% of the stack on hTA1-Gs).
    Use `net_new()` afterwards to report what actually survived and from which lineage.
    """
    job = proj.create_job(ws.uid, 'remove_duplicate_particles', title=title)
    for uid, grp in sources:
        job.connect('particles', uid, grp)
    if micrographs:
        job.connect('micrographs', micrographs[0], micrographs[1])
    job.set_param('min_dist_A', min_dist_A)
    try:
        job.set_param('shift_key', shift_key)
    except Exception:
        if verbose:
            print(f'  note: shift_key not settable on this build; left at default')
    job.queue(lane=lane, hostname=hostname)
    if wait:
        job.wait_for_done()
    kept = rejected = None
    try:
        kept = len(job.load_output('particles_kept'))
        rejected = len(job.load_output('particles_rejected'))
    except Exception:
        pass
    if verbose:
        print(f'  {job.uid}: kept {kept:,} / rejected {rejected:,}'
              if kept is not None else f'  {job.uid}: queued')
    return dict(uid=job.uid, kept=kept, rejected=rejected)


def net_new(proj, merged_uid, baseline_uid, merged_group='particles_kept',
            baseline_group='particles', verbose=True):
    """Split a merged set into net-new vs baseline-origin by `uid`, and report replacement losses.

    Also the check for the `error_field` hazard above: `lost_from_baseline` counts baseline particles
    that did NOT survive the merge, i.e. ones whose slot was taken by a higher-scoring duplicate.
    """
    base = proj.find_job(baseline_uid).load_output(baseline_group, slots=['blob'])
    ubase = set(np.asarray(base['uid']).tolist())
    merged = proj.find_job(merged_uid).load_output(merged_group, slots=['blob'])
    um = np.asarray(merged['uid'])
    is_new = np.array([u not in ubase for u in um.tolist()])
    kept_base = int((~is_new).sum())
    lost = len(ubase) - kept_base
    if verbose:
        print(f'  {merged_uid}: {len(um):,} total · net-new {int(is_new.sum()):,} · '
              f'from {baseline_uid} {kept_base:,}/{len(ubase):,}'
              + (f'  ⚠ {lost:,} ({100*lost/len(ubase):.2f}%) baseline particles replaced by '
                 f'higher-scoring duplicates' if lost else ''))
    return dict(is_new=is_new, n_new=int(is_new.sum()), n_base_kept=kept_base,
                n_base_lost=lost, uids=um)
