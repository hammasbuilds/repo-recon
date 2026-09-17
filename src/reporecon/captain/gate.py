"""The go / no-go decision, and the reasons behind it.

A gate that refuses without naming the specific violation is not a gate, it is
an obstacle. Every check here returns the number it saw and the threshold it
compared against, so a refusal can be argued with.

Thresholds are relative to the repository's own history wherever possible. A
400-line change is unremarkable in a repo whose median commit is 700 lines and
alarming in one whose median is 20; a fixed threshold is wrong in both.
"""

from __future__ import annotations

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
        result.checks.append(
            Check("nothing to release", BLOCK, "no commits in range", 0, 1)
        )
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
                f"{len(untested)} of {len(source)} source-changing commits "
                f"changed no test file",
                round(share, 3),
                untested_warn,
            )
        )
    else:
        result.checks.append(
            Check("source changes without tests", PASS, "no source changes", 0.0, untested_warn)
        )

    # 2. The single riskiest commit.
    top_commit, top_score = scored[0]
    result.checks.append(
        Check(
            "riskiest commit",
            _band(top_score, risk_warn, risk_block),
            f"{top_score} - {top_commit.sha[:8]} {top_commit.subject[:60]}",
            top_score,
            risk_warn,
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
    big = [c for c in commits if c.churn > baseline.median_churn * 20]
    if big:
        result.checks.append(
            Check(
                "commits far above this repo's median",
                WARN,
                f"{len(big)} commit(s) over {baseline.median_churn * 20:.0f} lines "
                f"(20x the {baseline.median_churn:.0f}-line median)",
                float(len(big)),
                0,
            )
        )
    else:
        result.checks.append(
            Check("commits far above this repo's median", PASS, "none", 0, 0)
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
