from __future__ import annotations

from copy import deepcopy
from typing import Any

from .catalog import DIRECT_OPERATIONS, Catalog, CatalogError

INTERACTIVE_OPERATIONS = {"manual_picking"}


def _latest_source(steps: list[dict[str, Any]], index: int, result_types: set[str]) -> str | None:
    for previous in reversed(steps[:index]):
        if result_types.intersection(previous.get("expected_results", [])):
            return str(previous["id"])
    return None


def _explicit_override_policy(
    step: dict[str, Any],
    effective_params: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Return active explicit requirements and effective-value constraint errors."""
    policy = step.get("explicit_param_overrides")
    if policy is None:
        return [], []
    if not isinstance(policy, dict):
        raise CatalogError("Workflow explicit_param_overrides must be a mapping.")

    def required_names(value: Any, location: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item for item in value
        ):
            raise CatalogError(f"Workflow {location} must be a list of parameter names.")
        return list(value)

    required = required_names(
        policy.get("required"),
        "explicit_param_overrides.required",
    )
    conditional = policy.get("conditional", [])
    if isinstance(conditional, dict):
        conditional = [conditional]
    if not isinstance(conditional, list):
        raise CatalogError(
            "Workflow explicit_param_overrides.conditional must be a mapping or list of mappings."
        )
    for index, item in enumerate(conditional):
        location = f"explicit_param_overrides.conditional[{index}]"
        if not isinstance(item, dict):
            raise CatalogError(f"Workflow {location} must be a mapping.")
        when = item.get("when")
        if not isinstance(when, dict):
            raise CatalogError(f"Workflow {location}.when must be a mapping.")
        param = when.get("param")
        if not isinstance(param, str) or not param:
            raise CatalogError(f"Workflow {location}.when.param must be a parameter name.")
        if "equals" not in when:
            raise CatalogError(f"Workflow {location}.when must define equals.")
        conditional_required = required_names(
            item.get("required"),
            f"{location}.required",
        )
        if effective_params.get(param) == when["equals"]:
            required.extend(conditional_required)

    required = list(dict.fromkeys(required))
    constraint_errors: list[str] = []
    constraints = policy.get("constraints", [])
    if isinstance(constraints, dict):
        constraints = [constraints]
    if not isinstance(constraints, list):
        raise CatalogError(
            "Workflow explicit_param_overrides.constraints must be a mapping or list of mappings."
        )
    for index, constraint in enumerate(constraints):
        location = f"explicit_param_overrides.constraints[{index}]"
        if not isinstance(constraint, dict):
            raise CatalogError(f"Workflow {location} must be a mapping.")
        param = constraint.get("param")
        if not isinstance(param, str) or not param:
            raise CatalogError(f"Workflow {location}.param must be a parameter name.")
        if "equals" not in constraint:
            raise CatalogError(f"Workflow {location} must define equals.")
        message = constraint.get("message")
        if message is not None and (not isinstance(message, str) or not message):
            raise CatalogError(f"Workflow {location}.message must be a non-empty string.")
        expected = constraint["equals"]
        if param not in effective_params and param in required:
            # Defer the constraint until the required dataset-specific override
            # is bound; planning must remain possible for an unresolved step.
            continue
        if param not in effective_params:
            constraint_errors.append(
                message or f"parameter {param!r} must equal {expected!r}, but it is unset."
            )
        elif effective_params[param] != expected:
            constraint_errors.append(
                message
                or (
                    f"parameter {param!r} must equal {expected!r}; "
                    f"got {effective_params[param]!r}."
                )
            )
    return required, constraint_errors


def resolve_workflow_step_params(
    step: dict[str, Any],
    param_overrides: dict[str, Any] | None = None,
    *,
    require_explicit: bool = False,
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Resolve effective parameters and enforce dataset-bound override provenance."""
    overrides = dict(param_overrides or {})
    params = dict(step.get("params") or {})
    params.update(overrides)
    required, constraint_errors = _explicit_override_policy(step, params)
    missing = sorted(name for name in required if name not in overrides)
    if require_explicit and (missing or constraint_errors):
        step_id = str(step.get("id", "<unknown>"))
        details: list[str] = []
        if missing:
            details.append(f"missing explicit parameter overrides: {missing}")
        if constraint_errors:
            details.append("constraint violations: " + "; ".join(constraint_errors))
        raise CatalogError(
            f"Workflow step {step_id!r} is not execution-ready; " + "; ".join(details)
        )
    return params, missing, constraint_errors


def _only_unresolved_explicit_values_are_missing(
    error: CatalogError,
    missing_explicit_overrides: list[str],
) -> bool:
    """Identify a render failure caused solely by intentionally unbound parameters."""
    message = str(error)
    marker = "Missing required values for "
    if not message.startswith(marker) or ":" not in message:
        return False
    missing_values = {
        item.strip().split(".")[-1]
        for item in message.rsplit(":", 1)[1].split(",")
        if item.strip()
    }
    return bool(missing_values) and missing_values.issubset(
        set(missing_explicit_overrides)
    )


def workflow_step_validation_params(
    step: dict[str, Any],
    params: dict[str, Any],
    missing_explicit_overrides: list[str],
) -> dict[str, Any]:
    """Fill only unresolved fields with non-executable examples for validation."""
    examples = step.get("tutorial_example_params") or {}
    if not isinstance(examples, dict):
        raise CatalogError("Workflow tutorial_example_params must be a mapping.")
    validation_params = dict(params)
    while True:
        required, _ = _explicit_override_policy(step, validation_params)
        added = False
        for name in required:
            if name not in validation_params and name in examples:
                validation_params[name] = examples[name]
                added = True
        if not added:
            break
    return validation_params


def validate_workflow(
    catalog: Catalog,
    workflow_id: str,
    param_overrides: dict[str, dict[str, Any]] | None = None,
    include_opt_in_steps: list[str] | None = None,
) -> dict[str, Any]:
    workflow = deepcopy(catalog.workflow(workflow_id))
    overrides = param_overrides or {}
    issues: list[dict[str, str]] = []
    plan: list[dict[str, Any]] = []
    declared_steps = workflow.get("steps", [])
    declared_step_ids = {str(step["id"]) for step in declared_steps}
    unknown_override_steps = sorted(set(overrides) - declared_step_ids)
    if unknown_override_steps:
        raise CatalogError(f"Unknown workflow override steps: {unknown_override_steps}")
    requested_opt_in_steps = set(include_opt_in_steps or [])
    unknown_opt_in_steps = sorted(requested_opt_in_steps - declared_step_ids)
    if unknown_opt_in_steps:
        raise CatalogError(f"Unknown workflow opt-in steps: {unknown_opt_in_steps}")

    available_opt_in_steps: list[str] = []
    steps: list[dict[str, Any]] = []
    skipped_steps: list[dict[str, str]] = []
    for step in declared_steps:
        step_id = str(step["id"])
        enabled_by_default = step.get("enabled_by_default", True)
        if not isinstance(enabled_by_default, bool):
            raise CatalogError(f"Workflow step {step_id!r} enabled_by_default must be a boolean.")
        if not enabled_by_default:
            available_opt_in_steps.append(step_id)
        if enabled_by_default or step_id in requested_opt_in_steps:
            steps.append(step)
            continue
        skipped_steps.append(
            {
                "step_id": step_id,
                "operation": str(step["operation"]),
                "reason": "disabled_by_default",
                "activation_policy": str(step.get("activation_policy", "explicit_opt_in")),
            }
        )

    for index, step in enumerate(steps):
        step_id = str(step["id"])
        requested = str(step["operation"])
        resolved = catalog.resolve_operation(requested)
        params, missing_explicit_overrides, constraint_errors = resolve_workflow_step_params(
            step,
            overrides.get(step_id),
        )
        validation_params = workflow_step_validation_params(
            step,
            params,
            missing_explicit_overrides,
        )
        dependencies: dict[str, dict[str, Any]] = {}

        if missing_explicit_overrides:
            issues.append(
                {
                    "level": "warning",
                    "step_id": step_id,
                    "message": (
                        "Dataset-specific explicit parameter overrides are required before "
                        "rendering or execution; tutorial_example_params are validation-only: "
                        f"{missing_explicit_overrides}"
                    ),
                }
            )
        for message in constraint_errors:
            issues.append(
                {
                    "level": "error",
                    "step_id": step_id,
                    "message": f"Explicit parameter constraint violated: {message}",
                }
            )

        for role, input_spec in (step.get("inputs") or {}).items():
            if input_spec.get("kind") == "external_input":
                dependencies[role] = {"source": "external", "result_types": input_spec.get("result_types", [])}
                continue
            result_types = set(input_spec.get("result_types") or [])
            source = _latest_source(steps, index, result_types)
            dependencies[role] = {
                "source": source or "unresolved",
                "result_types": sorted(result_types),
            }
            if source is None and input_spec.get("required", True):
                issues.append(
                    {
                        "level": "error",
                        "step_id": step_id,
                        "message": f"No previous step produces required input {role}: {sorted(result_types)}",
                    }
                )

        if resolved in DIRECT_OPERATIONS:
            support = "direct_command"
            executable = True
            job_type = catalog.describe_operation(resolved).get("job_type")
            try:
                validated_params = catalog.validate_direct_params(
                    resolved,
                    validation_params,
                )
                if not missing_explicit_overrides:
                    params = validated_params
            except CatalogError as exc:
                if not _only_unresolved_explicit_values_are_missing(
                    exc,
                    missing_explicit_overrides,
                ):
                    issues.append(
                        {
                            "level": "error",
                            "step_id": step_id,
                            "message": f"Step cannot be rendered: {exc}",
                        }
                    )
        else:
            try:
                description = catalog.describe_operation(resolved)
                support = str(description["support_level"])
                executable = bool(description["executable"])
                job_type = description.get("job_type")
                mapping = catalog.mapping(resolved)
                unknown = set(params) - set(mapping.get("params") or {})
                required = {
                    name
                    for name, spec in (mapping.get("params") or {}).items()
                    if spec.get("required") and "default" not in spec
                }
                missing = required - set(params) - set(missing_explicit_overrides)
                if unknown:
                    issues.append(
                        {
                            "level": "error",
                            "step_id": step_id,
                            "message": f"Unknown mapped parameters: {sorted(unknown)}",
                        }
                    )
                if missing:
                    issues.append(
                        {
                            "level": "error",
                            "step_id": step_id,
                            "message": f"Missing required mapped parameters: {sorted(missing)}",
                        }
                    )
                mapped_inputs = set(mapping.get("inputs") or {})
                unknown_inputs = set(step.get("inputs") or {}) - mapped_inputs
                if unknown_inputs:
                    issues.append(
                        {
                            "level": "error",
                            "step_id": step_id,
                            "message": f"Unknown mapped inputs: {sorted(unknown_inputs)}",
                        }
                    )
                validation_inputs = {
                    role: f"Validation/{step_id}/{role}.star"
                    for role in (step.get("inputs") or {})
                }
                try:
                    catalog.render_job(resolved, validation_inputs, validation_params)
                except CatalogError as exc:
                    if not _only_unresolved_explicit_values_are_missing(
                        exc,
                        missing_explicit_overrides,
                    ):
                        issues.append(
                            {
                                "level": "error",
                                "step_id": step_id,
                                "message": f"Step cannot be rendered: {exc}",
                            }
                        )
            except (CatalogError, KeyError, TypeError, ValueError) as exc:
                support = "unmapped"
                executable = False
                job_type = None
                issues.append({"level": "error", "step_id": step_id, "message": str(exc)})

        if not executable:
            issues.append(
                {
                    "level": "warning",
                    "step_id": step_id,
                    "message": f"Step is planning-only with support_level={support}.",
                }
            )

        interactive = resolved in INTERACTIVE_OPERATIONS
        step_has_errors = any(
            item["level"] == "error" and item.get("step_id") == step_id
            for item in issues
        )
        plan.append(
            {
                "index": index + 1,
                "step_id": step_id,
                "requested_operation": requested,
                "operation": resolved,
                "job_type": job_type,
                "support_level": support,
                "executable": executable,
                "execution_ready": (
                    executable and not missing_explicit_overrides and not step_has_errors
                ),
                "missing_explicit_param_overrides": missing_explicit_overrides,
                "interactive_or_selection_pause": interactive,
                "optional": bool(step.get("optional", False)),
                "enabled_by_default": step.get("enabled_by_default", True),
                "activation_policy": step.get("activation_policy"),
                "quality_checkpoint": step.get("quality_checkpoint"),
                "launch_gate": step.get("launch_gate"),
                "dependencies": dependencies,
                "params": params,
                "warnings": step.get("warnings", []),
                "expected_results": step.get("expected_results", []),
            }
        )

    errors = [item for item in issues if item["level"] == "error"]
    return {
        "workflow_id": workflow_id,
        "description": workflow.get("description"),
        "relion_version": catalog.relion_version,
        "authorization_required": workflow.get("authorization_required", True),
        "authorization_source": workflow.get("authorization_source", "user_task_request"),
        "requires_human_approval": workflow.get("requires_human_approval", False),
        "valid_for_planning": not errors,
        "valid_for_full_execution": not errors and all(
            step["execution_ready"] for step in plan
        ),
        "declared_step_count": len(declared_steps),
        "step_count": len(plan),
        "available_opt_in_steps": available_opt_in_steps,
        "included_opt_in_steps": sorted(requested_opt_in_steps.intersection(available_opt_in_steps)),
        "skipped_steps": skipped_steps,
        "issues": issues,
        "plan": plan,
        "parameter_dependency_rules": workflow.get("parameter_dependency_rules", []),
    }


def workflow_step(catalog: Catalog, workflow_id: str, step_id: str) -> dict[str, Any]:
    workflow = catalog.workflow(workflow_id)
    for step in workflow.get("steps", []):
        if step.get("id") == step_id:
            return deepcopy(step)
    raise CatalogError(f"Unknown workflow step {step_id!r} in workflow {workflow_id!r}.")
