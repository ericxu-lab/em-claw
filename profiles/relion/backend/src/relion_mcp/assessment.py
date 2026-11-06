"""Schema validation and scientific decision guards for 3D assessments.

The validator deliberately operates on JSON-shaped values.  This keeps the
scientific decision contract usable both from the MCP surface and directly
from Python without coupling it to RELION job execution or image rendering.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

SCHEMA_VERSION = "relion.3d-assessment/v1"

REFERENCE_UTILITIES = frozenset({"credible", "ambiguous", "unusable"})
MORPHOLOGIES = frozenset(
    {"coherent", "mixed", "artifact_dominated", "unassessable"}
)
DISPOSITIONS = frozenset({"primary", "fallback", "preserve", "reject", "human_review"})
PAIR_RELATIONS = frozenset({"same", "distinct", "unassessable"})
ALIGNMENTS = frozenset(
    {"aligned", "common_frame_verified", "unaligned", "unassessable"}
)
BIOLOGICAL_INTERPRETATIONS = frozenset({"supported", "unknown", "unsupported"})
DECISIONS = frozenset(
    {"auto_accept", "primary_fallback", "defer", "human_gate", "auto_reject"}
)
CONFIDENCE_LEVELS = frozenset({"low", "medium", "high"})
MAP_ALIGNMENT_STATUSES = frozenset(
    {
        "aligned",
        "identity_optimal",
        "ambiguous_alignment",
        "unstable_alignment",
        "fit_unverified",
        "boundary_limited",
        "insufficient_overlap",
        "optimizer_stopped",
    }
)
VERIFIED_MAP_ALIGNMENT_STATUSES = frozenset({"aligned", "identity_optimal"})
NEXT_ACTIONS = frozenset(
    {"advance_primary", "activate_fallback", "repeat", "retry", "await_human", "stop"}
)
AUTONOMOUS_DECISIONS = frozenset({"auto_accept", "auto_reject", "primary_fallback"})

_SAME_PATHOLOGY = re.compile(
    r"\bsame\s+(?:underlying\s+)?patholog(?:y|ies)\b", re.IGNORECASE
)
_MAP_PIXELS_ROOT = re.compile(r"^map_pixels_pair_sha256:[0-9a-f]{64}$")
_MAP_ANALYSIS_ID = re.compile(r"^map_comparison_analysis_sha256:[0-9a-f]{64}$")


def _issue(code: str, path: str, message: str) -> dict[str, str]:
    return {"code": code, "path": path, "message": message}


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _nonempty_string(
    record: Mapping[str, Any],
    key: str,
    path: str,
    errors: list[dict[str, str]],
) -> str | None:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(
            _issue(
                "invalid_field",
                f"{path}.{key}",
                f"{key} must be a non-empty string.",
            )
        )
        return None
    return value.strip()


def _enum_string(
    record: Mapping[str, Any],
    key: str,
    allowed: frozenset[str],
    path: str,
    errors: list[dict[str, str]],
) -> str | None:
    value = record.get(key)
    if not isinstance(value, str) or value not in allowed:
        errors.append(
            _issue(
                "invalid_enum",
                f"{path}.{key}",
                f"{key} must be one of {sorted(allowed)}.",
            )
        )
        return None
    return value


def _record_list(
    value: object,
    path: str,
    errors: list[dict[str, str]],
    *,
    allow_empty: bool,
) -> list[Mapping[str, Any]]:
    if not _is_sequence(value):
        errors.append(_issue("invalid_type", path, f"{path} must be a list of objects."))
        return []
    records: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            errors.append(
                _issue(
                    "invalid_type",
                    f"{path}[{index}]",
                    f"{path}[{index}] must be an object.",
                )
            )
            continue
        records.append(item)
    if not records and not allow_empty:
        errors.append(_issue("empty_collection", path, f"{path} must not be empty."))
    return records


def _recommended_decisions(
    decision: object,
    *,
    valid: bool,
    provisional_alternative_allowed: bool,
) -> list[str]:
    if valid and decision in DECISIONS:
        return [str(decision)]
    if provisional_alternative_allowed:
        return ["primary_fallback", "defer"]
    return ["defer"]


def validate_3d_assessment(
    *,
    classes: list[dict[str, Any]],
    pairs: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    decision: str,
    confidence: str,
    next_action: str,
    approval_flag: bool,
    rationale: str = "",
) -> dict[str, Any]:
    """Validate a 3D class assessment and enforce decision safety guards.

    Input records are JSON-shaped.  Every class requires ``class_id``,
    ``reference_utility``, ``morphology``, and ``disposition``.  Every pair
    requires ``class_a``, ``class_b``, ``relation``, ``alignment``,
    ``biological_interpretation``, and the machine-readable boolean
    ``same_pathology_claimed``.  Evidence records require ``evidence_id``,
    ``source``, and ``root_source``; derived views with the same root count as
    one independent source. A high-confidence aligned ``same`` or ``distinct``
    relation must include a complete evidence record naming
    ``relion_compare_maps`` with a verified tool status, canonical pixel root,
    analysis identifier, fixed-transform declaration, and the matching
    ``class_a``/``class_b`` pair binding.

    Final ``auto_accept``/``auto_reject`` decisions require high confidence.
    ``primary_fallback`` also permits medium confidence for approved,
    provisional processing: its primary must be credible and assessable,
    and no class may be rejected. The caller must preserve outputs and record
    ranked alternatives, activation triggers, and the task budget in run state.
    Routine downstream processing may continue within that budget with an
    assessment at each checkpoint; this read-only validator does not verify
    external state or grant final scientific acceptance.

    ``defer`` records unresolved science with ``retry``, ``repeat``, or ``stop``
    without requesting human input or advancing a branch. Only a valid
    ``human_gate`` with ``await_human`` sets ``human_input_required``. Invalid
    submissions require caller repair and reassessment, not a human question;
    recommendations never change the submitted decision or confidence.

    The function reports all structural and scientific errors rather than
    raising on the first one.  A caller must require both ``schema_valid`` and
    ``decision_allowed`` (or simply ``valid``) before acting on a decision.
    """
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    class_records = _record_list(classes, "classes", errors, allow_empty=False)
    pair_records = _record_list(pairs, "pairs", errors, allow_empty=True)
    evidence_records = _record_list(evidence, "evidence", errors, allow_empty=False)

    class_ids: set[str] = set()
    class_assessments: list[tuple[str | None, str | None, str | None, str | None]] = []
    reference_utilities: list[str] = []
    for index, record in enumerate(class_records):
        path = f"classes[{index}]"
        class_id = _nonempty_string(record, "class_id", path, errors)
        utility = _enum_string(
            record, "reference_utility", REFERENCE_UTILITIES, path, errors
        )
        morphology = _enum_string(record, "morphology", MORPHOLOGIES, path, errors)
        disposition = _enum_string(record, "disposition", DISPOSITIONS, path, errors)
        class_assessments.append((class_id, utility, morphology, disposition))
        if class_id is not None:
            if class_id in class_ids:
                errors.append(
                    _issue(
                        "duplicate_class_id",
                        f"{path}.class_id",
                        f"Duplicate class_id {class_id!r}.",
                    )
                )
            class_ids.add(class_id)
        if utility is not None:
            reference_utilities.append(utility)
        if utility in {"credible", "ambiguous"} and disposition == "reject":
            warnings.append(
                _issue(
                    "class_disposition_conflict",
                    path,
                    f"A {utility} reference is marked for rejection; review its disposition.",
                )
            )
        if utility == "credible" and morphology == "artifact_dominated":
            warnings.append(
                _issue(
                    "class_quality_conflict",
                    path,
                    "A credible reference is described as artifact_dominated.",
                )
            )

    distinct_same_pathology_pairs: list[str] = []
    unverified_distinct_pairs: list[str] = []
    unverified_same_pairs: list[str] = []
    verified_relation_pairs: list[str] = []
    claimed_relation_pairs: list[tuple[tuple[str, str], str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    rationale_same_pathology = isinstance(rationale, str) and bool(
        _SAME_PATHOLOGY.search(rationale)
    )
    if not isinstance(rationale, str):
        errors.append(
            _issue("invalid_type", "rationale", "rationale must be a string.")
        )

    for index, record in enumerate(pair_records):
        path = f"pairs[{index}]"
        class_a = _nonempty_string(record, "class_a", path, errors)
        class_b = _nonempty_string(record, "class_b", path, errors)
        relation = _enum_string(record, "relation", PAIR_RELATIONS, path, errors)
        alignment = _enum_string(record, "alignment", ALIGNMENTS, path, errors)
        biological = _enum_string(
            record,
            "biological_interpretation",
            BIOLOGICAL_INTERPRETATIONS,
            path,
            errors,
        )
        same_pathology = record.get("same_pathology_claimed")
        if not isinstance(same_pathology, bool):
            errors.append(
                _issue(
                    "invalid_field",
                    f"{path}.same_pathology_claimed",
                    "same_pathology_claimed must be a boolean.",
                )
            )
            same_pathology = False
        pair_rationale = record.get("rationale", "")
        if not isinstance(pair_rationale, str):
            errors.append(
                _issue(
                    "invalid_type",
                    f"{path}.rationale",
                    "Optional pair rationale must be a string.",
                )
            )
            pair_rationale = ""
        text_claim = bool(_SAME_PATHOLOGY.search(pair_rationale))

        if class_a is not None and class_a not in class_ids:
            errors.append(
                _issue(
                    "unknown_class",
                    f"{path}.class_a",
                    f"Pair references unknown class {class_a!r}.",
                )
            )
        if class_b is not None and class_b not in class_ids:
            errors.append(
                _issue(
                    "unknown_class",
                    f"{path}.class_b",
                    f"Pair references unknown class {class_b!r}.",
                )
            )
        if class_a is not None and class_a == class_b:
            errors.append(
                _issue(
                    "self_pair",
                    path,
                    "A pair must contain two different classes.",
                )
            )
        pair_key: tuple[str, str] | None = None
        if class_a is not None and class_b is not None and class_a != class_b:
            pair_key = tuple(sorted((class_a, class_b)))
            if pair_key in seen_pairs:
                errors.append(
                    _issue("duplicate_pair", path, f"Duplicate class pair {pair_key!r}.")
                )
            seen_pairs.add(pair_key)

        if relation == "distinct" and (
            same_pathology or text_claim or rationale_same_pathology
        ):
            label = (
                f"{class_a}/{class_b}"
                if class_a is not None and class_b is not None
                else path
            )
            distinct_same_pathology_pairs.append(label)
        pair_label = (
            f"{class_a}/{class_b}"
            if class_a is not None and class_b is not None
            else path
        )
        if relation in {"same", "distinct"} and pair_key is not None:
            claimed_relation_pairs.append((pair_key, pair_label))
        if relation == "distinct" and alignment in {"unaligned", "unassessable"}:
            label = (
                f"{class_a}/{class_b}"
                if class_a is not None and class_b is not None
                else path
            )
            unverified_distinct_pairs.append(label)
            warnings.append(
                _issue(
                    "distinct_pair_alignment_unverified",
                    f"{path}.alignment",
                    "Structural distinctness should be confirmed in an aligned common frame.",
                )
            )
        if relation == "same" and alignment in {"unaligned", "unassessable"}:
            unverified_same_pairs.append(pair_label)
            warnings.append(
                _issue(
                    "same_pair_alignment_unverified",
                    f"{path}.alignment",
                    "Structural sameness should be confirmed in an aligned common frame.",
                )
            )
        if relation in {"same", "distinct"} and alignment in {
            "aligned",
            "common_frame_verified",
        }:
            verified_relation_pairs.append(
                f"{class_a}/{class_b}"
                if class_a is not None and class_b is not None
                else path
            )
        if biological == "supported" and alignment in {"unaligned", "unassessable"}:
            warnings.append(
                _issue(
                    "biological_claim_alignment_unverified",
                    f"{path}.biological_interpretation",
                    "A supported biological interpretation should use aligned comparison evidence.",
                )
            )

    evidence_ids: set[str] = set()
    roots: dict[str, list[str]] = {}
    explicit_alignment_evidence_ids: list[str] = []
    verified_alignment_evidence_ids: list[str] = []
    verified_alignment_evidence_by_pair: dict[tuple[str, str], list[str]] = {}
    for index, record in enumerate(evidence_records):
        path = f"evidence[{index}]"
        evidence_id = _nonempty_string(record, "evidence_id", path, errors)
        source = _nonempty_string(record, "source", path, errors)
        root_source = _nonempty_string(record, "root_source", path, errors)
        if evidence_id is not None:
            if evidence_id in evidence_ids:
                errors.append(
                    _issue(
                        "duplicate_evidence_id",
                        f"{path}.evidence_id",
                        f"Duplicate evidence_id {evidence_id!r}.",
                    )
                )
            evidence_ids.add(evidence_id)
        if evidence_id is not None and root_source is not None:
            roots.setdefault(root_source, []).append(evidence_id)
        if (
            evidence_id is not None
            and source is not None
            and "relion_compare_maps" in source.casefold()
        ):
            explicit_alignment_evidence_ids.append(evidence_id)
            analysis_id = _nonempty_string(record, "analysis_id", path, errors)
            evidence_class_a = _nonempty_string(record, "class_a", path, errors)
            evidence_class_b = _nonempty_string(record, "class_b", path, errors)
            alignment_status = _enum_string(
                record,
                "alignment_status",
                MAP_ALIGNMENT_STATUSES,
                path,
                errors,
            )
            fixed_transform = record.get("fixed_transform_across_cutoffs")
            fixed_transform_valid = fixed_transform is True
            if not fixed_transform_valid:
                errors.append(
                    _issue(
                        "invalid_map_alignment_evidence",
                        f"{path}.fixed_transform_across_cutoffs",
                        "relion_compare_maps evidence must declare "
                        "fixed_transform_across_cutoffs=true.",
                    )
                )
            root_valid = bool(
                root_source is not None and _MAP_PIXELS_ROOT.fullmatch(root_source)
            )
            if root_source is not None and not root_valid:
                errors.append(
                    _issue(
                        "invalid_map_pixels_root",
                        f"{path}.root_source",
                        "relion_compare_maps evidence must use the exact canonical "
                        "map_pixels_pair_sha256:<64 lowercase hex> root returned by "
                        "the tool, not a whole-file, header, or placeholder hash.",
                    )
                )
            analysis_id_valid = bool(
                analysis_id is not None and _MAP_ANALYSIS_ID.fullmatch(analysis_id)
            )
            if analysis_id is not None and not analysis_id_valid:
                errors.append(
                    _issue(
                        "invalid_map_comparison_analysis_id",
                        f"{path}.analysis_id",
                        "analysis_id must be the exact "
                        "map_comparison_analysis_sha256:<64 lowercase hex> value "
                        "returned by relion_compare_maps.",
                    )
                )
            evidence_pair: tuple[str, str] | None = None
            pair_binding_valid = True
            if evidence_class_a is not None and evidence_class_a not in class_ids:
                pair_binding_valid = False
                errors.append(
                    _issue(
                        "unknown_class",
                        f"{path}.class_a",
                        f"Alignment evidence references unknown class {evidence_class_a!r}.",
                    )
                )
            if evidence_class_b is not None and evidence_class_b not in class_ids:
                pair_binding_valid = False
                errors.append(
                    _issue(
                        "unknown_class",
                        f"{path}.class_b",
                        f"Alignment evidence references unknown class {evidence_class_b!r}.",
                    )
                )
            if (
                evidence_class_a is not None
                and evidence_class_b is not None
                and evidence_class_a == evidence_class_b
            ):
                pair_binding_valid = False
                errors.append(
                    _issue(
                        "self_pair",
                        path,
                        "Alignment evidence must bind two different classes.",
                    )
                )
            elif evidence_class_a is not None and evidence_class_b is not None:
                evidence_pair = tuple(sorted((evidence_class_a, evidence_class_b)))

            if (
                alignment_status in VERIFIED_MAP_ALIGNMENT_STATUSES
                and fixed_transform_valid
                and root_valid
                and analysis_id_valid
                and pair_binding_valid
                and evidence_pair is not None
            ):
                verified_alignment_evidence_ids.append(evidence_id)
                verified_alignment_evidence_by_pair.setdefault(
                    evidence_pair,
                    [],
                ).append(evidence_id)
            elif alignment_status is not None:
                warnings.append(
                    _issue(
                        "map_alignment_unassessable",
                        f"{path}.alignment_status",
                        f"relion_compare_maps status {alignment_status!r} cannot verify "
                        "a same/distinct relation.",
                    )
                )

    relation_pairs_without_verified_evidence = [
        label
        for pair_key, label in claimed_relation_pairs
        if pair_key not in verified_alignment_evidence_by_pair
    ]
    if verified_relation_pairs and not explicit_alignment_evidence_ids:
        warnings.append(
            _issue(
                "pair_alignment_evidence_unlinked",
                "evidence",
                "A same/distinct relation marked aligned or common-frame-verified should "
                "cite relion_compare_maps in evidence.source; affected pair(s): "
                f"{verified_relation_pairs}.",
            )
        )
    elif relation_pairs_without_verified_evidence:
        warnings.append(
            _issue(
                "pair_alignment_evidence_unbound",
                "evidence",
                "Registered-map evidence is missing, unsuccessful, malformed, or bound "
                "to a different class pair; affected pair(s): "
                f"{relation_pairs_without_verified_evidence}.",
            )
        )

    if not isinstance(decision, str) or decision not in DECISIONS:
        errors.append(
            _issue(
                "invalid_enum",
                "decision",
                f"decision must be one of {sorted(DECISIONS)}.",
            )
        )
    if not isinstance(confidence, str) or confidence not in CONFIDENCE_LEVELS:
        errors.append(
            _issue(
                "invalid_enum",
                "confidence",
                f"confidence must be one of {sorted(CONFIDENCE_LEVELS)}.",
            )
        )
    if not isinstance(next_action, str) or next_action not in NEXT_ACTIONS:
        errors.append(
            _issue(
                "invalid_enum",
                "next_action",
                f"next_action must be one of {sorted(NEXT_ACTIONS)}.",
            )
        )
    if not isinstance(approval_flag, bool):
        errors.append(
            _issue(
                "invalid_field",
                "approval_flag",
                "approval_flag must be a boolean.",
            )
        )

    structural_error_count = len(errors)
    decision_errors: list[dict[str, str]] = []
    if (
        isinstance(decision, str)
        and decision in {"auto_accept", "auto_reject"}
        and confidence != "high"
    ):
        decision_errors.append(
            _issue(
                "autonomous_decision_requires_high_confidence",
                "confidence",
                f"{decision} requires high confidence; gather evidence or use an "
                "approved provisional primary_fallback at medium confidence.",
            )
        )
    if decision == "primary_fallback" and (
        not isinstance(confidence, str) or confidence not in {"medium", "high"}
    ):
        decision_errors.append(
            _issue(
                "primary_fallback_requires_medium_or_high_confidence",
                "confidence",
                "primary_fallback requires medium or high confidence; gather "
                "evidence, retry, or stop before advancing a low-confidence branch.",
            )
        )
    if (
        confidence == "low"
        and isinstance(next_action, str)
        and next_action in {"advance_primary", "activate_fallback"}
    ):
        decision_errors.append(
            _issue(
                "low_confidence_cannot_advance",
                "next_action",
                "Low confidence requires evidence gathering, retry, or stop "
                "before advancing or activating a branch.",
            )
        )
    if decision == "defer" and isinstance(next_action, str) and next_action in {
        "advance_primary",
        "activate_fallback",
        "await_human",
    }:
        decision_errors.append(
            _issue(
                "defer_requires_nonadvancing_action",
                "next_action",
                "defer requires retry, repeat, or stop; it cannot advance a "
                "branch or request human input.",
            )
        )
    elif decision != "human_gate" and next_action == "await_human":
        decision_errors.append(
            _issue(
                "await_human_requires_human_gate",
                "next_action",
                "await_human requires an explicit human_gate decision. Repair "
                "the assessment; validation errors do not request user input.",
            )
        )
    if decision == "human_gate" and isinstance(next_action, str) and next_action in {
        "advance_primary",
        "activate_fallback",
    }:
        decision_errors.append(
            _issue(
                "human_gate_cannot_advance",
                "next_action",
                "human_gate cannot advance a branch; use an eligible authorized "
                "primary_fallback or defer for further evidence gathering.",
            )
        )
    if decision == "human_gate" and isinstance(next_action, str) and next_action in {
        "retry",
        "repeat",
        "stop",
    }:
        warnings.append(
            _issue(
                "human_gate_without_human_input",
                "decision",
                "Use defer for evidence gathering, retry, repeat, or stop that "
                "does not require a human answer; this legacy human_gate "
                "does not request input.",
            )
        )
    if (
        isinstance(decision, str)
        and decision in AUTONOMOUS_DECISIONS
        and approval_flag is False
    ):
        decision_errors.append(
            _issue(
                "autonomous_decision_not_approved",
                "approval_flag",
                f"{decision} is forbidden when approval_flag is false.",
            )
        )

    if decision == "auto_accept":
        accepted = [
            (class_id, utility, morphology)
            for class_id, utility, morphology, disposition in class_assessments
            if disposition == "primary"
        ]
        if len(accepted) != 1:
            decision_errors.append(
                _issue(
                    "auto_accept_requires_one_primary",
                    "classes",
                    "auto_accept requires exactly one class with disposition=primary; "
                    f"found {len(accepted)}.",
                )
            )
        else:
            accepted_id, accepted_utility, accepted_morphology = accepted[0]
            if accepted_utility != "credible":
                decision_errors.append(
                    _issue(
                        "auto_accept_primary_not_credible",
                        "classes",
                        "The primary class in an auto_accept decision must be credible; "
                        f"{accepted_id!r} is {accepted_utility!r}.",
                    )
                )
            if accepted_morphology == "artifact_dominated":
                decision_errors.append(
                    _issue(
                        "auto_accept_primary_artifact_dominated",
                        "classes",
                        f"The primary class {accepted_id!r} in an auto_accept decision "
                        "is described as artifact_dominated.",
                    )
                )
        if distinct_same_pathology_pairs:
            decision_errors.append(
                _issue(
                    "auto_accept_distinct_same_pathology_conflict",
                    "decision",
                    "auto_accept is forbidden because distinct pair(s) are claimed to "
                    f"share the same pathology: {distinct_same_pathology_pairs}.",
                )
            )

    if decision == "primary_fallback":
        primaries = [
            (class_id, utility)
            for class_id, utility, morphology, disposition in class_assessments
            if disposition == "primary"
        ]
        if len(primaries) != 1:
            decision_errors.append(
                _issue(
                    "primary_fallback_requires_one_primary",
                    "classes",
                    "primary_fallback requires exactly one class with disposition=primary; "
                    f"found {len(primaries)}.",
                )
            )
        elif primaries[0][1] != "credible":
            decision_errors.append(
                _issue(
                    "primary_fallback_primary_not_credible",
                    "classes",
                    "The primary class in a primary_fallback decision must be credible; "
                    f"{primaries[0][0]!r} is {primaries[0][1]!r}.",
                )
            )
        if reference_utilities and all(
            utility == "unusable" for utility in reference_utilities
        ):
            decision_errors.append(
                _issue(
                    "primary_fallback_all_unusable",
                    "decision",
                    "primary_fallback is forbidden when every class is unusable.",
                )
            )
        if confidence == "medium":
            if any(
                disposition == "reject"
                for _, _, _, disposition in class_assessments
            ):
                decision_errors.append(
                    _issue(
                        "provisional_primary_fallback_cannot_reject",
                        "classes",
                        "A medium-confidence primary_fallback is provisional and "
                        "must preserve every class; no disposition may be reject.",
                    )
                )
            if any(
                morphology in {"artifact_dominated", "unassessable"}
                and disposition == "primary"
                for _, _, morphology, disposition in class_assessments
            ):
                decision_errors.append(
                    _issue(
                        "provisional_primary_fallback_primary_unassessable",
                        "classes",
                        "A provisional primary must have assessable morphology "
                        "and cannot be artifact_dominated.",
                    )
                )
            warnings.append(
                _issue(
                    "provisional_primary_fallback",
                    "decision",
                    "Medium confidence permits approved reversible downstream processing "
                    "within the task budget, with reassessment at each checkpoint. "
                    "Preserve all outputs and record ranked alternatives, activation "
                    "triggers, and the task budget in run state; this "
                    "decision is not final scientific acceptance.",
                )
            )

    if decision == "auto_reject":
        protected = sorted(
            {
                utility
                for utility in reference_utilities
                if utility in {"credible", "ambiguous"}
            }
        )
        if protected:
            decision_errors.append(
                _issue(
                    "auto_reject_protected_class",
                    "decision",
                    "auto_reject is forbidden when any class has reference_utility "
                    f"in {protected}; use an eligible primary_fallback or defer.",
                )
            )
        if distinct_same_pathology_pairs:
            decision_errors.append(
                _issue(
                    "auto_reject_distinct_same_pathology_conflict",
                    "decision",
                    "auto_reject is forbidden because distinct pair(s) are claimed to "
                    f"share the same pathology: {distinct_same_pathology_pairs}.",
                )
            )
    if confidence == "high" and len(roots) < 2:
        decision_errors.append(
            _issue(
                "high_confidence_requires_independent_roots",
                "confidence",
                "High confidence requires at least two independent root evidence sources; "
                f"found {len(roots)}.",
            )
        )
    if confidence == "high" and unverified_distinct_pairs:
        decision_errors.append(
            _issue(
                "high_confidence_distinct_alignment_unverified",
                "pairs",
                "High-confidence distinct relations require aligned or verified-common-frame "
                f"comparison; unverified pair(s): {unverified_distinct_pairs}.",
            )
        )
    if confidence == "high" and unverified_same_pairs:
        decision_errors.append(
            _issue(
                "high_confidence_same_alignment_unverified",
                "pairs",
                "High-confidence same relations require aligned or verified-common-frame "
                f"comparison; unverified pair(s): {unverified_same_pairs}.",
            )
        )
    if (
        confidence == "high"
        and claimed_relation_pairs
        and relation_pairs_without_verified_evidence
    ):
        decision_errors.append(
            _issue(
                "high_confidence_relation_requires_alignment_evidence",
                "evidence",
                "High-confidence same/distinct relations require an explicit registered-map "
                "comparison with status aligned or identity_optimal; matching headers, a "
                "failed/unstable fit, or a submitted alignment enum "
                "is insufficient, and evidence must bind the assessed classes; missing "
                f"pair(s): {relation_pairs_without_verified_evidence}.",
            )
        )

    errors.extend(decision_errors)
    schema_valid = structural_error_count == 0
    decision_allowed = schema_valid and not decision_errors
    valid = schema_valid and decision_allowed
    primary_assessments = [
        (utility, morphology)
        for _, utility, morphology, disposition in class_assessments
        if disposition == "primary"
    ]
    provisional_alternative_allowed = (
        schema_valid
        and approval_flag is True
        and isinstance(confidence, str)
        and confidence in {"medium", "high"}
        and len(primary_assessments) == 1
        and primary_assessments[0][0] == "credible"
        and primary_assessments[0][1] in {"coherent", "mixed"}
        and all(disposition != "reject" for _, _, _, disposition in class_assessments)
        and bool(decision_errors)
        and {item["code"] for item in decision_errors}.issubset(
            {
                "autonomous_decision_requires_high_confidence",
                "auto_reject_protected_class",
                "human_gate_cannot_advance",
            }
        )
    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "valid": valid,
        "schema_valid": schema_valid,
        "decision_allowed": decision_allowed,
        "requested_decision": decision,
        "recommended_decisions": _recommended_decisions(
            decision,
            valid=valid,
            provisional_alternative_allowed=provisional_alternative_allowed,
        ),
        "human_input_required": (
            valid and decision == "human_gate" and next_action == "await_human"
        ),
        "confidence": confidence,
        "next_action": next_action,
        "approval_flag": approval_flag,
        "evidence_summary": {
            "evidence_count": len(evidence_records),
            "independent_root_source_count": len(roots),
            "root_sources": sorted(roots),
            "evidence_by_root_source": {
                root: sorted(ids) for root, ids in sorted(roots.items())
            },
            "explicit_alignment_evidence_ids": sorted(
                explicit_alignment_evidence_ids
            ),
            "verified_alignment_evidence_ids": sorted(
                verified_alignment_evidence_ids
            ),
            "verified_alignment_evidence_by_pair": {
                "/".join(pair): sorted(ids)
                for pair, ids in sorted(verified_alignment_evidence_by_pair.items())
            },
        },
        "errors": errors,
        "warnings": warnings,
    }
    return result
