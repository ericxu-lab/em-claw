"""The CryoSPARC symmetry helper stays linked to the vendored RELION scanner."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

EMCLAW_ROOT = Path(__file__).resolve().parents[2]
HELPER = EMCLAW_ROOT / "profiles" / "cryosparc" / "vis_res" / "symmetry_scan.py"
RELION_PACKAGE = EMCLAW_ROOT / "profiles" / "relion" / "backend" / "src" / "relion_mcp"


def module_names(path: Path) -> set[str]:
    """Top-level functions, classes and assigned names defined by a module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


class SymmetryScanLinkTests(unittest.TestCase):
    def test_helper_imports_resolve_in_the_relion_backend(self) -> None:
        tree = ast.parse(HELPER.read_text(encoding="utf-8"))
        imports = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("relion_mcp.")
        ]
        self.assertTrue(imports, "symmetry_scan.py no longer imports the RELION scanner")
        for node in imports:
            module = RELION_PACKAGE / (node.module.split(".", 1)[1] + ".py")
            self.assertTrue(module.is_file(), f"missing RELION module {module.name}")
            missing = {alias.name for alias in node.names} - module_names(module)
            self.assertFalse(missing, f"{module.name} no longer defines {sorted(missing)}")

    def test_relion_server_still_exposes_the_same_scan(self) -> None:
        server = (RELION_PACKAGE / "server.py").read_text(encoding="utf-8")
        self.assertIn("def relion_scan_symmetry(", server)
        self.assertIn("render_symmetry_scan(", server)


if __name__ == "__main__":
    unittest.main(verbosity=2)
