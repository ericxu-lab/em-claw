# RELION MCP

`relion-mcp` is a local MCP server for planning, running, monitoring, and
visually assessing RELION 5 single-particle analysis (SPA) workflows from
Codex. It translates neutral operation parameters into native
`relion_pipeliner --addJobOptions` assignments without invoking a shell.

Project access is restricted to roots configured with
`RELION_PROJECT_ROOTS`. The server starts in read-only plan mode; writes require
run mode, `confirm=true`, and a project under an approved root.

## Documentation

- [Tool API](docs/API.md): tool names, arguments, defaults, return values, and
  write annotations.
- [Configuration](docs/CONFIGURATION.md): environment variables, executable
  resolution, Codex registration, and queue/site concerns.
- [Development](docs/DEVELOPMENT.md): repository layout, tests, contract
  validation, and release checks.
- [Visual tools](docs/VISUAL_TOOLS.md): image transport and renderer design.
- [Agent invariants](AGENTS.md): non-negotiable safety and authorization rules.
- [RELION SPA orchestrator
  skill](.agents/skills/relion-spa-orchestrator/SKILL.md): scientific procedure,
  checkpoint criteria, iteration policy, run-state schema, routed
  [InitialModel recovery
  procedure](.agents/skills/relion-spa-orchestrator/references/initial-model-class3d.md),
  and routed
  [resolution-optimization
  procedure](.agents/skills/relion-spa-orchestrator/references/resolution-optimization.md)
  for unsatisfactory Refine3D results.

## Bundled workflow contracts

The package includes version-controlled copies of:

- [`operations.yaml`](src/relion_mcp/configs/operations.yaml)
- [`relion_operation_manifest.yaml`](src/relion_mcp/configs/relion_operation_manifest.yaml)
- [`relion_pipeliner_mappings.yaml`](src/relion_mcp/configs/relion_pipeliner_mappings.yaml)
- [`relion_5_spa_tutorial_v1.yaml`](src/relion_mcp/configs/relion_5_spa_tutorial_v1.yaml)

The tutorial contract defines stage order and dependencies. Dataset-specific
values live only under `tutorial_example_params`; they are documentation and
are never merged into executable job parameters. Guarded steps report
`missing_explicit_param_overrides` until metadata-locked or derived values are
supplied, and workflow render/run/add calls refuse those unresolved steps.
In particular, the final post-Class3D re-extraction requires an explicitly
derived even box and native sampling (`rescale_particles: false`).

LoG size limits, Topaz particle diameter, and 2D/3D particle masks now require
explicit, measured or derived values; generic job rendering enforces the same
requirement. Existing callers that relied on tutorial sizes must supply these
parameters. Initial-model generation and refinement default to C1; supported,
explicitly justified symmetry remains available.

Particle preparation preserves separate retained, recoverable, and proven-junk
pools. The orchestrator activates a bounded recovery classification when
material loss or view diversity warrants it, and curates precise Topaz training
labels separately from broader reconstruction candidates. Stratified
micrograph planning/materialization tools create reproducible training and
held-out populations, optionally partitioning existing labels with them.
Selection audits expose identity, half-set, and acquisition-stratum losses;
particle-box views and complete 2D class pages provide the corresponding pixel
evidence. These diagnostics inform the skill's decisions rather than making
automatic scientific acceptance claims.

Current execution coverage is intentionally bounded:

- ordinary mapped jobs use native RELION pipeline registration and schedule
  launch;
- `rescale_selected_3d_reference_map` uses `relion_image_handler` through
  `relion_rescale_map`;
- `align_symmetry_reference` uses native `relion_align_symmetry` through the
  read-only `relion_render_symmetry_alignment` and additive
  `relion_align_symmetry_reference` tools to create a new unsymmetrized
  reference aligned to a caller-selected target symmetry without registering
  or launching a pipeline job;
- CryoSPARC particle exports use the read-only
  `relion_render_cryosparc_particle_import` and additive
  `relion_import_cryosparc_particles` pair to create a validated, copy-only
  RELION Class3D input bundle without modifying the source or registering a
  pipeline job;
- `modelangelo_atomic_modelling` is planning-only (`dry_run_mappable`);
- `dynamight_motion_exploration` is planning-only (`manifest_only`); and
- `manual_particle_picking` is excluded from the default plan and is included
  only after explicit user opt-in; and
- `local_resolution` is excluded from the default plan, then included once for
  the selected best/final map or earlier only after an explicit user request.

`relion_validate_workflow` reports these support states before execution.

## Execution model

Use read-only render tools before every write. For normal processing, use the
combined `relion_run_job`, `relion_run_workflow_step`, or
`relion_run_particle_class_selection` tools: each registers and launches its
job or bounded graph as one write intent. Add-only and start-only tools exist
for recovery and deliberately staged advanced use.

Class3D is protected by a hard InitialModel acceptance interlock. After direct
inspection of the map, use `relion_compare_map_projections` to compare synthetic
projections with at least three explicitly selected credible, distinct 2D class
appearances at one physical low-pass. Central-slab panels are not projection
matches, and NCC is diagnostic rather than an automatic verdict. Publish each
exact inspected acceptance image with `relion_publish_initial_model_evidence`
and use its returned project-relative path. Then call
`relion_record_initial_model_acceptance` with the final
InitialModel map/model/optimiser/data artifacts, the final Class3D particle
population, three distinct evidence artifacts, high-confidence assessment,
physical geometry, and audited particle lineage. It creates an immutable,
content-addressed `relion.initial-model-acceptance/v1` record under
`.relion-mcp/initial-model-acceptances/`; use
`relion_validate_initial_model_acceptance` to check it. Pass the returned
project-relative path as `initial_model_acceptance` to every Class3D
`relion_run_job`, `relion_run_workflow_step`, `relion_add_job`, or
`relion_add_workflow_step` call. Although the generic signatures make this
argument optional for other operations, Class3D registration requires it.
`relion_start_jobs` independently revalidates a matching live record against
the registered Class3D reference, final particles, particle diameter, and all
bound hashes before scheduler launch. Missing, stale, incomplete, changed, or
mismatched evidence fails closed.

The record binds the submitted evidence and provenance and rejects malformed
MRC/PNG/particle-STAR inputs, geometry/header mismatches, duplicate evidence
content, and missing independent evidence roots. It does not interpret the
scientific meaning of the pixels, prove subset-lineage claims, or infer
biological truth. The exact assessment, geometry, evidence, lineage, and
source-artifact contract is documented in the
[API reference](docs/API.md#initialmodel-to-class3d-acceptance-interlock).

Exact class selection defaults to one pipeline-registered RELION External job
running the fixed package selector. It reproduces the GUI's selected particle
STAR semantics in source order, writes backup/provenance/output-node metadata,
and can emit selected 2D class averages when an optimiser or model STAR is
provided. If the selector is unavailable, `auto` uses the existing native
Select/JoinStar graph; callers may also request either mode explicitly.

CryoSPARC particle import accepts a primary particles CS file plus optional
ordered passthrough CS files beneath an authorized source project. On the
combined EM-Claw route, the RELION MCP snapshots API-derived
`{uid, project_dir}` identities plus directory device/inode values from the
authenticated active CryoSPARC
environment at startup, resolves each canonical `project_dir` through
authoritative `Project.dir()`, and accepts only a source whose canonical
directory exactly matches one of those values. A standalone RELION server
instead uses a configured source root as the authorization boundary when this
bridge is used. In both cases the renderer also validates particle-data UID
joins, Class3D CTF metadata, particle indices, box and pixel geometry, zero-based
CryoSPARC to one-based RELION optics-group mapping, referenced MRC stacks, pyem
readiness, and the no-clobber destination. A
pre-existing RELION project is not required before planning: if
the proposed project is absent or uninitialized, the renderer returns
`bootstrap_required=true` and exact arguments for `relion_initialize_project`.
Initialize explicitly, render again, and then materialize the additive
`External/CryoSPARC/<export_name>/` bundle. Register its `particles.star`
separately with the returned `import_particles` handoff before Class3D. The
converter never performs 2D selection, particle filtering, coordinate/pose
transforms, stack repacking, or implicit project creation.

Visual tools return a PNG as the first MCP content block and compact
provenance text second; multipage 2D rendering repeats this image/text pair for
each sheet. A successful call is not proof that the active model
saw the pixels; the orchestrator skill defines the required randomized smoke
test and evidence policy.

`relion_compare_maps` provides the generic quantitative route for related
Class3D or refined maps. It estimates one bounded local candidate-to-reference
transform at a declared physical low-pass, freezes that transform across all
requested comparison cutoffs, and returns pre/post correlations, overlap,
support-masked difference views, centered-grid transform provenance, canonical
pixel-root identity, and multistart plus spatial-jackknife registration status.
Only stable/generalizing `aligned` or exact `identity_optimal` registration can
support a later relation assessment. The tool is read-only and does not itself
classify maps as biologically same or distinct.
Different strongly low-passed starting references are recorded as a causal
confound rather than used to prohibit descriptive final-map comparison.

`relion_scan_symmetry` performs a bounded, read-only two-stage search on
independent C1 half-maps: a common-order scan followed, when indicated by a
high-order sentinel, by a narrow higher-order follow-up. It returns an
exploratory shortlist only; it neither changes the maps nor accepts or imposes
symmetry. Use the sequence C1 InitialModel, C1 Refine3D, candidate
identification, matched symmetry-constrained Refine3D/PostProcess, then a
C1-referenced map comparison with `relion_compare_symmetry` and targeted local
inspection.

`relion_render_symmetry_alignment` and `relion_align_symmetry_reference` wrap
RELION's independent native point-group alignment search. Both tools require
an explicit `target_symmetry`: accepted values are C2-C64, D2-D32, T, O, and
I1-I4. C1, bare I, and custom symmetry-definition files are rejected. C4 is
therefore only one accepted call-site choice, not a property fixed by the
operation. The bounded search disables center-of-mass recentering, rotates
about the array center, and writes a new full-bandwidth aligned reference
beneath `ReferencePrep/` without applying or averaging the target symmetry,
changing particle metadata, or creating a RELION pipeline job. Native
execution resolves the selected built-in group inside private staging. A
byte-verified private input snapshot, private staging directory, and
no-replace publication through a verified destination-directory handle
protect the source and existing project files. Input
size is capped by `RELION_SYMMETRY_ALIGNMENT_MAX_BOX_PX` (512 by default),
staging free space is checked before execution, and the search cutoff must
retain at least three Fourier-radius scoring shells. The
native randomized search does not consume the scanner's reported axis or
orientation matrix, and its output is preparation for a task-authorized
matched C1/target-symmetry branch rather than acceptance or imposition of the
target symmetry.

For ordinary 3D checkpoints, use the rendered evidence to build a structured
assessment and pass it through `relion_validate_3d_assessment` before acting on
an autonomous decision. The validator enforces reject-all, evidence-root,
alignment, confidence, and approval guards; it does not inspect the maps or
infer biological meaning. At the InitialModel-to-Class3D boundary, this general
assessment is not launch authorization: the separate hash-bound acceptance
record above is mandatory.

## Requirements

- Python 3.11+
- `uv`
- RELION 5 on the MCP host
- for CryoSPARC particle import only, an external pyem installation with an
  explicitly configured Python interpreter and `csparc2star.py`
- any selected external programs, such as CTFFIND or Topaz, configured for
  RELION
- valid CryoSPARC project storage visible to the MCP process, authorized by
  exact membership in the combined route's startup live-project inventory or,
  for standalone use of this bridge, an explicit source root; and RELION
  project storage visible to both the MCP and compute jobs
- an image-capable Codex client/model for autonomous visual assessment

## Install and verify

```bash
cd /path/to/em-claw/profiles/relion/backend
uv sync --extra test
uv run pytest
```

For MCP Inspector:

```bash
uv run mcp dev src/relion_mcp/server.py
```

Use the sole copy-paste MCP client configuration in
[`examples/mcp-config.toml`](examples/mcp-config.toml), replace its two
absolute paths, and restart Codex. Confirm registration with `/mcp` or
`codex mcp list`.

## First plan

Keep `RELION_MCP_MODE=plan`, then ask Codex to call:

1. `relion_environment()` to verify executables, approved roots, mode, and any
   configured CryoSPARC particle-import capability;
2. `relion_validate_workflow()` to inspect support limits and resolve every
   `missing_explicit_param_overrides` entry until the next step reports
   `execution_ready: true`; and
3. the visual smoke-test pair before the first scientific visual decision
   after an MCP process, SDK, client, or model change.

Render the first job with explicit dataset inputs and overrides. To execute,
switch to run mode and request processing with any scientific/resource limits.
That request authorizes routine additive work within scope; the agent records
finite bounds and proceeds without a separate segment approval, as described
by the [orchestrator skill](.agents/skills/relion-spa-orchestrator/SKILL.md).
The agent monitors and advances through the requested workflow without pausing
at job boundaries. Credible provisional outputs may feed normal downstream
processing while uncertainty and alternatives remain recorded. Supported
reversible experiments use the same task authorization and a matched baseline;
final scientific acceptance/rejection still requires high confidence. At a hard
limit or diminishing returns, return the best available result with limitations.
