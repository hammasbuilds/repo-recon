"""The registry manifest, against the package it describes.

A listing is read from `server.json`, the README and the tool surface, by something that
never runs the code. Every one of those is a claim about the package, and the release
workflow gates on some of them at tag time — which is the worst moment to find out.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import tomllib

from reporecon import __version__
from reporecon.mcp_server import ROOT_ENV, TOOLS

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def test_the_manifest_describes_this_package():
    package = MANIFEST["packages"][0]
    assert package["identifier"] == PROJECT["name"]
    assert package["version"] == MANIFEST["version"] == PROJECT["version"] == __version__
    # A registry client runs `uvx <identifier>`, which needs a script of that name.
    assert PROJECT["scripts"][PROJECT["name"]] == "reporecon.mcp_server:main"
    assert len(MANIFEST["description"]) <= 200


def test_the_manifest_declares_the_one_setting_that_matters():
    """The confinement root is the only environment variable this server reads, and it
    is the one a reader needs to know exists before pointing it at anything."""
    package = MANIFEST["packages"][0]
    declared = [v["name"] for v in package["environmentVariables"]]
    assert declared == [ROOT_ENV]

    # Every environment variable the source reads, whether written as a literal or
    # through a constant. A constant resolved to its value, so the test is about the
    # variable and not about how the code spells it.
    constants = {"ROOT_ENV": ROOT_ENV}
    read: set[str] = set()
    for path in (ROOT / "src" / "reporecon").rglob("*.py"):
        for match in re.finditer(
            r"os\.environ(?:\.get)?\(\s*(?:\"([A-Z_]+)\"|'([A-Z_]+)'|([A-Za-z_]+))",
            path.read_text(encoding="utf-8"),
        ):
            name = match.group(1) or match.group(2) or match.group(3)
            read.add(constants.get(name, name))
    assert read <= set(declared), f"read but not declared: {sorted(read - set(declared))}"


def test_the_readme_proves_ownership_of_the_registry_name():
    """PyPI ownership is proved by the `mcp-name:` marker surviving into the long
    description. The release workflow asserts it reaches PKG-INFO; this asserts it is
    there to begin with."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert re.search(r"mcp-name: " + re.escape(MANIFEST["name"]) + r"(\s|-->)", readme)


def test_every_tool_presents_a_surface_a_client_can_read():
    """What a Trust Index actually grades: can a reader see what each tool takes and
    returns without running it."""
    for tool in TOOLS:
        assert tool.get("description"), tool["name"]
        assert tool.get("inputSchema", {}).get("properties"), tool["name"]
        assert tool.get("outputSchema", {}).get("properties"), tool["name"]
        assert tool["inputSchema"].get("additionalProperties") is False, tool["name"]
        for name, prop in tool["inputSchema"]["properties"].items():
            assert prop.get("description"), f"{tool['name']}.{name} has no description"
