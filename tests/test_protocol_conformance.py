"""Three shapes the protocol defines, and what this server did with them.

Each was found by an independent reviewer sending a well-formed message and reading the
reply. None of them crashed the session - the loop has been hardened against that - and
all three answered something untrue about the input.

**A request batch.** `PROTOCOL_VERSIONS` advertises `2025-03-26` and `2024-11-05`, and a
JSON array of requests is part of the spec in both; it was removed in `2025-06-18`.
`isinstance(message, dict)` rejected the array with "not a JSON-RPC 2.0 message", which
an array of requests is, so a conforming client's batch of N requests got one `id: null`
error.

**A limit in two units.** `_bounded_lines` reads `MAX_LINE + 1` BYTES and the check
counted CHARACTERS, so 1.4 MB of `é` truncated at a million bytes, arrived as 500,000
characters, passed a 1,000,000-character check and came back as "invalid JSON:
Unterminated string" - while the reader's own docstring promised a message naming the
limit.

**A structured id.** `{"id": {"a": 1}}` was accepted and echoed. The spec says id SHOULD
be a String, Number or Null; the null case had a dedicated branch and a comment, and the
shape the spec does not contemplate had neither.
"""

from __future__ import annotations

import io
import json

import pytest

from reporecon.mcp_server import BATCH_VERSIONS, MAX_LINE, PROTOCOL_VERSIONS, ReconServer, serve

#: The id of the message sent after the interesting one. A reply carrying it is the
#: assertion that matters most: the session is still there.
AFTER = 909


def _run(lines: list[bytes]) -> list:
    stdin = io.BytesIO(b"\n".join(lines) + b"\n")
    stdout = io.BytesIO()
    serve(stdin, stdout, ReconServer())
    return [json.loads(raw) for raw in stdout.getvalue().splitlines()]


def _encode(message) -> bytes:
    return json.dumps(message).encode("utf-8")


def _init(version: str) -> bytes:
    return _encode(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": version, "capabilities": {}},
        }
    )


def _ping(request_id=AFTER) -> bytes:
    return _encode({"jsonrpc": "2.0", "id": request_id, "method": "ping"})


def _alive(replies: list) -> bool:
    return any(isinstance(r, dict) and r.get("id") == AFTER for r in replies)


# -- the batch -------------------------------------------------------------------------

BATCH = [
    {"jsonrpc": "2.0", "id": 2, "method": "ping"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
]


def test_the_versions_that_take_a_batch_are_the_ones_that_define_it():
    """A fact about the spec, pinned so the table cannot drift from the versions
    advertised: batching was removed in 2025-06-18."""
    assert BATCH_VERSIONS <= set(PROTOCOL_VERSIONS), sorted(BATCH_VERSIONS)
    assert "2025-06-18" not in BATCH_VERSIONS
    assert "2025-03-26" in BATCH_VERSIONS
    assert "2024-11-05" in BATCH_VERSIONS


@pytest.mark.parametrize("version", sorted(BATCH_VERSIONS))
def test_a_batch_is_answered_with_one_array_on_a_version_that_defines_it(version: str):
    replies = _run([_init(version), _encode(BATCH), _ping()])
    batch_reply = replies[1]
    assert isinstance(batch_reply, list), batch_reply
    assert [r["id"] for r in batch_reply] == [2, 3], batch_reply
    assert all("result" in r for r in batch_reply), batch_reply
    assert _alive(replies)


def test_a_batch_is_refused_by_version_where_it_was_removed():
    """Refused for the reason it is refused. "not a JSON-RPC 2.0 message" was a
    statement about the shape and it was false of that shape."""
    replies = _run([_init("2025-06-18"), _encode(BATCH), _ping()])
    reply = replies[1]
    assert isinstance(reply, dict), reply
    assert reply["error"]["code"] == -32600
    assert "2025-06-18" in reply["error"]["message"]
    assert "batch" in reply["error"]["message"]
    assert "not a JSON-RPC 2.0 message" not in reply["error"]["message"]
    assert _alive(replies)


def test_a_batch_of_only_notifications_is_answered_with_nothing():
    """Which is what the spec says, and the reason the handler returns a reply rather
    than writing one."""
    replies = _run(
        [
            _init("2024-11-05"),
            _encode([{"jsonrpc": "2.0", "method": "ping"}]),
            _ping(),
        ]
    )
    assert len(replies) == 2, replies  # the handshake and the ping after it
    assert _alive(replies)


def test_an_empty_batch_is_an_invalid_request():
    replies = _run([_init("2024-11-05"), _encode([]), _ping()])
    assert replies[1]["error"]["code"] == -32600
    assert "empty" in replies[1]["error"]["message"]
    assert _alive(replies)


def test_one_bad_element_does_not_spoil_the_batch():
    """Each element is a request in its own right, so each gets its own answer."""
    replies = _run(
        [
            _init("2024-11-05"),
            _encode([{"jsonrpc": "2.0", "id": 2, "method": "ping"}, "garbage"]),
            _ping(),
        ]
    )
    batch_reply = replies[1]
    assert isinstance(batch_reply, list) and len(batch_reply) == 2, batch_reply
    assert "result" in batch_reply[0], batch_reply[0]
    assert batch_reply[1]["error"]["code"] == -32600, batch_reply[1]
    assert _alive(replies)


def test_a_batch_and_a_line_go_through_the_same_handler():
    """Or the batch path is a second implementation of the protocol, which is how two
    paths come to disagree. The same request, both ways, same result."""
    one = _run([_init("2024-11-05"), _ping(5), _ping()])
    many = _run([_init("2024-11-05"), _encode([{"jsonrpc": "2.0", "id": 5, "method": "ping"}]), _ping()])
    assert one[1] == many[1][0], (one[1], many[1])


# -- the line limit --------------------------------------------------------------------


def test_a_multibyte_line_over_the_byte_limit_is_reported_as_over_the_limit():
    """Two bytes per character, so this is 1.4 MB and 700,047 characters: over the
    limit in bytes and under it in characters. It used to come back as malformed JSON,
    which is a statement about the content of a line that was cut in half."""
    payload = '{"jsonrpc":"2.0","id":2,"method":"ping","x":"' + "é" * 700_000 + '"}'
    raw = payload.encode("utf-8")
    assert len(raw) > MAX_LINE
    assert len(payload) < MAX_LINE, "this line is over the limit in characters too"

    replies = _run([_init("2025-06-18"), raw, _ping()])
    errors = [r for r in replies if isinstance(r, dict) and "error" in r]
    assert errors, replies
    assert errors[0]["error"]["code"] == -32700
    assert "the limit is" in errors[0]["error"]["message"]
    assert "cut off" in errors[0]["error"]["message"]
    assert "JSONDecodeError" not in errors[0]["error"]["message"]
    assert _alive(replies)


def test_an_ascii_line_over_the_limit_still_says_so():
    """The case that already worked, kept, because the fix added a second test and the
    first one has to go on holding."""
    raw = b'{"jsonrpc":"2.0","id":2,"method":"ping","x":"' + b"a" * (MAX_LINE + 10) + b'"}'
    replies = _run([_init("2025-06-18"), raw, _ping()])
    errors = [r for r in replies if isinstance(r, dict) and "error" in r]
    assert errors and errors[0]["error"]["code"] == -32700
    assert "the limit is" in errors[0]["error"]["message"]
    assert _alive(replies)


def test_the_line_after_an_over_long_one_is_a_line_and_not_its_tail():
    """The drain. Without it the remainder of a 2 MB line is read as the next request."""
    raw = b'{"x":"' + b"a" * (MAX_LINE * 2) + b'"}'
    replies = _run([_init("2025-06-18"), raw, _ping()])
    assert _alive(replies), replies
    # One refusal for the long line, not several.
    parse_errors = [
        r for r in replies if isinstance(r, dict) and r.get("error", {}).get("code") == -32700
    ]
    assert len(parse_errors) == 1, parse_errors


# -- the id ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "request_id", [{"a": 1}, [1, 2], 1.5, True, False], ids=["object", "array", "float", "true", "false"]
)
def test_an_id_that_is_not_a_string_or_an_integer_is_refused(request_id):
    replies = _run(
        [
            _init("2025-06-18"),
            _encode({"jsonrpc": "2.0", "id": request_id, "method": "ping"}),
            _ping(),
        ]
    )
    reply = replies[1]
    assert "error" in reply, reply
    assert reply["error"]["code"] == -32600, reply
    assert "id must be" in reply["error"]["message"], reply
    assert reply["id"] is None, reply
    assert _alive(replies)


@pytest.mark.parametrize("request_id", ["abc", 7, -1, 0], ids=["string", "int", "negative", "zero"])
def test_an_ordinary_id_is_answered_and_echoed(request_id):
    """The other direction: the refusal above must not have eaten the two shapes the
    spec actually names."""
    replies = _run(
        [
            _init("2025-06-18"),
            _encode({"jsonrpc": "2.0", "id": request_id, "method": "ping"}),
            _ping(),
        ]
    )
    assert "result" in replies[1], replies[1]
    assert replies[1]["id"] == request_id, replies[1]


def test_a_null_id_keeps_its_own_message():
    """It has a dedicated branch for a reason - a client using null ids was left
    waiting - and the new scalar check must not swallow it."""
    replies = _run(
        [
            _init("2025-06-18"),
            _encode({"jsonrpc": "2.0", "id": None, "method": "ping"}),
            _ping(),
        ]
    )
    assert replies[1]["error"]["code"] == -32600
    assert "must not be null" in replies[1]["error"]["message"], replies[1]
    assert _alive(replies)


def test_a_notification_is_still_not_answered():
    """No `id` at all, which is different from a null one."""
    replies = _run(
        [_init("2025-06-18"), _encode({"jsonrpc": "2.0", "method": "ping"}), _ping()]
    )
    assert len(replies) == 2, replies
    assert _alive(replies)
