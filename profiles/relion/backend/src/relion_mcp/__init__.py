"""RELION MCP server package."""

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _project_version() -> str:
    """Prefer checkout metadata, then installed package metadata."""
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        with pyproject.open("rb") as handle:
            return str(tomllib.load(handle)["project"]["version"])
    except (KeyError, OSError, tomllib.TOMLDecodeError):
        try:
            return version("relion-mcp")
        except PackageNotFoundError:
            return "0+unknown"


__version__ = _project_version()
