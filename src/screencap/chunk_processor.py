"""Background chunk processing during continuous recording.

Runs as a non-daemon thread: at each video auto-cut, processes the
completed chunk (transcribe → export events → generate manifest →
upload → delete old chunks).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

def _safe_delete(path: Path) -> None:
    """Permanently delete a file from the filesystem."""
    path.unlink()


class ChunkProcessor:
    """Process completed recording chunks in the background.

    Per chunk: wait for audio ack → transcribe → export events JSONL →
    generate task manifest → upload → delete old chunks on success.
    Never raises — all errors caught and logged.
    """

    def __init__(
        self,
        capture_dir: Path,
        chunk_process_q,  # multiprocessing.Queue — receives rotation events from fan-out
        audio_ack_q,      # multiprocessing.Queue — receives audio rotation/final acks
        *,
        recording_name: str | None = None,
        upload_enabled: bool = True,
        auto_delete: bool = True,
        rest_threshold: float = 120.0,
        flush_requested=None,      # multiprocessing.Event — triggers writer buffer flush
        flush_ack_counter=None,    # multiprocessing.Value('i') — counts writer acks
        cloud_intent: bool = False,
        privacy_mode: str = "internal",
        screen_filter=None,
        segmentation_mode: str = "llm",
    ) -> None:
        self._capture_dir = Path(capture_dir)
        self._db_path = self._capture_dir / "recording.db"
        self._q = chunk_process_q
        self._audio_ack_q = audio_ack_q
        self._recording_name = recording_name or self._capture_dir.name
        self._upload_enabled = upload_enabled
        self._auto_delete = auto_delete
        self._rest_threshold = rest_threshold
        self._flush_requested = flush_requested
        self._flush_ack_counter = flush_ack_counter
        self._cloud_intent = cloud_intent
        self._privacy_mode = privacy_mode
        self._screen_filter = screen_filter
        self._segmentation_mode = segmentation_mode

        # Initialize scrubbing pipeline for cloud-intent recordings
        self._pipeline = None
        self._anonymizer = None
        self._masking_classifier = None
        self._masking_evaluator = None
        self._masking_pixel_ratio = 2.0  # safe Retina default
        self._upload_disabled_reason: str | None = None
        if cloud_intent and upload_enabled:
            try:
                from screencap.privacy import Anonymizer, create_default_pipeline
                self._pipeline = create_default_pipeline(require_pii=True)
                self._anonymizer = Anonymizer()
                logger.info("Scrubbing pipeline initialized for cloud-intent recording")
            except Exception as e:
                logger.error(
                    f"Privacy deps not available — disabling uploads for safety: {e}"
                )
                self._upload_enabled = False
                self._upload_disabled_reason = f"Privacy deps not available: {e}"
                logger.warning(
                    "Privacy dependencies are missing. "
                    "Reinstall or update screencap."
                )

            # Initialize classifier/evaluator for screenshot masking
            try:
                from screencap.config import get_privacy_config
                from screencap.privacy.context import DefaultContextClassifier
                from screencap.privacy.policy import DefaultPolicyEvaluator, PrivacyMode

                _pc = get_privacy_config()
                # Cloud uploads must use public mode so that CHAT/EMAIL/etc.
                # apps get MASK_WINDOW (blurred in screenshots) instead of
                # TEXT_REDACT (which only scrubs text, not visuals).
                if self._cloud_intent:
                    from dataclasses import replace as _dc_replace
                    _pc = _dc_replace(_pc, mode=PrivacyMode.PUBLIC)
                self._masking_evaluator = DefaultPolicyEvaluator(_pc)
                self._masking_classifier = DefaultContextClassifier(
                    app_classes=_pc.app_classes,
                )
            except Exception as e:
                logger.warning(f"Could not init masking classifier: {e}")
                self._upload_enabled = False
                if self._upload_disabled_reason is None:
                    self._upload_disabled_reason = f"Masking classifier init failed: {e}"

        self._chunk_results: dict[int, bool] = {}  # idx → all_uploaded
        self._status_lock = threading.Lock()
        self._status: str = ""
        self._total_freed: int = 0
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @property
    def status(self) -> str:
        """Thread-safe status string for Rich Live display."""
        with self._status_lock:
            return self._status

    @property
    def upload_warning(self) -> str | None:
        """Reason uploads were disabled, or None if uploads are healthy."""
        return self._upload_disabled_reason

    def _set_status(self, s: str) -> None:
        with self._status_lock:
            self._status = s

    def all_chunks_uploaded(self) -> bool:
        """True if every processed chunk was uploaded successfully.

        Returns False if no chunks were processed at all — that means
        the video writer likely failed and no rotation events arrived.
        """
        if not self._chunk_results:
            return False
        return all(self._chunk_results.values())

    def upload_summary(self) -> tuple[int, int]:
        """Return (n_uploaded, n_total) from chunk results.

        Must only be called after stop() — _chunk_results is not
        thread-safe for concurrent reads.
        """
        n_total = len(self._chunk_results)
        n_uploaded = sum(1 for v in self._chunk_results.values() if v)
        return n_uploaded, n_total

    @property
    def was_force_stopped(self) -> bool:
        """True if stop() timed out and had to force-stop the thread.

        When True, _chunk_results may be incomplete — a chunk that was
        mid-processing when _stop_event fired will have no entry.
        """
        return self._stop_event.is_set()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=False, name="chunk_processor",
        )
        self._thread.start()

    def stop(self, timeout: float = 300.0) -> None:
        """Send poison pill, wait for drain, then force-stop if needed."""
        if self._thread is None:
            return
        try:
            self._q.put({"type": "poison_pill"}, timeout=5)
        except Exception:
            logger.debug("ChunkProcessor already stopped, skipping shutdown signal")
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logger.warning(
                f"ChunkProcessor did not finish in {timeout}s, signaling stop"
            )
            # Signal the thread to abort current work and exit
            self._stop_event.set()
            # Give it a short grace period to notice the stop event
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                logger.warning("ChunkProcessor thread still alive after stop signal")

    def _run(self) -> None:
        import queue as _queue_mod

        consecutive_errors = 0
        while not self._stop_event.is_set():
            try:
                msg = self._q.get(timeout=2.0)
                consecutive_errors = 0  # reset on successful get
            except _queue_mod.Empty:
                # Normal timeout — queue has no messages yet, just keep waiting
                continue
            except (OSError, EOFError, BrokenPipeError):
                # Queue pipe is broken (child processes died) — exit loop
                logger.debug("ChunkProcessor queue broken, exiting")
                break
            except Exception:
                consecutive_errors += 1
                # If queue is consistently failing, it's dead — exit
                if consecutive_errors >= 5:
                    logger.debug("ChunkProcessor queue unresponsive, exiting")
                    break
                continue
            if msg.get("type") == "poison_pill":
                break
            try:
                self._process_chunk(msg)
            except Exception:
                idx = msg.get("completed_index", -1)
                logger.exception(f"Chunk {idx} processing failed")
                self._chunk_results[idx] = False

    def _process_chunk(self, msg: dict) -> None:
        idx = msg["completed_index"]
        start_ts = msg["chunk_start_time"]
        end_ts = msg["rotation_time"]
        is_final = msg.get("type") == "final_chunk"

        self._set_status(f"Chunk {idx}: waiting for audio...")

        # 1. Wait for audio ack
        self._wait_for_audio(idx, is_final=is_final)
        if self._stop_event.is_set():
            return

        # 2. Transcribe audio
        self._set_status(f"Chunk {idx}: transcribing...")
        transcript_path = self._transcribe(idx)
        if self._stop_event.is_set():
            return

        # 3. Flush writer buffers → export events from DB
        self._set_status(f"Chunk {idx}: flushing writers...")
        self._trigger_flush()
        if self._stop_event.is_set():
            return
        self._set_status(f"Chunk {idx}: exporting events...")
        self._export_events(idx, start_ts, end_ts)
        if self._stop_event.is_set():
            return

        # 4. Generate task manifest (with blocked intervals if available)
        self._set_status(f"Chunk {idx}: generating manifest...")
        blocked_intervals = None
        if self._screen_filter is not None and hasattr(self._screen_filter, 'get_blocked_intervals'):
            try:
                blocked_intervals = self._screen_filter.get_blocked_intervals(start_ts, end_ts) or None
            except Exception:
                logger.warning(f"Failed to get blocked_intervals for chunk {idx}", exc_info=True)
        self._generate_manifest(idx, start_ts, end_ts, blocked_intervals=blocked_intervals)

        # 5. Scrub text surfaces for cloud-intent recordings
        if self._cloud_intent and self._pipeline is not None:
            self._set_status(f"Chunk {idx}: scrubbing...")
            self._scrub_chunk_files(idx, transcript_path)
            if self._stop_event.is_set():
                return

        # 5b. Mask sensitive window regions in screenshots
        if self._cloud_intent and self._masking_classifier is not None:
            self._set_status(f"Chunk {idx}: masking screenshots...")
            try:
                self._mask_chunk_screenshots(idx, start_ts, end_ts)
            except Exception:
                logger.warning(f"Screenshot masking failed for chunk {idx}", exc_info=True)
            if self._stop_event.is_set():
                return

        # 6. Upload
        success = False
        if self._upload_enabled:
            self._set_status(f"Chunk {idx}: uploading...")
            files = self._collect_chunk_files(idx, transcript_path)
            if files:
                success = self._upload_chunk(idx, files)
            else:
                logger.warning(f"No files found for chunk {idx}")
        else:
            success = self._upload_disabled_reason is None

        self._chunk_results[idx] = success

        # 7. Delete old chunks (keep 2 most recent)
        if success and self._auto_delete:
            freed = self._delete_old_chunks(idx, keep_recent=2)
            self._total_freed += freed

        n_done = sum(1 for v in self._chunk_results.values() if v)
        if self._total_freed > 0:
            freed_str = _fmt_bytes(self._total_freed)
            self._set_status(f"{n_done} chunks uploaded, {freed_str} freed")
        elif n_done > 0:
            self._set_status(f"Chunk {idx} uploaded")
        else:
            self._set_status("")

    def _mask_chunk_screenshots(self, idx: int, start_ts: float, end_ts: float) -> None:
        """Mask sensitive window regions in chunk screenshots before upload.

        Uses stored per-screenshot window geometry for selective masking.
        Falls back to full-frame masking when geometry is unavailable.
        Does NOT block the chunk pipeline on error.
        """
        if self._masking_classifier is None or self._masking_evaluator is None:
            return

        chunk_dir = self._capture_dir / f"chunk_{idx}"
        screenshots_dir = chunk_dir / "screenshots"
        if not screenshots_dir.is_dir():
            return

        from screencap.privacy.context import (
            load_window_geometry,
            parse_screenshot_timestamp,
        )
        from screencap.privacy.masking import (
            MaskStrategy,
            mask_screenshot,
            window_regions_from_geometry,
        )
        from screencap.privacy.policy import ContextClass

        db_path = self._db_path
        masked_count = 0

        # Shared connection for geometry lookups across the loop
        _geom_conn = None
        try:
            _geom_conn = sqlite3.connect(str(db_path))
        except Exception:
            pass

        try:
            for img_path in sorted(screenshots_dir.glob("*.jpg")):
                ts = parse_screenshot_timestamp(img_path.name)
                if ts is None:
                    continue

                geom = None
                try:
                    geom = load_window_geometry(db_path, ts, conn=_geom_conn)
                except Exception:
                    pass

                # Attempt selective masking if geometry is available
                selective_applied = False
                if geom is not None:
                    try:
                        from PIL import Image
                        with Image.open(img_path) as probe:
                            img_w, img_h = probe.size

                        regions = window_regions_from_geometry(
                            geom.windows, img_w, img_h, self._masking_pixel_ratio,
                            self._masking_classifier, self._masking_evaluator,
                            display_origin=geom.display_origin,
                        )
                        if regions:
                            mask_screenshot(
                                img_path,
                                None,
                                regions=regions,
                            )
                            masked_count += 1
                        # No regions = no sensitive windows visible; keep as-is
                        selective_applied = True
                    except Exception:
                        logger.debug(
                            f"Selective masking failed for {img_path.name}",
                            exc_info=True,
                        )

                # Fallback: full-frame masking when geometry unavailable or
                # selective masking failed. Conservative for cloud upload path.
                if not selective_applied:
                    try:
                        mask_screenshot(
                            img_path,
                            ContextClass.UNKNOWN,
                            strategy=MaskStrategy.FULL_WINDOW,
                        )
                        masked_count += 1
                    except Exception:
                        # Fail-closed: delete unmasked screenshot before cloud upload
                        logger.warning(
                            f"All masking failed for {img_path.name} — deleting for safety",
                            exc_info=True,
                        )
                        img_path.unlink(missing_ok=True)
        finally:
            if _geom_conn is not None:
                _geom_conn.close()

        if masked_count > 0:
            logger.info(f"Chunk {idx}: masked {masked_count} screenshots")

    def _trigger_flush(self) -> None:
        """Trigger writer processes to flush DB buffers before event export.

        Cross-process flush protocol: sets flush_requested Event → writers
        call flush_buffers() and increment counter → we wait and clear.
        """
        if self._flush_requested is None or self._flush_ack_counter is None:
            return

        with self._flush_ack_counter.get_lock():
            self._flush_ack_counter.value = 0

        self._flush_requested.set()

        # Poll for acks — wait up to 5s, stop early once acks stabilize
        deadline = time.time() + 5.0
        prev_acked = 0
        stable_since = time.time()
        while time.time() < deadline and not self._stop_event.is_set():
            time.sleep(0.1)
            with self._flush_ack_counter.get_lock():
                acked = self._flush_ack_counter.value
            if acked > prev_acked:
                prev_acked = acked
                stable_since = time.time()
            elif acked > 0 and time.time() - stable_since > 1.0:
                # No new acks for 1s after at least one — all active writers done
                break

        self._flush_requested.clear()

        with self._flush_ack_counter.get_lock():
            acked = self._flush_ack_counter.value
        if acked == 0:
            logger.debug("Flush skipped: writers already finished")
        else:
            logger.info(f"Flush: {acked} writer(s) flushed buffers")

    def _wait_for_audio(self, idx: int, *, is_final: bool = False) -> None:
        """Wait for audio process to confirm rotation/finalization."""
        deadline = time.time() + 60
        while time.time() < deadline:
            if self._stop_event.is_set():
                return
            try:
                msg = self._audio_ack_q.get(timeout=2.0)
                completed = msg.get("completed_index")
                msg_type = msg.get("type")
                if completed == idx:
                    return
                if is_final and msg_type == "audio_final":
                    return
            except (OSError, EOFError, BrokenPipeError):
                # Audio process died, queue broken — proceed without ack
                logger.debug(f"Audio ack queue broken for chunk {idx}, proceeding")
                return
            except Exception:
                continue
        logger.warning(f"Audio ack for chunk {idx} not received in 60s, proceeding")

    def _transcribe(self, idx: int) -> Path | None:
        """Transcribe audio chunk quietly (no print output). Returns transcript path or None."""
        audio_path = self._capture_dir / f"audio_{idx:04d}.flac"
        if not audio_path.exists():
            logger.info(f"No audio file for chunk {idx}, skipping transcription")
            return None

        # Skip 0-byte audio files (empty final chunks)
        if audio_path.stat().st_size == 0:
            logger.info(f"Audio file for chunk {idx} is empty, skipping transcription")
            return None

        transcript_path = self._capture_dir / f"transcript_{idx:04d}.txt"
        transcript_json_path = self._capture_dir / f"transcript_{idx:04d}.json"

        if transcript_path.exists():
            return transcript_path

        if self._stop_event.is_set():
            return None

        # Try faster-whisper (quiet — no print output)
        try:
            from faster_whisper import WhisperModel

            logger.info(f"Transcribing chunk {idx} with faster-whisper...")
            model = WhisperModel("base", device="cpu", compute_type="int8")
            segments_iter, _info = model.transcribe(
                str(audio_path), word_timestamps=True,
            )
            segments = []
            full_text_parts = []
            for segment in segments_iter:
                if self._stop_event.is_set():
                    return None
                segments.append({
                    "start": segment.start,
                    "end": segment.end,
                    "text": segment.text.strip(),
                })
                full_text_parts.append(segment.text)

            transcript = "".join(full_text_parts).strip()
            _save_transcript_quiet(
                transcript, segments, transcript_path, transcript_json_path,
            )
            logger.info(f"Saved transcript for chunk {idx} ({len(segments)} segments)")
            return transcript_path
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"faster-whisper failed for chunk {idx}: {e}")

        if self._stop_event.is_set():
            return None

        # Try openai-whisper (quiet)
        try:
            import whisper

            logger.info(f"Transcribing chunk {idx} with openai-whisper...")
            whisper_model = whisper.load_model("base")
            result = whisper_model.transcribe(
                str(audio_path), fp16=False, word_timestamps=True,
            )
            transcript = result.get("text", "").strip()
            segments = []
            for seg in result.get("segments", []):
                segments.append({
                    "start": seg["start"],
                    "end": seg["end"],
                    "text": seg["text"].strip(),
                })
            _save_transcript_quiet(
                transcript, segments, transcript_path, transcript_json_path,
            )
            logger.info(f"Saved transcript for chunk {idx} ({len(segments)} segments)")
            return transcript_path
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"openai-whisper failed for chunk {idx}: {e}")

        if self._stop_event.is_set():
            return None

        # Try OpenAI API
        api_key = os.environ.get("OPENAI_API_KEY")
        if api_key:
            try:
                self._transcribe_api(
                    api_key, audio_path, transcript_path, transcript_json_path,
                )
                return transcript_path
            except Exception as e:
                logger.warning(f"OpenAI API transcription failed for chunk {idx}: {e}")

        logger.info(f"No transcription backend available for chunk {idx}")
        return None

    def _transcribe_api(
        self, api_key: str, audio_path: Path,
        transcript_path: Path, transcript_json_path: Path,
    ) -> None:
        """Transcribe using OpenAI Whisper API."""
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        with open(audio_path, "rb") as f:
            resp = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="verbose_json",
            )
        transcript_path.write_text(resp.text)
        transcript_json_path.write_text(json.dumps(resp.model_dump(), indent=2))

    def _export_events(self, idx: int, start_ts: float, end_ts: float) -> Path:
        """Export events from recording.db as JSONL for this chunk's time range.

        Uses the shared processing pipeline (same as CLI ``screencap export``)
        so both paths produce identical JSONL format.

        Steps: query action_event + window_event by time range →
        dict_to_action_event → process_events → deduplicate window →
        interleave → serialize.  Includes initial window context (last
        window event before chunk start) and format_version: 2 header.
        Mouse.move events are excluded by default.

        Chunk boundary note: the processing pipeline is stateful (click
        merging, typing aggregation).  A mouse.down at chunk end may stay
        unmerged — this is an accepted trade-off documented as a known
        limitation (orphan events at boundaries).
        """
        from screencap.engine.convert import dict_to_action_event
        from screencap.engine.events import MouseMoveEvent
        from screencap.engine.processing import (
            deduplicate_window_events,
            interleave_window_events,
            process_events,
        )
        from screencap.exporter import build_export_metadata, build_privacy_filter

        jsonl_path = self._capture_dir / f"events_{idx:04d}.jsonl"
        if jsonl_path.exists():
            return jsonl_path

        conn = sqlite3.connect(str(self._db_path))
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA query_only=ON")
        conn.row_factory = sqlite3.Row
        try:
            # 1. Query action events and convert to Pydantic
            action_rows = conn.execute(
                "SELECT * FROM action_event WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
                (start_ts, end_ts),
            ).fetchall()

            raw_events = []
            for row in action_rows:
                try:
                    evt = dict_to_action_event(dict(row))
                    if evt is not None:
                        raw_events.append(evt)
                except Exception:
                    logger.debug(f"Skipping malformed action event at ts={row['timestamp']}")

            # 2. Run through processing pipeline
            processed = process_events(raw_events)

            # 3. Exclude mouse.move by default
            processed = [e for e in processed if not isinstance(e, MouseMoveEvent)]

            # 4. Query window events for deduplication
            window_rows = self._query_window_events(conn, start_ts, end_ts)

            # 5. Add initial window context (last window before chunk start)
            initial_ctx = self._query_initial_window_context(conn, start_ts)
            if initial_ctx is not None:
                window_rows = [initial_ctx] + window_rows

            window_switches = deduplicate_window_events(window_rows)

            # 6. Apply privacy filter for cloud-intent
            if self._cloud_intent:
                pf = build_privacy_filter(
                    privacy_mode=self._privacy_mode,
                    cloud_intent=True,
                )
                window_switches = [
                    filtered for ws in window_switches
                    if (filtered := pf(ws)) is not None
                ]

            # 7. Interleave
            combined = interleave_window_events(processed, window_switches)

            # 8. Build metadata header
            meta = build_export_metadata(exclude_moves=True)

            # 9. Atomic write
            tmp_path = str(jsonl_path) + ".tmp"
            with open(tmp_path, "w") as f:
                f.write(json.dumps(meta) + "\n")
                for evt in combined:
                    f.write(evt.model_dump_json() + "\n")
            os.rename(tmp_path, str(jsonl_path))

            n_action = len(processed)
            n_window = len(window_switches)
            logger.info(
                f"Exported {n_action} action + {n_window} window events to {jsonl_path.name}"
            )
        except Exception:
            # Clean up partial .tmp on failure
            tmp_path = str(jsonl_path) + ".tmp"
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
        finally:
            conn.close()

        return jsonl_path

    def _query_window_events(
        self, conn: sqlite3.Connection, start_ts: float, end_ts: float,
    ) -> list[dict]:
        """Query window_event table for a time range. Returns list of dicts."""
        try:
            rows = conn.execute(
                "SELECT * FROM window_event WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
                (start_ts, end_ts),
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            # window_event table may not exist in older DBs
            logger.debug("window_event table not found, skipping window events")
            return []

    def _query_initial_window_context(
        self, conn: sqlite3.Connection, start_ts: float,
    ) -> dict | None:
        """Query the last window_event before chunk start for initial context."""
        try:
            row = conn.execute(
                "SELECT * FROM window_event WHERE timestamp < ? ORDER BY timestamp DESC LIMIT 1",
                (start_ts,),
            ).fetchone()
            if row is not None:
                d = dict(row)
                # Set timestamp to just before chunk start so it appears first
                d["timestamp"] = start_ts - 0.001
                return d
            return None
        except sqlite3.OperationalError:
            return None

    def _generate_manifest(
        self, idx: int, start_ts: float, end_ts: float,
        blocked_intervals: list[dict] | None = None,
    ) -> Path:
        """Generate task manifest for this chunk."""
        from screencap.task_manifest import generate_manifest

        return generate_manifest(
            self._capture_dir, idx, start_ts, end_ts,
            rest_threshold=self._rest_threshold,
            blocked_intervals=blocked_intervals,
            segmentation_mode=self._segmentation_mode,
        )

    def _scrub_chunk_files(self, idx: int, transcript_path: Path | None) -> None:
        """Inline-scrub text surfaces before upload for cloud-intent recordings.

        Scrubs events JSONL (combined keystroke aggregation), transcript .txt/.json,
        and manifest dominant_title. Uses atomic writes (.tmp + rename).
        Per-field failure → <SCRUB_FAILED> sentinel. Per-file failure → file
        marked for skip (renamed to .scrub_failed).
        """
        # --- Events JSONL ---
        events_path = self._capture_dir / f"events_{idx:04d}.jsonl"
        if events_path.exists():
            try:
                self._scrub_events_jsonl(events_path)
            except Exception as e:
                logger.error(f"Chunk {idx}: events JSONL scrub failed, skipping file: {e}")
                _rename_scrub_failed(events_path)

        # --- Transcript .txt ---
        if transcript_path and transcript_path.exists():
            try:
                self._scrub_transcript_txt(transcript_path)
            except Exception as e:
                logger.error(f"Chunk {idx}: transcript .txt scrub failed, skipping file: {e}")
                _rename_scrub_failed(transcript_path)

        # --- Transcript .json (defense-in-depth) ---
        transcript_json = self._capture_dir / f"transcript_{idx:04d}.json"
        if transcript_json.exists():
            try:
                self._scrub_transcript_json(transcript_json)
            except Exception as e:
                logger.error(f"Chunk {idx}: transcript .json scrub failed, skipping file: {e}")
                _rename_scrub_failed(transcript_json)

        # --- Manifest (dominant_title + derived_name) ---
        manifest_path = self._capture_dir / f"chunk_{idx:04d}_manifest.json"
        if manifest_path.exists():
            try:
                self._scrub_manifest(manifest_path)
            except Exception as e:
                logger.error(f"Chunk {idx}: manifest scrub failed, skipping file: {e}")
                _rename_scrub_failed(manifest_path)

    def _scrub_text_field(self, text: str) -> str:
        """Run a single text field through the pipeline. Returns scrubbed text.

        On AllDetectorsFailedError returns '<SCRUB_FAILED>'.
        """
        from screencap.privacy import AllDetectorsFailedError

        if not text or not text.strip():
            return text
        try:
            result = self._pipeline.detect(text)
        except AllDetectorsFailedError:
            return "<SCRUB_FAILED>"
        return self._anonymizer.anonymize(result.normalized_text, result.detections)

    def _scrub_events_jsonl(self, path: Path) -> None:
        """Scrub PII in events JSONL using the shared scrubber function.

        Delegates to ``scrub_events_jsonl()`` from the scrubber module for
        full-depth recursive scrubbing of all string fields in all events.
        """
        from screencap.scrubber import scrub_events_jsonl

        had_errors = scrub_events_jsonl(
            events_jsonl=path,
            pipeline=self._pipeline,
            anonymizer=self._anonymizer,
        )
        if had_errors:
            _rename_scrub_failed(path)

    def _scrub_transcript_txt(self, path: Path) -> None:
        """Scrub PII from transcript .txt file."""
        text = path.read_text(encoding="utf-8")
        scrubbed = self._scrub_text_field(text)
        tmp_path = str(path) + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(scrubbed)
        os.rename(tmp_path, str(path))

    def _scrub_transcript_json(self, path: Path) -> None:
        """Scrub PII from transcript .json file (text + segments)."""
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            if "text" in data and isinstance(data["text"], str):
                data["text"] = self._scrub_text_field(data["text"])
            if "segments" in data and isinstance(data["segments"], list):
                for seg in data["segments"]:
                    if isinstance(seg, dict) and isinstance(seg.get("text"), str):
                        seg["text"] = self._scrub_text_field(seg["text"])
        tmp_path = str(path) + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.rename(tmp_path, str(path))

    def _scrub_manifest(self, path: Path) -> None:
        """Scrub PII from manifest.

        v2 manifests (format_version: 2) have no tasks/titles — nothing to scrub.
        Legacy manifests: scrub dominant_title and re-derive task name.
        """
        data = json.loads(path.read_text(encoding="utf-8"))

        # v2 manifests have no text fields to scrub
        if data.get("format_version", 0) >= 2:
            return

        # Legacy manifest: scrub task titles
        from screencap.task_manifest import _derive_task_name

        for task in data.get("tasks", []):
            title = task.get("dominant_title", "")
            if title:
                scrubbed_title = self._scrub_text_field(title)
                task["dominant_title"] = scrubbed_title
                task["derived_name"] = _derive_task_name({
                    "bundle_id": task.get("dominant_app", ""),
                    "title": scrubbed_title,
                })
        if data.get("tasks"):
            primary = max(
                data["tasks"],
                key=lambda t: t.get("end_ts", 0) - t.get("start_ts", 0),
            )["derived_name"]
            data.setdefault("summary", {})["primary_task"] = primary

        tmp_path = str(path) + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.rename(tmp_path, str(path))

    def _collect_chunk_files(self, idx: int, transcript_path: Path | None) -> list[dict]:
        """Collect files belonging to this chunk for upload.

        Cloud-intent recordings include video (with placeholder frames for
        blocked intervals) and audio alongside text files.
        Skips 0-byte files and files renamed to .scrub_failed.
        """
        files = []
        patterns = [
            f"chunk_{idx:04d}.mp4",
            f"audio_{idx:04d}.flac",
            f"events_{idx:04d}.jsonl",
            f"chunk_{idx:04d}_manifest.json",
        ]
        if transcript_path and transcript_path.exists():
            patterns.append(transcript_path.name)

        for name in patterns:
            path = self._capture_dir / name
            if path.exists() and path.stat().st_size > 0:
                files.append({"name": name, "path": path})
            elif path.exists() and path.stat().st_size == 0:
                logger.info(f"Skipping 0-byte file: {name}")

        return files

    def _upload_chunk(self, idx: int, files: list[dict]) -> bool:
        """Upload chunk files silently. Returns True if core files succeeded.

        Transcript upload failure is non-fatal — the chunk is still
        considered uploaded if video/audio/events succeed.
        Retries once after a 5-second backoff on failure.
        """
        for attempt in range(2):
            try:
                return upload_chunk_files(
                    self._recording_name, files, self._capture_dir,
                )
            except Exception as e:
                if attempt == 0:
                    logger.warning(f"Chunk {idx} upload attempt 1 failed: {e}, retrying in 5s...")
                    time.sleep(5)
                else:
                    logger.error(f"Chunk {idx} upload failed after retry: {e}")
        return False

    def _delete_old_chunks(self, current_idx: int, keep_recent: int = 2) -> int:
        """Delete media files for old uploaded chunks. Returns bytes freed."""
        freed = 0
        for old_idx in range(0, current_idx - keep_recent + 1):
            if not self._chunk_results.get(old_idx, False):
                continue  # not uploaded — keep
            for ext_pattern in [
                f"chunk_{old_idx:04d}.mp4",
                f"audio_{old_idx:04d}.flac",
                f"events_{old_idx:04d}.jsonl",
            ]:
                path = self._capture_dir / ext_pattern
                if path.exists():
                    try:
                        freed += path.stat().st_size
                        _safe_delete(path)
                        logger.info(f"Deleted {path.name}")
                    except OSError as e:
                        logger.warning(f"Failed to delete {path.name}: {e}")
        return freed


def _rename_scrub_failed(path: Path) -> None:
    """Rename a file to .scrub_failed so it's excluded from upload."""
    try:
        failed_path = path.with_suffix(path.suffix + ".scrub_failed")
        path.rename(failed_path)
    except OSError as e:
        logger.warning(f"Failed to rename {path.name} for scrub failure: {e}")


def _save_transcript_quiet(
    transcript: str,
    segments: list[dict],
    transcript_path: Path,
    transcript_json_path: Path,
) -> None:
    """Save transcript to files without any print output."""
    transcript_path.write_text(transcript, encoding="utf-8")
    transcript_json_path.write_text(
        json.dumps({"text": transcript, "segments": segments}, indent=2),
        encoding="utf-8",
    )


def upload_chunk_files(
    recording_name: str, files: list[dict], capture_dir: Path,
) -> bool:
    """Upload chunk files silently (no progress bars).

    Returns True if all core files uploaded. Transcript failures are
    logged but treated as non-fatal.
    """
    from screencap.upload import (
        FileInfo,
        _content_type,
        request_signed_urls,
    )

    file_infos = []
    for f in files:
        p = Path(f["path"])
        file_infos.append(FileInfo(
            name=f["name"],
            path=p,
            content_type=_content_type(p),
            size=p.stat().st_size,
        ))

    if not file_infos:
        return True

    try:
        urls, gcs_prefix = request_signed_urls(recording_name, file_infos)
    except Exception as e:
        logger.error(f"Failed to get signed URLs: {e}")
        return False

    core_ok = True
    for fi in file_infos:
        is_core = not fi.name.startswith("transcript")
        if fi.name not in urls:
            # Server didn't return a URL at all — file was rejected/unknown
            if is_core:
                logger.error(f"Server returned no URL for core file {fi.name}")
                core_ok = False
            continue
        url = urls[fi.name]
        if url is None:
            continue  # server confirms already uploaded
        try:
            _upload_single(fi, url)
        except Exception as e:
            if not is_core:
                logger.warning(f"Non-fatal: failed to upload {fi.name}: {e}")
            else:
                logger.error(f"Failed to upload {fi.name}: {e}")
                core_ok = False

    # Write per-chunk status if core files succeeded
    if core_ok:
        status_file = capture_dir / f".chunk_{files[0]['name'].split('.')[0]}_status.json"
        try:
            status_file.write_text(json.dumps({
                "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "files": [f["name"] for f in files],
            }))
        except Exception:
            pass

    return core_ok


def _upload_single(fi, signed_url: str) -> None:
    """Upload a single file without progress tracking."""
    import requests

    with open(fi.path, "rb") as fh:
        resp = requests.put(
            signed_url,
            data=fh,
            headers={
                "Content-Type": fi.content_type,
                "Content-Length": str(fi.size),
            },
            timeout=(10, 600),
        )
    resp.raise_for_status()


def checkpoint_and_upload_db(
    capture_dir: Path, recording_name: str, *, cloud_intent: bool = False,
) -> bool:
    """WAL checkpoint recording.db then upload it.

    For cloud_intent=True, skips upload — raw DB contains unscrubbed PII.
    The DB stays local for post-hoc scrubbed upload via ``screencap upload``.
    """
    if cloud_intent:
        logger.info("Skipping recording.db upload for cloud-intent recording (unscrubbed)")
        return True
    db_path = capture_dir / "recording.db"
    if not db_path.exists():
        return False

    # Checkpoint — fold WAL into main DB
    try:
        conn = sqlite3.connect(str(db_path))
        result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        conn.close()
        if result and result[0] > 0:
            logger.warning(f"WAL checkpoint: {result[0]} blocked pages")
    except Exception as e:
        logger.warning(f"WAL checkpoint failed: {e}")

    # Upload recording.db
    from screencap.upload import FileInfo, _content_type, request_signed_urls

    fi = FileInfo(
        name="recording.db",
        path=db_path,
        content_type=_content_type(db_path),
        size=db_path.stat().st_size,
    )
    try:
        urls, _ = request_signed_urls(recording_name, [fi])
        if "recording.db" not in urls:
            logger.error("Server returned no URL for recording.db")
            return False
        url = urls["recording.db"]
        if url:
            _upload_single(fi, url)
        return True
    except Exception as e:
        logger.error(f"Failed to upload recording.db: {e}")
        return False


def _build_sentinel_data(
    recording_name: str,
    stop_reason: str,
    chunks_expected: int,
) -> dict:
    """Build sentinel dict for recording_complete.json."""
    import uuid
    from datetime import datetime, timezone

    from screencap import __version__

    return {
        "version": 1,
        "recording_name": recording_name,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "stop_reason": stop_reason,
        "screencap_version": __version__,
        "chunks_expected": chunks_expected,
        "sentinel_id": str(uuid.uuid4()),
    }


def upload_sentinel(
    capture_dir: Path,
    recording_name: str,
    *,
    stop_reason: str = "graceful",
    chunks_expected: int = 0,
) -> bool:
    """Create and upload recording_complete.json sentinel to trigger stitching.

    Always creates the local file even if upload fails (for recovery via
    ``screencap upload``).  Returns True if upload succeeded.
    """
    from screencap.upload import FileInfo, _content_type, request_signed_urls

    sentinel_data = _build_sentinel_data(recording_name, stop_reason, chunks_expected)
    sentinel_path = capture_dir / "recording_complete.json"

    # Write locally (atomic: tmp + rename)
    tmp_path = sentinel_path.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(json.dumps(sentinel_data, indent=2))
        tmp_path.rename(sentinel_path)
    except Exception as e:
        logger.warning(f"Failed to write local sentinel: {e}")
        # Try non-atomic fallback
        try:
            sentinel_path.write_text(json.dumps(sentinel_data, indent=2))
        except Exception:
            return False

    # Upload
    fi = FileInfo(
        name="recording_complete.json",
        path=sentinel_path,
        content_type="application/json",
        size=sentinel_path.stat().st_size,
    )
    try:
        urls, _ = request_signed_urls(recording_name, [fi])
        if "recording_complete.json" not in urls:
            logger.error("Server returned no URL for recording_complete.json")
            return False
        url = urls["recording_complete.json"]
        if url:
            _upload_single(fi, url)
        return True
    except Exception as e:
        logger.error(f"Failed to upload sentinel: {e}")
        return False


def stub_recording(recording_dir: Path) -> list[str]:
    """Delete media files from a fully-uploaded recording, keeping metadata."""
    keep_patterns = {
        "recording.db", "capture.db", ".upload_status.json",
        "session_summary.json", "profiling.json",
        "recording_complete.json",
    }
    keep_prefixes = (".chunk_", "chunk_")
    keep_suffixes = ("_manifest.json", ".json", ".txt")
    deleted = []

    for p in sorted(recording_dir.iterdir()):
        if not p.is_file():
            continue
        if p.name in keep_patterns:
            continue
        if any(p.name.startswith(pf) for pf in keep_prefixes) and p.name.endswith(".json"):
            continue
        if p.name.startswith("transcript") and p.name.endswith(".txt"):
            continue
        if p.suffix in (".mp4", ".flac", ".jsonl", ".png", ".jpg"):
            try:
                _safe_delete(p)
                deleted.append(p.name)
            except OSError as e:
                logger.warning(f"Failed to delete {p.name}: {e}")

    # Delete screenshots subdirectory
    screenshots_dir = recording_dir / "screenshots"
    if screenshots_dir.is_dir():
        for p in screenshots_dir.iterdir():
            if p.is_file():
                try:
                    _safe_delete(p)
                    deleted.append(f"screenshots/{p.name}")
                except OSError:
                    pass
        try:
            screenshots_dir.rmdir()
        except OSError:
            pass

    return deleted


def _fmt_bytes(n: int) -> str:
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.1f} GB"
