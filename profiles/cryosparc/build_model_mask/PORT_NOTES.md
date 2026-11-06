# PORT_NOTES — `multichain_cryoem_autofit.py` (merged v1 + v2, 2026-08-18)

**Merged from two port documents, with v2 taking priority wherever they conflict.**
- **v1** (2026-07-27): ported the `complex-map-dock-refine` skill gates into the tool.
- **v2** (2026-07-30/31): rebuilt rigid docking on PowerFit, fixed six blocking bugs, changed the
  acceptance metric, added QC images.
Originals are preserved in `_backup_pre_powerfit_2026-07-30/` (v1 notes + pre-v2 code) and
`_backup_pre_powerfit_2026-07-30/PORT_NOTES_v2_rigid_powerfit.md`.

v2 validated end-to-end on three real deliverable maps: `hatrimer_10097` J519 (2.86 Å, C3 influenza
HA trimer), `TRPV1_10059` J317 (2.81 Å, C4), `hta1_11755` J648 (3.63 Å, corrected-hand GPCR-Gs).

---

# ⚠️ WHERE v2 SUPERSEDES v1 — read this before using the older notes

| topic | v1 said | **v2 (CURRENT)** |
| --- | --- | --- |
| **Acceptance metric** | `%atoms-in-density` is the PRIMARY gate (`--atoms-in-density-gate` 0.60, `--accept-mode multi_metric`) | **`%atoms-in-density` needs an absolute contour level — the one quantity that could not be estimated reliably — so it is now REPORTED as a DIAGNOSTIC and NEVER GATES. Acceptance is Phenix `CC_mask` in BOTH accept modes.** |
| **Rigid docking** | random-restart `fitmap search` ladder | **PowerFit global → precise local `fitmap`** (`--dock-method powerfit`, default) |
| **Chirality check** | `check_model_chirality_L()` as written | **its sign was INVERTED and discarded every valid pose — fixed (v2 bug 1)** |
| **Contour level** | derived from `len(chain.sequence)` | **read off the MAP histogram (`--dock-contour-box-fraction`, default 0.05); residue counts removed (v2 bug 2)** |
| **QC rendering** | `vision_qc.py` via ChimeraX `save <png>` | **`qc_slices.py` — no OpenGL/Xvfb on this cluster, so the v1 path could never satisfy `--vision-qc gate` (v2 bug 5)** |

**Everything else in v1 still applies and is merged below**: handedness/never-mirror-the-model, the
target-lobe two-stage fit, the two-stage Phenix refine and geometry gates, mask frames, and the
robust ChimeraX invocation.

---

# 0. 🔴 STATE BEATS SEQUENCE IDENTITY — read this before choosing ANY template (standing rule, 2026-08-19)

**The single biggest cause of a failed or MISLEADING rigid fit is a starting model in the wrong
conformational/ligand state — not low sequence identity, not the wrong hand, not the search
method.** A high-identity template in the wrong state can score at the mirror-control noise floor;
a lower-identity template in the RIGHT state can score a confident ACCEPT. Sequence identity alone
never tells you this — only the STATE (active/inactive, open/closed, apo/bound, agonist/antagonist)
does. Full detail and the fix to `provider_swissmodel_templates`' ranking: § 8 below.

**TRPV1 J317** (map = DkTx/RTX-bound OPEN state; same sequence, same 335–751 range, same tetramer,
only the template's STATE differs):

| template | state | **rigid CC** |
| --- | --- | --- |
| **5IRX** | **open, DkTx+RTX (matches sample)** | **0.7384 ACCEPT** |
| 5IRZ | apo nanodisc | 0.3232 REVIEW |
| 7lqy | apo, and squirrel not rat | 0.2326 REJECT |

**CB1 `cb1_redo` J1350** (whole-map PowerFit, receptor chain only, sample = inactive/
antagonist-bound CB1; same protocol/map/resolution/contour/PowerFit params throughout, ONLY the
template changed) — measured session 69, the counterfactual sessions 43/49/55 never isolated
(they changed template state AND search method together):

| template | state | identity to our seq | **CC_mask** | hand_ratio | verdict |
| --- | --- | --- | --- | --- | --- |
| **5U09** | **inactive, antagonist-bound (matches sample)** | ~1.00 (matched blocks) | **0.5725** | **7.48** | **ACCEPT** |
| 6KPG | active, Gi-coupled (WRONG state) | 0.791 | **0.3187** | **3.52** | **REVIEW** |

**Read this honestly, not as a clean binary.** 6KPG did NOT collapse to the mirror-control noise
floor (that pattern — CC_mask 0.03–0.09 across every candidate — is what a genuinely uninformative
fit looks like, e.g. sessions 43/49/55's Fv attempts); it landed in REVIEW (0.3187, one PowerFit
candidate out of 10 rose well clear of its own noise floor: the other 9 scored 0.03–0.10, only
rank 3/original reached 0.32). The receptor FOLD is broadly conserved between active and inactive
GPCR states, so a wrong-state receptor still substantially overlaps the right footprint (shape
alone gets you partway) — this is a real, structurally sensible finding, not a tool bug. But
**state still cost a lot**: CC_mask dropped 44% (0.5725→0.3187) and hand_ratio dropped from a
confident 7.48 to 3.52 — barely clearing the 3.0 ambiguity floor, i.e. state mismatch pushed a
would-be-confident ACCEPT down to a borderline call that the pipeline flags REVIEW and demands a
human look at the QC image rather than trusting automatically. **Verdict: the state mismatch WAS
causal and costly on CB1 too, but the gap is a REVIEW-vs-ACCEPT margin here, not TRPV1's cleaner
REJECT-vs-ACCEPT split** (TRPV1's apo/wrong-species templates fell to 0.23–0.32, genuinely close to
REJECT). Both examples say the same thing — state beats identity — but CB1 is the more measured,
less dramatic case, and is reported as such rather than inflated to match TRPV1's cleaner story.

**Practical consequences, encoded into the tool this session:**
1. `--skip-modeling` (feeding an existing model via `--models-dir`) **completely bypasses** the
   tool's own state-aware template ranking — this is exactly how sessions 43/49/55 walked into the
   6KPG mistake (hand-picked by identity alone, never ranked by state). The tool now prints a loud
   WARNING whenever `--skip-modeling` is used, naming this exact CB1 case as precedent. **You are
   responsible for having chosen a state-matched model** when using this path — the tool cannot
   check it for you.
2. `--model-state` (the state-keyword string ranking is matched against) being empty/absent means
   even the automated provider ladder has nothing to rank on. The tool now warns on this too.
3. The chosen template's RCSB entry title is now surfaced in the log and the JSON report
   (`template_title` on the model_info / `report["template_titles"]`) for `swissmodel_templates`
   and `rcsb_template`, so a wrong-state choice is visible to whoever reads the run later instead of
   buried in an internal ranking decision.

---

# 1. Never mirror the model (v1 gate 1 — CURRENT)

- `mirror_model_coordinates()` is **removed**, not merely unused: mirroring a chirally-fixed protein
  model is never valid, only ever mistaken for "fixing" a wrong-hand map. Enforced structurally by
  `check_model_chirality_L()` / `assert_model_chirality_ok()`, called after **every** rigid-body
  transform (post-dock, post-local-refit, post-combine, post-refine-cycle). It computes the signed
  tetrahedron volume of (N, CA, C, CB) per residue and raises `PipelineError` on a D-form model.
  **⚠️ v2 bug 1: the sign was inverted** — canonical L is **POSITIVE** (verified 5IRX 1614/1614,
  AF-O35433 791/791, SWISS-MODEL Q91MA7 1371/1371). Before the fix every valid L model scored
  `fraction_l_form = 0.00` and **every docked pose was discarded before being scored.**
- `--dock-hands original,flip` (default) tests the SAME never-flipped chiral model against both the
  no-flip map and one axis-flipped map; **the winning MAP hand is kept.**
- Report keys: `input_map_hand_corrected` (bool), `output_grid_reference_map`, plus a loud WARNING
  when the input `--map` turns out to be wrong-hand.
- **Masks may be mirrored — a mask is a shape, not a chiral object.**
  `--emit-mask-frame {winning,input,both}` (default `both`):
  - `winning` — mask built directly in the corrected-hand frame (no interpolation-from-mirroring)
  - `input` — mirrored back onto the ORIGINAL `--map` grid; the fast path when you want to keep
    refining existing original-frame particles without re-refining in the flipped frame
  - `both` — emits and labels both (`mask_report["winning_hand_mask"]` / `["input_grid_mask"]`)

# 2. Rigid docking — PowerFit → local fitmap (v2 §2, SUPERSEDES v1's ladder; FIXED 2026-08-19)

`--dock-method` is now **`powerfit`-only** — PowerFit's FFT 6D global search is the **sole** global
search method, on BOTH the whole-map path and the `--target-lobe-map` path (the dispatch bug below
is fixed). `fitmap_search` (v1's ChimeraX random-restart ladder) is **RETIRED**, not merely
deprecated: it is no longer reachable from the CLI dispatch at all. Passing `--dock-method
fitmap_search` is still ACCEPTED for backward compatibility — it logs a loud deprecation WARNING
(citing the stochasticity below) and is silently routed to `powerfit`; any other value raises an
error. `dock_chain_model()` (the ladder's implementation) is left in the file, unused, only in case
some other script imports and calls it as a library — no other caller was found in the codebase.
**`chimerax_local_refit()` — the LOCAL polish step — is untouched and unaffected**; it is not the
thing being retired, and it is where PowerFit's own accuracy comes from (see the measured table
below). It was retired because it measured **stochastic**: the same HA input gave CC_mask
**0.724 / 0.058 / 0.046** on three runs, and it costs 20–60 min per hand per search level.

> ## 🕰️ HISTORICAL — the `--target-lobe-map` SILENTLY DISABLED PowerFit (found 2026-08-18, FIXED 2026-08-19)
> **This bug is FIXED as of 2026-08-19 — kept below as the record of what happened and what it cost,
> since three real cb1_redo sessions (43, 49, 55) burned significant wall-clock time on it.**
>
> Supplying `--target-lobe-map` used to route the run through `dock_chain_two_stage()`, whose stage 1
> called `dock_chain_model()` (the ChimeraX `fitmap ... search N` ladder) **unconditionally**,
> completely ignoring `--dock-method`. `dock_chain_two_stage()` was tested BEFORE `--dock-method` in
> the main dispatch, so the lobe path never reached `dock_chain_powerfit()` at all:
> ```python
> if lobe_maps is not None:
>     result = dock_chain_two_stage(...)   # <-- --target-lobe-map took this branch, ALWAYS
> elif args.dock_method == "powerfit":
>     result = dock_chain_powerfit(...)
> else:
>     result = dock_chain_model(...)       # fitmap_search
> ```
> This was a trap because the old §3 advice recommended `--target-lobe-map` for any multi-lobe
> complex, so the two pieces of advice silently conflicted: taking the lobe advice cost you the
> PowerFit default. Three cb1_redo runs (sessions 43, 49, 55) all believed they were using PowerFit
> and were in fact using the stochastic ladder the whole time — a near-noise CC from a lobe-routed
> run could simply be the ladder's known bad draw (0.058 / 0.046 on an input that also gave 0.724),
> not evidence of a wrong model.
>
> **The fix (2026-08-19):** `dock_chain_two_stage()`'s stage 1 now calls `dock_chain_powerfit()`
> against the lobe maps (with `trim_extend=None` — the candidate is never trimmed at this stage;
> that stays a job for stage 2 and the standard post-refine trim, exactly as before the fix); stage 2
> (the local whole-map refit) is **unchanged**. The main dispatch's three-way
> `if lobe_maps / elif powerfit / else dock_chain_model` split is gone — it is now just
> `if lobe_maps: dock_chain_two_stage(...) else: dock_chain_powerfit(...)`, both PowerFit-based.
>
> ### Measured impact of the bug (cb1_redo, 2026-08-18) — a true controlled comparison
> Same model (5U09 receptor-only), same target (`receptor_lobe.mrc`), same contour, both hands.
> **Only the search algorithm differed:**
>
> | | ChimeraX `fitmap` ladder | **PowerFit-on-lobe** |
> | --- | --- | --- |
> | CC_mask | **0.03–0.06** (noise floor) | **0.5725 ACCEPT** |
> | hand ratio | 1.16 (AMBIGUOUS) | **7.26 (confident)** |
> | more search helps? | **NO — got WORSE 300→1000→3000 (0.057/0.037/0.029)** | n/a |
>
> **Mechanism:** `fitmap ... search N` performs N *local* gradient climbs from N random starting
> poses — each trapped by a nearby local density peak (micelle texture, noise), and raising N does
> not help because it never explores the full 6D space. PowerFit does an angle-gridded (10°),
> FFT-accelerated **exhaustive translational search at every orientation**, so it cannot be trapped
> the same way. **The ladder getting WORSE with more restarts is the diagnostic signature of
> local-minimum trapping** — treat it as such rather than as evidence of a bad model. This exact
> signature is why `fitmap_search` was retired outright rather than merely left as a non-default
> option.
>
> **What was NOT proven by this comparison** (recorded to prevent over-attribution):
> - **State-matched template** (5U09 inactive vs 6KPG active): very plausibly contributes — an
>   active-state GPCR has a substantially different TM6 — but the counterfactual "PowerFit +
>   wrong-state template" was never run, so it is unproven, not established.
> - **Corrected contour level** (0.016 vs the default 0.05, which renders as the micelle): this
>   affects the atoms-in-density diagnostic and the post-fit density trim, **but NOT the `CC_mask`
>   gate** — `phenix.map_model_cc` computes that against the raw real map, independent of the
>   contour argument. So the level fix is real and worth doing, but it is **not** what produced 0.57.
> - **Lobe restriction**: present in EVERY attempt, so it is a necessary precondition (it prevents
>   locking onto the wrong lobe) but does not discriminate why this run succeeded.

## 2b. WHOLE MAP is now the validated default; `--target-lobe-map` is opt-in (2026-08-19)

**Reversed from the old advice** (which recommended cutting a `--target-lobe-map` for any
multi-lobe complex — see §3 history). Measurement now contradicts that default: PowerFit's own
core-weighted CC + Laplace edge filter handles multi-component maps unaided — the hTA1 benchmark
(§2 table below) placed a lone receptor chain correctly inside a WHOLE GPCR-Gs complex map
(CC_mask 0.3977) with no lobe guard at all. cb1_redo's own receptor case confirms this scores
**identically** on the lobe and the whole map:

| target (cb1_redo, `J1350`) | lobe map | whole map |
| --- | --- | --- |
| receptor (5U09, 272 res, ~35 kDa target region) | CC_mask **0.5725**, hand_ratio **7.26** | CC_mask **0.5725**, hand_ratio **7.48** |
| paired Fv (6VGR-framework, 224 res, ~25 kDa) | CC_mask **0.0688–0.08**, hand_ratio **1.0–1.3 REJECT** | CC_mask **0.56**, hand_ratio **5.6 ACCEPT** |

The receptor is indifferent to map scope (PowerFit stayed on-target either way); the Fv, on a small
crop, went from REJECT to ACCEPT purely by widening the target to the whole map — the crop was
**hurting**, not helping. `--target-lobe-map` is now documented as an explicit **OPT-IN**
restriction: use it when a whole-map search still mis-locks onto a brighter neighbouring component
(the original wrong-lobe failure mode it was built for, e.g. two Fab chains both landing in the
receptor lobe), not as a default precaution.

## 2c. Small-crop warning for `--target-lobe-map` (2026-08-19)

PowerFit's advantage is a **Laplace edge filter**, which needs surrounding context; a `--target-lobe-map`
cropped tightly around a small domain starves it — this is the actual, measured mechanism behind the
Fv failure above (not the wrong-component risk the flag itself exists to prevent). `check_lobe_crop_size()`
runs automatically whenever `--target-lobe-map` is supplied, once per candidate model (using the
model's OWN residue count, read from the PDB/mmCIF actually being docked — not the source FASTA
record, which can be much longer, e.g. a BRIL-fusion construct behind a receptor-only model):
- Estimates the model's expected volume from its residue count (`estimate_expected_voxels`,
  ~110 Da/residue × ~1.21 Å³/Da).
- Compares it against the lobe crop's own enclosed volume at an **absolute** density level derived
  from the WHOLE map (`--lobe-crop-level-box-fraction`, default 0.005 — deliberately tighter than
  the 0.05/0.016 used elsewhere for the full particle envelope, since this needs to isolate the
  density CORE). **Must be an absolute level from the whole map, not a level re-derived from the
  lobe crop itself**: `map_envelope_contour_level(box_fraction=...)` is normalized to always enclose
  the SAME box_fraction of whatever grid it is given, and since lobe crops here share the identical
  grid as the whole map (built by zeroing outside a half-space/slab, not by physically cropping the
  array), a per-crop box-fraction level returns near-identical "enclosed volume" regardless of actual
  crop size — blind to the thing being checked, by construction. Confirmed empirically: this bug
  gave fv_sublobe and fab_lobe (visually very different crop sizes) the SAME "ratio" until fixed.
- Below `--lobe-crop-min-ratio` (default 3.0 — "comfortably larger", not merely bigger), logs a LOUD
  warning and recommends the whole map instead. Purely advisory, never blocks.

Calibration against the two real outcomes above (both using each model's OWN residue count):
receptor_lobe (272-res model, WORKED) → ratio **3.53×**, clears the 3.0 floor; fv_sublobe AND the
whole fab_lobe (224-res model, BOTH FAILED) → ratio **1.11× / 1.43×**, both below it — the default
correctly separates the one crop that worked from the two that didn't.

Per hand (`original` / `flip` — the MAP is mirrored, the MODEL never is):
1. PowerFit on the **RAW** map (its own Laplace pre-filter, resampling and trimming; the
   floor/cap-clipped docking maps are not used in this path at all).
2. Each of the top `--powerfit-solutions` (default 5) goes through `chimerax_local_refit()` — local
   `fitmap`, no search, no recentering. **This is where the accuracy comes from** (PowerFit's 10°
   rotation grid leaves a few degrees on the table).
3. Phenix `CC_mask` before AND after polish; best solution per hand, best hand wins.
4. `assert_model_chirality_ok()` after every transform.

**Measured (CC_mask, correct hand):**

| case | PowerFit | + local fitmap | best other method | **mirrored-map control** |
| --- | --- | --- | --- | --- |
| HA protomer | 0.493 / 29 s | **0.7245** / +35 s | 0.724 (fitmap ladder, ~1 h, or 0.046) | 0.044 |
| TRPV1 subunit | 0.110 / 46 s | 0.3502 | 0.36 (`phenix.dock_in_map`, 4m46) | 0.041 |
| hTA1 receptor (GPCR) | 0.237 / 46 s | **0.3977** | 0.377–0.40 (`dock_in_map`, 7m12) | **0.062** |

PowerFit's core-weighted CC + Laplace filter is what keeps it off the bright G-protein density when
only the receptor is being placed — the failure mode that produced the wrong-hand hTA1 J616.

`phenix.dock_in_map` (installed, `min_cc=0.4`, `search_model_copies=N`) is a good alternative global
searcher if PowerFit is unavailable.

**PowerFit install gotchas** (venv at `build_model_mask/pf_env/`, the `--powerfit-exe` default):
PyPI `powerfit` is an UNRELATED package — the real one is
`git+https://github.com/haddocking/powerfit` (`powerfit-em` 5.0.2); gcc 4.8.5 defaults to C89 so the
build needs `CFLAGS=-std=gnu99`; pyfftw 0.15 wants FFTW headers this cluster lacks, so pin
`pyfftw==0.13.1` + `numpy==1.26.4` manylinux wheels; 5.0.2 uses `np.bool` (numpy≥2 alias) in
`analyzer.py` and `powerfitter.py` — both patched to `bool` in the installed venv; conda-forge is
unreachable from here, PyPI works. PowerFit's output PDBs left-justify the element in columns 77–78,
which Phenix rejects outright — `normalize_model_columns()` round-trips them through gemmi.

# 3. Target-lobe cut + two-stage global(lobe) → local(whole) fit (v1 gates 2 & 4; UPDATED 2026-08-19)

`--target-lobe-map PATH`: a pre-cut lobe/segment submap (same grid/hand convention as `--map`; both
an original and one axis-flip variant are derived from it via `prepare_maps()`, under
`out_dir/00_lobe/`). **This is now an OPT-IN restriction, not the recommended default — see §2b.**
- `dock_chain_two_stage()`: **stage 1** docks against the **lobe** maps via `dock_chain_powerfit()`
  (PowerFit global search + local ChimeraX polish, fixed 2026-08-19 — see the §2 HISTORICAL gotcha;
  it used to be the ChimeraX ladder unconditionally, ignoring `--dock-method`) — a CANDIDATE only,
  never returned as final, and never trimmed (`trim_extend=None`) at this stage. **Stage 2**
  (unchanged) takes that pose and does a **local-only** ChimeraX `fitmap` (no search, no
  recentering) against the **whole** real map of the winning hand, then rescores with Phenix CC.
- Without `--target-lobe-map` on a single-chain request the tool now logs a NOTE (not a warning)
  that the whole map is the validated default (see §2b) — PowerFit handles multi-component maps
  unaided (hTA1 benchmark). With `--target-lobe-map` supplied, it logs the measured cb1_redo
  numbers (§2b) and runs the small-crop check (§2c) per candidate model.
- `--local-refit-max-steps` (default 150) controls stage-2 `maxSteps`.
- `--lobe-crop-min-ratio` / `--lobe-crop-level-box-fraction` (defaults 3.0 / 0.005) control the
  small-crop warning (§2c).

# 4. ACCEPTANCE — masked CC, NOT atoms-in-density (v2 §3, SUPERSEDES v1 gate 3)

**`%atoms-in-density` required an absolute contour level — exactly the quantity that could not be
estimated reliably — so it is REPORTED as a diagnostic and NEVER gates.** Acceptance is Phenix
`CC_mask` (mean-subtracted, inside the model's own mask) in **both** accept modes:

| band | threshold | meaning |
| --- | --- | --- |
| **ACCEPT** | `>= --cc-gate` (0.40) | good model, correctly placed |
| **REVIEW** | `>= --cc-review` (0.25) | placed on the right component, but model conformation or local density imperfect. Accepted, flagged, **QC image MUST be read** |
| **REJECT** | below 0.25 | mirrored-map controls measured **0.04–0.06** |

- `--hand-ratio-min` (3.0): winning/opposite hand ratio below this flags handedness **AMBIGUOUS**.
- **Calibration:** 0.72 = good model correctly placed. 0.35–0.40 = correctly placed, imperfect model
  (TRPV1's AlphaFold monomer; hTA1's receptor at 3.63 Å). **0.04–0.06 = mirrored control, i.e. noise.**
  A bare 0.4 cut would have thrown away two CORRECT placements — hence the REVIEW band.
- **`CC_mask` is NOT portable across datasets** (it scales with resolution, sharpening, and how much
  of the map the chain covers). **The HAND RATIO is the robust criterion** — self-normalizing, and
  measured **6–17×** on correct calls. **Always score AFTER polish**; before-polish values
  (0.11–0.49) misclassify.
- `select_best_hand()` compares hands docked under **identical** conditions; `--accept-mode
  {multi_metric, cc_only}` is retained for legacy semantics, but **both now gate on CC_mask**.

# 5. Bugs fixed in v2 (before this, the tool could not produce a mask at all)

| # | bug | effect | fix |
| --- | --- | --- | --- |
| 1 | chirality sign inverted | every valid L model scored 0.00 and EVERY docked pose was discarded before scoring | sign flipped to `> 0` |
| 2 | contour level from `len(chain.sequence)` | the level belongs to the MAP, not the chain; on a Cn map came out 3–4× too high (TRPV1 4.18 vs a working 1.33) → ChimeraX "0 of 300 fits" | residue counts removed; `map_envelope_contour_level()` reads it off the map histogram (`--dock-contour-box-fraction` 0.05, `--dock-contour-level` to override), with a plateau guard |
| 3 | six Phenix PHIL names absent in Phenix 1.18.2 | BOTH refine paths failed; the "fall back to legacy" net fell into a second broken call, so no run reached the mask step | `output.prefix`→`output.file_name_prefix`; dropped `output.overwrite` and `skip_map_model_overlap_check`; `macro_cycles`/`run`→`refinement.*`; `secondary_structure.enabled`→`pdb_interpretation.secondary_structure.enabled` |
| 4 | `rename_model_chain()` renamed every chain to one ID → gemmi MERGED them | multimeric provider models silently truncated (SWISS-MODEL HA trimer 1488→496 residues, two protomers gone, no warning). SWISS-MODEL routinely returns oligomers; `rcsb_template` ALWAYS does | first chain takes the FASTA record's ID, each further chain a DISTINCT one — model kept in full; extra chains double as refinement context |
| 5 | `--vision-qc` rendered via ChimeraX `save <png>` | impossible here — no OpenGL and no Xvfb on the CryoSPARC master or any compute node — so `--vision-qc gate` could never be satisfied | new `qc_slices.py`; `vision_qc.py` kept as fallback for hosts with working OpenGL |
| 6 | per-attempt exceptions swallowed into a buried `ERROR.txt` | summary printed `CC_mask=None ... passed=None` and the error read "No chain passed the dock acceptance gate" — i.e. "poor fit" — when NOTHING had been scored. Bugs 1 and 2 both presented this way | partially addressed (the new dock path logs each attempt); the summary should still count errored-vs-scored attempts |

# 6. Fill the map by symmetry before flexible refinement (v2 §4)

`--map-symmetry C1|Cn|Dn` (default C1). Refining ONE chain inside a map holding the whole complex
leaves every neighbour's density unoccupied, so the chain **drifts into it**, and MolProbity — seeing
only the isolated chain — cannot detect the resulting inter-chain clashes. When the map was
reconstructed with symmetry imposed, `fill_map_by_symmetry()` generates the neighbours about +Z
through the box centre (CryoSPARC convention), refines the filled assembly, and builds the mask from
`--mask-chains` only.

| HA, same protomer, same rigid pose | lone chain | C3-filled |
| --- | --- | --- |
| cleanup cycle 1 | FAILED (Rama 94.94% < 96, rotamers 4.16% > 2) | PASSED |
| accepted | cycle 2, CC 0.8272 | cycle 1, CC 0.8259 |

For a genuine hetero-complex, **supply EVERY component as its own FASTA record** — all are
rigid-fitted and combined before refinement. A single sequence still works and still makes a mask; it
just logs that the map is unfilled.

# 7. Refinement — two-stage Phenix + geometry gates (v1 gate 6 + v2 §5, §7c)

- `phenix_secondary_structure_restraints()` generates `ss.eff`, including the
  `helix_type = alpha pi 3_10 *unknown` → `*alpha pi 3_10 unknown` fix (some Phenix versions default
  the wrong PHIL star, silently dropping alpha-helix restraints). Returns `None` (never raises) if
  the executable is missing.
- `phenix_molprobity()` / `geometry_passes()` gate on clashscore <10, Rama favored >96%, Rama
  outliers <0.5%, rotamer outliers <2% (all `--geometry-*` overridable).
- `phenix_two_stage_refine()`: **R1** = SS-restrained density search (`macro_cycles=3`,
  `run=minimization_global+local_grid_search`), then **R2** = gentle cleanup
  (`run=minimization_global`) tried at `--refine-cleanup-cycles` (default `1,2,3`) **in order**,
  keeping the FIRST cycle passing BOTH a fit floor AND the geometry gates.
- `--refine-mode {two_stage, legacy}` (default `two_stage`). Falls back to `legacy` automatically
  with a WARNING if the Phenix helpers are unavailable.
- **Policy (v2 §7c):** default remains **refine-then-mask**. **No reference-model restraints** in the
  two-stage path (removed at the user's direction); the `legacy` path keeps its own originals.
  `--refine-mode none` was **REJECTED** by the user.
- Drift is measured and REPORTED only, never restrains or blocks: `report["refine_drift"]`,
  `refine_drift_verdict`; warn above `--max-drift-warn-A` (2.0), flagged `REJECTED-DRIFT` above
  `--max-drift-fail-A` (5.0). Observed values are small (HA 0.60–0.69 Å, hTA1 0.86 Å).
- **`--post-refine-cc-warn` (0.50)** — because **CC can RISE while the model DEGRADES.** Observed on
  hTA1: cleanup 1 → CC 0.5382 clashscore 10.66; cycle 2 → 0.5430; cycle 3 → **0.5673 with clashscore
  20.59, Rama 92.28%, rotamers 15.53%.** CC climbed 0.398 → 0.567 while stereochemistry collapsed;
  only the geometry gate caught it. **Do not gate on CC alone.**

# 8. 🔴 STATE matters more than anything else (v2 §7d, 2026-07-31; standing rule promoted to § 0, 2026-08-19)

**See § 0 at the top of this document for the short, standing-rule version of this section (now
including a second worked example, CB1, alongside TRPV1) — read that first.** The single biggest
cause of a failed rigid fit is a starting model in the **wrong conformational / ligand state**.
TRPV1 J317 (map = DkTx/RTX-bound OPEN state), all models from the SAME sequence over
the SAME range 335–751 as 4-chain tetramers, docked identically:

| template | state | **rigid CC** | after refine | drift |
| --- | --- | --- | --- | --- |
| **5IRX** | **open, DkTx+RTX (matches sample)** | **0.7384 ACCEPT** | 0.7775 | **0.40 Å RMSD, 0% >2 Å** |
| 5IRZ | apo nanodisc | 0.3232 REVIEW | 0.7128 | 1.65 Å, 23% >2 Å |
| 7lqy | apo, and SQUIRREL not rat | 0.2326 REJECT | — | — |
| AlphaFold monomer | single predicted state | 0.2506 REVIEW | 0.383 | 1.14 Å, 8% >2 Å |

1. **Judge state on the RIGID CC, not the post-refinement CC.** After refinement the wrong-state model
   reaches 0.7128 vs 0.7775 — you would call both fine. Flexible refinement **BENT** the wrong-state
   model into the density to buy that correlation. The rigid CC (0.74 vs 0.32) and the DRIFT separate
   them cleanly; the refined CC does not.
2. **Geometry gates do NOT detect a wrong state.** Both failed all three cleanup cycles, and the
   WRONG-state model scored BETTER geometry (Rama 95.4%, rotamers 2.6%) than the correct one
   (90.3%, 10.7%). Geometry is a warning, not a discriminator.

**Template ranking was fixed accordingly:** `provider_swissmodel_templates` now ranks by (a)
STATE-keyword hits against the RCSB entry title (from `--model-state`), then (b) how well the
template's modelled range matches `--target-res-from/-to`, then coverage. Previously it sorted by
oligomeric state then COVERAGE, which buried 5IRX (coverage 0.498 = exactly the ordered TM+pore
region) behind apo templates padded to 0.78 by including the disordered ARD — it actively preferred
the wrong state.

# 9. Establish the sample context BEFORE predicting (v2 §7e)

Never feed the tool a bare sequence. From the project `base_datainfo.yaml` plus the actual request,
settle: the **COMPONENTS** in the map, the exact **SEQUENCES/species**, the **STATE**
(`--model-state`, `--target-res-from/-to`), and **WHAT THE MASK MUST EMBRACE (and exclude)**.

Modelling every component is thorough but **NOT a hard requirement** — if the mask concerns one part,
modelling that part alone is fine. The trade-off: density belonging to unmodelled components stays
unoccupied, so flexible refinement may drift into it and MolProbity cannot see the resulting
inter-chain clashes. Watch `refine_drift` and the QC images; add neighbours only if it misbehaves.
If anything is unknown, search the web (EMPIAR/EMDB/PDB) or **ask the human**. Do not guess, and do
not force a mask out of a fit that does not make sense.

# 10. When the rigid fit fails: switch the MODEL, then ASK THE HUMAN (v2 §7b)

**A failed rigid fit almost always means a bad STARTING MODEL, not a bad search.** Measured: the
AlphaFold monomer of TRPV1 (a domain-swapped channel subunit) never fit its own map at any CC —
0.013–0.019 with `fitmap`, 0.11 with PowerFit, 0.36 with `dock_in_map`, **all three searchers landing
in the same basin** — while a SWISS-MODEL TETRAMER from the SAME sequence docked immediately. An
isolated subunit of an intertwined or domain-swapped assembly is often simply **not a rigid body**.

The tool walks a provider ladder: `--model-fallback-providers` (default
`swissmodel,alphafold,rcsb_template,esmfold`). `--no-model-fallback` disables it; it is skipped under
`--skip-modeling`.

If the whole ladder fails, the run **STOPS and does NOT build a mask**, with an explicit
ASK-THE-HUMAN error naming what was tried and what is needed: a better starting model (right
oligomeric state), confirmation the component is really in this map, the correct hand/symmetry, or a
`--target-lobe-map`. **Deliberate** — a mask from a failed fit would wrap empty space or the WRONG
component, and every downstream local refine would silently inherit it.

# 11. Mask geometry, frames and naming (v2 §7, §7f + v1 gate 1)

| flag | default | HA (1.31 Å/px, 2.86 Å) | TRPV1 (1.216, 2.81) | hTA1 (1.069, 3.63) |
| --- | --- | --- | --- | --- |
| `--mask-radius-A` | **3.0 Å** | 3.0 | 3.0 | 3.0 |
| `--mask-dilate-pix` | **3 px** | 3.9 Å | 3.6 Å | 3.2 Å |
| `--mask-soften-x-res` | **2.0 × resolution** | 5.7 Å | 5.6 Å | 7.3 Å |

`--mask-radius-A` is the ball painted around EVERY selected atom (flat, not element-specific; the
`VDW_RADII_A` table is only used for the threshold volume estimate). Radius + dilate is ONE combined
hard-boundary budget of ~6–7 Å. **The soft edge floors at 3 px and scales with resolution, because a
soft edge below ~2× resolution causes Fourier ringing and inflates masked FSC.**

Effect on the HA protomer mask (496 residues, protein estimate 66,960 Å³); C3 overlap = fraction of
the mask integral also covered by its own 120°-rotated copy:

| radius / dilate / soften | core (mask=1) | integral | C3 overlap |
| --- | --- | --- | --- |
| 4.0 / 3.9 / 5.7 (old radius) | 205,918 (3.08×) | 4.08× | 30.9% |
| **3.0 / 3.9 / 5.7 (current)** | **180,232 (2.69×)** | **3.65×** | **27.9%** |
| 4.0 / 6.0 / 6.0 (tool stock) | — | 5.05× | 37.7% |
| 4.0 / 8.0 / 10.0 (loose) | — | 7.10× | 45.7% |

**18.5% is HA's structural floor** — protomers interdigitate through a three-helix coiled-coil stem,
so some interface overlap is the molecule, not the parameters. Field norms: RELION
`relion_mask_create` extends 2–5 px with a 3–6 px soft edge; CryoSPARC Volume Tools stock is dilation
0 / padding 12 px (much looser than practice); CryoSPARC's dynamic refinement mask uses near 6 Å /
far 14 Å.

The pipeline logs the resolved geometry to `report["mask_geometry"]` and WARNS when the soft-mask
volume exceeds 4× the protein estimate (note this mixes core and soft shell — the core alone, 2–3× is
healthy, is the more honest number).

**Naming (§7f):** `--mask-label` (short slug, e.g. `TRPV1_1copy_open_DkTx_RTX`) becomes the mask
FILENAME prefix; `--mask-description`, `--sample-description` and `--model-state` are recorded in the
mask report and in a `MASK_README.txt` beside the `.mrc`, with the geometry, both masked CCs and the
QC image path. **Carry the label into CryoSPARC as the `import_volumes` job TITLE and the local-refine
job COMMENT.**

**Mask frames:** see §1 — `--emit-mask-frame {winning,input,both}` (default `both`).

**Standalone mask builder:** `make_model_soft_mask.py` is the downstream mask/threshold step on its
own — it reads a fitted PDB/mmCIF, selects optional chains and a residue-number range, projects an
atom mask onto the input MRC grid, softens/dilates it, and estimates a local density threshold. Use
it directly when you already have an accepted fitted model and do not need the docking pipeline.

# 12. QC images — `qc_slices.py`, no OpenGL required (v2 §6, SUPERSEDES v1's vision-qc renderer)

Three orthogonal ±8 Å slab maximum-intensity projections with a 50 Å scale bar, rendered at THREE
points: end-of-dock, end-of-refine, and after masking.
- `render_dock_qc()` / `render_model_in_density()` — model CA trace in red over the map. **Red along
  bright density = good; red over dark background or on a neighbouring subunit = wrong.**
  Signature-compatible with `vision_qc.render_dock_qc`.
- `render_mask_qc()` — mask regions drawn separately with 3D volumes in the legend: magenta =
  `mask == 1` core; cyan dashed = outer extent of the cosine soft edge; orange dotted = hard atom
  mask; green = map density at the estimated hard-mask threshold.

`--vision-qc {off, render, gate}` (default `render`) still controls whether the run stops for a human
verdict (`--vision-qc-verdict-dock` / `--vision-qc-verdict-refine`). A `fail` on a chain excludes it
from combined refinement; a `fail` on the final refine stops before mask generation.

**Standalone CLI — use it on ANY map+model or map+mask, including outputs from other jobs:**
```bash
PY=YOUR_MODEL_MASK_PYTHON_PATH      # Python with gemmi, mrcfile, numpy, scipy, matplotlib
QC=build_model_mask/qc_slices.py     # relative to the CryoSPARC profile directory

# model in density — the dock / refine check
$PY $QC model --map J519_volume_map_sharp.mrc --model docked.pdb \
      --out dock_qc.png --title "J519 chain A dock"

# mask check — core / soft edge / hard mask / density threshold
$PY $QC mask --map J519_volume_map_sharp.mrc \
      --mask <run>/06_mask_on_original_grid/selected_chains_mask.soft_mask.mrc \
      --hard-mask <run>/06_mask_on_original_grid/selected_chains_mask.hard_atom_mask.mrc \
      --model <run>/06_mask_on_original_grid/selected_chains_fasta_range_model.pdb \
      --report <run>/06_mask_on_original_grid/selected_chains_mask.report.json \
      --out mask_qc.png --title "J519 chain A mask"
```
`--report` pulls `threshold_estimate.estimated_density_threshold` automatically; `--threshold` sets
it explicitly. `--model` and `--hard-mask` are optional (a mask alone renders fine — e.g. a
threshold-derived `volume_tools` mask with no model). `--slab-A` changes the slab half-thickness
(default 8 Å). **Then READ the PNG — these are made to be looked at, not filed.**

# 13. Robust ChimeraX invocation (v1 gate 5 — CURRENT)

`chimerax_fitmap()` and `chimerax_local_refit()` invoke ChimeraX as:
```
chimerax --nogui --exit --cmd "open <script>.cxc"
```
**never** `--script <file>.cxc`, which on some builds is routed through `runscript` (which expects a
**Python** file) and raises `SyntaxError` on a plain `.cxc` command file. This is the exact bug
present in `vis_res/vis_res_utils.py::render_map_surface` (uses `--script`), left untouched as
out-of-scope but cited as the pattern to avoid. `--cmd "open <file>"` works on every version tested
here (1.3, 1.7).

*(v1 note: `scripts/run_rigid_fit.py` from the source skill calls a StructAgent ChimeraX wrapper that
does not exist anywhere on this server; this port deliberately does NOT depend on it.)*

# 14. CLI flags (merged)

| Flag | Default | Purpose |
| --- | --- | --- |
| `--dock-method` | `powerfit` | **FIXED 2026-08-19**: PowerFit is now the ONLY global search, on BOTH the whole-map and `--target-lobe-map` paths. `fitmap_search` (v1's ladder) is RETIRED: still ACCEPTED for backward compat, logs a deprecation WARNING, auto-routes to `powerfit`; any other value errors. `chimerax_local_refit()` (local polish) is unaffected. |
| `--powerfit-solutions` | 5 | solutions polished/scored per hand |
| `--powerfit-exe` | `pf_env/bin/powerfit` | PowerFit venv |
| `--dock-contour-box-fraction` | 0.05 | **v2**: contour from the MAP histogram (no residue counts) |
| `--dock-contour-level` | none | explicit override |
| `--cc-gate` | 0.40 | ACCEPT threshold (masked CC) |
| `--cc-review` | 0.25 | REVIEW band floor |
| `--hand-ratio-min` | 3.0 | below this, handedness AMBIGUOUS |
| `--target-lobe-map` | none | **OPT-IN since 2026-08-19** (was recommended-default, now reversed — see §2b): lobe submap for hand-test + component ID (now PowerFit-routed, §2/§3), then local refit in whole map. Use when a whole-map search mis-locks onto a brighter neighbour, not as a precaution. |
| `--lobe-crop-min-ratio` | 3.0 | small-crop WARNING floor for `--target-lobe-map` (§2c); advisory only |
| `--lobe-crop-level-box-fraction` | 0.005 | absolute-level fraction (from the WHOLE map) used by the small-crop check (§2c) |
| `--local-refit-max-steps` | 150 | stage-2 ChimeraX `maxSteps` |
| `--accept-mode` | `multi_metric` | retained; **both modes now gate on CC_mask** |
| `--atoms-in-density-gate` | 0.60 | **v2: DIAGNOSTIC ONLY, no longer gates** |
| `--emit-mask-frame` | `both` | `winning` / `input` / `both` mask grids |
| `--map-symmetry` | `C1` | fill neighbours before flexible refine |
| `--refine-mode` | `two_stage` | vs `legacy` single-call |
| `--refine-cleanup-cycles` | `1,2,3` | tried in order, first pass wins |
| `--geometry-clashscore-max` | 10.0 | geometry gate |
| `--geometry-rama-favored-min` | 96.0 | geometry gate |
| `--geometry-rama-outliers-max` | 0.5 | geometry gate |
| `--geometry-rotamer-outliers-max` | 2.0 | geometry gate |
| `--max-drift-warn-A` / `--max-drift-fail-A` | 2.0 / 5.0 | drift reporting only |
| `--post-refine-cc-warn` | 0.50 | warn if final masked CC is below |
| `--model-fallback-providers` | `swissmodel,alphafold,rcsb_template,esmfold` | provider ladder on failure |
| `--no-model-fallback` | off | disable the ladder |
| `--model-state` | none | STATE keywords for template ranking (see §8) |
| `--target-res-from` / `--target-res-to` | none | residue range the map resolves |
| `--mask-radius-A` / `--mask-dilate-pix` / `--mask-soften-x-res` | 3.0 / 3 / 2.0× | mask geometry |
| `--mask-label` / `--mask-description` / `--sample-description` | none | naming + `MASK_README.txt` |
| `--vision-qc` | `render` | `off` / `render` / `gate` |
| `--rigid-only` | off | stop after rigid dock + local refit (skips flexible refine) |

All pre-existing flags (`--fasta`, `--map`, `--out-dir`, `--resolution`, `--score-key`,
`--search-levels`, `--dock-hands`, `--mirror-axis`, percentile/autobox knobs, `--mask-*`, `--python`,
`--modeling-script`, `--mask-script`, `--chimerax`, `--phenix-bin`, `--phenix-refine-extra`,
`--keep-hetero`, `--continue-on-model-error`, `--skip-modeling`, `--models-dir`, `--dry-run`) keep
their names, types and defaults.

# 15. Backward compatibility

- `import multichain_cryoem_autofit` always succeeds (verified in both `cryo_ief` and
  `cryosparc-tools` envs).
- Every gate is either a strictly-better default with the pre-port behaviour reachable via an
  explicit flag (`--accept-mode cc_only`, `--refine-mode legacy`, `--emit-mask-frame` includes
  `input` by default), or purely additive.
- Missing new Phenix helpers degrade to `legacy` refine with a WARNING rather than crashing.

# 16. Still open

- **Mask volume warning** should key on the full-weight core rather than the integral.
- **Symmetry-overlap QC**: report Cn self-overlap automatically for symmetric maps.
- v2 §1 bug 6 only partially addressed — the summary should count errored-vs-scored attempts.
- Under `--dock-method powerfit` the rigid stage no longer uses `preprocess_docking_map`
  (clip/cap/autobox) at all; that machinery is reachable only via `fitmap_search`.
- ~~Bug 4 (silent chain truncation)~~ FIXED 2026-07-30 (SWISS-MODEL HA trimer verified end-to-end:
  A→A, B→B, C→C, 1488 residues, dock ACCEPT CC_mask 0.6887 ratio 11.4, refine 0.7998 geometry OK).
- ~~`--refine-mode none`~~ REJECTED by the user; refine-then-mask stays the default.
- **`provider_rcsb_template` still does NOT do state-aware ranking** — unlike
  `provider_swissmodel_templates` (§ 0/§ 8), it takes the RCSB sequence-search API's TOP hit only
  (identity/e-value score), with no `--model-state` matching at all. This session (2026-08-19) only
  made its choice VISIBLE (title logged + `template_title` in the report); the ranking itself is
  unfixed — a real gap if `rcsb_template` is used as a provider in the fallback ladder on a
  state-sensitive sample.
