"""Shared daemon test helpers."""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
import textwrap
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_permission_probe: run the real TCC probe instead of the "
        "granted-by-default stub (probe-internals + live-integration tests).",
    )


@pytest.fixture(autouse=True)
def _isolate_recordings_and_run_dir(tmp_path, monkeypatch):
    """Hermetically isolate the recordings dir + terminal-stage flock dir.

    SCR-125 U6 adds a daemon-startup sweep (scans ``get_recordings_dir()``) and a
    per-exit terminal-stage resume (takes the ``~/.screencap/run`` flock). Without
    isolation those would touch the developer's REAL recordings / run dir during
    the test suite. Pointing both at a per-test tmp dir keeps the daemon tests
    hermetic (the sweep finds an empty dir → no-op; the flock lives in tmp)."""
    import screencap.config as cfg
    import screencap.terminal_stage as ts

    isolated = tmp_path / "recordings-isolated"
    isolated.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(isolated))
    monkeypatch.setattr(ts, "_RUN_DIR", tmp_path / "ts-run")
    # Paid-only launch (SCR): both client paywall flags now DEFAULT ON in
    # config.py. Daemon tests are not billing tests, so force both OFF for the
    # suite — the env var short-circuits the config read, so the default flip
    # cannot 402 the read-only recall/search verbs or gate recording.start.
    # Tests that DO exercise the gates override this by monkeypatching the config
    # fn (subscription-gate tests) or setting the env themselves;
    # ``test_config_isolation`` clears the env to exercise the config-file read.
    monkeypatch.setenv("SCREENCAP_STRIPE_PAYWALL", "0")
    monkeypatch.setenv("SCREENCAP_LOCAL_PAYWALL_ENFORCE", "0")
    # U14: the daemon's whoami / entitlement.refresh verbs now reconcile an
    # on-disk entitlement lease under ``get_base_dir()/run/``. Point the base dir
    # at a per-test tmp so those writes never touch the developer's real
    # ``~/.screencap/run/`` (mirrors the recordings isolation above).
    base = tmp_path / "base-isolated"
    base.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cfg, "_DEFAULT_BASE", base)
    # ``_CONFIG_PATH`` is bound at import time from the ORIGINAL ``_DEFAULT_BASE``,
    # so patching ``_DEFAULT_BASE`` above does NOT redirect ``config.toml`` reads:
    # ``_load_toml()`` still resolves the developer's real ``~/.screencap/config.toml``
    # and caches it. A developer testing the paywall (``local_paywall_enforce = true``)
    # would leak that flag into the daemon app, 402-ing the read-only recall/search
    # verb tests (dogfood 2026-07-11). Redirect the path at the isolated (empty) base
    # and reset the module-level cache on both sides so neither the leak-in nor the
    # isolated ``{}`` persists across tests.
    monkeypatch.setattr(cfg, "_CONFIG_PATH", base / "config.toml")
    # SCR-236 U3: also isolate the container-aware data root. With
    # SCREENCAP_RECORDINGS_DIR set above, ``get_data_root()`` bypasses the
    # container and resolves to ``get_base_dir()`` (the isolated ``base`` here), so
    # the content-index / backfill sidecars land in tmp. Explicitly clear any
    # inherited SCREENCAP_CONTAINER_ENABLED so a developer's env can't flip daemon
    # tests into container-path resolution mid-suite.
    monkeypatch.delenv("SCREENCAP_CONTAINER_ENABLED", raising=False)
    cfg.invalidate_config_cache()
    yield
    cfg.invalidate_config_cache()


@pytest.fixture(autouse=True)
def _granted_permissions_by_default(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
):
    """Default the daemon permission probe to all-granted (U6).

    The pre-spawn permission gate (recording.start) now blocks on a denied
    Screen Recording grant, so without this stub every recording.start test
    would 403 on a host whose interpreter lacks the grant (CI, dev). Defaulting
    to granted keeps those tests deterministic and host-independent. Tests that
    exercise the probe itself or the live gate opt out via
    ``@pytest.mark.real_permission_probe``; tests of the gate override
    ``probe_permissions`` themselves (a later monkeypatch wins).
    """
    if request.node.get_closest_marker("real_permission_probe"):
        return
    from screencap.daemon import permission_probe

    monkeypatch.setattr(
        permission_probe,
        "probe_permissions",
        lambda *a, **k: {
            "screen_recording": "granted",
            "accessibility": "granted",
            "input_monitoring": "granted",
        },
    )


@pytest.fixture
def allow_tmp_output_dir(tmp_path: Path):
    """Add ``tmp_path`` to the supervisor's output-dir allowlist for this test.

    Tests that pass a custom ``output_dir`` outside ``~/.screencap/recordings/``
    (e.g. ``tmp_path / "demo"``) need this fixture to bypass the allowlist
    check without touching production configuration. The entry is cleaned up
    after the test.
    """
    from screencap.daemon import supervisor as sv

    sv._extra_output_dir_allowlist.append(tmp_path.resolve())
    yield tmp_path
    try:
        sv._extra_output_dir_allowlist.remove(tmp_path.resolve())
    except ValueError:
        pass


@pytest.fixture
def daemon_socket_path(tmp_path: Path) -> Path:
    return short_socket_path(tmp_path)


def short_socket_path(tmp_path: Path) -> Path:
    """Return a short UDS path whose backing directory is still test-owned."""
    link = Path("/tmp") / f"sc-{uuid.uuid4().hex[:12]}"
    link.symlink_to(tmp_path, target_is_directory=True)
    atexit.register(lambda: link.unlink(missing_ok=True))
    return link / "run" / "api.sock"


@pytest.fixture
def daemon_env() -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        # The real daemon subprocess reads real TCC state (denied in CI), which
        # the in-process probe stub can't reach. Force granted so recording.start
        # isn't blocked by the U6 permission gate. Tests of the gate against a
        # real daemon can override this.
        "SCREENCAP_PERMISSION_PROBE_FAKE": "granted",
    }


def wait_for_socket(path: Path, proc: subprocess.Popen[bytes] | None = None, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return
        if proc is not None and proc.poll() is not None:
            stdout, stderr = proc.communicate(timeout=1)
            pytest.fail(
                f"daemon exited before socket appeared: {proc.returncode}\n"
                f"stdout:\n{stdout.decode(errors='replace')}\n"
                f"stderr:\n{stderr.decode(errors='replace')}"
            )
        time.sleep(0.025)
    pytest.fail(f"socket did not appear within {timeout}s: {path}")


@pytest.fixture
def serve_process(daemon_env: dict[str, str], daemon_socket_path: Path) -> Iterator[subprocess.Popen[bytes]]:
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "screencap.cli",
            "--no-update-check",
            "serve",
            "--socket",
            str(daemon_socket_path),
        ],
        env=daemon_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    wait_for_socket(daemon_socket_path, proc=proc)
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


@pytest.fixture
def uds_client_factory(daemon_socket_path: Path) -> Callable[[], httpx.AsyncClient]:
    def factory() -> httpx.AsyncClient:
        transport = httpx.AsyncHTTPTransport(uds=str(daemon_socket_path))
        return httpx.AsyncClient(transport=transport, base_url="http://screencap")

    return factory


@pytest.fixture
def stdin_confirming_engine_script(tmp_path: Path) -> Path:
    """A fake engine (spawned via ``SCREENCAP_DAEMON_ENGINE_COMMAND``) that reads
    its stdin control channel and, on each ``set_muted`` command, emits the
    CONFIRMED ``audio_muted`` / ``audio_unmuted`` event — so the U1→U4→U5 forward
    is observable end-to-end. If ``SCREENCAP_MUTE_LOG`` is set it also appends
    each received command to that file, so a test can assert exactly what the
    daemon forwarded. Shared by the ``recording.mute`` verb (U4) and
    snapshot-muted (U5) tests (SCR-254 polish: was duplicated in both)."""
    script = tmp_path / "stdin_confirming_engine.py"
    script.write_text(
        textwrap.dedent(
            """
            from __future__ import annotations

            import base64
            import json
            import os
            import signal
            import sys
            import threading
            import time

            args = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
            name = args.get("name") or "fake"
            log_path = os.environ.get("SCREENCAP_MUTE_LOG")


            def emit(event_type, **payload):
                sys.stderr.write(
                    json.dumps({"type": event_type, "schema_version": 1,
                                "ts": time.time(), **payload}) + "\\n")
                sys.stderr.flush()


            def read_stdin():
                for line in sys.stdin:
                    line = line.strip()
                    if not line:
                        continue
                    cmd = json.loads(line)
                    if cmd.get("type") == "set_muted":
                        if log_path:
                            with open(log_path, "a") as fh:
                                fh.write(json.dumps(cmd) + "\\n")
                        # Confirmed event fires only after the "real toggle".
                        emit("audio_muted" if cmd["muted"] else "audio_unmuted",
                             muted=cmd["muted"])


            def handle_term(_s, _f):
                emit("recording_finalized", name=name, duration_seconds=0.2,
                     force_stopped=False, disk_full=False)
                raise SystemExit(0)


            signal.signal(signal.SIGTERM, handle_term)
            threading.Thread(target=read_stdin, daemon=True).start()
            emit("started", claimant="daemon")
            while True:
                time.sleep(0.05)
            """
        ),
        encoding="utf-8",
    )
    return script
