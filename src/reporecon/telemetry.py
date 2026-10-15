"""One line per tool call, appended to a file you name — and nothing at all otherwise.

This package's headline is that the server **writes nothing**. That is why this is OFF
unless `REPO_RECON_TELEMETRY` holds a path: left unset, no file is created, nothing is
appended, and the claim stands exactly as written. Set it and the server writes one file,
the one you named, and nothing else — the tools still write nothing, and the confinement
root still refuses every path outside it.

The format is JSON Lines: one object per line, append-only, no database. `tail -f` works
on it, `grep` works on it, and deleting the file is a complete uninstall.

What is recorded is the SHAPE of a call, never a repository's contents. The tool name,
how long it took, and the handful of figures the console has a panel for — the resolution
rate, how many findings a review retracted, how many paths the confinement refused. Not
the file names, not the symbols, not the commit messages. A path on somebody's disk is
about them, and `tests/test_telemetry.py` asserts that the repository path put through the
server does not appear in the log it produced.

Three rules, because telemetry that breaks the thing it measures is worse than none:

  * It never raises into the caller. Every failure path ends in `return`.
  * It never costs more than a line write: no network, no lock, no fsync, no thread.
  * It is bounded. The file rotates at 8 MB keeping one generation, so a long-running
    server cannot fill the disk it was pointed at.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

#: The variable that switches this on, and the file it names.
ENV = "REPO_RECON_TELEMETRY"

#: Rotate past this, keeping one generation: 16 MB worst case.
MAX_BYTES = 8 * 1024 * 1024

#: Numbers copied out of a result when it carries them — each one has a panel.
#:
#: Every entry is a COUNT or a RATE. Nothing here is a name: `map_repo` returns module
#: names and `riskiest_commits` returns commit subjects, and neither belongs in a file
#: that outlives the call. The console shows how much, never what.
_NUMERIC_FIELDS = (
    # map_repo — the rate its own description says to read before trusting the rest
    "resolution_rate",
    "repo_resolution_rate",
    "modules",
    "symbols",
    "lines",
    # review_diff — the retraction ratio, which is this package's argument in one line
    "proposed",
    "confirmed",
    "retracted",
    "disproved",
    # check_modernisation — two counts that must never be merged
    "mechanical",
    "behavioural",
    "files_scanned",
    "files_read",
    "files_skipped",
    # trace_impact
    "reached_by",
    # release_readiness / riskiest_commits
    "commits_considered",
    "median_churn",
)


def destination() -> Path | None:
    """The file to append to, or None when telemetry is off."""
    raw = (os.environ.get(ENV) or "").strip()
    return Path(raw) if raw else None


def _rotate(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size >= MAX_BYTES:
            path.replace(path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass


def record(event: dict[str, Any]) -> None:
    """Append one event, if telemetry is on. Never raises."""
    path = destination()
    if path is None:
        return
    try:
        event.setdefault("at", time.time())
        line = json.dumps(event, ensure_ascii=False, default=str)
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate(path)
        with path.open("a", encoding="utf-8", newline="") as handle:
            handle.write(line + "\n")
    except (OSError, TypeError, ValueError, UnicodeError):
        return


def from_result(result: Any) -> dict[str, Any]:
    """The figures the console has a panel for, if the result carries them.

    `refused_paths` is reduced to its LENGTH. The confinement refusing a path is worth
    counting - it is the guard doing its job - and the path itself is a location on
    somebody's disk, which is not.
    """
    out: dict[str, Any] = {}
    if not isinstance(result, dict):
        return out
    for field in _NUMERIC_FIELDS:
        value = result.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[field] = value
    refused = result.get("refused_paths")
    if isinstance(refused, list):
        out["refused_paths"] = len(refused)
    verdict = result.get("verdict")
    if isinstance(verdict, str):
        out["verdict"] = verdict
    return out
