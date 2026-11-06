# DBRV Arm B v2 — pipeline to recover rare-view particles

> ## ⛔ READ THIS BEFORE BRIEFING ANY DBRV ARM — four things that have each cost a project
>
> ### 1. ⭐⭐ THE GOVERNING RESULT — **DO NOT SELECT RARE VIEWS IN 2D.**
> Measured on hTA1-Gs, **not to be re-litigated per project** (full text in `arm_b.py`'s docstring):
> *2D CANNOT IDENTIFY VIEWING DIRECTION.* Seed enrichment, held-out validation, dense-projection
> direction matching **and reference-selection scores** were each measured to carry **no direction
> signal** — even when handed **70,894 ground-truth rare-view particles** as a seed.
> **Keep every view, remove only junk, and let the 3D classification in step 6 vet direction.**
>
> 🔁 **INDEPENDENTLY CORROBORATED 2026-09-22 (jyf1).** agentM did not read this before designing a
> Stage-A gate that selected rare views in 2D on a directional margin
> (`ccFACEON - max(ccSIDE, ccLONG)`). That instrument **cleared every negative control** — kept side
> views 0/23 positive, blur control 0.000, overlap control 0/40, noise p95 +0.016 — and its selected
> set then **passed the cone leg at 1,065x the measured replicate floor** (31,743 particles,
> 21x the sham) **and was still JUNK**: resolution ALONG the target direction did not improve
> (6.68 -> 6.86 A). **A 2D direction statistic can clear all of its controls and still be measuring
> nothing.** Two projects, two methods, same answer.
>
> ### 2. ⭐⭐ THE SEED IS NOT OPTIONAL — and it is what makes the tight Sobel cut safe.
> Step 3's seed exists to fix the v1 failure of **classifying a rescued stack IN ISOLATION**.
> jyf1 skipped it and classified the recovered set alone. **Note the dependency:** step 4 can afford
> `sobel_max=1.8` (a tight cut keeping 27-34 %) **only because the seed keeps rare-view exemplars in
> the mixture.** Without the seed that same cut is destructive — which is the real resolution of the
> apparent conflict with the standing note that *"sobel_max over-cuts rare views."* **Both are true:
> seed present -> keep the default; seed absent -> you are cutting the target view.**
> ⚠ But seed **ENRICHMENT** is NOT a selection statistic (disproven, step 3) — the seed gives the view
> something to group WITH; it never scores classes.
>
> ### 3. 🔴 TWO CODE BUGS, FIXED 2026-09-22 — both were INTERMITTENTLY INVISIBLE
> - **`project_map` integrated along an axis ORTHOGONAL to the requested direction** (`.sum(axis=0)`
>   where scipy needs `axis=2`), **and `d` must be REVERSED** from physical (x,y,z) to array (z,y,x).
>   For near-cardinal directions the old code landed on the right axis **by accident** — right answer,
>   wrong reason — which is how it survived three projects.
> - **`viewing_directions` defaulted to `transpose=True`**, which **silently returns FAKE ISOTROPY**
>   (jyf1 J2634: eff/N 0.9173 "isotropic" vs the true 0.3560). Default is now **False** = `R[:, :, 2]`,
>   the third **COLUMN**.
> 🔒 **The old guard in that docstring — "verify against `elevation_census`" — CANNOT DETECT THE BUG**:
> both conventions use |v.z| = R[2,2], so the census is **byte-identical** either way.
> **Verify on an AZIMUTHAL statistic.** And keep the **ground-truth test**: the ABUNDANT projection must
> reproduce the dataset's own measured dominant 2D class. Keep the **bounding rule** too — a projection
> down axis k must be bounded by the other two 3D extents. ⚠ Test with an **ELLIPSOID, not a box**: a
> rectangle's caliper width is its **diagonal** and fails the check for an unrelated reason.
>
> ### 4. ⚠ WHAT A CLOSED ARM ACTUALLY REQUIRES
> **Each arm purifies its OWN rescued stack, in ROUNDS, and only then merges and goes to 3D.**
> jyf1 closed both arms **before** any of that — Arm B judged uncleaned/unmerged/un-sorted, which is
> the bank's **losing** readout point (163 comparisons: a merge is a coin flip, median +0.010 A, and
> every winner ran `merge -> hetero_refine -> refine`); Arm A was closed **without ever being
> 2D-classified at all**, while Arm P and Arm T both were. **Both closures had to be withdrawn.**
> 🔒 **Do not issue an arm verdict until that arm has been cleaned in rounds, merged with the FULL
> UNDRAINED clean stack, and junk-drained in 3D.**


Recovers viewing directions that were **picked and then discarded during 2D cleaning**, by going back
through the rejected 2D classes. Spec for the built tool.

Validated on hTA1-Gs (P5/W9, 2026-08). Verified against the live job graph — this is the lineage that
actually produced the result, not a cleaned-up version of it.

```
1. project the volume along its under-populated directions   → what the missing views look like
2. match rejected class averages (every round, every branch) → candidate rare-view classes
3. mix the candidates with a pose-labelled rare-view SEED    → the view has exemplars to group with
4. 2D classify the mixture, then select                      → reference_select_2D defaults + corr filter
5. merge into the clean stack, dedup, 3D purify              → recovered particles
```

## DBRV has two options — prefer Arm B v2

| | source of the lost views | what it does with the rare projections | cost |
|---|---|---|---|
| **Arm A** | never picked | uses them as **picking templates**, re-picks every micrograph | high (full re-pick) |
| **Arm B v2** ← preferred | picked, then discarded in 2D | uses them as **matching references** against rejected 2D classes | low |

Complementary rather than competing — spatial overlap of their rescued particles was 1.4–1.9% on
hTA1-Gs. Arm B is preferred because it needs no re-pick; run Arm A when Arm B's harvest comes up
empty, which is itself the evidence that the views were never picked at all.

## Code map

| step | call | tested |
|---|---|---|
| 1 projections | `arm_b.step1_projections` → `rare_view_projections.rare_and_abundant` (`featureless` drops junk magnets) | ✅ live |
| 2 harvest + match | `arm_b.step2_candidates` → `rareview_match.harvest_rejects` / `match_classes` / `cone_margin` | ✅ live (12 rounds, 15.4 M particles, 89 s) |
| 3 seed + control | `arm_b.step3_seed` → `particles.build_seed` | ⚠ queues jobs — untested end-to-end |
| 4 select + corr filter | `arm_b.step4_select` → `refselect_scores.class_scores` / `filter_by_corr` | scores ✅ live; queueing ⚠ |
| 5 merge | `arm_b.step5_merge` → `particles.merge_dedup` / `net_new` | ⚠ queues jobs |
| 6 combine | `arm_b.step6_combine` → `volumes.compare_classes` | ✅ live (reproduces `[[0,1],[2]]`) |
| 7 confirm | `arm_b.step7_confirm` | ✅ live (reproduces 51.18% / 6.92%) |

Executed lineage on hTA1-Gs:
`J1065 (matched classes) → J1068 (extract) + J815/J816 (seed) → J1079 (union) → J1088 (class_2D) →
J1174 (reference_select_2D) → J1185 (branch dedup) → J1186 (merge w/ J616) → J1190 (hetero) → refine`

---

## Step 1 — Learn what the missing views look like

From the refinement's own `alignments3D/pose`, not from guesswork:

```python
from dbrv.rare_view_projections import viewing_directions, sphere_bins, rare_directions, project_map
v, R = viewing_directions(proj, refine_uid)     # viewing direction = R[:, :, 2] — the 3rd COLUMN
b    = sphere_bins(v, n_bins=384)               # near-uniform Fibonacci hemisphere → occupancy
rare = rare_directions(b, frac=0.05)            # the under-populated bins ARE the rare views
imgs, psize = project_map(proj, refine_uid, rare['dirs'], lowpass_A=15.0)
```

Lowpass ~15 Å so the **silhouette** drives matching rather than internal detail. Project the abundant
directions too — they are needed as the contrast set in step 2.

**Render and inspect the projections before using them.** Drop any featureless one: the empty-cone
projections are the map's *weakest* (no measured data along those directions), so a smeared, blobby
projection acts as a junk magnet that matches nearly everything. This cost Arm A 4 of 13 templates.

⛔ **DO NOT sanity-check the pose convention against an elevation census — it CANNOT detect the error.**
(This line previously said to do exactly that; corrected 2026-09-22.) Both conventions compute
|v·ẑ| = R[2,2], the one element a transpose leaves fixed, so the census is **byte-identical** either way.
**Check an AZIMUTHAL statistic instead** — effective views, azimuthal occupancy, anything using
R[0,2]/R[1,2]. End-to-end test that settles it: a 20° cone about the map's measured ABUNDANT axis must be
strongly over-occupied and about the RARE axis strongly depleted (jyf1: 27.491 % = 4.56× uniform vs
0.770 % = 0.13×). Under the wrong convention the two read indistinguishably (7.396 % vs 7.411 %).
**Then check the PROJECTIONS too:** the ABUNDANT projection must reproduce the dataset's own measured
dominant 2D class, and a projection down axis k must be bounded by the other two 3D extents.

---

## Step 2 — Find candidate rare-view classes among the rejects

Take `particles_excluded` + `templates_excluded` from **every round of every picking branch** — each
round's rejects are disjoint, so all rounds contribute.

Score each rejected class average against the **full** projection stack and rank on the margin:

```
margin = best_CC(rare-direction projections) − best_CC(all OTHER projections)
```

Scoring against the rare projections alone does not work — at 15 Å everything correlates with
everything (0.89–0.94 across the board, no ranking). The margin asks the right question: *does this
class look more like the missing view than like any view we already have?*

Margins are small by nature (neighbouring views resemble each other), so pair the score with a
**blind visual read recorded before the scores are opened**, and drop featureless blobs regardless of
score. Where they disagree, the blind read wins.

hTA1-Gs: **19 classes** kept across 9 rounds × 3 branches → **1,087,602 candidate particles**.

Two data gotchas:
- A pool reporting `particles_excluded = 0` was **never materialised**, not absent — rebuild it as the
  parent `class_2D`'s particles minus the kept class indices.
- `select_2D` preserves `blob/idx` as the original class label; `reference_select_2D` **re-numbers it
  locally**. Recover true labels by matching to the parent's `class_averages` — never assume row
  position is the class id.

---

## Step 3 — Mix with a pose-labelled rare-view seed

Build a **SEED** of particles already *known* to be rare views — selected by their **refined
elevation** from the existing refinement (here: elevation > 50°, 70,894 particles). Not shape-guessed;
these have real 3D poses.

Extract the candidates to the working box first — `extract_micrographs_multi` refuses mixed input
pixel sizes, so candidates and seed cannot be unioned before extraction. Use the CPU path
(`compute_num_gpus=0`) and **verify the output count**; the GPU path silently truncates under
contention. Then union: `remove_duplicate_particles(min_dist_A=60, shift_key='alignments2D')`.

Rationale: v1 classified rejects *alone* and failed — a handful of genuine rare-view particles adrift
in a million rejects has nothing to group with, so they are absorbed into junk and side-view classes.
The seed gives the view enough exemplars to nucleate a class (~100 particles/class floor).

Seed particles are drawn from the clean stack, so they contribute **no net-new particles** after the
step-5 merge — their role is to shape the classification.

**Status of this step, stated honestly:**
- What was *disproven*: **seed enrichment as a selection statistic.** Seed particles do not segregate
  into their own classes (segregation index 0.075–0.105, vs 0.406–0.702 for an abundant-view control),
  and candidates show no affinity for seed-enriched classes (0.71–0.92× background = chance). Do not
  select classes by seed enrichment.
- What is *untested*: whether seed-mixing improves the final outcome at all. The counterfactual —
  running step 4 on candidates classified **without** the seed — was never run. The seed is in the
  lineage that produced the result; its contribution is unmeasured, not zero.
- **Run an abundant-view CONTROL seed** (elevation < 20°, matched size) through the identical
  pipeline. It is what proved the enrichment negative was real rather than a broken pipeline, and it
  costs one extra classification per branch.

---

## Step 4 — 2D classify, then select

**Classify** the mixture: `class_2D_new`, K sized so total-N/K and the expected rare population both
clear ~100 particles/class (here K = 200 / 200 / 100 per branch), `class2D_max_res=12` (classifies on
gross shape, which is the discrimination axis), `class2D_window_inner_A` = mask diameter,
`sigma_init_factor` and `force_max` at defaults.

**Select** with `reference_select_2D` against the **current refined volume**, at **schema defaults**:

```
connections: particles=<class_2D>.particles, templates=<class_2D>.class_averages,
             volume=<current refinement>.volume
params:      window_inner_A = <mask diameter>   # MUST be set: default is null and the job FAILS
             # leave at defaults: select_mode="thresholds on Sobel scores", sobel_max=1.8,
             #                    alpha_min=0.8, res_filter=20
```

Defaults kept **27–34%** per branch. Do **not** loosen `sobel_max` or use
`select_mode='thresholds only'` — that keeps 95–99% (a no-op that re-admits the whole discarded pool)
and changes the report format so the junk filter below cannot run.

**Then drop the bright-blob junk:** discard classes with **`corr > 1.1`** — the ice / high-contrast
featureless blob signature. These correlate with everything.

> 🔴 **CALIBRATE THIS THRESHOLD PER DATASET — 1.1 IS NOT PORTABLE.** Measured on jyf1 2026-09-22, where
> the whole `corr` distribution sits higher: **1.1 cut the two CLEANEST classes in the job** (corr 1.28 and
> 1.35, holding **66.12 % of all known-good side particles**) and, in another arm, **74.36 % of the
> abundant CONTROL** (corr 1.18 — the clean side rod the control exists to track). **Only one class
> (corr 7.97) was the ice blob the rule was written for; everything else sat at 1.18–1.39.**
> On hTA1-Gs the same rule removed exactly one class at corr 1.73 — so 1.1 was a junk cut *there* and is a
> **good-density cut here**. ⇒ **Inspect the corr DISTRIBUTION first and place the threshold above the
> body of it (~2 on jyf1), or cut by eye. Report both versions.**
> ⚠ **Direction of the risk:** a mis-calibrated cut that preferentially removes clean *side* rods will
> **artificially inflate a rare-view arm's apparent purity** — the flattering error a sham-controlled
> design exists to prevent.

```python
from dbrv.refselect_scores import class_scores
rows = class_scores(proj, sel_uid)              # [{class_id, n_ptcls, sobel, corr}, ...]
bad  = [r['class_id'] for r in rows if r['corr'] > 1.1]
```

The scores exist **only inside matplotlib PDFs embedded in the job's gridfs blob** — not in
`templates_selected.cs` (only `res_A` there). `dbrv/refselect_scores.py` decodes the Type3 glyph fonts
directly, so no `pdftotext` is needed. **Assert `len(rows) == len(templates_selected)`** before
acting; a mismatch means the score/montage pairing is wrong and the filter would cut the wrong classes.

hTA1-Gs: removed 1 class (corr 1.73, 21,392 particles) — independently flagged by a blind visual read.

### ⭐ STEP 4 IS A LOOP, NOT A SINGLE PASS (human directive, 2026-09-14)

**Both arms clean in 2D, and both clean for as many ROUNDS as the stack's dirtiness needs.**
Classify → select is one round; judge the result and run it again if junk survives. The round
count is a run-time decision (subC's, under the usual loop contract — a round counter and an
exit test written down before round 1), not a fixed number in this spec.

This resolves a discrepancy that had been live across three documents:

| record | what it said about 2D purification |
|---|---|
| `dbrv_design.md` step 4 (2026-07-21) | ONE shared purification, after uniting both arms |
| `dbrv_design.md` B4 (Arm B v1) | Arm B purifies alone, in rounds, then a 2D positive gate |
| this file, before today | a single classify + select, then straight to the merge |

**The ruling: each arm purifies its OWN rescued stack, in rounds.** Arm A's harvest is dirty
because the picker is deliberately run below its normal NCC cut (a rare-view template scores low
by construction); Arm B's is dirty because the pool is what a quality gate already rejected. The
cause differs, the remedy does not.

⚠ What does NOT come back is **v1's habit of classifying the rejects in isolation** — that is the
failure the seed in step 3 exists to fix. Rounds are run on the seeded mixture, keeping the seed
in place so the rare view always has exemplars to group with.
⚠ And selection stays **direction-conditioned**: keep the quality gate loose across rounds, since
a genuine rare view is legitimately blurry. Tightening round-on-round re-creates the very
rejection that lost the view in the first place.

---

## Step 5 — Merge, dedup, 3D purify

```
3 branches → remove_duplicate_particles(min_dist_A=60, shift_key='none')
           → merge with the clean stack → remove_duplicate_particles(min_dist_A=60, shift_key='none')
```

- **`shift_key='none'`**, not `'alignments2D'`, for the merge into the clean stack: the halves have
  mismatched shift provenance and at box 256 a 2D shift is comparable to the 60 Å radius → missed
  duplicates.
- **Guard `error_field`** (defaults to `pick_stats/ncc_score`, `use_neither=False`): among duplicates
  the job keeps the *higher-scoring* copy, so a rescued particle can silently replace its curated twin
  and discard its refined pose (1.4% of the stack here). Pre-filter client-side or verify kept-copy
  identity by `uid`, and report net-new count.
- Live output slots are **`particles_kept` / `particles_rejected`**.

Run the pre-3D 2D health gate on the merged stack. hTA1-Gs: 1,015,578 total, **256,179 net-new**.

### ⭐ THE 3D PURIFICATION IS MANDATORY, FOR BOTH ARMS (human directive, 2026-09-14)

**Cleaning in 2D does not discharge it.** However many 2D rounds an arm ran, the merged stack is
purified again in 3D before anything is refined as a deliverable — this is where the rescued
particles are finally vetted, because **2D cannot identify viewing direction** and so cannot
confirm that what survived is the missing view rather than more of the abundant one. The 2D loop
removes junk; the 3D step decides whether a rare-view population is really there.
Measured here: **65% of candidates survived the 3D clean** — i.e. a third of what 2D had passed
did not survive, on a stack 2D had already declared clean.

**3D purify** — `hetero_refine`, K=3: the current refined volume + self-lowpass copies at 20 Å and
30 Å as junk sinks. `multirefine_num_rand_assign_iters=0`, `multirefine_force_hard_class=False`,
`multirefine_res_init=20` (do **not** sharpen — a sharp reference biases the sink against the low-SNR
views being rescued), C1.

> 🔴🔴 **THIS SPEC IS SELF-DEGENERATE AS WRITTEN — `res_init=20` WITH A 20 Å SINK GIVES K=2, NOT K=3.**
> `hetero_refine` filters **every** input volume to `res_init` before it starts. Lowpassing an
> already-20 Å map to 20 Å is **idempotent**, so the reference and the 20 Å self-lowpass decoy become
> **literally the same volume**. Measured 2026-09-22 (jyf1), comparing the K=3 set *at* `res_init`:
>
> | `res_init` | cc(ref, sink20) | cc(ref, sink30) | cc(sink20, sink30) |
> |---|---|---|---|
> | **20 Å** | **1.0000** ⛔ | 0.9545 | 0.9545 |
> | **6 Å** | 0.9208 ✅ | 0.8789 | 0.9545 |
>
> ⇒ **at the spec's own numbers the junk drain is a 2-class drain with a duplicated reference.**
> 🔒 **THE RULE: EVERY DECOY MUST BE STRICTLY COARSER THAN `res_init`.** Either sharpen `res_init` below
> the sharpest sink, or push the sinks coarser. Check it by correlating the filtered set *at* `res_init`
> before queueing — do not infer it from the numbers looking different.
> *(Caveat: this models the filter as a hard lowpass at `res_init`. Under a soft filter cc would fall
> below 1 but stay near-degenerate, so the conclusion survives the filter shape.)*
>
> ⚠ **AND `res_init=20` IS NOT PORTABLE TO A SMALL PROTEIN IN A LARGE MICELLE.** The spec's rationale is
> "do not sharpen — a sharp reference biases the sink against the low-SNR views being rescued", which is
> sound *in its source regime*. On jyf1 (76 kDa in a ~140 Å particle) **a reference coarser than ~8 Å
> contains only the detergent belt** — belt 1.62–2.77× brighter than the protein interior, holding 92.4 %
> of the >5 σ voxels — so a 20 Å sort classifies on **a feature every particle shares**. Use
> **`res_init` below the belt-only threshold** for such specimens (jyf1: **6**). Derive it from your own
> map, not from this document.
>
> 🔧 **WHICH OF THOSE ARE ACTUALLY NON-DEFAULT — read live 2026-09-22, because the list above reads as
> four settings and only TWO deviate.** `multirefine_res_init` **is already 20** by default, and
> `force_hard_class` is already `False`, and `symmetry` already C1 — so those three are confirmations, not
> changes. The two that genuinely need setting are:
> - **`multirefine_num_rand_assign_iters` — LIVE DEFAULT IS 5, NOT 0.** Easy to skip precisely because it
>   sits in a list of knobs that are already correct.
> - **`multirefine_N` — LIVE DEFAULT 128.** Set it to the box explicitly or it **SILENTLY DOWNSAMPLES**
>   (150 → 128 on jyf1).
>
> ✅ **And verify the decoys are real filters, not silent no-ops** — `volume_tools vol_lpf_A` can appear to
> run without changing anything. Check radial power past each cutoff. *jyf1, relative to the 30 Å shell:
> J2634 read 0.406/0.269/0.158 at 20/15/10 Å; the lp20 decoy read 0.169/**0.000**/**0.000** and the lp30
> decoy **0.000** everywhere — a genuine collapse.* Also confirm both decoys share the particles' box and
> pixel size, or the hetero is a `reference_box_mismatch` void.

**Cross-FSC the output volumes and UNION the ones that agree.** A class seeded from a decoy is not
automatically junk: self-lowpass decoys are the same shape only blurrier, so real low-SNR particles
prefer them. hTA1-Gs: class_0 (sharp seed) vs class_1 (20 Å seed) → **FSC₀.₁₄₃ 4.72 Å, CC 0.93** →
same structure, combined; class_2 (30 Å seed) ~13 Å → genuine junk, dropped. Combining recovered
**81,782 particles** the naive reading discards, and turned an apparent 52.7% loss of curated
particles into the real **10.6%**.

**Output:** 845,408 total, **166,453 net-new rescued** (65% of candidates survived the 3D clean).

---

## Confirming the recovery

Measure the **refined** elevation distribution of the surviving rescued particles from the hetero's
own `alignments3D` (`viewing_directions` + `elevation_census`).

hTA1-Gs: rescued survivors **50.27% >45°** and **6.64% >75°** against the parent stack's 17.53% and
1.70% — **2.9× and 3.9× enriched**. This is the step that confirms the recovery worked; it is also
the only direction estimate in the whole pipeline that is a *measurement* rather than an inference
from 2D.

Then refine the recovered stack (`nonuniform_refine_new`, `refine_gs_resplit=True`).
