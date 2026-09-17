"""Score how risky a change is, from diff statistics rather than from opinion.

The whole argument of this module is that no single number works. A commit of
47,743 lines across 5 files is a generated data dump and carries almost no
review risk. A commit of 5,442 lines across 693 files is a broad structural
change and carries a great deal. Ranked by lines, the first looks nine times
worse. Ranked by spread, the second does.

So risk is a small vector reduced to a score with stated weights, and the
factors are reported alongside the number. A gate that says "risk 78" and will
not say why is not a gate, it is an oracle.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median

from .history import Commit, History

# Weights are declared here rather than tuned, because there is no labelled
# outcome data to tune them against. They encode a stated position: breadth of
# change matters more than volume, and an untested source change matters most.
WEIGHTS = {
    "spread": 0.30,
    "files": 0.25,
    "untested": 0.25,
    "volume": 0.12,
    "deletion": 0.08,
}


@dataclass
class RiskFactors:
    """Each component on a 0-1 scale, before weighting."""

    volume: float  # how much changed, log-scaled
    files: float  # how many files
    spread: float  # how many distinct top-level areas
    untested: float  # source changed without tests changing
    deletion: float  # net removal, which reviews catch less often

    def score(self) -> float:
        total = sum(WEIGHTS[k] * getattr(self, k) for k in WEIGHTS)
        return round(100 * total, 1)

    def explain(self) -> list[tuple[str, float, float]]:
        """(factor, raw 0-1, points contributed) sorted by contribution."""
        rows = [
            (k, getattr(self, k), round(100 * WEIGHTS[k] * getattr(self, k), 1))
            for k in WEIGHTS
        ]
        return sorted(rows, key=lambda r: -r[2])


def _log_scale(value: float, midpoint: float) -> float:
    """Map an unbounded count onto 0-1, reaching 0.5 at `midpoint`.

    Linear scaling is wrong here: the difference between 10 and 100 changed
    lines matters, the difference between 10,000 and 100,000 does not.
    """
    if value <= 0:
        return 0.0
    return min(1.0, math.log1p(value) / (2 * math.log1p(midpoint)))


def score_commit(commit: Commit, baseline: "Baseline | None" = None) -> RiskFactors:
    """Risk factors for one commit, relative to what is normal for this repo."""
    b = baseline or Baseline.default()

    volume = _log_scale(commit.churn, b.median_churn)
    files = _log_scale(len(commit.files), b.median_files)
    spread = min(1.0, commit.spread / 6.0)

    if commit.touches_source and not commit.touches_tests:
        # Scaled by how much source actually changed. An untested eight-line
        # fix is not as risky as an untested eight-hundred-line one, and the
        # first version of this scored them the same - which ranked a one-file
        # typo fix third-riskiest in mcp-lab, above a 693-file change.
        source_share = len(commit.source_files) / max(1, len(commit.files))
        source_churn = sum(f.churn for f in commit.source_files)
        untested = source_share * _log_scale(source_churn, b.median_churn)
    else:
        untested = 0.0

    net = commit.deleted - commit.added
    deletion = min(1.0, net / max(1, commit.churn)) if net > 0 else 0.0

    return RiskFactors(
        volume=round(volume, 4),
        files=round(files, 4),
        spread=round(spread, 4),
        untested=round(untested, 4),
        deletion=round(deletion, 4),
    )


@dataclass
class Baseline:
    """What a normal commit looks like in this repository.

    Risk is relative. A 400-line commit is unremarkable in a repo whose median
    is 700 and alarming in one whose median is 20, and a gate with a fixed
    line threshold fires constantly in the first and never in the second.
    """

    median_churn: float
    median_files: float

    @classmethod
    def default(cls) -> Baseline:
        return cls(median_churn=100.0, median_files=3.0)

    @classmethod
    def from_history(cls, history: History) -> Baseline:
        if not history.commits:
            return cls.default()
        churns = [c.churn for c in history.commits if c.churn > 0]
        files = [len(c.files) for c in history.commits if c.files]
        return cls(
            median_churn=median(churns) if churns else 100.0,
            median_files=median(files) if files else 3.0,
        )


def rank_by(history: History, key: str) -> list[Commit]:
    """Rank commits by a single metric, for the disagreement comparison."""
    keys = {
        "churn": lambda c: c.churn,
        "files": lambda c: len(c.files),
        "spread": lambda c: c.spread,
    }
    if key == "risk":
        base = Baseline.from_history(history)
        return sorted(history.commits, key=lambda c: -score_commit(c, base).score())
    return sorted(history.commits, key=lambda c: -keys[key](c))


def rank_disagreement(history: History, top: int = 10) -> dict[str, float]:
    """How much the single-metric rankings disagree, as top-N overlap.

    If lines-changed and files-changed identified the same risky commits, the
    overlap would be 1.0 and one metric would be enough.
    """
    if len(history.commits) < 2:
        return {}
    top = min(top, len(history.commits))
    sets = {
        k: {c.sha for c in rank_by(history, k)[:top]}
        for k in ("churn", "files", "spread")
    }
    out: dict[str, float] = {}
    pairs = [("churn", "files"), ("churn", "spread"), ("files", "spread")]
    for a, b in pairs:
        out[f"{a} vs {b}"] = round(len(sets[a] & sets[b]) / top, 3)
    return out
