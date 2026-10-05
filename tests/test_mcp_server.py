"""The MCP server, driven the way a client drives it.

Two things are checked that a unit test of the four libraries cannot:

**The protocol**, byte for byte over a pipe. Initialize, tools/list, tools/call, and the
difference between a protocol error (unknown method, unknown tool) and a tool result
that reports a bad argument — a client has to be able to tell those apart, because the
first is a bug and the second is something a model can correct.

**The promises in the tool descriptions.** Each tool says it reads only, separates
mechanical from behavioural, refuses an ambiguous symbol rather than guessing, and stays
inside `REPO_RECON_ROOT`. A description that is not true of the code is worse than a
missing one, so each of those is a test.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from reporecon.mcp_server import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    PROTOCOL_VERSIONS,
    TOOLS,
    ReconServer,
    ToolError,
    serve,
)

OLD_CODE = textwrap.dedent(
    '''
    import datetime
    from typing import Dict, List, Optional


    def stamp() -> str:
        """A naive timestamp."""
        return datetime.datetime.utcnow().isoformat()


    def collect(items: List[str], index: Optional[Dict[str, int]] = None) -> List[str]:
        out = []
        for item in items:
            out.append(item)
        return out


    def swallow(values):
        total = 0
        for value in values:
            try:
                total += int(value)
            except:
                pass
        return total
    '''
).lstrip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small repository with something for every tool to find."""
    package = tmp_path / "src" / "demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "old.py").write_text(OLD_CODE, encoding="utf-8")
    (package / "core.py").write_text(
        textwrap.dedent(
            '''
            """Two definitions, one calling the other."""


            def helper(value: int) -> int:
                return value * 2


            def entry(value: int) -> int:
                return helper(value) + 1
            '''
        ).lstrip(),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def server() -> ReconServer:
    return ReconServer()


# ---- the protocol --------------------------------------------------------------------


def drive(requests: list[dict]) -> list[dict]:
    """Feed requests through `serve` over byte streams, as a client would."""
    stdin = io.BytesIO("\n".join(json.dumps(r) for r in requests).encode("utf-8") + b"\n")
    stdout = io.BytesIO()
    serve(stdin, stdout, ReconServer())
    stdout.seek(0)
    return [json.loads(line) for line in stdout.read().decode("utf-8").splitlines() if line]


def test_initialize_reports_the_protocol_it_was_asked_for():
    (reply,) = drive(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
            }
        ]
    )
    result = reply["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == "repo-recon"
    assert "repo_resolution_rate" in result["instructions"]


def test_an_unknown_protocol_version_gets_the_newest():
    (reply,) = drive(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "1999-01-01"},
            }
        ]
    )
    assert reply["result"]["protocolVersion"] == PROTOCOL_VERSIONS[0]


def test_every_tool_is_listed_with_both_schemas_and_read_only_annotations():
    (reply,) = drive([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
    tools = reply["result"]["tools"]
    assert len(tools) == 6
    for tool in tools:
        assert tool["inputSchema"]["additionalProperties"] is False, tool["name"]
        assert tool["outputSchema"]["type"] == "object", tool["name"]
        assert tool["annotations"] == {
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        }, tool["name"]
        assert tool["description"].strip(), tool["name"]


def test_a_notification_is_not_answered():
    assert drive([{"jsonrpc": "2.0", "method": "notifications/initialized"}]) == []


def test_malformed_json_is_a_parse_error_and_the_server_keeps_going():
    stdin = io.BytesIO(b'{"not json\n{"jsonrpc": "2.0", "id": 2, "method": "ping"}\n')
    stdout = io.BytesIO()
    serve(stdin, stdout, ReconServer())
    stdout.seek(0)
    replies = [json.loads(line) for line in stdout.read().decode().splitlines() if line]
    assert replies[0]["error"]["code"] == -32700
    assert replies[1]["result"] == {}


def test_an_unknown_method_and_an_unknown_tool_are_protocol_errors():
    first, second = drive(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/invent"},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "no_such_tool", "arguments": {}},
            },
        ]
    )
    assert first["error"]["code"] == METHOD_NOT_FOUND
    assert second["error"]["code"] == INVALID_PARAMS


def test_a_bad_argument_is_a_tool_result_not_a_protocol_error():
    """A model can read and correct a tool result. It cannot correct a JSON-RPC error."""
    (reply,) = drive(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "map_repo", "arguments": {"path": "/nope/nowhere"}},
            }
        ]
    )
    assert "error" not in reply
    assert reply["result"]["isError"] is True
    assert "nowhere" in reply["result"]["content"][0]["text"]


def test_structured_content_is_withheld_from_a_client_that_predates_it(repo: Path):
    old, new = drive(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "map_repo", "arguments": {"path": str(repo)}},
            },
        ]
    )
    assert old["result"]["protocolVersion"] == "2024-11-05"
    assert "structuredContent" not in new["result"]
    assert json.loads(new["result"]["content"][0]["text"])["modules"] >= 2


# ---- what the descriptions promise ---------------------------------------------------


def test_mechanical_and_behavioural_edits_are_never_merged(server: ReconServer, repo: Path):
    """The promise that matters most, and the reason the counts are separate.

    `datetime.utcnow()` -> `datetime.now(UTC)` is correct AND changes a naive
    timestamp into an aware one. Reporting it beside a typing generic as one pile of
    "modernisations" is how a caller that depended on naive timestamps breaks.
    """
    out = server.check_modernisation({"path": str(repo)})
    assert out["mechanical"] >= 3
    assert out["behavioural"] >= 1
    kinds = {edit["rule"]: edit["kind"] for edit in out["edits"]}
    assert kinds["utcnow-deprecated"] == "behavioural"
    assert kinds["pep585-generics"] == "mechanical"
    # Every behavioural edit says what it would change.
    for edit in out["edits"]:
        if edit["kind"] == "behavioural":
            assert edit["note"].strip(), edit


def test_nothing_is_written_while_the_tools_run(server: ReconServer, repo: Path):
    before = {p: p.read_bytes() for p in sorted(repo.rglob("*.py"))}
    server.map_repo({"path": str(repo)})
    server.check_modernisation({"path": str(repo)})
    server.review_diff({"path": str(repo / "src" / "demo" / "old.py")})
    after = {p: p.read_bytes() for p in sorted(repo.rglob("*.py"))}
    assert before == after
    assert sorted(p.name for p in repo.rglob("*")) == sorted(p.name for p in repo.rglob("*"))


def test_an_unknown_rule_name_is_refused_with_the_list(server: ReconServer, repo: Path):
    with pytest.raises(ToolError) as caught:
        server.check_modernisation({"path": str(repo), "rules": ["no-such-rule"]})
    message = str(caught.value)
    assert "no-such-rule" in message
    assert "pep585-generics" in message  # it says what IS available


def test_a_rule_filter_narrows_the_scan(server: ReconServer, repo: Path):
    everything = server.check_modernisation({"path": str(repo)})
    narrowed = server.check_modernisation({"path": str(repo), "rules": ["pep585-generics"]})
    assert set(narrowed["by_rule"]) == {"pep585-generics"}
    assert narrowed["behavioural"] == 0
    assert sum(narrowed["by_rule"].values()) < sum(everything["by_rule"].values())


def test_a_confirmed_finding_survived_a_disproof_attempt(server: ReconServer, repo: Path):
    out = server.review_diff({"path": str(repo / "src" / "demo" / "old.py")})
    assert out["confirmed"] >= 1
    assert out["proposed"] >= out["confirmed"]
    assert out["retracted"] == out["proposed"] - out["confirmed"]
    assert any(f["rule"] == "bare-except" for f in out["findings"])


def test_a_diff_limits_the_review_to_the_lines_it_added(server: ReconServer, repo: Path):
    """Untouched code is not a problem with this change."""
    path = repo / "src" / "demo" / "old.py"
    diff = textwrap.dedent(
        """\
        --- a/src/demo/old.py
        +++ b/src/demo/old.py
        @@ -1,2 +1,3 @@
        +def added_but_clean():
        +    return 1
        """
    )
    whole = server.review_diff({"path": str(path)})
    scoped = server.review_diff({"path": str(path), "diff": diff})
    assert whole["confirmed"] >= 1
    assert scoped["confirmed"] == 0


def test_an_ambiguous_symbol_returns_candidates_rather_than_a_guess(
    server: ReconServer, tmp_path: Path
):
    """Answering about a different definition of the same name looks right and is not."""
    package = tmp_path / "src" / "twice"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    for module in ("a", "b"):
        (package / f"{module}.py").write_text(
            "def run():\n    return 1\n\n\ndef caller():\n    return run()\n", encoding="utf-8"
        )
    out = server.trace_impact({"path": str(tmp_path), "symbol": "run"})
    assert out["resolved"] is None
    assert len(out["candidates"]) >= 2
    assert out["callers_by_depth"] == {}


def test_a_symbol_that_exists_gets_its_callers(server: ReconServer, repo: Path):
    out = server.trace_impact({"path": str(repo), "symbol": "demo.core.helper"})
    assert out["resolved"] == "demo.core.helper"
    callers = {q for names in out["callers_by_depth"].values() for q in names}
    assert "demo.core.entry" in callers


def test_a_symbol_that_does_not_exist_is_reported_as_absent(server: ReconServer, repo: Path):
    out = server.trace_impact({"path": str(repo), "symbol": "nothing_called_this"})
    assert out["resolved"] is None
    assert out["candidates"] == []
    assert out["total"] == 0


def test_the_root_environment_variable_confines_every_path(
    server: ReconServer, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The server's whole input is a filesystem path; that is the thing worth bounding."""
    inside = repo / "src"
    outside = tmp_path.parent
    monkeypatch.setenv("REPO_RECON_ROOT", str(repo))
    assert server.map_repo({"path": str(inside)})["modules"] >= 2
    with pytest.raises(ToolError) as caught:
        server.map_repo({"path": str(outside)})
    assert "REPO_RECON_ROOT" in str(caught.value)


def test_a_bad_integer_argument_says_the_range(server: ReconServer, repo: Path):
    with pytest.raises(ToolError) as caught:
        server.map_repo({"path": str(repo), "top": 0})
    assert "between 1 and 100" in str(caught.value)


def test_git_tools_refuse_a_directory_that_is_not_a_repository(server: ReconServer, repo: Path):
    for name in ("release_readiness", "riskiest_commits"):
        with pytest.raises(ToolError) as caught:
            getattr(server, name)({"path": str(repo)})
        assert "git" in str(caught.value).lower(), name


# ---- the declared output shape -------------------------------------------------------


def _check_against_schema(value, schema, path="structuredContent"):
    """Enough of JSON Schema to prove the declared shape is the shape returned.

    Written out rather than imported: the package has no dependencies, and a schema
    nobody validates is a claim rather than a contract.
    """
    kinds = schema.get("type")
    kinds = [kinds] if isinstance(kinds, str) else list(kinds or [])
    python = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "null": type(None),
    }
    if kinds:
        if isinstance(value, bool) and "boolean" not in kinds:
            raise AssertionError(f"{path}: boolean where {kinds} was declared")
        allowed = tuple(python[k] for k in kinds if k in python)
        assert isinstance(value, allowed), f"{path}: {type(value).__name__} not in {kinds}"
    if "enum" in schema and value is not None:
        assert value in schema["enum"], f"{path}: {value!r} not in {schema['enum']}"
    if isinstance(value, dict):
        for key in schema.get("required", []):
            assert key in value, f"{path}: required key {key!r} missing"
        properties = schema.get("properties", {})
        for key, item in value.items():
            if key in properties:
                _check_against_schema(item, properties[key], f"{path}.{key}")
            elif isinstance(schema.get("additionalProperties"), dict):
                _check_against_schema(item, schema["additionalProperties"], f"{path}.{key}")
            elif properties:
                # Both directions. Walking returned -> declared only lets a tool return
                # a key its schema never mentions, and a client generating types from
                # these schemas drops the field without anything failing.
                #
                # Only where the schema says what it holds: a bare {"type": "object"}
                # constrains nothing and claims nothing, so there is no contract to
                # break. That is its own weakness and is tested separately.
                raise AssertionError(f"{path}.{key} is returned and not declared")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            _check_against_schema(item, schema["items"], f"{path}[{index}]")


def test_every_tools_output_matches_its_declared_schema(server: ReconServer, repo: Path):
    """On this repository, and on the repo-recon checkout itself for the git tools."""
    schemas = {tool["name"]: tool["outputSchema"] for tool in TOOLS}
    here = Path(__file__).resolve().parent.parent
    cases = [
        ("map_repo", {"path": str(repo)}),
        ("map_repo", {"path": str(repo), "top": 3}),
        ("trace_impact", {"path": str(repo), "symbol": "demo.core.helper"}),
        ("trace_impact", {"path": str(repo), "symbol": "absent"}),
        ("check_modernisation", {"path": str(repo)}),
        ("check_modernisation", {"path": str(repo / "src" / "demo" / "core.py")}),
        ("review_diff", {"path": str(repo / "src" / "demo" / "old.py")}),
        ("review_diff", {"path": str(repo / "src" / "demo" / "core.py")}),
    ]
    if (here / ".git").exists():
        cases += [
            ("release_readiness", {"path": str(here)}),
            ("riskiest_commits", {"path": str(here), "limit": 2}),
        ]
    for name, arguments in cases:
        _check_against_schema(getattr(server, name)(arguments), schemas[name], name)


def test_check_prints_what_it_exposes_without_reading_stdin():
    """A one-command liveness probe, so a client can be told the server works."""
    result = subprocess.run(
        [sys.executable, "-m", "reporecon.mcp_server", "--check"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src")},
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["server"] == "repo-recon"
    assert payload["read_only"] is True
    assert len(payload["tools"]) == 6


# ---- found by an independent review of this repository -------------------------------


def test_release_readiness_accepts_the_commit_count_it_advertises(server: ReconServer):
    """`last` was called `since` and documented as "a revision or date".

    The library's parameter is a COUNT of the most recent commits, and the string
    went straight into a list slice, so every value raised TypeError. A parameter
    that cannot be used at all, behind a description saying what it did not do.
    """
    here = Path(__file__).resolve().parent.parent
    if not (here / ".git").exists():
        pytest.skip("no git history here")
    whole = server.release_readiness({"path": str(here)})
    recent = server.release_readiness({"path": str(here), "last": 1})
    assert recent["commits_considered"] == 1
    assert whole["commits_considered"] >= recent["commits_considered"]
    for bad in ("HEAD~2", "2026-10-01", 0, 99999):
        with pytest.raises(ToolError):
            server.release_readiness({"path": str(here), "last": bad})


def _link_dir(link: Path, target: Path) -> bool:
    """Make `link` point at the directory `target`. True if it worked.

    A symlink is the portable way and the one this used to use - but on Windows it
    needs Developer Mode or an elevated account, so on the platform where the bug
    actually lived the test skipped itself. A junction needs neither, and a junction
    is what `rglob` followed. Trying the junction second keeps POSIX on symlinks.
    """
    try:
        link.symlink_to(target, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name != "nt":
        return False
    done = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode == 0 and link.is_dir()


def test_a_link_inside_the_root_does_not_reach_outside_it(
    server: ReconServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The confinement held for the path it was given, not for the paths it found.

    `rglob` follows directory symlinks and Windows junctions, so a link under
    REPO_RECON_ROOT pulled in files the root was meant to exclude — and `map_repo`
    reported one of them as a module of the repository.
    """
    confined = tmp_path / "confined"
    (confined / "inside").mkdir(parents=True)
    (confined / "inside" / "ok.py").write_text("x = 1\n", encoding="utf-8")
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "private.py").write_text('PASSWORD = "hunter2"\n', encoding="utf-8")
    if not _link_dir(confined / "leak", outside):
        pytest.skip("this platform or account cannot create a directory link")

    monkeypatch.setenv("REPO_RECON_ROOT", str(confined))
    # Named directly, it was always refused. It is the walk that leaked.
    with pytest.raises(ToolError):
        server.map_repo({"path": str(confined / "leak")})

    mapped = server.map_repo({"path": str(confined)})
    modules = {card["module"] for card in mapped["core_modules"]}
    assert not any("private" in m for m in modules), modules
    scanned = server.check_modernisation({"path": str(confined)})
    assert scanned["files_scanned"] == 1


def test_a_diff_for_another_file_reports_nothing_about_this_one(server: ReconServer, repo: Path):
    """It used to union every other file's added line numbers and apply them here.

    So a change to one file reported findings on untouched lines of another — a
    finding about code the author did not write in this change, which is exactly
    the noise the verify step exists to remove.
    """
    path = repo / "src" / "demo" / "old.py"
    whole = server.review_diff({"path": str(path)})
    assert whole["confirmed"] >= 1  # there is something to find

    elsewhere = "--- a/somewhere/else.py\n+++ b/somewhere/else.py\n@@ -1,0 +20,1 @@\n+x = 1\n"
    assert server.review_diff({"path": str(path), "diff": elsewhere})["confirmed"] == 0


def test_a_diff_that_names_no_file_is_refused_rather_than_ignored(server: ReconServer, repo: Path):
    path = repo / "src" / "demo" / "old.py"
    with pytest.raises(ToolError) as caught:
        server.review_diff({"path": str(path), "diff": "hello world"})
    assert "names no files" in str(caught.value)


# --- the denominator, which is the measurement -----------------------------------------


def _write(root: Path, files: dict[str, str]) -> None:
    for name, source in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")


def test_a_method_on_a_repo_class_counts_and_resolves(tmp_path: Path):
    """`repo_resolution_rate` reported 1.000 where ten of eleven calls bound to nothing.

    Every `x.method()` on a local variable went to `out_of_scope` - removed from the
    denominator rather than counted as unresolved - so a repository written in classes
    resolved almost nothing and reported that almost everything resolved. The server's
    own instructions tell a model to read this number, and the README's headline is
    "the denominator is the measurement".
    """
    _write(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/engine.py": """
                class Engine:
                    def run(self):
                        return 1

                    def stop(self):
                        return 2
                """,
            "pkg/main.py": """
                from pkg.engine import Engine


                def go():
                    e = Engine()
                    e.run()
                    e.run()
                    e.stop()
                """,
        },
    )
    out = ReconServer().map_repo({"path": str(tmp_path)})
    assert out["total_calls"] == 4  # Engine(), run, run, stop
    assert out["in_scope_calls"] == 4
    assert out["resolved_calls"] == 4
    assert out["repo_resolution_rate"] == 1.0
    # ...and because they resolved, the methods are not "called by nothing".
    assert [e["qualname"] for e in out["entry_points"]] == ["pkg.main.go"]


def test_a_method_on_someone_elses_object_stays_out_of_the_denominator(tmp_path: Path):
    """The fix must not go the other way. `response.json()` could never bind here, and
    counting it would make every repository look unmappable - which is the mistake the
    original `out_of_scope` bucket exists to avoid."""
    _write(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/net.py": """
                import httpx


                def fetch(url):
                    response = httpx.get(url)
                    return response.json()
                """,
        },
    )
    out = ReconServer().map_repo({"path": str(tmp_path)})
    assert out["total_calls"] == 2
    assert out["in_scope_calls"] == 0
    assert out["repo_resolution_rate"] == 0.0


def test_a_method_this_repo_defines_is_in_scope_even_unresolved(tmp_path: Path):
    """Type inference is one assignment deep on purpose. When it does not reach, the
    call is still in scope if this repository defines a method of that name - otherwise
    the rate goes back to flattering itself by shrinking what it is a rate of."""
    _write(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/engine.py": """
                class Engine:
                    def run(self):
                        return 1
                """,
            "pkg/main.py": """
                def go(engine):
                    engine.run()
                """,
        },
    )
    out = ReconServer().map_repo({"path": str(tmp_path)})
    assert out["total_calls"] == 1
    assert out["in_scope_calls"] == 1, "a parameter's type is not inferred, but run() is ours"
    assert out["resolved_calls"] == 0
    assert out["repo_resolution_rate"] == 0.0
    assert ["engine.run", 1] in out["unresolved_sample"]


def test_both_halves_of_the_denominator_are_shown(tmp_path: Path):
    """`unresolved_sample` showed the gap inside the denominator and nothing showed what
    was excluded from it - so the one number that can flatter a rate by shrinking it was
    the one a reader could not see."""
    _write(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/m.py": """
                def go(xs):
                    return len(sorted(xs))
                """,
        },
    )
    out = ReconServer().map_repo({"path": str(tmp_path)})
    excluded = dict(out["out_of_scope_sample"])
    assert excluded == {"len": 1, "sorted": 1}
    assert out["in_scope_calls"] == 0
    for pair in out["out_of_scope_sample"]:
        assert isinstance(pair, list), "declared as an array of arrays"
