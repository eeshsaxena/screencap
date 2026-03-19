"""Tests for screencap.namer — LLM auto-naming module."""

from __future__ import annotations

import io
import json
import sqlite3
import time
from pathlib import Path
from unittest import mock

import pytest

from screencap.namer import (
    _parse_llm_response,
    _run_provider_chain,
    _summarize_action_events,
    _update_task_description,
    assemble_context,
    auto_name,
    validate_slug,
)


# ---------------------------------------------------------------------------
# Slug validation
# ---------------------------------------------------------------------------


class TestValidateSlug:
    def test_valid_simple(self):
        assert validate_slug("hello") is True

    def test_valid_kebab(self):
        assert validate_slug("stripe-webhook-debugging") is True

    def test_valid_with_numbers(self):
        assert validate_slug("fix-bug-42") is True

    def test_valid_min_length(self):
        assert validate_slug("abc") is True

    def test_invalid_too_short(self):
        assert validate_slug("ab") is False

    def test_invalid_too_long(self):
        assert validate_slug("a" * 61) is False

    def test_invalid_uppercase(self):
        assert validate_slug("Hello-World") is False

    def test_invalid_leading_hyphen(self):
        assert validate_slug("-leading") is False

    def test_invalid_trailing_hyphen(self):
        assert validate_slug("trailing-") is False

    def test_invalid_double_hyphen(self):
        assert validate_slug("double--hyphen") is False

    def test_invalid_underscore(self):
        assert validate_slug("has_underscore") is False

    def test_invalid_spaces(self):
        assert validate_slug("has space") is False

    def test_invalid_not_string(self):
        assert validate_slug(123) is False

    def test_valid_all_numbers(self):
        assert validate_slug("123") is True

    def test_valid_number_prefix(self):
        assert validate_slug("42-fix") is True


# ---------------------------------------------------------------------------
# LLM response parsing
# ---------------------------------------------------------------------------


class TestParseLlmResponse:
    def test_clean_json(self):
        result = _parse_llm_response('{"slug": "my-name", "description": "desc"}')
        assert result == {"slug": "my-name", "description": "desc"}

    def test_json_in_markdown_fences(self):
        text = '```json\n{"slug": "my-name", "description": "desc"}\n```'
        result = _parse_llm_response(text)
        assert result["slug"] == "my-name"

    def test_json_in_plain_fences(self):
        text = '```\n{"slug": "my-name", "description": "desc"}\n```'
        result = _parse_llm_response(text)
        assert result["slug"] == "my-name"

    def test_json_embedded_in_text(self):
        text = 'Here is the name: {"slug": "my-name", "description": "desc"} done.'
        result = _parse_llm_response(text)
        assert result["slug"] == "my-name"

    def test_invalid_json(self):
        result = _parse_llm_response("not json at all")
        assert result is None

    def test_empty_string(self):
        result = _parse_llm_response("")
        assert result is None

    def test_whitespace_padding(self):
        result = _parse_llm_response('  \n{"slug": "padded", "description": "d"}\n  ')
        assert result["slug"] == "padded"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _make_recording_db(db_path: Path, *, with_screenshots: bool = False, with_events: bool = False, with_windows: bool = False):
    """Create a minimal recording.db for testing."""
    t = time.time()
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    cur.execute(
        "CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL, platform TEXT, task_description TEXT)"
    )
    cur.execute("INSERT INTO recording VALUES (1, ?, 'darwin', '')", (t,))

    cur.execute(
        "CREATE TABLE action_event (id INTEGER PRIMARY KEY, name TEXT, timestamp REAL, "
        "window_event_id INTEGER, window_event_timestamp REAL)"
    )

    if with_events:
        # Simulate chunked mode: window_event_id is NULL, but
        # window_event_timestamp is populated (matching window_event.timestamp).
        we_ts_1 = t if with_windows else None
        we_ts_2 = t + 1 if with_windows else None
        cur.execute(
            "INSERT INTO action_event VALUES (1, 'click', ?, NULL, ?)",
            (t + 0.1, we_ts_1),
        )
        cur.execute(
            "INSERT INTO action_event VALUES (2, 'type', ?, NULL, ?)",
            (t + 1.1, we_ts_2),
        )

    cur.execute(
        "CREATE TABLE window_event (id INTEGER PRIMARY KEY, timestamp REAL, title TEXT, "
        "app_bundle_id TEXT, app_version TEXT)"
    )

    if with_windows:
        cur.execute(
            "INSERT INTO window_event VALUES (1, ?, 'My Document - VS Code', 'com.microsoft.VSCode', '1.90')",
            (t,),
        )
        cur.execute(
            "INSERT INTO window_event VALUES (2, ?, 'GitHub Pull Request', 'com.google.Chrome', '131.0')",
            (t + 1,),
        )

    cur.execute(
        "CREATE TABLE screenshot (id INTEGER PRIMARY KEY, recording_timestamp REAL, "
        "recording_id INTEGER, timestamp REAL, png_data BLOB)"
    )

    if with_screenshots:
        # Create a small valid PNG
        from PIL import Image

        img = Image.new("RGB", (100, 100), color="red")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        for i in range(3):
            cur.execute(
                "INSERT INTO screenshot VALUES (?, ?, 1, ?, ?)",
                (i + 1, time.time(), time.time() + i, png_bytes),
            )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Summarize action events
# ---------------------------------------------------------------------------


class TestSummarizeActionEvents:
    def test_returns_window_titles_with_null_fk(self, tmp_path):
        """Chunked-mode recordings have window_event_id=NULL but
        window_event_timestamp populated — namer must still resolve titles."""
        db_path = tmp_path / "recording.db"
        _make_recording_db(db_path, with_events=True, with_windows=True)
        events = _summarize_action_events(db_path)
        assert len(events) >= 1
        titles = [e.get("window_title") for e in events]
        assert any(t is not None for t in titles), (
            "Expected at least one event with a window_title, "
            f"got: {events}"
        )


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------


class TestAssembleContext:
    def test_empty_dir(self, tmp_path):
        """No DB, no files — returns empty context."""
        ctx = assemble_context(tmp_path)
        assert ctx == {}

    def test_with_transcript(self, tmp_path):
        """Picks up transcript.json."""
        db_path = tmp_path / "recording.db"
        _make_recording_db(db_path)

        (tmp_path / "transcript.json").write_text(
            json.dumps({"text": "Hello world testing"}),
        )
        ctx = assemble_context(tmp_path)
        assert "transcript" in ctx
        assert "Hello world" in ctx["transcript"]

    def test_with_screenshots(self, tmp_path):
        """Extracts screenshots from DB and encodes as base64 JPEG."""
        db_path = tmp_path / "recording.db"
        _make_recording_db(db_path, with_screenshots=True)

        ctx = assemble_context(tmp_path)
        assert "screenshots_b64" in ctx
        assert len(ctx["screenshots_b64"]) == 3

    def test_with_window_titles(self, tmp_path):
        """Extracts window titles from DB."""
        db_path = tmp_path / "recording.db"
        _make_recording_db(db_path, with_windows=True)

        ctx = assemble_context(tmp_path)
        assert "window_titles" in ctx
        assert "My Document - VS Code" in ctx["window_titles"]

    def test_with_app_versions(self, tmp_path):
        """Picks up app_versions.json."""
        db_path = tmp_path / "recording.db"
        _make_recording_db(db_path)

        (tmp_path / "app_versions.json").write_text(
            json.dumps([{"name": "VS Code", "version": "1.90"}]),
        )
        ctx = assemble_context(tmp_path)
        assert "running_apps" in ctx
        assert "VS Code" in ctx["running_apps"]

    def test_transcript_truncation(self, tmp_path):
        """Very long transcript gets truncated."""
        db_path = tmp_path / "recording.db"
        _make_recording_db(db_path)

        long_text = " ".join(["word"] * 3000)
        (tmp_path / "transcript.json").write_text(json.dumps({"text": long_text}))
        ctx = assemble_context(tmp_path)
        assert ctx["transcript"].endswith("[truncated]")


# ---------------------------------------------------------------------------
# Provider chain
# ---------------------------------------------------------------------------


class TestProviderChain:
    def test_first_provider_succeeds(self):
        """If first provider succeeds, others are not tried."""
        ctx = {"transcript": "test"}
        good_result = {"slug": "test-name", "description": "Test"}

        with mock.patch("screencap.namer._try_claude_cli", return_value=good_result) as m_claude, \
             mock.patch("screencap.namer._try_chatgpt_cli") as m_chatgpt:
            result = _run_provider_chain(ctx)
            assert result == good_result
            m_claude.assert_called_once()
            m_chatgpt.assert_not_called()

    def test_fallthrough_to_second(self):
        """Falls through to second provider if first fails."""
        ctx = {"transcript": "test"}
        good_result = {"slug": "test-name", "description": "Test"}

        with mock.patch("screencap.namer._try_claude_cli", return_value=None), \
             mock.patch("screencap.namer._try_chatgpt_cli", return_value=good_result) as m_chatgpt:
            result = _run_provider_chain(ctx)
            assert result == good_result
            m_chatgpt.assert_called_once()

    def test_all_providers_fail(self):
        """Returns None when all providers fail."""
        ctx = {"transcript": "test"}

        with mock.patch("screencap.namer._try_claude_cli", return_value=None), \
             mock.patch("screencap.namer._try_chatgpt_cli", return_value=None), \
             mock.patch("screencap.namer._try_anthropic_api", return_value=None), \
             mock.patch("screencap.namer._try_openai_api", return_value=None), \
             mock.patch("screencap.namer._try_ollama", return_value=None):
            result = _run_provider_chain(ctx)
            assert result is None

    def test_local_only_skips_cloud(self):
        """local_only=True skips cloud providers."""
        ctx = {"transcript": "test"}
        good_result = {"slug": "local-name", "description": "Local"}

        with mock.patch("screencap.namer._try_claude_cli") as m_claude, \
             mock.patch("screencap.namer._try_chatgpt_cli") as m_chatgpt, \
             mock.patch("screencap.namer._try_anthropic_api") as m_anthropic, \
             mock.patch("screencap.namer._try_openai_api") as m_openai, \
             mock.patch("screencap.namer._try_ollama", return_value=good_result):
            result = _run_provider_chain(ctx, local_only=True)
            assert result == good_result
            m_claude.assert_not_called()
            m_chatgpt.assert_not_called()
            m_anthropic.assert_not_called()
            m_openai.assert_not_called()


# ---------------------------------------------------------------------------
# Name collision
# ---------------------------------------------------------------------------


class TestNameCollision:
    def test_no_collision(self, tmp_path):
        """No collision — uses slug as-is."""
        rec_dir = tmp_path / "rec-20260222T120000"
        rec_dir.mkdir()
        db_path = rec_dir / "recording.db"
        _make_recording_db(db_path)

        good_result = {"slug": "my-recording", "description": "Testing"}
        fake_ctx = {"transcript": "test"}

        with mock.patch("screencap.namer.assemble_context", return_value=fake_ctx), \
             mock.patch("screencap.namer._run_provider_chain", return_value=good_result):
            final = auto_name(rec_dir)

        assert final.name == "my-recording"
        assert final.exists()
        assert not rec_dir.exists()

    def test_collision_appends_counter(self, tmp_path):
        """Existing directory with same slug gets a counter appended."""
        (tmp_path / "my-recording").mkdir()

        rec_dir = tmp_path / "rec-20260222T120000"
        rec_dir.mkdir()
        db_path = rec_dir / "recording.db"
        _make_recording_db(db_path)

        good_result = {"slug": "my-recording", "description": "Testing"}
        fake_ctx = {"transcript": "test"}

        with mock.patch("screencap.namer.assemble_context", return_value=fake_ctx), \
             mock.patch("screencap.namer._run_provider_chain", return_value=good_result):
            final = auto_name(rec_dir)

        assert final.name == "my-recording-2"
        assert final.exists()

    def test_multiple_collisions(self, tmp_path):
        """Multiple collisions increment counter."""
        (tmp_path / "my-recording").mkdir()
        (tmp_path / "my-recording-2").mkdir()

        rec_dir = tmp_path / "rec-20260222T120000"
        rec_dir.mkdir()
        db_path = rec_dir / "recording.db"
        _make_recording_db(db_path)

        good_result = {"slug": "my-recording", "description": "Testing"}
        fake_ctx = {"transcript": "test"}

        with mock.patch("screencap.namer.assemble_context", return_value=fake_ctx), \
             mock.patch("screencap.namer._run_provider_chain", return_value=good_result):
            final = auto_name(rec_dir)

        assert final.name == "my-recording-3"


# ---------------------------------------------------------------------------
# DB update
# ---------------------------------------------------------------------------


class TestUpdateTaskDescription:
    def test_updates_recording_table(self, tmp_path):
        db_path = tmp_path / "recording.db"
        _make_recording_db(db_path)

        _update_task_description(db_path, "My description")

        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute("SELECT task_description FROM recording LIMIT 1")
        assert cur.fetchone()[0] == "My description"
        conn.close()

    def test_handles_missing_column_gracefully(self, tmp_path):
        """Does not crash if task_description column doesn't exist."""
        db_path = tmp_path / "recording.db"
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL)")
        cur.execute("INSERT INTO recording VALUES (1, ?)", (time.time(),))
        conn.commit()
        conn.close()

        # Should not raise
        _update_task_description(db_path, "desc")


# ---------------------------------------------------------------------------
# skip_rename
# ---------------------------------------------------------------------------


class TestSkipRename:
    def test_skip_rename_keeps_original_dir(self, tmp_path):
        """When skip_rename=True, directory is not renamed."""
        rec_dir = tmp_path / "rec-20260222T120000"
        rec_dir.mkdir()
        db_path = rec_dir / "recording.db"
        _make_recording_db(db_path)

        good_result = {"slug": "renamed", "description": "Testing"}
        fake_ctx = {"transcript": "test"}

        with mock.patch("screencap.namer.assemble_context", return_value=fake_ctx), \
             mock.patch("screencap.namer._run_provider_chain", return_value=good_result):
            final = auto_name(rec_dir, skip_rename=True)

        assert final == rec_dir
        assert rec_dir.exists()
        assert not (tmp_path / "renamed").exists()

    def test_skip_rename_still_updates_db(self, tmp_path):
        """Even with skip_rename, DB is updated with description."""
        rec_dir = tmp_path / "rec-20260222T120000"
        rec_dir.mkdir()
        db_path = rec_dir / "recording.db"
        _make_recording_db(db_path)

        good_result = {"slug": "renamed", "description": "Updated desc"}
        fake_ctx = {"transcript": "test"}

        with mock.patch("screencap.namer.assemble_context", return_value=fake_ctx), \
             mock.patch("screencap.namer._run_provider_chain", return_value=good_result):
            auto_name(rec_dir, skip_rename=True)

        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute("SELECT task_description FROM recording LIMIT 1")
        assert cur.fetchone()[0] == "Updated desc"
        conn.close()


# ---------------------------------------------------------------------------
# No providers available
# ---------------------------------------------------------------------------


class TestNoProviders:
    def test_returns_original_dir(self, tmp_path):
        """When all providers fail, returns original dir unchanged."""
        rec_dir = tmp_path / "rec-20260222T120000"
        rec_dir.mkdir()
        db_path = rec_dir / "recording.db"
        _make_recording_db(db_path)

        fake_ctx = {"transcript": "test"}

        with mock.patch("screencap.namer.assemble_context", return_value=fake_ctx), \
             mock.patch("screencap.namer._run_provider_chain", return_value=None):
            final = auto_name(rec_dir)

        assert final == rec_dir
        assert rec_dir.exists()
