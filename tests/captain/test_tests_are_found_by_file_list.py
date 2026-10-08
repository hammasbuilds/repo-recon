"""The fix that made the only blocking check unswitchable, which nothing tested.

`gate.py`'s first check can BLOCK, and it is gated on whether the repository has tests
at all. That used to be answered from `history.commits` — the SCAN WINDOW — so
`release_readiness(path, scan=3)` on a repository whose tests were committed four
commits ago saw no test file, concluded the project has none, and returned GO where the
default scan returns NO-GO. A caller's window decided whether the blocking check ran.

`tracked_test_files()` answers it from `git ls-files` instead, so it does not depend on
how far back anyone looked.

**Nothing in the suite executed any of it.** `grep -rn "test_files_tracked" tests/`
found nothing before this file, and an independent reviewer demonstrated it the way it
should be demonstrated: stubbing `tracked_test_files` to return `[]` and forcing
`test_files_tracked = 0` on every `read_history` result left **346 tests passing**. The
same mutation on `is_shallow` fails three tests and on the standout spread fails eight.
One of the three recent additions had no coverage at all, and it is the one holding up
the only check that can block a release.

So these tests are written against the thing the mutation breaks: the window is varied
while the file list is held constant, and the verdict has to stay put.
"""

from __future__ import annotations

import subprocess as sp
from pathlib import Path

import pytest

from reporecon.captain.gate import BLOCK, NOT_MEASURED, evaluate
from reporecon.captain.history import read_history, tracked_test_files


def _git(repo: Path, *args: str) -> str:
    done = sp.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return done.stdout


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-qm",
        message,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Tests committed FIRST, then four commits that touch only source.

    Which is the ordinary shape of a project: the test file was written once and the
    source keeps changing. A three-commit window sees no test file and the repository
    has forty of them.
    """
    root = tmp_path / "proj"
    root.mkdir()
    sp.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    (root / "tests").mkdir()
    (root / "src").mkdir()
    (root / "tests" / "test_core.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    (root / "src" / "core.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _commit(root, "the tests, written once")
    for n in range(4):
        (root / "src" / f"m{n}.py").write_text(f"def g{n}():\n    return {n}\n", encoding="utf-8")
        _commit(root, f"source change {n}, no tests")
    return root


def test_the_file_list_finds_the_test_file_whatever_the_window(repo: Path):
    """The function itself: it reads `git ls-files`, so there is no window in it."""
    tracked = tracked_test_files(repo)
    assert tracked == ["tests/test_core.py"], tracked


def test_read_history_carries_the_count_and_it_does_not_follow_the_scan(repo: Path):
    """The field the gate reads. One commit of history, one tracked test file."""
    for scan in (1, 2, 3, 50, None):
        history = read_history(repo, scan)
        assert history.test_files_tracked == 1, (scan, history.test_files_tracked)


def test_a_narrow_window_cannot_turn_the_blocking_check_off(repo: Path):
    """The defect, as a verdict.

    Four commits changed source and none changed a test, so this release is one the
    check exists to block. A window of three sees no test file at all - and the question
    "does this project have tests" is not a question about the window.
    """
    verdicts = {}
    for scan in (1, 2, 3, 4, 50):
        report = evaluate(read_history(repo, scan))
        coupling = [c for c in report.checks if "test" in c.name.lower()]
        assert coupling, [c.name for c in report.checks]
        verdicts[scan] = (coupling[0].status, coupling[0].detail)

    for scan, (status, detail) in verdicts.items():
        assert status == BLOCK, (scan, status, detail)
        assert "no test" in detail or "0 of" in detail or "without" in detail, (scan, detail)


def test_a_repository_with_no_tests_at_all_is_not_blocked_for_having_none(tmp_path: Path):
    """The other half, and the reason the check is gated at all.

    "This release changed source without touching tests" is a reading of the release.
    "This repository has no tests" is a fact about the project, and it reads as 100%
    untested on every release it will ever have - so the check must withdraw rather than
    answer NO-GO to every question anybody asks.
    """
    root = tmp_path / "untested"
    root.mkdir()
    sp.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    (root / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _commit(root, "only source, ever")
    (root / "m2.py").write_text("def g():\n    return 2\n", encoding="utf-8")
    _commit(root, "more source")

    history = read_history(root)
    assert history.test_files_tracked == 0
    report = evaluate(history)
    coupling = [c for c in report.checks if "test" in c.name.lower()]
    assert coupling and coupling[0].status == NOT_MEASURED, coupling
    assert coupling[0].name in [c.name for c in report.not_measured]


def test_the_count_comes_from_the_file_list_and_not_from_the_commits(repo: Path):
    """The distinction, stated as a measurement rather than as prose.

    A test file that exists and was not touched in the window is invisible to the
    commits and visible to `ls-files`. If these two ever agree for every window, this
    file is not testing what it says it is.
    """
    history = read_history(repo, 3)
    touched_in_window = any(change.is_test for commit in history.commits for change in commit.files)
    assert not touched_in_window, "the window touched a test file; the fixture moved"
    assert history.test_files_tracked == 1, history.test_files_tracked


def test_an_unlistable_repository_falls_back_to_the_window(tmp_path: Path, monkeypatch):
    """`tracked_test_files` returns `[]` when git cannot list, and the gate then reads
    the commits - the old behaviour, which is still better than calling an unreadable
    repository untested."""
    root = tmp_path / "proj"
    root.mkdir()
    sp.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    (root / "tests").mkdir()
    (root / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    (root / "s.py").write_text("x = 1\n", encoding="utf-8")
    _commit(root, "source and tests together")

    import reporecon.captain.history as module

    monkeypatch.setattr(module, "tracked_test_files", lambda _repo: [])
    history = module.read_history(root)
    assert history.test_files_tracked == 0
    report = evaluate(history)
    coupling = [c for c in report.checks if "test" in c.name.lower()]
    # The commit DID touch a test file, so the fallback finds the tests and the check
    # is measured rather than withdrawn.
    assert coupling and coupling[0].status != NOT_MEASURED, coupling
