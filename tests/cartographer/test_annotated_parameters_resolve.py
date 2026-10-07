"""`def run(server: ReconServer)` - the annotation names the type, and it was ignored.

The resolver does one kind of type inference: `engine = Engine()` binds `engine` to
`Engine`, so `engine.run()` can be resolved. An annotated ASSIGNMENT was already a
binding - `visit_AnnAssign` - and an annotated PARAMETER was not, so a function handed
an object instead of constructing one resolved nothing on it.

`demo.py` is written that way, because it is: `def _run(server: ReconServer)`, and then
38 calls to `server.map_repo(...)`, `server.review_diff(...)` and the rest. All 38 went
unresolved, which put every one of this server's six tool methods at the top of
`entry_points` under the heading *"Called by nothing in this repository: a CLI, a test
helper nobody uses, or dead code"* - a list that is the first thing `map_repo` tells a
newcomer, false at every one of its top rows.

An independent reviewer found it by reading the list and then grepping for the names.
`repo_resolution_rate` went from 0.8904 to 0.9391 when the annotation started counting,
and the honest denominator had been honest throughout: those 38 were classified `missed`
rather than excluded, so the rate was never inflated by them.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from reporecon.cartographer.graph import build_graph, entry_points
from reporecon.cartographer.parse import parse_repo


def _graph(tmp_path: Path, files: dict[str, str]):
    for name, source in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
    return build_graph(parse_repo(tmp_path))


LIBRARY = """
    class Engine:
        def run(self):
            return 1

        def stop(self):
            return 2
"""


def test_a_call_on_an_annotated_parameter_resolves(tmp_path: Path):
    graph = _graph(
        tmp_path,
        {
            "lib.py": LIBRARY,
            "app.py": """
                from lib import Engine


                def drive(engine: Engine) -> int:
                    return engine.run()
            """,
        },
    )
    assert graph.resolution.resolved_count >= 1, graph.resolution
    named = {s.qualname for s in entry_points(graph)}
    assert "lib.Engine.run" not in named, sorted(named)


def test_an_unannotated_parameter_still_does_not_resolve(tmp_path: Path):
    """The other direction, or the test above could be passing for another reason.

    Nothing says what `engine` is here, so the call is unresolvable and `run` has no
    static caller. That is the honest answer and it is why the annotation matters.
    """
    graph = _graph(
        tmp_path,
        {
            "lib.py": LIBRARY,
            "app.py": """
                def drive(engine):
                    return engine.run()
            """,
        },
    )
    named = {s.qualname for s in entry_points(graph)}
    assert "lib.Engine.run" in named, sorted(named)


@pytest.mark.parametrize(
    "signature",
    [
        "def drive(engine: Engine) -> int:",
        "def drive(*, engine: Engine) -> int:",
        "def drive(first, /, engine: Engine) -> int:",
        "def drive(engine: 'Engine') -> int:",
    ],
)
def test_every_parameter_kind_is_bound(signature: str, tmp_path: Path):
    """Positional, keyword-only, positional-only and a string annotation. A binding
    that covers one calling convention and not the others is a resolver whose answer
    depends on how the function was declared."""
    graph = _graph(
        tmp_path,
        {
            "lib.py": LIBRARY,
            "app.py": f"""
                from lib import Engine


                {signature}
                    return engine.run()
            """,
        },
    )
    named = {s.qualname for s in entry_points(graph)}
    assert "lib.Engine.run" not in named, (signature, sorted(named))


def test_the_annotation_binds_in_the_function_it_is_declared_on(tmp_path: Path):
    """Scope, which is where the first version of this got it wrong: the binding was
    keyed on the bare scope path while the resolver looks it up by the same string
    `_current_caller` builds - module first. The symptom was that nothing changed."""
    graph = _graph(
        tmp_path,
        {
            "lib.py": LIBRARY,
            "app.py": """
                from lib import Engine


                def drive(engine: Engine) -> int:
                    return engine.run()


                def elsewhere(engine) -> int:
                    return engine.stop()
            """,
        },
    )
    named = {s.qualname for s in entry_points(graph)}
    # `run` is called on an annotated parameter and resolves; `stop` is called on an
    # unannotated one in a different function and does not.
    assert "lib.Engine.run" not in named, sorted(named)
    assert "lib.Engine.stop" in named, sorted(named)


def test_this_repositorys_own_tool_methods_have_callers(tmp_path: Path):
    """End to end over this checkout, which is where the finding came from."""
    graph = build_graph(parse_repo(Path(".")))
    named = {s.qualname for s in entry_points(graph)}
    for tool in ("map_repo", "review_diff", "release_readiness", "riskiest_commits"):
        assert f"reporecon.mcp_server.ReconServer.{tool}" not in named, (
            f"{tool} is listed as called by nothing, and demo.py calls it by name"
        )
    assert graph.resolution.repo_resolution_rate > 0.90, graph.resolution.repo_resolution_rate


def test_a_property_is_not_an_entry_point(tmp_path: Path):
    """A `@property` is READ, and an attribute read is not a Call node, so a call graph
    structurally cannot see a single one of its uses.

    Six of them filled the top of the entry-point list once the annotated parameters
    started resolving - `History.caveat`, `GateResult.not_measured`,
    `Resolution.repo_resolution_rate` - every one read in several places. "Called by
    nothing in this repository" is not what a reader should be told about those.
    """
    graph = _graph(
        tmp_path,
        {
            "lib.py": """
                class Thing:
                    @property
                    def size(self) -> int:
                        return 1

                    def grow(self) -> int:
                        return 2
            """,
        },
    )
    named = {s.qualname for s in entry_points(graph)}
    assert "lib.Thing.size" not in named, sorted(named)
    # And the ordinary method beside it still is, so this is not excluding everything.
    assert "lib.Thing.grow" in named, sorted(named)


def test_a_visitor_method_is_not_an_entry_point(tmp_path: Path):
    """`ast.NodeVisitor.generic_visit` dispatches `visit_<NodeType>` by name, so these
    are the same case as a pytest test: reached from outside, heavily used, and with no
    static caller. They filled this list immediately after the properties."""
    graph = _graph(
        tmp_path,
        {
            "lib.py": """
                import ast


                class V(ast.NodeVisitor):
                    def visit_Call(self, node):
                        return node

                    def helper(self):
                        return 1
            """,
        },
    )
    named = {s.qualname for s in entry_points(graph)}
    assert "lib.V.visit_Call" not in named, sorted(named)
    assert "lib.V.helper" in named, sorted(named)
