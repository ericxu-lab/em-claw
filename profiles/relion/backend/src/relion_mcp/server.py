from __future__ import annotations

import json
import logging
import sys
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Image
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from . import __version__
from .assessment import validate_3d_assessment
from .catalog import (
    DIRECT_OPERATION_TOOL_NAMES,
    DIRECT_OPERATIONS,
    Catalog,
    CatalogError,
    safe_alias,
)
from .challenge import VisualChallengeStore
from .cryosparc_import import (
    cryosparc_import_environment,
    import_cryosparc_particles,
    render_cryosparc_particle_import,
)
from .initial_model_acceptance import (
    record_external_reference as _record_external_reference,
)
from .initial_model_acceptance import (
    record_initial_model_acceptance as _record_initial_model_acceptance,
)
from .initial_model_acceptance import (
    validate_initial_model_acceptance as _validate_initial_model_acceptance,
)
from .outputs import best_output
from .particle_audit import (
    audit_particle_selection,
    materialize_stratified_micrograph_subset,
    plan_stratified_micrograph_subset,
)
from .runner import RelionRunner
from .settings import Settings
from .visual_evidence import (
    publish_visual_evidence,
    register_visual_evidence_if_eligible,
)
from .visuals import (
    RenderedVisual,
    metadata_text,
    render_2d_class_pages,
    render_2d_classes,
    render_3d_classes,
    render_fsc_report,
    render_hand_comparison,
    render_map_comparison,
    render_map_projection_comparison,
    render_map_report,
    render_mask_report,
    render_particle_boxes,
    render_pick_overlays,
    render_smoke_test,
    render_symmetry_comparison,
    render_symmetry_scan,
    validate_2d_class_review,
)
from .workflow import (
    resolve_workflow_step_params,
    validate_workflow,
    workflow_step,
    workflow_step_validation_params,
)

CATALOG = Catalog()
SETTINGS = Settings.from_env()
RUNNER = RelionRunner(CATALOG, SETTINGS)

INSTRUCTIONS = """RELION 5 planning, execution, and visual-analysis server backed by bundled contracts. Start with relion_environment and relion_validate_workflow, and render every job before execution. For CryoSPARC particles destined for Class3D, discover the project and job from the active CryoSPARC environment, then render relion_render_cryosparc_particle_import with its canonical source project and selected relative .cs outputs. The combined route authorizes exact live project directories at MCP startup; a RELION-only route requires RELION_CRYOSPARC_IMPORT_ROOTS. If bootstrap_required is true, call relion_initialize_project with the returned arguments, render again, then call relion_import_cryosparc_particles and register its returned import_particles handoff. Tutorial example parameters are non-executable: resolve every missing_explicit_param_overrides entry from authoritative metadata or recorded derivation and require execution_ready=true. Final post-Class3D re-extraction uses an explicitly derived even box at native sampling with rescale_particles=false. Manual picking is explicit opt-in; its absence is not a human gate. LocalRes is also disabled by default: run it once after selecting the best/final deliverable map, or earlier only on an explicit user request; do not fan it out across candidate branches. Prefer relion_run_job, relion_run_workflow_step, and relion_run_particle_class_selection so registration and launch share one task-authorized write. A user request to process, run, continue, or optimize authorizes routine additive work within scope; record finite budgets and stop conditions without requiring separate segment approval. Preserve that authorization on resume unless revoked or superseded. Set confirm=true and approval_flag=true for covered actions. Continue monitoring and processing through the requested outcome; a job launch or checkpoint is not a reason to end the turn or ask whether to continue. Resolve routine choices autonomously. Medium-confidence primary_fallback may carry one credible assessable primary through successive downstream stages with checkpoint assessments, preserved alternatives, and recorded uncertainty, except that it may not cross the InitialModel-to-Class3D launch gate. Before recording InitialModel acceptance, use relion_compare_map_projections with explicit representative classes and one physical low-pass, directly inspect at least three credible distinct 2D appearances, and never infer acceptance from NCC alone. Use relion_publish_initial_model_evidence with the returned image.sha256 to publish the exact inspected projection-comparison PNG, as well as the exact inspected map-multiview and late-iteration reports. Then use relion_record_initial_model_acceptance to persist a high-confidence SEED_READY decision bound to the exact reference, Class3D particle state, geometry, completed InitialModel artifacts, lineage, and required evidence; pass the returned initial_model_acceptance path to the add/run call. Class3D registration and recovery starts fail closed when that record is absent, stale, incomplete, or mismatched. A refined consensus map from outside RELION, such as a CryoSPARC refinement, is not an InitialModel seed: bind it with relion_record_external_reference (hash-bound reference and particles, measured box and pixel agreement, provenance, and pose lineage) and pass its returned path the same way; no-alignment Class3D additionally requires pose_lineage poses_from_reference_refinement, and InitialModel outputs cannot use this route. Never bypass this gate with native relion_pipeliner, the scheduler helper, or another shell path; add and launch Class3D only through the guarded MCP tools. Use defer for unresolved science and reserve human_gate/await_human for essential unrecoverable input or necessary authority the task lacks. Validation errors require caller repair, not automatic user input. Evidence-based additive symmetry and focused/no-alignment experiments are covered by the processing task when baselines, scientific prerequisites, explicit user choices, and budgets are respected. Rebalance stage allocations within a finite overall budget; at diminishing returns or hard limits, return the best available result with limitations. Final scientific acceptance and rejection still require high confidence. Planning-only requests remain read-only. Add-only and start-only tools are recovery interfaces. Writes require RELION_MCP_MODE=run, confirm=true, and an approved RELION_PROJECT_ROOTS path. Visual reports contain image-first PNG content plus provenance text; verify pixel access with the smoke-test pair before the first scientific visual decision after the MCP process, SDK, Codex client, or model changes. relion_scan_symmetry is exploratory: its candidates array is capped, absence from that array is never a failure verdict, a raw C4-or-higher pass is not order-specific support when the high-order control is unavailable, monotonic, or a uniformly high plateau, and Dn also requires reproducible perpendicular-C2 operations; no scan result accepts or rejects biological symmetry. Inspect scan_summary, result_completeness, and hypothesis_results, keep C1 as the baseline, and require matched branch evidence. Use relion_compare_maps for quantitative pairwise density claims: fit one transform at a declared physical low-pass, freeze it across comparison cutoffs, and accept only aligned or identity_optimal registration status; every other status makes the relation unassessable. Matching MRC headers alone is not alignment, and correlation alone does not infer same or distinct biology. ModelAngelo is planning-only and DynaMight is manifest-only in the bundled mappings."""

mcp = MCPServer("relion", version=__version__, instructions=INSTRUCTIONS)
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE_ADDITIVE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)
WRITE_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
WRITE_DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=False,
)

_SMOKE_CHALLENGES = VisualChallengeStore()


def _pipeline_name(value: str | None) -> str:
    return SETTINGS.pipeline_name if value is None else value


def _visual_tool_result(rendered: RenderedVisual) -> CallToolResult:
    """Return image-first model-visible content without structuredContent.

    Codex consumes the ImageContent block as an image input. The trailing text block
    carries source paths, display transforms, and compact metrics that must remain
    visible to the model. Deliberately omit structured_content so rich content is not
    shadowed by clients that prefer structured output.
    """
    register_visual_evidence_if_eligible(rendered.png, rendered.metadata)
    return CallToolResult(
        content=[
            Image(data=rendered.png, format="png").to_image_content(),
            TextContent(type="text", text=metadata_text(rendered)),
        ]
    )


def _workflow_step_values(
    step_id: str,
    workflow_id: str,
    param_overrides: dict[str, Any] | None,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    step = workflow_step(CATALOG, workflow_id, step_id)
    resolved = CATALOG.resolve_operation(str(step["operation"]))
    params, missing, _ = resolve_workflow_step_params(
        step,
        param_overrides,
    )
    validation_params = workflow_step_validation_params(step, params, missing)
    if resolved in DIRECT_OPERATIONS:
        CATALOG.validate_direct_params(resolved, validation_params)
    else:
        validation_inputs = {
            role: f"Validation/{step_id}/{role}.star"
            for role in (step.get("inputs") or {})
        }
        CATALOG.render_job(resolved, validation_inputs, validation_params)
    resolve_workflow_step_params(step, param_overrides, require_explicit=True)
    return step, resolved, params


@mcp.resource("relion://workflow/relion_5_spa_tutorial_v1")
def tutorial_resource() -> str:
    """The bundled RELION 5 SPA tutorial workflow YAML."""
    path = CATALOG.config_dir / "relion_5_spa_tutorial_v1.yaml"
    return path.read_text(encoding="utf-8")


@mcp.resource("relion://operation/{operation}")
def operation_resource(operation: str) -> str:
    """A resolved operation definition with native RELION labels."""
    return json.dumps(CATALOG.describe_operation(operation), indent=2)


@mcp.tool(title="Check RELION environment", annotations=READ_ONLY)
def relion_environment(project_dir: str | None = None) -> dict[str, Any]:
    """Check executable availability, server mode, allowed roots and optional project state."""
    result = RUNNER.environment(project_dir)
    result["cryosparc_particle_import"] = cryosparc_import_environment(SETTINGS)
    return result


@mcp.tool(title="List RELION operations", annotations=READ_ONLY)
def relion_list_operations(support_level: str | None = None) -> list[dict[str, Any]]:
    """List mapped RELION operations and their execution support levels."""
    return CATALOG.list_operations(support_level)


@mcp.tool(title="Describe RELION operation", annotations=READ_ONLY)
def relion_describe_operation(operation: str) -> dict[str, Any]:
    """Describe one operation, including inputs, parameters, defaults and native GUI labels."""
    return CATALOG.describe_operation(operation)


@mcp.tool(title="Validate RELION tutorial", annotations=READ_ONLY)
def relion_validate_workflow(
    workflow_id: str = "relion_5_spa_tutorial_v1",
    param_overrides: dict[str, dict[str, Any]] | None = None,
    include_opt_in_steps: list[str] | None = None,
) -> dict[str, Any]:
    """Validate and dependency-plan a workflow, optionally including named opt-in steps."""
    result = validate_workflow(
        CATALOG,
        workflow_id,
        param_overrides,
        include_opt_in_steps=include_opt_in_steps,
    )
    result["catalog_issues"] = CATALOG.validate_catalog()
    return result


@mcp.tool(title="Render RELION job", annotations=READ_ONLY)
def relion_render_job(
    operation: str,
    inputs: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    pipeline_name: str | None = None,
    alias: str | None = None,
    include_defaults: bool = True,
) -> dict[str, Any]:
    """Translate an operation without registering or starting a RELION job."""
    rendered = CATALOG.render_job(operation, inputs, params, include_defaults=include_defaults)
    symmetry_preflight = RUNNER.preflight_symmetry(rendered)
    pipeline = _pipeline_name(pipeline_name)
    command = RUNNER.render_command(rendered, pipeline, safe_alias(alias))
    return {
        "requested_operation": rendered.requested_operation,
        "operation": rendered.operation,
        "job_type": rendered.relion_job_type,
        "support_level": rendered.support_level,
        "executable": rendered.support_level == "schedule_mappable",
        "options_by_manifest_key": rendered.options_by_key,
        "option_assignments": rendered.option_assignments,
        "command": command,
        "symmetry_preflight": symmetry_preflight,
        "launch_gate": (
            "initial_model_acceptance"
            if rendered.operation == "three_d_classification"
            or rendered.relion_job_type == "Class3D"
            else None
        ),
    }


@mcp.tool(title="Render tutorial step", annotations=READ_ONLY)
def relion_render_workflow_step(
    step_id: str,
    input_paths: dict[str, str] | None = None,
    param_overrides: dict[str, Any] | None = None,
    workflow_id: str = "relion_5_spa_tutorial_v1",
    pipeline_name: str | None = None,
    alias: str | None = None,
) -> dict[str, Any]:
    """Render one tutorial step using explicit selected input paths and optional parameter overrides."""
    step, resolved, params = _workflow_step_values(step_id, workflow_id, param_overrides)
    requested = str(step["operation"])
    if resolved == "align_symmetry_reference":
        raise CatalogError(
            "Use relion_render_symmetry_alignment with an explicit project and "
            "ReferencePrep/ output for this direct-command workflow step."
        )
    if resolved == "remove_duplicate_particles":
        raise CatalogError(
            "Use relion_render_remove_duplicate_particles with an explicit project, "
            "particles input, and Deduplicate/ output for this direct-command workflow step."
        )
    if resolved == "rescale_map":
        params = CATALOG.validate_direct_params(resolved, params)
        return {
            "step_id": step_id,
            "operation": resolved,
            "support_level": "direct_command",
            "params": params,
            "required_input": "input_map",
            "command_template": [
                SETTINGS.image_handler_executable,
                "--i",
                "<input_map>",
                "--angpix",
                str(params.get("input_pixel_size_A")),
                "--rescale_angpix",
                str(params.get("output_pixel_size_A")),
                "--new_box",
                str(params.get("new_box_size_pix")),
                "--o",
                "<derived_or_explicit_output_map>",
            ],
        }
    rendered = CATALOG.render_job(resolved, input_paths, params)
    symmetry_preflight = RUNNER.preflight_symmetry(rendered)
    pipeline = _pipeline_name(pipeline_name)
    return {
        "step_id": step_id,
        "requested_operation": requested,
        "operation": rendered.operation,
        "job_type": rendered.relion_job_type,
        "support_level": rendered.support_level,
        "input_paths": input_paths or {},
        "params": params,
        "option_assignments": rendered.option_assignments,
        "command": RUNNER.render_command(
            rendered,
            pipeline,
            safe_alias(step_id if alias is None else alias),
        ),
        "launch_gate": step.get("launch_gate"),
        "symmetry_preflight": symmetry_preflight,
        "quality_checkpoint": step.get("quality_checkpoint"),
        "warnings": step.get("warnings", []),
    }


@mcp.tool(title="Plan exact particle class selection", annotations=READ_ONLY)
def relion_plan_particle_class_selection(
    project_dir: str,
    particles_star: str,
    selected_class_numbers: list[int],
    alias: str,
    pipeline_name: str | None = None,
    materialization_mode: str = "auto",
    class_source_star: str | None = None,
) -> dict[str, Any]:
    """Render one GUI-equivalent selection job, with native Select/JoinStar fallback."""
    return RUNNER.plan_particle_class_selection(
        project_dir,
        particles_star,
        selected_class_numbers,
        alias,
        _pipeline_name(pipeline_name),
        materialization_mode,
        class_source_star,
    )


@mcp.tool(title="Create Codex image-content challenge", annotations=READ_ONLY)
def relion_visual_smoke_test() -> CallToolResult:
    """Attach a randomized PNG challenge whose tokens exist only in the image pixels."""
    challenge_id, labels = _SMOKE_CHALLENGES.create()
    return _visual_tool_result(
        render_smoke_test(SETTINGS, challenge_id=challenge_id, labels=labels)
    )


@mcp.tool(title="Verify Codex image-content challenge", annotations=READ_ONLY)
def relion_verify_visual_smoke_test(
    challenge_id: str,
    labels: dict[str, str],
) -> dict[str, Any]:
    """Verify tokens read from the smoke-test image without disclosing the expected tokens."""
    return _SMOKE_CHALLENGES.verify(challenge_id, labels)


@mcp.tool(title="Render RELION 2D class contact sheet", annotations=READ_ONLY)
def relion_render_2d_classes(
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
) -> CallToolResult:
    """Attach all requested classes as bounded PNG sheets and report source-bound coverage."""
    renderer = render_2d_class_pages if all_classes else render_2d_classes
    rendered = renderer(
        SETTINGS,
        project_dir,
        model_star,
        classes_mrcs=classes_mrcs,
        selected_class_numbers=selected_class_numbers,
        max_classes=max_classes,
        columns=columns,
        sort_by=sort_by,
        low_percentile=low_percentile,
        high_percentile=high_percentile,
    )
    if isinstance(rendered, RenderedVisual):
        return _visual_tool_result(rendered)
    return CallToolResult(content=[item for page in rendered for item in _visual_tool_result(page).content])


@mcp.tool(title="Validate RELION 2D rendering coverage", annotations=READ_ONLY)
def relion_validate_2d_class_review(
    project_dir: str,
    model_star: str,
    rendered_pages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate complete class rendering against current source hashes; scientific review remains separate."""
    return validate_2d_class_review(SETTINGS, project_dir, model_star, rendered_pages)


@mcp.tool(title="Compare map projections with RELION 2D classes", annotations=READ_ONLY)
def relion_compare_map_projections(
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
) -> CallToolResult:
    """Attach explicit 2D classes beside independently fitted synthetic map projections."""
    return _visual_tool_result(
        render_map_projection_comparison(
            SETTINGS,
            project_dir,
            map_path,
            model_star,
            selected_class_numbers,
            comparison_lowpass_A,
            particle_diameter_A,
            classes_mrcs=classes_mrcs,
            direction_count=direction_count,
            inplane_step_deg=inplane_step_deg,
            max_shift_A=max_shift_A,
            invert_class_contrast=invert_class_contrast,
        )
    )


@mcp.tool(title="Render RELION particle boxes", annotations=READ_ONLY)
def relion_render_particle_boxes(
    project_dir: str,
    particles_star: str,
    particle_numbers: list[int] | None = None,
    max_particles: int = 24,
    columns: int = 6,
    low_percentile: float = 1.0,
    high_percentile: float = 99.0,
) -> CallToolResult:
    """Attach actual stored particle boxes and source pixel statistics for extraction diagnosis."""
    return _visual_tool_result(render_particle_boxes(
        SETTINGS, project_dir, particles_star, particle_numbers=particle_numbers,
        max_particles=max_particles, columns=columns,
        low_percentile=low_percentile, high_percentile=high_percentile,
    ))


@mcp.tool(title="Audit RELION particle selection", annotations=READ_ONLY)
def relion_audit_particle_selection(
    project_dir: str,
    particles_star: str,
    selected_particles_star: str | None = None,
    selected_class_numbers: list[int] | None = None,
    strata_columns: list[str] | None = None,
    numeric_bins: dict[str, list[float]] | None = None,
) -> dict[str, Any]:
    """Measure exact particle retention, identity integrity and attrition by acquisition stratum."""
    return audit_particle_selection(
        SETTINGS, project_dir, particles_star, selected_particles_star=selected_particles_star,
        selected_class_numbers=selected_class_numbers, strata_columns=strata_columns,
        numeric_bins=numeric_bins,
    )


@mcp.tool(title="Plan stratified RELION micrograph subsets", annotations=READ_ONLY)
def relion_plan_micrograph_subset(
    project_dir: str,
    micrographs_star: str,
    output_name: str,
    training_count: int,
    heldout_count: int,
    seed: int = 0,
    strata_columns: list[str] | None = None,
    numeric_bins: dict[str, list[float]] | None = None,
    particles_star: str | None = None,
) -> dict[str, Any]:
    """Plan reproducible, disjoint training and held-out micrographs with explicit stratum coverage."""
    return plan_stratified_micrograph_subset(
        SETTINGS, project_dir, micrographs_star, output_name=output_name,
        training_count=training_count, heldout_count=heldout_count, seed=seed,
        strata_columns=strata_columns, numeric_bins=numeric_bins,
        particles_star=particles_star,
    )


@mcp.tool(title="Materialize stratified RELION micrograph subsets", annotations=WRITE_ADDITIVE)
def relion_materialize_micrograph_subset(
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
) -> dict[str, Any]:
    """Publish new training/held-out STAR files from an unchanged reviewed plan, without overwriting."""
    return materialize_stratified_micrograph_subset(
        SETTINGS, project_dir, micrographs_star, output_name=output_name,
        training_count=training_count, heldout_count=heldout_count, seed=seed,
        strata_columns=strata_columns, numeric_bins=numeric_bins,
        expected_plan_sha256=expected_plan_sha256, confirm=confirm,
        particles_star=particles_star,
    )


@mcp.tool(title="Render RELION/Topaz pick overlays", annotations=READ_ONLY)
def relion_render_pick_overlays(
    project_dir: str,
    particle_diameter_pix: float,
    pairs: list[dict[str, str]] | None = None,
    coordinate_index_star: str | None = None,
    fom_threshold: float | None = None,
    max_micrographs: int = 9,
    columns: int = 3,
    low_percentile: float = 1.0,
    high_percentile: float = 99.0,
) -> CallToolResult:
    """Attach representative micrographs with particle-coordinate circles and pick diagnostics."""
    return _visual_tool_result(
        render_pick_overlays(
            SETTINGS,
            project_dir,
            pairs=pairs,
            coordinate_index_star=coordinate_index_star,
            particle_diameter_pix=particle_diameter_pix,
            fom_threshold=fom_threshold,
            max_micrographs=max_micrographs,
            columns=columns,
            low_percentile=low_percentile,
            high_percentile=high_percentile,
        )
    )


@mcp.tool(title="Render RELION 3D class report", annotations=READ_ONLY)
def relion_render_3d_classes(
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
) -> CallToolResult:
    """Attach orthogonal slices and projections for aligned 3D classes plus RELION metadata."""
    return _visual_tool_result(
        render_3d_classes(
            SETTINGS,
            project_dir,
            model_star,
            class_maps=class_maps,
            selected_class_numbers=selected_class_numbers,
            max_classes=max_classes,
            sort_by=sort_by,
            include_mip=include_mip,
            display_lowpass_A=display_lowpass_A,
            low_percentile=low_percentile,
            high_percentile=high_percentile,
        )
    )


@mcp.tool(title="Align and compare RELION maps", annotations=READ_ONLY)
def relion_compare_maps(
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
) -> CallToolResult:
    """Fit one bounded local transform, then compare maps at fixed physical low-passes."""
    return _visual_tool_result(
        render_map_comparison(
            SETTINGS,
            project_dir,
            reference_map,
            candidate_map,
            reference_label,
            candidate_label,
            alignment_lowpass_A,
            comparison_lowpasses_A=comparison_lowpasses_A,
            mask_path=mask_path,
            alignment_mode=alignment_mode,
            max_shift_A=max_shift_A,
            max_rotation_deg=max_rotation_deg,
            threshold_sigma=threshold_sigma,
            low_percentile=low_percentile,
            high_percentile=high_percentile,
        )
    )


@mcp.tool(title="Validate RELION 3D assessment", annotations=READ_ONLY)
def relion_validate_3d_assessment(
    classes: list[dict[str, Any]],
    pairs: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    decision: str,
    confidence: str,
    next_action: str,
    approval_flag: bool,
    rationale: str = "",
) -> dict[str, Any]:
    """Validate structured 3D-class evidence and enforce scientific decision guards."""
    return validate_3d_assessment(
        classes=classes,
        pairs=pairs,
        evidence=evidence,
        decision=decision,
        confidence=confidence,
        next_action=next_action,
        approval_flag=approval_flag,
        rationale=rationale,
    )


@mcp.tool(title="Validate InitialModel acceptance", annotations=READ_ONLY)
def relion_validate_initial_model_acceptance(
    project_dir: str,
    acceptance_record: str,
    reference_volume: str | None = None,
    classification_particles: str | None = None,
    mask_diameter_A: float | None = None,
    alignment: bool | None = None,
) -> dict[str, Any]:
    """Validate a hash-bound InitialModel or external-reference record and optional Class3D bindings."""
    return _validate_initial_model_acceptance(
        SETTINGS,
        project_dir,
        acceptance_record,
        reference_volume=reference_volume,
        classification_particles=classification_particles,
        mask_diameter_A=mask_diameter_A,
        alignment=alignment,
    )


@mcp.tool(title="Record external refined reference", annotations=WRITE_IDEMPOTENT)
def relion_record_external_reference(
    project_dir: str,
    reference_volume: str,
    classification_particles: str,
    mask_diameter_A: float,
    provenance: dict[str, Any],
    pose_lineage: dict[str, Any],
    confirm: bool = False,
) -> dict[str, Any]:
    """Record a hash-bound refined map from outside RELION as an admissible Class3D reference."""
    return _record_external_reference(
        SETTINGS,
        project_dir,
        reference_volume,
        classification_particles,
        mask_diameter_A,
        provenance,
        pose_lineage,
        confirm,
    )


@mcp.tool(title="Record InitialModel acceptance", annotations=WRITE_IDEMPOTENT)
def relion_record_initial_model_acceptance(
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
) -> dict[str, Any]:
    """Record a high-confidence, evidence-complete InitialModel gate for Class3D."""
    return _record_initial_model_acceptance(
        SETTINGS,
        project_dir,
        reference_volume,
        initial_model_particles,
        classification_particles,
        model_star,
        optimiser_star,
        data_star,
        evidence_artifacts,
        assessment,
        geometry,
        particle_lineage,
        confirm,
    )


@mcp.tool(
    title="Publish inspected InitialModel visual evidence",
    annotations=WRITE_IDEMPOTENT,
)
def relion_publish_initial_model_evidence(
    project_dir: str,
    evidence_kind: Literal[
        "map_multiview", "projection_comparison", "late_iteration_comparison"
    ],
    image_sha256: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Publish the exact cached PNG inspected from an eligible visual report."""
    return publish_visual_evidence(
        SETTINGS,
        project_dir,
        evidence_kind,
        image_sha256,
        confirm=confirm,
    )


@mcp.tool(title="Render RELION map report", annotations=READ_ONLY)
def relion_render_map_report(
    project_dir: str,
    map_path: str,
    threshold_sigma: float = 2.0,
    display_lowpass_A: float | None = None,
    low_percentile: float = 0.5,
    high_percentile: float = 99.5,
) -> CallToolResult:
    """Attach central slices, projections, threshold surface, histogram, and map provenance."""
    return _visual_tool_result(
        render_map_report(
            SETTINGS,
            project_dir,
            map_path,
            threshold_sigma=threshold_sigma,
            display_lowpass_A=display_lowpass_A,
            low_percentile=low_percentile,
            high_percentile=high_percentile,
        )
    )


@mcp.tool(title="Render RELION mask report", annotations=READ_ONLY)
def relion_render_mask_report(
    project_dir: str,
    map_path: str,
    mask_path: str,
    density_threshold_sigma: float = 2.0,
    low_percentile: float = 0.5,
    high_percentile: float = 99.5,
) -> CallToolResult:
    """Attach map/mask overlays and clipping, soft-edge, component, and clearance diagnostics."""
    return _visual_tool_result(
        render_mask_report(
            SETTINGS,
            project_dir,
            map_path,
            mask_path,
            density_threshold_sigma=density_threshold_sigma,
            low_percentile=low_percentile,
            high_percentile=high_percentile,
        )
    )


@mcp.tool(title="Render RELION FSC report", annotations=READ_ONLY)
def relion_render_fsc_report(project_dir: str, postprocess_star: str) -> CallToolResult:
    """Attach masked, unmasked, corrected, and phase-randomised FSC curves with estimates."""
    return _visual_tool_result(render_fsc_report(SETTINGS, project_dir, postprocess_star))


@mcp.tool(title="Scan C1 half-map symmetry", annotations=READ_ONLY)
def relion_scan_symmetry(
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
) -> CallToolResult:
    """Return exploratory scan-heuristic evidence from independent C1 half-maps.

    ``candidates`` is a capped presentation shortlist. A group's absence from
    it is never a failure verdict; inspect ``scan_summary``,
    ``result_completeness``, and ``hypothesis_results``. ``focus_groups``
    forces requested hypotheses to be evaluated and keeps their outcomes
    visible. For C4 and higher, a missing, monotonic, or uniformly high flat
    high-order control makes a raw heuristic pass confounded rather than
    order-specific support. Dn additionally requires its perpendicular C2
    operations to pass the half-map and cross-half thresholds. The scan
    cannot accept or reject biological symmetry; matched refinement branches
    are required.
    """
    return _visual_tool_result(
        render_symmetry_scan(
            SETTINGS,
            project_dir,
            half_map_1,
            half_map_2,
            analysis_lowpass_A,
            mask_path=mask_path,
            common_max_order=common_max_order,
            max_order=max_order,
            stage2_window=stage2_window,
            max_candidates=max_candidates,
            focus_groups=focus_groups,
        )
    )


@mcp.tool(title="Compare symmetry-branch maps", annotations=READ_ONLY)
def relion_compare_symmetry(
    project_dir: str,
    candidate_maps: dict[str, str],
    reference_label: str | None = None,
    low_percentile: float = 0.5,
    high_percentile: float = 99.5,
) -> CallToolResult:
    """Attach matched slices and difference maps for already aligned C1/symmetry branch outputs."""
    return _visual_tool_result(
        render_symmetry_comparison(
            SETTINGS,
            project_dir,
            candidate_maps,
            reference_label=reference_label,
            low_percentile=low_percentile,
            high_percentile=high_percentile,
        )
    )


@mcp.tool(title="Compare map hands", annotations=READ_ONLY)
def relion_compare_hands(
    project_dir: str,
    map_path: str,
    opposite_hand_map: str | None = None,
    mirror_axis: Literal["x", "y", "z"] = "x",
    center_zyx: list[int] | None = None,
    box_size_pix: int | None = None,
    threshold_sigma: float = 2.0,
    low_percentile: float = 0.5,
    high_percentile: float = 99.5,
) -> CallToolResult:
    """Attach original/opposite-hand map views; absolute-hand assignment still needs independent evidence."""
    return _visual_tool_result(
        render_hand_comparison(
            SETTINGS,
            project_dir,
            map_path,
            opposite_hand_map=opposite_hand_map,
            mirror_axis=mirror_axis,
            center_zyx=center_zyx,
            box_size_pix=box_size_pix,
            threshold_sigma=threshold_sigma,
            low_percentile=low_percentile,
            high_percentile=high_percentile,
        )
    )


@mcp.tool(title="Initialize RELION project", annotations=WRITE_IDEMPOTENT)
def relion_initialize_project(
    project_dir: str,
    pipeline_name: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Create a RELION project marker and MCP metadata inside an approved project root."""
    return RUNNER.initialize_project(
        project_dir,
        _pipeline_name(pipeline_name),
        confirm,
    )


@mcp.tool(title="Render CryoSPARC particle import", annotations=READ_ONLY)
def relion_render_cryosparc_particle_import(
    project_dir: str,
    source_project_dir: str,
    particles_cs: str,
    passthrough_cs: list[str] | None = None,
    export_name: str = "particles",
) -> dict[str, Any]:
    """Render a row-preserving CryoSPARC-particle handoff for RELION Class3D."""
    return render_cryosparc_particle_import(
        SETTINGS,
        project_dir,
        source_project_dir,
        particles_cs,
        passthrough_cs,
        export_name,
    )


@mcp.tool(title="Import CryoSPARC particles", annotations=WRITE_ADDITIVE)
def relion_import_cryosparc_particles(
    project_dir: str,
    source_project_dir: str,
    particles_cs: str,
    passthrough_cs: list[str] | None = None,
    export_name: str = "particles",
    confirm: bool = False,
) -> dict[str, Any]:
    """Copy CryoSPARC stacks and convert metadata into a new RELION import bundle."""
    return import_cryosparc_particles(
        SETTINGS,
        project_dir,
        source_project_dir,
        particles_cs,
        passthrough_cs,
        export_name,
        confirm,
    )


@mcp.tool(title="Register and run RELION job", annotations=WRITE_ADDITIVE)
def relion_run_job(
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
) -> dict[str, Any]:
    """Register and start one rendered job in a single approved MCP write."""
    rendered = CATALOG.render_job(operation, inputs, params, include_defaults=include_defaults)
    return RUNNER.run_job(
        project_dir,
        rendered,
        pipeline_name=_pipeline_name(pipeline_name),
        alias=alias,
        schedule_name=schedule_name,
        confirm=confirm,
        initial_model_acceptance=initial_model_acceptance,
    )


@mcp.tool(title="Register and run tutorial step", annotations=WRITE_ADDITIVE)
def relion_run_workflow_step(
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
) -> dict[str, Any]:
    """Register and start one pipeliner-mappable tutorial step in one approved write."""
    _, resolved, params = _workflow_step_values(step_id, workflow_id, param_overrides)
    if resolved in DIRECT_OPERATIONS:
        tool_name = DIRECT_OPERATION_TOOL_NAMES[resolved]
        raise CatalogError(f"Use {tool_name} for this direct-command workflow step.")
    rendered = CATALOG.render_job(resolved, input_paths, params)
    return RUNNER.run_job(
        project_dir,
        rendered,
        pipeline_name=_pipeline_name(pipeline_name),
        alias=step_id if alias is None else alias,
        schedule_name=schedule_name,
        confirm=confirm,
        initial_model_acceptance=initial_model_acceptance,
    )


@mcp.tool(
    title="Register and run exact particle class selection",
    annotations=WRITE_ADDITIVE,
)
def relion_run_particle_class_selection(
    project_dir: str,
    particles_star: str,
    selected_class_numbers: list[int],
    alias: str,
    pipeline_name: str | None = None,
    schedule_name: str | None = None,
    confirm: bool = False,
    materialization_mode: str = "auto",
    class_source_star: str | None = None,
) -> dict[str, Any]:
    """Register and start exact GUI-equivalent class selection in one approved write."""
    return RUNNER.run_particle_class_selection(
        project_dir,
        particles_star,
        selected_class_numbers,
        alias,
        _pipeline_name(pipeline_name),
        schedule_name,
        confirm,
        materialization_mode,
        class_source_star,
    )


@mcp.tool(title="Add RELION job", annotations=WRITE_ADDITIVE)
def relion_add_job(
    project_dir: str,
    operation: str,
    inputs: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    pipeline_name: str | None = None,
    alias: str | None = None,
    include_defaults: bool = True,
    confirm: bool = False,
    initial_model_acceptance: str | None = None,
) -> dict[str, Any]:
    """Advanced/recovery API: register one job without starting it."""
    rendered = CATALOG.render_job(operation, inputs, params, include_defaults=include_defaults)
    return RUNNER.add_job(
        project_dir,
        rendered,
        pipeline_name=_pipeline_name(pipeline_name),
        alias=alias,
        confirm=confirm,
        initial_model_acceptance=initial_model_acceptance,
    )


@mcp.tool(title="Add exact particle class-selection jobs", annotations=WRITE_ADDITIVE)
def relion_add_particle_class_selection_jobs(
    project_dir: str,
    particles_star: str,
    selected_class_numbers: list[int],
    alias: str,
    pipeline_name: str | None = None,
    confirm: bool = False,
    materialization_mode: str = "auto",
    class_source_star: str | None = None,
) -> dict[str, Any]:
    """Advanced/recovery API: register exact-class materialization without starting it."""
    return RUNNER.add_particle_class_selection_jobs(
        project_dir,
        particles_star,
        selected_class_numbers,
        alias,
        _pipeline_name(pipeline_name),
        confirm,
        materialization_mode,
        class_source_star,
    )


@mcp.tool(title="Add tutorial step", annotations=WRITE_ADDITIVE)
def relion_add_workflow_step(
    project_dir: str,
    step_id: str,
    input_paths: dict[str, str] | None = None,
    param_overrides: dict[str, Any] | None = None,
    workflow_id: str = "relion_5_spa_tutorial_v1",
    pipeline_name: str | None = None,
    confirm: bool = False,
    alias: str | None = None,
    initial_model_acceptance: str | None = None,
) -> dict[str, Any]:
    """Advanced/recovery API: register one tutorial step without starting it."""
    _, resolved, params = _workflow_step_values(step_id, workflow_id, param_overrides)
    if resolved in DIRECT_OPERATIONS:
        tool_name = DIRECT_OPERATION_TOOL_NAMES[resolved]
        raise CatalogError(f"Use {tool_name} for this direct-command workflow step.")
    rendered = CATALOG.render_job(resolved, input_paths, params)
    return RUNNER.add_job(
        project_dir,
        rendered,
        pipeline_name=_pipeline_name(pipeline_name),
        alias=step_id if alias is None else alias,
        confirm=confirm,
        initial_model_acceptance=initial_model_acceptance,
    )


@mcp.tool(title="Start RELION jobs", annotations=WRITE_ADDITIVE)
def relion_start_jobs(
    project_dir: str,
    jobs: list[str],
    schedule_name: str,
    pipeline_name: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Recovery API: start one or more jobs that are already registered."""
    return RUNNER.start_jobs(
        project_dir,
        jobs,
        schedule_name,
        _pipeline_name(pipeline_name),
        confirm,
    )


@mcp.tool(title="Check RELION job status", annotations=READ_ONLY)
def relion_job_status(
    project_dir: str,
    job: str,
    pipeline_name: str | None = None,
    operation: str | None = None,
    expected_particle_count: int | None = None,
) -> dict[str, Any]:
    """Read native status and optionally verify operation-specific outputs."""
    return RUNNER.job_status(
        project_dir,
        job,
        _pipeline_name(pipeline_name),
        operation,
        expected_particle_count,
    )


@mcp.tool(title="Inspect RELION job outputs", annotations=READ_ONLY)
def relion_job_outputs(project_dir: str, job: str) -> dict[str, Any]:
    """Read job_pipeline.star and classify likely semantic outputs for downstream steps."""
    return RUNNER.job_outputs(project_dir, job)


@mcp.tool(title="Suggest tutorial step inputs", annotations=READ_ONLY)
def relion_suggest_step_inputs(
    project_dir: str,
    step_id: str,
    prior_jobs: dict[str, str],
    workflow_id: str = "relion_5_spa_tutorial_v1",
) -> dict[str, Any]:
    """Suggest candidate paths from explicit prior step-to-job bindings; never makes scientific selections."""
    workflow_plan = validate_workflow(CATALOG, workflow_id, include_opt_in_steps=[step_id])
    plan_step = next((item for item in workflow_plan["plan"] if item["step_id"] == step_id), None)
    if plan_step is None:
        raise CatalogError(f"Unknown workflow step {step_id!r}.")
    suggestions: dict[str, Any] = {}
    for role, dependency in plan_step["dependencies"].items():
        source = dependency["source"]
        if source == "external":
            suggestions[role] = {"source": "external", "candidate": None}
            continue
        job = prior_jobs.get(source)
        if not job:
            suggestions[role] = {"source_step": source, "candidate": None, "reason": "No prior job binding supplied."}
            continue
        output_info = RUNNER.job_outputs(project_dir, job)
        paths = [item["path"] for item in output_info["outputs"]]
        candidate = best_output(paths, dependency["result_types"])
        suggestions[role] = {
            "source_step": source,
            "job": job,
            "expected_result_types": dependency["result_types"],
            "candidate": candidate,
            "all_outputs": output_info["outputs"],
            "requires_scientific_review": True,
            "autonomy_policy": "Accept or reject only from high-confidence visual and structured evidence. A medium-confidence credible primary may feed successive downstream stages with alternatives and uncertainty preserved. Use defer for unresolved science, diagnose or use a fallback, and continue the requested workflow. Ask only for an essential blocker that cannot be resolved and prevents useful progress or a useful deliverable.",
        }
    return {"step_id": step_id, "suggestions": suggestions}


@mcp.tool(title="Tail RELION job log", annotations=READ_ONLY)
def relion_tail_job_log(
    project_dir: str,
    job: str,
    stream: Literal["out", "err"] = "err",
    lines: int = 100,
) -> dict[str, Any]:
    """Return the tail of run.out or run.err for one job."""
    return RUNNER.tail_job_log(project_dir, job, stream, lines)


@mcp.tool(title="Render RELION symmetry alignment", annotations=READ_ONLY)
def relion_render_symmetry_alignment(
    project_dir: str,
    input_map: str,
    output_map: str,
    target_symmetry: str,
    search_max_resolution_A: float,
) -> dict[str, Any]:
    """Render a bounded native point-group alignment without writing."""
    return RUNNER.render_symmetry_alignment(
        project_dir,
        input_map,
        output_map,
        target_symmetry,
        search_max_resolution_A,
    )


@mcp.tool(title="Materialize symmetry-aligned RELION reference", annotations=WRITE_ADDITIVE)
def relion_align_symmetry_reference(
    project_dir: str,
    input_map: str,
    output_map: str,
    target_symmetry: str,
    search_max_resolution_A: float,
    confirm: bool = False,
) -> dict[str, Any]:
    """Create one new unsymmetrized point-group-aligned reference with native RELION."""
    return RUNNER.align_symmetry_reference(
        project_dir,
        input_map,
        output_map,
        target_symmetry,
        search_max_resolution_A,
        confirm,
    )


@mcp.tool(title="Abort RELION job", annotations=WRITE_DESTRUCTIVE)
def relion_abort_job(project_dir: str, job: str, confirm: bool = False) -> dict[str, Any]:
    """Request a cooperative RELION abort by creating RELION_JOB_ABORT_NOW."""
    return RUNNER.abort_job(project_dir, job, confirm)


@mcp.tool(title="Render duplicate-particle removal", annotations=READ_ONLY)
def relion_render_remove_duplicate_particles(
    project_dir: str,
    particles: str,
    duplicate_distance_A: float,
    output_star: str,
    image_pixel_size_A: float | None = None,
) -> dict[str, Any]:
    """Preflight native duplicate removal and resolve its command and output paths without writing."""
    return RUNNER.render_remove_duplicate_particles(
        project_dir,
        particles,
        duplicate_distance_A,
        output_star,
        image_pixel_size_A,
    )


@mcp.tool(title="Remove duplicate particles", annotations=WRITE_ADDITIVE)
def relion_remove_duplicate_particles(
    project_dir: str,
    particles: str,
    duplicate_distance_A: float,
    output_star: str,
    image_pixel_size_A: float | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Remove duplicated particles within an explicit distance with native relion_star_handler."""
    return RUNNER.remove_duplicate_particles(
        project_dir,
        particles,
        duplicate_distance_A,
        output_star,
        image_pixel_size_A,
        confirm,
    )


@mcp.tool(title="Rescale RELION map", annotations=WRITE_DESTRUCTIVE)
def relion_rescale_map(
    project_dir: str,
    input_map: str,
    input_pixel_size_A: float,
    output_pixel_size_A: float,
    new_box_size_pix: int,
    output_map: str | None = None,
    output_suffix: str = "_rescaled",
    overwrite: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    """Run an explicitly parameterized relion_image_handler map rescale/rebox operation."""
    return RUNNER.rescale_map(
        project_dir,
        input_map,
        input_pixel_size_A,
        output_pixel_size_A,
        new_box_size_pix,
        output_map,
        output_suffix,
        overwrite,
        confirm,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
