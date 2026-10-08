"""The entry-point list is about the repository, not about how its tests are written.

"Definitions nothing inside the repo calls" is the right question. The answer it printed
put the longest test function in the suite at the top, and listed three of the six MCP
tools while omitting the other three - because some test calls `map_repo` by name and no
test calls `review_diff` that way. All six are dispatched identically through
`getattr(server, name)(arguments)`.

Both halves are the same mistake: counting the test suite as part of the repository when
answering a question about the repository's shape. A test function is called by pytest,
and a call from a test does not make a definition reached by the code.

What remains, and is said in `entry_points`' own docstring rather than fixed: a method
reached only by dynamic dispatch has no static caller at all, so it appears here however
heavily it is used. That is the limit of reading an import graph.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reporecon.cartographer.graph import build_graph, entry_points
from reporecon.cartographer.parse import parse_repo

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def graph():
    return build_graph(parse_repo(ROOT))


def test_no_test_function_is_published_as_an_entry_point(graph):
    """pytest calls every one of them, which says nothing about the code."""
    named = [s.qualname for s in entry_points(graph)]
    assert named, "the list is empty, so this proves nothing"
    offenders = [q for q in named if ".test_" in q or q.startswith("test") or ".tests." in q]
    assert offenders == [], offenders


def test_all_six_tools_are_reported_the_same_way(graph):
    """They are dispatched identically, so they must be reported identically.

    The invariant is "all six or none". Which it is has changed twice, and both times
    the change was a fact about the resolver rather than about the tools:

      * originally three were listed and three were not, and the difference was which
        ones a test happened to call by name - an accident of test style;
      * then all six were listed, because `getattr(server, name)(arguments)` is the only
        path the server itself uses and no call graph can see it;
      * now none are, because `demo.py` calls all six by name through a parameter
        annotated `server: ReconServer`, and an annotated parameter is a binding the
        resolver reads.

    That last one is the point of the change: an independent reviewer found all five
    listed methods had resolvable call sites in this repository, in a list whose heading
    is "Called by nothing in this repository" and which is the first thing `map_repo`
    tells a newcomer. What this test pins is the consistency, and the measured side is
    stated so that a drift back is a failure rather than a surprise.
    """
    named = {s.qualname for s in entry_points(graph)}
    tools = {
        "map_repo",
        "trace_impact",
        "check_modernisation",
        "review_diff",
        "release_readiness",
        "riskiest_commits",
    }
    listed = {t for t in tools if any(q.endswith(f"ReconServer.{t}") for q in named)}
    assert listed in (tools, set()), sorted(listed)
    assert listed == set(), (
        "a tool method is back on the entry-point list; demo.py calls every one of them "
        f"by name, so this says the resolver stopped seeing that: {sorted(listed)}"
    )


def test_a_call_from_a_test_does_not_count_as_a_caller(tmp_path: Path):
    """Driven on a repository built here, because the behaviour is the rule and the
    repository above is one instance of it."""
    root = tmp_path / "r"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "lib.py").write_text("def only_tests_call_me():\n    return 1\n", "utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_lib.py").write_text(
        "from pkg.lib import only_tests_call_me\n\n\n"
        "def test_it():\n    assert only_tests_call_me() == 1\n",
        encoding="utf-8",
    )
    named = {s.qualname for s in entry_points(build_graph(parse_repo(root)))}
    assert any(q.endswith("only_tests_call_me") for q in named), named
    assert not any("test_it" in q for q in named), named


def test_a_call_from_the_code_does_count(tmp_path: Path):
    """The other direction, or the filter would be an excuse to list everything."""
    root = tmp_path / "r"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "lib.py").write_text("def called():\n    return 1\n", encoding="utf-8")
    (root / "pkg" / "app.py").write_text(
        "from pkg.lib import called\n\n\ndef main():\n    return called()\n", encoding="utf-8"
    )
    named = {s.qualname for s in entry_points(build_graph(parse_repo(root)))}
    assert not any(q.endswith("lib.called") for q in named), named
    assert any(q.endswith("app.main") for q in named), named
