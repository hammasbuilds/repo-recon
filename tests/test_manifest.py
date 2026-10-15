"""The registry manifest, against the package it describes.

A listing is read from `server.json`, the README and the tool surface, by something that
never runs the code. Every one of those is a claim about the package, and the release
workflow gates on some of them at tag time — which is the worst moment to find out.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.10, which `requires-python = ">=3.10"` promises
    # `tomllib` entered the standard library in 3.11. This module imported it
    # unconditionally, so on 3.10 the whole file failed at COLLECTION - pytest reported
    # one error and ran nothing, and every claim this file makes about `server.json`
    # went unchecked on the oldest version the package says it supports.
    import tomli as tomllib

from reporecon import __version__, telemetry
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
    """Both environment variables this server reads are declared, and they are the two a
    reader needs to know exist: one bounds what can be read, the other is the only thing
    that makes this server write a file.

    This said "the confinement root is the only environment variable this server reads"
    and asserted a one-element list. Adding the call log made that false, and the
    assertion is what caught it.
    """
    package = MANIFEST["packages"][0]
    declared = [v["name"] for v in package["environmentVariables"]]
    assert declared == [ROOT_ENV, telemetry.ENV]

    # Every environment variable the source reads, whether written as a literal or
    # through a constant. A constant resolved to its value, so the test is about the
    # variable and not about how the code spells it.
    constants = {"ROOT_ENV": ROOT_ENV, "ENV": telemetry.ENV}
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


def test_the_readme_states_the_publication_order():
    """`server.json` declares a PyPI package at a version, which is what the registry
    needs and is not a statement that the upload has happened.

    This package is not on PyPI and the README said nothing about it either way - so a
    reader had the manifest declaring `registryType: "pypi"` and no indication that
    submitting it before the upload makes every client fail at `uvx repo-recon`.
    Nothing here can check whether the upload has happened; what is checkable is that
    the order is written down.
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Upload to PyPI first, then" in readme
    assert "is **not** a statement that the upload has happened" in readme


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
        # OUTPUT properties too. This loop covered inputs only, so 27 output fields
        # carried no description at all - including `mechanical` and `behavioural`,
        # the two an entire README section is devoted to keeping apart, and `repo`,
        # which meant a directory name in one tool and an absolute path in another
        # with nothing anywhere saying which. A client reads the output schema to know
        # what it is being handed.
        for name, prop in tool["outputSchema"]["properties"].items():
            assert prop.get("description"), (
                f"{tool['name']} returns {name} and the schema does not say what it is"
            )


# -- the lint gate's own scope ------------------------------------------------


def test_the_lint_gate_covers_the_whole_tree_not_just_src_and_tests():
    """A gate narrower than the repository is a gate with a blind spot.

    Both workflows ran `ruff check src tests` and `ruff format --check src tests`, so
    `demo.py`, `docs/` and the workflow files themselves were outside it. A formatting
    violation in `docs/migration-pilot.md` - the document that shows this tool's own
    sample input - sat in `main` with CI green, and was found by running the command
    the README tells a reader to run rather than the one CI runs.

    The two must be the same command. One block has to stay unformatted because it IS
    the before-modernisation sample; that is excluded in `pyproject.toml`, where the
    exclusion travels with the repository instead of living in each caller's memory.
    """
    root = Path(__file__).resolve().parents[1]
    for name in ("ci.yml", "release.yml"):
        body = (root / ".github" / "workflows" / name).read_text(encoding="utf-8")
        assert "ruff check ." in body, f"{name}: the lint gate is narrower than the tree"
        assert "ruff format --check ." in body, f"{name}: the format gate is narrower"
        assert "ruff check src tests" not in body, f"{name}: the narrow gate is back"
        assert "ruff format --check src tests" not in body, f"{name}: the narrow gate is back"

    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    excluded = config["tool"]["ruff"].get("extend-exclude", [])
    assert excluded == ["docs/migration-pilot.md"], (
        f"the exclusion list is {excluded}; every entry is a file the gate does not "
        "read, so it is worth one each"
    )
    # And the excluded file still holds the thing it is excluded for. An exclusion
    # outliving its reason is a hole nobody is watching.
    sample = (root / excluded[0]).read_text(encoding="utf-8")
    assert "[1,  2]" in sample, (
        "the deliberately unformatted sample is gone, so the exclusion has outlived "
        "its reason and should be removed"
    )
