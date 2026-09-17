"""The two-stage pipeline, and the diff filter that makes it a review.

A reviewer that comments on code the author did not touch is not reviewing the
change, it is auditing the repository. So findings are restricted to lines the
diff actually added, while *verification* still reads the whole file - most
false positives are false because of something outside the hunk.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .checks import Proposal, propose
from .verify import FileContext, Verdict, verify_all


@dataclass
class FileReview:
    path: str
    verdicts: list[Verdict] = field(default_factory=list)
    changed_lines: set[int] | None = None

    @property
    def confirmed(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.confirmed]

    @property
    def retracted(self) -> list[Verdict]:
        return [v for v in self.verdicts if not v.confirmed]


@dataclass
class Review:
    files: list[FileReview] = field(default_factory=list)

    @property
    def proposed(self) -> int:
        return sum(len(f.verdicts) for f in self.files)

    @property
    def confirmed(self) -> int:
        return sum(len(f.confirmed) for f in self.files)

    @property
    def retracted(self) -> int:
        return self.proposed - self.confirmed

    @property
    def retraction_rate(self) -> float:
        return self.retracted / self.proposed if self.proposed else 0.0

    def by_rule(self) -> dict[str, tuple[int, int]]:
        out: dict[str, list[int]] = {}
        for file in self.files:
            for verdict in file.verdicts:
                slot = out.setdefault(verdict.rule, [0, 0])
                slot[0] += 1
                if verdict.confirmed:
                    slot[1] += 1
        return {k: (v[0], v[1]) for k, v in out.items()}


def review_source(
    path: str, source: str, changed_lines: set[int] | None = None
) -> FileReview:
    """Propose on the whole file, verify with full context, filter to the diff."""
    context = FileContext.build(path, source)
    proposals: list[Proposal] = propose(source)

    if changed_lines is not None:
        proposals = [
            p for p in proposals
            if any(line in changed_lines for line in range(p.line, p.end_line + 1))
        ]

    return FileReview(
        path=path,
        verdicts=verify_all(context, proposals),
        changed_lines=changed_lines,
    )


# -- diffs -----------------------------------------------------------------

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def added_lines(diff: str) -> dict[str, set[int]]:
    """Line numbers added per file, parsed from a unified diff.

    Only added lines count. A finding on a line the change did not touch
    belongs in an audit, not in a review of this change.
    """
    out: dict[str, set[int]] = {}
    current: str | None = None
    line_number = 0

    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            name = raw[4:].strip()
            if name == "/dev/null":
                current = None
            else:
                current = name[2:] if name.startswith(("a/", "b/")) else name
                out.setdefault(current, set())
            continue
        if raw.startswith("@@"):
            match = _HUNK.match(raw)
            if match:
                line_number = int(match.group(1))
            continue
        if current is None:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            out[current].add(line_number)
            line_number += 1
        elif raw.startswith("-"):
            continue
        else:
            line_number += 1
    return out


def git_diff(repo: Path, ref: str = "HEAD~1") -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "diff", "--unified=0", ref],
        capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:200] or "git diff failed")
    return proc.stdout


def review_diff(repo: Path, ref: str = "HEAD~1") -> Review:
    """Review only what a change added."""
    diff = git_diff(repo, ref)
    review = Review()
    for rel, lines in added_lines(diff).items():
        if not rel.endswith(".py") or not lines:
            continue
        path = repo / rel
        if not path.is_file():
            continue  # deleted in the working tree
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        file_review = review_source(rel, source, lines)
        if file_review.verdicts:
            review.files.append(file_review)
    return review


def render(review: Review, show_retracted: bool = False) -> str:
    out = ["=" * 78]
    out.append(
        f"  {review.confirmed} finding(s) to report  -  "
        f"{review.retracted} of {review.proposed} proposals retracted "
        f"({review.retraction_rate:.0%})"
    )
    out.append("=" * 78)

    if review.confirmed:
        out.append("")
        for file in review.files:
            if not file.confirmed:
                continue
            out.append(f"  {file.path}")
            for verdict in file.confirmed:
                proposal = verdict.proposal
                out.append(f"    line {proposal.line:5d}  [{proposal.severity}] {proposal.rule}")
                out.append(f"              {proposal.message}")
    else:
        out.append("")
        out.append("  Nothing survived verification.")

    if show_retracted and review.retracted:
        out.append("")
        out.append("  RETRACTED  (proposed, then defeated)")
        for file in review.files:
            for verdict in file.retracted:
                out.append(
                    f"    {file.path}:{verdict.proposal.line} {verdict.rule}"
                )
                out.append(f"              defeated: {verdict.reason}")

    by_rule = review.by_rule()
    if by_rule:
        out.append("")
        out.append("  BY RULE  (proposed / confirmed)")
        for rule, (proposed, confirmed) in sorted(by_rule.items(), key=lambda kv: -kv[1][0]):
            rate = 1 - confirmed / proposed
            out.append(f"    {rule:24s} {proposed:5d} / {confirmed:<5d} {rate:.0%} retracted")

    out.append("")
    out.append("=" * 78)
    return "\n".join(out)
