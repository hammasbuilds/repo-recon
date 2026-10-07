"""Read a repository's history through the git CLI.

`git` is treated as the data source rather than a library dependency: it is
already installed anywhere this tool would run, it is faster than any Python
implementation of the same queries, and its output format is stable.

Nothing here writes to the repository. Every command is a read.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

#: The field separator, and the record separator: git emits one before each commit.
#:
#: NUL, because a commit message cannot contain one. A commit object is a header, a
#: blank line and the message, stored as bytes - git refuses a NUL in it rather than
#: writing an object nothing could read back.
#:
#: This was `\x1e` and `\x1f`, under a comment saying they "cannot occur in a commit
#: subject". They can: `git commit -F` writes whatever bytes it is given, and a message
#: carrying both made `read_history` report a commit that does not exist - with a sha,
#: an author, a date and a subject of the committer's choosing, holding the file
#: statistics of the real commit it was glued to. `release_readiness` then named it as
#: the riskiest change in the release.
_SEP = "\x00"

#: How the separator is written in the `--format` argument. git expands
#: `%x00` itself, and it has to: Windows refuses to spawn a process with a
#: NUL inside an argv entry, so passing the byte straight through raises
#: `ValueError: embedded null character` before git is reached. The escape is
#: plain text in argv and a NUL in the output.
_SEP_FORMAT = "%x00"

#: What a commit sha looks like. Checked on every record, because choosing a separator
#: the input cannot contain is one half of parsing untrusted output and recognising
#: what you parsed is the other. This alone would have rejected the fabricated commit.
_SHA = re.compile(r"^[0-9a-f]{40}$")

SOURCE_SUFFIXES = frozenset({".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java"})

DOC_SUFFIXES = frozenset({".md", ".rst", ".txt", ".adoc"})


class NotAGitRepository(RuntimeError):
    pass


@dataclass(frozen=True)
class FileChange:
    path: str
    added: int
    deleted: int

    @property
    def churn(self) -> int:
        return self.added + self.deleted

    @property
    def is_test(self) -> bool:
        parts = self.path.split("/")
        name = parts[-1]
        return (
            any(p in ("tests", "test") for p in parts)
            or name.startswith("test_")
            or name.endswith("_test.py")
        )

    @property
    def is_source(self) -> bool:
        if self.is_test:
            return False
        return Path(self.path).suffix in SOURCE_SUFFIXES

    @property
    def is_doc(self) -> bool:
        return Path(self.path).suffix in DOC_SUFFIXES


@dataclass
class Commit:
    sha: str
    author: str
    when: datetime
    subject: str
    files: list[FileChange] = field(default_factory=list)

    @property
    def added(self) -> int:
        return sum(f.added for f in self.files)

    @property
    def deleted(self) -> int:
        return sum(f.deleted for f in self.files)

    @property
    def churn(self) -> int:
        return self.added + self.deleted

    @property
    def source_files(self) -> list[FileChange]:
        return [f for f in self.files if f.is_source]

    @property
    def test_files(self) -> list[FileChange]:
        return [f for f in self.files if f.is_test]

    @property
    def touches_source(self) -> bool:
        return bool(self.source_files)

    @property
    def touches_tests(self) -> bool:
        return bool(self.test_files)

    @property
    def is_docs_only(self) -> bool:
        return bool(self.files) and all(f.is_doc for f in self.files)

    @property
    def areas(self) -> set[str]:
        """Top-level directories touched, with root-level files as one area.

        Without the grouping, `README.md` and `.gitignore` each counted as their
        own "area" and a docs-only commit looked as broad as a refactor.
        """
        out = set()
        for f in self.files:
            head, sep, _ = f.path.partition("/")
            out.add(head if sep else "(root)")
        return out

    @property
    def spread(self) -> int:
        """How wide the change reaches, in distinct top-level areas."""
        return len(self.areas)


@dataclass
class History:
    repo: str
    commits: list[Commit]
    #: The git directory these commits were read from. Not always `repo/.git`: a
    #: `.git` FILE holding `gitdir: <elsewhere>` is a worktree or submodule redirect
    #: and git follows it, so a directory holding one module and such a file reports
    #: another repository's history. Both tools reported only the name or path they
    #: were given, so the answer was that history under this directory's label.
    git_dir: str = ""
    #: Whether this clone is truncated. A `--depth 1` clone has one commit whose parents
    #: have been grafted away, so it holds the whole tree as a single change - and
    #: `--no-merges` does not exclude it even when it is a merge, because a graft has no
    #: parents to merge. Every repo-relative figure here is then computed from that one
    #: synthetic commit: the median IS it, and the gate compares it with itself.
    #:
    #: Not a refusal. Asking about a shallow checkout is reasonable; answering as though
    #: the commits that are missing are not missing is not.
    shallow: bool = False

    @property
    def caveat(self) -> str:
        """What a reader must be told about these commits before relying on them."""
        if not self.shallow:
            return ""
        return (
            f"this is a shallow clone: {len(self.commits)} commit(s) are present and the "
            "rest of the history was not fetched, so every figure computed against this "
            "repository's own median is computed against a truncated history"
        )

    @property
    def source_commits(self) -> list[Commit]:
        return [c for c in self.commits if c.touches_source]


def _git(repo: Path, *args: str, timeout: float = 120) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise NotAGitRepository(
            f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()[:200]}"
        )
    return proc.stdout


def is_repository(path: Path) -> bool:
    return (path / ".git").exists()


def _renamed_to(path: str) -> str:
    """The new path from a numstat rename.

    git writes a rename as `a/{b => c}/d` - the braces are mid-string, around the part
    that changed, with the shared prefix and suffix outside them. `split(" => ")[-1]`
    then `.strip("}")` turned `{oldpkg => newpkg}/m.py` into `newpkg}/m.py`: the brace
    is not at either end so strip never saw it, and a prefixed form lost its prefix.
    A path ending in `}` is not a Python file, so the commit stopped counting as a
    source change and the top-level area it contributed was a phantom.
    """
    if " => " not in path:
        return path
    start = path.find("{")
    end = path.find("}", start)
    if start != -1 and end != -1:
        inner = path[start + 1 : end].split(" => ")[-1]
        return (path[:start] + inner + path[end + 1 :]).replace("//", "/")
    return path.split(" => ")[-1]


def _unquote(path: str) -> str:
    """git quotes a path with non-ASCII bytes: `"src/caf\303\251.py"`.

    Left quoted, `Path(path).suffix` is `.py"` rather than `.py`, so the file was not
    source, the commit was not a source change, and a repository whose Python files all
    have non-ASCII names reported "no source changes" and passed the test-coupling gate.
    """
    if not (path.startswith('"') and path.endswith('"') and len(path) > 1):
        return path
    body = path[1:-1]
    try:
        raw = body.encode("latin-1", "backslashreplace").decode("unicode_escape")
        return raw.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return body


def is_shallow(repo: Path) -> bool:
    """Whether this checkout is a truncated clone.

    One `rev-parse`, which is why the absence of this check was an omission rather than
    a trade: `git rev-parse --is-shallow-repository` prints true or false and has been
    in git since 2.15.
    """
    try:
        return _git(repo, "rev-parse", "--is-shallow-repository").strip() == "true"
    except Exception:  # pragma: no cover - a git too old to know the flag
        return (Path(git_dir(repo) or (repo / ".git")) / "shallow").exists()


def git_dir(repo: Path) -> str:
    """The git directory this path really reads, which is not always `repo / ".git"`.

    A `.git` *file* holding `gitdir: <elsewhere>` is a worktree or a submodule
    redirect, and git follows it. So a directory containing one `m.py` and such a file
    reports the commits of whatever repository it points at - and both history tools
    reported only the name or the path they were GIVEN, so the answer was another
    repository's history labelled with this directory.

    That is not a confinement escape: unconfined, a caller could pass the real path
    directly. It is a mislabelling, and the fix is to say which git directory was
    read rather than to guess that it was the obvious one.
    """
    repo = repo.resolve()
    if not is_repository(repo):
        raise NotAGitRepository(f"{repo} is not a git repository")
    return _git(repo, "rev-parse", "--absolute-git-dir").strip()


def resolve_revision(repo: Path, rev: str) -> str:
    """``rev`` as a full commit sha, or ValueError naming what was not found.

    Resolved rather than passed through, because a published figure should record the
    commit it was measured at and not the branch name that pointed there at the time.
    """
    repo = repo.resolve()
    if not is_repository(repo):
        raise NotAGitRepository(f"{repo} is not a git repository")
    try:
        found = _git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}").strip()
    except Exception:
        raise ValueError(f"no such revision in this repository: {rev!r}") from None
    if not found:
        raise ValueError(f"no such revision in this repository: {rev!r}")
    return found


def read_history(repo: Path, limit: int | None = None, rev: str = "HEAD") -> History:
    """Every commit with its per-file line counts, ending at ``rev``.

    Merge commits are excluded: their numstat is relative to one parent and
    double-counts work already attributed to the commits being merged.

    ``rev`` exists so a published figure can name the commit it is over. Read from
    HEAD, every history figure this package produces about its own repository moves on
    the next commit - including the commit that publishes the figure, which made
    `demo.py --update-readme` unable to produce a README that matched a fresh run: the
    count it wrote was one short the moment it was committed, and the test asserting
    "this block is that command's output" then failed on every subsequent commit.

    A revision makes the measurement reproducible instead of current, which is the
    only form in which it can be checked.
    """
    repo = repo.resolve()
    if not is_repository(repo):
        raise NotAGitRepository(f"{repo} is not a git repository")

    args = [
        "log",
        rev,
        "--no-merges",
        "--numstat",
        # A leading separator before each record, so the numstat block that follows a
        # subject belongs to it and the next record begins at the next separator.
        f"--format={_SEP_FORMAT}%H{_SEP_FORMAT}%an{_SEP_FORMAT}%aI{_SEP_FORMAT}%s",
    ]
    if limit:
        args.append(f"-n{limit}")

    raw = _git(repo, *args)
    commits: list[Commit] = []

    # Four fields per commit, the fourth carrying the subject and the numstat block
    # after it. `fields[0]` is whatever precedes the first separator, which is nothing.
    fields = raw.split(_SEP)
    skipped = 0
    for index in range(1, len(fields) - 3, 4):
        sha, author, iso = fields[index], fields[index + 1], fields[index + 2]
        subject, _, body = fields[index + 3].partition("\n")
        subject = subject.strip("\n")
        if not _SHA.match(sha):
            # Not a commit. Nothing should produce this now that the separator is one
            # git will not write into a message, and that is exactly why a record
            # failing the check is dropped rather than repaired: the alternative is
            # reporting a commit whose sha came from somewhere unknown.
            skipped += 1
            continue
        try:
            when = datetime.fromisoformat(iso)
        except ValueError:
            when = datetime.now(timezone.utc)

        files: list[FileChange] = []
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            cols = line.split("\t")
            if len(cols) != 3:
                continue
            added_s, deleted_s, path = cols
            # Binary files report "-" for both counts.
            added = int(added_s) if added_s.isdigit() else 0
            deleted = int(deleted_s) if deleted_s.isdigit() else 0
            path = _unquote(_renamed_to(path))
            files.append(FileChange(path=path.replace("\\", "/"), added=added, deleted=deleted))

        commits.append(Commit(sha=sha, author=author, when=when, subject=subject, files=files))

    if skipped:  # pragma: no cover - unreachable while git refuses NUL in a message
        raise ValueError(
            f"{skipped} record(s) in `git log` output did not begin with a commit sha. "
            "Something is producing separator bytes inside the log; refusing to report "
            "a history that may contain commits this repository does not hold."
        )
    return History(
        repo=repo.name,
        commits=commits,
        git_dir=git_dir(repo),
        shallow=is_shallow(repo),
    )
