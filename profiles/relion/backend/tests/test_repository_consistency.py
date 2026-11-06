from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

from relion_mcp import __version__
from relion_mcp.settings import ENVIRONMENT_DEFAULTS

ROOT = Path(__file__).resolve().parents[1]


def test_source_version_matches_project_and_lock_metadata() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    locked = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    project_version = project["project"]["version"]
    locked_project = next(
        package
        for package in locked["package"]
        if package["name"] == project["project"]["name"]
        and package.get("source") == {"editable": "."}
    )

    assert __version__ == project_version
    assert locked_project["version"] == project_version


def _tool_definitions() -> dict[str, ast.FunctionDef]:
    tree = ast.parse((ROOT / "src/relion_mcp/server.py").read_text(encoding="utf-8"))
    result: dict[str, ast.FunctionDef] = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "tool"
            for decorator in node.decorator_list
        ):
            result[node.name] = node
    return result


def test_api_inventory_and_sections_match_live_tools() -> None:
    api = (ROOT / "docs/API.md").read_text(encoding="utf-8")
    definitions = _tool_definitions()
    live = set(definitions)
    inventory_rows = re.findall(
        r"^\| `(relion_[a-z0-9_]+)` \| ([^|]+?) \| `([A-Z_]+)` \|$",
        api,
        flags=re.MULTILINE,
    )
    inventory = {name for name, _, _ in inventory_rows}
    sections = set(re.findall(r"^### `(relion_[a-z0-9_]+)`$", api, flags=re.MULTILINE))

    assert len(live) == 50
    assert inventory == live
    assert sections == live

    documented_metadata = {
        name: (title.strip(), annotation)
        for name, title, annotation in inventory_rows
    }
    live_metadata: dict[str, tuple[str, str]] = {}
    for name, node in definitions.items():
        decorator = next(
            item
            for item in node.decorator_list
            if isinstance(item, ast.Call)
            and isinstance(item.func, ast.Attribute)
            and item.func.attr == "tool"
        )
        keywords = {item.arg: item.value for item in decorator.keywords}
        title = ast.literal_eval(keywords["title"])
        annotation_node = keywords["annotations"]
        assert isinstance(annotation_node, ast.Name)
        live_metadata[name] = (title, annotation_node.id)

    assert documented_metadata == live_metadata


def test_documented_tool_signatures_match_source() -> None:
    api = (ROOT / "docs/API.md").read_text(encoding="utf-8")
    source = (ROOT / "src/relion_mcp/server.py").read_text(encoding="utf-8")
    lines = source.splitlines()

    for name, node in _tool_definitions().items():
        header = " ".join(
            line.strip()
            for line in lines[node.lineno - 1 : node.body[0].lineno - 1]
        )
        source_signature = header.removeprefix("def ").removesuffix(":")
        match = re.search(
            rf"^### `{re.escape(name)}`\n\n```text\n(.*?)\n```",
            api,
            flags=re.MULTILINE | re.DOTALL,
        )
        assert match is not None, f"Missing API signature block for {name}"
        documented_signature = match.group(1)

        def normalize(value: str) -> str:
            normalized = re.sub(r"\s+", " ", value).strip()
            normalized = normalized.replace("( ", "(").replace(" )", ")")
            return re.sub(r",\s*\)", ")", normalized)

        assert normalize(documented_signature) == normalize(source_signature)


def test_environment_examples_match_runtime_configuration() -> None:
    env_values = dict(
        line.split("=", 1)
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )
    example = tomllib.loads(
        (ROOT / "examples/mcp-config.toml").read_text(encoding="utf-8")
    )
    example_values = example["mcp_servers"]["relion"]["env"]

    assert set(env_values) == set(ENVIRONMENT_DEFAULTS)
    assert set(example_values) == set(ENVIRONMENT_DEFAULTS)
    runtime_defaults = {
        name: value
        for name, value in ENVIRONMENT_DEFAULTS.items()
        if name != "RELION_PROJECT_ROOTS"
    }
    assert {name: env_values[name] for name in runtime_defaults} == runtime_defaults
    assert {name: example_values[name] for name in runtime_defaults} == runtime_defaults


def test_configuration_table_matches_runtime_defaults() -> None:
    configuration = (ROOT / "docs/CONFIGURATION.md").read_text(encoding="utf-8")
    documented = dict(
        re.findall(
            r"^\| `(RELION_[A-Z0-9_]+)` \| `([^`]*)`(?: \(empty\))? \|",
            configuration,
            flags=re.MULTILINE,
        )
    )
    documented = {
        name: "" if value == '""' else value
        for name, value in documented.items()
    }

    assert documented == ENVIRONMENT_DEFAULTS


def test_relative_markdown_links_resolve() -> None:
    excluded = {".git", ".venv", ".pytest_cache", ".ruff_cache", "build", "dist"}
    missing: list[str] = []
    for document in ROOT.rglob("*.md"):
        if excluded.intersection(document.relative_to(ROOT).parts):
            continue
        text = document.read_text(encoding="utf-8")
        for target in re.findall(r"\[[^]]*\]\(([^)]+)\)", text):
            relative = target.strip().split("#", 1)[0]
            if not relative or "://" in relative or relative.startswith("mailto:"):
                continue
            if not (document.parent / relative).resolve().exists():
                missing.append(f"{document.relative_to(ROOT)}: {relative}")

    assert not missing, "\n".join(missing)


def test_source_does_not_invoke_a_shell() -> None:
    violations: list[str] = []
    for source in (ROOT / "src").rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
                and node.func.attr == "system"
            ):
                violations.append(f"{source.relative_to(ROOT)}:{node.lineno}: os.system")
            for keyword in node.keywords:
                if (
                    keyword.arg == "shell"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                ):
                    violations.append(
                        f"{source.relative_to(ROOT)}:{node.lineno}: shell keyword set to true"
                    )

    assert not violations, "\n".join(violations)
