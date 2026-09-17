"""Parser tests.

Fixtures are written to tmp_path rather than mocked, because the thing under
test is behaviour against real files on a real filesystem - including the files
that do not parse.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from cartographer.parse import (
    UNNAMEABLE,
    detect_internal_prefixes,
    find_nested_projects,
    iter_python_files,
    module_name_for,
    parse_repo,
)


def write(root: Path, rel: str, source: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    return p


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    write(tmp_path, "pyproject.toml", "[project]\nname='demo'\n")
    write(
        tmp_path,
        "src/demo/core.py",
        """
        import json
        from demo.util import helper

        CONST = 1

        class Engine:
            def run(self):
                return self.step()

            def step(self):
                return helper(CONST)

        def entry():
            return Engine().run()
        """,
    )
    write(
        tmp_path,
        "src/demo/util.py",
        """
        def helper(x):
            return "".join([str(x)])
        """,
    )
    return tmp_path


# -- module naming --------------------------------------------------------


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("src/demo/core.py", "demo.core"),
        ("demo/core.py", "demo.core"),
        ("lib/demo/core.py", "demo.core"),
        ("src/demo/__init__.py", "demo"),
        ("script.py", "script"),
    ],
)
def test_module_name_strips_src_and_init(tmp_path: Path, rel: str, expected: str):
    p = write(tmp_path, rel, "x = 1")
    assert module_name_for(p, tmp_path) == expected


# -- symbols --------------------------------------------------------------


def test_methods_are_distinguished_from_functions(repo: Path):
    parsed = parse_repo(repo)
    kinds = {s.qualname: s.kind for s in parsed.symbols}
    assert kinds["demo.core.Engine"] == "class"
    assert kinds["demo.core.Engine.run"] == "method"
    assert kinds["demo.core.entry"] == "function"


def test_nested_function_is_not_a_method(tmp_path: Path):
    write(tmp_path, "pkg/mod.py", """
        class C:
            def outer(self):
                def inner():
                    return 1
                return inner()
    """)
    parsed = parse_repo(tmp_path)
    kinds = {s.qualname: s.kind for s in parsed.symbols}
    assert kinds["pkg.mod.C.outer"] == "method"
    # `inner` sits inside a method but is a plain function, not another method.
    assert kinds["pkg.mod.C.outer.inner"] == "function"


def test_docstring_and_visibility_captured(repo: Path):
    parsed = parse_repo(repo)
    by_name = {s.qualname: s for s in parsed.symbols}
    assert by_name["demo.core.entry"].is_public is True
    assert by_name["demo.util.helper"].span == 2


# -- imports --------------------------------------------------------------


def test_internal_and_external_imports_are_separated(repo: Path):
    parsed = parse_repo(repo)
    core = next(m for m in parsed.modules if m.module == "demo.core")
    targets = {i.target: i.is_internal for i in core.imports}
    assert targets["demo.util"] is True
    assert targets["json"] is False


def test_relative_import_resolves_against_package(tmp_path: Path):
    write(tmp_path, "pkg/__init__.py", "")
    write(tmp_path, "pkg/a.py", "from .b import thing")
    write(tmp_path, "pkg/b.py", "def thing(): pass")
    parsed = parse_repo(tmp_path)
    a = next(m for m in parsed.modules if m.module == "pkg.a")
    edge = a.imports[0]
    assert edge.is_relative is True
    assert edge.target == "pkg.b"
    assert edge.is_internal is True


# -- calls ----------------------------------------------------------------


def test_call_receiver_without_a_name_is_marked_not_dropped(repo: Path):
    """`"".join(...)` must not be recorded as a call to a function named `join`.

    Recording the bare attribute made string methods look like unresolved calls
    to repo functions, which is the bug this test exists to prevent recurring.
    """
    parsed = parse_repo(repo)
    util = next(m for m in parsed.modules if m.module == "demo.util")
    names = {c.callee for c in util.calls}
    assert f"{UNNAMEABLE}.join" in names
    assert "join" not in names


def test_caller_is_the_enclosing_definition(repo: Path):
    parsed = parse_repo(repo)
    core = next(m for m in parsed.modules if m.module == "demo.core")
    step = [c for c in core.calls if c.callee == "helper"]
    assert step and step[0].caller == "demo.core.Engine.step"


# -- robustness -----------------------------------------------------------


def test_syntax_error_is_recorded_not_raised(tmp_path: Path):
    write(tmp_path, "good.py", "def f(): pass")
    write(tmp_path, "broken.py", "def f(:\n")
    parsed = parse_repo(tmp_path)
    errors = dict(parsed.parse_errors)
    assert "broken.py" in errors
    assert "syntax error" in errors["broken.py"]
    # The good file is still mapped.
    assert any(s.name == "f" for s in parsed.symbols)


def test_caches_and_venvs_are_skipped(tmp_path: Path):
    write(tmp_path, "real.py", "def keep(): pass")
    write(tmp_path, ".venv/lib/fake.py", "def drop(): pass")
    write(tmp_path, "__pycache__/cached.py", "def drop2(): pass")
    files, _ = iter_python_files(tmp_path)
    assert [f.name for f in files] == ["real.py"]


def test_vendored_project_is_excluded_and_reported(tmp_path: Path):
    """A checked-out dependency carries its own pyproject.toml.

    Without this, mapping mcp-lab returned 398 modules, most of them the
    `requests` library checked out by its SWE-bench project, rather than the
    52 modules mcp-lab actually contains.
    """
    write(tmp_path, "pyproject.toml", "[project]\nname='mine'\n")
    write(tmp_path, "mine/app.py", "def keep(): pass")
    write(tmp_path, "checkouts/other/pyproject.toml", "[project]\nname='other'\n")
    write(tmp_path, "checkouts/other/other/lib.py", "def drop(): pass")

    nested = find_nested_projects(tmp_path)
    assert [p.name for p in nested] == ["other"]

    parsed = parse_repo(tmp_path)
    assert {s.name for s in parsed.symbols} == {"keep"}
    assert parsed.excluded_projects == ["checkouts/other"]


def test_relative_import_inside_package_init_resolves_to_sibling(tmp_path: Path):
    """`from .mod import x` inside `pkg/__init__.py` targets `pkg.mod`.

    An `__init__.py` already *is* its package, so one fewer level is dropped
    than for an ordinary module. Dropping the full level sent urdu-nlp-toolkit's
    re-exports to a top-level `normalize` that does not exist, and took that
    repo's internal call resolution from 100% down to 24%.
    """
    write(tmp_path, "src/pkg/__init__.py", "from .normalise import clean")
    write(tmp_path, "src/pkg/normalise.py", "def clean(s): return s")
    write(tmp_path, "src/pkg/app.py", """
        from pkg import clean

        def run(s):
            return clean(s)
    """)
    parsed = parse_repo(tmp_path)
    init = next(m for m in parsed.modules if m.module == "pkg")
    assert init.imports[0].target == "pkg.normalise"


def test_relative_import_in_ordinary_module_targets_the_package(tmp_path: Path):
    """The counterpart: a non-init module drops the full level."""
    write(tmp_path, "src/pkg/__init__.py", "")
    write(tmp_path, "src/pkg/a.py", "from .b import thing")
    write(tmp_path, "src/pkg/b.py", "def thing(): pass")
    parsed = parse_repo(tmp_path)
    a = next(m for m in parsed.modules if m.module == "pkg.a")
    assert a.imports[0].target == "pkg.b"


def test_internal_prefixes_exclude_stdlib(repo: Path):
    files, _ = iter_python_files(repo)
    prefixes = detect_internal_prefixes(repo, files)
    assert "demo" in prefixes
    assert "json" not in prefixes
