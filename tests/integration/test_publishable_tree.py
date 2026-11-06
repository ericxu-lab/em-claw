"""Publishable tree: tracked files carry no site paths, addresses, or credentials."""

from __future__ import annotations

import re
import shlex
import subprocess
import unittest
from pathlib import Path

EMCLAW_ROOT = Path(__file__).resolve().parents[2]
# This file holds the patterns themselves, so it is not scanned.
SELF = Path(__file__).resolve().relative_to(EMCLAW_ROOT).as_posix()

# Home directories, plus the OpenHPC module tree that exposes a cluster's software layout.
LOCAL_PATH = re.compile(
    r"/work/home/\S+|/home/[A-Za-z][\w.-]*|/Users/[A-Za-z][\w.-]*|/opt/ohpc/\S*"
)
PRIVATE_IPV4 = re.compile(
    r"\b(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b"
)
# Bare cluster hostnames such as node7 or login2.
CLUSTER_HOST = re.compile(r"\b(?:node|gpu|mgt|login)\d{1,3}\b")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# RELION image references (000001@particles.mrcs) look like addresses.
NOT_AN_EMAIL = re.compile(r"@[\w.-]+\.(?:mrcs?|star)$")
ALLOWED_EMAIL_DOMAINS = {"example.com", "example.org", "example.test", "example.invalid"}

# Values of these keys in a local .env must never appear as a token in a tracked
# file. Each key has a minimum length: a password shorter than six characters
# cannot be told apart from ordinary numbers or words, so it is not scanned.
SECRET_ENV_KEYS = {
    "CRYOSPARC_LICENSE_ID": 8,
    "CRYOSPARC_MASTER_HOSTNAME": 3,
    "CRYOSPARC_EMAIL": 6,
    "CRYOSPARC_PASSWORD": 6,
}


def tracked_files() -> list[str]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=EMCLAW_ROOT,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise unittest.SkipTest(f"not a git checkout: {exc}") from exc
    return [name for name in result.stdout.decode("utf-8").split("\0") if name]


def tracked_text() -> dict[str, str]:
    texts = {}
    for name in tracked_files():
        path = EMCLAW_ROOT / name
        if name == SELF or path.is_symlink() or not path.is_file():
            continue
        payload = path.read_bytes()
        if b"\0" in payload:
            continue
        texts[name] = payload.decode("utf-8", errors="replace")
    return texts


def env_values(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):]
        name, sep, raw = line.partition("=")
        if not sep or name not in SECRET_ENV_KEYS:
            continue
        try:
            parts = shlex.split(raw, comments=True)
        except ValueError:
            continue
        if parts:
            values[name] = parts[0]
    return values


class PublishableTreeTests(unittest.TestCase):
    """Scan every tracked text file before it is published."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.texts = tracked_text()

    def findings(self, pattern: re.Pattern[str]) -> list[str]:
        found = []
        for name, text in self.texts.items():
            for number, line in enumerate(text.splitlines(), start=1):
                for match in pattern.finditer(line):
                    found.append((name, number, match.group(0)))
        return [f"{name}:{number}: {value}" for name, number, value in found]

    def test_no_local_home_paths(self) -> None:
        found = self.findings(LOCAL_PATH)
        self.assertFalse(found, "local home paths:\n" + "\n".join(found))

    def test_no_private_ip_addresses(self) -> None:
        found = self.findings(PRIVATE_IPV4)
        self.assertFalse(found, "private IP addresses:\n" + "\n".join(found))

    def test_no_cluster_hostnames(self) -> None:
        found = self.findings(CLUSTER_HOST)
        self.assertFalse(found, "cluster hostnames:\n" + "\n".join(found))

    def test_no_real_email_addresses(self) -> None:
        found = [
            item
            for item in self.findings(EMAIL)
            if not NOT_AN_EMAIL.search(item)
            and item.rsplit("@", 1)[1].lower() not in ALLOWED_EMAIL_DOMAINS
        ]
        self.assertFalse(found, "email addresses:\n" + "\n".join(found))

    def test_no_environment_files_are_tracked(self) -> None:
        found = [
            name
            for name in tracked_files()
            if (base := Path(name).name) == ".env"
            or (base.startswith(".env.") and base != ".env.example")
        ]
        self.assertFalse(found, "tracked environment files: " + ", ".join(found))

    def test_local_credentials_do_not_appear_in_tracked_files(self) -> None:
        secrets = {}
        for env_file in sorted((EMCLAW_ROOT / "profiles").glob("*/.env")):
            for name, value in env_values(env_file).items():
                if len(value) >= SECRET_ENV_KEYS[name]:
                    secrets[value] = f"{env_file.parent.name}/.env {name}"
        if not secrets:
            self.skipTest("no local profile .env with credentials")
        patterns = {
            re.compile(r"(?<![\w.@-])" + re.escape(value) + r"(?![\w.@-])"): origin
            for value, origin in secrets.items()
        }
        found = [
            f"{name}: value of {origin}"
            for name, text in self.texts.items()
            for pattern, origin in patterns.items()
            if pattern.search(text)
        ]
        self.assertFalse(found, "local credential values in tracked files:\n" + "\n".join(found))


if __name__ == "__main__":
    unittest.main(verbosity=2)
