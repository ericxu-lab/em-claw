import pytest

from relion_mcp.catalog import Catalog, CatalogError

SCRATCHABLE_JOB_CASES = [
    pytest.param(
        "two_d_classification",
        {"particles": "Extract/job007/particles.star"},
        {"num_classes": 50, "mask_diameter_A": 200},
        id="two-d-classification",
    ),
    pytest.param(
        "initial_model",
        {"particles": "Select/job008/particles.star"},
        {"num_classes": 1, "mask_diameter_A": 200, "symmetry": "C1"},
        id="initial-model",
    ),
    pytest.param(
        "three_d_classification",
        {
            "particles": "Select/job014/particles.star",
            "reference_volume": "InitialModel/job015/initial_model.mrc",
        },
        {"num_classes": 4, "mask_diameter_A": 200, "symmetry": "C1"},
        id="three-d-classification",
    ),
    pytest.param(
        "refine3d",
        {
            "particles": "Select/job014/particles.star",
            "reference_volume": "InitialModel/job015/initial_model.mrc",
        },
        {"mask_diameter_A": 200, "symmetry": "C1"},
        id="refine3d",
    ),
]

GEOMETRY_JOB_CASES = [
    ("manual_picking", {"micrographs": "CtfFind/job003/micrographs.star"},
     {"particle_diameter_A": 185}, "particle_diameter_A", "diameter"),
    ("log_autopicking", {"micrographs": "CtfFind/job003/micrographs.star"},
     {"log_diameter_min_A": 140, "log_diameter_max_A": 185},
     "log_diameter_min_A", "log_diam_min"),
    ("log_autopicking", {"micrographs": "CtfFind/job003/micrographs.star"},
     {"log_diameter_min_A": 140, "log_diameter_max_A": 185},
     "log_diameter_max_A", "log_diam_max"),
    ("topaz_training", {"micrographs": "Select/job004/micrographs.star",
                        "particles": "Select/job008/particles.star"},
     {"topaz_particle_diameter_A": 185}, "topaz_particle_diameter_A", "topaz_particle_diameter"),
    ("topaz_picking", {"micrographs": "CtfFind/job003/micrographs.star",
                       "model": "AutoPick/job009/model.sav"},
     {"topaz_particle_diameter_A": 185}, "topaz_particle_diameter_A", "topaz_particle_diameter"),
    *[(case.values[0], case.values[1], case.values[2], "mask_diameter_A", "particle_diameter")
      for case in SCRATCHABLE_JOB_CASES],
]


@pytest.fixture(scope="module")
def geometry_catalog():
    return Catalog()


@pytest.mark.parametrize("operation,inputs,params,parameter,native_key", GEOMETRY_JOB_CASES)
def test_particle_geometry_is_required_and_has_no_adapter_default(
    operation, inputs, params, parameter, native_key, geometry_catalog,
):
    catalog = geometry_catalog
    missing_geometry = {name: value for name, value in params.items() if name != parameter}
    for include_defaults in (False, True):
        with pytest.raises(CatalogError, match=f"Missing required values.*{parameter}"):
            catalog.render_job(operation, inputs, missing_geometry, include_defaults=include_defaults)
    rendered = catalog.render_job(operation, inputs, params)
    assert rendered.options_by_key[native_key] == str(float(params[parameter]))
    for definition in (catalog.mapping(operation)["params"][parameter],
                       catalog.neutral[operation]["params"][parameter]):
        assert definition["required"] is True
        assert "default" not in definition
    assert "derived_geometry" in catalog.neutral[operation]["params"][parameter]["allowed_sources"]


@pytest.mark.parametrize("operation,inputs,params,parameter,native_key", GEOMETRY_JOB_CASES)
@pytest.mark.parametrize("invalid", [0, -1, float("nan"), float("inf"), True])
def test_particle_geometry_rejects_nonpositive_or_nonfinite_values(
    operation, inputs, params, parameter, native_key, invalid, geometry_catalog,
):
    with pytest.raises(CatalogError, match=parameter):
        geometry_catalog.render_job(operation, inputs, {**params, parameter: invalid})


def test_log_particle_diameter_range_cannot_be_reversed(geometry_catalog):
    with pytest.raises(CatalogError, match="log_diameter_min_A may not exceed"):
        geometry_catalog.render_job(
            "log_autopicking",
            {"micrographs": "Select/job004/micrographs.star"},
            {"log_diameter_min_A": 185, "log_diameter_max_A": 140},
        )


def test_native_micrograph_split_requires_sample_size_and_randomizes_by_default(geometry_catalog):
    inputs = {"micrographs": "CtfFind/job003/micrographs.star"}
    with pytest.raises(CatalogError, match="Missing required values.*subset_size"):
        geometry_catalog.render_job("micrograph_subset_selection", inputs)
    with pytest.raises(CatalogError, match="subset_size must be at least 1"):
        geometry_catalog.render_job("micrograph_subset_selection", inputs, {"subset_size": 0})
    rendered = geometry_catalog.render_job(
        "micrograph_subset_selection", inputs, {"subset_size": 48},
    )
    assert rendered.options_by_key["split_size"] == "48"
    assert rendered.options_by_key["do_random"] == "Yes"


def test_catalog_manifest_references_are_valid():
    catalog = Catalog()
    assert catalog.validate_catalog() == []


@pytest.mark.parametrize(
    "operation",
    [
        "import_movies",
        "import_particles",
        "particle_selection",
        "particle_extraction",
        "extract_particles",
        "postprocessing",
        "postprocess",
        "bayesian_polishing",
        "polish",
        "remove_duplicate_particles",
        "rescale_map",
        "align_symmetry_reference",
    ],
)
def test_routine_operation_descriptions_use_task_authorization(operation):
    described = Catalog().describe_operation(operation)

    assert described["authorization_required"] is True
    assert described["authorization_source"] == "user_task_request"
    assert described["requires_human_approval"] is False
    if "definition" in described:
        assert described["definition"]["requires_human_approval"] is False


def test_reduced_approval_metadata_preserves_protected_operations():
    catalog = Catalog()
    for operation in ("manual_picking", "modelangelo", "dynamight"):
        described = catalog.describe_operation(operation)
        assert described["authorization_required"] is True
        assert described["requires_human_approval"] is True

    rescale = catalog.describe_operation("rescale_map")
    assert rescale["human_approval_conditions"] == ["overwrite_existing_output"]
    assert rescale["definition"]["human_approval_conditions"] == ["overwrite_existing_output"]
    assert catalog.describe_operation("modelangelo")["executable"] is False
    assert catalog.describe_operation("dynamight")["executable"] is False
    for operation in ("manual_picking", "align_symmetry_reference"):
        note = catalog.describe_operation(operation)["authorization_note"]
        assert "without another prompt" in note


def test_operation_authorization_defaults_follow_declared_effects():
    catalog = Catalog()
    catalog.neutral["import_particles"] = {
        "modifies_backend_project": False,
        "creates_files": False,
    }

    described = catalog.describe_operation("import_particles")

    assert described["authorization_required"] is False
    assert described["authorization_source"] is None
    assert described["requires_human_approval"] is False


def test_tutorial_aliases_render_native_labels():
    catalog = Catalog()
    rendered = catalog.render_job(
        "particle_extraction",
        inputs={"micrographs": "CtfFind/job003/micrographs_ctf.star", "coordinates": "AutoPick/job006/coords_suffix_autopick.star"},
        params={"box_size_pix": 256},
    )
    assert rendered.operation == "extract_particles"
    assert rendered.relion_job_type == "Extract"
    assert rendered.options_by_key["star_mics"] == "CtfFind/job003/micrographs_ctf.star"
    assert rendered.options_by_key["extract_size"] == "256"
    assert any("Particle box size" in item for item in rendered.option_assignments)


@pytest.mark.parametrize(
    ("params", "message"),
    [
        (
            {"box_size_pix": 255, "rescale_particles": False},
            "box_size_pix must be even",
        ),
        (
            {"box_size_pix": 256, "rescale_particles": True},
            "requires rescaled_size_pix",
        ),
        (
            {
                "box_size_pix": 256,
                "rescale_particles": False,
                "rescaled_size_pix": 128,
            },
            "must be omitted",
        ),
        (
            {
                "box_size_pix": 256,
                "rescale_particles": True,
                "rescaled_size_pix": 127,
            },
            "rescaled_size_pix must be even",
        ),
        (
            {
                "box_size_pix": 256,
                "rescale_particles": True,
                "rescaled_size_pix": 258,
            },
            "may not exceed box_size_pix",
        ),
        (
            {
                "box_size_pix": 256,
                "rescale_particles": False,
                "background_diameter_pix": 256,
            },
            "background_diameter_pix must be smaller",
        ),
    ],
)
def test_particle_extraction_geometry_combinations_are_guarded(params, message):
    with pytest.raises(CatalogError, match=message):
        Catalog().render_job(
            "particle_extraction",
            inputs={"micrographs": "CtfFind/job003/micrographs_ctf.star"},
            params=params,
        )


def test_particle_extraction_native_geometry_omits_rescale_assignment():
    rendered = Catalog().render_job(
        "particle_extraction",
        inputs={"micrographs": "CtfFind/job003/micrographs_ctf.star"},
        params={
            "box_size_pix": 256,
            "background_diameter_pix": 200,
            "rescale_particles": False,
        },
    )

    assert rendered.options_by_key["do_rescale"] == "No"
    assert "rescale" not in rendered.options_by_key


def test_particle_extraction_geometry_fields_allow_derived_authority():
    definitions = Catalog().neutral["particle_extraction"]["params"]

    for name in ("box_size_pix", "rescaled_size_pix", "background_diameter_pix"):
        assert "derived" in definitions[name]["allowed_sources"]
        assert definitions[name]["authority_order"][0] == "derived"


def test_manual_picking_operation_remains_explicitly_renderable():
    rendered = Catalog().render_job(
        "manual_picking",
        inputs={"micrographs": "CtfFind/job003/micrographs_ctf.star"},
        params={"particle_diameter_A": 200},
    )

    assert rendered.operation == "manual_picking"
    assert rendered.relion_job_type == "ManualPick"
    assert rendered.options_by_key["fn_in"] == "CtfFind/job003/micrographs_ctf.star"
    assert rendered.options_by_key["diameter"] == "200.0"


@pytest.mark.parametrize(
    ("operation", "inputs", "params"),
    [
        (
            "two_d_classification",
            {"particles": "Extract/job007/particles.star"},
            {"num_classes": 50, "mask_diameter_A": 200, "e_step_resolution_limit_A": 12},
        ),
        (
            "three_d_classification",
            {
                "particles": "Select/job014/particles.star",
                "reference_volume": "InitialModel/job015/initial_model.mrc",
            },
            {
                "num_classes": 4, "mask_diameter_A": 200,
                "symmetry": "C1",
                "e_step_resolution_limit_A": 12,
            },
        ),
    ],
)
def test_classification_e_step_resolution_limit_is_discoverable_and_renderable(
    operation,
    inputs,
    params,
):
    catalog = Catalog()

    rendered = catalog.render_job(operation, inputs=inputs, params=params)
    described = catalog.describe_operation(operation)["params"]["e_step_resolution_limit_A"]

    assert rendered.options_by_key["highres_limit"] == "12.0"
    assert described["manifest_option_key"] == "highres_limit"
    assert described["manifest_default"] == -1
    assert described["gui_label"].strip() == "Limit resolution E-step to (A):"
    assert "e_step_resolution_limit_A" in catalog.neutral[operation]["params"]


@pytest.mark.parametrize(("operation", "inputs", "required_params"), SCRATCHABLE_JOB_CASES)
def test_scratch_directory_is_discoverable_and_renderable(
    operation,
    inputs,
    required_params,
):
    catalog = Catalog()
    scratch_directory = "/local_nvme/relion-scratch/user_01"

    described = catalog.describe_operation(operation)["params"]["scratch_directory"]
    rendered = catalog.render_job(
        operation,
        inputs=inputs,
        params={**required_params, "scratch_directory": scratch_directory},
    )

    assert described["type"] == "absolute_runtime_path"
    assert described["required"] is False
    assert described["manifest_option_key"] == "scratch_dir"
    assert described["manifest_default"] == "std::string(default_scratch)"
    assert described["gui_label"] == "Copy particles to scratch directory:"
    assert "user_override" in described["allowed_sources"]
    assert described["authority_order"][0] == "user_override"
    assert "default" not in catalog.mapping(operation)["params"]["scratch_directory"]
    assert rendered.options_by_key["scratch_dir"] == scratch_directory
    assert (
        f"Copy particles to scratch directory: == {scratch_directory}"
        in rendered.option_assignments
    )


@pytest.mark.parametrize(("operation", "inputs", "required_params"), SCRATCHABLE_JOB_CASES)
def test_omitted_scratch_directory_preserves_native_environment_default(
    operation,
    inputs,
    required_params,
):
    rendered = Catalog().render_job(
        operation,
        inputs=inputs,
        params=required_params,
    )

    assert "scratch_dir" not in rendered.options_by_key
    assert all(
        not assignment.startswith("Copy particles to scratch directory:")
        for assignment in rendered.option_assignments
    )


@pytest.mark.parametrize(("operation", "inputs", "required_params"), SCRATCHABLE_JOB_CASES)
def test_explicit_empty_scratch_directory_disables_native_default_for_job(
    operation,
    inputs,
    required_params,
):
    rendered = Catalog().render_job(
        operation,
        inputs=inputs,
        params={**required_params, "scratch_directory": ""},
    )

    assert rendered.options_by_key["scratch_dir"] == ""
    assert "Copy particles to scratch directory: == " in rendered.option_assignments
    assert "Copy particles to scratch directory: == ;" in rendered.option_string


@pytest.mark.parametrize(
    ("scratch_directory", "message"),
    [
        ("scratch/relion", "absolute POSIX"),
        ("/", "filesystem root"),
        ("//", "filesystem root"),
        ("/scratch/../escape", "path segments"),
        ("/scratch/./job", "path segments"),
        ("/scratch/relion_volatile", "parent base"),
        ("/scratch/relion_volatile/", "parent base"),
        ("/scratch\njob", "control characters"),
        ("/scratch;Injected == Yes", "option delimiters"),
        ("/scratch/$(touch_bad)", "only letters"),
        ("/scratch/job&other", "only letters"),
        ("/scratch/job name", "only letters"),
        (123, "must be strings"),
    ],
)
def test_scratch_directory_rejects_unsafe_runtime_paths(scratch_directory, message):
    with pytest.raises(CatalogError, match=message):
        Catalog().render_job(
            "two_d_classification",
            inputs={"particles": "Extract/job007/particles.star"},
            params={"num_classes": 50, "mask_diameter_A": 200, "scratch_directory": scratch_directory},
        )


def test_three_d_classification_no_alignment_and_reference_mask_are_typed():
    catalog = Catalog()
    inputs = {
        "particles": "Refine3D/job020/run_data.star",
        "reference_volume": "Refine3D/job020/run_class001.mrc",
        "reference_mask": "MaskCreate/job021/mask.mrc",
    }
    required_params = {"num_classes": 4, "mask_diameter_A": 200, "symmetry": "C1"}

    no_alignment = catalog.render_job(
        "three_d_classification",
        inputs=inputs,
        params={**required_params, "alignment": False},
    )
    aligned = catalog.render_job(
        "three_d_classification",
        inputs=inputs,
        params={**required_params, "alignment": True},
    )
    global_no_alignment = catalog.render_job(
        "three_d_classification",
        inputs={
            "particles": inputs["particles"],
            "reference_volume": inputs["reference_volume"],
        },
        params={**required_params, "alignment": False},
    )
    existing_default = catalog.render_job(
        "three_d_classification",
        inputs={
            "particles": inputs["particles"],
            "reference_volume": inputs["reference_volume"],
        },
        params=required_params,
    )
    described = catalog.describe_operation("three_d_classification")
    neutral = catalog.neutral["three_d_classification"]

    assert no_alignment.options_by_key["fn_mask"] == "MaskCreate/job021/mask.mrc"
    assert no_alignment.options_by_key["dont_skip_align"] == "No"
    assert aligned.options_by_key["dont_skip_align"] == "Yes"
    assert global_no_alignment.options_by_key["dont_skip_align"] == "No"
    assert "fn_mask" not in global_no_alignment.options_by_key
    assert "dont_skip_align" not in existing_default.options_by_key
    assert "Reference mask (optional): == MaskCreate/job021/mask.mrc" in (
        no_alignment.option_assignments
    )
    assert "Perform image alignment? == No" in no_alignment.option_assignments
    assert described["inputs"]["reference_mask"]["manifest_option_key"] == "fn_mask"
    assert described["params"]["alignment"]["manifest_option_key"] == "dont_skip_align"
    assert described["params"]["alignment"]["manifest_default"] is True
    assert neutral["required_inputs"]["reference_mask"]["result_types"] == ["mask"]
    assert neutral["params"]["alignment"]["default"] is True
    assert neutral["params"]["alignment"]["allowed_sources"] == [
        "workflow_default",
        "user_override",
    ]


@pytest.mark.parametrize(
    "operation",
    ["two_d_classification", "initial_model", "three_d_classification"],
)
def test_adaptive_classification_neutral_and_relion_parameters_remain_aligned(operation):
    catalog = Catalog()

    assert set(catalog.neutral[operation]["params"]) == set(catalog.mapping(operation)["params"])


@pytest.mark.parametrize(
    ("operation", "parameter"),
    [
        ("two_d_classification", "num_classes"),
        ("two_d_classification", "number_of_vdam_mini_batches"),
        ("two_d_classification", "regularisation_parameter"),
        ("two_d_classification", "e_step_resolution_limit_A"),
        ("two_d_classification", "offset_search_range_pix"),
        ("initial_model", "num_classes"),
        ("initial_model", "number_of_vdam_mini_batches"),
        ("initial_model", "regularisation_parameter"),
        ("three_d_classification", "num_classes"),
        ("three_d_classification", "initial_lowpass_A"),
        ("three_d_classification", "number_of_iterations"),
        ("three_d_classification", "regularisation_parameter"),
        ("three_d_classification", "e_step_resolution_limit_A"),
        ("three_d_classification", "use_blush_regularisation"),
    ],
)
def test_conditional_classification_parameter_allows_strategy_intent(operation, parameter):
    definition = Catalog().neutral[operation]["params"][parameter]

    assert definition["strategy_selectable"] is True
    assert "strategy_intent" in definition["allowed_sources"]


def test_particle_selection_score_threshold_allows_evidence_gated_strategy_intent():
    catalog = Catalog()
    definition = catalog.neutral["particle_selection"]["params"][
        "minimum_selection_score"
    ]
    described = catalog.describe_operation("particle_selection")["params"][
        "minimum_selection_score"
    ]

    assert definition["source"] == "user_or_agent"
    assert definition["strategy_selectable"] is True
    assert definition["allowed_sources"] == [
        "strategy_intent",
        "user_override",
        "workflow_default",
    ]
    assert definition["authority_order"] == [
        "strategy_intent",
        "user_override",
        "workflow_default",
    ]
    assert definition["impacts"] == ["particle_selection"]
    assert described["strategy_selectable"] is True
    assert described["allowed_sources"] == definition["allowed_sources"]
    assert described["authority_order"] == definition["authority_order"]


def test_refine3d_solvent_corrected_fsc_is_strategy_selectable_and_renders_with_mask():
    catalog = Catalog()
    definition = catalog.neutral["refine3d"]["params"]["solvent_correct_fsc"]
    described = catalog.describe_operation("refine3d")["params"]["solvent_correct_fsc"]

    assert definition["default"] is False
    assert definition["source"] == "user_or_agent"
    assert definition["exposure"] == "user_overridable"
    assert definition["strategy_selectable"] is True
    assert definition["allowed_sources"] == [
        "strategy_intent",
        "user_override",
        "workflow_default",
    ]
    assert definition["authority_order"] == [
        "strategy_intent",
        "user_override",
        "workflow_default",
    ]
    assert described["default"] is False
    assert described["strategy_selectable"] is True
    assert described["allowed_sources"] == definition["allowed_sources"]
    assert described["authority_order"] == definition["authority_order"]
    assert described["manifest_option_key"] == "do_solvent_fsc"
    assert described["gui_label"] == "Use solvent-flattened FSCs?"

    rendered = catalog.render_job(
        "refine3d",
        inputs={
            "particles": "Select/job014/particles.star",
            "reference_volume": "InitialModel/job015/initial_model.mrc",
            "mask": "MaskCreate/job021/mask.mrc",
        },
        params={"mask_diameter_A": 200, "symmetry": "C1", "solvent_correct_fsc": True},
    )

    assert rendered.options_by_key["fn_mask"] == "MaskCreate/job021/mask.mrc"
    assert rendered.options_by_key["do_solvent_fsc"] == "Yes"
    assert "Use solvent-flattened FSCs? == Yes" in rendered.option_assignments


def test_refine3d_solvent_corrected_fsc_requires_mask_only_when_enabled():
    catalog = Catalog()
    inputs = {
        "particles": "Select/job014/particles.star",
        "reference_volume": "InitialModel/job015/initial_model.mrc",
    }

    with pytest.raises(CatalogError, match="requires a reference mask"):
        catalog.render_job(
            "refine3d",
            inputs=inputs,
            params={"mask_diameter_A": 200, "symmetry": "C1", "solvent_correct_fsc": True},
        )

    explicitly_disabled = catalog.render_job(
        "refine3d",
        inputs=inputs,
        params={"mask_diameter_A": 200, "symmetry": "C1", "solvent_correct_fsc": False},
    )
    assert explicitly_disabled.options_by_key["do_solvent_fsc"] == "No"
    assert "fn_mask" not in explicitly_disabled.options_by_key


def test_refine3d_solvent_corrected_fsc_safe_default_remains_omitted():
    rendered = Catalog().render_job(
        "refine3d",
        inputs={
            "particles": "Select/job014/particles.star",
            "reference_volume": "InitialModel/job015/initial_model.mrc",
        },
        params={"mask_diameter_A": 200, "symmetry": "C1"},
    )

    assert "do_solvent_fsc" not in rendered.options_by_key
    assert "fn_mask" not in rendered.options_by_key


def test_ctf_refinement_scientific_controls_allow_exact_user_override_and_render():
    catalog = Catalog()
    scientific_controls = {
        "fit_ctf",
        "fit_defocus",
        "fit_astigmatism",
        "fit_beamtilt",
        "fit_anisotropic_magnification",
        "fit_trefoil",
        "fit_fourth_order_aberrations",
    }

    for parameter in scientific_controls:
        definition = catalog.neutral["ctf_refinement"]["params"][parameter]
        assert "user_override" in definition["allowed_sources"]
        assert definition["authority_order"][0] == "user_override"

    rendered = catalog.render_job(
        "ctf_refinement",
        inputs={
            "particles": "Refine3D/job158/run_data.star",
            "postprocess": "PostProcess/job161/postprocess.star",
        },
        params={
            "fit_ctf": True,
            "fit_defocus": "Per-particle",
            "fit_astigmatism": "No",
            "fit_beamtilt": False,
            "fit_anisotropic_magnification": False,
            "fit_trefoil": False,
            "fit_fourth_order_aberrations": False,
        },
    )

    assert rendered.options_by_key["do_ctf"] == "Yes"
    assert rendered.options_by_key["do_defocus"] == "Per-particle"
    assert rendered.options_by_key["do_astig"] == "No"
    assert rendered.options_by_key["do_tilt"] == "No"
    assert rendered.options_by_key["do_aniso_mag"] == "No"
    assert rendered.options_by_key["do_trefoil"] == "No"
    assert rendered.options_by_key["do_4thorder"] == "No"


def test_import_movies_can_be_satisfied_by_raw_movie_glob():
    catalog = Catalog()
    rendered = catalog.render_job(
        "import_movies",
        params={"raw_movie_glob": "Movies/*.tiff", "pixel_size_A": 0.885},
    )
    assert rendered.options_by_key["fn_in_raw"] == "Movies/*.tiff"


def test_import_modes_render_fixed_selectors_without_defaults():
    catalog = Catalog()

    movies = catalog.render_job(
        "import_movies",
        params={"raw_movie_glob": "Movies/*.tiff", "pixel_size_A": 0.885},
        include_defaults=False,
    )
    particles = catalog.render_job(
        "import_particles",
        inputs={"particles": "External/CryoSPARC/export/particles.star"},
        include_defaults=False,
    )

    assert movies.options_by_key["do_raw"] == "Yes"
    assert movies.options_by_key["do_other"] == "No"
    assert particles.options_by_key["do_raw"] == "No"
    assert particles.options_by_key["do_other"] == "Yes"
    assert particles.options_by_key["node_type"] == "Particles STAR file (.star)"


def test_import_particles_legacy_import_type_is_compatibility_only():
    catalog = Catalog()
    rendered = catalog.render_job(
        "import_particles",
        inputs={"particles": "External/CryoSPARC/export/particles.star"},
        params={"import_type": "Particles STAR file (.star)"},
        include_defaults=False,
    )

    assert rendered.options_by_key["node_type"] == "Particles STAR file (.star)"
    assert (
        catalog.describe_operation("import_particles")["params"]["import_type"][
            "compatibility_only"
        ]
        is True
    )

    with pytest.raises(CatalogError, match="import_type is fixed"):
        catalog.render_job(
            "import_particles",
            inputs={"particles": "External/CryoSPARC/export/particles.star"},
            params={"import_type": "3D reference (.mrc)"},
        )


def test_import_particles_declares_particle_result_type():
    assert Catalog().describe_operation("import_particles")["expected_results"] == [
        "selected_particles"
    ]


@pytest.mark.parametrize("invalid_fixed_options", [False, [], "", 0])
def test_catalog_rejects_falsey_nonmapping_fixed_options(invalid_fixed_options):
    catalog = Catalog()
    catalog.mappings["import_particles"]["fixed_options"] = invalid_fixed_options

    issues = catalog.validate_catalog()
    assert any(
        "fixed_options for import_particles must be a mapping" in issue["message"]
        for issue in issues
    )
    with pytest.raises(
        CatalogError, match="fixed_options for import_particles must be a mapping"
    ):
        catalog.render_job(
            "import_particles",
            inputs={"particles": "External/CryoSPARC/export/particles.star"},
        )


def test_catalog_rejects_unknown_fixed_option_manifest_key():
    catalog = Catalog()
    catalog.mappings["import_particles"]["fixed_options"]["not_a_native_option"] = {
        "value": True,
        "type": "bool",
        "format": "yes_no",
    }

    issues = catalog.validate_catalog()
    assert any(
        "references missing manifest key not_a_native_option" in issue["message"]
        for issue in issues
    )
    with pytest.raises(CatalogError, match="references missing manifest key"):
        catalog.render_job(
            "import_particles",
            inputs={"particles": "External/CryoSPARC/export/particles.star"},
        )


def test_catalog_rejects_fixed_option_mapping_collision():
    catalog = Catalog()
    catalog.mappings["import_particles"].setdefault("params", {})["raw_mode"] = {
        "required": False,
        "type": "bool",
        "manifest_option_key": "do_raw",
    }

    issues = catalog.validate_catalog()
    assert any(
        "fixed_options.do_raw collides with params.raw_mode" in issue["message"]
        for issue in issues
    )
    with pytest.raises(CatalogError, match="fixed_options.do_raw collides"):
        catalog.render_job(
            "import_particles",
            inputs={"particles": "External/CryoSPARC/export/particles.star"},
        )


@pytest.mark.parametrize(
    ("gain_rotation", "expected"),
    [
        (0, "No rotation (0)"),
        (1, "90 degrees (1)"),
        (2, "180 degrees (2)"),
        (3, "270 degrees (3)"),
    ],
)
def test_motion_correction_gain_rotation_renders_relion_radio_label(
    gain_rotation,
    expected,
):
    rendered = Catalog().render_job(
        "motion_correction",
        inputs={"movies": "Import/job002/movies.star"},
        params={
            "gain_reference": "Movies/gain.mrc",
            "gain_rotation": gain_rotation,
        },
    )

    assert rendered.options_by_key["gain_rot"] == expected
    assert f"Gain rotation: == {expected}" in rendered.option_assignments


@pytest.mark.parametrize(
    ("gain_flip", "expected"),
    [
        (0, "No flipping (0)"),
        (1, "Flip upside down (1)"),
        (2, "Flip left to right (2)"),
    ],
)
def test_motion_correction_gain_flip_renders_relion_radio_label(gain_flip, expected):
    rendered = Catalog().render_job(
        "motion_correction",
        inputs={"movies": "Import/job002/movies.star"},
        params={
            "gain_reference": "Movies/gain.mrc",
            "gain_flip": gain_flip,
        },
    )

    assert rendered.options_by_key["gain_flip"] == expected
    assert f"Gain flip: == {expected}" in rendered.option_assignments


def test_rejects_unsafe_project_relative_path():
    catalog = Catalog()
    try:
        catalog.render_job(
            "motion_correction",
            inputs={"movies": "../outside.star"},
            params={},
        )
    except CatalogError as exc:
        assert "project-relative" in str(exc)
    else:
        raise AssertionError("Unsafe path was accepted")


def test_support_filter_excludes_direct_command_when_not_requested():
    catalog = Catalog()
    schedule_only = catalog.list_operations("schedule_mappable")
    assert all(item["support_level"] == "schedule_mappable" for item in schedule_only)
    assert all(item["operation"] != "rescale_map" for item in schedule_only)


def test_align_symmetry_reference_direct_contract_is_explicit_and_bounded():
    catalog = Catalog()

    listed = {
        item["operation"]: item
        for item in catalog.list_operations("direct_command")
    }
    assert listed["align_symmetry_reference"]["support_level"] == "direct_command"
    assert listed["align_symmetry_reference"]["job_type"] == "relion_align_symmetry"

    described = catalog.describe_operation("align_symmetry_reference")
    assert described["operation"] == "align_symmetry_reference"
    assert described["support_level"] == "direct_command"
    assert described["executable"] is True
    assert described["render_tool"] == "relion_render_symmetry_alignment"
    assert described["execution_tool"] == "relion_align_symmetry_reference"
    assert described["definition"]["required_inputs"]["input_map"]["required"] is True
    assert set(described["definition"]["params"]) == {
        "output_map",
        "search_max_resolution_A",
        "target_symmetry",
    }

    validated = catalog.validate_direct_params(
        "align_symmetry_reference",
        {
            "output_map": "ReferencePrep/aligned_c4.mrc",
            "search_max_resolution_A": 20.0,
            "target_symmetry": "C4",
        },
    )
    assert validated == {
        "output_map": "ReferencePrep/aligned_c4.mrc",
        "search_max_resolution_A": 20.0,
        "target_symmetry": "C4",
    }


@pytest.mark.parametrize(
    "target_symmetry",
    ["C2", "C64", "D2", "D32", "T", "O", "I1", "I2", "I3", "I4"],
)
def test_align_symmetry_reference_accepts_bounded_relion_point_groups(
    target_symmetry,
):
    validated = Catalog().validate_direct_params(
        "align_symmetry_reference",
        {
            "output_map": "ReferencePrep/aligned.mrc",
            "search_max_resolution_A": 20.0,
            "target_symmetry": target_symmetry,
        },
    )

    assert validated["target_symmetry"] == target_symmetry


@pytest.mark.parametrize(
    "target_symmetry",
    [
        "C1",
        "C65",
        "D33",
        "I",
        "I5",
        "c4",
        "../C4",
        "C4;Injected == Yes",
    ],
)
def test_align_symmetry_reference_rejects_unsafe_or_unbounded_point_groups(
    target_symmetry,
):
    with pytest.raises(CatalogError, match="target_symmetry"):
        Catalog().validate_direct_params(
            "align_symmetry_reference",
            {
                "output_map": "ReferencePrep/aligned.mrc",
                "search_max_resolution_A": 20.0,
                "target_symmetry": target_symmetry,
            },
        )


@pytest.mark.parametrize(
    ("params", "message"),
    [
        (
            {
                "output_map": "../aligned.mrc",
                "search_max_resolution_A": 20.0,
                "target_symmetry": "C4",
            },
            "project-relative",
        ),
        (
            {
                "output_map": "Maps/aligned.mrc",
                "search_max_resolution_A": 20.0,
                "target_symmetry": "C4",
            },
            "dedicated ReferencePrep",
        ),
        (
            {
                "output_map": "ReferencePrep/aligned.mrc",
                "search_max_resolution_A": 0.0,
                "target_symmetry": "C4",
            },
            "at least",
        ),
        (
            {
                "output_map": "ReferencePrep/aligned.mrc",
                "search_max_resolution_A": 20.0,
                "target_symmetry": "C4",
                "apply_sym": True,
            },
            "Unknown direct-command parameters",
        ),
    ],
)
def test_align_symmetry_reference_rejects_unsafe_or_expansive_parameters(
    params,
    message,
):
    with pytest.raises(CatalogError, match=message):
        Catalog().validate_direct_params("align_symmetry_reference", params)


def test_rejects_semicolon_in_string_parameter():
    catalog = Catalog()
    try:
        catalog.render_job(
            "ctf_estimation",
            inputs={"micrographs": "MotionCorr/job002/corrected_micrographs.star"},
            params={"ctffind_executable": "ctffind;Injected == Yes"},
        )
    except CatalogError as exc:
        assert "semicolons" in str(exc) or "option delimiters" in str(exc)
    else:
        raise AssertionError("Semicolon-delimited option injection was accepted")


def test_ctffind_accepts_absolute_executable_path():
    catalog = Catalog()
    executable = "/opt/ctffind/bin/ctffind"

    rendered = catalog.render_job(
        "ctf_estimation",
        inputs={"micrographs": "MotionCorr/job002/corrected_micrographs.star"},
        params={"ctffind_executable": executable},
    )

    assert rendered.options_by_key["fn_ctffind_exe"] == executable


def test_ctf_estimation_defaults_to_ctffind4_selected():
    rendered = Catalog().render_job(
        "ctf_estimation",
        inputs={"micrographs": "MotionCorr/job002/corrected_micrographs.star"},
    )

    assert rendered.options_by_key["use_ctffind4"] == "Yes"
    assert "Use CTFFIND-4.1? == Yes" in rendered.option_assignments


@pytest.mark.parametrize(
    ("use_ctffind4", "expected"),
    [(True, "Yes"), (False, "No")],
)
def test_ctf_estimation_use_ctffind4_renders_relion_yes_no(use_ctffind4, expected):
    rendered = Catalog().render_job(
        "ctf_estimation",
        inputs={"micrographs": "MotionCorr/job002/corrected_micrographs.star"},
        params={"use_ctffind4": use_ctffind4},
    )

    assert rendered.options_by_key["use_ctffind4"] == expected
    assert f"Use CTFFIND-4.1? == {expected}" in rendered.option_assignments


@pytest.mark.parametrize(
    "symmetry",
    ["C1", "C2", "C17", "C100", "D2", "D42", "D1234", "I", "O", "T"],
)
def test_accepts_supported_point_group_symmetry(symmetry):
    rendered = Catalog().render_job(
        "initial_model",
        inputs={"particles": "Select/job008/particles.star"},
        params={"num_classes": 1, "mask_diameter_A": 200, "symmetry": symmetry},
    )

    assert rendered.options_by_key["sym_name"] == symmetry


@pytest.mark.parametrize(
    "symmetry",
    [
        "C0",
        "C01",
        "D1",
        "D02",
        "I1",
        "O2",
        "T3",
        "c2",
        " C2",
        "C2 ",
        "C",
        "D",
        "C2;Injected == Yes",
        2,
    ],
)
def test_rejects_unsupported_point_group_symmetry(symmetry):
    with pytest.raises(CatalogError, match=r"C1, Cn or Dn \(n >= 2\), I, O, or T"):
        Catalog().render_job(
            "initial_model",
            inputs={"particles": "Select/job008/particles.star"},
            params={"num_classes": 1, "mask_diameter_A": 200, "symmetry": symmetry},
        )


def test_rejects_unsafe_executable_shell_syntax():
    catalog = Catalog()
    try:
        catalog.render_job(
            "ctf_estimation",
            inputs={"micrographs": "MotionCorr/job002/corrected_micrographs.star"},
            params={"ctffind_executable": "$(touch /tmp/injected)"},
        )
    except CatalogError as exc:
        assert "executable" in str(exc)
    else:
        raise AssertionError("Executable shell syntax was accepted")


def test_topaz_training_accepts_absolute_executable_path():
    catalog = Catalog()
    executable = "/opt/topaz/0.2.4/bin/topaz"

    rendered = catalog.render_job(
        "topaz_training",
        inputs={
            "micrographs": "Select/job005/micrographs_split1.star",
            "particles": "JoinStar/job017/join_particles.star",
        },
        params={"topaz_executable": executable, "topaz_particle_diameter_A": 180},
    )

    assert rendered.options_by_key["fn_topaz_exe"] == executable


def test_topaz_picking_accepts_absolute_executable_path():
    catalog = Catalog()
    executable = "/opt/topaz/0.2.4/bin/topaz"

    rendered = catalog.render_job(
        "topaz_picking",
        inputs={
            "micrographs": "CtfFind/job004/micrographs_ctf.star",
            "model": "AutoPick/job018/model_training.sav",
        },
        params={"topaz_executable": executable, "topaz_particle_diameter_A": 180},
    )

    assert rendered.options_by_key["fn_topaz_exe"] == executable


def test_topaz_picking_defaults_to_one_mpi_rank():
    rendered = Catalog().render_job(
        "topaz_picking",
        params={"topaz_particle_diameter_A": 180},
        inputs={
            "micrographs": "CtfFind/job004/micrographs_ctf.star",
            "model": "AutoPick/job018/model_training.sav",
        },
    )

    assert rendered.options_by_key["nr_mpi"] == "1"


@pytest.mark.parametrize("number_of_mpi", [0, -1])
def test_topaz_picking_rejects_nonpositive_mpi_count(number_of_mpi):
    with pytest.raises(
        CatalogError,
        match="parameter number_of_mpi must be at least 1",
    ):
        Catalog().render_job(
            "topaz_picking",
            inputs={
                "micrographs": "CtfFind/job004/micrographs_ctf.star",
                "model": "AutoPick/job018/model_training.sav",
            },
            params={"number_of_mpi": number_of_mpi, "topaz_particle_diameter_A": 180},
        )


def test_mapped_parameter_maximum_is_enforced():
    catalog = Catalog()
    boundary = catalog.render_job(
        "import_movies",
        params={
            "raw_movie_glob": "Movies/*.tiff",
            "pixel_size_A": 0.885,
            "amplitude_contrast": 1,
        },
    )

    assert boundary.options_by_key["Q0"] == "1.0"

    with pytest.raises(
        CatalogError,
        match="parameter amplitude_contrast must be at most 1",
    ):
        catalog.render_job(
            "import_movies",
            params={
                "raw_movie_glob": "Movies/*.tiff",
                "pixel_size_A": 0.885,
                "amplitude_contrast": 1.1,
            },
        )


@pytest.mark.parametrize("gpu_ids", ["0:1:2:3", "1:1:1:1"])
def test_topaz_picking_accepts_one_gpu_assignment_per_mpi_rank(gpu_ids):
    rendered = Catalog().render_job(
        "topaz_picking",
        inputs={
            "micrographs": "CtfFind/job004/micrographs_ctf.star",
            "model": "AutoPick/job018/model_training.sav",
        },
        params={
            "topaz_particle_diameter_A": 180,
            "use_gpu": True,
            "gpu_ids": gpu_ids,
            "number_of_mpi": 4,
        },
    )

    assert rendered.options_by_key["nr_mpi"] == "4"
    assert rendered.options_by_key["gpu_ids"] == gpu_ids


def test_topaz_picking_mpi_allows_automatic_gpu_allocation():
    rendered = Catalog().render_job(
        "topaz_picking",
        inputs={
            "micrographs": "CtfFind/job004/micrographs_ctf.star",
            "model": "AutoPick/job018/model_training.sav",
        },
        params={"use_gpu": True, "number_of_mpi": 4, "topaz_particle_diameter_A": 180},
    )

    assert rendered.options_by_key["nr_mpi"] == "4"
    assert "gpu_ids" not in rendered.options_by_key


@pytest.mark.parametrize("gpu_ids", ["1", "0::2:3"])
def test_topaz_picking_rejects_missing_gpu_rank_assignments(gpu_ids):
    with pytest.raises(
        CatalogError,
        match="one colon-delimited GPU assignment per MPI rank",
    ):
        Catalog().render_job(
            "topaz_picking",
            inputs={
                "micrographs": "CtfFind/job004/micrographs_ctf.star",
                "model": "AutoPick/job018/model_training.sav",
            },
            params={
                "topaz_particle_diameter_A": 180,
                "use_gpu": True,
                "gpu_ids": gpu_ids,
                "number_of_mpi": 4,
            },
        )


def test_particle_selection_auto_class_ranker_defaults_recenter_off():
    rendered = Catalog().render_job(
        "particle_selection",
        inputs={"classes": "Class2D/job008/run_it025_model.star"},
        params={"auto_select_2d_classes": True, "minimum_selection_score": 0.05},
    )

    assert rendered.options_by_key["do_class_ranker"] == "Yes"
    assert rendered.options_by_key["rank_threshold"] == "0.05"
    assert rendered.options_by_key["do_recenter"] == "No"
    assert "Re-center the class averages? == No" in rendered.option_assignments


def test_particle_selection_recenter_class_averages_renders_relion_yes_no():
    rendered = Catalog().render_job(
        "particle_selection",
        inputs={"classes": "Class2D/job008/run_it025_model.star"},
        params={"recenter_class_averages": True},
    )

    assert rendered.options_by_key["do_recenter"] == "Yes"
    assert "Re-center the class averages? == Yes" in rendered.option_assignments


def test_particle_selection_renders_exact_class_number_metadata_filter():
    catalog = Catalog()

    rendered = catalog.render_job(
        "particle_selection",
        inputs={"particles": "Class3D/job019/run_it025_data.star"},
        params={
            "select_by_metadata_values": True,
            "selection_label": "rlnClassNumber",
            "selection_minimum": 1,
            "selection_maximum": 1,
        },
    )

    assert rendered.relion_job_type == "Select"
    assert rendered.options_by_key["fn_data"] == "Class3D/job019/run_it025_data.star"
    assert rendered.options_by_key["do_select_values"] == "Yes"
    assert rendered.options_by_key["select_label"] == "rlnClassNumber"
    assert rendered.options_by_key["select_minval"] == "1"
    assert rendered.options_by_key["select_maxval"] == "1"
    assert "OR select from particles.star: == Class3D/job019/run_it025_data.star" in rendered.option_assignments
    assert "Select based on metadata values? == Yes" in rendered.option_assignments
    assert "Metadata label for subset selection: == rlnClassNumber" in rendered.option_assignments
    assert "Minimum metadata value: == 1" in rendered.option_assignments
    assert "Maximum metadata value: == 1" in rendered.option_assignments


@pytest.mark.parametrize(
    ("inputs", "params"),
    [
        (
            {},
            {
                "select_by_metadata_values": True,
                "selection_label": "rlnClassNumber",
                "selection_minimum": 1,
                "selection_maximum": 1,
            },
        ),
        (
            {"particles": "Class3D/job019/run_it025_data.star"},
            {
                "select_by_metadata_values": True,
                "selection_minimum": 1,
                "selection_maximum": 1,
            },
        ),
        (
            {"particles": "Class3D/job019/run_it025_data.star"},
            {
                "select_by_metadata_values": True,
                "selection_label": "rlnClassNumber",
                "selection_maximum": 1,
            },
        ),
        (
            {"particles": "Class3D/job019/run_it025_data.star"},
            {
                "select_by_metadata_values": True,
                "selection_label": "rlnClassNumber",
                "selection_minimum": 1,
            },
        ),
    ],
)
def test_particle_selection_rejects_incomplete_metadata_filter(inputs, params):
    with pytest.raises(CatalogError, match="missing native options"):
        Catalog().render_job("particle_selection", inputs=inputs, params=params)


def test_particle_selection_rejects_metadata_values_when_filter_is_disabled():
    with pytest.raises(CatalogError, match="select_by_metadata_values=true"):
        Catalog().render_job(
            "particle_selection",
            inputs={"particles": "Class3D/job019/run_it025_data.star"},
            params={
                "select_by_metadata_values": False,
                "selection_label": "rlnClassNumber",
                "selection_minimum": 1,
                "selection_maximum": 1,
            },
        )


def test_particle_selection_rejects_unsafe_metadata_label():
    with pytest.raises(CatalogError):
        Catalog().render_job(
            "particle_selection",
            inputs={"particles": "Class3D/job019/run_it025_data.star"},
            params={
                "select_by_metadata_values": True,
                "selection_label": "rlnClassNumber;do_split == Yes",
                "selection_minimum": 1,
                "selection_maximum": 1,
            },
        )


def test_particle_selection_rejects_reversed_metadata_range():
    with pytest.raises(CatalogError, match="may not exceed"):
        Catalog().render_job(
            "particle_selection",
            inputs={"particles": "Class3D/job019/run_it025_data.star"},
            params={
                "select_by_metadata_values": True,
                "selection_label": "rlnClassNumber",
                "selection_minimum": 2,
                "selection_maximum": 1,
            },
        )


@pytest.mark.parametrize(
    ("extra_inputs", "extra_params"),
    [
        ({"classes": "Class3D/job019/run_it025_optimiser.star"}, {}),
        ({}, {"auto_select_2d_classes": True}),
    ],
)
def test_particle_selection_rejects_conflicting_metadata_modes(extra_inputs, extra_params):
    inputs = {
        "particles": "Class3D/job019/run_it025_data.star",
        **extra_inputs,
    }
    params = {
        "select_by_metadata_values": True,
        "selection_label": "rlnClassNumber",
        "selection_minimum": 1,
        "selection_maximum": 1,
        **extra_params,
    }

    with pytest.raises(CatalogError, match="cannot be combined"):
        Catalog().render_job("particle_selection", inputs=inputs, params=params)


@pytest.mark.parametrize("input_count", [2, 3, 4])
def test_join_star_renders_two_to_four_particle_inputs(input_count):
    inputs = {
        f"particle_star_{index}": f"Select/job{index:03d}/particles.star"
        for index in range(1, input_count + 1)
    }

    rendered = Catalog().render_job(
        "join_star",
        inputs=inputs,
        params={"combine_particles": True},
    )

    assert rendered.relion_job_type == "JoinStar"
    assert rendered.options_by_key["do_part"] == "Yes"
    assert rendered.options_by_key["do_mic"] == "No"
    assert rendered.options_by_key["do_mov"] == "No"
    for index in range(1, input_count + 1):
        assert rendered.options_by_key[f"fn_part{index}"] == inputs[f"particle_star_{index}"]


def test_join_star_rejects_one_particle_input():
    with pytest.raises(CatalogError, match="two to four inputs"):
        Catalog().render_job(
            "join_star",
            inputs={"particle_star_1": "Select/job001/particles.star"},
            params={"combine_particles": True},
        )


def test_join_star_rejects_gapped_particle_inputs():
    with pytest.raises(CatalogError, match="contiguous"):
        Catalog().render_job(
            "join_star",
            inputs={
                "particle_star_1": "Select/job001/particles.star",
                "particle_star_3": "Select/job003/particles.star",
            },
            params={"combine_particles": True},
        )


def test_join_star_rejects_duplicate_particle_inputs():
    with pytest.raises(CatalogError, match="distinct"):
        Catalog().render_job(
            "join_star",
            inputs={
                "particle_star_1": "Select/job001/particles.star",
                "particle_star_2": "Select/job001/particles.star",
            },
            params={"combine_particles": True},
        )


def test_join_star_rejects_inputs_mixed_across_modes():
    with pytest.raises(CatalogError, match="do not match"):
        Catalog().render_job(
            "join_star",
            inputs={
                "particle_star_1": "Select/job001/particles.star",
                "particle_star_2": "Select/job002/particles.star",
                "micrograph_star_1": "CtfFind/job003/micrographs_ctf.star",
            },
            params={"combine_particles": True},
        )


def test_join_star_rejects_no_enabled_mode():
    with pytest.raises(CatalogError, match="exactly one enabled combine mode"):
        Catalog().render_job(
            "join_star",
            inputs={
                "particle_star_1": "Select/job001/particles.star",
                "particle_star_2": "Select/job002/particles.star",
            },
        )


def test_join_star_rejects_multiple_enabled_modes():
    with pytest.raises(CatalogError, match="exactly one enabled combine mode"):
        Catalog().render_job(
            "join_star",
            inputs={
                "particle_star_1": "Select/job001/particles.star",
                "particle_star_2": "Select/job002/particles.star",
                "micrograph_star_1": "CtfFind/job003/micrographs_ctf.star",
                "micrograph_star_2": "CtfFind/job004/micrographs_ctf.star",
            },
            params={"combine_particles": True, "combine_micrographs": True},
        )


def test_remove_duplicate_particles_direct_contract_is_explicit_and_bounded():
    catalog = Catalog()

    listed = {
        item["operation"]: item
        for item in catalog.list_operations("direct_command")
    }
    assert listed["remove_duplicate_particles"]["support_level"] == "direct_command"
    assert listed["remove_duplicate_particles"]["job_type"] == "relion_star_handler"

    described = catalog.describe_operation("remove_duplicate_particles")
    assert described["support_level"] == "direct_command"
    assert described["executable"] is True
    assert described["render_tool"] == "relion_render_remove_duplicate_particles"
    assert described["execution_tool"] == "relion_remove_duplicate_particles"
    assert described["definition"]["required_inputs"]["particles"]["required"] is True
    assert set(described["definition"]["params"]) == {
        "duplicate_distance_A",
        "image_pixel_size_A",
        "output_star",
    }

    validated = catalog.validate_direct_params(
        "remove_duplicate_particles",
        {
            "duplicate_distance_A": 140.0,
            "output_star": "Deduplicate/union_dedup.star",
        },
    )
    assert validated == {
        "duplicate_distance_A": 140.0,
        "output_star": "Deduplicate/union_dedup.star",
    }


@pytest.mark.parametrize(
    ("output_star", "message"),
    [
        ("Extract/job001/particles.star", "Deduplicate/"),
        ("Deduplicate/union.txt", ".star suffix"),
    ],
)
def test_remove_duplicate_particles_rejects_unsafe_output_paths(output_star, message):
    with pytest.raises(CatalogError) as excinfo:
        Catalog().validate_direct_params(
            "remove_duplicate_particles",
            {"duplicate_distance_A": 140.0, "output_star": output_star},
        )

    assert message in str(excinfo.value)


def test_remove_duplicate_particles_requires_a_positive_distance():
    with pytest.raises(CatalogError):
        Catalog().validate_direct_params(
            "remove_duplicate_particles",
            {"duplicate_distance_A": 0.0, "output_star": "Deduplicate/union.star"},
        )


def test_direct_operations_are_not_renderable_as_pipeliner_jobs():
    with pytest.raises(CatalogError) as excinfo:
        Catalog().render_job("remove_duplicate_particles")

    assert "relion_render_remove_duplicate_particles" in str(excinfo.value)
