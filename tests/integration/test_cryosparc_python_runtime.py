"""Regression tests for the launcher-owned CryoSPARC SDK runtime."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CRYO_PYTHON = ROOT / "libexec" / "em-claw-cryosparc-python"
COMBINED_PROFILE = ROOT / "profiles" / "combined"
COMBINED_SETTINGS = COMBINED_PROFILE / ".claude" / "settings.json"

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
record = {
    "argv": sys.argv[1:],
    "cryo": {name: os.environ.get(name) for name in cryo_names},
    "cryo_env_file": os.environ.get("EMCLAW_CRYOSPARC_ENV_FILE"),
    "relion_present": sorted(
        name for name in os.environ if name.startswith("RELION_")
    ),
    "relion_env_file_present": "EMCLAW_RELION_ENV_FILE" in os.environ,
    "uv_cache": os.environ.get("UV_CACHE_DIR"),
    "dont_write_bytecode": os.environ.get("PYTHONDONTWRITEBYTECODE"),
}
with open(os.environ["EMCLAW_TEST_RECORD"], "w", encoding="utf-8") as handle:
    json.dump(record, handle, sort_keys=True)
"""


class CryosparcPythonRuntimeTests(unittest.TestCase):
    """The shell write path must use the same compatible SDK as the MCP."""

    def setUp(self) -> None:
        self.temporary_context = tempfile.TemporaryDirectory(
            prefix="emclaw-cryo-python-", dir="/tmp"
        )
        self.addCleanup(self.temporary_context.cleanup)
        self.temporary = Path(self.temporary_context.name)
        self.record = self.temporary / "record.json"
        self.runtime = self.temporary / "runtime"
        self.home = self.temporary / "home"
        self.home.mkdir()
        self.cryo_source = self.temporary / "cryosparc-source"
        self.cryo_source.mkdir()
        self.cryo_env = self.temporary / "cryosparc.env"
        self.cryo_env.write_text(
            f"EMCLAW_CRYOSPARC_SOURCE={self.cryo_source}\n"
            "CRYOSPARC_LICENSE_ID=file-license\n"
            "CRYOSPARC_MASTER_HOSTNAME=file-host\n"
            "CRYOSPARC_BASE_PORT=39000\n"
            "CRYOSPARC_EMAIL=file@example.test\n"
            "CRYOSPARC_PASSWORD=file-password\n",
            encoding="utf-8",
        )
        self.cryo_env.chmod(0o600)
        self.fake_uv = self._executable("fake-uv", FAKE_UV)
        self.bypass_python = self._executable(
            "must-not-run-python", "raise SystemExit(91)\n"
        )

    def _executable(self, name: str, body: str) -> Path:
        path = self.temporary / name
        path.write_text("#!/usr/bin/env python3\n" + body.lstrip(), encoding="utf-8")
        path.chmod(0o700)
        return path

    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        for name in list(environment):
            if name.startswith(("CRYOSPARC_", "RELION_")):
                environment.pop(name)
        for name in (
            "EMCLAW_COMBINED_ROUTE",
            "EMCLAW_CRYOSPARC_ENV_FILE",
            "EMCLAW_CRYOSPARC_MCP_PYTHON",
            "EMCLAW_RELION_ENV_FILE",
            "EMCLAW_RUNTIME_DIR",
            "EMCLAW_TEST_RECORD",
            "EMCLAW_UV_BIN",
            "UV_CACHE_DIR",
        ):
            environment.pop(name, None)
        environment.update(
            {
                "HOME": str(self.home),
                "EMCLAW_CRYOSPARC_ENV_FILE": str(self.cryo_env),
                "EMCLAW_CRYOSPARC_MCP_PYTHON": str(self.bypass_python),
                "EMCLAW_RELION_ENV_FILE": str(self.temporary / "relion.env"),
                "EMCLAW_RUNTIME_DIR": str(self.runtime),
                "EMCLAW_TEST_RECORD": str(self.record),
                "EMCLAW_UV_BIN": str(self.fake_uv),
                "RELION_MCP_REPO": "opposite-backend-canary",
                "RELION_MCP_MODE": "plan",
                "RELION_PROJECT_ROOTS": "/opposite/backend/canary",
                "RELION_SCRATCH_DIR": "/opposite/scratch/canary",
                "RELION_TEST_CANARY": "opposite-backend-canary",
            }
        )
        return environment

    def test_forwards_python_argv_through_the_pinned_sdk_runtime(self) -> None:
        code = "print('sdk-write-probe')"
        python_arguments = ["-c", code, "argument with spaces", ""]

        result = subprocess.run(
            [str(CRYO_PYTHON), *python_arguments],
            check=False,
            capture_output=True,
            encoding="utf-8",
            env=self.environment(),
            cwd=self.temporary,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertEqual(
            record["argv"],
            [
                "run",
                "--no-project",
                "--python",
                "3.11",
                "--with",
                "cryosparc-tools==4.7.1",
                "--with",
                "numpy==1.26.4",
                "--with",
                "pillow>=10,<12",
                "--with",
                "mrcfile>=1.4,<2",
                "--with",
                "matplotlib>=3.8,<3.10",
                "--with",
                "pyyaml>=6,<7",
                "--with",
                "scipy>=1.11,<1.15",
                "python",
                *python_arguments,
            ],
        )
        self.assertNotIn("cryo_ief", " ".join(record["argv"]))
        self.assertEqual(
            record["cryo"],
            {
                "CRYOSPARC_LICENSE_ID": "file-license",
                "CRYOSPARC_MASTER_HOSTNAME": "file-host",
                "CRYOSPARC_BASE_PORT": "39000",
                "CRYOSPARC_EMAIL": "file@example.test",
                "CRYOSPARC_PASSWORD": "file-password",
            },
        )
        self.assertEqual(record["cryo_env_file"], str(self.cryo_env))
        self.assertEqual(record["relion_present"], [])
        self.assertFalse(record["relion_env_file_present"])
        self.assertEqual(record["uv_cache"], str(self.runtime / "uv-cache"))
        self.assertEqual(record["dont_write_bytecode"], "1")

    def test_rejects_missing_python_argv(self) -> None:
        cases = (
            (CRYO_PYTHON, []),
        )
        for command, arguments in cases:
            with self.subTest(command=command.name, arguments=arguments):
                self.record.unlink(missing_ok=True)
                result = subprocess.run(
                    [str(command), *arguments],
                    check=False,
                    capture_output=True,
                    encoding="utf-8",
                    env=self.environment(),
                    cwd=self.temporary,
                )
                self.assertEqual(result.returncode, 64, result.stdout + result.stderr)
                self.assertFalse(self.record.exists())

    def test_combined_profile_allows_only_the_owned_sdk_wrapper(self) -> None:
        settings = json.loads(COMBINED_SETTINGS.read_text(encoding="utf-8"))
        allowed = settings["permissions"]["allow"]

        self.assertFalse(
            any("cryo_ief" in permission.lower() for permission in allowed),
            "the combined profile must not broadly authorize the cryo_ief interpreter",
        )
        wrapper_permissions = [
            permission
            for permission in allowed
            if permission.startswith("Bash(") and CRYO_PYTHON.name in permission
        ]
        self.assertEqual(len(wrapper_permissions), 1)
        match = re.fullmatch(r"Bash\((.+):\*\)", wrapper_permissions[0])
        self.assertIsNotNone(match)
        assert match is not None
        configured = Path(match.group(1))
        if not configured.is_absolute():
            configured = COMBINED_PROFILE / configured
        self.assertEqual(configured.resolve(strict=True), CRYO_PYTHON.resolve(strict=True))


if __name__ == "__main__":
    unittest.main()
