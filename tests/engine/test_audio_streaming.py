"""Tests for streaming FLAC audio recording.

Verifies the streaming FLAC writer pattern used in record_audio():
locked buffer + flush thread + soundfile.SoundFile incremental writes.
"""

from __future__ import annotations

import multiprocessing
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import soundfile


SAMPLERATE = 16000
CHANNELS = 1


def _make_audio_chunk(duration_secs: float = 1.0) -> np.ndarray:
    """Generate a sine wave chunk (mono float32)."""
    n_samples = int(SAMPLERATE * duration_secs)
    t = np.linspace(0, duration_secs, n_samples, endpoint=False)
    return np.sin(2 * np.pi * 440 * t).astype(np.float32).reshape(-1, 1)


class TestStreamingFlacWriter:
    """Test the streaming FLAC write pattern directly (no record_audio)."""

    def test_write_multiple_chunks_produces_valid_flac(self, tmp_path):
        """Open writer, write 3 chunks, close — verify valid FLAC output."""
        flac_path = tmp_path / "audio.flac"
        chunk = _make_audio_chunk(1.0)

        with soundfile.SoundFile(
            str(flac_path), mode="w",
            samplerate=SAMPLERATE, channels=CHANNELS, format="FLAC",
        ) as sf_writer:
            for _ in range(3):
                sf_writer.write(chunk)

        info = soundfile.info(str(flac_path))
        assert info.samplerate == SAMPLERATE
        assert info.channels == CHANNELS
        assert abs(info.duration - 3.0) < 0.01

    def test_short_recording_single_write(self, tmp_path):
        """A single short write (< 30s) produces valid FLAC."""
        flac_path = tmp_path / "audio.flac"
        chunk = _make_audio_chunk(0.5)

        with soundfile.SoundFile(
            str(flac_path), mode="w",
            samplerate=SAMPLERATE, channels=CHANNELS, format="FLAC",
        ) as sf_writer:
            sf_writer.write(chunk)

        info = soundfile.info(str(flac_path))
        assert abs(info.duration - 0.5) < 0.01

    def test_zero_length_recording(self, tmp_path):
        """Closing writer without writing produces an empty file."""
        flac_path = tmp_path / "audio.flac"

        with soundfile.SoundFile(
            str(flac_path), mode="w",
            samplerate=SAMPLERATE, channels=CHANNELS, format="FLAC",
        ):
            pass  # no writes

        # libsndfile produces a zero-byte file when no frames are written
        assert flac_path.stat().st_size == 0

    def test_written_data_matches_input(self, tmp_path):
        """Data read back from FLAC matches what was written."""
        flac_path = tmp_path / "audio.flac"
        chunk = _make_audio_chunk(0.25)

        with soundfile.SoundFile(
            str(flac_path), mode="w",
            samplerate=SAMPLERATE, channels=CHANNELS, format="FLAC",
        ) as sf_writer:
            sf_writer.write(chunk)

        data, sr = soundfile.read(str(flac_path), dtype="float32")
        assert sr == SAMPLERATE
        # FLAC is lossless, but there may be tiny rounding — use generous atol
        np.testing.assert_allclose(
            data.reshape(-1, 1), chunk, atol=1e-4,
        )


class TestBufferDrainLogic:
    """Test the locked buffer + drain pattern used by record_audio."""

    def test_drain_returns_all_frames(self):
        """Draining returns concatenated frames and clears the buffer."""
        buffer = []
        lock = threading.Lock()

        chunk_a = _make_audio_chunk(0.1)
        chunk_b = _make_audio_chunk(0.2)

        with lock:
            buffer.append(chunk_a)
            buffer.append(chunk_b)

        # Drain
        with lock:
            drained = buffer[:]
            buffer.clear()
        result = np.concatenate(drained, axis=0)

        expected_len = len(chunk_a) + len(chunk_b)
        assert len(result) == expected_len
        assert len(buffer) == 0

    def test_drain_empty_buffer(self):
        """Draining an empty buffer returns nothing."""
        buffer = []
        lock = threading.Lock()

        with lock:
            if not buffer:
                result = None
            else:
                result = np.concatenate(buffer[:], axis=0)
                buffer.clear()

        assert result is None

    def test_concurrent_append_and_drain(self):
        """Concurrent appenders and drainer don't lose samples."""
        buffer: list[np.ndarray] = []
        lock = threading.Lock()
        total_appended = []
        n_appenders = 4
        chunks_per_appender = 50

        def appender():
            for _ in range(chunks_per_appender):
                chunk = _make_audio_chunk(0.01)  # 160 samples
                with lock:
                    buffer.append(chunk)
                    total_appended.append(len(chunk))

        drained_total = []

        def drainer():
            for _ in range(20):
                time.sleep(0.005)
                with lock:
                    if buffer:
                        d = buffer[:]
                        buffer.clear()
                    else:
                        continue
                frames = np.concatenate(d, axis=0)
                drained_total.append(len(frames))

        threads = [threading.Thread(target=appender) for _ in range(n_appenders)]
        drain_t = threading.Thread(target=drainer)
        drain_t.start()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        drain_t.join()

        # Final drain of anything left
        with lock:
            if buffer:
                frames = np.concatenate(buffer[:], axis=0)
                drained_total.append(len(frames))
                buffer.clear()

        expected = sum(total_appended)
        actual = sum(drained_total)
        assert actual == expected, f"Lost {expected - actual} samples"


class TestRecordAudioIntegration:
    """Integration tests for record_audio() with mocked sounddevice."""

    def _make_mock_stream(self, callback_fn, duration_secs=2.0):
        """Create a mock InputStream that feeds chunks to the callback."""
        mock_stream = MagicMock()
        mock_stream.samplerate = SAMPLERATE

        chunks_to_feed = []
        chunk_duration = 0.0625  # ~16 Hz callback like real sounddevice
        n_chunks = int(duration_secs / chunk_duration)
        for _ in range(n_chunks):
            chunks_to_feed.append(_make_audio_chunk(chunk_duration))

        feed_thread = None

        def start():
            nonlocal feed_thread

            def _feed():
                for chunk in chunks_to_feed:
                    callback_fn(chunk, len(chunk), None, MagicMock())
                    time.sleep(0.001)  # fast but not instant

            feed_thread = threading.Thread(target=_feed, daemon=True)
            feed_thread.start()

        mock_stream.start = start
        mock_stream.stop = MagicMock()
        mock_stream.close = MagicMock()
        mock_stream._feed_thread = lambda: feed_thread

        return mock_stream

    @patch("screencap.engine.recorder.crud")
    @patch("screencap.engine.recorder.get_session_for_path")
    @patch("screencap.engine.recorder.utils")
    def test_record_audio_produces_valid_flac(
        self, mock_utils, mock_get_session, mock_crud, tmp_path,
    ):
        """Full record_audio() with mocked sounddevice produces valid FLAC."""
        from screencap.engine.recorder import record_audio
        from screencap.engine.db.models import Recording

        mock_utils.set_start_time = MagicMock()
        mock_utils.get_timestamp.return_value = 1000.0

        db_path = str(tmp_path / "recording.db")
        Path(db_path).touch()

        recording = MagicMock(spec=Recording)
        recording.timestamp = 1000.0
        recording.id = 1

        terminate = multiprocessing.Event()
        started = multiprocessing.Event()

        captured_callback = {}

        def mock_input_stream(callback, samplerate, channels):
            captured_callback["fn"] = callback
            return self._make_mock_stream(callback, duration_secs=1.0)

        with patch("sounddevice.InputStream", side_effect=mock_input_stream):
            def stop_after_delay():
                time.sleep(1.5)
                terminate.set()

            stopper = threading.Thread(target=stop_after_delay, daemon=True)
            stopper.start()

            record_audio(recording, db_path, terminate, started)

        assert started.is_set()

        flac_path = tmp_path / "audio.flac"
        assert flac_path.exists()

        info = soundfile.info(str(flac_path))
        assert info.samplerate == SAMPLERATE
        assert info.channels == CHANNELS
        assert info.frames > 0
        assert info.duration > 0

        # Verify insert_audio_info was called
        mock_crud.insert_audio_info.assert_called_once()
        call_args = mock_crud.insert_audio_info.call_args
        # Args are now: (session, recording, start_timestamp, SAMPLERATE, [])
        assert call_args[0][4] == []  # word_list

    @patch("screencap.engine.recorder.crud")
    @patch("screencap.engine.recorder.get_session_for_path")
    @patch("screencap.engine.recorder.utils")
    def test_record_audio_short_recording(
        self, mock_utils, mock_get_session, mock_crud, tmp_path,
    ):
        """Short recording (< flush interval) — all frames in final flush."""
        from screencap.engine.recorder import record_audio
        from screencap.engine.db.models import Recording

        mock_utils.set_start_time = MagicMock()
        mock_utils.get_timestamp.return_value = 1000.0

        db_path = str(tmp_path / "recording.db")
        Path(db_path).touch()

        recording = MagicMock(spec=Recording)
        recording.timestamp = 1000.0
        recording.id = 1

        terminate = multiprocessing.Event()
        started = multiprocessing.Event()

        def mock_input_stream(callback, samplerate, channels):
            return self._make_mock_stream(callback, duration_secs=0.5)

        with patch("sounddevice.InputStream", side_effect=mock_input_stream):
            def stop_quickly():
                time.sleep(0.8)
                terminate.set()

            stopper = threading.Thread(target=stop_quickly, daemon=True)
            stopper.start()

            record_audio(recording, db_path, terminate, started)

        flac_path = tmp_path / "audio.flac"
        info = soundfile.info(str(flac_path))
        assert info.frames > 0
        assert info.duration < 30  # well under flush interval

    @patch("screencap.engine.recorder.crud")
    @patch("screencap.engine.recorder.get_session_for_path")
    @patch("screencap.engine.recorder.utils")
    def test_record_audio_immediate_stop(
        self, mock_utils, mock_get_session, mock_crud, tmp_path,
    ):
        """Immediate stop with no frames — lazy writer (SCR-218 U2) produces NO
        FLAC at all (so has_audio stays false), and skips the DB insert."""
        from screencap.engine.recorder import record_audio
        from screencap.engine.db.models import Recording

        mock_utils.set_start_time = MagicMock()
        mock_utils.get_timestamp.return_value = 1000.0

        db_path = str(tmp_path / "recording.db")
        Path(db_path).touch()

        recording = MagicMock(spec=Recording)
        recording.timestamp = 1000.0
        recording.id = 1

        terminate = multiprocessing.Event()
        started = multiprocessing.Event()

        def mock_input_stream(callback, samplerate, channels):
            # No chunks fed — immediate stop
            mock = MagicMock()
            mock.samplerate = SAMPLERATE
            mock.start = MagicMock()
            mock.stop = MagicMock()
            mock.close = MagicMock()
            return mock

        with patch("sounddevice.InputStream", side_effect=mock_input_stream):
            # Set terminate before starting — immediate stop
            terminate.set()
            record_audio(recording, db_path, terminate, started)

        flac_path = tmp_path / "audio.flac"
        # Lazy writer: no captured frame → the FLAC is never opened, so no file
        # (not even an empty header) lands on disk and has_audio stays false.
        assert not flac_path.exists()
        # insert_audio_info should NOT have been called
        mock_crud.insert_audio_info.assert_not_called()

    @patch("screencap.engine.recorder.crud")
    @patch("screencap.engine.recorder.get_session_for_path")
    @patch("screencap.engine.recorder.utils")
    def test_duration_matches_expected(
        self, mock_utils, mock_get_session, mock_crud, tmp_path,
    ):
        """Recorded duration approximately matches mock stream duration."""
        from screencap.engine.recorder import record_audio
        from screencap.engine.db.models import Recording

        mock_utils.set_start_time = MagicMock()
        mock_utils.get_timestamp.return_value = 1000.0

        db_path = str(tmp_path / "recording.db")
        Path(db_path).touch()

        recording = MagicMock(spec=Recording)
        recording.timestamp = 1000.0
        recording.id = 1

        terminate = multiprocessing.Event()
        started = multiprocessing.Event()

        target_duration = 2.0

        def mock_input_stream(callback, samplerate, channels):
            return self._make_mock_stream(callback, duration_secs=target_duration)

        with patch("sounddevice.InputStream", side_effect=mock_input_stream):
            def stop_after():
                time.sleep(target_duration + 0.5)
                terminate.set()

            stopper = threading.Thread(target=stop_after, daemon=True)
            stopper.start()

            record_audio(recording, db_path, terminate, started)

        flac_path = tmp_path / "audio.flac"
        info = soundfile.info(str(flac_path))
        # Allow 20% tolerance due to timing in mocked threads
        assert abs(info.duration - target_duration) < target_duration * 0.2
