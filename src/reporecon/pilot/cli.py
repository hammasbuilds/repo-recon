"""Command line for migration-pilot. argparse only - the core has no dependencies."""

from __future__ import annotations

import argparse
import difflib
import json
import sys
import warnings
from collections import Counter
from pathlib import Path

from .apply import modernise_until_stable
from .rules import ALL_RULES, DEFAULT_RULES, MECHANICAL, RULE_KIND, scan_source

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


def _selected_rules(args: argparse.Namespace) -> set[str]:
    if args.rule:
        unknown = set(args.rule) - set(DEFAULT_RULES)
        if unknown:
            print(f"unknown rule(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            raise SystemExit(2)
        return set(args.rule)
    return set(DEFAULT_RULES)


def _scan_command(args: argparse.Namespace) -> int:
    target = Path(args.path).resolve()
    rules = _selected_rules(args)
    counts: Counter = Counter()
    files_with_edits = 0
    errors = 0
    rows = []

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        for path in _python_files(target):
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scan = scan_source(str(path), source, rules)
            if scan.parse_error:
                errors += 1
                continue
            if not scan.edits:
                continue
            files_with_edits += 1
            rows.append((path, scan))
            for edit in scan.edits:
                counts[edit.rule] += 1

    mechanical = sum(v for k, v in counts.items() if RULE_KIND[k] == MECHANICAL)
    behavioural = sum(counts.values()) - mechanical

    for path, scan in rows[: args.limit]:
        try:
            shown = path.relative_to(target)
        except ValueError:
            shown = path
        print(f"{shown}")
        for edit in scan.edits:
            mark = "auto " if edit.mechanical else "REVIEW"
            print(f"  [{mark}] line {edit.line:5d}  {edit.rule}")
            print(f"           {edit.before.strip()[:60]}  ->  {edit.replacement[:60]}")
            if not edit.mechanical and args.verbose:
                print(f"           {edit.note}")
    if len(rows) > args.limit:
        print(f"... and {len(rows) - args.limit} more files")

    print()
    print(f"  files with edits : {files_with_edits}")
    print(f"  parse errors     : {errors}")
    print(f"  mechanical       : {mechanical}   (provably equivalent, safe to apply)")
    print(f"  behavioural      : {behavioural}   (changes meaning, never auto-applied)")
    for rule, n in counts.most_common():
        print(f"      {RULE_KIND[rule]:12s} {rule:30s} {n}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "files_with_edits": files_with_edits,
                    "parse_errors": errors,
                    "mechanical": mechanical,
                    "behavioural": behavioural,
                    "by_rule": dict(counts),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"wrote {args.json}")
    return 0


def _apply_command(args: argparse.Namespace) -> int:
    """Apply mechanical edits only. Behavioural edits are never touched."""
    target = Path(args.path).resolve()
    rules = {r for r in _selected_rules(args) if RULE_KIND[r] == MECHANICAL}
    changed = 0
    edits = 0
    rejected: list[tuple[str, str]] = []

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        for path in _python_files(target):
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            result, applied, rejections = modernise_until_stable(str(path), source, rules)
            for reason in rejections:
                rejected.append((str(path), reason))
            if result == source:
                continue
            changed += 1
            edits += len(applied)
            if args.write:
                path.write_text(result, encoding="utf-8")
            else:
                diff = difflib.unified_diff(
                    source.splitlines(keepends=True),
                    result.splitlines(keepends=True),
                    fromfile=str(path), tofile=str(path), n=1,
                )
                sys.stdout.writelines(diff)

    print()
    verb = "rewrote" if args.write else "would rewrite"
    print(f"  {verb} {changed} file(s), {edits} edit(s)")
    if not args.write:
        print("  dry run - pass --write to apply")
    for path, reason in rejected:
        print(f"  REJECTED {path}: {reason}")
    return 0


def _rules_command(_: argparse.Namespace) -> int:
    print("Rules, and whether applying one can change what the program does:\n")
    for name, kind, description in ALL_RULES:
        print(f"  {kind:12s} {name}")
        print(f"               {description}")
    print("\nMechanical rewrites are verified AST-equivalent once annotations are")
    print("stripped; if one changes executable code the file is left untouched.")
    print("Behavioural rewrites are reported and never applied - run")
    print("`python scripts/prove_utcnow.py` for why.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="migration-pilot",
        description="Modernise Python mechanically, and refuse to when it would change meaning.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="report what could change, apply nothing")
    s.add_argument("path")
    s.add_argument("--rule", action="append", help="restrict to this rule (repeatable)")
    s.add_argument("--limit", type=int, default=25, help="files to list")
    s.add_argument("--verbose", "-v", action="store_true")
    s.add_argument("--json")
    s.set_defaults(func=_scan_command)

    a = sub.add_parser("apply", help="apply mechanical edits (dry run by default)")
    a.add_argument("path")
    a.add_argument("--rule", action="append")
    a.add_argument("--write", action="store_true", help="actually modify files")
    a.set_defaults(func=_apply_command)

    r = sub.add_parser("rules", help="list the rules and their classification")
    r.set_defaults(func=_rules_command)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
