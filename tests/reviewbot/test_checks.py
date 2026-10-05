"""What the proposers see, and what they must not.

A checker that misses the obvious cases is worse than no checker: it reports a clean
file and the reader believes it. These tests are the shapes, not the count.
"""

from __future__ import annotations

import textwrap

import pytest

from reporecon.reviewbot.checks import propose


@pytest.mark.parametrize(
    "handler,caught",
    [
        ("except Exception:\n        pass", "Exception"),
        ("except Exception:\n        return None", "Exception"),
        ("except (ValueError, KeyError):\n        pass", "(ValueError, KeyError)"),
        ("except OSError:\n        pass", "OSError"),
        ("except ValueError:\n        return", "ValueError"),
    ],
)
def test_every_shape_of_swallow_is_seen(handler, caught):
    """It required a bare `Exception` Name AND a body of exactly one `Pass`.

    Four of the six obvious swallows were invisible, including this repository's own
    `except OSError: continue` — so its reviewer could not see its own code. Found by
    pointing it at itself once the rule was widened, and the four it then found were
    all real.
    """
    source = f"def f():\n    try:\n        go()\n    {handler}\n"
    found = [p for p in propose(source) if p.rule == "swallowed-exception"]
    assert len(found) == 1, [p.message for p in propose(source)]
    assert caught in found[0].message


def test_a_swallow_inside_a_loop_is_seen():
    source = textwrap.dedent(
        """\
        def f(xs):
            for x in xs:
                try:
                    go(x)
                except OSError:
                    continue
        """
    )
    assert [p.rule for p in propose(source) if p.rule == "swallowed-exception"]


@pytest.mark.parametrize(
    "handler",
    [
        "except ValueError:\n        log.warning('bad')\n        raise",
        "except Exception as exc:\n        record(exc)",
        "except OSError:\n        return fallback()",
        "except KeyError:\n        raise Missing() from None",
    ],
)
def test_a_handler_that_does_something_is_not_a_swallow(handler):
    """A handler with a body is a decision. This check is for the ones with none."""
    source = f"def f():\n    try:\n        go()\n    {handler}\n"
    assert [p for p in propose(source) if p.rule == "swallowed-exception"] == []
