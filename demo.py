"""Point every tool at this repository and print what comes back.

    python demo.py

No arguments, no network, no model. The output in the README is this script's output,
so a README that has drifted is a demo that stopped running.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from reporecon.mcp_server import ReconServer  # noqa: E402

HERE = Path(__file__).resolve().parent


def rule(title: str) -> None:
    print(f"\n{title}\n" + "-" * len(title))


def main() -> int:
    server = ReconServer()

    rule("map_repo - what is in here")
    report = server.map_repo({"path": str(HERE), "top": 5})
    print(f"{report['modules']} modules, {report['symbols']} symbols, {report['loc']:,} lines")
    print(
        f"call graph: {report['resolved_calls']} of {report['in_scope_calls']} in-scope "
        f"calls bound to a definition here ({report['repo_resolution_rate']:.1%})"
    )
    print(
        f"  the same number over EVERY call site, builtins included: "
        f"{report['resolution_rate']:.1%} - which measures nothing"
    )
    print("core modules by PageRank over the call graph:")
    for card in report["core_modules"][:5]:
        print(f"  {card['module']}")
    print("entry points - called by nothing in this repository:")
    for card in report["entry_points"][:4]:
        print(f"  {card['qualname']}")

    rule("trace_impact - what breaks if this changes")
    impact = server.trace_impact(
        {"path": str(HERE), "symbol": "reporecon.cartographer.parse.parse_repo"}
    )
    print(f"{impact['resolved']} is reached by {impact['total']} definition(s):")
    for depth, names in sorted(impact["callers_by_depth"].items()):
        print(f"  {depth}: {', '.join(sorted(names)[:4])}")

    rule("check_modernisation - and what it would change")
    scan = server.check_modernisation({"path": str(HERE / "src")})
    print(
        f"{scan['files_scanned']} files: {scan['mechanical']} mechanical, "
        f"{scan['behavioural']} behavioural"
    )
    print(f"  rules available: {', '.join(scan['rules_available'])}")
    if scan["by_rule"]:
        for name, count in scan["by_rule"].items():
            print(f"  {name}: {count}")
    else:
        print("  nothing to modernise - this package is already written for 3.10+")

    rule("review_diff - only what survived a disproof attempt")
    review = server.review_diff({"path": str(HERE / "src" / "reporecon" / "mcp_server.py")})
    print(
        f"{review['proposed']} proposed, {review['confirmed']} confirmed, "
        f"{review['retracted']} retracted"
    )
    for finding in review["findings"][:3]:
        print(f"  line {finding['line']}: {finding['rule']} - {finding['message']}")

    if (HERE / ".git").exists():
        rule("release_readiness - scored from the history, not from opinion")
        gate = server.release_readiness({"path": str(HERE)})
        print(f"verdict: {gate['verdict']} over {gate['commits_considered']} commits")
        for check in gate["checks"]:
            print(f"  {check['status']:5s} {check['name']}: {check['detail']}")

        rule("riskiest_commits - with the score's components")
        risk = server.riskiest_commits({"path": str(HERE), "limit": 3})
        print(
            f"a normal commit here is {risk['median_churn']:.0f} lines across "
            f"{risk['median_files']:.0f} files"
        )
        for commit in risk["commits"]:
            because = ", ".join(f"{b['factor']} {b['points']}" for b in commit["because"][:3])
            print(f"  {commit['score']:5.1f}  {commit['sha'][:8]}  {commit['subject'][:44]}")
            print(f"         {because}")
    else:
        print("\n(no .git here, so the two history tools are skipped)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
