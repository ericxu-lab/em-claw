# MCP API reference

This is the canonical public API reference for the MCP surface defined in
[`server.py`](../src/relion_mcp/server.py). The bundled operation and workflow
contracts remain the authority for operation-specific inputs and parameters;
use `relion_list_operations`, `relion_describe_operation`, and
`relion_validate_workflow` to inspect them at runtime.

The current server exposes 49 tools and two resources. Tool argument names,
ordering, types, and defaults below match the Python signatures. A
`pipeline_name` value of `None` means “use the configured
`RELION_PIPELINE_NAME`,” whose environment default is `default`; it does not
mean that the pipeline has no name.

## Annotation profiles

MCP annotations are hints to the client. The server separately enforces run
mode, root confinement, and `confirm=true` for every write.
The agent supplies `confirm=true` for actions covered by the user's processing
request; it is not a requirement for a new conversational approval per call.

| Profile | `readOnlyHint` | `destructiveHint` | `idempotentHint` | `openWorldHint` |
|---|---:|---:|---:|---:|
| `READ_ONLY` | `true` | unspecified | unspecified | `false` |
| `WRITE_ADDITIVE` | `false` | `false` | `false` | `false` |
| `WRITE_IDEMPOTENT` | `false` | `false` | `true` | `false` |
| `WRITE_DESTRUCTIVE` | `false` | `true` | `false` | `false` |

For Codex, keep the host approval mode at `writes` as shown in the
[configuration example](../examples/mcp-config.toml). See
[`CONFIGURATION.md`](CONFIGURATION.md) for the distinction between host approval
and the server's own execution gates.

## Resources

### `relion://workflow/relion_5_spa_tutorial_v1`

Handler: `tutorial_resource() -> str`

Returns the bundled tutorial YAML as text.

### `relion://operation/{operation}`

Handler: `operation_resource(operation: str) -> str`

Resolves aliases and returns an indented JSON string containing the same
operation description exposed by `relion_describe_operation`.

## Operation aliases and support overlays

The mapping contract accepts these compatibility names:

| Requested name | Resolved operation |
|---|---|
| `beam_induced_motion_correction` | `motion_correction` |
| `autopicking` | `reference_autopicking` |
| `particle_picking` | `log_autopicking` |
| `particle_extraction` | `extract_particles` |
| `postprocessing` | `postprocess` |
| `bayesian_polishing` | `polish` |

Responses distinguish `requested_operation` from the resolved `operation`
where that distinction is useful.

Support levels are an adapter overlay, not a copy of the source-derived
manifest status. The native manifest inventories RELION job types and labels;
the pipeliner mapping may promote a fully implemented logical operation to
`schedule_mappable`. Runtime execution follows the mapping-level support:

- `schedule_mappable` can be registered through `relion_pipeliner`.
- `direct_command` is executable through a dedicated tool. `rescale_map` uses
  `relion_rescale_map`; `remove_duplicate_particles` uses the rendered/write
  pair `relion_render_remove_duplicate_particles` and
  `relion_remove_duplicate_particles`; `align_symmetry_reference` uses the
  rendered/write pair `relion_render_symmetry_alignment` and
  `relion_align_symmetry_reference`.
- `dry_run_mappable`, `manifest_only`, `mapped_optional`, and `unsupported`
  remain non-executable through the generic write tools.

Query `relion_list_operations` rather than maintaining a separate operation
list in documentation.

CryoSPARC particle conversion is a specialized, additive data handoff rather
than an operation-catalog entry. Use
`relion_render_cryosparc_particle_import` to inspect it and
`relion_import_cryosparc_particles` to materialize the reviewed bundle. The
resulting STAR is registered separately with the mapped `import_particles`
operation before Class3D.

## Workflow inclusion and aliases

`optional` and `enabled_by_default` have different meanings. `optional` is
descriptive metadata surfaced in each plan entry. Only
`enabled_by_default: false` removes a step from the default plan and places it
in `available_opt_in_steps`; pass its ID in `include_opt_in_steps` to include
it. An omitted `enabled_by_default` is treated as `true`, even when
`optional: true` is present.

The bundled workflow disables `manual_particle_picking` and
`local_resolution` by default. Manual picking requires an explicit user
request. Include LocalRes after selecting one best/final deliverable map, or
earlier only after an explicit user request; it is not a routine per-candidate
or matched-branch acceptance step.

The workflow render, run, and add tools accept an optional `alias` argument.
When it is `None`, they use the workflow `step_id` as the RELION job alias. A
caller-supplied alias changes the native job alias only; it does not change the
workflow step ID, dependency plan, or operation. Aliases contain 1–80 letters,
digits, `.`, `_`, or `-`.

## Contract caveats

- Workflow dependency entries and `relion_suggest_step_inputs` are
  result-type/provenance hints; they never bind an input automatically. In
  particular, `postprocess_after_dynamight` requires explicitly verified
  DynaMight backprojection half-maps. The bundled DynaMight adapter does not
  register those outputs, so an earlier `half_maps` producer must not be used
  merely because it is the latest type match.
- `parameter_dependency_rules` are returned as contract data for the
  orchestrator. The MCP does not currently rewrite downstream overrides from
  those rules.
- Values under `tutorial_example_params` are non-executable documentation used
  only to validate that the template remains renderable. They are never merged
  into effective parameters. A step with `explicit_param_overrides` remains
  `execution_ready=false`, and workflow render/run/add calls fail closed, until
  every active requirement is present in the caller's `param_overrides`.
- For `import_movies`, the logical `source_movies` input and
  `raw_movie_glob` parameter both target RELION's native `fn_in_raw` option;
  when both are supplied, the explicit `raw_movie_glob` assignment is the
  effective native value. This preserves the bundled adapter contract.
- Source/provenance paths embedded in contract metadata identify the upstream
  extraction source. They are not required to exist in an installed package;
  the four bundled files under `src/relion_mcp/configs/` are the runtime
  contracts.

## Started and completion semantics

The combined `relion_run_*` tools register the job or graph and launch one
RELION schedule in one MCP write. Their successful response has
`pipeline_registered=true` and `started=true`.

`started=true` means that the detached schedule launcher was created
successfully. It does **not** mean that RELION finished the job or that the
scientific result was accepted. Use `relion_job_status`, outputs, logs, and the
appropriate evidence tools to establish completion. Pass the logical
`operation` to `relion_job_status` when expected-output verification is
required; for particle imports, also pass the source particle count when it is
known. Native success and `output_verification.output_verified` are reported
separately. If launch fails after registration, the error identifies the
preserved jobs; do not register them a second time. Diagnose the failure and
recover with `relion_start_jobs`.

The `relion_add_*` tools are advanced/recovery interfaces. They only register
jobs. In the class-selection add response, `started=false` and
`next_write_tool="relion_start_jobs"` make that state explicit.

## InitialModel-to-Class3D acceptance interlock

Class3D registration and launch are protected by a hard,
`relion.initial-model-acceptance/v1` interlock. Rendering remains read-only:
`relion_render_job` reports `launch_gate="initial_model_acceptance"` for the
resolved `three_d_classification` operation, but it does not satisfy or bypass
the gate.

Create a record only with `relion_record_initial_model_acceptance`, then pass
the returned project-relative `initial_model_acceptance` path to the Class3D
call through `relion_run_job`, `relion_run_workflow_step`, `relion_add_job`, or
`relion_add_workflow_step`. The argument is `str | None = None` because these
four generic wrappers also serve other operations. In practice it is required
when the resolved operation is `three_d_classification`; omitting it fails
before registration, while supplying it for any other operation is rejected.

The record tool accepts only project-relative regular files and binds each
artifact by canonical path, byte size, and SHA-256. The InitialModel source
must satisfy all of the following:

- `reference_volume` is a final `run_itNNN_classMMM.mrc` in an
  `InitialModel/jobNNN/` directory, decodes as a finite, nonconstant,
  floating-point 3-D MRC, and has cubic dimensions and isotropic voxel size
  matching the declared geometry;
- `model_star`, `optimiser_star`, and `data_star` are the matching
  `run_itNNN_model.star`, `run_itNNN_optimiser.star`, and
  `run_itNNN_data.star` from the latest complete iteration of that same job;
- the final model STAR declares a 3-D reference and lists the selected map in
  `rlnReferenceImage`;
- the same directory contains a non-symlink `job.star` whose job type is
  `relion.initialmodel` and whose `fn_img` exactly names
  `initial_model_particles`, plus `RELION_JOB_EXIT_SUCCESS`; and
- `initial_model_particles` and `classification_particles` parse as nonempty
  RELION particle STAR files with no duplicate `rlnImageName` values. The
  latter is the exact final Class3D particle population to which the record is
  bound.

`assessment` requires the following exact acceptance values. Additional
fields may be recorded, but they do not relax any required criterion.

| Field | Required value |
|---|---|
| `seed_status` | `"SEED_READY"` |
| `reference_utility` | `"credible"` |
| `confidence` | `"high"` |
| `decision` | `"auto_accept"` |
| `next_action` | `"advance_primary"` |
| `approval_flag` | `true` |
| `visual_access` | `"direct"` |
| `distinct_2d_views_matched` | integer greater than or equal to `3` |
| `late_iteration_stable` | `true` |
| `multiple_contours_checked` | `true` |
| `boundary_clearance_pass` | `true` |
| `clipping_detected` | `false` |
| `major_artifact_detected` | `false` |
| `independent_root_sources` | at least two distinct values from `"map_pixels"`, `"particle_class_metadata"`, and `"late_iteration_convergence"` |
| `critical_gaps` | an empty list |
| `rationale` | a nonempty string |

`geometry` requires exactly the measurements needed to bind the seed to the
Class3D particle diameter and physical box:

| Field | Required constraint |
|---|---|
| `expected_particle_extent_A` | two positive finite numbers `[minimum, maximum]`, with `minimum <= maximum` |
| `mask_diameter_A` | positive finite number strictly greater than the maximum expected extent |
| `box_size_pix` | positive integer |
| `pixel_size_A` | positive finite number |
| `boundary_clearance_A` | positive finite number |

The physical box must also satisfy
`box_size_pix * pixel_size_A > mask_diameter_A + 2 * boundary_clearance_A`.
At registration and launch, the recorded `mask_diameter_A` must match the
rendered Class3D native `particle_diameter` within `1e-6` Å.

`evidence_artifacts` is a mapping from safe lower-case names to nonempty,
project-relative regular files. Its three required values must be genuine,
nonconstant PNG images of at least 16 by 16 pixels. It must contain three
distinct paths and three distinct content hashes under the
exact keys `map_multiview`, `projection_comparison`, and
`late_iteration_comparison`. Additional evidence names must match
`[a-z][a-z0-9_]{0,63}`. All supplied evidence files are hash-bound; a missing,
changed, or unavailable artifact fails validation. A caller with `unknown` or
`unassessable` required evidence must treat that as a critical gap and must not
record acceptance.

Eligible visual reports are cached in the running MCP process when returned to
the client. After directly inspecting a report, pass its exact metadata
`image.sha256` to `relion_publish_initial_model_evidence` with the matching
`evidence_kind`. The publisher materializes those same PNG bytes under
`.relion-mcp/initial-model-evidence/` and returns the project-relative path for
`evidence_artifacts`. A cache miss or expiry requires rendering and inspecting
the report again. Publication proves byte identity and compatible report type;
it does not interpret the pixels or satisfy the scientific acceptance criteria
by itself.

`particle_lineage.mode` is one of:

- `same_particles`: `initial_model_particles` and
  `classification_particles` must have the exact same project-relative path
  and SHA-256; or
- `initial_model_subset_of_classification`: `particle_lineage.evidence_path`
  is required and is stored as a path/size/SHA-256-bound artifact. That
  artifact must document the audited, duplicate-free relationship between the
  InitialModel seed subset and the full final Class3D population.

Records are canonical-JSON content addressed under
`.relion-mcp/initial-model-acceptances/acceptance-<24-hex>.json`. The filename
contains the first 24 hexadecimal characters of the full payload SHA-256,
which is also stored as `record_sha256`. Publication is no-replace and
idempotent for identical content; newly published files are mode `0444` to
reduce accidental mutation. Top-level keys are exactly `schema`,
`reference_volume`, `initial_model_particles`, `classification_particles`,
`initial_model_artifacts`, `evidence_artifacts`, `assessment`, `geometry`,
`particle_lineage`, and `record_sha256`; `initial_model_artifacts` contains
exactly `model_star`, `optimiser_star`, `data_star`, `job_star`, and
`success_marker`. The record tool writes every artifact object with its
project-relative `path`, `sha256`, and `size_bytes`. Validation requires the
exact top-level and InitialModel-artifact key sets, valid artifact metadata,
matching content and filename digests, all current artifact sizes and hashes,
the acceptance criteria above, current MRC/PNG/STAR decoding and source
relations, and any requested Class3D reference, particle, and mask bindings.

`relion_start_jobs` has no acceptance-path argument. Before launching a
registered Class3D job, it reads the job's `job.star`, extracts `fn_ref`,
`fn_img`, and `particle_diameter`, and revalidates a matching record from the
acceptance directory. Thus add-only recovery and the start half of a combined
run both fail before scheduler launch if the record or any bound artifact has
become missing, stale, tampered with, or mismatched. A registered Class3D job
created outside the guarded add path is subject to the same start-time check.

The record is an integrity- and provenance-bound assertion. The server checks
the declared criteria, source structure, MRC/PNG/STAR validity, basic image
variation, and bound bytes, but it does not interpret the scientific content,
prove that a subset audit is correct, or infer that the seed is biologically
true. The caller remains responsible for direct scientific assessment before
recording `auto_accept`.

This interlock governs MCP add/run/start tools. An execution environment that
also gives the agent unrestricted shell access to `relion_pipeliner` can bypass
any application-level MCP check; production profiles must therefore require
Class3D execution through the guarded MCP tools rather than direct native
commands.

### External refined references

A refined consensus map produced outside RELION, for example by a CryoSPARC
homogeneous, non-uniform, or local refinement, is not an InitialModel seed
and has no InitialModel artifacts to bind. Admit it with
`relion_record_external_reference`, which publishes a
`relion.external-reference-acceptance/v1` record named
`external-<digest>.json` in the same directory. Pass its returned
`initial_model_acceptance` path to the same Class3D add/run argument; start-only
recovery discovers both record types.

The record binds the reference MRC and the classification particle STAR by
canonical path, byte size, and SHA-256 and requires:

- `reference_volume` outside `InitialModel/`, decoding as a finite,
  nonconstant, floating-point, cubic 3-D MRC with isotropic voxels;
- a particle STAR with a `data_optics` loop whose every `rlnImageSize` equals
  the reference box and whose every `rlnImagePixelSize` equals the reference
  voxel size, plus nonempty particle rows without duplicate `rlnImageName`;
- `mask_diameter_A` positive and no larger than the physical box;
- `provenance` with nonempty `source_software`, `source_job`, and `rationale`,
  and optional `source_path` and lowercase `source_sha256`; and
- `pose_lineage` exactly `{"mode": "poses_from_reference_refinement"}` or
  `{"mode": "independent_of_reference"}`. The former also requires
  `rlnAngleRot`, `rlnAngleTilt`, `rlnAnglePsi`, and an origin pair in the
  particle STAR.

At registration and start the gate re-measures geometry, rehashes both files,
and checks the reference path, particle path, and `particle_diameter` as for an
InitialModel record. A Class3D rendered with native `dont_skip_align == No`
keeps the input poses, so it is admitted only with
`poses_from_reference_refinement`.

## Tool inventory

The MCP titles and annotation profiles are part of the public discovery
surface.

| Tool | MCP title | Annotation |
|---|---|---|
| `relion_environment` | Check RELION environment | `READ_ONLY` |
| `relion_list_operations` | List RELION operations | `READ_ONLY` |
| `relion_describe_operation` | Describe RELION operation | `READ_ONLY` |
| `relion_validate_workflow` | Validate RELION tutorial | `READ_ONLY` |
| `relion_render_job` | Render RELION job | `READ_ONLY` |
| `relion_render_workflow_step` | Render tutorial step | `READ_ONLY` |
| `relion_render_symmetry_alignment` | Render RELION symmetry alignment | `READ_ONLY` |
| `relion_render_remove_duplicate_particles` | Render duplicate-particle removal | `READ_ONLY` |
| `relion_render_cryosparc_particle_import` | Render CryoSPARC particle import | `READ_ONLY` |
| `relion_plan_particle_class_selection` | Plan exact particle class selection | `READ_ONLY` |
| `relion_visual_smoke_test` | Create Codex image-content challenge | `READ_ONLY` |
| `relion_verify_visual_smoke_test` | Verify Codex image-content challenge | `READ_ONLY` |
| `relion_render_2d_classes` | Render RELION 2D class contact sheet | `READ_ONLY` |
| `relion_validate_2d_class_review` | Validate RELION 2D rendering coverage | `READ_ONLY` |
| `relion_compare_map_projections` | Compare map projections with RELION 2D classes | `READ_ONLY` |
| `relion_render_particle_boxes` | Render RELION particle boxes | `READ_ONLY` |
| `relion_audit_particle_selection` | Audit RELION particle selection | `READ_ONLY` |
| `relion_plan_micrograph_subset` | Plan stratified RELION micrograph subsets | `READ_ONLY` |
| `relion_materialize_micrograph_subset` | Materialize stratified RELION micrograph subsets | `WRITE_ADDITIVE` |
| `relion_render_pick_overlays` | Render RELION/Topaz pick overlays | `READ_ONLY` |
| `relion_render_3d_classes` | Render RELION 3D class report | `READ_ONLY` |
| `relion_compare_maps` | Align and compare RELION maps | `READ_ONLY` |
| `relion_validate_3d_assessment` | Validate RELION 3D assessment | `READ_ONLY` |
| `relion_validate_initial_model_acceptance` | Validate InitialModel acceptance | `READ_ONLY` |
| `relion_record_initial_model_acceptance` | Record InitialModel acceptance | `WRITE_IDEMPOTENT` |
| `relion_record_external_reference` | Record external refined reference | `WRITE_IDEMPOTENT` |
| `relion_publish_initial_model_evidence` | Publish inspected InitialModel visual evidence | `WRITE_IDEMPOTENT` |
| `relion_render_map_report` | Render RELION map report | `READ_ONLY` |
| `relion_render_mask_report` | Render RELION mask report | `READ_ONLY` |
| `relion_render_fsc_report` | Render RELION FSC report | `READ_ONLY` |
| `relion_scan_symmetry` | Scan C1 half-map symmetry | `READ_ONLY` |
| `relion_compare_symmetry` | Compare symmetry-branch maps | `READ_ONLY` |
| `relion_compare_hands` | Compare map hands | `READ_ONLY` |
| `relion_initialize_project` | Initialize RELION project | `WRITE_IDEMPOTENT` |
| `relion_import_cryosparc_particles` | Import CryoSPARC particles | `WRITE_ADDITIVE` |
| `relion_run_job` | Register and run RELION job | `WRITE_ADDITIVE` |
| `relion_run_workflow_step` | Register and run tutorial step | `WRITE_ADDITIVE` |
| `relion_run_particle_class_selection` | Register and run exact particle class selection | `WRITE_ADDITIVE` |
| `relion_add_job` | Add RELION job | `WRITE_ADDITIVE` |
| `relion_add_particle_class_selection_jobs` | Add exact particle class-selection jobs | `WRITE_ADDITIVE` |
| `relion_add_workflow_step` | Add tutorial step | `WRITE_ADDITIVE` |
| `relion_start_jobs` | Start RELION jobs | `WRITE_ADDITIVE` |
| `relion_job_status` | Check RELION job status | `READ_ONLY` |
| `relion_job_outputs` | Inspect RELION job outputs | `READ_ONLY` |
| `relion_suggest_step_inputs` | Suggest tutorial step inputs | `READ_ONLY` |
| `relion_tail_job_log` | Tail RELION job log | `READ_ONLY` |
| `relion_abort_job` | Abort RELION job | `WRITE_DESTRUCTIVE` |
| `relion_align_symmetry_reference` | Materialize symmetry-aligned RELION reference | `WRITE_ADDITIVE` |
| `relion_remove_duplicate_particles` | Remove duplicate particles | `WRITE_ADDITIVE` |
| `relion_rescale_map` | Rescale RELION map | `WRITE_DESTRUCTIVE` |

## Read-only catalog and workflow tools

### `relion_environment`

```text
relion_environment(project_dir: str | None = None) -> dict[str, Any]
```

Annotation: `READ_ONLY`.

Returns keys `mode`, `approved_roots`, `expected_relion_version`, `pipeliner`,
`image_handler`, `align_symmetry`, `star_handler`, `class_selector`,
`particle_class_selection_modes`, `symmetry_alignment_limits`, `relion_refine`, `execution_path`,
`relion_import`, `ctffind`, `topaz`, `version_probe`, `project`, `project_marker`,
`pipeline_file`, `pipeline_exists`, and `cryosparc_particle_import`.
`cryosparc_particle_import` reports any configured standalone read roots,
availability of the combined route's startup live-project inventory, the
active source-authorization policy, copy-only output policy, Class3D CTF
requirement, explicit-initialization policy, output layout, pyem
interpreter/script resolution and script SHA-256, and aggregate `ready` state.
`class_selector` is the resolved
GUI-equivalent selector executable or `None`; `particle_class_selection_modes`
reports the preferred mode, GUI-equivalent availability, and fallback mode.
`topaz` is resolved from `RELION_TOPAZ_EXECUTABLE` for availability reporting;
the executable used by a Topaz training or picking job remains its
operation-level `topaz_executable` parameter.
`align_symmetry` is the resolved native `relion_align_symmetry` executable or
`None`. `symmetry_alignment_limits` reports the configured maximum input box
side and the fixed minimum number of Fourier-radius scoring shells.
Project fields are `None`/`false` when `project_dir` is omitted.

### `relion_list_operations`

```text
relion_list_operations(support_level: str | None = None) -> list[dict[str, Any]]
```

Annotation: `READ_ONLY`.

Returns operation rows with `operation`, `job_type`, `support_level`,
`description`, and `expected_results`. A non-`None` support level filters by
exact value.

### `relion_describe_operation`

```text
relion_describe_operation(operation: str) -> dict[str, Any]
```

Annotation: `READ_ONLY`.

Mapped operations return `requested_operation`, `operation`, `job_type`,
`support_level`, `executable`, `description`, `inputs`, `params`, and
`expected_results`. Input and parameter entries combine neutral scientific
contract metadata with executable mapping data, then add native GUI labels,
manifest defaults, help, and file patterns. The direct
`rescale_map` and `align_symmetry_reference` operations return
`requested_operation`, `operation`, `job_type`, `support_level`, `executable`,
`description`, `expected_results`, and their neutral `definition`.

Mapped and direct operations also expose `authorization_required`,
`authorization_source`, `authorization_note`, `requires_human_approval`, and
`human_approval_conditions`. Routine additive work uses `user_task_request`
authorization without a separate human approval. Protected or opt-in actions
still need their specific authorization, which an earlier explicit request can
already supply. Conditional protection includes replacing an existing rescale
output; these metadata do not bypass server or host permissions.

### `relion_validate_workflow`

```text
relion_validate_workflow(
    workflow_id: str = "relion_5_spa_tutorial_v1",
    param_overrides: dict[str, dict[str, Any]] | None = None,
    include_opt_in_steps: list[str] | None = None,
) -> dict[str, Any]
```

Annotation: `READ_ONLY`.

Returns `workflow_id`, `description`, `relion_version`,
`authorization_required`, `authorization_source`, `requires_human_approval`,
`valid_for_planning`, `valid_for_full_execution`,
`declared_step_count`, `step_count`, `available_opt_in_steps`,
`included_opt_in_steps`, `skipped_steps`, `issues`, `plan`, and
`parameter_dependency_rules`, plus `catalog_issues`. Each plan entry contains
`index`, `step_id`, `requested_operation`, resolved `operation`, `job_type`,
`support_level`, `executable`, `execution_ready`,
`missing_explicit_param_overrides`, `interactive_or_selection_pause`,
`optional`, `enabled_by_default`, `activation_policy`, `quality_checkpoint`,
`launch_gate`, `dependencies`, effective `params`, `warnings`, and
`expected_results`.
Unknown override or opt-in step IDs are rejected.
`interactive_or_selection_pause` is true for manual picking. Automated particle
selection retains its scientific `quality_checkpoint` without an interactive
pause. The standard workflow requires task authorization, not separate approval
for every stage.

### `relion_render_job`

```text
relion_render_job(
    operation: str,
    inputs: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    pipeline_name: str | None = None,
    alias: str | None = None,
    include_defaults: bool = True,
) -> dict[str, Any]
```

Annotation: `READ_ONLY`.

Returns `requested_operation`, resolved `operation`, `job_type`,
`support_level`, `executable`, `options_by_manifest_key`,
`option_assignments`, argv-style `command`, `launch_gate`, and
`symmetry_preflight`. `launch_gate` is `"initial_model_acceptance"` for
`three_d_classification` and `None` otherwise. The tool does not modify a
project. For 2D and 3D classification,
`e_step_resolution_limit_A` maps to RELION's native `highres_limit`; use a
positive Angstrom value to limit the E-step or `-1` to disable the limit.
For `three_d_classification`, `alignment` defaults to `true`. Set it explicitly
to `false` to render RELION's native `dont_skip_align == No` classification-only
mode; the input particle STAR must already contain the orientations that will
remain fixed. The optional `reference_mask` input maps to native `fn_mask` for
focused classification. No-alignment and focused classification remain
protected scientific hypotheses under the repository orchestrator even though
the render and run tools expose their typed mechanics.

### `relion_render_workflow_step`

```text
relion_render_workflow_step(
    step_id: str,
    input_paths: dict[str, str] | None = None,
    param_overrides: dict[str, Any] | None = None,
    workflow_id: str = "relion_5_spa_tutorial_v1",
    pipeline_name: str | None = None,
    alias: str | None = None,
) -> dict[str, Any]
```

Annotation: `READ_ONLY`.

A mapped step returns `step_id`, `requested_operation`, resolved `operation`,
`job_type`, `support_level`, `input_paths`, effective `params`,
`option_assignments`, argv-style `command`, `launch_gate`, `symmetry_preflight`,
`quality_checkpoint`, and `warnings`. The direct rescale step instead returns
`step_id`, `operation`, `support_level`, `params`, `required_input`, and an
argv-style `command_template`. A future `align_symmetry_reference` workflow
step is rejected here with an instruction to use
`relion_render_symmetry_alignment`, which requires explicit project and
`ReferencePrep/` paths. Guarded steps reject missing explicit overrides before
rendering; tutorial examples never satisfy that execution gate.

### `relion_render_symmetry_alignment`

```text
relion_render_symmetry_alignment(
    project_dir: str,
    input_map: str,
    output_map: str,
    target_symmetry: str,
    search_max_resolution_A: float,
) -> dict[str, Any]
```

Annotation: `READ_ONLY`.

Validates the confined project-relative input and output paths and renders the
native `relion_align_symmetry` command template without creating an output.
The explicit output must be a new `.mrc` path beneath `ReferencePrep/`; paths
inside native RELION job directories are outside this operation's contract.
`target_symmetry` is required and accepts C2-C64, D2-D32, T, O, or I1-I4.
C1, bare I, and custom symmetry-definition files are rejected. C4 is only an
example of an accepted caller-selected target; it is not fixed by the
operation. The bounded search disables center-of-mass recentering and rotates
about the geometric array center, uses a 64-pixel working box, 400 random
global orientations, local-search range and step values of 2, padding factor
2, and trilinear interpolation. It never enables `--apply_sym` or `--only_rot`,
never averages the map, and exposes no overwrite control.

The input must be a finite, nonconstant, real mode-2 MRC on an even cubic grid
of at least 64 pixels and no more than the configured
`RELION_SYMMETRY_ALIGNMENT_MAX_BOX_PX` (512 by default), with positive
isotropic sampling, canonical MRC axis mapping, and zero starts/origin. The
input file also has a derived size cap based on that box limit. The cutoff must
be no finer than the fixed working-grid Nyquist limit and must retain at least
three Fourier-radius scoring shells; for a physical box length `L`, it must be
strictly less than `L/2`.

Returns the project-relative `input_map` and `output_map`, normalized
`target_symmetry`, input hash and grid, the validated positive finite
`search_max_resolution_A`, the bounded effective settings, and an argv-style
`command_template`. It also returns the resolved project-relative input source,
input-size/resource limits, and the minimum free staging-space requirement.
Its input-snapshot and staged-output placeholders are replaced only for
execution inside a private temporary directory, where native RELION resolves
the selected built-in point group. The maximum resolution limits only the
alignment search; the full-size input map is rotated and
written at full bandwidth by the materialize tool. Rendering does not register
a pipeline job, average the map, or modify particle metadata.

### `relion_render_remove_duplicate_particles`

```text
relion_render_remove_duplicate_particles(
    project_dir: str,
    particles: str,
    duplicate_distance_A: float,
    output_star: str,
    image_pixel_size_A: float | None = None,
) -> dict[str, Any]
```

Annotation: `READ_ONLY`.

Performs the complete duplicate-removal preflight without running RELION or
creating directories or files. It resolves and validates the input, the new
`Deduplicate/` output, and RELION's implicit sibling `<stem>_removed.star`, and
renders the argv-style native `command_template` that the write tool will
instantiate and run inside a private staging directory. Both final destinations
must be absent and distinct from the input.

The native `--image_angpix` value is the coordinate-system pixel size used by
the extraction micrographs. The renderer obtains the authoritative value from
`rlnMicrographPixelSize` for every optics group referenced by the particle
table; neither `rlnMicrographOriginalPixelSize`, which describes the original
acquisition/MTF scale, nor a possibly rescaled particle-stack
`rlnImagePixelSize` is substituted for it. If any referenced group lacks
`rlnMicrographPixelSize`, an explicit verified `image_pixel_size_A` is required,
even when image and original pixel sizes happen to agree. Because native
`relion_star_handler` accepts only one `--image_angpix`, referenced optics
groups with different coordinate pixel sizes are rejected and must be split.
A supplied value must agree with authoritative STAR metadata when present. The
resolved scalar is always emitted as `--image_angpix`, avoiding RELION's unsafe
fallback to the first optics row when that row is not used by the particles.

Native SPA duplicate removal also requires `rlnCoordinateX`, `rlnCoordinateY`,
`rlnImageName`, `rlnMicrographName`, `rlnOpticsGroup`, `rlnOriginXAngst`, and
`rlnOriginYAngst` in `data_particles`, plus a loadable optics table. A fresh
unaligned extraction commonly lacks the origin columns and is rejected during
rendering; use an aligned/selected particle STAR or an explicitly prepared
equivalent rather than discovering the incompatibility after execution. The
optics table must list contiguous group IDs `1..N` in row order; otherwise
RELION's loader would silently renumber particle groups, so the renderer fails
before execution instead. The four coordinate/origin values must be finite,
required image and micrograph names cannot use STAR null markers, and the SPA
path rejects the tomography-triggering `rlnCenteredCoordinateZAngst` column.
To match native RELION parsing, native-bound files must be valid UTF-8 without
NUL bytes or any carriage returns. Particle and optics rows must each be
complete on one physical line; inter-row full-line comments and extra or
truncated values fail preflight. The particle loop must be the block's first
data construct, while `data_optics` must contain exactly one loop and no scalar
list. Ambiguous native `data_`/`loop_` substring dispatch, malformed column
declarations, duplicate logical labels, and the writer-special `rlnComment` and
`rlnSortedIndex` columns are rejected. Values are parsed with RELION's own
line-simplification and token rules, then validated against a version-pinned
RELION 5.0 label-type snapshot. Null markers are unsafe for recognized
non-string types. Native-parsed strings must survive RELION's writer/readback
unchanged, while scalar and vector values are normalized using the native
writer presentation for integrity comparison.

The result has `status: "rendered"` and contains the normalized input and both
output paths, the physical duplicate distance, particle and coordinate
pixel-size metadata, whether the particle images were rescaled after
extraction, `optics_groups`, the
`command_template`, and publication-strategy metadata. Use this result to
review the complete write boundary before calling
`relion_remove_duplicate_particles`.

### `relion_render_cryosparc_particle_import`

```text
relion_render_cryosparc_particle_import(
    project_dir: str,
    source_project_dir: str,
    particles_cs: str,
    passthrough_cs: list[str] | None = None,
    export_name: str = "particles",
) -> dict[str, Any]
```

Annotation: `READ_ONLY`.

Validates a CryoSPARC particle export for use as RELION Class3D input without
creating a directory or running pyem. `source_project_dir` must be absolute and
authorized by exactly one route-specific policy. On the combined EM-Claw
route, its canonical path must exactly match `project_dir` in the API-derived
`{uid, project_dir}` inventory and directory device/inode identities that the
RELION MCP captured from the
authenticated active CryoSPARC environment at startup. Each inventory
`project_dir` is resolved through authoritative `Project.dir()`. Use
`mcp__cryosparc__inspect_project_job` for the requested `P#`/`J#`, pass its
`relion_handoff.source_project_dir`, and choose `particles_cs` and any ordered
`passthrough_cs` entries only from `relion_handoff.allowed_cs_files`. Do not
guess an omitted path when `relion_handoff.allowed_cs_files_truncated=true`. A
standalone RELION server has no live inventory and instead uses an explicit
`RELION_CRYOSPARC_IMPORT_ROOTS` boundary when this bridge is used; that root
boundary is sufficient for source authorization on the standalone route.

Every CS input is a relative path beneath that project. The renderer requires
unique primary and passthrough UIDs, the
particle blob path/index, shape and pixel-size fields, Class3D CTF metadata,
and regular in-root MRC/MRCS
image stacks whose headers, indices, box sizes, and pixel sizes agree with the
CS metadata. CryoSPARC's zero-based `ctf/exp_group_id` values are recorded and
normalized to the one-based `rlnOpticsGroup` values emitted by pyem. Duplicate
image references and MRC volume headers are rejected.

The result includes `project_exists`, `project_initialized`,
`bootstrap_required`, and an explicit `bootstrap` call for
`relion_initialize_project`. A user does not need a pre-existing RELION
project: rendering against a missing target is allowed, reports
`execution_ready=false`, and leaves the target absent. Run the returned
bootstrap separately, then render again before conversion.

The remaining result describes the ordered inputs, particle and stack counts,
bytes to copy, collision-safe `.mrcs` destinations, pyem readiness and argv
template, Class3D alignment warnings, and the no-replace outputs under
`External/CryoSPARC/<export_name>/`. `copy_mode` is fixed to `copy`; this tool
does not filter particles, transform coordinates or poses, repack stacks,
register a RELION job, or modify a source file. `import_job_handoff` supplies
the `relion_render_job`/`relion_run_job` request for the mapped
`import_particles` operation after materialization.

### `relion_plan_particle_class_selection`

```text
relion_plan_particle_class_selection(
    project_dir: str,
    particles_star: str,
    selected_class_numbers: list[int],
    alias: str,
    pipeline_name: str | None = None,
    materialization_mode: str = "auto",
    class_source_star: str | None = None,
) -> dict[str, Any]
```

Annotation: `READ_ONLY`.

`materialization_mode` accepts `auto`, `gui_equivalent`, or `select_joinstar`.
`auto` prefers one pipeline-registered RELION External job running the fixed
`RELION_CLASS_SELECTOR`; it falls back to the native Select/JoinStar graph when
the selector is unavailable. Explicit `gui_equivalent` fails closed when the
selector cannot be resolved. `class_source_star` may be a classification
optimiser or model STAR and is supported only by `gui_equivalent`; it enables a
selected `class_averages.star` output for 2D models.

Returns `operation`, `execution_model`, `materialization_mode`,
`requested_materialization_mode`, `materialization_fallback_reason`,
`project_dir`, `pipeline_name`,
`particles_star`, `selected_class_numbers`, `source_particle_count`,
`source_class_counts`, `selected_class_counts`, `expected_particle_count`,
`select_stages`, `join_stages`, `planned_job_count`,
`start_order_stage_ids`, `expected_final_output`,
`suggested_schedule_name`, `pipeline_registered=false`, `started=false`,
`next_write_tool="relion_run_particle_class_selection"`, and
`advanced_add_only_tool="relion_add_particle_class_selection_jobs"`.

## Visual tools

Twelve tools below return `CallToolResult` with exactly two content blocks in
this order:

1. PNG `ImageContent` with MIME type `image/png`;
2. `TextContent` containing JSON provenance, rendering settings, and metrics.

They deliberately omit `structuredContent`. Detailed input formats,
scientific scope, and renderer limits are documented in
[`VISUAL_TOOLS.md`](VISUAL_TOOLS.md).

### `relion_visual_smoke_test`

```text
relion_visual_smoke_test() -> CallToolResult
```

Annotation: `READ_ONLY`. Returns the common image-first result. The metadata
contains the challenge ID; the expected tokens exist only in the pixels.

### `relion_verify_visual_smoke_test`

```text
relion_verify_visual_smoke_test(
    challenge_id: str,
    labels: dict[str, str],
) -> dict[str, Any]
```

Annotation: `READ_ONLY`.

`labels` is keyed by `top_left`, `top_right`, `bottom_left`, and
`bottom_right`. A live challenge returns `passed`, per-position `matches`,
`attempts_remaining`, `reason`, and `instruction`. An unknown or expired
challenge returns `passed=false`, `reason`, and `required_keys`.

### `relion_render_2d_classes`

```text
relion_render_2d_classes(
    project_dir: str,
    model_star: str,
    classes_mrcs: str | None = None,
    selected_class_numbers: list[int] | None = None,
    max_classes: int = 64,
    columns: int = 8,
    sort_by: Literal["index", "occupancy", "resolution", "score"] = "index",
    low_percentile: float = 1.0,
    high_percentile: float = 99.0,
    all_classes: bool = True,
) -> CallToolResult
```

Annotation: `READ_ONLY`. Returns all requested classes as repeated image/text
page pairs by default. `max_classes` bounds each sheet; `all_classes=false`
returns one sheet with explicit missing-class IDs. At most 16 sheets per call;
use explicit subsets and validate combined coverage for larger jobs. Each page
includes source hashes, class IDs and pagination metadata. Inspect all pages
and pass their metadata to `relion_validate_2d_class_review` before exact
autonomous selection.

### `relion_compare_map_projections`

```text
relion_compare_map_projections(
    project_dir: str,
    map_path: str,
    model_star: str,
    selected_class_numbers: list[int],
    comparison_lowpass_A: float,
    particle_diameter_A: float,
    classes_mrcs: str | None = None,
    direction_count: int = 192,
    inplane_step_deg: float = 10.0,
    max_shift_A: float = 12.0,
    invert_class_contrast: bool = False,
) -> CallToolResult
```

Annotation: `READ_ONLY`. Returns the common image-first result. The caller must
provide a nonempty, unique list of at most 12 one-based 2D class numbers. For
InitialModel acceptance, explicitly select and inspect at least three classes
already judged to be credible, distinct appearances from the seed population;
the tool does not infer credibility or distinctness. Class-stack files are
capped at 512 MiB and selected class images at 1024 pixels per side.

The selected class averages and cubic 3D map must have compatible square boxes,
matching physical pixel sizes, and matching fields of view. The tool applies
the declared common physical low-pass in memory, canonicalizes the map to
physical Z/Y/X, resamples both sides to one bounded analysis grid, and searches
deterministic full-sphere viewing directions, in-plane rotations, and bounded translations
inside a soft radial mask derived from `particle_diameter_A`. It returns, for
each selected class, the class average, best fitted synthetic projection,
magenta/green overlay, signed residual, fit geometry, NCC diagnostics, and a
separated runner-up direction.
Source files are never modified.

This is the orientation-matched synthetic projection comparison required by the
InitialModel policy. A map report or 3D-class report's central-slab or
threshold-occupancy panel is not a synthetic projection and must not be cited
as one. Class number, fitted direction, NCC, runner-up margin, or
`count_requirement_met` alone cannot accept a match or seed; metadata therefore
reports `automatic_match_verdict=false`. There is no universal score threshold.
Direct pixel inspection and all independent acceptance evidence remain required.

### `relion_render_pick_overlays`

```text
relion_render_pick_overlays(
    project_dir: str,
    particle_diameter_pix: float,
    pairs: list[dict[str, str]] | None = None,
    coordinate_index_star: str | None = None,
    fom_threshold: float | None = None,
    max_micrographs: int = 9,
    columns: int = 3,
    low_percentile: float = 1.0,
    high_percentile: float = 99.0,
) -> CallToolResult
```

Annotation: `READ_ONLY`. Returns the common image-first result. Supply exactly
one of `pairs` or `coordinate_index_star`; every explicit pair has
`micrograph` and `coordinates` project-relative paths.

### `relion_render_3d_classes`

```text
relion_render_3d_classes(
    project_dir: str,
    model_star: str,
    class_maps: list[str] | None = None,
    selected_class_numbers: list[int] | None = None,
    max_classes: int = 8,
    sort_by: Literal["index", "occupancy", "resolution"] = "index",
    include_mip: bool = True,
    display_lowpass_A: float | None = None,
    low_percentile: float = 1.0,
    high_percentile: float = 99.5,
) -> CallToolResult
```

Annotation: `READ_ONLY`. Returns the common image-first result.

All rendered classes use shared display limits and must have identical shape,
voxel size, origin, start indices, and MRC axis mapping; the tool does not align
them. Despite its legacy name, `include_mip=true` adds a Z central-slab mean
with a threshold-occupancy contour, not a maximum-intensity projection. Metadata
contains size-aware connected-component summaries and pairwise raw and common-
Gaussian-filter similarity/overlap diagnostics. The common filter uses a fixed
sigma in downsampled voxels and is not a physical downstream low-pass.
Set `display_lowpass_A` to a finite positive resolution no finer than the map's
physical Nyquist limit to render every class through the same in-memory radial
Fourier low-pass before downsampling. The raised-cosine edge spans two Fourier
shells. Metadata records the cutoff, transition, physical grid, and read-only
provenance; source maps are never changed. Call the tool separately with the
default `None` when both raw and filtered evidence is required.

### `relion_compare_maps`

```text
relion_compare_maps(
    project_dir: str,
    reference_map: str,
    candidate_map: str,
    reference_label: str,
    candidate_label: str,
    alignment_lowpass_A: float,
    comparison_lowpasses_A: list[float] | None = None,
    mask_path: str | None = None,
    alignment_mode: Literal["translation", "rigid"] = "rigid",
    max_shift_A: float = 15.0,
    max_rotation_deg: float = 15.0,
    threshold_sigma: float = 2.0,
    low_percentile: float = 0.5,
    high_percentile: float = 99.5,
) -> CallToolResult
```

Annotation: `READ_ONLY`. Returns the common image-first result. The fixed
`reference_map` and moving `candidate_map` require truthful, distinct labels.
Both maps must be finite, non-constant 3D maps with identical canonical
physical shapes and matching isotropic sampling. Their MRC axis mappings are
canonicalized to physical Z/Y/X. Header origins, starts, and axis mappings may
differ and are recorded; matching headers are not treated as proof that the
density is aligned. The tool rejects the same filesystem object or
alias supplied twice. Byte-identical density in two different artifacts is
allowed because it can be a legitimate branch result, but it is flagged and
deduplicated by canonical voxel content. An optional mask must share the
reference map's complete physical MRC grid and contain values in `[0, 1]`.

The tool performs bounded **local** translation or rigid registration, not a
global arbitrary-orientation search. It fits the candidate-to-reference
transform once at the required physical `alignment_lowpass_A`, after raising
that cutoff when necessary to respect the bounded analysis grid's Nyquist
limit. It then freezes the transform for the effective alignment cutoff and up
to six optional `comparison_lowpasses_A`; it never refits at each filter or
contour. `max_rotation_deg` is capped at 30 degrees per Euler axis,
`max_shift_A` cannot exceed one quarter of the shortest map extent, and the
optimizer has a fixed evaluation cap. Translation bounds below half a source
voxel, and rigid-rotation bounds too small to move the analysis radius by half
a pixel, are rejected so a tiny search window cannot force identity. No
transformed MRC is materialized. The exact identity is always a candidate. A
non-identical selected fit—including a non-identical identity optimum—is
checked with five deterministic spatial block-jackknife probes; pose dispersion
and held-out Fisher-z gain test repeatability without imposing a universal
correlation cutoff. Exact canonical voxel identity is recorded directly. The
validation probes never replace the selected transform used for reported
metrics.

Metadata reports pre/post weighted Pearson correlation, normalized RMS
difference, threshold Dice/Jaccard, common metric weights, supported-fold
diagnostics, search bounds, interpolation, and every requested cutoff view. The
transform is reported as a centered canonical-grid displacement plus a complete
candidate-index-XYZ to reference-index-XYZ homogeneous matrix; MRC origin/start
metadata is explicitly excluded from that density fit. Whole-file hashes remain
artifact provenance. Evidence deduplication uses an unordered pair of canonical
ZYX float32 voxel hashes, while a separate analysis ID includes role ordering,
canonical shape, physical and analysis sampling, mask, transform, filters,
bounds, threshold, and algorithm version.

Only `aligned` and `identity_optimal` are accepted registration statuses.
`boundary_limited`, `insufficient_overlap`, `optimizer_stopped`,
`ambiguous_alignment`, `unstable_alignment`, or `fit_unverified` makes a
structural relation unassessable. Even an accepted registration does not infer
`same`, `distinct`, biological identity, or a benefit from combining particle
sets. All filters, contours, before/after measurements, stability probes, and
difference views from one map pair remain correlated derivatives of one
`map_pixels` evidence root. Different strongly low-passed starting references
do not prevent descriptive registered comparison, but they remain a causal
confound for an A/B/A∪B performance claim.

### `relion_validate_3d_assessment`

```text
relion_validate_3d_assessment(
    classes: list[dict[str, Any]],
    pairs: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    decision: str,
    confidence: str,
    next_action: str,
    approval_flag: bool,
    rationale: str = "",
) -> dict[str, Any]
```

Annotation: `READ_ONLY`.

Validates the independent `relion.3d-assessment/v1` decision contract without
reading or changing a RELION project. Class records require `class_id`,
`reference_utility` (`credible`, `ambiguous`, or `unusable`), `morphology`
(`coherent`, `mixed`, `artifact_dominated`, or `unassessable`), and
`disposition` (`primary`, `fallback`, `preserve`, `reject`, or `human_review`).
Pair records require `class_a`, `class_b`, `relation` (`same`, `distinct`,
or `unassessable`), `alignment` (`aligned`, `common_frame_verified`,
`unaligned`, or `unassessable`), `biological_interpretation` (`supported`,
`unknown`, or `unsupported`), and the explicit boolean
`same_pathology_claimed`. An optional pair-level `rationale` is also accepted.

Each evidence record requires `evidence_id`, `source`, and `root_source`.
Multiple renderings or measurements derived from the same `root_source` count
as one independent source. Decisions are `auto_accept`, `primary_fallback`,
`defer`, `human_gate`, or `auto_reject`; confidence is `low`, `medium`, or `high`.
`next_action` is one of `advance_primary`, `activate_fallback`, `repeat`,
`retry`, `await_human`, or `stop`.

For a quantitative map relation, evidence naming `relion_compare_maps` must
also copy its `alignment_status`, canonical `map_pixels_pair_sha256:`
`root_source`, `map_comparison_analysis_sha256:` `analysis_id`, and
`fixed_transform_across_cutoffs=true`, plus `class_a` and `class_b` binding the
result to the assessed pair. Both hash identifiers require the exact 64-digit
lowercase hexadecimal value returned by the tool; placeholders are invalid.
Only tool status `aligned` or `identity_optimal` can support a high-confidence
`same`/`distinct` relation, and the pair itself must be submitted as aligned or
common-frame-verified.
Matching MRC headers, an arbitrary source string, or any failed/unstable status
does not establish `common_frame_verified`. The validator checks this
provenance contract but cannot independently reread the cited pixels or
transform.

The validator forbids `auto_reject` when any class is credible or ambiguous,
or when a structurally distinct pair is claimed to exhibit the same pathology.
Every autonomous decision (`auto_accept`, `auto_reject`, or
`primary_fallback`) requires `approval_flag=true`, which may come from the
existing processing request. Final `auto_accept` and `auto_reject` require high
confidence. `primary_fallback` permits medium confidence for provisional
downstream processing and requires exactly one credible primary. At medium confidence
that primary must have coherent or mixed morphology, and no class may be
rejected. The caller must preserve ranked alternatives, activation triggers,
finite budgets, and checkpoint assessments in run state; the validator does not
verify those records. This processing may span multiple stages without a
separate validation experiment at each step. Low-confidence assessments cannot
advance a primary or activate a fallback. Use `defer` with `retry`, `repeat`, or
`stop` for unresolved science that the agent can diagnose or leave unresolved.
It cannot advance a branch or request human input. `human_gate` cannot advance
a branch either; legacy diagnostic actions remain supported with a warning.
The returned `human_input_required` is true only for a valid `human_gate` with
`await_human`; other decisions cannot request `await_human`. Invalid records
normally recommend `defer` for caller repair;
when a credible, preserved, authorized primary meets all continuation guards,
the validator may recommend `primary_fallback` instead of a final decision.
An invalid record never requests human input merely because validation failed.
High confidence requires at
least two distinct root evidence sources and verified alignment for every
`distinct` pair. The validator never silently rewrites a forbidden decision:
callers must check `valid` (or both `schema_valid` and `decision_allowed`) and
may use `recommended_decisions` to route the assessment safely.

The validator checks only the submitted structure and guards. It does not read
map pixels, verify the scientific truth or independence of evidence records,
perform alignment, infer a biological state, or grant RELION write authority.

### `relion_validate_initial_model_acceptance`

```text
relion_validate_initial_model_acceptance(
    project_dir: str,
    acceptance_record: str,
    reference_volume: str | None = None,
    classification_particles: str | None = None,
    mask_diameter_A: float | None = None,
    alignment: bool | None = None,
) -> dict[str, Any]
```

Annotation: `READ_ONLY`.

Validates one project-relative `relion.initial-model-acceptance/v1` record
using the complete [InitialModel-to-Class3D interlock](#initialmodel-to-class3d-acceptance-interlock)
contract, or one `relion.external-reference-acceptance/v1` record using the
[external refined reference](#external-refined-references) contract. For an
external record, `alignment=false` additionally requires
`poses_from_reference_refinement`. It recomputes the canonical record digest, checks the
content-addressed filename, rehashes every bound source, evidence, and lineage
artifact, and reevaluates the required assessment and geometry fields.

The three optional arguments bind the integrity check to a proposed Class3D
launch. When supplied, `reference_volume` and `classification_particles` must
exactly match the recorded project-relative paths, and `mask_diameter_A` must
match the recorded geometry within `1e-6` Å. Omit them only when validating
the record itself rather than a particular launch; the Class3D add/start paths
supply all three internally.

The normal result contains `valid`, `schema`, `project_dir`,
`acceptance_record`, `record_sha256`, and `errors`. Each error has `code`,
`path`, and `message`. An unsafe, missing, symlinked, or wrongly named record
path is rejected rather than searched for elsewhere.

### `relion_render_map_report`

```text
relion_render_map_report(
    project_dir: str,
    map_path: str,
    threshold_sigma: float = 2.0,
    display_lowpass_A: float | None = None,
    low_percentile: float = 0.5,
    high_percentile: float = 99.5,
) -> CallToolResult
```

Annotation: `READ_ONLY`. Returns the common image-first result.

The report uses central slices and 25%-width central-slab means with
threshold-occupancy contours rather than raw maximum-intensity projections.
Its 26-neighbour connectedness metadata includes raw and significant component
counts, dominant-component voxel/excess-density fractions, and 95% coverage
counts, all measured on the downsampled rendering volume.
When `display_lowpass_A` is supplied, the report applies the same physical,
two-shell raised-cosine low-pass described above in memory before downsampling
and labels all displayed pixels and derived metrics as filtered. The MRC is
left untouched; use a separate default call for the raw view.

### `relion_render_mask_report`

```text
relion_render_mask_report(
    project_dir: str,
    map_path: str,
    mask_path: str,
    density_threshold_sigma: float = 2.0,
    low_percentile: float = 0.5,
    high_percentile: float = 99.5,
) -> CallToolResult
```

Annotation: `READ_ONLY`. Returns the common image-first result.
The map and mask must have identical shape, voxel size, origin, start indices,
and MRC axis mapping.

### `relion_render_fsc_report`

```text
relion_render_fsc_report(
    project_dir: str,
    postprocess_star: str,
) -> CallToolResult
```

Annotation: `READ_ONLY`. Returns the common image-first result.

### `relion_scan_symmetry`

```text
relion_scan_symmetry(
    project_dir: str,
    half_map_1: str,
    half_map_2: str,
    analysis_lowpass_A: float,
    mask_path: str | None = None,
    common_max_order: int = 8,
    max_order: int = 32,
    stage2_window: int = 2,
    max_candidates: int = 5,
    focus_groups: list[str] | None = None,
) -> CallToolResult
```

Annotation: `READ_ONLY`. Returns the common image-first result. `half_map_1`
and `half_map_2` are project-relative paths to independently reconstructed C1
half-maps. They must share shape, positive voxel size, origin, start indices,
and MRC axis mapping. An optional mask must share the same physical grid. The required
`analysis_lowpass_A` applies a common, in-memory analysis filter; the source
maps and mask are not modified. The tool rejects one filesystem object or
byte-identical content supplied twice, but the caller must still establish
random-half assignment provenance.

Results use schema `relion.symmetry-scan/v2`. Version 2 removes the former
scan-derived asymmetry-fraction calculation, support threshold, and report
fields.

Bounded analysis-grid resampling may require a coarser effective cutoff than
the requested value. The PNG and `analysis_contract` report both
`requested_analysis_lowpass_A` and `effective_analysis_lowpass_A`; interpret
all scores at the effective cutoff.

The bounded two-stage search first evaluates common rotational orders through
`common_max_order`. A high-order sentinel then checks for reproducible evidence
above that range through `max_order`. At most two supported anchors trigger
local Stage 2 follow-ups controlled by `stage2_window` (1–8): the strongest evidence
peak and, when available on the same per-half axis, one composite order with
supported harmonic divisors. The sentinel is a search trigger, not a symmetry
candidate or acceptance result. `max_candidates` caps the ranked baseline of the
detailed presentation shortlist, not the compact record of evaluated hypotheses.
`focus_groups` may
name bounded point-group hypotheses such as `["C4"]`. Every requested group is
evaluated and retained in `hypothesis_results`; a requested group that passes
the raw reproducibility gates is also reserved in the detailed `candidates`
output independently of presentation rank, even if a high-order control then
marks it confounded. Multiple raw-passing focus groups can therefore expand
`candidates` beyond `max_candidates` (up to the eight-group focus bound);
`result_completeness.focus_display_override_count` records that expansion.
The one-order minimum preserves neighboring prime-order coverage when a nearby
harmonic becomes the sentinel anchor.
Ambiguity is assessed from the full raw reproducibility-pass ranking, including
confounded hypotheses, before that presentation cap is applied; the search
metadata records whether detailed candidates were truncated and identifies the
two groups underlying the ambiguity check.

Read `scan_summary`, `result_completeness`, and `hypothesis_results` before
interpreting `candidates`. The `candidates` array remains available for backward
compatibility, but it is capped and contains presentation details rather than an
exhaustive outcome table. Absence from `candidates` can mean that a group was
not scheduled, was untestable, failed one or more scan heuristics, or passed the
heuristics but was omitted by the cap. It must never be used alone as evidence
that a group failed.

`scan_summary` separates raw reproducibility, order-specific support, and ranking
ambiguity from
the biological conclusion. Its safety contract records an unresolved biological
conclusion, C1 as the processing baseline, and that neither acceptance nor
rejection is permitted from the scan. `result_completeness` reports the number
of raw hypotheses passing scan heuristics before the cap, the number with valid
order-specific support, the number shown and omitted, truncation, and omitted
group names when available. For C4 and higher cyclic or dihedral groups, a raw pass is marked
`support_status=inconclusive`, `supported=false`, and
`scan_heuristic_outcome=confounded` when the C4-and-higher order spectrum cannot
run the six-order smooth-density control, shows a monotonic small-angle ramp, or
forms a uniformly high flat plateau consistent with continuous axial symmetry.
Dihedral groups must additionally meet the ordinary 0.4 half-map and cross-half
thresholds on their perpendicular C2 operations, not merely on a mean inflated
by the cyclic subgroup. The raw result remains available as
`raw_scan_heuristics_passed=true`; it must not be described as finite-order
support.
`hypothesis_results` records per-group coverage, scan-heuristic outcome,
reporting state, global rank when applicable, and individual gate diagnostics.
Use the per-group record—not shortlist membership—to describe a focused group.
The legacy `status`, `decision`, and `candidates` fields retain their version 2
types for existing clients. Exploratory reports use `decision=defer` and
`human_input_required=false`: unresolved biological symmetry does not request
human input or prevent C1 processing. The report remains `report_only` and does
not authorize or launch a job. In algorithm version 3, `candidates` is a detailed
raw reproducibility-pass presentation and may include confounded hypotheses;
use the explicit support fields instead of assuming the old supported-only
meaning.

The report is exploratory. It compares evidence between the two half-maps and
returns a bounded shortlist of point-group/axis hypotheses with diagnostic
provenance and ambiguity warnings. It does not modify a map, generate an
aligned or symmetrized reference, impose symmetry, or authorize a symmetry
choice. A group may only be described as passing or failing the scan heuristics
under the reported center, effective low-pass, mask, and search configuration.
No scan outcome accepts or rejects biological symmetry. Use the sequence C1
InitialModel, gold-standard C1 Refine3D, candidate
identification, then a matched candidate-symmetry Refine3D/PostProcess branch
with the same particles and random-half assignments. Compare the already
aligned branches with C1 as reference and inspect FSC and half-map evidence,
compatible existing local-resolution evidence when available, density lost
from C1, smeared side chains/domains, split or distorted helices, weakened
ligand density, apparent fractional occupancy, and conformations averaged
together. Defer a new LocalRes job until the final branch is selected unless
the user explicitly requests it. The report shows requested and effective
low-pass values prominently. It also reports center estimation or, for a legacy
core, that molecular-center validity was not established. The scanner assumes
the physical array center as the rotation center unless its preprocessing
diagnostics explicitly report otherwise. It locates the characteristic axes of `T`, `O`, and
abstract `I` independently in both halves and performs a coarse-to-fine twist
search.
Complete conjugated operator sets are compared before reciprocal full-group
scoring, so different equivalent polyhedral axes do not create a false
disagreement. `D2` has no unique principal axis and is returned as an unordered
orthogonal `axis_frame_xyz`; the image metadata adds the corresponding centered
`axis_frame_lines`. The scanner does not choose among RELION's `I1`–`I4`
coordinate conventions. These limitations are repeated in every result.

### `relion_compare_symmetry`

```text
relion_compare_symmetry(
    project_dir: str,
    candidate_maps: dict[str, str],
    reference_label: str | None = None,
    low_percentile: float = 0.5,
    high_percentile: float = 99.5,
) -> CallToolResult
```

Annotation: `READ_ONLY`. Returns the common image-first result. Candidate map
values are project-relative paths and must already share shape, voxel size,
origin, start indices, MRC axis mapping, and a scientifically meaningful frame.
For symmetry confirmation, set `reference_label` to the matched C1 branch. The
rendered central slices, independently percentile-scaled views, and global
similarities are a triage overview only; they do not establish matched absolute
contours or reliably inspect off-center/local side chains, domains, helices,
ligands, occupancy, or conformational averaging. Use common filtering and
contour/enclosed-volume logic plus targeted local inspection before accepting
or rejecting symmetry.

### `relion_compare_hands`

```text
relion_compare_hands(
    project_dir: str,
    map_path: str,
    opposite_hand_map: str | None = None,
    mirror_axis: Literal["x", "y", "z"] = "x",
    center_zyx: list[int] | None = None,
    box_size_pix: int | None = None,
    threshold_sigma: float = 2.0,
    low_percentile: float = 0.5,
    high_percentile: float = 99.5,
) -> CallToolResult
```

Annotation: `READ_ONLY`. Returns the common image-first result.
An explicit opposite-hand map must share the original map's shape, voxel size,
origin, start indices, and MRC axis mapping. When the tool generates the
opposite hand, it crops first and then reflects the selected region.

## Write, recovery, and status tools

### `relion_initialize_project`

```text
relion_initialize_project(
    project_dir: str,
    pipeline_name: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]
```

Annotation: `WRITE_IDEMPOTENT`.

Returns `project_dir`, `marker`, `pipeline_name`, `pipeline_file`,
`pipeline_created`, `initializer_command`, `initializer_stdout`,
`initializer_stderr`, and `metadata`.

### `relion_record_initial_model_acceptance`

```text
relion_record_initial_model_acceptance(
    project_dir: str,
    reference_volume: str,
    initial_model_particles: str,
    classification_particles: str,
    model_star: str,
    optimiser_star: str,
    data_star: str,
    evidence_artifacts: dict[str, str],
    assessment: dict[str, Any],
    geometry: dict[str, Any],
    particle_lineage: dict[str, Any],
    confirm: bool = False,
) -> dict[str, Any]
```

Annotation: `WRITE_IDEMPOTENT`.

Checks the complete [InitialModel-to-Class3D interlock](#initialmodel-to-class3d-acceptance-interlock)
contract and publishes one no-replace, content-addressed acceptance JSON under
`.relion-mcp/initial-model-acceptances/`. It requires run mode, an approved
project, and `confirm=true`. Do not create, copy, rename, or edit these records
manually.

Returns `valid=true`, `schema`, `project_dir`, both
`initial_model_acceptance` and `acceptance_record` naming the same
project-relative artifact, full `record_sha256`, and the bound
`reference_volume`, `classification_particles`, and `mask_diameter_A`. Pass
`initial_model_acceptance` unchanged to a Class3D add/run wrapper.

### `relion_record_external_reference`

```text
relion_record_external_reference(
    project_dir: str,
    reference_volume: str,
    classification_particles: str,
    mask_diameter_A: float,
    provenance: dict[str, Any],
    pose_lineage: dict[str, Any],
    confirm: bool = False,
) -> dict[str, Any]
```

Annotation: `WRITE_IDEMPOTENT`.

Checks the [external refined reference](#external-refined-references) contract
and publishes one no-replace, content-addressed `external-<digest>.json` under
`.relion-mcp/initial-model-acceptances/`. It requires run mode, an approved
project, and `confirm=true`, and it refuses `InitialModel/` outputs.

Returns `valid=true`, `schema`, `project_dir`, `initial_model_acceptance` and
`acceptance_record` naming the same project-relative artifact, `record_sha256`,
the bound `reference_volume` and `classification_particles`, the measured
`geometry` (`box_size_pix`, `pixel_size_A`, `optics_group_count`,
`mask_diameter_A`), and `pose_lineage`.

### `relion_publish_initial_model_evidence`

```text
relion_publish_initial_model_evidence(
    project_dir: str,
    evidence_kind: Literal[
        "map_multiview", "projection_comparison", "late_iteration_comparison"
    ],
    image_sha256: str,
    confirm: bool = False,
) -> dict[str, Any]
```

Annotation: `WRITE_IDEMPOTENT`.

Publishes the exact cached PNG identified by the inspected visual report's
`image.sha256`. `map_multiview` accepts a `relion_map_report` or
`relion_3d_classes` report, `projection_comparison` accepts only a
`relion_map_projection_comparison` report, and `late_iteration_comparison`
accepts a `relion_map_comparison` or `relion_3d_classes` report. The cached
report must belong to the same canonical project and still be present in the
same running MCP process. Restart, expiry, or eviction requires a new render and
inspection before publication.

The write requires run mode, an approved project, and `confirm=true`. It uses an
immutable, content-addressed, no-replace destination at
`.relion-mcp/initial-model-evidence/<evidence-kind>-<24-hex>.png`; an identical
existing artifact is accepted idempotently and conflicting bytes fail closed.
Returns `path`, full `sha256`, `size_bytes`, and `report_type`. Publication binds
the reviewed bytes but does not make any scientific claim; the caller must
still supply three distinct artifacts and a complete assessment to
`relion_record_initial_model_acceptance`.

### `relion_import_cryosparc_particles`

```text
relion_import_cryosparc_particles(
    project_dir: str,
    source_project_dir: str,
    particles_cs: str,
    passthrough_cs: list[str] | None = None,
    export_name: str = "particles",
    confirm: bool = False,
) -> dict[str, Any]
```

Annotation: `WRITE_ADDITIVE`.

Materializes the plan produced by
`relion_render_cryosparc_particle_import`. The destination must already be an
initialized RELION project: if the renderer reports `bootstrap_required=true`,
call `relion_initialize_project` first. This tool never initializes a project
implicitly and refuses to run pyem until the marker and configured pipeline
file exist. Use the same source project and relative CS inputs that passed the
renderer's route-specific inventory-or-root authorization. For projects
initialized by this MCP, the recorded project
`pipeline_name` takes precedence over the server default. Normal write gates
also require run mode, an approved
`RELION_PROJECT_ROOTS` target, and `confirm=true`.

The tool snapshots the ordered CS inputs, invokes the configured
`csparc2star.py` as an argv list in private staging, structurally validates and
rewrites modern `data_optics` and `data_particles` metadata with bounded-memory
particle-row streaming, binds every row's optics group back to its referenced
stack, and copies each source stack byte-for-byte to a collision-safe `.mrcs`
name. It publishes a new
`External/CryoSPARC/<export_name>/` directory atomically without replacement.
The bundle contains `particles.star`, `stacks/*.mrcs`, and
`conversion_manifest.json`; the manifest records source and output hashes,
pyem provenance, validation results, and the subsequent `import_particles`
handoff. No RELION pipeline job is registered by this call.

Returns the rendered plan plus `status="materialized"`, the executed argv,
`output_summary`, `manifest_sha256`, and publication confirmation, including
the actual `publication.method`. The preferred method is
`renameat2_noreplace`; filesystems such as GPFS that reject that flag use an
exclusive empty destination reservation followed by an atomic same-directory
rename. The fallback never replaces a pre-existing destination, and a process
crash leaves at most an empty fail-closed reservation rather than a readable
partial bundle. The source CS and stack files remain unchanged. An existing
bundle, changed input, failed pyem run, malformed STAR, invalid image
reference, insufficient disk space, or incomplete publication causes the
write to fail without publishing a partial bundle.

### `relion_run_job`

```text
relion_run_job(
    project_dir: str,
    operation: str,
    inputs: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    pipeline_name: str | None = None,
    alias: str | None = None,
    schedule_name: str | None = None,
    include_defaults: bool = True,
    confirm: bool = False,
    initial_model_acceptance: str | None = None,
) -> dict[str, Any]
```

Annotation: `WRITE_ADDITIVE`.

Returns `operation`, `project_dir`, `pipeline_name`, `job`,
`jobs_to_start`, `schedule_name`, `pipeline_registered=true`, `started=true`,
and nested `add_result` and `start_result` objects. For Class3D,
`initial_model_acceptance` is required and `add_result` and `start_result`
report their independently validated gate results. The argument is rejected
for non-Class3D operations.

### `relion_run_workflow_step`

```text
relion_run_workflow_step(
    project_dir: str,
    step_id: str,
    input_paths: dict[str, str] | None = None,
    param_overrides: dict[str, Any] | None = None,
    workflow_id: str = "relion_5_spa_tutorial_v1",
    pipeline_name: str | None = None,
    schedule_name: str | None = None,
    confirm: bool = False,
    alias: str | None = None,
    initial_model_acceptance: str | None = None,
) -> dict[str, Any]
```

Annotation: `WRITE_ADDITIVE`.

Returns the same run-job shape as `relion_run_job`. `alias=None` uses
`step_id`. Direct-command steps are rejected with an instruction to use their
dedicated write tool: `relion_rescale_map` or
`relion_align_symmetry_reference`. A workflow step resolving to Class3D
requires `initial_model_acceptance`; the argument is rejected for every other
resolved operation.

### `relion_run_particle_class_selection`

```text
relion_run_particle_class_selection(
    project_dir: str,
    particles_star: str,
    selected_class_numbers: list[int],
    alias: str,
    pipeline_name: str | None = None,
    schedule_name: str | None = None,
    confirm: bool = False,
    materialization_mode: str = "auto",
    class_source_star: str | None = None,
) -> dict[str, Any]
```

Annotation: `WRITE_ADDITIVE`.

Mode and class-source semantics match the planning tool. Returns the
class-selection add fields `operation`, `execution_model`,
`materialization_mode`, `requested_materialization_mode`,
`materialization_fallback_reason`,
`project_dir`, `pipeline_name`, `particles_star_input`,
`selected_class_numbers`, `selected_class_counts`, `expected_particle_count`,
`select_jobs`, `join_jobs`, `jobs_to_start`, `final_stage_id`, `final_job`,
`particles_star`, optional `auxiliary_outputs`, and `suggested_schedule_name`, updated with
`pipeline_registered=true`, `schedule_name`, `started=true`,
`next_write_tool=null`, and nested `start_result`.

### `relion_add_job`

```text
relion_add_job(
    project_dir: str,
    operation: str,
    inputs: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    pipeline_name: str | None = None,
    alias: str | None = None,
    include_defaults: bool = True,
    confirm: bool = False,
    initial_model_acceptance: str | None = None,
) -> dict[str, Any]
```

Annotation: `WRITE_ADDITIVE`.

Returns `project_dir`, `pipeline_name`, registered `job`, argv-style
`command`, truncated `stdout` and `stderr`, and `symmetry_preflight`. It does
not include `started`; the job has only been registered. For Class3D,
`initial_model_acceptance` is required and the response also contains the
validated `initial_model_acceptance_gate`. The argument is rejected for
non-Class3D operations.

### `relion_add_particle_class_selection_jobs`

```text
relion_add_particle_class_selection_jobs(
    project_dir: str,
    particles_star: str,
    selected_class_numbers: list[int],
    alias: str,
    pipeline_name: str | None = None,
    confirm: bool = False,
    materialization_mode: str = "auto",
    class_source_star: str | None = None,
) -> dict[str, Any]
```

Annotation: `WRITE_ADDITIVE`.

Mode and class-source semantics match the planning tool. Returns `operation`,
`execution_model`, `materialization_mode`, `project_dir`, `pipeline_name`,
`particles_star_input`, `selected_class_numbers`, `selected_class_counts`,
`expected_particle_count`, `select_jobs`, `join_jobs`, `jobs_to_start`,
`final_stage_id`, `final_job`, final `particles_star`, optional `auxiliary_outputs`,
`suggested_schedule_name`, `pipeline_registered=true`, `started=false`, and
`next_write_tool="relion_start_jobs"`.

### `relion_add_workflow_step`

```text
relion_add_workflow_step(
    project_dir: str,
    step_id: str,
    input_paths: dict[str, str] | None = None,
    param_overrides: dict[str, Any] | None = None,
    workflow_id: str = "relion_5_spa_tutorial_v1",
    pipeline_name: str | None = None,
    confirm: bool = False,
    alias: str | None = None,
    initial_model_acceptance: str | None = None,
) -> dict[str, Any]
```

Annotation: `WRITE_ADDITIVE`.

Returns the same add-job shape as `relion_add_job`. `alias=None` uses
`step_id`. Direct-command steps are rejected with an instruction to use their
dedicated write tool: `relion_rescale_map` or
`relion_align_symmetry_reference`. A workflow step resolving to Class3D
requires `initial_model_acceptance`; the argument is rejected for every other
resolved operation.

### `relion_start_jobs`

```text
relion_start_jobs(
    project_dir: str,
    jobs: list[str],
    schedule_name: str,
    pipeline_name: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]
```

Annotation: `WRITE_ADDITIVE`.

Returns `project_dir`, `schedule_name`, normalized `jobs`, detached launcher
`pid`, scheduler `log`, `diagnostic_log`, argv-style `command`, and
`initial_model_acceptance_gates` for any Class3D jobs. Before creating the
launcher, the tool revalidates every Class3D job against a matching live
acceptance record discovered from its registered `job.star`; there is
deliberately no caller-supplied acceptance argument. A return means the
launcher started, not that the jobs completed.

### `relion_job_status`

```text
relion_job_status(
    project_dir: str,
    job: str,
    pipeline_name: str | None = None,
    operation: str | None = None,
    expected_particle_count: int | None = None,
) -> dict[str, Any]
```

Annotation: `READ_ONLY`.

Returns normalized `job`, `job_dir`, `exists`, `marker_status`,
`pipeline_status`, `run_out`, and `run_err`. Omitting `operation`
preserves this native-status-only response.

When `operation` is supplied, the response also contains
`output_verification`, `completion_status`, and `completion_verified`.
The verifier resolves the logical operation's expected-result contract,
requires matching output edges and existing regular files, and leaves
`marker_status` and `pipeline_status` unchanged. `completion_verified=true`
requires both native statuses to be `Succeeded` and
`output_verification.output_verified=true`.

For `import_particles`, verification specifically requires the job-local
`particles.star` edge, a readable nonempty `data_particles` table, and no
movie/micrograph output edge. Set the optional positive
`expected_particle_count` to enforce an exact count (for example, `5545`);
it is rejected for other operations. A native success with wrong-mode,
missing, malformed, empty, or count-mismatched output reports
`completion_status="output_failed"` rather than rewriting RELION's native
status fields.

### `relion_job_outputs`

```text
relion_job_outputs(
    project_dir: str,
    job: str,
) -> dict[str, Any]
```

Annotation: `READ_ONLY`.

Returns normalized `job`, `job_pipeline_star`, `exists`, and `outputs`.
`outputs` is empty when the job pipeline STAR is absent; otherwise each entry
contains the classified output metadata used for downstream suggestions.

### `relion_suggest_step_inputs`

```text
relion_suggest_step_inputs(
    project_dir: str,
    step_id: str,
    prior_jobs: dict[str, str],
    workflow_id: str = "relion_5_spa_tutorial_v1",
) -> dict[str, Any]
```

Annotation: `READ_ONLY`.

Returns `step_id` and `suggestions`, keyed by input role. Suggestions identify
an external source, a missing prior binding, or a candidate from a supplied
source-step job. Candidate suggestions include `source_step`, `job`,
`expected_result_types`, `candidate`, `all_outputs`,
`requires_scientific_review=true`, and `autonomy_policy`. They are hints, not
scientific selections.

### `relion_tail_job_log`

```text
relion_tail_job_log(
    project_dir: str,
    job: str,
    stream: Literal["out", "err"] = "err",
    lines: int = 100,
) -> dict[str, Any]
```

Annotation: `READ_ONLY`.

Returns `path`, `exists`, and `text`. `lines` must be between 1 and 1000.

### `relion_abort_job`

```text
relion_abort_job(
    project_dir: str,
    job: str,
    confirm: bool = False,
) -> dict[str, Any]
```

Annotation: `WRITE_DESTRUCTIVE`.

Returns normalized `job`, `abort_marker`, and whether the cooperative abort
marker was newly `created`.

### `relion_align_symmetry_reference`

```text
relion_align_symmetry_reference(
    project_dir: str,
    input_map: str,
    output_map: str,
    target_symmetry: str,
    search_max_resolution_A: float,
    confirm: bool = False,
) -> dict[str, Any]
```

Annotation: `WRITE_ADDITIVE`.

Instantiates the rendered command template with a byte-verified private input
snapshot and randomized staging path, asks native RELION to resolve the
selected built-in point group inside that private staging directory, and then
materializes one new, full-size reference aligned to the required
`target_symmetry`. Accepted targets are C2-C64, D2-D32, T, O, and I1-I4; C1,
bare I, and custom symmetry-definition files are rejected. C4 is only an
example of an accepted call-site choice. After validating the staged transform
and MRC, the tool opens and verifies the destination directory without
following its final symlink, publishes through a directory-handle-anchored
no-replace hard link, and cleans the staging directory. The input and output
must be distinct, and the
`ReferencePrep/` output must not already exist; there is no overwrite mode.
The source map is preserved. The tool does not apply or average the target
symmetry, low-pass the output, edit particle metadata, register a pipeline job,
or start a pipeline job.

Before native execution, the tool verifies that the staging filesystem has
space for the private input snapshot and full-size staged output plus a fixed
reserve. Before publication, it re-resolves the caller's `input_map` binding;
if a symlink or path binding changed during execution, publication is refused.

The native orientation search is stochastic and independent of
`relion_scan_symmetry`. It does not consume or reproduce the scanner's
`axis_xyz` or `orientation_matrix_xyz`, and a successful command neither
accepts the target symmetry nor supplies scientific justification for a
target-symmetry refinement. A bounded matched comparison may use the existing
processing request's authority once its prerequisites pass. The
result returns the exact executed command, executable-version probe, parsed
RELION Euler angles and DIFF2, input and output hashes and grids,
publication/cleanup state, and bounded native log tails. These are provenance
for a later, task-authorized matched C1/target-symmetry validation
branch.

### `relion_remove_duplicate_particles`

```text
relion_remove_duplicate_particles(
    project_dir: str,
    particles: str,
    duplicate_distance_A: float,
    output_star: str,
    image_pixel_size_A: float | None = None,
    confirm: bool = False,
) -> dict[str, Any]
```

Annotation: `WRITE_ADDITIVE`. The operation preserves its input and can only
publish two new STAR files; it has no overwrite mode.

Runs native `relion_star_handler --remove_duplicates`, which keeps one particle
per cluster of centres closer than `duplicate_distance_A`.

Call `relion_render_remove_duplicate_particles` first with the same scientific
arguments. `image_pixel_size_A` maps to native `--image_angpix` and denotes the
pre-extraction coordinate/micrograph scale, normally recorded as
`rlnMicrographPixelSize`. It is not `rlnMicrographOriginalPixelSize` and is not
the possibly downsampled `rlnImagePixelSize`. If authoritative micrograph-scale
metadata is absent, the argument is required. The write repeats the renderer's
metadata and native-schema checks immediately before execution: all referenced
optics groups must agree on one coordinate scale, and an explicit value must
match available authoritative metadata. Optics group IDs must also be the
contiguous sequence `1..N` in table-row order so native loading cannot renumber
particle metadata. Coordinates and X/Y origins must be finite, image and
micrograph names must be present rather than STAR null markers, and
`rlnCenteredCoordinateZAngst` is rejected because this endpoint supports SPA,
not the native tomography branch. The strict native-bound STAR checks described
for the renderer—including encoding, block/loop structure, tokenization, typed
values, and physical row layout—are repeated before execution.

`output_star` must be project-relative, end in `.star`, and sit beneath the
dedicated `Deduplicate/` namespace. RELION also writes discarded rows to the
implicit sibling `<stem>_removed.star`. Both final paths are preflighted and
must be absent, non-symlink destinations distinct from the input. Native RELION
runs inside a private staging directory. The retained table and every nonempty
removed table are structurally validated. Only the removed sidecar may use
either a named `data_particles` block or the legal unnamed `data_` block written
by older RELION 5 beta builds; inputs and retained outputs still require
`data_particles`. RELION's legitimate zero-byte sidecar means zero removed
rows. The retained table must contain at least one row.
Counts plus multiplicity-sensitive digests of both `rlnImageName` and each
complete canonicalized particle row must prove that the outputs partition the
input, and a type-aware digest must prove the retained optics table is unchanged,
before either staged result is published with no-replace semantics. The removed
sidecar must not acquire an optics block. Invalid, incomplete, conflicting, or
raced outputs fail closed; private-staging cleanup is attempted and any cleanup
failure is surfaced as an error or post-publication warning. An existing
destination is never replaced.

Returns `status: "materialized"`, `input_particles`, `output_particles`,
`removed_particles_star`, the effective distance and coordinate pixel size,
the observed particle and micrograph pixel-size metadata, `input_summary`,
`output_summary`, and
`removed_summary`, `removed_particle_count`, publication/cleanup state, the
executed argv-style `command`, and bounded `stdout` and `stderr` tails.

This is a direct command: it does **not** register a pipeliner job, so the
output STAR is not a JobResult and must be bound explicitly as a path by the
next step. Deduplication chooses between coincident particles by native
`relion_star_handler` rules, not on any quality criterion, and running it after
extraction does not undo the duplicated extraction itself — apply it before any
refinement that depends on half-set independence.

### `relion_rescale_map`

```text
relion_rescale_map(
    project_dir: str,
    input_map: str,
    input_pixel_size_A: float,
    output_pixel_size_A: float,
    new_box_size_pix: int,
    output_map: str | None = None,
    output_suffix: str = "_rescaled",
    overwrite: bool = False,
    confirm: bool = False,
) -> dict[str, Any]
```

Annotation: `WRITE_DESTRUCTIVE` because `overwrite=true` may replace an
existing output.

Returns project-relative `input_map`, project-relative `output_map`,
`output_exists=true`, argv-style `command`, and truncated `stdout` and
`stderr`. The input map is never overwritten in place. With `output_map=None`,
the output name is derived from the input stem and `output_suffix`.

### `relion_validate_2d_class_review`

```text
relion_validate_2d_class_review(
    project_dir: str,
    model_star: str,
    rendered_pages: list[dict[str, Any]],
) -> dict[str, Any]
```

Annotation: `READ_ONLY`.

Checks page metadata from `relion_render_2d_classes` against the current model, its exact image references, and class-stack SHA-256 hashes. Returns `valid`, total classes, rendered/missing IDs and issues. Combine metadata from every page. An explicit stack override that differs from the model references cannot certify coverage of that model. Rendering coverage does not establish pixel inspection or scientific acceptance.

### `relion_render_particle_boxes`

```text
relion_render_particle_boxes(
    project_dir: str,
    particles_star: str,
    particle_numbers: list[int] | None = None,
    max_particles: int = 24,
    columns: int = 6,
    low_percentile: float = 1.0,
    high_percentile: float = 99.0,
) -> CallToolResult
```

Annotation: `READ_ONLY`.

Returns actual stored particle pixels without alignment, inversion or normalization. Extraction may already have normalized them. `particle_numbers` are one-based STAR row indices; otherwise samples evenly spaced rows. Reports image references, per-box pixel hashes and statistics. Display contrast is scaled per panel. Use for centering, clipping, polarity and normalization diagnosis; this bounded sample is not a dataset-wide quality estimate.

### `relion_audit_particle_selection`

```text
relion_audit_particle_selection(
    project_dir: str,
    particles_star: str,
    selected_particles_star: str | None = None,
    selected_class_numbers: list[int] | None = None,
    strata_columns: list[str] | None = None,
    numeric_bins: dict[str, list[float]] | None = None,
) -> dict[str, Any]
```

Annotation: `READ_ONLY`.

Audits the full parent table and either an exact class-number preview or a selected STAR. Reports identity/count integrity, duplicates, class counts, per-stratum attrition and source fingerprints, with `valid_for_comparison` and issues. Uses `rlnImageName` within one extraction lineage. Changed image identities after re-extraction need separate lineage mapping; do not interpret them as selection loss. Invariant metadata or half-set changes invalidate an unchanged-selection comparison.

`strata_columns` names categorical metadata; `numeric_bins` provides explicit finite increasing boundaries, e.g. `{"rlnDefocusU": [10000, 20000, 30000]}`. Marginal reports include complete attrition summaries and up to 256 groups, prioritizing depleted groups. `joint_strata` exposes combination losses such as optics × defocus; the default per-micrograph marginal is excluded from this joint report unless explicitly requested. Missing axes or more than 10,000 combinations yield an explicit unavailable result. This report supports scientific decisions; it does not accept or reject particles.

### `relion_plan_micrograph_subset`

```text
relion_plan_micrograph_subset(
    project_dir: str,
    micrographs_star: str,
    output_name: str,
    training_count: int,
    heldout_count: int,
    seed: int = 0,
    strata_columns: list[str] | None = None,
    numeric_bins: dict[str, list[float]] | None = None,
    particles_star: str | None = None,
) -> dict[str, Any]
```

Annotation: `READ_ONLY`.

Plans reproducible, disjoint training, held-out and remaining micrograph subsets. Counts are explicit; `seed` fixes reproducibility. Categorical `strata_columns` and explicit `numeric_bins` use actual metadata. Default optics-group stratification alone does not establish ice, time or view coverage. Inspect limitations when counts cannot represent all strata.

The read-only plan includes source/plan fingerprints, micrograph IDs, coverage, and proposed `External/MicrographSubsets/<output_name>/` paths. The held-out population is for independent evaluation; RELION/Topaz still has its own internal training validation split.

When curated labels already span the acquisition, supply `particles_star` to partition them by the same micrograph identities. Both inputs are fingerprinted. Unknown micrographs, duplicate particle identities, and invalid metadata prevent materialization. Check `training_labels_available` and `heldout_labels_available`; a micrograph split alone does not guarantee usable labels.

### `relion_materialize_micrograph_subset`

```text
relion_materialize_micrograph_subset(
    project_dir: str,
    micrographs_star: str,
    output_name: str,
    training_count: int,
    heldout_count: int,
    expected_plan_sha256: str,
    seed: int = 0,
    strata_columns: list[str] | None = None,
    numeric_bins: dict[str, list[float]] | None = None,
    confirm: bool = False,
    particles_star: str | None = None,
) -> dict[str, Any]
```

Annotation: `WRITE_ADDITIVE`.

Requires run mode, an approved root, `confirm=true`, and the exact `expected_plan_sha256` from planning with the same arguments. Recomputes the plan and refuses changed sources/arguments or existing destinations. Publishes new training, held-out and remaining STARs with source columns/optics preserved plus a manifest. With `particles_star`, also writes the corresponding disjoint label STARs and source snapshot, preserving full rows, particle identities, optics, coordinates, CTF and existing half sets. Use the returned `particle_outputs` paths.

Bind the training STAR explicitly to typed LoG/Topaz jobs; held-out micrographs and labels stay outside training. This creates an additive external-input bundle, not a registered native pipeline job. Inputs are never overwritten.
