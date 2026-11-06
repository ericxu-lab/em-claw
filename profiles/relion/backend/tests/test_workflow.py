from dataclasses import replace

import pytest

from relion_mcp import server
from relion_mcp.catalog import Catalog, CatalogError
from relion_mcp.server import relion_render_workflow_step, relion_suggest_step_inputs
from relion_mcp.workflow import resolve_workflow_step_params, validate_workflow

REFERENCE_RESCALE_OVERRIDES = {
    "input_pixel_size_A": 3.54,
    "output_pixel_size_A": 1.244,
    "new_box_size_pix": 256,
    "output_suffix": "_box256",
}

PARTICLE_GEOMETRY_STEPS = {
    "manual_particle_picking": {"particle_diameter_A": 190},
    "log_autopicking": {"log_diameter_min_A": 140, "log_diameter_max_A": 190},
    "two_d_classification": {"mask_diameter_A": 220},
    "topaz_training": {"topaz_particle_diameter_A": 190},
    "topaz_picking": {"topaz_particle_diameter_A": 190},
    "topaz_two_d_classification": {"mask_diameter_A": 220},
    "de_novo_initial_model": {"mask_diameter_A": 220},
    "three_d_classification": {"mask_diameter_A": 220},
    "refine3d": {"mask_diameter_A": 220},
    "refine3d_after_ctf_refinement": {"mask_diameter_A": 220},
    "refined_after_polishing": {"mask_diameter_A": 220},
}


def test_particle_geometry_examples_do_not_make_steps_execution_ready():
    catalog = Catalog()
    plan = validate_workflow(
        catalog, "relion_5_spa_tutorial_v1",
        include_opt_in_steps=["manual_particle_picking"],
    )
    assert plan["valid_for_planning"] is True
    planned = {step["step_id"]: step for step in plan["plan"]}
    for step_id, required in PARTICLE_GEOMETRY_STEPS.items():
        step = planned[step_id]
        assert step["execution_ready"] is False
        assert set(step["missing_explicit_param_overrides"]) == set(required)
        assert set(required).isdisjoint(step["params"])


@pytest.mark.parametrize("step_id", PARTICLE_GEOMETRY_STEPS)
def test_missing_particle_geometry_blocks_render_and_registration(step_id, monkeypatch):
    def unexpected_registration(*args, **kwargs):
        pytest.fail("Missing dataset geometry reached job registration")

    monkeypatch.setattr(server.RUNNER, "run_job", unexpected_registration)
    monkeypatch.setattr(server.RUNNER, "add_job", unexpected_registration)
    with pytest.raises(CatalogError, match="missing explicit parameter overrides"):
        relion_render_workflow_step(step_id)
    for tool in (server.relion_run_workflow_step, server.relion_add_workflow_step):
        with pytest.raises(CatalogError, match="missing explicit parameter overrides"):
            tool("/approved/project", step_id, confirm=True)


@pytest.mark.parametrize("step_id,geometry", PARTICLE_GEOMETRY_STEPS.items())
def test_measured_particle_geometry_reaches_native_options(step_id, geometry):
    catalog = Catalog()
    step = next(step for step in catalog.workflow("relion_5_spa_tutorial_v1")["steps"]
                if step["id"] == step_id)
    inputs = {role: f"QA/{role}.star" for role in step["inputs"]}
    rendered = relion_render_workflow_step(step_id, input_paths=inputs, param_overrides=geometry)
    params = rendered["params"]
    native = catalog.render_job(step["operation"], inputs, params).options_by_key
    for name, value in geometry.items():
        assert params[name] == value
        key = catalog.mapping(step["operation"])["params"][name]["manifest_option_key"]
        assert native[key] == str(float(value))
    if "symmetry" in params:
        assert params["symmetry"] == "C1"
        assert native["sym_name"] == "C1"


def test_non_c1_symmetry_remains_an_explicit_workflow_override():
    result = relion_render_workflow_step(
        "de_novo_initial_model",
        input_paths={"particles": "Select/job014/particles.star"},
        param_overrides={"mask_diameter_A": 220, "symmetry": "D2"},
    )
    assert result["params"]["symmetry"] == "D2"
    assert "Symmetry: == D2" in result["option_assignments"]


def test_native_micrograph_subset_size_is_dataset_bound():
    step_id = "select_micrograph_subset_for_autopicking"
    plan = validate_workflow(Catalog(), "relion_5_spa_tutorial_v1")
    step = next(step for step in plan["plan"] if step["step_id"] == step_id)
    assert step["execution_ready"] is False
    assert step["missing_explicit_param_overrides"] == ["subset_size"]
    assert "subset_size" not in step["params"]
    assert step["params"]["randomize_order"] is True
    inputs = {"micrographs": "CtfFind/job003/micrographs.star"}
    with pytest.raises(CatalogError, match="missing explicit parameter overrides.*subset_size"):
        relion_render_workflow_step(step_id, input_paths=inputs)
    rendered = relion_render_workflow_step(
        step_id, input_paths=inputs, param_overrides={"subset_size": 48},
    )
    assert rendered["params"]["subset_size"] == 48


def test_render_job_surfaces_symmetry_preflight(monkeypatch):
    observed = []

    def fake_preflight(rendered):
        observed.append(rendered.options_by_key["sym_name"])
        return {"symmetry": "C100", "status": "compatible"}

    monkeypatch.setattr(server.RUNNER, "preflight_symmetry", fake_preflight)

    result = server.relion_render_job(
        "initial_model",
        inputs={"particles": "Select/job008/particles.star"},
        params={"num_classes": 1, "symmetry": "C100", "mask_diameter_A": 200},
    )

    assert observed == ["C100"]
    assert result["symmetry_preflight"] == {
        "symmetry": "C100",
        "status": "compatible",
    }


def test_tutorial_is_plannable_but_not_fully_executable():
    result = validate_workflow(Catalog(), "relion_5_spa_tutorial_v1")
    assert result["declared_step_count"] == 35
    assert result["step_count"] == 33
    assert result["available_opt_in_steps"] == [
        "manual_particle_picking",
        "local_resolution",
    ]
    assert result["included_opt_in_steps"] == []
    assert result["skipped_steps"] == [
        {
            "step_id": "manual_particle_picking",
            "operation": "manual_picking",
            "reason": "disabled_by_default",
            "activation_policy": "explicit_user_request",
        },
        {
            "step_id": "local_resolution",
            "operation": "local_resolution",
            "reason": "disabled_by_default",
            "activation_policy": "final_best_map_selected_or_explicit_user_request",
        },
    ]
    assert result["valid_for_planning"] is True
    assert result["valid_for_full_execution"] is False
    supports = {step["step_id"]: step["support_level"] for step in result["plan"]}
    assert "manual_particle_picking" not in supports
    assert "local_resolution" not in supports
    assert supports["rescale_selected_3d_reference_map"] == "direct_command"
    assert supports["modelangelo_atomic_modelling"] == "dry_run_mappable"
    assert supports["dynamight_motion_exploration"] == "manifest_only"


def test_2d_class_ranker_workflow_starts_at_point_zero_five():
    workflow = Catalog().workflow("relion_5_spa_tutorial_v1")
    steps = {step["id"]: step for step in workflow["steps"]}

    for step_id in (
        "particle_selection_from_2d",
        "topaz_particle_selection_from_2d",
    ):
        assert steps[step_id]["params"]["auto_select_2d_classes"] is True
        assert steps[step_id]["params"]["minimum_selection_score"] == 0.05


def test_tutorial_uses_task_authorization_without_a_separate_approval_gate():
    catalog = Catalog()
    result = validate_workflow(catalog, "relion_5_spa_tutorial_v1")

    assert result["authorization_required"] is True
    assert result["authorization_source"] == "user_task_request"
    assert result["requires_human_approval"] is False
    assert "missing_human_approval" not in catalog.workflow(
        "relion_5_spa_tutorial_v1"
    )["avoid_when"]
    # Task authorization does not supply missing acquisition parameters or
    # make unsupported stages executable.
    steps = {step["step_id"]: step for step in result["plan"]}
    assert steps["import_movies"]["missing_explicit_param_overrides"]
    assert steps["import_movies"]["execution_ready"] is False
    assert steps["modelangelo_atomic_modelling"]["executable"] is False
    assert steps["dynamight_motion_exploration"]["executable"] is False


def test_automatic_particle_selection_keeps_checkpoints_without_interactive_pauses():
    result = validate_workflow(Catalog(), "relion_5_spa_tutorial_v1")
    selections = [
        step for step in result["plan"] if step["operation"] == "particle_selection"
    ]

    assert selections
    for step in selections:
        assert step["interactive_or_selection_pause"] is False
        assert step["quality_checkpoint"]
        assert step["dependencies"]
    assert not any(step["interactive_or_selection_pause"] for step in result["plan"])


def test_manual_picking_can_be_explicitly_included():
    result = validate_workflow(
        Catalog(),
        "relion_5_spa_tutorial_v1",
        include_opt_in_steps=["manual_particle_picking"],
    )

    assert result["declared_step_count"] == 35
    assert result["step_count"] == 34
    assert result["included_opt_in_steps"] == ["manual_particle_picking"]
    assert result["skipped_steps"] == [
        {
            "step_id": "local_resolution",
            "operation": "local_resolution",
            "reason": "disabled_by_default",
            "activation_policy": "final_best_map_selected_or_explicit_user_request",
        }
    ]
    manual_step = next(item for item in result["plan"] if item["step_id"] == "manual_particle_picking")
    assert manual_step["enabled_by_default"] is False
    assert manual_step["activation_policy"] == "explicit_user_request"
    assert manual_step["interactive_or_selection_pause"] is True
    extraction = next(item for item in result["plan"] if item["step_id"] == "particle_extraction")
    assert extraction["dependencies"]["coordinates"]["source"] == "log_autopicking"

    rendered = relion_render_workflow_step(
        step_id="manual_particle_picking",
        param_overrides={"particle_diameter_A": 200},
        input_paths={"micrographs": "CtfFind/job003/micrographs_ctf.star"},
    )
    assert rendered["operation"] == "manual_picking"
    assert rendered["job_type"] == "ManualPick"


def test_local_resolution_is_deferred_and_can_be_explicitly_included():
    default = validate_workflow(Catalog(), "relion_5_spa_tutorial_v1")
    assert all(step["step_id"] != "local_resolution" for step in default["plan"])

    result = validate_workflow(
        Catalog(),
        "relion_5_spa_tutorial_v1",
        include_opt_in_steps=["local_resolution"],
    )

    assert result["declared_step_count"] == 35
    assert result["step_count"] == 34
    assert result["included_opt_in_steps"] == ["local_resolution"]
    assert result["skipped_steps"] == [
        {
            "step_id": "manual_particle_picking",
            "operation": "manual_picking",
            "reason": "disabled_by_default",
            "activation_policy": "explicit_user_request",
        }
    ]
    step = next(item for item in result["plan"] if item["step_id"] == "local_resolution")
    assert step["optional"] is True
    assert step["enabled_by_default"] is False
    assert (
        step["activation_policy"]
        == "final_best_map_selected_or_explicit_user_request"
    )
    assert step["dependencies"]["half_map"]["source"] == "refined_after_polishing"
    assert step["dependencies"]["mask"]["source"] == "mask_creation"


def test_workflow_step_alias_is_used_by_render_run_and_add(monkeypatch):
    inputs = {"movies": "Import/job001/movies.star"}
    rendered = relion_render_workflow_step(
        step_id="motion_correction",
        input_paths=inputs,
        alias="motion_r02",
    )
    assert rendered["command"][-2:] == ["--setJobAlias", "motion_r02"]

    observed: list[tuple[str, str | None]] = []

    def fake_run_job(project_dir, rendered_job, **kwargs):
        observed.append(("run", kwargs["alias"]))
        return {"project_dir": project_dir, "operation": rendered_job.operation}

    def fake_add_job(project_dir, rendered_job, **kwargs):
        observed.append(("add", kwargs["alias"]))
        return {"project_dir": project_dir, "operation": rendered_job.operation}

    monkeypatch.setattr(server.RUNNER, "run_job", fake_run_job)
    monkeypatch.setattr(server.RUNNER, "add_job", fake_add_job)
    server.relion_run_workflow_step(
        "/approved/project",
        "motion_correction",
        input_paths=inputs,
        alias="motion_r03",
    )
    server.relion_add_workflow_step(
        "/approved/project",
        "motion_correction",
        input_paths=inputs,
        alias="motion_r04",
    )

    assert observed == [("run", "motion_r03"), ("add", "motion_r04")]


def test_initialize_project_uses_configured_pipeline_default(monkeypatch):
    observed: list[str] = []

    def fake_initialize(project_dir, pipeline_name, confirm):
        observed.append(pipeline_name)
        return {"project_dir": project_dir, "pipeline_name": pipeline_name}

    monkeypatch.setattr(
        server,
        "SETTINGS",
        replace(server.SETTINGS, pipeline_name="site_pipeline"),
    )
    monkeypatch.setattr(server.RUNNER, "initialize_project", fake_initialize)

    defaulted = server.relion_initialize_project("/approved/project")
    explicit = server.relion_initialize_project(
        "/approved/project",
        pipeline_name="explicit_pipeline",
    )

    assert observed == ["site_pipeline", "explicit_pipeline"]
    assert defaulted["pipeline_name"] == "site_pipeline"
    assert explicit["pipeline_name"] == "explicit_pipeline"


def test_unknown_workflow_opt_in_step_is_rejected():
    with pytest.raises(CatalogError, match="Unknown workflow opt-in steps"):
        validate_workflow(
            Catalog(),
            "relion_5_spa_tutorial_v1",
            include_opt_in_steps=["not_a_workflow_step"],
        )


def test_unknown_workflow_override_step_is_rejected():
    with pytest.raises(CatalogError, match="Unknown workflow override steps"):
        validate_workflow(
            Catalog(),
            "relion_5_spa_tutorial_v1",
            param_overrides={"not_a_workflow_step": {"symmetry": "C1"}},
        )


def test_reextract_geometry_is_not_execution_ready_without_explicit_dataset_overrides():
    result = validate_workflow(Catalog(), "relion_5_spa_tutorial_v1")
    step = next(
        item
        for item in result["plan"]
        if item["step_id"] == "reextract_refined_particles"
    )

    assert result["valid_for_planning"] is True
    assert step["execution_ready"] is False
    assert {"box_size_pix", "rescale_particles"}.issubset(
        step["missing_explicit_param_overrides"]
    )


def test_render_reextract_requires_explicit_geometry_overrides():
    with pytest.raises(CatalogError, match="explicit"):
        relion_render_workflow_step(
            "reextract_refined_particles",
            input_paths={
                "micrographs": "CtfFind/job003/micrographs_ctf.star",
                "refined_particles": "Select/job019/particles.star",
            },
        )


def test_explicit_native_reextract_renders_without_rescaling():
    overrides = {"box_size_pix": 288, "rescale_particles": False}
    planned = validate_workflow(
        Catalog(),
        "relion_5_spa_tutorial_v1",
        param_overrides={"reextract_refined_particles": overrides},
    )
    step = next(
        item
        for item in planned["plan"]
        if item["step_id"] == "reextract_refined_particles"
    )

    assert step["execution_ready"] is True
    assert step["missing_explicit_param_overrides"] == []

    rendered = relion_render_workflow_step(
        "reextract_refined_particles",
        input_paths={
            "micrographs": "CtfFind/job003/micrographs_ctf.star",
            "refined_particles": "Select/job019/particles.star",
        },
        param_overrides=overrides,
    )

    assert rendered["params"]["box_size_pix"] == 288
    assert rendered["params"]["rescale_particles"] is False
    assert "rescaled_size_pix" not in rendered["params"]
    assert "Rescale particles? == No" in rendered["option_assignments"]
    assert not any(
        assignment.startswith("Re-scaled size")
        for assignment in rendered["option_assignments"]
    )


def test_final_reextract_rejects_explicit_particle_rescaling():
    overrides = {
        "box_size_pix": 288,
        "rescale_particles": True,
        "rescaled_size_pix": 192,
    }
    planned = validate_workflow(
        Catalog(),
        "relion_5_spa_tutorial_v1",
        param_overrides={"reextract_refined_particles": overrides},
    )
    step = next(
        item
        for item in planned["plan"]
        if item["step_id"] == "reextract_refined_particles"
    )

    assert step["execution_ready"] is False
    assert step["missing_explicit_param_overrides"] == []
    assert any(
        issue["step_id"] == "reextract_refined_particles"
        and "native sampling" in issue["message"]
        for issue in planned["issues"]
    )
    with pytest.raises(CatalogError, match="native sampling"):
        relion_render_workflow_step(
            "reextract_refined_particles",
            input_paths={
                "micrographs": "CtfFind/job003/micrographs_ctf.star",
                "refined_particles": "Select/job019/particles.star",
            },
            param_overrides=overrides,
        )


def test_explicit_rescaled_initial_extraction_requires_rescaled_size():
    incomplete = {"box_size_pix": 320, "rescale_particles": True}
    planned = validate_workflow(
        Catalog(),
        "relion_5_spa_tutorial_v1",
        param_overrides={"particle_extraction": incomplete},
    )
    step = next(
        item
        for item in planned["plan"]
        if item["step_id"] == "particle_extraction"
    )

    assert step["execution_ready"] is False
    assert step["missing_explicit_param_overrides"] == ["rescaled_size_pix"]
    with pytest.raises(CatalogError, match="rescaled_size_pix"):
        relion_render_workflow_step(
            "particle_extraction",
            input_paths={
                "micrographs": "CtfFind/job003/micrographs_ctf.star",
                "coordinates": "AutoPick/job006/coords_suffix_autopick.star",
            },
            param_overrides=incomplete,
        )

    rendered = relion_render_workflow_step(
        "particle_extraction",
        input_paths={
            "micrographs": "CtfFind/job003/micrographs_ctf.star",
            "coordinates": "AutoPick/job006/coords_suffix_autopick.star",
        },
        param_overrides={**incomplete, "rescaled_size_pix": 160},
    )
    assert rendered["params"]["rescaled_size_pix"] == 160
    assert "Rescale particles? == Yes" in rendered["option_assignments"]
    assert any(
        assignment.startswith("Re-scaled size") and assignment.endswith("== 160")
        for assignment in rendered["option_assignments"]
    )


def test_conditional_explicit_requirements_do_not_mutate_workflow_policy():
    step = {
        "id": "extraction",
        "params": {},
        "explicit_param_overrides": {
            "required": ["box_size_pix", "rescale_particles"],
            "conditional": [
                {
                    "when": {"param": "rescale_particles", "equals": True},
                    "required": ["rescaled_size_pix"],
                }
            ],
        },
    }

    _, rescaled_missing, _ = resolve_workflow_step_params(
        step,
        {"box_size_pix": 320, "rescale_particles": True},
    )
    _, native_missing, _ = resolve_workflow_step_params(
        step,
        {"box_size_pix": 288, "rescale_particles": False},
    )

    assert rescaled_missing == ["rescaled_size_pix"]
    assert native_missing == []
    assert step["explicit_param_overrides"]["required"] == [
        "box_size_pix",
        "rescale_particles",
    ]


def test_invalid_workflow_override_marks_plan_invalid():
    result = validate_workflow(
        Catalog(),
        "relion_5_spa_tutorial_v1",
        param_overrides={"de_novo_initial_model": {"num_classes": "many"}},
    )

    matching = [
        issue
        for issue in result["issues"]
        if issue["step_id"] == "de_novo_initial_model"
        and "Step cannot be rendered" in issue["message"]
    ]
    assert result["valid_for_planning"] is False
    assert len(matching) == 1
    assert "parameter num_classes must be an integer" in matching[0]["message"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"input_pixel_size_A": "unknown"}, "must be numeric"),
        ({"input_pixel_size_A": float("nan")}, "must be finite"),
        ({"input_pixel_size_A": float("inf")}, "must be finite"),
        ({"input_pixel_size_A": 10**400}, "must be finite"),
        ({"output_pixel_size_A": 0}, "must be positive"),
        ({"new_box_size_pix": 0}, "must be at least 1"),
        ({"output_map": "../outside.mrc"}, "must be project-relative"),
        ({"output_suffix": "unsafe suffix"}, "may contain only"),
    ],
)
def test_invalid_direct_workflow_overrides_mark_plan_invalid(overrides, message):
    result = validate_workflow(
        Catalog(),
        "relion_5_spa_tutorial_v1",
        param_overrides={
            "rescale_selected_3d_reference_map": {
                **REFERENCE_RESCALE_OVERRIDES,
                **overrides,
            }
        },
    )

    matching = [
        issue
        for issue in result["issues"]
        if issue["step_id"] == "rescale_selected_3d_reference_map"
        and "Step cannot be rendered" in issue["message"]
    ]
    assert result["valid_for_planning"] is False
    assert len(matching) == 1
    assert message in matching[0]["message"]


def test_render_direct_workflow_step_validates_overrides():
    with pytest.raises(CatalogError, match="must be positive"):
        relion_render_workflow_step(
            "rescale_selected_3d_reference_map",
            param_overrides={
                **REFERENCE_RESCALE_OVERRIDES,
                "output_pixel_size_A": 0,
            },
        )


def test_classification_e_step_resolution_overrides_are_plannable():
    result = validate_workflow(
        Catalog(),
        "relion_5_spa_tutorial_v1",
        param_overrides={
            "two_d_classification": {"e_step_resolution_limit_A": 12},
            "topaz_two_d_classification": {"e_step_resolution_limit_A": 12},
            "three_d_classification": {"e_step_resolution_limit_A": 7},
        },
    )

    effective = {step["step_id"]: step["params"] for step in result["plan"]}
    assert result["valid_for_planning"] is True
    assert effective["two_d_classification"]["e_step_resolution_limit_A"] == 12
    assert effective["topaz_two_d_classification"]["e_step_resolution_limit_A"] == 12
    assert effective["three_d_classification"]["e_step_resolution_limit_A"] == 7


def test_three_d_no_alignment_override_and_reference_mask_render():
    planned = validate_workflow(
        Catalog(),
        "relion_5_spa_tutorial_v1",
        param_overrides={"three_d_classification": {"alignment": False}},
    )
    effective = {step["step_id"]: step["params"] for step in planned["plan"]}

    assert planned["valid_for_planning"] is True
    assert effective["three_d_classification"]["alignment"] is False

    rendered = relion_render_workflow_step(
        "three_d_classification",
        input_paths={
            "particles": "Refine3D/job020/run_data.star",
            "reference_volume": "Refine3D/job020/run_class001.mrc",
            "reference_mask": "MaskCreate/job021/mask.mrc",
        },
        param_overrides={"alignment": False, "mask_diameter_A": 200},
    )

    assert rendered["params"]["alignment"] is False
    assert rendered["input_paths"]["reference_mask"] == "MaskCreate/job021/mask.mrc"
    assert "Reference mask (optional): == MaskCreate/job021/mask.mrc" in rendered[
        "option_assignments"
    ]
    assert "Perform image alignment? == No" in rendered["option_assignments"]


def test_solvent_corrected_fsc_activates_only_for_masked_polished_refinement():
    planned = validate_workflow(Catalog(), "relion_5_spa_tutorial_v1")
    steps = {step["step_id"]: step for step in planned["plan"]}

    assert "solvent_correct_fsc" not in steps["refine3d"]["params"]
    assert "solvent_correct_fsc" not in steps["refine3d_after_ctf_refinement"]["params"]
    polished = steps["refined_after_polishing"]
    assert polished["params"]["solvent_correct_fsc"] is True
    assert polished["dependencies"]["mask"]["source"] == "mask_creation"

    rendered = relion_render_workflow_step(
        "refined_after_polishing",
        param_overrides={"mask_diameter_A": 200},
        input_paths={
            "particles": "Polish/job030/shiny.star",
            "reference_volume": "Refine3D/job029/run_class001.mrc",
            "mask": "MaskCreate/job021/mask.mrc",
        },
    )
    assert "Reference mask (optional): == MaskCreate/job021/mask.mrc" in rendered[
        "option_assignments"
    ]
    assert "Use solvent-flattened FSCs? == Yes" in rendered["option_assignments"]


def test_workflow_cannot_activate_solvent_corrected_fsc_without_mask():
    planned = validate_workflow(
        Catalog(),
        "relion_5_spa_tutorial_v1",
        param_overrides={"refine3d": {"solvent_correct_fsc": True}},
    )

    matching = [
        issue
        for issue in planned["issues"]
        if issue["step_id"] == "refine3d"
        and "requires a reference mask" in issue["message"]
    ]
    assert planned["valid_for_planning"] is False
    assert len(matching) == 1


def test_topaz_branch_dependencies_choose_latest_producer():
    result = validate_workflow(Catalog(), "relion_5_spa_tutorial_v1")
    initial_extraction = next(item for item in result["plan"] if item["step_id"] == "particle_extraction")
    assert initial_extraction["dependencies"]["coordinates"]["source"] == "log_autopicking"
    step = next(item for item in result["plan"] if item["step_id"] == "topaz_particle_extraction")
    assert step["dependencies"]["coordinates"]["source"] == "topaz_picking"


def test_explicit_manual_step_input_suggestion_is_available_without_running_a_job():
    result = relion_suggest_step_inputs(
        project_dir="/unused",
        step_id="manual_particle_picking",
        prior_jobs={},
    )

    assert result["step_id"] == "manual_particle_picking"
    assert result["suggestions"]["micrographs"]["source_step"] == "ctf_estimation"
    assert result["suggestions"]["micrographs"]["candidate"] is None


@pytest.mark.parametrize(
    ("include_opt_in_steps", "expected_rendered"),
    [
        (None, 30),
        (["manual_particle_picking"], 31),
        (["local_resolution"], 31),
        (["manual_particle_picking", "local_resolution"], 32),
    ],
)
def test_every_active_schedule_mappable_tutorial_step_renders(include_opt_in_steps, expected_rendered):
    catalog = Catalog()
    workflow = catalog.workflow("relion_5_spa_tutorial_v1")
    steps = {step["id"]: step for step in workflow["steps"]}
    explicit_overrides = {
        step["id"]: dict(step.get("tutorial_example_params") or {})
        for step in workflow["steps"]
        if step.get("explicit_param_overrides")
    }
    native_reextract = explicit_overrides["reextract_refined_particles"]
    native_reextract["rescale_particles"] = False
    native_reextract.pop("rescaled_size_pix", None)
    plan = validate_workflow(
        catalog,
        "relion_5_spa_tutorial_v1",
        param_overrides=explicit_overrides,
        include_opt_in_steps=include_opt_in_steps,
    )["plan"]
    rendered = 0
    for state in plan:
        if state["support_level"] != "schedule_mappable" or not state["execution_ready"]:
            continue
        step = steps[state["step_id"]]
        operation = catalog.resolve_operation(step["operation"])
        mapping = catalog.mapping(operation)
        input_paths = {}
        for role in step.get("inputs") or {}:
            spec = mapping["inputs"][role]
            native = catalog.manifest_options(state["job_type"])[spec["manifest_option_key"]]
            pattern = str(native.get("file_pattern") or "").lower()
            extension = ".mrc" if "mrc" in pattern or role in {"map", "volume", "mask", "reference_map"} else ".star"
            input_paths[role] = f"QA/{step['id']}/{role}{extension}"
        catalog.render_job(operation, input_paths, state["params"])
        rendered += 1
    assert rendered == expected_rendered
