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
#: The check ran and had nothing to look at. Not the same as passing: a release with
#: no source changes was reported as having *cleared* the test-coupling check, and
#: `release_readiness` handed an agent a clean GO without saying so. Swept over 75
#: live checkouts in the author's own folder on 2026-10-04 - the reason this check
#: exists, not a figure anyone can re-derive, because those repositories have moved
#: and the sweep names no revision. The behaviour is in the tests instead.
#: local checkouts at `since=8`, 12 earn a clean GO and 4 of those 12 rest on this
#: check seeing nothing - releases of documentation and data, where "no source
#: shipped untested" is true only because no source shipped. (Measured through
#: THIS gate. The monorepo this was filtered out of has diverged in its checks
#: and reports 17 and 6 over the same folder, which is why the number is taken
#: here rather than carried across.)
NOT_MEASURED = "not measured"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    observed: float
    threshold: float
    #: What `observed` and `threshold` are counted in. The riskiest-commit check
    #: reports a RATIO when the release can support a comparison and a risk SCORE when
    #: it cannot, and both used to arrive as a bare number under one field name - so a
    #: client graphing `observed` across two calls plotted 1.54 (a ratio) next to 36.0
    #: (a score) and the schema said only "the observed value compared with the
    #: threshold". Naming the unit is cheaper than splitting the field, and it makes
    #: the two branches distinguishable without reading this module.
    unit: str = "score"


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
    def not_measured(self) -> list[Check]:
        """Checks that ran and had nothing to look at.

        They do not change the verdict - nothing was wrong, because nothing was seen -
        but they change how much the verdict is worth, and a caller cannot tell unless
        the result says so.
        """
        return [c for c in self.checks if c.status == NOT_MEASURED]

    @property
    def evidence(self) -> str:
        """How many of the checks actually measured something."""
        measured = len(self.checks) - len(self.not_measured)
        return f"{measured} of {len(self.checks)} checks measured something"

    @property
    def verdict(self) -> str:
        if self.blocked:
            return "NO-GO"
        return "GO WITH WARNINGS" if self.warnings else "GO"


#: Severity order, so two bands can be combined by taking the worse of them.
_RANK = {PASS: 0, NOT_MEASURED: 0, WARN: 1, BLOCK: 2}

#: How many commits the standout comparison needs before it says anything. With one,
#: the median is the top score and the ratio is 1.0 by construction; with two it is
#: their mean, so the top is always above it by the same arithmetic. Three is the
#: smallest number where "stands out from the rest" is a claim about the release
#: rather than about division.
_STANDOUT_MINIMUM = 3

#: How much the release's scores have to differ before "stands out from the rest" is a
#: statement about this release. Measured as (riskiest - least risky) / riskiest.
#:
#: A count was the wrong guard. `_log_scale` returns 0.5 at the median, so in a
#: homogeneous history volume and files carry no information and every commit scores
#: the same - ten identical commits report "36.0 against a median of 36.0 (1.0x)" and
#: pass, at any length.
#:
#: Five per cent, chosen on 2026-10-07 from the spread of five repositories in the
#: author's own `D:/github` at windows of 1, 3, 5, 10 and 20 commits: the narrowest real
#: release was 0.10 and most were 0.6 to 0.8, while every uniform fixture sits at exactly
#: 0.00. That is evidence the threshold is not arbitrary and it is NOT a measurement
#: anyone can reproduce - those are live checkouts that have moved since, and the
#: sentence names no revision because there was no single revision to name. What can be
#: checked is in `tests/captain/test_standout_is_measurable.py`: uniform releases are
#: withdrawn, a varied one is compared, and the fixtures are in the file.
_STANDOUT_SPREAD = 0.05


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

    # A truncated clone has no repository habits to compare a release against: its
    # commits are the ones that were fetched, and `--depth 1` leaves exactly one,
    # holding the whole tree. `history.py` says answering "as though the commits that
    # are missing are not missing" is not acceptable, and returning `shallow: true`
    # beside four confident checks was annotating rather than answering. Every
    # repo-relative check is withdrawn; the absolute ones still report.
    if history.shallow:
        result.checks.append(
            Check(
                "shallow clone",
                WARN,
                history.caveat,
                float(len(commits)),
                1,
                unit="count",
            )
        )

    if not commits:
        result.checks.append(
            Check("nothing to release", BLOCK, "no commits in range", 0, 1, unit="count")
        )
        return result

    scored = sorted(
        ((c, score_commit(c, baseline).score()) for c in commits),
        key=lambda pair: -pair[1],
    )
    result.riskiest = scored[:10]

    # 1. Source changes shipping without any test change.
    #
    # Two different statements used to come out of one band. "This release changed
    # source without touching tests" is a reading of the release; "this repository has
    # no tests" is a fact about the project, and it reads as 100% untested on every
    # release it will ever have - so the only check that can BLOCK answered NO-GO to
    # every question anyone asked about such a repository, permanently, which is not a
    # release decision. It is also a rate over an empty population: the denominator of
    # "did this change bring tests" is the test files that exist.
    # The repository's tracked files, not the commits in the window. `history.commits`
    # IS the scan window - `read_history(repo, limit=scan)` - and `scan` is an argument
    # a caller passes, so reading it here meant `release_readiness(path, scan=3)` could
    # decide a repository has no tests because none were touched in three commits, and
    # turn off the only check in this gate that can BLOCK.
    #
    # Falls back to the window when the file list is unavailable, which is the old
    # behaviour and is still better than treating an unreadable repository as untested.
    tested_anywhere = bool(history.test_files_tracked) or any(
        c.touches_tests for c in history.commits
    )
    source = [c for c in commits if c.touches_source]
    if source and not tested_anywhere:
        result.checks.append(
            Check(
                "source changes without tests",
                NOT_MEASURED,
                "this repository tracks no test files at all, so this release reads "
                "100% untested by construction rather than by observation; see the "
                "check below",
                1.0,
                untested_warn,
                unit="share",
            )
        )
        result.checks.append(
            Check(
                "no tests in this repository",
                WARN,
                "`git ls-files` reports no test file in this repository. That is worth "
                "knowing and it is not a judgement of this release: it is the same on "
                "every release this repository will have",
                1.0,
                1,
                unit="count",
            )
        )
    elif source:
        untested = [c for c in source if not c.touches_tests]
        share = len(untested) / len(source)
        result.checks.append(
            Check(
                "source changes without tests",
                _band(share, untested_warn, untested_block),
                f"{len(untested)} of {len(source)} source-changing commits changed no test file",
                round(share, 3),
                untested_warn,
                unit="share",
            )
        )
    else:
        result.checks.append(
            Check(
                "source changes without tests",
                NOT_MEASURED,
                (
                    "every commit in range is documentation, so test coupling was not observed"
                    if all(c.is_docs_only for c in commits)
                    else "no source changes in range, so test coupling was not observed"
                ),
                0.0,
                untested_warn,
                unit="share",
            )
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
    all_scores = [score for _, score in scored]
    typical = statistics.median(all_scores)
    lowest = min(all_scores)
    spread = (top_score - lowest) / top_score if top_score else 0.0
    absolute = _band(top_score, risk_warn, risk_block)

    # "Stands out from the rest of the release" needs a rest of the release. With one
    # or two commits the median IS the top score (or next to it), so the ratio is
    # identically 1.0 - a number divided by itself, reported as
    # "35.9 against a median of 35.9 for this release (1.0x)" under
    # "4 of 4 checks measured something". `last=1` is the most natural call there is
    # for "is this commit safe to ship", and it was the one where the comparison could
    # not say anything.
    #
    # `NOT_MEASURED` exists in this module for exactly this - "the check ran and had
    # nothing to look at" - and was not being applied here. The absolute band stays
    # live, so a single alarming commit is still caught; what is withdrawn is the
    # comparison, not the check.
    # Why the comparison cannot be made, or None if it can. Three reasons rather than
    # one, because they are three different facts about the release and a reader acting
    # on "not measured" needs to know which: too few commits to have a rest; a median of
    # zero, against which every number is infinitely large; and scores so alike that the
    # ratio is arithmetic rather than evidence.
    cannot_compare: str | None = None
    if history.shallow:
        cannot_compare = (
            "this is a shallow clone, so the median it would be compared against is the "
            "median of the commits that happened to be fetched"
        )
    elif len(scored) < _STANDOUT_MINIMUM:
        cannot_compare = (
            f"{len(scored)} commit(s) is too few to say whether it stands out from the release"
        )
    elif typical <= 0:
        cannot_compare = (
            "the median commit in this release scores 0.0, and every number is "
            "infinitely above zero"
        )
    elif spread < _STANDOUT_SPREAD:
        cannot_compare = (
            f"every commit in this release scores about the same "
            f"({lowest} to {top_score}, {spread:.0%} apart), so the ratio is 1.0 by "
            "arithmetic rather than because nothing stands out"
        )

    if cannot_compare is not None:
        result.checks.append(
            Check(
                "riskiest commit",
                NOT_MEASURED if absolute == PASS else absolute,
                f"{top_score} - {top_commit.sha[:8]} {_subject(top_commit.subject, 60)}; "
                f"{cannot_compare}, so only the absolute band was applied",
                top_score,
                risk_warn,
                unit="score",
            )
        )
    else:
        ratio = top_score / typical
        standout = _band(ratio, stands_out_warn, stands_out_block)
        verdict = standout if _RANK[standout] >= _RANK[absolute] else absolute
        result.checks.append(
            Check(
                "riskiest commit",
                verdict,
                f"{top_score} against a median of {typical:.1f} for this release "
                f"({ratio:.1f}x) - {top_commit.sha[:8]} {_subject(top_commit.subject, 60)}",
                round(ratio, 2),
                stands_out_warn,
                unit="ratio",
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
            unit="count",
        )
    )

    # 4. Outliers relative to this repo's own habits.
    # The threshold is one such commit, not zero. Both branches reported threshold 0,
    # so the passing case read "observed 0, threshold 0" - which under `_band`'s `>=`
    # is a failure, and contradicted the schema's own statement that the status is the
    # observed value compared with the threshold.
    big = [c for c in commits if c.churn > baseline.median_churn * 20]
    limit = baseline.median_churn * 20
    # The same question as check 2, asked of the same numbers: can this comparison
    # distinguish anything? Each commit is compared against 20x the median of these
    # commits, so over one commit the test is `churn > churn * 20` - false by
    # construction, and reported as a pass. The guard written for check 2 was not
    # applied here, which is how a one-commit release and a shallow clone both read
    # "none over 254,520 lines" and passed.
    if history.shallow or len(commits) < _STANDOUT_MINIMUM or baseline.median_churn <= 0:
        result.checks.append(
            Check(
                "commits far above this repo's median",
                NOT_MEASURED,
                f"{len(commits)} commit(s) with a median of "
                f"{baseline.median_churn:.0f} lines is too few to say what this "
                "repository's habits are, and each commit would be compared against "
                "20x the median of itself",
                float(len(big)),
                1,
                unit="count",
            )
        )
    else:
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
                unit="count",
            )
        )

    return result


def _subject(text: str, width: int) -> str:
    """A commit subject, safe to write to a terminal.

    A commit message is arbitrary bytes, and this is the one place they are handed to a
    device that interprets them: a `\x07` in a subject rang the bell, and an escape
    sequence would do whatever the terminal does with it. C0 and C1 controls become
    U+FFFD, which is what that character is for, and the text is otherwise untouched -
    the repository really does contain that message and a renderer that rewrote it would
    be lying about the history.

    JSON output needs none of this: `json.dumps` escapes a control character, and a
    client that renders the result is responsible for its own terminal.
    """
    cleaned = "".join("\ufffd" if ord(c) < 32 or 127 <= ord(c) < 160 else c for c in text)
    return cleaned[:width]


def render(result: GateResult) -> str:
    w = 78
    out = ["=" * w, f"  {result.repo}  -  {result.verdict}", "=" * w, ""]
    out.append(f"  {result.commits_considered} commit(s) assessed; {result.evidence}")
    out.append("")
    out.append("  CHECKS")
    symbol = {PASS: "ok  ", WARN: "warn", BLOCK: "STOP", NOT_MEASURED: " -  "}
    for c in result.checks:
        out.append(f"    [{symbol[c.status]}] {c.name}")
        out.append(f"           {c.detail}")
    out.append("")
    out.append("  RISKIEST COMMITS  (composite: spread, files, untested, volume, deletion)")
    for commit, score in result.riskiest[:5]:
        out.append(
            f"    {score:5.1f}  {commit.sha[:8]}  {commit.churn:6d}L "
            f"{len(commit.files):4d}f {commit.spread}dir  {_subject(commit.subject, 44)}"
        )
    out.append("")
    out.append("=" * w)
    return "\n".join(out)
