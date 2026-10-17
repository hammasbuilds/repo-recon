r"""Fetch a corpus of real Python projects to measure the resolver against.

    python scripts/fetch_corpus.py --list
    python scripts/fetch_corpus.py              # -> D:\reporecon-corpus\src (or --work)

`map_repo` reports what share of a repository's in-scope calls it can bind to a
definition in that repository. For this repository the answer is about 93%, and that is
one data point about one codebase - written by one person, in one style, with no
metaclasses, no plugin registry and almost no dynamic dispatch. A resolver measured only
on the code it was written against is measured on its easiest case.

So the figure the README quotes for other people's code comes from here: twelve projects
chosen for the things that defeat static resolution, at pinned tags so the number is
re-derivable. Nothing is committed - this is 100 MB of other people's source under their
own licences, and the script is the reproducible way to get exactly what was measured.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

CODELOAD = "https://codeload.github.com/{repo}/tar.gz/refs/tags/{tag}"
AGENT = {"User-Agent": "Mozilla/5.0 (static-resolution corpus fetch)"}

#: name -> (github repo, tag, why it is in here).
#:
#: Picked for the shapes that break a resolver, not for popularity: decorator-heavy
#: frameworks, a plugin system, a class hierarchy built at import time, a library that
#: re-exports everything through `__init__`, and two that are mostly C extensions in
#: tree so that most calls go somewhere the AST cannot follow.
PROJECTS = {
    "requests": ("psf/requests", "v2.32.3", "re-exports through __init__, heavy urllib3 use"),
    "flask": ("pallets/flask", "3.0.3", "decorator-driven routing, app/request context"),
    "click": ("pallets/click", "8.1.7", "decorators that rebuild the callable"),
    "rich": ("Textualize/rich", "v13.7.1", "protocol classes and __rich__ dispatch"),
    "jinja": ("pallets/jinja", "3.1.4", "code generation; calls into generated namespaces"),
    "attrs": ("python-attrs/attrs", "23.2.0", "classes assembled at runtime"),
    "httpx": ("encode/httpx", "0.27.0", "async, and a transport layer behind a protocol"),
    "starlette": ("encode/starlette", "0.37.2", "ASGI callables passed as values"),
    "pytest": ("pytest-dev/pytest", "8.2.2", "a plugin system; hooks are called by name"),
    "pydantic": ("pydantic/pydantic", "v2.7.4", "metaclass-built models, a Rust core"),
    "sqlalchemy": ("sqlalchemy/sqlalchemy", "rel_2_0_31", "large, and dispatch through events"),
    "black": ("psf/black", "24.4.2", "a visitor over its own AST types"),
}


def get(url: str, into: Path, tries: int = 3) -> bool:
    for attempt in range(tries):
        try:
            request = urllib.request.Request(url, headers=AGENT)
            with urllib.request.urlopen(request, timeout=300) as answer, into.open("wb") as out:
                shutil.copyfileobj(answer, out)
            return True
        except (urllib.error.URLError, OSError, TimeoutError) as err:
            if attempt == tries - 1:
                print(f"    gave up: {type(err).__name__} {getattr(err, 'code', '')}")
                return False
            time.sleep(3 + 5 * attempt)
    return False


def unpack(archive: Path, into: Path) -> Path:
    """Extract, then lift the tarball's single top directory into `into`.

    `filter="data"` refuses a member that would land outside the directory: an archive
    is untrusted input, and a member named `../../x` wrote outside it until Python 3.12
    made that a choice.

    The lift matters. A GitHub tarball holds everything under `requests-2.32.3/`, and
    `map_repo` reads a directory containing a `pyproject.toml` nested inside the path it
    was given as a SEPARATE project and excludes it - correctly, since that is what a
    vendored dependency looks like. Left nested, every project in this corpus measured
    0 modules and `excluded_projects: ["requests-2.32.3"]`.
    """
    into.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as bundle:
        bundle.extractall(into, filter="data")
    return flatten(into)


def flatten(into: Path) -> Path:
    """If `into` holds exactly one directory and nothing else, lift its contents up."""
    children = list(into.iterdir())
    if len(children) != 1 or not children[0].is_dir():
        return into
    top = children[0]
    for item in list(top.iterdir()):
        shutil.move(str(item), str(into / item.name))
    top.rmdir()
    return into


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--work",
        default=r"D:\reporecon-corpus" if sys.platform == "win32" else "/tmp/reporecon-corpus",
    )
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args(argv)

    if args.list:
        for name, (repo, tag, why) in sorted(PROJECTS.items()):
            print(f"  {name:<12} {repo:<26} {tag:<12} {why}")
        print(f"\n  {len(PROJECTS)} projects")
        return 0

    work = Path(args.work)
    archives, source = work / "archive", work / "src"
    for folder in (archives, source):
        folder.mkdir(parents=True, exist_ok=True)

    for name in args.only or sorted(PROJECTS):
        if name not in PROJECTS:
            print(f"  {name}: not in the corpus; --list shows it")
            continue
        repo, tag, _ = PROJECTS[name]
        destination = source / name
        if destination.is_dir() and any(destination.iterdir()):
            # Flattened here too, so a corpus unpacked by an earlier version is fixed
            # rather than left in the shape that measured zero.
            flatten(destination)
            print(f"  {name:<12} already unpacked")
            continue
        bundle = archives / f"{name}.tar.gz"
        if not (bundle.is_file() and bundle.stat().st_size > 1000):
            print(f"  {name:<12} downloading {repo}@{tag}", flush=True)
            if not get(CODELOAD.format(repo=repo, tag=tag), bundle):
                continue
        unpack(bundle, destination)
        files = sum(1 for _ in destination.rglob("*.py"))
        print(f"  {name:<12} {files:>6} python file(s)", flush=True)

    print(f"\n  corpus at {source}")
    print("  measure it: python scripts/resolution_sweep.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
