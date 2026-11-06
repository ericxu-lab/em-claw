"""Offline contracts for env-bound CryoSPARC project/job discovery."""

from __future__ import annotations

import ast
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from types import ModuleType


ROOT = Path(__file__).resolve().parents[2]
DISCOVERY_SOURCE = ROOT / "profiles" / "cryosparc" / "instance_discovery.py"
MCP_SOURCE = ROOT / "profiles" / "cryosparc" / "cryosparc_mcp.py"


def _load_discovery() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "emclaw_cryosparc_instance_discovery",
        DISCOVERY_SOURCE,
    )
    if specification is None or specification.loader is None:
        raise AssertionError("could not load CryoSPARC discovery module")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


DISCOVERY = _load_discovery()


class _Document:
    def __init__(self, value: dict[str, object]) -> None:
        self.doc = value


class _ProjectDocument(_Document):
    def dir(self) -> Path:
        return Path(str(self.doc["project_dir"]))


class _FakeCryosparc:
    def __init__(
        self,
        project: dict[str, object],
        job: dict[str, object],
        projects: list[dict[str, object]] | None = None,
    ) -> None:
        self.project = project
        self.job = job
        self.projects = projects if projects is not None else [project]
        self.cli = self
        self.calls: list[tuple[str, ...]] = []

    def find_project(self, project_uid: str) -> _Document:
        self.calls.append(("project", project_uid))
        record = next(
            (
                candidate
                for candidate in self.projects
                if candidate.get("uid") == project_uid
            ),
            self.project,
        )
        return _ProjectDocument(record)

    def find_job(self, project_uid: str, job_uid: str) -> _Document:
        self.calls.append(("job", project_uid, job_uid))
        return _Document(self.job)

    def list_projects(self) -> list[dict[str, object]]:
        self.calls.append(("list_projects",))
        return self.projects


class CryosparcInstanceDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_context = tempfile.TemporaryDirectory(
            prefix="emclaw-cryosparc-discovery-",
            dir="/tmp",
        )
        self.addCleanup(self.temporary_context.cleanup)
        self.project = Path(self.temporary_context.name) / "CS-project"
        self.job = self.project / "J218"
        self.job.mkdir(parents=True)
        (self.job / "particles_kept.cs").write_bytes(b"kept")
        (self.job / "passthrough.cs").write_bytes(b"pass")
        (self.job / "events.bson").write_bytes(b"ignored")
        outside = self.project.parent / "outside.cs"
        outside.write_bytes(b"outside")
        (self.job / "linked.cs").symlink_to(outside)
        self.project_document: dict[str, object] = {
            "uid": "P8",
            "title": "configured instance project",
            "project_dir": str(self.project),
            "password": "must-not-leak",
        }
        self.job_document: dict[str, object] = {
            "uid": "J218",
            "project_uid": "P8",
            "job_type": "remove_duplicate_particles",
            "status": "completed",
            "title": "deduplicate",
            "job_dir": "J218",
            "output_results": [
                {
                    "group_name": "particles_kept",
                    "type": "particle.blob",
                    "name": "blob",
                    "num_items": [6129],
                    "metafiles": ["J218/particles_kept.cs"],
                    "passthrough": False,
                    "internal_secret": "must-not-leak",
                }
            ],
        }

    def test_resolves_authoritative_project_job_and_bounded_cs_files(self) -> None:
        client = _FakeCryosparc(self.project_document, self.job_document)

        result = DISCOVERY.inspect_project_job(client, "P8", "J218")

        self.assertEqual(
            client.calls,
            [("project", "P8"), ("job", "P8", "J218")],
        )
        self.assertEqual(result["connection_source"], "active_cryosparc_environment")
        self.assertEqual(result["source_project_dir"], str(self.project.resolve()))
        self.assertEqual(result["project"]["uid"], "P8")
        self.assertEqual(result["job"]["job_type"], "remove_duplicate_particles")
        self.assertEqual(result["job"]["output_groups"][0]["num_items"], [6129])
        self.assertEqual(
            result["job"]["output_groups"][0]["metafiles"],
            ["J218/particles_kept.cs"],
        )
        self.assertEqual(
            result["job"]["cs_files"],
            [
                {"path": "J218/particles_kept.cs", "size_bytes": 4},
                {"path": "J218/passthrough.cs", "size_bytes": 4},
            ],
        )
        encoded = json.dumps(result, sort_keys=True)
        self.assertNotIn("must-not-leak", encoded)
        self.assertNotIn("internal_secret", encoded)

    def test_rejects_malformed_uids_before_querying_instance(self) -> None:
        client = _FakeCryosparc(self.project_document, self.job_document)
        cases = (("8", "J218"), ("P8", "218"), ("P8/../P9", "J218"))
        for project_uid, job_uid in cases:
            with self.subTest(project_uid=project_uid, job_uid=job_uid):
                with self.assertRaises(DISCOVERY.CryosparcDiscoveryError):
                    DISCOVERY.inspect_project_job(client, project_uid, job_uid)
        self.assertEqual(client.calls, [])

    def test_rejects_job_directory_escape(self) -> None:
        escaped_job = dict(self.job_document, job_dir="../outside")
        client = _FakeCryosparc(self.project_document, escaped_job)

        with self.assertRaisesRegex(
            DISCOVERY.CryosparcDiscoveryError,
            "stay beneath project_dir",
        ):
            DISCOVERY.inspect_project_job(client, "P8", "J218")

    def test_prefers_authoritative_sdk_directories_over_document_copies(self) -> None:
        class _SdkValue:
            def __init__(self, document: dict[str, object], directory: Path) -> None:
                self.doc = document
                self._directory = directory

            def dir(self) -> PurePosixPath:
                return PurePosixPath(self._directory)

        class _SdkClient:
            def find_project(self, _project_uid: str) -> _SdkValue:
                stale = dict(
                    self_project_document,
                    project_dir="/stale/project/copy",
                )
                return _SdkValue(stale, self_project)

            def find_job(self, _project_uid: str, _job_uid: str) -> _SdkValue:
                stale = dict(self_job_document, job_dir="../stale-job-copy")
                return _SdkValue(stale, self_job)

        self_project = self.project
        self_job = self.job
        self_project_document = self.project_document
        self_job_document = self.job_document

        result = DISCOVERY.inspect_project_job(_SdkClient(), "P8", "J218")

        self.assertEqual(result["source_project_dir"], str(self.project.resolve()))
        self.assertEqual(result["job"]["job_dir"], "J218")

    def test_returns_bounded_relion_handoff_paths(self) -> None:
        client = _FakeCryosparc(self.project_document, self.job_document)

        result = DISCOVERY.inspect_project_job(client, "P8", "J218")

        handoff = result["relion_handoff"]
        self.assertEqual(handoff["project_uid"], "P8")
        self.assertEqual(handoff["job_uid"], "J218")
        self.assertEqual(handoff["source_project_dir"], str(self.project.resolve()))
        self.assertEqual(
            handoff["allowed_cs_files"],
            ["J218/particles_kept.cs", "J218/passthrough.cs"],
        )
        self.assertFalse(handoff["allowed_cs_files_truncated"])

    def test_lists_canonical_projects_from_authenticated_instance(self) -> None:
        second = self.project.parent / "CS-second"
        second.mkdir()
        client = _FakeCryosparc(
            self.project_document,
            self.job_document,
            projects=[
                {"uid": "P9", "project_dir": str(second)},
                self.project_document,
            ],
        )

        result = DISCOVERY.list_live_projects(client)

        self.assertEqual(
            result,
            [
                {
                    "uid": "P8",
                    "project_dir": str(self.project.resolve()),
                    "device": self.project.stat().st_dev,
                    "inode": self.project.stat().st_ino,
                },
                {
                    "uid": "P9",
                    "project_dir": str(second.resolve()),
                    "device": second.stat().st_dev,
                    "inode": second.stat().st_ino,
                },
            ],
        )
        self.assertEqual(
            client.calls,
            [("list_projects",), ("project", "P9"), ("project", "P8")],
        )

    def test_live_inventory_errors_do_not_echo_remote_details(self) -> None:
        class _FailingCli:
            def list_projects(self) -> None:
                raise RuntimeError("remote-secret-detail")

        class _FailingCryosparc:
            cli = _FailingCli()

        with self.assertRaises(DISCOVERY.CryosparcDiscoveryError) as captured:
            DISCOVERY.list_live_projects(_FailingCryosparc())
        self.assertNotIn("remote-secret-detail", str(captured.exception))

    def test_live_inventory_uses_sdk_directory_and_omits_unmounted_project(
        self,
    ) -> None:
        class _SdkProject:
            def __init__(self, uid: str, directory: Path) -> None:
                self.doc = {"uid": uid, "project_dir": "/stale/document/path"}
                self._directory = directory

            def dir(self) -> Path:
                return self._directory

        class _InventoryClient:
            cli: _InventoryClient

            def __init__(self) -> None:
                self.cli = self

            def list_projects(self) -> list[dict[str, str]]:
                return [
                    {"uid": "P4", "project_dir": "/stale/P4"},
                    {"uid": "P8", "project_dir": "/stale/P8"},
                ]

            def find_project(self, uid: str) -> _SdkProject:
                directory = (
                    self_project.parent / "unmounted-P4"
                    if uid == "P4"
                    else self_project
                )
                return _SdkProject(uid, directory)

        self_project = self.project
        project_stat = self.project.stat()

        result = DISCOVERY.list_live_projects(_InventoryClient())

        self.assertEqual(
            result,
            [
                {
                    "uid": "P8",
                    "project_dir": str(self.project.resolve()),
                    "device": project_stat.st_dev,
                    "inode": project_stat.st_ino,
                }
            ],
        )

    def test_rejects_job_from_another_project(self) -> None:
        wrong_project = dict(self.job_document, project_uid="P9")

        with self.assertRaisesRegex(
            DISCOVERY.CryosparcDiscoveryError,
            "different project",
        ):
            DISCOVERY.inspect_project_job(
                _FakeCryosparc(self.project_document, wrong_project),
                "P8",
                "J218",
            )

    def test_caps_returned_output_groups(self) -> None:
        oversized = dict(
            self.job_document,
            output_results=self.job_document["output_results"]
            * (DISCOVERY._MAX_OUTPUT_GROUPS + 1),
        )

        result = DISCOVERY.inspect_project_job(
            _FakeCryosparc(self.project_document, oversized),
            "P8",
            "J218",
        )

        self.assertEqual(
            len(result["job"]["output_groups"]),
            DISCOVERY._MAX_OUTPUT_GROUPS,
        )
        self.assertTrue(result["job"]["output_groups_truncated"])

    def test_api_error_message_does_not_echo_remote_details(self) -> None:
        class _FailingCryosparc:
            def find_project(self, _project_uid: str) -> None:
                raise RuntimeError("remote-secret-detail")

        with self.assertRaises(DISCOVERY.CryosparcDiscoveryError) as captured:
            DISCOVERY.inspect_project_job(_FailingCryosparc(), "P8", "J218")
        self.assertNotIn("remote-secret-detail", str(captured.exception))

    def test_profile_exposes_and_allows_the_live_discovery_tool(self) -> None:
        tree = ast.parse(MCP_SOURCE.read_text(encoding="utf-8"))
        tool_names = {
            ast.literal_eval(keyword.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Tool"
            for keyword in node.keywords
            if keyword.arg == "name"
        }
        self.assertIn("inspect_project_job", tool_names)
        permission = "mcp__cryosparc__inspect_project_job"
        for profile in ("cryosparc", "combined"):
            settings = json.loads(
                (
                    ROOT
                    / "profiles"
                    / profile
                    / ".claude"
                    / "settings.json"
                ).read_text(encoding="utf-8")
            )
            self.assertIn(permission, settings["permissions"]["allow"])


if __name__ == "__main__":
    unittest.main()
