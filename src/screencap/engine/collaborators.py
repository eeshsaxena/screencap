"""``RecordingCollaborators`` — engine-side collaborator lifecycle.

Owns the three collaborators that share the engine flush primitives
(``flush_requested`` / ``flush_ack_counter`` / ``flush_lock``):

* ``RecorderPrivacyFilter`` — capture-time policy enforcement; constructor
  honours ``cloud_intent`` (forces ``PrivacyMode.PUBLIC``) and the
  window-data gate.
* ``ChunkProcessor``        — per-chunk transcribe / export / upload thread.
* ``ScrubWorker``           — sidecar thread that processes menu-bar disable
  jobs against the live ``recording.db``.

A single helper class (rather than a Protocol with two implementations)
is the right shape here: every recording uses these collaborators when
their gates are enabled. The chunk processor and privacy filter are
gated by their own configuration inputs rather than by a policy axis;
the scrub worker always starts.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace as _dc_replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from screencap.engine.screen_recorder import (
    IpcChannels,
    LegacyOptions,
    RecordingRequest,
)

if TYPE_CHECKING:
    from screencap.privacy.policy import PrivacyConfig


class RecordingCollaborators:
    """Engine-owned privacy filter + chunk processor + scrub worker.

    Lifecycle, called from ``_run_screen_recorder``:

    1. ``build_recorder_privacy_filter(capture_dir, capture_window_data)`` —
       before ``engine.Recorder.__enter__``; returns the filter to pass
       into the engine and the resolved ``privacy_config`` for downstream
       wiring.
    2. ``start(recorder, ...)`` — after ``__enter__``; spawns the chunk
       and scrub threads attached to the engine's queues + flush
       primitives.
    3. ``poll_overrides()`` — drains pending menu bar overrides at stop.
    4. ``finalize(...)`` — runs catch-all scrub, drains both threads,
       checkpoints + uploads the DB, reconciles GCS, uploads the
       sentinel, decides whether to stub. Must be called *inside* the
       engine ``with``-block so the engine's queue cleanup at ``__exit__``
       happens after consumers drain.
    """

    def __init__(
        self,
        *,
        request: RecordingRequest,
        legacy: LegacyOptions,
        channels: IpcChannels,
    ) -> None:
        self._request = request
        self._legacy = legacy
        self._channels = channels
        # Shared lock — serializes engine-flush handshakes between
        # chunk_processor and scrub_worker (both consume the same
        # flush_ack_counter on the engine Recorder, racing them would
        # zero out each other's in-flight ack counts mid-poll).
        self._flush_lock = threading.Lock()
        self._chunk_processor: Any | None = None
        self._scrub_worker: Any | None = None
        self._chunk_q: Any | None = None
        self._audio_ack_q: Any | None = None
        # Resolved by ``build_recorder_privacy_filter`` BEFORE filter
        # construction so the outer fail-closed gate can read it even if
        # filter construction raises.
        self._privacy_config: "PrivacyConfig | None" = None

    @property
    def privacy_config(self) -> "PrivacyConfig | None":
        return self._privacy_config

    @property
    def chunk_processor(self) -> Any | None:
        return self._chunk_processor

    @property
    def scrub_worker(self) -> Any | None:
        return self._scrub_worker

    # ------------------------------------------------------------------
    # Privacy filter
    # ------------------------------------------------------------------

    def build_recorder_privacy_filter(
        self,
        *,
        capture_dir: Path,
        capture_window_data: bool,
    ) -> tuple[Any | None, "PrivacyConfig | None", Path]:
        """Construct ``RecorderPrivacyFilter`` with cloud_intent floor + window-data gate.

        Returns ``(screen_filter, privacy_config, override_file)``:

        * ``screen_filter`` — the filter, or ``None`` if window data is
          disabled (filter cannot work without window events).
        * ``privacy_config`` — the resolved config; mode is tightened to
          ``PrivacyMode.PUBLIC`` when ``request.cloud_intent`` or
          ``legacy.force_mode`` requires it. Never loosened.
        * ``override_file`` — ``capture_dir / ".menubar_overrides.json"``,
          where the filter persists per-target action overrides.
        """
        from screencap.config import get_privacy_config
        from screencap.privacy.policy import _MODE_STRICTNESS
        from screencap.privacy.recorder_enforcement import RecorderPrivacyFilter

        override_file = capture_dir / ".menubar_overrides.json"
        privacy_config = get_privacy_config()

        force_mode = self._legacy.force_mode
        if self._request.cloud_intent:
            from screencap.privacy.policy import PrivacyMode

            force_mode = PrivacyMode.PUBLIC

        if force_mode is not None:
            if _MODE_STRICTNESS[force_mode] <= _MODE_STRICTNESS[privacy_config.mode]:
                privacy_config = _dc_replace(privacy_config, mode=force_mode)

        # Publish the resolved config on the helper BEFORE constructing the
        # filter. If RecorderPrivacyFilter raises, the outer fail-closed
        # gate in _run_screen_recorder still has the config to gate on
        # (otherwise it sees ``privacy_config=None`` and a PUBLIC-mode user
        # silently degrades to the warn-and-proceed path).
        self._privacy_config = privacy_config

        if not capture_window_data:
            return None, privacy_config, override_file

        screen_filter = RecorderPrivacyFilter(
            privacy_config,
            cloud_intent=self._request.cloud_intent,
            window_feed_q=self._channels.window_feed,
            override_q=self._channels.override,
            override_file=override_file,
        )
        return screen_filter, privacy_config, override_file

    # ------------------------------------------------------------------
    # ChunkProcessor + ScrubWorker lifecycle
    # ------------------------------------------------------------------

    def start(
        self,
        *,
        recorder: Any,
        capture_dir: Path,
        screen_filter: Any | None,
        privacy_config: "PrivacyConfig | None",
        chunking_enabled: bool,
        console: Any | None = None,
    ) -> None:
        """Spawn ChunkProcessor (if chunking) and ScrubWorker.

        Reads the engine queues + flush primitives from ``recorder`` (the
        engine ``Recorder`` instance, post-``__enter__``). Both consumers
        share ``self._flush_lock`` so concurrent flush handshakes don't
        race on the engine's ``flush_ack_counter``.

        ``console`` is used only for the failure-path warnings; tests
        pass ``None`` to suppress output.
        """
        import multiprocessing as _mp

        self._build_chunk_processor(
            recorder=recorder,
            capture_dir=capture_dir,
            screen_filter=screen_filter,
            privacy_config=privacy_config,
            chunking_enabled=chunking_enabled,
            mp_module=_mp,
            console=console,
        )
        self._build_scrub_worker(
            recorder=recorder, capture_dir=capture_dir, console=console,
        )

    def _build_chunk_processor(
        self,
        *,
        recorder: Any,
        capture_dir: Path,
        screen_filter: Any | None,
        privacy_config: "PrivacyConfig | None",
        chunking_enabled: bool,
        mp_module: Any,
        console: Any,
    ) -> None:
        if not chunking_enabled:
            return
        try:
            cpq = getattr(recorder, "_chunk_process_q", None)
            aaq = getattr(recorder, "_audio_ack_q", None)
            if cpq is None or aaq is None:
                return
            if not isinstance(cpq, mp_module.queues.Queue):
                return
            self._chunk_q = cpq
            self._audio_ack_q = aaq

            from screencap.chunk_processor import ChunkProcessor
            from screencap.config import (
                get_auto_delete_after_upload,
                get_rest_threshold,
            )

            cloud_intent = self._request.cloud_intent
            keep_local = self._request.keep_local
            live_upload = bool(self._legacy.live_upload)
            effective_upload = live_upload if cloud_intent else False
            auto_delete = (
                cloud_intent
                and not keep_local
                and get_auto_delete_after_upload()
            )
            mode_str = privacy_config.mode.value if privacy_config else "internal"

            cp = ChunkProcessor(
                capture_dir,
                cpq,
                aaq,
                recording_name=self._request.name,
                upload_enabled=effective_upload,
                auto_delete=auto_delete,
                rest_threshold=get_rest_threshold(),
                flush_requested=getattr(recorder, "_flush_requested", None),
                flush_ack_counter=getattr(recorder, "_flush_ack_counter", None),
                flush_lock=self._flush_lock,
                cloud_intent=cloud_intent,
                privacy_mode=mode_str,
                screen_filter=screen_filter,
                segmentation_mode=self._request.segmentation_mode,
                scrub_enabled=self._request.scrub_enabled,
                show_on_website=self._request.show_on_website,
            )
            cp.start()
            self._chunk_processor = cp
        except Exception as exc:  # noqa: BLE001
            self._chunk_processor = None
            if console is not None:
                console.print(
                    f"[yellow]Warning:[/yellow] ChunkProcessor failed to start: {exc}"
                )

    # ------------------------------------------------------------------
    # End-of-recording finalize
    # ------------------------------------------------------------------

    def finalize_catchall_scrub(self, *, capture_dir: Path) -> None:
        """Enumerate ``.menubar_overrides.json`` and queue final disable msgs.

        Catches activity captured AFTER the user toggled a target to
        "exclude" but BEFORE the recording stopped — those rows were
        buffered in the engine writer and would otherwise leak into the
        final recording. Queued onto ``disable_q`` BEFORE
        :meth:`stop_scrub_worker` so the worker processes them in the
        same drain pass.

        Safe to call when ``self.scrub_worker is None`` (no-op).
        """
        if self._scrub_worker is None:
            return

        from screencap.privacy.actions import EXCLUDED_ACTION_VALUES

        override_path = capture_dir / ".menubar_overrides.json"
        if not override_path.exists():
            return

        import json
        import time

        try:
            overrides_state = json.loads(override_path.read_text())
        except Exception:  # noqa: BLE001
            overrides_state = {}

        disable_q = self._channels.disable
        now = time.time()
        for key, action in overrides_state.items():
            if action not in EXCLUDED_ACTION_VALUES:
                continue
            if "::" in key:
                bundle, dom = key.split("::", 1)
                msg: dict = {
                    "kind": "domain",
                    "bundle_id": bundle,
                    "app_name": None,
                    "root_domain": dom,
                    "ts_unix": now,
                    "source": "shutdown_catchall",
                }
            else:
                msg = {
                    "kind": "app",
                    "bundle_id": key,
                    "app_name": None,
                    "root_domain": None,
                    "ts_unix": now,
                    "source": "shutdown_catchall",
                }
            try:
                disable_q.put_nowait(msg)
            except Exception:  # noqa: BLE001
                pass

    def stop_chunk_processor(
        self,
        *,
        deadline_seconds: float = 300.0,
        console: Any | None = None,
    ) -> None:
        """Poison-pill the ChunkProcessor and join its thread.

        Surfaces ``chunk_processor.status`` through ``console.status`` if
        the caller passed one — mirrors the live spinner in the legacy
        path. ``KeyboardInterrupt`` during the join is swallowed so the
        recording artifacts on disk survive the user's Ctrl+C; uploaders
        recover via ``screencap upload``.
        """
        cp = self._chunk_processor
        if cp is None:
            return
        try:
            try:
                cp._q.put({"type": "poison_pill"}, timeout=5)
            except Exception:  # noqa: BLE001
                pass
            thread = cp._thread
            deadline = time.time() + deadline_seconds
            if console is not None:
                with console.status("[dim]Finishing up...[/dim]") as spinner:
                    while thread is not None and thread.is_alive():
                        if time.time() > deadline:
                            break
                        step = cp.status
                        if step:
                            spinner.update(f"[dim]{step}[/dim]")
                        thread.join(timeout=0.5)
            else:
                if thread is not None:
                    thread.join(timeout=deadline_seconds)
            cp._thread = None
        except KeyboardInterrupt:
            if console is not None:
                console.print(
                    "[yellow]Force quit — data is saved on disk.[/yellow]"
                )
                console.print(
                    "[dim]Run [bold]screencap upload[/bold] later to upload remaining files.[/dim]"
                )

    def close_engine_queues(self, *, menubar_owns_channels: bool) -> None:
        """Close the engine queues now that consumers have drained.

        chunk_processor and scrub_worker have already finished by the
        time this runs, so closing is safe; the engine's own ``__exit__``
        cleanup that follows is a harmless second close.

        ``menubar_owns_channels=False`` (session-worker mode) means the
        controller owns the disable queue, so the worker must NOT close
        it; we still close the chunk-processor queues which the engine
        created.
        """
        if self._chunk_q is None and self._audio_ack_q is None:
            return
        from screencap._startup import close_queues_safely

        if menubar_owns_channels:
            close_queues_safely(
                self._chunk_q, self._audio_ack_q, self._channels.disable,
            )
        else:
            close_queues_safely(self._chunk_q, self._audio_ack_q)

    def finalize_uploads(
        self,
        *,
        capture_dir: Path,
        stop_reason: str,
        recording_name: str,
        console: Any | None = None,
    ) -> dict[str, Any]:
        """Run WAL checkpoint, GCS reconcile, sentinel upload, stub decision.

        Returns a status dict::

            {
              "sentinel_uploaded": bool,
              "all_chunks_uploaded": bool,
              "n_chunks": int,
              "n_uploaded": int,
              "n_total": int,
              "upload_warning": str | None,
              "force_stopped": bool,
              "stubbed": bool,
              "followup_kind": str | None,
            }

        ``recording_name`` is the post-rename name (read from
        ``.recording_id`` if present); ``stop_reason`` is the ``_stop_reason``
        string the live loop produced. The caller uses the returned dict
        to decide what to print after the live display teardown.
        """
        cp = self._chunk_processor
        cloud_intent = self._request.cloud_intent
        keep_local = self._request.keep_local
        live_upload = bool(self._legacy.live_upload)
        verbose = bool(self._legacy.verbose)
        show_on_website = self._request.show_on_website

        result: dict[str, Any] = {
            "sentinel_uploaded": False,
            "all_chunks_uploaded": False,
            "n_chunks": 0,
            "n_uploaded": 0,
            "n_total": 0,
            "upload_warning": None,
            "force_stopped": False,
            "stubbed": False,
            "followup_kind": None,
        }
        if cp is None:
            return result

        # WAL checkpoint + DB upload
        if live_upload:
            try:
                from screencap.chunk_processor import checkpoint_and_upload_db

                if console is not None:
                    with console.status("[dim]Uploading recording database...[/dim]"):
                        checkpoint_and_upload_db(
                            capture_dir, recording_name,
                            cloud_intent=cloud_intent,
                        )
                else:
                    checkpoint_and_upload_db(
                        capture_dir, recording_name,
                        cloud_intent=cloud_intent,
                    )
            except Exception as exc:  # noqa: BLE001
                if verbose and console is not None:
                    console.print(
                        f"[yellow]Warning:[/yellow] DB upload failed: {exc}"
                    )

        # GCS reconcile
        if live_upload:
            try:
                flipped = cp.reconcile_against_gcs()
                if flipped > 0 and verbose and console is not None:
                    console.print(
                        f"[dim]Reconciled {flipped} chunk(s) against GCS[/dim]"
                    )
            except Exception as exc:  # noqa: BLE001
                if verbose and console is not None:
                    console.print(
                        f"[yellow]Warning:[/yellow] GCS reconcile failed: {exc}"
                    )

        all_uploaded = (
            cp.all_chunks_uploaded() and not cp.was_force_stopped
        )
        n_uploaded, n_total = cp.upload_summary()
        n_chunks = len(list(capture_dir.glob("chunk_*_manifest.json")))
        result["all_chunks_uploaded"] = all_uploaded
        result["n_uploaded"] = n_uploaded
        result["n_total"] = n_total
        result["n_chunks"] = n_chunks
        result["upload_warning"] = cp.upload_warning
        result["force_stopped"] = cp.was_force_stopped

        # Sentinel upload — only when everything's in
        sentinel_uploaded = False
        if cloud_intent and live_upload and all_uploaded and n_chunks > 0:
            try:
                from screencap.chunk_processor import upload_sentinel

                sentinel_uploaded = upload_sentinel(
                    capture_dir, recording_name,
                    stop_reason=stop_reason or "graceful",
                    chunks_expected=n_chunks,
                    show_on_website=show_on_website,
                )
                if sentinel_uploaded and console is not None:
                    raw_url = (
                        f"https://screencap.sh/?source=recordings"
                        f"&recording={recording_name}#data"
                    )
                    session_url = (
                        f"https://screencap.sh/?source=sessions"
                        f"&recording={recording_name}#data"
                    )
                    console.print(
                        f"\n  [dim]View (raw):[/dim] "
                        f"[link={raw_url}]{raw_url}[/link]"
                    )
                    console.print(
                        f"  [dim]View (processed, ~2 min):[/dim] "
                        f"[link={session_url}]{session_url}[/link]"
                    )
                elif not sentinel_uploaded and console is not None:
                    console.print(
                        f"[yellow]Sentinel upload failed — run "
                        f"'screencap upload {recording_name}' to trigger stitching.[/yellow]"
                    )
            except Exception as exc:  # noqa: BLE001
                if verbose and console is not None:
                    console.print(
                        f"[yellow]Warning:[/yellow] Sentinel upload failed: {exc}"
                    )
        result["sentinel_uploaded"] = sentinel_uploaded

        # Stub recording decision
        has_chunk_files = any(capture_dir.glob("chunk_*.mp4"))
        safe_to_stub = all_uploaded and live_upload and has_chunk_files
        if cloud_intent and not keep_local:
            safe_to_stub = safe_to_stub and sentinel_uploaded
        else:
            safe_to_stub = False
        if safe_to_stub:
            try:
                from screencap.chunk_processor import stub_recording

                deleted = stub_recording(capture_dir)
                result["stubbed"] = bool(deleted)
                if deleted and verbose and console is not None:
                    console.print(
                        f"[dim]Cleaned up {len(deleted)} local media files[/dim]"
                    )
            except Exception as exc:  # noqa: BLE001
                if verbose and console is not None:
                    console.print(
                        f"[yellow]Warning:[/yellow] Stub failed: {exc}"
                    )
        elif live_upload and (not all_uploaded or not has_chunk_files):
            from screencap.recorder import (
                FOLLOWUP_FORCE_STOPPED,
                FOLLOWUP_NONE_UPLOADED,
                FOLLOWUP_PARTIAL,
                FOLLOWUP_UPLOAD_DISABLED,
            )

            if cp.was_force_stopped:
                followup_kind = FOLLOWUP_FORCE_STOPPED
            elif n_total == 0:
                followup_kind = FOLLOWUP_NONE_UPLOADED
            elif cp.upload_warning:
                followup_kind = FOLLOWUP_UPLOAD_DISABLED
            else:
                followup_kind = FOLLOWUP_PARTIAL
            try:
                import json as _json

                (capture_dir / ".upload_followup.json").write_text(
                    _json.dumps({
                        "kind": followup_kind,
                        "n_uploaded": n_uploaded,
                        "n_total": n_total,
                        "upload_warning": cp.upload_warning or None,
                    })
                )
                result["followup_kind"] = followup_kind
            except OSError:
                pass

        return result

    def stop_scrub_worker(self, *, timeout: float = 30.0) -> None:
        """Send poison pill, wait for the worker thread to drain.

        Always called *after* :meth:`finalize_catchall_scrub` — the
        ordering is load-bearing: a worker stopped first would silently
        drop the catch-all messages.
        """
        if self._scrub_worker is None:
            return
        try:
            self._scrub_worker.stop(timeout=timeout)
        except Exception:  # noqa: BLE001
            pass

    def _build_scrub_worker(
        self,
        *,
        recorder: Any,
        capture_dir: Path,
        console: Any,
    ) -> None:
        try:
            from screencap.privacy.scrub_worker import ScrubWorker

            sw = ScrubWorker(
                disable_q=self._channels.disable,
                recording_db_path=capture_dir / "recording.db",
                capture_dir=capture_dir,
                flush_requested=getattr(recorder, "_flush_requested", None),
                flush_ack_counter=getattr(recorder, "_flush_ack_counter", None),
                flush_lock=self._flush_lock,
            )
            sw.start()
            self._scrub_worker = sw
        except Exception as exc:  # noqa: BLE001
            self._scrub_worker = None
            # Cloud-bound recordings cannot ship un-scrubbed PII to GCS just
            # because the user didn't pass --verbose. Surface unconditionally
            # and SystemExit when the scrub worker is load-bearing.
            if self._request.cloud_intent and self._request.scrub_enabled:
                if console is not None:
                    console.print(
                        f"[red]Error:[/red] Scrub worker failed to start: {exc!r}\n"
                        "Cloud upload disabled because PII scrubbing is unavailable.",
                    )
                raise SystemExit(1) from exc
            if console is not None:
                console.print(
                    f"[yellow]Warning:[/yellow] Scrub worker failed to start: {exc!r}",
                )
