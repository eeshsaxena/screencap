"""CLI-delegation backend (BYO cloud, U4).

Drives :class:`~screencap.segmentation.providers.cli_delegate.CliDelegateProvider`
against a **fake CLI** — never the real ``codex`` / ``claude`` / ``gemini`` — via
two seams:

* the injectable ``run_cli`` seam for the parse / validate / error-path shapes
  (no subprocess spawned);
* a real temp-file fake-CLI executable + monkeypatched binary resolution for the
  behaviors that must exercise the real ``_default_run_cli`` (scrubbed env,
  binary-path resolution, stderr-not-logged-verbatim).

Privacy-marked, Vision-free tests (`@pytest.mark.privacy`, CI's only lane):
- the subprocess is spawned with a scrubbed env (no ``*_KEY`` / ``*_SECRET``);
- the backend is handed only text (no frame bytes reach the CLI).
"""

from __future__ import annotations

import json
import logging
import os
import stat
import time

import pytest

from screencap.segmentation.generation import Evidence
from screencap.segmentation.provider import (
    PROVIDER_UNAVAILABLE,
    LLMProvider,
    get_provider,
)
from screencap.segmentation.providers import cli_delegate
from screencap.segmentation.providers.cli_delegate import (
    VENDOR_IDS,
    CliDelegateProvider,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _stripped() -> dict:
    """A privacy-stripped activity summary as the local segmentation stage marks it."""
    return {
        "stripped": True,
        "summary": {"timeline": [{"t": "0:00:00", "app": "VS Code"}]},
        "session_start": 1000.0,
        "session_end": 4600.0,
        "time_map": {"0:00:00": 1000.0, "0:30:00": 2800.0},
    }


def _raw_tasks() -> dict:
    """A well-formed raw tasks object (relative timestamps, pre-validation)."""
    return {
        "tasks": [
            {"start_time": "0:00:00", "end_time": "0:30:00", "name": "Fix login",
             "description": "d", "category": "development", "apps_used": ["VS Code"],
             "confidence": "high"},
        ],
        "summary": {"overview": "o", "primary_focus": "development",
                    "time_breakdown": {}, "key_accomplishments": []},
        "tags": ["python"],
    }


def _ev(text: str = "You edited main.py.", stripped: bool = True) -> Evidence:
    return Evidence(text=text, stripped=stripped)


def _cli_stdout(vendor: str, payload: str) -> str:
    """Wrap ``payload`` in the vendor's on-the-wire stdout shape.

    - openai-cli (plain ``codex exec``): the final message is printed as raw text.
    - anthropic-cli: ``{"result": ...}`` JSON envelope.
    - gemini-cli: ``{"response": ...}`` JSON envelope.
    """
    if vendor == "openai-cli":
        return payload
    if vendor == "anthropic-cli":
        return json.dumps({"result": payload})
    return json.dumps({"response": payload})


# ---------------------------------------------------------------------------
# segment — happy path + error paths (run_cli seam, no subprocess)
# ---------------------------------------------------------------------------


def _pin_binary(monkeypatch, path) -> None:
    """Pin :func:`cli_delegate._resolve_binary` to ``path`` for all vendors.

    Applied via ``monkeypatch`` so it's restored after each test — the tests that
    use the ``run_cli`` seam only need a *resolvable* binary before the seam is
    reached; the actual path value is irrelevant since no spawn happens.
    """
    monkeypatch.setattr(cli_delegate, "_resolve_binary", lambda spec: path)


@pytest.mark.parametrize("vendor", VENDOR_IDS)
class TestSegmentHappyPath:
    def test_valid_output_yields_validated_tasks(self, vendor, monkeypatch):
        stdout = _cli_stdout(vendor, json.dumps(_raw_tasks()))
        _pin_binary(monkeypatch, "/fake/bin")
        p = CliDelegateProvider(vendor, run_cli=lambda binary, prompt: stdout)
        result = p.segment(_stripped())
        assert isinstance(result, dict)
        assert result["tasks"][0]["name"] == "Fix login"
        # Validator converted the relative timestamp → Unix.
        assert result["tasks"][0]["start_ts"] == 1000.0

    def test_json_fenced_output_is_unwrapped(self, vendor, monkeypatch):
        fenced = "```json\n" + json.dumps(_raw_tasks()) + "\n```"
        stdout = _cli_stdout(vendor, fenced)
        _pin_binary(monkeypatch, "/fake/bin")
        p = CliDelegateProvider(vendor, run_cli=lambda binary, prompt: stdout)
        result = p.segment(_stripped())
        assert isinstance(result, dict)
        assert result["tasks"][0]["name"] == "Fix login"


@pytest.mark.parametrize("vendor", VENDOR_IDS)
class TestSegmentErrorPaths:
    def test_unavailable_from_run_cli_propagates(self, vendor, monkeypatch):
        _pin_binary(monkeypatch, "/fake/bin")
        p = CliDelegateProvider(vendor, run_cli=lambda binary, prompt: PROVIDER_UNAVAILABLE)
        assert p.segment(_stripped()) is PROVIDER_UNAVAILABLE

    def test_unparseable_stdout_is_unavailable(self, vendor, monkeypatch):
        stdout = _cli_stdout(vendor, "this is not json at all {{{")
        _pin_binary(monkeypatch, "/fake/bin")
        p = CliDelegateProvider(vendor, run_cli=lambda binary, prompt: stdout)
        assert p.segment(_stripped()) is PROVIDER_UNAVAILABLE

    def test_empty_stdout_is_unavailable(self, vendor, monkeypatch):
        _pin_binary(monkeypatch, "/fake/bin")
        p = CliDelegateProvider(vendor, run_cli=lambda binary, prompt: "")
        assert p.segment(_stripped()) is PROVIDER_UNAVAILABLE

    def test_binary_absent_is_unavailable_without_spawn(self, vendor, monkeypatch):
        calls: list = []
        # No configured path, and shutil.which finds nothing.
        _pin_binary(monkeypatch, None)
        p = CliDelegateProvider(
            vendor, run_cli=lambda binary, prompt: (calls.append(binary), "x")[1]
        )
        assert p.segment(_stripped()) is PROVIDER_UNAVAILABLE
        assert calls == []  # never reached the CLI

    def test_unmarked_summary_refused_without_spawn(self, vendor, monkeypatch):
        calls: list = []
        _pin_binary(monkeypatch, "/fake/bin")
        p = CliDelegateProvider(
            vendor, run_cli=lambda binary, prompt: (calls.append(binary), "x")[1]
        )
        summary = _stripped()
        summary["stripped"] = False
        assert p.segment(summary) is PROVIDER_UNAVAILABLE
        assert calls == []  # fail-closed: no CLI spawn on unmarked input


# ---------------------------------------------------------------------------
# answer — happy path + error paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vendor", VENDOR_IDS)
class TestAnswer:
    def test_returns_sanitized_text(self, vendor, monkeypatch):
        stdout = _cli_stdout(vendor, "You edited <i>main.py</i>.")
        _pin_binary(monkeypatch, "/fake/bin")
        p = CliDelegateProvider(vendor, run_cli=lambda binary, prompt: stdout)
        out = p.answer("what did I do?", _ev())
        assert isinstance(out, str)
        assert "<" not in out and ">" not in out  # markup neutralized (KTD10)
        assert "main.py" in out

    def test_unavailable_from_run_cli(self, vendor, monkeypatch):
        _pin_binary(monkeypatch, "/fake/bin")
        p = CliDelegateProvider(vendor, run_cli=lambda binary, prompt: PROVIDER_UNAVAILABLE)
        assert p.answer("q", _ev()) is PROVIDER_UNAVAILABLE

    def test_empty_answer_is_unavailable(self, vendor, monkeypatch):
        stdout = _cli_stdout(vendor, "   ")
        _pin_binary(monkeypatch, "/fake/bin")
        p = CliDelegateProvider(vendor, run_cli=lambda binary, prompt: stdout)
        assert p.answer("q", _ev()) is PROVIDER_UNAVAILABLE

    @pytest.mark.privacy
    def test_unmarked_evidence_refused_without_spawn(self, vendor, monkeypatch):
        calls: list = []
        _pin_binary(monkeypatch, "/fake/bin")
        p = CliDelegateProvider(
            vendor, run_cli=lambda binary, prompt: (calls.append(binary), "x")[1]
        )
        assert p.answer("q", _ev(stripped=False)) is PROVIDER_UNAVAILABLE
        assert calls == []  # no CLI spawn on unmarked evidence


# ---------------------------------------------------------------------------
# available() — existence/stat only, never reads the auth files (KTD1)
# ---------------------------------------------------------------------------


class TestAvailable:
    def test_false_when_binary_absent(self, monkeypatch, tmp_path):
        p = CliDelegateProvider("openai-cli")
        # An auth artifact exists, but the binary does not.
        artifact = tmp_path / "auth.json"
        artifact.write_text("{}")
        p._spec.auth_artifacts = (artifact,)
        monkeypatch.setattr(cli_delegate.shutil, "which", lambda name: None)
        monkeypatch.delenv("SCREENCAP_OPENAI_CLI_PATH", raising=False)
        assert p.available() is False

    def test_false_when_auth_artifact_missing(self, monkeypatch, tmp_path):
        p = CliDelegateProvider("openai-cli")
        # The binary resolves, but no auth artifact exists.
        p._spec.auth_artifacts = (tmp_path / "does-not-exist.json",)
        fake_bin = tmp_path / "codex"
        fake_bin.write_text("#!/bin/sh\n")
        fake_bin.chmod(0o755)
        monkeypatch.setattr(cli_delegate.shutil, "which", lambda name: str(fake_bin))
        monkeypatch.delenv("SCREENCAP_OPENAI_CLI_PATH", raising=False)
        assert p.available() is False

    def test_true_when_binary_and_auth_present(self, monkeypatch, tmp_path):
        p = CliDelegateProvider("openai-cli")
        artifact = tmp_path / "auth.json"
        artifact.write_text("{}")
        p._spec.auth_artifacts = (artifact,)
        fake_bin = tmp_path / "codex"
        fake_bin.write_text("#!/bin/sh\n")
        fake_bin.chmod(0o755)
        monkeypatch.setattr(cli_delegate.shutil, "which", lambda name: str(fake_bin))
        monkeypatch.delenv("SCREENCAP_OPENAI_CLI_PATH", raising=False)
        assert p.available() is True

    def test_available_stats_but_never_reads_auth_file(self, monkeypatch, tmp_path):
        """Detection must ``stat`` the auth artifact, never ``open`` it (KTD1)."""
        p = CliDelegateProvider("anthropic-cli")
        artifact = tmp_path / ".credentials.json"
        artifact.write_text('{"secret":"should-never-be-read"}')
        p._spec.auth_artifacts = (artifact,)
        fake_bin = tmp_path / "claude"
        fake_bin.write_text("#!/bin/sh\n")
        fake_bin.chmod(0o755)
        monkeypatch.setattr(cli_delegate.shutil, "which", lambda name: str(fake_bin))
        monkeypatch.delenv("SCREENCAP_ANTHROPIC_CLI_PATH", raising=False)

        # Trip-wire: any attempt to open the auth artifact fails the test.
        real_open = open

        def _guard_open(file, *args, **kwargs):  # noqa: ANN001
            if str(file) == str(artifact):
                raise AssertionError("available() opened the vendor auth file (KTD1)")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr("builtins.open", _guard_open)
        # Also guard the low-level Path.read_* that would bypass builtins.open.
        from pathlib import Path as _P

        def _guard_read_text(self, *a, **k):  # noqa: ANN001
            if str(self) == str(artifact):
                raise AssertionError("available() read the vendor auth file (KTD1)")
            return _P.read_bytes(self).decode()

        monkeypatch.setattr(_P, "read_text", _guard_read_text)
        assert p.available() is True  # decided by stat alone


# ---------------------------------------------------------------------------
# Binary-path resolution — configured override wins over PATH
# ---------------------------------------------------------------------------


class TestBinaryResolution:
    def test_configured_path_beats_path_probe(self, monkeypatch, tmp_path):
        configured = tmp_path / "my-codex"
        configured.write_text("#!/bin/sh\n")
        configured.chmod(0o755)

        # A DIFFERENT binary is on PATH — the configured override must win.
        monkeypatch.setattr(
            cli_delegate.shutil, "which", lambda name: "/usr/bin/codex-on-path"
        )
        monkeypatch.setenv("SCREENCAP_OPENAI_CLI_PATH", str(configured))
        spec = cli_delegate._vendor_specs()["openai-cli"]
        assert cli_delegate._resolve_binary(spec) == str(configured)

    def test_falls_back_to_path_when_no_override(self, monkeypatch):
        monkeypatch.delenv("SCREENCAP_OPENAI_CLI_PATH", raising=False)
        monkeypatch.setattr(
            cli_delegate.shutil, "which", lambda name: "/usr/local/bin/codex"
        )
        spec = cli_delegate._vendor_specs()["openai-cli"]
        assert cli_delegate._resolve_binary(spec) == "/usr/local/bin/codex"

    def test_configured_non_executable_is_rejected(self, monkeypatch, tmp_path):
        # A configured path that isn't executable is not silently ignored in favor
        # of PATH — it returns None (the operator pointed at the wrong thing).
        not_exec = tmp_path / "not-exec"
        not_exec.write_text("x")
        not_exec.chmod(0o644)
        monkeypatch.setenv("SCREENCAP_OPENAI_CLI_PATH", str(not_exec))
        monkeypatch.setattr(cli_delegate.shutil, "which", lambda name: "/usr/bin/codex")
        spec = cli_delegate._vendor_specs()["openai-cli"]
        assert cli_delegate._resolve_binary(spec) is None


# ---------------------------------------------------------------------------
# Real subprocess via a fake CLI — scrubbed env, stderr hygiene (privacy lane)
# ---------------------------------------------------------------------------


def _write_fake_cli(tmp_path, script_body: str):
    """Write an executable fake-CLI script and return its path."""
    path = tmp_path / "fake-cli"
    path.write_text("#!/usr/bin/env python3\n" + script_body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IRUSR)
    return path


@pytest.mark.privacy
class TestScrubbedEnv:
    """The child process must not inherit the daemon's secrets (KTD5)."""

    def test_secret_env_absent_from_child(self, monkeypatch, tmp_path):
        # A fake CLI that dumps whichever secret-ish vars it can see to stdout.
        fake = _write_fake_cli(
            tmp_path,
            "import os, json\n"
            "leaked = {k: v for k, v in os.environ.items() "
            "if any(m in k.upper() for m in ('KEY','TOKEN','SECRET'))}\n"
            "print(json.dumps({'result': json.dumps(leaked), 'response': json.dumps(leaked)}))\n",
        )
        # Inject secrets into the PARENT env the provider inherits from.
        monkeypatch.setenv("OPENAI_API_KEY", "sk-parent-should-not-leak")
        monkeypatch.setenv("SCREENCAP_SECRET", "top-secret")
        monkeypatch.setenv("FIREBASE_TOKEN", "fb-token")

        # anthropic-cli emits a JSON {"result": ...} envelope we can read back.
        p = CliDelegateProvider("anthropic-cli")
        monkeypatch.setenv("SCREENCAP_ANTHROPIC_CLI_PATH", str(fake))

        out = p.answer("what leaked?", _ev())
        assert isinstance(out, str)
        leaked = json.loads(out)
        assert leaked == {}, f"secrets leaked to the child env: {leaked}"

    def test_non_secret_env_still_passed(self, monkeypatch, tmp_path):
        # A benign var (no secret marker) is still inherited — the scrub is a
        # denylist, not a wipe.
        fake = _write_fake_cli(
            tmp_path,
            "import os, json\n"
            "val = os.environ.get('SCREENCAP_BENIGN_MARKER', '')\n"
            "print(json.dumps({'result': val}))\n",
        )
        monkeypatch.setenv("SCREENCAP_BENIGN_MARKER", "present")
        p = CliDelegateProvider("anthropic-cli")
        monkeypatch.setenv("SCREENCAP_ANTHROPIC_CLI_PATH", str(fake))
        assert p.answer("q", _ev()) == "present"


@pytest.mark.privacy
class TestNoFrameBytes:
    """The CLI is handed only text — no frame bytes reach the subprocess (KTD4)."""

    def test_only_text_reaches_the_cli(self, monkeypatch, tmp_path):
        # A fake CLI that echoes back what it received on stdin + argv, so the test
        # can assert only the (text) evidence appears — no bytes/binary payload.
        fake = _write_fake_cli(
            tmp_path,
            "import sys, json\n"
            "seen = {'stdin': sys.stdin.read(), 'argv': sys.argv[1:]}\n"
            "print(json.dumps({'result': json.dumps(seen)}))\n",
        )
        p = CliDelegateProvider("anthropic-cli")
        monkeypatch.setenv("SCREENCAP_ANTHROPIC_CLI_PATH", str(fake))
        out = p.answer("what did I do?", _ev(text="SENTINEL-EVIDENCE-TEXT"))
        seen = json.loads(out)
        # The evidence text is present; nothing binary rode along.
        assert "SENTINEL-EVIDENCE-TEXT" in seen["stdin"]
        # claude reads the prompt on stdin — argv carries only flags, no payload.
        assert all("SENTINEL-EVIDENCE-TEXT" not in a for a in seen["argv"])


@pytest.mark.privacy
class TestStderrHygiene:
    """A non-zero exit must not log the child's stderr verbatim (KTD5)."""

    def test_stderr_not_logged_verbatim_on_failure(self, monkeypatch, tmp_path, caplog):
        secret_marker = "RECORDING-DERIVED-SECRET-IN-STDERR"
        fake = _write_fake_cli(
            tmp_path,
            "import sys\n"
            f"sys.stderr.write({secret_marker!r})\n"
            "sys.exit(3)\n",
        )
        p = CliDelegateProvider("anthropic-cli")
        monkeypatch.setenv("SCREENCAP_ANTHROPIC_CLI_PATH", str(fake))
        with caplog.at_level(logging.WARNING):
            assert p.answer("q", _ev()) is PROVIDER_UNAVAILABLE
        combined = " ".join(r.getMessage() for r in caplog.records)
        assert secret_marker not in combined, "stderr was logged verbatim (KTD5)"
        # The class of failure (the exit code) IS logged so the failure is debuggable.
        assert "exited 3" in combined


class TestRealSubprocessTimeout:
    """A hanging CLI degrades to unavailable, never a hang (KTD5)."""

    def test_timeout_is_unavailable(self, monkeypatch, tmp_path):
        fake = _write_fake_cli(tmp_path, "import time\ntime.sleep(30)\n")
        p = CliDelegateProvider("anthropic-cli")
        monkeypatch.setenv("SCREENCAP_ANTHROPIC_CLI_PATH", str(fake))
        monkeypatch.setenv(cli_delegate._TIMEOUT_ENV, "0.3")
        assert p.answer("q", _ev()) is PROVIDER_UNAVAILABLE


@pytest.mark.privacy
class TestRealSubprocessDoSGuards:
    """The REAL ``_default_run_cli`` path enforces the byte cap + reaps the whole
    process group — a flooding or hanging (grandchild-spawning) CLI can't drive
    daemon memory pressure or leak an orphaned process (FIX4/FIX6 DoS guards)."""

    def test_stdout_flood_is_unavailable_and_bounded(self, monkeypatch, tmp_path):
        # A fake CLI that writes far past the (lowered) byte cap, forever. The
        # provider must kill it and return unavailable QUICKLY, holding only a
        # bounded buffer — never accumulating the whole flood.
        fake = _write_fake_cli(
            tmp_path,
            "import sys, os\n"
            "buf = b'x' * 65536\n"
            "while True:\n"
            "    try:\n"
            "        os.write(1, buf)\n"
            "    except OSError:\n"
            "        break\n",
        )
        p = CliDelegateProvider("anthropic-cli")
        monkeypatch.setenv("SCREENCAP_ANTHROPIC_CLI_PATH", str(fake))
        # Lower the cap so the test floods past it in a handful of chunks.
        monkeypatch.setattr(cli_delegate, "_MAX_STDOUT_BYTES", 256 * 1024)
        monkeypatch.setenv(cli_delegate._TIMEOUT_ENV, "10")  # cap should trip first

        started = time.monotonic()
        assert p.answer("q", _ev()) is PROVIDER_UNAVAILABLE
        elapsed = time.monotonic() - started
        # The byte cap (not the 10s timeout) tripped: a quick, bounded return.
        assert elapsed < 5.0, f"flood was not bounded quickly (took {elapsed:.1f}s)"

    def test_timeout_reaps_child_and_grandchild(self, monkeypatch, tmp_path):
        # A fake CLI that spawns a grandchild (a sleeper), records both PIDs, then
        # hangs. On timeout the provider SIGKILLs the whole session, so BOTH the
        # direct child and the grandchild must be gone (os.killpg reaps the tree,
        # not just the direct child).
        pid_file = tmp_path / "pids.txt"
        fake = _write_fake_cli(
            tmp_path,
            "import os, sys, subprocess, time\n"
            # Grandchild: an independent sleeper in the same session/group.
            "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            f"open({str(pid_file)!r}, 'w').write(str(os.getpid()) + ' ' + str(g.pid))\n"
            "time.sleep(120)\n",
        )
        p = CliDelegateProvider("anthropic-cli")
        monkeypatch.setenv("SCREENCAP_ANTHROPIC_CLI_PATH", str(fake))
        monkeypatch.setenv(cli_delegate._TIMEOUT_ENV, "0.5")

        assert p.answer("q", _ev()) is PROVIDER_UNAVAILABLE

        # Wait for the pid file (the fake writes it before its own sleep).
        deadline = time.monotonic() + 3.0
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_file.exists(), "fake CLI never recorded its PIDs"
        child_pid, grandchild_pid = (int(x) for x in pid_file.read_text().split())

        # Give the group-kill a moment to land, then assert BOTH are dead.
        def _alive(pid: int) -> bool:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                return True  # exists but not signalable by us
            return True

        gone_deadline = time.monotonic() + 5.0
        while time.monotonic() < gone_deadline and (
            _alive(child_pid) or _alive(grandchild_pid)
        ):
            time.sleep(0.05)
        assert not _alive(child_pid), "direct child was not reaped on timeout"
        assert not _alive(grandchild_pid), (
            "grandchild was not reaped — os.killpg must signal the whole group"
        )


# ---------------------------------------------------------------------------
# Factory registration
# ---------------------------------------------------------------------------


class TestFactory:
    @pytest.mark.parametrize("vendor", VENDOR_IDS)
    def test_get_provider_maps_to_cli_delegate(self, vendor):
        provider = get_provider(vendor)
        assert isinstance(provider, CliDelegateProvider)
        assert provider.vendor == vendor
        assert isinstance(provider, LLMProvider)

    def test_unknown_vendor_construction_rejected(self):
        with pytest.raises(ValueError):
            CliDelegateProvider("bogus-cli")
