"""No test in this suite has an assertion that cannot fail by shape.

`test_nothing_is_written_while_the_tools_run` had one: it read

    assert sorted(p.name for p in repo.rglob("*")) == sorted(
        p.name for p in repo.rglob("*")
    )

with both sides evaluated after the tools ran, so it compared the directory with
itself at one instant. It was there to catch a file appearing or disappearing - the
read-only claim this whole project rests on - and the contents check beside it covers
only `*.py`, so a tool writing a `.json` or deleting a fixture passed both.

Three shapes are checked, each one found in a real test by a reviewer somewhere:

  * `assert "k" in d` *after* `d["k"]` has been read in the same function. The
    subscript would have raised first, so the assertion adds nothing. Order matters:
    the same assertion BEFORE the subscript is a guard that turns a `KeyError` into a
    readable failure, and several tests here use it that way.
  * `assert a == a` where both sides are the same side-effect-free expression. Calls
    are excluded, because `assert f(x) == f(x)` on a pure function is two calls and is
    exactly how per-call nondeterminism is caught - it only looks like comparing a
    value with itself.
  * `assert <non-empty literal>`.

This is a shape check, not a judgement: a redundant assertion beside a real one is
noise rather than a defect. What it catches is the one that was load-bearing.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _dump(node: ast.AST) -> str:
    return ast.dump(node)


def _string_subscripts(function: ast.FunctionDef) -> set[tuple[str, str, int]]:
    """(container, key, line) for every `x["k"]` read inside this function."""
    return {
        (_dump(node.value), node.slice.value, node.lineno)
        for node in ast.walk(function)
        if isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    }


def _cannot_fail(function: ast.FunctionDef) -> list[str]:
    seen = _string_subscripts(function)
    out: list[str] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        same = (
            isinstance(test, ast.Compare)
            and len(test.ops) == 1
            and _dump(test.left) == _dump(test.comparators[0])
        )
        if (
            isinstance(test, ast.Compare)
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.In)
            and isinstance(test.left, ast.Constant)
            and isinstance(test.left.value, str)
            and any(
                container == _dump(test.comparators[0])
                and key == test.left.value
                and line < node.lineno
                for container, key, line in seen
            )
        ):
            out.append(
                f'line {node.lineno}: `assert "{test.left.value}" in ...` after that '
                "key has already been read, so the subscript would have raised first"
            )
        elif (
            same
            and isinstance(test.ops[0], (ast.Eq, ast.Is))
            and not any(isinstance(n, ast.Call) for n in ast.walk(test.left))
        ):
            out.append(f"line {node.lineno}: both sides of the comparison are the same")
        elif isinstance(test, ast.Constant) and test.value:
            out.append(f"line {node.lineno}: `assert {test.value!r}`")
    return out


def test_there_are_tests_to_check():
    """A sweep over no files passes."""
    files = sorted(ROOT.rglob("test_*.py"))
    assert len(files) >= 8, [f.name for f in files]


def test_no_assertion_in_this_suite_can_pass_whatever_the_code_does():
    offenders: list[str] = []
    for path in sorted(ROOT.rglob("test_*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not (isinstance(function, ast.FunctionDef) and function.name.startswith("test_")):
                continue
            for why in _cannot_fail(function):
                offenders.append(f"{path.relative_to(ROOT).as_posix()}::{function.name} {why}")
    assert not offenders, offenders


def test_the_one_that_was_found_is_fixed():
    """Named, because a count alone is satisfied by deleting the test."""
    source = (ROOT / "test_mcp_server.py").read_text(encoding="utf-8")
    assert "listing_before = " in source, "the before-listing is gone"
    assert "listing_before == listing_after" in source
    assert "a file appeared or disappeared" in source
