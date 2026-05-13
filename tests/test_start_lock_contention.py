"""End-to-end exit-code tests for ``screencap start``.

Phase 2 U1.6 made ``screencap start`` a thin daemon HTTP client; the
SessionController hand-off is gone. The exit-code semantics moved with
it: ``lock_contended`` and other terminal conditions are now reported
by the daemon's ``recording.start`` envelope and the ``recording_finalized``
event, not by an in-process ``SessionController`` raising ``SystemExit``.

The original tests in this file mocked ``SessionController`` to verify
that an exception raised by ``run()`` propagated as the right CLI exit
code with the right ``stopped`` event. The replacement coverage is
twofold:

- Daemon-side behavior (lock acquisition, force kill, CAS) is covered
  in ``tests/daemon/test_control_verbs.py``.
- The CLI's translation of the daemon envelope into exit codes is
  covered by ``tests/cli/test_start_daemon_client.py`` (new in U1.6).

The skipped placeholders below document the obsolete contracts so
future readers see why the file shrank.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.skip(
    reason=(
        "Phase 2 U1.6: SessionController hand-off removed from screencap "
        "start; exit-code propagation is exercised by "
        "tests/cli/test_start_daemon_client.py and the daemon-side suite "
        "in tests/daemon/test_control_verbs.py."
    )
)


def test_lock_contention_propagates_exit_code_2():
    """Daemon ``lock_contended`` envelope → CLI exit 2.

    See ``tests/cli/test_start_daemon_client.py::test_start_lock_contended_returns_exit_2``.
    """


def test_uncaught_exception_emits_stopped_with_exit_code_1():
    """Generic daemon-side failure → CLI exit 1.

    See ``tests/cli/test_start_daemon_client.py::test_start_daemon_error_returns_exit_1``.
    """


def test_clean_exit_emits_stopped_with_exit_code_0():
    """Successful ``recording_finalized`` → CLI exit 0.

    See ``tests/cli/test_start_daemon_client.py::test_start_clean_finalize_exits_zero``.
    """
