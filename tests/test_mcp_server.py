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
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    NOT_INITIALIZED,
    PROTOCOL_VERSIONS,
    ROOT_ENV,
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


#: The handshake, prepended by `drive`. Every result's shape is negotiated -
#: `structuredContent` and `outputSchema` arrived in protocol 2025-06-18 - so a test
#: that skips `initialize` is testing a shape no real client gets. Several tests in
#: this file were doing exactly that, which is how the server came to answer cold
#: calls in the newest shape whatever the client had declared.
HANDSHAKE = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "tests"},
    },
}


def drive(requests: list[dict], *, handshake: bool = True) -> list[dict]:
    """Feed requests through `serve` over byte streams, as a client would.

    The handshake goes first and its reply is stripped, so indices are unchanged and
    every test exercises the path a client actually takes. `handshake=False` sends
    them cold, which only the tests about the handshake itself want.
    """
    sent = ([HANDSHAKE] if handshake else []) + list(requests)
    stdin = io.BytesIO("\n".join(json.dumps(r) for r in sent).encode("utf-8") + b"\n")
    stdout = io.BytesIO()
    serve(stdin, stdout, ReconServer())
    stdout.seek(0)
    replies = [json.loads(line) for line in stdout.read().decode("utf-8").splitlines() if line]
    return replies[1:] if handshake else replies


def test_initialize_reports_the_protocol_it_was_asked_for():
    (reply,) = drive(
        handshake=False,
        requests=[
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
            }
        ],
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
    """Contents AND the file list, both captured before the tools run.

    The second assertion read

        assert sorted(p.name for p in repo.rglob("*")) == sorted(
            p.name for p in repo.rglob("*")
        )

    - both sides evaluated after the tools, so it compared the directory with itself
    at one instant and could not fail. It was there to catch a file appearing or
    disappearing, and the contents check above it covers only `*.py`, so a tool
    writing a `.json` or deleting a fixture would have passed both.
    """
    listing_before = sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*"))
    before = {p: p.read_bytes() for p in sorted(repo.rglob("*.py"))}

    server.map_repo({"path": str(repo)})
    server.check_modernisation({"path": str(repo)})
    server.review_diff({"path": str(repo / "src" / "demo" / "old.py")})

    after = {p: p.read_bytes() for p in sorted(repo.rglob("*.py"))}
    listing_after = sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*"))
    assert before == after
    assert listing_before == listing_after, (
        f"a file appeared or disappeared: {sorted(set(listing_after) ^ set(listing_before))}"
    )


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


def _one_commit_repo(root: Path, message: str, files: dict[str, str]) -> Path:
    """A real git repository with one commit, because the gate reads `git log`."""
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "Tester"],
        ["add", "-A"],
        ["commit", "-q", "-m", message],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    return root


def test_a_docs_only_release_does_not_report_a_check_it_never_measured(
    server: ReconServer, tmp_path: Path
):
    """It came back `verdict: "pass"` with the test-coupling check reporting PASS.

    The check's denominator was zero - no source changed, so nothing was observed
    about whether source arrives with tests - and an agent reading the summary had
    nothing to tell it the clean verdict rested on one fewer observation than it
    appeared to. Swept over 75 local checkouts at `last=8` through this gate, 12
    earn a clean verdict and 4 of those 12 are this case - measured here rather
    than carried over from the monorepo this was filtered out of, whose checks
    have diverged and which reports 17 and 6 over the same folder.
    """
    repo = _one_commit_repo(
        tmp_path / "docs", "rewrite the install section", {"README.md": "# guide" + chr(10)}
    )
    out = server.release_readiness({"path": str(repo)})

    assert out["verdict"] == "pass", "nothing observed is not a failure"
    # TWO checks have nothing to look at here, not one. The coupling check is the
    # subject of this test; `riskiest commit` joins it because a release of one commit
    # has no rest-of-release to stand out from - it used to compare the commit's score
    # against the median of itself and report 1.0x as a measurement.
    assert out["not_measured"] == ["source changes without tests", "riskiest commit"], out[
        "not_measured"
    ]
    assert out["checks_measured"] == out["checks_total"] - 2
    assert out["evidence"] == "2 of 4 checks measured something"

    coupling = next(c for c in out["checks"] if "without tests" in c["name"])
    assert coupling["status"] == "not measured"
    assert coupling["status"] != "pass"


def test_a_release_that_touches_source_measures_every_check(server: ReconServer, tmp_path: Path):
    repo = _one_commit_repo(
        tmp_path / "src-change",
        "add a parser and its tests",
        {
            "src/pkg/parse.py": "def f():" + chr(10) + "    return 1" + chr(10),
            "tests/test_parse.py": "def test_f():" + chr(10) + "    assert True" + chr(10),
        },
    )
    out = server.release_readiness({"path": str(repo)})

    # One commit, so the standout comparison has no rest-of-release to compare with
    # and reports NOT_MEASURED - see
    # `tests/captain/test_risk.py::test_a_release_of_one_commit_does_not_compare_it_with_itself`.
    # This asserted `not_measured == []`, which passed only while that check was
    # dividing a score by itself.
    assert out["not_measured"] == ["riskiest commit"], out["not_measured"]
    assert out["checks_measured"] == out["checks_total"] - 1

    # What this test is actually about: source changed and arrived with a test, so the
    # coupling check observed something.
    coupling = next(c for c in out["checks"] if "tests" in c["name"])
    assert coupling["status"] != "not measured", coupling
    assert out["evidence"] == "3 of 4 checks measured something"


def test_every_status_the_gate_can_return_is_in_the_declared_schema():
    """`not measured` is a fourth status, and a client validating against the schema
    would reject a response carrying it if the enum still listed three."""
    from reporecon.captain.gate import BLOCK, NOT_MEASURED, PASS, WARN

    tool = next(t for t in TOOLS if t["name"] == "release_readiness")
    check = tool["outputSchema"]["properties"]["checks"]["items"]["properties"]["status"]
    assert set(check["enum"]) == {PASS, WARN, BLOCK, NOT_MEASURED}

    summary = tool["outputSchema"]["properties"]
    assert {"evidence", "checks_measured", "checks_total", "not_measured"} <= set(summary)


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
        # Deliberate: the symlink failing is the signal to try a junction, and the
        # reason it failed does not change what to do next. Found by this repository's
        # own reviewer, which was right to ask.
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


# --- the limits SECURITY.md calls "always on" ------------------------------------------


def test_the_file_limit_applies_to_the_walking_tools_too(tmp_path: Path, monkeypatch):
    """SECURITY.md: "Two further limits are always on ... so one call cannot walk a
    whole disk or be made to read a multi-gigabyte generated module."

    They lived in the MCP server and were applied by two of its six tools.
    `map_repo` and `trace_impact` go through `parse_repo`, which had no count cap and
    no size cap - and `map_repo`'s schema had no way to say it had over-read.
    """
    from reporecon.cartographer import parse as parse_module

    monkeypatch.setattr(parse_module, "FILE_LIMIT", 5)
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    for n in range(12):
        (pkg / f"m{n}.py").write_text(f"X{n} = {n}\n", encoding="utf-8")

    out = ReconServer().map_repo({"path": str(tmp_path)})
    assert out["truncated"] is True
    assert out["modules"] == 5


def test_a_file_too_large_to_parse_is_reported_not_read(tmp_path: Path, monkeypatch):
    from reporecon.cartographer import parse as parse_module

    monkeypatch.setattr(parse_module, "SOURCE_LIMIT", 200)
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "small.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (pkg / "huge.py").write_text("# " + "x" * 500 + "\n", encoding="utf-8")

    out = ReconServer().map_repo({"path": str(tmp_path)})
    errors = dict(out["parse_errors"])
    assert "pkg/huge.py" in errors
    assert "the limit is 200" in errors["pkg/huge.py"]
    assert out["truncated"] is False  # the count cap was not the one that fired


def test_a_blank_confinement_root_refuses_rather_than_runs_unconfined(monkeypatch):
    """The one way to set this variable wrong was the way that turned it off, and the
    startup banner then omitted the line that would have shown it."""
    from reporecon.mcp_server import ConfigError, _root

    for value in ("", "   "):
        monkeypatch.setenv(ROOT_ENV, value)
        with pytest.raises(ConfigError):
            _root()

    monkeypatch.delenv(ROOT_ENV, raising=False)
    assert _root() is None


def test_a_gitfile_pointing_outside_the_root_is_refused(tmp_path: Path, monkeypatch):
    """`.git` is sometimes a file holding `gitdir: <path>` - that is how worktrees and
    submodules are laid out. `is_repository` only asked whether the entry existed, so a
    one-line `.git` file inside the root pointed git at a repository outside it."""
    outside = tmp_path / "secret"
    (outside / ".git").mkdir(parents=True)
    confined = tmp_path / "confined"
    confined.mkdir()
    (confined / "ok.py").write_text("x = 1\n", encoding="utf-8")
    (confined / ".git").write_text(f"gitdir: {outside / '.git'}\n", encoding="utf-8")

    monkeypatch.setenv(ROOT_ENV, str(confined))
    server = ReconServer()
    for tool in ("riskiest_commits", "release_readiness"):
        with pytest.raises(ToolError) as caught:
            getattr(server, tool)({"path": str(confined), "last": 5})
        assert "outside" in str(caught.value), tool


def test_a_file_that_does_not_parse_is_not_reported_as_clean(tmp_path: Path):
    """ "0 proposed, 0 confirmed, 0 retracted" is exactly what a clean file looks like.

    `propose` swallows the SyntaxError and returns [], and review_diff had no channel
    to say so — while its sibling `check_modernisation` reports parse errors and its
    schema explains why: a scan that found nothing in code that does not compile is
    not a clean scan.
    """
    broken = tmp_path / "broken.py"
    broken.write_text("def broken(:\n", encoding="utf-8")
    out = ReconServer().review_diff({"path": str(broken)})
    assert out["proposed"] == 0
    assert "invalid syntax" in out["parse_error"]

    clean = tmp_path / "clean.py"
    clean.write_text("def f():\n    return 1\n", encoding="utf-8")
    assert ReconServer().review_diff({"path": str(clean)})["parse_error"] == ""


def test_a_file_that_is_not_python_is_a_wrong_argument(tmp_path: Path):
    """A one-line markdown file parses as Python, because the line is a comment."""
    readme = tmp_path / "README.md"
    readme.write_text("# Title\n\nSome prose.\n", encoding="utf-8")
    with pytest.raises(ToolError) as caught:
        ReconServer().review_diff({"path": str(readme)})
    assert "not a Python file" in str(caught.value)


def test_a_unique_name_is_not_ambiguous_with_its_longer_neighbour(tmp_path: Path):
    """`q.endswith(f".{symbol}") or symbol in q` — the second clause is a substring
    test, and every short name is a prefix of something."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod.py").write_text(
        "def helper():\n    return 1\n\n\n"
        "def helper_extended():\n    return 2\n\n\n"
        "def caller():\n    helper()\n",
        encoding="utf-8",
    )
    server = ReconServer()
    out = server.trace_impact({"path": str(tmp_path), "symbol": "helper"})
    assert out["resolved"] == "pkg.mod.helper"
    assert out["candidates"] == []
    assert (
        server.trace_impact({"path": str(tmp_path), "symbol": "helper_extended"})["resolved"]
        == "pkg.mod.helper_extended"
    )


# --- the protocol, where a caller's mistake must look like a caller's mistake ----------


def _session(messages: list[dict]) -> list[dict]:
    """Drive the server over a pipe and return its replies."""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    env.pop(ROOT_ENV, None)
    lines = [json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})]
    lines += [json.dumps(m) for m in messages]
    done = subprocess.run(
        [sys.executable, "-m", "reporecon.mcp_server"],
        input="\n".join(lines) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,
    )
    return [json.loads(line) for line in done.stdout.splitlines() if line.strip()]


@pytest.mark.parametrize("params", [[1, 2], "x", 7])
def test_params_that_are_not_an_object_are_the_callers_mistake(params):
    """`.get` on a list is an AttributeError, which came back as -32603 with the Python
    exception type in the message: an internal error reported for a caller's mistake,
    and the implementation leaking onto the wire."""
    replies = _session([{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}])
    assert replies[-1]["error"]["code"] == INVALID_PARAMS
    assert "must be an object" in replies[-1]["error"]["message"]


def test_an_explicit_null_id_is_answered_not_ignored():
    """It was treated as a notification, so a client that uses null ids waited for a
    reply that never came. MCP forbids the null id; it is a malformed request."""
    replies = _session([{"jsonrpc": "2.0", "id": None, "method": "ping"}])
    assert replies[-1]["error"]["code"] == INVALID_REQUEST

    # ...and a real notification, with no id at all, still gets nothing.
    assert len(_session([{"jsonrpc": "2.0", "method": "notifications/initialized"}])) == 1


@pytest.mark.parametrize(
    "message", [{"jsonrpc": "2.0", "id": 1}, {"jsonrpc": "2.0", "id": 1, "method": 7}]
)
def test_a_request_with_no_usable_method_is_malformed_not_unknown(message):
    """-32601 "unknown method: None" is a different claim from "this request has no
    method", and only the second one is true."""
    replies = _session([message])
    assert replies[-1]["error"]["code"] == INVALID_REQUEST


def test_an_argument_no_tool_takes_is_refused():
    """Every input schema says additionalProperties: false and nothing enforced it, so
    `{"path": ..., "tpo": 5}` ran happily with top=12 — a typo silently got the default
    and the caller was told it had asked for something it had not."""
    replies = _session(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "map_repo",
                    "arguments": {"path": str(Path(__file__).resolve().parents[1]), "tpo": 5},
                },
            }
        ]
    )
    error = replies[-1]["error"]
    assert error["code"] == INVALID_PARAMS
    assert "'tpo'" in error["message"]
    assert "it takes path, top" in error["message"]


def test_every_pair_on_the_wire_is_a_list_not_a_tuple(tmp_path: Path):
    """`asdict` keeps a tuple a tuple, and the schema says array.

    JSON turns it into a list eventually, which is why this survived — but a client
    reading the in-process result, and any validator run against the payload, sees a
    tuple. Found by a real JSON Schema validator over the same payloads the hand-rolled
    checker walks; `parse_errors` was the last one.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "ok.py").write_text("def f(xs):\n    return len(xs)\n", encoding="utf-8")
    (pkg / "broken.py").write_text("def nope(:\n", encoding="utf-8")

    out = ReconServer().map_repo({"path": str(tmp_path)})
    for key in ("parse_errors", "unresolved_sample", "out_of_scope_sample"):
        for item in out[key]:
            assert isinstance(item, list), (key, type(item).__name__)
    assert any("broken.py" in pair[0] for pair in out["parse_errors"])


# --- the schemas, under a real validator ------------------------------------------------
#
# `_check_against_schema` above is enough JSON Schema to prove the declared shape is the
# shape returned, written out because the package has no dependencies. It cannot check
# the schemas themselves, and it is an approximation of a specification - so a real
# validator is run beside it, as a development dependency rather than a runtime one.
# It earned its place immediately: it found `parse_errors` arriving as tuples where the
# schema says array, which the hand-rolled walker had no reason to notice.

jsonschema = pytest.importorskip("jsonschema", reason="dev dependency; see [dependency-groups]")


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t["name"])
@pytest.mark.parametrize("which", ["inputSchema", "outputSchema"])
def test_the_declared_schemas_are_valid_json_schema(tool, which):
    """A schema that is not valid JSON Schema is a schema no client can use."""
    schema = tool.get(which)
    assert schema is not None, f"{tool['name']} declares no {which}"
    jsonschema.Draft202012Validator.check_schema(schema)


def test_every_output_validates_under_a_real_validator(tmp_path: Path):
    """Success, refusal and parse-error paths, against the schema each tool advertises."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "m.py").write_text(
        "from typing import List\n\n\ndef f(xs: List[int]):\n    return len(xs)\n",
        encoding="utf-8",
    )
    (pkg / "broken.py").write_text("def nope(:\n", encoding="utf-8")

    here = str(Path(__file__).resolve().parents[1])
    cases = [
        ("map_repo", {"path": here}),
        ("map_repo", {"path": str(tmp_path)}),
        ("trace_impact", {"path": here, "symbol": "build_graph"}),
        ("trace_impact", {"path": here, "symbol": "no_such_symbol_anywhere"}),
        ("check_modernisation", {"path": str(tmp_path)}),
        ("review_diff", {"path": str(pkg / "m.py")}),
        ("review_diff", {"path": str(pkg / "broken.py")}),
        ("release_readiness", {"path": here, "last": 20}),
        ("riskiest_commits", {"path": here, "last": 20, "limit": 3}),
    ]
    schemas = {tool["name"]: tool["outputSchema"] for tool in TOOLS}
    server = ReconServer()
    problems = []
    for name, arguments in cases:
        payload = getattr(server, name)(arguments)
        for error in jsonschema.Draft202012Validator(schemas[name]).iter_errors(payload):
            where = ".".join(str(p) for p in error.path) or "<root>"
            problems.append(f"{name}.{where}: {error.message}")
    assert problems == []


def _tree_fingerprint(root: Path) -> dict[str, tuple[int, int]]:
    """Every file under `root` by size and mtime_ns, ignoring caches."""
    out: dict[str, tuple[int, int]] = {}
    for found in sorted(root.rglob("*")):
        if not found.is_file() or "__pycache__" in found.parts:
            continue
        stat = found.stat()
        out[str(found.relative_to(root))] = (stat.st_size, stat.st_mtime_ns)
    return out


def test_no_tool_writes_anything_under_the_repository_it_is_given(tmp_path: Path):
    """The headline property, proved rather than declared.

    `readOnlyHint: True` on all six tools is an annotation: it tells a client what to
    expect and enforces nothing. The module docstring says "Nothing is written and
    nothing is executed", and the only thing asserting it was the annotation saying so.

    This fingerprints every file under a real repository by size and mtime, calls all
    six tools, and compares. A tool that wrote a cache, a lock or an output file beside
    the source it read would show up here and nowhere else.
    """
    repo = _one_commit_repo(
        tmp_path / "subject",
        "add a module and its test",
        {
            "src/pkg/__init__.py": "",
            "src/pkg/core.py": (
                "from typing import List"
                + chr(10)
                + chr(10)
                + "def run(xs: List[int]) -> int:"
                + chr(10)
                + "    assert xs"
                + chr(10)
                + "    return sum(xs)"
                + chr(10)
            ),
            "tests/test_core.py": (
                "from pkg.core import run"
                + chr(10)
                + chr(10)
                + "def test_run():"
                + chr(10)
                + "    assert run([1]) == 1"
                + chr(10)
            ),
            "pyproject.toml": "[project]" + chr(10) + 'name = "subject"' + chr(10),
        },
    )
    # `review_diff` reads a Python file and, optionally, the diff that changed it -
    # it refuses a non-Python path rather than reporting "0 proposed" on a README.
    unified = (
        "--- a/src/pkg/core.py\n"
        "+++ b/src/pkg/core.py\n"
        "@@ -1,3 +1,4 @@\n"
        " def run(xs):\n"
        "+    assert xs\n"
        "     return sum(xs)\n"
    )

    before = _tree_fingerprint(repo)
    assert before, "the fixture repository is empty, so this test would prove nothing"

    server = ReconServer()
    calls = [
        ("map_repo", {"path": str(repo)}),
        ("trace_impact", {"path": str(repo), "symbol": "pkg.core.run"}),
        ("check_modernisation", {"path": str(repo)}),
        ("review_diff", {"path": str(repo / "src" / "pkg" / "core.py"), "diff": unified}),
        ("release_readiness", {"path": str(repo)}),
        ("riskiest_commits", {"path": str(repo)}),
    ]
    assert len(calls) == len(TOOLS), "every tool must be exercised, not most of them"
    for name, arguments in calls:
        getattr(server, name)(arguments)

    after = _tree_fingerprint(repo)
    assert after == before, {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "changed": sorted(k for k in before.keys() & after.keys() if before[k] != after[k]),
    }


def test_the_tools_exercised_above_are_the_tools_that_exist():
    """So adding a seventh tool fails this file rather than slipping past it."""
    assert {tool["name"] for tool in TOOLS} == {
        "map_repo",
        "trace_impact",
        "check_modernisation",
        "review_diff",
        "release_readiness",
        "riskiest_commits",
    }


# -- messages that describe the input they got --------------------------------


def test_the_three_bad_path_cases_say_three_different_things():
    """One message covered all of them: "'path' must be a non-empty string".

    So `{"path": 123}` was told the string it sent was empty, and `{"path": ""}` was
    told nothing about what belongs there. The caller is a model; a message that
    misdescribes the argument is a message it cannot act on, and it calls again with
    the same argument.
    """

    def result(arguments):
        (reply,) = drive(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "map_repo", "arguments": arguments},
                }
            ]
        )
        return reply["result"]

    absent = result({})
    assert absent["isError"] is True
    assert absent["content"][0]["text"] == (
        "'path' is required: the path to a repository on this machine"
    )

    wrong_type = result({"path": 123})
    assert wrong_type["isError"] is True
    assert wrong_type["content"][0]["text"] == "'path' must be a string, got int"

    for blank in ("", "   "):
        empty = result({"path": blank})
        assert empty["isError"] is True
        assert empty["content"][0]["text"] == "'path' was given but is empty; it needs a path"


def test_an_unknown_tool_name_comes_back_with_the_names_that_exist():
    """A model that just guessed a name needs the list, not a restatement of its guess.

    The same reasoning as the `additionalProperties` message, which already names the
    arguments a tool takes.
    """
    (reply,) = drive(
        [
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {"name": "map_repos", "arguments": {}},
            }
        ]
    )
    error = reply["error"]
    assert error["code"] == -32602
    assert "unknown tool: 'map_repos'" in error["message"]
    for name in ("map_repo", "trace_impact", "review_diff", "riskiest_commits"):
        assert name in error["message"], name


# -- the lifecycle ------------------------------------------------------------------


def test_a_tool_call_before_initialize_is_refused():
    """It used to be answered, in a shape the client had not agreed to.

    `protocol` started at the NEWEST version, so a cold `tools/call` came back with
    `structuredContent` - a field a 2024 client has no place for - and `tools/list`
    advertised `outputSchema` the same way. MCP puts the MUST on the client and only a
    SHOULD on the server, so leniency looked harmless; it stopped looking harmless
    once the shapes were compared, because the honest outcomes are identical or
    refused and silently different is neither.
    """
    replies = drive(
        handshake=False,
        requests=[
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "map_repo",
                    "arguments": {"path": str(Path(__file__).resolve().parents[1])},
                },
            }
        ],
    )
    assert len(replies) == 1, replies
    error = replies[0]["error"]
    assert error["code"] == NOT_INITIALIZED, error
    assert "before initialize" in error["message"]
    assert "initialize first" in error["message"]


def test_tools_list_before_initialize_is_refused_too():
    """`outputSchema` is withheld from an older client, so an early listing advertises
    a contract the server has not agreed to honour."""
    replies = drive(handshake=False, requests=[{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
    assert replies[0]["error"]["code"] == NOT_INITIALIZED, replies


def test_ping_is_answerable_cold():
    """It has no response shape to negotiate, and a gate that refused it would refuse
    a client's liveness check."""
    replies = drive(handshake=False, requests=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    assert "result" in replies[0], replies


def test_the_answer_is_the_same_before_and_after_the_handshake():
    """What the refusal protects, as the comparison that justifies it: with the gate
    in place the cold call is an error rather than a different answer."""
    request = {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "tools/call",
        "params": {
            "name": "map_repo",
            "arguments": {"path": str(Path(__file__).resolve().parents[1])},
        },
    }
    cold = drive(handshake=False, requests=[request])[0]
    assert "error" in cold, cold

    warm = drive([request])[0]
    assert "result" in warm, warm
    assert "structuredContent" in warm["result"], sorted(warm["result"])


# -- which repository was actually read ---------------------------------------------


def _redirect_repo(where: Path, gitdir: Path) -> Path:
    """A directory holding one module and a `.git` FILE pointing elsewhere.

    This is the shape of a git worktree or a submodule, so it is not exotic - and git
    follows the redirect, which means the history read is the history of `gitdir`.
    """
    where.mkdir(parents=True)
    (where / "m.py").write_text("def f():" + chr(10) + "    return 1" + chr(10), encoding="utf-8")
    (where / ".git").write_text(f"gitdir: {gitdir.as_posix()}" + chr(10), encoding="utf-8")
    return where


def test_a_git_file_redirect_is_named_rather_than_hidden(server: ReconServer, tmp_path: Path):
    """Both tools reported only the name or the path they were GIVEN.

    So a directory containing one module and a `.git` file pointing at this repository
    came back with thirty of this repository's commits, labelled with that directory's
    name - another repository's history under the wrong label, with nothing in the
    answer able to say so.

    Not a confinement escape: unconfined, a caller could pass the real path directly.
    A mislabelling, and the fix is to report which git directory was read instead of
    assuming it was the obvious one.
    """
    root = Path(__file__).resolve().parents[1]
    redirect = _redirect_repo(tmp_path / "redirect", root / ".git")

    readiness = server.release_readiness({"path": str(redirect)})
    assert readiness["repo"] == "redirect"
    assert Path(readiness["git_dir"]).resolve() == (root / ".git").resolve(), readiness["git_dir"]
    # The commits really are this repository's, which is why the label mattered.
    assert readiness["commits_considered"] > 20, readiness["commits_considered"]

    ranked = server.riskiest_commits({"path": str(redirect), "limit": 1})
    assert ranked["repo"] == "redirect"
    assert Path(ranked["git_dir"]).resolve() == (root / ".git").resolve(), ranked["git_dir"]


def test_an_ordinary_repository_reports_its_own_git_dir(server: ReconServer):
    """The common case, so the field is not only meaningful in the odd one."""
    root = Path(__file__).resolve().parents[1]
    out = server.release_readiness({"path": str(root)})
    assert Path(out["git_dir"]).resolve() == (root / ".git").resolve(), out["git_dir"]
    assert out["repo"] == root.name


def test_both_history_tools_name_the_repository_the_same_way(server: ReconServer):
    """`repo` was a directory NAME in one tool and an absolute PATH in the other,
    both declared as a bare string with no description, so a client joining the two
    results on it matched nothing."""
    root = Path(__file__).resolve().parents[1]
    readiness = server.release_readiness({"path": str(root)})
    ranked = server.riskiest_commits({"path": str(root)})
    assert readiness["repo"] == ranked["repo"] == root.name
    assert readiness["git_dir"] == ranked["git_dir"]
