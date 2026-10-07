"""What a client is told depends on the version it negotiated, in both directions.

`serve`'s own comment says the pre-handshake defect was that a `tools/call` arriving
cold was answered in full "and a `tools/list` advertised `outputSchema` the same way".
The fix covered the cold path. On the negotiated path `tools/list` still returned every
tool whole, so a session agreed at 2024-11-05 was handed `outputSchema`, `title` and
`annotations` - fields that arrived in 2025-06-18 - and then `tools/call` correctly
withheld `structuredContent`, because `_STRUCTURED` does consult the version.

A client that reads `outputSchema` and never receives a structured result has been
promised something the server then withholds. That is the asymmetry, and it is here on
purpose: the listing and the result have to describe the same contract.

The second half is the handshake itself. A session is initialised once; a second
`initialize` at a different version used to replace it, which changes the shape of every
later result in that session.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from reporecon.mcp_server import PROTOCOL_VERSIONS, ReconServer, serve

#: Fields `tools/list` may only show to a client that negotiated 2025-06-18 or later.
LATER_FIELDS = ("outputSchema", "title", "annotations")


def session(messages: list[dict]) -> list[dict]:
    stdin = io.BytesIO(("\n".join(json.dumps(m) for m in messages) + "\n").encode("utf-8"))
    stdout = io.BytesIO()
    serve(stdin, stdout, ReconServer())
    stdout.seek(0)
    return [json.loads(line) for line in stdout.read().decode("utf-8").splitlines() if line]


def _initialize(version: str, request_id: int = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {"protocolVersion": version},
    }


@pytest.mark.parametrize("version", ["2024-11-05", "2025-03-26"])
def test_a_client_that_predates_a_field_is_not_shown_it(version: str):
    replies = session(
        [_initialize(version), {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}]
    )
    assert replies[0]["result"]["protocolVersion"] == version
    tools = replies[1]["result"]["tools"]
    assert tools, replies[1]
    for tool in tools:
        for field in LATER_FIELDS:
            assert field not in tool, (version, tool["name"], field)
        # And what every version does define is still there.
        assert tool["name"] and tool["description"] and tool["inputSchema"]


@pytest.mark.parametrize("version", ["2025-06-18", "2025-11-25"])
def test_a_client_that_has_the_field_is_shown_it(version: str):
    """Withholding from everyone would be the same mistake pointed the other way."""
    replies = session(
        [_initialize(version), {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}]
    )
    for tool in replies[1]["result"]["tools"]:
        for field in LATER_FIELDS:
            assert field in tool, (version, tool["name"], field)


def test_the_listing_and_the_result_describe_one_contract(tmp_path: Path):
    """The asymmetry itself: `outputSchema` advertised and `structuredContent` withheld.

    Checked as the implication a client would draw - if a tool declares an output
    schema, a call to it returns the structured content that schema describes.
    """
    repo = tmp_path / "r"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (repo / "pkg" / "n.py").write_text("from .m import f\n\n\ndef g():\n    return f()\n", "utf-8")

    for version in PROTOCOL_VERSIONS:
        replies = session(
            [
                _initialize(version),
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "map_repo", "arguments": {"path": str(repo)}},
                },
            ]
        )
        declared = any("outputSchema" in t for t in replies[1]["result"]["tools"])
        returned = "structuredContent" in replies[2]["result"]
        assert declared == returned, (
            f"{version}: tools/list {'declares' if declared else 'omits'} outputSchema "
            f"and tools/call {'returns' if returned else 'withholds'} structuredContent"
        )


def test_a_second_initialize_at_a_different_version_is_refused():
    replies = session(
        [
            _initialize("2025-06-18"),
            _initialize("2024-11-05", request_id=2),
            {"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}},
        ]
    )
    assert "error" in replies[1], replies[1]
    assert replies[1]["error"]["code"] == -32600
    assert "already initialized" in replies[1]["error"]["message"]
    # The session survives it, which is the difference between a refusal and a crash.
    assert any(r.get("id") == 3 and "result" in r for r in replies), replies


def test_the_version_is_not_changed_by_the_refused_handshake():
    """A refusal that still applied the change would be worse than accepting it."""
    replies = session(
        [
            _initialize("2025-06-18"),
            _initialize("2024-11-05", request_id=2),
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        ]
    )
    tools = replies[-1]["result"]["tools"]
    assert all("outputSchema" in t for t in tools), "the session was downgraded anyway"


def test_re_sending_the_same_initialize_is_answered():
    """A client retrying a message it is not sure arrived is not making an error."""
    replies = session([_initialize("2025-06-18"), _initialize("2025-06-18", request_id=2)])
    assert "result" in replies[1], replies[1]
    assert replies[1]["result"]["protocolVersion"] == "2025-06-18"
