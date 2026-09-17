"""Command line for review-bot. argparse only - the core has no dependencies."""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from collections import Counter
from pathlib import Path

from .checks import RULES, propose
from .review import Review, render, review_diff, review_source
from .verify import FileContext, verify_all

SKIP_DIRS = frozenset(
    {
        ".git", ".venv", ".venvs", "venv", "env", "__pycache__", ".pytest_cache",
        ".ruff_cache", ".mypy_cache", "node_modules", "build", "dist", ".tox",
        ".eggs", "site-packages",
    }
)


def _python_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    return sorted(
        p for p in target.rglob("*.py")
        if not any(part in SKIP_DIRS for part in p.relative_to(target).parts)
    )


def _diff_command(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    try:
        review = review_diff(repo, args.ref)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(render(review, show_retracted=args.show_retracted))
    if args.strict and review.confirmed:
        return 1
    return 0


def _scan_command(args: argparse.Namespace) -> int:
    """Review whole files rather than a diff. Used to measure precision."""
    target = Path(args.path).resolve()
    review = Review()
    skipped_tests = 0

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        for path in _python_files(target):
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not args.include_tests and FileContext.build(str(path), source).is_test:
                skipped_tests += 1
                continue
            try:
                shown = str(path.relative_to(target))
            except ValueError:
                shown = str(path)
            file_review = review_source(shown, source)
            if file_review.verdicts:
                review.files.append(file_review)

    print(render(review, show_retracted=args.show_retracted))
    if skipped_tests:
        print(f"  ({skipped_tests} test files skipped; pass --include-tests to review them)")

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "proposed": review.proposed,
                    "confirmed": review.confirmed,
                    "retracted": review.retracted,
                    "retraction_rate": round(review.retraction_rate, 4),
                    "by_rule": {
                        k: {"proposed": p, "confirmed": c}
                        for k, (p, c) in review.by_rule().items()
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"wrote {args.json}")
    return 0


def _rules_command(_: argparse.Namespace) -> int:
    from .verify import _DEFEATERS

    print("Proposers are deliberately over-eager. Each one has verifiers that")
    print("try to defeat it before anything is reported.\n")
    for rule in RULES:
        has = "yes" if rule in _DEFEATERS else "no"
        print(f"  {rule:24s} defeaters: {has}")
    print("\nA proposal survives only if no defeater applies. The retraction rate")
    print("is printed on every run, because it is the number that matters.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="review-bot",
        description="Propose findings, then try to disprove each one. Report only survivors.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("diff", help="review what a change added")
    d.add_argument("repo")
    d.add_argument("--ref", default="HEAD~1", help="compare against this ref")
    d.add_argument("--show-retracted", action="store_true")
    d.add_argument("--strict", action="store_true", help="exit 1 if anything is reported")
    d.set_defaults(func=_diff_command)

    s = sub.add_parser("scan", help="review whole files, to measure precision")
    s.add_argument("path")
    s.add_argument("--include-tests", action="store_true")
    s.add_argument("--show-retracted", action="store_true")
    s.add_argument("--json")
    s.set_defaults(func=_scan_command)

    r = sub.add_parser("rules", help="list proposers and whether they have defeaters")
    r.set_defaults(func=_rules_command)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
