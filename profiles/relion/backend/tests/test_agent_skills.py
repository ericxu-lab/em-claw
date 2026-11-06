from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / ".agents" / "skills"


def _parse_skill(path: Path) -> tuple[dict[str, object], str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} is missing YAML frontmatter"
    _, frontmatter, body = text.split("---", 2)
    metadata = yaml.safe_load(frontmatter)
    assert isinstance(metadata, dict), f"{path} frontmatter must be a mapping"
    return metadata, body


def test_repository_skills_have_valid_metadata_and_links() -> None:
    skill_files = sorted(SKILLS_ROOT.glob("*/SKILL.md"))
    assert skill_files

    for skill_file in skill_files:
        metadata, body = _parse_skill(skill_file)
        skill_name = skill_file.parent.name

        assert set(metadata) == {"name", "description"}
        assert metadata["name"] == skill_name
        assert isinstance(metadata["description"], str)
        assert metadata["description"].strip()
        assert "TODO" not in body

        agents_file = skill_file.parent / "agents" / "openai.yaml"
        if agents_file.exists():
            agents = yaml.safe_load(agents_file.read_text(encoding="utf-8"))
            interface = agents["interface"]
            assert interface["display_name"]
            assert 25 <= len(interface["short_description"]) <= 64
            assert f"${skill_name}" in interface["default_prompt"]


def test_orchestrator_routes_resolution_optimization() -> None:
    skill_file = SKILLS_ROOT / "relion-spa-orchestrator" / "SKILL.md"
    _, body = _parse_skill(skill_file)
    reference = skill_file.parent / "references" / "resolution-optimization.md"

    assert "references/resolution-optimization.md" in body
    assert reference.is_file()
    assert not (SKILLS_ROOT / "relion-resolution-optimizer").exists()

    reference_body = reference.read_text(encoding="utf-8")
    assert "[RELION SPA orchestrator](../SKILL.md)" in reference_body
    assert "[Refine3D and post-processing](refine3d-postprocessing.md)" in reference_body
    assert "contract_gap" in reference_body
    assert "documentation-relevant uncertainty is `high`" in reference_body
    assert "Do not perform any external documentation or web search" in reference_body


def test_resolution_optimization_encodes_particle_attrition_recovery_gate() -> None:
    skill_file = SKILLS_ROOT / "relion-spa-orchestrator" / "SKILL.md"
    reference = skill_file.parent / "references" / "resolution-optimization.md"
    reference_body = reference.read_text(encoding="utf-8")
    _, marker, recovery_section = reference_body.partition(
        "Use rule `resolution_revisit_broader_2d_particles`"
    )
    assert marker
    policy = " ".join(recovery_section.split("\n### ", 1)[0].split())

    assert "3.5–10 Å" in policy
    assert "`N_current < 100,000`" in policy
    assert "`N_current < 10,000`" not in policy
    assert "planning flags, not evidence" in policy
    assert "pre-symmetry gold-standard C1 consensus" in policy
    assert "same C1 particle branch before any point-group symmetry is imposed" in policy
    assert "Do not evaluate this gate from a symmetry-imposed FSC" in policy
    assert "multiplying or dividing by the symmetry order" in policy
    assert "matched gold-standard C1 Refine3D and PostProcess baseline first" in policy
    assert "before comparing a non-C1 symmetry branch" in policy

    for lineage_field in (
        "`N_current`",
        "`N_broad`",
        "`N_excluded = N_broad - N_current`",
    ):
        assert lineage_field in policy

    assert "Require no duplicate particle identities" in policy
    assert "`current_ids ⊆ broad_ids`" in policy
    assert "`overlap_count = N_current`" in policy
    assert "exact `broad_ids - current_ids` set difference" in policy
    assert "best accepted, reference-suitable pre-symmetry C1 consensus" in policy
    assert "the newest job, the sharpest-looking map" in policy
    assert "the map with the smallest reported FSC resolution" in policy
    assert "unsharpened C1 reconstruction source" in policy
    assert "provenance-recorded `initial_lowpass_A`" in policy
    assert "Never use a symmetry-imposed map as a neutral C1 reference" in policy

    assert "InitialModel/ab-initio class assignment" in policy
    assert "including a mixed lineage" in policy
    assert "Treat this as provenance repair" in policy
    assert "full appropriate accepted 2D particle set" in policy
    assert "as the input to global Class3D" in policy
    assert "Never refine only the particles assigned to the chosen InitialModel class" in policy

    assert "input fingerprint equals the broader accepted 2D pool" in policy
    assert "next-ranked coherent, compatible population" in policy
    assert "one bounded global Class3D on the full broader accepted 2D set" in policy
    assert "Preserve confirmed junk and supported distinct states separately" in policy
    assert "If all exclusions are confirmed junk or supported distinct states" in policy
    assert "if their dispositions remain ambiguous, first inspect the missing evidence or run a bounded eligible validation" in policy
    assert "Do not send all excluded particles directly to final Refine3D" in policy
    assert "Choose exactly one recovery route" in policy

    assert "primary broader-pool global Class3D recovery in C1" in policy
    assert "A plausible or shortlisted symmetry candidate alone" in policy
    assert "matched Class3D branch within the processing or optimization scope" in policy
    assert "same broader-pool particle fingerprint" in policy
    assert "change only `symmetry`" in policy
    assert "asymmetric, partially occupied, pseudo-symmetric, or minority states" in policy
    assert "same-particle matched C1 and candidate-symmetry Refine3D/PostProcess" in policy

    assert "fresh gold-standard C1 Refine3D" in policy
    assert "both half maps with intact provenance" in policy
    assert "compatible validated global mask" in policy
    assert "matched PostProcess" in policy
    assert "phase-randomized FSC behavior" in policy
    assert "convergence evidence" in policy
    assert "directional-resolution" in policy
    assert "orientation diagnostics when available" in policy
    assert "positive evidence root outside half-map derivatives" in policy
    assert "Increased particle count or a smaller nominal resolution alone is insufficient" in policy

    assert "stable `selection_checkpoint_id`" in policy
    assert "separate `candidate_branch_fingerprint`" in policy
    assert "An accepted, rejected, or inconclusive recovery closes that checkpoint" in policy
    assert "Do not retrigger it because the recovery created a new particle" in policy


def test_stage_tilt_spa_ctf_refinement_is_multi_round_and_evidence_gated() -> None:
    skill_dir = SKILLS_ROOT / "relion-spa-orchestrator"
    metadata, skill_body = _parse_skill(skill_dir / "SKILL.md")
    optimization_body = (
        skill_dir / "references" / "resolution-optimization.md"
    ).read_text(encoding="utf-8")

    description = " ".join(str(metadata["description"]).split())
    skill_policy = " ".join(skill_body.split())
    assert "stage-tilted acquisition CTF refinement" in description
    for provenance_field in (
        "stage_tilt:",
        "status: unknown",
        "angles_deg_by_group: {}",
        "group_assignment: null",
        "provenance: null",
        "`confirmed`, `absent`, `mixed`, or `unknown`",
        "particle Euler angles are not acquisition-stage metadata",
    ):
        assert provenance_field in skill_policy

    _, marker, section = optimization_body.partition(
        "#### Stage-tilted SPA CTF-refinement loop"
    )
    assert marker
    policy = " ".join(section.split("\n### 5. Execute through the orchestrator", 1)[0].split())

    for scope_guard in (
        "single-particle micrographs",
        "Do not apply it to a tomography tilt series",
        "the SPA `ctf_refinement` operation is not `TomoCtfrefine`",
        "Particle `rlnAngleTilt` is an orientation estimate",
        "Treat specimen stage tilt as acquisition geometry, not beam tilt",
        "does not itself authorize `fit_beamtilt`",
        "anisotropic magnification, trefoil, or fourth-order aberration",
    ):
        assert scope_guard in policy

    for eligibility_guard in (
        "positive evidence to prioritize per-particle defocus refinement",
        "after the ordinary signal and identifiability gates pass",
        "not as a waiver of those gates",
        "`diagnosis: not_yet_eligible`",
        "Do not weaken the signal requirements for beam tilt or higher-order aberrations",
    ):
        assert eligibility_guard in policy

    for repeat_guard in (
        "initial per-particle-defocus round plus at least one evidence-gated repeat",
        "A one-pass plan is incomplete unless an explicit stop condition is met",
        "Do not execute a fixed number of rounds regardless of evidence",
        "last assessed usable Refine3D particle STAR and its latest matching PostProcess STAR",
        "`fit_defocus: Per-particle`",
        "fresh gold-standard Refine3D with preserved half sets",
        "matched PostProcess before judging the round or launching another one",
        "round counter",
        "predeclared exit test",
        "A credible `medium`-confidence primary may continue provisionally",
        "Never chain an evidence-gated repeat directly from an unassessed CtfRefine output",
        "require an exact allowed source such as an explicit user override",
        "record a `contract_gap` rather than relabeling them as workflow defaults",
        "record the evidence gap and try available reports or compatible existing artifacts",
        "Do not accept a round from the fit likelihood alone",
    ):
        assert repeat_guard in policy

    assert "do not count those three chained jobs as three accepted stage-tilt cycles" in policy
    assert "Stop when any valid round is non-distinguishable or worse" in policy
    assert "Keep the last accepted branch primary" in policy
    assert "Never continue merely to reach the pre-budgeted round count" in policy


def test_class3d_reference_treatment_prefers_a_safe_best_accepted_map() -> None:
    skill_file = SKILLS_ROOT / "relion-spa-orchestrator" / "SKILL.md"
    reference = skill_file.parent / "references" / "initial-model-class3d.md"
    reference_body = " ".join(reference.read_text(encoding="utf-8").split())

    assert "Rank accepted candidate maps by their reference utility" in reference_body
    assert "highest-ranked accepted, reference-suitable map" in reference_body
    assert "Do not define `best_reference` by job recency" in reference_body
    assert "compatible best accepted pre-symmetry C1 consensus" in reference_body
    assert "unsharpened reconstruction source" in reference_body
    assert "more-strongly-filtered or de novo C1 fallback" in reference_body
    assert "accepts both `initial_volume` and `refined_volume`" in reference_body
    assert "direct typed `three_d_classification` job" in reference_body
    assert "Do not relabel the refined map as an initial volume" in reference_body


def test_initial_model_recovery_is_preserving_inexpensive_and_bounded() -> None:
    skill_dir = SKILLS_ROOT / "relion-spa-orchestrator"
    metadata, skill_body = _parse_skill(skill_dir / "SKILL.md")
    skill_policy = " ".join(skill_body.split())
    description = " ".join(str(metadata["description"]).split())
    particle_policy = " ".join(
        (skill_dir / "references" / "particle-preparation.md")
        .read_text(encoding="utf-8")
        .split()
    )
    initial_model = (
        skill_dir / "references" / "initial-model-class3d.md"
    ).read_text(encoding="utf-8")
    initial_policy = " ".join(initial_model.split())

    assert "initial-model recovery" in description
    assert "initial_model_seed_partitions: []" in skill_policy
    assert "initial_model_recovery: []" in skill_policy
    assert "stage-specific seed status does not replace" in skill_policy

    for partition_guard in (
        "role-specific seed ledger over that same source inventory",
        "**seed-eligible** (`seed_eligible`)",
        "**uncertain** (`uncertain`)",
        "**clear junk** (`clear_junk`)",
        "only particles already supported as `proven_junk`",
        "never choose this role from sharpness, class population, occupancy, or ranker score alone",
        "exclusion from the current seed is not a permanent rejection",
        "original source fingerprint and count",
        "complete selection history",
        "roles are disjoint and exhaustive",
        "particles omitted only by balancing or a compute cap remain seed-eligible",
        "assign separate `seed_branch_id` values",
        "do not blend them merely to raise particle count",
    ):
        assert partition_guard in particle_policy

    for readiness_guard in (
        "Assign exactly one stage-local `seed_status`",
        "`SEED_READY`: `reference_utility: credible`",
        "`UNCERTAIN`: `reference_utility: ambiguous`",
        "`REJECT_CANDIDATE`: `reference_utility: unusable`",
        "Insufficient evidence always means `UNCERTAIN`",
        "plausible physical dimensions",
        "no gross artifact that prevents reference use",
        "several distinct credible 2D appearances",
        "shared physical pixel size and field of view",
        "same conservative physical low-pass",
        "central-slab or occupancy view is not a reprojected class match",
        "`relion_compare_map_projections` with at least three explicit `selected_class_numbers`",
        "There is no score threshold or metadata flag that auto-accepts a match or seed",
        "Create the three PNG evidence paths only with `relion_publish_initial_model_evidence`",
        "Publication proves which PNG bytes were inspected, not that their scientific interpretation passes the gate",
        "provisionally usable as a low-resolution starting reference",
        "not a validated final structure",
        "It authorizes Class3D only through the matching `initial_model_accepted` record",
        "uncertain particles, omitted eligible particles, and alternate seed branches for recovery",
        "Do not require high-resolution detail, a repeated initialization, or an additional Class3D or Refine3D job",
        "Keep `seed_status` separate from the parent `decision`, `confidence`, `next_action`, and class `disposition` enums",
    ):
        assert readiness_guard in initial_policy

    _, marker, recovery = initial_model.partition("## Poor-map recovery")
    assert marker
    recovery_policy = " ".join(recovery.split("\n## Reference treatment", 1)[0].split())
    for recovery_guard in (
        "a completed attempt whose assessed candidates contain no `SEED_READY` map",
        "Record observations separately from hypotheses",
        "Propose at most three evidence-supported alternatives",
        "recommend exactly one affordable, reversible next action",
        "change seed composition, targeted 2D classification, extraction/centring correction, or an initialization/symmetry correction",
        "the hypothesis and exact evidence for and against it",
        "the proposed change and the main factor held responsible",
        "the expected improvement and a predeclared stop condition",
        "estimated computational cost",
        "the particle-preservation plan",
        "Change one main factor when practical",
        "reassess with the same inexpensive readiness criteria",
        "Do not automatically tighten selection",
        "repeat a failed or unchanged approach",
        "the cheapest missing observation",
    ):
        assert recovery_guard in recovery_policy


def test_class3d_no_alignment_is_typed_and_covered_by_processing_scope() -> None:
    skill_dir = SKILLS_ROOT / "relion-spa-orchestrator"
    _, skill_body = _parse_skill(skill_dir / "SKILL.md")
    class3d_body = (skill_dir / "references" / "initial-model-class3d.md").read_text(
        encoding="utf-8"
    )
    masks_body = (skill_dir / "references" / "symmetry-and-masks.md").read_text(
        encoding="utf-8"
    )
    policy = " ".join((skill_body + class3d_body + masks_body).split())

    for contract_token in (
        "`reference_mask`",
        "`fn_mask`",
        "`alignment: false`",
        "`dont_skip_align == No`",
        "That authority also covers no-alignment validation",
        "matched unmasked/aligned baseline",
        "trusted orientations",
    ):
        assert contract_token in policy

    assert "Current Class3D inputs do not expose a native reference mask" not in policy
    assert "disable alignment only after a typed mapping exists" not in policy


def test_symmetry_scan_policy_forbids_inference_from_the_capped_shortlist() -> None:
    reference = (
        SKILLS_ROOT
        / "relion-spa-orchestrator"
        / "references"
        / "symmetry-and-masks.md"
    )
    policy = " ".join(reference.read_text(encoding="utf-8").split())

    assert (
        "Absence from the capped candidates array is not evidence that a group "
        "failed the scan heuristics."
    ) in policy
    assert "No symmetry-scan outcome accepts or rejects biological symmetry." in policy
    for field in ("`scan_summary`", "`result_completeness`", "`hypothesis_results`"):
        assert field in policy


def test_solvent_flattened_fsc_selection_is_typed_and_evidence_gated() -> None:
    skill_dir = SKILLS_ROOT / "relion-spa-orchestrator"
    metadata, skill_body = _parse_skill(skill_dir / "SKILL.md")
    refine_body = (skill_dir / "references" / "refine3d-postprocessing.md").read_text(
        encoding="utf-8"
    )
    masks_body = (skill_dir / "references" / "symmetry-and-masks.md").read_text(
        encoding="utf-8"
    )

    description = " ".join(str(metadata["description"]).split())
    routing = " ".join(skill_body.split())
    assert "solvent-flattened FSC" in description
    assert "solvent-corrected FSC" in description
    assert "autonomous solvent-flattened FSC (solvent-corrected FSC) selection" in routing
    assert "including `allowed_sources`, `authority_order`, and `strategy_selectable`" in routing
    assert "does not expose that full authority metadata" not in routing

    _, marker, section = refine_body.partition(
        "## Solvent-flattened FSC decision and validation"
    )
    assert marker
    policy = " ".join(section.split("\n## Refinement acceptance", 1)[0].split())

    for contract_guard in (
        "`strategy_intent`",
        "typed `mask` input",
        "`solvent_correct_fsc: true`",
        "`fn_mask`",
        "`do_solvent_fsc == Yes`",
        "do not use a shell command or `other_args`",
    ):
        assert contract_guard in policy

    for evidence_guard in (
        "relatively small part of a correctly sized box",
        "stability across a reasonable nearby mask",
        "preserved matched baseline",
        "`solvent_correct_fsc: false`",
        "A smaller nominal resolution alone is insufficient",
        "corresponding unfiltered half-map pair",
        "corrected PostProcess FSC for final resolution assessment",
        "`human_gate`",
        "A focused mask still requires a supported target and matched baseline",
        "Final `high` confidence is not needed merely to test this option",
    ):
        assert evidence_guard in policy

    membrane_policy = " ".join(masks_body.split())
    for membrane_guard in (
        "whole protein plus coherent, reproducible lipid or detergent/micelle density",
        "supported in both half-maps",
        "soft margin",
        "entire diffuse micelle",
        "Preserve nearby mask variants",
        "Membrane association alone never authorizes solvent-corrected FSC",
    ):
        assert membrane_guard in membrane_policy


def test_particle_preparation_tools_match_executable_interfaces() -> None:
    reference = (
        SKILLS_ROOT
        / "relion-spa-orchestrator"
        / "references"
        / "particle-preparation.md"
    )
    policy = reference.read_text(encoding="utf-8")
    referenced_tools = set(re.findall(r"`(relion_[a-z0-9_]+)`", policy))
    tree = ast.parse((REPO_ROOT / "src/relion_mcp/server.py").read_text())
    definitions: dict[str, ast.FunctionDef] = {}
    annotations: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if not (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "tool"
            ):
                continue
            definitions[node.name] = node
            annotation = next(
                keyword.value
                for keyword in decorator.keywords
                if keyword.arg == "annotations"
            )
            assert isinstance(annotation, ast.Name)
            annotations[node.name] = annotation.id

    # A routed skill must never require a tool the server does not expose.
    assert referenced_tools
    assert referenced_tools <= definitions.keys()
    for name in (
        "relion_plan_micrograph_subset",
        "relion_audit_particle_selection",
        "relion_validate_2d_class_review",
        "relion_render_particle_boxes",
    ):
        assert annotations[name] == "READ_ONLY"

    # The documented subset write requires the reviewed plan fingerprint and
    # explicit confirmation; the skill must not imply an unguarded native job.
    materialize = definitions["relion_materialize_micrograph_subset"]
    assert annotations[materialize.name] == "WRITE_ADDITIVE"
    for function in (definitions["relion_plan_micrograph_subset"], materialize):
        positional = [argument.arg for argument in function.args.args]
        defaults = dict(
            zip(
                positional[len(positional) - len(function.args.defaults):],
                function.args.defaults,
            )
        )
        defaults.update(
            {
                argument.arg: default
                for argument, default in zip(
                    function.args.kwonlyargs, function.args.kw_defaults
                )
                if default is not None
            }
        )
        parameters = positional + [a.arg for a in function.args.kwonlyargs]
        # Existing labels can be partitioned safely, while a fresh LoG
        # bootstrap can still plan and materialize micrographs alone.
        assert "particles_star" in parameters
        assert ast.literal_eval(defaults["particles_star"]) is None
        if function is materialize:
            assert "expected_plan_sha256" in parameters
            assert "expected_plan_sha256" not in defaults
            assert ast.literal_eval(defaults["confirm"]) is False


def test_topaz_fom_evaluation_starts_from_tutorial_prior() -> None:
    reference = (
        SKILLS_ROOT
        / "relion-spa-orchestrator"
        / "references"
        / "particle-preparation.md"
    )
    policy = " ".join(reference.read_text(encoding="utf-8").split())

    for baseline_guard in (
        "Evaluate the tutorial prior `minimum_autopick_fom = -3` first",
        "`baseline_source: tutorial_prior`",
        "threshold already validated for that exact Topaz model and dataset",
        "report the exact retained count",
        "before choosing any alternative threshold",
        "Do not select the first FOM value from score percentiles alone",
    ):
        assert baseline_guard in policy

    for comparison_guard in (
        "same subset for every candidate threshold comparison",
        "does not scientifically reject the `-3` baseline",
        "Do not auto-reject or skip the `-3` baseline solely from retained count",
        "matched 2D classification",
        "Preserve `-3` as an inactive fallback",
    ):
        assert comparison_guard in policy


def test_duplicate_removal_is_rendered_metadata_safe_and_reversible() -> None:
    reference = (
        SKILLS_ROOT
        / "relion-spa-orchestrator"
        / "references"
        / "particle-preparation.md"
    )
    policy = " ".join(reference.read_text(encoding="utf-8").split())

    for contract_guard in (
        "`relion_render_remove_duplicate_particles`",
        "`relion_remove_duplicate_particles` as an additive write",
        "Require authoritative `rlnMicrographPixelSize` values to agree across every referenced optics group",
        "never substitute `rlnMicrographOriginalPixelSize`",
        "particle `rlnImagePixelSize`",
        "require an explicit verified value and provenance record even when those other pixel sizes agree",
        "split them rather than forcing one scalar",
        "An explicit value must match authoritative metadata",
        "Require all native SPA coordinate, micrograph, optics-group, and X/Y origin columns",
    ):
        assert contract_guard in policy

    for publication_guard in (
        "implicit `<stem>_removed.star` sidecar",
        "Both are no-clobber destinations",
        "accepts native RELION's zero-byte sidecar as exactly zero removals",
        "requires counts plus the `rlnImageName` and complete-row metadata multisets to partition the input",
        "requires at least one retained row",
        "Preserve the removed-particle sidecar as reversible provenance",
        "stop on any metadata, structural, identity, count, or publication validation failure",
    ):
        assert publication_guard in policy

    assert "current particle selection does not expose native duplicate" not in policy


def test_class3d_similar_classes_require_staged_union_adjudication() -> None:
    skill_file = SKILLS_ROOT / "relion-spa-orchestrator" / "SKILL.md"
    _, skill_body = _parse_skill(skill_file)
    skill_policy = " ".join(skill_body.split())
    skill_dir = skill_file.parent
    class3d_body = (skill_dir / "references" / "initial-model-class3d.md").read_text(
        encoding="utf-8"
    )
    _, marker, section = class3d_body.partition(
        "## Similar-class singleton-versus-union refinement adjudication"
    )
    assert marker
    policy = " ".join(section.split("\n## Golden K=2 assessment scenario", 1)[0].split())

    # This is one bounded staged experiment, not general permission to advance
    # every plausible class or materialize the union before its evidence gate.
    assert "`three_d_validate_similar_class_union`" in skill_policy
    assert "narrow staged matched-control exception" in skill_policy
    assert "first complete the declared singleton-refinement comparison" in skill_policy
    assert "Materialize and refine the exact particle union only when" in skill_policy
    assert "Treat both stages as one bounded experiment" in skill_policy
    assert "continue from one primary afterward" in skill_policy

    for trigger_guard in (
        "exactly one pair from the same final Class3D data STAR",
        "selection-credible, internally coherent, mutually exclusive",
        "source `same`/`high` is not required merely to gather this evidence",
        "covered by a processing or optimization request when all entry checks and budgets pass",
        "Autonomous union materialization additionally requires",
        "A `medium`-confidence compatibility assessment can justify this preserved experiment",
        "are required for final union promotion, not merely to gather valid evidence",
        "Failed or unassessable refined-map alignment never authorizes a union",
        "a `same` relation at `high` confidence",
        "an evidence root outside map-pixel derivatives",
        "Do not define \"quite similar\" with a universal Pearson correlation",
        "Similar global resolutions alone never trigger this rule",
        "high-confidence `distinct` relation established after valid alignment blocks a union",
        "source-map `unassessable` relation permits only the bounded singleton diagnostic stage when its independent entry checks pass",
        "A group larger than two",
        "request new authorization only if it exceeds the existing scope or limits",
        "do not run them serially by default",
    ):
        assert trigger_guard in policy

    for selection_guard in (
        "`relion_plan_particle_class_selection`",
        "`relion_run_particle_class_selection`",
        "`ids(A) ∩ ids(B) = ∅`",
        "Only after that gate passes, materialize A ∪ B",
        "`ids(A ∪ B) = ids(A) ∪ ids(B)`",
        "`N(A ∪ B) = N(A) + N(B)`",
        "`rlnRandomSubset` value is unchanged",
        "Planner counts alone do not prove identity or half-set preservation",
    ):
        assert selection_guard in policy

    for matched_control in (
        "gold-standard C1 Refine3D for A and B",
        "same neutral, unsharpened, strongly low-passed pre-selection C1 reference",
        "Do not seed each singleton from its own class map",
        "Hold grid, hand, initial low-pass",
        "one broad, non-branch-privileging validated soft global mask",
        "matched PostProcess pixel/MTF/B-factor/FSC policy",
        "same neutral reference, settings, global mask, and policies used by both singletons",
        "N singleton branches and one conditional exact union",
        "Count the N+1 bundle as one scientific hypothesis",
    ):
        assert matched_control in policy

    for comparison_guard in (
        "corrected masked/unmasked/phase-randomized FSC",
        "`relion_compare_maps`",
        "keep the transform fixed across sensitivity views",
        "outside those shared particle/half-map derivatives",
        "Use `relion_render_3d_classes` for source montage and context",
        "Do not feed an added union map through stale Class3D model metadata",
        "do not repurpose `relion_compare_symmetry`",
        "A failed or incompatible registration is a comparison blocker",
    ):
        assert comparison_guard in policy

    for decision_guard in (
        "quality improvement over the best singleton",
        "remain compatible with both singleton maps",
        "Increased particle count, a smaller nominal resolution",
        "similarity alone is insufficient",
        "singleton refinements diverge into reproducibly distinct maps",
        "the union smears or loses density",
        "combination has not demonstrated a benefit",
        "Mark the experiment inconclusive when any identity/half-set audit",
        "advance one primary only",
        "`biological_interpretation: unknown`",
    ):
        assert decision_guard in policy

    for staged_gate in (
        "Only after that gate passes, materialize A ∪ B",
        "N singleton branches and one conditional exact union",
        "relation passes the assessable compatibility check with no distinct or minority-state evidence",
    ):
        assert staged_gate in policy

    for immediate_union_policy in (
        "one bounded three-branch experiment",
        "never a prerequisite for creating or running A ∪ B",
        "Complete this audit before running any of the three refinements",
        "gold-standard C1 Refine3D for A, B, and A ∪ B",
    ):
        assert immediate_union_policy not in policy

    particle_policy = " ".join(
        (skill_dir / "references" / "particle-preparation.md")
        .read_text(encoding="utf-8")
        .split()
    )
    refine_policy = " ".join(
        (skill_dir / "references" / "refine3d-postprocessing.md")
        .read_text(encoding="utf-8")
        .split()
    )
    optimization_policy = " ".join(
        (skill_dir / "references" / "resolution-optimization.md")
        .read_text(encoding="utf-8")
        .split()
    )
    assert "planner's count contract alone is not this provenance audit" in particle_policy
    assert "materialize A and B independently from the same final Class3D" in particle_policy
    assert "Materialize A ∪ B independently" in particle_policy
    assert "only after the refined singleton-map gate passes" in particle_policy
    assert "treat each singleton and the exact union as a distinct upstream particle state" in refine_policy
    assert "eligible singleton/conditional-union diagnostic" in refine_policy
    assert "apply this entire reference identically to every matched branch" in refine_policy
    assert "for the staged union rule, the declared matched sibling" in refine_policy
    assert "using one declared transform across unsharpened and common-low-pass sensitivity views" in refine_policy
    assert "route it through `three_d_validate_similar_class_union`" in optimization_policy


def test_localres_is_deferred_until_final_selection_or_explicit_request() -> None:
    skill_dir = SKILLS_ROOT / "relion-spa-orchestrator"
    _, skill_body = _parse_skill(skill_dir / "SKILL.md")
    skill_policy = " ".join(skill_body.split())
    references = {
        name: " ".join(
            (skill_dir / "references" / name).read_text(encoding="utf-8").split()
        )
        for name in (
            "initial-model-class3d.md",
            "refine3d-postprocessing.md",
            "resolution-optimization.md",
            "symmetry-and-masks.md",
            "map-interpretation.md",
        )
    }

    for required in (
        "Keep RELION LocalRes deferred",
        "Do not render, run, or request approval for `local_resolution` as a routine",
        "include `local_resolution` as an opt-in final characterization",
        "Run it earlier only when the user explicitly requests it",
        "do not equate localized density inspection with a new LocalRes job",
    ):
        assert required in skill_policy

    class3d = references["initial-model-class3d.md"]
    assert "Do not run LocalRes independently on all three candidate branches" in class3d
    assert "Select the primary from the three refined branches before scheduling LocalRes" in class3d
    assert "missing LocalRes output is not an A/B/A ∪ B adjudication blocker" in class3d

    refine = references["refine3d-postprocessing.md"]
    assert "Do not run RELION LocalRes as part of every Refine3D/PostProcess" in refine
    assert "opt in to `local_resolution` once for that selected map" in refine
    assert "Run LocalRes before selection only when the user explicitly requests it" in refine
    assert "does not trigger automatic LocalRes fan-out" in refine
    assert "global and local-resolution evidence plus" not in refine

    optimization = references["resolution-optimization.md"]
    assert "Defer LocalRes until the best map is selected" in optimization
    assert "opt in to LocalRes once for that selected map" in optimization

    symmetry = references["symmetry-and-masks.md"]
    assert "Do not run LocalRes on every symmetry candidate" in symmetry
    assert "Select the final C1 or symmetry branch before scheduling LocalRes" in symmetry

    interpretation = references["map-interpretation.md"]
    assert "does not authorize an intermediate LocalRes job" in interpretation
    assert "Missing optional LocalRes evidence does not block processing or require a user question" in interpretation
