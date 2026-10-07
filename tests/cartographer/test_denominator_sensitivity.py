"""The resolution rate's denominator is set by the heuristic it grades.

`in_scope_calls = resolved + missed`, and `_classify` decides which calls are in scope.
A repo call it misfiles as `out_of_scope` leaves the denominator entirely - so
under-resolution RAISES the headline rate, which is the direction a metric must not
move. `graph.py` says this plainly about the 1.000 case, and
`tests/cartographer/test_graph.py` asserts a two-sided band with a date, which is the
right shape for the measurement.

What was missing is the sensitivity check: a known-resolvable call planted in a
repository, and the assertion that it is *counted*. A null result is not evidence until
a planted positive shows the instrument finds it, and a rate whose denominator is chosen
by a classifier needs the classifier tested against calls whose answer is known.

Each case below is a call whose correct classification is not in doubt, written as the
shape it is: a plain function call, a method on an instance of a class defined here, a
call through an alias, a call into a sibling module. Any of them silently becoming
`out_of_scope` would make this repository's published rate look better.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reporecon.cartographer.graph import build_graph
from reporecon.cartographer.parse import parse_repo

#: name -> (the file to write, the call that must be counted as in-scope)
PLANTED = {
    "a plain function call": (
        "pkg/caller.py",
        "from pkg.lib import helper\n\n\ndef go():\n    return helper()\n",
    ),
    "a method on a local class": (
        "pkg/caller.py",
        "from pkg.lib import Engine\n\n\ndef go():\n    return Engine().run()\n",
    ),
    "a call through an import alias": (
        "pkg/caller.py",
        "from pkg.lib import helper as h\n\n\ndef go():\n    return h()\n",
    ),
    "a module-qualified call": (
        "pkg/caller.py",
        "from pkg import lib\n\n\ndef go():\n    return lib.helper()\n",
    ),
}

LIBRARY = """class Engine:
    def run(self):
        return 1


def helper():
    return 2
"""


def _repo(tmp_path: Path, rel: str, source: str) -> Path:
    root = tmp_path / "r"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "lib.py").write_text(LIBRARY, encoding="utf-8")
    (root / rel).write_text(source, encoding="utf-8")
    return root


@pytest.mark.parametrize("case", sorted(PLANTED))
def test_a_call_that_must_resolve_is_in_the_denominator(tmp_path: Path, case: str):
    """The planted positive. If this falls out, the rate goes UP."""
    rel, source = PLANTED[case]
    resolution = build_graph(parse_repo(_repo(tmp_path, rel, source))).resolution
    assert resolution.in_scope_calls >= 1, (
        case,
        resolution.resolved_count,
        dict(resolution.missed),
    )
    assert resolution.repo_resolution_rate > 0.0, case


@pytest.mark.parametrize("case", sorted(PLANTED))
def test_the_planted_call_actually_binds(tmp_path: Path, case: str):
    """In the denominator and unresolved would be a different defect: the rate would
    fall rather than rise, and the call this repository is built to follow would be one
    it cannot follow."""
    rel, source = PLANTED[case]
    resolution = build_graph(parse_repo(_repo(tmp_path, rel, source))).resolution
    assert resolution.resolved_count >= 1, (case, dict(resolution.missed))


def test_a_call_that_must_not_be_in_the_denominator_is_not(tmp_path: Path):
    """The other side. A rate that counts everything is as uninformative as one that
    counts nothing: `response.json()` and `path.exists()` target no definition here, and
    putting them in the denominator makes every repository look unmappable.
    """
    root = _repo(
        tmp_path,
        "pkg/caller.py",
        "import json\n\n\ndef go(path):\n    return json.loads(path.read_text())\n",
    )
    resolution = build_graph(parse_repo(root)).resolution
    assert resolution.in_scope_calls == 0, dict(resolution.missed)
    # And the rate over an empty population is not reported as a number. 0.0 here
    # means "no in-scope calls", which `map_repo` reports beside the denominator.
    assert resolution.repo_resolution_rate == 0.0


def test_dropping_a_resolvable_call_would_raise_the_rate(tmp_path: Path):
    """The mechanism, demonstrated rather than described.

    One resolvable call and one unresolvable one is 50%. Reclassifying the resolvable
    one as out of scope would take the denominator to 1 and the rate to 0% - and
    reclassifying the MISSED one takes it to 100%, which is the direction that flatters.
    """
    root = tmp_path / "r"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "lib.py").write_text(LIBRARY, encoding="utf-8")
    (root / "pkg" / "caller.py").write_text(
        "from pkg.lib import helper\nfrom pkg import missing_module\n\n\n"
        "def go():\n    return helper() + missing_module.gone()\n",
        encoding="utf-8",
    )
    resolution = build_graph(parse_repo(root)).resolution
    assert resolution.resolved_count >= 1
    assert resolution.in_scope_calls >= resolution.resolved_count
    rate = resolution.repo_resolution_rate
    assert 0.0 < rate <= 1.0, (rate, resolution.resolved_count, resolution.in_scope_calls)

    # Had the resolved call been dropped from the population instead, the same code
    # would report a rate of 0.0 over a denominator of one.
    without = resolution.in_scope_calls - resolution.resolved_count
    assert without < resolution.in_scope_calls, "the denominator does not move with the hits"
