"""Four codebase tools as one MCP server — standard library only.

    repo-recon-mcp
    python -m reporecon.mcp_server

Speaks the Model Context Protocol over stdio: newline-delimited JSON-RPC 2.0 on stdin
and stdout, diagnostics on stderr. Written against the protocol rather than an SDK so
the package keeps `dependencies = []`.

What it exposes, and why an agent wants it:

  **map_repo**            what is in this repository, what depends on what, where to
                          start reading. From the AST, with no model and no embeddings.
  **trace_impact**        what breaks if this definition changes.
  **check_modernisation** what can be modernised mechanically, and — separately — what
                          a rewrite would change the *meaning* of.
  **review_diff**         findings that survived an attempt to disprove them.
  **release_readiness**   whether this history is safe to ship, scored from diff
                          statistics rather than from opinion.
  **riskiest_commits**    which commits in the recent history carry the most risk.

Three properties hold across all six, and they are the reason this is worth adding to a
client rather than letting a model read files itself:

**Nothing is written and nothing is executed.** Every tool parses source and returns
what it read. There is no apply, no fix, no shell. `openWorldHint` is false because the
answer depends only on the files it was pointed at.

**A repository path is the only input.** No API key, no token, no network. The server
opens files under the path it was given and nothing else.

**Mechanical and behavioural are never merged.** `check_modernisation` separates an edit
that cannot change behaviour from one that can, because a tool that reports both as
"modernisations" is how a `datetime.utcnow()` change becomes a timezone bug.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, BinaryIO

from . import __version__
from .captain.gate import evaluate
from .captain.history import NotAGitRepository, read_history
from .captain.risk import Baseline, score_commit
from .cartographer.graph import build_graph
from .cartographer.parse import parse_repo
from .cartographer.report import build_report, impact_of
from .pilot.rules import RULE_KIND, scan_source
from .reviewbot.review import review_source

# Newest first. A client asking for one of these gets it; anything else gets the newest.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
# structuredContent arrived in 2025-06-18; older clients get the text block only.
_STRUCTURED = {"2025-11-25", "2025-06-18"}

SERVER_NAME = "repo-recon"
ROOT_ENV = "REPO_RECON_ROOT"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# A repository, not a disk. Walking an unbounded tree is how this becomes a way to
# read every file on a machine, and how one call takes a minute.
FILE_LIMIT = 4_000
# Per-file, so one generated module cannot dominate a result.
SOURCE_LIMIT = 2_000_000
LIST_LIMIT = 200
DIFF_LIMIT = 500_000

INSTRUCTIONS = (
    "Four read-only readers for a Python repository you did not write. Point them at a "
    "path on this machine; they parse source and return what they read. None of them "
    "writes a file or opens a network connection. Two of them - release_readiness and "
    "riskiest_commits - run `git log` and `git diff` in the repository you name, with a "
    "fixed argument list and no shell, which is the only process any of them starts. "
    "Call map_repo first: it says which modules matter, where the entry points are, and "
    "how much of the call graph resolved. Read repo_resolution_rate, not resolution_rate: "
    "the first is the share of calls that COULD bind here and did, the second counts "
    "every `len` and third-party method in the denominator and is always far lower for "
    "reasons that say nothing about this repository. A low repo_resolution_rate means "
    "the map is partial and the other answers are reading less than you think. "
    "check_modernisation reports mechanical and behavioural edits SEPARATELY and you must "
    "keep them apart: a mechanical edit cannot change behaviour, a behavioural one can, "
    "and applying the second class without reading it is how a modernisation becomes a "
    "bug. review_diff reports only findings that survived an attempt to disprove them; a "
    "retracted finding is not a finding and must not be repeated to the user."
)


def _path_property(what: str) -> dict:
    return {"type": "string", "description": f"Path to {what} on this machine."}


_READ_ONLY = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}


def _tool(
    name: str,
    title: str,
    description: str,
    properties: dict,
    required: list,
    output: dict,
    output_required: list | None = None,
) -> dict:
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": output,
            "required": output_required if output_required is not None else sorted(output),
        },
        "annotations": _READ_ONLY,
    }


_EDIT = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "line": {"type": "integer"},
        "rule": {"type": "string"},
        "kind": {"type": "string", "enum": ["mechanical", "behavioural"]},
        "before": {"type": "string"},
        "replacement": {"type": "string"},
        "note": {"type": "string", "description": "Why it may change meaning."},
    },
    "required": ["path", "line", "rule", "kind"],
}

_SYMBOL = {
    "type": "object",
    "properties": {
        "qualname": {"type": "string", "description": "Dotted path, module to symbol."},
        "name": {"type": "string"},
        "kind": {"type": "string", "enum": ["function", "method", "class"]},
        "path": {"type": "string", "description": "Repo-relative file."},
        "line": {"type": "integer", "description": "1-indexed line of the definition."},
        "fan_in": {
            "type": "integer",
            "description": "Call sites in this repository that bind here. Zero means "
            "nothing in the repository calls it, not that nothing calls it.",
        },
        "docstring": {"type": ["string", "null"], "description": "First line only."},
    },
    "required": ["qualname", "name", "path"],
}

TOOLS: list[dict[str, Any]] = [
    _tool(
        "map_repo",
        "Map a repository",
        "What is in this repository and what depends on what, read from the AST: the "
        "modules that matter by PageRank over the import graph, the entry points, the "
        "most-called definitions, and how much of the call graph resolved. Read the "
        "resolution rate before trusting the rest: it is the share of in-scope calls "
        "that bound to a definition here, and a low one means the map is partial. No "
        "model, no embeddings, nothing executed.",
        {
            "path": _path_property("the repository to map"),
            "top": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "How many modules and symbols to list. Default 12.",
            },
        },
        ["path"],
        output={
            "name": {"type": "string"},
            "root": {
                "type": "string",
                "description": "The directory that was read, resolved. A client that "
                "passed a relative path or a link needs to know which tree answered.",
            },
            "modules": {"type": "integer"},
            "symbols": {"type": "integer"},
            "loc": {"type": "integer"},
            "total_calls": {"type": "integer"},
            "in_scope_calls": {
                "type": "integer",
                "description": "Calls that could bind to a definition in this repository. "
                "Calls into the standard library or a third-party package are excluded, "
                "because counting them in the denominator is how a resolver looks worse "
                "than it is.",
            },
            "resolved_calls": {"type": "integer"},
            "repo_resolution_rate": {
                "type": "number",
                "description": "resolved / in_scope — THE number to read. Of the calls "
                "that could bind to a definition here, the share that did.",
            },
            "resolution_rate": {
                "type": "number",
                "description": "resolved / every call site, builtins and third-party "
                "calls included. Always much lower and not a measure of anything: most "
                "calls in any repository are `len` or a method on someone else's object, "
                "and none of those could ever bind here. Reported for completeness; read "
                "repo_resolution_rate instead.",
            },
            "core_modules": {
                "type": "array",
                "description": "Ranked by PageRank over the import graph, most central first.",
                "items": {
                    "type": "object",
                    "properties": {
                        "module": {"type": "string", "description": "Dotted import path."},
                        "path": {"type": "string", "description": "Repo-relative file."},
                        "loc": {"type": "integer"},
                        "symbols": {"type": "integer"},
                        "importance": {
                            "type": "number",
                            "description": "PageRank score over the call graph. Comparable "
                            "within one result, not between repositories.",
                        },
                        "imports": {"type": "array", "items": {"type": "string"}},
                        "imported_by": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["module", "path"],
                },
            },
            "entry_points": {
                "type": "array",
                "description": "Called by nothing in this repository: a CLI, a test "
                "helper nobody uses, or dead code. Which one is for the reader to say.",
                "items": _SYMBOL,
            },
            "most_called": {
                "type": "array",
                "description": "Highest fan-in first. Changing one of these reaches furthest.",
                "items": _SYMBOL,
            },
            "unresolved_sample": {
                "type": "array",
                "items": {"type": "array"},
                "description": "In scope and bound to nothing, as [name, count] pairs: "
                "the gap between resolved_calls and in_scope_calls.",
            },
            "out_of_scope_sample": {
                "type": "array",
                "items": {"type": "array"},
                "description": "Ruled OUT of in_scope_calls, as [name, count] pairs - "
                "builtins, the standard library, and methods on objects this repository "
                "does not define. The half of the denominator that can flatter the rate "
                "by shrinking it, so it is shown rather than described.",
            },
            "truncated": {
                "type": "boolean",
                "description": "True when the walk stopped at the file limit. Every "
                "count here is then a floor, not a total.",
            },
            "excluded_projects": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Vendored projects with their own pyproject.toml, skipped "
                "as separate repositories. Their files are missing from everything above.",
            },
            "parse_errors": {
                "type": "array",
                "items": {"type": "array"},
                "description": "Files that did not parse. These are missing from everything above.",
            },
        },
        output_required=[
            "loc",
            "modules",
            "name",
            "repo_resolution_rate",
            "resolved_calls",
            "symbols",
        ],
    ),
    _tool(
        "trace_impact",
        "What breaks if this changes",
        "Every definition that reaches the named one through the call graph, breadth "
        "first, grouped by how many hops away it is. Depth is capped: beyond three hops "
        "almost everything in a small repository is reachable, and a blast radius of "
        "'everything' tells a reader nothing. Give a bare name or a dotted qualname; a "
        "bare name that matches several definitions returns the candidates instead of "
        "guessing which one you meant.",
        {
            "path": _path_property("the repository"),
            "symbol": {
                "type": "string",
                "description": "A definition, by bare name ('parse_repo') or qualname "
                "('cartographer.parse.parse_repo').",
            },
            "depth": {"type": "integer", "minimum": 1, "maximum": 3, "description": "Default 3."},
        },
        ["path", "symbol"],
        output={
            "symbol": {"type": "string"},
            "resolved": {
                "type": ["string", "null"],
                "description": "The qualname it was taken to mean. Null when ambiguous.",
            },
            "candidates": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Present instead of an answer when the name is ambiguous.",
            },
            "callers_by_depth": {
                "type": "object",
                "description": "Keys are 'hop_1', 'hop_2', 'hop_3': the callers at "
                "that distance. hop_1 calls the symbol directly; hop_2 calls something "
                "in hop_1.",
                "additionalProperties": {"type": "array", "items": {"type": "string"}},
            },
            "total": {
                "type": "integer",
                "description": "Callers across every hop when the symbol resolved, and "
                "the number of candidates when it did not - read it with `resolved`, "
                "which says which question was answered.",
            },
        },
        output_required=["symbol", "total"],
    ),
    _tool(
        "check_modernisation",
        "What can be modernised, and what that would change",
        "Modernisations this codebase is eligible for, in two classes that must not be "
        "merged. MECHANICAL edits cannot change behaviour — a typing generic, an "
        "unnecessary parenthesis. BEHAVIOURAL edits can: `datetime.utcnow()` to "
        "`datetime.now(UTC)` changes a naive timestamp into an aware one, which is "
        "correct and is also a change in meaning that a caller may depend on. Each "
        "behavioural edit carries a note saying what it would change. Nothing is "
        "applied; this reads files and returns what it found.",
        {
            "path": _path_property("a file or directory to scan"),
            "rules": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Restrict to these rule names. All applicable rules by "
                "default; list them with no rules and read `rules_available`.",
            },
        },
        ["path"],
        output={
            "files_scanned": {"type": "integer"},
            "mechanical": {"type": "integer"},
            "behavioural": {"type": "integer"},
            "by_rule": {"type": "object", "additionalProperties": {"type": "integer"}},
            "rules_available": {"type": "array", "items": {"type": "string"}},
            "edits": {"type": "array", "items": _EDIT},
            "parse_errors": {
                "type": "array",
                "description": "Files that did not parse. They are missing from the counts "
                "above, so a scan that found nothing in a repository that does not compile "
                "is not a clean scan.",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "error": {"type": "string", "description": "The SyntaxError's text."},
                    },
                    "required": ["path", "error"],
                },
            },
            "truncated": {"type": "boolean"},
        },
        output_required=[
            "behavioural",
            "by_rule",
            "edits",
            "files_scanned",
            "mechanical",
            "rules_available",
        ],
    ),
    _tool(
        "review_diff",
        "Review, keeping only what survives",
        "Review findings for a file or a unified diff, after each proposed finding has "
        "been tested against the file's full context and the ones that do not hold have "
        "been retracted. Both counts come back: `proposed` is what the checks suggested, "
        "`confirmed` is what survived. A retracted finding is not a finding — do not "
        "report it to the user as one. Pass `diff` to review only the lines a change "
        "added, which is the normal case; pass `path` alone to review a whole file.",
        {
            "path": _path_property("a file to review"),
            "diff": {
                "type": "string",
                "description": "A unified diff. Findings are filtered to the lines it "
                "adds, so untouched code is not reported as a problem with this change.",
            },
        },
        ["path"],
        output={
            "path": {"type": "string"},
            "proposed": {"type": "integer"},
            "confirmed": {"type": "integer"},
            "retracted": {
                "type": "integer",
                "description": "Proposed and then disproved. Reported so the ratio is "
                "visible: a checker that retracts nothing is not verifying anything.",
            },
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "line": {"type": "integer"},
                        "rule": {"type": "string"},
                        "message": {"type": "string"},
                        "snippet": {"type": "string"},
                    },
                    "required": ["line", "rule"],
                },
            },
            "parse_error": {
                "type": "string",
                "description": "Empty when the file parsed. When it is not, nothing was "
                "reviewed, and the three counts above are zero for that reason rather "
                "than because the file is clean.",
            },
        },
        output_required=[
            "confirmed",
            "findings",
            "parse_error",
            "path",
            "proposed",
            "retracted",
        ],
    ),
    _tool(
        "release_readiness",
        "Is this safe to ship",
        "Release readiness scored from the repository's own history rather than from "
        "opinion: commit churn, how many files a change spreads across, and whether "
        "changes arrive with tests. Each check comes back as pass, warn or block with "
        "the number behind it, so a verdict can be argued with. Needs a git repository.",
        {
            "path": _path_property("the git repository"),
            "last": {
                "type": "integer",
                "minimum": 1,
                "maximum": 2000,
                "description": "Assess only the most recent N commits - the release "
                "candidate. Omitted, the whole read history is assessed, which is an "
                "audit rather than a release decision.",
            },
            "scan": {
                "type": "integer",
                "minimum": 1,
                "maximum": 2000,
                "description": "How many commits of history to read. Default 200.",
            },
        },
        ["path"],
        output={
            "repo": {"type": "string"},
            "verdict": {"type": "string", "enum": ["pass", "warn", "block"]},
            "blocked": {"type": "boolean"},
            "commits_considered": {"type": "integer"},
            "checks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "status": {"type": "string", "enum": ["pass", "warn", "block"]},
                        "detail": {"type": "string"},
                        "observed": {
                            "type": "number",
                            "description": "What this check measured. The status is this "
                            "compared with `threshold`, so a caller can disagree with the "
                            "threshold and still use the number.",
                        },
                        "threshold": {
                            "type": "number",
                            "description": "The value `observed` had to stay under to pass.",
                        },
                    },
                    "required": ["name", "status"],
                },
            },
        },
        output_required=["blocked", "checks", "commits_considered", "repo", "verdict"],
    ),
    _tool(
        "riskiest_commits",
        "Which commits carry the most risk",
        "Recent commits ranked by a risk score built from churn, how many files they "
        "touch, how far they spread across the tree, and whether they bring tests with "
        "them. The score is a reading of the diff, not a judgement of the author, and "
        "the components are returned alongside it so it can be checked.",
        {
            "path": _path_property("the git repository"),
            "scan": {
                "type": "integer",
                "minimum": 1,
                "maximum": 2000,
                "description": "Commits of history to read. Default 200.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "How many to return. Default 10.",
            },
        },
        ["path"],
        output={
            "repo": {"type": "string"},
            "commits_considered": {"type": "integer"},
            "median_churn": {
                "type": "number",
                "description": "What a normal commit looks like here. Scores are relative "
                "to it, because a 400-line change is unremarkable in a repository whose "
                "median is 700 and alarming in one whose median is 20.",
            },
            "median_files": {"type": "number"},
            "commits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "sha": {"type": "string"},
                        "subject": {"type": "string"},
                        "score": {"type": "number", "description": "0-100."},
                        "churn": {"type": "integer"},
                        "files": {"type": "integer"},
                        "spread": {"type": "integer"},
                        "touches_tests": {"type": "boolean"},
                        "because": {
                            "type": "array",
                            "description": "The score's components, so a ranking can be "
                            "argued with rather than taken on faith.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "factor": {"type": "string"},
                                    "raw": {"type": "number"},
                                    "points": {"type": "number"},
                                },
                                "required": ["factor", "points"],
                            },
                        },
                    },
                    "required": ["sha", "score"],
                },
            },
        },
        output_required=["commits", "commits_considered", "median_churn", "repo"],
    ),
]


class ToolError(ValueError):
    """A bad argument. Reported to the model as a tool result it can correct."""


class ProtocolError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---- argument handling ---------------------------------------------------------------


class ConfigError(Exception):
    """A setting that cannot be honoured. Refuses to start rather than fail open."""


def _root() -> Path | None:
    """The directory every path must sit under, when one is configured.

    An empty or blank value used to mean "no confinement", silently - the one way to
    set this variable wrong is the one that turns it off, and the startup banner then
    omitted the "confined to" line that would have shown it. A value that was set and
    cannot be used is a configuration error, not a default.
    """
    raw = os.environ.get(ROOT_ENV)
    if raw is None:
        return None
    if not raw.strip():
        raise ConfigError(
            f"{ROOT_ENV} is set to an empty value. Unset it to run unconfined, or give "
            "it a directory; an empty value will not be read as 'no limit'."
        )
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise ConfigError(f"{ROOT_ENV} is {raw!r}, which is not a directory.")
    return root


def _confined_repository(root: Path) -> None:
    """Refuse a checkout whose git directory lives outside the confinement root.

    `.git` is usually a directory, and sometimes a *file* holding `gitdir: <path>` -
    that is how worktrees and submodules are laid out. `is_repository` only asks
    whether the entry exists, so a one-line `.git` file placed inside the root pointed
    git at a repository outside it, and `riskiest_commits` returned that repository's
    commit subjects, authors and file paths. The path check is on the argument; this
    is the redirection the argument does not mention.
    """
    confine = _root()
    if confine is None:
        return
    marker = root / ".git"
    if not marker.is_file():
        return
    try:
        text = marker.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        # The confinement check could not be made. Returning quietly would let the call
        # through on exactly the file that exists to redirect it.
        raise ToolError(f"{marker} cannot be read, so it cannot be checked: {exc}") from None
    if not text.startswith("gitdir:"):
        return
    pointed = Path(text.split(":", 1)[1].strip()).expanduser()
    pointed = (root / pointed).resolve() if not pointed.is_absolute() else pointed.resolve()
    if confine not in pointed.parents and pointed != confine:
        raise ToolError(f"{root}/.git points at {pointed}, which is outside {ROOT_ENV} ({confine})")


def _resolve(value: Any, *, name: str = "path", want: str = "any") -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"'{name}' must be a non-empty string")
    path = Path(value).expanduser()
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        raise ToolError(f"{value!r}: {exc.strerror or 'cannot be read'}") from None

    try:
        confine = _root()
    except ConfigError as exc:
        raise ToolError(str(exc)) from None
    if confine is not None and confine not in path.parents and path != confine:
        # When the operator sets a root, a path outside it is refused rather than
        # quietly read: this server's whole input is a filesystem path, and that is
        # the one thing worth bounding.
        raise ToolError(f"{value!r} is outside {ROOT_ENV} ({confine})")
    if want == "dir" and not path.is_dir():
        raise ToolError(f"{value!r} is not a directory")
    if want == "file" and not path.is_file():
        raise ToolError(f"{value!r} is not a file")
    return path


def _int(arguments: dict, name: str, default: int, low: int, high: int) -> int:
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        if value is None:
            return default
        raise ToolError(f"'{name}' must be an integer, got {type(value).__name__}")
    if not low <= value <= high:
        raise ToolError(f"'{name}' must be between {low} and {high}, got {value}")
    return value


def _string(arguments: dict, name: str, *, limit: int = 4_000) -> str | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolError(f"'{name}' must be a string, got {type(value).__name__}")
    if len(value) > limit:
        raise ToolError(f"'{name}' is {len(value):,} characters; the limit is {limit:,}")
    return value.strip() or None


def _python_files(target: Path) -> tuple[list[Path], bool, list[Path]]:
    """Python files under `target`, capped.

    Returns (files, truncated, broken links). The last two are why a scan read less
    than the tree, and a scan that read less than the tree should say so.
    """
    if target.is_file():
        return [target], False, []
    skip = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache"}
    # `rglob` follows directory symlinks and Windows junctions, so a link inside the
    # tree reaches files outside it. With REPO_RECON_ROOT set, a junction under the root
    # pointing anywhere readable was walked and its contents returned — the confinement
    # held for the path it was given and not for the paths it then found. Every file is
    # re-checked against the boundary after resolution.
    confine = _root()
    boundary = confine if confine is not None else target.resolve()
    found: list[Path] = []
    broken: list[Path] = []
    for path in sorted(target.rglob("*.py")):
        if any(part in skip for part in path.parts):
            continue
        try:
            resolved = path.resolve()
        except OSError:
            # A broken link. Not scanned, and not silently absent either: it comes back
            # as a parse error so a scan that read less than the tree is visibly so.
            broken.append(path)
            continue
        if not (resolved == boundary or resolved.is_relative_to(boundary)):
            continue
        found.append(path)
        if len(found) >= FILE_LIMIT:
            return found, True, broken
    return found, False, broken


def _read(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > SOURCE_LIMIT:
        raise ToolError(f"{path.name} is {len(text):,} characters; the limit is {SOURCE_LIMIT:,}")
    return text


# ---- the tools -----------------------------------------------------------------------


class ReconServer:
    """Each method takes the tool's arguments and returns its structured result."""

    def map_repo(self, arguments: dict) -> dict:
        root = _resolve(arguments.get("path"), want="dir")
        top = _int(arguments, "top", 12, 1, 100)
        report = build_report(root, top=top)
        payload = asdict(report)
        for key in ("core_modules", "entry_points", "most_called"):
            payload[key] = payload.get(key, [])[:LIST_LIMIT]
        for key in ("unresolved_sample", "out_of_scope_sample", "parse_errors"):
            # Pairs, as the schema says: a tuple survives asdict and is only a list
            # once JSON has touched it, which is too late for a client reading the
            # in-process result - and a real validator over the same payload says so.
            payload[key] = [list(pair) for pair in payload.get(key, [])[:LIST_LIMIT]]
        return payload

    def trace_impact(self, arguments: dict) -> dict:
        root = _resolve(arguments.get("path"), want="dir")
        symbol = _string(arguments, "symbol")
        if not symbol:
            raise ToolError("'symbol' is required")
        depth = _int(arguments, "depth", 3, 1, 3)

        graph = build_graph(parse_repo(root))
        if symbol in graph.by_qualname:
            resolved = symbol
        else:
            # An exact tail match first: `helper` means the definition called helper,
            # not every qualname containing the letters. The bare substring made any
            # short name ambiguous with its own longer neighbours - one `helper` and
            # one `helper_extended` were reported as two candidates for `helper` - and
            # every short name is a prefix of something. The substring remains as a
            # fallback, where nothing matched exactly, because `Class.method` typed as
            # `method` should still find it.
            matches = sorted(q for q in graph.by_qualname if q.endswith(f".{symbol}"))
            if not matches:
                matches = sorted(q for q in graph.by_qualname if symbol in q)
            if not matches:
                return {
                    "symbol": symbol,
                    "resolved": None,
                    "candidates": [],
                    "callers_by_depth": {},
                    "total": 0,
                }
            if len(matches) > 1:
                # Ambiguity is reported, not resolved by picking the first: answering
                # about a different definition of the same name is a wrong answer that
                # looks right.
                return {
                    "symbol": symbol,
                    "resolved": None,
                    "candidates": matches[:LIST_LIMIT],
                    "callers_by_depth": {},
                    "total": len(matches),
                }
            resolved = matches[0]

        by_depth = impact_of(graph, resolved, depth=depth)
        trimmed = {str(k): sorted(v)[:LIST_LIMIT] for k, v in by_depth.items()}
        return {
            "symbol": symbol,
            "resolved": resolved,
            "candidates": [],
            "callers_by_depth": trimmed,
            "total": sum(len(v) for v in by_depth.values()),
        }

    def check_modernisation(self, arguments: dict) -> dict:
        target = _resolve(arguments.get("path"))
        rules = arguments.get("rules")
        if rules is not None:
            if not isinstance(rules, list) or not all(isinstance(r, str) for r in rules):
                raise ToolError("'rules' must be a list of rule names")
            unknown = sorted(set(rules) - set(RULE_KIND))
            if unknown:
                raise ToolError(
                    f"unknown rule(s): {', '.join(unknown)}. Available: "
                    + ", ".join(sorted(RULE_KIND))
                )
            rules = set(rules)

        files, truncated, broken = _python_files(target)
        edits: list[dict] = []
        by_rule: dict[str, int] = {}
        errors: list[dict] = []
        mechanical = behavioural = 0

        def shown(p: Path) -> str:
            """Repo-relative, which is what map_repo reports and what the schema says.

            These were absolute while `_SYMBOL.path` beside them was relative, so two
            tools answering about the same file disagreed about how to name it.
            """
            try:
                return p.relative_to(target if target.is_dir() else target.parent).as_posix()
            except ValueError:
                return str(p)

        for path in broken:
            errors.append({"path": shown(path), "error": "unreadable: broken link"})
        for path in files:
            try:
                scan = scan_source(str(path), _read(path), rules)
            except (OSError, ToolError) as exc:
                errors.append({"path": shown(path), "error": str(exc)})
                continue
            if scan.parse_error:
                errors.append({"path": shown(path), "error": scan.parse_error})
                continue
            for edit in scan.edits:
                mechanical += edit.mechanical
                behavioural += not edit.mechanical
                by_rule[edit.rule] = by_rule.get(edit.rule, 0) + 1
                if len(edits) < LIST_LIMIT:
                    edits.append(
                        {
                            "path": shown(path),
                            "line": edit.line,
                            "rule": edit.rule,
                            "kind": edit.kind,
                            "before": edit.before,
                            "replacement": edit.replacement,
                            "note": edit.note,
                        }
                    )

        return {
            "files_scanned": len(files),
            "mechanical": mechanical,
            "behavioural": behavioural,
            "by_rule": dict(sorted(by_rule.items())),
            "rules_available": sorted(RULE_KIND),
            "edits": edits,
            "parse_errors": errors[:LIST_LIMIT],
            "truncated": truncated,
        }

    def review_diff(self, arguments: dict) -> dict:
        path = _resolve(arguments.get("path"), want="file")
        if path.suffix not in {".py", ".pyi"}:
            # Pointed at a README, this reported "0 proposed, 0 confirmed, 0 retracted"
            # - indistinguishable from a clean Python file, and a one-line markdown
            # file parses as Python because the line is a comment. These checks are
            # Python checks; anything else is a wrong argument, not a clean result.
            raise ToolError(f"{path.name} is not a Python file; these checks read Python")
        diff = _string(arguments, "diff", limit=DIFF_LIMIT)

        changed: set[int] | None = None
        if diff:
            from .reviewbot.review import added_lines

            per_file = added_lines(diff)
            if not per_file:
                raise ToolError(
                    "that diff names no files; pass a unified diff, or omit it to review "
                    "the whole file"
                )
            # Match on the suffix: a diff names paths relative to the repository root
            # and the tool is given an absolute one.
            wanted = str(path).replace("\\", "/")
            for name, lines in per_file.items():
                candidate = name.removeprefix("a/").removeprefix("b/")
                if wanted.endswith("/" + candidate) or wanted == candidate:
                    changed = lines
                    break
            if changed is None:
                # The diff touched other files and not this one, so this file has no
                # added lines to review. The previous version unioned every other
                # file's line numbers and applied them here, which reported findings on
                # lines the change never touched - a finding about code the author did
                # not write in this change is exactly the noise the verify step exists
                # to remove.
                changed = set()

        source = _read(path)
        # A file that does not parse proposes nothing, and "0 proposed, 0 confirmed,
        # 0 retracted" is exactly what a clean file looks like. check_modernisation gets
        # this right and its own schema says why: a scan that found nothing in a
        # repository that does not compile is not a clean scan.
        parse_error = ""
        try:
            ast.parse(source)
        except SyntaxError as exc:
            parse_error = f"line {exc.lineno}: {exc.msg}"

        file_review = review_source(str(path), source, changed)
        findings = [
            {
                "line": verdict.proposal.line,
                "rule": verdict.proposal.rule,
                "message": verdict.proposal.message,
                "snippet": verdict.proposal.snippet,
            }
            for verdict in file_review.confirmed
        ]
        return {
            "path": str(path),
            "proposed": len(file_review.verdicts),
            "confirmed": len(file_review.confirmed),
            "retracted": len(file_review.verdicts) - len(file_review.confirmed),
            "findings": findings[:LIST_LIMIT],
            "parse_error": parse_error,
        }

    def release_readiness(self, arguments: dict) -> dict:
        root = _resolve(arguments.get("path"), want="dir")
        _confined_repository(root)
        scan = _int(arguments, "scan", 200, 1, 2000)
        # `evaluate(since=...)` is a COUNT of the most recent commits, not a revision.
        # The tool used to advertise "revision or date" and pass the string straight
        # into a list slice, so every value of it raised TypeError - a parameter that
        # could not be used at all, behind a description that said what it did not do.
        last = arguments.get("last")
        last = None if last is None else _int(arguments, "last", 200, 1, 2000)
        try:
            history = read_history(root, limit=scan)
        except NotAGitRepository as exc:
            raise ToolError(str(exc)) from None
        result = evaluate(history, since=last)
        return {
            "repo": result.repo,
            "verdict": "block" if result.blocked else ("warn" if result.warnings else "pass"),
            "blocked": result.blocked,
            "commits_considered": result.commits_considered,
            "checks": [asdict(check) for check in result.checks],
        }

    def riskiest_commits(self, arguments: dict) -> dict:
        root = _resolve(arguments.get("path"), want="dir")
        _confined_repository(root)
        scan = _int(arguments, "scan", 200, 1, 2000)
        limit = _int(arguments, "limit", 10, 1, 100)
        try:
            history = read_history(root, limit=scan)
        except NotAGitRepository as exc:
            raise ToolError(str(exc)) from None

        # Risk is relative to what a normal commit looks like HERE: a 400-line change
        # is unremarkable in a repository whose median is 700 and alarming in one whose
        # median is 20. Scoring against a fixed baseline would rank every commit in a
        # busy repository as risky and none in a quiet one.
        baseline = Baseline.from_history(history)
        scored = [(commit, score_commit(commit, baseline)) for commit in history.commits]
        ranked = sorted(scored, key=lambda pair: -pair[1].score())[:limit]
        return {
            "repo": str(root),
            "commits_considered": len(history.commits),
            "median_churn": baseline.median_churn,
            "median_files": baseline.median_files,
            "commits": [
                {
                    "sha": commit.sha,
                    "subject": commit.subject,
                    "score": factors.score(),
                    "churn": commit.churn,
                    "files": len(commit.files),
                    "spread": commit.spread,
                    "touches_tests": commit.touches_tests,
                    # The components, so a ranking can be argued with rather than
                    # taken on faith.
                    "because": [
                        {"factor": name, "raw": raw, "points": points}
                        for name, raw, points in factors.explain()
                        if points
                    ],
                }
                for commit, factors in ranked
            ],
        }


# ---- protocol ------------------------------------------------------------------------

_BY_NAME = {tool["name"]: tool for tool in TOOLS}


def _result(payload: dict, protocol: str) -> dict:
    text = json.dumps(payload, indent=2, default=str)
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if protocol in _STRUCTURED:
        out["structuredContent"] = payload
    return out


def _error_result(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def serve(stdin: BinaryIO, stdout: BinaryIO, server: ReconServer) -> None:
    """One request per line until stdin closes."""
    protocol = PROTOCOL_VERSIONS[0]

    def send(message: dict) -> None:
        stdout.write((json.dumps(message) + "\n").encode("utf-8"))
        stdout.flush()

    for raw in stdin:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": PARSE_ERROR, "message": f"invalid JSON: {exc}"},
                }
            )
            continue

        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": INVALID_REQUEST, "message": "not a JSON-RPC 2.0 message"},
                }
            )
            continue

        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        if "id" not in message:
            continue  # a notification; nothing to answer
        if request_id is None:
            # An explicit null id is not a notification. Treating it as one left a
            # client that uses null ids waiting for a reply that never came; MCP
            # forbids the null id, so it is answered as the malformed request it is.
            send(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": INVALID_REQUEST,
                        "message": "id must not be null; use a string or an integer",
                    },
                }
            )
            continue

        try:
            if not isinstance(params, dict):
                # `.get` on a list is an AttributeError, which came back as -32603 with
                # the Python exception type in the message: an internal error reported
                # for a caller's mistake, and the implementation leaking into the wire.
                raise ProtocolError(INVALID_PARAMS, "'params' must be an object")
            if not isinstance(method, str):
                # Missing or non-string: the request is malformed, which is -32600. It
                # was reported as -32601 "unknown method: None", a different claim.
                raise ProtocolError(INVALID_REQUEST, "request has no method")
            if method == "initialize":
                asked = params.get("protocolVersion")
                protocol = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                result: dict[str, Any] = {
                    "protocolVersion": protocol,
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": SERVER_NAME,
                        "title": "Repo Recon",
                        "version": __version__,
                    },
                    "instructions": INSTRUCTIONS,
                }
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                name = params.get("name")
                arguments = params.get("arguments") or {}
                if name not in _BY_NAME:
                    raise ProtocolError(INVALID_PARAMS, f"unknown tool: {name!r}")
                if not isinstance(arguments, dict):
                    raise ProtocolError(INVALID_PARAMS, "'arguments' must be an object")
                # Every input schema says additionalProperties: false, and nothing
                # enforced it: `{"path": ..., "tpo": 5}` ran happily with top=12, so a
                # typo silently got the default and the caller was told it had asked
                # for something it had not.
                allowed = set(_BY_NAME[name]["inputSchema"].get("properties", {}))
                unknown = sorted(set(arguments) - allowed)
                if unknown:
                    raise ProtocolError(
                        INVALID_PARAMS,
                        f"{name} has no argument {unknown[0]!r}; it takes "
                        + ", ".join(sorted(allowed)),
                    )
                try:
                    payload = getattr(server, name)(arguments)
                except ToolError as exc:
                    # A correctable argument is a tool result, not a protocol error:
                    # the model can read it and try again.
                    result = _error_result(str(exc))
                else:
                    result = _result(payload, protocol)
            elif method == "ping":
                result = {}
            else:
                raise ProtocolError(METHOD_NOT_FOUND, f"unknown method: {method!r}")
        except ProtocolError as exc:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": exc.code, "message": exc.message},
                }
            )
            continue
        except Exception as exc:  # pragma: no cover - reported, never a crash
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": INTERNAL_ERROR,
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                }
            )
            continue

        send({"jsonrpc": "2.0", "id": request_id, "result": result})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="repo-recon-mcp",
        description="Four read-only codebase readers as one MCP server over stdio.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Print what this server exposes and exit, without reading stdin.",
    )
    args = parser.parse_args(argv)

    server = ReconServer()
    try:
        confine = _root()
    except ConfigError as exc:
        # Before serving, and on stderr: a server that starts unconfined because its
        # confinement was mis-set is worse than one that does not start.
        print(f"{SERVER_NAME}: {exc}", file=sys.stderr, flush=True)
        return 2
    if args.check:
        print(
            json.dumps(
                {
                    "server": SERVER_NAME,
                    "version": __version__,
                    "protocol": PROTOCOL_VERSIONS[0],
                    "tools": [t["name"] for t in TOOLS],
                    "read_only": True,
                    "root": str(confine) if confine else None,
                },
                indent=2,
            )
        )
        return 0

    print(
        f"{SERVER_NAME} {__version__}: {len(TOOLS)} read-only tools"
        + (f", confined to {confine}" if confine else ""),
        file=sys.stderr,
        flush=True,
    )
    serve(sys.stdin.buffer, sys.stdout.buffer, server)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
