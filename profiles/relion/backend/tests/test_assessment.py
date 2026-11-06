from __future__ import annotations

from typing import Any

import pytest

from relion_mcp.assessment import validate_3d_assessment


def _classes() -> list[dict[str, Any]]:
    return [
        {
            "class_id": "Class001",
            "reference_utility": "credible",
            "morphology": "coherent",
            "disposition": "primary",
        },
        {
            "class_id": "Class002",
            "reference_utility": "ambiguous",
            "morphology": "mixed",
            "disposition": "preserve",
        },
    ]


def _distinct_pair(*, same_pathology_claimed: bool = False) -> list[dict[str, Any]]:
    return [
        {
            "class_a": "Class001",
            "class_b": "Class002",
            "relation": "distinct",
            "alignment": "common_frame_verified",
            "biological_interpretation": "unknown",
            "same_pathology_claimed": same_pathology_claimed,
        }
    ]


def _independent_evidence() -> list[dict[str, Any]]:
    return [
        {
            "evidence_id": "registered_map_metrics",
            "source": "relion_compare_maps:InitialModel/job116/Class001-Class002",
            "root_source": "map_pixels_pair_sha256:" + "a" * 64,
            "analysis_id": "map_comparison_analysis_sha256:" + "b" * 64,
            "class_a": "Class001",
            "class_b": "Class002",
            "alignment_status": "aligned",
            "fixed_transform_across_cutoffs": True,
        },
        {
            "evidence_id": "particle_assignments",
            "source": "Class3D/job117/run_it025_data.star",
            "root_source": "Class3D/job117",
        },
    ]


def _validate(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "classes": _classes(),
        "pairs": _distinct_pair(),
        "evidence": _independent_evidence(),
        "decision": "primary_fallback",
        "confidence": "high",
        "next_action": "advance_primary",
        "approval_flag": True,
        "rationale": "The maps are coherent, stable, and structurally distinct.",
    }
    values.update(overrides)
    return validate_3d_assessment(**values)


def _codes(result: dict[str, Any]) -> set[str]:
    return {error["code"] for error in result["errors"]}


def test_primary_fallback_with_independent_evidence_is_valid() -> None:
    result = _validate()

    assert result["valid"] is True
    assert result["schema_valid"] is True
    assert result["decision_allowed"] is True
    assert result["errors"] == []
    assert result["evidence_summary"]["independent_root_source_count"] == 2
    assert result["evidence_summary"]["explicit_alignment_evidence_ids"] == [
        "registered_map_metrics"
    ]
    assert result["evidence_summary"]["verified_alignment_evidence_ids"] == [
        "registered_map_metrics"
    ]
    assert result["evidence_summary"]["verified_alignment_evidence_by_pair"] == {
        "Class001/Class002": ["registered_map_metrics"]
    }


def test_high_confidence_relation_cannot_claim_common_frame_without_alignment_evidence() -> None:
    evidence = _independent_evidence()
    evidence[0]["source"] = "InitialModel/job116/run_it100_model.star"

    result = _validate(evidence=evidence)

    assert result["schema_valid"] is True
    assert result["decision_allowed"] is False
    assert "high_confidence_relation_requires_alignment_evidence" in _codes(result)
    assert {warning["code"] for warning in result["warnings"]} == {
        "pair_alignment_evidence_unlinked"
    }


def test_failed_map_registration_cannot_verify_high_confidence_relation() -> None:
    evidence = _independent_evidence()
    evidence[0]["alignment_status"] = "unstable_alignment"

    result = _validate(evidence=evidence)

    assert result["schema_valid"] is True
    assert result["decision_allowed"] is False
    assert "high_confidence_relation_requires_alignment_evidence" in _codes(result)
    assert result["evidence_summary"]["verified_alignment_evidence_ids"] == []
    assert {warning["code"] for warning in result["warnings"]} == {
        "map_alignment_unassessable",
        "pair_alignment_evidence_unbound",
    }


def test_map_comparison_evidence_rejects_whole_file_style_root() -> None:
    evidence = _independent_evidence()
    evidence[0]["root_source"] = "map_pair_sha256:header-sensitive"

    result = _validate(evidence=evidence)

    assert result["schema_valid"] is False
    assert "invalid_map_pixels_root" in _codes(result)


def test_alignment_evidence_must_bind_the_assessed_pair() -> None:
    classes = _classes() + [
        {
            "class_id": "Class003",
            "reference_utility": "ambiguous",
            "morphology": "coherent",
            "disposition": "preserve",
        }
    ]
    evidence = _independent_evidence()
    evidence[0]["class_a"] = "Class001"
    evidence[0]["class_b"] = "Class003"

    result = _validate(classes=classes, evidence=evidence)

    assert result["schema_valid"] is True
    assert result["decision_allowed"] is False
    assert "high_confidence_relation_requires_alignment_evidence" in _codes(result)
    assert result["evidence_summary"]["verified_alignment_evidence_by_pair"] == {
        "Class001/Class003": ["registered_map_metrics"]
    }


def test_high_confidence_same_relation_cannot_be_unaligned() -> None:
    pair = _distinct_pair()
    pair[0]["relation"] = "same"
    pair[0]["alignment"] = "unassessable"

    result = _validate(pairs=pair)

    assert result["schema_valid"] is True
    assert result["decision_allowed"] is False
    assert "high_confidence_same_alignment_unverified" in _codes(result)
    assert {warning["code"] for warning in result["warnings"]} == {
        "same_pair_alignment_unverified"
    }


def test_job116_like_credible_and_distinct_classes_cannot_be_auto_rejected() -> None:
    result = _validate(decision="auto_reject")

    assert result["schema_valid"] is True
    assert result["decision_allowed"] is False
    assert result["valid"] is False
    assert "auto_reject_protected_class" in _codes(result)
    assert result["recommended_decisions"] == ["primary_fallback", "defer"]
    assert result["human_input_required"] is False


def test_auto_reject_requires_approval_flag() -> None:
    classes = [
        {
            "class_id": "Class001",
            "reference_utility": "unusable",
            "morphology": "artifact_dominated",
            "disposition": "reject",
        }
    ]
    result = _validate(
        classes=classes,
        pairs=[],
        evidence=[
            {
                "evidence_id": "map_quality",
                "source": "relion_render_map_report:Class001",
                "root_source": "map_pixels:Class001",
            },
            {
                "evidence_id": "particle_metadata",
                "source": "Class3D/job117/run_data.star",
                "root_source": "Class3D/job117",
            },
        ],
        decision="auto_reject",
        confidence="high",
        approval_flag=False,
    )

    assert result["decision_allowed"] is False
    assert _codes(result) == {"autonomous_decision_not_approved"}


def test_distinct_pair_cannot_support_same_pathology_auto_reject_rationale() -> None:
    classes = [
        {
            "class_id": "Class001",
            "reference_utility": "unusable",
            "morphology": "artifact_dominated",
            "disposition": "reject",
        },
        {
            "class_id": "Class002",
            "reference_utility": "unusable",
            "morphology": "artifact_dominated",
            "disposition": "reject",
        },
    ]
    result = _validate(
        classes=classes,
        pairs=_distinct_pair(same_pathology_claimed=True),
        decision="auto_reject",
        confidence="high",
    )

    assert result["decision_allowed"] is False
    assert "auto_reject_distinct_same_pathology_conflict" in _codes(result)


def test_obvious_same_pathology_text_is_a_secondary_guard() -> None:
    classes = [
        {
            "class_id": "Class001",
            "reference_utility": "unusable",
            "morphology": "artifact_dominated",
            "disposition": "reject",
        },
        {
            "class_id": "Class002",
            "reference_utility": "unusable",
            "morphology": "artifact_dominated",
            "disposition": "reject",
        },
    ]
    result = _validate(
        classes=classes,
        decision="auto_reject",
        confidence="high",
        rationale="Both classes show the same pathology.",
    )

    assert "auto_reject_distinct_same_pathology_conflict" in _codes(result)


def test_derived_views_from_one_root_do_not_support_high_confidence() -> None:
    result = _validate(
        evidence=[
            {
                "evidence_id": "raw_slices",
                "source": "InitialModel/job116/classes.png",
                "root_source": "InitialModel/job116",
            },
            {
                "evidence_id": "threshold_surface",
                "source": "InitialModel/job116/surface.png",
                "root_source": "InitialModel/job116",
            },
        ]
    )

    assert result["schema_valid"] is True
    assert result["decision_allowed"] is False
    assert "high_confidence_requires_independent_roots" in _codes(result)
    summary = result["evidence_summary"]
    assert summary["evidence_count"] == 2
    assert summary["independent_root_source_count"] == 1
    assert summary["evidence_by_root_source"] == {
        "InitialModel/job116": ["raw_slices", "threshold_surface"]
    }


def test_schema_rejects_unknown_pair_class_and_missing_pair_flag() -> None:
    result = _validate(
        pairs=[
            {
                "class_a": "Class001",
                "class_b": "Class999",
                "relation": "distinct",
                "alignment": "aligned",
                "biological_interpretation": "unknown",
            }
        ],
        confidence="medium",
    )

    assert result["schema_valid"] is False
    assert result["decision_allowed"] is False
    assert {"unknown_class", "invalid_field"}.issubset(_codes(result))


def test_all_unusable_classes_can_be_auto_rejected_and_recommendation_is_stable() -> None:
    classes = [
        {
            "class_id": "Class001",
            "reference_utility": "unusable",
            "morphology": "artifact_dominated",
            "disposition": "reject",
        },
        {
            "class_id": "Class002",
            "reference_utility": "unusable",
            "morphology": "artifact_dominated",
            "disposition": "reject",
        },
    ]
    result = _validate(
        classes=classes,
        decision="auto_reject",
        next_action="repeat",
    )

    assert result["valid"] is True
    assert result["recommended_decisions"] == ["auto_reject"]


@pytest.mark.parametrize(
    "decision", ["auto_accept", "auto_reject", "primary_fallback"]
)
def test_approval_flag_blocks_every_autonomous_decision(decision: str) -> None:
    result = _validate(decision=decision, approval_flag=False)

    assert result["decision_allowed"] is False
    assert "autonomous_decision_not_approved" in _codes(result)


def test_primary_fallback_requires_exactly_one_credible_primary() -> None:
    no_primary = _classes()
    no_primary[0]["disposition"] = "preserve"
    ambiguous_primary = _classes()
    ambiguous_primary[0]["reference_utility"] = "ambiguous"

    missing = _validate(classes=no_primary)
    not_credible = _validate(classes=ambiguous_primary)

    assert "primary_fallback_requires_one_primary" in _codes(missing)
    assert "primary_fallback_primary_not_credible" in _codes(not_credible)


def test_all_unusable_classes_cannot_be_primary_fallback() -> None:
    classes = _classes()
    for item in classes:
        item["reference_utility"] = "unusable"

    result = _validate(classes=classes)

    assert "primary_fallback_primary_not_credible" in _codes(result)
    assert "primary_fallback_all_unusable" in _codes(result)


@pytest.mark.parametrize("decision", ["auto_accept", "auto_reject"])
@pytest.mark.parametrize("confidence", ["medium", "low"])
def test_final_decisions_require_high_confidence(
    decision: str, confidence: str
) -> None:
    result = _validate(decision=decision, confidence=confidence)

    assert result["schema_valid"] is True
    assert result["decision_allowed"] is False
    assert "autonomous_decision_requires_high_confidence" in _codes(result)


def test_medium_primary_fallback_can_gather_a_second_evidence_channel() -> None:
    result = _validate(
        confidence="medium",
        pairs=[],
        evidence=[
            {
                "evidence_id": "direct_map_inspection",
                "source": "InitialModel/job116/classes.png",
                "root_source": "InitialModel/job116",
            }
        ],
    )

    assert result["valid"] is True
    assert result["recommended_decisions"] == ["primary_fallback"]
    assert result["evidence_summary"]["independent_root_source_count"] == 1
    assert {warning["code"] for warning in result["warnings"]} == {
        "provisional_primary_fallback"
    }


def test_medium_primary_fallback_still_requires_approval() -> None:
    result = _validate(confidence="medium", approval_flag=False)

    assert result["decision_allowed"] is False
    assert _codes(result) == {"autonomous_decision_not_approved"}


def test_medium_primary_fallback_cannot_reject_an_alternative() -> None:
    classes = _classes()
    classes[1]["reference_utility"] = "unusable"
    classes[1]["disposition"] = "reject"

    result = _validate(classes=classes, confidence="medium")

    assert result["schema_valid"] is True
    assert result["decision_allowed"] is False
    assert _codes(result) == {"provisional_primary_fallback_cannot_reject"}


@pytest.mark.parametrize("morphology", ["artifact_dominated", "unassessable"])
def test_medium_primary_fallback_requires_assessable_primary(
    morphology: str,
) -> None:
    classes = _classes()
    classes[0]["morphology"] = morphology

    result = _validate(classes=classes, confidence="medium")

    assert result["decision_allowed"] is False
    assert _codes(result) == {
        "provisional_primary_fallback_primary_unassessable"
    }


def test_medium_primary_fallback_still_requires_credible_primary() -> None:
    classes = _classes()
    classes[0]["reference_utility"] = "ambiguous"

    result = _validate(classes=classes, confidence="medium")

    assert result["decision_allowed"] is False
    assert _codes(result) == {"primary_fallback_primary_not_credible"}


def test_low_confidence_cannot_advance_a_provisional_primary() -> None:
    result = _validate(confidence="low")

    assert result["decision_allowed"] is False
    assert _codes(result) == {
        "primary_fallback_requires_medium_or_high_confidence",
        "low_confidence_cannot_advance",
    }


@pytest.mark.parametrize("next_action", ["advance_primary", "activate_fallback"])
def test_low_confidence_cannot_advance_under_a_human_gate_label(
    next_action: str,
) -> None:
    result = _validate(
        decision="human_gate",
        confidence="low",
        next_action=next_action,
    )

    assert result["decision_allowed"] is False
    assert _codes(result) == {
        "low_confidence_cannot_advance",
        "human_gate_cannot_advance",
    }


@pytest.mark.parametrize("confidence", ["medium", "high"])
@pytest.mark.parametrize("next_action", ["advance_primary", "activate_fallback"])
@pytest.mark.parametrize("approval_flag", [False, True])
def test_human_gate_cannot_bypass_branch_advancement_guards(
    confidence: str, next_action: str, approval_flag: bool,
) -> None:
    result = _validate(
        decision="human_gate",
        confidence=confidence,
        next_action=next_action,
        approval_flag=approval_flag,
    )

    assert result["valid"] is False
    assert _codes(result) == {"human_gate_cannot_advance"}
    assert result["human_input_required"] is False
    assert result["recommended_decisions"] == (
        ["primary_fallback", "defer"] if approval_flag else ["defer"]
    )


@pytest.mark.parametrize("next_action", ["repeat", "retry", "stop"])
def test_low_confidence_can_record_evidence_gathering_or_stop(
    next_action: str,
) -> None:
    result = _validate(
        decision="human_gate",
        confidence="low",
        next_action=next_action,
        approval_flag=False,
    )

    assert result["valid"] is True
    assert result["next_action"] == next_action
    assert result["human_input_required"] is False
    assert "human_gate_without_human_input" in {
        warning["code"] for warning in result["warnings"]
    }


@pytest.mark.parametrize("next_action", ["repeat", "retry", "stop"])
def test_defer_records_unresolved_science_without_requesting_input(
    next_action: str,
) -> None:
    result = _validate(
        decision="defer",
        confidence="low",
        next_action=next_action,
        approval_flag=False,
    )

    assert result["valid"] is True
    assert result["recommended_decisions"] == ["defer"]
    assert result["human_input_required"] is False
    assert result["requested_decision"] == "defer"
    assert result["confidence"] == "low"


@pytest.mark.parametrize(
    "next_action", ["advance_primary", "activate_fallback", "await_human"]
)
def test_defer_cannot_advance_or_request_human_input(next_action: str) -> None:
    result = _validate(decision="defer", next_action=next_action)

    assert result["valid"] is False
    assert _codes(result) == {"defer_requires_nonadvancing_action"}
    assert result["human_input_required"] is False
    assert result["recommended_decisions"] == ["defer"]


def test_valid_explicit_human_gate_requests_input() -> None:
    result = _validate(
        decision="human_gate",
        confidence="low",
        next_action="await_human",
        approval_flag=False,
    )

    assert result["valid"] is True
    assert result["human_input_required"] is True
    assert result["recommended_decisions"] == ["human_gate"]


@pytest.mark.parametrize(
    "decision", ["auto_accept", "auto_reject", "primary_fallback", "defer"]
)
def test_only_explicit_human_gate_can_await_human(decision: str) -> None:
    classes = _classes()
    if decision == "auto_reject":
        for item in classes:
            item["reference_utility"] = "unusable"
            item["morphology"] = "artifact_dominated"
            item["disposition"] = "reject"

    result = _validate(
        classes=classes,
        decision=decision,
        next_action="await_human",
    )

    assert result["schema_valid"] is True
    assert result["valid"] is False
    assert result["human_input_required"] is False
    assert result["recommended_decisions"] == ["defer"]
    assert _codes(result) == {
        "defer_requires_nonadvancing_action" if decision == "defer"
        else "await_human_requires_human_gate"
    }


def test_invalid_human_gate_requires_caller_repair_without_requesting_input() -> None:
    classes = _classes()
    del classes[0]["morphology"]

    result = _validate(
        classes=classes,
        decision="human_gate",
        confidence="low",
        next_action="await_human",
        approval_flag=False,
    )

    assert result["valid"] is False
    assert result["schema_valid"] is False
    assert result["human_input_required"] is False
    assert result["recommended_decisions"] == ["defer"]
    assert result["requested_decision"] == "human_gate"
    assert result["next_action"] == "await_human"


@pytest.mark.parametrize("confidence", [None, "", "certain", {}, []])
def test_invalid_confidence_is_not_promoted_or_sent_to_a_human(
    confidence: Any,
) -> None:
    result = _validate(decision="auto_accept", confidence=confidence)

    assert result["schema_valid"] is False
    assert result["human_input_required"] is False
    assert result["recommended_decisions"] == ["defer"]
    assert result["confidence"] == confidence
    assert result["requested_decision"] == "auto_accept"


def test_medium_final_decision_recommends_eligible_provisional_processing() -> None:
    result = _validate(decision="auto_accept", confidence="medium")

    assert result["valid"] is False
    assert result["recommended_decisions"] == ["primary_fallback", "defer"]
    assert result["human_input_required"] is False
    assert result["requested_decision"] == "auto_accept"
    assert result["confidence"] == "medium"


@pytest.mark.parametrize(
    "overrides",
    [
        {"approval_flag": False},
        {"confidence": "low"},
        {"rationale": None},
        {"next_action": "invalid"},
        {"confidence": "high", "evidence": _independent_evidence()[:1]},
    ],
)
def test_invalid_final_decision_does_not_recommend_unsafe_promotion(
    overrides: dict[str, Any],
) -> None:
    values = {"decision": "auto_accept", "confidence": "medium"}
    values.update(overrides)

    result = _validate(**values)

    assert result["valid"] is False
    assert result["recommended_decisions"] == ["defer"]
    assert result["human_input_required"] is False


@pytest.mark.parametrize(
    ("index", "field", "value"),
    [
        (0, "morphology", "unassessable"),
        (0, "morphology", "artifact_dominated"),
        (0, "reference_utility", "ambiguous"),
        (0, "disposition", "preserve"),
        (1, "disposition", "primary"),
        (1, "disposition", "reject"),
    ],
)
def test_provisional_recommendation_requires_preserved_assessable_primary(
    index: int, field: str, value: str,
) -> None:
    classes = _classes()
    classes[index][field] = value

    result = _validate(classes=classes, decision="auto_reject")

    assert result["valid"] is False
    assert result["recommended_decisions"] == ["defer"]
    assert result["human_input_required"] is False


def test_high_confidence_distinct_pair_requires_verified_alignment() -> None:
    pairs = _distinct_pair()
    pairs[0]["alignment"] = "unaligned"

    high = _validate(pairs=pairs)
    gated = _validate(
        pairs=pairs,
        decision="human_gate",
        confidence="medium",
        next_action="await_human",
        approval_flag=False,
    )

    assert "high_confidence_distinct_alignment_unverified" in _codes(high)
    assert gated["valid"] is True
    assert {warning["code"] for warning in gated["warnings"]} == {
        "distinct_pair_alignment_unverified"
    }


def test_next_action_uses_orchestrator_controlled_vocabulary() -> None:
    result = _validate(next_action="seed Class3D")

    assert result["schema_valid"] is False
    assert "invalid_enum" in _codes(result)


def test_mcp_tool_exposes_the_same_read_only_validation() -> None:
    from relion_mcp.server import relion_validate_3d_assessment

    direct = _validate()
    via_tool = relion_validate_3d_assessment(
        classes=_classes(),
        pairs=_distinct_pair(),
        evidence=_independent_evidence(),
        decision="primary_fallback",
        confidence="high",
        next_action="advance_primary",
        approval_flag=True,
        rationale="The maps are coherent, stable, and structurally distinct.",
    )

    assert via_tool == direct
