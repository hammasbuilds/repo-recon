"""Risk scoring and gate tests.

The commits here are built by hand rather than read from git, because the
point is the scoring function and a fixture repository would only add latency.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from captain.gate import BLOCK, PASS, WARN, evaluate
from captain.history import Commit, FileChange, History
from captain.risk import Baseline, rank_by, rank_disagreement, score_commit

WHEN = datetime(2026, 9, 17, tzinfo=timezone.utc)


def make(sha: str, subject: str, files: list[tuple[str, int, int]]) -> Commit:
    return Commit(
        sha=sha,
        author="t",
        when=WHEN,
        subject=subject,
        files=[FileChange(p, a, d) for p, a, d in files],
    )


BASE = Baseline(median_churn=100.0, median_files=3.0)


# -- the factors ----------------------------------------------------------


def test_a_wide_change_outranks_a_voluminous_one():
    """The finding this project is built on.

    A generated data dump is enormous in lines and trivial in review effort.
    A broad refactor is the reverse. Ranked by lines alone, the dump wins.
    """
    dump = make("a", "regenerate results", [(f"results/r{i}.json", 10000, 0) for i in range(5)])
    refactor = make(
        "b", "rename across the package",
        [(f"src/pkg/m{i}.py", 8, 8) for i in range(60)] + [("tests/test_all.py", 5, 5)],
    )
    assert dump.churn > refactor.churn * 5
    assert score_commit(refactor, BASE).score() > score_commit(dump, BASE).score()


def test_untested_source_scores_higher_than_the_same_change_with_tests():
    without = make("a", "fix", [("src/pkg/core.py", 120, 40)])
    with_tests = make("b", "fix", [("src/pkg/core.py", 120, 40), ("tests/test_core.py", 30, 0)])
    assert score_commit(without, BASE).score() > score_commit(with_tests, BASE).score()


def test_an_untested_tiny_change_is_not_ranked_like_an_untested_large_one():
    """The first version scored these identically.

    That ranked a one-line typo fix third-riskiest in mcp-lab, above a
    693-file change, because `untested` ignored how much source had changed.
    """
    tiny = make("a", "typo", [("src/pkg/core.py", 1, 1)])
    large = make("b", "rewrite", [("src/pkg/core.py", 800, 300)])
    tiny_untested = score_commit(tiny, BASE).untested
    large_untested = score_commit(large, BASE).untested
    assert large_untested > tiny_untested * 2


def test_docs_only_commits_score_low():
    docs = make("a", "readme", [("README.md", 200, 50)])
    code = make("b", "code", [("src/pkg/core.py", 200, 50)])
    assert score_commit(docs, BASE).score() < score_commit(code, BASE).score()


def test_volume_compresses_the_range_instead_of_scaling_with_it():
    """A 10,000x larger diff must not be 10,000x the risk.

    An earlier version of this test asserted diminishing returns *per decade*,
    which a log scale does not provide - it gives equal steps per decade by
    construction. The property that actually matters is compression against
    linear, plus a ceiling: without one, a single generated data file would
    saturate any release gate on its own.
    """
    small = score_commit(make("a", "s", [("src/a.py", 10, 0), ("tests/t.py", 1, 0)]), BASE)
    huge = score_commit(make("c", "h", [("src/a.py", 100000, 0), ("tests/t.py", 1, 0)]), BASE)

    line_ratio = 100000 / 10
    volume_ratio = huge.volume / small.volume
    assert volume_ratio < line_ratio / 1000  # 10,000x lines, under 10x volume
    assert huge.volume <= 1.0  # and it cannot exceed the ceiling


def test_volume_steps_evenly_per_decade_until_it_saturates():
    def vol(lines: int) -> float:
        return score_commit(make("x", "s", [("src/a.py", lines, 0)]), BASE).volume

    first = vol(100) - vol(10)
    second = vol(1000) - vol(100)
    assert first == pytest.approx(second, abs=0.02)


def test_factors_sum_to_the_score():
    commit = make("a", "x", [("src/pkg/a.py", 50, 10), ("docs/b.md", 5, 0)])
    factors = score_commit(commit, BASE)
    assert sum(points for _, _, points in factors.explain()) == pytest.approx(
        factors.score(), abs=0.2
    )


def test_explain_is_ordered_by_contribution():
    commit = make("a", "x", [(f"src/area{i}/m.py", 40, 5) for i in range(5)])
    points = [p for _, _, p in score_commit(commit, BASE).explain()]
    assert points == sorted(points, reverse=True)


# -- baselines are relative -----------------------------------------------

def test_the_same_commit_scores_lower_in_a_repo_of_large_commits():
    """A 400-line change is unremarkable where the median is 700."""
    commit = make("a", "x", [("src/pkg/a.py", 300, 100), ("tests/t.py", 10, 0)])
    small_repo = Baseline(median_churn=20.0, median_files=2.0)
    large_repo = Baseline(median_churn=700.0, median_files=15.0)
    assert score_commit(commit, small_repo).score() > score_commit(commit, large_repo).score()


def test_baseline_from_empty_history_falls_back_to_defaults():
    base = Baseline.from_history(History(repo="x", commits=[]))
    assert base.median_churn == Baseline.default().median_churn


# -- the gate -------------------------------------------------------------


def _history(commits: list[Commit]) -> History:
    return History(repo="demo", commits=commits)


def test_all_source_commits_untested_blocks():
    h = _history([make(str(i), "x", [("src/a.py", 50, 0)]) for i in range(3)])
    result = evaluate(h)
    check = next(c for c in result.checks if c.name == "source changes without tests")
    assert check.status == BLOCK
    assert result.blocked
    assert result.verdict == "NO-GO"


def test_well_tested_small_release_passes():
    h = _history([
        make(str(i), "x", [("src/a.py", 10, 2), ("tests/test_a.py", 8, 0)])
        for i in range(4)
    ])
    result = evaluate(h)
    assert not result.blocked
    assert result.verdict in ("GO", "GO WITH WARNINGS")


def test_an_empty_range_is_blocked_rather_than_passed():
    """Nothing to release must not read as a clean bill of health."""
    result = evaluate(_history([]))
    assert result.blocked


def test_every_check_reports_what_it_saw():
    h = _history([make("a", "x", [("src/a.py", 10, 0), ("tests/t.py", 1, 0)])])
    for check in evaluate(h).checks:
        assert check.detail
        assert check.status in (PASS, WARN, BLOCK)


def test_strictness_is_relative_to_the_repo_median():
    """A 3000-line commit is an outlier only where commits are usually small."""
    normal = [make(str(i), "x", [("src/a.py", 20, 0), ("tests/t.py", 2, 0)]) for i in range(6)]
    spike = make("big", "huge", [("src/a.py", 3000, 0), ("tests/t.py", 5, 0)])
    result = evaluate(_history([spike, *normal]))
    outlier = next(c for c in result.checks if "median" in c.name)
    assert outlier.status == WARN


# -- ranking comparison ---------------------------------------------------


def test_rank_disagreement_is_empty_for_a_single_commit():
    assert rank_disagreement(_history([make("a", "x", [("a.py", 1, 0)])])) == {}


def test_rank_by_risk_differs_from_rank_by_churn_on_a_dump():
    dump = make("dump", "results", [("results/r.json", 50000, 0)])
    broad = make("broad", "refactor", [(f"src/a{i}/m.py", 20, 20) for i in range(8)])
    h = _history([dump, broad])
    assert rank_by(h, "churn")[0].sha == "dump"
    assert rank_by(h, "risk")[0].sha == "broad"
