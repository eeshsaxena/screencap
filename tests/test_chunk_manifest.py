"""Unit tests for the ChunkManifest seam (SCR-35).

These exercise ``ChunkManifest`` directly — no ``ChunkProcessor``, no
recorder, no multiprocessing queues (R6) — by patching the
``task_manifest.generate_manifest`` renderer. The v1/v2 *rendering* itself is
covered by ``tests/test_llm_segmentation.py`` and the manifest renderer
tests; here we only assert the orchestration ChunkManifest owns: mode
selection, blocked_intervals gathering, and partial-file cleanup on failure.
"""

from unittest.mock import MagicMock, patch

import pytest

from screencap.chunk_manifest import ChunkManifest


def test_produce_forwards_mode_and_threshold(tmp_path):
    """produce() delegates to generate_manifest with this recording's mode + threshold."""
    cm = ChunkManifest(tmp_path, segmentation_mode="idle", rest_threshold=42.0)
    with patch("screencap.task_manifest.generate_manifest") as gen:
        gen.return_value = tmp_path / "chunk_0000_manifest.json"
        out = cm.produce(0, 1000.0, 1060.0)

    gen.assert_called_once()
    assert gen.call_args.kwargs["segmentation_mode"] == "idle"
    assert gen.call_args.kwargs["rest_threshold"] == 42.0
    assert out == tmp_path / "chunk_0000_manifest.json"


def test_produce_defaults_to_llm_mode(tmp_path):
    cm = ChunkManifest(tmp_path)  # default segmentation_mode
    with patch("screencap.task_manifest.generate_manifest") as gen:
        cm.produce(0, 1.0, 2.0)
    assert gen.call_args.kwargs["segmentation_mode"] == "llm"


def test_produce_gathers_blocked_intervals_from_screen_filter(tmp_path):
    """produce() pulls blocked_intervals for the chunk window and forwards them."""
    sf = MagicMock()
    sf.get_blocked_intervals.return_value = [
        {"start_ts": 1000.5, "end_ts": 1010.0, "reason": "app_policy"},
    ]
    cm = ChunkManifest(tmp_path, screen_filter=sf)
    with patch("screencap.task_manifest.generate_manifest") as gen:
        cm.produce(3, 1000.0, 1060.0)

    sf.get_blocked_intervals.assert_called_once_with(1000.0, 1060.0)
    assert gen.call_args.kwargs["blocked_intervals"] == [
        {"start_ts": 1000.5, "end_ts": 1010.0, "reason": "app_policy"},
    ]


def test_produce_no_screen_filter_passes_none(tmp_path):
    cm = ChunkManifest(tmp_path, screen_filter=None)
    with patch("screencap.task_manifest.generate_manifest") as gen:
        cm.produce(0, 1.0, 2.0)
    assert gen.call_args.kwargs["blocked_intervals"] is None


def test_produce_empty_intervals_coalesce_to_none(tmp_path):
    sf = MagicMock()
    sf.get_blocked_intervals.return_value = []
    cm = ChunkManifest(tmp_path, screen_filter=sf)
    with patch("screencap.task_manifest.generate_manifest") as gen:
        cm.produce(0, 1.0, 2.0)
    assert gen.call_args.kwargs["blocked_intervals"] is None


def test_produce_screen_filter_error_is_swallowed(tmp_path):
    """A filter that raises is logged and treated as 'no intervals', not propagated."""
    sf = MagicMock()
    sf.get_blocked_intervals.side_effect = RuntimeError("boom")
    cm = ChunkManifest(tmp_path, screen_filter=sf)
    with patch("screencap.task_manifest.generate_manifest") as gen:
        cm.produce(0, 1.0, 2.0)  # must not raise
    assert gen.call_args.kwargs["blocked_intervals"] is None


def test_produce_screen_filter_without_method_passes_none(tmp_path):
    cm = ChunkManifest(tmp_path, screen_filter=object())  # no get_blocked_intervals
    with patch("screencap.task_manifest.generate_manifest") as gen:
        cm.produce(0, 1.0, 2.0)
    assert gen.call_args.kwargs["blocked_intervals"] is None


def test_produce_cleans_up_partial_manifest_on_failure(tmp_path):
    """A mid-write failure leaves no truncated manifest, and the error re-raises."""
    partial = tmp_path / "chunk_0000_manifest.json"

    def boom(*a, **kw):
        partial.write_text('{"partial":')  # simulate a partial write
        raise RuntimeError("manifest write blew up")

    cm = ChunkManifest(tmp_path)
    with patch("screencap.task_manifest.generate_manifest", side_effect=boom):
        with pytest.raises(RuntimeError):
            cm.produce(0, 1.0, 2.0)

    assert not partial.exists(), "partial manifest must be removed"
