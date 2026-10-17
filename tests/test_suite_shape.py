"""No test module can leave a run without the run saying so.

An independent reviewer cloned this repository, made a virtualenv, installed pytest and
nothing else, and got

    174 passed, 1 skipped

against a suite of 246. `tests/test_mcp_server.py` had
`pytest.importorskip("jsonschema")` at module level, a thousand lines in; a module-level
importorskip raises during *collection*, so the whole file was dropped. The 72 tests
that disappeared were the protocol tests, the confinement tests, the read-only
fingerprint test and the schema sweep - which is what README.md advertises the suite
for. CI installs the dev group, so CI never saw it.

Two tests use the validator and it is a fixture now, so the skip lands on those two.
This file is the guard, and it is about the SHAPE of the suite rather than about any
behaviour of the package: a module-level skip is invisible in a pass count, and a pass
count is what anyone reads.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent

MODULES = sorted(p for p in TESTS.rglob("test_*.py"))

#: Calls that stop a whole module when they run at import time. `skip` needs
#: `allow_module_level=True` to be legal there at all; `importorskip` does not, which is
#: why it is the one that got in.
MODULE_STOPPERS = {"importorskip", "skip", "exit", "fail"}

#: What the suite collects. Pinned because the failure mode is a count that drops while
#: the line above it still says "passed".
COLLECTED = 461


def _module_level_calls(tree: ast.Module) -> list[tuple[str, int]]:
    """Calls evaluated at import time, excluding anything inside a def or a class.

    A call in a function body runs when that function runs. Only the module's own top
    level - including the body of a top-level `if` or `try`, which is where this kind of
    thing hides - runs during collection.
    """
    found: list[tuple[str, int]] = []
    stack: list[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name in MODULE_STOPPERS:
                found.append((name, node.lineno))
        stack.extend(ast.iter_child_nodes(node))
    return found


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_no_module_skips_itself_at_import_time(module: Path):
    """The defect as a rule, rather than as the one file it happened in.

    A dependency only some tests need belongs in a fixture: the skip is then counted
    against those tests and the rest of the module runs.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"))
    stoppers = _module_level_calls(tree)
    assert not stoppers, (
        f"{module.name} calls {stoppers} at import time, which drops the whole module "
        "during collection. Use a fixture so the skip is counted against the tests "
        "that need it."
    )


def test_the_suite_is_all_of_it():
    """Collection, which is the number a module-level skip changes."""
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--collect-only", "-p", "no:cacheprovider"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=600,
    )
    assert done.returncode == 0, done.stdout[-2000:]
    stated = re.search(r"(\d+) tests? collected", done.stdout)
    assert stated, done.stdout[-2000:]
    assert int(stated.group(1)) == COLLECTED, (
        f"{stated.group(1)} tests collected, not {COLLECTED}. If that is deliberate, "
        "this figure moves with it."
    )


def test_the_optional_dependency_is_optional_for_the_suite_and_not_for_ci():
    """A dev dependency the suite cannot run without is a runtime dependency.

    The package advertises no runtime dependencies. `jsonschema` is in a dependency
    group, so a reader who installs the package alone must still get a complete and
    honest run - 13 skips, not 72 disappearances.
    """
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "jsonschema" in pyproject
    assert "[dependency-groups]" in pyproject

    server_tests = (TESTS / "test_mcp_server.py").read_text(encoding="utf-8")
    assert "def jsonschema():" in server_tests, "the validator is not behind a fixture"
    tree = ast.parse(server_tests)
    users = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name.startswith("test_")
        and any(a.arg == "jsonschema" for a in node.args.args)
    ]
    assert sorted(users) == [
        "test_every_output_validates_under_a_real_validator",
        "test_the_declared_schemas_are_valid_json_schema",
    ], users


def test_the_modules_that_import_from_another_test_module_are_named():
    """Why one module-level skip could remove more than one file.

    A test module importing helpers from another is reasonable - there should be one
    copy of a helper that drives a real server over a pipe - but it means a
    collection-time skip in the imported module takes the importer with it. The
    dependency is allowed and written down; the skip is what the test above forbids.
    """
    importers: dict[str, set[str]] = {}
    for module in MODULES:
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                ("tests.test_", "test_")
            ):
                importers.setdefault(module.name, set()).add(node.module)
    assert importers == {}, importers
