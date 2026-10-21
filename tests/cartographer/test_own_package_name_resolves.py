"""`import mypkg` then `mypkg.get()` - a project calling itself by its public name.

This is how a library's own test suite is usually written: `import requests` at the top,
then `requests.get(...)` throughout. The call is into the repository, one re-export hop
from the definition, and the resolver already knew how to follow that hop.

What it could not survive was a second import in the same file. The index that maps a
locally-bound name to a module took the *head* of a dotted target as a binding for any
import, including a `from` import - so

    from requests.packages.urllib3.poolmanager import PoolManager

rebound `requests` to `requests.packages.urllib3.poolmanager` for the rest of the file,
and every `requests.get()` below it was looked up as
`requests.packages.urllib3.poolmanager.get`, which is nothing. A `from x.y import z`
statement binds `z`. It says nothing whatever about what `x` means.

Measured over the twelve pinned projects, fixing it moved requests from 35.9% to 54.8%
of in-scope calls, click 66.4% to 74.8%, flask 30.8% to 37.4% and attrs 52.9% to 58.8%.
The median moved only 59.9% to 60.2%, because the projects at the median do not import
themselves by name - which is the honest way to report it: this is a large fix for one
shape of codebase and no fix at all for another.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from reporecon.cartographer.graph import build_graph
from reporecon.cartographer.parse import parse_repo

PROJECT = '[project]\nname = "mypkg"\nversion = "0"\n'


def _graph(tmp_path: Path, files: dict[str, str]):
    for name, source in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
    return build_graph(parse_repo(tmp_path))


def _edges(graph) -> set[tuple[str, str]]:
    return {(edge.caller, edge.callee) for edges in graph.callers_of.values() for edge in edges}


TREE = {
    "pyproject.toml": PROJECT,
    "mypkg/__init__.py": "from .api import get\n",
    "mypkg/api.py": """
        def get(url):
            return url
    """,
    "mypkg/deep/__init__.py": "",
    "mypkg/deep/pool.py": """
        class PoolManager:
            pass
    """,
}


def test_a_call_through_the_projects_own_package_name_resolves(tmp_path: Path):
    graph = _graph(
        tmp_path,
        TREE
        | {
            "tests/test_a.py": """
            import mypkg


            def test_get():
                mypkg.get("/")
        """,
        },
    )
    assert ("tests.test_a.test_get", "mypkg.api.get") in _edges(graph)


def test_a_deep_from_import_does_not_rebind_the_package_name(tmp_path: Path):
    """The regression itself: the `from` import must not redefine `mypkg`.

    Both statements are present and in the order they appear in the real file, because
    the bug was an overwrite - with only the `import mypkg` line the old code passed.
    """
    graph = _graph(
        tmp_path,
        TREE
        | {
            "tests/test_a.py": """
            import mypkg
            from mypkg.deep.pool import PoolManager


            def test_get():
                PoolManager()
                mypkg.get("/")
        """,
        },
    )
    edges = _edges(graph)
    assert ("tests.test_a.test_get", "mypkg.api.get") in edges
    # And the `from` import still binds what it really does bind.
    assert ("tests.test_a.test_get", "mypkg.deep.pool.PoolManager") in edges


def test_a_from_import_alone_binds_no_package_name(tmp_path: Path):
    """The other direction, so the fix is not just a wider net.

    With no `import mypkg`, the name `mypkg` is not bound in this file at all - calling
    `mypkg.get()` there would be a NameError at runtime - so the resolver must not
    invent an edge for it. A test that only checks the first direction would pass just
    as well on code that binds every head name it ever sees.
    """
    graph = _graph(
        tmp_path,
        TREE
        | {
            "tests/test_a.py": """
            from mypkg.deep.pool import PoolManager


            def test_get():
                PoolManager()
                mypkg.get("/")
        """,
        },
    )
    edges = _edges(graph)
    assert ("tests.test_a.test_get", "mypkg.deep.pool.PoolManager") in edges
    assert ("tests.test_a.test_get", "mypkg.api.get") not in edges


def test_import_of_a_submodule_still_binds_its_head(tmp_path: Path):
    """`import mypkg.deep.pool` binds `mypkg`, and `mypkg.deep.pool.PoolManager()`
    has to keep working - that form is an `import`, not a `from` import."""
    graph = _graph(
        tmp_path,
        TREE
        | {
            "tests/test_a.py": """
            import mypkg.deep.pool


            def test_pool():
                mypkg.deep.pool.PoolManager()
        """,
        },
    )
    assert ("tests.test_a.test_pool", "mypkg.deep.pool.PoolManager") in _edges(graph)
