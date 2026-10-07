"""A commit message cannot add a commit to the history.

`history.py` parsed `git log` output split on `\\x1e` and `\\x1f`, under a comment saying
those bytes "cannot occur in a commit subject". They can: `git commit -F` writes whatever
bytes it is given. An independent reviewer committed a message carrying both and
`read_history` returned three commits for a two-commit repository - with the sha, the
author, the date and the subject of the third chosen by whoever wrote the message, and
the real commit's file statistics re-attributed to it. `release_readiness` then named
the commit that does not exist as the riskiest change in the release.

Two things changed, and both are tested here, because either alone would have been
enough and neither alone is the habit worth keeping:

  * the separator is NUL, which git refuses inside a commit message - a property of the
    object format rather than an assumption about what people type;
  * every record's first field is checked against `^[0-9a-f]{40}$`, because recognising
    what you parsed is the other half of parsing output you did not write.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reporecon.captain.history import read_history
from reporecon.captain.risk import Baseline, score_commit


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "victim"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "Tester")
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "normal commit")
    return root


def _commit_with_message(repo: Path, message: bytes, filename: str) -> None:
    """Commit with raw bytes as the message, which `-m` cannot express."""
    path = repo / "message.txt"
    path.write_bytes(message)
    (repo / filename).write_text("y = 2\n", encoding="utf-8")
    git(repo, "add", filename)
    git(repo, "commit", "-q", "-F", str(path))
    path.unlink()


#: The payload, in the shape the old format produced: a record separator, then the four
#: fields of a commit the attacker invents.
PAYLOAD = (
    b"inject\x1e"
    b"deadbeefcafebabedeadbeefcafebabedeadbeef\x1f"
    b"Attacker\x1f"
    b"2020-01-01T00:00:00+00:00\x1f"
    b"owned subject"
)


def test_a_message_carrying_the_old_separators_adds_no_commit(repo: Path):
    _commit_with_message(repo, PAYLOAD, "b.py")
    history = read_history(repo)

    assert len(history.commits) == 2, [(c.sha[:8], c.subject) for c in history.commits]
    assert not [c for c in history.commits if c.sha.startswith("deadbeef")]
    assert {c.author for c in history.commits} == {"Tester"}
    assert "Attacker" not in {c.author for c in history.commits}


def test_every_sha_the_history_reports_is_a_sha(repo: Path):
    """The check that does not depend on the separator being unreachable."""
    _commit_with_message(repo, PAYLOAD, "b.py")
    import re

    for commit in read_history(repo).commits:
        assert re.fullmatch(r"[0-9a-f]{40}", commit.sha), commit.sha


def test_the_real_commit_keeps_its_own_files(repo: Path):
    """The injection did not only add a commit; it took the files off a real one.

    The fabricated record absorbed the numstat block that followed the subject, so the
    commit that actually changed `b.py` was reported as changing nothing and scored 0.
    """
    _commit_with_message(repo, PAYLOAD, "b.py")
    newest = read_history(repo).commits[0]
    assert "b.py" in [f.path for f in newest.files], [f.path for f in newest.files]
    assert newest.churn > 0


def test_the_payload_survives_as_what_it_is(repo: Path):
    """Text, in the subject of the commit whose message it is. Not sanitised away: the
    repository really does contain that message, and a tool that silently rewrote it
    would be lying about the history in the other direction."""
    _commit_with_message(repo, PAYLOAD, "b.py")
    newest = read_history(repo).commits[0]
    assert newest.subject.startswith("inject")
    assert "deadbeef" in newest.subject


def test_the_risk_table_holds_only_commits_that_exist(repo: Path):
    """Where the fabrication surfaced: `release_readiness` named the invented commit as
    the riskiest change in the release, with a score built from another commit's files."""
    _commit_with_message(repo, PAYLOAD, "b.py")
    history = read_history(repo)
    baseline = Baseline.from_history(history)
    ranked = sorted(
        ((commit, score_commit(commit, baseline)) for commit in history.commits),
        key=lambda pair: -pair[1].score(),
    )
    shas = {commit.sha for commit in history.commits}
    for commit, _factors in ranked:
        assert commit.sha in shas, commit.sha
    # And the top of that table is a commit that changed something, which the
    # fabrication took away from the real one.
    assert ranked[0][1].score() > 0, ranked[0]


def test_a_newline_in_a_message_does_not_split_a_commit_either(repo: Path):
    """The subject is the first line; the body is not a record boundary.

    A message whose body looks like numstat output is the same attack through a
    different byte, and it was never possible - but nothing said so.
    """
    _commit_with_message(
        repo,
        b"a subject\n\n10\t0\tfabricated/path.py\n",
        "c.py",
    )
    history = read_history(repo)
    assert len(history.commits) == 2
    newest = history.commits[0]
    assert newest.subject == "a subject"
    assert "fabricated/path.py" not in [f.path for f in newest.files]
