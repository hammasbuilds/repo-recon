"""History tests against real git repositories built in tmp_path.

Parsing `git log --numstat` has enough edge cases - renames, binary files,
empty commits, unicode subjects - that mocking the output would only test the
fixtures. These build actual repositories and run actual git.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from captain.history import (
    Commit,
    FileChange,
    NotAGitRepository,
    is_repository,
    read_history,
)


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "demo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "Tester")
    return root


def commit(repo: Path, message: str, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


# -- classification -------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "is_test"),
    [
        ("tests/test_x.py", True),
        ("src/pkg/test_helper.py", True),
        ("src/pkg/core.py", False),
        ("test/thing.py", True),
        ("src/latest/core.py", False),  # "latest" contains "test" but is not one
    ],
)
def test_test_files_are_recognised(path: str, is_test: bool):
    assert FileChange(path, 1, 0).is_test is is_test


def test_source_excludes_tests_and_docs():
    assert FileChange("src/a.py", 1, 0).is_source
    assert not FileChange("tests/test_a.py", 1, 0).is_source
    assert not FileChange("README.md", 1, 0).is_source
    assert FileChange("README.md", 1, 0).is_doc


def test_root_level_files_group_into_one_area():
    """Without grouping, README.md and .gitignore each looked like an 'area'."""
    c = Commit(sha="x", author="a", when=None, subject="s", files=[
        FileChange("README.md", 1, 0),
        FileChange(".gitignore", 1, 0),
        FileChange("src/a.py", 1, 0),
    ])
    assert c.areas == {"(root)", "src"}
    assert c.spread == 2


# -- reading real history -------------------------------------------------


def test_reads_commits_newest_first(repo: Path):
    commit(repo, "first", {"a.py": "x = 1\n"})
    commit(repo, "second", {"b.py": "y = 2\n"})
    history = read_history(repo)
    assert [c.subject for c in history.commits] == ["second", "first"]


def test_line_counts_are_parsed(repo: Path):
    commit(repo, "add", {"a.py": "1\n2\n3\n"})
    commit(repo, "edit", {"a.py": "1\n2\n3\n4\n5\n"})
    latest = read_history(repo).commits[0]
    assert latest.added == 2
    assert latest.deleted == 0
    assert latest.churn == 2


def test_deletions_are_counted(repo: Path):
    commit(repo, "add", {"a.py": "1\n2\n3\n4\n"})
    commit(repo, "trim", {"a.py": "1\n"})
    latest = read_history(repo).commits[0]
    assert latest.deleted == 3
    assert latest.added == 0


def test_touches_source_and_tests_are_distinguished(repo: Path):
    commit(repo, "code only", {"src/a.py": "x = 1\n"})
    commit(repo, "code and test", {"src/b.py": "y = 2\n", "tests/test_b.py": "pass\n"})
    newest, older = read_history(repo).commits
    assert newest.touches_source and newest.touches_tests
    assert older.touches_source and not older.touches_tests


def test_docs_only_commit_is_flagged(repo: Path):
    commit(repo, "init", {"a.py": "x = 1\n"})
    commit(repo, "docs", {"README.md": "hello\n"})
    assert read_history(repo).commits[0].is_docs_only


def test_unicode_subject_survives(repo: Path):
    commit(repo, "اردو normalisation", {"a.py": "x = 1\n"})
    assert "اردو" in read_history(repo).commits[0].subject


def test_limit_caps_the_number_of_commits(repo: Path):
    for i in range(5):
        commit(repo, f"c{i}", {f"f{i}.py": f"x = {i}\n"})
    assert len(read_history(repo, limit=2).commits) == 2


def test_binary_files_do_not_crash_the_parser(repo: Path):
    (repo / "blob.bin").write_bytes(bytes(range(256)))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "binary")
    latest = read_history(repo).commits[0]
    assert [f.path for f in latest.files] == ["blob.bin"]
    assert latest.churn == 0  # git reports "-" for binary, counted as zero


def test_rename_keeps_the_new_path(repo: Path):
    commit(repo, "add", {"old.py": "x = 1\n" * 20})
    git(repo, "mv", "old.py", "new.py")
    git(repo, "commit", "-q", "-m", "rename")
    paths = [f.path for f in read_history(repo).commits[0].files]
    assert any("new.py" in p for p in paths)


def test_a_non_repository_is_refused(tmp_path: Path):
    assert not is_repository(tmp_path)
    with pytest.raises(NotAGitRepository):
        read_history(tmp_path)


def test_co_change_pairs_files_that_move_together(repo: Path):
    commit(repo, "one", {"src/a.py": "x=1\n", "src/b.py": "y=1\n"})
    commit(repo, "two", {"src/a.py": "x=2\n", "src/b.py": "y=2\n"})
    pairs = read_history(repo).co_change()
    assert pairs["src/a.py"]["src/b.py"] == 2
