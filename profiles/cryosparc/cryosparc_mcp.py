#!/usr/bin/env python3
"""
CryoSPARC MCP Server

Provides tools for validating CryoSPARC pipeline plans:
- Live project/job discovery for cross-backend handoffs
- Job type validation (with fuzzy suggestions)
- Connection validation (input/output slot types)
- Parameter validation (names, types, values)
- Full pipeline plan validation
"""
from __future__ import annotations

import json
import math
import os
from typing import Any

import mcp.server.stdio
import mcp.types as types
from instance_discovery import inspect_project_job
from mcp.server import Server

# ---------------------------------------------------------------------------
# CryoSPARC connection
# ---------------------------------------------------------------------------
CS_LICENSE = os.environ["CRYOSPARC_LICENSE_ID"]
CS_HOST    = os.environ["CRYOSPARC_MASTER_HOSTNAME"]
CS_PORT    = int(os.environ["CRYOSPARC_BASE_PORT"])
CS_EMAIL   = os.environ["CRYOSPARC_EMAIL"]
CS_PASSWORD = os.environ["CRYOSPARC_PASSWORD"]

def _get_cs():
    from cryosparc.tools import CryoSPARC
    return CryoSPARC(
        license=CS_LICENSE,
        host=CS_HOST,
        base_port=CS_PORT,
        email=CS_EMAIL,
        password=CS_PASSWORD,
    )

# ---------------------------------------------------------------------------
# Job spec cache — loaded once on first use
# ---------------------------------------------------------------------------
_JOB_SPECS: dict[str, dict] = {}   # name -> full spec dict
_SPECS_LOADED = False

def _ensure_specs():
    global _JOB_SPECS, _SPECS_LOADED
    if _SPECS_LOADED:
        return
    cs = _get_cs()
    raw = cs.get_job_specs()
    for group in raw:
        for job in group.get("contains", []):
            name = job.get("name")
            if name:
                _JOB_SPECS[name] = job
    _SPECS_LOADED = True

# ---------------------------------------------------------------------------
# Known output slots (not in static spec — derived from pipeline code +
# CryoSPARC docs).  Format: job_type -> list of output group names
# ---------------------------------------------------------------------------
KNOWN_OUTPUTS: dict[str, list[str]] = {
    # Import
    "import_movies":                  ["imported_movies"],
    "import_micrographs":             ["imported_micrographs"],
    "import_particles":               ["imported_particles"],
    "import_volumes":                 ["imported_volume_1"],   # may be _2, _3…
    "import_templates":               ["imported_templates"],
    # Motion correction
    "patch_motion_correction_multi":  ["micrographs"],
    "rigid_motion_correction":        ["micrographs"],
    "rigid_motion_correction_multi":  ["micrographs"],
    "local_motion_correction":        ["micrographs"],
    "local_motion_correction_multi":  ["micrographs"],
    "reference_motion_correction":    ["micrographs"],
    # CTF
    "patch_ctf_estimation_multi":     ["exposures"],
    "ctf_estimation":                 ["exposures"],
    "ctf_estimation_gctf":            ["exposures"],
    "patch_ctf_extract":              ["particles"],
    "ctf_refine_global":              ["exposures"],
    "ctf_refine_local":               ["particles"],
    # Picking
    "blob_picker_gpu":                ["particles"],
    "template_picker_gpu":            ["particles"],
    "filament_tracer_gpu":            ["particles"],
    "topaz_train":                    ["topaz_model", "micrographs"],
    "topaz_extract":                  ["particles"],
    "topaz_denoise":                  ["micrographs"],
    "deep_picker_train":              ["deep_picker_model"],
    "deep_picker_inference":          ["particles"],
    "manual_picker":                  ["particles", "templates"],
    "manual_picker_v2":               ["particles", "templates"],
    "auto_blob_picker_gpu":           ["particles"],
    "inspect_picks_v2":               ["particles"],
    "inspect_simple":                 ["particles"],
    # Junk / curate
    "junk_detector_v1":               ["exposures", "particles_accepted", "particles_rejected"],
    "junk_detector_devbeta1":         ["exposures", "particles_accepted", "particles_rejected"],
    "junk_filter":                    ["particles"],
    "curate_exposures":               ["exposures_accepted", "exposures_rejected"],
    "curate_exposures_v2":            ["exposures_accepted", "exposures_rejected"],
    # Extraction
    "extract_micrographs_cpu_parallel": ["particles"],
    "extract_micrographs_multi":      ["particles"],
    "downsample_particles":           ["particles"],
    "restack_particles":              ["particles"],
    # 2D classification
    "class_2D":                       ["particles", "class_averages"],
    "class_2D_new":                   ["particles", "class_averages"],
    "class_2D_streaming":             ["particles", "class_averages"],
    "select_2D":                      ["particles_selected", "particles_rejected", "class_averages_selected"],
    "reference_select_2D":            ["particles_selected", "particles_rejected", "templates_selected"],
    "reconstruct_2D":                 ["class_averages"],
    "rebalance_classes_2D":           ["particles", "class_averages"],
    # 3D ab-initio / reconstruction
    "homo_abinit":                    ["particles", "volume_class_0", "volume_class_1", "volume_class_2"],
    "homo_reconstruct":               ["map", "map_sharp"],
    "hetero_reconstruct":             ["map_class_0", "map_class_1"],
    "hetero_reconstruct_new":         ["map_class_0", "map_class_1"],
    # 3D refinement
    "homo_refine":                    ["particles", "volume", "volume_map_sharp", "volume_map_half_A", "volume_map_half_B"],
    "homo_refine_new":                ["particles", "volume", "volume_map_sharp"],
    "homo_refine_streaming":          ["particles", "volume"],
    "nonuniform_refine":              ["particles", "volume", "volume_map_sharp"],
    "nonuniform_refine_new":          ["particles", "volume", "volume_map_sharp"],
    "new_local_refine":               ["particles", "volume"],
    "naive_local_refine":             ["particles", "volume"],
    "helix_refine":                   ["particles", "volume"],
    # 3D classification
    "class_3D":                       ["particles_class_0", "volume_class_0"],
    "hetero_refine":                  ["particles_class_0", "volume_class_0"],
    "reference_select_3D":            ["particles_selected"],
    "regroup_3D":                     ["particles", "volume"],
    "regroup_3D_new":                 ["particles", "volume"],
    # Utilities
    "particle_subtract":              ["particles"],
    "sym_expand":                     ["particles"],
    "particle_sets":                  ["particles_set_A", "particles_set_B"],
    "exposure_sets":                  ["exposures_set_A", "exposures_set_B"],
    "exposure_groups":                ["exposures"],
    "subset_particles":               ["particles"],
    "remove_duplicate_particles":     ["particles"],
    "class_probability_filter":       ["particles"],
    "cache_particles":                ["particles"],
    "standardize_particle_psize":     ["particles"],
    "reassign_particles_mics":        ["particles"],
    "rebalance_3D":                   ["particles"],
    # Post-processing
    "sharpen":                        ["map_sharp"],
    "local_resolution":               ["volume_map_local_resolution"],
    "local_filter":                   ["volume_filtered"],
    "validation":                     ["fsc_curves"],
    "fsc3D":                          ["fsc_curves"],
    "deepemhancer":                   ["volume"],
    "reslog":                         ["reslog_data"],
    "orientation_diagnostics":        ["diagnostics"],
    "align_3D":                       ["volume"],
    "align_3D_new":                   ["volume"],
    "volume_tools":                   ["volume"],
    "volume_alignment_tools":         ["volume", "particles"],
    "split_volumes_group":            ["volume_0", "volume_1"],
    "select_volume":                  ["volume"],
    # Flex / 3DVA
    "var_3D":                         ["variability_series"],
    "var_3D_disp":                    ["variability_display"],
    "flex_prep":                      ["particles"],
    "flex_train":                     ["flex_model"],
    "flex_generate":                  ["volume"],
    "flex_highres":                   ["particles"],
    "flex_meshprep":                  ["mesh"],
    # Helix
    "helix_initmodel":                ["volume"],
    "helix_symmetrize":               ["volume"],
    "helix_search":                   ["helix_symmetry"],
    "helix_average_power_spectra":    ["power_spectra"],
    # Templates
    "create_templates":               ["templates"],
    # Misc
    "patch_to_local":                 ["particles"],
    "exposure_tools":                 ["exposures"],
    "generate_thumbs":                ["thumbnails"],
    "simulator":                      ["particles"],
    "simulator_gpu":                  ["particles"],
    "topaz_cross_validation":         ["topaz_model"],
    "snowflake":                      [],   # external, user-defined
}

# ---------------------------------------------------------------------------
# Known input slots per job type.
# Format: job_type -> {slot_name: type_string}
# ---------------------------------------------------------------------------
def _get_input_slots(job_type: str) -> dict[str, str]:
    """Return {slot_name: type} from the live spec, or {} if unknown."""
    _ensure_specs()
    spec = _JOB_SPECS.get(job_type, {})
    result = {}
    for group in spec.get("input_slot_groups", []):
        result[group["name"]] = group.get("type", "unknown")
    return result


def _fuzzy_suggest(name: str, candidates: list[str], n: int = 5) -> list[str]:
    """Return up to n candidates that share characters with name."""
    name_l = name.lower()
    scored = []
    for c in candidates:
        c_l = c.lower()
        # simple overlap score
        common = sum(1 for ch in name_l if ch in c_l)
        scored.append((common, c))
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored[:n]]


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
server = Server("cryosparc-validator")


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="list_job_types",
            description=(
                "List all valid CryoSPARC job type names with short descriptions. "
                "Use this to find the correct job type name before building a pipeline."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "description": "Optional substring filter (case-insensitive)",
                    }
                },
            },
        ),
        types.Tool(
            name="get_job_spec",
            description=(
                "Get the full specification for a CryoSPARC job type: "
                "input slots (name, type, required), known output slots, "
                "and all parameter names with types and descriptions."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "job_type": {"type": "string", "description": "CryoSPARC job type name"}
                },
                "required": ["job_type"],
            },
        ),
        types.Tool(
            name="inspect_project_job",
            description=(
                "Resolve a P#/J# pair on the single live CryoSPARC instance selected by "
                "the active CRYOSPARC_* environment. Returns the authoritative project "
                "directory, bounded job metadata, output groups, and candidate .cs files. "
                "Use its relion_handoff fields before any filesystem search or handoff."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_uid": {
                        "type": "string",
                        "pattern": "^P[1-9][0-9]*$",
                        "description": "CryoSPARC project UID, for example P8",
                    },
                    "job_uid": {
                        "type": "string",
                        "pattern": "^J[1-9][0-9]*$",
                        "description": "CryoSPARC job UID, for example J218",
                    },
                },
                "required": ["project_uid", "job_uid"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="validate_job_type",
            description=(
                "Check whether a job type name is valid. "
                "Returns an error with fuzzy suggestions if it is not."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "job_type": {"type": "string"}
                },
                "required": ["job_type"],
            },
        ),
        types.Tool(
            name="validate_params",
            description=(
                "Validate a dict of parameters for a given job type. "
                "Checks that every key exists in the job spec and the value "
                "type is compatible."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "job_type": {"type": "string"},
                    "params": {
                        "type": "object",
                        "description": "Parameter dict to validate, e.g. {\"compute_num_gpus\": 2}",
                    },
                },
                "required": ["job_type", "params"],
            },
        ),
        types.Tool(
            name="validate_connection",
            description=(
                "Validate a connection between two jobs. "
                "Checks that the source output exists for the source job, "
                "the target input slot exists for the target job, "
                "and their data types are compatible."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "from_job_type": {"type": "string", "description": "Source job type"},
                    "from_output":   {"type": "string", "description": "Output slot name on the source job"},
                    "to_job_type":   {"type": "string", "description": "Target job type"},
                    "to_input":      {"type": "string", "description": "Input slot name on the target job"},
                },
                "required": ["from_job_type", "from_output", "to_job_type", "to_input"],
            },
        ),
        types.Tool(
            name="validate_pipeline",
            description=(
                "Validate a full pipeline plan represented as a list of job definitions. "
                "Each job definition should have: "
                "  job_type (str), "
                "  title (str, optional), "
                "  connections (dict: input_slot -> [source_job_index_or_id, output_name]), "
                "  params (dict). "
                "Returns a report of all errors and warnings."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "jobs": {
                        "type": "array",
                        "description": "List of job definition objects",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id":          {"type": "string", "description": "Local reference id for this job in the plan"},
                                "job_type":    {"type": "string"},
                                "title":       {"type": "string"},
                                "connections": {"type": "object"},
                                "params":      {"type": "object"},
                            },
                            "required": ["job_type"],
                        },
                    }
                },
                "required": ["jobs"],
            },
        ),
        types.Tool(
            name="get_connection_template",
            description=(
                "Return a ready-to-use Python code snippet for connecting to "
                "CryoSPARC with the correct credentials for this installation."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        result = _dispatch(name, arguments)
    except Exception as exc:
        result = {"error": str(exc)}
    return [types.TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


def _dispatch(name: str, args: dict) -> Any:
    if name == "list_job_types":
        return _tool_list_job_types(args.get("filter", ""))
    if name == "get_job_spec":
        return _tool_get_job_spec(args["job_type"])
    if name == "inspect_project_job":
        return inspect_project_job(
            _get_cs(),
            args["project_uid"],
            args["job_uid"],
        )
    if name == "validate_job_type":
        return _tool_validate_job_type(args["job_type"])
    if name == "validate_params":
        return _tool_validate_params(args["job_type"], args.get("params", {}))
    if name == "validate_connection":
        return _tool_validate_connection(
            args["from_job_type"], args["from_output"],
            args["to_job_type"],  args["to_input"],
        )
    if name == "validate_pipeline":
        return _tool_validate_pipeline(args["jobs"])
    if name == "get_connection_template":
        return _tool_get_connection_template()
    return {"error": f"Unknown tool: {name}"}


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _tool_list_job_types(filter_str: str) -> dict:
    _ensure_specs()
    result = []
    for name, spec in sorted(_JOB_SPECS.items()):
        if filter_str and filter_str.lower() not in name.lower():
            continue
        result.append({
            "name": name,
            "title": spec.get("title", spec.get("shorttitle", "")),
            "description": spec.get("description", "")[:120],
        })
    return {"count": len(result), "job_types": result}


def _tool_get_job_spec(job_type: str) -> dict:
    _ensure_specs()
    if job_type not in _JOB_SPECS:
        suggestions = _fuzzy_suggest(job_type, list(_JOB_SPECS.keys()))
        return {
            "error": f"Unknown job type: {job_type!r}",
            "suggestions": suggestions,
        }
    spec = _JOB_SPECS[job_type]

    # Input slots
    inputs = []
    for g in spec.get("input_slot_groups", []):
        inputs.append({
            "name":     g["name"],
            "type":     g.get("type", ""),
            "title":    g.get("title", ""),
            "required": g.get("count_min", 1) > 0,
            "max":      g.get("count_max", 1) if g.get("count_max") != math.inf else "unlimited",
        })

    # Output slots (from static map)
    outputs = KNOWN_OUTPUTS.get(job_type, [])

    # Parameters
    params_info = []
    for pname, pdef in spec.get("params_base", {}).items():
        params_info.append({
            "name":     pname,
            "type":     pdef.get("type", ""),
            "title":    pdef.get("title", ""),
            "default":  pdef.get("value"),
            "advanced": pdef.get("advanced", False),
            "hidden":   pdef.get("hidden", False),
        })
    params_info.sort(key=lambda x: (x["hidden"], x["advanced"], x["name"]))

    return {
        "job_type":    job_type,
        "title":       spec.get("title", ""),
        "description": spec.get("description", ""),
        "inputs":      inputs,
        "outputs":     outputs,
        "params":      params_info,
    }


def _tool_validate_job_type(job_type: str) -> dict:
    _ensure_specs()
    if job_type in _JOB_SPECS:
        return {"valid": True, "job_type": job_type}
    suggestions = _fuzzy_suggest(job_type, list(_JOB_SPECS.keys()))
    return {
        "valid": False,
        "error": f"{job_type!r} is not a valid CryoSPARC job type.",
        "suggestions": suggestions,
    }


def _tool_validate_params(job_type: str, params: dict) -> dict:
    _ensure_specs()
    if job_type not in _JOB_SPECS:
        suggestions = _fuzzy_suggest(job_type, list(_JOB_SPECS.keys()))
        return {"error": f"Unknown job type: {job_type!r}", "suggestions": suggestions}

    spec = _JOB_SPECS[job_type]
    valid_params = spec.get("params_base", {})
    errors = []
    warnings = []

    for key, value in params.items():
        if key not in valid_params:
            close = _fuzzy_suggest(key, list(valid_params.keys()), n=3)
            errors.append({
                "param": key,
                "error": f"Unknown parameter {key!r} for job type {job_type!r}",
                "suggestions": close,
            })
            continue

        pdef = valid_params[key]
        expected_type = pdef.get("type", "")

        # Map CryoSPARC param types to Python types
        # enum: string, allowed values not in spec so can't check them
        # path: filesystem path string
        # resource_selection: UI widget, skip
        TYPE_CHECKS: dict[str, tuple] = {
            "number":  (int, float),
            "boolean": (bool,),
            "string":  (str,),
            "enum":    (str,),
            "path":    (str,),
        }
        if expected_type in TYPE_CHECKS and not isinstance(value, TYPE_CHECKS[expected_type]):
            # bool is a subclass of int in Python — don't flag bool as valid for number
            if not (expected_type == "number" and isinstance(value, bool)):
                warnings.append({
                    "param":    key,
                    "warning":  f"Expected type {expected_type!r} but got {type(value).__name__!r}",
                    "expected": expected_type,
                })

    return {
        "job_type": job_type,
        "valid":    len(errors) == 0,
        "errors":   errors,
        "warnings": warnings,
        "valid_param_count": len(valid_params),
    }


def _tool_validate_connection(
    from_job_type: str, from_output: str,
    to_job_type: str,   to_input: str,
) -> dict:
    _ensure_specs()
    errors = []

    # Validate source job type
    if from_job_type not in _JOB_SPECS:
        return {
            "valid": False,
            "error": f"Unknown source job type: {from_job_type!r}",
            "suggestions": _fuzzy_suggest(from_job_type, list(_JOB_SPECS.keys())),
        }

    # Validate target job type
    if to_job_type not in _JOB_SPECS:
        return {
            "valid": False,
            "error": f"Unknown target job type: {to_job_type!r}",
            "suggestions": _fuzzy_suggest(to_job_type, list(_JOB_SPECS.keys())),
        }

    # Check source output exists
    known_out = KNOWN_OUTPUTS.get(from_job_type, [])
    if known_out and from_output not in known_out:
        close = _fuzzy_suggest(from_output, known_out, n=3)
        errors.append({
            "error": f"{from_job_type!r} has no output named {from_output!r}",
            "known_outputs": known_out,
            "suggestions": close,
        })

    # Check target input slot exists
    target_inputs = _get_input_slots(to_job_type)
    if target_inputs and to_input not in target_inputs:
        close = _fuzzy_suggest(to_input, list(target_inputs.keys()), n=3)
        errors.append({
            "error": f"{to_job_type!r} has no input slot named {to_input!r}",
            "known_inputs": list(target_inputs.keys()),
            "suggestions": close,
        })

    # Type compatibility: map output names to the CryoSPARC data types they carry.
    # "exposure" type is used by input slots named exposures/movies/micrographs.
    OUTPUT_TO_TYPE: dict[str, str] = {
        "imported_movies":    "exposure",
        "imported_micrographs": "exposure",
        "micrographs":        "exposure",
        "exposures":          "exposure",
        "exposures_accepted": "exposure",
        "exposures_rejected": "exposure",
        "exposures_set_A":    "exposure",
        "exposures_set_B":    "exposure",
        "particles":          "particle",
        "particles_accepted": "particle",
        "particles_rejected": "particle",
        "particles_selected": "particle",
        "particles_class_0":  "particle",
        "particles_class_1":  "particle",
        "particles_set_A":    "particle",
        "particles_set_B":    "particle",
        "class_averages":     "template",
        "class_averages_selected": "template",
        "templates":          "template",
        "templates_selected": "template",
        "imported_templates": "template",
        "volume":             "volume",
        "volume_class_0":     "volume",
        "volume_class_1":     "volume",
        "volume_class_2":     "volume",
        "map":                "volume",
        "map_sharp":          "volume",
        "imported_volume_1":  "volume",
        "topaz_model":        "hyperparameter",
        "deep_picker_model":  "hyperparameter",
        "flex_model":         "hyperparameter",
    }
    # Input slot types that accept a given data type
    TYPE_COMPAT: dict[str, set[str]] = {
        "exposure":      {"exposure"},
        "particle":      {"particle"},
        "template":      {"template"},
        "volume":        {"volume"},
        "hyperparameter":{"hyperparameter"},
    }

    warnings = []
    if not errors and target_inputs:
        in_type  = target_inputs.get(to_input, "")
        out_type = OUTPUT_TO_TYPE.get(from_output, "")
        if in_type and out_type:
            allowed = TYPE_COMPAT.get(in_type, set())
            if out_type not in allowed:
                warnings.append(
                    f"Possible type mismatch: input {to_input!r} expects type {in_type!r} "
                    f"but output {from_output!r} carries type {out_type!r}."
                )

    return {
        "valid":    len(errors) == 0,
        "from":     f"{from_job_type}.{from_output}",
        "to":       f"{to_job_type}.{to_input}",
        "errors":   errors,
        "warnings": warnings,
    }


def _tool_validate_pipeline(jobs: list[dict]) -> dict:
    _ensure_specs()
    errors: list[dict] = []
    warnings: list[dict] = []

    # Build an id -> index map for connection resolution
    id_map: dict[str, int] = {}
    for i, job in enumerate(jobs):
        jid = job.get("id") or str(i)
        id_map[jid] = i

    for i, job in enumerate(jobs):
        job_id   = job.get("id", str(i))
        jtype    = job.get("job_type", "")
        label    = f"Job[{job_id}] ({jtype})"

        # Validate job type
        if not jtype:
            errors.append({"job": job_id, "error": "Missing job_type"})
            continue

        if jtype not in _JOB_SPECS:
            suggestions = _fuzzy_suggest(jtype, list(_JOB_SPECS.keys()))
            errors.append({
                "job":   job_id,
                "error": f"Invalid job type {jtype!r}",
                "suggestions": suggestions,
            })
            continue

        # Validate params
        params = job.get("params", {})
        if params:
            p_result = _tool_validate_params(jtype, params)
            for e in p_result.get("errors", []):
                errors.append({"job": job_id, **e})
            for w in p_result.get("warnings", []):
                warnings.append({"job": job_id, **w})

        # Validate connections
        connections = job.get("connections", {})
        spec = _JOB_SPECS[jtype]
        required_inputs = {
            g["name"]: g
            for g in spec.get("input_slot_groups", [])
            if g.get("count_min", 1) > 0
        }

        for slot_name, conn in connections.items():
            # conn should be [source_id_or_index, output_name]
            if not isinstance(conn, (list, tuple)) or len(conn) != 2:
                errors.append({
                    "job":   job_id,
                    "error": f"Connection {slot_name!r} must be [source_id, output_name], got {conn!r}",
                })
                continue

            src_ref, out_name = conn

            # Resolve source job
            if isinstance(src_ref, int):
                src_idx = src_ref
            else:
                src_idx = id_map.get(str(src_ref))
                if src_idx is None:
                    errors.append({
                        "job":   job_id,
                        "error": f"Connection {slot_name!r}: source id {src_ref!r} not found in pipeline",
                    })
                    continue

            if src_idx < 0 or src_idx >= len(jobs):
                errors.append({
                    "job":   job_id,
                    "error": f"Connection {slot_name!r}: source index {src_idx} out of range",
                })
                continue

            src_type = jobs[src_idx].get("job_type", "")
            conn_result = _tool_validate_connection(src_type, out_name, jtype, slot_name)
            conn_label = f"{slot_name}<-{src_type}.{out_name}"
            for e in conn_result.get("errors", []):
                errors.append({"job": job_id, "connection": conn_label, **e})
            for w in conn_result.get("warnings", []):
                warnings.append({"job": job_id, "connection": conn_label, "warning": w})

        # Warn about missing required inputs that are not connected
        for req_name in required_inputs:
            if req_name not in connections:
                warnings.append({
                    "job":     job_id,
                    "warning": f"Required input slot {req_name!r} is not connected",
                })

    return {
        "valid":         len(errors) == 0,
        "job_count":     len(jobs),
        "error_count":   len(errors),
        "warning_count": len(warnings),
        "errors":        errors,
        "warnings":      warnings,
    }


def _tool_get_connection_template() -> dict:
    return {
        "description": "CryoSPARC connection parameters for this installation",
        "code": (
            "from cryosparc.tools import CryoSPARC\n\n"
            f"cs = CryoSPARC(\n"
            f"    license=\"{CS_LICENSE}\",\n"
            f"    host=\"{CS_HOST}\",\n"
            f"    base_port={CS_PORT},\n"
            f"    email=\"{CS_EMAIL}\",\n"
            f"    password=\"{CS_PASSWORD}\",\n"
            ")\n"
            "assert cs.test_connection()\n"
        ),
        "host":      CS_HOST,
        "base_port": CS_PORT,
        "email":     CS_EMAIL,
        "license":   CS_LICENSE,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def main():
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
