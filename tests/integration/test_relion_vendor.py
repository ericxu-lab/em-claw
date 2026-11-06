"""Self-contained contracts for the vendored RELION backend snapshot."""

from __future__ import annotations

import hashlib
import json
import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "profiles" / "relion"
BACKEND = PROFILE / "backend"
PROVENANCE = PROFILE / "backend.provenance.json"


class RelionVendorTests(unittest.TestCase):
    GROUP = "A"

    def test_backend_is_a_real_clean_directory(self) -> None:
        self.assertTrue(BACKEND.is_dir())
        self.assertFalse(BACKEND.is_symlink())
        forbidden_names = {
            ".git",
            ".venv",
            ".pytest_cache",
            ".ruff_cache",
            "__pycache__",
            "build",
            "dist",
        }
        present = {
            path.relative_to(BACKEND).as_posix()
            for path in BACKEND.rglob("*")
            if path.name in forbidden_names
        }
        self.assertEqual(present, set())
        symlinks = {
            path.relative_to(BACKEND).as_posix()
            for path in BACKEND.rglob("*")
            if path.is_symlink()
        }
        self.assertEqual(symlinks, set())

    def test_provenance_matches_every_exported_file(self) -> None:
        provenance = json.loads(PROVENANCE.read_text(encoding="utf-8"))
        self.assertEqual(
            set(provenance),
            {
                "schema_version",
                "component",
                "package_name",
                "package_version",
                "license",
                "source_repository",
                "source_remote",
                "source_revision",
                "source_tree",
                "export_method",
                "exported_path",
                "content_digest_algorithm",
                "content_digest_sha256",
                "excluded_checkout_artifacts",
            },
        )
        self.assertEqual(provenance["schema_version"], "em-claw-vendored-backend/v1")
        self.assertEqual(provenance["component"], "relion")
        self.assertEqual(provenance["package_name"], "relion-mcp")
        self.assertEqual(provenance["package_version"], "0.5.0")
        self.assertEqual(provenance["license"], "MIT")
        self.assertEqual(provenance["source_repository"], "relion")
        self.assertIsNone(provenance["source_remote"])
        self.assertEqual(
            provenance["source_revision"],
            "96dcfc10949983fe9578a4b59da4c409ed4bf7f7",
        )
        self.assertEqual(
            provenance["source_tree"],
            "fee2be29fe36e9d5e4155430a5d3ea895c5abd39",
        )
        self.assertEqual(
            provenance["export_method"],
            "git archive with local identity normalization plus profile-local feature changes",
        )
        self.assertEqual(provenance["exported_path"], "profiles/relion/backend")
        self.assertEqual(
            provenance["content_digest_algorithm"],
            "sha256 of sorted path\\0mode\\0size\\0file_sha256\\n records",
        )
        self.assertEqual(
            provenance["excluded_checkout_artifacts"],
            [
                ".git/",
                ".venv/",
                ".pytest_cache/",
                ".ruff_cache/",
                "__pycache__/",
                "build/",
                "dist/",
            ],
        )
        self.assertEqual(
            provenance["content_digest_sha256"],
            "59425e4b8ee8b3080ca28a6af09a7543fabc7b293d550eac6bb57be74a4851b3",
        )

        digest = hashlib.sha256()
        files = sorted(
            path
            for path in BACKEND.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        self.assertEqual(len(files), 70)
        for path in files:
            payload = path.read_bytes()
            mode = "100755" if path.stat().st_mode & 0o111 else "100644"
            record = (
                f"{path.relative_to(BACKEND).as_posix()}\0{mode}\0"
                f"{len(payload)}\0{hashlib.sha256(payload).hexdigest()}\n"
            )
            digest.update(record.encode("utf-8"))
        self.assertEqual(digest.hexdigest(), provenance["content_digest_sha256"])

    def test_profile_keeps_only_four_relative_adapter_links(self) -> None:
        expected = {
            "AGENTS.md": BACKEND / "AGENTS.md",
            "docs": BACKEND / "docs",
            "src": BACKEND / "src",
            ".claude/skills/relion-spa-orchestrator": (
                BACKEND / ".agents" / "skills" / "relion-spa-orchestrator"
            ),
        }
        observed = {
            path.relative_to(PROFILE).as_posix()
            for path in PROFILE.rglob("*")
            if path.is_symlink() and BACKEND not in path.parents
        }
        self.assertEqual(observed, set(expected))
        for relative, target in expected.items():
            link = PROFILE / relative
            self.assertFalse(Path(os.readlink(link)).is_absolute())
            self.assertEqual(link.resolve(strict=True), target.resolve(strict=True))


if __name__ == "__main__":
    unittest.main()
