"""The call log, and the console's arithmetic over it.

Three properties carry the weight, and each is a promise this could quietly break:

  * **Off unless asked.** The README's first sentence is that this server's tools write
    nothing. A build that creates a file the moment it is installed would make that
    sentence false, so the default is asserted over a directory rather than over the
    function that decides it.
  * **No path reaches the log.** A path on somebody's disk is about them. The log holds
    counts and rates - how much resolved, how many findings were retracted, how many
    paths the confinement refused - and this puts a repository through the server and
    asserts its path is not in the file afterwards.
  * **Recording cannot break a read.** A full disk during `map_repo` must not become a
    failed `map_repo`.

And one about placement, because it was wrong first: the instrumentation went into the
protocol dispatch inside `serve()`, which a direct caller never reaches - and this
repository's own tests call `server.map_repo({...})` directly, so none of them produced an
event. It lives on `ReconServer.call` now, and the test below calls both ways round.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from reporecon import telemetry
from reporecon.mcp_server import ReconServer, ToolError

ROOT = Path(__file__).resolve().parents[1]


def _collect():
    """`observability/collect.py`, which is the console's arithmetic and not part of the
    installed package."""
    spec = importlib.util.spec_from_file_location(
        "reporecon_observability_collect", ROOT / "observability" / "collect.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def server():
    return ReconServer()


def _exercise(server, target: Path) -> None:
    server.call("map_repo", {"path": str(target), "top": 3})
    server.call("check_modernisation", {"path": str(target)})
    with pytest.raises(ToolError):
        server.call("map_repo", {"path": str(target / "does-not-exist")})


def test_nothing_is_written_when_the_variable_is_unset(server, tmp_path, monkeypatch):
    """The default, asserted over a directory. The README's first sentence depends on it."""
    monkeypatch.delenv(telemetry.ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    assert telemetry.destination() is None
    server.call("map_repo", {"path": str(ROOT / "src"), "top": 3})
    assert list(tmp_path.rglob("*")) == []


def test_a_direct_call_is_recorded_too(server, tmp_path, monkeypatch):
    """Not only one that arrived over the protocol.

    This is the placement bug, kept as a test: with the instrumentation in `serve()`'s
    nested dispatch, every call in this suite - all of which go through the server object
    directly - produced nothing at all.
    """
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv(telemetry.ENV, str(log))
    server.call("map_repo", {"path": str(ROOT / "src"), "top": 3})
    written = log.read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in written if line.strip()]
    assert len(rows) == 1
    assert rows[0]["tool"] == "map_repo"
    assert rows[0]["outcome"] == "ok"
    assert "repo_resolution_rate" in rows[0]


def test_a_failed_call_is_recorded_as_one(server, tmp_path, monkeypatch):
    """A console that records what worked and drops what did not is the wrong way round."""
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv(telemetry.ENV, str(log))
    with pytest.raises(ToolError):
        server.call("map_repo", {"path": str(tmp_path / "nowhere")})
    written = log.read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in written if line.strip()]
    assert [r["outcome"] for r in rows] == ["tool_error"]


def test_no_path_reaches_the_log(server, tmp_path, monkeypatch):
    """Counts and rates, never a location on somebody's disk."""
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv(telemetry.ENV, str(log))
    target = ROOT / "src" / "reporecon" / "captain"
    server.call("map_repo", {"path": str(target), "top": 3})

    written = log.read_text(encoding="utf-8")
    assert "captain" not in written, "a path reached the log"
    assert str(ROOT) not in written, "a path reached the log"
    # ...while the figures the console draws did.
    assert "repo_resolution_rate" in written


def test_an_unwritable_destination_does_not_break_a_read(server, tmp_path, monkeypatch):
    """A directory where the file should be, which is the mistake a reader will make."""
    blocked = tmp_path / "a-directory"
    blocked.mkdir()
    monkeypatch.setenv(telemetry.ENV, str(blocked))
    payload = server.call("map_repo", {"path": str(ROOT / "src"), "top": 3})
    assert payload["modules"] > 0


def test_the_log_rotates_rather_than_growing_without_bound(tmp_path, monkeypatch):
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv(telemetry.ENV, str(log))
    monkeypatch.setattr(telemetry, "MAX_BYTES", 400)
    for index in range(80):
        telemetry.record({"tool": "map_repo", "outcome": "ok", "ms": 1.0, "n": index})
    assert log.with_suffix(".jsonl.1").exists(), "nothing was rotated"
    assert log.stat().st_size < 4000


def test_a_half_written_line_does_not_empty_the_console(tmp_path):
    """A live server appends while the console reads."""
    collect = _collect()
    log = tmp_path / "calls.jsonl"
    good = json.dumps({"at": 1.0, "tool": "map_repo", "outcome": "ok", "ms": 1.0})
    log.write_text(good + "\n" + good + "\n" + '{"at": 3.0, "tool": "map_re', encoding="utf-8")
    assert len(collect.read(log)) == 2


def test_the_two_resolution_rates_are_kept_apart(tmp_path):
    """The repo's figure and the one over every call site are different numbers.

    `resolution_rate` counts every call site including `len()` and `print()`, so it is
    mostly a statement about the standard library. The console shows both, and shows
    which is which, because quoting the second as the repository's is the mistake the
    tool's own description warns about.
    """
    collect = _collect()
    log = tmp_path / "calls.jsonl"
    log.write_text(
        "".join(
            json.dumps(
                {
                    "at": float(i),
                    "tool": "map_repo",
                    "outcome": "ok",
                    "ms": 1.0,
                    "repo_resolution_rate": 0.9,
                    "resolution_rate": 0.25,
                }
            )
            + "\n"
            for i in range(2)
        ),
        encoding="utf-8",
    )
    summary = collect.summarise(collect.read(log))
    assert summary["resolution"]["mean"] == 0.9
    assert summary["resolution"]["all_call_sites_mean"] == 0.25
    assert summary["resolution"]["reads"] == 2


def test_the_retraction_ratio_is_over_what_was_proposed(tmp_path):
    """156 of 157 retracted is the number worth showing, and it is a share of proposals."""
    collect = _collect()
    log = tmp_path / "calls.jsonl"
    log.write_text(
        json.dumps(
            {
                "at": 1.0,
                "tool": "review_diff",
                "outcome": "ok",
                "ms": 1.0,
                "proposed": 157,
                "confirmed": 1,
                "retracted": 156,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    summary = collect.summarise(collect.read(log))
    review = summary["review"]
    assert review["proposed"] == 157
    assert review["confirmed"] + review["retracted"] == review["proposed"]
    assert review["retraction_rate"] == round(156 / 157, 3)


def test_no_reviews_is_not_a_retraction_rate_of_zero(tmp_path):
    """Nothing proposed and nothing retracted is not "retracts nothing"; it is no data.

    A zero here would read as a tool that confirms everything it proposes, which is the
    opposite of what an empty log means.
    """
    collect = _collect()
    log = tmp_path / "calls.jsonl"
    log.write_text(
        json.dumps({"at": 1.0, "tool": "map_repo", "outcome": "ok", "ms": 1.0}) + "\n",
        encoding="utf-8",
    )
    summary = collect.summarise(collect.read(log))
    assert summary["review"]["reviews"] == 0
    assert summary["review"]["retraction_rate"] is None


def test_refused_paths_is_a_count_not_a_list(tmp_path, monkeypatch):
    """The confinement refusing a path is worth counting; the path is not worth keeping."""
    monkeypatch.setenv(telemetry.ENV, str(tmp_path / "calls.jsonl"))
    extracted = telemetry.from_result(
        {
            "refused_paths": ["/somewhere/private.py", "/elsewhere/secret.py"],
            "modules": 3,
        }
    )
    assert extracted["refused_paths"] == 2
    assert extracted["modules"] == 3
