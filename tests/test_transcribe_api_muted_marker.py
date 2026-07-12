"""The OpenAI Whisper API transcription backend must apply the muted marker too
(SCR-218 U6). Regression test for the review finding that _transcribe_api wrote
the raw response verbatim, bypassing the drop-muted-speech + marker step that the
two local whisper backends run — a cloud-bound privacy leak on that one path.
"""

from __future__ import annotations

import json
import multiprocessing
import sys
import types

from screencap.chunk_processor import ChunkProcessor
from screencap.engine.db import crud
from screencap.redaction.muted_marker import MUTED_MARKER_TEXT


def _install_fake_openai(monkeypatch):
    class _Resp:
        text = "leak clean"

        def model_dump(self):
            # Compressed FLAC-relative segments: "leak" sits inside the muted
            # span (must be dropped), "clean" is well outside (must survive).
            return {
                "text": "leak clean",
                "segments": [
                    {"start": 20.0, "end": 21.0, "text": "leak"},
                    {"start": 2.0, "end": 3.0, "text": "clean"},
                ],
            }

    class _Transcriptions:
        def create(self, **_kw):
            return _Resp()

    class _Audio:
        def __init__(self):
            self.transcriptions = _Transcriptions()

    class _OpenAI:
        def __init__(self, api_key=None):
            self.audio = _Audio()

    fake = types.ModuleType("openai")
    fake.OpenAI = _OpenAI
    monkeypatch.setitem(sys.modules, "openai", fake)


def test_transcribe_api_drops_muted_speech_and_marks(recording_db, monkeypatch):
    capture_dir = recording_db.db_path.parent
    # Muted span 1020-1035 (recording-relative); chunk span 1000-1060.
    crud.open_muted_interval(recording_db.session, recording_db.recording, 1020.0)
    crud.close_muted_interval(recording_db.session, recording_db.recording, 1035.0)

    audio = capture_dir / "audio_0000.flac"
    audio.write_bytes(b"x")
    tpath = capture_dir / "transcript_0000.txt"
    jpath = capture_dir / "transcript_0000.json"

    _install_fake_openai(monkeypatch)

    cp = ChunkProcessor(
        capture_dir,
        multiprocessing.Queue(),
        multiprocessing.Queue(),
        recording_name="test",
        upload_enabled=False,
        auto_delete=False,
    )

    cp._transcribe_api("key", audio, tpath, jpath, 1000.0, 1060.0)

    saved = json.loads(jpath.read_text())
    texts = [s["text"] for s in saved["segments"]]
    assert MUTED_MARKER_TEXT in texts, "API backend must insert the muted marker"
    assert "leak" not in texts, "muted-span speech must be dropped on the API path"
    assert "clean" in texts, "legit speech must survive"
    assert "leak" not in tpath.read_text(), "muted speech must not reach the .txt"
