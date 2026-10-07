"""The riskiest-commit comparison, over releases that cannot support one.

The first version of this check compared the top commit's score against a fixed 45. The
second compared it against the median of the release and guarded that with a minimum
COUNT of three commits. The defect both times is the same one and it is about dispersion:
`_log_scale` returns 0.5 at the median, so in a homogeneous history volume and files
carry no information and every commit scores the same. A count does not detect that.

An independent reviewer ran four releases through the second version:

    10 identical 1-line commits      36.0 against a median of 36.0 (1.0x)  pass
    10 identical 1000-line commits   36.0 against a median of 36.0 (1.0x)  pass
    50 identical realistic commits   28.5 against a median of 28.5 (1.0x)  pass
    the three-commit fixture         28.7 against a median of 28.5 (1.01x)

all four under "4 of 4 checks measured something", and a fifth where the median is 0.0
and the detail read "28.5 against a median of 0.0 for this release (1.0x)" with a clean
GO. Each of those is a case here.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from reporecon.captain.gate import NOT_MEASURED, evaluate
from reporecon.captain.history import Commit, FileChange, History


def _commit(n: int, lines: int, files: int, *, tested: bool = True) -> Commit:
    changed = [FileChange(path=f"src/m{i}.py", added=lines, deleted=0) for i in range(files)]
    if tested:
        changed.append(FileChange(path="tests/test_m.py", added=2, deleted=0))
    return Commit(
        sha=f"{n:040x}",
        author="a",
        when=datetime.now(timezone.utc),
        subject=f"commit {n}",
        files=changed,
    )


def _empty(n: int) -> Commit:
    return Commit(
        sha=f"{n:040x}", author="a", when=datetime.now(timezone.utc), subject="empty", files=[]
    )


def _riskiest(commits: list[Commit]):
    result = evaluate(History(repo="r", commits=commits))
    return result, next(c for c in result.checks if c.name == "riskiest commit")


UNIFORM = {
    "ten identical one-line commits": [_commit(n, 1, 1) for n in range(10)],
    "ten identical thousand-line commits": [_commit(n, 1000, 20) for n in range(10)],
    "fifty identical realistic commits": [_commit(n, 40, 3) for n in range(50)],
    "three commits a fifth of a point apart": [_commit(n, 10 + n, 1) for n in range(3)],
}


@pytest.mark.parametrize("release", sorted(UNIFORM))
def test_a_release_whose_commits_score_alike_cannot_be_compared(release: str):
    """However many commits it has. The count was the guard and it is not the question."""
    result, check = _riskiest(UNIFORM[release])
    assert check.status == NOT_MEASURED, check
    assert "about the same" in check.detail, check.detail
    assert "against a median of" not in check.detail, check.detail
    assert "1.0x" not in check.detail, check.detail
    # And the evidence line counts it as unmeasured, which is what a caller reads to
    # decide how much the verdict is worth.
    assert [c.name for c in result.not_measured] == ["riskiest commit"]
    assert result.evidence.startswith("3 of 4"), result.evidence


def test_a_median_of_zero_is_not_a_ratio_of_one():
    """`ratio = top / typical if typical else 1.0` called a commit infinitely above the
    median "exactly typical", and the structured `observed` a client reads said 1.0
    against a threshold of 1.5 under a clean GO."""
    result, check = _riskiest([_commit(0, 1300, 2)] + [_empty(n) for n in range(1, 4)])
    assert check.status == NOT_MEASURED, check
    assert "scores 0.0" in check.detail, check.detail
    assert check.observed > 1.0, check
    assert [c.name for c in result.not_measured] == ["riskiest commit"]


def test_a_release_that_does_vary_is_compared():
    """What is withdrawn is the comparison on releases that cannot support one. A
    release that can must still get it, or the fix has traded a meaningless number for
    a missing one."""
    result, check = _riskiest([_commit(0, 2, 1), _commit(1, 30, 2), _commit(2, 900, 9)])
    assert check.status != NOT_MEASURED, check
    assert "against a median of" in check.detail, check.detail
    assert check.unit == "ratio", check
    assert check.observed > 1.2, check
    assert result.evidence.startswith("4 of 4"), result.evidence


def test_a_uniformly_alarming_release_is_reported_by_the_checks_that_can_see_it():
    """Which are not the two relative ones, and that limit is worth stating.

    The absolute band is kept "so a release where everything is alarming is not passed
    for being uniformly alarming" - but it cannot fire on a uniform release either. The
    score is repo-relative and `Baseline` comes from this same history, so ten identical
    commits score at their own median whatever their size: ten 4,000-line 60-file
    commits score 36.0, under the 45.0 warn band, exactly as ten one-line commits score
    28.5. Neither can the outlier check, for the same reason.

    What is left is the absolute fact: this repository has no tests. That is a WARN of
    its own, and a reader is told it by name. A release of the same shape in a
    repository that HAS tests is blocked by the share check -
    `test_all_source_commits_untested_blocks` is that case.
    """
    result, check = _riskiest([_commit(n, 4000, 60, tested=False) for n in range(10)])
    assert check.status == NOT_MEASURED, check
    assert "about the same" in check.detail, check.detail

    named = {c.name: c for c in result.checks}
    assert named["no tests in this repository"].status == "warn", named
    assert result.verdict == "GO WITH WARNINGS", [(c.name, c.status) for c in result.checks]

    # And the repo-relative score really is flat across sizes, which is why the
    # absolute band cannot be the thing that catches this.
    _small, small_check = _riskiest([_commit(n, 1, 1, tested=False) for n in range(10)])
    assert abs(small_check.observed - check.observed) < 10, (small_check, check)


def test_the_two_branches_say_which_unit_they_report():
    """`observed` was a risk score in one branch and a ratio in the other, under one
    field name and one schema description - so a client graphing it across two calls
    plots 36.0 against 1.54."""
    _uniform_result, uniform = _riskiest(UNIFORM["ten identical one-line commits"])
    _varied_result, varied = _riskiest([_commit(0, 2, 1), _commit(1, 30, 2), _commit(2, 900, 9)])
    assert uniform.unit == "score"
    assert varied.unit == "ratio"
    assert uniform.unit != varied.unit
    # The thresholds they are compared against differ in the same way.
    assert uniform.threshold > 10, uniform
    assert varied.threshold < 10, varied
