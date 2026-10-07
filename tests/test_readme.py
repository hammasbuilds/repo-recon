"""The README's numbers, checked against the code they describe.

Every repository in this portfolio that stated a count in prose eventually stated a
wrong one — and the drift is never noticed by the person who caused it, because the
README is not the file they were editing. So the numbers that can be recomputed are
recomputed here, and `demo.py` is where the README's output block comes from.

The git-derived half of that output IS pinned, to the revision the block names. This
docstring said the opposite - "deliberately not pinned: commit subjects and risk scores
move whenever anyone commits, and a test that fails on every commit teaches people to
ignore it" - which described the arrangement that was replaced, and the reason it was
replaced is in that sentence: a figure which fails on every commit is a figure nobody
can check, so it was both unpinned and unpinnable.

The replacement trades one failure mode for another, so both are watched here. The old
one was always stale and always failing. The new one is a pin that can freeze
arbitrarily far behind HEAD with a green suite, which `test_the_pin_has_not_fallen_far_behind_head`
is for: ancestry alone says the figure was measured here, not that it is still worth
reading.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from reporecon.mcp_server import TOOLS, ReconServer

ROOT = Path(__file__).resolve().parent.parent
#: How far behind HEAD the published history figures may be before the suite asks
#: for a re-pin. Generous on purpose: re-pinning is deliberate and a few commits of
#: drift is the normal state between them. What this catches is a pin nobody has
#: looked at in a long time, which the old arrangement could not have - it failed
#: on every commit instead.
PIN_DRIFT_LIMIT = 25

README = (ROOT / "README.md").read_text(encoding="utf-8")


def prose() -> str:
    """The README with its fenced blocks removed.

    A claim is something the README asserts in its own voice. The demo output it embeds
    is not a claim — the first version of the test below failed on "376 tests" inside a
    quoted commit subject, which was the output doing its job.
    """
    kept: list[str] = []
    inside = False
    for line in README.splitlines():
        if line.startswith("```"):
            inside = not inside
            continue
        if not inside:
            kept.append(line)
    return "\n".join(kept)


@pytest.fixture(scope="module")
def mapped() -> dict:
    return ReconServer().map_repo({"path": str(ROOT), "top": 5})


def test_the_readme_states_this_repositorys_real_size(mapped: dict):
    match = re.search(r"(\d[\d,]*) modules, (\d[\d,]*) symbols, (\d[\d,]*) lines", README)
    assert match, "the README no longer states modules/symbols/lines"
    modules, symbols, loc = (int(group.replace(",", "")) for group in match.groups())
    assert (modules, symbols, loc) == (mapped["modules"], mapped["symbols"], mapped["loc"]), (
        f"README is stale: it says {modules} modules, {symbols} symbols, {loc:,} lines; "
        f"the code is {mapped['modules']}, {mapped['symbols']}, {mapped['loc']:,}. "
        "Re-run `python demo.py` and paste its output into the README."
    )


def test_the_readme_quotes_both_resolution_rates_correctly(mapped: dict):
    """The section's whole point is which number is which, so both must be right."""
    quoted = re.findall(r"\*\*([\d.]+%)\*\*", prose())
    assert len(quoted) >= 2, "the README no longer quotes two rates"
    every_call, in_scope = quoted[0], quoted[1]
    # Compared as the demo formats them, not as floats with a tolerance: at one decimal
    # place a tolerance of half a step sits exactly on the boundary and the first
    # version of this test failed on 27.6 vs 27.55.
    assert every_call == f"{mapped['resolution_rate']:.1%}"
    assert in_scope == f"{mapped['repo_resolution_rate']:.1%}"
    # The honest one must be the larger. If these ever cross, the sentence explaining
    # why the first measures nothing has stopped being true.
    assert float(in_scope.rstrip("%")) > float(every_call.rstrip("%"))


def test_the_readme_lists_every_tool_the_server_exposes():
    named = set(re.findall(r"\*\*`(\w+)`\*\*", prose()))
    exposed = {tool["name"] for tool in TOOLS}
    assert exposed <= named, f"not documented: {sorted(exposed - named)}"


def test_the_readme_claims_no_test_count():
    """A count in prose goes stale. CI and `pytest -q` are the record."""
    found = re.search(r"\b\d+ tests\b", prose())
    assert not found, f"the README states {found.group(0)!r}; let CI and `pytest -q` be the record"


def test_the_readme_promises_only_what_security_md_does():
    """Both files make the same four claims, and they must not drift apart."""
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    for claim in ("No network", "No writes", "No dependencies"):
        assert claim in prose(), f"README dropped {claim!r}"
        assert claim.lower() in security.lower(), f"SECURITY.md dropped {claim!r}"


# -- the block is the command's output, byte for byte -------------------------


def test_update_readme_is_idempotent_on_a_current_readme(tmp_path):
    """So "the README block is this command's output" is a checkable statement.

    The two README tests above assert the four numbers. This asserts the whole block:
    re-running the writer against a current README must find nothing to change. If it
    rewrites anything, the block differs from the command's stdout somewhere the number
    checks do not look - a reordered section, a dropped line, an edited comment.

    It also means the remedy for a stale README is one command rather than hand-editing
    four figures in two places, which is where a transcription error would come from.

    Run against a COPY of the README, not the tracked one. This used to write the real
    file and restore it in a `finally`, which is the whole project's one writer: every
    tool here is read-only and says so, `SECURITY.md` makes a point of it, and the
    thing editing the repository was the test suite. An interrupt or a crash inside
    the writer left the checkout edited - and a failing run still printed "rewrote the
    output block and the two rates in README.md", which is alarming in a repository
    whose claim is that nothing writes.

    `demo.HERE` is where the writer looks, so pointing it at a temp directory holding
    a copy exercises the same code with nothing at stake.
    """
    import importlib.util
    import shutil

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("reporecon_demo", root / "demo.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)

    before = (root / "README.md").read_text(encoding="utf-8")
    sandbox = Path(tmp_path) / "readme"
    sandbox.mkdir()
    shutil.copy(root / "README.md", sandbox / "README.md")
    demo.README_PATH = sandbox / "README.md"

    assert demo.main(["--update-readme"]) == 0
    after = (sandbox / "README.md").read_text(encoding="utf-8")
    assert after == before, (
        "`python demo.py --update-readme` changed the README, so the block in it is "
        "not this command's output. Run that command and commit the result."
    )
    # The tracked file was not touched, which is the point of the copy.
    assert (root / "README.md").read_text(encoding="utf-8") == before


def test_the_readme_documents_the_command_that_regenerates_it():
    """A remedy nobody can find is not a remedy."""
    assert "--update-readme" in README


# -- the published history figures name the commit they are over --------------------


def test_the_readme_names_the_revision_its_history_figures_are_over():
    """Because otherwise they are a claim about whenever anyone last ran the command.

    This tool measures the repository it lives in, so `release_readiness` and
    `riskiest_commits` move on the next commit - including the commit that publishes
    them. `demo.py --update-readme` wrote "over 28 commits", the commit carrying that
    README made it 29, and `test_update_readme_is_idempotent_on_a_current_readme`
    failed from then on. The README could not be right for longer than it took to
    commit it, and the test that said so was correct every time.

    The block now carries `measured at <sha>` and the demo reads it back, so a re-run
    reproduces what is published instead of measuring a repository that has moved on.
    `--at-head` re-pins, which is the only thing that may change these figures.
    """
    import re

    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    pinned = re.search(r"^  measured at ([0-9a-f]{7,40})", readme, re.MULTILINE)
    assert pinned, "the README no longer records the revision its history is over"
    assert len(pinned.group(1)) >= 12, pinned.group(1)


def test_the_pinned_revision_is_a_commit_in_this_repository():
    """A sha nobody can resolve is not a record of anything. An ancestor of HEAD, so
    the figures were measured here rather than copied from somewhere else."""
    import re
    import subprocess

    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    sha = re.search(r"^  measured at ([0-9a-f]{7,40})", readme, re.MULTILINE).group(1)

    verified = subprocess.run(
        ["git", "rev-parse", "--verify", f"{sha}^{{commit}}"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert verified.returncode == 0, verified.stderr
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", sha, "HEAD"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert ancestor.returncode == 0, f"{sha} is not an ancestor of HEAD"


def test_the_published_commit_count_is_what_that_revision_gives():
    """The figure, re-derived at the revision it names.

    This is the assertion the old arrangement could not make: with the measurement
    taken at HEAD the number was right only until the next commit, so nothing could
    check it. Taken at a named revision it is checkable for as long as the commit
    exists, which is the whole point of writing the revision down.
    """
    import re
    import sys

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    from reporecon.mcp_server import ReconServer

    readme = (root / "README.md").read_text(encoding="utf-8")
    sha = re.search(r"^  measured at ([0-9a-f]{7,40})", readme, re.MULTILINE).group(1)
    stated = re.search(r"^verdict: (\w+) over (\d+) commits$", readme, re.MULTILINE)
    assert stated, "the README no longer states the verdict and the commit count"

    result = ReconServer().release_readiness({"path": str(root), "rev": sha})
    assert result["verdict"] == stated.group(1), (result["verdict"], stated.group(1))
    assert result["commits_considered"] == int(stated.group(2)), (
        result["commits_considered"],
        stated.group(2),
    )
    assert result["rev"].startswith(sha), (result["rev"], sha)


def test_the_reported_revision_is_always_a_sha():
    """This test was a tautology, and the field under it was broken.

    It read:

        at_head = server.release_readiness({"path": str(root)})
        assert at_head["rev"], at_head
        assert "rev" in at_head

    `"HEAD"` is truthy, so the first assertion passed on the broken value; the second
    cannot fail after the first has already subscripted the key. Its own docstring
    conceded "No assertion that they differ." Under it, `_rev` returned the literal
    string `"HEAD"` whenever `rev` was omitted - the default path, every call - in a
    field whose schema says "The full sha the history was read up to", while passing
    `rev: "HEAD"` explicitly gave the sha.

    A branch pointer is the "currently" that naming the revision exists to abolish, so
    the assertion is on the shape: forty hex characters, however the question was
    asked.
    """
    import re
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    from reporecon.mcp_server import ReconServer

    server = ReconServer()
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout.strip()

    for arguments in (
        {"path": str(root)},
        {"path": str(root), "rev": "HEAD"},
    ):
        result = server.release_readiness(arguments)
        assert re.fullmatch(r"[0-9a-f]{40}", result["rev"]), (arguments, result["rev"])
        assert result["rev"] == head, (arguments, result["rev"], head)

    # An earlier revision is a different question and says so.
    earlier = server.release_readiness({"path": str(root), "rev": "HEAD~2"})
    assert re.fullmatch(r"[0-9a-f]{40}", earlier["rev"]), earlier["rev"]
    assert earlier["rev"] != head
    assert (
        earlier["commits_considered"]
        < server.release_readiness({"path": str(root)})["commits_considered"]
    )


def test_the_pin_has_not_fallen_far_behind_head():
    """Ancestry says the figure was measured here. It does not say it is still worth
    reading.

    The arrangement this replaced was always stale and always failing, which is a bad
    failure mode but a loud one. This one is quiet: a pin twenty commits behind looks
    exactly like a pin on HEAD, the suite is green either way, and nothing in the
    output said how far back it was. An independent review demonstrated it - three
    commits past the pin, 232 tests passing, no warning anywhere.

    So the block prints the distance and this bounds it. The limit is generous because
    re-pinning is a deliberate act and a few commits of drift is the normal state
    between them; what it catches is a pin nobody has looked at in a while.
    """
    import re
    import subprocess

    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    pinned = re.search(r"^  measured at ([0-9a-f]{7,40})", readme, re.MULTILINE)
    assert pinned, "the README no longer records the revision its history is over"

    behind = subprocess.run(
        ["git", "rev-list", "--count", f"{pinned.group(1)}..HEAD"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert behind.returncode == 0, behind.stderr
    distance = int(behind.stdout.strip() or 0)
    assert distance <= PIN_DRIFT_LIMIT, (
        f"the published history figures were measured {distance} commits ago; "
        f"run `python demo.py --update-readme --at-head` and commit the result"
    )


def test_the_block_states_the_distance_it_is_behind():
    """A reader cannot see the drift unless the output says it, and the suite cannot
    be the only place it is visible."""
    import re
    import subprocess

    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    stated = re.search(
        r"^  measured at ([0-9a-f]{7,40}), (\d+) commit\(s\) behind HEAD", readme, re.MULTILINE
    )
    assert stated, "the block no longer states how far behind the pin is"

    behind = subprocess.run(
        ["git", "rev-list", "--count", f"{stated.group(1)}..HEAD"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert int(stated.group(2)) == int(behind.stdout.strip() or 0), (
        stated.group(2),
        behind.stdout.strip(),
    )
