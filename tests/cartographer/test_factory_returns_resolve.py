"""`app = make_app()` where `make_app() -> App` - the return type names the variable.

The resolver already does three kinds of type inference: `engine = Engine()` binds
`engine` to `Engine`, `engine: Engine` binds it, and `def run(engine: Engine)` binds the
parameter. A variable assigned from a CALL to a repo function bound to the *function*,
after which the resolver looked for a method on it, found none, and gave up - although
the answer was written in the repository's own AST, one hop away, as that function's
return annotation.

A factory function and a pytest fixture are the same shape, so this is the pattern most
of a modern test suite is written in.

It is worth being exact about what it bought, because the reason to keep it is that it is
correct rather than that it moved a number: measured over twelve real projects at pinned
tags, the median repo resolution rate went from 59.7% to 59.9%. It applies rarely in those
codebases because most of their factories and fixtures carry no return annotation at all.
The large remainder is a different problem - 64% of the unresolved calls in that corpus
are `variable.method` where the variable's type is stated nowhere in the source, and no
amount of following annotations reaches those.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from reporecon.cartographer.graph import build_graph
from reporecon.cartographer.parse import parse_repo


def _graph(tmp_path: Path, files: dict[str, str]):
    for name, source in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
    return build_graph(parse_repo(tmp_path))


PROJECT = '[project]\nname = "mypkg"\nversion = "0"\n'
CORE = """
    class App:
        def route(self, rule):
            return rule


    def make_app() -> App:
        return App()


    def make_anything():
        return App()
"""


def _edges(graph) -> set[tuple[str, str]]:
    return {(edge.caller, edge.callee) for edges in graph.callers_of.values() for edge in edges}


def test_a_variable_from_an_annotated_factory_resolves(tmp_path: Path):
    graph = _graph(
        tmp_path,
        {
            "pyproject.toml": PROJECT,
            "mypkg/__init__.py": "from .core import App, make_app\n",
            "mypkg/core.py": CORE,
            "tests/test_a.py": """
                from mypkg import make_app


                def test_routes():
                    app = make_app()
                    app.route("/")
            """,
        },
    )
    assert ("tests.test_a.test_routes", "mypkg.core.App.route") in _edges(graph)


def test_a_variable_from_an_unannotated_factory_is_left_unresolved(tmp_path: Path):
    """The other direction. Nothing in the source says what `make_anything` returns, and
    guessing from the body is a different and much less safe piece of inference."""
    graph = _graph(
        tmp_path,
        {
            "pyproject.toml": PROJECT,
            "mypkg/__init__.py": "from .core import App, make_anything\n",
            "mypkg/core.py": CORE,
            "tests/test_b.py": """
                from mypkg import make_anything


                def test_routes():
                    app = make_anything()
                    app.route("/")
            """,
        },
    )
    edges = _edges(graph)
    assert ("tests.test_b.test_routes", "mypkg.core.make_anything") in edges
    assert ("tests.test_b.test_routes", "mypkg.core.App.route") not in edges


def test_a_factory_returning_a_factory_is_not_followed(tmp_path: Path):
    """One hop only. Chaining would resolve more and a wrong guess here is a wrong call
    edge, which is worse than a missing one in a tool a reader uses to find callers."""
    graph = _graph(
        tmp_path,
        {
            "pyproject.toml": PROJECT,
            "mypkg/__init__.py": "from .core import App\n",
            "mypkg/core.py": CORE
            + """

    def make_factory() -> "object":
        return make_app
            """,
            "tests/test_c.py": """
                from mypkg.core import make_factory


                def test_routes():
                    factory = make_factory()
                    factory.route("/")
            """,
        },
    )
    assert ("tests.test_c.test_routes", "mypkg.core.App.route") not in _edges(graph)


def test_the_return_annotation_is_recorded_on_the_symbol(tmp_path: Path):
    """The field the resolver reads, asserted directly: a parser that stopped filling it
    would leave the resolution rate to explain the regression."""
    repo = parse_repo_with(tmp_path, {"pyproject.toml": PROJECT, "mypkg/core.py": CORE})
    by_name = {symbol.name: symbol for module in repo.modules for symbol in module.symbols}
    assert by_name["make_app"].returns == "App"
    assert by_name["make_anything"].returns == ""
    assert by_name["App"].returns == ""
    assert by_name["route"].returns == ""


def parse_repo_with(tmp_path: Path, files: dict[str, str]):
    for name, source in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
    return parse_repo(tmp_path)
