---
name: relion-spa-orchestrator
description: >
  Orchestrate, diagnose, visually assess, select, resume, and iteratively
  improve RELION 5 single-particle cryo-EM processing through the RELION MCP.
  Use for CryoSPARC particle import into RELION, particle picking, extraction,
  selection, 2D classification, initial-model recovery, 3D classification,
  symmetry, masks, Refine3D, solvent-flattened FSC or solvent-corrected FSC,
  postprocessing, stage-tilted acquisition CTF
  refinement, handedness, biological map interpretation, and evidence-driven
  resolution optimization after an unsatisfactory refinement. Select
  dataset-dependent parameters with evidence, preserve fallbacks, and continue
  processing autonomously within the requested scope.
---

# RELION SPA Orchestrator

## Scope and operating contract

Process a single-particle cryo-EM dataset in the stage order of the bundled
[`relion_5_spa_tutorial_v1.yaml`](../../../src/relion_mcp/configs/relion_5_spa_tutorial_v1.yaml).
Treat that workflow as a dependency graph. Values under
`tutorial_example_params` are non-executable documentation, never dataset
defaults. Bind every `explicit_param_overrides` requirement from metadata or a
recorded scientific decision and require the plan step to report
`execution_ready: true` before rendering or running it. Follow the repository
[invariants](../../../AGENTS.md) and the exact [tool API](../../../docs/API.md).
Treat [`operations.yaml`](../../../src/relion_mcp/configs/operations.yaml),
[`relion_pipeliner_mappings.yaml`](../../../src/relion_mcp/configs/relion_pipeliner_mappings.yaml),
and [`relion_operation_manifest.yaml`](../../../src/relion_mcp/configs/relion_operation_manifest.yaml)
as the executable operation contract.

Use MCP tools for covered RELION operations. Do not invoke covered executables
through a shell, bypass a missing typed option through `other_args`, or edit a
RELION pipeline, STAR file, map, optimiser, particle stack, or job directory in
place. Preserve every completed scientific branch and use a unique alias for
every attempt.

Keep manual picking opt-in. Do not render, run, or request approval for
`manual_particle_picking` unless the user explicitly requests it. Without that
request, continue from accepted CTF micrographs through subset selection and
LoG autopicking; missing manual picks are not a scientific gate.
When requested, validate with
`include_opt_in_steps=["manual_particle_picking"]`, preserve the interactive
pause, and bind its coordinate output explicitly before extraction.

Keep RELION LocalRes deferred. Do not render, run, or request approval for
`local_resolution` as a routine per-candidate, matched-branch, or intermediate
step. After selecting one best/final deliverable map, include
`local_resolution` as an opt-in final characterization and run it once for that
selected map. Run it earlier only when the user explicitly requests it. Reuse
compatible existing local-resolution evidence when available, but do not
equate localized density inspection with a new LocalRes job.

Do not execute ModelAngelo or DynaMight unless workflow validation reports the
requested operation executable.

## Continuous execution

Processing requests authorize completion of the requested workflow, not just
its next job. Checkpoints and job launches are progress updates, not places to
end the turn or ask for continuation. Monitor active jobs at sensible intervals;
after completion, inspect outputs, update state, and dispatch the next ready
step. Continue until the requested outcome, an explicit user stop, a hard
resource limit, or an essential unresolved blocker. If the client interrupts
execution, persist the exact next action so resume continues without reapproval.

Resolve routine technical choices yourself. Use a credible provisional branch
when full scientific confidence is unnecessary for the next processing step.
Do not run an extra experiment solely to fill an evidence checklist; reuse
compatible existing evidence and let needed downstream jobs provide validation.
Scientific uncertainty limits the claim, not automatically the processing.
Never invent required acquisition facts, consume failed outputs, or violate
half-set, geometry, executable-contract, path, or no-clobber checks.
The InitialModel-to-Class3D transition is a fail-closed exception. A provisional
or medium-confidence initial map cannot enter Class3D. Require the recorded,
high-confidence `initial_model_accepted` checkpoint and exact hash-bound
reference, final particle-state, seed-lineage, and geometry agreement defined in
the InitialModel/Class3D reference before registering, starting, or launching
Class3D. Never add or launch Class3D through native `relion_pipeliner`, the
scheduler helper, or another shell path; use only the guarded MCP tools.

The request covers supported additive hypothesis tests, including one justified
symmetry comparison against C1 and focused/no-alignment exploration of a
supported structural target, within the campaign budget. Preserve baselines
and respect explicit user choices such as "C1 only". No separate hypothesis
approval is needed. A scan or trial alone still cannot establish symmetry or
biology; retain C1 or an unmasked baseline when the evidence is unresolved.

Distinguish user/hardware limits and the finite overall campaign budget from
agent-chosen stage allocations. Rebalance stage allocations within the overall
budget without asking. Do not enlarge explicit user caps or continue unbounded
optimization. An unproductive branch ends locally; use a usable fallback and
continue the pipeline. At a campaign limit or diminishing returns, return the
best available outputs with limitations instead of asking for a budget increase.

Ask only for an essential fact that cannot be recovered and prevents any valid
continuation or useful deliverable, an unavoidable conflict with explicit user
constraints, or necessary destructive/external-publication authority. Keep
optional hypotheses and unsupported biological interpretations unresolved in
the report. Bundle genuinely blocking questions; never ask whether to continue
routine authorized work.

## Reference routing

Read each applicable reference completely before planning, selecting
parameters, assessing evidence, or running its stage. Read multiple references
when a decision crosses their boundaries. Keep the shared policies in this
file authoritative; keep detailed stage rules in exactly one reference.

| Work in scope | Required reference |
| --- | --- |
| CryoSPARC particle import for RELION Class3D, picking, Topaz, reference picking, extraction/re-extraction, micrograph or particle selection, 2D classification, and exact class selection | [Particle preparation](references/particle-preparation.md) |
| InitialModel seed construction/recovery, reference preparation, Class3D, and 3D-class assessment/selection | [Initial model and Class3D](references/initial-model-class3d.md) |
| Symmetry inference/imposition/relaxation and global or focused masks | [Symmetry and masks](references/symmetry-and-masks.md) |
| Refine3D, autonomous solvent-flattened FSC (solvent-corrected FSC) selection, refinement acceptance, postprocessing, and CTF-refinement/polishing handoff | [Refine3D and postprocessing](references/refine3d-postprocessing.md) |
| Diagnosing and iteratively improving an unsatisfactory completed Refine3D/PostProcess reconstruction | [Resolution optimization](references/resolution-optimization.md) |
| Handedness, biological interpretation, model-based evidence, and deposition/publication claims | [Map interpretation](references/map-interpretation.md) |

For an optimization campaign, read the resolution-optimization reference,
Refine3D/postprocessing reference, and every additional stage reference routed
by the diagnosed bottleneck.

Apply detailed conditional parameter rules only where the applicable reference
defines them. At the current development stage, those matrices cover particle
preparation, InitialModel/Class3D, Refine3D, and post-refinement resolution
optimization. Treat symmetry and focused masks as controlled experiments with
matched baselines. Absolute hand, sharpening interpretation, and biological
claims still require evidence appropriate to those conclusions.

## Decision model

Record one scientific `decision` at every checkpoint:

- `auto_accept`: accept a supported output while preserving provenance.
- `auto_reject`: reject obvious junk or invalid output without deleting it.
- `primary_fallback`: advance one credible primary while preserving ordered,
  inactive alternatives with activation triggers. At medium confidence this is
  provisional processing, not final scientific acceptance.
- `defer`: leave a scientific conclusion unresolved while gathering evidence,
  retrying, or ending that branch without asking the user.
- `human_gate`: record an unresolved decision. First gather evidence or repair
  the problem; ask one focused question only when useful autonomous progress
  requires missing user input or authorization. Use it only for an actual
  `await_human` checkpoint; use `defer` for internal unresolved decisions.

Record the operational consequence separately as `next_action`:
`advance_primary`, `activate_fallback`, `repeat`, `retry`, `await_human`, or
`stop`.

Only `high` confidence permits final `auto_accept` or `auto_reject`.
Require complete directly inspected evidence, agreement of
at least two independent evidence channels, no unresolved counterevidence or
safety red flag, and stability under a reasonable perturbation when needed for scientific
`high`. Reversible provenance and action inside the approved boundary are
separate gates for autonomous action; they do not raise or lower the epistemic
confidence of a map relation. A validated scientific red flag may support
`auto_reject`; it must not be an unresolved hazard. At `medium` confidence,
`primary_fallback` may advance one credible, assessable primary through ordinary
downstream processing within the task scope. This may span successive stages;
assess each output before reuse and preserve every alternative with a rank and
activation trigger. Record what remains uncertain and the evidence that would
resolve it. Do not reject classes or put provisional outputs in `accepted_outputs`;
track usable provisional outputs in `provisional_outputs`. Needed downstream
jobs can provide the validation; no separate experiment is required merely to
proceed. Strong conclusions still need high confidence, but routine continuation
does not require a new second evidence channel or perturbation at every stage.
This rule never authorizes Class3D from an InitialModel `primary_fallback`;
that boundary requires `assessment.seed_status: SEED_READY`,
`assessment.decision: auto_accept`, `assessment.reference_utility: credible`,
`assessment.confidence: high`, `assessment.next_action: advance_primary`,
`assessment.approval_flag: true`, and the complete
`relion.initial-model-acceptance/v1` record.

At `low` confidence, do not choose a primary; gather evidence or repair the
problem. Use `defer` with `next_action: retry`, `repeat`, or `stop` for unresolved
science. Legacy `human_gate` records with those actions do not request input.
Only `next_action: await_human` asks the user. Confidence tiers and ranking
scores are not calibrated probabilities.

Count independence by root evidence source, not by presentation. A PNG,
isosurface, histogram, threshold-component count, and correlation calculated
from the same MRC are transformations of one `map_pixels` root, not independent
channels. Pre/post-alignment correlations, overlap metrics, difference views,
filters, contours, and transforms derived from the same map pair remain one
`map_pixels` root; optimizing a transform does not create an independent
channel. Likewise, repeated descriptions of one STAR field are one metadata
root. Record root artifact or experiment identifiers and the derivation for
every evidence item. Prefer agreement among roots with different failure modes,
such as map pixels, particle assignments/orientation distributions,
late-iteration convergence, an independently initialized branch, or bounded
downstream validation. Same-run roots may corroborate a decision, but do not
mislabel causally coupled derivatives as an independent perturbation.

Apply a reject-all guard to every multi-output checkpoint. `auto_reject` of the
entire set is allowed only when every output is individually assessed as
obvious junk or invalid, every rejection satisfies the high-confidence rules,
and no output is credible or ambiguous for its stage-specific role. A
difference between outputs is evidence of an inter-output relation, not
evidence that either output is junk. If any output remains credible or
ambiguous, preserve it and use `primary_fallback` or `defer` as applicable.

Advance only the primary through the ordinary processing continuation. Activate
at most the next ranked fallback when its
recorded trigger is met or the user redirects the workflow. If candidates
cannot be ranked reliably, preserve them and gather the missing evidence or
run one bounded discriminating experiment. Use `human_gate` with `await_human`
only when that cannot resolve a necessary choice; do not process all candidates
by default.

The particle-preparation reference also permits a bounded recovery diagnostic
before Topaz or 3D when material attrition, coverage loss, or uncertain mixed
classes justify it. Preserve the primary while examining the exact recoverable
pool, optionally with recorded retained alignment support. This is one declared
diagnostic with finite attempts and audited identities, not permission for
unlimited parallel continuations or recycling proven junk. Assess the result,
record one aggregate checkpoint, and resume one primary. A first clean 2D result
does not require a second cleaning round merely to satisfy a counter.

Rule `three_d_validate_similar_class_union` is a narrow staged matched-control
exception. When its eligibility, contract, approval, and compute gates pass,
first complete the declared singleton-refinement comparison. Materialize and
refine the exact particle union only when the predeclared aligned compatibility
gate passes. A medium-confidence compatible pair may justify the experiment;
promotion of the union still requires the complete refined `same`/`high` gate.
Treat both stages as one bounded experiment. These controls do not
authorize multiple continuation branches outside that declared experiment or
processing an unbounded class set; the paired A/B diagnostic branches are
explicitly allowed inside it. Record one aggregate decision and continue from
one primary afterward.

## Visual evidence

Never claim to see an artifact unless the active model received its actual
image content. A path, STAR row, class number, filename, map dimension, or
scalar metric is not visual evidence.

Before the first scientific visual decision after an MCP process, SDK, Codex
client, or model change:

1. Call `relion_visual_smoke_test`.
2. Read the randomized quadrant tokens from the attached pixels.
3. Submit them to `relion_verify_visual_smoke_test`.
4. Record `visual_access: direct` only when `passed=true`; otherwise record
   `unavailable`, attempt to restore image access, and continue independent
   work. Use `human_gate` with `await_human` only if decisive morphology remains
   inaccessible and no useful authorized fallback exists.

Inspect every scientific PNG, not only its trailing metadata. Record the tool,
source paths, rendering parameters, observations, and returned image SHA-256;
do not store base64 data. Combine visual evidence with structured RELION
evidence. Scientific 3D comparisons require compatible hand and sampling plus a
verified common physical frame. For a quantitative pairwise density claim, call
`relion_compare_maps`: estimate the candidate-to-reference transform once at a
predeclared physical low-pass and apply that fixed transform to every matched
comparison filter and contour. Ordinary per-map renderers do not align maps.

Record both source paths and hashes, grid/hand checks, transform direction and
parameters, alignment filter/bounds/status, comparison filter/mask/valid
overlap, pre/post metrics, contour convention, and evidence-root identity. A
failed, unstable, boundary-limited, or insufficient-overlap alignment makes the
relation `unassessable`, not `distinct`; do not refit separately at each filter
or contour. Never treat independently auto-scaled panels or unmatched contours
as a quantitative comparison. Keep raw evidence available whenever a filtered
or normalized view is used.

## Required context and startup

Resolve context when the next operation needs it. Recover values from the
project, acquisition records, and prior state before asking the user. Missing
downstream context does not block a ready upstream stage:

- project directory, pipeline name, raw-movie glob, and gain reference;
- calibrated pixel size, voltage, spherical aberration, amplitude contrast,
  dose, detector/MTF provenance, optics groups, contrast convention, and
  acquisition geometry, including stage-tilt angles/group assignments and
  their provenance;
- particle dimensions and the extraction/re-extraction geometry plan;
- composition, oligomeric state, symmetry hypotheses, and available
  sequence/structure evidence;
- GPU, MPI/thread, queue, RAM/storage, elapsed-time, and attempt budgets;
- the approval boundary and permitted autonomous decisions; and
- visual access plus independent symmetry and hand evidence.

Unknown or conflicting acquisition metadata, contrast, or geometry blocks only
operations that depend on it. Unknown identity, oligomeric state, symmetry, or
hand evidence does not block ordinary C1 image processing, but limits symmetry,
handedness, and biological conclusions. Do not invent acquisition facts or use
tutorial values to fill required metadata.

Start every processing session as follows:

1. Call `relion_environment`, passing `project_dir` only when it exists.
2. Call `relion_validate_workflow(workflow_id="relion_5_spa_tutorial_v1")`.
3. Load `RELION_RUN_STATE.yaml` when it exists. Otherwise, draft the missing
   state in memory without creating the file.
4. Verify metadata, geometry, input provenance, resources, and explicit
   overrides. Resolve every `missing_explicit_param_overrides` entry and require
   `execution_ready: true` for the next step.
5. Complete the visual smoke challenge if the session will make a scientific
   visual decision and this MCP/SDK/client/model combination has not passed it.
6. Derive authorization from the user's processing request and any existing
   limits. A request to run, continue, or optimize authorizes routine additive
   work needed for that objective; do not require a separate segment approval.
   A planning-only request or server plan mode remains read-only.
7. Create or update `RELION_RUN_STATE.yaml` with the request's scope, input
   branch, permitted parameter groups/ranges, finite budgets, autonomous
   decisions, evidence, fallback triggers, and stops. Use existing limits or
   record conservative limits from available resources; do not require the
   user to fill a budget form. Keep the draft in memory in read-only sessions.

Treat `relion_suggest_step_inputs` as a provenance hint, never as a scientific
selection.

## Run-state and provenance

Maintain at least:

```yaml
workflow_id: relion_5_spa_tutorial_v1
project_dir: /approved/project/path
pipeline_name: default
visual_access: direct
approval_envelope:
  authorization_source: user_processing_request
  request: null
  stages: []
  allowed_parameter_groups: {}
  protected_actions: []
  max_scientific_attempts: {}
  max_native_iterations: {}
  max_vdam_mini_batches: {}
  compute_budget: {}
  autonomous_decisions: [auto_accept, auto_reject, primary_fallback, defer]
dataset:
  movie_glob: null
  gain_reference: null
  pixel_size_A: null
  voltage_kv: null
  spherical_aberration_mm: null
  amplitude_contrast: null
  dose_per_frame: null
  contrast_polarity: null
  stage_tilt:
    status: unknown
    angles_deg_by_group: {}
    group_assignment: null
    provenance: null
  particle_extent_A: {minimum: null, typical: null, maximum: null}
  molecular_composition: null
  oligomeric_state: null
  symmetry_hypotheses: [C1]
geometry_states: {}
micrograph_subsets: []
topaz_training_labels: []
preparation_partitions: []
preparation_recovery: []
initial_model_seed_partitions: []
initial_model_recovery: []
initial_model_acceptance:
  artifact_path: null
  record_sha256: null
  checkpoint: null
resources: {}
step_bindings: {}
accepted_outputs: {}
provisional_outputs: {}
inactive_fallbacks: []
activated_fallbacks: []
iterations: {}
parameter_decisions: []
decisions: []
contract_gaps: []
pending_human_gates: []
rejected_branches: []
```

Set `dataset.stage_tilt.status` to `confirmed`, `absent`, `mixed`, or `unknown`.
Use acquisition records for the angles and group assignment; particle Euler
angles are not acquisition-stage metadata.

For particle preparation, retain the deterministic micrograph-split manifest
and hash in `micrograph_subsets`, label and training-micrograph identities in
`topaz_training_labels`, and exact retained/ambiguous-recoverable/proven-junk
identities and audit reports in `preparation_partitions`. Record every triggered
or deferred recovery in `preparation_recovery`, with source checkpoint,
hypothesis, support-particle identities, attempt/compute charge, evidence,
disposition, and stopping condition. These records preserve provenance; they
do not replace checkpoint decisions or confer scientific acceptance.

For InitialModel, record each role-specific seed partition and structural
population branch in `initial_model_seed_partitions`, including the source
fingerprint, exact class and particle identities, selection history, and links
to the canonical preparation pools. Record each completed-attempt status and
bounded recovery response in `initial_model_recovery`. The stage-specific seed
status does not replace the checkpoint `decision`, `confidence`, or
`next_action` fields. Populate the `initial_model_acceptance` artifact binding
only after the hard acceptance interlock passes. The MCP-owned artifact must use
schema `relion.initial-model-acceptance/v1` and bind the exact accepted
reference, final Class3D particle STAR, seed particle STAR, lineage audit,
geometry, evidence artifacts, decision, and file hashes.

For every scientific attempt, record a `parameter_decision` containing:

```yaml
- id: two_d_classification_r02
  step_id: two_d_classification
  attempt: 2
  based_on_attempt: two_d_classification_r01
  objective: clean_particles_preserve_view_diversity
  baseline_source: prior_attempt
  baseline: {num_classes: 100, regularisation_parameter: 2}
  observations: {mixed_coherent_classes: true}
  evidence_refs: [Class2D/jobXXX/run_it100_model.star, visual:sha256]
  diagnosis: under_partitioned
  triggered_rule: two_d_increase_class_count
  authority: evidence_selectable
  changed_family: model_complexity
  atomic_group: model_complexity
  prerequisites: [geometry_verified, direct_class_visuals]
  candidate_overrides: {num_classes: 150}
  selected_overrides: {num_classes: 150}
  held_constant_parameters: [regularisation_parameter, mask_diameter_A]
  invalidates_from: two_d_classification
  downstream_scope: [particle_selection, topaz_training, initial_model]
  effective_parameters: {num_classes: 150, regularisation_parameter: 2}
  rendered_native_options: {nr_classes: "150", tau_fudge: "2.0"}
  expected_effect: Separate mixed views without increasing overfitting.
  success_condition: Coherent classes increase while retained view diversity remains.
  stop_condition: Tiny unstable classes increase without new coherent views.
  contract_check:
    neutral_parameter: num_classes
    allowed_source: strategy_intent
    native_option: nr_classes
    rendered: true
  approval_check: inside_envelope
```

Use `baseline_source`: `tutorial_prior`, `prior_attempt`, `dataset_geometry`,
`user_directed`, or `resource_derived`. For a first attempt with no adaptive
rule, use `diagnosis: baseline`, `triggered_rule: baseline_prior`, and
`based_on_attempt: null`. Record measured observations and exact evidence
links, not only adjectives. Update effective and native values after rendering.

At each checkpoint also record the exact source job, structured and visual
evidence, primary/fallback/rejected outputs, confidence, `decision`,
`next_action`, rationale, red flags, and reversibility.
Use one checkpoint decision with per-output dispositions when one run contains
accepted, rejected, and fallback classes; do not create contradictory decision
enums for the same checkpoint.

For a multi-output checkpoint, also record:

- `evidence_roots`, mapping each root artifact or experiment to every derived
  render, metric, and observation;
- one stage-specific disposition for every output, including evidence for,
  counterevidence, and unresolved ambiguity;
- pairwise relations separately from per-output quality; and
- the reject-all guard result, including whether any credible or ambiguous
  output blocks whole-set rejection.

Do not use one field for both scientific defects in an output and blockers to
the decision. Record output defects under its disposition, and record missing,
conflicting, or non-independent decision evidence as decision blockers.

## Parameter authority

Assign every selected value one authority class:

| Authority | Rule |
| --- | --- |
| `metadata_locked` | Copy measured acquisition/calibration facts from authoritative metadata. Never optimize them from prettier downstream output. Conflicts block execution. |
| `derived_geometry` | Derive particle diameter, box/rescale/pixel geometry, background circle, mask clearance, and compatible reference geometry from measurements. Change dependent values as one atomic group and propagate invalidation downstream. |
| `evidence_selectable` | Change thresholds, K, T, E-step limits, search ranges, iteration/VDAM limits, or Blush only when a stage rule, evidence, contract, approval range, and budget all permit it. |
| `protected_hypothesis` | Historical provenance label for controlled hypotheses. Processing authorization covers evidence-based symmetry, focused/no-alignment, local-search, and recentering experiments that serve the requested objective and preserve a matched baseline. The name does not mandate human approval. Honor fixed user constraints and scientific prerequisites. Absolute-hand changes, manual final sharpening/FSC claims, and deposition still need specific authority; reuse it when already provided. |
| `resource_derived` | Derive MPI, threads, GPUs, pooling, preread, scratch, queue, and similar feasibility settings from resources. Never count a resource change as scientific evidence. Treat padding/binning as scientific-resource hybrids when they can alter information or artifacts. |

An authority class is scientific provenance, not necessarily the literal source
token accepted by the operation catalog. Use only a catalog-allowed source when
rendering and record both the scientific authority and actual rendering source.
Measured particle/mask diameters permit `derived_geometry`; supply explicit
positive finite values for every required diameter. Do not relabel an
unsupported threshold or search-setting source as a workflow default.

## Experiment design and precedence

Apply this order before every first attempt or repeat:

1. Correct metadata, input provenance, contrast, geometry, or severe upstream
   junk before tuning a downstream classifier or refinement.
2. State one objective, such as pick recall, particle cleaning, view
   separation, junk-sink modelling, consensus cleaning, heterogeneity
   discovery, or final refinement.
3. For a repeat, diagnose one primary failure mode such as
   `under_partitioned`, `over_partitioned`, `overfit_noise`,
   `over_smoothed_clean`, `centering_or_clipping`, `reference_bias`,
   `not_converged`, or `contract_gap`.
4. Change one scientific hypothesis or one declared atomic parameter group.
   Hold unrelated scientific parameters fixed. Do not require one scalar when
   coupled values must remain consistent.
5. Define expected effect, validation, success, and stop conditions before the
   write. A smaller nominal resolution alone is never sufficient.

Examples of atomic groups include extraction `box_size_pix` plus
`rescaled_size_pix` and derived pixel size; LoG diameter minimum/maximum;
phase-search enable/minimum/maximum/step; focused mask plus alignment mode; and
a manual polishing sigma triplet. Record each component and why coupling is
required.

A first-attempt baseline may set all required metadata, derived geometry, and
stage priors together. A resource-only adjustment may accompany a scientific
attempt when needed for feasibility, but record it separately and do not treat
it as scientific evidence.

Every value must lie in the intersection of verified evidence, user-fixed
constraints, the approval envelope, the executable contract, and remaining
attempt/native-iteration/VDAM/compute budgets. Numeric values in stage
references are planning heuristics, never automatic authorization.

## Executable-contract gate

Place proposed values in `candidate_overrides`. Before promoting them to
`selected_overrides`:

1. Call `relion_describe_operation` and verify the neutral parameter exists.
2. Inspect its returned neutral contract metadata, including `allowed_sources`,
   `authority_order`, and `strategy_selectable`; `operations.yaml` remains the
   source contract. Autonomous adaptation requires `strategy_intent`; an
   explicit user value requires `user_override`; derived values require the
   appropriate metadata/derived source.
3. Verify the mapping points to the intended native option in the manifest.
4. Render with `relion_render_workflow_step` or `relion_render_job` and compare
   every requested neutral value with the effective parameter and native
   assignment.
5. Verify approval, dependency, invalidation, and budget bounds.

A rule may diagnose an unavailable candidate, but an unmapped, mis-mapped,
unauthorized, or non-rendering option must not enter `selected_overrides`, be
passed through a generic escape hatch, or be claimed as executed. Record it in
`contract_gaps`, preserve the branch, and either use a mapped upstream remedy
or continue another supported action. Ask about extending the contract only
when the requested outcome cannot otherwise be reached; an optional unavailable
operation does not pause the campaign.

For a justified Class3D no-alignment branch within the processing task, verify that neutral
`alignment: false` renders native `dont_skip_align == No`. For a focused
branch, also bind the validated mask through `reference_mask` and verify native
`fn_mask`; treat the mask and alignment mode as one atomic group. These typed
bindings make the job executable but do not relax matched-baseline,
trusted-orientation, evidence, or explicit user constraints.

Keep known limitations visible:

- `two_d_classification.mask_diameter_A` is mapped but does not currently
  authorize autonomous `strategy_intent`; treat its correction as
  `derived_geometry` from verified sample geometry or an explicit user value.
- LoG `lowpass_filter_A` currently targets native `lowpass`, whereas LoG
  maximum resolution uses native `log_maxres`; do not adapt LoG spectral
  resolution until that mapping is corrected and render-tested.
- Particle-selection `selection_minimum` and `selection_maximum` are currently
  typed as integers even though many RELION metadata thresholds are fractional;
  do not coerce or claim a decimal cutoff until the contract uses compatible
  types and passes a render test.
- A catalog field marked strategy-selectable does not waive scientific
  prerequisites. Mapped `symmetry` still requires supported evidence and a
  matched C1 baseline; the processing request authorizes a bounded comparison.

## Execution loop and repeats

For each step in the requested workflow:

1. Read the applicable stage reference completely.
2. Resolve every input from the primary or an explicitly activated fallback;
   never use the first matching filename blindly.
   Before a Class3D write, also validate the live inputs against the current
   `initial_model_acceptance` record. Any missing record, hash mismatch, changed
   mask diameter or geometry, changed particle population, stale lineage audit, or
   required `unknown`/`unassessable` evidence invalidates the checkpoint and
   blocks Class3D until InitialModel is reassessed. When the reference is a
   refined consensus map from outside RELION, such as a CryoSPARC refinement,
   use `relion_record_external_reference` (hash-bound map and particles,
   provenance, pose lineage) instead of an InitialModel record; a no-alignment
   branch needs `poses_from_reference_refinement`. Publish the record only
   through `relion_record_initial_model_acceptance`, mirror its returned path
   and hash in run state, and pass that project-relative path as
   `initial_model_acceptance` to every Class3D add/run wrapper; never edit the
   MCP-owned artifact directly.
3. Create the parameter-decision record with `candidate_overrides` and apply
   the executable-contract gate.
4. Assign a unique alias, render explicit inputs and candidate parameters, then
   promote only verified values to `selected_overrides` and record their
   effective/native assignments.
5. Confirm the write lies inside the approval boundary.
6. Register and launch pipeliner jobs with one combined `relion_run_*` call.
   For an `execution_ready` direct-command step, use its dedicated typed write
   tool; the bundled `rescale_map` step must use `relion_rescale_map`, not
   `relion_run_workflow_step`. Use add-only or start-only calls only for
   recovery or deliberately staged advanced work.
7. Monitor status, inspect ambiguous or failing logs, and enumerate outputs.
8. Generate the checkpoint's structured and visual evidence.
9. Apply its acceptance/repeat rules and record confidence, `decision`, and
   `next_action`.
10. Advance, activate one fallback, repeat, retry, await a human, or stop.

Continue without conversational re-approval while actions remain inside the
boundary. A repeat requires an understood prior attempt, written hypothesis,
allowed atomic change, unique alias, preserved prior output, exact input branch,
remaining budget, and predefined validation/stop conditions. Never repeat a
successful job unchanged. A newly accepted particle set creates a new input
data state and resets its stage-attempt budget.

An autonomous scientific repeat requires a recorded hypothesis, useful
discriminating evidence, and remaining budget. A high-confidence rejection may
use `decision: auto_reject` with `next_action: repeat`; a medium-confidence
credible candidate may use `primary_fallback` for continued processing. Incomplete
evidence first calls for read-only diagnosis or a justified experiment, not an
automatic user question. Never relax metadata, geometry, half-set, or executable
contract requirements to launch an experiment.

Allow up to two unchanged `retry` attempts for diagnosed transient scheduler,
node, or temporary-filesystem failures, within the task's limits. Persistent
failures trigger a resource correction or fallback, not a request to retry the
same failing command indefinitely.

## Human gates, failures, and completion

Before asking, exhaust relevant available diagnostics, supported bounded
validation, and ranked fallbacks. If a question is still necessary, use
`human_gate` with `next_action: await_human` and provide exact source files and
renders, the preferred option and confidence, evidence for and against each
candidate, the missing high-confidence criterion, the reversible default, and
the accepted response format. Preserve the affected candidates inactive and
continue independent authorized work. Optional unresolvable experiments may
stop with their reason recorded while the accepted primary continues.

Classify failures before acting:

- `transient_infrastructure`: allow up to two unchanged retries within limits;
- `resource_failure`: change only resource parameters;
- `input_failure`: repair provenance;
- `parameter_failure`: correct the invalid option;
- `scientific_failure`: apply the stage repeat or fallback rules;
- `visual_access_failure`: restore visual evidence or gate;
- `contract_gap`: do not execute the unavailable option;
- `unknown`: preserve logs, diagnose, and stop only the affected branch if
  unresolved; continue from another usable branch when available.

Never continue from a failed, aborted, missing-output, or unassessed job.

Use aliases such as `<step_id>_r01`, `<step_id>_r02_<reason>`,
`<step_id>_r02_fallback_<label>`, `<step_id>_mask_t02_e03_s08`,
`<step_id>_sym_D2`, or `<step_id>_hand_inverted`. Never reuse an alias or
overwrite output.

The user's processing request supplies authorization for routine additive work
within its scope. Record that boundary in `approval_envelope`; the historical
field name does not require an extra approval exchange. Set `confirm=true` and
`approval_flag=true` for covered actions. Resume with the same authorization
unless revoked or superseded, after revalidating live state and permissions.
Request new authorization only for an out-of-boundary or separately protected
action not already authorized. Do not enlarge explicit budgets automatically.

A stage is complete only when its job succeeded, expected outputs exist,
required evidence was inspected, the parameter and scientific decisions were
recorded, and any required next step can be rendered with explicit inputs.
A terminal stage needs no additional job. At the requested endpoint, finish
with the best assessed map and other outputs even when their scientific status
is provisional. Report uncertainties and limitations without asking for final
acceptance or launching extra jobs solely to obtain a high-confidence label.
This completes processing without claiming scientific validation that the
evidence does not establish. Report stage
status; accepted, rejected, primary, and fallback outputs; decisions and
evidence; recovery paths; contract gaps; unresolved questions; current best
particles/maps/half-maps/masks/postprocessing/model artifacts; and the exact
next safe action. Never claim biological correctness because a job completed,
a model fit, or one metric improved.
