"""Read a repository's history through the git CLI.

`git` is treated as the data source rather than a library dependency: it is
already installed anywhere this tool would run, it is faster than any Python
implementation of the same queries, and its output format is stable.

Nothing here writes to the repository. Every command is a read.
"""

from __future__ import annotations

import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Separators chosen because they cannot occur in a commit subject.
_REC = "\x1e"
_FLD = "\x1f"

SOURCE_SUFFIXES = frozenset({".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java"})

DOC_SUFFIXES = frozenset({".md", ".rst", ".txt", ".adoc"})

CONFIG_NAMES = frozenset(
    {
        "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "uv.lock",
        "package.json", "package-lock.json", "Dockerfile", "docker-compose.yml",
        ".gitignore", "Makefile",
    }
)


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

    @property
    def is_config(self) -> bool:
        return Path(self.path).name in CONFIG_NAMES


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

    @property
    def source_commits(self) -> list[Commit]:
        return [c for c in self.commits if c.touches_source]

    @property
    def authors(self) -> Counter:
        return Counter(c.author for c in self.commits)

    def churn_by_file(self) -> Counter:
        out: Counter = Counter()
        for c in self.commits:
            for f in c.files:
                out[f.path] += f.churn
        return out

    def commits_by_file(self) -> Counter:
        out: Counter = Counter()
        for c in self.commits:
            for f in c.files:
                out[f.path] += 1
        return out

    def co_change(self) -> dict[str, Counter]:
        """Which files change together. Coupling the import graph cannot see.

        Two files that always change in the same commit are coupled even when
        neither imports the other - a schema and its migration, a function and
        the fixture that feeds it.
        """
        pairs: dict[str, Counter] = defaultdict(Counter)
        for c in self.commits:
            paths = [f.path for f in c.files if f.is_source or f.is_test]
            if len(paths) < 2 or len(paths) > 30:
                continue  # a 200-file commit couples nothing meaningfully
            for i, a in enumerate(paths):
                for b in paths[i + 1:]:
                    pairs[a][b] += 1
                    pairs[b][a] += 1
        return pairs


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


def read_history(repo: Path, limit: int | None = None) -> History:
    """Every commit with its per-file line counts.

    Merge commits are excluded: their numstat is relative to one parent and
    double-counts work already attributed to the commits being merged.
    """
    repo = repo.resolve()
    if not is_repository(repo):
        raise NotAGitRepository(f"{repo} is not a git repository")

    args = [
        "log",
        "--no-merges",
        "--numstat",
        f"--format={_REC}%H{_FLD}%an{_FLD}%aI{_FLD}%s",
    ]
    if limit:
        args.append(f"-n{limit}")

    raw = _git(repo, *args)
    commits: list[Commit] = []

    for block in raw.split(_REC):
        block = block.strip("\n")
        if not block:
            continue
        head, _, body = block.partition("\n")
        parts = head.split(_FLD)
        if len(parts) < 4:
            continue
        sha, author, iso, subject = parts[0], parts[1], parts[2], parts[3]
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
            # Renames appear as "old => new"; keep the new path.
            if " => " in path:
                path = path.split(" => ")[-1].strip("}")
            files.append(FileChange(path=path.replace("\\", "/"), added=added, deleted=deleted))

        commits.append(
            Commit(sha=sha, author=author, when=when, subject=subject, files=files)
        )

    return History(repo=repo.name, commits=commits)
