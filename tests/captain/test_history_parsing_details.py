"""Four branches with multi-paragraph docstrings about the bug they fixed, and no test.

An independent reviewer listed them: `_unquote`, `_renamed_to`, the "nothing to release"
BLOCK branch, and the `skipped` raise. It exercised the first two directly and found them
**correct** - so these are coverage gaps rather than defects, and the reason to close
them is that each one describes a bug that was shipped once:

  * `_renamed_to`: git writes a rename as `a/{b => c}/d`, with the braces mid-string
    around the part that changed. `split(" => ")[-1]` then `.strip("}")` turned
    `{oldpkg => newpkg}/m.py` into `newpkg}/m.py`, because the brace is not at either
    end - and a path ending in `}` is not a Python file, so the commit stopped counting
    as a source change and the area it contributed was a phantom.
  * `_unquote`: git quotes a path containing a non-ASCII byte as `"src/caf\\303\\251.py"`.
    Read literally that is a different file from `src/café.py`.
  * The `skipped` raise: it carried `# pragma: no cover - unreachable while git refuses
    NUL in a message` and was reachable by `git init --object-format=sha256`.

A branch with a paragraph explaining what it prevents and no test is a branch that can
be deleted without anything noticing, which is the state all four were in.
"""

from __future__ import annotations

import datetime as dt

import pytest

from reporecon.captain.gate import BLOCK, evaluate
from reporecon.captain.history import (
    Commit,
    FileChange,
    History,
    UnreadableHistory,
    _renamed_to,
    _unquote,
)

# -- renames ---------------------------------------------------------------------------

RENAMES = {
    # The plain form: the whole path replaced.
    "src/old.py => src/new.py": "src/new.py",
    # The braced form, with a shared prefix and suffix OUTSIDE the braces. This is the
    # one that produced `newpkg}/m.py`.
    "src/{oldpkg => newpkg}/m.py": "src/newpkg/m.py",
    "a/{b => c}/d.py": "a/c/d.py",
    # A deletion inside the braces: the component goes away entirely.
    "a/{b => }/d.py": "a/d.py",
    # And an insertion.
    "a/{ => b}/d.py": "a/b/d.py",
    # No rename at all passes through untouched.
    "src/plain.py": "src/plain.py",
}


@pytest.mark.parametrize("written", sorted(RENAMES))
def test_a_rename_yields_the_path_the_file_now_has(written: str):
    assert _renamed_to(written) == RENAMES[written], written


@pytest.mark.parametrize("written", sorted(RENAMES))
def test_no_rename_form_leaves_a_brace_in_the_path(written: str):
    """The symptom that mattered: a path ending in `}` has no `.py` suffix, so the
    commit stopped counting as a source change."""
    got = _renamed_to(written)
    assert "{" not in got and "}" not in got, (written, got)
    assert "=>" not in got, (written, got)


def test_a_renamed_python_file_still_counts_as_a_source_change():
    """End to end through the thing the parse feeds: `touches_source` and `areas`."""
    commit = Commit(
        sha="a" * 40,
        author="t",
        when=dt.datetime(2026, 1, 1),
        subject="move the package",
        files=[
            FileChange(path=_renamed_to("src/{oldpkg => newpkg}/m.py"), added=3, deleted=1)
        ],
    )
    assert commit.touches_source, commit.files
    assert commit.areas == {"src"}, commit.areas


# -- quoted paths ----------------------------------------------------------------------

QUOTED = {
    r'"src/caf\303\251.py"': "src/café.py",
    r'"src/{a => caf\303\251}/d.py"': 'src/{a => café}/d.py',
    # Already plain: unchanged, quotes and all absent.
    "src/plain.py": "src/plain.py",
    # An escaped quote inside the name.
    r'"src/a\"b.py"': 'src/a"b.py',
    r'"src/tab\there.py"': "src/tab\there.py",
}


@pytest.mark.parametrize("written", sorted(QUOTED))
def test_a_quoted_path_is_decoded_to_the_file_it_names(written: str):
    assert _unquote(written) == QUOTED[written], written


def test_the_two_run_together_the_way_the_parser_runs_them():
    """`_unquote(_renamed_to(path))` - a renamed path with a non-ASCII component, which
    is both forms at once and the only reason either is interesting."""
    assert _unquote(_renamed_to(r'"src/{a => caf\303\251}/d.py"')) == "src/café/d.py"


# -- nothing to release ----------------------------------------------------------------


def test_a_release_with_no_commits_is_blocked_and_says_why():
    """The branch for a question that has no subject. "Is this safe to ship" over zero
    commits is not a pass - there is nothing to assess, and a pass would read as one."""
    report = evaluate(History(repo="r", commits=[]))
    assert report.blocked, report
    blocking = [c for c in report.checks if c.status == BLOCK]
    assert blocking, [(c.name, c.status) for c in report.checks]
    assert any(
        "nothing" in c.detail.lower() or "no commit" in c.detail.lower() for c in blocking
    ), [c.detail for c in blocking]


# -- a history that cannot be vouched for ----------------------------------------------


def test_an_unreadable_record_raises_the_named_type(tmp_path, monkeypatch):
    """`UnreadableHistory`, so the tools can turn it into a tool error rather than
    letting -32603 carry a Python type to the wire.

    Driven through `read_history` with `_git` returning a record whose first field is
    not a sha - which is exactly what a 64-character SHA-256 object name looked like to
    a 40-character pattern. A first version of this test raised the exception inside
    the `pytest.raises` block, which is a test that cannot fail.
    """
    import subprocess as sp

    from reporecon.captain import history as module

    root = tmp_path / "repo"
    root.mkdir()
    sp.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    (root / "m.py").write_text("x = 1\n", encoding="utf-8")
    sp.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    sp.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-qm", "first"],
        check=True, capture_output=True,
    )

    real = module._git

    def fake(repo, *args, **kwargs):
        if args and args[0] == "log":
            # One record, whose first field is not a sha. The LEADING separator
            # matters: the format is `%x00%H%x00%an%x00%aI%x00%s`, so `fields[0]` is
            # whatever precedes the first separator and is not a record at all - a
            # fake without one is skipped silently rather than parsed and rejected,
            # which is how the first version of this test came back DID NOT RAISE.
            return (
                module._SEP
                + module._SEP.join(
                    ["not-a-sha", "t", "2026-01-01T00:00:00+00:00", "subject"]
                )
                + chr(10)
            )
        return real(repo, *args, **kwargs)

    monkeypatch.setattr(module, "_git", fake)
    with pytest.raises(UnreadableHistory) as caught:
        module.read_history(root)
    assert "record(s)" in str(caught.value), str(caught.value)
    assert "cannot be reported" in str(caught.value), str(caught.value)


def test_a_sha256_repository_reads_like_any_other(tmp_path):
    """The input that made the branch above reachable, end to end.

    `git init --object-format=sha256` names objects with 64 hex characters. Every
    record failed a `{40}` pattern, `skipped` reached the record count, and an ordinary
    repository was told it was producing separator bytes inside its own log - as -32603,
    because the tools caught only `NotAGitRepository`.
    """
    import subprocess as sp

    from reporecon.captain.history import read_history

    root = tmp_path / "sha256"
    made = sp.run(
        ["git", "init", "-q", "--object-format=sha256", str(root)],
        capture_output=True, text=True,
    )
    if made.returncode != 0:
        pytest.skip(f"this git cannot make a sha256 repository: {made.stderr.strip()[:80]}")
    for name in ("a.py", "b.py"):
        body = "def " + name[0] + "():" + chr(10) + "    return 1" + chr(10)
        (root / name).write_text(body, encoding="utf-8")
        sp.run(["git", "-C", str(root), "add", name], check=True, capture_output=True)
        sp.run(
            ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
             "commit", "-qm", f"add {name}"],
            check=True, capture_output=True,
        )

    head = sp.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert len(head) == 64, head

    history = read_history(root)
    assert len(history.commits) == 2, history.commits
    assert all(len(c.sha) == 64 for c in history.commits), [c.sha for c in history.commits]
    # And the gate runs over it, which is the thing that came back as an internal error.
    assert evaluate(history).verdict


def test_a_sha256_name_is_accepted_by_the_sha_pattern():
    """Git has two object formats. A `{40}` pattern failed every record of an ordinary
    SHA-256 repository and the guard then accused it of forging its own history."""
    from reporecon.captain.history import _SHA

    assert _SHA.match("a" * 40)
    assert _SHA.match("a" * 64)
    assert not _SHA.match("a" * 39)
    assert not _SHA.match("a" * 41)
    assert not _SHA.match("a" * 63)
    assert not _SHA.match("a" * 65)
    assert not _SHA.match("z" * 40), "not hex"
