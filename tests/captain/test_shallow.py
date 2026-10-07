"""A truncated clone is reported as truncated.

A `--depth 1` clone holds one commit whose parents have been grafted away. It therefore
carries the whole tree as a single change - and `--no-merges` does not exclude it even
when it is a merge, because a graft has no parents to merge - so a reviewer pointing the
tool at one got `commits_considered: 1`, `median_churn: 6`, `median_files: 5` and a
verdict, every figure computed against a single synthetic commit that is the median of
itself.

Both workflow files in this repository set `fetch-depth: 0` for exactly this reason, so
the hazard was known to the people who wrote the tool and not to anyone calling it.

Nothing is refused. Asking about a shallow checkout is reasonable; answering as though
the commits that are missing are not missing is not.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reporecon.captain.gate import evaluate
from reporecon.captain.history import is_shallow, read_history


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture(scope="module")
def clones(tmp_path_factory) -> tuple[Path, Path]:
    """A real repository and a real `--depth 1` clone of it."""
    root = tmp_path_factory.mktemp("shallow")
    full = root / "full"
    full.mkdir()
    git(full, "init", "-q")
    git(full, "config", "user.email", "t@example.com")
    git(full, "config", "user.name", "Tester")
    for i in range(8):
        (full / f"src_m{i}.py").write_text(f"x = {i}\n", encoding="utf-8")
        git(full, "add", "-A")
        git(full, "commit", "-q", "-m", f"commit {i}")

    shallow = root / "shallow"
    subprocess.run(
        ["git", "clone", "--depth", "1", "-q", full.as_uri(), str(shallow)],
        check=True,
        capture_output=True,
    )
    return full, shallow


def test_a_full_clone_is_not_called_shallow(clones):
    full, _ = clones
    assert is_shallow(full) is False
    history = read_history(full)
    assert history.shallow is False
    assert history.caveat == ""
    assert len(history.commits) == 8


def test_a_shallow_clone_says_so(clones):
    _, shallow = clones
    assert is_shallow(shallow) is True
    history = read_history(shallow)
    assert history.shallow is True
    assert len(history.commits) == 1, [c.subject for c in history.commits]


def test_the_caveat_names_what_is_missing_and_why_it_matters(clones):
    _, shallow = clones
    caveat = read_history(shallow).caveat
    assert "shallow clone" in caveat
    assert "1 commit(s) are present" in caveat
    # The consequence, not just the fact: every repo-relative figure is computed
    # against a history that is not this repository's history.
    assert "median" in caveat


def test_the_gate_still_answers_over_a_shallow_clone(clones):
    """Refusing would be the other failure. The verdict is about the commits that are
    there, and the caveat is what says which commits those are."""
    _, shallow = clones
    history = read_history(shallow)
    result = evaluate(history)
    assert result.checks
    assert result.commits_considered == 1
    assert history.caveat
