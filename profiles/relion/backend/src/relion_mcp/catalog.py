from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml


class CatalogError(ValueError):
    """Raised when workflow or mapping data is invalid."""


EXECUTABLE_SUPPORT_LEVELS = {"schedule_mappable"}
DIRECT_OPERATION_JOB_TYPES = {
    "align_symmetry_reference": "relion_align_symmetry",
    "remove_duplicate_particles": "relion_star_handler",
    "rescale_map": "relion_image_handler",
}
DIRECT_OPERATION_TOOL_NAMES = {
    "align_symmetry_reference": "relion_align_symmetry_reference",
    "remove_duplicate_particles": "relion_remove_duplicate_particles",
    "rescale_map": "relion_rescale_map",
}
DIRECT_OPERATION_RENDER_TOOL_NAMES = {
    "align_symmetry_reference": "relion_render_symmetry_alignment",
    "remove_duplicate_particles": "relion_render_remove_duplicate_particles",
}
DIRECT_OPERATIONS = frozenset(DIRECT_OPERATION_JOB_TYPES)
SYMMETRY_ALIGNMENT_MAX_OPERATORS = 64


def _load_yaml(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise CatalogError(f"Expected a YAML mapping in {path}")
    return loaded


def safe_project_relative(value: str) -> str:
    """Validate a RELION project-relative path or glob without resolving it."""
    if not isinstance(value, str) or not value.strip():
        raise CatalogError("RELION path values must be non-empty strings.")
    if "\x00" in value or "\n" in value or "\r" in value or ";" in value:
        raise CatalogError("RELION path values may not contain control characters or semicolons.")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise CatalogError(f"RELION inputs must be project-relative: {value}")
    return value


def safe_executable(value: str) -> str:
    """Validate a bare executable name or an absolute POSIX executable path."""
    if not isinstance(value, str) or not value.strip():
        raise CatalogError("RELION executable values must be non-empty strings.")
    if any(token in value for token in ("\x00", "\n", "\r", ";", " == ")):
        raise CatalogError(
            "RELION executable values may not contain control characters, semicolons, or option delimiters."
        )
    if not re.fullmatch(r"[A-Za-z0-9_./+:-]+", value):
        raise CatalogError(
            "RELION executable values may contain only letters, digits, '_', '.', '/', '+', ':', and '-'."
        )
    path = PurePosixPath(value)
    if ".." in path.parts:
        raise CatalogError("RELION executable paths may not contain '..'.")
    if len(path.parts) > 1 and not path.is_absolute():
        raise CatalogError("RELION executables must be a bare command name or an absolute path.")
    return value


def safe_absolute_runtime_path(value: str, *, allow_empty: bool = False) -> str:
    """Validate an absolute runtime path without resolving it on the MCP host.

    Runtime paths may refer to node-local storage that exists only on the
    eventual execution host, so validation is lexical.  Keep the accepted
    character set deliberately narrow because RELION later embeds native job
    options in generated shell commands.
    """
    if not isinstance(value, str):
        raise CatalogError("RELION runtime paths must be strings.")
    if value == "" and allow_empty:
        return value
    if not value.strip():
        raise CatalogError("RELION runtime paths must be non-empty unless empty is allowed.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise CatalogError("RELION runtime paths may not contain control characters.")
    if ";" in value or " == " in value:
        raise CatalogError("RELION runtime paths may not contain RELION option delimiters.")

    path = PurePosixPath(value)
    if not path.is_absolute():
        raise CatalogError("RELION runtime paths must be absolute POSIX paths.")
    if len(path.parts) <= 1:
        raise CatalogError("RELION runtime paths may not name the filesystem root.")
    if any(part in {".", ".."} for part in value.split("/")):
        raise CatalogError("RELION runtime paths may not contain '.' or '..' path segments.")
    if path.name == "relion_volatile":
        raise CatalogError(
            "RELION scratch paths must name the parent base, not relion_volatile itself."
        )
    if not re.fullmatch(r"/[A-Za-z0-9_./+:-]+", value):
        raise CatalogError(
            "RELION runtime paths may contain only letters, digits, '_', '.', '/', '+', ':', and '-'."
        )
    return value


def safe_alias(value: str | None) -> str | None:
    if value is None:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
        raise CatalogError("Job aliases may contain only letters, digits, '.', '_' and '-'.")
    return value


def safe_symmetry(value: str) -> str:
    """Validate a supported RELION point-group symmetry identifier."""
    pattern = r"(?:C1|[CD](?:[2-9]|[1-9][0-9]+)|[IOT])"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise CatalogError(
            "RELION symmetry values must be C1, Cn or Dn (n >= 2), I, O, or T."
        )
    return value


def safe_alignment_symmetry(value: str) -> str:
    """Validate a bounded proper point group accepted by relion_align_symmetry."""
    pattern = r"(?:[CD](?:[2-9]|[1-9][0-9])|T|O|I[1-4])"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise CatalogError(
            "RELION alignment symmetry must be canonical uppercase Cn or Dn (n >= 2), "
            "T, O, or an explicit icosahedral convention I1-I4."
        )
    ordered_group = re.fullmatch(r"([CD])([1-9][0-9]*)", value)
    operator_count = (
        int(ordered_group.group(2))
        * (2 if ordered_group.group(1) == "D" else 1)
        if ordered_group is not None
        else {"T": 12, "O": 24}.get(value, 60)
    )
    if operator_count > SYMMETRY_ALIGNMENT_MAX_OPERATORS:
        raise CatalogError(
            "RELION alignment symmetry exceeds the bounded limit of "
            f"{SYMMETRY_ALIGNMENT_MAX_OPERATORS} symmetry operators."
        )
    return value


def _as_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise CatalogError(f"{name} must be a boolean.")


def _validate_type(value: Any, expected: str | None, name: str) -> Any:
    if expected in {None, "str", "enum", "path", "executable"}:
        if not isinstance(value, str):
            raise CatalogError(f"{name} must be a string.")
        if expected == "path":
            return safe_project_relative(value)
        if expected == "executable":
            return safe_executable(value)
        return value
    if expected == "absolute_runtime_path":
        try:
            return safe_absolute_runtime_path(value, allow_empty=True)
        except CatalogError as exc:
            raise CatalogError(f"{name}: {exc}") from exc
    if expected == "symmetry":
        try:
            return safe_symmetry(value)
        except CatalogError as exc:
            raise CatalogError(f"{name}: {exc}") from exc
    if expected == "alignment_symmetry":
        try:
            return safe_alignment_symmetry(value)
        except CatalogError as exc:
            raise CatalogError(f"{name}: {exc}") from exc
    if expected == "bool":
        return _as_bool(value, name)
    if expected == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise CatalogError(f"{name} must be an integer.")
        return value
    if expected == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CatalogError(f"{name} must be numeric.")
        try:
            result = float(value)
        except OverflowError as exc:
            raise CatalogError(f"{name} must be finite.") from exc
        if not math.isfinite(result):
            raise CatalogError(f"{name} must be finite.")
        return result
    raise CatalogError(f"Unsupported mapping type {expected!r} for {name}.")


def _format_value(value: Any, fmt: str | None, name: str) -> str:
    if fmt == "yes_no":
        formatted = "Yes" if _as_bool(value, name) else "No"
    elif fmt == "gain_rotation_radio":
        labels = {
            0: "No rotation (0)",
            1: "90 degrees (1)",
            2: "180 degrees (2)",
            3: "270 degrees (3)",
        }
        if value not in labels:
            raise CatalogError(f"{name} must be one of 0, 1, 2, 3.")
        formatted = labels[value]
    elif fmt == "gain_flip_radio":
        labels = {
            0: "No flipping (0)",
            1: "Flip upside down (1)",
            2: "Flip left to right (2)",
        }
        if value not in labels:
            raise CatalogError(f"{name} must be one of 0, 1, 2.")
        formatted = labels[value]
    else:
        formatted = "Yes" if isinstance(value, bool) and value else "No" if isinstance(value, bool) else str(value)
    if ";" in formatted or " == " in formatted or "\n" in formatted or "\r" in formatted or "\x00" in formatted:
        raise CatalogError(f"{name} may not contain control characters or RELION option delimiters.")
    return formatted


def _validate_fixed_option_value(
    value: Any,
    spec: dict[str, Any],
    native_spec: dict[str, Any],
    name: str,
) -> Any:
    """Validate an invariant mapping value against its declared and native types."""
    if "type" in spec:
        value = _validate_type(value, spec.get("type"), name)

    option_type = native_spec.get("option_type")
    if option_type == "boolean":
        value = _as_bool(value, name)
    elif option_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CatalogError(
                f"{name} must be numeric for its RELION manifest option."
            )
        if not math.isfinite(float(value)):
            raise CatalogError(f"{name} must be finite.")
    elif option_type in {"filename", "radio", "string"}:
        if not isinstance(value, str):
            raise CatalogError(
                f"{name} must be a string for its RELION manifest option."
            )
    elif isinstance(value, (dict, list, set, tuple)):
        raise CatalogError(f"{name} must be a scalar value.")

    # Formatting also validates boolean/radio adapters and rejects option-string
    # delimiters before the fixed value can enter a rendered assignment.
    _format_value(value, spec.get("format"), name)
    return value


def _validate_option_combination(operation: str, options: dict[str, str]) -> None:
    """Validate native option relationships that cannot be expressed in YAML types."""
    if operation == "log_autopicking" and float(options["log_diam_min"]) > float(options["log_diam_max"]):
        raise CatalogError("log_diameter_min_A may not exceed log_diameter_max_A.")

    if operation == "extract_particles":
        box_size = int(options["extract_size"])
        if box_size % 2:
            raise CatalogError("Particle extraction box_size_pix must be even.")

        rescale_enabled = options.get("do_rescale") == "Yes"
        rescaled_size = options.get("rescale")
        if rescale_enabled and rescaled_size is None:
            raise CatalogError(
                "Particle extraction with rescale_particles=true requires "
                "rescaled_size_pix."
            )
        if not rescale_enabled and rescaled_size is not None:
            raise CatalogError(
                "rescaled_size_pix must be omitted when rescale_particles=false."
            )
        if rescaled_size is not None:
            parsed_rescaled_size = int(rescaled_size)
            if parsed_rescaled_size % 2:
                raise CatalogError("Particle rescaled_size_pix must be even.")
            if parsed_rescaled_size > box_size:
                raise CatalogError(
                    "Particle rescaled_size_pix may not exceed box_size_pix; "
                    "upsampling does not restore information."
                )

        background_diameter = options.get("bg_diameter")
        if background_diameter is not None and int(background_diameter) >= box_size:
            raise CatalogError(
                "Particle background_diameter_pix must be smaller than box_size_pix."
            )

    if operation == "topaz_picking" and options.get("use_gpu") == "Yes":
        mpi_count = int(options.get("nr_mpi", "1"))
        gpu_ids = options.get("gpu_ids", "")
        if mpi_count > 1 and gpu_ids and gpu_ids[0] in "0123456789":
            gpu_groups = gpu_ids.split(":")
            if len(gpu_groups) != mpi_count or any(
                not group or group[0] not in "0123456789" for group in gpu_groups
            ):
                raise CatalogError(
                    "Topaz picking requires one colon-delimited GPU assignment "
                    "per MPI rank, or an empty gpu_ids value for automatic allocation."
                )

    if (
        operation == "refine3d"
        and options.get("do_solvent_fsc") == "Yes"
        and not options.get("fn_mask")
    ):
        raise CatalogError(
            "Solvent-corrected FSC requires a reference mask when "
            "solvent_correct_fsc=true."
        )

    if operation == "particle_selection":
        value_keys = {"select_label", "select_minval", "select_maxval"}
        metadata_enabled = options.get("do_select_values") == "Yes"
        if metadata_enabled or value_keys.intersection(options):
            if not metadata_enabled:
                raise CatalogError(
                    "Metadata selection values require select_by_metadata_values=true."
                )
            required = {"fn_data", *value_keys}
            missing = sorted(required - set(options))
            if missing:
                raise CatalogError(
                    f"Metadata particle selection is missing native options: {missing}"
                )
            if "fn_model" in options or options.get("do_class_ranker") == "Yes":
                raise CatalogError(
                    "Metadata particle selection cannot be combined with optimiser/class-ranker selection."
                )
            if not re.fullmatch(r"rln[A-Za-z0-9_]+", options["select_label"]):
                raise CatalogError("selection_label must be a safe RELION rln metadata label.")
            if int(options["select_minval"]) > int(options["select_maxval"]):
                raise CatalogError("selection_minimum may not exceed selection_maximum.")

    if operation == "join_star":
        modes = {
            "particles": ("do_part", tuple(f"fn_part{index}" for index in range(1, 5))),
            "micrographs": ("do_mic", tuple(f"fn_mic{index}" for index in range(1, 5))),
            "movies": ("do_mov", tuple(f"fn_mov{index}" for index in range(1, 5))),
        }
        enabled = [name for name, (flag, _) in modes.items() if options.get(flag) == "Yes"]
        if len(enabled) != 1:
            raise CatalogError("JoinStar requires exactly one enabled combine mode.")
        active = enabled[0]
        active_keys = set(modes[active][1])
        active_inputs = [key for key in modes[active][1] if key in options]
        if not 2 <= len(active_inputs) <= 4:
            raise CatalogError(f"JoinStar {active} mode requires two to four inputs.")
        expected_prefix = list(modes[active][1][: len(active_inputs)])
        if active_inputs != expected_prefix:
            raise CatalogError(f"JoinStar {active} inputs must be contiguous starting at input 1.")
        input_values = [options[key] for key in active_inputs]
        if len(input_values) != len(set(input_values)):
            raise CatalogError("JoinStar input paths must be distinct.")
        inactive_inputs = sorted(
            key
            for name, (_, keys) in modes.items()
            if name != active
            for key in keys
            if key in options and key not in active_keys
        )
        if inactive_inputs:
            raise CatalogError(
                f"JoinStar inputs do not match the enabled {active} mode: {inactive_inputs}"
            )


@dataclass(frozen=True)
class RenderedJob:
    requested_operation: str
    operation: str
    relion_job_type: str
    support_level: str
    options_by_key: dict[str, str]
    option_assignments: list[str]

    @property
    def option_string(self) -> str:
        return ";".join(self.option_assignments) + (";" if self.option_assignments else "")


class Catalog:
    def __init__(self, config_dir: Path | None = None):
        self.config_dir = config_dir or Path(__file__).with_name("configs")
        self.neutral = _load_yaml(self.config_dir / "operations.yaml")["operations"]
        self.manifest_document = _load_yaml(self.config_dir / "relion_operation_manifest.yaml")
        self.manifest = self.manifest_document["job_types"]
        self.mappings_document = _load_yaml(self.config_dir / "relion_pipeliner_mappings.yaml")
        self.mappings = self.mappings_document["operations"]
        self.aliases = self.mappings_document.get("operation_aliases", {})
        self.workflows = {
            "relion_5_spa_tutorial_v1": _load_yaml(self.config_dir / "relion_5_spa_tutorial_v1.yaml")
        }

    @property
    def relion_version(self) -> str:
        return str(self.mappings_document.get("relion_version", "unknown"))

    def resolve_operation(self, name: str) -> str:
        return self.aliases.get(name, name)

    def mapping(self, name: str) -> dict[str, Any]:
        resolved = self.resolve_operation(name)
        if resolved not in self.mappings:
            if resolved in DIRECT_OPERATION_TOOL_NAMES:
                tool_name = DIRECT_OPERATION_RENDER_TOOL_NAMES.get(
                    resolved,
                    DIRECT_OPERATION_TOOL_NAMES[resolved],
                )
                raise CatalogError(
                    f"Operation {name!r} is a direct command, not a pipeliner job; "
                    f"use {tool_name}."
                )
            raise CatalogError(f"No RELION pipeliner mapping exists for operation {name!r}.")
        return self.mappings[resolved]

    def manifest_options(self, job_type: str) -> dict[str, dict[str, Any]]:
        if job_type not in self.manifest:
            raise CatalogError(f"Unknown RELION manifest job type {job_type!r}.")
        return {item["key"]: item for item in self.manifest[job_type].get("options", [])}

    def _validated_fixed_options(
        self,
        operation: str,
        mapping: dict[str, Any],
        native: dict[str, dict[str, Any]],
    ) -> tuple[
        dict[str, tuple[Any, dict[str, Any], str]],
        dict[str, tuple[str, Any, dict[str, Any]]],
    ]:
        """Return validated invariant options and compatibility-only parameters."""
        configured = mapping.get("fixed_options")
        if configured is None:
            configured = {}
        if not isinstance(configured, dict):
            raise CatalogError(f"fixed_options for {operation} must be a mapping.")

        mapped_keys: dict[str, str] = {}
        for section in ("inputs", "params"):
            for logical_name, mapped_spec in (mapping.get(section) or {}).items():
                manifest_key = mapped_spec.get("manifest_option_key")
                if manifest_key:
                    mapped_keys.setdefault(manifest_key, f"{section}.{logical_name}")

        fixed_values: dict[str, tuple[Any, dict[str, Any], str]] = {}
        compatibility_params: dict[str, tuple[str, Any, dict[str, Any]]] = {}
        for manifest_key, fixed_spec in configured.items():
            location = f"fixed_options.{manifest_key}"
            if not isinstance(manifest_key, str) or not manifest_key:
                raise CatalogError(
                    f"{location} for {operation} has an invalid manifest key."
                )
            if manifest_key not in native:
                raise CatalogError(
                    f"{location} references missing manifest key {manifest_key} for {operation}."
                )
            if manifest_key in mapped_keys:
                raise CatalogError(
                    f"{location} collides with {mapped_keys[manifest_key]} for {operation}."
                )
            if not isinstance(fixed_spec, dict):
                raise CatalogError(f"{location} for {operation} must be a mapping.")
            unknown_fields = set(fixed_spec) - {
                "format",
                "legacy_param",
                "type",
                "value",
            }
            if unknown_fields:
                raise CatalogError(
                    f"{location} for {operation} has unknown fields: {sorted(unknown_fields)}"
                )
            if "value" not in fixed_spec:
                raise CatalogError(f"{location} for {operation} requires value.")

            value = _validate_fixed_option_value(
                fixed_spec["value"],
                fixed_spec,
                native[manifest_key],
                location,
            )
            fixed_values[manifest_key] = (value, fixed_spec, location)

            legacy_param = fixed_spec.get("legacy_param")
            if legacy_param is None:
                continue
            if not isinstance(legacy_param, str) or not legacy_param:
                raise CatalogError(
                    f"{location}.legacy_param must be a non-empty string."
                )
            if legacy_param in (mapping.get("params") or {}):
                raise CatalogError(
                    f"{location}.legacy_param collides with params.{legacy_param} for {operation}."
                )
            if legacy_param in compatibility_params:
                raise CatalogError(
                    f"Compatibility parameter {legacy_param!r} is declared more than once for {operation}."
                )
            compatibility_params[legacy_param] = (manifest_key, value, fixed_spec)

        return fixed_values, compatibility_params

    def validate_catalog(self) -> list[dict[str, str]]:
        issues: list[dict[str, str]] = []
        for operation, mapping in self.mappings.items():
            job_type = mapping.get("manifest_job_type") or mapping.get("relion_job_type")
            if job_type not in self.manifest:
                issues.append({"level": "error", "operation": operation, "message": f"Missing job type {job_type}"})
                continue
            known = self.manifest_options(job_type)
            for section in ("inputs", "params"):
                for name, spec in (mapping.get(section) or {}).items():
                    key = spec.get("manifest_option_key")
                    if key and key not in known:
                        issues.append(
                            {
                                "level": "error",
                                "operation": operation,
                                "message": f"{section}.{name} references missing manifest key {key}",
                            }
                        )
            try:
                self._validated_fixed_options(operation, mapping, known)
            except CatalogError as exc:
                issues.append(
                    {
                        "level": "error",
                        "operation": operation,
                        "message": str(exc),
                    }
                )
        return issues

    def list_operations(self, support_level: str | None = None) -> list[dict[str, Any]]:
        rows = []
        for name, mapping in self.mappings.items():
            if support_level and mapping.get("support_level") != support_level:
                continue
            rows.append(
                {
                    "operation": name,
                    "job_type": mapping.get("relion_job_type"),
                    "support_level": mapping.get("support_level"),
                    "description": mapping.get("description", ""),
                    "expected_results": mapping.get("expected_results", []),
                }
            )
        if support_level in {None, "direct_command"}:
            for operation, job_type in DIRECT_OPERATION_JOB_TYPES.items():
                definition = self.neutral[operation]
                expected_results = [
                    str(item.get("result_type")) if isinstance(item, dict) else str(item)
                    for item in definition.get("expected_results", [])
                ]
                rows.append(
                    {
                        "operation": operation,
                        "job_type": job_type,
                        "support_level": "direct_command",
                        "description": definition.get("description", ""),
                        "expected_results": expected_results,
                    }
                )
        return rows

    def describe_operation(self, name: str) -> dict[str, Any]:
        resolved = self.resolve_operation(name)
        authorization_definition = self.neutral.get(resolved)
        if authorization_definition is None:
            authorization_definition = next(
                (
                    definition
                    for operation, definition in self.neutral.items()
                    if self.resolve_operation(operation) == resolved
                ),
                {},
            )
        authorization_required = authorization_definition.get(
            "authorization_required",
            bool(
                authorization_definition.get("modifies_backend_project", True)
                or authorization_definition.get("creates_files", False)
            ),
        )
        authorization = {
            "authorization_required": authorization_required,
            "authorization_source": authorization_definition.get(
                "authorization_source", "user_task_request" if authorization_required else None
            ),
            "authorization_note": authorization_definition.get("authorization_note"),
            "requires_human_approval": authorization_definition.get("requires_human_approval", False),
            "human_approval_conditions": list(
                authorization_definition.get("human_approval_conditions", [])
            ),
        }
        if resolved in DIRECT_OPERATIONS:
            definition = self.neutral[resolved]
            return {
                "requested_operation": name,
                "operation": resolved,
                "job_type": DIRECT_OPERATION_JOB_TYPES[resolved],
                "support_level": "direct_command",
                "executable": True,
                **authorization,
                "render_tool": DIRECT_OPERATION_RENDER_TOOL_NAMES.get(resolved),
                "execution_tool": DIRECT_OPERATION_TOOL_NAMES[resolved],
                "description": definition.get("description"),
                "expected_results": [
                    str(item.get("result_type")) if isinstance(item, dict) else str(item)
                    for item in definition.get("expected_results", [])
                ],
                "definition": definition,
            }
        mapping = self.mapping(name)
        job_type = mapping.get("manifest_job_type") or mapping.get("relion_job_type")
        native = self.manifest_options(job_type)
        neutral = self.neutral.get(resolved, {})

        def enrich(section: str) -> dict[str, Any]:
            result: dict[str, Any] = {}
            neutral_section = "required_inputs" if section == "inputs" else "params"
            neutral_specs = neutral.get(neutral_section) or {}
            for key, spec in (mapping.get(section) or {}).items():
                # Expose scientific authority and dependency metadata through
                # the public descriptor while keeping executable mapping fields
                # authoritative if the two layers share a key.
                copied = {**(neutral_specs.get(key) or {}), **spec}
                manifest_key = spec.get("manifest_option_key")
                if manifest_key:
                    item = native[manifest_key]
                    copied["gui_label"] = item.get("gui_label")
                    copied["manifest_default"] = item.get("default")
                    copied["help"] = item.get("help")
                    copied["file_pattern"] = item.get("file_pattern")
                result[key] = copied
            if section == "params":
                _, compatibility_params = self._validated_fixed_options(
                    resolved,
                    mapping,
                    native,
                )
                for key, (
                    manifest_key,
                    value,
                    fixed_spec,
                ) in compatibility_params.items():
                    item = native[manifest_key]
                    result[key] = {
                        "required": False,
                        "type": fixed_spec.get("type"),
                        "manifest_option_key": manifest_key,
                        "default": value,
                        "fixed_value": value,
                        "compatibility_only": True,
                        "deprecated": True,
                        "gui_label": item.get("gui_label"),
                        "manifest_default": item.get("default"),
                        "help": item.get("help"),
                        "file_pattern": item.get("file_pattern"),
                    }
            return result

        support = str(mapping.get("support_level", "unknown"))
        return {
            "requested_operation": name,
            "operation": resolved,
            "job_type": job_type,
            "support_level": support,
            "executable": support in EXECUTABLE_SUPPORT_LEVELS,
            **authorization,
            "description": mapping.get("description"),
            "inputs": enrich("inputs"),
            "params": enrich("params"),
            "expected_results": mapping.get("expected_results", []),
        }

    def validate_direct_params(
        self,
        name: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate parameters for a dedicated direct-command operation."""
        resolved = self.resolve_operation(name)
        if resolved not in DIRECT_OPERATIONS:
            raise CatalogError(f"Operation {name!r} is not a direct command.")
        definition = self.neutral.get(resolved)
        if not isinstance(definition, dict):
            raise CatalogError(f"Missing direct operation definition for {resolved!r}.")

        specs = definition.get("params") or {}
        supplied = dict(params or {})
        unknown = sorted(set(supplied) - set(specs))
        if unknown:
            raise CatalogError(f"Unknown direct-command parameters: {unknown}")

        validated: dict[str, Any] = {}
        missing: list[str] = []
        for param_name, spec in specs.items():
            if param_name in supplied:
                value = supplied[param_name]
            elif "default" in spec:
                value = spec["default"]
            elif spec.get("required"):
                missing.append(param_name)
                continue
            else:
                continue

            value = _validate_type(
                value,
                spec.get("type"),
                f"parameter {param_name}",
            )
            minimum = spec.get("min")
            maximum = spec.get("max")
            if (
                resolved == "rescale_map"
                and param_name in {"input_pixel_size_A", "output_pixel_size_A"}
                and value <= 0
            ):
                raise CatalogError(f"parameter {param_name} must be positive.")
            if minimum is not None and value < minimum:
                raise CatalogError(f"parameter {param_name} must be at least {minimum}.")
            if maximum is not None and value > maximum:
                raise CatalogError(f"parameter {param_name} must be at most {maximum}.")
            validated[param_name] = value

        if missing:
            raise CatalogError(
                f"Missing required values for {resolved}: " + ", ".join(missing)
            )
        if resolved == "rescale_map":
            if validated["new_box_size_pix"] % 2:
                raise CatalogError("parameter new_box_size_pix must be even.")
            if "output_map" in validated:
                validated["output_map"] = safe_project_relative(validated["output_map"])
            output_suffix = validated.get("output_suffix")
            if output_suffix is not None and not re.fullmatch(
                r"[A-Za-z0-9_.-]{1,80}", output_suffix
            ):
                raise CatalogError(
                    "parameter output_suffix may contain only letters, digits, '.', '_' and '-'."
                )
        elif resolved == "remove_duplicate_particles":
            output_star = safe_project_relative(validated["output_star"])
            output_path = PurePosixPath(output_star)
            if output_path.suffix.lower() != ".star":
                raise CatalogError("parameter output_star must have a .star suffix.")
            if not output_path.parts or output_path.parts[0] != "Deduplicate":
                raise CatalogError(
                    "parameter output_star must be beneath the dedicated Deduplicate/ namespace."
                )
            validated["output_star"] = output_star
        elif resolved == "align_symmetry_reference":
            output_map = safe_project_relative(validated["output_map"])
            output_path = PurePosixPath(output_map)
            if output_path.suffix.lower() != ".mrc":
                raise CatalogError("parameter output_map must have a .mrc suffix.")
            if not output_path.parts or output_path.parts[0] != "ReferencePrep":
                raise CatalogError(
                    "parameter output_map must be beneath the dedicated ReferencePrep/ namespace."
                )
            validated["output_map"] = output_map
        return validated

    def render_job(
        self,
        operation: str,
        inputs: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        *,
        include_defaults: bool = True,
    ) -> RenderedJob:
        requested = operation
        resolved = self.resolve_operation(operation)
        mapping = self.mapping(resolved)
        support = str(mapping.get("support_level", "unknown"))
        job_type = str(mapping.get("manifest_job_type") or mapping.get("relion_job_type"))
        native = self.manifest_options(job_type)
        supplied_inputs = dict(inputs or {})
        supplied_params = dict(params or {})
        neutral_params = self.neutral.get(resolved, {}).get("params") or {}
        fixed_values, compatibility_params = self._validated_fixed_options(
            resolved,
            mapping,
            native,
        )

        unknown_inputs = set(supplied_inputs) - set(mapping.get("inputs") or {})
        unknown_params = set(supplied_params) - (
            set(mapping.get("params") or {}) | set(compatibility_params)
        )
        if unknown_inputs:
            raise CatalogError(f"Unknown inputs for {resolved}: {sorted(unknown_inputs)}")
        if unknown_params:
            raise CatalogError(f"Unknown parameters for {resolved}: {sorted(unknown_params)}")

        for name, (
            manifest_key,
            fixed_value,
            fixed_spec,
        ) in compatibility_params.items():
            if name not in supplied_params:
                continue
            supplied_value = _validate_fixed_option_value(
                supplied_params[name],
                fixed_spec,
                native[manifest_key],
                f"parameter {name}",
            )
            supplied_formatted = _format_value(
                supplied_value,
                fixed_spec.get("format"),
                f"parameter {name}",
            )
            fixed_formatted = _format_value(
                fixed_value,
                fixed_spec.get("format"),
                f"fixed option {manifest_key}",
            )
            if supplied_formatted != fixed_formatted:
                raise CatalogError(
                    f"parameter {name} is fixed to {fixed_formatted!r} for {resolved}; "
                    f"received {supplied_formatted!r}."
                )

        values_by_manifest_key: dict[str, tuple[Any, dict[str, Any], str]] = dict(
            fixed_values
        )

        for name, spec in (mapping.get("inputs") or {}).items():
            if name in supplied_inputs:
                value = safe_project_relative(supplied_inputs[name])
                values_by_manifest_key[spec["manifest_option_key"]] = (value, spec, f"input {name}")

        for name, spec in (mapping.get("params") or {}).items():
            if name in supplied_params:
                value = supplied_params[name]
            elif include_defaults and "default" in spec:
                value = spec["default"]
            else:
                continue
            validation_spec = {**(neutral_params.get(name) or {}), **spec}
            expected_type = validation_spec.get("type")
            # The supplied mapping contract labels executable selectors as
            # ``path`` even though RELION accepts and commonly requires an
            # absolute external-program path. Preserve that authoritative
            # contract while applying the correct executable-specific safety
            # validation in the adapter.
            if expected_type == "path" and name.endswith("_executable"):
                expected_type = "executable"
            value = _validate_type(value, expected_type, f"parameter {name}")
            minimum = validation_spec.get("min")
            exclusive_minimum = validation_spec.get("exclusive_min")
            maximum = validation_spec.get("max")
            if minimum is not None and value < minimum:
                raise CatalogError(f"parameter {name} must be at least {minimum}.")
            if exclusive_minimum is not None and value <= exclusive_minimum:
                raise CatalogError(f"parameter {name} must be greater than {exclusive_minimum}.")
            if maximum is not None and value > maximum:
                raise CatalogError(f"parameter {name} must be at most {maximum}.")
            values_by_manifest_key[spec["manifest_option_key"]] = (
                value,
                spec,
                f"parameter {name}",
            )

        missing: list[str] = []
        for section in ("inputs", "params"):
            for name, spec in (mapping.get(section) or {}).items():
                if not spec.get("required"):
                    continue
                manifest_key = spec.get("manifest_option_key")
                if manifest_key not in values_by_manifest_key:
                    missing.append(f"{section}.{name}")
        if missing:
            raise CatalogError(f"Missing required values for {resolved}: {', '.join(missing)}")

        options_by_key: dict[str, str] = {}
        assignments: list[str] = []
        for manifest_key, (raw, spec, logical_name) in values_by_manifest_key.items():
            formatted = _format_value(raw, spec.get("format"), logical_name)
            options_by_key[manifest_key] = formatted
            label = str(native[manifest_key]["gui_label"])
            if ";" in label or " == " in label or "\n" in label or "\r" in label:
                raise CatalogError(f"Unsafe GUI label in manifest for {manifest_key}")
            assignments.append(f"{label} == {formatted}")

        _validate_option_combination(resolved, options_by_key)

        return RenderedJob(
            requested_operation=requested,
            operation=resolved,
            relion_job_type=job_type,
            support_level=support,
            options_by_key=options_by_key,
            option_assignments=assignments,
        )

    def workflow(self, workflow_id: str) -> dict[str, Any]:
        if workflow_id not in self.workflows:
            raise CatalogError(f"Unknown workflow {workflow_id!r}.")
        return self.workflows[workflow_id]
