# RELION SPA project instructions

## Scope and authority

For any request to plan, run, resume, diagnose, compare, visually assess,
select, or iterate RELION SPA processing, use the
[`relion-spa-orchestrator`](.agents/skills/relion-spa-orchestrator/SKILL.md)
skill.

When a completed Refine3D reconstruction has unsatisfactory resolution, FSC
behavior, or map quality and the task is to improve it, follow the
orchestrator's routed
[resolution-optimization reference](.agents/skills/relion-spa-orchestrator/references/resolution-optimization.md).
The SPA orchestrator remains the single authority for the complete processing
and optimization campaign.

Treat the bundled
[`relion_5_spa_tutorial_v1.yaml`](src/relion_mcp/configs/relion_5_spa_tutorial_v1.yaml)
as the stage-order and dependency baseline. Treat
[`operations.yaml`](src/relion_mcp/configs/operations.yaml),
[`relion_pipeliner_mappings.yaml`](src/relion_mcp/configs/relion_pipeliner_mappings.yaml),
and
[`relion_operation_manifest.yaml`](src/relion_mcp/configs/relion_operation_manifest.yaml)
as the operation and native-option contracts. Do not edit these contracts while
processing data. Record dataset-specific values and branches in
`RELION_RUN_STATE.yaml`.

The [tool API](docs/API.md) and [configuration reference](docs/CONFIGURATION.md)
are canonical for tool arguments, defaults, and server settings.

## Autonomous processing default

A request to process, run, continue, or optimize means carry the requested
workflow through to its outcome. Scientific checkpoints are internal decisions,
not approval stops. After each job, assess the outputs, update state, and launch
the next ready step without waiting for a user message. While a job runs, keep
monitoring at sensible intervals and report progress; do not end the task merely
because a job was submitted or a stage completed. Never ask "shall I continue?"
for work already covered by the request.

Choose routine classes, thresholds, masks, and parameter/resource settings from
the available evidence. A credible medium-confidence candidate can feed normal
downstream processing, with its uncertainty recorded and alternatives retained.
Use downstream results to resolve the uncertainty; a separate validation job
or a new perturbation is not mandatory at every checkpoint. A processing choice
does not establish biological identity, absolute hand, or a final quality claim.
The InitialModel-to-Class3D boundary is the exception: never register, start,
or launch Class3D from a medium-confidence seed or an availability-only MRC.
Class3D requires the high-confidence `initial_model_accepted` checkpoint defined
by the InitialModel/Class3D reference, bound to the exact reference, final
Class3D particle state, audited seed-particle lineage, and geometry.

Use `defer` for an unresolved scientific decision while diagnosing, retrying,
or ending an unproductive branch. A branch stop does not stop the campaign:
activate a usable fallback or continue other ready work. At diminishing returns
or a hard budget limit, finish with the best available result and its limitations
instead of asking to extend the run. Ask only when an essential fact cannot be
recovered, no valid continuation or useful deliverable exists, or the next
necessary action exceeds an explicit user constraint or needs destructive or
external publication authority. Bundle any genuinely blocking questions.

## Execution invariants

- Start processing sessions with `relion_environment` and
  `relion_validate_workflow`.
- Verify acquisition metadata, pixel/box geometry, particle diameter,
  symmetry assumptions, external executables, GPU layout, and queue settings;
  tutorial values are not dataset defaults.
- Render every job with explicit inputs and overrides before running it.
- Use MCP tools for covered RELION operations. Do not invoke covered RELION
  executables through a shell.
- For CryoSPARC particles destined for Class3D, call
  `relion_render_cryosparc_particle_import` first. If it reports
  `bootstrap_required=true`, initialize the proposed RELION project with its
  returned `relion_initialize_project` arguments and render again before
  calling `relion_import_cryosparc_particles`. A pre-existing RELION project is
  not required, but conversion must never create one implicitly or invoke pyem
  through a shell.
- Treat the live-inventoried or explicitly configured CryoSPARC source project
  and all CS/MRC inputs as read-only. On the combined route, use
  `mcp__cryosparc__inspect_project_job` and select only its
  `relion_handoff.source_project_dir` and `relion_handoff.allowed_cs_files`;
  RELION independently requires its canonical directory to exactly match
  `project_dir` in the API-derived `{uid, project_dir}` inventory, with matching
  pinned directory device/inode values, captured from
  the authenticated CryoSPARC environment at MCP startup. Each inventory
  `project_dir` is resolved through authoritative `Project.dir()`. A standalone
  RELION server requires `RELION_CRYOSPARC_IMPORT_ROOTS` only when it uses this
  bridge.
  Preserve the source, create one new no-clobber `External/CryoSPARC/` bundle,
  then register its STAR through the returned `import_particles` handoff before
  Class3D.
- Never edit a RELION pipeline, optimiser, model, data STAR, map, particle
  file, or job directory in place. Never rename, move, delete, or overwrite a
  RELION job directory.
- Treat `relion_suggest_step_inputs` results as provenance hints, not
  scientific selections.
- Use combined `relion_run_*` tools for normal registration and launch. Use
  add-only/start-only tools only for recovery or deliberately staged advanced
  work.
- Assign a unique alias to every scientific attempt and preserve every
  completed branch.
- Treat `initial_model_accepted` as a hard Class3D launch interlock. Before any
  Class3D add, start, or run write, verify a recorded
  `relion.initial-model-acceptance/v1` record whose `assessment` has
  `seed_status: SEED_READY`, `reference_utility: credible`,
  `decision: auto_accept`, `confidence: high`, `next_action: advance_primary`,
  `approval_flag: true`, direct visual access, stable late iterations, no
  clipping or major artifact, at least two typed independent roots
  (`map_pixels`, `particle_class_metadata`, or `late_iteration_convergence`),
  and no critical gaps. Its hashes must match the
  actual reference volume and full final 2D-selected Class3D particle STAR. The
  InitialModel particles must be that final population or an audited
  duplicate-free subset of it. Missing, stale, `unknown`, or `unassessable`
  required evidence fails closed; an InitialModel filename or successful job
  is inventory, not launch authorization. Create the immutable record only with
  `relion_record_initial_model_acceptance`, never by editing `.relion-mcp`, and
  pass the returned project-relative artifact path as
  `initial_model_acceptance` to every Class3D add/run wrapper.
- A refined consensus map from outside RELION (for example a CryoSPARC
  refinement) is not an InitialModel seed. Bind it with
  `relion_record_external_reference` instead, pass that returned path the same
  way, and use `pose_lineage: poses_from_reference_refinement` for a
  no-alignment Class3D. Never route an InitialModel output through it.
- Never invoke native `relion_pipeliner`, the scheduler helper, or another
  shell path to add or launch Class3D. Class3D execution must go through the
  guarded MCP add/run/start tools so the interlock cannot be bypassed.
- For an accepted exact class set, or a credible provisional set entering
  ordinary downstream processing, call `relion_plan_particle_class_selection`
  and inspect the resolved `materialization_mode`, expected count, and options,
  then launch it with `relion_run_particle_class_selection`. Prefer the
  pipeline-registered one-job `gui_equivalent` mode; use the native
  Select/JoinStar graph only when the fixed selector is unavailable or the
  caller explicitly requests `select_joinstar`. Record provisional selections
  as such; materializing a selection does not put it in `accepted_outputs`.
- Preserve classes, maps, masks, hands, symmetry branches, rejected outputs,
  and inactive fallbacks until an accepted downstream result is recorded.
- Do not execute ModelAngelo or DynaMight unless workflow validation reports
  their mappings executable.
- Manual picking is opt-in. Do not render, run, or request approval for
  `manual_particle_picking` unless the user explicitly requests it. Missing
  manual picks are not a scientific gate; the default branch proceeds through
  micrograph subset selection and LoG autopicking.

## Visual evidence

Never claim to have inspected an MRC, MRCS, PDF, plot, class average, pick
overlay, or map unless the active model received viewable image content.

Run `relion_visual_smoke_test` and
`relion_verify_visual_smoke_test` before the first scientific visual decision
after an MCP process, SDK, Codex client, or model change. A session with no
scientific visual decision does not need the test. Record `visual_access:
direct` only when verification returns
`passed=true`; otherwise record `unavailable`, attempt to restore image access,
and continue work that does not depend on morphology. Ask for help only when
the missing visual evidence prevents the next useful action.

Inspect the PNG itself, not only trailing metadata. Record the tool, source
paths, rendering parameters, and returned image SHA-256. Metrics support visual
evidence but do not replace it. Use focused follow-up renders instead of
repeatedly attaching a full contact sheet.

## Decisions and next actions

Record one scientific `decision` at each checkpoint:

- `auto_accept`
- `auto_reject`
- `primary_fallback`
- `defer`
- `human_gate`

Record the operational consequence separately as `next_action`, using one of
`advance_primary`, `activate_fallback`, `repeat`, `retry`, `await_human`, or
`stop`.

Only `high` confidence permits final `auto_accept` or `auto_reject`. High
confidence requires complete directly inspected evidence, agreement of at least
two independent evidence channels, no unresolved red flag, and stability under
a reasonable perturbation when needed to substantiate the conclusion. At
`medium` confidence, use `primary_fallback` to advance one credible provisional
branch through downstream stages while preserving alternatives. Reassess each
stage; do not reject classes or claim final acceptance. Complete, compatible
existing evidence may be reused; do not run extra jobs solely to raise a
confidence label before an otherwise valid processing step. This permission
does not cross the InitialModel-to-Class3D interlock: Class3D requires the
recorded high-confidence `initial_model_accepted` checkpoint.
Autonomous actions require reversible provenance and task authorization.
At `low` confidence, gather evidence or repair the problem before choosing a
branch. Record unresolved science as `defer` with `retry`, `repeat`, or `stop`;
reserve `human_gate` with `await_human` for an actual blocking user question.
Legacy unresolved `human_gate` records do not by themselves request input. Confidence
tiers are not calibrated probabilities.

Advance only the primary from a `primary_fallback` decision. Keep alternatives
inactive and ranked with explicit activation triggers. Activate only the next
ranked fallback when its trigger is met or the user redirects the workflow.
The particle-preparation reference permits one bounded recovery diagnostic
before Topaz or 3D for material attrition, coverage loss, or uncertain mixed
classes, while preserving the primary. Audit exact recoverable and any retained
alignment-support identities, honor finite budgets, and resume one assessed
primary afterward. This exception does not authorize routine parallel
continuations or automatic recycling of proven junk. Ranker scores guide
triage; they do not impose a score band for recovering credible particles.
Keep precise diverse Topaz labels separate from inclusive reconstruction
candidates, and do not require another 2D round when no residual issue supports
it. Follow the routed reference for deterministic training/heldout splits and
complete class-image coverage.

## Approval boundary

Scientific uncertainty and write authorization are different. A request to
process, run, continue, or optimize a dataset authorizes the routine additive
jobs, project initialization, state updates, selections, retries, and bounded
validation needed for that task. Record this scope in `approval_envelope`,
including the originating request, input branch, parameter families, resource
and attempt limits, and stop conditions; it is not a mandatory approval form.
Use existing user limits, and record a finite overall campaign budget when none
was specified. Stage allocations chosen by the agent may be rebalanced within
that budget without approval; they are not user-imposed limits. Stop or finish
with the best result at the overall limit; never silently enlarge a user cap.
Do not ask the user to approve a plan or each processing segment
again. Within scope, set `confirm=true` and `approval_flag=true` yourself.
Planning-only requests and server plan mode remain read-only. Read-only
rendering, monitoring, log inspection, output discovery, and evidence assessment
do not require approval. On resume, preserve authorization unless revoked or
superseded, and recheck the live environment and state.

Request new authorization only for an out-of-scope action or a separately
protected action such as destructive cleanup, overwrite, abort, changing the
absolute hand, or deposition, if it was not already authorized. Evidence-based
symmetry comparisons and focused/no-alignment experiments are ordinary additive
processing when they serve the requested objective, preserve a matched baseline,
use a supported target, and respect user constraints. Their scientific checks
remain required; a fresh human authorization is not. Keep C1 when symmetry is
unresolved and defer unsupported biological claims. First resolve uncertainty with available diagnostics,
bounded validation, or an existing fallback. Ask a focused question only when
essential evidence remains unavailable/conflicting, a consequential biological
choice cannot be resolved, or progress requires changing the user's scope or
hard limits. Skip optional blocked branches and continue the authorized primary.

## State and completion

Maintain `RELION_RUN_STATE.yaml` using the schema in the orchestrator skill.
Record metadata, job bindings, overrides, visual evidence, decisions,
next actions, iterations, accepted outputs, rejected outputs, and fallback
status. Record the current `initial_model_acceptance` artifact path and
`record_sha256` separately; bind it to the accepted reference and
Class3D-particle fingerprints and invalidate it whenever either input, mask
diameter, geometry, or particle lineage changes.

A stage is complete only when the job succeeded, expected outputs exist,
required evidence was inspected, the decision/confidence/next action were
recorded, and any required next step can be rendered with explicit inputs. A
terminal stage does not require another job. At the requested endpoint, return
the best assessed outputs even if scientific acceptance remains provisional;
state the uncertainty and limitations without asking for an acceptance decision
or running extra jobs solely to make the confidence label high. Job
completion or one improved metric alone is not scientific or biological
success.

Update `RELION_RUN_STATE.yaml` for a job before registering the next job, not
in a batch at session end or after a polling loop. A long-running background
job (e.g. full-set Topaz picking) is not an exception: record its launch
immediately, then update its status and evidence the same time you next poll,
inspect, or act on it. The file must never fall behind actual job registration
by more than one job. Do not bind a job's output as the next job's input while
that producing job's own entry is still missing evidence, decision, or
next_action — an unrecorded stage cannot be evidence-gated by the next one.
