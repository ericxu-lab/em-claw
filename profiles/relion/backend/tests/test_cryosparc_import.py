from __future__ import annotations

import errno
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import mrcfile
import numpy as np
import pytest

import relion_mcp.cryosparc_import as cryosparc_import_module
from relion_mcp.cryosparc_import import (
    CryosparcImportError,
    cryosparc_import_environment,
    import_cryosparc_particles,
    render_cryosparc_particle_import,
)
from relion_mcp.settings import (
    ConfigurationError,
    CryosparcProjectIdentity,
    Settings,
)
from relion_mcp.star import parse_star_block


def _settings(
    relion_root: Path,
    source_root: Path,
    pyem_script: Path,
    *,
    mode: str = "run",
) -> Settings:
    return Settings(
        mode=mode,
        project_roots=(relion_root.resolve(),),
        pipeliner_executable="relion_pipeliner",
        image_handler_executable="relion_image_handler",
        pipeline_name="default",
        add_job_timeout_seconds=90,
        utility_timeout_seconds=30,
        cryosparc_import_roots=(source_root.resolve(),),
        pyem_python=sys.executable,
        csparc2star_script=str(pyem_script.resolve()),
    )


def _live_project_identity(uid: str, project: Path) -> CryosparcProjectIdentity:
    resolved = project.resolve()
    project_stat = resolved.stat()
    return CryosparcProjectIdentity(
        uid=uid,
        project_dir=resolved,
        device=project_stat.st_dev,
        inode=project_stat.st_ino,
    )


def _initialize_project(project: Path, *, pipeline_name: str = "default") -> None:
    project.mkdir(parents=True)
    (project / ".gui_projectdir").touch()
    (project / f"{pipeline_name}_pipeline.star").write_text(
        "data_pipeline_general\n\n_rlnPipeLineJobCounter 1\n",
        encoding="utf-8",
    )
    metadata = project / ".relion-mcp"
    metadata.mkdir()
    (metadata / "project.json").write_text(
        json.dumps({"pipeline_name": pipeline_name}) + "\n", encoding="utf-8"
    )


def _write_stack(
    path: Path,
    *,
    count: int = 6,
    box: int = 4,
    pixel_size: float = 1.5,
    ispg: int = 0,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.arange(count * box * box, dtype=np.float32).reshape(count, box, box)
    with mrcfile.new(path) as handle:
        handle.set_data(data)
        handle.header.ispg = ispg
        handle.voxel_size = pixel_size


def _write_primary(
    path: Path,
    *,
    stack_paths: tuple[bytes, ...] = (b"J1/extract/particles.mrc",) * 3,
    stack_indices: tuple[int, ...] = (0, 2, 5),
    uids: tuple[int, ...] = (101, 102, 103),
    stack_shapes: tuple[tuple[int, int], ...] | None = None,
    pixel_sizes: tuple[float, ...] | None = None,
    optics_groups: tuple[int, ...] | None = None,
    include_ctf: bool = False,
    include_alignment: bool = True,
) -> None:
    fields: list[tuple] = [
        ("uid", "<u8"),
        ("blob/path", "S160"),
        ("blob/idx", "<u4"),
        ("blob/shape", "<u4", (2,)),
        ("blob/psize_A", "<f4"),
    ]
    if include_ctf:
        fields.extend(
            [
                ("ctf/df1_A", "<f4"),
                ("ctf/df2_A", "<f4"),
                ("ctf/df_angle_rad", "<f4"),
                ("ctf/accel_kv", "<f4"),
                ("ctf/cs_mm", "<f4"),
                ("ctf/amp_contrast", "<f4"),
            ]
        )
    if optics_groups is not None:
        fields.append(("ctf/exp_group_id", "<i4"))
    if include_alignment:
        fields.extend(
            [
                ("alignments3D/pose", "<f4", (3,)),
                ("alignments3D/shift", "<f4", (2,)),
            ]
        )
    array = np.zeros(len(uids), dtype=np.dtype(fields))
    array["uid"] = uids
    array["blob/path"] = stack_paths
    array["blob/idx"] = stack_indices
    array["blob/shape"] = stack_shapes or ((4, 4),) * len(uids)
    array["blob/psize_A"] = pixel_sizes or (1.5,) * len(uids)
    if include_ctf:
        array["ctf/df1_A"] = 10000
        array["ctf/df2_A"] = 10200
        array["ctf/df_angle_rad"] = 0.1
        array["ctf/accel_kv"] = 300
        array["ctf/cs_mm"] = 2.7
        array["ctf/amp_contrast"] = 0.1
    if optics_groups is not None:
        array["ctf/exp_group_id"] = optics_groups
    if include_alignment:
        array["alignments3D/pose"] = (0.1, 0.2, 0.3)
        array["alignments3D/shift"] = (1.0, -1.0)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        np.save(handle, array, allow_pickle=False)


def _write_passthrough(
    path: Path,
    *,
    uids: tuple[int, ...] = (103, 101, 102),
    micrograph_paths: tuple[bytes, ...] | None = None,
) -> None:
    fields: list[tuple] = [
        ("uid", "<u8"),
        ("ctf/df1_A", "<f4"),
        ("ctf/df2_A", "<f4"),
        ("ctf/df_angle_rad", "<f4"),
        ("ctf/accel_kv", "<f4"),
        ("ctf/cs_mm", "<f4"),
        ("ctf/amp_contrast", "<f4"),
    ]
    if micrograph_paths is not None:
        if len(micrograph_paths) != len(uids):
            raise ValueError("micrograph_paths and uids must have equal lengths")
        fields.append(("location/micrograph_path", "S160"))
    array = np.zeros(len(uids), dtype=np.dtype(fields))
    array["uid"] = uids
    array["ctf/df1_A"] = 10000
    array["ctf/df2_A"] = 10200
    array["ctf/df_angle_rad"] = 0.1
    array["ctf/accel_kv"] = 300
    array["ctf/cs_mm"] = 2.7
    array["ctf/amp_contrast"] = 0.1
    if micrograph_paths is not None:
        array["location/micrograph_path"] = micrograph_paths
    with path.open("xb") as handle:
        np.save(handle, array, allow_pickle=False)


def _write_fake_pyem(
    path: Path,
    references: tuple[str, ...] = (
        "000001@J1/extract/particles.mrc",
        "000003@J1/extract/particles.mrc",
        "000006@J1/extract/particles.mrc",
    ),
    *,
    optics_box: int = 4,
    optics_pixel_size: float = 1.5,
    optics_groups: tuple[int, ...] = (1,),
    particle_groups: tuple[int, ...] | None = None,
    micrograph_paths: tuple[str, ...] | None = None,
) -> None:
    if particle_groups is None:
        particle_groups = (1,) * len(references)
    assert len(particle_groups) == len(references)
    if micrograph_paths is not None:
        assert len(micrograph_paths) == len(references)
    optics_rows = tuple(
        (group, optics_box, optics_pixel_size, 300, 2.7, 0.1, 2)
        for group in optics_groups
    )
    particles = tuple(
        zip(
            references,
            particle_groups,
            micrograph_paths or (None,) * len(references),
            strict=True,
        )
    )
    include_micrographs = micrograph_paths is not None
    script = """#!/usr/bin/env python3
import sys

args = sys.argv[1:]
loglevel = args.index('--loglevel')
output = args[loglevel - 1]
with open(output, 'w', encoding='utf-8', newline='\\n') as handle:
    handle.write('data_optics\\n\\nloop_\\n')
    for number, name in enumerate((
        'rlnOpticsGroup', 'rlnImageSize', 'rlnImagePixelSize', 'rlnVoltage',
        'rlnSphericalAberration', 'rlnAmplitudeContrast',
        'rlnImageDimensionality'), start=1):
        handle.write('_%s #%d\\n' % (name, number))
    for row in OPTICS_ROWS:
        handle.write(' '.join(str(value) for value in row) + '\\n')
    handle.write('\\n')
    handle.write('data_particles\\n\\nloop_\\n')
    particle_columns = [
        'rlnImageName', 'rlnOpticsGroup', 'rlnDefocusU', 'rlnDefocusV',
        'rlnDefocusAngle', 'rlnAngleRot', 'rlnAngleTilt', 'rlnAnglePsi',
        'rlnOriginXAngst', 'rlnOriginYAngst']
    if INCLUDE_MICROGRAPHS:
        particle_columns.append('rlnMicrographName')
    for number, name in enumerate(particle_columns, start=1):
        handle.write('_%s #%d\\n' % (name, number))
    for reference, group, micrograph in reversed(PARTICLES):
        row = reference + ' ' + str(group) + ' 10000 10200 5 10 20 30 1.5 -1.5'
        if INCLUDE_MICROGRAPHS:
            row += ' ' + micrograph
        handle.write(row + '\\n')
    handle.write('\\n')
print('fake pyem completed')
"""
    script = script.replace("OPTICS_ROWS", repr(optics_rows))
    script = script.replace("INCLUDE_MICROGRAPHS", repr(include_micrographs))
    script = script.replace("PARTICLES", repr(particles))
    path.write_text(
        script,
        encoding="utf-8",
    )
    path.chmod(0o755)


@pytest.fixture
def conversion_tree(tmp_path: Path, monkeypatch):
    relion_root = tmp_path / "relion-projects"
    source_root = tmp_path / "cryosparc-projects"
    project = relion_root / "P1-relion"
    source_project = source_root / "P1"
    relion_root.mkdir()
    source_project.mkdir(parents=True)
    _initialize_project(project)
    _write_stack(source_project / "J1/extract/particles.mrc")
    _write_primary(source_project / "J10/J10_particles.cs")
    (source_project / "J10").mkdir(exist_ok=True)
    _write_passthrough(source_project / "J10/J10_passthrough_particles.cs")
    fake_pyem = tmp_path / "csparc2star.py"
    _write_fake_pyem(fake_pyem)
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("EMCLAW_RUNTIME_DIR", str(runtime))
    settings = _settings(relion_root, source_root, fake_pyem)
    return settings, project, source_project, fake_pyem


def test_render_is_read_only_and_describes_class3d_handoff(conversion_tree):
    settings, project, source_project, _ = conversion_tree

    plan = render_cryosparc_particle_import(
        settings,
        str(project),
        str(source_project),
        "J10/J10_particles.cs",
        ["J10/J10_passthrough_particles.cs"],
        "class3d_input",
    )

    assert plan["execution_ready"] is True
    assert plan["particle_count"] == 3
    assert plan["stack_count"] == 1
    assert plan["copy_mode"] == "copy"
    assert plan["outputs"]["particles_star"] == (
        "External/CryoSPARC/class3d_input/particles.star"
    )
    assert plan["passthrough_order"] == ["J10/J10_passthrough_particles.cs"]
    assert plan["import_job_handoff"]["operation"] == "import_particles"
    assert plan["pyem"]["command_template"][-3:] == [
        "raw-particles.star",
        "--loglevel",
        "INFO",
    ]
    assert "--relion2" not in plan["pyem"]["command_template"]
    assert not (project / "External").exists()


def test_import_environment_reports_live_standalone_and_unavailable_policies(
    conversion_tree,
):
    settings, _, source_project, _ = conversion_tree

    live = cryosparc_import_environment(
        replace(
            settings,
            cryosparc_import_roots=(),
            cryosparc_live_projects=(_live_project_identity("P1", source_project),),
        )
    )
    standalone = cryosparc_import_environment(settings)
    unavailable = cryosparc_import_environment(
        replace(
            settings,
            cryosparc_import_roots=(),
            cryosparc_live_projects=None,
        )
    )

    assert live["ready"] is True
    assert live["approved_source_roots"] == []
    assert live["source_authorization"] == {
        "live_project_inventory_available": True,
        "live_project_count": 1,
        "configured_roots_available": False,
        "policy": "live_cryosparc_project_inventory",
    }
    assert live["configured_source_roots_required_only_for_standalone"] is True
    assert standalone["ready"] is True
    assert standalone["source_authorization"]["policy"] == "configured_roots"
    assert (
        standalone["source_authorization"]["live_project_inventory_available"] is False
    )
    assert unavailable["ready"] is False
    assert unavailable["source_authorization"] == {
        "live_project_inventory_available": False,
        "live_project_count": None,
        "configured_roots_available": False,
        "policy": "unavailable",
    }


def test_render_uses_live_inventory_and_rejects_a_different_project(
    conversion_tree,
):
    settings, project, source_project, _ = conversion_tree
    live_settings = replace(
        settings,
        cryosparc_live_projects=(_live_project_identity("P1", source_project),),
    )

    plan = render_cryosparc_particle_import(
        live_settings,
        str(project),
        str(source_project),
        "J10/J10_particles.cs",
        ["J10/J10_passthrough_particles.cs"],
    )

    assert plan["source_authorization"]["method"] == (
        "live_cryosparc_project_inventory"
    )
    assert plan["source_authorization"]["project_uid"] == "P1"
    assert plan["source_authorization"]["source_project_dir"] == str(
        source_project.resolve()
    )

    different_project = source_project.parent / "P2"
    different_project.mkdir()
    with pytest.raises(ConfigurationError, match="active CryoSPARC instance"):
        render_cryosparc_particle_import(
            live_settings,
            str(project),
            str(different_project),
            "J10/J10_particles.cs",
        )


def test_render_missing_project_requests_explicit_bootstrap(conversion_tree):
    settings, project, source_project, _ = conversion_tree
    missing_project = project.parent / "new-project"

    plan = render_cryosparc_particle_import(
        settings,
        str(missing_project),
        str(source_project),
        "J10/J10_particles.cs",
        ["J10/J10_passthrough_particles.cs"],
    )

    assert plan["bootstrap_required"] is True
    assert plan["execution_ready"] is False
    assert plan["bootstrap"]["tool"] == "relion_initialize_project"
    assert plan["bootstrap"]["arguments"]["project_dir"] == str(missing_project)
    assert not missing_project.exists()


@pytest.mark.parametrize("export_name", [".", ".."])
def test_export_name_rejects_directory_segments(conversion_tree, export_name):
    settings, project, source_project, _ = conversion_tree

    with pytest.raises(CryosparcImportError, match="may not be"):
        render_cryosparc_particle_import(
            settings,
            str(project),
            str(source_project),
            "J10/J10_particles.cs",
            export_name=export_name,
        )


def test_initialized_project_uses_declared_nondefault_pipeline(conversion_tree):
    settings, project, source_project, _ = conversion_tree
    custom_project = project.parent / "custom-pipeline-project"
    _initialize_project(custom_project, pipeline_name="analysis")

    plan = render_cryosparc_particle_import(
        settings,
        str(custom_project),
        str(source_project),
        "J10/J10_particles.cs",
        ["J10/J10_passthrough_particles.cs"],
    )

    assert plan["project_initialized"] is True
    assert plan["bootstrap_required"] is False
    assert plan["execution_ready"] is True
    assert plan["bootstrap"]["arguments"]["pipeline_name"] == "analysis"
    assert plan["bootstrap"]["pipeline_file"] == str(
        custom_project / "analysis_pipeline.star"
    )
    assert plan["import_job_handoff"]["pipeline_name"] == "analysis"


def test_write_refuses_missing_project_before_pyem(conversion_tree, monkeypatch):
    settings, project, source_project, _ = conversion_tree
    missing_project = project.parent / "new-project"
    monkeypatch.setattr(
        "relion_mcp.cryosparc_import.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("pyem must not run")
        ),
    )

    with pytest.raises(CryosparcImportError, match="relion_initialize_project"):
        import_cryosparc_particles(
            settings,
            str(missing_project),
            str(source_project),
            "J10/J10_particles.cs",
            ["J10/J10_passthrough_particles.cs"],
            confirm=True,
        )

    assert not missing_project.exists()


def test_materializes_star_stacks_manifest_and_preserves_sources(conversion_tree):
    settings, project, source_project, _ = conversion_tree
    source_stack = source_project / "J1/extract/particles.mrc"
    source_hash = __import__("hashlib").sha256(source_stack.read_bytes()).hexdigest()

    result = import_cryosparc_particles(
        settings,
        str(project),
        str(source_project),
        "J10/J10_particles.cs",
        ["J10/J10_passthrough_particles.cs"],
        "class3d_input",
        confirm=True,
    )

    bundle = project / "External/CryoSPARC/class3d_input"
    particles_star = bundle / "particles.star"
    manifest_path = bundle / "conversion_manifest.json"
    stacks = list((bundle / "stacks").glob("*.mrcs"))
    assert result["status"] == "materialized"
    assert result["publication"]["published_without_replacement"] is True
    assert len(stacks) == 1
    assert (
        __import__("hashlib").sha256(stacks[0].read_bytes()).hexdigest() == source_hash
    )
    assert (
        __import__("hashlib").sha256(source_stack.read_bytes()).hexdigest()
        == source_hash
    )

    particles = parse_star_block(particles_star, "particles", strict_loops=True)
    assert particles is not None
    assert particles["rlnImageName"] == [
        f"000006@External/CryoSPARC/class3d_input/stacks/{stacks[0].name}",
        f"000003@External/CryoSPARC/class3d_input/stacks/{stacks[0].name}",
        f"000001@External/CryoSPARC/class3d_input/stacks/{stacks[0].name}",
    ]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["particle_count"] == 3
    assert manifest["source_files_modified"] is False
    assert manifest["stacks"][0]["source_sha256"] == source_hash
    assert manifest["stacks"][0]["output_sha256"] == source_hash
    assert manifest["import_job_handoff"]["inputs"]["particles"] == (
        "External/CryoSPARC/class3d_input/particles.star"
    )
    assert not any(
        path.name.startswith(".cryosparc-import-") for path in bundle.parent.iterdir()
    )


def test_copies_micrographs_rewrites_project_relative_paths_and_records_provenance(
    conversion_tree,
):
    settings, project, source_project, fake_pyem = conversion_tree
    micrograph_a_relative = "J1/motioncorrected/micrograph-a.mrc"
    micrograph_b_relative = "J1/motioncorrected/micrograph-b.mrc"
    micrograph_a = source_project / micrograph_a_relative
    micrograph_b = source_project / micrograph_b_relative
    _write_stack(micrograph_a, count=1)
    _write_stack(micrograph_b, count=1)
    passthrough = source_project / "J10/J10_micrographs.cs"
    _write_passthrough(
        passthrough,
        uids=(103, 101, 102),
        micrograph_paths=(
            micrograph_b_relative.encode(),
            micrograph_a_relative.encode(),
            micrograph_a_relative.encode(),
        ),
    )
    _write_fake_pyem(
        fake_pyem,
        micrograph_paths=(
            micrograph_a_relative,
            micrograph_a_relative,
            micrograph_b_relative,
        ),
    )

    plan = render_cryosparc_particle_import(
        settings,
        str(project),
        str(source_project),
        "J10/J10_particles.cs",
        ["J10/J10_micrographs.cs"],
        "with_micrographs",
    )

    assert plan["schema_version"] == "em-claw/cryosparc-particle-import/v2"
    assert plan["micrograph_count"] == 2
    assert plan["micrograph_bytes_to_copy"] == (
        micrograph_a.stat().st_size + micrograph_b.stat().st_size
    )
    assert plan["micrograph_path_policy"] == {
        "source_field": "location/micrograph_path",
        "pyem_column": "rlnMicrographName",
        "mode": "copy_and_rewrite",
        "coverage_required": True,
        "output_paths_project_relative": True,
    }
    assert not (project / "External").exists()

    result = import_cryosparc_particles(
        settings,
        str(project),
        str(source_project),
        "J10/J10_particles.cs",
        ["J10/J10_micrographs.cs"],
        "with_micrographs",
        confirm=True,
    )

    star = project / result["outputs"]["particles_star"]
    particles = parse_star_block(star, "particles", strict_loops=True)
    assert particles is not None
    rewritten = particles["rlnMicrographName"]
    assert len(set(rewritten)) == 2
    assert all(
        value.startswith("External/CryoSPARC/with_micrographs/micrographs/")
        for value in rewritten
    )
    assert all(
        not Path(value).is_absolute() and ".." not in Path(value).parts
        for value in rewritten
    )
    assert all(
        (project / value).is_file() and not (project / value).is_symlink()
        for value in rewritten
    )
    assert result["output_summary"]["micrograph_paths_rewritten"] is True
    assert result["output_summary"]["micrograph_count"] == 2
    assert sorted(result["output_summary"]["micrograph_reference_counts"].values()) == [
        1,
        2,
    ]

    bundle = project / result["outputs"]["bundle"]
    manifest = json.loads(
        (bundle / "conversion_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["schema_version"] == "em-claw/cryosparc-particle-import/v2"
    assert len(manifest["micrographs"]) == 2
    source_hashes = {
        __import__("hashlib").sha256(path.read_bytes()).hexdigest()
        for path in (micrograph_a, micrograph_b)
    }
    assert {
        record["source_sha256"] for record in manifest["micrographs"]
    } == source_hashes
    assert all(
        record["source_sha256"] == record["output_sha256"]
        for record in manifest["micrographs"]
    )
    assert {record["destination"] for record in manifest["micrographs"]} == set(
        rewritten
    )


def test_rejects_incomplete_pyem_micrograph_coverage_without_publication(
    conversion_tree,
):
    settings, project, source_project, fake_pyem = conversion_tree
    micrograph_a_relative = "J1/motioncorrected/micrograph-a.mrc"
    micrograph_b_relative = "J1/motioncorrected/micrograph-b.mrc"
    _write_stack(source_project / micrograph_a_relative, count=1)
    _write_stack(source_project / micrograph_b_relative, count=1)
    passthrough = source_project / "J10/J10_micrographs.cs"
    _write_passthrough(
        passthrough,
        uids=(103, 101, 102),
        micrograph_paths=(
            micrograph_b_relative.encode(),
            micrograph_a_relative.encode(),
            micrograph_a_relative.encode(),
        ),
    )
    _write_fake_pyem(
        fake_pyem,
        micrograph_paths=(
            micrograph_a_relative,
            micrograph_b_relative,
            micrograph_b_relative,
        ),
    )

    with pytest.raises(CryosparcImportError, match="exact particle coverage"):
        import_cryosparc_particles(
            settings,
            str(project),
            str(source_project),
            "J10/J10_particles.cs",
            ["J10/J10_micrographs.cs"],
            "bad_micrograph_coverage",
            confirm=True,
        )

    assert not (project / "External/CryoSPARC/bad_micrograph_coverage").exists()
    external = project / "External/CryoSPARC"
    assert not any(
        path.name.startswith(".cryosparc-import-") for path in external.iterdir()
    )


def test_rejects_changed_pyem_micrograph_assignments_with_equal_coverage(
    conversion_tree,
):
    settings, project, source_project, fake_pyem = conversion_tree
    micrograph_a_relative = "J1/motioncorrected/micrograph-a.mrc"
    micrograph_b_relative = "J1/motioncorrected/micrograph-b.mrc"
    _write_stack(source_project / micrograph_a_relative, count=1)
    _write_stack(source_project / micrograph_b_relative, count=1)
    _write_passthrough(
        source_project / "J10/J10_micrographs.cs",
        uids=(103, 101, 102),
        micrograph_paths=(
            micrograph_b_relative.encode(),
            micrograph_a_relative.encode(),
            micrograph_a_relative.encode(),
        ),
    )
    _write_fake_pyem(
        fake_pyem,
        micrograph_paths=(
            micrograph_a_relative,
            micrograph_b_relative,
            micrograph_a_relative,
        ),
    )

    with pytest.raises(
        CryosparcImportError, match="changed rlnMicrographName assignments"
    ):
        import_cryosparc_particles(
            settings,
            str(project),
            str(source_project),
            "J10/J10_particles.cs",
            ["J10/J10_micrographs.cs"],
            "bad_micrograph_assignments",
            confirm=True,
        )

    assert not (project / "External/CryoSPARC/bad_micrograph_assignments").exists()


@pytest.mark.parametrize(
    "micrograph_path",
    [
        pytest.param(b"../outside.mrc", id="traversal"),
        pytest.param(b"J1/motioncorrected/missing.mrc", id="missing"),
    ],
)
def test_rejects_unsafe_or_missing_micrograph_before_pyem(
    conversion_tree,
    monkeypatch,
    micrograph_path,
):
    settings, project, source_project, _ = conversion_tree
    passthrough = source_project / "J10/J10_bad_micrographs.cs"
    _write_passthrough(
        passthrough,
        micrograph_paths=(micrograph_path,) * 3,
    )
    monkeypatch.setattr(
        "relion_mcp.cryosparc_import.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("pyem must not run")
        ),
    )

    with pytest.raises(CryosparcImportError):
        render_cryosparc_particle_import(
            settings,
            str(project),
            str(source_project),
            "J10/J10_particles.cs",
            ["J10/J10_bad_micrographs.cs"],
            "bad_micrograph_path",
        )


def test_zero_based_cryosparc_groups_map_to_one_based_relion_optics(conversion_tree):
    settings, project, source_project, fake_pyem = conversion_tree
    primary = source_project / "J11/J11_particles.cs"
    _write_primary(
        primary,
        include_ctf=True,
        optics_groups=(0, 1, 1),
    )
    _write_fake_pyem(
        fake_pyem,
        optics_groups=(1, 2),
        particle_groups=(1, 2, 2),
    )

    plan = render_cryosparc_particle_import(
        settings,
        str(project),
        str(source_project),
        "J11/J11_particles.cs",
        export_name="zero_based_optics",
    )
    assert [
        (row["cryosparc_exp_group_id"], row["rlnOpticsGroup"])
        for row in plan["class3d"]["source_optics_groups"]
    ] == [(0, 1), (1, 2)]

    result = import_cryosparc_particles(
        settings,
        str(project),
        str(source_project),
        "J11/J11_particles.cs",
        export_name="zero_based_optics",
        confirm=True,
    )

    assert result["output_summary"]["optics_group_counts"] == {"1": 1, "2": 2}
    output = project / result["outputs"]["particles_star"]
    optics = parse_star_block(output, "optics", strict_loops=True)
    assert optics is not None
    assert optics["rlnOpticsGroup"] == ["1", "2"]


def test_particle_rewrite_streams_rows_without_parsing_particles_block(
    conversion_tree, monkeypatch
):
    settings, project, source_project, _ = conversion_tree
    parsed_blocks: list[str] = []
    duplicate_tracking: list[bool | None] = []
    real_parse_star_block = cryosparc_import_module.parse_star_block
    real_particle_summary = cryosparc_import_module.particle_star_row_summary

    def guarded_parse_star_block(path, block_name, **kwargs):
        parsed_blocks.append(block_name)
        if block_name == "particles":
            raise AssertionError("data_particles must be processed as a stream")
        return real_parse_star_block(path, block_name, **kwargs)

    def guarded_particle_summary(*args, **kwargs):
        duplicate_tracking.append(kwargs.get("track_duplicate_image_names"))
        return real_particle_summary(*args, **kwargs)

    monkeypatch.setattr(
        cryosparc_import_module, "parse_star_block", guarded_parse_star_block
    )
    monkeypatch.setattr(
        cryosparc_import_module,
        "particle_star_row_summary",
        guarded_particle_summary,
    )

    import_cryosparc_particles(
        settings,
        str(project),
        str(source_project),
        "J10/J10_particles.cs",
        ["J10/J10_passthrough_particles.cs"],
        export_name="streamed_particles",
        confirm=True,
    )

    assert parsed_blocks == ["optics", "optics"]
    assert duplicate_tracking == [False, False]


def test_multiple_stacks_with_same_basename_get_distinct_mrcs_names(conversion_tree):
    settings, project, source_project, fake_pyem = conversion_tree
    _write_stack(source_project / "J2/extract/particles.mrc", count=4)
    primary = source_project / "J11/J11_particles.cs"
    _write_primary(
        primary,
        stack_paths=(
            b"J1/extract/particles.mrc",
            b"J2/extract/particles.mrc",
            b"J2/extract/particles.mrc",
        ),
        stack_indices=(0, 1, 3),
        include_ctf=True,
    )
    _write_fake_pyem(
        fake_pyem,
        references=(
            "000001@J1/extract/particles.mrc",
            "000002@J2/extract/particles.mrc",
            "000004@J2/extract/particles.mrc",
        ),
    )

    result = import_cryosparc_particles(
        settings,
        str(project),
        str(source_project),
        "J11/J11_particles.cs",
        export_name="two_stacks",
        confirm=True,
    )

    names = [Path(stack["destination"]).name for stack in result["stacks"]]
    assert len(names) == len(set(names)) == 2
    assert all(name.endswith(".mrcs") for name in names)


@pytest.mark.parametrize(
    ("optics_box", "optics_pixel_size", "expected_output"),
    [
        pytest.param(6, 1.5, "output=(6, 1.5", id="box-size"),
        pytest.param(4, 2.0, "output=(4, 2.0", id="pixel-size"),
    ],
)
def test_pyem_optics_metadata_must_match_referenced_stack(
    conversion_tree,
    optics_box,
    optics_pixel_size,
    expected_output,
):
    settings, project, source_project, fake_pyem = conversion_tree
    _write_fake_pyem(
        fake_pyem,
        optics_box=optics_box,
        optics_pixel_size=optics_pixel_size,
    )

    with pytest.raises(
        CryosparcImportError,
        match="pyem optics metadata does not match the CryoSPARC source",
    ) as error:
        import_cryosparc_particles(
            settings,
            str(project),
            str(source_project),
            "J10/J10_particles.cs",
            ["J10/J10_passthrough_particles.cs"],
            export_name="mismatched_pyem_optics",
            confirm=True,
        )
    assert expected_output in str(error.value)


@pytest.mark.parametrize(
    ("second_box", "second_pixel_size"),
    [
        pytest.param(6, 1.5, id="box-size"),
        pytest.param(4, 2.0, id="pixel-size"),
    ],
)
def test_heterogeneous_stacks_cannot_share_one_source_optics_group(
    conversion_tree,
    monkeypatch,
    second_box,
    second_pixel_size,
):
    settings, project, source_project, _ = conversion_tree
    _write_stack(
        source_project / "J2/extract/particles.mrc",
        count=4,
        box=second_box,
        pixel_size=second_pixel_size,
    )
    primary = source_project / "J11/J11_particles.cs"
    _write_primary(
        primary,
        stack_paths=(
            b"J1/extract/particles.mrc",
            b"J2/extract/particles.mrc",
            b"J2/extract/particles.mrc",
        ),
        stack_indices=(0, 1, 3),
        stack_shapes=((4, 4), (second_box, second_box), (second_box, second_box)),
        pixel_sizes=(1.5, second_pixel_size, second_pixel_size),
        include_ctf=True,
    )
    monkeypatch.setattr(
        "relion_mcp.cryosparc_import.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("pyem must not run")
        ),
    )

    with pytest.raises(
        CryosparcImportError,
        match=r"RELION optics group 1 .* contain inconsistent box, pixel",
    ):
        import_cryosparc_particles(
            settings,
            str(project),
            str(source_project),
            "J11/J11_particles.cs",
            export_name="heterogeneous_source_optics",
            confirm=True,
        )


def test_missing_passthrough_uid_is_rejected_before_pyem(conversion_tree, monkeypatch):
    settings, project, source_project, _ = conversion_tree
    bad = source_project / "J10/bad_passthrough.cs"
    _write_passthrough(bad, uids=(101, 103))
    monkeypatch.setattr(
        "relion_mcp.cryosparc_import.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("pyem must not run")
        ),
    )

    with pytest.raises(CryosparcImportError, match="inner merge would silently drop"):
        import_cryosparc_particles(
            settings,
            str(project),
            str(source_project),
            "J10/J10_particles.cs",
            ["J10/bad_passthrough.cs"],
            export_name="bad_uid",
            confirm=True,
        )


def test_blob_path_traversal_is_rejected(conversion_tree):
    settings, project, source_project, _ = conversion_tree
    primary = source_project / "J12/J12_particles.cs"
    _write_primary(
        primary,
        stack_paths=(b"../outside.mrc",) * 3,
        include_ctf=True,
    )

    with pytest.raises(CryosparcImportError, match="stay beneath source_project_dir"):
        render_cryosparc_particle_import(
            settings,
            str(project),
            str(source_project),
            "J12/J12_particles.cs",
        )


def test_out_of_range_particle_index_is_rejected(conversion_tree):
    settings, project, source_project, _ = conversion_tree
    primary = source_project / "J13/J13_particles.cs"
    _write_primary(primary, stack_indices=(0, 2, 6), include_ctf=True)

    with pytest.raises(CryosparcImportError, match="outside stack"):
        render_cryosparc_particle_import(
            settings,
            str(project),
            str(source_project),
            "J13/J13_particles.cs",
        )


def test_duplicate_stack_image_references_are_rejected(conversion_tree):
    settings, project, source_project, _ = conversion_tree
    primary = source_project / "J14/J14_particles.cs"
    _write_primary(primary, stack_indices=(0, 0, 5), include_ctf=True)

    with pytest.raises(CryosparcImportError, match="duplicate stack image references"):
        render_cryosparc_particle_import(
            settings,
            str(project),
            str(source_project),
            "J14/J14_particles.cs",
        )


def test_mrc_volume_is_rejected_as_particle_stack(conversion_tree):
    settings, project, source_project, _ = conversion_tree
    _write_stack(source_project / "J3/extract/volume.mrc", ispg=1)
    primary = source_project / "J15/J15_particles.cs"
    _write_primary(
        primary,
        stack_paths=(b"J3/extract/volume.mrc",) * 3,
        include_ctf=True,
    )

    with pytest.raises(CryosparcImportError, match=r"MRC ispg=0.*ispg=1"):
        render_cryosparc_particle_import(
            settings,
            str(project),
            str(source_project),
            "J15/J15_particles.cs",
        )


def test_write_gates_run_mode_confirmation_and_optional_source_roots(conversion_tree):
    settings, project, source_project, _ = conversion_tree
    with pytest.raises(ConfigurationError, match="confirm=true"):
        import_cryosparc_particles(
            settings,
            str(project),
            str(source_project),
            "J10/J10_particles.cs",
        )
    with pytest.raises(ConfigurationError, match="Execution is disabled"):
        import_cryosparc_particles(
            replace(settings, mode="plan"),
            str(project),
            str(source_project),
            "J10/J10_particles.cs",
            confirm=True,
        )
    restricted_elsewhere = source_project.parent.parent / "other-cryosparc-root"
    restricted_elsewhere.mkdir()
    with pytest.raises(
        ConfigurationError, match="optional RELION_CRYOSPARC_IMPORT_ROOTS"
    ):
        render_cryosparc_particle_import(
            replace(
                settings,
                cryosparc_import_roots=(restricted_elsewhere.resolve(),),
            ),
            str(project),
            str(source_project),
            "J10/J10_particles.cs",
        )


def test_existing_bundle_is_never_replaced(conversion_tree, monkeypatch):
    settings, project, source_project, _ = conversion_tree
    bundle = project / "External/CryoSPARC/existing"
    bundle.mkdir(parents=True)
    marker = bundle / "keep.txt"
    marker.write_text("keep\n", encoding="utf-8")
    monkeypatch.setattr(
        "relion_mcp.cryosparc_import.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("pyem must not run")
        ),
    )

    with pytest.raises(CryosparcImportError, match="already exists"):
        import_cryosparc_particles(
            settings,
            str(project),
            str(source_project),
            "J10/J10_particles.cs",
            ["J10/J10_passthrough_particles.cs"],
            export_name="existing",
            confirm=True,
        )

    assert marker.read_text(encoding="utf-8") == "keep\n"


def test_publication_falls_back_to_exclusive_reservation_without_overwrite(
    tmp_path: Path,
    monkeypatch,
):
    parent = tmp_path / "publish"
    parent.mkdir()
    source = parent / "staging"
    source.mkdir()
    (source / "payload").write_text("complete\n", encoding="utf-8")
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(
        cryosparc_import_module,
        "_renameat2_noreplace_errno",
        lambda *_args: errno.EOPNOTSUPP,
    )
    try:
        method = cryosparc_import_module._rename_directory_noreplace(
            parent_fd,
            "staging",
            "particles",
        )
        assert method == "exclusive_destination_reservation_then_rename"
        assert (parent / "particles/payload").read_text(encoding="utf-8") == (
            "complete\n"
        )

        second = parent / "second"
        second.mkdir()
        (second / "different").write_text("keep\n", encoding="utf-8")
        with pytest.raises(CryosparcImportError, match="appeared during publication"):
            cryosparc_import_module._rename_directory_noreplace(
                parent_fd,
                "second",
                "particles",
            )
        assert (parent / "particles/payload").read_text(encoding="utf-8") == (
            "complete\n"
        )
        assert (second / "different").read_text(encoding="utf-8") == "keep\n"
    finally:
        os.close(parent_fd)


def test_pyem_child_uses_argv_private_cache_and_no_cryosparc_credentials(
    conversion_tree, monkeypatch
):
    settings, project, source_project, _ = conversion_tree
    observed: dict[str, object] = {}
    real_run = __import__("subprocess").run

    def recording_run(command, **kwargs):
        observed["command"] = command
        observed["environment"] = kwargs["env"]
        observed["shell"] = kwargs.get("shell")
        cache = Path(kwargs["env"]["NUMBA_CACHE_DIR"])
        observed["cache_was_private"] = (
            cache.is_dir() and Path(kwargs["cwd"]) in cache.parents
        )
        return real_run(command, **kwargs)

    monkeypatch.setenv("CRYOSPARC_PASSWORD", "must-not-cross")
    monkeypatch.setenv("EMCLAW_CRYOSPARC_ENV_FILE", "/secret")
    monkeypatch.setattr("relion_mcp.cryosparc_import.subprocess.run", recording_run)

    import_cryosparc_particles(
        settings,
        str(project),
        str(source_project),
        "J10/J10_particles.cs",
        ["J10/J10_passthrough_particles.cs"],
        export_name="environment",
        confirm=True,
    )

    command = observed["command"]
    environment = observed["environment"]
    assert isinstance(command, list)
    assert observed["shell"] is None
    assert command[-3:] == ["raw-particles.star", "--loglevel", "INFO"]
    assert "--relion2" not in command
    assert isinstance(environment, dict)
    assert "CRYOSPARC_PASSWORD" not in environment
    assert "EMCLAW_CRYOSPARC_ENV_FILE" not in environment
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert observed["cache_was_private"] is True
    assert not Path(environment["NUMBA_CACHE_DIR"]).exists()
