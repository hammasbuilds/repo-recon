"""A suppression marker for one thing used to suppress everything.

`_acknowledged` substring-matched `("noqa", "nosec", "intentional", "deliberate", ...)`
against any comment within plus or minus one line, before any per-rule defeater, for
every rule. Measured by an independent reviewer:

    plain mutable default (real bug)          proposed=1 confirmed=1 retracted=0
    same bug + an unrelated  # noqa: E501     proposed=1 confirmed=0 retracted=1
    same bug, comment on the NEXT line        proposed=1 confirmed=0 retracted=1
    bare except + an unrelated  # nosec B110  proposed=1 confirmed=0 retracted=1

A ruff line-length suppression deleted a shared-mutable-default finding. Three errors:
the directive's code was never read, a comment BELOW a statement was treated as
acknowledging it, and the retraction reason never reached the caller - the payload was
`{"proposed": 1, "confirmed": 0, "retracted": 1, "findings": []}`.

A coded directive now suppresses only where the code belongs to the rule; a bare one
still suppresses anything, because that is what a bare one means; the line below is not
read; and retractions are returned with their reasons and counted apart from disproofs,
since an acknowledgement is not a disproof and the module's docstring calls the
retraction rate "the number this project is built to measure".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reporecon.mcp_server import ReconServer

#: A shared mutable default that IS mutated, so every per-rule defeater fails and the
#: only thing that can retract it is an acknowledgement. That is what makes it the probe.
BUG = "def f(xs=[]):{comment}\n    xs.append(1)\n    return xs\n"

CONFIRMED = "confirmed"
RETRACTED = "retracted"

CASES: dict[str, tuple[str, str]] = {
    # (source, expected outcome)
    "no comment at all": (BUG.format(comment=""), CONFIRMED),
    # The case that found this. E501 is line length.
    "an unrelated coded noqa": (BUG.format(comment="  # noqa: E501"), CONFIRMED),
    "an unrelated coded noqa, several codes": (
        BUG.format(comment="  # noqa: E501,F401,I001"),
        CONFIRMED,
    ),
    # B110 is try-except-pass: it belongs to `swallowed-exception`, not here.
    "a coded nosec for another rule": (BUG.format(comment="  # nosec B110"), CONFIRMED),
    # A bare directive means "everything on this line".
    "a bare noqa": (BUG.format(comment="  # noqa"), RETRACTED),
    "a bare nosec": (BUG.format(comment="  # nosec"), RETRACTED),
    # B006 is the mutable-default check in flake8-bugbear.
    "the matching code": (BUG.format(comment="  # noqa: B006"), RETRACTED),
    "the matching code among others": (
        BUG.format(comment="  # noqa: E501,B006"),
        RETRACTED,
    ),
    # Prose is about the code, not about a checker.
    "prose on the same line": (BUG.format(comment="  # deliberate"), RETRACTED),
    "prose on the line above": ("# deliberate\n" + BUG.format(comment=""), RETRACTED),
    # And the direction. A comment after a statement does not acknowledge it.
    "prose on the line below": (
        "def f(xs=[]):\n    # deliberate\n    xs.append(1)\n    return xs\n",
        CONFIRMED,
    ),
    "a bare noqa on the line below": (
        "def f(xs=[]):\n    # noqa\n    xs.append(1)\n    return xs\n",
        CONFIRMED,
    ),
}


@pytest.fixture(scope="module")
def server() -> ReconServer:
    return ReconServer()


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_marker_suppresses_only_what_it_is_about(
    case: str, server: ReconServer, tmp_path_factory
):
    source, expected = CASES[case]
    path = tmp_path_factory.mktemp("sup") / "m.py"
    path.write_text(source, encoding="utf-8")
    result = server.review_diff({"path": str(path)})

    assert result["proposed"] == 1, (case, result)
    if expected == CONFIRMED:
        assert result["confirmed"] == 1, (case, result["retractions"])
        assert result["retracted"] == 0, (case, result["retractions"])
    else:
        assert result["confirmed"] == 0, (case, result["findings"])
        assert result["retracted"] == 1, (case, result)


def test_the_bare_and_coded_cases_really_do_differ(server: ReconServer, tmp_path: Path):
    """Both directions over one rule, or the parametrised test above could be passing
    for a reason unrelated to the code."""
    outcomes = {}
    for label, comment in (
        ("bare", "  # noqa"),
        ("mine", "  # noqa: B006"),
        ("other", "  # noqa: E501"),
    ):
        path = tmp_path / f"{label}.py"
        path.write_text(BUG.format(comment=comment), encoding="utf-8")
        outcomes[label] = server.review_diff({"path": str(path)})["confirmed"]
    assert outcomes == {"bare": 0, "mine": 0, "other": 1}, outcomes


def test_an_acknowledgement_is_reported_as_one_and_not_as_a_disproof(
    server: ReconServer, tmp_path: Path
):
    """The counts a caller reads. `retracted` used to be one number covering both, and
    the module measures its retraction rate with it."""
    path = tmp_path / "m.py"
    path.write_text(BUG.format(comment="  # deliberate"), encoding="utf-8")
    result = server.review_diff({"path": str(path)})
    assert result["retracted"] == 1
    assert result["acknowledged"] == 1
    assert result["disproved"] == 0
    assert result["retractions"], result
    row = result["retractions"][0]
    assert row["acknowledged"] is True
    assert "deliberate" in row["reason"]
    assert row["rule"] == "mutable-default"
    assert row["line"] == 1, row


def test_a_defeater_retraction_is_reported_as_a_disproof(
    server: ReconServer, tmp_path: Path
):
    """The other kind, which is the one the project is built to measure: a mutable
    default the function never mutates is not a bug, and nothing is marked."""
    path = tmp_path / "m.py"
    path.write_text("def f(xs=[]):\n    return len(xs)\n", encoding="utf-8")
    result = server.review_diff({"path": str(path)})
    assert result["proposed"] == 1, result
    assert result["retracted"] == 1
    assert result["disproved"] == 1, result["retractions"]
    assert result["acknowledged"] == 0
    row = result["retractions"][0]
    assert row["acknowledged"] is False
    assert "deliberate" not in row["reason"]
    assert row["reason"], "a retraction with no reason is a count"


def test_every_rule_has_a_code_map_entry():
    """A rule with no entry can never be suppressed by a coded directive, which is a
    quiet asymmetry rather than a safe default - the author writes the code their
    linter printed and nothing happens."""
    from reporecon.reviewbot.verify import _DEFEATERS, _DIRECTIVE_CODES

    assert set(_DIRECTIVE_CODES) == set(_DEFEATERS), (
        sorted(set(_DEFEATERS) ^ set(_DIRECTIVE_CODES))
    )
    for rule, codes in _DIRECTIVE_CODES.items():
        assert codes, rule
        for code in codes:
            assert code == code.lower(), (rule, code)
