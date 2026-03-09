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

        # Initialize scrubbing pipeline for cloud-intent recordings
        self._pipeline = None
        self._anonymizer = None
        if cloud_intent and upload_enabled:
            try:
                from screencap.privacy import Anonymizer, create_default_pipeline
                self._pipeline = create_default_pipeline()
                self._anonymizer = Anonymizer()
                logger.info("Scrubbing pipeline initialized for cloud-intent recording")
            except ImportError as e:
                logger.error(
                    f"Privacy deps not installed — disabling uploads for safety: {e}"
                )
                self._upload_enabled = False

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
            logger.warning("Failed to send poison pill to ChunkProcessor")
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
            success = True  # upload disabled = success

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
            logger.warning("Flush: no writers acknowledged (writers may have exited)")
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
        """Export events from recording.db as JSONL for this chunk's time range."""
        jsonl_path = self._capture_dir / f"events_{idx:04d}.jsonl"
        if jsonl_path.exists():
            return jsonl_path

        conn = sqlite3.connect(str(self._db_path))
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA query_only=ON")
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM action_event WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
                (start_ts, end_ts),
            ).fetchall()
            with open(jsonl_path, "w") as f:
                for row in rows:
                    f.write(json.dumps(dict(row)) + "\n")
            logger.info(f"Exported {len(rows)} events to {jsonl_path.name}")
        finally:
            conn.close()

        return jsonl_path

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
        )

    def _scrub_chunk_files(self, idx: int, transcript_path: Path | None) -> None:
        """Inline-scrub text surfaces before upload for cloud-intent recordings.

        Scrubs events JSONL (combined keystroke aggregation), transcript .txt/.json,
        and manifest dominant_title. Uses atomic writes (.tmp + rename).
        Per-field failure → <SCRUB_FAILED> sentinel. Per-file failure → file
        marked for skip (renamed to .scrub_failed).
        """
        from screencap.privacy import AllDetectorsFailedError
        from screencap.privacy.actions import KEYSTROKE_CONTENT_FIELDS

        pipeline = self._pipeline
        anonymizer = self._anonymizer

        # --- Events JSONL ---
        events_path = self._capture_dir / f"events_{idx:04d}.jsonl"
        if events_path.exists():
            try:
                self._scrub_events_jsonl(events_path, pipeline, anonymizer)
            except Exception as e:
                logger.error(f"Chunk {idx}: events JSONL scrub failed, skipping file: {e}")
                _rename_scrub_failed(events_path)

        # --- Transcript .txt ---
        if transcript_path and transcript_path.exists():
            try:
                self._scrub_transcript_txt(transcript_path, pipeline, anonymizer)
            except Exception as e:
                logger.error(f"Chunk {idx}: transcript .txt scrub failed, skipping file: {e}")
                _rename_scrub_failed(transcript_path)

        # --- Transcript .json (defense-in-depth) ---
        transcript_json = self._capture_dir / f"transcript_{idx:04d}.json"
        if transcript_json.exists():
            try:
                self._scrub_transcript_json(transcript_json, pipeline, anonymizer)
            except Exception as e:
                logger.error(f"Chunk {idx}: transcript .json scrub failed, skipping file: {e}")
                _rename_scrub_failed(transcript_json)

        # --- Manifest (dominant_title + derived_name) ---
        manifest_path = self._capture_dir / f"chunk_{idx:04d}_manifest.json"
        if manifest_path.exists():
            try:
                self._scrub_manifest(manifest_path, pipeline, anonymizer)
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

    def _scrub_events_jsonl(self, path: Path, pipeline, anonymizer) -> None:
        """Scrub keystroke content in events JSONL using combined-text aggregation."""
        from screencap.privacy.actions import KEYSTROKE_CONTENT_FIELDS

        lines = path.read_text(encoding="utf-8").splitlines()
        events = []
        for line in lines:
            if line.strip():
                events.append(json.loads(line))

        # Aggregate consecutive key.down events into combined text strings
        # to defeat the 4-char short-circuit in DetectionPipeline
        i = 0
        while i < len(events):
            evt = events[i]
            if evt.get("name") != "key.down" or not evt.get("key_char"):
                i += 1
                continue

            # Collect run of consecutive key.down events
            run_start = i
            combined_chars = []
            while i < len(events) and events[i].get("name") == "key.down":
                ch = events[i].get("key_char", "")
                combined_chars.append(ch if ch else "")
                i += 1

            combined_text = "".join(combined_chars)
            if len(combined_text) < 4:
                # Too short for detection — leave as-is
                continue

            # Run detection on combined text
            scrubbed = self._scrub_text_field(combined_text)
            if scrubbed == combined_text:
                # No PII detected
                continue

            # PII found — null keystroke content fields for this entire run
            for j in range(run_start, run_start + len(combined_chars)):
                for field in KEYSTROKE_CONTENT_FIELDS:
                    if field in events[j]:
                        events[j][field] = None

        # Also scrub any text fields on non-keystroke events
        for evt in events:
            if evt.get("name") in ("key.down", "key.up"):
                continue
            if "text" in evt and isinstance(evt["text"], str) and evt["text"]:
                evt["text"] = self._scrub_text_field(evt["text"])

        # Atomic write
        tmp_path = str(path) + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            for evt in events:
                f.write(json.dumps(evt) + "\n")
        os.rename(tmp_path, str(path))

    def _scrub_transcript_txt(self, path: Path, pipeline, anonymizer) -> None:
        """Scrub PII from transcript .txt file."""
        text = path.read_text(encoding="utf-8")
        scrubbed = self._scrub_text_field(text)
        tmp_path = str(path) + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(scrubbed)
        os.rename(tmp_path, str(path))

    def _scrub_transcript_json(self, path: Path, pipeline, anonymizer) -> None:
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

    def _scrub_manifest(self, path: Path, pipeline, anonymizer) -> None:
        """Scrub dominant_title in manifest and re-derive task name from clean title."""
        from screencap.task_manifest import _derive_task_name

        data = json.loads(path.read_text(encoding="utf-8"))
        for task in data.get("tasks", []):
            title = task.get("dominant_title", "")
            if title:
                scrubbed_title = self._scrub_text_field(title)
                task["dominant_title"] = scrubbed_title
                # Re-derive task name from scrubbed title
                task["derived_name"] = _derive_task_name({
                    "bundle_id": task.get("dominant_app", ""),
                    "title": scrubbed_title,
                })
        # Update primary_task in summary
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
        url = urls.get(fi.name)
        if url is None:
            continue  # already uploaded or skipped
        try:
            _upload_single(fi, url)
        except Exception as e:
            # Transcript failure is non-fatal
            if fi.name.startswith("transcript"):
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
        url = urls.get("recording.db")
        if url:
            _upload_single(fi, url)
        return True
    except Exception as e:
        logger.error(f"Failed to upload recording.db: {e}")
        return False


def stub_recording(recording_dir: Path) -> list[str]:
    """Delete media files from a fully-uploaded recording, keeping metadata."""
    keep_patterns = {
        "recording.db", "capture.db", ".upload_status.json",
        "session_summary.json", "profiling.json",
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
