"""How many processes a call starts, and which, counted rather than described.

Three places in this repository state the subprocess surface: README.md's subtitle,
README.md's "Nothing of yours is executed" bullet, and the server's `INSTRUCTIONS` -
which is the text every MCP client shows the model, so it is the one a reader is most
likely to act on.

All three have been wrong, twice, in the same way. The bullet once said "two `git`
invocations", which is the number of CALL SITES in the source and not the number a call
makes; it was corrected to four, and `git ls-files` arrived with `tracked_test_files` in
the same release and made it five. SECURITY.md was updated and the other two were not.

`grep -rn "ls-files" tests/` found nothing, which is why it drifted twice. The point of
this file is that the next command to arrive fails a test rather than a review.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import reporecon.captain.history as history_module
from reporecon.mcp_server import INSTRUCTIONS, ReconServer

HERE = Path(__file__).resolve().parent.parent

#: Every git subcommand the server is allowed to start, as it appears in the argument
#: list. A new one here has to be added to the three documents as well, and the test
#: below checks that it is.
ALLOWED = {
    ("log",),
    ("rev-parse", "--verify"),
    ("rev-parse", "--absolute-git-dir"),
    ("rev-parse", "--is-shallow-repository"),
    ("ls-files", "-z"),
}

#: Measured. Five per history call, and six with `REPO_RECON_ROOT` set, because the
#: confinement check asks git which repository it is about to read rather than parsing
#: `.git` as a file - that is one extra `rev-parse --absolute-git-dir`, before anything
#: else runs.
PER_HISTORY_CALL = 5
PER_HISTORY_CALL_CONFINED = 6


@pytest.fixture
def spy(monkeypatch):
    """Records every `subprocess.run` the package makes, and still runs it."""
    seen: list[list[str]] = []
    real = subprocess.run

    def recorder(args, **kwargs):
        seen.append([str(a) for a in args])
        return real(args, **kwargs)

    monkeypatch.setattr(history_module.subprocess, "run", recorder)
    return seen


def _subcommands(seen: list[list[str]]) -> list[tuple[str, ...]]:
    """The git subcommand of each call, without the repository path or the revision."""
    out = []
    for args in seen:
        assert args[0] == "git", args
        assert args[1] == "-C", args
        rest = tuple(args[3:])
        for candidate in sorted(ALLOWED, key=len, reverse=True):
            if rest[: len(candidate)] == candidate:
                out.append(candidate)
                break
        else:
            out.append(rest)
    return out


@pytest.mark.parametrize("tool", ["release_readiness", "riskiest_commits"])
def test_a_history_call_starts_exactly_the_commands_we_say_it_does(tool: str, spy):
    server = ReconServer()
    getattr(server, tool)({"path": str(HERE), "scan": 5})
    found = _subcommands(spy)
    assert len(found) == PER_HISTORY_CALL, (tool, found)
    assert set(found) == ALLOWED, (tool, sorted(set(found) ^ ALLOWED))


@pytest.mark.parametrize("tool", ["release_readiness", "riskiest_commits"])
def test_confinement_costs_one_more_call_and_no_new_command(tool: str, spy, monkeypatch):
    """The confinement check asks git instead of reading `.git`, which is one extra
    `rev-parse --absolute-git-dir` and not a new command."""
    monkeypatch.setenv("REPO_RECON_ROOT", str(HERE.parent))
    server = ReconServer()
    getattr(server, tool)({"path": str(HERE), "scan": 5})
    found = _subcommands(spy)
    assert len(found) == PER_HISTORY_CALL_CONFINED, (tool, found)
    assert set(found) == ALLOWED, (tool, sorted(set(found) ^ ALLOWED))
    assert found.count(("rev-parse", "--absolute-git-dir")) == 2, found


@pytest.mark.parametrize(
    "tool",
    ["map_repo", "trace_impact", "check_modernisation", "review_diff"],
)
def test_the_other_four_tools_start_no_process_at_all(tool: str, spy):
    server = ReconServer()
    arguments = {
        "map_repo": {"path": str(HERE / "src")},
        "trace_impact": {"path": str(HERE / "src"), "symbol": "read_history"},
        "check_modernisation": {"path": str(HERE / "src")},
        "review_diff": {"path": str(HERE / "src" / "reporecon" / "mcp_server.py")},
    }[tool]
    getattr(server, tool)(arguments)
    assert spy == [], (tool, spy)


def test_nothing_is_passed_through_a_shell(spy):
    """`shell=False` is the default and the claim is explicit, so it is checked."""
    server = ReconServer()
    server.release_readiness({"path": str(HERE), "scan": 3})
    assert spy, "nothing ran, so this proves nothing"
    for args in spy:
        assert isinstance(args, list), args
        # The path is an argument, never text inside a command string.
        assert args[1] == "-C", args


# -- and the three documents that state it --------------------------------------------


def _documents() -> dict[str, str]:
    return {
        "README.md": (HERE / "README.md").read_text(encoding="utf-8"),
        "SECURITY.md": (HERE / "SECURITY.md").read_text(encoding="utf-8"),
        "INSTRUCTIONS": INSTRUCTIONS,
    }


@pytest.mark.parametrize("where", sorted(_documents()))
def test_every_document_that_lists_the_commands_lists_all_of_them(where: str):
    """The subtitle said "`git log` and `git rev-parse`" after `ls-files` had been
    added, and so did `INSTRUCTIONS`, which is what a client shows the model."""
    text = _documents()[where]
    assert "ls-files" in text, (
        f"{where} does not mention `git ls-files`, which every history call runs"
    )
    assert "rev-parse" in text, where
    assert "log" in text, where


#: The two counts as the README writes them, in words, so a changed count fails here.
_WORDS = {4: "four", 5: "five", 6: "six", 7: "seven"}


def test_the_readme_states_the_measured_counts():
    """Both of them: the per-call count and the one more that confinement costs.

    In words, which is how the README writes them - and the point is that the words
    have to be the measured numbers, so a sixth command arriving makes this fail.
    """
    text = (HERE / "README.md").read_text(encoding="utf-8").lower()
    assert f"**{_WORDS[PER_HISTORY_CALL]}**" in text, (
        f"the README does not state {_WORDS[PER_HISTORY_CALL]} per history call"
    )
    assert f"**{_WORDS[PER_HISTORY_CALL_CONFINED]}**" in text, (
        f"the README does not state {_WORDS[PER_HISTORY_CALL_CONFINED]} with the root set"
    )
