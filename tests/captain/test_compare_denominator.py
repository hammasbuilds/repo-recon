"""A survey says how many checkouts it surveyed, and which it did not.

`release-captain compare <folder>` prints a per-repository table and then three means.
`--min-commits` (default 8) dropped a repository with a bare `continue`, three lines
below a comment saying what is wrong with that: "Skipped in silence, the survey's
denominator shrank with no sign of it." A reviewer pointed it at six repositories, five
fell out, and the summary read

    churn vs files  mean excess over chance = +50%

over a single repository, with the denominator nowhere in the output. This is the
command whose figures the kept READMEs in `docs/` quote.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reporecon.captain.cli import main

ROOT = Path(__file__).resolve().parents[2]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def _repo(parent: Path, name: str, commits: int) -> Path:
    root = parent / name
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "Tester")
    for i in range(commits):
        (root / f"src_m{i}.py").write_text(f"x = {i}\n", encoding="utf-8")
        (root / f"tests_t{i}.py").write_text(f"assert {i} == {i}\n", encoding="utf-8")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", f"commit {i}")
    return root


@pytest.fixture(scope="module")
def folder(tmp_path_factory) -> Path:
    """Six checkouts: one big enough to survey, five that are not."""
    parent = tmp_path_factory.mktemp("folder")
    _repo(parent, "surveyed", 12)
    for i in range(5):
        _repo(parent, f"too-small-{i}", 2)
    return parent


def test_the_dropped_checkouts_are_counted_and_named(folder: Path, capsys):
    assert main(["compare", str(folder)]) == 0
    out = capsys.readouterr().out

    assert "5 of 6 checkout(s) not surveyed" in out, out
    for i in range(5):
        assert f"too-small-{i}" in out, out
    assert "under --min-commits 8" in out, out


def test_the_mean_carries_the_number_it_is_over(folder: Path, capsys):
    """ "+50%" and "+50% over 1 repository" are different claims."""
    assert main(["compare", str(folder)]) == 0
    out = capsys.readouterr().out
    for key in ("churn vs files", "churn vs spread", "files vs spread"):
        line = next(ln for ln in out.splitlines() if ln.startswith(key))
        assert "over 1 repository" in line, line


def test_a_sample_too_small_to_be_a_survey_says_so(folder: Path, capsys):
    assert main(["compare", str(folder)]) == 0
    out = capsys.readouterr().out
    assert "is not a survey" in out, out
    assert "not because they mean anything at this size" in out, out


def test_a_real_survey_does_not_carry_that_warning(tmp_path, capsys):
    """The note has to be quiet when the sample is adequate, or it is noise on every
    run and a reader stops seeing it."""
    parent = tmp_path / "many"
    parent.mkdir()
    for i in range(3):
        _repo(parent, f"repo-{i}", 12)
    assert main(["compare", str(parent)]) == 0
    out = capsys.readouterr().out
    assert "is not a survey" not in out, out
    assert "over 3 repositories" in out, out
    assert "not surveyed" not in out, out
