"""The README's numbers, checked against the code they describe.

Every repository in this portfolio that stated a count in prose eventually stated a
wrong one — and the drift is never noticed by the person who caused it, because the
README is not the file they were editing. So the numbers that can be recomputed are
recomputed here, and `demo.py` is where the README's output block comes from.

The git-derived half of that output is deliberately not pinned: commit subjects and risk
scores move whenever anyone commits, and a test that fails on every commit teaches
people to ignore it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from reporecon.mcp_server import TOOLS, ReconServer

ROOT = Path(__file__).resolve().parent.parent
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


def test_update_readme_is_idempotent_on_a_current_readme():
    """So "the README block is this command's output" is a checkable statement.

    The two README tests above assert the four numbers. This asserts the whole block:
    re-running the writer against a current README must find nothing to change. If it
    rewrites anything, the block differs from the command's stdout somewhere the number
    checks do not look - a reordered section, a dropped line, an edited comment.

    It also means the remedy for a stale README is one command rather than hand-editing
    four figures in two places, which is where a transcription error would come from.
    """
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("reporecon_demo", root / "demo.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)

    before = (root / "README.md").read_text(encoding="utf-8")
    try:
        assert demo.main(["--update-readme"]) == 0
        after = (root / "README.md").read_text(encoding="utf-8")
        assert after == before, (
            "`python demo.py --update-readme` changed the README, so the block in it is "
            "not this command's output. Run that command and commit the result."
        )
    finally:
        # Restored whatever happened: a test that leaves the repository edited when it
        # fails makes the next run's diff unreadable.
        (root / "README.md").write_text(before, encoding="utf-8")


def test_the_readme_documents_the_command_that_regenerates_it():
    """A remedy nobody can find is not a remedy."""
    assert "--update-readme" in README
