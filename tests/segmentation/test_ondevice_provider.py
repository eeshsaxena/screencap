"""Tests for the on-device (Foundation Models) segmentation backend (U5).

The real Apple Foundation Models call is Swift-only and macOS-26+, so it cannot
run in CI on the macOS-13 floor. These tests drive the **Python** side against a
*fake helper* — a tiny script written to a tmp dir and pointed at via
``SCREENCAP_ONDEVICE_HELPER`` — exercising every path the real helper can take:

- happy path: helper emits a valid ``{"status":"ok","result":{…}}`` envelope →
  provider returns validated tasks (relative→Unix conversion, tag normalization).
- the whole "could not run" family → the DISTINCT ``PROVIDER_UNAVAILABLE``
  sentinel (asserted to be neither ``None`` nor an empty-tasks dict): missing
  binary, non-zero exit, timeout, OS-unsupported/model-off signal, garbage
  stdout.
- malformed-but-repairable model output → the shared validator repairs it.
- empty/invalid model output → ``None`` (ran, no usable tasks) — NOT the sentinel.
- fail-closed: a summary not marked ``stripped=True`` is refused WITHOUT ever
  spawning the helper.

These fixtures are local to this file — the shared ``_fixtures.py`` is off-limits.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from screencap.segmentation.provider import (
    PROVIDER_UNAVAILABLE,
    LLMProvider,
    get_provider,
)
from screencap.segmentation.providers.ondevice import OnDeviceProvider

# ---------------------------------------------------------------------------
# Fixtures — a minimal STRIPPED activity-data dict + a fake-helper factory.
# ---------------------------------------------------------------------------

def _stripped_activity_data() -> dict:
    """Activity-data dict marked ``stripped=True`` (as U4 hands it here).

    Session spans Unix [1000, 4600] (3600s). ``time_map`` maps the relative
    timestamps the model emits back to Unix.
    """
    return {
        "stripped": True,
        "summary": {
            "duration": "1h 0m 0s",
            "timeline": [
                {"t": "0:00:00", "app": "VS Code", "cat": "CODE",
                 "title": "main.py"},
            ],
        },
        "entries": [],
        "session_start": 1000.0,
        "session_end": 4600.0,
        "time_map": {
            "0:00:00": 1000.0,
            "0:30:00": 2800.0,
            "1:00:00": 4600.0,
        },
    }


def _valid_result() -> dict:
    """A well-formed model ``result`` (relative timestamps, pre-validation)."""
    return {
        "tasks": [
            {"start_time": "0:00:00", "end_time": "0:30:00",
             "name": "Implement auth", "description": "Wrote auth.py.",
             "category": "development", "apps_used": ["VS Code"],
             "confidence": "high"},
            {"start_time": "0:30:00", "end_time": "1:00:00",
             "name": "Coordinate review", "description": "Pinged team.",
             "category": "communication", "apps_used": ["Slack"],
             "confidence": "medium"},
        ],
        "summary": {
            "overview": "Built auth then coordinated review.",
            "primary_focus": "development",
            "time_breakdown": {"development": 50, "communication": 50},
            "key_accomplishments": ["Shipped auth"],
        },
        "tags": ["python", "auth"],
    }


def _write_helper(tmp_path: Path, body: str, *, name: str = "fake_helper.py") -> Path:
    """Write an executable python fake helper and return its path.

    ``body`` is the script body after a shebang. Helpers read the activity
    summary JSON from stdin and print an envelope (or misbehave) on stdout.
    """
    script = tmp_path / name
    script.write_text("#!" + sys.executable + "\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


@pytest.fixture()
def helper_env(monkeypatch):
    """Return a callable that points SCREENCAP_ONDEVICE_HELPER at a fresh helper.

    Also shrinks the subprocess timeout so the timeout test is fast.
    """
    def _use(path: Path) -> None:
        monkeypatch.setenv("SCREENCAP_ONDEVICE_HELPER", str(path))

    monkeypatch.setenv("SCREENCAP_ONDEVICE_HELPER_TIMEOUT", "3")
    return _use


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_valid_helper_output_returns_validated_tasks(self, tmp_path, helper_env):
        """A valid ``ok`` envelope → validated tasks (relative→Unix conversion)."""
        body = (
            "import json, sys\n"
            "sys.stdin.read()\n"
            f"print(json.dumps({{'status': 'ok', 'result': {_valid_result()!r}}}))\n"
        )
        helper_env(_write_helper(tmp_path, body))

        result = OnDeviceProvider().segment(_stripped_activity_data())

        assert isinstance(result, dict)
        assert len(result["tasks"]) == 2
        # Validator converts relative → Unix using the time_map.
        assert result["tasks"][0]["start_ts"] == 1000.0
        assert result["tasks"][0]["end_ts"] == 2800.0
        assert result["tasks"][0]["name"] == "Implement auth"
        assert result["tasks"][0]["derived_name"] == "implement-auth"
        assert result["tags"] == ["python", "auth"]

    def test_summary_subdict_is_what_reaches_helper(self, tmp_path, helper_env):
        """The helper receives ``summary`` only — not session bounds/time_map."""
        capture = tmp_path / "stdin_capture.json"
        body = (
            "import json, sys\n"
            "data = sys.stdin.read()\n"
            f"open({str(capture)!r}, 'w').write(data)\n"
            f"print(json.dumps({{'status': 'ok', 'result': {_valid_result()!r}}}))\n"
        )
        helper_env(_write_helper(tmp_path, body))

        OnDeviceProvider().segment(_stripped_activity_data())

        seen = json.loads(capture.read_text())
        assert "timeline" in seen
        assert "session_start" not in seen
        assert "time_map" not in seen


# ---------------------------------------------------------------------------
# "Could not run" family → the DISTINCT unavailable sentinel
# ---------------------------------------------------------------------------

class TestUnavailableSentinel:
    def test_sentinel_is_distinct_from_none_and_empty(self):
        """Guard the contract U7 routes on: sentinel ≠ None ≠ empty tasks."""
        assert PROVIDER_UNAVAILABLE is not None
        assert PROVIDER_UNAVAILABLE != {"tasks": []}
        assert PROVIDER_UNAVAILABLE != {}
        # Falsy for convenience, but identity is the contract.
        assert not PROVIDER_UNAVAILABLE

    def test_missing_binary_is_unavailable(self, tmp_path, helper_env, monkeypatch):
        """A helper path that doesn't exist → unavailable (no bundle fallback)."""
        monkeypatch.setenv(
            "SCREENCAP_ONDEVICE_HELPER", str(tmp_path / "does_not_exist")
        )
        result = OnDeviceProvider().segment(_stripped_activity_data())
        assert result is PROVIDER_UNAVAILABLE

    def test_no_helper_configured_is_unavailable(self, monkeypatch):
        """No env override + no app bundle (test runs from a source tree) →
        discovery fails → unavailable."""
        monkeypatch.delenv("SCREENCAP_ONDEVICE_HELPER", raising=False)
        result = OnDeviceProvider().segment(_stripped_activity_data())
        assert result is PROVIDER_UNAVAILABLE

    def test_nonzero_exit_is_unavailable(self, tmp_path, helper_env):
        body = (
            "import sys\n"
            "sys.stdin.read()\n"
            "sys.stderr.write('boom')\n"
            "sys.exit(3)\n"
        )
        helper_env(_write_helper(tmp_path, body))
        result = OnDeviceProvider().segment(_stripped_activity_data())
        assert result is PROVIDER_UNAVAILABLE

    def test_timeout_is_unavailable(self, tmp_path, helper_env):
        """A helper that hangs past the (shrunk) timeout → unavailable."""
        body = (
            "import sys, time\n"
            "sys.stdin.read()\n"
            "time.sleep(30)\n"
        )
        helper_env(_write_helper(tmp_path, body))
        result = OnDeviceProvider().segment(_stripped_activity_data())
        assert result is PROVIDER_UNAVAILABLE

    def test_os_unsupported_signal_is_unavailable(self, tmp_path, helper_env):
        """Helper reports the model is off (OS < 26 / AI disabled) → unavailable."""
        body = (
            "import json, sys\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'status': 'unavailable', "
            "'reason': 'os-unsupported'}))\n"
        )
        helper_env(_write_helper(tmp_path, body))
        result = OnDeviceProvider().segment(_stripped_activity_data())
        assert result is PROVIDER_UNAVAILABLE

    def test_garbage_stdout_is_unavailable_not_crash(self, tmp_path, helper_env):
        """Non-JSON stdout → unavailable, never an exception."""
        body = (
            "import sys\n"
            "sys.stdin.read()\n"
            "print('not json at all <<<')\n"
        )
        helper_env(_write_helper(tmp_path, body))
        result = OnDeviceProvider().segment(_stripped_activity_data())
        assert result is PROVIDER_UNAVAILABLE

    def test_empty_stdout_is_unavailable(self, tmp_path, helper_env):
        body = (
            "import sys\n"
            "sys.stdin.read()\n"
            "# exits 0, prints nothing\n"
        )
        helper_env(_write_helper(tmp_path, body))
        result = OnDeviceProvider().segment(_stripped_activity_data())
        assert result is PROVIDER_UNAVAILABLE

    def test_ok_envelope_without_result_is_unavailable(self, tmp_path, helper_env):
        body = (
            "import json, sys\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'status': 'ok'}))\n"
        )
        helper_env(_write_helper(tmp_path, body))
        result = OnDeviceProvider().segment(_stripped_activity_data())
        assert result is PROVIDER_UNAVAILABLE


# ---------------------------------------------------------------------------
# Model ran, output shape handled by the shared validator
# ---------------------------------------------------------------------------

class TestValidatorSeam:
    def test_repairable_output_is_repaired(self, tmp_path, helper_env):
        """A bad category is repaired to 'other' by the shared validator (not a
        crash, not unavailable) — the helper *ran*."""
        result_body = _valid_result()
        result_body["tasks"][0]["category"] = "not-a-real-category"
        body = (
            "import json, sys\n"
            "sys.stdin.read()\n"
            f"print(json.dumps({{'status': 'ok', 'result': {result_body!r}}}))\n"
        )
        helper_env(_write_helper(tmp_path, body))

        result = OnDeviceProvider().segment(_stripped_activity_data())
        assert isinstance(result, dict)
        assert result["tasks"][0]["category"] == "other"

    def test_empty_tasks_result_is_none_not_sentinel(self, tmp_path, helper_env):
        """Ran but produced no usable tasks → ``None`` (distinct from the
        unavailable sentinel — the helper DID run)."""
        body = (
            "import json, sys\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'status': 'ok', 'result': "
            "{'tasks': [], 'summary': {}, 'tags': []}}))\n"
        )
        helper_env(_write_helper(tmp_path, body))

        result = OnDeviceProvider().segment(_stripped_activity_data())
        assert result is None
        assert result is not PROVIDER_UNAVAILABLE

    def test_malformed_task_item_does_not_crash(self, tmp_path, helper_env):
        """A non-dict task item makes the validator raise; segment swallows it to
        ``None`` (ran, no usable tasks) rather than propagating / crashing."""
        body = (
            "import json, sys\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'status': 'ok', 'result': {'tasks': [42]}}))\n"
        )
        helper_env(_write_helper(tmp_path, body))

        result = OnDeviceProvider().segment(_stripped_activity_data())
        assert result is None


# ---------------------------------------------------------------------------
# Fail-closed privacy gate (R11)
# ---------------------------------------------------------------------------

class TestFailClosed:
    def test_unmarked_summary_refused_without_spawning_helper(
        self, tmp_path, helper_env
    ):
        """A summary not marked ``stripped=True`` is refused → unavailable, and
        the helper is NEVER spawned (proven by a sentinel file it would write)."""
        touched = tmp_path / "helper_ran.flag"
        body = (
            "import json, sys\n"
            "sys.stdin.read()\n"
            f"open({str(touched)!r}, 'w').write('ran')\n"
            f"print(json.dumps({{'status': 'ok', 'result': {_valid_result()!r}}}))\n"
        )
        helper_env(_write_helper(tmp_path, body))

        data = _stripped_activity_data()
        del data["stripped"]  # unmarked
        result = OnDeviceProvider().segment(data)

        assert result is PROVIDER_UNAVAILABLE
        assert not touched.exists(), "helper must not run on unmarked input"

    def test_stripped_false_is_also_refused(self, tmp_path, helper_env):
        """Only the exact ``stripped is True`` marker passes; a falsy value is
        still refused (fail-closed, no truthiness ambiguity)."""
        body = (
            "import json, sys\n"
            "sys.stdin.read()\n"
            f"print(json.dumps({{'status': 'ok', 'result': {_valid_result()!r}}}))\n"
        )
        helper_env(_write_helper(tmp_path, body))

        data = _stripped_activity_data()
        data["stripped"] = False
        assert OnDeviceProvider().segment(data) is PROVIDER_UNAVAILABLE

        data["stripped"] = "yes"  # truthy but not the marker
        assert OnDeviceProvider().segment(data) is PROVIDER_UNAVAILABLE


# ---------------------------------------------------------------------------
# Factory wiring
# ---------------------------------------------------------------------------

class TestFactory:
    def test_on_device_maps_to_ondevice_provider(self):
        provider = get_provider("on-device")
        assert isinstance(provider, OnDeviceProvider)
        # Structurally satisfies the interface.
        assert isinstance(provider, LLMProvider)


# ---------------------------------------------------------------------------
# Import-lightness — the backend stays SDK-free at import time.
# ---------------------------------------------------------------------------

class TestImportLightness:
    def test_ondevice_module_is_import_light(self):
        """Importing the backend must not pull a vendor SDK. Run in a clean
        subprocess so cross-test imports can't mask a stray import."""
        import subprocess

        code = (
            "import sys; "
            "import screencap.segmentation.providers.ondevice; "
            "assert 'google.genai' not in sys.modules; "
            "print('OK')"
        )
        env = dict(os.environ)
        src_root = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "src",
        )
        env["PYTHONPATH"] = src_root + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, env=env,
        )
        assert proc.returncode == 0, proc.stderr
        assert "OK" in proc.stdout
