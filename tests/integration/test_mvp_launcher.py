"""Offline tests for the three-route EM-Claw launcher."""

from __future__ import annotations

import json
import os
import pty
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "bin" / "em-claw"
CRYO_BOOTSTRAP = ROOT / "libexec" / "em-claw-cryosparc-mcp"
RELION_BOOTSTRAP = ROOT / "libexec" / "em-claw-relion-mcp"
COMBINED_PROFILE = ROOT / "profiles" / "combined"
CRYO_PROFILE = ROOT / "profiles" / "cryosparc"
RELION_PROFILE = ROOT / "profiles" / "relion"
RELION_BACKEND = RELION_PROFILE / "backend"
RELION_SELECTOR = RELION_PROFILE / "bin" / "relion-mcp-class-select"

FAKE_CLAUDE = r"""
import json
import os
import sys

cryo_names = (
    "CRYOSPARC_LICENSE_ID",
    "CRYOSPARC_MASTER_HOSTNAME",
    "CRYOSPARC_BASE_PORT",
    "CRYOSPARC_EMAIL",
    "CRYOSPARC_PASSWORD",
)
relion_names = (
    "RELION_MCP_REPO",
    "RELION_MCP_MODE",
    "RELION_PROJECT_ROOTS",
    "RELION_SCRATCH_DIR",
)
cryo_projects = os.environ.get("EMCLAW_TEST_CRYO_PROJECTS")
relion_write_target = os.environ.get("EMCLAW_TEST_RELION_WRITE_TARGET")
relion_write_succeeded = None
if relion_write_target is not None:
    try:
        with open(relion_write_target, "w", encoding="utf-8") as handle:
            handle.write("writable")
    except OSError:
        relion_write_succeeded = False
    else:
        relion_write_succeeded = True
record = {
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "display": os.environ.get("DISPLAY"),
    "xauthority": os.environ.get("XAUTHORITY"),
    "cryo_present": [name for name in cryo_names if name in os.environ],
    "relion_present": [name for name in relion_names if name in os.environ],
    "relion_mode": os.environ.get("RELION_MCP_MODE"),
    "relion_repo": os.environ.get("RELION_MCP_REPO"),
    "relion_scratch_dir": os.environ.get("RELION_SCRATCH_DIR"),
    "relion_project_roots": os.environ.get("RELION_PROJECT_ROOTS"),
    "relion_cryosparc_import_roots": os.environ.get("RELION_CRYOSPARC_IMPORT_ROOTS"),
    "relion_pyem_python": os.environ.get("RELION_PYEM_PYTHON"),
    "relion_csparc2star": os.environ.get("RELION_CSPARC2STAR"),
    "relion_pipeliner": os.environ.get("RELION_PIPELINER"),
    "path": os.environ.get("PATH"),
    "relion_write_succeeded": relion_write_succeeded,
    "cryo_license": os.environ.get("CRYOSPARC_LICENSE_ID"),
    "cryo_hostname": os.environ.get("CRYOSPARC_MASTER_HOSTNAME"),
    "cryo_source": os.environ.get("EMCLAW_CRYOSPARC_SOURCE"),
    "cryo_env_file": os.environ.get("EMCLAW_CRYOSPARC_ENV_FILE"),
    "relion_env_file": os.environ.get("EMCLAW_RELION_ENV_FILE"),
    "strict_connector_env": os.environ.get("ENABLE_CLAUDEAI_MCP_SERVERS"),
    "mcp_timeout": os.environ.get("MCP_TIMEOUT"),
    "project_inventory_present": (
        "EMCLAW_CRYOSPARC_PROJECT_INVENTORY_JSON" in os.environ
    ),
    "combined_route_present": "EMCLAW_COMBINED_ROUTE" in os.environ,
    "cryo_projects_overlayed": (
        cryo_projects is not None
        and os.path.isdir("projects")
        and os.path.isdir(cryo_projects)
        and os.path.samefile("projects", cryo_projects)
    ),
}
with open(os.environ["EMCLAW_TEST_RECORD"], "w") as handle:
    json.dump(record, handle, sort_keys=True)
"""

FAKE_UV = r"""
import json
import os
import sys

cryo_names = (
    "CRYOSPARC_LICENSE_ID",
    "CRYOSPARC_MASTER_HOSTNAME",
    "CRYOSPARC_BASE_PORT",
    "CRYOSPARC_EMAIL",
    "CRYOSPARC_PASSWORD",
)
relion_names = (
    "RELION_MCP_REPO",
    "RELION_MCP_MODE",
    "RELION_PROJECT_ROOTS",
    "RELION_SCRATCH_DIR",
)
record = {
    "argv": sys.argv[1:],
    "pythonpath": os.environ.get("PYTHONPATH"),
    "relion_repo": os.environ.get("RELION_MCP_REPO"),
    "class_selector": os.environ.get("RELION_CLASS_SELECTOR"),
    "emclaw_uv_bin": os.environ.get("EMCLAW_UV_BIN"),
    "path": os.environ.get("PATH"),
    "uv_cache": os.environ.get("UV_CACHE_DIR"),
    "dont_write_bytecode": os.environ.get("PYTHONDONTWRITEBYTECODE"),
    "cryo_present": [name for name in cryo_names if name in os.environ],
    "relion_present": [name for name in relion_names if name in os.environ],
    "cryo_license": os.environ.get("CRYOSPARC_LICENSE_ID"),
    "relion_mode": os.environ.get("RELION_MCP_MODE"),
    "relion_pipeliner": os.environ.get("RELION_PIPELINER"),
    "relion_cryosparc_import_roots": os.environ.get("RELION_CRYOSPARC_IMPORT_ROOTS"),
    "relion_pyem_python": os.environ.get("RELION_PYEM_PYTHON"),
    "relion_csparc2star": os.environ.get("RELION_CSPARC2STAR"),
    "cryo_test_canary": os.environ.get("CRYOSPARC_TEST_CANARY"),
    "relion_test_canary": os.environ.get("RELION_TEST_CANARY"),
    "cryo_env_file": os.environ.get("EMCLAW_CRYOSPARC_ENV_FILE"),
    "relion_env_file": os.environ.get("EMCLAW_RELION_ENV_FILE"),
    "project_inventory": os.environ.get(
        "EMCLAW_CRYOSPARC_PROJECT_INVENTORY_JSON"
    ),
    "combined_route_present": "EMCLAW_COMBINED_ROUTE" in os.environ,
}
with open(os.environ["EMCLAW_TEST_RECORD"], "w") as handle:
    json.dump(record, handle, sort_keys=True)
"""


class ProfileLauncherTests(unittest.TestCase):
    GROUP = "F"

    def setUp(self) -> None:
        self.temporary_context = tempfile.TemporaryDirectory(
            prefix="emclaw-mvp-", dir="/tmp"
        )
        self.addCleanup(self.temporary_context.cleanup)
        self.temporary = Path(self.temporary_context.name)
        self.record = self.temporary / "record.json"
        self.home = self.temporary / "home"
        self.home.mkdir()
        self.runtime = self.temporary / "runtime"
        self.cryo_source = self.temporary / "cryosparc-source"
        (self.cryo_source / ".claude" / "agents").mkdir(parents=True)
        (self.cryo_source / ".claude" / "skills").mkdir()
        (self.cryo_source / "projects").mkdir()
        self.fake_claude = self._executable("fake-claude", FAKE_CLAUDE)
        self.cryo_env = self.temporary / "cryosparc.env"
        self.cryo_env.write_text(
            f"EMCLAW_CRYOSPARC_SOURCE={self.cryo_source}\n"
            "CRYOSPARC_LICENSE_ID=cryo-file-license\n"
            "CRYOSPARC_MASTER_HOSTNAME=cryo-file-host\n"
            "CRYOSPARC_BASE_PORT=39000\n"
            "CRYOSPARC_EMAIL=cryo@example.test\n"
            "CRYOSPARC_PASSWORD=cryo-file-password\n",
            encoding="utf-8",
        )
        self.cryo_env.chmod(0o600)
        self.relion_env = self.temporary / "relion.env"
        self.relion_env.write_text(
            "RELION_PROJECT_ROOTS=\n"
            "RELION_SCRATCH_DIR=\n"
            "RELION_CRYOSPARC_IMPORT_ROOTS=\n"
            "RELION_PYEM_PYTHON=/site/pyem-python\n"
            "RELION_CSPARC2STAR=/site/csparc2star.py\n",
            encoding="utf-8",
        )
        self.relion_env.chmod(0o600)

    def _executable(self, name: str, body: str) -> Path:
        path = self.temporary / name
        path.write_text("#!/usr/bin/env python3\n" + body.lstrip(), encoding="utf-8")
        path.chmod(0o700)
        return path

    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.pop("UV_CACHE_DIR", None)
        environment.pop("MCP_TIMEOUT", None)
        environment.pop("EMCLAW_RELION_MCP_PYTHON", None)
        environment.pop("EMCLAW_UV_BIN", None)
        environment.pop("RELION_CLASS_SELECTOR", None)
        environment.pop("RELION_SCRATCH_DIR", None)
        for name in (
            "CRYOSPARC_LICENSE_ID",
            "CRYOSPARC_MASTER_HOSTNAME",
            "CRYOSPARC_BASE_PORT",
            "CRYOSPARC_EMAIL",
            "CRYOSPARC_PASSWORD",
            "RELION_MCP_REPO",
            "RELION_MCP_MODE",
            "RELION_PROJECT_ROOTS",
            "RELION_CRYOSPARC_IMPORT_ROOTS",
            "RELION_PYEM_PYTHON",
            "RELION_CSPARC2STAR",
            "EMCLAW_CRYOSPARC_PROJECT_INVENTORY_JSON",
            "EMCLAW_COMBINED_ROUTE",
        ):
            environment.pop(name, None)
        environment.update(
            {
                "EMCLAW_CLAUDE_BIN": str(self.fake_claude),
                "EMCLAW_CRYOSPARC_SOURCE": str(self.cryo_source),
                "EMCLAW_CRYOSPARC_ENV_FILE": str(self.cryo_env),
                "EMCLAW_RELION_ENV_FILE": str(self.relion_env),
                "EMCLAW_RUNTIME_DIR": str(self.runtime),
                "EMCLAW_TEST_CRYO_PROJECTS": str(self.cryo_source / "projects"),
                "EMCLAW_TEST_RECORD": str(self.record),
                "HOME": str(self.home),
            }
        )
        return environment

    def invoke(
        self,
        *arguments: str,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(LAUNCHER), *arguments],
            check=False,
            capture_output=True,
            encoding="utf-8",
            env=self.environment() if environment is None else environment,
            cwd=self.temporary,
        )

    def invoke_with_terminal_stderr(
        self,
        *arguments: str,
        environment: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        """Run the launcher with stderr on a pseudo-terminal, as an interactive user would."""
        controller, terminal = pty.openpty()
        try:
            result = subprocess.run(
                [str(LAUNCHER), *arguments],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=terminal,
                encoding="utf-8",
                env=self.environment() if environment is None else environment,
                cwd=self.temporary,
            )
        finally:
            os.close(terminal)
        chunks = []
        while True:
            try:
                chunk = os.read(controller, 65536)
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        os.close(controller)
        return result, b"".join(chunks).decode("utf-8", errors="replace")

    def load_record(self) -> dict[str, object]:
        return json.loads(self.record.read_text(encoding="utf-8"))

    def assert_controlled_prefix(
        self, record: dict[str, object], profile: Path
    ) -> list[str]:
        arguments = record["argv"]
        self.assertIsInstance(arguments, list)
        assert isinstance(arguments, list)
        expected = [
            "--settings",
            str(profile / ".claude" / "settings.json"),
            "--strict-mcp-config",
            "--mcp-config",
            str(profile / ".mcp.json"),
        ]
        self.assertEqual(arguments[: len(expected)], expected)
        return arguments

    def test_combine_launch_uses_both_mcps_and_both_environment_families(
        self,
    ) -> None:
        user_arguments = ["--model", "sonnet", "argument with spaces", ""]
        result = self.invoke("combine", "--", *user_arguments)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        record = self.load_record()
        arguments = self.assert_controlled_prefix(record, COMBINED_PROFILE)
        self.assertEqual(
            arguments[5:9],
            [
                "--plugin-dir",
                str(self.cryo_source / ".claude"),
                "--add-dir",
                str(self.cryo_source / "projects"),
            ],
        )
        self.assertEqual(arguments[9], "--append-system-prompt")
        self.assertIn("CryoSPARC", arguments[10])
        self.assertIn("RELION", arguments[10])
        self.assertEqual(arguments[11:], user_arguments)
        self.assertEqual(Path(str(record["cwd"])), COMBINED_PROFILE)
        # The unified Claude process needs both families for cwd-relative
        # CryoSPARC helpers and RELION routing. Each MCP bootstrap scrubs the
        # opposite family before it starts its child process.
        self.assertEqual(
            set(record["cryo_present"]),
            {
                "CRYOSPARC_LICENSE_ID",
                "CRYOSPARC_MASTER_HOSTNAME",
                "CRYOSPARC_BASE_PORT",
                "CRYOSPARC_EMAIL",
                "CRYOSPARC_PASSWORD",
            },
        )
        self.assertEqual(record["cryo_license"], "cryo-file-license")
        self.assertEqual(record["cryo_hostname"], "cryo-file-host")
        self.assertEqual(record["cryo_env_file"], str(self.cryo_env))
        self.assertEqual(record["relion_env_file"], str(self.relion_env))
        self.assertEqual(
            set(record["relion_present"]),
            {
                "RELION_MCP_REPO",
                "RELION_MCP_MODE",
                "RELION_PROJECT_ROOTS",
                "RELION_SCRATCH_DIR",
            },
        )
        self.assertEqual(record["relion_mode"], "run")
        self.assertEqual(
            Path(str(record["relion_repo"])).resolve(strict=True),
            (RELION_PROFILE / "backend").resolve(strict=True),
        )
        self.assertEqual(record["strict_connector_env"], "false")
        self.assertEqual(record["mcp_timeout"], "300000")
        self.assertFalse(record["project_inventory_present"])
        self.assertFalse(record["combined_route_present"])
        self.assertTrue(record["cryo_projects_overlayed"])

    def test_cryosparc_route_loads_only_cryosparc_environment(self) -> None:
        environment = self.environment()
        environment.update(
            {
                "EMCLAW_RELION_ENV_FILE": str(
                    self.temporary / "missing-relion-must-be-ignored.env"
                ),
                "RELION_MCP_MODE": "invalid-route-canary",
                "RELION_PROJECT_ROOTS": "/missing/route-canary",
                "RELION_SCRATCH_DIR": "relative-route-canary",
                "RELION_PIPELINER": "route-canary",
            }
        )

        result = self.invoke("cryosparc", environment=environment)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = self.load_record()
        arguments = self.assert_controlled_prefix(record, CRYO_PROFILE)
        self.assertEqual(
            arguments[5:9],
            [
                "--plugin-dir",
                str(self.cryo_source / ".claude"),
                "--add-dir",
                str(self.cryo_source / "projects"),
            ],
        )
        self.assertEqual(arguments[9], "--append-system-prompt")
        self.assertEqual(len(arguments), 11)
        self.assertEqual(Path(str(record["cwd"])), CRYO_PROFILE)
        self.assertEqual(
            set(record["cryo_present"]),
            {
                "CRYOSPARC_LICENSE_ID",
                "CRYOSPARC_MASTER_HOSTNAME",
                "CRYOSPARC_BASE_PORT",
                "CRYOSPARC_EMAIL",
                "CRYOSPARC_PASSWORD",
            },
        )
        self.assertEqual(record["relion_present"], [])
        self.assertIsNone(record["relion_pipeliner"])
        self.assertIsNone(record["relion_env_file"])
        self.assertFalse(record["project_inventory_present"])
        self.assertFalse(record["combined_route_present"])
        self.assertTrue(record["cryo_projects_overlayed"])

    def test_relion_route_has_no_cryosparc_prerequisite_or_environment(self) -> None:
        environment = self.environment()
        environment.pop("EMCLAW_CRYOSPARC_SOURCE")
        environment["EMCLAW_CRYOSPARC_ENV_FILE"] = str(
            self.temporary / "missing-cryo-must-be-ignored.env"
        )
        environment.update(
            {
                "CRYOSPARC_LICENSE_ID": "opposite-route-canary",
                "CRYOSPARC_MASTER_HOSTNAME": "opposite-route-canary",
                "CRYOSPARC_BASE_PORT": "1",
                "CRYOSPARC_EMAIL": "opposite-route-canary",
                "CRYOSPARC_PASSWORD": "opposite-route-canary",
            }
        )

        result = self.invoke("relion", environment=environment)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = self.load_record()
        arguments = self.assert_controlled_prefix(record, RELION_PROFILE)
        self.assertEqual(arguments[5], "--append-system-prompt")
        self.assertEqual(len(arguments), 7)
        self.assertEqual(Path(str(record["cwd"])), RELION_PROFILE)
        self.assertEqual(record["cryo_present"], [])
        self.assertIsNone(record["cryo_source"])
        self.assertIsNone(record["cryo_env_file"])
        self.assertEqual(record["relion_mode"], "run")
        self.assertEqual(record["relion_env_file"], str(self.relion_env))
        self.assertFalse(record["project_inventory_present"])
        self.assertFalse(record["combined_route_present"])
        self.assertFalse(record["cryo_projects_overlayed"])

    def test_relion_defaults_to_run_and_preserves_explicit_plan_mode(self) -> None:
        for route in ("relion", "combine"):
            with self.subTest(route=route, mode="default"):
                self.record.unlink(missing_ok=True)
                result = self.invoke(route)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.load_record()["relion_mode"], "run")

            with self.subTest(route=route, mode="explicit plan"):
                self.record.unlink(missing_ok=True)
                environment = self.environment()
                environment["RELION_MCP_MODE"] = "plan"
                result = self.invoke(route, environment=environment)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.load_record()["relion_mode"], "plan")

    def test_relion_env_file_is_loaded_but_caller_environment_wins(self) -> None:
        relion_env = self.temporary / "relion-plan.env"
        relion_env.write_text(
            "RELION_MCP_MODE=plan\n"
            "RELION_PROJECT_ROOTS=\n"
            "RELION_SCRATCH_DIR=\n",
            encoding="utf-8",
        )
        relion_env.chmod(0o600)
        environment = self.environment()
        environment["EMCLAW_RELION_ENV_FILE"] = str(relion_env)
        for route in ("relion", "combine"):
            with self.subTest(route=route, source="file"):
                from_file = self.invoke(
                    "--dry-run", route, environment=environment
                )
                self.assertEqual(
                    from_file.returncode, 0, from_file.stdout + from_file.stderr
                )
                self.assertIn("RELION mode: plan\n", from_file.stdout)

            with self.subTest(route=route, source="caller"):
                caller_environment = environment.copy()
                caller_environment["RELION_MCP_MODE"] = "run"
                overridden = self.invoke(
                    "--dry-run", route, environment=caller_environment
                )
                self.assertEqual(
                    overridden.returncode,
                    0,
                    overridden.stdout + overridden.stderr,
                )
                self.assertIn("RELION mode: run\n", overridden.stdout)

    def test_explicit_relion_env_file_must_exist(self) -> None:
        environment = self.environment()
        environment["EMCLAW_RELION_ENV_FILE"] = str(
            self.temporary / "missing-relion.env"
        )
        for route in ("relion", "combine"):
            with self.subTest(route=route):
                result = self.invoke("--dry-run", route, environment=environment)
                self.assertEqual(result.returncode, 2)
                self.assertIn("RELION environment file", result.stderr)
                self.assertFalse(self.record.exists())

    def test_empty_relion_env_file_pointer_disables_optional_file(self) -> None:
        for route in ("relion", "combine"):
            with self.subTest(route=route):
                self.record.unlink(missing_ok=True)
                environment = self.environment()
                environment["EMCLAW_RELION_ENV_FILE"] = ""
                if route == "relion":
                    environment.pop("EMCLAW_CRYOSPARC_SOURCE")
                    environment["EMCLAW_CRYOSPARC_ENV_FILE"] = str(
                        self.temporary / "missing-cryo-must-be-ignored.env"
                    )

                result = self.invoke(route, environment=environment)

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                record = self.load_record()
                self.assertEqual(record["relion_mode"], "run")
                self.assertIn(record["relion_env_file"], (None, ""))
                if route == "relion":
                    self.assertEqual(record["cryo_present"], [])
                    self.assertIsNone(record["cryo_env_file"])

    def test_empty_relion_env_file_pointer_disables_bootstrap_discovery(
        self,
    ) -> None:
        isolated_root = self.temporary / "isolated-relion-bootstrap"
        isolated_bootstrap = isolated_root / "libexec" / RELION_BOOTSTRAP.name
        isolated_bootstrap.parent.mkdir(parents=True)
        shutil.copy2(RELION_BOOTSTRAP, isolated_bootstrap)
        isolated_profile = isolated_root / "profiles" / "relion"
        (isolated_profile / "bin").mkdir(parents=True)
        (isolated_profile / "backend").symlink_to(
            RELION_BACKEND, target_is_directory=True
        )
        (isolated_profile / "bin" / RELION_SELECTOR.name).symlink_to(
            RELION_SELECTOR
        )
        isolated_profile.joinpath(".env").write_text(
            "RELION_PIPELINER=default-file-must-not-load\n",
            encoding="utf-8",
        )

        fake_uv = self._executable("empty-env-fake-uv", FAKE_UV)
        environment = self.environment()
        environment["EMCLAW_RELION_ENV_FILE"] = ""
        environment["EMCLAW_UV_BIN"] = str(fake_uv)
        environment.pop("RELION_PIPELINER", None)

        result = subprocess.run(
            [str(isolated_bootstrap)],
            check=False,
            capture_output=True,
            encoding="utf-8",
            env=environment,
            cwd=self.temporary,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = self.load_record()
        self.assertIsNone(record["relion_pipeliner"])
        self.assertEqual(record["relion_env_file"], "")

    def test_combine_launch_preserves_forwarded_x11_environment(self) -> None:
        environment = self.environment()
        environment["DISPLAY"] = "localhost:10.0"
        environment["XAUTHORITY"] = "/tmp/emclaw-test-xauthority"

        result = self.invoke("combine", environment=environment)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = self.load_record()
        self.assertEqual(record["display"], "localhost:10.0")
        self.assertEqual(record["xauthority"], "/tmp/emclaw-test-xauthority")

    def test_relion_forwards_optional_nonexistent_compute_node_scratch(self) -> None:
        scratch = "/node-local/relion-scratch/session-17"
        environment = self.environment()
        environment["RELION_SCRATCH_DIR"] = scratch

        result = self.invoke("relion", environment=environment)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = self.load_record()
        self.assertIn("RELION_SCRATCH_DIR", record["relion_present"])
        self.assertEqual(record["relion_scratch_dir"], scratch)

    def test_relion_exports_empty_scratch_as_disabled(self) -> None:
        environment = self.environment()
        environment["RELION_SCRATCH_DIR"] = ""

        result = self.invoke("relion", environment=environment)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = self.load_record()
        self.assertIn("RELION_SCRATCH_DIR", record["relion_present"])
        self.assertEqual(record["relion_scratch_dir"], "")

    def test_nonempty_relion_project_root_is_canonicalized_and_writable(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix=".emclaw relion-root-", dir=ROOT
        ) as relion_root_text:
            relion_root = Path(relion_root_text)
            write_target = relion_root / "write-probe"
            environment = self.environment()
            environment["RELION_PROJECT_ROOTS"] = str(relion_root)
            environment["EMCLAW_TEST_RELION_WRITE_TARGET"] = str(write_target)

            result = self.invoke("combine", environment=environment)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            record = self.load_record()
            self.assertEqual(record["relion_project_roots"], str(relion_root))
            arguments = record["argv"]
            self.assertIsInstance(arguments, list)
            assert isinstance(arguments, list)
            added = [
                arguments[index + 1]
                for index, value in enumerate(arguments[:-1])
                if value == "--add-dir"
            ]
            self.assertIn(str(relion_root), added)
            self.assertTrue(record["relion_write_succeeded"])
            self.assertEqual(write_target.read_text(encoding="utf-8"), "writable")

    def test_relion_roots_are_canonical_additional_directories_on_both_routes(
        self,
    ) -> None:
        first = self.temporary / "RELION project one"
        second = self.temporary / "relion-project-two"
        first.mkdir()
        second.mkdir()
        alias = self.temporary / "relion-project-alias"
        alias.symlink_to(first, target_is_directory=True)
        environment = self.environment()
        environment["RELION_PROJECT_ROOTS"] = os.pathsep.join(
            (str(alias), str(second), str(first))
        )

        for route in ("relion", "combine"):
            with self.subTest(route=route):
                self.record.unlink(missing_ok=True)
                result = self.invoke(route, environment=environment)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                arguments = self.load_record()["argv"]
                self.assertIsInstance(arguments, list)
                assert isinstance(arguments, list)
                added = [
                    arguments[index + 1]
                    for index, value in enumerate(arguments[:-1])
                    if value == "--add-dir"
                ]
                expected = [str(first.resolve()), str(second.resolve())]
                if route == "combine":
                    expected.insert(0, str(self.cryo_source / "projects"))
                self.assertEqual(added, expected)

    def test_dry_run_redacts_relion_additional_directory_paths(self) -> None:
        relion_root = self.temporary / "private-relion-project-root"
        relion_root.mkdir()
        environment = self.environment()
        environment["RELION_PROJECT_ROOTS"] = str(relion_root)

        result = self.invoke("--dry-run", "combine", environment=environment)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(str(relion_root), result.stdout + result.stderr)
        self.assertIn("RELION_PROJECT_ROOT", result.stdout)

    def test_relion_root_canonicalized_inside_protected_cryo_source_is_rejected(
        self,
    ) -> None:
        protected_root = self.cryo_source / "protected-secret-canary"
        protected_root.mkdir()
        external_alias = self.temporary / "apparently-external-relion-root"
        external_alias.symlink_to(protected_root, target_is_directory=True)
        environment = self.environment()
        environment["RELION_PROJECT_ROOTS"] = str(external_alias)

        result = self.invoke("combine", environment=environment)

        self.assertEqual(result.returncode, 2)
        output = result.stdout + result.stderr
        self.assertIn("RELION_PROJECT_ROOTS", output)
        self.assertNotIn("secret-canary", output)
        self.assertFalse(self.record.exists())
        self.assertFalse(self.runtime.exists())

    def test_relion_env_file_path_is_private_to_relion_child(self) -> None:
        tools = self.temporary / "relion-private-bin"
        tools.mkdir()
        for name in ("env", "mkdir", "readlink"):
            executable = shutil.which(name)
            self.assertIsNotNone(executable)
            tools.joinpath(name).symlink_to(executable)
        tools.joinpath("python3").symlink_to(sys.executable)

        relion_env = self.temporary / "relion-private.env"
        relion_env.write_text(
            "RELION_PROJECT_ROOTS=\n"
            "RELION_SCRATCH_DIR=\n"
            "RELION_PIPELINER=/relion-private/bin/relion_pipeliner\n"
            "RELION_CRYOSPARC_IMPORT_ROOTS=/site/cryosparc-projects\n"
            "RELION_PYEM_PYTHON=/site/pyem-python\n"
            "RELION_CSPARC2STAR=/site/csparc2star.py\n"
            f"PATH={tools}\n",
            encoding="utf-8",
        )
        relion_env.chmod(0o600)
        environment = self.environment()
        inherited_path = environment["PATH"]
        environment["EMCLAW_RELION_ENV_FILE"] = str(relion_env)

        launched = self.invoke("combine", environment=environment)
        self.assertEqual(
            launched.returncode, 0, launched.stdout + launched.stderr
        )
        parent_record = self.load_record()
        self.assertEqual(parent_record["path"], inherited_path)
        self.assertIsNone(parent_record["relion_pipeliner"])
        self.assertIsNone(parent_record["relion_cryosparc_import_roots"])
        self.assertIsNone(parent_record["relion_pyem_python"])
        self.assertIsNone(parent_record["relion_csparc2star"])

        self.record.unlink()
        fake_uv = self._executable("private-path-fake-uv", FAKE_UV)
        environment["EMCLAW_UV_BIN"] = str(fake_uv)
        relion_child = subprocess.run(
            [str(RELION_BOOTSTRAP)],
            check=False,
            capture_output=True,
            encoding="utf-8",
            env=environment,
            cwd=self.temporary,
        )
        self.assertEqual(
            relion_child.returncode,
            0,
            relion_child.stdout + relion_child.stderr,
        )
        relion_record = self.load_record()
        self.assertEqual(relion_record["path"], str(tools))
        self.assertEqual(
            relion_record["relion_pipeliner"],
            "/relion-private/bin/relion_pipeliner",
        )
        self.assertEqual(
            relion_record["relion_cryosparc_import_roots"],
            "/site/cryosparc-projects",
        )
        self.assertEqual(relion_record["relion_pyem_python"], "/site/pyem-python")
        self.assertEqual(relion_record["relion_csparc2star"], "/site/csparc2star.py")

        self.record.unlink()
        cryo_child = subprocess.run(
            [str(CRYO_BOOTSTRAP)],
            check=False,
            capture_output=True,
            encoding="utf-8",
            env=environment,
            cwd=self.temporary,
        )
        self.assertEqual(
            cryo_child.returncode, 0, cryo_child.stdout + cryo_child.stderr
        )
        cryo_record = self.load_record()
        self.assertEqual(cryo_record["path"], inherited_path)
        self.assertIsNone(cryo_record["relion_pipeliner"])
        self.assertIsNone(cryo_record["relion_cryosparc_import_roots"])
        self.assertIsNone(cryo_record["relion_pyem_python"])
        self.assertIsNone(cryo_record["relion_csparc2star"])
        self.assertIsNone(cryo_record["relion_env_file"])

    def test_dry_run_reports_combined_profile_and_effective_relion_mode(self) -> None:
        cases = (
            ("explicit run", "run", "run"),
            ("explicit plan", "plan", "plan"),
            ("empty defaults to run", "", "run"),
            ("unset defaults to run", None, "run"),
        )
        for label, configured, expected in cases:
            with self.subTest(case=label):
                self.record.unlink(missing_ok=True)
                environment = self.environment()
                if configured is None:
                    environment.pop("RELION_MCP_MODE", None)
                else:
                    environment["RELION_MCP_MODE"] = configured

                result = self.invoke("--dry-run", "combine", environment=environment)

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f"RELION mode: {expected}\n", result.stdout)
                self.assertIn(
                    f"working directory: {COMBINED_PROFILE}\n", result.stdout
                )
                self.assertIn(
                    f"MCP configuration: {COMBINED_PROFILE / '.mcp.json'}\n",
                    result.stdout,
                )
                self.assertIn("CryoSPARC source: read-only", result.stdout)
                self.assertNotIn(
                    "cryo-file-license", result.stdout + result.stderr
                )
                self.assertNotIn(
                    "cryo-file-password", result.stdout + result.stderr
                )
                self.assertFalse(self.record.exists())
                self.assertFalse(self.runtime.exists())

    def test_relion_rejects_invalid_mode_without_execution(self) -> None:
        for configured in ("execute", "RUN", "run\nsecret-canary"):
            with self.subTest(mode=configured):
                self.record.unlink(missing_ok=True)
                environment = self.environment()
                environment["RELION_MCP_MODE"] = configured

                result = self.invoke("--dry-run", "relion", environment=environment)

                self.assertEqual(result.returncode, 2)
                self.assertIn(
                    "RELION_MCP_MODE must be 'plan' or 'run'", result.stderr
                )
                self.assertNotIn("secret-canary", result.stdout + result.stderr)
                self.assertFalse(self.record.exists())

    def test_relion_rejects_unsafe_scratch_without_execution_or_value_leak(self) -> None:
        cases = (
            "relative/secret-canary",
            "/",
            "///./",
            "/scratch/./secret-canary",
            "/scratch/../secret-canary",
            "/scratch path/secret-canary",
            "/scratch;secret-canary",
            "/scratch/$secret-canary",
            "/scratch/`secret-canary`",
            "/scratch == secret-canary",
            "/scratch\nsecret-canary",
            "/scratch/secret-canary ",
            "/scratch/secret-canary/relion_volatile",
        )
        for configured in cases:
            with self.subTest(scratch=configured):
                self.record.unlink(missing_ok=True)
                environment = self.environment()
                environment["RELION_SCRATCH_DIR"] = configured

                result = self.invoke("--dry-run", "relion", environment=environment)

                self.assertEqual(result.returncode, 2)
                output = result.stdout + result.stderr
                self.assertIn("RELION_SCRATCH_DIR", output)
                self.assertNotIn("secret-canary", output)
                self.assertFalse(self.record.exists())

    def test_relion_dry_run_reports_only_scratch_state(self) -> None:
        scratch = "/node-local/relion-scratch/secret-canary"
        environment = self.environment()
        environment["RELION_SCRATCH_DIR"] = scratch

        configured = self.invoke("--dry-run", "relion", environment=environment)

        self.assertEqual(configured.returncode, 0, configured.stdout + configured.stderr)
        self.assertIn("RELION_SCRATCH_DIR: set\n", configured.stdout)
        self.assertNotIn(scratch, configured.stdout + configured.stderr)
        self.assertFalse(self.record.exists())

        environment["RELION_SCRATCH_DIR"] = ""
        disabled = self.invoke("--dry-run", "relion", environment=environment)

        self.assertEqual(disabled.returncode, 0, disabled.stdout + disabled.stderr)
        self.assertIn("RELION_SCRATCH_DIR: unset\n", disabled.stdout)
        self.assertFalse(self.record.exists())

    def test_each_route_accepts_no_claude_arguments(self) -> None:
        for route, profile, expected_length in (
            ("cryosparc", CRYO_PROFILE, 11),
            ("relion", RELION_PROFILE, 7),
            ("combine", COMBINED_PROFILE, 11),
        ):
            with self.subTest(route=route):
                self.record.unlink(missing_ok=True)
                result = self.invoke(route)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                record = self.load_record()
                arguments = self.assert_controlled_prefix(record, profile)
                self.assertEqual(arguments[-2], "--append-system-prompt")
                self.assertEqual(len(arguments), expected_length)
                self.assertNotIn("--system-prompt-snapshot", arguments)

    def test_omitted_route_defaults_to_combine(self) -> None:
        cases = (
            ((), []),
            (("--", "--model", "sonnet"), ["--model", "sonnet"]),
        )
        for arguments, expected_user_arguments in cases:
            with self.subTest(arguments=arguments):
                self.record.unlink(missing_ok=True)
                result = self.invoke(*arguments)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                record = self.load_record()
                claude_arguments = self.assert_controlled_prefix(
                    record, COMBINED_PROFILE
                )
                self.assertEqual(
                    claude_arguments[11:], expected_user_arguments
                )
                self.assertEqual(Path(str(record["cwd"])), COMBINED_PROFILE)
                self.assertTrue(record["cryo_present"])
                self.assertTrue(record["relion_present"])

    def test_dry_run_without_route_previews_combine_without_side_effects(
        self,
    ) -> None:
        result = self.invoke("--dry-run")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("selected route: combine\n", result.stdout)
        self.assertIn(
            f"working directory: {COMBINED_PROFILE}\n", result.stdout
        )
        self.assertIn("MCP servers: cryosparc, relion\n", result.stdout)
        self.assertIn("CryoSPARC environment: configured", result.stdout)
        self.assertIn("RELION environment: configured", result.stdout)
        self.assertFalse(self.record.exists())
        self.assertFalse(self.runtime.exists())

    def test_launcher_finds_claude_in_home_local_bin(self) -> None:
        local_bin = self.home / ".local" / "bin"
        local_bin.mkdir(parents=True)
        home_claude = local_bin / "claude"
        home_claude.symlink_to(self.fake_claude)
        environment = self.environment()
        environment.pop("EMCLAW_CLAUDE_BIN")
        environment["PATH"] = "/usr/bin:/bin"

        dry_run = subprocess.run(
            [str(LAUNCHER), "--dry-run", "combine"],
            check=False,
            capture_output=True,
            encoding="utf-8",
            env=environment,
            cwd=self.temporary,
        )
        self.assertEqual(dry_run.returncode, 0, dry_run.stdout + dry_run.stderr)
        self.assertIn(f"Claude argv: {self.fake_claude}", dry_run.stdout)

        result = subprocess.run(
            [str(LAUNCHER), "combine"],
            check=False,
            capture_output=True,
            encoding="utf-8",
            env=environment,
            cwd=self.temporary,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(Path(str(self.load_record()["cwd"])), COMBINED_PROFILE)

    def test_cryosparc_source_can_be_loaded_from_private_env_file(self) -> None:
        environment = self.environment()
        environment.pop("EMCLAW_CRYOSPARC_SOURCE")
        for route in ("cryosparc", "combine"):
            with self.subTest(route=route):
                result = self.invoke("--dry-run", route, environment=environment)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(
                    f"CryoSPARC source: read-only ({self.cryo_source})\n",
                    result.stdout,
                )

    def test_explicit_cryosparc_source_overrides_env_file_value(self) -> None:
        environment = self.environment()
        env_file = self.temporary / "cryosparc-other-source.env"
        env_file.write_text(
            "EMCLAW_CRYOSPARC_SOURCE=/missing/file-source-must-not-win\n"
            "CRYOSPARC_LICENSE_ID=cryo-file-license\n"
            "CRYOSPARC_MASTER_HOSTNAME=cryo-file-host\n"
            "CRYOSPARC_BASE_PORT=39000\n"
            "CRYOSPARC_EMAIL=cryo@example.test\n"
            "CRYOSPARC_PASSWORD=cryo-file-password\n",
            encoding="utf-8",
        )
        env_file.chmod(0o600)
        environment["EMCLAW_CRYOSPARC_ENV_FILE"] = str(env_file)

        for route in ("cryosparc", "combine"):
            with self.subTest(route=route):
                result = self.invoke("--dry-run", route, environment=environment)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(
                    f"CryoSPARC source: read-only ({self.cryo_source})\n",
                    result.stdout,
                )
                self.assertNotIn(
                    "file-source-must-not-win", result.stdout + result.stderr
                )

    def test_cryosparc_source_is_required_in_environment_or_env_file(self) -> None:
        environment = self.environment()
        environment.pop("EMCLAW_CRYOSPARC_SOURCE")
        no_source_env = self.temporary / "cryosparc-no-source.env"
        no_source_env.write_text(
            "CRYOSPARC_LICENSE_ID=cryo-file-license\n"
            "CRYOSPARC_MASTER_HOSTNAME=cryo-file-host\n"
            "CRYOSPARC_BASE_PORT=39000\n"
            "CRYOSPARC_EMAIL=cryo@example.test\n"
            "CRYOSPARC_PASSWORD=cryo-file-password\n",
            encoding="utf-8",
        )
        no_source_env.chmod(0o600)
        environment["EMCLAW_CRYOSPARC_ENV_FILE"] = str(no_source_env)
        for route in ("cryosparc", "combine"):
            with self.subTest(route=route):
                result = self.invoke("--dry-run", route, environment=environment)
                self.assertEqual(result.returncode, 2)
                self.assertIn("EMCLAW_CRYOSPARC_SOURCE", result.stderr)

    def test_explicit_cryosparc_env_file_must_exist(self) -> None:
        environment = self.environment()
        environment["EMCLAW_CRYOSPARC_ENV_FILE"] = str(
            self.temporary / "missing-cryosparc.env"
        )
        for route in ("cryosparc", "combine"):
            with self.subTest(route=route):
                result = self.invoke("--dry-run", route, environment=environment)
                self.assertEqual(result.returncode, 2)
                self.assertIn("CryoSPARC environment file", result.stderr)
                self.assertFalse(self.record.exists())

    def test_relative_runtime_paths_are_rejected_without_writes(self) -> None:
        environment = self.environment()
        environment["EMCLAW_RUNTIME_DIR"] = "relative-runtime"
        result = subprocess.run(
            [str(LAUNCHER), "combine"],
            check=False,
            capture_output=True,
            encoding="utf-8",
            env=environment,
            cwd=self.temporary,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("EMCLAW_RUNTIME_DIR must be an absolute path", result.stderr)
        self.assertFalse((self.temporary / "relative-runtime").exists())

    def test_combined_profile_declares_both_backend_servers(self) -> None:
        value = json.loads(
            (COMBINED_PROFILE / ".mcp.json").read_text(encoding="utf-8")
        )
        self.assertEqual(set(value), {"mcpServers"})
        self.assertEqual(set(value["mcpServers"]), {"cryosparc", "relion"})
        for backend, bootstrap in (
            ("cryosparc", CRYO_BOOTSTRAP),
            ("relion", RELION_BOOTSTRAP),
        ):
            with self.subTest(backend=backend):
                server = value["mcpServers"][backend]
                command = Path(server["command"])
                self.assertFalse(command.is_absolute())
                self.assertEqual(
                    (COMBINED_PROFILE / command).resolve(strict=True), bootstrap
                )
                self.assertEqual(server["args"], [])
                self.assertEqual(server["type"], "stdio")

        cryo_environment = value["mcpServers"]["cryosparc"]["env"]
        relion_environment = value["mcpServers"]["relion"]["env"]
        self.assertEqual(
            cryo_environment["EMCLAW_CRYOSPARC_ENV_FILE"],
            "${EMCLAW_CRYOSPARC_ENV_FILE}",
        )
        self.assertNotIn("EMCLAW_RELION_ENV_FILE", cryo_environment)
        self.assertEqual(
            relion_environment["EMCLAW_RELION_ENV_FILE"],
            "${EMCLAW_RELION_ENV_FILE}",
        )
        self.assertEqual(
            relion_environment["EMCLAW_CRYOSPARC_ENV_FILE"],
            "${EMCLAW_CRYOSPARC_ENV_FILE}",
        )
        self.assertEqual(relion_environment["EMCLAW_COMBINED_ROUTE"], "1")
        self.assertEqual(
            relion_environment["RELION_MCP_MODE"], "${RELION_MCP_MODE:-run}"
        )

        settings = json.loads(
            (COMBINED_PROFILE / ".claude" / "settings.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            set(settings["enabledMcpjsonServers"]), {"cryosparc", "relion"}
        )
        self.assertFalse(settings["enableAllProjectMcpServers"])
        self.assertEqual(settings["permissions"]["defaultMode"], "auto")
        self.assertIn(
            "mcp__cryosparc__get_connection_template",
            settings["permissions"]["deny"],
        )
        self.assertIn(
            "mcp__relion__relion_abort_job", settings["permissions"]["deny"]
        )
        self.assertIn(
            "mcp__relion__relion_render_cryosparc_particle_import",
            settings["permissions"]["allow"],
        )
        self.assertIn(
            "mcp__relion__relion_import_cryosparc_particles",
            settings["permissions"]["allow"],
        )

    def test_single_backend_profiles_declare_only_the_selected_server(self) -> None:
        for backend, profile, bootstrap in (
            ("cryosparc", CRYO_PROFILE, CRYO_BOOTSTRAP),
            ("relion", RELION_PROFILE, RELION_BOOTSTRAP),
        ):
            with self.subTest(backend=backend):
                config = json.loads(
                    (profile / ".mcp.json").read_text(encoding="utf-8")
                )
                self.assertEqual(set(config), {"mcpServers"})
                self.assertEqual(set(config["mcpServers"]), {backend})
                server = config["mcpServers"][backend]
                command = Path(server["command"])
                self.assertFalse(command.is_absolute())
                self.assertEqual((profile / command).resolve(strict=True), bootstrap)
                settings = json.loads(
                    (profile / ".claude" / "settings.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(settings["enabledMcpjsonServers"], [backend])
                self.assertFalse(settings["enableAllProjectMcpServers"])

        relion_environment = json.loads(
            (RELION_PROFILE / ".mcp.json").read_text(encoding="utf-8")
        )["mcpServers"]["relion"]["env"]
        self.assertEqual(
            relion_environment["RELION_MCP_MODE"], "${RELION_MCP_MODE:-run}"
        )

    def test_launcher_resume_forms_select_profile_and_preserve_environment_isolation(
        self,
    ) -> None:
        cases = (
            (
                "default combined picker",
                ("--resume",),
                COMBINED_PROFILE,
                ["--resume"],
                True,
                True,
            ),
            (
                "short option with value",
                ("-r", "combined-session"),
                COMBINED_PROFILE,
                ["--resume", "combined-session"],
                True,
                True,
            ),
            (
                "route before resume",
                ("cryosparc", "--resume", "cryo-session"),
                CRYO_PROFILE,
                ["--resume", "cryo-session"],
                True,
                False,
            ),
            (
                "route before picker",
                ("relion", "-r"),
                RELION_PROFILE,
                ["--resume"],
                False,
                True,
            ),
            (
                "route after resume value",
                ("--resume", "relion-session", "relion"),
                RELION_PROFILE,
                ["--resume", "relion-session"],
                False,
                True,
            ),
            (
                "route after equals form",
                ("--resume=cryo-equals-session", "cryosparc"),
                CRYO_PROFILE,
                ["--resume", "cryo-equals-session"],
                True,
                False,
            ),
            (
                "route-word search after explicit route",
                ("combine", "--resume", "relion"),
                COMBINED_PROFILE,
                ["--resume", "relion"],
                True,
                True,
            ),
            (
                "attached short option",
                ("relion", "-rrelion-session"),
                RELION_PROFILE,
                ["--resume", "relion-session"],
                False,
                True,
            ),
        )
        for (
            label,
            launcher_arguments,
            expected_profile,
            expected_resume,
            expect_cryo,
            expect_relion,
        ) in cases:
            with self.subTest(case=label):
                self.record.unlink(missing_ok=True)
                result = self.invoke(*launcher_arguments)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                record = self.load_record()
                claude_arguments = self.assert_controlled_prefix(
                    record, expected_profile
                )
                resume_index = len(claude_arguments) - len(expected_resume)
                self.assertEqual(
                    claude_arguments[resume_index - 2 : resume_index],
                    ["--system-prompt-snapshot", "off"],
                )
                self.assertEqual(
                    claude_arguments[-len(expected_resume) :], expected_resume
                )
                self.assertEqual(Path(str(record["cwd"])), expected_profile)
                self.assertEqual(bool(record["cryo_present"]), expect_cryo)
                self.assertEqual(bool(record["relion_present"]), expect_relion)
                self.assertEqual(record["cryo_env_file"] is not None, expect_cryo)
                self.assertEqual(
                    record["relion_env_file"] is not None, expect_relion
                )

    def test_resume_dry_run_selects_route_without_side_effects(self) -> None:
        cases = (
            (
                ("--dry-run", "--resume"),
                COMBINED_PROFILE,
                "combine",
                True,
                True,
            ),
            (
                ("relion", "--resume", "relion-session", "--dry-run"),
                RELION_PROFILE,
                "relion",
                False,
                True,
            ),
            (
                ("--resume=cryo-session", "--dry-run", "cryosparc"),
                CRYO_PROFILE,
                "cryosparc",
                True,
                False,
            ),
        )
        for arguments, profile, route, expect_cryo, expect_relion in cases:
            with self.subTest(arguments=arguments):
                self.record.unlink(missing_ok=True)
                result = self.invoke(*arguments)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f"selected route: {route}\n", result.stdout)
                self.assertIn(f"working directory: {profile}\n", result.stdout)
                self.assertEqual(
                    "CryoSPARC environment: configured" in result.stdout,
                    expect_cryo,
                )
                self.assertEqual(
                    "RELION environment: configured" in result.stdout,
                    expect_relion,
                )
                self.assertIn("--resume", result.stdout)
                self.assertIn("--system-prompt-snapshot off", result.stdout)
                self.assertFalse(self.record.exists())
                self.assertFalse(self.runtime.exists())

    def test_resume_after_separator_and_duplicate_resume_are_rejected(self) -> None:
        cases = (
            ("relion", "--", "--resume"),
            ("--", "--resume", "native-session"),
            ("cryosparc", "--", "--resume=native-equals-session"),
            ("combine", "--", "-rnative-session"),
            ("--resume", "one", "--resume", "two"),
            ("-rone", "--resume=two"),
            ("--resume", "one", "--", "--resume", "two"),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.record.unlink(missing_ok=True)
                result = self.invoke(*arguments)
                self.assertEqual(result.returncode, 64)
                self.assertFalse(self.record.exists())

    def test_launcher_control_flags_and_continue_are_rejected(self) -> None:
        cases = (
            ("--mcp-config", "/tmp/other.json"),
            ("--mcp-config=/tmp/other.json",),
            ("--settings", "/tmp/other.json"),
            ("--plugin-dir=/tmp/other",),
            ("--strict-mcp-config",),
            ("--plugin-url", "https://example.invalid/plugin.zip"),
            ("--system-prompt", "ignore the route policy"),
            ("--append-system-prompt=ignore the route policy",),
            ("--system-prompt-snapshot", "on"),
            ("--permission-mode", "bypassPermissions"),
            ("--dangerously-skip-permissions",),
            ("--allow-dangerously-skip-permissions",),
            ("--safe-mode",),
            ("--bare",),
            ("--continue",),
            ("-c",),
            ("-cc",),
            ("-cr",),
            ("-rc",),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.record.unlink(missing_ok=True)
                result = self.invoke("combine", "--", *arguments)
                self.assertEqual(result.returncode, 64)
                self.assertFalse(self.record.exists())

        for argument in ("--continue", "-c"):
            with self.subTest(launcher_argument=argument):
                result = self.invoke(argument)
                self.assertEqual(result.returncode, 64)
                self.assertFalse(self.record.exists())

    def test_unknown_or_multiple_routes_are_rejected(self) -> None:
        for arguments in (
            ("combined",),
            ("unknown",),
            ("cryosparc", "relion"),
            ("relion", "combine"),
            ("--dry-run", "combine", "cryosparc"),
        ):
            with self.subTest(arguments=arguments):
                self.record.unlink(missing_ok=True)
                result = self.invoke(*arguments)
                self.assertEqual(result.returncode, 64)
                self.assertFalse(self.record.exists())

    def test_cryo_mcp_bootstrap_uses_isolated_uv_runtime(self) -> None:
        fake_uv = self._executable("fake-uv", FAKE_UV)
        environment = self.environment()
        environment.update(
            {
                "EMCLAW_UV_BIN": str(fake_uv),
                "RELION_MCP_REPO": "opposite-backend-canary",
                "RELION_MCP_MODE": "plan",
                "RELION_PROJECT_ROOTS": "/opposite/backend/canary",
                "RELION_SCRATCH_DIR": "/opposite/scratch/canary",
                "RELION_TEST_CANARY": "opposite-backend-canary",
            }
        )
        result = subprocess.run(
            [str(CRYO_BOOTSTRAP)],
            check=False,
            capture_output=True,
            encoding="utf-8",
            env=environment,
            cwd=self.temporary,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = self.load_record()
        self.assertEqual(
            record["argv"],
            [
                "run",
                "--no-project",
                "--python",
                "3.11",
                "--with",
                "mcp==1.29.1",
                "--with",
                "cryosparc-tools==4.7.1",
                "--with",
                "numpy==1.26.4",
                "python",
                str(CRYO_PROFILE / "cryosparc_mcp.py"),
            ],
        )
        self.assertEqual(record["uv_cache"], str(self.runtime / "uv-cache"))
        self.assertEqual(record["dont_write_bytecode"], "1")
        self.assertEqual(
            set(record["cryo_present"]),
            {
                "CRYOSPARC_LICENSE_ID",
                "CRYOSPARC_MASTER_HOSTNAME",
                "CRYOSPARC_BASE_PORT",
                "CRYOSPARC_EMAIL",
                "CRYOSPARC_PASSWORD",
            },
        )
        self.assertEqual(record["cryo_license"], "cryo-file-license")
        self.assertEqual(record["relion_present"], [])
        self.assertIsNone(record["relion_test_canary"])
        self.assertIsNone(record["relion_env_file"])
        self.assertEqual(record["cryo_env_file"], str(self.cryo_env))
        self.assertIsNone(record["project_inventory"])
        self.assertFalse(record["combined_route_present"])

    def test_cryo_mcp_bootstrap_lists_live_projects_with_same_environment(self) -> None:
        fake_uv = self._executable("fake-inventory-uv", FAKE_UV)
        environment = self.environment()
        environment["EMCLAW_UV_BIN"] = str(fake_uv)

        result = subprocess.run(
            [str(CRYO_BOOTSTRAP), "--list-projects"],
            check=False,
            capture_output=True,
            encoding="utf-8",
            env=environment,
            cwd=self.temporary,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = self.load_record()
        self.assertEqual(
            record["argv"][-3:],
            [
                "python",
                str(CRYO_PROFILE / "instance_discovery.py"),
                "--list-projects",
            ],
        )
        self.assertEqual(
            set(record["cryo_present"]),
            {
                "CRYOSPARC_LICENSE_ID",
                "CRYOSPARC_MASTER_HOSTNAME",
                "CRYOSPARC_BASE_PORT",
                "CRYOSPARC_EMAIL",
                "CRYOSPARC_PASSWORD",
            },
        )

    def test_relion_mcp_bootstrap_uses_vendored_backend_by_default(self) -> None:
        fake_uv = self._executable("fake-uv", FAKE_UV)
        environment = self.environment()
        environment.pop("RELION_MCP_REPO", None)
        environment["EMCLAW_UV_BIN"] = str(fake_uv)
        environment["RELION_CLASS_SELECTOR"] = "relion-mcp-class-select"
        environment.update(
            {
                "CRYOSPARC_LICENSE_ID": "opposite-backend-canary",
                "CRYOSPARC_MASTER_HOSTNAME": "opposite-backend-canary",
                "CRYOSPARC_BASE_PORT": "1",
                "CRYOSPARC_EMAIL": "opposite-backend-canary",
                "CRYOSPARC_PASSWORD": "opposite-backend-canary",
                "CRYOSPARC_TEST_CANARY": "opposite-backend-canary",
            }
        )
        inherited_path = environment["PATH"]
        result = subprocess.run(
            [str(RELION_BOOTSTRAP)],
            check=False,
            capture_output=True,
            encoding="utf-8",
            env=environment,
            cwd=self.temporary,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = self.load_record()
        self.assertEqual(
            record["argv"],
            [
                "run",
                "--no-project",
                "--python",
                "3.11",
                "--with",
                "mcp[cli]==2.0.0",
                "--with",
                "PyYAML==6.0.2",
                "--with",
                "numpy==1.26.4",
                "--with",
                "matplotlib==3.8.4",
                "--with",
                "contourpy==1.2.1",
                "--with",
                "mrcfile==1.5.4",
                "--with",
                "Pillow==10.4.0",
                "--with",
                "scipy==1.11.4",
                "python",
                "-m",
                "relion_mcp.server",
            ],
        )
        self.assertEqual(record["uv_cache"], str(self.runtime / "uv-cache"))
        self.assertEqual(record["dont_write_bytecode"], "1")
        self.assertEqual(Path(record["relion_repo"]), RELION_BACKEND.resolve())
        self.assertEqual(Path(record["pythonpath"]), RELION_BACKEND.resolve() / "src")
        self.assertEqual(Path(record["class_selector"]), RELION_SELECTOR.resolve())
        self.assertEqual(Path(record["emclaw_uv_bin"]), fake_uv.resolve())
        self.assertEqual(record["path"], inherited_path)
        self.assertEqual(record["cryo_present"], [])
        self.assertIsNone(record["cryo_test_canary"])
        self.assertIsNone(record["cryo_env_file"])
        self.assertEqual(record["relion_env_file"], str(self.relion_env))
        self.assertIn("RELION_MCP_REPO", record["relion_present"])
        self.assertIn("RELION_PROJECT_ROOTS", record["relion_present"])
        self.assertIn("RELION_SCRATCH_DIR", record["relion_present"])
        self.assertEqual(record["relion_cryosparc_import_roots"], "")
        self.assertEqual(record["relion_pyem_python"], "/site/pyem-python")
        self.assertEqual(record["relion_csparc2star"], "/site/csparc2star.py")
        self.assertIsNone(record["project_inventory"])
        self.assertFalse(record["combined_route_present"])

    def test_combined_relion_bootstrap_pins_private_live_project_inventory(
        self,
    ) -> None:
        isolated_root = self.temporary / "combined-bootstrap"
        isolated_libexec = isolated_root / "libexec"
        isolated_libexec.mkdir(parents=True)
        isolated_relion = isolated_libexec / RELION_BOOTSTRAP.name
        shutil.copy2(RELION_BOOTSTRAP, isolated_relion)

        live_project = self.temporary / "live-cryosparc-project"
        live_project.mkdir()
        live_project_stat = live_project.stat()
        inventory = [
            {
                "uid": "P8",
                "project_dir": str(live_project),
                "device": live_project_stat.st_dev,
                "inode": live_project_stat.st_ino,
            }
        ]
        isolated_cryo = isolated_libexec / CRYO_BOOTSTRAP.name
        isolated_cryo.write_text(
            "#!/usr/bin/env python3\n"
            f"print({json.dumps(json.dumps(inventory, separators=(',', ':')))})\n",
            encoding="utf-8",
        )
        isolated_cryo.chmod(0o700)

        fake_uv = self._executable("combined-inventory-uv", FAKE_UV)
        custom_selector = self._executable(
            "combined-inventory-selector", "raise SystemExit(0)\n"
        )
        environment = self.environment()
        environment.update(
            {
                "EMCLAW_COMBINED_ROUTE": "1",
                "EMCLAW_CRYOSPARC_PROJECT_INVENTORY_JSON": "caller-forgery",
                "EMCLAW_RELION_ENV_FILE": "",
                "EMCLAW_UV_BIN": str(fake_uv),
                "RELION_CLASS_SELECTOR": str(custom_selector),
                "RELION_MCP_REPO": str(RELION_BACKEND),
            }
        )

        result = subprocess.run(
            [str(isolated_relion)],
            check=False,
            capture_output=True,
            encoding="utf-8",
            env=environment,
            cwd=self.temporary,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = self.load_record()
        self.assertEqual(json.loads(record["project_inventory"]), inventory)
        self.assertFalse(record["combined_route_present"])
        self.assertEqual(record["cryo_present"], [])

    def test_relion_mcp_bootstrap_preserves_explicit_class_selector(self) -> None:
        fake_uv = self._executable("fake-uv", FAKE_UV)
        custom_selector = self._executable("custom-selector", "raise SystemExit(0)\n")
        isolated_bootstrap = (
            self.temporary / "isolated" / "libexec" / RELION_BOOTSTRAP.name
        )
        isolated_bootstrap.parent.mkdir(parents=True)
        shutil.copy2(RELION_BOOTSTRAP, isolated_bootstrap)
        environment = self.environment()
        environment["RELION_MCP_REPO"] = str(RELION_BACKEND)
        environment["EMCLAW_UV_BIN"] = str(fake_uv)
        environment["RELION_CLASS_SELECTOR"] = str(custom_selector)
        inherited_path = environment["PATH"]

        result = subprocess.run(
            [str(isolated_bootstrap)],
            check=False,
            capture_output=True,
            encoding="utf-8",
            env=environment,
            cwd=self.temporary,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = self.load_record()
        self.assertEqual(Path(record["class_selector"]), custom_selector)
        self.assertEqual(record["path"], inherited_path)

    def test_relion_class_selector_ignores_project_python_modules(self) -> None:
        self.assertTrue(RELION_SELECTOR.is_file())
        self.assertTrue(os.access(RELION_SELECTOR, os.X_OK))
        self.assertGreaterEqual(sys.version_info, (3, 11))

        project = self.temporary / "relion-project"
        particles = project / "Class2D" / "job001" / "particles.star"
        particles.parent.mkdir(parents=True)
        particles.write_text(
            """data_particles

loop_
_rlnImageName #1
_rlnClassNumber #2
000001@particles.mrcs 1
000002@particles.mrcs 2
000003@particles.mrcs 3
000004@particles.mrcs 1

""",
            encoding="utf-8",
        )
        output = project / "External" / "job001"
        output.mkdir(parents=True)

        shadow_package = project / "relion_mcp"
        shadow_package.mkdir()
        shadow_package.joinpath("__init__.py").write_text(
            "raise RuntimeError('project-local relion_mcp was imported')\n",
            encoding="utf-8",
        )
        unsafe_marker = project / "unsafe-sitecustomize-imported"
        project.joinpath("sitecustomize.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(unsafe_marker)!r}).write_text('unsafe', encoding='utf-8')\n",
            encoding="utf-8",
        )

        environment = self.environment()
        environment["EMCLAW_RELION_MCP_PYTHON"] = sys.executable
        environment["RELION_MCP_REPO"] = str(RELION_BACKEND)
        result = subprocess.run(
            [
                str(RELION_SELECTOR),
                "--o",
                "External/job001/",
                "--in_parts",
                "Class2D/job001/particles.star",
                "--classes",
                "1,3",
                "--expected-particles",
                "3",
            ],
            check=False,
            capture_output=True,
            encoding="utf-8",
            env=environment,
            cwd=project,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["selected_particle_count"], 3)
        self.assertFalse(unsafe_marker.exists())
        self.assertTrue((output / "particles.star").is_file())
        self.assertTrue((output / "selection_provenance.json").is_file())
        self.assertTrue((output / "RELION_OUTPUT_NODES.star").is_file())
        self.assertTrue((output / "RELION_JOB_EXIT_SUCCESS").is_file())
        self.assertFalse((output / "RELION_JOB_EXIT_FAILURE").exists())

    def test_relion_class_selector_uv_fallback_uses_safe_path(self) -> None:
        tools = self.temporary / "selector-tools"
        tools.mkdir()
        for name in ("dirname", "mkdir"):
            executable = shutil.which(name)
            self.assertIsNotNone(executable)
            tools.joinpath(name).symlink_to(executable)

        fake_uv = self.temporary / "selector-fake-uv"
        fake_uv.write_text(
            f"#!{sys.executable}\n" + FAKE_UV.lstrip(),
            encoding="utf-8",
        )
        fake_uv.chmod(0o700)
        environment = {
            "EMCLAW_RUNTIME_DIR": str(self.runtime),
            "EMCLAW_TEST_RECORD": str(self.record),
            "EMCLAW_UV_BIN": str(fake_uv),
            "HOME": str(self.home),
            "PATH": str(tools),
            "RELION_MCP_REPO": str(RELION_BACKEND),
            "UV_CACHE_DIR": str(self.runtime / "uv-cache"),
        }

        result = subprocess.run(
            ["/bin/bash", str(RELION_SELECTOR), "--help"],
            check=False,
            capture_output=True,
            encoding="utf-8",
            env=environment,
            cwd=self.temporary,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = self.load_record()
        self.assertEqual(
            record["argv"],
            [
                "run",
                "--no-project",
                "--no-config",
                "--python-preference",
                "only-managed",
                "--python",
                "3.11",
                "python",
                "-P",
                "-m",
                "relion_mcp.class_select",
                "--help",
            ],
        )
        self.assertEqual(Path(record["pythonpath"]), RELION_BACKEND / "src")

    def test_configured_relion_python_does_not_require_a_uv_cache(self) -> None:
        fake_python = self._executable("fake-python", FAKE_UV)
        environment = self.environment()
        environment.pop("RELION_MCP_REPO", None)
        environment["EMCLAW_RELION_MCP_PYTHON"] = str(fake_python)
        environment["UV_CACHE_DIR"] = "unused-relative-cache"
        result = subprocess.run(
            [str(RELION_BOOTSTRAP)],
            check=False,
            capture_output=True,
            encoding="utf-8",
            env=environment,
            cwd=self.temporary,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = self.load_record()
        self.assertEqual(record["argv"], ["-m", "relion_mcp.server"])
        self.assertEqual(record["dont_write_bytecode"], "1")
        self.assertEqual(Path(record["class_selector"]), RELION_SELECTOR.resolve())
        self.assertFalse((self.temporary / "unused-relative-cache").exists())

    def test_interactive_launch_prints_the_welcome_banner_to_the_terminal(self) -> None:
        for locale, present, absent in (
            ("en_US.UTF-8", "\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2557", " _____ __  __"),
            ("C", " _____ __  __", "\u2588"),
        ):
            with self.subTest(locale=locale):
                self.record.unlink(missing_ok=True)
                environment = self.environment()
                environment["LC_ALL"] = locale
                result, terminal = self.invoke_with_terminal_stderr("relion", environment=environment)
                self.assertEqual(result.returncode, 0, result.stdout + terminal)
                self.assertIn(present, terminal)
                self.assertNotIn(absent, terminal)
                self.assertIn("An Experience-Grounded Language-Model Agent", terminal)
                self.assertIn("for Auto Cryo-EM Single-Particle Analysis", terminal)
                self.assertIn("route: relion (RELION)", terminal)
                self.assertNotIn("Language-Model Agent", result.stdout)
                self.assertEqual(self.load_record()["relion_mode"], "run")

    def test_banner_is_absent_without_a_terminal_on_dry_run_or_when_disabled(self) -> None:
        marker = "An Experience-Grounded Language-Model Agent"
        piped = self.invoke("relion")
        self.assertEqual(piped.returncode, 0, piped.stdout + piped.stderr)
        self.assertNotIn(marker, piped.stdout + piped.stderr)

        dry_run, terminal = self.invoke_with_terminal_stderr("--dry-run", "relion")
        self.assertEqual(dry_run.returncode, 0, dry_run.stdout + terminal)
        self.assertNotIn(marker, dry_run.stdout + terminal)

        environment = self.environment()
        environment["EMCLAW_NO_BANNER"] = "1"
        disabled, terminal = self.invoke_with_terminal_stderr("relion", environment=environment)
        self.assertEqual(disabled.returncode, 0, disabled.stdout + terminal)
        self.assertNotIn(marker, disabled.stdout + terminal)


if __name__ == "__main__":
    unittest.main()
