"""Point every tool at this repository and print what comes back.

    python demo.py

No arguments, no network, no model. The output in the README is this script's output,
so a README that has drifted is a demo that stopped running.
"""

from __future__ import annotations

import contextlib
import io
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from reporecon.mcp_server import ReconServer  # noqa: E402

HERE = Path(__file__).resolve().parent


def rule(title: str) -> None:
    print(f"\n{title}\n" + "-" * len(title))


#: The fenced block in the README that holds this script's output. Anchored on the
#: sentence that introduces it rather than on "the first fence": the README has several
#: and the one just above this sentence is a command line.
_LEAD = "`python demo.py`, which is where every number above comes from."
_FENCE = "```"


def update_readme(output: str, mapped: dict) -> int:
    """Write this run's output into the README. Returns 0 on success.

    This exists because the tool measures the repository it lives in, so every commit
    that adds a test moves the module, symbol and line counts and both resolution rates.
    `test_the_readme_states_this_repositorys_real_size` therefore fails on the commit
    that adds a test - correctly - and the remedy was to hand-edit four numbers in two
    places by eye, which is a transcription step and so a place to get one wrong.
    """
    path = HERE / "README.md"
    before = path.read_text(encoding="utf-8")

    lead = before.find(_LEAD)
    if lead < 0:
        print("could not find the sentence introducing the output block", file=sys.stderr)
        return 1
    opening = before.find(_FENCE, lead)
    closing = before.find(_FENCE, opening + len(_FENCE))
    if opening < 0 or closing < 0:
        print("the output block is not fenced", file=sys.stderr)
        return 1
    body_from = opening + len(_FENCE)
    after = before[:body_from] + chr(10) + output + before[closing:]

    # The two rates are quoted again in the prose above the block, in the sentence
    # whose whole point is which number is which. Rewriting the block and leaving
    # those would trade one stale figure for another.
    every = f"{mapped['resolution_rate']:.1%}"
    in_scope = f"{mapped['repo_resolution_rate']:.1%}"
    quoted = re.findall(r"\*\*([\d.]+%)\*\*", after[:body_from])
    for was, now in zip(quoted[:2], (every, in_scope), strict=False):
        head, sep, rest = after.partition("**" + was + "**")
        if sep:
            after = head + "**" + now + "**" + rest

    if after == before:
        print("README.md already matches this run")
        return 0
    path.write_text(after, encoding="utf-8")
    print("rewrote the output block and the two rates in README.md")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    writing = "--update-readme" in argv
    if writing:
        # Captured rather than printed twice: the README block has to be byte-identical
        # to what a reader sees, and reformatting it on the way in is how the two drift.
        buffer = io.StringIO()
        sink = contextlib.redirect_stdout(buffer)
    else:
        buffer, sink = None, contextlib.nullcontext()

    with sink:
        code, mapped = _run(ReconServer())

    if buffer is not None:
        output = buffer.getvalue().lstrip(chr(10))
        print(output)
        if code == 0:
            code = update_readme(output, mapped)
    return code


#: The line the published block carries, naming the commit the history figures are
#: over. Read back out of the README so a re-run reproduces them instead of measuring
#: a repository that has moved on.
_PIN = re.compile(r"^  measured at ([0-9a-f]{7,40})$", re.MULTILINE)


def _pinned_revision() -> str:
    """The revision the README's history figures were measured at, or HEAD.

    HEAD on the first run and whenever `--at-head` is passed, which is how the pin is
    created and how it is moved. Any other run reproduces what is published.
    """
    if "--at-head" in sys.argv[1:]:
        return "HEAD"
    readme = HERE / "README.md"
    if readme.is_file():
        found = _PIN.search(readme.read_text(encoding="utf-8"))
        if found:
            return found.group(1)
    return "HEAD"


def _run(server: ReconServer) -> tuple[int, dict]:
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
    print("core modules by PageRank over the import graph:")
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
        # The revision the published figures are over, which is NOT HEAD.
        #
        # Every history figure here moves on the next commit - including the commit
        # that publishes it. So `--update-readme` wrote a commit count, the commit
        # that carried the new README made it one short, and
        # `test_update_readme_is_idempotent_on_a_current_readme` then failed on every
        # commit after that. The README could not be correct for longer than it took
        # to commit it.
        #
        # Reading the revision back out of the README makes the measurement
        # reproducible rather than current: the block says which commit it is over,
        # and anyone can re-run it and get the same numbers. `--at-head` re-pins it,
        # deliberately, which is the only time these figures are allowed to change.
        rev = _pinned_revision()
        rule("release_readiness - scored from the history, not from opinion")
        gate = server.release_readiness({"path": str(HERE), "rev": rev})
        print(f"verdict: {gate['verdict']} over {gate['commits_considered']} commits")
        print(f"  measured at {gate['rev'][:12]}")
        for check in gate["checks"]:
            print(f"  {check['status']:5s} {check['name']}: {check['detail']}")

        rule("riskiest_commits - with the score's components")
        risk = server.riskiest_commits({"path": str(HERE), "limit": 3, "rev": rev})
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

    # The map, so `update_readme` can rewrite the two rates in the prose without
    # re-parsing the repository or re-deriving them from the text it just printed.
    return 0, report


if __name__ == "__main__":
    raise SystemExit(main())
