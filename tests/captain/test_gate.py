"""The release gate, and whether its thresholds mean anything.

The risk score is deliberately relative to the repository it is measured in. A gate
that compares it to a fixed number is asking a different question from the one the
score answers, and these tests are that distinction.
"""

from __future__ import annotations

import datetime as dt

from reporecon.captain.gate import evaluate
from reporecon.captain.history import Commit, FileChange, History


def _plain(n: int, *, lines: int, files: int) -> Commit:
    return Commit(
        sha=f"{n:040x}",
        author="A",
        when=dt.datetime(2026, 1, 1) + dt.timedelta(days=n),
        subject=f"commit {n}",
        files=[FileChange(path=f"src/m{n}_{i}.py", added=lines, deleted=0) for i in range(files)],
    )


def _riskiest(history: History):
    return next(c for c in evaluate(history).checks if c.name == "riskiest commit")


def test_a_uniform_release_has_no_riskiest_commit_worth_naming():
    """The score is repo-relative by design: `_log_scale` returns 0.5 at the median, so
    in a homogeneous history volume and files carry no information and every commit
    scores the same. Comparing that to a fixed 45 is the mistake this module's own
    first paragraph warns about — ten one-line commits and ten 20,000-line commits
    differed only by `spread`, and the verdict followed the constant, not the risk.
    """
    for lines, files in ((1, 1), (1000, 20)):
        history = History(
            repo="r", commits=[_plain(n, lines=lines, files=files) for n in range(10)]
        )
        check = _riskiest(history)
        assert check.observed == 1.0, (lines, files, check.detail)
        assert check.status == "pass", (lines, files, check.detail)


def test_one_commit_that_stands_out_is_named():
    history = History(
        repo="r",
        commits=[_plain(n, lines=1, files=1) for n in range(9)] + [_plain(9, lines=5000, files=40)],
    )
    check = _riskiest(history)
    assert check.observed > 1.5
    assert check.status in {"warn", "block"}
    assert "against a median of" in check.detail


def test_the_outlier_check_passes_against_a_threshold_it_can_pass():
    """Both branches reported threshold 0, so the passing case read "observed 0,
    threshold 0" — which under `_band`'s `>=` is a failure, and contradicted the
    schema's own statement that the status is the observed value against the
    threshold."""
    history = History(repo="r", commits=[_plain(n, lines=1, files=1) for n in range(10)])
    check = next(c for c in evaluate(history).checks if "median" in c.name)
    assert check.status == "pass"
    assert check.observed == 0.0
    assert check.threshold == 1
