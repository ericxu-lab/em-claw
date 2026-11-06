# RELION Resolution Optimization

## Contents

- [Authority and required context](#authority-and-required-context)
- [Campaign authorization](#campaign-authorization)
- [Establish the baseline](#establish-the-baseline)
- [Optimization loop](#optimization-loop)
- [Particle-attrition recovery gate](#particle-attrition-recovery-gate)
- [Stage-tilted SPA CTF-refinement loop](#stage-tilted-spa-ctf-refinement-loop)
- [Documentation and web-research gate](#documentation-and-web-research-gate)
- [Stopping criteria](#stopping-criteria)
- [Final report](#final-report)

Improve a RELION SPA reconstruction scientifically and reproducibly. Treat
resolution optimization as sequential hypothesis testing, not a fixed recipe
or parameter sweep.

## Authority and required context

This reference defines an optimization campaign inside the parent
[RELION SPA orchestrator](../SKILL.md). It is not an independent execution
interface.

1. Apply the parent orchestrator completely. It remains authoritative for
   evidence, approvals, provenance, protected decisions, operation contracts,
   rendering, execution, retries, and state transitions.
2. Read
   [Refine3D and post-processing](refine3d-postprocessing.md)
   for every campaign.
3. Read the following routed references when their decision becomes relevant:
   - [initial model and Class3D](initial-model-class3d.md)
     for heterogeneity or subset selection;
   - [particle preparation](particle-preparation.md)
     for extraction, centering, box, particle quality, and upstream
     movie/coordinate provenance questions;
   - [symmetry and masks](symmetry-and-masks.md)
     for symmetry, mask, focused, or local-refinement questions;
   - [map interpretation](map-interpretation.md)
     only when making biological or structural claims.
4. Follow repository `AGENTS.md` and `RELION_RUN_STATE.yaml`. If this reference
   conflicts with the parent orchestrator, an operation contract, or a
   higher-priority instruction, follow the latter and record the conflict.

Do not copy dataset-specific tutorial values, symmetry, masks, optics settings,
or correction sequences into a new dataset merely because they previously
worked. Treat every prior recommendation or planned future step as a hypothesis
only; reapply the current evidence, eligibility, contract, and approval gates.
It is neither scientific evidence nor authorization.

## Campaign authorization

Read-only diagnosis does not require write approval. A request to process,
continue, or improve the reconstruction authorizes routine additive jobs and
bounded evidence-based optimization toward that objective. Record the task's
scope and any existing limits as the approval envelope; do not ask for a
separate campaign or per-segment confirmation. A diagnose-only request remains
read-only. Before project/state writes or job creation, record:

- the starting branch and allowed job families;
- parameter families and bounded ranges or named alternatives;
- compute and scientific-attempt budgets;
- acceptance, rejection, retry, and stopping rules;
- protected decisions already authorized and any that require new scope.

The processing request also covers reversible, supported symmetry/focused-mask
experiments and provisional downstream processing; it need not name each job.
Treat agent-chosen stage budgets as allocations that can be rebalanced within
one recorded finite campaign limit. Preserve actual user/hardware hard limits
and never reset the campaign limit merely because a new input state is created.
Budget exhaustion completes optimization with the best supported result; do not
ask to extend it by default. Available compute alone does not establish
scientific eligibility. Keep every prior
job. Never edit, delete, move, or overwrite RELION job artifacts. Continue from
the best supported usable branch, including an assessed provisional primary when
final acceptance is not yet justified, rather than automatically the newest job.

Defer LocalRes until the best map is selected. Use compatible existing
local-resolution evidence when available during baseline and branch
comparisons, but do not fan out a new job across candidates. After the
best/final deliverable map is chosen, opt in to LocalRes once for that selected
map. Run it earlier only when the user explicitly requests it.

## Establish the baseline

Before an expensive experiment, assemble the current reconstruction as
`BASELINE`; register it in state when processing is authorized. If there is no valid
mask and PostProcess result, preserve the starting candidate and establish a
valid baseline first.

Record enough identity and evidence to make later comparisons meaningful:

- exact particle STAR, particle count, optics groups, random-half assignments,
  and a particle/half-set fingerprint;
- exact half maps, reference grid, origin, handedness, symmetry, pixel size,
  box size, physical field of view, Nyquist limit, and MTF provenance;
- exact mask and PostProcess STAR;
- corrected masked, unmasked, and phase-randomized FSC behavior, including
  FSC=0.143 resolution and FSC=0.5 when available;
- sharpening B-factor and Guinier behavior;
- local-resolution and directional/anisotropy evidence when available;
- particle count, angular distribution, convergence, pose accuracy, and
  estimated angular/translational accuracy;
- CTF/optics diagnostics and the complete CTF-refinement and polishing history;
- the broader accepted 2D-cleaned particle STAR, count, and fingerprint when it
  exists, plus every InitialModel/Class3D input and per-class/selected count
  between that pool and the current reconstruction;
- an attrition ledger identifying each exclusion stage, exact class or particle
  population, evidence-supported disposition, and whether recovery was already
  tested; and
- relevant InitialModel/Class3D history and previous accepted, rejected,
  inconclusive, or failed experiments.

Do not judge the baseline from a sharpened map or one FSC number alone.

## Optimization loop

For each scientific attempt, complete the following loop.

### 1. Diagnose and rank bottlenecks

Prefer causes in this order unless the evidence clearly supports another
ordering:

1. provenance, metadata, contrast, geometry, pixel-size, handedness, symmetry,
   or CTF inconsistency;
2. severe junk, extraction, centering, box, mask, or sampling problems;
3. particle quality, compositional/conformational heterogeneity, or missing
   views;
4. alignment convergence or pose accuracy;
5. motion or CTF-model errors for which the reconstruction has sufficient
   signal.

State the leading hypothesis, supporting and contradicting observations,
confidence, predicted diagnostic change, and the cheapest decisive test.

### 2. Design one bounded experiment

Change one major processing decision at a time where practical. Use two or
three bounded branches only when they distinguish a genuine uncertainty; never
launch a large blind sweep. Predeclare:

- the hypothesis and expected effect;
- exact input branch and invariant controls;
- the single major changed family;
- the complete downstream evaluation chain;
- the evidence that will accept, reject, or leave the branch inconclusive.

Use these routes only when supported by evidence:

| Evidence | Preferred response |
| --- | --- |
| Metadata, contrast, geometry, pixel size, symmetry, or CTF inconsistency | Repair the upstream inconsistency before dependent optimization; defer only the affected branch and continue valid work. |
| Heterogeneity | Run a bounded Class3D test on the current particles, preserve minority states, and independently refine scientifically credible subsets. |
| Usable moderate-resolution pre-symmetry C1 consensus with substantial particle attrition | Apply the particle-attrition recovery gate below before tuning downstream corrections or testing non-C1 symmetry. |
| Sharp core with weak peripheral density | Inspect compatible existing local-resolution evidence when available and flexibility first. Supported focused-mask, symmetry, and local/no-align comparisons on preserved baselines are covered by processing or optimization requests when the typed contract and technical prerequisites pass. |
| Strongly non-uniform orientations | Diagnose preferred orientation and anisotropy. Do not claim parameter optimization can recover missing views; stop when acquisition is the limiting factor. |
| Poor alignment | Investigate centering, extraction, box, symmetry, particle quality, and reference suitability before polishing or high-order CTF fitting. |
| Plausible particle motion | Test polishing only after verifying compatible movie/MotionCorr provenance, frames, dose per frame, coordinates, optics, PostProcess reference, contract support, and compute. |
| Plausible CTF-model error with sufficient high-resolution signal | Test only the smallest identifiable CTF parameter family that passes the eligibility and contract gates below. |

### 3. Apply the particle-attrition recovery gate

Use rule `resolution_revisit_broader_2d_particles` to audit an upstream
particle-selection bottleneck when all of these planning flags coexist:

- an accepted, usable pre-symmetry gold-standard C1 consensus and compatible
  PostProcess baseline exist, with intact half-set provenance and no unresolved
  baseline red flag;
- the C1 corrected FSC resolution is roughly 3.5–10 Å; and
- `N_current < 100,000`, where `N_current` is the exact particle count used by
  that C1 reconstruction.

Measure the resolution and `N_current` from the same C1 particle branch before
any point-group symmetry is imposed. Do not evaluate this gate from a
symmetry-imposed FSC or adjust particle count by multiplying or dividing by the
symmetry order. If only a non-C1 reconstruction is available, establish a
matched gold-standard C1 Refine3D and PostProcess baseline first. Apply the
particle-attrition gate before comparing a non-C1 symmetry branch.

These values are planning flags, not evidence, universal cutoffs, or automatic
write authorization. Establish exact lineage to a materially broader,
compatible accepted 2D-cleaned population before proposing recovery. Record
the source selection job and class set, then compare canonical unique particle
lineage identities. Require no duplicate particle identities, verify
`current_ids ⊆ broad_ids` and `overlap_count = N_current`, and fingerprint the
exact `broad_ids - current_ids` set difference. Across re-extraction or
polishing, trace canonical identity through provenance rather than assuming an
image name is stable. Only after that proof record `N_current`, `N_broad`,
`N_excluded = N_broad - N_current`, particle fingerprints, optics groups,
extraction geometry, and half-set provenance.

Whenever a route below requires global Class3D on the broader pool, prefer the
best accepted, reference-suitable pre-symmetry C1 consensus from a compatible
particle lineage. Define `best_reference` by evidence-complete Class3D
reference utility: a coherent reproducible low-frequency envelope, no
unresolved reference or mask artifact, and compatible pixel/box grid,
origin/frame, hand, and C1 convention. Do not choose it merely because it is
the newest job, the sharpest-looking map, or the map with the smallest reported
FSC resolution. Prefer an unsharpened C1 reconstruction source, apply a strong
provenance-recorded `initial_lowpass_A` under the InitialModel/Class3D reference
treatment, and record the source-map fingerprint, filtering, any explicit
rescaling/reboxing, greyscale treatment, and compatibility inspection. Never
use a symmetry-imposed map as a neutral C1 reference.

Route from the earliest unsupported exclusion and take only the corresponding
route:

| Exclusion provenance | Bounded recovery route |
| --- | --- |
| Particles were removed by InitialModel/ab-initio class assignment, including a mixed lineage in which a later Class3D never received the broader pool. | Treat this as provenance repair. Restore the full appropriate accepted 2D particle set as the input to global Class3D, as required by the InitialModel/Class3D reference. Never refine only the particles assigned to the chosen InitialModel class as the final consensus. |
| The only exclusion was a Class3D selection whose input fingerprint equals the broader accepted 2D pool. | Audit every excluded class or population under the 3D-class evidence contract. Preserve confirmed junk and supported distinct states separately. If one next-ranked coherent, compatible population exists, test at most one exact, duplicate-free union with the current primary while preserving half assignments. If evidence instead shows the prior partition or selection was under-separated, unstable, or incomplete, test one bounded global Class3D on the full broader accepted 2D set under the recovery-reference rule above. Do not send all excluded particles directly to final Refine3D. If all exclusions are confirmed junk or supported distinct states, close the gate without recovery; if their dispositions remain ambiguous, first inspect the missing evidence or run a bounded eligible validation. Use `human_gate` with `await_human` only if no safe useful route remains. |
| Lineage, compatibility, or exclusion evidence is missing or conflicting. | Keep the accepted consensus primary and inspect provenance and source artifacts before recovery. If the essential gap remains, pause that route and continue other justified work; use `human_gate` with `await_human` only when user input is required. Do not reconstruct a broader set from guessed filenames, class numbers, or counts. |

Choose exactly one recovery route—the exact union or the broader global
Class3D branch—for a source selection checkpoint; do not try both serially.
Materialize any exact selected union through the typed class-selection path.
Require the union members to be disjoint, retain existing random-half
assignments unchanged, and assign recovered particles through an audited
gold-standard-safe route. Re-extract only when a verified geometry or sampling
change requires it.

When an exact-union recovery pair satisfies the general similar-class gate,
route it through `three_d_validate_similar_class_union` in the InitialModel/
Class3D reference rather than refining only the union. Reuse the current
primary as a singleton comparator only when its exact fingerprints, common
reference, scientific settings, mask, and PostProcess policy are genuinely
matched; otherwise complete a fresh matched singleton branch.

For a broader global Class3D route, run and assess the primary broader-pool
global Class3D recovery in C1. A plausible or shortlisted symmetry candidate
alone does not establish final symmetry or make a symmetry-imposed starting
map a neutral reference. After the C1 recovery Class3D is complete and assessed,
allow at most one named non-C1 candidate as a matched Class3D branch within the
processing or optimization scope when assessable evidence supports testing
its exact group and the axis/RELION convention are verified. Final symmetry
acceptance still requires independent evidence. Use the same broader-pool
particle fingerprint, accepted C1 reference source and low-pass, K, T, angular/offset searches, and other
scientific controls; change only `symmetry`. Do not impose symmetry when the
recovery objective includes asymmetric, partially occupied, pseudo-symmetric,
or minority states. Preserve the C1 branch and treat any non-C1 Class3D result
as provisional; final symmetry acceptance still requires a same-particle
matched C1 and candidate-symmetry Refine3D/PostProcess comparison under the
symmetry policy. Continue the supported C1 route when symmetry evidence or
technical eligibility is insufficient; request a human decision only if the
requested objective requires that unresolved symmetry choice.

Every recovery branch then requires:

- a fresh gold-standard C1 Refine3D, both half maps with intact provenance,
  and convergence evidence;
- a compatible validated global mask and matched PostProcess with corrected,
  unmasked, and phase-randomized FSC behavior;
- matched-filter map, solvent, edge, and corner inspection; and
- directional-resolution and orientation diagnostics when available, plus
  compatible existing local-resolution evidence when available.

Keep the current primary, every excluded population, and all state-specific
maps preserved while the recovery branch is assessed.

Accept recovery only when a predeclared, distinguishable improvement is
corroborated by compatible corrected and unmasked FSC behavior plus map
evidence, compatible existing LocalRes evidence when available, and at least
one positive evidence root outside half-map derivatives,
such as assignment/convergence behavior, orientation behavior, or an
independently initialized branch. Require no new heterogeneity dilution,
minority-state loss, reference bias, mask dependence, half-set leakage, edge
artifacts, or anisotropy/view warnings. Increased particle count or a smaller
nominal resolution alone is insufficient. A supported homogeneous subset may
remain the best global or state-specific result even when it contains fewer
than 100,000 particles.

Derive a stable `selection_checkpoint_id` before recovery from the source
broader-pool fingerprint, source current-particle fingerprint, and earliest
source exclusion stage/job. Store the exact union or Class3D experiment under
a separate `candidate_branch_fingerprint`; never replace the checkpoint's
source fields with its recovery output. An accepted, rejected, or inconclusive
recovery closes that checkpoint. Do not retrigger it because the recovery
created a new particle or refinement state. Open a new checkpoint only when an
independently justified upstream preparation or sorting job creates a new
broader-pool or source-exclusion fingerprint.

### 4. Apply the CTF-refinement eligibility gate

Poor resolution alone is not evidence for CTF refinement. Read-only diagnosis
may identify CTF refinement as a conditional candidate. Before authorizing or
launching it, verify all of the following:

- a valid, broad PostProcess mask encompasses nearly the full complex and does
  not manufacture the apparent high-resolution signal;
- the relevant optics group or particle grouping has enough observations;
- the reference contains signal beyond the range needed to identify the fitted
  parameter;
- the requested controls are representable by the installed typed operation
  contract and remain within the approved envelope;
- the experiment fits one parameter family at a time when practical.

Use these conservative signal checks unless exact-version authoritative
documentation establishes a different requirement:

- beam tilt: useful signal beyond approximately 4.5 Å;
- trefoil: useful signal beyond approximately 3.5 Å;
- fourth-order aberrations: useful signal beyond approximately 3.0 Å;
- per-particle defocus: a reference extending well beyond approximately 4 Å;
- per-micrograph astigmatism: enough particles per micrograph for stable
  estimation.

Estimate anisotropic magnification separately from higher-order aberrations.
At substantially lower resolution, prioritize heterogeneity, particle quality,
orientations, alignment, sampling, and motion. A small sharp core or a
mask-dependent FSC crossing does not by itself pass this gate.

An improved CTF-refinement likelihood never accepts a branch. Acceptance
requires a fresh gold-standard Refine3D and matched PostProcess evaluation.

If a scientifically justified CTF or polishing choice is not expressible by
the current typed contract, record a `contract_gap` and stop that branch while
continuing other justified work. Request contract development only if the
objective cannot otherwise progress. Do not use free-form arguments, shell commands, or an undocumented
workflow default to bypass the contract. Contract expansion is a separate
software-development task, never an in-campaign workaround.

#### Stage-tilted SPA CTF-refinement loop

Apply this rule only to single-particle micrographs collected at one or more
intentional specimen-stage tilts. Do not apply it to a tomography tilt series;
the SPA `ctf_refinement` operation is not `TomoCtfrefine`. Verify acquisition
metadata rather than inferring stage tilt from preferred orientations or a
defocus plot. Particle `rlnAngleTilt` is an orientation estimate, not
acquisition-stage metadata. Record `dataset.stage_tilt.status`, the angle for
each acquisition or optics group, the micrograph-to-group assignment, and
provenance. Preserve separate groups when acquisition conditions genuinely
differ.

Treat specimen stage tilt as acquisition geometry, not beam tilt. Stage tilt
predicts a position-dependent defocus gradient across each micrograph. It does
not itself authorize `fit_beamtilt`, anisotropic magnification, trefoil, or
fourth-order aberration estimation; test those as separate, individually
eligible hypotheses under the ordinary gates above.

Treat confirmed stage tilt as positive evidence to prioritize per-particle
defocus refinement after the ordinary signal and identifiability gates pass,
not as a waiver of those gates. If the reference does not yet support a stable
fit, record `diagnosis: not_yet_eligible` and improve or diagnose the upstream
reconstruction first. Do not weaken the signal requirements for beam tilt or
higher-order aberrations merely because the stage was tilted.

For an eligible stage-tilted branch, pre-budget an initial per-particle-defocus
round plus at least one evidence-gated repeat, with capacity for a third round
when the preceding assessed usable result retains a material residual and the
budget allows it. A one-pass plan is incomplete unless an explicit stop condition is
met. Do not execute a fixed number of rounds regardless of evidence.

Treat each round as one complete scientific attempt:

1. Consume the last assessed usable Refine3D particle STAR and its latest
   matching PostProcess STAR, whether accepted or a credible provisional
   primary. Preserve random-half assignments and the preceding
   accepted particle/map state.
2. Render a uniquely aliased `ctf_refinement_stage_tilt_defocus_rNN` job with
   `fit_ctf: true` and `fit_defocus: Per-particle`. Keep astigmatism, beam tilt,
   anisotropic magnification, trefoil, and fourth-order terms off unless an
   independently supported and contract-authorized hypothesis explicitly
   includes one of them. Render every Boolean and enum; do not rely on stale
   GUI or tutorial values. The current scientific fit controls require an
   exact allowed source such as an explicit user override; if the intended
   evidence-selected values lack contract authority, record a `contract_gap`
   rather than relabeling them as workflow defaults.
3. Apply the Refine3D/postprocessing handoff completely: create the new input
   state, run a fresh gold-standard Refine3D with preserved half sets, and run
   matched PostProcess before judging the round or launching another one.
4. Record the round counter, parent state, fitted family and search bounds,
   parameter deltas, defocus-versus-image-position evidence by tilt group,
   FSC/map comparison, expected effect, success condition, and predeclared
   exit test. Inspect the CTF plots directly when morphology of the fitted
   gradient is part of the decision.
5. Seed the next round from this assessed Refine3D/PostProcess state when
   technical prerequisites and evidence of residual correctable error pass.
   A credible `medium`-confidence primary may continue provisionally under
   `primary_fallback`; final `high`-confidence acceptance is not required to
   run the next valid experiment. Never chain an evidence-gated repeat directly
   from an unassessed CtfRefine output.

If the MCP surface cannot expose a required CTF plot or particle-position
diagnostic as directly inspectable evidence, record the evidence gap and try
available reports or compatible existing artifacts. Preserve the last accepted
branch, or the last usable assessed provisional primary, while the round
remains unassessed; use `human_gate` with `await_human` only when the essential gap cannot be resolved autonomously and blocks useful
progress. Do not accept a round from the fit likelihood alone.

The bundled aberration, anisotropic-magnification, and per-particle-defocus
workflow steps remain distinct parameter-family passes; do not count those
three chained jobs as three accepted stage-tilt cycles. A stage-tilt repeat is
complete only after its fresh Refine3D and matched PostProcess evaluation.

Stop when any valid round is non-distinguishable or worse, the residual
gradient is resolved or stable, fits are unstable or boundary-limited, signal
eligibility is lost, an upstream bottleneck becomes dominant, or a contract,
approval, attempt, or compute limit is reached. Keep the last accepted branch
primary, or the last usable assessed provisional primary, and preserve the
stopped round. Continue other justified processing after this loop ends. Never
continue merely to reach the pre-budgeted round count.

### 5. Execute through the orchestrator

For each approved branch:

1. run the orchestrator's environment and workflow validation;
2. reload and reconcile `RELION_RUN_STATE.yaml`;
3. inspect exact upstream artifacts and render exact typed inputs/options;
4. apply the contract gate and use the combined `relion_run_*` operation with a
   unique alias;
5. follow every meaningful upstream correction or particle selection with a
   fresh gold-standard Refine3D, matched mask/PostProcess, and relevant local or
   directional resolution diagnostics;
6. write artifacts and decisions back to state before starting another branch.

Preserve half-set independence throughout. Never use a joint high-resolution
map as an effectively unfiltered restart reference. If the available typed
workflow cannot represent a gold-standard-safe continuation, record the gap
and stop only that route rather than improvising; continue from a usable
fallback or return the best supported result.

### 6. Compare like with like

Compare a branch against the same registered baseline using compatible:

- particle and half-set identities or an explicitly justified selection;
- symmetry, pixel size, grid, MTF, masking, PostProcess, and FSC procedures;
- downstream refinement and evaluation settings.

Treat the resolution as a tuple of FSC curve, threshold crossing, mask,
pixel/grid calibration, particle state, and half-set identity—not a scalar.
Use the physical box width to estimate Fourier-shell spacing:

`delta_q = 1 / physical_box_width_A`

Predeclare a distinguishable gain at the available shell sampling and require
corroboration from multiple independent diagnostics. Do not use a universal
0.1–0.2 Å threshold. Pixel recalibration, mask-only shifts, or sharpening-only
changes are not genuine resolution improvements.

Check for leakage, reference bias, mask artifacts, edge effects, anisotropy,
minority-state loss, and implausible FSC behavior. Evidence derived from the
same two half maps is correlated; use independent diagnostics such as optimizer
behavior, particle/CTF/motion changes, directional resolution, or a declared
perturbation where appropriate.

### 7. Accept, reject, or retain as alternate

Accept a branch as `accepted_primary` only when the predeclared, distinguishable
improvement is supported by compatible FSC and map evidence, plus compatible
existing LocalRes evidence when available, without a new warning. Use
`accepted_fallback` or a state-specific/local best when it improves an
interpretable region or state but is not the best global map.

Reject a branch when it performs worse, fails its predicted diagnostic effect,
or introduces a scientific warning. Mark it `inconclusive` when the comparison
is invalid or undersampled. Particle loss is not by itself grounds for
rejection when a justified homogeneous-state selection is the experiment, but
preserve and report excluded minority states.

These dispositions are per-branch results, not additional decision or action
enum values. Record every branch in its applicable run-state collection, then
record exactly one decision for the aggregate checkpoint outcome:

| Aggregate checkpoint outcome | `decision` | `next_action` | Run-state collections |
|---|---|---|---|
| One accepted primary and no retained alternate | `auto_accept` | `advance_primary` | `accepted_outputs`; also record any rejected branches in `rejected_branches` |
| One accepted primary plus preserved state/local alternatives | `primary_fallback` | `advance_primary` | `accepted_outputs` and ordered `inactive_fallbacks`, including activation triggers |
| A previously preserved fallback is activated | `primary_fallback` | `activate_fallback` | `accepted_outputs` and `activated_fallbacks` |
| Every candidate individually satisfies high-confidence rejection, the reject-all guard passes, and a bounded scientific attempt remains | `auto_reject` | `repeat` | `rejected_branches` and the attempt in `iterations` |
| Every candidate individually satisfies high-confidence rejection, the reject-all guard passes, and no bounded attempt remains | `auto_reject` | `stop` | `rejected_branches` |
| One credible candidate is provisionally preferred at `medium` confidence | `primary_fallback` | `advance_primary` through normal downstream stages or `repeat` for an experiment | `decisions`, `iterations`, and preserved ordered `inactive_fallbacks`; do not record final acceptance |
| Evidence is inconclusive and a safe diagnostic or bounded correction remains | `defer` | `repeat` or `retry` | `decisions` and the attempt in `iterations`; record the unresolved evidence gap |
| Evidence remains inconclusive and the bounded attempt or compute budget is exhausted | `defer` | `stop` | `decisions` and preserved inconclusive branches; do not relabel uncertainty as rejection |
| Essential evidence or authorization requires user input and no safe useful work remains | `human_gate` | `await_human` | `pending_human_gates` |

Keep final `auto_accept` and `auto_reject` at `high` confidence. A `medium`
`primary_fallback` selects a credible reversible primary, preserves every
alternative without rejection, and never establishes a final scientific
conclusion. That primary may feed normal downstream processing across multiple
stages, with assessment and a recorded decision after each stage. Use `defer`
for unresolved science requiring diagnostics or branch-local termination.
Reserve `human_gate` exclusively for `next_action: await_human` when a user
answer is actually required. A `stop` ends the affected branch or optimization
loop; continue other requested work from the last usable primary/fallback.
Use `repeat` for a changed scientific hypothesis or validation attempt and
`retry` only for recovery from an execution or evidence-delivery failure.
Having no accepted candidate never establishes that all candidates are junk.

## Documentation and web-research gate

Project artifacts, `RELION_RUN_STATE.yaml`, routed skill references, bundled
operation contracts, and exact-version local help are required operating
evidence. Reading a known applicable local source is not discretionary external
research.

Before any external documentation or web search, record:

- `uncertainty`: `low`, `medium`, or `high`;
- `uncertainty_kinds`: one or more of `empirical`, `methodological`,
  `version_specific`, or `contract`;
- the exact missing or conflicting fact;
- how different answers would change the decision;
- the local evidence and exact-version help already checked.

Classify the consequence of the unresolved fact, not the general difficulty of
the dataset:

- `low`: the applicable evidence is complete enough, or another answer would
  not change the action;
- `medium`: the gap may change hypothesis ranking, but a safe bounded local
  diagnostic can resolve or contain it;
- `high`: the gap blocks a safe material choice about the experiment, fitted
  family/range, acceptance, gold-standard independence, or major compute use.

Do not perform any external documentation or web search for low or medium
uncertainty. Do not search merely because resolution is poor, a correction
exists, or a supported decision could use another citation. A narrowly focused
search is still an external search and follows the same gate.

Perform external documentation research only when all of the following are
true. Otherwise follow the already determined action without researching a
moot premise, unless the explicit browse override below applies.

1. documentation-relevant uncertainty is `high`;
2. a specific fact remains missing or conflicting after project evidence,
   routed references, contracts, and exact-version local help;
3. authoritative documentation could answer it—the uncertainty is not an
   empirical property of this dataset;
4. different answers would materially change the next experiment, fitted
   parameter family/range, acceptance rule, gold-standard safety, or a major
   compute decision; and
5. no existing contract, approval, provenance, evidence, or safety failure
   already determines the current action.

Then formulate one focused research question. Check exact-version local
documentation first; if unresolved, consult official RELION documentation,
release notes, or upstream source, then the primary method paper if needed.
Stop researching when a version-matched authoritative source resolves the
question. Record the source, version, conclusion, decision impact, and
remaining uncertainty.

Documentation never overrides dataset evidence, the typed contract, approval,
or gold-standard safeguards. A documented but unrenderable option remains a
`contract_gap`. High empirical uncertainty—such as heterogeneity, orientations,
motion, centering, or particle quality—does not trigger web research; resolve
it with local diagnostics, a bounded branch, or a human gate. If high
documentation uncertainty remains unresolved, preserve the branch and do not
act on the uncertain premise.

An explicit user request or higher-priority requirement to browse overrides
only the no-search restriction. It never overrides contracts, approvals,
provenance, evidence, or gold-standard safeguards.

## Stopping criteria

End the optimization campaign and return its best supported result when any
condition applies; continue any remaining requested pipeline stages that are
technically valid. These are completion rules, not automatic user prompts:

1. three scientifically distinct, valid attempts fail to produce a
   distinguishable corroborated improvement;
2. two consecutive valid optimization cycles produce no distinguishable gain
   at the available Fourier-shell sampling;
3. evidence identifies an acquisition-limited bottleneck, such as severe
   preferred orientation or intrinsically poor particles;
4. all remaining useful routes require an unauthorized protected decision,
   exceed the task's scope, or are unsupported by the typed contract, and no
   bounded diagnostic or usable assessed fallback can resolve the blockage;
5. further work risks half-set leakage, reference bias, or another
   gold-standard violation;
6. the campaign reaches its recorded finite overall attempt or compute budget,
   or an actual user/hardware limit;
7. required evidence or upstream data for the only remaining justified branch
   are unavailable.

Three failed experiments is a ceiling, not a target. Stop earlier on a decisive
red flag. One scientific attempt means a complete hypothesis through its
downstream Refine3D/PostProcess assessment. Transient retries, invalid jobs, and
inconclusive comparisons do not count as distinct scientific failures. Missing
movies stops polishing, not unrelated justified branches. Agent stage
allocations may be rebalanced within the overall limit, but missing evidence
or an exhausted campaign never silently expands that limit. Ask a question
only when completing the requested outcome actually requires an essential
user answer; do not ask for permission to continue a valid provisional path.

## Final report

At termination, report the best assessed outputs, including provisional
results with explicit confidence and limitations when final acceptance is not
justified. Do not ask for final acceptance or add experiments solely to obtain
a high-confidence label. Include:

- **BEST GLOBAL:** job, exact reconstruction tuple, resolution, particle count,
  and half-set identity;
- **BEST LOCAL/STATE-SPECIFIC:** optional alternate branches and what they
  improve;
- **BASELINE:** the same identity fields and resolution evidence;
- **IMPROVEMENT:** signed `baseline_A - best_A`, Fourier-shell
  distinguishability, and comparison compatibility;
- **EXPERIMENTS:** exact inputs/outputs, hypothesis, changed family, expected
  effect, disposition, and decisive evidence for every branch;
- **HELPED / FAILED / INCONCLUSIVE:** separated explicitly;
- **LIKELY REMAINING BOTTLENECK:** with confidence and contrary evidence;
- **CONTRACT GAPS OR PROTECTED DECISIONS:** if any;
- **NEXT STEP:** one concrete processing, decision, or acquisition action;
  request user input only when the remaining action requires it.

The report must make clear whether the campaign improved global resolution,
only a local/state-specific result, or neither.
