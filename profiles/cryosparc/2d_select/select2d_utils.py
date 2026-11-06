"""
select2d_utils.py — 2D class-average selection for CryoSPARC, no Web UI.

METHOD v4 (human directive 2026-08-26) — THREE STEPS, ALWAYS IN THIS ORDER.

  PRIMARY : RELION `relion_class_ranker` scores every 2D class. The score is an
            INSTRUMENT, not the decision: it is not portable across datasets and it
            systematically under-scores RARE VIEWS (a minority orientation has fewer
            particles -> lower SNR -> lower score). Never let it decide alone.

  STEP 1 - CHOOSE THE CUT BY EYE (mode='vision', vision_min=...)
            Read the SCORE-ORDERED sheet and find where quality actually falls off.
            For a big job read it in 50-class batches: render_score_batches(r, 50).
              * FUZZY/SMEARED classes, or an INITIAL round -> LOOSE (bottom of range).
                Do NOT tighten a fuzzy set: a higher cut deletes views without curing
                the blur. The fix is upstream (finer/high-K 2D, re-pick, re-extract).
              * CLEAN classes -> slightly tighter.
              * TOO LITTLE KEPT TO PROCEED -> go MUCH looser. Starving the stack is
                worse than carrying junk that step 2 can remove.
            Range: REFERENCE_BAND 0.15-0.3 normally; may go BELOW it (down to
            MIN_NOISY_CUT) for a very noisy dataset whose whole score distribution is
            compressed. NEVER above MAX_OPERATING_CUT=0.3 — past that you buy sharpness
            by deleting rare views, and orientation coverage costs far more than junk.
            Judge fuzzy-vs-clean on the KEPT set, not the whole job: a job whose overall
            median is poor can still have a clean top (J485: overall 29.6 A, kept 6-8 A).

  STEP 2 - QUALITY add/drop, against `2d_select_criteria.yaml` (UNIVERSAL, signature-BLIND)
              add  = good classes the ranker rejected — ESPECIALLY RARE VIEWS. This is
                     the only defence against the score's bias, so hunt for them
                     deliberately: characterise the dominant view(s) above the cut, then
                     scan BELOW the cut for structured classes of a DIFFERENT geometry.
                     Measured misses at scores far under any cut: TRPV1 #67 = 0.10 / 8 A
                     with a fully resolved TM-helix cross.
              drop = kept classes that are featureless blobs, smeared, detached-satellite
                     or scale-mismatched — whatever the ranker over-rated. Good `res_A`
                     does NOT save them (J485 #149 reports 3 A and is pure noise).

  STEP 3 - IDENTITY drop, ONLY with a `physical_signature` (base_datainfo.yaml).
            DROP-ONLY, never a rescue, and never a quality override. Two situations so
            far: (a) a CONTAMINANT that averages well — it passes quality on merit and
            must be cut at the initial 2D or it dilutes every later classification;
            (b) a mostly-BROKEN complex you are deliberately setting aside.
            An ABSENT feature is NOT evidence of a different species: the distinguishing
            lobe can be hidden by the viewing direction, so dropping "no visible lobe"
            deletes rare views. Drop only what is POSITIVELY another species.
            See .claude/agents/subC.md "SCOPE".

Typical flow:
    from select2d_utils import (relion_rank_select, threshold_report,
                                render_score_batches, guard_adjust,
                                render_kept_grid, queue_select2d)
    r = relion_rank_select(proj, 'J52')            # ranker scores (instrument only)
    threshold_report(r, stage='initial')           # evidence for the cut
    render_score_batches(r, batch=50)              # READ every class, in order
    keep = guard_adjust(r, mode='vision', vision_min=0.18,   # step 1
                        drop=[...], add=[...],               # step 2 (quality)
                        identity_drop=[...],                 # step 3 (needs signature)
                        physical_signature=sig)
    render_kept_grid(r, keep, out_name='kept_guarded.png')
    queue_select2d(cs, 'P4', 'W5', 'J52', keep)

ORDERING: the keep array is in RANKER ORDER (class_ranker score descending, ties by
blob_idx) everywhere — returned by relion_rank_select/guard_adjust, printed, and
written into the select_2D `selected_templates` param — so it reads in the same
sequence as the rendered sheets (all_scored.png, kept.png). CryoSPARC's own
select_2D plot is NOT reorderable: run_select2D.py turns the param string into a
membership test and lays its "Selected N classes:" figure out in templates-dataset
row order (particle-count descending). Use render_kept_grid()/`r['kept_png']` for a
score-ordered view of what was kept.

The heavy RELION lifting is delegated to relion_2d_selection_pipeline.py (the
faithful engine: STAR construction, numpy/torch compat shim, class_ranker call).
"""
from __future__ import annotations
import os
import re
import sys
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import mrcfile
from PIL import Image, ImageDraw, ImageOps
from cryosparc.dataset import Dataset

# RELION class_ranker engine. It lives HERE, beside this module, so
# 2d_select/ is self-contained: it used to sit at the repo root, where it was deleted as
# an orphan and restored from a backup more than once. Resolve it
# relative to this file — never via a hard-coded repo path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import relion_2d_selection_pipeline as relion_engine

# ── RELION 5.0 (self-contained launcher in relion5/; site paths come from the CryoSPARC .env) ──
_HERE = Path(__file__).resolve().parent
RELION_CLASS_RANKER = str(_HERE / 'relion5' / 'relion_class_ranker')
RELION_PYTHON = os.environ.get('EMCLAW_RELION_PYTHON', 'YOUR_RELION5_PYTHON_PATH')
DEFAULT_WORKROOT = Path(os.environ.get('EMCLAW_RELION_RUNS_DIR', str(_HERE / 'relion_runs')))
DEFAULT_CRITERIA_PATH = Path(__file__).resolve().parent / '2d_select_criteria.yaml'

# Microscope optics for the RELION class_ranker input star (data_optics block: voltage / Cs /
# amplitude contrast). These are only a LAST-RESORT FALLBACK — prefer the per-project real values,
# resolved by `_resolve_optics` from the project's base_datainfo.yaml (accelerating_voltage_kV /
# spherical_aberration_mm). Pixel size is always read from the live data (blob/psize_A), not here.
# Class-ranker score is image-feature-dominated so it's fairly insensitive to these, but a
# wrong-optics star is still incorrect in principle for a non-300 kV / non-2.7 mm dataset.
FALLBACK_VOLTAGE_KV, FALLBACK_CS_MM, FALLBACK_AMP_CONTRAST = 300.0, 2.7, 0.1
# Back-compat aliases (older callers referenced these names):
VOLTAGE_KV, CS_MM, AMP_CONTRAST = FALLBACK_VOLTAGE_KV, FALLBACK_CS_MM, FALLBACK_AMP_CONTRAST

# Optional module-level default path to the ACTIVE project's base_datainfo.yaml. A driver may set
# `select2d_utils.DATAINFO_PATH = 'projects/<name>/base_datainfo.yaml'` once so bare
# relion_rank_select(proj, uid) calls still pick up the correct optics without threading the path.
DATAINFO_PATH: str | Path | None = None


def _resolve_optics(voltage=None, cs_mm=None, amp_contrast=None, datainfo_path=None):
    """Resolve (voltage_kV, cs_mm, amp_contrast) for the RELION optics block, in priority order:
      1. explicit kwargs,
      2. the project's base_datainfo.yaml (accelerating_voltage_kV / spherical_aberration_mm) —
         from `datainfo_path` or the module-level DATAINFO_PATH,
      3. FALLBACK_* constants (300 kV / 2.7 mm / 0.1) with a printed WARNING naming what fell back.
    base_datainfo.yaml has no amplitude-contrast field, so amp_contrast stays the 0.1 default unless
    given explicitly — this is the standard cryo value and the class_ranker is insensitive to it."""
    import yaml
    src = {'voltage': 'kwarg', 'cs_mm': 'kwarg', 'amp': 'kwarg'}
    path = datainfo_path or DATAINFO_PATH
    if (voltage is None or cs_mm is None) and path and Path(path).exists():
        try:
            cfg = yaml.safe_load(open(path)) or {}
            if voltage is None and cfg.get('accelerating_voltage_kV') is not None:
                voltage = float(cfg['accelerating_voltage_kV']); src['voltage'] = f'yaml:{path}'
            if cs_mm is None and cfg.get('spherical_aberration_mm') is not None:
                cs_mm = float(cfg['spherical_aberration_mm']); src['cs_mm'] = f'yaml:{path}'
        except Exception as e:
            print(f'[optics] WARNING: could not read {path}: {e!r}')
    fell_back = []
    if voltage is None:
        voltage = FALLBACK_VOLTAGE_KV; src['voltage'] = 'FALLBACK'; fell_back.append('voltage')
    if cs_mm is None:
        cs_mm = FALLBACK_CS_MM; src['cs_mm'] = 'FALLBACK'; fell_back.append('cs_mm')
    if amp_contrast is None:
        amp_contrast = FALLBACK_AMP_CONTRAST; src['amp'] = 'default 0.1'
    if fell_back:
        print(f'[optics] WARNING: {fell_back} using FALLBACK constants '
              f'({FALLBACK_VOLTAGE_KV}kV/{FALLBACK_CS_MM}mm) — pass datainfo_path or voltage/cs_mm '
              f'to use the real per-dataset optics.')
    print(f'[optics] voltage={voltage}kV ({src["voltage"]}), cs={cs_mm}mm ({src["cs_mm"]}), '
          f'amp_contrast={amp_contrast} ({src["amp"]})')
    return voltage, cs_mm, amp_contrast


# ── Shared: class metadata ───────────────────────────────────────────────────

def build_class_info(proj, job_uid: str) -> list[dict]:
    """
    Return class dicts sorted by particle count descending.
    Each dict: {blob_idx, res_A, n_particles}. blob_idx == selected_templates value.
    Display order in any rendered grid != blob_idx; always use this map.
    """
    proj_dir = Path(proj.dir())
    cs_files = sorted((proj_dir / job_uid).glob(f'{job_uid}_*_class_averages.cs'))
    pt_files = sorted((proj_dir / job_uid).glob(f'{job_uid}_*_particles.cs'))
    assert cs_files and pt_files, f"No .cs files found in {proj_dir / job_uid}"

    ca = Dataset.load(str(cs_files[-1]))
    pt_files = [f for f in pt_files if 'passthrough' not in Path(f).name]
    assert pt_files, f"No non-passthrough particles .cs found in {proj_dir / job_uid}"
    pt = Dataset.load(str(pt_files[-1]))

    class_counts: dict[int, int] = {}
    for c in pt['alignments2D/class']:
        class_counts[int(c)] = class_counts.get(int(c), 0) + 1

    return sorted([
        {'blob_idx':    int(ca['blob/idx'][i]),
         'res_A':       float(ca['blob/res_A'][i]),
         'n_particles': class_counts.get(int(ca['blob/idx'][i]), 0)}
        for i in range(len(ca))
    ], key=lambda x: -x['n_particles'])


# `NNNNNN@/path/to.mrcs  <score> ...` from RELION's rank_model.star. The score group is a
# FULL float incl. scientific notation — RELION prints e.g. `1.058578e-04` for near-zero
# scores and a `[\d.]+` group would capture only the mantissa (see relion_rank_select).
_SCORE_RE = re.compile(r'(\d+)@\S+\s+([-+]?(?:\d+\.?\d*|\.\d+)(?:[eEdD][-+]?\d+)?)')


def order_by_score(blob_idx, scores: dict) -> list[int]:
    """
    Put a set of class blob_idx into RANKER ORDER: class_ranker score descending,
    ties broken by blob_idx ascending. This is the same sequence `all_scored.png` /
    `kept.png` are laid out in, so a printed keep-array reads left-to-right, top-to-
    bottom against the rendered sheet.

    NOTE this is a *presentation* order only. CryoSPARC's own select_2D job plot
    ("Selected N classes:") is built from the templates dataset ROW ORDER via
    `n.where(templates_selection_mask)` — for a class_2D output that is blob_idx
    ascending — so the order of the `selected_templates` string is discarded by the
    job. Verified live on P1/J78. See `render_kept_grid` for a ranker-ordered view.
    """
    seen, uniq = set(), []
    for b in blob_idx:
        b = int(b)
        if b not in seen:
            seen.add(b)
            uniq.append(b)
    return sorted(uniq, key=lambda b: (-scores.get(b, 0.0), b))


def run_dir_name(proj, job_uid: str) -> str:
    """
    Work-dir name for a ranker run: `P<proj>_J<job>` (e.g. `P1_J49`).

    `work_root` (2d_select/relion_runs) is SHARED across every project, but CryoSPARC
    job UIDs are only unique WITHIN a project — the counter is project-wide and does
    NOT restart per workspace (P5: W1=J1..J48, W2=J49..J1483, W7=J180..J238). So a
    bare `J49` dir collides across projects: J49 is a class_2D in P1, an extract in
    P3 and an import in P5, and a P1 run would overwrite a P5 run's scores/PNGs.
    The PROJECT prefix is the whole fix — the workspace adds no uniqueness, since the
    J number is already unique within a project.
    """
    return f'{proj.uid}_{job_uid}'


def class_particle_counts(proj, job_uid: str) -> dict:
    """
    {blob_idx: n_particles} for a class_2D job, from `alignments2D/class`.

    This is the quantity that decides whether a cut leaves ENOUGH TO PROCEED. Class
    count and class FRACTION are both poor proxies: on J485 the clean cut keeps 15/200
    classes = 7.5% of CLASSES but 75% of the PARTICLES, because the good classes are the
    heavily-populated ones. Conversely a cut can keep many sparsely-populated classes and
    almost no particles.
    """
    d = proj.find_job(job_uid).load_output('particles', slots=['alignments2D'])
    cls = np.asarray(d['alignments2D/class']).astype(int)
    idx, cnt = np.unique(cls, return_counts=True)
    return {int(i): int(c) for i, c in zip(idx, cnt)}


def _final_class_files(proj, job_uid: str) -> tuple[Path, Path]:
    """(class_averages.cs, class_averages.mrc) for the final iteration."""
    jdir = Path(proj.dir()) / job_uid
    cs_files = sorted(jdir.glob(f'{job_uid}_*_class_averages.cs'))
    mrc_files = sorted(jdir.glob(f'{job_uid}_*_class_averages.mrc'))
    assert cs_files and mrc_files, f"No class-average files in {jdir}"
    return cs_files[-1], mrc_files[-1]


# ── PRIMARY: RELION class_ranker selection ───────────────────────────────────

def relion_rank_select(proj, job_uid: str,
                       min_score: float = 0.2,
                       work_root: str | Path = DEFAULT_WORKROOT,
                       relion_class_ranker: str = RELION_CLASS_RANKER,
                       relion_python: str = RELION_PYTHON,
                       voltage: float | None = None,
                       cs_mm: float | None = None,
                       amp_contrast: float | None = None,
                       datainfo_path: str | Path | None = None) -> dict:
    """
    PRIMARY selection: run RELION class_ranker on a CryoSPARC 2D job.

    Optics (voltage/Cs/amp-contrast) for the RELION data_optics block are resolved by
    `_resolve_optics`: explicit kwargs > the project's base_datainfo.yaml (accelerating_voltage_kV /
    spherical_aberration_mm, via `datainfo_path` or module DATAINFO_PATH) > FALLBACK constants
    (300 kV / 2.7 mm / 0.1) with a warning. Pixel size is always read from the live data.

    Returns a result dict:
      {job_uid, n, min_score, scores{blob_idx:score}, res{blob_idx:res_A},
       keep[blob_idx>=min_score], out_dir, star, mrcs,
       scored_png, borderline_png}

    `scored_png` (all classes, keep/reject borders + score) and `borderline_png`
    (the disagreement cases) are rendered for the visual guard — Read them, then
    call guard_adjust() to loosen/tighten.
    """
    volt, cs_val, amp = _resolve_optics(voltage, cs_mm, amp_contrast, datainfo_path)
    cs_path, mrc_path = _final_class_files(proj, job_uid)
    out_dir = relion_engine.ensure_dir(Path(work_root) / run_dir_name(proj, job_uid))
    ranker_out = relion_engine.ensure_dir(out_dir / 'ranker_out')

    cs = relion_engine.load_cs_metadata(cs_path)
    mrcs_path = relion_engine.ensure_mrcs_link(mrc_path, out_dir)
    _, _, opt_star = relion_engine.write_ranker_input(
        cs, mrcs_path, out_dir, volt, cs_val, amp)
    relion_engine.run_ranker(opt_star, ranker_out, relion_class_ranker,
                             relion_python, min_score)

    # Map RELION 1-based stack row -> blob_idx (cs['blob/idx']) and read scores.
    # The score MUST be parsed with a full float pattern: RELION prints small scores
    # in scientific notation (`1.058578e-04`), and a `[\d.]+` pattern silently
    # truncates that to its mantissa `1.058578` — turning the WORST class in the set
    # into the top-ranked one and keeping it unconditionally (mantissa >= 1 always
    # clears min_score). Observed live on P1/J49 class 31. See _FLOAT_RE.
    blob_of_row = cs['blob/idx'].astype(int)
    res_of_row = cs['blob/res_A'].astype(float)
    scores, res = {}, {}
    for line in (ranker_out / 'rank_model.star').read_text().splitlines():
        m = _SCORE_RE.match(line.strip())
        if m:
            row = int(m.group(1)) - 1
            b = int(blob_of_row[row])
            scores[b] = float(m.group(2))
            res[b] = float(res_of_row[row])
    assert len(scores) == len(blob_of_row), (
        f'parsed {len(scores)} scores from rank_model.star but the job has '
        f'{len(blob_of_row)} classes — score parsing is dropping rows')

    # RANKER ORDER (score desc), not blob_idx order — matches all_scored.png / kept.png.
    keep = order_by_score([b for b, s in scores.items() if s >= min_score], scores)
    result = {
        'job_uid': job_uid, 'n': len(scores), 'min_score': min_score,
        'scores': scores, 'res': res, 'keep': keep,
        'out_dir': out_dir, 'star': ranker_out / 'class_averages.star',
        'mrcs': mrcs_path,
    }
    try:
        result['n_particles'] = class_particle_counts(proj, job_uid)
    except Exception as e:
        print(f'[n_particles] could not count per-class particles ({e!r}); '
              f'threshold_report will fall back to the class-count floor')
        result['n_particles'] = None
    result['scored_png'] = _render_scored_grid(result)
    result['borderline_png'] = _render_borderline(result)
    result['kept_png'] = render_kept_grid(result, keep)
    print(f'relion_rank_select {job_uid}: {len(keep)}/{len(scores)} '
          f'kept at min_score={min_score} -> {keep}')
    return result


# ── Threshold decision ───────────────────────────────────────────────────────

# Human directive 2026-08-26 (REVISED, supersedes the earlier 0.2-0.5 band):
#   * the operating range is 0.15-0.3 — a cut ABOVE 0.3 is TOO TIGHT and starts costing
#     RARE VIEWS (orientation coverage), which is far more expensive than carrying some
#     junk into 3D;
#   * FIRST round -> LOOSE (bottom of the range);
#   * LATER rounds -> normal or only SLIGHTLY tighter — never past MAX_OPERATING_CUT;
#   * if the classes are STILL fuzzy/smeared after several rounds, do NOT tighten
#     further. Fuzziness is not cured by a higher cut — tightening just deletes views
#     while the blur stays. The fix is upstream (finer/high-K 2D, re-pick, re-extract).
REFERENCE_BAND = (0.15, 0.3)   # normal operating range (revised 2026-08-26)
MAX_OPERATING_CUT = 0.3        # above this: flag TOO TIGHT (view loss)
MIN_NOISY_CUT = 0.05           # below the band is allowed ONLY for a very noisy dataset
FUZZY_RES_A = 15.0             # kept-median res worse than this => treat as FUZZY -> go LOOSE


def threshold_report(result: dict, thresholds=None, stage: str | None = None,
                     fuzzy: bool | None = None,
                     min_gap_A: float = 1.0, min_keep_classes: int = 5,
                     min_keep_particle_frac: float = 0.15,
                     n_particles: dict | None = None,
                     verbose: bool = True) -> list[dict]:
    """
    Decide the ranker threshold on EVIDENCE, not on the score number.

    The raw score is NOT portable across datasets — its whole distribution shifts with
    contrast. Measured: J49 (gcgr) max 0.680 / median 0.221; J1476 (transporter) max
    0.288 — J49's MEDIAN sits above J1476's BEST class. So min_score 0.2 keeps 52% of
    one job and 3.3% of the other, and RELION's own default 0.5 selects NOTHING on
    J1476. `--relative_thresholds` (score as a fraction of that job's max) does not fix
    it either: on J1476 it goes ANTI-correlated (rel 25% of max -> gap -0.55 A).

    The portable quantity is the RESOLUTION GAP = median res_A(rejected) - median
    res_A(kept). It is scale-free and falsifiable: it asks whether a cut actually
    separates good from bad. res_A is an imperfect proxy (a sharp junk class scores
    well on it) but it is INDEPENDENT of the ranker score, which is what makes it a
    valid cross-check rather than a circular one.

    POLICY (human directive 2026-08-26):
      * initial class_2D  -> relatively LOOSE (don't discard rare views early)
      * final round       -> relatively TIGHT
      * reference band    -> 0.2-0.5. Far outside it (e.g. 0.03) => DOUBLE-CHECK:
                             0.03 is p3, i.e. ~95% kept, i.e. no filter at all.
      * if the table cannot decide -> let the VISION GUARD define the set
                             (guard_adjust drop/add), do not keep tuning by feel.

    `stage` ('initial' | 'final') only annotates which end of the band to prefer.
    Returns one dict per threshold; prints the table when `verbose`.
    """
    scores, res = result['scores'], result['res']
    b = sorted(scores)
    bidx = np.array(list(b))
    s = np.array([scores[i] for i in b], float)
    r = np.array([res.get(i, np.nan) for i in b], float)
    # PARTICLE counts are the real "enough to proceed" measure — not class count, and
    # certainly not class FRACTION: J485's clean cut keeps 7.5% of CLASSES but 66% of
    # PARTICLES, because the good classes are the heavily-populated ones.
    npc = n_particles if n_particles is not None else (result.get('n_particles') or None)
    if npc:
        npc = {int(a): int(v) for a, v in npc.items()}
    tot_p = sum(npc.values()) if npc else 0
    if thresholds is None:
        thresholds = sorted({0.10, 0.125, 0.15, 0.20, 0.25, 0.30,
                             round(float(result.get('min_score', 0.2)), 3)})

    rows = []
    for t in thresholds:
        k = s >= t
        row = {'threshold': float(t), 'n_keep': int(k.sum()), 'n': len(s),
               'keep_frac': float(k.mean()), 'percentile': float((s < t).mean()),
               'in_band': REFERENCE_BAND[0] <= t <= REFERENCE_BAND[1]}
        if npc:
            kp = int(sum(npc.get(int(x), 0) for x in bidx[k]))
            row['n_part'] = kp
            row['part_frac'] = float(kp / tot_p) if tot_p else float('nan')
        else:
            row['n_part'] = None; row['part_frac'] = float('nan')
        if k.sum() == 0 or (~k).sum() == 0:
            row.update(med_res_keep=np.nan, med_res_rej=np.nan, gap=np.nan,
                       note='degenerate')
        else:
            mk, mr = float(np.nanmedian(r[k])), float(np.nanmedian(r[~k]))
            row.update(med_res_keep=mk, med_res_rej=mr, gap=mr - mk, note='')
        rows.append(row)

    # A candidate must clear BOTH bars: real separation AND enough material left to
    # work with. Gap alone is not enough — on J1476 the biggest gap (3.59 A) came from
    # keeping 2 classes of 150, which cannot seed anything. These floors are working
    # heuristics, not established constants; override them per dataset if you can say why.
    # Floor on CLASS COUNT, not class fraction. Fraction is a bad proxy for "enough to
    # proceed": on a K=200 job the clean cut can keep 7.5% of CLASSES and still carry
    # 75% of the PARTICLES (J485 at 0.275 = 15 classes = 84,608 of 112,312). A 10%
    # fraction floor disqualified exactly those cuts and forced a wrong verdict.
    def _enough(x):
        # BOTH floors must hold (human directive 2026-08-26):
        #   PARTICLE FRACTION >= min_keep_particle_frac  — enough signal to proceed;
        #   CLASS COUNT       >= min_keep_classes        — enough VIEWS. Too few views
        #     is its own failure even when the particle count looks fine: a handful of
        #     heavily-populated classes can carry most of the stack while covering only
        #     one orientation, which reconstructs badly (preferred orientation).
        ok_part = (x['part_frac'] >= min_keep_particle_frac) if npc else True
        return ok_part and x['n_keep'] >= min_keep_classes
    cand = [x for x in rows
            if not np.isnan(x['gap']) and x['gap'] >= min_gap_A
            and _enough(x) and x['in_band']]
    # v4 POLICY: LOOSE when the set is FUZZY or this is an INITIAL round; slightly
    # tighter only when the kept classes are CLEAN. Judge fuzzy on the KEPT set.
    # Evaluate fuzziness on the TIGHTEST candidate, i.e. "if I cut tight, is what
    # remains clean?". Judging at the LOOSEST candidate is wrong: any job with a junk
    # tail then reads as fuzzy and gets pushed loose (J485's top 15 are 6-8 A, but at
    # 0.15 the kept median is 22.9 A). Fuzzy must describe the set you would KEEP.
    _probe = max((x['threshold'] for x in cand), default=0.2)
    kept_med = (float(np.nanmedian(r[s >= _probe])) if (s >= _probe).any()
                else float('nan'))
    is_fuzzy = fuzzy if fuzzy is not None else (
        not np.isnan(kept_med) and kept_med > FUZZY_RES_A)
    if cand:
        capped = [x for x in cand if x['threshold'] <= MAX_OPERATING_CUT] or cand
        if stage == 'initial' or is_fuzzy:
            pick = min(capped, key=lambda x: x['threshold'])   # LOOSE
            why = 'initial round' if stage == 'initial' else f'FUZZY (kept med {kept_med:.1f} A)'
            why = f'{why} -> LOOSEST qualifying cut'
        elif stage == 'final':
            pick = max(capped, key=lambda x: x['threshold'])   # slightly tighter
            why = 'clean + final -> tightest qualifying cut (capped at 0.3)'
        else:
            pick = max(capped, key=lambda x: x['gap']); why = 'best gap'
        verdict = (f'USE {pick["threshold"]:.2f} — gap {pick["gap"]:.2f} A, keeps '
                   f'{pick["n_keep"]}/{pick["n"]} = {100*pick["keep_frac"]:.1f}%  ({why})')
        if pick['n_keep'] <= min_keep_classes:
            verdict += (f'\n  !! ONLY {pick["n_keep"]} CLASSES — at the view floor. Few views '
                        f'reconstruct badly however many particles they carry; on an INITIAL '
                        f'round especially, prefer a looser cut.')
        if npc and pick['part_frac'] < min_keep_particle_frac:
            verdict += (f'\n  !! TOO LITTLE TO PROCEED ({100*pick["part_frac"]:.1f}% of particles). v4 rule: go '
                        f'MUCH LOOSER — below the band if the distribution is compressed '
                        f'(down to {MIN_NOISY_CUT}); starving the stack is worse than '
                        f'carrying junk that the quality pass can remove.')
    else:
        why_bad = []
        best_gap = max((x['gap'] for x in rows if not np.isnan(x['gap'])), default=float('nan'))
        if np.isnan(best_gap) or best_gap <= 0:
            why_bad.append('no cut separates good from bad (gap <= 0)')
        elif best_gap < min_gap_A:
            why_bad.append(f'best gap only {best_gap:.2f} A (< {min_gap_A} A)')
        thin = [x for x in rows if not np.isnan(x['gap']) and x['gap'] >= min_gap_A
                and not _enough(x)]
        if thin:
            t0 = max(thin, key=lambda x: x['gap'])
            desc = (f'{100*t0["part_frac"]:.1f}% of particles' if npc
                    else f'{t0["n_keep"]} classes')
            why_bad.append(f'the separating cuts keep too little (best {desc})')
        med_all = float(np.nanmedian(r))
        if thin:
            why_bad.append(f'v4 rule: TOO LITTLE TO PROCEED -> go MUCH LOOSER (below the '
                           f'band, down to {MIN_NOISY_CUT}, if the score distribution is '
                           f'compressed) rather than accept a starved stack')
        verdict = ('CANNOT DECIDE FROM THE TABLE — ' + '; '.join(why_bad) +
                   f'. Overall median res {med_all:.1f} A: if kept AND rejected are both '
                   f'poor the 2D round itself is the problem and the fix is UPSTREAM. '
                   f'Do NOT keep tuning by feel — let the VISION GUARD define the set '
                   f'(guard_adjust drop/add).')

    if verbose:
        tag = {'initial': ' [initial -> prefer the LOOSE end]',
               'final': ' [final -> prefer the TIGHT end]'}.get(stage, '')
        print(f'threshold_report {result.get("job_uid","?")}: n={len(s)} '
              f'max={s.max():.3f} median={np.median(s):.3f}{tag}')
        print(f'  reference band {REFERENCE_BAND[0]}-{REFERENCE_BAND[1]} '
              f'(outside it -> DOUBLE-CHECK)')
        print(f'  {"thresh":>8} {"band":>5} {"keep":>11} {"pctile":>7} '
              f'{"res kept":>9} {"res rej":>8} {"gap":>7}')
        for x in rows:
            g = '   n/a ' if np.isnan(x['gap']) else f'{x["gap"]:6.2f}A'
            mk = '     n/a ' if np.isnan(x['med_res_keep']) else f'{x["med_res_keep"]:8.2f}A'
            mr = '    n/a ' if np.isnan(x['med_res_rej']) else f'{x["med_res_rej"]:7.2f}A'
            pf = f'{100*x["part_frac"]:6.1f}%' if not np.isnan(x['part_frac']) else '   n/a '
            print(f'  {x["threshold"]:8.2f} {"ok" if x["in_band"] else "OUT!":>5} '
                  f'{x["n_keep"]:>4}/{x["n"]:<3}{100*x["keep_frac"]:5.1f}% part{pf} '
                  f'{mk} {mr} {g}  {x["note"]}')
        print(f'  VERDICT: {verdict}')
    return rows


# ── Visual guard (secondary) ─────────────────────────────────────────────────

def guard_adjust(result: dict, mode: str = 'none',
                 vision_min: float | None = None,
                 loose_min: float = 0.15, tight_min: float = 0.25,
                 res_cap: float = 15.0,
                 drop: list[int] | None = None,
                 add: list[int] | None = None,
                 identity_drop: list[int] | None = None,
                 physical_signature: str | None = None) -> list[int]:
    """
    Apply the secondary VISUAL guard to a relion_rank_select() result.

    Two steps, both vision-driven, in this order:
      (1) WHERE TO CUT — read `result['scored_png']`, which is laid out in SCORE
          ORDER, and find where class quality actually falls off. That boundary IS
          the threshold: pass it as `mode='vision', vision_min=<score>`. This is
          preferred over the fixed loose/tight numbers because the score is not
          portable across datasets (see threshold_report).
      (2) PUT BACK / DROP — per-class corrections on top: `add` rescues a rejected
          class with a clear protein pattern, `drop` removes a kept class that is a
          featureless blob / detached-satellite / smear the ranker over-rated.

    A cut plus explicit exceptions is AUDITABLE; a freehand per-class set is not.
    (Measured: a purely per-class vision set on J49 was reproducible by NO threshold
    — kept scores ran down to 0.159 while dropped ones reached 0.258.)

    mode:
      'vision' -> cut at `vision_min`, chosen by READING the score-ordered sheet.
                  PREFERRED. The chosen value is reported with its percentile, keep
                  fraction and resolution gap, and flagged if outside REFERENCE_BAND.
      'none'   -> ranker keep set unchanged (just apply explicit drop/add).
      'loose'  -> legacy fixed cut at loose_min. NOTE loose_min=0.15 sits BELOW the
                  0.2 band floor, i.e. it silently applies a cut the standing rule
                  says needs a double-check. Prefer mode='vision'.
      'tight'  -> legacy fixed cut at tight_min + drop classes worse than res_cap A.

    `drop` / `add` are explicit blob_idx and ALWAYS win over the cut.
    """
    scores, res = result['scores'], result['res']
    drop = set(drop or [])
    add = set(add or [])

    if mode == 'vision':
        if vision_min is None:
            raise ValueError("mode='vision' requires vision_min=<score you read off "
                             "the score-ordered sheet>")
        cut = float(vision_min)
        keep = {b for b, s in scores.items() if s >= cut}
    elif mode == 'loose':
        cut = loose_min
        keep = {b for b, s in scores.items() if s >= loose_min}
    elif mode == 'tight':
        cut = tight_min
        keep = {b for b, s in scores.items() if s >= tight_min and res.get(b, 99) <= res_cap}
    elif mode == 'none':
        cut = float(result.get('min_score', float('nan')))
        keep = set(result['keep'])
    else:
        raise ValueError(f"mode must be 'none'|'vision'|'loose'|'tight', got {mode!r}")

    # TRIPWIRE — always report WHERE the cut landed, for every mode. A bare score is
    # unreadable: AT1R's 0.03 looked strict and was p3 (94.8% kept, i.e. no filter).
    if not np.isnan(cut):
        s = np.array(list(scores.values()), float)
        k = s >= cut
        pct, frac = 100 * (s < cut).mean(), 100 * k.mean()
        band = ('in band' if REFERENCE_BAND[0] <= cut <= REFERENCE_BAND[1]
                else ('TOO TIGHT' if cut > MAX_OPERATING_CUT
                      else ('below band (noisy-case allowance)' if cut >= MIN_NOISY_CUT
                            else 'TOO LOOSE')))
        gap = ''
        if k.sum() and (~k).sum():
            b = sorted(scores)
            r = np.array([res.get(i, np.nan) for i in b], float)
            sb = np.array([scores[i] for i in b], float)
            kb = sb >= cut
            gap = (f', res gap {np.nanmedian(r[~kb]) - np.nanmedian(r[kb]):.2f} A')
        print(f'  cut {cut:.3f} = p{pct:.0f}, keeps {int(k.sum())}/{len(s)} = {frac:.1f}%'
              f'{gap}  [{band} {REFERENCE_BAND[0]}-{REFERENCE_BAND[1]}]')
        if band == 'TOO TIGHT':
            print(f'  !! TOO TIGHT: {cut:.3f} > {MAX_OPERATING_CUT}. Tightening past this '
                  f'costs RARE VIEWS (orientation coverage) — far more expensive than '
                  f'carrying some junk into 3D. Loosen unless you can justify it.')
        elif band == 'TOO LOOSE':
            print(f'  !! DOUBLE-CHECK: {cut:.3f} < {REFERENCE_BAND[0]} — near-no-filter territory.')
        if k.sum():
            b = sorted(scores)
            rk = np.array([res.get(i, np.nan) for i in b], float)
            sk = np.array([scores[i] for i in b], float)
            med_keep = float(np.nanmedian(rk[sk >= cut]))
            if med_keep > FUZZY_RES_A:
                print(f'  !! STILL FUZZY: kept-median res {med_keep:.1f} A (> {FUZZY_RES_A}). '
                      f'Do NOT tighten further — a higher cut deletes views without curing '
                      f'the blur. Fix upstream (finer/high-K 2D, re-pick, re-extract).')

    # STEP 2 - QUALITY (2d_select_criteria.yaml, universal, signature-BLIND).
    n_cut = len(keep)
    keep = (keep | add) - drop
    n_q = len(keep)

    # STEP 3 - IDENTITY (physical_signature). DROP-ONLY, and only where the class is
    # POSITIVELY another species (wrong size/shape/contaminant, or a broken complex you
    # are deliberately setting aside). A merely ABSENT feature is NOT evidence: the
    # target's distinguishing lobe can be hidden by the viewing direction, so dropping
    # "no visible lobe" deletes rare views. See subC.md SCOPE + [[vision_criterion_inversion]].
    identity_drop = set(identity_drop or [])
    if identity_drop and not physical_signature:
        raise ValueError('identity_drop requires physical_signature — the identity gate '
                         'is only valid when the human supplied the specimen description')
    keep = keep - identity_drop

    keep = order_by_score([b for b in keep if b in scores], scores)
    print(f"guard_adjust(mode={mode}): cut {n_cut} -> quality {n_q} "
          f"(+{len(add)} add, -{len(drop)} drop) -> identity -{len(identity_drop)} "
          f"=> {len(keep)} kept")
    if identity_drop:
        print(f"  identity drops (signature-based): {sorted(identity_drop)}")
    print(f"  {keep}")
    return keep


# ── Rendering (for the visual guard) ─────────────────────────────────────────

def _label_font(cell_w: int):
    """A TTF sized to the panel (~1/16 of the cell width) so labels stay readable on a
    5x-upscaled sheet — PIL's default bitmap font is a fixed ~11 px and vanishes."""
    from PIL import ImageFont
    size = max(11, int(cell_w / 16))
    try:
        import matplotlib
        ttf = (Path(matplotlib.__file__).parent /
               'mpl-data/fonts/ttf/DejaVuSans-Bold.ttf')
        return ImageFont.truetype(str(ttf), size), size
    except Exception:
        return ImageFont.load_default(), 11


def _norm_thumb(img: np.ndarray, w: int, h: int, scale: int) -> Image.Image:
    lo, hi = np.percentile(img, [1, 99])
    hi = max(hi, lo + 1e-6)
    arr = (np.clip((img - lo) / (hi - lo), 0, 1) * 255).astype('uint8')
    im = ImageOps.autocontrast(Image.fromarray(arr).convert('L'))
    return im.resize((w * scale, h * scale), Image.NEAREST).convert('RGB')


def _render_scored_grid(result: dict, scale: int = 4, ncols: int = 10) -> Path:
    """All classes sorted by score, green=kept / red=rejected, with score+res."""
    with mrcfile.open(str(result['mrcs']), permissive=True) as m:
        data = m.data.copy()
    scores, res = result['scores'], result['res']
    keep = set(result['keep'])
    order = order_by_score(range(len(data)), scores)
    h, w = data.shape[1:]
    font, fsize = _label_font(w * scale)
    pad = fsize + 8
    cellw, cellh = w * scale, h * scale + pad
    rows = (len(order) + ncols - 1) // ncols
    sheet = Image.new('RGB', (ncols * cellw, rows * cellh), (20, 20, 20))
    dr = ImageDraw.Draw(sheet)
    for i, b in enumerate(order):
        x, y = (i % ncols) * cellw, (i // ncols) * cellh
        sheet.paste(_norm_thumb(data[b], w, h, scale), (x, y + pad))
        col = (40, 200, 80) if b in keep else (210, 60, 60)
        dr.rectangle([x, y + pad, x + w * scale - 1, y + h * scale + pad - 1],
                     outline=col, width=3)
        dr.text((x + 4, y + 3), f"#{b} {scores.get(b,0):.2f} {res.get(b,0):.0f}A",
                fill=col, font=font)
    out = result['out_dir'] / 'all_scored.png'
    sheet.save(out)
    return out


def render_kept_grid(result: dict, keep: list[int] | None = None,
                     scale: int = 5, ncols: int = 10,
                     out_name: str = 'kept.png') -> Path | None:
    """
    The KEPT classes only, laid out in RANKER ORDER (score descending) — the sheet
    a human reads to sanity-check a selection, best class first, worst kept class
    last (the borderline tail lands together at the end of the sheet).

    This is the display CryoSPARC cannot give you: its select_2D "Selected N
    classes:" plot is fixed to the templates dataset row order (particle-count
    descending), regardless of the `selected_templates` string order.

    Each panel is labelled `rank. #blob_idx  score  res` — the rank numbers run in
    the same sequence as the keep array printed by relion_rank_select() /
    guard_adjust() and passed to queue_select2d().

    Pass `keep` to render a guard-adjusted set; defaults to result['keep'].
    """
    scores, res = result['scores'], result['res']
    order = order_by_score(result['keep'] if keep is None else keep, scores)
    if not order:
        return None
    with mrcfile.open(str(result['mrcs']), permissive=True) as m:
        data = m.data.copy()
    h, w = data.shape[1:]
    font, fsize = _label_font(w * scale)
    pad = fsize + 8
    cellw, cellh = w * scale, h * scale + pad
    rows = (len(order) + ncols - 1) // ncols
    sheet = Image.new('RGB', (ncols * cellw, rows * cellh), (20, 20, 20))
    dr = ImageDraw.Draw(sheet)
    col = (40, 200, 80)
    for i, b in enumerate(order):
        x, y = (i % ncols) * cellw, (i // ncols) * cellh
        sheet.paste(_norm_thumb(data[b], w, h, scale), (x, y + pad))
        dr.rectangle([x, y + pad, x + w * scale - 1, y + h * scale + pad - 1],
                     outline=col, width=3)
        dr.text((x + 4, y + 3),
                f"{i+1}. #{b} {scores.get(b,0):.2f} {res.get(b,0):.0f}A",
                fill=col, font=font)
    out = result['out_dir'] / out_name
    sheet.save(out)
    print(f'kept grid ({len(order)} classes, ranker order): {out}')
    return out


def render_score_batches(result: dict, batch: int = 50, scale: int = 4, ncols: int = 10,
                         out_prefix: str = 'batch') -> list[Path]:
    """
    The score-ordered sheet split into batches of `batch` classes (default 50), so a
    vision pass can actually READ every class on a large job.

    Human directive 2026-08-26: when the class count is too big to judge in one sheet,
    cut it into 50-class batches and read them in order. A 200- or 300-class job read
    as one image is not read at all — panels shrink below the size where secondary
    structure is visible, and the tail never gets looked at, so `add` decisions
    silently cover only the top of the list.

    Panels stay in RANKER ORDER across batches (batch 1 = ranks 1-50, etc.) and are
    labelled `rank. #blob score res`, so a cut chosen in batch N is directly readable
    against the batch boundaries.
    """
    scores, res = result['scores'], result['res']
    order = order_by_score(list(scores), scores)
    with mrcfile.open(str(result['mrcs']), permissive=True) as m:
        data = m.data.copy()
    h, w = data.shape[1:]
    font, fsize = _label_font(w * scale)
    pad = fsize + 8
    cellw, cellh = w * scale, h * scale + pad
    out = []
    for bi in range(0, len(order), batch):
        chunk = order[bi:bi + batch]
        rows = (len(chunk) + ncols - 1) // ncols
        sheet = Image.new('RGB', (ncols * cellw, rows * cellh), (20, 20, 20))
        dr = ImageDraw.Draw(sheet)
        for i, b in enumerate(chunk):
            x, y = (i % ncols) * cellw, (i // ncols) * cellh
            sheet.paste(_norm_thumb(data[b], w, h, scale), (x, y + pad))
            dr.rectangle([x, y + pad, x + w * scale - 1, y + h * scale + pad - 1],
                         outline=(90, 90, 90), width=2)
            dr.text((x + 4, y + 3),
                    f"{bi + i + 1}. #{b} {scores.get(b,0):.2f} {res.get(b,0):.0f}A",
                    fill=(230, 230, 90), font=font)
        p = result['out_dir'] / f'{out_prefix}_{bi//batch + 1:02d}_ranks{bi+1}-{bi+len(chunk)}.png'
        sheet.save(p)
        out.append(p)
    print(f'render_score_batches: {len(out)} batches of <={batch} '
          f'({len(order)} classes) -> {result["out_dir"]}')
    return out


def _render_borderline(result: dict, scale: int = 5,
                       res_cap: float = 15.0, good_res: float = 9.0) -> Path | None:
    """
    The disagreement cases worth a human glance:
      kept-but-poor-res     (score>=cut & res>res_cap)    -> tighten candidates
      rejected-but-good-res (score<cut  & res<good_res)   -> loosen / add-back candidates
    """
    scores, res = result['scores'], result['res']
    keep = set(result['keep'])
    focus = sorted(
        [b for b in scores
         if (b in keep and res.get(b, 0) > res_cap) or (b not in keep and res.get(b, 99) < good_res)],
        key=lambda b: res.get(b, 0))
    if not focus:
        return None
    with mrcfile.open(str(result['mrcs']), permissive=True) as m:
        data = m.data.copy()
    h, w = data.shape[1:]
    font, fsize = _label_font(w * scale)
    pad = fsize + 8
    sheet = Image.new('RGB', (len(focus) * w * scale, h * scale + pad), (20, 20, 20))
    dr = ImageDraw.Draw(sheet)
    for i, b in enumerate(focus):
        x = i * w * scale
        sheet.paste(_norm_thumb(data[b], w, h, scale), (x, pad))
        col = (40, 200, 80) if b in keep else (210, 60, 60)
        dr.rectangle([x, pad, x + w * scale - 1, h * scale + pad - 1], outline=col, width=3)
        dr.text((x + 4, 3), f"#{b} {scores.get(b,0):.2f} {res.get(b,0):.0f}A",
                fill=col, font=font)
    out = result['out_dir'] / 'borderline.png'
    sheet.save(out)
    return out


# ── CryoSPARC native render (optional visual reference) ──────────────────────

def extract_native_image(proj, job_uid: str, out_dir: str | Path = '.') -> Path:
    """Extract CryoSPARC's native 2D class render (final iteration) from gridfs_data."""
    proj_dir = Path(proj.dir())
    gf_path = proj_dir / job_uid / 'gridfs_data' / 'gridfsdata_0'
    assert gf_path.exists(), f"gridfs file not found: {gf_path}"
    data = gf_path.read_bytes()
    PNG_START, PNG_END = b'\x89PNG\r\n\x1a\n', b'IEND\xaeB`\x82'

    results, pos = [], 0
    while True:
        s = data.find(PNG_START, pos)
        if s == -1:
            break
        e = data.find(PNG_END, s)
        if e == -1:
            break
        end = e + 8
        context = data[max(0, s - 300):s].decode('latin-1', errors='ignore')
        names = re.findall(r'[\w./\-]+\.png', context)
        results.append((names[-1] if names else '', data[s:end]))
        pos = end

    target, max_iter = None, -1
    for fname, chunk in results:
        if '2d_classes' in fname and 'iteration' in fname:
            mm = re.search(r'iteration_(\d+)', fname)
            if mm and int(mm.group(1)) > max_iter:
                max_iter, target = int(mm.group(1)), (fname, chunk)
    assert target, "No 2D class visualization found in gridfs"

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / target[0].split('/')[-1]
    out_path.write_bytes(target[1])
    print(f'Extracted native image: {out_path.name} (iteration {max_iter})')
    return out_path


def label_native_image(native_image_path: str | Path, proj, job_uid: str,
                       result: dict | None = None,
                       out_path: str | Path = None, ncols: int = 10) -> Path:
    """
    Overlay labels on CryoSPARC's native render so Claude can do the VISION guard
    on the cleanest (mask-filtered) image while seeing the parameters.

    Each panel is annotated `#blob_idx  Np  res` (display position -> blob_idx via
    build_class_info, the same particle-count-descending order CryoSPARC renders).
    If `result` (from relion_rank_select) is given, the label also shows the ranker
    score and is colored GREEN if the ranker kept it / RED if rejected — so the
    visual pass judges protein pattern first, then nudges the ranker set.
    """
    import matplotlib.image as mpimg
    if out_path is None:
        out_path = Path(native_image_path).parent / f'{job_uid}_native_labeled.png'

    class_info = build_class_info(proj, job_uid)
    scores = (result or {}).get('scores', {})
    keep = set((result or {}).get('keep', []))
    native = mpimg.imread(str(native_image_path))
    H, W = native.shape[:2]
    nrows = (len(class_info) + ncols - 1) // ncols
    cw, ch = W / ncols, H / nrows

    fig, ax = plt.subplots(figsize=(22, 12))
    ax.imshow(native); ax.axis('off')
    for pos, info in enumerate(class_info):
        b = info['blob_idx']
        row, col = pos // ncols, pos % ncols
        nk = info['n_particles']
        ns = f'{nk/1000:.1f}k' if nk >= 1000 else str(nk)
        if result is not None:
            color = '#00ff66' if b in keep else '#ff4444'
            txt = f'#{b} {scores.get(b,0):.2f} {ns} {info["res_A"]:.0f}A'
        else:
            res = info['res_A']
            color = '#00ff88' if res <= 6 else ('#ffee00' if res <= 12 else '#ff4444')
            txt = f'#{b} {ns} {res:.0f}A'
        ax.text(col*cw + cw*0.03, row*ch + ch*0.05, txt,
                fontsize=7, fontweight='bold', color=color, va='top', ha='left',
                bbox=dict(boxstyle='round,pad=0.12', fc='black', alpha=0.7, lw=0))
    plt.tight_layout(pad=0)
    fig.savefig(str(out_path), dpi=250, bbox_inches='tight', facecolor='black')
    plt.close()
    print(f'Labeled native image saved: {out_path}')
    return Path(out_path)


# ── Create + queue select_2D job ─────────────────────────────────────────────

def _select2d_external_fallback(cs, proj, ws, source_job_uid: str,
                                keep_blob_idx: list[int], title: str) -> object:
    """
    FALLBACK for large stacks (multi-million particles): the built-in `select_2D`
    interactive job runs on the CryoSPARC MASTER process, which on this server has
    been observed to fail deterministically with
        OSError: [Errno 12] Cannot allocate memory
        ... numpy.core.memmap ... cryosparc_tools/cryosparc/dataset.py load()
    when mmap-loading a multi-GB `..._particles.cs` passthrough (e.g. ~6.9M
    particles / 3.4 GB) — a master-node resource limit, NOT a data/param problem,
    and NOT transient (fails identically on retry). Confirmed: loading the exact
    same file client-side (this worker node) with `cryosparc.dataset.Dataset.load`
    succeeds in ~25 s using plain RAM.

    Workaround: do the class-membership filtering CLIENT-SIDE (this process) using
    the `alignments2D/class` field (0-based, == blob_idx), then register the
    result as a CryoSPARC `external` job with 4 outputs matching select_2D's
    contract: particles_selected / particles_excluded / templates_selected /
    templates_excluded. Downstream jobs (template_picker_gpu.templates,
    topaz_train.particles, extract, class_2D_new, dedup) consume this exactly
    like a normal select_2D output — verified end-to-end on hta1_11755 J342.
    """
    src = proj.find_job(source_job_uid)
    print(f'[select2d fallback] loading {source_job_uid} particles + class_averages client-side...')
    part_path = None
    for r in src.doc.get('output_results', []):
        if r.get('group_name') == 'particles' and not r.get('passthrough'):
            part_path = None  # discovered via load_output below instead (simpler/robust)
            break
    d = src.load_output('particles')
    ca = src.load_output('class_averages')

    keep = sorted(set(int(i) for i in keep_blob_idx))
    cls = np.asarray(d['alignments2D/class'])
    mask = np.isin(cls, keep)
    d_sel, d_rej = d.mask(mask), d.mask(~mask)

    ca_idx = np.asarray(ca['blob/idx'])
    ca_mask = np.isin(ca_idx, keep)
    ca_sel, ca_rej = ca.mask(ca_mask), ca.mask(~ca_mask)

    part_prefixes = sorted({f.split('/')[0] for f in d.fields() if '/' in f})
    print(f'[select2d fallback] particles kept {len(d_sel):,} / {len(d)} rej {len(d_rej):,}; '
          f'classes kept {len(ca_sel)} / {len(ca)} (slots: {part_prefixes})')

    ext = cs.create_external_job(proj.uid, ws.uid,
        title=f'{title} [external fallback: master mmap OOM]')
    ext.connect('particles', source_job_uid, 'particles')
    ext.connect('templates', source_job_uid, 'class_averages')
    ext.add_output(type='particle', name='particles_selected', slots=part_prefixes)
    ext.add_output(type='particle', name='particles_excluded', slots=part_prefixes)
    ext.add_output(type='template', name='templates_selected', slots=['blob'])
    ext.add_output(type='template', name='templates_excluded', slots=['blob'])
    ext.start('running')
    # save_output occasionally raises a spurious 500 from command_vis even though
    # the dataset was actually persisted (observed live) — verify by re-loading
    # rather than trusting the exception.
    for name, ds in [('particles_selected', d_sel), ('particles_excluded', d_rej),
                     ('templates_selected', ca_sel), ('templates_excluded', ca_rej)]:
        try:
            ext.save_output(name, ds)
        except Exception as e:
            print(f'[select2d fallback] save_output({name}) raised {e!r}; verifying via reload...')
        try:
            got = ext.load_output(name)
            assert len(got) == len(ds), f'{name}: expected {len(ds)} got {len(got)}'
            print(f'[select2d fallback] {name}: {len(got):,} rows confirmed saved')
        except Exception as e2:
            raise RuntimeError(f'select2d fallback: output {name} did not persist ({e2!r})') from e2
    ext.stop(error=False)
    print(f'[select2d fallback] {ext.uid} completed (external job substituting for select_2D)')
    return ext


def queue_select2d(cs, proj_uid: str, ws_uid: str,
                   source_job_uid: str,
                   keep_blob_idx: list[int],
                   title: str = 'select_2D (RELION ranker + guard)',
                   max_retries: int = 1) -> object:
    """
    Create a select_2D job with selected_templates pre-filled and queue it.
    selected_templates takes blob_idx (NOT display rank). job.queue() with NO
    lane= (interactive job rule). Returns the completed job object.

    The caller's ORDER of `keep_blob_idx` is preserved in the param string (only
    duplicates are dropped), so a ranker-ordered keep list from
    relion_rank_select()/guard_adjust() stays readable in the job's parameter
    field and in this function's printout. Be aware the ORDER IS COSMETIC for
    CryoSPARC itself: run_select2D.py parses the string into a membership test and
    builds its outputs + its "Selected N classes:" plot from the templates dataset
    row order (particle-count descending). To eyeball the kept classes in ranker
    order, read `result['kept_png']` (see render_kept_grid).

    On large stacks the interactive select_2D job can fail deterministically with
    a master-node mmap OOM (see `_select2d_external_fallback`). After
    `max_retries` failed attempts, falls back automatically to a client-side
    external-job filter that reproduces the same 4 outputs.
    """
    proj = cs.find_project(proj_uid)
    ws = proj.find_workspace(ws_uid)
    seen, ordered_keep = set(), []
    for i in keep_blob_idx:                       # preserve caller order (= ranker order)
        i = int(i)
        if i not in seen:
            seen.add(i)
            ordered_keep.append(i)
    selected_str = ','.join(str(i) for i in ordered_keep)
    job = ws.create_job(
        'select_2D',
        connections={
            'particles': (source_job_uid, 'particles'),
            'templates': (source_job_uid, 'class_averages'),
        },
        params={'selected_templates': selected_str},
        title=title,
    )
    print(f'Created {job.uid} — {len(ordered_keep)} classes selected (ranker order): {ordered_keep}')
    for attempt in range(max_retries + 1):
        if attempt > 0:
            job.clear()
        job.queue()
        print(f'Queued (attempt {attempt+1}/{max_retries+1}), waiting...')
        status = job.wait_for_done()
        job.refresh()
        if status == 'completed':
            sel = job.load_output('particles_selected')
            print(f'✓ {job.uid} completed: {len(sel):,} particles selected')
            return job
        print(f'✗ {job.uid} attempt {attempt+1} ended with status: {status}')
    print(f'select_2D {job.uid} failed after {max_retries+1} attempts -> '
          f'falling back to external-job manual filter (master mmap OOM workaround)')
    return _select2d_external_fallback(cs, proj, ws, source_job_uid, keep_blob_idx, title)
