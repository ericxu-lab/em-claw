from dataclasses import replace
from pathlib import Path

import pytest

from relion_mcp import particle_audit
from relion_mcp.particle_audit import (
    ParticleAuditError,
    audit_particle_selection,
    materialize_stratified_micrograph_subset,
    plan_stratified_micrograph_subset,
)
from relion_mcp.settings import ConfigurationError, Settings
from relion_mcp.star import parse_star


@pytest.fixture
def project(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    settings = Settings(
        mode="run", project_roots=(root,), pipeliner_executable="missing",
        image_handler_executable="missing", pipeline_name="default",
        add_job_timeout_seconds=90, utility_timeout_seconds=900,
    )
    return settings, root


OPTICS = """data_optics

loop_
_rlnOpticsGroup #1
_rlnOpticsGroupName #2
_rlnImagePixelSize #3
1 group1 1.5
2 group2 1.5

"""


def write_micrographs(path, rows=None):
    if rows is None:
        rows = [
            (f"Movies/mic{group}_{defocus}_{index}.mrc", group, defocus, f"session{group}")
            for group in (1, 2) for defocus in (10000, 20000) for index in range(4)
        ]
    path.write_text(OPTICS + """data_micrographs

loop_
_rlnMicrographName #1
_rlnOpticsGroup #2
_rlnDefocusU #3
_rlnCustomSession #4
""" + "\n".join(" ".join(map(str, row)) for row in rows) + "\n\n")
    return rows


def write_particles(path, rows=None, optics=OPTICS, extra_column=False):
    if rows is None:
        rows = [
            (f"{index:06}@Extract/job001/particles.mrcs", 1 if index <= 3 else 2,
             1 if index % 2 else 2, f"Movies/mic{index % 2}.mrc", 10 + index,
             20 + index, 10000 if index <= 3 else 20000, 1 if index % 2 else 2)
            for index in range(1, 7)
        ]
    columns = ["rlnImageName", "rlnClassNumber", "rlnOpticsGroup", "rlnMicrographName",
               "rlnCoordinateX", "rlnCoordinateY", "rlnDefocusU", "rlnRandomSubset"]
    if extra_column:
        columns.append("rlnAngleRot")
    path.write_text(optics + "data_particles\n\nloop_\n" +
                    "\n".join(f"_{column} #{index}" for index, column in enumerate(columns, 1)) +
                    "\n" + "\n".join(" ".join(map(str, row)) for row in rows) + "\n\n")
    return rows


def arguments():
    return {"output_name": "pilot", "training_count": 6, "heldout_count": 4, "seed": 29,
            "strata_columns": ["rlnOpticsGroup"], "numeric_bins": {"rlnDefocusU": [15000]}}


def test_subset_is_read_only_reproducible_and_order_independent(project):
    settings, root = project
    rows = write_micrographs(root / "micrographs.star")
    first = plan_stratified_micrograph_subset(settings, str(root), "micrographs.star", **arguments())
    repeated = plan_stratified_micrograph_subset(settings, str(root), "micrographs.star", **arguments())
    assert first == repeated
    assert not (root / "External").exists()
    write_micrographs(root / "micrographs.star", list(reversed(rows)))
    reordered = plan_stratified_micrograph_subset(settings, str(root), "micrographs.star", **arguments())
    assert first["training_micrographs"] == reordered["training_micrographs"]
    assert first["heldout_micrographs"] == reordered["heldout_micrographs"]
    assert first["plan_sha256"] != reordered["plan_sha256"]  # bytes are pinned too
    train, heldout, remaining = (set(first[f"{name}_micrographs"]) for name in ("training", "heldout", "remaining"))
    assert not train & heldout and not train & remaining and not heldout & remaining
    assert train | heldout | remaining == {row[0] for row in rows}
    assert all(group["training"] >= 1 and group["heldout"] == 1 for group in first["coverage"])


def test_numeric_edges_and_optics_join_have_explicit_semantics(project):
    settings, root = project
    write_micrographs(root / "micrographs.star", [
        ("a.mrc", 1, 15000, "s1"), ("b.mrc", 1, 10000, "s1"),
        ("c.mrc", 2, 15000, "s2"), ("d.mrc", 2, 20000, "s2"),
    ])
    plan = plan_stratified_micrograph_subset(
        settings, str(root), "micrographs.star", output_name="subset", training_count=3,
        heldout_count=1, strata_columns=["rlnOpticsGroupName"], numeric_bins={"rlnDefocusU": [15000]},
    )
    assert {group["stratum"]["rlnDefocusU"] for group in plan["coverage"]} == {
        "[-inf, 15000.0)", "[15000.0, +inf)"
    }
    assert {group["stratum"]["rlnOpticsGroupName"] for group in plan["coverage"]} == {"group1", "group2"}


def test_sparse_strata_and_insufficient_capacity_are_reported(project):
    settings, root = project
    write_micrographs(root / "micrographs.star", [
        ("rare.mrc", 1, 10000, "s1"),
        *((f"common{index}.mrc", 2, 20000, "s2") for index in range(8)),
    ])
    plan = plan_stratified_micrograph_subset(
        settings, str(root), "micrographs.star", output_name="subset", training_count=1,
        heldout_count=2,
    )
    assert plan["training_micrographs"] == ["rare.mrc"]
    assert any("Training capacity" in text for text in plan["limitations"])
    assert any("singleton" in text for text in plan["limitations"])


def test_materialization_preserves_rows_optics_extra_columns_and_snapshot(project):
    settings, root = project
    write_micrographs(root / "micrographs.star")
    source_bytes = (root / "micrographs.star").read_bytes()
    source = parse_star(root / "micrographs.star")
    plan = plan_stratified_micrograph_subset(settings, str(root), "micrographs.star", **arguments())
    result = materialize_stratified_micrograph_subset(
        settings, str(root), "micrographs.star", expected_plan_sha256=plan["plan_sha256"], confirm=True,
        **arguments(),
    )
    assert result["status"] == "materialized"
    assert result["pipeline_registered"] is False
    assert (root / result["source_snapshot"]).read_bytes() == source_bytes
    all_rows = []
    for path in result["outputs"].values():
        subset = parse_star(root / path)
        assert subset["optics"] == source["optics"]
        assert list(subset["micrographs"]) == list(source["micrographs"])
        all_rows.extend(zip(*subset["micrographs"].values(), strict=True))
    assert sorted(all_rows) == sorted(zip(*source["micrographs"].values(), strict=True))
    assert (root / "micrographs.star").read_bytes() == source_bytes
    assert (root / result["manifest"]).is_file()
    with pytest.raises(ParticleAuditError, match="already exists"):
        materialize_stratified_micrograph_subset(
            settings, str(root), "micrographs.star", expected_plan_sha256=plan["plan_sha256"], confirm=True,
            **arguments(),
        )


@pytest.mark.parametrize("mode,confirm", [("plan", True), ("run", False)])
def test_materialization_requires_run_mode_and_confirm(project, mode, confirm):
    settings, root = project
    write_micrographs(root / "micrographs.star")
    with pytest.raises(ConfigurationError):
        materialize_stratified_micrograph_subset(
            replace(settings, mode=mode), str(root), "micrographs.star", expected_plan_sha256="0" * 64,
            confirm=confirm, **arguments(),
        )
    assert not (root / "External").exists()


def test_materialization_rejects_changed_source_or_parameters(project):
    settings, root = project
    rows = write_micrographs(root / "micrographs.star")
    plan = plan_stratified_micrograph_subset(settings, str(root), "micrographs.star", **arguments())
    with pytest.raises(ParticleAuditError, match="hash mismatch"):
        materialize_stratified_micrograph_subset(
            settings, str(root), "micrographs.star", expected_plan_sha256=plan["plan_sha256"], confirm=True,
            **{**arguments(), "seed": 17},
        )
    write_micrographs(root / "micrographs.star", list(reversed(rows)))
    with pytest.raises(ParticleAuditError, match="hash mismatch"):
        materialize_stratified_micrograph_subset(
            settings, str(root), "micrographs.star", expected_plan_sha256=plan["plan_sha256"], confirm=True,
            **arguments(),
        )
    assert not (root / "External").exists()


def test_source_mutation_during_staging_does_not_publish(project, monkeypatch):
    settings, root = project
    write_micrographs(root / "micrographs.star")
    plan = plan_stratified_micrograph_subset(settings, str(root), "micrographs.star", **arguments())
    original = particle_audit._subset_bytes

    def mutate(*args):
        (root / "micrographs.star").write_text((root / "micrographs.star").read_text() + "# changed\n")
        return original(*args)

    monkeypatch.setattr(particle_audit, "_subset_bytes", mutate)
    with pytest.raises(ParticleAuditError, match="changed before publication"):
        materialize_stratified_micrograph_subset(
            settings, str(root), "micrographs.star", expected_plan_sha256=plan["plan_sha256"], confirm=True,
            **arguments(),
        )
    assert not (root / plan["output_directory"]).exists()
    assert not list((root / "External" / "MicrographSubsets").iterdir())


def test_destination_race_never_overwrites_another_writer(project, monkeypatch):
    settings, root = project
    write_micrographs(root / "micrographs.star")
    plan = plan_stratified_micrograph_subset(settings, str(root), "micrographs.star", **arguments())
    original = particle_audit._rename_directory_noreplace
    destination = root / plan["output_directory"]

    def competing_writer(*args):
        destination.mkdir()
        (destination / "owner.txt").write_text("another writer")
        return original(*args)

    monkeypatch.setattr(particle_audit, "_rename_directory_noreplace", competing_writer)
    with pytest.raises(ParticleAuditError, match="not replaced"):
        materialize_stratified_micrograph_subset(
            settings, str(root), "micrographs.star", expected_plan_sha256=plan["plan_sha256"], confirm=True,
            **arguments(),
        )
    assert list(destination.iterdir()) == [destination / "owner.txt"]
    assert (destination / "owner.txt").read_text() == "another writer"


def test_writes_and_reads_require_approved_project_roots(project):
    settings, root = project
    write_micrographs(root / "micrographs.star")
    with pytest.raises(ConfigurationError, match="RELION_PROJECT_ROOTS"):
        plan_stratified_micrograph_subset(
            replace(settings, project_roots=()), str(root), "micrographs.star", **arguments(),
        )
    with pytest.raises(ConfigurationError, match="RELION_PROJECT_ROOTS"):
        materialize_stratified_micrograph_subset(
            replace(settings, project_roots=()), str(root), "micrographs.star", **arguments(),
            expected_plan_sha256="0" * 64, confirm=True,
        )


@pytest.mark.parametrize("path", ["../outside.star", "/tmp/outside.star"])
def test_read_tools_reject_path_traversal(project, path):
    settings, root = project
    with pytest.raises(ConfigurationError):
        audit_particle_selection(settings, str(root), path)
    with pytest.raises(ConfigurationError):
        plan_stratified_micrograph_subset(settings, str(root), path, **arguments())


def test_symlink_escape_and_output_symlinks_are_rejected(project):
    settings, root = project
    write_micrographs(root.parent / "outside.star")
    (root / "micrographs.star").symlink_to(root.parent / "outside.star")
    with pytest.raises(ConfigurationError, match="outside"):
        plan_stratified_micrograph_subset(settings, str(root), "micrographs.star", **arguments())
    write_micrographs(root / "inside.star")
    (root / "External").symlink_to(root.parent, target_is_directory=True)
    with pytest.raises(ParticleAuditError, match="symbolic links"):
        plan_stratified_micrograph_subset(settings, str(root), "inside.star", **arguments())


@pytest.mark.parametrize("kwargs", [
    {"output_name": "../escape"}, {"numeric_bins": {"rlnDefocusU": [2, 1]}},
    {"numeric_bins": {"rlnDefocusU": [float("nan")]}}, {"training_count": 100},
    {"strata_columns": ["rlnMissingIceThickness"]}, {"training_count": True},
])
def test_invalid_subset_requests_fail_before_writes(project, kwargs):
    settings, root = project
    write_micrographs(root / "micrographs.star")
    with pytest.raises(ParticleAuditError):
        plan_stratified_micrograph_subset(settings, str(root), "micrographs.star", **{**arguments(), **kwargs})
    assert not (root / "External").exists()


def test_empty_heldout_and_remaining_outputs_are_valid_metadata(project):
    settings, root = project
    write_micrographs(root / "micrographs.star")
    kwargs = {"output_name": "all", "training_count": 16, "heldout_count": 0}
    plan = plan_stratified_micrograph_subset(settings, str(root), "micrographs.star", **kwargs)
    result = materialize_stratified_micrograph_subset(
        settings, str(root), "micrographs.star", expected_plan_sha256=plan["plan_sha256"], confirm=True, **kwargs,
    )
    for name in ("heldout", "remaining"):
        assert parse_star(root / result["outputs"][name])["micrographs"]["rlnMicrographName"] == []


def write_subset_labels(root):
    micrographs = write_micrographs(root / "micrographs.star")
    particles = [
        (f"{2 * index + particle + 1:06}@Extract/job001/particles.mrcs", 1, row[1], row[0],
         40 + particle, 50 + particle, row[2], 1 if particle == 0 else 2)
        for index, row in enumerate(micrographs) for particle in range(2)
    ]
    write_particles(root / "labels.star", particles)
    return particles


def test_curated_particle_labels_partition_by_micrographs_with_all_metadata_preserved(project):
    settings, root = project
    write_subset_labels(root)
    before = (root / "labels.star").read_bytes()
    source = parse_star(root / "labels.star")
    kwargs = {**arguments(), "particles_star": "labels.star"}
    plan = plan_stratified_micrograph_subset(settings, str(root), "micrographs.star", **kwargs)
    assert plan["particle_counts"] == {"source": 32, "training": 12, "heldout": 8, "remaining": 12}
    result = materialize_stratified_micrograph_subset(
        settings, str(root), "micrographs.star", **kwargs,
        expected_plan_sha256=plan["plan_sha256"], confirm=True,
    )
    all_rows = []
    partition_identities = []
    for name, path in result["particle_outputs"].items():
        subset = parse_star(root / path)
        assert subset["optics"] == source["optics"]
        assert list(subset["particles"]) == list(source["particles"])
        assert set(subset["particles"]["rlnMicrographName"]) <= set(result[f"{name}_micrographs"])
        all_rows.extend(zip(*subset["particles"].values(), strict=True))
        partition_identities.append(set(subset["particles"]["rlnImageName"]))
    assert not partition_identities[0] & partition_identities[1]
    assert not partition_identities[0] & partition_identities[2]
    assert not partition_identities[1] & partition_identities[2]
    assert sorted(all_rows) == sorted(zip(*source["particles"].values(), strict=True))
    assert (root / result["particle_source_snapshot"]).read_bytes() == before
    assert (root / "labels.star").read_bytes() == before


def test_changed_particle_label_source_blocks_stale_subset_plan(project):
    settings, root = project
    rows = write_subset_labels(root)
    kwargs = {**arguments(), "particles_star": "labels.star"}
    plan = plan_stratified_micrograph_subset(settings, str(root), "micrographs.star", **kwargs)
    # Even a valid invariant-metadata change must trigger a fresh plan hash.
    changed = list(rows[0])
    changed[7] = 2
    write_particles(root / "labels.star", [tuple(changed), *rows[1:]])
    with pytest.raises(ParticleAuditError, match="hash mismatch"):
        materialize_stratified_micrograph_subset(
            settings, str(root), "micrographs.star", **kwargs,
            expected_plan_sha256=plan["plan_sha256"], confirm=True,
        )
    assert not (root / "External").exists()


@pytest.mark.parametrize("change", ["unknown_micrograph", "duplicate_image", "invalid_halfset"])
def test_invalid_training_label_membership_or_identities_cannot_be_materialized(project, change):
    settings, root = project
    rows = write_subset_labels(root)
    changed = list(rows[0])
    if change == "unknown_micrograph":
        changed[3] = "Movies/unknown.mrc"
    elif change == "duplicate_image":
        changed[0] = rows[1][0]
    else:
        changed[7] = 3
    write_particles(root / "labels.star", [tuple(changed), *rows[1:]])
    with pytest.raises(ParticleAuditError):
        plan_stratified_micrograph_subset(
            settings, str(root), "micrographs.star", particles_star="labels.star", **arguments(),
        )


def test_particle_preview_counts_actual_identities_and_stratum_attrition(project):
    settings, root = project
    write_particles(root / "particles.star")
    report = audit_particle_selection(
        settings, str(root), "particles.star", selected_class_numbers=[2],
        numeric_bins={"rlnDefocusU": [15000]},
    )
    assert report["valid_for_comparison"]
    assert report["source"]["class_counts"] == {1: 3, 2: 3}
    assert report["selected"]["class_counts"] == {2: 3}
    assert report["retained_unique_particles"] == report["excluded_unique_particles"] == 3
    assert report["retained_fraction"] == 0.5
    assert "rlnMicrographName" in report["strata"]
    groups = {group["value"]: group for group in report["strata"]["rlnDefocusU"]["groups"]}
    assert groups["[-inf, 15000.0)"]["retained_fraction"] == 0
    assert groups["[15000.0, +inf)"]["retained_fraction"] == 1


def test_materialized_particle_subset_has_conserved_metadata(project):
    settings, root = project
    rows = write_particles(root / "particles.star")
    selected = [(*row, 33) for row in rows[3:]]  # alignment columns may be added
    write_particles(root / "selected.star", selected, extra_column=True)
    report = audit_particle_selection(
        settings, str(root), "particles.star", selected_particles_star="selected.star",
        strata_columns=["rlnOpticsGroupName"],
    )
    assert report["valid_for_comparison"]
    assert report["retained_unique_particles"] == 3
    assert report["selected_source_sha256"]
    assert report["strata"]["rlnOpticsGroupName"]["group_count"] == 2


@pytest.mark.parametrize("column,new_value", [(4, 99), (6, 50000), (7, 2)])
def test_invariant_coordinate_ctf_or_halfset_changes_invalidate_comparison(project, column, new_value):
    settings, root = project
    rows = write_particles(root / "particles.star")
    selected = list(rows[0])
    selected[column] = new_value
    write_particles(root / "selected.star", [tuple(selected)])
    report = audit_particle_selection(settings, str(root), "particles.star", selected_particles_star="selected.star")
    assert not report["valid_for_comparison"]
    assert report["retained_fraction"] is None
    assert report["excluded_unique_particles"] is None
    assert any(issue["code"] == "changed_or_missing_invariant_metadata" for issue in report["issues"])
    assert all(group["retained_fraction"] is None for group in report["strata"]["rlnOpticsGroup"]["groups"])


def test_changed_optics_sampling_invalidate_comparison(project):
    settings, root = project
    rows = write_particles(root / "particles.star")
    write_particles(root / "selected.star", rows[:2], optics=OPTICS.replace("1.5", "3.0"))
    report = audit_particle_selection(settings, str(root), "particles.star", selected_particles_star="selected.star")
    assert not report["valid_for_comparison"]
    assert any(issue["code"] == "changed_or_missing_optics_metadata" for issue in report["issues"])


def test_changed_phase_shift_is_a_ctf_invariant(project):
    settings, root = project
    rows = write_particles(root / "particles.star")
    write_particles(root / "particles.star", [(*row, 0) for row in rows], extra_column=True)
    write_particles(root / "selected.star", [(*rows[0], 45)], extra_column=True)
    for path in (root / "particles.star", root / "selected.star"):
        path.write_text(path.read_text().replace("rlnAngleRot", "rlnPhaseShift"))
    report = audit_particle_selection(settings, str(root), "particles.star", selected_particles_star="selected.star")
    assert not report["valid_for_comparison"]
    issue = next(issue for issue in report["issues"] if issue["code"] == "changed_or_missing_invariant_metadata")
    assert issue["columns"] == {"rlnPhaseShift": 1}


def test_reextracted_image_identity_is_not_guessed_from_coordinates(project):
    settings, root = project
    rows = write_particles(root / "particles.star")
    write_particles(root / "selected.star", [
        (row[0].replace("job001", "job002"), *row[1:]) for row in rows[:3]
    ])
    report = audit_particle_selection(settings, str(root), "particles.star", selected_particles_star="selected.star")
    assert not report["valid_for_comparison"]
    assert report["retained_unique_particles"] is None
    assert report["issues"][0]["code"] == "selected_image_ids_not_in_source"


def test_duplicate_ids_with_different_index_padding_are_identified(project):
    settings, root = project
    rows = write_particles(root / "particles.star")
    rows.append((rows[0][0].replace("000001@", "1@"), *rows[0][1:]))
    write_particles(root / "particles.star", rows)
    report = audit_particle_selection(settings, str(root), "particles.star")
    assert report["source"]["row_count"] == 7
    assert report["source"]["unique_particle_count"] == 6
    assert report["source"]["duplicate_image_name_count"] == 1
    assert report["selected"]["duplicate_image_name_count"] == 1
    assert not report["valid_for_comparison"]


def test_missing_selection_classes_and_missing_strata_are_actionable(project):
    settings, root = project
    write_particles(root / "particles.star")
    report = audit_particle_selection(
        settings, str(root), "particles.star", selected_class_numbers=[3],
        strata_columns=["rlnMissingTime"],
    )
    assert not report["valid_for_comparison"]
    assert report["missing_selected_classes"] == [3]
    assert report["missing_strata_columns"] == ["rlnMissingTime"]


def test_empty_class_preview_is_a_valid_zero_retention_case(project):
    settings, root = project
    write_particles(root / "particles.star")
    report = audit_particle_selection(settings, str(root), "particles.star", selected_class_numbers=[])
    assert report["valid_for_comparison"]
    assert report["retained_unique_particles"] == 0
    assert report["retained_fraction"] == 0


def test_reports_do_not_return_unbounded_per_particle_rows(project):
    settings, root = project
    rows = [
        (f"{index}@stack.mrcs", 1, 1, f"mic{index}.mrc", 5, 5, 10000, 1)
        for index in range(1, 401)
    ]
    write_particles(root / "particles.star", rows)
    report = audit_particle_selection(settings, str(root), "particles.star", selected_class_numbers=[1])
    assert report["source"]["unique_particle_count"] == 400
    group = report["strata"]["rlnMicrographName"]
    assert group["group_count"] == 400
    assert group["truncated"]
    assert len(group["groups"]) == 256
    assert "particle_rows" not in report


def test_late_depleted_stratum_is_prioritized_and_full_summary_is_not_truncated(project):
    settings, root = project
    rows = [
        (f"{index}@stack.mrcs", 2 if index == 400 else 1, 1,
         f"mic{index:04}.mrc", 5, 5, 10000, 1)
        for index in range(1, 401)
    ]
    write_particles(root / "particles.star", rows)
    report = audit_particle_selection(settings, str(root), "particles.star", selected_class_numbers=[1])
    group = report["strata"]["rlnMicrographName"]
    assert group["truncated"]
    assert group["groups"][0]["value"] == "mic0400.mrc"
    assert group["groups"][0]["retained_fraction"] == 0
    assert group["all_groups_summary"] == {
        "source_unique_particles": 400, "selected_unique_particles": 399,
        "lost_stratum_count": 1, "partially_retained_stratum_count": 0,
        "fully_retained_stratum_count": 399, "minimum_retained_fraction": 0,
        "maximum_retained_fraction": 1,
    }


def test_joint_optics_defocus_exposes_loss_hidden_by_marginal_counts(project):
    settings, root = project
    rows = []
    for optics in (1, 2):
        for defocus in (10000, 20000):
            for particle in range(2):
                rows.append((f"{len(rows) + 1}@stack.mrcs",
                             2 if optics == 1 and defocus == 10000 else 1,
                             optics, f"mic{optics}_{defocus}.mrc", 10 + particle, 20,
                             defocus, particle + 1))
    write_particles(root / "particles.star", rows)
    report = audit_particle_selection(
        settings, str(root), "particles.star", selected_class_numbers=[1],
        numeric_bins={"rlnDefocusU": [15000]},
    )
    # Neither individual acquisition axis appears completely lost.
    for column in ("rlnOpticsGroup", "rlnDefocusU"):
        assert report["strata"][column]["all_groups_summary"]["lost_stratum_count"] == 0
    joint = report["joint_strata"]
    assert joint["available"]
    assert joint["columns"] == ["rlnOpticsGroup", "rlnDefocusU"]
    assert joint["group_count"] == 4
    assert joint["all_groups_summary"]["lost_stratum_count"] == 1
    assert joint["all_groups_summary"]["source_unique_particles"] == 8
    assert joint["all_groups_summary"]["selected_unique_particles"] == 6
    assert joint["groups"][0] == {
        "stratum": {"rlnOpticsGroup": "1", "rlnDefocusU": "[-inf, 15000.0)"},
        "source_unique_particles": 2, "selected_unique_particles": 0,
        "excluded_unique_particles": 2, "retained_fraction": 0,
    }


def test_joint_strata_are_optional_bounded_and_never_report_partial_aggregates(project, monkeypatch):
    settings, root = project
    write_particles(root / "particles.star")
    ordinary = audit_particle_selection(settings, str(root), "particles.star")
    assert ordinary["joint_strata"] is None
    monkeypatch.setattr(particle_audit, "_MAX_JOINT_STRATA", 2)
    report = audit_particle_selection(
        settings, str(root), "particles.star", strata_columns=["rlnOpticsGroup"],
        numeric_bins={"rlnDefocusU": [15000]},
    )
    joint = report["joint_strata"]
    assert not joint["available"]
    assert joint["reason"] == "joint_stratum_limit_exceeded"
    assert joint["group_count_lower_bound"] == 3
    assert joint["groups"] == []
    assert "all_groups_summary" not in joint
    assert report["strata"]["rlnOpticsGroup"]["all_groups_summary"]["source_unique_particles"] == 6


def test_joint_strata_requires_every_requested_measured_axis(project):
    settings, root = project
    write_particles(root / "particles.star")
    report = audit_particle_selection(
        settings, str(root), "particles.star", strata_columns=["rlnOpticsGroup", "rlnMissingIce"],
    )
    assert not report["joint_strata"]["available"]
    assert report["joint_strata"]["reason"] == "missing_measured_strata"
    assert report["joint_strata"]["missing_columns"] == ["rlnMissingIce"]
