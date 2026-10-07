"""Lines that used to end the process, and the cap that bounds what is read at all.

`json.loads(line)` was wrapped in `except json.JSONDecodeError`, which is the way a line
is *invalid* and not the only way parsing one can fail. A reviewer found the cliff:

    10,000 nested `[`   -32600 "not a JSON-RPC 2.0 message", then `ping` answered
    15,000 nested `[`   RecursionError, process exits 1, the `ping` never answered
    20,000 nested `{`   the same

A 30 KB line terminated the server, taking that request and every request after it. The
depth at which the decoder stops returning and starts raising is a property of the
interpreter's stack, so reading the code could not show it.

There was no cap on the line either - `for raw in stdin` buffers whatever arrives - and
`path` was the one tool argument `_string`'s 4,000-character limit did not cover, so a
64 MB path was accepted, resolved and echoed back whole in an error message.
"""

from __future__ import annotations

import io
import json

import pytest

from reporecon.mcp_server import ARGUMENT_LIMIT, MAX_LINE, ReconServer, serve

AFTER = 99


def session(lines: list) -> list[dict]:
    """Raw lines through the real read loop, so a line that is not JSON can be sent."""
    payload = b"\n".join(
        line if isinstance(line, bytes) else json.dumps(line).encode("utf-8") for line in lines
    )
    stdout = io.BytesIO()
    serve(io.BytesIO(payload + b"\n"), stdout, ReconServer())
    stdout.seek(0)
    return [json.loads(line) for line in stdout.read().decode("utf-8").splitlines() if line]


def _handshake() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18"},
    }


def _ping() -> dict:
    return {"jsonrpc": "2.0", "id": AFTER, "method": "ping", "params": {}}


@pytest.mark.parametrize("depth", [100, 10_000, 15_000, 40_000])
def test_a_deeply_nested_line_does_not_end_the_session(depth: int):
    """Every depth, because the failure was a cliff between two of them."""
    replies = session([_handshake(), b"[" * depth, _ping()])
    assert [r for r in replies if r.get("id") == AFTER], f"the session ended at depth {depth}"


@pytest.mark.parametrize("opener,closer", [(b"[", b"]"), (b"{", b"}")])
def test_a_balanced_deeply_nested_document_is_a_parse_error(opener: bytes, closer: bytes):
    """Balanced, so the decoder does the work rather than failing early on the syntax."""
    line = opener * 20_000 + b"1" + closer * 20_000 if opener == b"{" else opener * 20_000
    replies = session([_handshake(), line, _ping()])
    errors = [r for r in replies if "error" in r]
    assert errors, replies
    assert errors[0]["error"]["code"] == -32700, errors[0]
    assert [r for r in replies if r.get("id") == AFTER], "the session ended"


def test_a_line_over_the_cap_is_refused_before_it_is_parsed():
    replies = session([_handshake(), b'{"a":"' + b"x" * (MAX_LINE + 10) + b'"}', _ping()])
    errors = [r for r in replies if "error" in r]
    assert errors, replies
    assert errors[0]["error"]["code"] == -32700
    assert "the limit is" in errors[0]["error"]["message"]
    assert [r for r in replies if r.get("id") == AFTER], "the session ended"


def test_an_enormous_path_is_refused_rather_than_echoed():
    """`_string` has capped its arguments since it was written; `path` went around it,
    because it is read by `_resolve` instead. A 64 MB path came back in full."""
    enormous = "x" * (ARGUMENT_LIMIT + 1)
    replies = session(
        [
            _handshake(),
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "map_repo", "arguments": {"path": enormous}},
            },
            _ping(),
        ]
    )
    result = next(r for r in replies if r.get("id") == 2)["result"]
    assert result.get("isError"), result
    text = result["content"][0]["text"]
    assert "the limit is" in text, text
    # The message is about the argument; it does not contain the argument.
    assert len(text) < 1_000, len(text)
    assert "xxxxxxxxxx" not in text
    assert [r for r in replies if r.get("id") == AFTER], "the session ended"


def test_a_path_inside_the_cap_is_still_read():
    """A cap that refuses a legitimate path would be the other failure."""
    replies = session(
        [
            _handshake(),
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "map_repo", "arguments": {"path": "no_such_directory_here"}},
            },
        ]
    )
    text = next(r for r in replies if r.get("id") == 2)["result"]["content"][0]["text"]
    assert "the limit is" not in text, text


def test_the_reply_to_a_hostile_line_is_one_line():
    """stdout is a protocol stream. An error object split over two lines is not a
    message, and a client reading line by line desynchronises on it."""
    stdout = io.BytesIO()
    payload = b"\n".join(
        [json.dumps(_handshake()).encode(), b"[" * 20_000, json.dumps(_ping()).encode()]
    )
    serve(io.BytesIO(payload + b"\n"), stdout, ReconServer())
    lines = stdout.getvalue().splitlines()
    assert len(lines) == 3, lines
    for line in lines:
        json.loads(line)
