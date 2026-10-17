r"""What share of other people's calls `map_repo` can bind to a definition.

    python scripts/resolution_sweep.py                  # over D:\reporecon-corpus\src
    python scripts/resolution_sweep.py --work <dir>

The README quotes a resolution rate for this repository. This runs the same tool over
twelve real projects at pinned tags and prints the rate for each, because a resolver
measured only on the code it was written against is measured on its easiest case.

Both rates are printed, since which one you read is the whole point of that section of
the README: `repo_resolution_rate` is over calls to code the repository owns, and
`resolution_rate` is over every call site including builtins and third-party libraries -
the second is dominated by code the project does not contain and makes every repository
look unmappable.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from reporecon.mcp_server import ReconServer  # noqa: E402


def payload(answer: dict) -> dict:
    """The tool's own result.

    `ReconServer.call` returns it directly; the protocol wrapper (`result`, then
    `structuredContent` or a text block) is added by `_call_tool` on the way out. Read
    one level too high this returns `{}` for every project, and twelve real codebases
    print as "no result" — a null result produced by looking in the wrong place.
    """
    if not isinstance(answer, dict):
        return {}
    if "repo_resolution_rate" in answer or "modules" in answer:
        return answer
    result = answer.get("result") or {}
    data = result.get("structuredContent") or {}
    if data:
        return data
    for block in result.get("content") or []:
        if block.get("type") == "text":
            try:
                return json.loads(block.get("text") or "{}")
            except ValueError:
                return {}
    return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--work",
        default=r"D:\reporecon-corpus" if sys.platform == "win32" else "/tmp/reporecon-corpus",
    )
    args = parser.parse_args(argv)

    source = Path(args.work) / "src"
    projects = sorted(p for p in source.iterdir() if p.is_dir()) if source.is_dir() else []
    if not projects:
        print(f"  no corpus under {source}")
        print("  fetch it: python scripts/fetch_corpus.py")
        return 1

    server = ReconServer()
    print(f"\n  {len(projects)} project(s). 'repo' is over calls to code the project owns;")
    print("  'all' is over every call site, builtins and third-party included.\n")
    print(
        f"  {'project':<13} {'modules':>8} {'symbols':>8} {'lines':>9} "
        f"{'repo':>7} {'all':>7} {'seconds':>8}"
    )
    print("  " + "-" * 66)

    rates: list[float] = []
    shapes: dict[str, int] = {}
    for project in projects:
        started = time.time()
        data = payload(server.call("map_repo", {"path": str(project)}))
        took = time.time() - started
        if not data or not data.get("modules"):
            # Said rather than printed as 0.0%: a project whose directory was read as a
            # nested one is excluded entire, and a rate of zero over no modules is not a
            # measurement of the resolver.
            excluded = data.get("excluded_projects") or []
            why = f"excluded {excluded}" if excluded else f"no result: {str(data)[:40]}"
            print(f"  {project.name:<13} {'-':>8} {why}")
            continue
        rates.append(data["repo_resolution_rate"])
        for callee, count in data.get("unresolved_sample") or []:
            shape = shape_of(callee)
            shapes[shape] = shapes.get(shape, 0) + count
        print(
            f"  {project.name:<13} {data.get('modules', 0):>8,} {data.get('symbols', 0):>8,} "
            f"{data.get('loc', 0):>9,} {data['repo_resolution_rate']:>6.1%} "
            f"{data.get('resolution_rate', 0):>6.1%} {took:>8.1f}"
        )

    if rates:
        print(
            f"\n  repo resolution rate over {len(rates)} project(s): "
            f"median {statistics.median(rates):.1%}, "
            f"worst {min(rates):.1%}, best {max(rates):.1%}"
        )
    if shapes:
        # Where the unresolved calls actually are, because "59.9%" on its own says
        # nothing about whether the remainder is reachable. Grouped by the shape of the
        # call site rather than listed: one project's `client.get` and another's `s.get`
        # are the same problem.
        total = sum(shapes.values())
        print(f"\n  the unresolved calls, by shape ({total:,} in the samples)")
        for shape, count in sorted(shapes.items(), key=lambda kv: -kv[1]):
            print(f"    {count:>6,}  {count / total:>5.0%}  {shape}")
    return 0


def shape_of(callee: str) -> str:
    """Which kind of call site this is, for grouping."""
    head, _, rest = callee.partition(".")
    if not rest:
        return "a bare name"
    if head in ("self", "cls"):
        return "self.method"
    if head[:1].isupper():
        return "Class.method - a class this project owns"
    if rest.count(".") >= 1:
        return "a.b.c - more than one hop"
    return "variable.method - the variable's type is not stated anywhere"


if __name__ == "__main__":
    raise SystemExit(main())
