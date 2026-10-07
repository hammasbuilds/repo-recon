"""The suite runs in a known environment, whatever the operator has set.

`REPO_RECON_ROOT` confines every path the server will look at. It is a legitimate thing
for an operator to set, and with it pointing anywhere other than this checkout the suite
failed 36 tests and errored twice:

    ToolError: 'D:\\github\\repo-recon' is outside REPO_RECON_ROOT (...)

Loud rather than silent, so nothing was ever wrong in a way a reader would believe - but
anyone running the server the way it is meant to be run could not run its tests, and a
test suite that depends on an ambient variable is measuring the machine as much as the
code.

Cleared for every test. The tests that are ABOUT confinement set it themselves, with
`monkeypatch`, which is what makes them tests of the variable rather than tests under
it.
"""

from __future__ import annotations

import pytest

#: Environment this package reads. Cleared so the suite describes the code.
#:
#: Named individually rather than cleared wholesale: a test that needs `PATH` or
#: `SYSTEMROOT` has to keep them, and the point is to control what this package reads,
#: not to invent a hermetic machine.
OWN_ENVIRONMENT = ("REPO_RECON_ROOT",)


@pytest.fixture(autouse=True, scope="session")
def _own_environment_is_not_the_operators():
    """Session-scoped, and `os.environ` directly.

    A function-scoped `monkeypatch` fixture runs after module- and session-scoped
    fixtures, so the ones that build a map or drive a server at module scope still saw
    the operator's value and errored during setup - which looks like a broken test
    rather than a confined one. The variable is read at call time by `_root()`, so
    clearing it once for the session is enough and is what the session actually needs.
    """
    import os

    saved = {name: os.environ.pop(name, None) for name in OWN_ENVIRONMENT}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value
