"""Mitmproxy programmatic runner (V1 — metadata-only).

The :func:`run_proxy` function is the ``mp.Process`` target spawned by
the engine for the ``--network`` flag. It:

1. Asserts spawn-mode (defense-in-depth — the CLI entry already sets
   it; this catches any future regression where a child process is
   forked instead of spawned, which would inherit the parent's PRNG
   state and break AES-GCM nonce uniqueness in V1.5+).
2. (smoke-test only) When given a ``nonce_check_q``, generates 100
   ``os.urandom(12)`` values and posts them to the queue BEFORE
   installing the addon — used by :file:`test_proxy_runner.py` to
   verify parent/child PRNG independence. Also runs a fast
   within-child uniqueness check on 10000 fresh values.
3. Imports mitmproxy lazily (heavy import — never at module top).
4. Builds the ``ignore_hosts`` regex from the privacy + network
   blocklist sources.
5. Constructs ``DumpMaster`` INSIDE the asyncio loop with
   ``with_termlog=False`` and ``with_dumper=False`` (we want no
   human-readable output to stdout). DumpMaster's ``__init__`` calls
   ``asyncio.get_running_loop()`` so it must be constructed inside a
   running coroutine, not at the top level.
6. Adds a :class:`NetworkCapture` instance as the only user addon.
7. Runs ``master.run()``, polls until the listening socket is bound,
   and then signals ``started_event``. The event is set ONLY after
   BOTH the addon was installed successfully AND the listener is
   bound — the engine waits on this before flipping the system proxy.
8. Installs a SIGTERM handler that schedules ``master.shutdown()`` on
   the loop. Parent process sends SIGTERM during teardown.
9. On clean shutdown (or any exception), logs final stats to
   ``log_path``.

V1 has NO ``dek`` / ``dek_wrapped`` / ``dek_nonce`` arguments — the
addon does not need a DEK because all bodies are hashed-and-discarded.
The argument signature was deliberately kept minimal.
"""

from __future__ import annotations

import multiprocessing
import os
import time
import traceback
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import multiprocessing as mp

    from screencap.network.config import NetworkConfig
    from screencap.privacy.policy import PrivacyConfig


def run_proxy(
    out_q: "mp.Queue",
    recording_id: int,
    network_config: "NetworkConfig",
    privacy_config: "PrivacyConfig",
    port: int,
    log_path: Path,
    started_event: "mp.Event",
    confdir: Path,
    nonce_check_q: "mp.Queue | None" = None,
) -> None:
    """``mp.Process`` target — runs DumpMaster + NetworkCapture addon.

    Args:
        out_q: Queue consumed by the main recorder's network reader
            thread. The addon pushes events here.
        recording_id: Foreign-key reference for downstream rows.
        network_config: Source of ``body_size_cap`` and the network
            blocklist.
        privacy_config: Source of ``mask_domains``.
        port: TCP port the proxy listens on (127.0.0.1:port).
        log_path: Diagnostic log file path. Created/truncated by the
            engine pre-flight step; this function appends to it.
        started_event: Set by this function ONCE the proxy is fully
            ready — addon installed AND socket bound. The engine
            waits on this before flipping the system proxy.
        confdir: Mitmproxy conf directory. Should contain the
            pre-generated CA cert + key from Unit 2.
        nonce_check_q: When provided (smoke-test path), 100
            ``os.urandom(12)`` samples are posted to this queue before
            the addon is installed. Production callers pass ``None``.
    """
    # 1. Spawn-mode defense-in-depth assertion.
    start_method = multiprocessing.get_start_method(allow_none=True)
    assert start_method == "spawn", (
        f"network proxy requires spawn mode, got start_method={start_method!r}"
    )

    # 2. Optional smoke-test PRNG channel + within-child uniqueness check.
    if nonce_check_q is not None:
        try:
            for _ in range(100):
                nonce_check_q.put_nowait(os.urandom(12))
        except Exception:
            # Queue full / closed — caller will see the partial count.
            pass

        # Within-child uniqueness check (fast — 10000 nonces is ~10ms).
        # Catches PyInstaller bootloader PRNG regressions where a
        # frozen-binary child seeds urandom with a constant.
        seen: set[bytes] = set()
        for _ in range(10000):
            n = os.urandom(12)
            assert n not in seen, "os.urandom(12) collision in within-child check"
            seen.add(n)

    # 3. Heavy imports inside the function body — never at module top.
    try:
        import asyncio
        import signal

        from mitmproxy import options as mp_options
        from mitmproxy.tools.dump import DumpMaster

        from screencap.network.blocklist import build_ignore_hosts_regex
        from screencap.network.capture_addon import NetworkCapture
    except Exception as exc:  # noqa: BLE001
        _emit_log(log_path, f"FATAL: import failure during run_proxy: {exc!r}")
        _emit_log(log_path, traceback.format_exc())
        # started_event NEVER gets set — engine times out and aborts.
        return

    # 4. Build ignore_hosts regex.
    try:
        ignore_hosts = build_ignore_hosts_regex(privacy_config, network_config)
    except Exception as exc:  # noqa: BLE001
        _emit_log(log_path, f"FATAL: ignore_hosts build failed: {exc!r}")
        _emit_log(log_path, traceback.format_exc())
        return

    # 5. Construct mitmproxy Options. We do NOT set stream_large_bodies —
    # the addon controls streaming explicitly per-flow.
    try:
        opts = mp_options.Options(
            listen_host="127.0.0.1",
            listen_port=port,
            confdir=str(confdir),
            ignore_hosts=ignore_hosts,
        )
    except Exception as exc:  # noqa: BLE001
        _emit_log(log_path, f"FATAL: mitmproxy options construction failed: {exc!r}")
        _emit_log(log_path, traceback.format_exc())
        return

    # Container for the async master so we can reach it from SIGTERM.
    master_holder: dict = {"master": None, "loop": None, "addon": None}

    async def _main() -> None:
        # 6. DumpMaster.__init__ calls asyncio.get_running_loop() —
        # so it MUST be constructed inside the running coroutine.
        try:
            master = DumpMaster(opts, with_termlog=False, with_dumper=False)
        except Exception as exc:  # noqa: BLE001
            _emit_log(log_path, f"FATAL: DumpMaster construction failed: {exc!r}")
            _emit_log(log_path, traceback.format_exc())
            return
        master_holder["master"] = master
        master_holder["loop"] = asyncio.get_running_loop()

        # 7. Install our addon. Failure here is captured below so
        # started_event is never set.
        try:
            addon = NetworkCapture(
                out_q=out_q,
                recording_id=recording_id,
                network_config=network_config,
                privacy_config=privacy_config,
                log_path=log_path,
            )
            master.addons.add(addon)
            master_holder["addon"] = addon
        except Exception as exc:  # noqa: BLE001
            _emit_log(log_path, f"FATAL: addon installation failed: {exc!r}")
            _emit_log(log_path, traceback.format_exc())
            return

        # 8. Launch master.run() as a task and poll for the listening
        # socket. Set started_event only after the addon is installed
        # AND the socket is bound.
        run_task = asyncio.create_task(master.run())

        deadline = time.monotonic() + 15.0
        bound = False
        while time.monotonic() < deadline:
            if run_task.done():
                exc = run_task.exception()
                if exc:
                    _emit_log(
                        log_path,
                        f"FATAL: master.run() failed during startup: {exc!r}",
                    )
                    return
                break
            ps = master.addons.get("proxyserver")
            if ps is not None:
                addrs = getattr(ps, "listen_addrs", None)
                if callable(addrs):
                    try:
                        addrs = addrs()
                    except Exception:  # noqa: BLE001
                        addrs = None
                if addrs:
                    bound = True
                    started_event.set()
                    _emit_log(
                        log_path,
                        f"proxy listening on {addrs}, addon installed",
                    )
                    break
            await asyncio.sleep(0.05)

        if not bound:
            _emit_log(log_path, "FATAL: timed out waiting for listener bind")
            # Still wait for run_task so we can log its eventual error.
            try:
                await run_task
            except asyncio.CancelledError:
                pass
            return

        # Now wait for master.run() to finish (SIGTERM → shutdown).
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    # 9. SIGTERM handler — must be installed on the main thread of this
    # process. We install BEFORE starting the loop. The handler closes
    # over master_holder so it can find the master once the coroutine
    # has constructed it.
    def _sigterm_handler(signum, _frame):  # noqa: ARG001
        master = master_holder.get("master")
        loop = master_holder.get("loop")
        if master is None or loop is None:
            return
        # mitmproxy 11.x's Master.shutdown is a SYNC function (sets an
        # internal flag); calling it returns None, so
        # asyncio.run_coroutine_threadsafe fails with "A coroutine object
        # is required". Schedule the sync call on the loop thread instead.
        try:
            loop.call_soon_threadsafe(master.shutdown)
        except Exception as exc:  # noqa: BLE001
            _emit_log(log_path, f"WARN: SIGTERM handler error: {exc!r}")

    try:
        signal.signal(signal.SIGTERM, _sigterm_handler)
    except (ValueError, OSError) as exc:
        # signal can fail if not on the main thread — log + continue.
        _emit_log(log_path, f"WARN: cannot install SIGTERM handler: {exc!r}")

    # 10. Run the loop until master.run() returns (clean shutdown).
    try:
        asyncio.run(_main())
    except Exception as exc:  # noqa: BLE001
        _emit_log(log_path, f"FATAL: run_proxy unexpected error: {exc!r}")
        _emit_log(log_path, traceback.format_exc())
        return

    # 11. Final shutdown stats.
    addon = master_holder.get("addon")
    if addon is not None:
        _emit_log(
            log_path,
            f"clean shutdown: flows={addon._flow_count} "  # noqa: SLF001
            f"final_drop_count={addon._dropped_count} "  # noqa: SLF001
            f"runtime_tunnel_hosts={sorted(addon._runtime_tunnel_hosts)}",  # noqa: SLF001
        )


def _emit_log(log_path: Path | None, msg: str) -> None:
    """Append a line to the proxy diagnostic log (best-effort)."""
    if log_path is None:
        return
    try:
        with Path(log_path).open("a", encoding="utf-8") as fh:
            fh.write(f"[{time.time():.3f}] {msg}\n")
    except OSError:
        pass
