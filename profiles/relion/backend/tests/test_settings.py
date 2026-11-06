import json
from dataclasses import replace
from pathlib import Path

import pytest

from relion_mcp.settings import (
    ConfigurationError,
    CryosparcProjectIdentity,
    Settings,
)


def settings_with_roots(*roots: Path) -> Settings:
    return Settings(
        mode="plan",
        project_roots=tuple(root.resolve() for root in roots),
        pipeliner_executable="relion_pipeliner",
        image_handler_executable="relion_image_handler",
        pipeline_name="default",
        add_job_timeout_seconds=90,
        utility_timeout_seconds=900,
    )


def live_project_identity(uid: str, project: Path) -> CryosparcProjectIdentity:
    resolved = project.resolve()
    project_stat = resolved.stat()
    return CryosparcProjectIdentity(
        uid=uid,
        project_dir=resolved,
        device=project_stat.st_dev,
        inode=project_stat.st_ino,
    )


def live_project_record(uid: str, project: Path) -> dict[str, str | int]:
    identity = live_project_identity(uid, project)
    return {
        "uid": identity.uid,
        "project_dir": str(identity.project_dir),
        "device": identity.device,
        "inode": identity.inode,
    }


def test_project_access_requires_approved_roots(tmp_path: Path):
    settings = settings_with_roots()
    try:
        settings.validate_project_path(str(tmp_path))
    except ConfigurationError as exc:
        assert "RELION_PROJECT_ROOTS" in str(exc)
    else:
        raise AssertionError("Project access was allowed without approved roots")


def test_existing_symlink_cannot_escape_approved_root(tmp_path: Path):
    root = tmp_path / "approved"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "linked-project"
    link.symlink_to(outside, target_is_directory=True)
    settings = settings_with_roots(root)
    try:
        settings.validate_project_path(str(link), may_create=True)
    except ConfigurationError as exc:
        assert "outside RELION_PROJECT_ROOTS" in str(exc)
    else:
        raise AssertionError("Symlink escape was accepted")


def test_project_subpath_symlink_cannot_escape_project(tmp_path: Path):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / "escaped").symlink_to(outside, target_is_directory=True)
    settings = settings_with_roots(tmp_path)

    with pytest.raises(ConfigurationError, match="outside project_dir"):
        settings.resolve_project_subpath(project, "escaped/run.out")


def test_project_roots_must_be_absolute(monkeypatch):
    monkeypatch.setenv("RELION_PROJECT_ROOTS", "relative/projects")

    with pytest.raises(ConfigurationError, match="must be absolute paths"):
        Settings.from_env()


def test_cryosparc_import_roots_are_canonicalized(monkeypatch, tmp_path: Path):
    root = tmp_path / "cryosparc-projects"
    root.mkdir()
    monkeypatch.setenv("RELION_CRYOSPARC_IMPORT_ROOTS", str(root / ".." / root.name))

    settings = Settings.from_env()

    assert settings.cryosparc_import_roots == (root.resolve(),)


def test_cryosparc_import_roots_must_be_absolute(monkeypatch):
    monkeypatch.setenv("RELION_CRYOSPARC_IMPORT_ROOTS", "relative/projects")

    with pytest.raises(
        ConfigurationError,
        match="RELION_CRYOSPARC_IMPORT_ROOTS entries must be absolute paths",
    ):
        Settings.from_env()


def test_live_cryosparc_project_inventory_is_parsed_and_sorted(
    monkeypatch,
    tmp_path: Path,
):
    project_1 = tmp_path / "P1"
    project_2 = tmp_path / "P2"
    project_1.mkdir()
    project_2.mkdir()
    monkeypatch.setenv(
        "EMCLAW_CRYOSPARC_PROJECT_INVENTORY_JSON",
        json.dumps(
            [
                live_project_record("P2", project_2),
                live_project_record("P1", project_1),
            ]
        ),
    )

    settings = Settings.from_env()

    assert settings.cryosparc_live_projects == (
        live_project_identity("P1", project_1),
        live_project_identity("P2", project_2),
    )


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("not-json", "must be a JSON array"),
        (json.dumps({"uid": "P1"}), "must be a bounded array"),
        (
            json.dumps([{"uid": "P1"}]),
            "contain only uid, project_dir, device, and inode",
        ),
        (
            json.dumps(
                [
                    {
                        "uid": "project-1",
                        "project_dir": "/tmp",
                        "device": 0,
                        "inode": 1,
                    }
                ]
            ),
            "valid project UIDs",
        ),
        (
            json.dumps(
                [
                    {
                        "uid": "P1",
                        "project_dir": 1,
                        "device": 0,
                        "inode": 1,
                    }
                ]
            ),
            "directories must be strings",
        ),
        (
            json.dumps(
                [
                    {
                        "uid": "P1",
                        "project_dir": "relative/project",
                        "device": 0,
                        "inode": 1,
                    }
                ]
            ),
            "must be absolute paths",
        ),
        (
            json.dumps(
                [
                    {
                        "uid": "P1",
                        "project_dir": "/tmp",
                        "device": True,
                        "inode": 1,
                    }
                ]
            ),
            "identities must be non-negative integers",
        ),
        (
            json.dumps(
                [
                    {
                        "uid": "P1",
                        "project_dir": "/tmp",
                        "device": 0,
                        "inode": 0,
                    }
                ]
            ),
            "identities must be non-negative integers",
        ),
    ],
)
def test_live_cryosparc_project_inventory_rejects_invalid_records(
    monkeypatch,
    raw: str,
    message: str,
):
    monkeypatch.setenv("EMCLAW_CRYOSPARC_PROJECT_INVENTORY_JSON", raw)

    with pytest.raises(ConfigurationError, match=message):
        Settings.from_env()


def test_live_cryosparc_project_inventory_enforces_transport_and_count_bounds(
    monkeypatch,
):
    variable = "EMCLAW_CRYOSPARC_PROJECT_INVENTORY_JSON"
    monkeypatch.setenv(variable, "x" * (64 * 1024 + 1))
    with pytest.raises(ConfigurationError, match="transport bound"):
        Settings.from_env()

    monkeypatch.setenv(
        variable,
        json.dumps(
            [
                {"uid": f"P{index + 1}", "project_dir": "/tmp"}
                for index in range(513)
            ]
        ),
    )
    with pytest.raises(ConfigurationError, match="bounded array"):
        Settings.from_env()


def test_live_cryosparc_project_inventory_requires_canonical_real_directories(
    monkeypatch,
    tmp_path: Path,
):
    project = tmp_path / "project"
    project.mkdir()
    project_link = tmp_path / "project-link"
    project_link.symlink_to(project, target_is_directory=True)
    variable = "EMCLAW_CRYOSPARC_PROJECT_INVENTORY_JSON"

    for noncanonical in (project / ".." / project.name, project_link):
        project_stat = project.stat()
        monkeypatch.setenv(
            variable,
            json.dumps(
                [
                    {
                        "uid": "P1",
                        "project_dir": str(noncanonical),
                        "device": project_stat.st_dev,
                        "inode": project_stat.st_ino,
                    }
                ]
            ),
        )
        with pytest.raises(ConfigurationError, match="canonical real directories"):
            Settings.from_env()


def test_live_cryosparc_project_inventory_verifies_directory_identity(
    monkeypatch,
    tmp_path: Path,
):
    project = tmp_path / "P1"
    project.mkdir()
    record = live_project_record("P1", project)
    record["inode"] = int(record["inode"]) + 1
    monkeypatch.setenv(
        "EMCLAW_CRYOSPARC_PROJECT_INVENTORY_JSON",
        json.dumps([record]),
    )

    with pytest.raises(ConfigurationError, match="changed during startup"):
        Settings.from_env()


def test_live_cryosparc_project_inventory_rejects_duplicate_uids_and_directories(
    monkeypatch,
    tmp_path: Path,
):
    project_1 = tmp_path / "P1"
    project_2 = tmp_path / "P2"
    project_1.mkdir()
    project_2.mkdir()
    variable = "EMCLAW_CRYOSPARC_PROJECT_INVENTORY_JSON"

    monkeypatch.setenv(
        variable,
        json.dumps(
            [
                live_project_record("P1", project_1),
                live_project_record("P1", project_2),
            ]
        ),
    )
    with pytest.raises(ConfigurationError, match="UIDs must not be repeated"):
        Settings.from_env()

    monkeypatch.setenv(
        variable,
        json.dumps(
            [
                live_project_record("P1", project_1),
                live_project_record("P2", project_1),
            ]
        ),
    )
    with pytest.raises(ConfigurationError, match="directories must not be repeated"):
        Settings.from_env()


def test_live_inventory_uses_exact_membership_and_precedes_configured_roots(
    tmp_path: Path,
):
    live_project = tmp_path / "live" / "P1"
    configured_root = tmp_path / "configured"
    configured_project = configured_root / "P2"
    nested_live_directory = live_project / "J1"
    nested_live_directory.mkdir(parents=True)
    configured_project.mkdir(parents=True)
    settings = replace(
        settings_with_roots(tmp_path),
        cryosparc_import_roots=(configured_root.resolve(),),
        cryosparc_live_projects=(
            live_project_identity("P1", live_project),
        ),
    )

    assert settings.validate_cryosparc_project_path(str(live_project)) == (
        live_project.resolve()
    )
    for rejected in (configured_project, nested_live_directory):
        with pytest.raises(ConfigurationError, match="active CryoSPARC instance"):
            settings.validate_cryosparc_project_path(str(rejected))


def test_live_inventory_rejects_same_path_real_directory_substitution(tmp_path: Path):
    live_project = tmp_path / "live" / "P1"
    live_project.mkdir(parents=True)
    settings = replace(
        settings_with_roots(tmp_path),
        cryosparc_live_projects=(
            live_project_identity("P1", live_project),
        ),
    )
    original_project = tmp_path / "original-P1"
    live_project.rename(original_project)
    live_project.mkdir()

    with pytest.raises(ConfigurationError, match="active CryoSPARC instance"):
        settings.validate_cryosparc_project_path(str(live_project))


def test_cryosparc_project_access_requires_live_inventory_or_configured_root(
    tmp_path: Path,
):
    source_project = tmp_path / "P1"
    source_project.mkdir()

    with pytest.raises(ConfigurationError, match="live project inventory"):
        settings_with_roots(tmp_path).validate_cryosparc_project_path(
            str(source_project)
        )

    configured = replace(
        settings_with_roots(tmp_path),
        cryosparc_import_roots=(tmp_path.resolve(),),
    )
    assert configured.validate_cryosparc_project_path(str(source_project)) == (
        source_project.resolve()
    )


@pytest.mark.parametrize("name", ["RELION_PYEM_PYTHON", "RELION_CSPARC2STAR"])
def test_pyem_paths_must_be_absolute_when_configured(monkeypatch, name: str):
    monkeypatch.setenv(name, "relative/path")

    with pytest.raises(ConfigurationError, match=rf"{name} must be an absolute path"):
        Settings.from_env()


def test_pyem_paths_can_be_configured(monkeypatch, tmp_path: Path):
    python = tmp_path / "python"
    script = tmp_path / "csparc2star.py"
    monkeypatch.setenv("RELION_PYEM_PYTHON", str(python))
    monkeypatch.setenv("RELION_CSPARC2STAR", str(script))

    settings = Settings.from_env()

    assert settings.pyem_python == str(python)
    assert settings.csparc2star_script == str(script)


def test_topaz_executable_can_be_configured_from_environment(monkeypatch):
    monkeypatch.setenv("RELION_TOPAZ_EXECUTABLE", "/opt/site/bin/topaz")

    assert Settings.from_env().topaz_executable == "/opt/site/bin/topaz"


@pytest.mark.parametrize(
    "scratch_directory",
    ["/scratch", "/node-local/relion-scratch/job-17"],
)
def test_scratch_directory_accepts_operator_approved_cluster_paths(
    monkeypatch, scratch_directory: str
):
    monkeypatch.setenv("RELION_SCRATCH_DIR", scratch_directory)

    assert Settings.from_env().scratch_directory == scratch_directory


@pytest.mark.parametrize("value", [None, ""])
def test_scratch_directory_empty_or_unset_is_disabled(monkeypatch, value: str | None):
    if value is None:
        monkeypatch.delenv("RELION_SCRATCH_DIR", raising=False)
    else:
        monkeypatch.setenv("RELION_SCRATCH_DIR", value)

    assert Settings.from_env().scratch_directory is None


@pytest.mark.parametrize(
    "value",
    [
        "relative/scratch",
        "/",
        "/scratch/../outside",
        "/scratch/./job",
        "/scratch/relion_volatile",
        "/scratch/relion_volatile/",
        "/scratch;touch",
        "/scratch == injected",
        "/scratch/$USER",
        "/scratch path",
        "/scratch`command`",
        "/scratch\nother",
        "/scratch\x7fother",
        " /scratch",
    ],
)
def test_scratch_directory_rejects_unsafe_values(monkeypatch, value: str):
    monkeypatch.setenv("RELION_SCRATCH_DIR", value)

    with pytest.raises(ConfigurationError, match="RELION_SCRATCH_DIR"):
        Settings.from_env()


@pytest.mark.parametrize(
    "name",
    [
        "RELION_ADD_JOB_TIMEOUT",
        "RELION_UTILITY_TIMEOUT",
        "RELION_SYMMETRY_ALIGNMENT_MAX_BOX_PX",
        "RELION_VISUAL_MAX_SIDE_PX",
        "RELION_VISUAL_MAX_PNG_BYTES",
        "RELION_VISUAL_MAX_PANELS",
    ],
)
@pytest.mark.parametrize("value", ["not-an-integer", "0", "-1"])
def test_positive_integer_environment_settings_are_validated(monkeypatch, name: str, value: str):
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigurationError, match=rf"{name} must be a positive integer"):
        Settings.from_env()
