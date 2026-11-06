"""Root neutrality: the dispatcher root owns no backend profile or shared state."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

EMCLAW_ROOT = Path(__file__).resolve().parents[2]


class RootNeutralityTests(unittest.TestCase):
    """Group C: the dispatcher root owns no backend profile or shared state."""

    GROUP = "C"

    def test_root_has_no_backend_configuration_or_agent_tree(self) -> None:
        forbidden = (
            ".mcp.json",
            ".claude",
            ".agents",
            "agents",
            "skills",
        )
        present = [name for name in forbidden if os.path.lexists(EMCLAW_ROOT / name)]
        self.assertFalse(
            present,
            "root backend surfaces present: " + ", ".join(sorted(present)),
        )

    def test_root_instructions_import_neither_backend(self) -> None:
        text = (EMCLAW_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        import_lines = [
            str(index)
            for index, line in enumerate(text.splitlines(), start=1)
            if line.lstrip().startswith("@")
        ]
        lowered = text.lower()
        forbidden_tokens = (
            "mcp__relion__",
            "mcp__cryosparc__",
            "relion-spa-orchestrator",
            "cryosparc-tools",
            "@agents.md",
            "@profiles/",
        )
        found = [token for token in forbidden_tokens if token in lowered]
        self.assertFalse(
            import_lines,
            "root CLAUDE.md contains imports at lines: " + ", ".join(import_lines),
        )
        self.assertFalse(
            found,
            "root CLAUDE.md contains backend import tokens: "
            + ", ".join(sorted(found)),
        )

    def test_root_has_no_shared_processing_state_or_project_pointer(self) -> None:
        forbidden = (
            "runs",
            "base_datainfo.yaml",
            "RELION_RUN_STATE.yaml",
            "project",
            "projects",
            "current_project",
            "relion_project",
            "cryosparc_project",
        )
        present = [name for name in forbidden if os.path.lexists(EMCLAW_ROOT / name)]
        root_links = sorted(
            item.name for item in EMCLAW_ROOT.iterdir() if item.is_symlink()
        )
        self.assertFalse(
            present,
            "root shared-state markers present: " + ", ".join(sorted(present)),
        )
        self.assertFalse(
            root_links,
            "root project pointers present: " + ", ".join(root_links),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
