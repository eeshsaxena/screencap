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
from screencap.segmentation.providers import ondevice
from screencap.segmentation.providers.ondevice import OnDeviceProvider

# Everything here is privacy-bearing (the fail-closed strip gates + what may
# reach the model); CI's privacy lane must run the whole module.
pytestmark = pytest.mark.privacy

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

class TestBundledHelperDiscovery:
    """`_find_bundled_helper()` bundle-walk across the real app layouts (SCR-239).

    The daemon can run from more than one place inside the app, but the helper
    only ever ships in the OUTER ``ScreenCap.app/Contents/MacOS`` (the app's
    "Embed IntelligenceHelper" build phase copies it there). After SCR-196 the
    daemon runs from a NESTED ``ScreencapDaemon.app`` under
    ``Contents/Library/LoginItems`` — so the helper it needs is in an ANCESTOR
    bundle, past the first (helper-less) ``Contents``. These pin the walk so the
    nested layout still discovers the outer helper, while a bundle-less install
    stays ``None``.
    """

    @staticmethod
    def _make_helper(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    @staticmethod
    def _make_module(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
        return path

    def _discover(self, monkeypatch, ondevice_file: Path):
        from screencap.segmentation.providers import ondevice

        monkeypatch.setattr(ondevice, "__file__", str(ondevice_file))
        return ondevice._find_bundled_helper()

    def test_outer_app_resources_layout_is_discovered(self, tmp_path, monkeypatch):
        """CLI at ``ScreenCap.app/Contents/Resources`` → helper in that same
        ``Contents/MacOS`` (the historical single-bundle layout)."""
        app = tmp_path / "ScreenCap.app"
        helper = app / "Contents" / "MacOS" / "IntelligenceHelper"
        self._make_helper(helper)
        mod = self._make_module(
            app / "Contents" / "Resources" / "screencap"
            / "segmentation" / "providers" / "ondevice.py"
        )
        assert self._discover(monkeypatch, mod) == helper

    def test_nested_daemon_bundle_finds_outer_helper(self, tmp_path, monkeypatch):
        """SCR-196: the daemon runs from a NESTED ``ScreencapDaemon.app`` whose
        own ``Contents/MacOS`` has no helper; discovery must keep walking up to
        the OUTER app's ``Contents/MacOS`` rather than stop at the first
        ``Contents``. This is the regression guard for the idle-gap fallback."""
        app = tmp_path / "ScreenCap.app"
        helper = app / "Contents" / "MacOS" / "IntelligenceHelper"
        self._make_helper(helper)
        mod = self._make_module(
            app / "Contents" / "Library" / "LoginItems" / "ScreencapDaemon.app"
            / "Contents" / "Frameworks" / "screencap"
            / "segmentation" / "providers" / "ondevice.py"
        )
        assert self._discover(monkeypatch, mod) == helper

    def test_no_app_bundle_is_none(self, tmp_path, monkeypatch):
        """A source/CLI-only tree with no ``.app`` ancestor → ``None`` (headless
        installs ship no bundled helper — U7 then degrades)."""
        mod = self._make_module(
            tmp_path / "src" / "screencap" / "segmentation"
            / "providers" / "ondevice.py"
        )
        assert self._discover(monkeypatch, mod) is None

    def test_contents_without_any_helper_is_none(self, tmp_path, monkeypatch):
        """A ``Contents`` ancestor whose ``MacOS`` lacks the helper and no outer
        bundle carries one → ``None``, not a crash or a false positive."""
        mod = self._make_module(
            tmp_path / "Weird.app" / "Contents" / "Resources" / "screencap"
            / "segmentation" / "providers" / "ondevice.py"
        )
        assert self._discover(monkeypatch, mod) is None


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


# ===========================================================================
# Free-form answer path (SCR-243, U4)
# ===========================================================================

from screencap.segmentation.generation import Evidence  # noqa: E402


def _stripped_evidence(text: str = "You edited main.py and ran pytest.") -> Evidence:
    return Evidence(text=text, stripped=True)


def _answer_helper(printed: str) -> str:
    """Fake helper body: drain stdin, print a fixed answer envelope string."""
    return "import sys\nsys.stdin.read()\n" + f"print({printed!r})\n"


class TestAnswer:
    def test_ok_envelope_returns_sanitized_text(self, tmp_path, helper_env):
        env = json.dumps({"status": "ok", "result": "You worked on <b>auth</b>."})
        helper_env(_write_helper(tmp_path, _answer_helper(env)))
        out = OnDeviceProvider().answer("what did I do?", _stripped_evidence())
        assert "<" not in out and ">" not in out  # markup neutralized by sanitize_answer (KTD10)
        assert "auth" in out

    @pytest.mark.privacy
    def test_unmarked_evidence_refused_without_spawn(self, tmp_path, helper_env):
        sentinel = tmp_path / "spawned"
        body = (
            "import pathlib, sys\n"
            f"pathlib.Path({str(sentinel)!r}).write_text('x')\n"
            "sys.stdin.read()\nprint('{\"status\":\"ok\",\"result\":\"x\"}')\n"
        )
        helper_env(_write_helper(tmp_path, body))
        out = OnDeviceProvider().answer("q", Evidence(text="raw", stripped=False))
        assert out is PROVIDER_UNAVAILABLE
        assert not sentinel.exists()  # helper never spawned

    def test_oversized_evidence_unavailable(self, tmp_path, helper_env):
        helper_env(_write_helper(tmp_path, _answer_helper('{"status":"ok","result":"hi"}')))
        huge = Evidence(text="x" * (600 * 1024), stripped=True)
        assert OnDeviceProvider().answer("q", huge) is PROVIDER_UNAVAILABLE

    def test_oversized_prompt_unavailable(self, tmp_path, helper_env):
        # The prompt cap is an order of magnitude smaller than evidence (16KiB);
        # exercise its half of the size gate independently.
        helper_env(_write_helper(tmp_path, _answer_helper('{"status":"ok","result":"hi"}')))
        assert OnDeviceProvider().answer("p" * (20 * 1024), _stripped_evidence()) is PROVIDER_UNAVAILABLE

    def test_missing_helper_unavailable(self, monkeypatch):
        monkeypatch.setenv("SCREENCAP_ONDEVICE_HELPER", "/nonexistent/helper")
        assert OnDeviceProvider().answer("q", _stripped_evidence()) is PROVIDER_UNAVAILABLE

    def test_unavailable_envelope(self, tmp_path, helper_env):
        env = json.dumps({"status": "unavailable", "reason": "os-below-macos-26"})
        helper_env(_write_helper(tmp_path, _answer_helper(env)))
        assert OnDeviceProvider().answer("q", _stripped_evidence()) is PROVIDER_UNAVAILABLE

    def test_nonzero_exit_unavailable(self, tmp_path, helper_env):
        helper_env(_write_helper(tmp_path, "import sys\nsys.stdin.read()\nsys.exit(3)\n"))
        assert OnDeviceProvider().answer("q", _stripped_evidence()) is PROVIDER_UNAVAILABLE

    def test_garbage_stdout_unavailable(self, tmp_path, helper_env):
        helper_env(_write_helper(tmp_path, "import sys\nsys.stdin.read()\nprint('not json')\n"))
        assert OnDeviceProvider().answer("q", _stripped_evidence()) is PROVIDER_UNAVAILABLE

    def test_non_string_result_unavailable(self, tmp_path, helper_env):
        env = json.dumps({"status": "ok", "result": {"not": "a string"}})
        helper_env(_write_helper(tmp_path, _answer_helper(env)))
        assert OnDeviceProvider().answer("q", _stripped_evidence()) is PROVIDER_UNAVAILABLE

    def test_empty_result_unavailable(self, tmp_path, helper_env):
        env = json.dumps({"status": "ok", "result": "   "})
        helper_env(_write_helper(tmp_path, _answer_helper(env)))
        assert OnDeviceProvider().answer("q", _stripped_evidence()) is PROVIDER_UNAVAILABLE


# ===========================================================================
# Window-scoped helper verbs (SCR-275, U3): arbitrate / name-window /
# day-summary — reason-bearing CallResults + the KTD-3 retry taxonomy.
# ===========================================================================

def _ok(result: dict) -> dict:
    return {"status": "ok", "result": result}


def _unavailable(reason: str) -> dict:
    return {"status": "unavailable", "reason": reason}


def _verb_helper(
    tmp_path: Path,
    envelopes: list[dict],
    *,
    count_file: Path,
    capture: Path | None = None,
) -> Path:
    """Fake helper that logs each invocation and emits ``envelopes[n]``.

    ``count_file`` holds the invocation count (its absence proves the helper
    was never spawned — the strip-gate evidence). The n-th run emits
    ``envelopes[n]`` (the last repeats); with ``capture``, the n-th run also
    writes its stdin to ``<capture><n>``.
    """
    capture_line = (
        f"pathlib.Path({str(capture)!r} + str(n)).write_text(data)\n"
        if capture else ""
    )
    body = (
        "import json, pathlib, sys\n"
        "data = sys.stdin.read()\n"
        f"cf = pathlib.Path({str(count_file)!r})\n"
        "n = int(cf.read_text()) if cf.exists() else 0\n"
        "cf.write_text(str(n + 1))\n"
        + capture_line
        + f"envelopes = {envelopes!r}\n"
        "print(json.dumps(envelopes[min(n, len(envelopes) - 1)]))\n"
    )
    return _write_helper(tmp_path, body)


def _calls(count_file: Path) -> int:
    return int(count_file.read_text()) if count_file.exists() else 0


def _stripped_digest() -> dict:
    """A U1-style naming digest as the U4 orchestrator wraps it: the trimmed
    payload content plus the fail-closed ``stripped=True`` attestation."""
    return {
        "stripped": True,
        "duration": "0:25:00",
        "timeline": [
            {"t": "0:00:00", "app": "VS Code", "cat": "CODE", "title": "auth.py"},
        ],
    }


class TestVerbTimeouts:
    """The new verbs default to 60s; the legacy whole-day paths keep 120s.
    One env var (SCREENCAP_ONDEVICE_HELPER_TIMEOUT) overrides both."""

    def test_defaults_differ(self, monkeypatch):
        monkeypatch.delenv("SCREENCAP_ONDEVICE_HELPER_TIMEOUT", raising=False)
        assert ondevice._verb_timeout_s() == 60.0
        assert ondevice._helper_timeout_s() == 120.0

    def test_env_overrides_both(self, monkeypatch):
        monkeypatch.setenv("SCREENCAP_ONDEVICE_HELPER_TIMEOUT", "7.5")
        assert ondevice._verb_timeout_s() == 7.5
        assert ondevice._helper_timeout_s() == 7.5


class TestCallArbitrate:
    def test_happy_path_parses_merges_and_builds_request(self, tmp_path, helper_env):
        cf, cap = tmp_path / "count", tmp_path / "stdin"
        helper_env(_verb_helper(
            tmp_path, [_ok({"merges": [[1, 2], [4, 5]]})], count_file=cf, capture=cap,
        ))
        provider = OnDeviceProvider()

        res = provider.call_arbitrate(["line a", "line b", "line c"], stripped=True)

        assert res.reason is None and res.ok
        assert res.value == [[1, 2], [4, 5]]
        assert provider.last_unavailable_reason is None
        # The request wrapper is built HERE: indexed one-line-per-window form.
        seen = json.loads(Path(str(cap) + "0").read_text())
        assert seen == {
            "task": "arbitrate",
            "windows": [
                {"i": 0, "line": "line a"},
                {"i": 1, "line": "line b"},
                {"i": 2, "line": "line c"},
            ],
        }

    def test_empty_merges_means_keep_all_boundaries(self, tmp_path, helper_env):
        cf = tmp_path / "count"
        helper_env(_verb_helper(tmp_path, [_ok({"merges": []})], count_file=cf))
        res = OnDeviceProvider().call_arbitrate(["a", "b"], stripped=True)
        assert res.reason is None
        assert res.value == []  # empty = keep every heuristic boundary

    def test_unavailable_reason_surfaces_exactly(self, tmp_path, helper_env):
        cf = tmp_path / "count"
        helper_env(_verb_helper(tmp_path, [_unavailable("guardrail")], count_file=cf))
        provider = OnDeviceProvider()
        res = provider.call_arbitrate(["a", "b"], stripped=True)
        assert res.value is None and not res.ok
        assert res.reason == "guardrail"
        assert provider.last_unavailable_reason == "guardrail"

    def test_unstripped_refused_without_spawning(self, tmp_path, helper_env):
        cf = tmp_path / "count"
        helper_env(_verb_helper(tmp_path, [_ok({"merges": []})], count_file=cf))
        provider = OnDeviceProvider()
        res = provider.call_arbitrate(["a", "b"], stripped=False)
        assert res.value is None
        assert res.reason == ondevice.REASON_NOT_STRIPPED
        assert provider.last_unavailable_reason == ondevice.REASON_NOT_STRIPPED
        assert _calls(cf) == 0, "helper must not spawn on unstripped input"

    def test_malformed_merges_is_invalid_result(self, tmp_path, helper_env):
        cf = tmp_path / "count"
        helper_env(_verb_helper(
            tmp_path, [_ok({"merges": [[1, "two"]]})], count_file=cf,
        ))
        res = OnDeviceProvider().call_arbitrate(["a", "b"], stripped=True)
        assert res.value is None
        assert res.reason == ondevice.REASON_BAD_RESULT


class TestCallNameWindow:
    def test_happy_path_returns_name_category(self, tmp_path, helper_env):
        cf, cap = tmp_path / "count", tmp_path / "stdin"
        helper_env(_verb_helper(
            tmp_path,
            [_ok({"name": "Implement auth", "category": "development"})],
            count_file=cf, capture=cap,
        ))
        provider = OnDeviceProvider()

        res = provider.call_name_window(_stripped_digest())

        assert res.reason is None
        assert res.value == ("Implement auth", "development")
        assert provider.last_unavailable_reason is None
        # Wrapper: {"task":"name-window","digest":{…}} with the attestation
        # marker asserted here and NOT forwarded into the model prompt.
        seen = json.loads(Path(str(cap) + "0").read_text())
        assert seen["task"] == "name-window"
        assert "stripped" not in seen["digest"]
        assert seen["digest"]["timeline"] == _stripped_digest()["timeline"]

    def test_unstripped_digest_refused_without_spawning(self, tmp_path, helper_env):
        cf = tmp_path / "count"
        helper_env(_verb_helper(
            tmp_path, [_ok({"name": "x", "category": "other"})], count_file=cf,
        ))
        provider = OnDeviceProvider()

        digest = _stripped_digest()
        del digest["stripped"]  # unmarked
        assert provider.call_name_window(digest).reason == ondevice.REASON_NOT_STRIPPED

        digest = _stripped_digest()
        digest["stripped"] = False
        assert provider.call_name_window(digest).reason == ondevice.REASON_NOT_STRIPPED

        assert _calls(cf) == 0, "helper must not spawn on unstripped digests"

    def test_context_window_surfaced_not_retried(self, tmp_path, helper_env):
        """KTD-3: digest halving on context-window is CALLER-driven (U4); this
        layer surfaces the reason after a single attempt."""
        cf = tmp_path / "count"
        helper_env(_verb_helper(
            tmp_path, [_unavailable("context-window")], count_file=cf,
        ))
        provider = OnDeviceProvider()
        res = provider.call_name_window(_stripped_digest())
        assert res.value is None
        assert res.reason == "context-window"
        assert provider.last_unavailable_reason == "context-window"
        assert _calls(cf) == 1

    def test_non_string_name_is_invalid_result(self, tmp_path, helper_env):
        cf = tmp_path / "count"
        helper_env(_verb_helper(
            tmp_path, [_ok({"name": 42, "category": "development"})], count_file=cf,
        ))
        res = OnDeviceProvider().call_name_window(_stripped_digest())
        assert res.value is None
        assert res.reason == ondevice.REASON_BAD_RESULT


class TestCallDaySummary:
    _ROWS = [
        {"name": "Implement auth", "category": "development", "minutes": 50},
        {"name": "Coordinate review", "category": "communication", "minutes": 10},
    ]

    def test_happy_path_no_strip_gate_needed(self, tmp_path, helper_env):
        """day-summary input carries NO strip gate by design: the rows are
        model outputs / mechanical labels (name, category, minutes) — already
        past the strip chokepoint, never raw screen content."""
        cf, cap = tmp_path / "count", tmp_path / "stdin"
        helper_env(_verb_helper(
            tmp_path,
            [_ok({"overview": "Built auth, then reviews.", "tags": ["auth", "review"]})],
            count_file=cf, capture=cap,
        ))
        provider = OnDeviceProvider()

        res = provider.call_day_summary(self._ROWS)

        assert res.reason is None
        assert res.value == {
            "overview": "Built auth, then reviews.", "tags": ["auth", "review"],
        }
        assert provider.last_unavailable_reason is None
        seen = json.loads(Path(str(cap) + "0").read_text())
        assert seen == {"task": "day-summary", "tasks": self._ROWS}

    def test_unavailable_reason_surfaces(self, tmp_path, helper_env):
        cf = tmp_path / "count"
        helper_env(_verb_helper(
            tmp_path, [_unavailable("respond-failed")], count_file=cf,
        ))
        provider = OnDeviceProvider()
        res = provider.call_day_summary(self._ROWS)
        assert res.value is None
        assert res.reason == "respond-failed"
        assert provider.last_unavailable_reason == "respond-failed"

    def test_malformed_tags_is_invalid_result(self, tmp_path, helper_env):
        cf = tmp_path / "count"
        helper_env(_verb_helper(
            tmp_path, [_ok({"overview": "ok", "tags": "not-a-list"})], count_file=cf,
        ))
        res = OnDeviceProvider().call_day_summary(self._ROWS)
        assert res.value is None
        assert res.reason == ondevice.REASON_BAD_RESULT


class TestVerbGenericFailures:
    """No envelope ever arrived → a generic Python-side reason; never a crash."""

    def _arbitrate(self) -> "ondevice.CallResult":
        return OnDeviceProvider().call_arbitrate(["a", "b"], stripped=True)

    def test_garbage_stdout(self, tmp_path, helper_env):
        helper_env(_write_helper(
            tmp_path, "import sys\nsys.stdin.read()\nprint('not json <<<')\n",
        ))
        res = self._arbitrate()
        assert res.value is None
        assert res.reason == ondevice.REASON_BAD_ENVELOPE

    def test_hang_maps_to_timeout(self, tmp_path, helper_env):
        helper_env(_write_helper(
            tmp_path, "import sys, time\nsys.stdin.read()\ntime.sleep(30)\n",
        ))
        res = self._arbitrate()  # helper_env shrinks the timeout to 3s
        assert res.value is None
        assert res.reason == ondevice.REASON_TIMEOUT

    def test_nonzero_exit(self, tmp_path, helper_env):
        helper_env(_write_helper(
            tmp_path, "import sys\nsys.stdin.read()\nsys.exit(3)\n",
        ))
        res = self._arbitrate()
        assert res.value is None
        assert res.reason == ondevice.REASON_NONZERO_EXIT

    def test_missing_helper(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "SCREENCAP_ONDEVICE_HELPER", str(tmp_path / "does_not_exist"),
        )
        res = self._arbitrate()
        assert res.value is None
        assert res.reason == ondevice.REASON_HELPER_MISSING


class TestRetryTaxonomy:
    """KTD-3 at this layer: decoding-failure → one fresh-spawn retry;
    rate-limited → one retry after a backoff; guardrail / refusal /
    context-window → never retried here."""

    @pytest.fixture()
    def naps(self, monkeypatch) -> list:
        recorded: list = []
        monkeypatch.setattr(ondevice, "_sleep", recorded.append)
        return recorded

    def test_decoding_failure_retried_exactly_once_then_fails(
        self, tmp_path, helper_env, naps
    ):
        cf = tmp_path / "count"
        helper_env(_verb_helper(
            tmp_path, [_unavailable("decoding-failure")], count_file=cf,
        ))
        res = OnDeviceProvider().call_name_window(_stripped_digest())
        assert res.value is None
        assert res.reason == "decoding-failure"
        assert _calls(cf) == 2  # exactly one retry
        assert naps == []  # no backoff for a decoding failure

    def test_decoding_failure_then_ok_succeeds_on_retry(self, tmp_path, helper_env):
        cf = tmp_path / "count"
        helper_env(_verb_helper(
            tmp_path,
            [_unavailable("decoding-failure"),
             _ok({"name": "Fix bug", "category": "development"})],
            count_file=cf,
        ))
        provider = OnDeviceProvider()
        res = provider.call_name_window(_stripped_digest())
        assert res.reason is None
        assert res.value == ("Fix bug", "development")
        assert provider.last_unavailable_reason is None
        assert _calls(cf) == 2

    def test_rate_limited_retried_once_after_backoff(self, tmp_path, helper_env, naps):
        cf = tmp_path / "count"
        helper_env(_verb_helper(
            tmp_path,
            [_unavailable("rate-limited"),
             _ok({"name": "Fix bug", "category": "development"})],
            count_file=cf,
        ))
        res = OnDeviceProvider().call_name_window(_stripped_digest())
        assert res.reason is None
        assert _calls(cf) == 2
        assert naps == [ondevice._RATE_LIMIT_BACKOFF_S]

    def test_persistent_rate_limit_fails_after_one_retry(
        self, tmp_path, helper_env, naps
    ):
        cf = tmp_path / "count"
        helper_env(_verb_helper(
            tmp_path, [_unavailable("rate-limited")], count_file=cf,
        ))
        res = OnDeviceProvider().call_name_window(_stripped_digest())
        assert res.reason == "rate-limited"
        assert _calls(cf) == 2
        assert naps == [ondevice._RATE_LIMIT_BACKOFF_S]  # one backoff, no more

    @pytest.mark.parametrize("reason", ["guardrail", "refusal"])
    def test_never_retried_reasons(self, tmp_path, helper_env, naps, reason):
        cf = tmp_path / "count"
        helper_env(_verb_helper(tmp_path, [_unavailable(reason)], count_file=cf))
        res = OnDeviceProvider().call_arbitrate(["a", "b"], stripped=True)
        assert res.reason == reason
        assert _calls(cf) == 1  # single attempt — never retried
        assert naps == []


class TestLegacyReasonRecording:
    """segment/answer behavior is unchanged EXCEPT the provider now records
    the unavailable reason out-of-band (KTD-1)."""

    def test_segment_records_helper_reported_reason(self, tmp_path, helper_env):
        body = (
            "import json, sys\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'status': 'unavailable', "
            "'reason': 'os-below-macos-26'}))\n"
        )
        helper_env(_write_helper(tmp_path, body))
        provider = OnDeviceProvider()
        assert provider.segment(_stripped_activity_data()) is PROVIDER_UNAVAILABLE
        assert provider.last_unavailable_reason == "os-below-macos-26"

    def test_segment_success_clears_reason(self, tmp_path, helper_env, monkeypatch):
        provider = OnDeviceProvider()
        monkeypatch.setenv("SCREENCAP_ONDEVICE_HELPER", str(tmp_path / "missing"))
        assert provider.segment(_stripped_activity_data()) is PROVIDER_UNAVAILABLE
        assert provider.last_unavailable_reason == ondevice.REASON_HELPER_MISSING

        body = (
            "import json, sys\n"
            "sys.stdin.read()\n"
            f"print(json.dumps({{'status': 'ok', 'result': {_valid_result()!r}}}))\n"
        )
        helper_env(_write_helper(tmp_path, body))
        assert isinstance(provider.segment(_stripped_activity_data()), dict)
        assert provider.last_unavailable_reason is None

    def test_segment_ran_but_no_tasks_reason_is_none(self, tmp_path, helper_env):
        """A ran-but-None outcome is NOT an unavailability — no reason."""
        body = (
            "import json, sys\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'status': 'ok', 'result': "
            "{'tasks': [], 'summary': {}, 'tags': []}}))\n"
        )
        helper_env(_write_helper(tmp_path, body))
        provider = OnDeviceProvider()
        assert provider.segment(_stripped_activity_data()) is None
        assert provider.last_unavailable_reason is None

    def test_segment_strip_gate_records_reason(self, monkeypatch):
        monkeypatch.delenv("SCREENCAP_ONDEVICE_HELPER", raising=False)
        provider = OnDeviceProvider()
        data = _stripped_activity_data()
        data["stripped"] = False
        assert provider.segment(data) is PROVIDER_UNAVAILABLE
        assert provider.last_unavailable_reason == ondevice.REASON_NOT_STRIPPED

    def test_answer_records_helper_reported_reason(self, tmp_path, helper_env):
        env = json.dumps(
            {"status": "unavailable", "reason": "model-unavailable-deviceNotEligible"}
        )
        helper_env(_write_helper(tmp_path, _answer_helper(env)))
        provider = OnDeviceProvider()
        assert provider.answer("q", _stripped_evidence()) is PROVIDER_UNAVAILABLE
        assert provider.last_unavailable_reason == "model-unavailable-deviceNotEligible"

    def test_answer_success_reason_is_none(self, tmp_path, helper_env):
        env = json.dumps({"status": "ok", "result": "You worked on auth."})
        helper_env(_write_helper(tmp_path, _answer_helper(env)))
        provider = OnDeviceProvider()
        assert "auth" in provider.answer("q", _stripped_evidence())
        assert provider.last_unavailable_reason is None


class TestChainedReasonForwarding:
    """ChainedOnDeviceProvider forwards the reason of whichever backend's
    result it returned — the same optional-attribute pattern as
    ``supports_frames`` (read via getattr, absent → None)."""

    class _Backend:
        def __init__(self, result, reason=None):
            self._result, self.last_unavailable_reason = result, reason

        def segment(self, activity_summary):
            return self._result

    class _BareBackend:  # no reason attribute at all
        def __init__(self, result):
            self._result = result

        def segment(self, activity_summary):
            return self._result

    def _chain(self, *backends):
        from screencap.segmentation.providers.chained import ChainedOnDeviceProvider

        return ChainedOnDeviceProvider(list(backends))

    def test_forwards_last_backend_reason_when_all_unavailable(self):
        chain = self._chain(
            self._Backend(PROVIDER_UNAVAILABLE, "guardrail"),
            self._Backend(PROVIDER_UNAVAILABLE, "respond-failed"),
        )
        assert chain.segment({}) is PROVIDER_UNAVAILABLE
        assert chain.last_unavailable_reason == "respond-failed"

    def test_success_forwards_none(self):
        tasks = {"tasks": [{"name": "x"}]}
        chain = self._chain(
            self._Backend(PROVIDER_UNAVAILABLE, "context-window"),
            self._Backend(tasks, None),
        )
        assert chain.segment({}) is tasks
        assert chain.last_unavailable_reason is None

    def test_backend_without_attribute_defaults_none(self):
        chain = self._chain(self._BareBackend(PROVIDER_UNAVAILABLE))
        assert chain.segment({}) is PROVIDER_UNAVAILABLE
        assert chain.last_unavailable_reason is None

    def test_empty_chain_reason_none(self):
        chain = self._chain()
        assert chain.segment({}) is PROVIDER_UNAVAILABLE
        assert chain.last_unavailable_reason is None

    def test_ran_but_none_stops_chain_and_forwards_that_backend(self):
        stale = self._Backend(None, None)
        never_reached = self._Backend(PROVIDER_UNAVAILABLE, "guardrail")
        chain = self._chain(stale, never_reached)
        assert chain.segment({}) is None
        assert chain.last_unavailable_reason is None
