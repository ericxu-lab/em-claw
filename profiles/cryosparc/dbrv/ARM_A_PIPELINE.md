# DBRV Arm A — recover rare views that were NEVER PICKED

> **Written 2026-09-22, restructured the same day to the human's own statement of the method.**
> Until now Arm A had **no record in this toolkit** — no driver, no doc, only scattered mentions in one
> project's design notes. The first full Arm A pilot (jyf1) was therefore run on improvised terms.
> Steps 2–4 are **shared with Arm B**; `ARM_B_v2_PIPELINE.md` is authoritative for them, **including its
> header block of four hazards — read that first.**

## The method, in four steps

```
1. PICK the rare view       (a) template picking from rare-view projections
                           AND (b) topaz trained on rare-view particles     <- BOTH, not either/or
2. 2D CLEAN                 each BRANCH separately: reference_select_2D, in ROUNDS, WITH the seed
   then COMBINE the CLEANED branches + remove_duplicate_particles
3. MERGE                    union with the original dataset + remove_duplicate_particles
4. 3D CLEAN                 hetero junk-drain — MANDATORY; where direction is finally vetted
```

> ⭐ **RUN BOTH PICKERS — this is the BREADTH principle, and it is how the pool grows.**
> *"To ENLARGE the particle pool, use breadth — looser selection is not optimal (it re-admits junk). Run
> parallel picking methods, each as its own branch with its own 2D clean and select, then combine the
> branches + `remove_duplicate_particles` — complementary pickers grow the deduped union without lowering
> quality."* The two routes fail for **different and non-overlapping reasons** (see below), so they miss
> different particles, which is exactly the condition under which a deduped union is worth more than
> either alone. **Each branch is cleaned SEPARATELY first** — combining raw picks pools the junk too.
>
> 🔒 **BUT AN IDENTITY GATE MUST PASS BEFORE A BRANCH JOINS THE UNION.** Breadth combines pickers that
> find the **same object** by different means. *A branch that found a DIFFERENT object does not add
> breadth — it injects a contaminant, and the union hides it better than the branch did.* **jyf1: the
> topaz branch picked a non-target object (0.00 % abundant-view mass vs 43.61 % in the main lineage) and
> must be EXCLUDED from any union on those grounds, not merged in for breadth.** Run leg (b) of the pilot
> gate on every branch and admit only the ones that pass.

**Arm B is tried first** (far cheaper — no re-pick). Arm A answers what Arm B cannot: **is the view
physically present on the grid at all?**

| | **Arm A (re-pick)** | Arm B (rejected 2D averages) |
|---|---|---|
| Recovers | **views never picked at all** | views picked then discarded at 2D |
| Cost | **re-pick + extract every micrograph (heavy)** | re-read existing class averages (cheap) |
| Main risk | **junk-magnet templates / a detector that learns the wrong object** | the quality gate re-rejects the same rare views |
| Diagnostic value | **is the view on the grid?** | did OUR CLEANING throw the view away? |

---

## ⚠ The caveat both picking routes inherit
**The empty-cone projections are the map's WEAKEST** — the missing cone means almost no measured
information along those directions, so a projection down it is dominated by smearing from the
over-represented views. **Arm A turns that into a picker**, committing the re-pick to the least
trustworthy part of the map.
- **Render and inspect every projection; drop featureless ones** — a smeared blob is a **junk magnet**.
  *This cost Arm A 4 of 13 templates on hTA1-Gs.*
- Lowpass hard (**~15 Å**) so the silhouette drives matching.
- **Verify the frame on an AZIMUTHAL statistic, never an elevation census**, and confirm the ABUNDANT
  projection reproduces the dataset's measured dominant 2D class. *jyf1: this caught a live LONG-vs-MID
  axis error, 0.44× uniform (fail) vs 4.55× (correct).*
- **The circularity breaks only at the END**: a rescue is real when the map becomes measurably more
  ISOTROPIC — never because picks matched the template well.
- **Re-project the NEW map each cycle**; budget **2–4+ cycles**.

---

## Step 1(a) — TEMPLATE PICKING from rare-view projections
`import_templates` → `template_picker_gpu`. Re-derive `diameter` from the measured particle. Record
`lowpass_res_template`. Then `extract_micrographs_multi` — **verify the output count**, the GPU path
silently truncates. *(jyf1: 332,508/379,973 = 87.51 % kept, pure edge rejection ⇒ no truncation.)*

**🔴 MEASURED OUTCOME, TWICE, AND THE SAME BOTH TIMES (jyf1):**
- **Templates = noisy experimental 2D class averages:** **97.6 % junk**, **zero** end-on surviving, best
  class a **SIDE** view.
- **Templates = noise-free map projections** (the difference that justified re-running it): junk improved
  to **79.7–82.1 %** — a real gain — but **the outcome was identical: best class a SIDE view, no coherent
  end-on class added, and E/S recall 0.48, i.e. it ANTI-ENRICHES the view it was built to find.**
- 🔒 **The common cause, measured:** *at the 20 Å picking lowpass the face-on projection has **NO
  correlation advantage anywhere on the micrographs — not even at TRUE face-on particle positions.***
  **A noise-free template cannot rescue a view whose projection carries no contrast advantage.**

## Step 1(b) — TOPAZ trained on rare-view particles
Train a detector on particles already known to be the rare view, then extract over everything.

**🔴 THE PARAMETER THAT DECIDES IT: `topaz_train.num_particles` IS A PU-LEARNING CLASS PRIOR** — the
expected number of **TRUE PARTICLES PER MICROGRAPH** for the **LABELLED class on the TRAINING
micrographs**. It is **not a specimen constant**; it is a property of the *(positive set, micrograph set)*
pair, and it must be **recomputed whenever either changes**.

| value | where it came from | verdict |
|---|---|---|
| 300 | copied from a bank precedent | ⛔ wrong by **~150×** |
| 19–22 | the project's own **all-particle** rate | ⛔ **still wrong by ~10×** |
| **2** | the **labelled class's own** rate | ✅ correct |

⭐⭐ **The middle row is the trap, and it survives the obvious correction.** "Don't copy the constant,
calibrate from your own data" lands on 19 — the rate of *all* clean particles. Label **only** a rare view
while declaring 19/micrograph and PU learning concludes ~17 further positives per micrograph are hiding in
the unlabelled set — and **the most particle-like unlabelled objects are the abundant views.** Topaz then
relearns a **general picker**, the view targeting evaporates, **and every surface number still looks
healthy.**

**⚠ AND IT SILENTLY SWITCHES THE LEARNING ALGORITHM.** If the declared prior falls **below** the observed
labelled fraction, Topaz abandons **GE-binomial for PN** — a different algorithm, chosen by the library,
never requested, **recorded only in the log**. *(jyf1: pi 0.00019961 vs observed 0.00022742 ⇒ switched.)*
The two fail in opposite directions for a view-targeted picker: **GE-binomial** promotes the
highest-scoring unlabelled regions — the abundant, high-contrast views — diluting the targeting; **PN**
treats all unlabelled as negative, making unlabelled rare-view particles contradictory negatives.
🔒 **Read the job's own log for what the library DID. A parameter you set is a request, not a record.**

**⚠ `topaz_train` trains only on micrographs present in the particle file** — *jyf1: 5,343 of 7,452; the
2,109 particle-free micrographs never entered training.* Choose the training subset by **positive
coverage**, not convenience.

**⛔ `topaz_extract` HAS TWO GATES, AND THE SECOND IS USUALLY THE BINDING ONE.** The visible
`--threshold -6` is permissive; the **advanced `particle_threshold`** runs `topaz convert -t 0` on
**logits**, so 0 = sigmoid 0.5. *jyf1: **1,726,119 picks cleared the visible gate (a 177 MB file) and the
advanced gate discarded 100 % of them (a 33-byte file containing only a header).***
🔒 **A correctly calibrated posterior for a rare class should almost never exceed 0.5**, so the default
gate returns nothing **by construction**. ⚠ The bank's silence on this key is silence about a corpus of
**abundant-class** projects — see `bank_frequency_does_not_transfer_across_regimes`.

**🔴 MEASURED OUTCOME (jyf1, "Arm T"):** the detector **learned a NON-TARGET OBJECT** while passing every
quality gate — **0.00 % of picked mass at aspect ≥1.8 vs 43.61 % in the main lineage**, i.e. *the abundant
90 % view was ABSENT from its picks.* Only **0.93 %** of its picks sat at positions no previous picker had
already found. **A consistent contaminant averages beautifully; quality gates cannot see identity.**

---

## ⭐⭐ THE PILOT GATE — run on ~300 micrographs BEFORE committing to a full re-pick
Cheap, and it can close the arm outright. **Three legs, all required.**

**(a) THE CEILING — novelty against the PRE-CLEANING pools, with a random-position null.**
🔒 **Never quote novelty against the CLEANED stack.** *jyf1 read **98.47 % "new"** that way and **39.04 %**
against the pre-cleaning pools — a survivorship artefact (Arm T: 96.73 % vs 0.93 %).*
🔒 **Density-match before comparing arms** — *jyf1's template route picked **1,266.6/mic vs the topaz
route's 14.7 — 86×**; matched, its novelty was **11.84 %** against a **59.04 %** random-position null.*
⚠ **Read the null correctly: landing on already-picked positions is what a GOOD picker does** — real
particles are mostly already found. Low novelty vs chance is **competence, not failure.**

**(b) IDENTITY — is it picking the target object?** Require **the ABUNDANT view to be PRESENT** in what it
picks. *This is the leg the topaz route failed at 0.00 %.* Report picked-mass fraction at the abundant
view's aspect, against the main lineage's.

**(c) 2D CLEANING — NOT OPTIONAL; a pilot gate cannot replace it.** *jyf1 closed Arm A on legs (a) and (b)
with its 332,508 picks **never classified**, while the other two arms had both been cleaned — unequal
evaluation depth, and the closure had to be withdrawn.*
**Classify WITH the seed, and report composition SPLIT BY NOVEL vs ALREADY-PICKED position.**
⭐ **The decisive question is whether novel picks CO-CLASSIFY WITH THE SEED.** *jyf1: novel enrichment in
the seed-nucleated rare-view class was **0.57 — DEPLETED — identical across two independent seed
constructions** (control class 0.19), the top-20 novel-enriched classes held **zero** protein-like
classes while the bottom-20 held **all 13**, and every novel-enriched class was an oversized 110–205 Å
rim-less blob. **That closed Arm A on evidence.***

---

## Steps 2–4 — shared with Arm B (`ARM_B_v2_PIPELINE.md`)
**2. 2D clean, in ROUNDS, with the SEED.** ⚠ **Arm A needs the seed MORE than Arm B**: *"Arm A's harvest is
dirty BY CONSTRUCTION, because the picker is deliberately run below its normal NCC cut — a rare-view
template scores low by construction."* `reference_select_2D` at schema defaults, `window_inner_A`
**required**, then the bright-blob cut — **calibrate that threshold on your own `corr` distribution, 1.1 is
not portable.** Keep the gate **loose** across rounds. **Junk only, never direction.**
**3. Merge + dedup** with the FULL UNDRAINED original. ⚠ `remove_duplicate_particles` keeps the
**higher-scoring** copy, so a rescued particle can silently replace its curated twin and discard that
twin's refined pose — verify kept-copy identity by `uid` and **report NET-NEW, not total.**
**4. 3D clean — MANDATORY. 2D cleaning does not discharge it.** `hetero_refine` K=3 = the current volume +
self-lowpass copies at **20 Å and 30 Å** as junk sinks, `res_init=20` (**do NOT sharpen** — a sharp
reference biases the sink against the low-SNR views being rescued), `num_rand_assign_iters=0`,
`force_hard_class=False`, C1, and **`multirefine_N` set explicitly to the box** (the default downsamples).
**Cross-FSC the outputs and UNION the ones that agree** — a decoy-seeded class is not automatically junk.
**Confirm** with the refined elevation census of the survivors.

## A NEGATIVE Arm A is informative
It says the missing view is **not merely unpicked**. With a negative Arm B (not merely mis-cleaned), it
points at the grid — and at **tilted collection**, not at more processing.
