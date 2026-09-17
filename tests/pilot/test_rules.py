"""Rule and rewrite tests.

The properties that matter are safety properties, so most of these assert what
the tool *refuses* to do rather than what it does.
"""

from __future__ import annotations

import ast
import textwrap

import pytest

from pilot.apply import apply_edits, modernise, modernise_until_stable
from pilot.rules import BEHAVIOURAL, MECHANICAL, RULE_KIND, scan_source


def scan(source: str, rules=None):
    return scan_source("t.py", textwrap.dedent(source).lstrip(), rules)


def run(source: str):
    text = textwrap.dedent(source).lstrip()
    return modernise_until_stable("t.py", text)[0]


# -- mechanical rewrites --------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("x: List[int]", "x: list[int]"),
        ("x: Dict[str, int]", "x: dict[str, int]"),
        ("x: Tuple[int, ...]", "x: tuple[int, ...]"),
        ("x: Set[str]", "x: set[str]"),
        ("x: Type[int]", "x: type[int]"),
        ("x: Optional[str]", "x: str | None"),
        ("x: Union[int, str]", "x: int | str"),
        ("x: typing.List[int]", "x: list[int]"),
        ("x: typing.Optional[int]", "x: int | None"),
    ],
)
def test_annotations_are_modernised(before: str, after: str):
    assert run(before + "\n").strip() == after


def test_nested_generics_unwind_completely():
    assert run("x: Dict[str, List[Optional[int]]]\n").strip() == "x: dict[str, list[int | None]]"


def test_surrounding_formatting_is_preserved():
    """Round-tripping through ast.unparse would reformat the whole file.

    A two-line change becoming a whole-file diff is the reason codemods get
    rejected in review.
    """
    source = """
        import typing


        def f(
            a: Optional[str],   # keep this comment
            b: int = 3,
        ) -> List[int]:
            '''Docstring stays.'''
            return [1,  2]
    """
    result = run(source)
    assert "# keep this comment" in result
    assert "'''Docstring stays.'''" in result
    assert "return [1,  2]" in result  # odd spacing untouched
    assert "a: str | None" in result
    assert "-> list[int]" in result


def test_unrelated_names_are_left_alone():
    """A local class called `List` is not typing.List."""
    source = "class List:\n    pass\n\nx = List()\n"
    assert run(source) == source


def test_a_file_with_nothing_to_do_is_unchanged():
    source = "def f(a: int) -> str:\n    return str(a)\n"
    assert run(source) == source


# -- behavioural rules are found but never applied -----------------------


@pytest.mark.parametrize(
    "source",
    [
        "import datetime\nx = datetime.datetime.utcnow()\n",
        "from datetime import datetime\nx = datetime.utcnow()\n",
    ],
)
def test_utcnow_is_reported(source: str):
    found = scan(source)
    assert [e.rule for e in found.behavioural] == ["utcnow-deprecated"]


def test_utcnow_is_not_rewritten_by_apply():
    """The documented fix changes naive to aware; see scripts/prove_utcnow.py."""
    source = "from datetime import datetime\nx = datetime.utcnow()\n"
    assert run(source) == source


def test_behavioural_edits_carry_an_explanation():
    for edit in scan("from datetime import datetime\nx = datetime.utcnow()\n").behavioural:
        assert "naive" in edit.note.lower() or "timezone" in edit.note.lower()


def test_every_rule_is_classified():
    assert set(RULE_KIND.values()) == {MECHANICAL, BEHAVIOURAL}


def test_naive_now_is_behavioural_not_mechanical():
    found = scan("from datetime import datetime\nx = datetime.now()\n")
    assert [e.kind for e in found.edits] == [BEHAVIOURAL]


# -- safety ---------------------------------------------------------------


def test_a_rewrite_that_would_not_parse_is_discarded():
    from pilot.rules import Edit

    source = "x: List[int]\n"
    broken = Edit(1, 3, 1, 12, "list[[[", "bogus", MECHANICAL)
    rewrite = apply_edits("t.py", source, [broken])
    assert rewrite.rejected is not None
    assert rewrite.result == source


def test_a_mechanical_rewrite_that_changes_code_is_discarded():
    """The equivalence check exists so a buggy rule cannot corrupt a file."""
    from pilot.rules import Edit

    source = "x: List[int] = [1]\nprint(x)\n"
    sabotage = Edit(2, 0, 2, 8, "print(0)", "bogus", MECHANICAL)
    rewrite = apply_edits("t.py", source, [sabotage])
    assert rewrite.rejected is not None
    assert "executable code" in rewrite.rejected
    assert rewrite.result == source


def test_an_unparseable_file_is_reported_not_rewritten():
    rewrite = modernise("t.py", "def f(:\n")
    assert rewrite.rejected is not None
    assert rewrite.result == "def f(:\n"


def test_overlapping_edits_keep_the_innermost_and_report_the_rest():
    found = scan("x: Dict[str, List[int]]\n")
    rewrite = apply_edits("t.py", "x: Dict[str, List[int]]\n", found.mechanical)
    assert rewrite.changed
    assert rewrite.skipped  # the outer edit waits for the next pass
    ast.parse(rewrite.result)


def test_repeated_passes_terminate():
    text, applied, rejections = modernise_until_stable("t.py", "x: int\n")
    assert applied == []
    assert rejections == []
    assert text == "x: int\n"


def test_every_rewritten_file_still_parses():
    source = textwrap.dedent("""
        from typing import Dict, List, Optional, Union

        def f(a: Optional[str], b: Dict[str, List[int]]) -> Union[int, str]:
            return len(b) if a else ""
    """).lstrip()
    ast.parse(modernise_until_stable("t.py", source)[0])
