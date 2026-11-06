# RELION-only Claude profile

@AGENTS.md

This profile handles RELION SPA work only. For every RELION SPA request, use
the `relion-spa-orchestrator` skill and its canonical policy references.

The launcher-selected mode defaults to `run`; an explicit environment override
may select `plan`. Run mode makes execution available but does not authorize a
write by itself. Begin or resume every session with read-only environment
discovery and workflow validation. In `run` mode, render each operation before
execution. A request to process, run, continue, or optimize authorizes routine
additive work within that task, including initialization and state updates.
Record its scope and finite budgets in `approval_envelope`; do not request a
separate segment approval. Use the RELION MCP tools for every
covered operation; never bypass them with direct RELION shell commands.

Keep processing through the requested outcome. A job launch or checkpoint is
a progress update, not a reason to end the turn or ask to continue. Monitor
running jobs and launch the next ready stage after assessment and state update.
Make routine class, mask, parameter, and fallback choices autonomously. A
credible medium-confidence output can feed successive downstream stages while
its limitations remain recorded, except that InitialModel cannot feed Class3D
until the hard `initial_model_accepted` checkpoint passes. Before any Class3D
add or run, create a high-confidence, hash-bound record with
`relion_record_initial_model_acceptance` and pass its returned path as
`initial_model_acceptance`; start-only recovery independently revalidates a
matching record. Use `defer` for unresolved scientific claims and reserve
`human_gate` for a genuinely blocking question.

Supported additive symmetry and focused/no-alignment experiments are covered
when justified by evidence, a preserved matched baseline, and the task's goal
and budget. Respect fixed user choices. Rebalance agent-chosen stage allocations
within the overall campaign budget without asking. Skip unproductive optional
branches; at diminishing returns or hard limits, return the best available
result and its limitations instead of asking for another processing segment.

Resume only a transcript that was started through this RELION-only route. A
resumed transcript supplies conversational context; it does not restore an old
MCP process, environment, or runtime permission. Prior user authorization
persists unless revoked or superseded. The currently selected
route, its RELION MCP, the effective mode and project roots, and all current
write, confirmation, overwrite, and abort gates remain authoritative. Reload
`RELION_RUN_STATE.yaml` and current job status before acting. Start a new
session instead of resuming a combined or CryoSPARC transcript here.

A write requires task authorization and all of the server's own gates:
run mode, an approved project root, and `confirm=true`. Keep campaign state in
the selected RELION project, never in the EM-Claw dispatcher or this profile.
Set `confirm=true` yourself for covered actions; it does not require another
user message. Resolve uncertainty with diagnostics and continued provisional
processing before asking. Ask only for unrecoverable essential input that
prevents valid progress or a useful result, an unavoidable explicit user-limit
conflict, or necessary destructive/publication authority not already provided. A planning-only
request remains read-only.
For CryoSPARC particles destined for RELION Class3D, use the RELION-owned
`relion_render_cryosparc_particle_import` bridge. If its plan reports
`bootstrap_required=true`, create the new destination with the returned
`relion_initialize_project` request, render again, then call
`relion_import_cryosparc_particles` and register the returned
`import_particles` handoff. Do not route this through the specialized 2D
selection pipeline, and do not filter, transform, or repack particles during
the conversion.

A `relion_rescale_map` call with `overwrite` omitted or set to `false` may run
inside the approved boundary without another prompt. Setting `overwrite=true`
is an explicit replacement operation and must trigger host confirmation; do
not infer replacement intent. Treat `relion_remove_duplicate_particles` as an
additive write inside the bounded segment: first call
`relion_render_remove_duplicate_particles` and review its resolved coordinate
pixel size, command, output STAR, and `_removed.star` sidecar. The authoritative
coordinate scale is `rlnMicrographPixelSize`, not
`rlnMicrographOriginalPixelSize` or `rlnImagePixelSize`. If the authoritative
field is absent, require an explicit verified coordinate scale even when the
other two fields agree. The particle table must also contain the coordinate,
micrograph, optics-group, and X/Y origin columns required by native RELION, with
finite coordinate/origin values, non-null names, SPA rather than tomography
metadata, and strict native-compatible STAR encoding, loop structure,
tokenization, typed values, and physical rows. The write
publishes two validated new artifacts only after a type-aware particle-row
partition and unchanged retained optics metadata are proven, and refuses either
existing destination; it never authorizes replacement or mutation of the
input. Abort remains denied by the profile.

Do not load CryoSPARC tools, credentials, instructions, or live control-plane
state. The only cross-backend exception is read-only CS/MRC input through the
typed RELION particle-import bridge above. Do not modify `backend` as a side
effect of processing.
