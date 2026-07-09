"""Tests for the downloaded local-model backend (U2, SCR-239).

The real model (MLX / llama.cpp on a ~2 GB model) cannot run in CI, so these
drive the **Python** side against a *fake worker* — a tiny script pointed at via
``SCREENCAP_DOWNLOADED_WORKER`` with the model path faked via
``SCREENCAP_LOCAL_MODEL_PATH`` — exercising every path the real worker takes plus
the KTD3 hardening (fail-closed strip gate, allowlist env + offline, size caps,
stderr-not-logged) and the KTD12 sanitizer. Real-model behavior is covered by the
U12 eval.
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
from screencap.segmentation.providers.downloaded import DownloadedProvider


def _stripped_activity_data() -> dict:
    """Activity-data dict marked ``stripped=True`` (as U4/U8 hands it here)."""
    return {
        "stripped": True,
        "summary": {
            "duration": "1h 0m 0s",
            "timeline": [{"t": "0:00:00", "app": "VS Code", "title": "main.py"}],
        },
        "entries": [],
        "session_start": 1000.0,
        "session_end": 4600.0,
        "time_map": {"0:00:00": 1000.0, "0:30:00": 2800.0, "1:00:00": 4600.0},
    }


def _valid_result() -> dict:
    return {
        "tasks": [
            {"start_time": "0:00:00", "end_time": "0:30:00", "name": "Implement auth",
             "description": "Wrote auth.py.", "category": "development",
             "apps_used": ["VS Code"], "confidence": "high"},
            {"start_time": "0:30:00", "end_time": "1:00:00", "name": "Coordinate review",
             "description": "Pinged team.", "category": "communication",
             "apps_used": ["Slack"], "confidence": "medium"},
        ],
        "summary": {
            "overview": "Built auth then coordinated review.",
            "primary_focus": "development",
            "time_breakdown": {"development": 50, "communication": 50},
            "key_accomplishments": ["Shipped auth"],
        },
        "tags": ["python", "auth"],
    }


def _write_worker(tmp_path: Path, body: str, *, name: str = "fake_worker.py") -> Path:
    script = tmp_path / name
    script.write_text("#!" + sys.executable + "\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


@pytest.fixture()
def worker_env(monkeypatch, tmp_path):
    """Point the provider at a fresh fake worker + a faked installed model path."""
    model = tmp_path / "model.gguf"
    model.write_text("fake-weights")
    monkeypatch.setenv("SCREENCAP_LOCAL_MODEL_PATH", str(model))
    monkeypatch.setenv("SCREENCAP_DOWNLOADED_WORKER_TIMEOUT", "3")

    def _use(path: Path) -> None:
        monkeypatch.setenv("SCREENCAP_DOWNLOADED_WORKER", str(path))

    return _use


class TestHappyPath:
    def test_valid_worker_output_returns_validated_tasks(self, tmp_path, worker_env):
        body = (
            "import json, sys\n"
            "sys.stdin.read()\n"
            f"print(json.dumps({{'status': 'ok', 'result': {_valid_result()!r}}}))\n"
        )
        worker_env(_write_worker(tmp_path, body))

        result = DownloadedProvider().segment(_stripped_activity_data())

        assert isinstance(result, dict)
        assert len(result["tasks"]) == 2
        assert result["tasks"][0]["start_ts"] == 1000.0
        assert result["tasks"][0]["end_ts"] == 2800.0
        assert result["tasks"][0]["name"] == "Implement auth"
        assert result["tags"] == ["python", "auth"]

    def test_worker_receives_prompt_and_model_path(self, tmp_path, worker_env):
        capture = tmp_path / "stdin.json"
        body = (
            "import json, sys\n"
            f"open({str(capture)!r}, 'w').write(sys.stdin.read())\n"
            f"print(json.dumps({{'status': 'ok', 'result': {_valid_result()!r}}}))\n"
        )
        worker_env(_write_worker(tmp_path, body))

        DownloadedProvider().segment(_stripped_activity_data())

        seen = json.loads(capture.read_text())
        assert isinstance(seen["prompt"], str) and seen["prompt"]
        assert seen["model_path"].endswith("model.gguf")


class TestUnavailableSentinel:
    def test_no_model_installed_is_unavailable(self, tmp_path, worker_env, monkeypatch):
        monkeypatch.delenv("SCREENCAP_LOCAL_MODEL_PATH", raising=False)
        body = "import sys; sys.stdin.read(); print('{}')\n"
        worker_env(_write_worker(tmp_path, body))
        assert DownloadedProvider().segment(_stripped_activity_data()) is PROVIDER_UNAVAILABLE

    def test_missing_worker_is_unavailable(self, tmp_path, worker_env, monkeypatch):
        monkeypatch.setenv("SCREENCAP_DOWNLOADED_WORKER", str(tmp_path / "nope"))
        assert DownloadedProvider().segment(_stripped_activity_data()) is PROVIDER_UNAVAILABLE

    def test_nonzero_exit_is_unavailable(self, tmp_path, worker_env):
        worker_env(_write_worker(tmp_path, "import sys; sys.stdin.read(); sys.exit(3)\n"))
        assert DownloadedProvider().segment(_stripped_activity_data()) is PROVIDER_UNAVAILABLE

    def test_timeout_is_unavailable(self, tmp_path, worker_env):
        worker_env(_write_worker(tmp_path, "import sys, time; sys.stdin.read(); time.sleep(30)\n"))
        assert DownloadedProvider().segment(_stripped_activity_data()) is PROVIDER_UNAVAILABLE

    def test_unavailable_status_is_unavailable(self, tmp_path, worker_env):
        body = (
            "import json, sys\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'status': 'unavailable', 'reason': 'no-usable-output'}))\n"
        )
        worker_env(_write_worker(tmp_path, body))
        assert DownloadedProvider().segment(_stripped_activity_data()) is PROVIDER_UNAVAILABLE

    def test_garbage_stdout_is_unavailable(self, tmp_path, worker_env):
        worker_env(_write_worker(tmp_path, "import sys; sys.stdin.read(); print('not json <<<')\n"))
        assert DownloadedProvider().segment(_stripped_activity_data()) is PROVIDER_UNAVAILABLE

    def test_empty_stdout_is_unavailable(self, tmp_path, worker_env):
        worker_env(_write_worker(tmp_path, "import sys; sys.stdin.read()\n"))
        assert DownloadedProvider().segment(_stripped_activity_data()) is PROVIDER_UNAVAILABLE

    def test_ok_without_result_is_unavailable(self, tmp_path, worker_env):
        body = "import json, sys; sys.stdin.read(); print(json.dumps({'status': 'ok'}))\n"
        worker_env(_write_worker(tmp_path, body))
        assert DownloadedProvider().segment(_stripped_activity_data()) is PROVIDER_UNAVAILABLE


class TestValidatorSeam:
    def test_repairable_output_is_repaired(self, tmp_path, worker_env):
        result_body = _valid_result()
        result_body["tasks"][0]["category"] = "not-a-real-category"
        body = (
            "import json, sys\n"
            "sys.stdin.read()\n"
            f"print(json.dumps({{'status': 'ok', 'result': {result_body!r}}}))\n"
        )
        worker_env(_write_worker(tmp_path, body))
        result = DownloadedProvider().segment(_stripped_activity_data())
        assert isinstance(result, dict)
        assert result["tasks"][0]["category"] == "other"

    def test_empty_tasks_result_is_none_not_sentinel(self, tmp_path, worker_env):
        body = (
            "import json, sys\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'status': 'ok', 'result': "
            "{'tasks': [], 'summary': {}, 'tags': []}}))\n"
        )
        worker_env(_write_worker(tmp_path, body))
        result = DownloadedProvider().segment(_stripped_activity_data())
        assert result is None
        assert result is not PROVIDER_UNAVAILABLE

    def test_malformed_task_item_does_not_crash(self, tmp_path, worker_env):
        body = (
            "import json, sys\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'status': 'ok', 'result': {'tasks': [42]}}))\n"
        )
        worker_env(_write_worker(tmp_path, body))
        assert DownloadedProvider().segment(_stripped_activity_data()) is None


@pytest.mark.privacy
class TestFailClosed:
    def test_unmarked_summary_refused_without_spawning(self, tmp_path, worker_env):
        touched = tmp_path / "ran.flag"
        body = (
            "import json, sys\n"
            "sys.stdin.read()\n"
            f"open({str(touched)!r}, 'w').write('ran')\n"
            f"print(json.dumps({{'status': 'ok', 'result': {_valid_result()!r}}}))\n"
        )
        worker_env(_write_worker(tmp_path, body))

        data = _stripped_activity_data()
        del data["stripped"]
        assert DownloadedProvider().segment(data) is PROVIDER_UNAVAILABLE
        assert not touched.exists(), "worker must not run on unmarked input"

    def test_stripped_false_or_truthy_non_true_refused(self, tmp_path, worker_env):
        # Only the exact `stripped is True` marker passes (the provider gate is
        # the boundary; a forged/mutated non-True value fails closed).
        body = (
            "import json, sys\n"
            "sys.stdin.read()\n"
            f"print(json.dumps({{'status': 'ok', 'result': {_valid_result()!r}}}))\n"
        )
        worker_env(_write_worker(tmp_path, body))
        data = _stripped_activity_data()
        data["stripped"] = False
        assert DownloadedProvider().segment(data) is PROVIDER_UNAVAILABLE
        data["stripped"] = "yes"  # truthy but not the marker
        assert DownloadedProvider().segment(data) is PROVIDER_UNAVAILABLE


@pytest.mark.privacy
class TestWorkerHardening:
    def test_worker_env_excludes_secrets_and_forces_offline(
        self, tmp_path, worker_env, monkeypatch
    ):
        monkeypatch.setenv("GOOGLE_GENAI_API_KEY", "super-secret")
        monkeypatch.setenv("SCREENCAP_ENGINE_TOKEN_FILE", "/tmp/token")
        capture = tmp_path / "env.json"
        body = (
            "import json, os, sys\n"
            "sys.stdin.read()\n"
            f"open({str(capture)!r}, 'w').write(json.dumps(dict(os.environ)))\n"
            f"print(json.dumps({{'status': 'ok', 'result': {_valid_result()!r}}}))\n"
        )
        worker_env(_write_worker(tmp_path, body))

        DownloadedProvider().segment(_stripped_activity_data())

        seen = json.loads(capture.read_text())
        assert "GOOGLE_GENAI_API_KEY" not in seen
        assert "SCREENCAP_ENGINE_TOKEN_FILE" not in seen
        assert not any(
            m in k.upper() for k in seen for m in ("KEY", "TOKEN", "SECRET")
        )
        assert seen.get("HF_HUB_OFFLINE") == "1"
        assert seen.get("TRANSFORMERS_OFFLINE") == "1"

    def test_oversized_stdin_rejected_before_spawn(self, tmp_path, worker_env):
        touched = tmp_path / "ran.flag"
        body = (
            "import sys\n"
            f"open({str(touched)!r}, 'w').write('ran'); sys.stdin.read()\n"
        )
        worker_env(_write_worker(tmp_path, body))
        data = _stripped_activity_data()
        # A summary far beyond the stdin cap.
        data["summary"]["timeline"] = [{"t": "0:00:00", "title": "x" * 1000}] * 2000
        assert DownloadedProvider().segment(data) is PROVIDER_UNAVAILABLE
        assert not touched.exists(), "oversized input must be rejected before spawn"

    def test_oversized_stdout_is_unavailable(self, tmp_path, worker_env):
        # Worker emits a valid envelope but far larger than the stdout cap.
        body = (
            "import sys\n"
            "sys.stdin.read()\n"
            "sys.stdout.write('{\"status\": \"ok\", \"result\": {\"pad\": \"' "
            "+ 'x' * (2 * 1024 * 1024) + '\"}}')\n"
        )
        worker_env(_write_worker(tmp_path, body))
        assert DownloadedProvider().segment(_stripped_activity_data()) is PROVIDER_UNAVAILABLE


@pytest.mark.privacy
class TestUntrustedOutputSanitized:
    def test_injected_markup_and_control_chars_stripped(self, tmp_path, worker_env):
        result_body = _valid_result()
        result_body["tasks"][0]["name"] = "Fix login <script>alert(1)</script>\x07"
        result_body["tasks"][0]["description"] = "See <b>bold</b>\x00 stuff"
        body = (
            "import json, sys\n"
            "sys.stdin.read()\n"
            f"print(json.dumps({{'status': 'ok', 'result': {result_body!r}}}))\n"
        )
        worker_env(_write_worker(tmp_path, body))

        result = DownloadedProvider().segment(_stripped_activity_data())
        name = result["tasks"][0]["name"]
        assert "<script>" not in name and "\x07" not in name
        assert name == "Fix login alert(1)"
        assert "<b>" not in result["tasks"][0]["description"]
        assert "\x00" not in result["tasks"][0]["description"]


class TestFactoryAndImport:
    def test_downloaded_maps_to_provider(self):
        provider = get_provider("downloaded")
        assert isinstance(provider, DownloadedProvider)
        assert isinstance(provider, LLMProvider)

    def test_module_is_import_light(self):
        code = (
            "import sys; "
            "import screencap.segmentation.providers.downloaded; "
            "assert 'mlx_lm' not in sys.modules; "
            "assert 'llama_cpp' not in sys.modules; "
            "assert 'google.genai' not in sys.modules; "
            "print('OK')"
        )
        import subprocess

        env = dict(os.environ)
        src_root = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "src",
        )
        env["PYTHONPATH"] = src_root + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, env=env
        )
        assert proc.returncode == 0, proc.stderr
        assert "OK" in proc.stdout


# ===========================================================================
# Free-form answer path (SCR-243, U10) — fake worker emits a TEXT envelope.
# ===========================================================================

from screencap.segmentation.generation import Evidence  # noqa: E402


def _ev(text: str = "you edited main.py", stripped: bool = True) -> Evidence:
    return Evidence(text=text, stripped=stripped)


class TestAnswer:
    def test_text_envelope_returns_sanitized(self, tmp_path, worker_env):
        body = (
            "import sys\nsys.stdin.read()\n"
            "print('{\"status\":\"ok\",\"result\":\"You edited <b>main.py</b>.\"}')\n"
        )
        worker_env(_write_worker(tmp_path, body))
        assert DownloadedProvider().answer("q", _ev()) == "You edited main.py."

    def test_worker_receives_generate_text_mode(self, tmp_path, worker_env):
        capture = tmp_path / "stdin.json"
        body = (
            "import sys\n"
            f"open({str(capture)!r}, 'w').write(sys.stdin.read())\n"
            "print('{\"status\":\"ok\",\"result\":\"ok\"}')\n"
        )
        worker_env(_write_worker(tmp_path, body))
        DownloadedProvider().answer("q", _ev())
        seen = json.loads(capture.read_text())
        assert seen["mode"] == "generate_text"
        assert isinstance(seen["prompt"], str) and seen["prompt"]

    @pytest.mark.privacy
    def test_unmarked_evidence_refused_without_spawn(self, tmp_path, worker_env):
        sentinel = tmp_path / "spawned"
        body = (
            "import pathlib, sys\n"
            f"pathlib.Path({str(sentinel)!r}).write_text('x')\n"
            "sys.stdin.read()\nprint('{\"status\":\"ok\",\"result\":\"x\"}')\n"
        )
        worker_env(_write_worker(tmp_path, body))
        assert DownloadedProvider().answer("q", _ev(stripped=False)) is PROVIDER_UNAVAILABLE
        assert not sentinel.exists()

    def test_no_model_installed_unavailable(self, tmp_path, worker_env, monkeypatch):
        monkeypatch.delenv("SCREENCAP_LOCAL_MODEL_PATH", raising=False)
        body = "import sys; sys.stdin.read(); print('{\"status\":\"ok\",\"result\":\"x\"}')\n"
        worker_env(_write_worker(tmp_path, body))
        assert DownloadedProvider().answer("q", _ev()) is PROVIDER_UNAVAILABLE

    def test_nonzero_exit_unavailable(self, tmp_path, worker_env):
        worker_env(_write_worker(tmp_path, "import sys; sys.stdin.read(); sys.exit(2)\n"))
        assert DownloadedProvider().answer("q", _ev()) is PROVIDER_UNAVAILABLE

    def test_unavailable_envelope(self, tmp_path, worker_env):
        body = "import sys; sys.stdin.read(); print('{\"status\":\"unavailable\",\"reason\":\"x\"}')\n"
        worker_env(_write_worker(tmp_path, body))
        assert DownloadedProvider().answer("q", _ev()) is PROVIDER_UNAVAILABLE

    def test_non_string_result_unavailable(self, tmp_path, worker_env):
        body = "import sys; sys.stdin.read(); print('{\"status\":\"ok\",\"result\":{\"a\":1}}')\n"
        worker_env(_write_worker(tmp_path, body))
        assert DownloadedProvider().answer("q", _ev()) is PROVIDER_UNAVAILABLE

    def test_empty_result_unavailable(self, tmp_path, worker_env):
        body = "import sys; sys.stdin.read(); print('{\"status\":\"ok\",\"result\":\"   \"}')\n"
        worker_env(_write_worker(tmp_path, body))
        assert DownloadedProvider().answer("q", _ev()) is PROVIDER_UNAVAILABLE
