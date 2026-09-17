"""Command line for release-captain. argparse only - the core has no dependencies."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .gate import evaluate, render
from .history import NotAGitRepository, is_repository, read_history
from .risk import Baseline, rank_by, rank_disagreement, score_commit


def _gate_command(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    try:
        history = read_history(repo, limit=args.scan)
    except NotAGitRepository as exc:
        print(str(exc), file=sys.stderr)
        return 2

    result = evaluate(history, since=args.since)
    print(render(result))

    if args.json:
        payload = {
            "repo": result.repo,
            "verdict": result.verdict,
            "blocked": result.blocked,
            "commits_considered": result.commits_considered,
            "checks": [asdict(c) for c in result.checks],
            "riskiest": [
                {
                    "sha": c.sha,
                    "subject": c.subject,
                    "score": s,
                    "churn": c.churn,
                    "files": len(c.files),
                    "spread": c.spread,
                    "touches_tests": c.touches_tests,
                }
                for c, s in result.riskiest
            ],
        }
        Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"wrote {args.json}")

    return 1 if (result.blocked and args.strict) else 0


def _explain_command(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    history = read_history(repo)
    baseline = Baseline.from_history(history)
    match = [c for c in history.commits if c.sha.startswith(args.sha)]
    if not match:
        print(f"no commit starting {args.sha!r}", file=sys.stderr)
        return 1
    commit = match[0]
    factors = score_commit(commit, baseline)
    print(f"{commit.sha[:10]}  {commit.subject}")
    print(f"  {commit.churn} lines, {len(commit.files)} files, "
          f"{commit.spread} areas, tests {'yes' if commit.touches_tests else 'NO'}")
    print(f"  risk {factors.score()}")
    print()
    print(f"  {'factor':10s} {'raw':>6s} {'points':>7s}")
    for name, raw, points in factors.explain():
        print(f"  {name:10s} {raw:6.2f} {points:7.1f}")
    return 0


def _compare_command(args: argparse.Namespace) -> int:
    """Do the single metrics rank the same commits as risky?

    Raw top-N overlap is confounded: with 8 commits and a top-5 list, chance
    alone gives 62%. The excess over chance is the only readable number.
    """
    parent = Path(args.parent).resolve()
    rows = []
    for d in sorted(parent.iterdir()):
        if not d.is_dir() or not is_repository(d):
            continue
        try:
            history = read_history(d)
        except NotAGitRepository:
            continue
        n = len(history.commits)
        if n < args.min_commits:
            continue
        top = min(args.top, n)
        chance = top / n
        dis = rank_disagreement(history, top=top)
        if dis:
            rows.append((d.name, n, chance, dis))

    if not rows:
        print("no repositories with enough history")
        return 0

    print(f"{'repo':26s} {'n':>3s} {'chance':>7s} {'c/f':>6s} {'c/s':>6s} {'f/s':>6s}")
    print("-" * 62)
    for name, n, chance, dis in sorted(rows, key=lambda r: -r[1]):
        print(
            f"{name:26s} {n:3d} {chance:7.0%} "
            f"{dis['churn vs files']:6.0%} {dis['churn vs spread']:6.0%} "
            f"{dis['files vs spread']:6.0%}"
        )

    print("-" * 62)
    for key in ("churn vs files", "churn vs spread", "files vs spread"):
        excess = [dis[key] - chance for _, _, chance, dis in rows]
        print(f"{key:18s} mean excess over chance = {sum(excess) / len(excess):+.0%}")
    return 0


def _rank_command(args: argparse.Namespace) -> int:
    history = read_history(Path(args.repo).resolve())
    baseline = Baseline.from_history(history)
    for commit in rank_by(history, args.by)[: args.top]:
        score = score_commit(commit, baseline).score()
        print(
            f"  {score:5.1f}  {commit.sha[:8]}  {commit.churn:7d}L "
            f"{len(commit.files):4d}f {commit.spread}dir  {commit.subject[:48]}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="captain",
        description="Release readiness from diff statistics, not from opinion.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    g = sub.add_parser("gate", help="go / no-go for a release")
    g.add_argument("repo")
    g.add_argument("--since", type=int, default=None, help="assess the last N commits")
    g.add_argument("--scan", type=int, default=None, help="read at most N commits of history")
    g.add_argument("--json", help="also write the decision as JSON")
    g.add_argument("--strict", action="store_true", help="exit 1 when blocked, for CI")
    g.set_defaults(func=_gate_command)

    e = sub.add_parser("explain", help="why one commit scored what it did")
    e.add_argument("repo")
    e.add_argument("sha")
    e.set_defaults(func=_explain_command)

    r = sub.add_parser("rank", help="rank commits by one metric")
    r.add_argument("repo")
    r.add_argument("--by", choices=["churn", "files", "spread", "risk"], default="risk")
    r.add_argument("--top", type=int, default=10)
    r.set_defaults(func=_rank_command)

    c = sub.add_parser("compare", help="do single metrics agree, above chance?")
    c.add_argument("parent")
    c.add_argument("--top", type=int, default=5)
    c.add_argument("--min-commits", type=int, default=8)
    c.set_defaults(func=_compare_command)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
