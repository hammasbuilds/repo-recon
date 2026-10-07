"""`docs/` holds four historical READMEs, and says so on every one of them.

The four tools were standalone packages. Their original READMEs are kept because the
reasoning in them is the reasoning behind the code, and rewriting them would make them a
worse record of it. The problem is that a reader meets them as documentation of what is
installed here, and between them they name:

  * `python ui/server.py`, `cd ui && npm install && npm run dev` and `python ui/serve.py`
    for a Vue UI, a Remix UI and a web view - there is no `ui/` directory anywhere in
    this repository;
  * `python scripts/prove_utcnow.py` - there is no `scripts/` directory;
  * `captain gate` and `cartographer map` - the installed commands are `release-captain`
    and `repo-cartographer`;
  * per-file test badges totalling 128 against a suite of 280, and figures measured over
    a folder of local checkouts with no revision and no date.

An independent review found all four, and the main README's own line - "Their original
READMEs are kept in docs/" - said nothing about any of it. Each file now opens with a
header naming the three kinds of thing in it that do not apply. This file holds that
claim: the header is present, it is accurate about the rename, and the directories it
says are absent really are absent.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

#: Each kept README, and the command this package installs for it.
PACKAGES = {
    "release-captain.md": "release-captain",
    "repo-cartographer.md": "repo-cartographer",
    "migration-pilot.md": "migration-pilot",
    "review-bot.md": "review-bot",
}

#: Directories the headers say are not here. The claim, checked.
ABSENT = ("ui", "scripts")


def test_every_kept_readme_is_one_of_the_four():
    """A fifth file would be a document nobody has read in this light."""
    assert sorted(p.name for p in DOCS.glob("*.md")) == sorted(PACKAGES)


@pytest.mark.parametrize("filename", sorted(PACKAGES))
def test_each_one_says_what_it_is_before_anything_else(filename: str):
    text = (DOCS / filename).read_text(encoding="utf-8")
    assert text.lstrip().startswith(">"), "the header is not the first thing in the file"
    # Whitespace-collapsed, because the header is wrapped prose and a phrase that
    # spans a line break is the same phrase.
    head = " ".join(text.split("---", 1)[0].replace(">", " ").split())
    assert "original README of a standalone package" in head, filename
    assert "not documentation of what this package installs" in head, filename


@pytest.mark.parametrize("filename", sorted(PACKAGES))
def test_the_header_names_the_command_that_is_installed(filename: str):
    """And the old name, because a reader copying a line out of the body needs the
    translation rather than a note that one exists."""
    head = (DOCS / filename).read_text(encoding="utf-8").split("---", 1)[0]
    command = PACKAGES[filename]
    assert f"`{command}`" in head, (filename, command)

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(rf"^{re.escape(command)} = ", pyproject, re.MULTILINE), command


@pytest.mark.parametrize("directory", ABSENT)
def test_the_directories_the_headers_say_are_absent_are_absent(directory: str):
    """If a `ui/` ever appears, the headers become wrong in the other direction."""
    found = [p for p in ROOT.rglob(directory) if p.is_dir() and ".venv" not in p.parts]
    assert found == [], f"{directory}/ exists now: {found}"


@pytest.mark.parametrize("filename", sorted(PACKAGES))
def test_a_reader_is_warned_about_the_commands_in_the_body(filename: str):
    """The bodies really do carry the old command names and the UI lines; that is why
    the header is there. This asserts the warning covers what is actually in the file,
    so a header that stopped matching its body would fail rather than reassure."""
    text = (DOCS / filename).read_text(encoding="utf-8")
    head, _, body = text.partition("---")
    if re.search(r"\bui/|npm install|npm run dev", body):
        assert "no `ui/`" in head, filename
    if "scripts/" in body:
        assert "no `scripts/`" in head, filename


@pytest.mark.parametrize("filename", sorted(PACKAGES))
def test_the_header_says_the_figures_are_undated(filename: str):
    """Every one of these quotes figures over a folder of live checkouts with no
    revision and no date. The main README fixed that for its own half by pinning a
    commit; these were left, and the honest thing is to say which is which."""
    head = (DOCS / filename).read_text(encoding="utf-8").split("---", 1)[0]
    assert "undated" in head, filename
    assert "README.md" in head, "the header does not point at the reproducible figures"
