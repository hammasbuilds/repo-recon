"""The go / no-go decision, and the reasons behind it.

A gate that refuses without naming the specific violation is not a gate, it is
an obstacle. Every check here returns the number it saw and the threshold it
compared against, so a refusal can be argued with.

Thresholds are relative to the repository's own history wherever possible. A
400-line change is unremarkable in a repo whose median commit is 700 lines and
alarming in one whose median is 20; a fixed threshold is wrong in both.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from .history import Commit, History
from .risk import Baseline, score_commit

PASS = "pass"
WARN = "warn"
BLOCK = "block"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    observed: float
    threshold: float

    @property
    def ok(self) -> bool:
        return self.status == PASS


@dataclass
class GateResult:
    repo: str
    commits_considered: int
    checks: list[Check] = field(default_factory=list)
    riskiest: list[tuple[Commit, float]] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(c.status == BLOCK for c in self.checks)

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    @property
    def blockers(self) -> list[Check]:
        return [c for c in self.checks if c.status == BLOCK]

    @property
    def verdict(self) -> str:
        if self.blocked:
            return "NO-GO"
        return "GO WITH WARNINGS" if self.warnings else "GO"


#: Severity order, so two bands can be combined by taking the worse of them.
_RANK = {PASS: 0, WARN: 1, BLOCK: 2}


def _band(value: float, warn_at: float, block_at: float) -> str:
    if value >= block_at:
        return BLOCK
    if value >= warn_at:
        return WARN
    return PASS


def evaluate(
    history: History,
    since: int | None = None,
    untested_warn: float = 0.34,
    untested_block: float = 0.67,
    risk_warn: float = 45.0,
    risk_block: float = 65.0,
    stands_out_warn: float = 1.5,
    stands_out_block: float = 2.5,
) -> GateResult:
    """Assess the commits that would ship in this release.

    `since` limits to the most recent N commits - the release candidate. With
    no limit the whole history is assessed, which is useful for an audit but is
    not a release decision.
    """
    commits = history.commits[:since] if since else history.commits
    baseline = Baseline.from_history(history)
    result = GateResult(repo=history.repo, commits_considered=len(commits))

    if not commits:
        result.checks.append(Check("nothing to release", BLOCK, "no commits in range", 0, 1))
        return result

    scored = sorted(
        ((c, score_commit(c, baseline).score()) for c in commits),
        key=lambda pair: -pair[1],
    )
    result.riskiest = scored[:10]

    # 1. Source changes shipping without any test change.
    source = [c for c in commits if c.touches_source]
    if source:
        untested = [c for c in source if not c.touches_tests]
        share = len(untested) / len(source)
        result.checks.append(
            Check(
                "source changes without tests",
                _band(share, untested_warn, untested_block),
                f"{len(untested)} of {len(source)} source-changing commits changed no test file",
                round(share, 3),
                untested_warn,
            )
        )
    else:
        result.checks.append(
            Check("source changes without tests", PASS, "no source changes", 0.0, untested_warn)
        )

    # 2. The single riskiest commit, judged against this release rather than against a
    # number. The score is deliberately repo-relative - `_log_scale` returns 0.5 at the
    # median, so in a homogeneous history volume and files carry no information at all
    # and every commit scores the same. Comparing that to a fixed 45 is the mistake this
    # module's own first paragraph warns about: ten identical one-line commits and ten
    # identical 20,000-line commits differed only by `spread`, and the verdict followed
    # the constant, not the risk.
    #
    # What a release decision wants is whether one commit stands out from the rest of
    # the release. The absolute band is kept as a floor, so a release where everything
    # is alarming is not passed for being uniformly alarming.
    top_commit, top_score = scored[0]
    typical = statistics.median(score for _, score in scored)
    ratio = top_score / typical if typical else 1.0
    standout = _band(ratio, stands_out_warn, stands_out_block)
    absolute = _band(top_score, risk_warn, risk_block)
    verdict = standout if _RANK[standout] >= _RANK[absolute] else absolute
    result.checks.append(
        Check(
            "riskiest commit",
            verdict,
            f"{top_score} against a median of {typical:.1f} for this release "
            f"({ratio:.1f}x) - {top_commit.sha[:8]} {top_commit.subject[:60]}",
            round(ratio, 2),
            stands_out_warn,
        )
    )

    # 3. Breadth: a release touching many areas is harder to roll back cleanly.
    areas = {a for c in commits for a in c.areas}
    result.checks.append(
        Check(
            "areas touched",
            _band(len(areas), 6, 12),
            f"{len(areas)} top-level areas: {', '.join(sorted(areas)[:8])}",
            float(len(areas)),
            6,
        )
    )

    # 4. Outliers relative to this repo's own habits.
    # The threshold is one such commit, not zero. Both branches reported threshold 0,
    # so the passing case read "observed 0, threshold 0" - which under `_band`'s `>=`
    # is a failure, and contradicted the schema's own statement that the status is the
    # observed value compared with the threshold.
    big = [c for c in commits if c.churn > baseline.median_churn * 20]
    limit = baseline.median_churn * 20
    result.checks.append(
        Check(
            "commits far above this repo's median",
            WARN if big else PASS,
            (
                f"{len(big)} commit(s) over {limit:.0f} lines "
                f"(20x the {baseline.median_churn:.0f}-line median)"
                if big
                else f"none over {limit:.0f} lines (20x the "
                f"{baseline.median_churn:.0f}-line median)"
            ),
            float(len(big)),
            1,
        )
    )

    return result


def render(result: GateResult) -> str:
    w = 78
    out = ["=" * w, f"  {result.repo}  -  {result.verdict}", "=" * w, ""]
    out.append(f"  {result.commits_considered} commit(s) assessed")
    out.append("")
    out.append("  CHECKS")
    symbol = {PASS: "ok  ", WARN: "warn", BLOCK: "STOP"}
    for c in result.checks:
        out.append(f"    [{symbol[c.status]}] {c.name}")
        out.append(f"           {c.detail}")
    out.append("")
    out.append("  RISKIEST COMMITS  (composite: spread, files, untested, volume, deletion)")
    for commit, score in result.riskiest[:5]:
        out.append(
            f"    {score:5.1f}  {commit.sha[:8]}  {commit.churn:6d}L "
            f"{len(commit.files):4d}f {commit.spread}dir  {commit.subject[:44]}"
        )
    out.append("")
    out.append("=" * w)
    return "\n".join(out)
