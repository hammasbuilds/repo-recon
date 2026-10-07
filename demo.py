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
#: The file `--update-readme` rewrites. Patched by the test that exercises the writer.
README_PATH = HERE / "README.md"


def update_readme(output: str, mapped: dict) -> int:
    """Write this run's output into the README. Returns 0 on success.

    This exists because the tool measures the repository it lives in, so every commit
    that adds a test moves the module, symbol and line counts and both resolution rates.
    `test_the_readme_states_this_repositorys_real_size` therefore fails on the commit
    that adds a test - correctly - and the remedy was to hand-edit four numbers in two
    places by eye, which is a transcription step and so a place to get one wrong.
    """
    # Module-level so a test can point it elsewhere. `HERE` cannot be reused for
    # that: it is also what the tools are told to measure, so moving it measures the
    # wrong tree. The only writer in a repository full of read-only tools should be
    # able to run with nothing at stake.
    path = README_PATH
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


#: Where the constructed review example is written. Under the scratch directory the
#: demo already uses, and deleted immediately after.
_EXAMPLE_DIR = HERE / "build" / "_demo_example"

#: Four functions, each chosen for a different outcome from `review_diff`:
#:
#:   * `tally` mutates a shared mutable default, which is the bug;
#:   * `count` has the same default and never mutates it, so the per-rule defeater
#:     disproves it - the thing this tool exists to do and the thing its own published
#:     output never showed;
#:   * `fetch` has one the author marked `# noqa: B006`, which is an acknowledgement
#:     rather than a disproof and is counted apart from one;
#:   * `read_all` swallows an exception, and nothing defeats it.
EXAMPLE_SOURCE = '''def tally(seen=[]):
    """A shared mutable default that IS mutated: every caller sees the last call."""
    seen.append(1)
    return len(seen)


def count(items=[]):
    """The same default, never mutated. Not a bug, and a checker that says it is will
    be ignored on the one that is."""
    return len(items)


def fetch(cache={}):  # noqa: B006
    """Marked by the author, with the code of the check they silenced."""
    cache["x"] = 1
    return cache


def read_all(paths):
    """A swallowed exception with nothing to say for itself."""
    out = []
    for path in paths:
        try:
            out.append(open(path).read())
        except Exception:
            pass
    return out
'''


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
_PIN = re.compile(r"^  measured at ([0-9a-f]{7,40})", re.MULTILINE)


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
    # Every row the call asked for. This sliced `[:4]` after asking for `top=5`, so the
    # published list dropped its last row silently - and a reader comparing the output
    # with the field cannot tell a short list from a truncated one.
    print("entry points - called by nothing in this repository:")
    for card in report["entry_points"]:
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
    # `files_read`, not `files_scanned`: the second is what was found and the first is
    # what the counts are over. This printed `files_scanned` alone, so a directory of
    # four unparseable files read "4 files: 0 mechanical, 0 behavioural" - a clean bill
    # of health for a scan that read nothing. The skipped count is printed when it is
    # not zero, because a reader cannot tell 0-of-21 from 21-of-21 otherwise.
    print(
        f"{scan['files_read']} of {scan['files_scanned']} files read: "
        f"{scan['mechanical']} mechanical, {scan['behavioural']} behavioural"
    )
    if scan["files_skipped"]:
        print(f"  {scan['files_skipped']} skipped (unreadable, too large, or not parseable)")
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

    # And the half of this tool that its own output never showed. Over this package the
    # checks propose two findings and retract none, so the published demo was a disproof
    # engine demonstrating zero disproofs - for the feature the README headlines. The
    # machinery works; this repository is simply not the input that exercises it.
    #
    # A constructed example, labelled as one, because a demo of a disproof needs
    # something to disprove. These four functions are not in this repository.
    print()
    print("the same checks over four functions written to exercise them:")
    example = _EXAMPLE_DIR / "example.py"
    example.parent.mkdir(parents=True, exist_ok=True)
    example.write_text(EXAMPLE_SOURCE, encoding="utf-8")
    try:
        shown = server.review_diff({"path": str(example)})
    finally:
        example.unlink(missing_ok=True)
    print(
        f"  {shown['proposed']} proposed: {shown['confirmed']} confirmed, "
        f"{shown['disproved']} disproved, {shown['acknowledged']} acknowledged"
    )
    for finding in shown["findings"]:
        print(f"  kept     line {finding['line']}: {finding['rule']}")
    for row in shown["retractions"]:
        print(f"  dropped  line {row['line']}: {row['rule']} - {row['reason']}")

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
        # "audit", not "release". `last` is omitted here, and the tool's own schema
        # says so: "Omitted, the whole read history is assessed, which is an audit
        # rather than a release decision." The heading said "release_readiness -
        # scored from the history, not from opinion" over a verdict about all 29
        # commits, which is not a release decision about any of them.
        rule("release_readiness - the whole history audited, not a release decision")
        gate = server.release_readiness({"path": str(HERE), "rev": rev})
        print(f"verdict: {gate['verdict']} over {gate['commits_considered']} commits")
        # The sha alone. The distance from HEAD belongs here by the same argument -
        # pinned and twenty commits behind looks exactly like pinned and current - and
        # it cannot be published: committing the block changes the distance it states,
        # which is the circularity this whole arrangement exists to remove, one level
        # up. It was printed here for one commit and the suite went red on the commit
        # that carried it.
        #
        # So the sha is published, because it survives being committed, and the
        # distance is enforced by `test_the_pin_has_not_fallen_far_behind_head` -
        # computed at the moment of asking, where a moving number belongs.
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
