"""Tests for the ``screencap transcribe`` CLI subcommand.

The Whisper backend is mocked end-to-end (no model load, no audio decode), so
these run fully offline. The focus here is the success-path Rich-markup safety
covered by SCR-169 — the saved paths and transcript preview are interpolated
into Rich-markup strings and must be escaped.
"""

from __future__ import annotations

from unittest import mock

from click.testing import CliRunner

from screencap.cli import cli


def _make_recording_with_audio(base, name):
    """Create a recording dir with a >=1KB audio.flac so transcribe proceeds."""
    rec_dir = base / name
    rec_dir.mkdir(parents=True)
    # transcribe rejects audio < 1024 bytes as empty/corrupt.
    (rec_dir / "audio.flac").write_bytes(b"\x00" * 2048)
    return rec_dir


def test_transcribe_success_escapes_rich_markup(tmp_path):
    """SCR-169: the ``transcribe`` success path interpolates the saved transcript
    paths and the transcript *preview* into Rich-markup strings. The transcript
    text is content the recorded audio produced (Whisper output over arbitrary
    speech) — not developer-controlled — so it can carry markup metacharacters.
    A preview containing an unbalanced ``[/]`` makes Rich raise ``MarkupError``
    and crash the command after transcription already succeeded; a balanced
    ``[x]`` run is silently stripped. The dynamic segments must be escaped so the
    brackets survive verbatim.

    The Whisper backend is mocked: ``_select_local_model`` returns a local model
    without touching ``_ensure_whisper_backend`` (no model download), and the
    resolved local transcribe fn writes a bracketed transcript instead of running
    inference."""
    from rich.errors import MarkupError

    _make_recording_with_audio(tmp_path, "demo")

    def fake_transcribe(audio_path, transcript_path, transcript_json_path, model):
        # Stand in for Whisper: emit a transcript carrying markup metacharacters.
        transcript_path.write_text("the meeting we[/]ird notes are [secret] here")
        transcript_json_path.write_text("[]")

    with mock.patch("screencap.config.get_recordings_dir", return_value=tmp_path), \
         mock.patch(
             "screencap.transcription._select_local_model",
             return_value=("local", "base"),
         ), \
         mock.patch(
             "screencap.engine.cli._transcribe_faster_whisper",
             side_effect=fake_transcribe,
         ), \
         mock.patch(
             "screencap.engine.cli._transcribe_local",
             side_effect=fake_transcribe,
         ):
        result = CliRunner().invoke(cli, ["transcribe", "demo", "--model", "base"])

    assert not isinstance(result.exception, MarkupError), result.exception
    assert result.exit_code == 0
    # Escaped, so the literal brackets survive in the preview instead of
    # crashing/stripping.
    assert "we[/]ird" in result.output
    assert "[secret]" in result.output
