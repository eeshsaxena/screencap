"""Tests for screencap.network.proxy_runner (V1 — metadata-only).

Heavy mitmproxy integration tests live in Unit 7's smoke test. This
file covers:
- spawn-mode assertion fires before mitmproxy import
- started_event is NOT set when addon installation fails
- happy-path lifecycle: process binds the listener, started_event
  fires, SIGTERM cleanly shuts it down
- smoke nonce-check kwarg posts 100 nonces independently of out_q
"""

from __future__ import annotations

import multiprocessing as mp
import os
import queue
import signal
import socket
import time

import pytest

from screencap.network.config import NetworkConfig
from screencap.privacy.policy import PrivacyConfig

# Spawn context — production matches this; we use a fresh context so
# tests don't rely on the global default being reset.
SPAWN_CTX = mp.get_context("spawn")


def _picklable_privacy() -> PrivacyConfig:
    """Construct a PrivacyConfig whose app_classes is a plain dict.

    PrivacyConfig.__post_init__ wraps app_classes in MappingProxyType,
    which is not picklable across spawn boundaries. Production code
    builds the PrivacyConfig once and never crosses a process boundary
    with it; the runner is invoked inside the engine which runs in the
    main process. The proxy_runner mp.Process is the one place where
    we DO need it picklable, and the engine handles this in real flow
    (the proxy process doesn't actually access app_classes — it only
    reads mask_domains). For tests we strip the MappingProxyType.
    """
    pc = PrivacyConfig()
    object.__setattr__(pc, "app_classes", {})
    return pc


def _free_port() -> int:
    """Return a free TCP port on localhost."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ---------------------------------------------------------------------------
# Spawn-mode assertion
# ---------------------------------------------------------------------------


class TestSpawnAssertion:
    def test_fork_mode_raises(self, tmp_path):
        """If a fork-context process invokes run_proxy, the spawn
        assertion fires immediately. We use the real run_proxy via a
        fork context to ensure the assertion is the first thing that
        runs (before any mitmproxy import).
        """
        from screencap.network.proxy_runner import run_proxy

        # If `fork` start method is unavailable on this platform, skip.
        try:
            fork_ctx = mp.get_context("fork")
        except ValueError:
            pytest.skip("fork start method unavailable on this platform")

        out_q = fork_ctx.Queue(maxsize=10)
        started_event = fork_ctx.Event()
        log_path = tmp_path / "proxy.log"
        log_path.touch()

        proc = fork_ctx.Process(
            target=run_proxy,
            args=(
                out_q,
                42,
                NetworkConfig(),
                _picklable_privacy(),
                _free_port(),
                log_path,
                started_event,
                tmp_path / "confdir",
            ),
        )
        proc.start()
        proc.join(timeout=10)
        # Process exited (assertion raised before any blocking work).
        assert not proc.is_alive()
        # Non-zero exit on AssertionError.
        assert proc.exitcode != 0
        # started_event NEVER set.
        assert not started_event.is_set()


# ---------------------------------------------------------------------------
# started_event ordering — addon-install failure
# ---------------------------------------------------------------------------


def _run_proxy_with_broken_addon(
    out_q,
    recording_id,
    network_config,
    privacy_config,
    port,
    log_path,
    started_event,
    confdir,
):
    """Wrapper that mocks NetworkCapture to raise during construction."""
    # Inject a faulty NetworkCapture before run_proxy imports it.
    import screencap.network.capture_addon as addon_mod

    class BrokenAddon:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("simulated addon construction failure")

    addon_mod.NetworkCapture = BrokenAddon  # type: ignore[misc]

    from screencap.network.proxy_runner import run_proxy
    run_proxy(
        out_q,
        recording_id,
        network_config,
        privacy_config,
        port,
        log_path,
        started_event,
        confdir,
    )


class TestStartedEventOrdering:
    def test_addon_failure_keeps_started_event_unset(self, tmp_path):
        """When addon construction raises, started_event MUST NOT be set
        — the engine relies on this to abort before flipping system proxy."""
        out_q = SPAWN_CTX.Queue(maxsize=10)
        started_event = SPAWN_CTX.Event()
        log_path = tmp_path / "proxy.log"
        log_path.touch()

        proc = SPAWN_CTX.Process(
            target=_run_proxy_with_broken_addon,
            args=(
                out_q,
                42,
                NetworkConfig(),
                _picklable_privacy(),
                _free_port(),
                log_path,
                started_event,
                tmp_path / "confdir",
            ),
        )
        proc.start()
        # Wait briefly — assertion fires fast (no socket bind).
        ready = started_event.wait(timeout=4.0)
        assert ready is False, "started_event should NOT have been set"

        proc.join(timeout=5)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=3)


# ---------------------------------------------------------------------------
# Smoke nonce check
# ---------------------------------------------------------------------------


def _run_with_nonce_check(
    out_q,
    recording_id,
    network_config,
    privacy_config,
    port,
    log_path,
    started_event,
    confdir,
    nonce_check_q,
):
    """Wrapper that imports run_proxy then invokes it with the
    smoke-test kwarg. We do this in a separate function so it can be
    pickled via spawn."""
    from screencap.network.proxy_runner import run_proxy
    run_proxy(
        out_q,
        recording_id,
        network_config,
        privacy_config,
        port,
        log_path,
        started_event,
        confdir,
        nonce_check_q=nonce_check_q,
    )


class TestNonceCheck:
    def test_smoke_nonces_arrive_unique(self, tmp_path):
        """100 fresh nonces arrive on nonce_check_q before the addon is
        installed. The within-child uniqueness check (10000 fresh
        nonces) also runs but is expensive to verify here — we just
        confirm the parent/child handshake works."""
        out_q = SPAWN_CTX.Queue(maxsize=10)
        nonce_check_q = SPAWN_CTX.Queue(maxsize=200)
        started_event = SPAWN_CTX.Event()
        log_path = tmp_path / "proxy.log"
        log_path.touch()

        proc = SPAWN_CTX.Process(
            target=_run_with_nonce_check,
            args=(
                out_q,
                42,
                NetworkConfig(),
                _picklable_privacy(),
                _free_port(),
                log_path,
                started_event,
                tmp_path / "confdir",
                nonce_check_q,
            ),
        )
        proc.start()
        try:
            collected: list[bytes] = []
            deadline = time.monotonic() + 10.0
            while len(collected) < 100 and time.monotonic() < deadline:
                try:
                    collected.append(nonce_check_q.get(timeout=0.5))
                except queue.Empty:
                    pass

            assert len(collected) == 100, (
                f"expected 100 smoke nonces, got {len(collected)}"
            )
            # All unique.
            assert len(set(collected)) == 100
            # Each is 12 bytes.
            assert all(isinstance(n, bytes) and len(n) == 12 for n in collected)
        finally:
            # Clean up — proc is going to attempt to bind the listener;
            # send SIGTERM and join.
            try:
                if proc.is_alive():
                    os.kill(proc.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
            proc.join(timeout=5)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=3)


# ---------------------------------------------------------------------------
# Happy-path lifecycle (real mitmproxy DumpMaster + SIGTERM)
# ---------------------------------------------------------------------------


def _run_real_proxy(
    out_q,
    recording_id,
    network_config,
    privacy_config,
    port,
    log_path,
    started_event,
    confdir,
):
    """Plain wrapper for spawn pickling."""
    from screencap.network.proxy_runner import run_proxy
    run_proxy(
        out_q,
        recording_id,
        network_config,
        privacy_config,
        port,
        log_path,
        started_event,
        confdir,
    )


class TestProcessLifecycle:
    @pytest.mark.timeout(30)
    def test_start_listen_terminate(self, tmp_path):
        """The proxy process starts, binds the listener (sets
        started_event), then exits cleanly on SIGTERM."""
        port = _free_port()
        out_q = SPAWN_CTX.Queue(maxsize=10)
        started_event = SPAWN_CTX.Event()
        log_path = tmp_path / "proxy.log"
        log_path.touch()
        # confdir needs to exist; mitmproxy will create CA on first
        # run if it's empty (slow, but fine for this test which just
        # checks the lifecycle).
        confdir = tmp_path / "confdir"
        confdir.mkdir()

        proc = SPAWN_CTX.Process(
            target=_run_real_proxy,
            args=(
                out_q,
                42,
                NetworkConfig(proxy_port=port),
                _picklable_privacy(),
                port,
                log_path,
                started_event,
                confdir,
            ),
        )
        proc.start()
        try:
            # Wait up to 15s for the listener to bind. On cold mitmproxy
            # init this includes CA generation in the empty confdir.
            ready = started_event.wait(timeout=20.0)
            assert ready, (
                "started_event never fired; log:\n" + log_path.read_text()
            )

            # Verify the listener is actually accepting connections.
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(2.0)
                # Connect attempt — succeeds if the proxy is bound.
                s.connect(("127.0.0.1", port))
        finally:
            # SIGTERM the process — the runner schedules
            # master.shutdown() and exits cleanly.
            if proc.is_alive():
                try:
                    os.kill(proc.pid, signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    pass

            proc.join(timeout=10)
            if proc.is_alive():
                # Force-kill to keep the test suite from hanging.
                proc.terminate()
                proc.join(timeout=3)

        # Process exited.
        assert not proc.is_alive()
