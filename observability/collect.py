"""JSON Lines in, panels out. Pure functions, so a panel is testable without a server.

The two panels this console exists for are the two numbers this package tells you to read
before trusting anything else:

**Resolution rate.** `map_repo`'s own description says to read it first — it is the share
of in-scope calls that bound to a definition in the repository, and a low one means the
map is partial. A console showing "52 modules, 625 symbols" and omitting the rate would
be presenting a number the tool itself says not to trust alone.

**Proposed against confirmed.** `review_diff` proposes findings, tries to disprove each
one, and retracts those that do not hold. One run of it went 156 proposed, 155 retracted,
1 confirmed. A console reporting "156 findings" would be reporting the number the tool
spent its effort disagreeing with.

As in the sibling project, a percentile over very few calls is not a percentile, and each
one carries the count it came from.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

#: Below this, a percentile is reported with the count beside it rather than alone.
THIN = 20


def read(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    """Every readable event, oldest first. A half-written last line is skipped.

    A live server appends while this reads, so the final line can be truncated at the
    moment it is parsed. One damaged line must cost one event, not the whole console.
    """
    events: list[dict[str, Any]] = []
    for candidate in (path.with_suffix(path.suffix + ".1"), path):
        if not candidate.exists():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                events.append(event)
    events.sort(key=lambda e: e.get("at") or 0)
    return events[-limit:] if limit else events


def within(events: Iterable[dict], seconds: float | None) -> list[dict]:
    """The events from the last `seconds`, or all of them when seconds is None."""
    if not seconds:
        return list(events)
    cutoff = time.time() - seconds
    return [e for e in events if (e.get("at") or 0) >= cutoff]


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * len(ordered) + 0.5) - 1))
    return ordered[index]


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def summarise(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Every panel the console draws."""
    durations = [float(e.get("ms") or 0) for e in events]
    outcomes = Counter(str(e.get("outcome") or "unknown") for e in events)

    by_tool: dict[str, dict[str, Any]] = {}
    for event in events:
        tool = str(event.get("tool") or "unknown")
        row = by_tool.setdefault(
            tool, {"tool": tool, "calls": 0, "ok": 0, "errors": 0, "rejected": 0, "ms": []}
        )
        row["calls"] += 1
        outcome = str(event.get("outcome") or "unknown")
        if outcome == "ok":
            row["ok"] += 1
        elif outcome in ("rejected", "unknown_tool"):
            row["rejected"] += 1
        else:
            row["errors"] += 1
        row["ms"].append(float(event.get("ms") or 0))

    tools = []
    for row in by_tool.values():
        times = row.pop("ms")
        row["p50"] = round(_percentile(times, 0.50), 2)
        row["p95"] = round(_percentile(times, 0.95), 2)
        row["max"] = round(max(times), 2) if times else 0.0
        row["thin"] = len(times) < THIN
        tools.append(row)
    tools.sort(key=lambda r: (-r["calls"], r["tool"]))

    # ---- signature panel 1: how much of the call graph actually resolved -------------
    rates = [float(e["repo_resolution_rate"]) for e in events if "repo_resolution_rate" in e]
    all_rates = [float(e["resolution_rate"]) for e in events if "resolution_rate" in e]
    resolution = {
        "reads": len(rates),
        "mean": _mean(rates),
        "worst": round(min(rates), 4) if rates else None,
        "best": round(max(rates), 4) if rates else None,
        # The other number, kept beside it on purpose: it counts every call site
        # including `len()` and `print()`, so it is mostly a statement about the
        # standard library. Shown so nobody quotes it as the repository's figure.
        "all_call_sites_mean": _mean(all_rates),
    }

    # ---- signature panel 2: what review_diff retracted -------------------------------
    proposed = sum(
        int(e["proposed"]) for e in events if isinstance(e.get("proposed"), (int, float))
    )
    confirmed = sum(
        int(e["confirmed"]) for e in events if isinstance(e.get("confirmed"), (int, float))
    )
    retracted = sum(
        int(e["retracted"]) for e in events if isinstance(e.get("retracted"), (int, float))
    )
    reviews = sum(1 for e in events if "proposed" in e)
    review = {
        "reviews": reviews,
        "proposed": proposed,
        "confirmed": confirmed,
        "retracted": retracted,
        # The share of proposals that did not survive being argued with.
        "retraction_rate": round(retracted / proposed, 3) if proposed else None,
    }

    # ---- the confinement, doing its job ----------------------------------------------
    refused_paths = sum(
        int(e["refused_paths"]) for e in events if isinstance(e.get("refused_paths"), (int, float))
    )

    modernisation = {
        "mechanical": sum(
            int(e["mechanical"]) for e in events if isinstance(e.get("mechanical"), (int, float))
        ),
        "behavioural": sum(
            int(e["behavioural"]) for e in events if isinstance(e.get("behavioural"), (int, float))
        ),
        "files_scanned": sum(
            int(e["files_scanned"])
            for e in events
            if isinstance(e.get("files_scanned"), (int, float))
        ),
    }

    verdicts = Counter(str(e["verdict"]) for e in events if e.get("verdict"))

    return {
        "calls": len(events),
        "first": min((e.get("at") or 0) for e in events) if events else None,
        "last": max((e.get("at") or 0) for e in events) if events else None,
        "outcomes": dict(outcomes.most_common()),
        "p50": round(_percentile(durations, 0.50), 2),
        "p95": round(_percentile(durations, 0.95), 2),
        "max": round(max(durations), 2) if durations else 0.0,
        "thin": len(durations) < THIN,
        "thin_at": THIN,
        "tools": tools,
        "resolution": resolution,
        "review": review,
        "refused_paths": refused_paths,
        "modernisation": modernisation,
        "verdicts": dict(verdicts.most_common()),
        "recent": [
            {
                k: v
                for k, v in e.items()
                if k
                in (
                    "at",
                    "tool",
                    "outcome",
                    "ms",
                    "repo_resolution_rate",
                    "proposed",
                    "confirmed",
                    "retracted",
                    "refused_paths",
                    "verdict",
                )
            }
            for e in events[-25:][::-1]
        ],
    }
