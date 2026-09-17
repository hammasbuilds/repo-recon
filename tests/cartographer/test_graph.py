"""Graph tests: resolution, the honest denominator, and ranking."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from cartographer.graph import (
    build_graph,
    entry_points,
    most_called,
    pagerank,
)
from cartographer.parse import parse_repo


def write(root: Path, rel: str, source: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")


@pytest.fixture
def graph(tmp_path: Path):
    write(tmp_path, "pyproject.toml", "[project]\nname='demo'\n")
    write(tmp_path, "src/demo/util.py", """
        def helper(x):
            return x + 1
    """)
    write(tmp_path, "src/demo/core.py", """
        from demo.util import helper

        class Engine:
            def run(self):
                return self.step()

            def step(self):
                return helper(1)

        def entry():
            return Engine().run()
    """)
    write(tmp_path, "src/demo/cli.py", """
        from demo.core import entry

        def main():
            print(entry())
    """)
    return build_graph(parse_repo(tmp_path))


# -- resolution -----------------------------------------------------------


def test_imported_function_resolves_to_its_definition(graph):
    callers = graph.callers("demo.util.helper")
    assert len(callers) == 1
    assert callers[0].caller == "demo.core.Engine.step"


def test_self_call_resolves_within_the_class(graph):
    callers = graph.callers("demo.core.Engine.step")
    assert [c.caller for c in callers] == ["demo.core.Engine.run"]


def test_cross_module_call_resolves(graph):
    assert graph.fan_in("demo.core.entry") == 1
    assert graph.callers("demo.core.entry")[0].caller == "demo.cli.main"


def test_builtin_calls_never_resolve_to_repo_symbols(graph):
    assert "print" not in graph.by_qualname
    assert graph.resolution.out_of_scope["print"] >= 1


# -- the honest denominator ----------------------------------------------


def test_repo_rate_excludes_calls_that_could_never_bind(graph):
    """Builtins in the denominator make every repo look unmappable.

    The same denominator mistake that made grammar-constrained decoding look
    19 points worse than plain prompting in langchain-lab 01, when it was
    actually three times more accurate.
    """
    res = graph.resolution
    assert res.in_scope_calls < res.total_calls
    assert res.repo_resolution_rate > res.resolution_rate
    assert res.repo_resolution_rate == pytest.approx(1.0)


def test_in_scope_is_resolved_plus_missed(graph):
    res = graph.resolution
    assert res.in_scope_calls == res.resolved_count + sum(res.missed.values())


def test_every_unresolved_call_is_classified(graph):
    res = graph.resolution
    assert sum(res.unresolved.values()) == sum(res.out_of_scope.values()) + sum(
        res.missed.values()
    )


# -- ranking --------------------------------------------------------------


def test_pagerank_ranks_the_depended_upon_module_highest(graph):
    ranks = pagerank(graph)
    # util is imported by core, which is imported by cli.
    assert ranks["demo.util"] > ranks["demo.cli"]


def test_pagerank_is_a_distribution(graph):
    ranks = pagerank(graph)
    assert sum(ranks.values()) == pytest.approx(1.0, abs=1e-6)


def test_entry_points_are_uncalled_public_definitions(graph):
    names = {s.name for s in entry_points(graph)}
    assert "main" in names  # nothing in the repo calls it
    assert "helper" not in names  # core calls it


def test_most_called_is_ordered_by_fan_in(graph):
    ranked = most_called(graph)
    counts = [c for _, c in ranked]
    assert counts == sorted(counts, reverse=True)


# -- integration against a real repository --------------------------------


def test_call_through_a_package_reexport_resolves(tmp_path: Path):
    """`from pkg import clean` where `pkg/__init__.py` re-exports it.

    The definition lives in `pkg.normalise`, not `pkg`. Resolution must follow
    the re-export chain or every call through a package facade goes unbound.
    """
    write(tmp_path, "src/pkg/__init__.py", "from .normalise import clean")
    write(tmp_path, "src/pkg/normalise.py", "def clean(s):\n    return s")
    write(tmp_path, "src/pkg/app.py", """
        from pkg import clean

        def run(s):
            return clean(s)
    """)
    graph = build_graph(parse_repo(tmp_path))
    callers = graph.callers("pkg.normalise.clean")
    assert [c.caller for c in callers] == ["pkg.app.run"]
    assert graph.resolution.repo_resolution_rate == pytest.approx(1.0)


REAL_REPO = Path(r"D:\github\langchain-lab")


@pytest.mark.skipif(not REAL_REPO.exists(), reason="local checkout not present")
def test_real_repo_resolves_most_of_its_internal_calls():
    """Pure AST resolution should bind the large majority of repo-internal calls.

    No embeddings, no model. If this drops sharply, resolution has regressed.
    """
    graph = build_graph(parse_repo(REAL_REPO))
    assert graph.resolution.repo_resolution_rate > 0.85


@pytest.mark.skipif(not REAL_REPO.exists(), reason="local checkout not present")
def test_real_repo_core_module_is_the_shared_one():
    """langchain-lab's load-bearing module is its shared model registry."""
    graph = build_graph(parse_repo(REAL_REPO))
    ranks = pagerank(graph)
    top = max(ranks, key=ranks.get)
    assert top.startswith("shared.")
