"""Tests for the propose-then-disprove pipeline.

Most assertions are about *retraction*. Confirming a real bug is the easy
half; the half that decides whether anyone leaves the bot enabled is refusing
to post the plausible-looking ones.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from reviewbot.checks import propose
from reviewbot.review import added_lines, render, review_diff, review_source
from reviewbot.verify import FileContext, verify


def src(text: str) -> str:
    return textwrap.dedent(text).lstrip()


def rules_of(code: str) -> list[str]:
    return [p.rule for p in propose(src(code))]


def review(code: str, path: str = "app.py"):
    return review_source(path, src(code))


def confirmed_rules(code: str, path: str = "app.py") -> list[str]:
    return [v.rule for v in review(code, path).confirmed]


def retracted_reasons(code: str, path: str = "app.py") -> list[str]:
    return [v.reason for v in review(code, path).retracted]


# -- proposers fire -------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "rule"),
    [
        ("def f(a=[]):\n    a.append(1)\n", "mutable-default"),
        ("def f(a=dict()):\n    a['x'] = 1\n", "mutable-default"),
        ("try:\n    pass\nexcept:\n    pass\n", "bare-except"),
        ("try:\n    pass\nexcept Exception:\n    pass\n", "swallowed-exception"),
        ("if x == None:\n    pass\n", "eq-none"),
        ("if x == True:\n    pass\n", "eq-bool"),
        ("f = open('x')\n", "open-without-with"),
        ("assert x\n", "assert-in-source"),
        ("for i in items:\n    items.append(i)\n", "mutate-while-iterating"),
    ],
)
def test_proposers_fire(code: str, rule: str):
    assert rule in rules_of(code)


def test_an_unparseable_file_proposes_nothing():
    assert propose("def f(:\n") == []


# -- verifiers defeat the plausible ones ---------------------------------


def test_a_mutable_default_that_is_never_mutated_is_retracted():
    """Sharing a default between calls is only observable if it is mutated."""
    code = """
        def f(a=[]):
            return len(a)
    """
    assert "mutable-default" in rules_of(code)
    assert "mutable-default" not in confirmed_rules(code)


def test_a_mutable_default_that_is_mutated_is_confirmed():
    code = """
        def f(a=[]):
            a.append(1)
            return a
    """
    assert "mutable-default" in confirmed_rules(code)


def test_a_bare_except_that_reraises_is_retracted():
    code = """
        try:
            work()
        except:
            cleanup()
            raise
    """
    assert "bare-except" in rules_of(code)
    assert "bare-except" not in confirmed_rules(code)


def test_a_bare_except_that_swallows_is_confirmed():
    code = """
        try:
            work()
        except:
            pass
    """
    assert "bare-except" in confirmed_rules(code)


def test_open_inside_a_with_statement_is_retracted():
    code = "with open('x') as fh:\n    fh.read()\n"
    assert "open-without-with" in rules_of(code)
    assert "open-without-with" not in confirmed_rules(code)


def test_open_consumed_immediately_is_retracted():
    code = "text = open('x').read()\n"
    assert "open-without-with" not in confirmed_rules(code)


def test_a_retained_handle_is_confirmed():
    code = "fh = open('x')\nuse(fh)\n"
    assert "open-without-with" in confirmed_rules(code)


def test_eq_none_is_retracted_in_a_query_dsl():
    """SQLAlchemy overloads __eq__; `is None` would be wrong there."""
    code = """
        from sqlalchemy import select

        q = select(User).where(User.deleted_at == None)
    """
    assert "eq-none" in rules_of(code)
    assert "eq-none" not in confirmed_rules(code)


def test_eq_none_is_confirmed_in_plain_python():
    code = "if value == None:\n    pass\n"
    assert "eq-none" in confirmed_rules(code)


def test_asserts_are_retracted_in_test_files():
    assert "assert-in-source" not in confirmed_rules("assert x == 1\n", "tests/test_a.py")


def test_asserts_are_confirmed_in_source_files():
    assert "assert-in-source" in confirmed_rules("assert x == 1\n", "src/app.py")


@pytest.mark.parametrize("marker", ["noqa", "nosec", "intentional", "deliberate"])
def test_an_acknowledged_finding_is_retracted(marker: str):
    """Re-raising something the author already marked is what gets a bot muted."""
    code = f"fh = open('x')  # {marker}\n"
    assert not review(code).confirmed


def test_a_swallowed_exception_with_an_explanation_is_retracted():
    code = """
        try:
            optional_step()
        except Exception:  # best effort, failure here is fine
            pass
    """
    assert "swallowed-exception" not in confirmed_rules(code)


def test_every_retraction_states_a_reason():
    code = "with open('x') as fh:\n    fh.read()\n"
    reasons = retracted_reasons(code)
    assert reasons and all(r.strip() for r in reasons)


def test_a_confirmed_verdict_says_no_defeater_applied():
    context = FileContext.build("app.py", "fh = open('x')\nuse(fh)\n")
    proposal = propose("fh = open('x')\nuse(fh)\n")[0]
    verdict = verify(context, proposal)
    assert verdict.confirmed
    assert verdict.reason == "no defeater applied"


# -- diff scoping ---------------------------------------------------------


DIFF = textwrap.dedent("""
    diff --git a/app.py b/app.py
    --- a/app.py
    +++ b/app.py
    @@ -1,0 +2,1 @@
    +fh = open('new.txt')
""").lstrip()


def test_added_lines_are_parsed():
    assert added_lines(DIFF) == {"app.py": {2}}


def test_a_deleted_file_contributes_no_lines():
    diff = "--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x = 1\n"
    assert added_lines(diff) == {}


def test_findings_outside_the_diff_are_not_reported():
    """A reviewer that comments on untouched code is auditing, not reviewing."""
    code = src("""
        old = open('a')
        use(old)
        new = open('b')
        use(new)
    """)
    only_line_three = review_source("app.py", code, changed_lines={3})
    lines = [v.proposal.line for v in only_line_three.verdicts]
    assert lines == [3]


def test_verification_still_reads_the_whole_file():
    """The defeater lives outside the changed hunk, and must still be found."""
    code = src("""
        from sqlalchemy import select
        q = select(User).where(User.x == None)
    """)
    result = review_source("q.py", code, changed_lines={2})
    assert result.verdicts
    assert not result.confirmed  # the sqlalchemy import on line 1 defeated it


# -- aggregate reporting --------------------------------------------------


def test_retraction_rate_is_reported():
    code = src("""
        with open('a') as fh:
            fh.read()
        kept = open('b')
        use(kept)
    """)
    result = review_source("app.py", code)
    from reviewbot.review import Review

    review_all = Review(files=[result])
    assert review_all.proposed == 2
    assert review_all.confirmed == 1
    assert review_all.retraction_rate == 0.5


def test_render_names_the_retraction_rate():
    from reviewbot.review import Review

    text = render(Review(files=[review_source("app.py", "fh = open('x')\nuse(fh)\n")]))
    assert "retracted" in text


def test_an_empty_review_does_not_divide_by_zero():
    from reviewbot.review import Review

    assert Review().retraction_rate == 0.0


# -- against a real repository -------------------------------------------


@pytest.mark.slow
def test_review_diff_runs_against_a_real_commit(tmp_path: Path):
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)

    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "one"], check=True)

    (repo / "app.py").write_text("x = 1\nfh = open('y')\nuse(fh)\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "two"], check=True)

    result = review_diff(repo, "HEAD~1")
    assert result.confirmed == 1
