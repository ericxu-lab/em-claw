"""Confined metadata audits and reproducible, additive micrograph subsets.

These tools do not launch RELION or infer particle quality. Particle identity is
the image index and stack path, never a guessed correspondence after re-extraction.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import os
import re
import secrets
import stat
from collections import Counter, defaultdict
from itertools import pairwise
from pathlib import Path, PurePosixPath
from typing import Any

from .cryosparc_import import CryosparcImportError, _rename_directory_noreplace
from .settings import Settings
from .star import (
    _canonical_star_value,
    _parse_star_lines,
    _tokens,
    parse_star_block,
)


class ParticleAuditError(ValueError):
    """Invalid or unsafe metadata audit/subset request."""


_MAX_GROUPS = 256
_MAX_JOINT_STRATA = 10_000
_NULL = {"", ".", "?"}
_BASE = PurePosixPath("External/MicrographSubsets")


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_digest(value: Any) -> str:
    return _digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _source(settings: Settings, project: Path, raw: str) -> Path:
    path = settings.resolve_project_subpath(project, raw, must_exist=True)
    if path.suffix.lower() != ".star" or not path.is_file():
        raise ParticleAuditError("Input must be a regular project-confined .star file.")
    return path


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ParticleAuditError("STAR input must be a regular file.")
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size, after.st_mtime_ns, after.st_ctime_ns
    ):
        raise ParticleAuditError("Source STAR changed while it was being read; retry from a stable input.")
    return digest.hexdigest()


def _loop(block: dict[str, Any] | None, name: str) -> dict[str, list[str]]:
    if not block or any(not isinstance(values, list) for values in block.values()):
        raise ParticleAuditError(f"data_{name} must contain one ordinary metadata loop.")
    if len({len(values) for values in block.values()}) != 1:
        raise ParticleAuditError(f"data_{name} columns have unequal lengths.")
    return block


def _optics_lookup(optics: dict[str, list[str]] | None) -> dict[str, int]:
    if not optics:
        return {}
    groups = optics.get("rlnOpticsGroup")
    if groups is None:
        raise ParticleAuditError("data_optics has no rlnOpticsGroup.")
    index: dict[str, int] = {}
    for row, value in enumerate(groups):
        group = _canonical_star_value("rlnOpticsGroup", value)
        if int(group.removeprefix("i:")) < 1:
            raise ParticleAuditError("Optics group identities must be positive integers.")
        if group in index:
            raise ParticleAuditError("data_optics repeats an optics group.")
        index[group] = row
    return index


def _values(
    data: dict[str, list[str]], optics: dict[str, list[str]] | None, column: str
) -> list[str] | None:
    if column in data:
        return data[column]
    if not optics or column not in optics or "rlnOpticsGroup" not in data:
        return None
    lookup = _optics_lookup(optics)
    try:
        return [
            optics[column][lookup[_canonical_star_value("rlnOpticsGroup", group)]]
            for group in data["rlnOpticsGroup"]
        ]
    except KeyError as exc:
        raise ParticleAuditError("A row references an undefined optics group.") from exc


def _strata_spec(
    data: dict[str, list[str]],
    optics: dict[str, list[str]] | None,
    strata_columns: list[str] | None,
    numeric_bins: dict[str, list[float]] | None,
    *,
    particle_audit: bool = False,
) -> tuple[list[str], dict[str, list[float]], dict[str, list[str]], list[str]]:
    columns = list(strata_columns) if strata_columns is not None else [
        name for name in ("rlnOpticsGroup", "rlnMicrographName")
        if name in data and (particle_audit or name != "rlnMicrographName")
    ]
    if len(columns) != len(set(columns)) or len(columns) > 12:
        raise ParticleAuditError("strata_columns must contain at most 12 distinct column names.")
    bins: dict[str, list[float]] = {}
    for column, edges in (numeric_bins or {}).items():
        if not isinstance(edges, list) or not 1 <= len(edges) <= 100:
            raise ParticleAuditError("Each numeric_bins value must contain 1–100 finite increasing edges.")
        if any(isinstance(edge, bool) or not isinstance(edge, (int, float)) for edge in edges):
            raise ParticleAuditError("Numeric bin edges must be numbers, not strings or booleans.")
        converted = [float(edge) for edge in edges]
        if any(not math.isfinite(edge) for edge in converted) or any(
            left >= right for left, right in pairwise(converted)
        ):
            raise ParticleAuditError("Numeric bin edges must be finite and strictly increasing.")
        bins[column] = converted
    if len(bins) > 12 or set(columns).intersection(bins):
        raise ParticleAuditError("Use each stratum column once, as categorical or numeric.")
    values: dict[str, list[str]] = {}
    missing: list[str] = []
    for column in [*columns, *bins]:
        if not isinstance(column, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", column):
            raise ParticleAuditError("Stratum names must be STAR column names without a leading underscore.")
        resolved = _values(data, optics, column)
        if resolved is None:
            missing.append(column)
        else:
            values[column] = resolved
    return columns, bins, values, missing


def _stratum_value(column: str, raw: str, bins: dict[str, list[float]]) -> str:
    if raw in _NULL:
        return "<missing>"
    if column not in bins:
        if column == "rlnOpticsGroup":
            return _canonical_star_value(column, raw).removeprefix("i:")
        return raw
    try:
        value = float(raw)
    except ValueError:
        return "<invalid_numeric>"
    if not math.isfinite(value):
        return "<invalid_numeric>"
    edges = bins[column]
    index = bisect.bisect_right(edges, value)
    lower = str(edges[index - 1]) if index else "-inf"
    upper = str(edges[index]) if index < len(edges) else "+inf"
    return f"[{lower}, {upper})"


def _image_identity(raw: str) -> str:
    match = re.fullmatch(r"([0-9]+)@(.+)", raw)
    if match is None or int(match[1]) < 1 or match[2] in _NULL:
        raise ParticleAuditError(f"Invalid rlnImageName identity {raw!r}.")
    return f"{int(match[1])}@{PurePosixPath(match[2]).as_posix()}"


def _particle_input(path: Path) -> tuple[dict[str, list[str]], dict[str, list[str]] | None, str]:
    digest = _file_digest(path)
    data = _loop(parse_star_block(path, "particles", strict_loops=True), "particles")
    optics_raw = parse_star_block(path, "optics", strict_loops=True)
    optics = _loop(optics_raw, "optics") if optics_raw is not None else None
    if _file_digest(path) != digest:
        raise ParticleAuditError("Source STAR changed during the audit; retry from a stable input.")
    if "rlnImageName" not in data:
        raise ParticleAuditError("data_particles requires rlnImageName for identity-based attrition.")
    if optics is not None:
        lookup = _optics_lookup(optics)
        if "rlnOpticsGroup" not in data or any(
            _canonical_star_value("rlnOpticsGroup", group) not in lookup
            for group in data["rlnOpticsGroup"]
        ):
            raise ParticleAuditError("Particle rows must reference defined optics groups.")
    return data, optics, digest


def _identities(data: dict[str, list[str]]) -> tuple[dict[str, int], int, list[str]]:
    index: dict[str, int] = {}
    duplicates = 0
    examples: list[str] = []
    for row, raw in enumerate(data["rlnImageName"]):
        identity = _image_identity(raw)
        if identity in index:
            duplicates += 1
            if len(examples) < 10:
                examples.append(identity)
        else:
            index[identity] = row
    return index, duplicates, examples


def _class_counts(data: dict[str, list[str]], rows: Any) -> dict[int, int] | None:
    if "rlnClassNumber" not in data:
        return None
    counts: Counter[int] = Counter()
    for row in rows:
        raw = data["rlnClassNumber"][row]
        if not re.fullmatch(r"[1-9][0-9]*", raw):
            raise ParticleAuditError(f"Invalid positive rlnClassNumber: {raw!r}.")
        counts[int(raw)] += 1
    return dict(sorted(counts.items()))


def _attrition_summary(
    total: Counter[Any], retained: Counter[Any], valid: bool, *,
    joint_columns: list[str] | None = None,
) -> dict[str, Any]:
    records = [
        {**({"stratum": dict(zip(joint_columns, group, strict=True))} if joint_columns else {"value": group}),
         "source_unique_particles": count,
         "selected_unique_particles": retained[group] if valid else None,
         "excluded_unique_particles": count - retained[group] if valid else None,
         "retained_fraction": retained[group] / count if valid else None}
        for group, count in sorted(total.items())
    ]
    if valid:
        # The initial sort supplies a deterministic tie-break for categorical tuples.
        records.sort(key=lambda record: (record["retained_fraction"], -record["source_unique_particles"]))
    fractions = [record["retained_fraction"] for record in records] if valid else []
    return {
        "group_count": len(records), "groups": records[:_MAX_GROUPS],
        "truncated": len(records) > _MAX_GROUPS,
        "omitted_group_count": max(0, len(records) - _MAX_GROUPS),
        "group_order": "lowest_retained_fraction_first" if valid else "value",
        "all_groups_summary": {
            "source_unique_particles": sum(total.values()),
            "selected_unique_particles": sum(retained.values()) if valid else None,
            "lost_stratum_count": sum(value == 0 for value in fractions) if valid else None,
            "partially_retained_stratum_count": sum(0 < value < 1 for value in fractions) if valid else None,
            "fully_retained_stratum_count": sum(value == 1 for value in fractions) if valid else None,
            "minimum_retained_fraction": min(fractions) if fractions else None,
            "maximum_retained_fraction": max(fractions) if fractions else None,
        },
    }


def _joint_attrition(
    source_ids: dict[str, int], selected_ids: dict[str, int], columns: list[str],
    values: dict[str, list[str]], bins: dict[str, list[float]], valid: bool,
) -> dict[str, Any] | None:
    if len(columns) < 2:
        return None
    missing = [column for column in columns if column not in values]
    if missing:
        return {"available": False, "columns": columns, "reason": "missing_measured_strata",
                "missing_columns": missing, "groups": [],
                "action": "Supply measured source metadata for every requested joint stratum."}
    total: Counter[tuple[str, ...]] = Counter()
    retained: Counter[tuple[str, ...]] = Counter()
    for identity, row in source_ids.items():
        group = tuple(_stratum_value(column, values[column][row], bins) for column in columns)
        if group not in total and len(total) >= _MAX_JOINT_STRATA:
            return {"available": False, "columns": columns, "reason": "joint_stratum_limit_exceeded",
                    "group_count_lower_bound": _MAX_JOINT_STRATA + 1,
                    "maximum_joint_strata": _MAX_JOINT_STRATA, "groups": [],
                    "action": "Request fewer joint categorical axes or coarser explicit numeric bins; marginal counts remain available."}
        total[group] += 1
        if identity in selected_ids:
            retained[group] += 1
    return {"available": True, "columns": columns,
            **_attrition_summary(total, retained, valid, joint_columns=columns)}


def audit_particle_selection(
    settings: Settings,
    project_dir: str,
    particles_star: str,
    *,
    selected_particles_star: str | None = None,
    selected_class_numbers: list[int] | None = None,
    strata_columns: list[str] | None = None,
    numeric_bins: dict[str, list[float]] | None = None,
) -> dict[str, Any]:
    """Audit unique image identities, metadata conservation and per-stratum attrition.

    Supply either a materialized subset or class IDs to preview. Missing IDs,
    duplicates and changed invariant metadata invalidate retention comparisons;
    diagnostics remain available but never imply a scientific rejection.
    """
    if selected_particles_star is not None and selected_class_numbers is not None:
        raise ParticleAuditError("Supply selected_particles_star or selected_class_numbers, not both.")
    project = settings.validate_project_path(project_dir)
    source_path = _source(settings, project, particles_star)
    data, optics, source_hash = _particle_input(source_path)
    source_ids, duplicates, duplicate_examples = _identities(data)
    source_class_counts = _class_counts(data, source_ids.values())
    issues: list[dict[str, Any]] = []
    limitations = [
        "Image identity uses the numeric stack index and lexical stack path; stack aliases are not resolved.",
        "Different extraction stacks require an explicit lineage mapping; coordinates are not used to guess identity.",
        "Class labels and alignment parameters may change; this audits membership and acquisition metadata, not unchanged alignment.",
        "Compare a selection with its immediate parent; intentional CTF refinement needs a separate provenance-aware comparison.",
        "Counts and attrition do not establish particle quality, view diversity or biological identity.",
    ]
    if duplicates:
        issues.append({"code": "duplicate_source_image_ids", "count": duplicates,
                       "action": "Resolve duplicate image identities before comparing selections."})
    selected_hash: str | None = None
    selected_duplicates = 0
    selected_duplicate_examples: list[str] = []
    selected_data = data
    selected_optics = optics
    selection_requested = selected_particles_star is not None or selected_class_numbers is not None
    missing_classes: list[int] = []
    if selected_class_numbers is not None:
        if len(selected_class_numbers) > 256 or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in selected_class_numbers
        ) or len(selected_class_numbers) != len(set(selected_class_numbers)):
            raise ParticleAuditError("selected_class_numbers must contain at most 256 distinct positive integers.")
        if source_class_counts is None:
            raise ParticleAuditError("Class selection preview requires rlnClassNumber.")
        requested = set(selected_class_numbers)
        missing_classes = sorted(requested - set(source_class_counts))
        selected_ids = {
            identity: row for identity, row in source_ids.items()
            if int(data["rlnClassNumber"][row]) in requested
        }
        selected_row_count = sum(
            int(raw) in requested for raw in data["rlnClassNumber"]
        )
        if missing_classes:
            issues.append({"code": "requested_classes_absent", "classes": missing_classes,
                           "action": "Check class IDs against this exact source classification."})
    elif selected_particles_star is not None:
        selected_path = _source(settings, project, selected_particles_star)
        selected_data, selected_optics, selected_hash = _particle_input(selected_path)
        selected_ids, selected_duplicates, selected_duplicate_examples = _identities(selected_data)
        selected_row_count = len(selected_data["rlnImageName"])
        if selected_duplicates:
            issues.append({"code": "duplicate_selected_image_ids", "count": selected_duplicates,
                           "action": "Resolve duplicate image identities in the selected STAR."})
    else:
        selected_ids = source_ids
        selected_row_count = len(data["rlnImageName"])
    if selected_particles_star is None:
        selected_duplicates = selected_row_count - len(selected_ids)
        selected_duplicate_examples = [identity for identity in duplicate_examples if identity in selected_ids]
    unknown = selected_ids.keys() - source_ids.keys()
    if unknown:
        issues.append({"code": "selected_image_ids_not_in_source", "count": len(unknown),
                       "examples": sorted(unknown)[:10],
                       "action": "Use STARs from the same extraction lineage or provide an explicit audited identity mapping."})

    changed: Counter[str] = Counter()
    if selected_particles_star is not None:
        invariant_columns = {
            column for column in data if column.startswith(("rlnCoordinate", "rlnDefocus", "rlnCtf"))
        } | ({"rlnMicrographName", "rlnOpticsGroup", "rlnRandomSubset", "rlnImagePixelSize", "rlnPhaseShift",
              "rlnImageSize", "rlnMicrographPixelSize", "rlnMagnification", "rlnDetectorPixelSize",
              "rlnVoltage", "rlnSphericalAberration", "rlnAmplitudeContrast"} & data.keys())
        for column in sorted(invariant_columns):
            if column not in selected_data:
                changed[column] = len(selected_ids)
                continue
            for identity, selected_row in selected_ids.items():
                source_row = source_ids.get(identity)
                if source_row is not None:
                    source_value = data[column][source_row]
                    selected_value = selected_data[column][selected_row]
                    if source_value != selected_value and _canonical_star_value(column, source_value) != (
                        _canonical_star_value(column, selected_value)
                    ):
                        changed[column] += 1
        if changed:
            issues.append({"code": "changed_or_missing_invariant_metadata", "columns": dict(changed),
                           "action": "Restore source coordinates, CTF, optics IDs and existing half-set assignments."})
        if optics is not None:
            source_lookup = _optics_lookup(optics)
            selected_lookup = _optics_lookup(selected_optics)
            mismatches: list[str] = []
            referenced = set(selected_data.get("rlnOpticsGroup", []))
            for raw in sorted(referenced):
                group = _canonical_star_value("rlnOpticsGroup", raw)
                if group not in source_lookup or group not in selected_lookup or any(
                    column not in (selected_optics or {}) or _canonical_star_value(
                        column, optics[column][source_lookup[group]]
                    ) != _canonical_star_value(column, selected_optics[column][selected_lookup[group]])
                    for column in optics
                ):
                    mismatches.append(raw)
            if selected_optics is None or mismatches:
                issues.append({"code": "changed_or_missing_optics_metadata", "groups": mismatches,
                               "action": "Use unchanged extraction/optics metadata for a valid selection comparison."})
        if "rlnRandomSubset" not in data and "rlnRandomSubset" in selected_data:
            limitations.append("Half-set labels were absent in the source; their preservation cannot be audited.")

    columns, bins, values, missing = _strata_spec(
        data, optics, strata_columns, numeric_bins, particle_audit=True
    )
    if missing:
        limitations.append("Unavailable requested strata: " + ", ".join(missing) + ". Supply measured source metadata.")
    valid = not issues
    strata: dict[str, Any] = {}
    for column in [*columns, *bins]:
        if column not in values:
            continue
        total: Counter[str] = Counter()
        retained: Counter[str] = Counter()
        for identity, row in source_ids.items():
            group = _stratum_value(column, values[column][row], bins)
            total[group] += 1
            if identity in selected_ids:
                retained[group] += 1
        strata[column] = _attrition_summary(total, retained, valid)
    joint_columns = [column for column in [*columns, *bins]
                     if strata_columns is not None or column != "rlnMicrographName"]
    joint = _joint_attrition(source_ids, selected_ids, joint_columns, values, bins, valid)
    if joint is not None and not joint["available"]:
        limitations.append("Joint stratum audit unavailable: " + joint["reason"] + ". " + joint["action"])
    selected_counts = _class_counts(selected_data, selected_ids.values())
    retained_count = len(selected_ids) if valid else None
    return {
        "report_type": "particle_selection_audit", "project_dir": str(project),
        "particles_star": str(source_path.relative_to(project)), "source_sha256": source_hash,
        "selected_particles_star": selected_particles_star, "selected_source_sha256": selected_hash,
        "selected_class_numbers": sorted(selected_class_numbers) if selected_class_numbers is not None else None,
        "selection_requested": selection_requested, "valid_for_comparison": valid,
        "source": {"row_count": len(data["rlnImageName"]), "unique_particle_count": len(source_ids),
                   "duplicate_image_name_count": duplicates, "duplicate_examples": duplicate_examples,
                   "class_counts": source_class_counts},
        "selected": {"row_count": selected_row_count, "unique_particle_count": len(selected_ids),
                     "duplicate_image_name_count": selected_duplicates,
                     "duplicate_examples": selected_duplicate_examples, "class_counts": selected_counts},
        "retained_unique_particles": retained_count,
        "excluded_unique_particles": len(source_ids) - retained_count if valid else None,
        "retained_fraction": retained_count / len(source_ids) if valid and source_ids else None,
        "missing_selected_classes": missing_classes,
        "strata": strata, "joint_strata": joint, "numeric_bins": bins, "missing_strata_columns": missing,
        "issues": issues, "limitations": limitations,
    }


def _snapshot_star(
    settings: Settings, project: Path, input_star: str
) -> tuple[Path, bytes, dict[str, dict[str, list[str]]]]:
    source = _source(settings, project, input_star)
    raw = source.read_bytes()
    if b"\x00" in raw or b"\r" in raw:
        raise ParticleAuditError("Subset input STAR must be UTF-8 with LF line endings and no NUL bytes.")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ParticleAuditError("Subset input STAR must be valid UTF-8.") from exc
    names = [line.strip()[5:] for line in lines if line.strip().startswith("data_")]
    if len(names) != len(set(names)):
        raise ParticleAuditError("Repeated STAR blocks cannot be preserved unambiguously.")
    blocks = _parse_star_lines(lines, strict_loops=True)
    for name, block in blocks.items():
        if not re.fullmatch(r"[A-Za-z0-9_]+", name):
            raise ParticleAuditError("Unsupported STAR block name.")
        _loop(block, name)
    if _file_digest(source) != _digest(raw):
        raise ParticleAuditError("Source STAR changed while planning the subset.")
    return source, raw, blocks


def _snapshot_micrographs(
    settings: Settings, project: Path, micrographs_star: str
) -> tuple[Path, bytes, dict[str, dict[str, list[str]]]]:
    source, raw, blocks = _snapshot_star(settings, project, micrographs_star)
    data = _loop(blocks.get("micrographs"), "micrographs")
    ids = data.get("rlnMicrographName")
    if not ids or any(value in _NULL for value in ids) or len({PurePosixPath(value).as_posix() for value in ids}) != len(ids):
        raise ParticleAuditError("Micrographs must have nonempty, unique rlnMicrographName identities.")
    if "optics" in blocks:
        lookup = _optics_lookup(blocks["optics"])
        if "rlnOpticsGroup" not in data or any(
            _canonical_star_value("rlnOpticsGroup", group) not in lookup
            for group in data["rlnOpticsGroup"]
        ):
            raise ParticleAuditError("Micrograph rows must reference defined optics groups.")
    return source, raw, blocks


def _snapshot_particle_labels(
    settings: Settings, project: Path, particles_star: str, micrograph_ids: set[str],
) -> tuple[bytes, dict[str, dict[str, list[str]]]]:
    _, raw, blocks = _snapshot_star(settings, project, particles_star)
    data = _loop(blocks.get("particles"), "particles")
    if not {"rlnImageName", "rlnMicrographName", "rlnCoordinateX", "rlnCoordinateY"}.issubset(data):
        raise ParticleAuditError("Training labels require image/micrograph identities and X/Y coordinates.")
    ids, duplicates, _ = _identities(data)
    if not ids or duplicates:
        raise ParticleAuditError("Training-label particles must have nonempty, unique image identities.")
    unknown = set(data["rlnMicrographName"]) - micrograph_ids
    if unknown:
        raise ParticleAuditError("Training labels reference micrographs outside the source micrograph STAR: "
                                 + ", ".join(sorted(unknown)[:10]))
    optics = blocks.get("optics")
    if optics is not None:
        lookup = _optics_lookup(optics)
        if "rlnOpticsGroup" not in data or any(
            _canonical_star_value("rlnOpticsGroup", group) not in lookup
            for group in data["rlnOpticsGroup"]
        ):
            raise ParticleAuditError("Training labels must reference defined optics groups.")
    for name, block in blocks.items():
        for column, values in block.items():
            for value in values:
                canonical = _canonical_star_value(column, value)
                if name == "particles" and column == "rlnRandomSubset" and canonical not in {"i:1", "i:2"}:
                    raise ParticleAuditError("Training-label half-set assignments must remain valid 1/2 labels.")
    return raw, blocks


def _check_output_path(settings: Settings, project: Path, output_name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", output_name):
        raise ParticleAuditError("output_name must be a simple 1–100 character name, without path separators.")
    relative = _BASE / output_name
    for count in range(1, len(relative.parts) + 1):
        current = project.joinpath(*relative.parts[:count])
        if current.is_symlink():
            raise ParticleAuditError("Subset output directories may not be symbolic links.")
    destination = settings.resolve_project_subpath(project, str(relative))
    if destination.exists():
        raise ParticleAuditError("Subset output already exists; choose a new output_name. Nothing is overwritten.")
    return destination


def _rank(seed: int, label: Any) -> str:
    return _json_digest([seed, label])


def _prepare_subset(
    settings: Settings,
    project_dir: str,
    micrographs_star: str,
    *,
    output_name: str,
    training_count: int,
    heldout_count: int,
    seed: int,
    strata_columns: list[str] | None,
    numeric_bins: dict[str, list[float]] | None,
    particles_star: str | None,
) -> tuple[dict[str, Any], bytes, dict[str, dict[str, list[str]]], tuple[bytes, dict[str, dict[str, list[str]]]] | None]:
    for field, value, minimum in (("training_count", training_count, 1), ("heldout_count", heldout_count, 0)):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ParticleAuditError(f"{field} must be an integer >= {minimum}.")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ParticleAuditError("seed must be an explicit integer.")
    project = settings.validate_project_path(project_dir)
    destination = _check_output_path(settings, project, output_name)
    source, raw, blocks = _snapshot_micrographs(settings, project, micrographs_star)
    data = blocks["micrographs"]
    optics = blocks.get("optics")
    ids = data["rlnMicrographName"]
    if training_count + heldout_count > len(ids):
        raise ParticleAuditError("Requested training plus heldout counts exceed available unique micrographs.")
    columns, bins, values, missing = _strata_spec(data, optics, strata_columns, numeric_bins)
    if missing:
        raise ParticleAuditError("Missing measured strata columns: " + ", ".join(missing)
                                 + ". Supply a STAR with measured metadata; ice/time values are never inferred.")
    names = [*columns, *bins]
    groups: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for row, identity in enumerate(ids):
        key = tuple(_stratum_value(column, values[column][row], bins) for column in names)
        if any(value in {"<missing>", "<invalid_numeric>"} for value in key):
            raise ParticleAuditError(f"Missing or invalid measured stratum for micrograph {identity!r}.")
        groups[key].append(identity)
    # Include sparse strata first when the requested budget cannot cover all strata.
    order = sorted(groups, key=lambda key: (len(groups[key]), _rank(seed, key)))
    available = {key: sorted(groups[key], key=lambda identity: (_rank(seed, identity), identity)) for key in order}
    training: list[str] = []
    heldout: list[str] = []
    allocations: dict[str, Counter[tuple[str, ...]]] = {"training": Counter(), "heldout": Counter()}

    def take(pool: list[str], name: str, limit: int, *, one_pass: bool) -> None:
        while len(pool) < limit:
            progress = False
            for key in order:
                if available[key] and len(pool) < limit:
                    pool.append(available[key].pop())
                    allocations[name][key] += 1
                    progress = True
            if one_pass or not progress:
                return

    take(training, "training", training_count, one_pass=True)
    take(heldout, "heldout", heldout_count, one_pass=True)
    take(training, "training", training_count, one_pass=False)
    take(heldout, "heldout", heldout_count, one_pass=False)
    remaining = sorted(set(ids) - set(training) - set(heldout))
    coverage = []
    for key in order:
        coverage.append({"stratum": dict(zip(names, key, strict=True)), "available": len(groups[key]),
                         "training": allocations["training"][key], "heldout": allocations["heldout"][key],
                         "remaining": len(available[key])})
    limitations = [
        "Selection represents only the explicitly supplied measured strata; it does not assess image quality.",
        "Training and heldout micrographs are disjoint; their spatial/acquisition independence needs dataset evidence.",
        "These STARs are additive metadata artifacts, not registered RELION pipeline jobs.",
    ]
    if not names:
        limitations.append("No measured strata are available/requested; this is seeded sampling of one pool.")
    if training_count < len(groups):
        limitations.append("Training capacity is insufficient to represent every stratum; sparse strata were prioritized.")
    if heldout_count and heldout_count < len(groups):
        limitations.append("Heldout capacity is insufficient to represent every stratum.")
    if heldout_count and any(item["heldout"] == 0 for item in coverage):
        limitations.append("Some strata cannot appear in both sets at these counts, including singleton strata; inspect coverage.")
    if not heldout_count:
        limitations.append("No external heldout micrographs were requested; no independent validation pool is produced.")
    relative = destination.relative_to(project).as_posix()
    plan = {
        "report_type": "stratified_micrograph_subset_plan", "schema_version": 1,
        "project_dir": str(project), "micrographs_star": source.relative_to(project).as_posix(),
        "source_sha256": _digest(raw), "output_name": output_name, "output_directory": relative,
        "seed": seed, "allocation_method": "coverage_first_round_robin_sparse_strata_first",
        "strata_columns": columns, "numeric_bins": bins, "numeric_bin_convention": "[lower, upper)",
        "source_count": len(ids), "training_count": len(training), "heldout_count": len(heldout),
        "remaining_count": len(remaining), "training_micrographs": sorted(training),
        "heldout_micrographs": sorted(heldout), "remaining_micrographs": remaining,
        "coverage": coverage, "limitations": limitations, "pipeline_registered": False,
        "outputs": {name: f"{relative}/{name}_micrographs.star" for name in ("training", "heldout", "remaining")},
        "manifest": f"{relative}/subset_manifest.json", "source_snapshot": f"{relative}/source_micrographs.star",
    }
    labels = None
    if particles_star is not None:
        labels = _snapshot_particle_labels(settings, project, particles_star, set(ids))
        particle_raw, particle_blocks = labels
        label_data = particle_blocks["particles"]
        partition_by_micrograph = {
            identity: name for name in ("training", "heldout", "remaining")
            for identity in plan[f"{name}_micrographs"]
        }
        particle_ids: dict[str, list[str]] = {name: [] for name in ("training", "heldout", "remaining")}
        for identity, micrograph in zip(label_data["rlnImageName"], label_data["rlnMicrographName"], strict=True):
            particle_ids[partition_by_micrograph[micrograph]].append(_image_identity(identity))
        plan.update({
            "particles_star": _source(settings, project, particles_star).relative_to(project).as_posix(),
            "particle_source_sha256": _digest(particle_raw),
            "particle_source_snapshot": f"{relative}/source_particles.star",
            "particle_outputs": {name: f"{relative}/{name}_particles.star" for name in particle_ids},
            "particle_counts": {"source": len(label_data["rlnImageName"]),
                                **{name: len(identities) for name, identities in particle_ids.items()}},
            "particle_identity_sha256": {name: _json_digest(sorted(identities)) for name, identities in particle_ids.items()},
            "training_labels_available": bool(particle_ids["training"]),
            "heldout_labels_available": bool(particle_ids["heldout"]),
        })
        for name in ("training", "heldout"):
            if plan[f"{name}_count"] and not particle_ids[name]:
                plan["limitations"].append(f"The {name} micrograph subset has no supplied curated labels; it cannot train or score Topaz labels yet.")
    plan["plan_sha256"] = _json_digest(plan)
    return plan, raw, blocks, labels


def plan_stratified_micrograph_subset(
    settings: Settings, project_dir: str, micrographs_star: str, *, output_name: str,
    training_count: int, heldout_count: int, seed: int = 0,
    strata_columns: list[str] | None = None, numeric_bins: dict[str, list[float]] | None = None,
    particles_star: str | None = None,
) -> dict[str, Any]:
    """Plan disjoint, reproducible metadata subsets without writing or executing jobs."""
    plan, _, _, _ = _prepare_subset(
        settings, project_dir, micrographs_star, output_name=output_name,
        training_count=training_count, heldout_count=heldout_count, seed=seed,
        strata_columns=strata_columns, numeric_bins=numeric_bins, particles_star=particles_star,
    )
    return plan


def _token(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ParticleAuditError("Control characters cannot be serialized safely into subset STAR files.")
    if value and not any(character.isspace() for character in value) and value[0] not in "#'\";_":
        token = value
    elif "'" not in value:
        token = f"'{value}'"
    elif '"' not in value:
        token = f'"{value}"'
    else:
        raise ParticleAuditError("A STAR token containing both quote styles cannot be safely serialized.")
    if _tokens(token) != [value]:
        raise ParticleAuditError("A STAR token does not survive native token round-trip.")
    return token


def _subset_bytes(
    blocks: dict[str, dict[str, list[str]]], selected: set[str], *,
    target_block: str = "micrographs", identity_column: str = "rlnMicrographName",
) -> bytes:
    lines: list[str] = []
    # Keep the possibly empty selected loop last; preserve every optics/extra loop.
    for name in [*(name for name in blocks if name != target_block), target_block]:
        block = blocks[name]
        columns = list(block)
        lines.extend([f"data_{name}", "", "loop_"])
        lines.extend(f"_{column} #{index}" for index, column in enumerate(columns, 1))
        rows = range(len(block[columns[0]]))
        if name == target_block:
            rows = sorted(
                (row for row in rows if block[identity_column][row] in selected),
                key=lambda row: block[identity_column][row],
            )
        for row in rows:
            lines.append(" ".join(_token(block[column][row]) for column in columns))
        lines.extend(["", ""])
    payload = "\n".join(lines).encode("utf-8")
    parsed = _parse_star_lines(payload.decode().splitlines(), strict_loops=True)
    for name, block in blocks.items():
        if name == target_block:
            row_ids = parsed[name][identity_column]
            if set(row_ids) != selected or len(row_ids) != len(selected):
                raise ParticleAuditError("Subset serialization changed row identities.")
            source_rows = {identity: row for row, identity in enumerate(block[identity_column])}
            if set(parsed[name]) != set(block) or any(
                parsed[name][column][row] != block[column][source_rows[identity]]
                for row, identity in enumerate(row_ids) for column in block
            ):
                raise ParticleAuditError("Subset serialization changed row metadata.")
        elif parsed[name] != block:
            raise ParticleAuditError("Subset serialization changed optics or other metadata.")
    return payload


def materialize_stratified_micrograph_subset(
    settings: Settings, project_dir: str, micrographs_star: str, *, output_name: str,
    training_count: int, heldout_count: int, expected_plan_sha256: str, seed: int = 0,
    strata_columns: list[str] | None = None, numeric_bins: dict[str, list[float]] | None = None,
    particles_star: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Publish a reviewed plan as a new, no-clobber directory of preserved STARs."""
    settings.assert_execution_enabled(confirm)
    plan, snapshot, blocks, labels = _prepare_subset(
        settings, project_dir, micrographs_star, output_name=output_name,
        training_count=training_count, heldout_count=heldout_count, seed=seed,
        strata_columns=strata_columns, numeric_bins=numeric_bins, particles_star=particles_star,
    )
    if expected_plan_sha256 != plan["plan_sha256"]:
        raise ParticleAuditError("Plan/source hash mismatch; render and review a fresh subset plan.")
    project = Path(plan["project_dir"])
    payloads = {f"{name}_micrographs.star": _subset_bytes(blocks, set(plan[f"{name}_micrographs"]))
                for name in ("training", "heldout", "remaining")}
    payloads["source_micrographs.star"] = snapshot
    if labels is not None:
        particle_raw, particle_blocks = labels
        label_data = particle_blocks["particles"]
        for name in ("training", "heldout", "remaining"):
            selected_micrographs = set(plan[f"{name}_micrographs"])
            selected_particles = {
                identity for identity, micrograph in zip(label_data["rlnImageName"], label_data["rlnMicrographName"], strict=True)
                if micrograph in selected_micrographs
            }
            payloads[f"{name}_particles.star"] = _subset_bytes(
                particle_blocks, selected_particles, target_block="particles", identity_column="rlnImageName",
            )
        payloads["source_particles.star"] = particle_raw
    manifest = {**plan, "output_sha256": {name: _digest(payload) for name, payload in payloads.items()}}
    payloads["subset_manifest.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_fd = os.open(project, flags)
    staging_name = ".subset-" + secrets.token_hex(12)
    staging_fd: int | None = None
    published = False
    written: list[str] = []
    try:
        for component in _BASE.parts:
            try:
                os.mkdir(component, mode=0o755, dir_fd=parent_fd)
            except FileExistsError:
                pass
            child_fd = os.open(component, flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = child_fd
        os.mkdir(staging_name, mode=0o700, dir_fd=parent_fd)
        staging_fd = os.open(staging_name, flags, dir_fd=parent_fd)
        for name, payload in payloads.items():
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=staging_fd)
            written.append(name)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        source = _source(settings, project, micrographs_star)
        if _file_digest(source) != plan["source_sha256"]:
            raise ParticleAuditError("Source STAR changed before publication; no subset was published.")
        if particles_star is not None and _file_digest(_source(settings, project, particles_star)) != plan["particle_source_sha256"]:
            raise ParticleAuditError("Particle label source changed before publication; no subset was published.")
        # Reject path replacement after staging, including symlink swaps of parent directories.
        _check_output_path(settings, project, output_name)
        parent_stat = os.stat(project / str(_BASE), follow_symlinks=False)
        opened_stat = os.fstat(parent_fd)
        if (parent_stat.st_dev, parent_stat.st_ino) != (opened_stat.st_dev, opened_stat.st_ino):
            raise ParticleAuditError("Subset parent directory changed before publication.")
        os.fsync(staging_fd)
        method = _rename_directory_noreplace(parent_fd, staging_name, output_name)
        published = True
        os.fsync(parent_fd)
    except (OSError, CryosparcImportError) as exc:
        raise ParticleAuditError(f"Could not publish the subset without replacement: {exc}") from exc
    finally:
        if staging_fd is not None:
            if not published:
                for name in written:
                    os.unlink(name, dir_fd=staging_fd)
            os.close(staging_fd)
            if not published:
                os.rmdir(staging_name, dir_fd=parent_fd)
        os.close(parent_fd)
    return {**plan, "status": "materialized", "publication_method": method,
            "output_sha256": manifest["output_sha256"],
            "manifest_sha256": _digest(payloads["subset_manifest.json"])}
