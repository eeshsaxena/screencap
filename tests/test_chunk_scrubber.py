"""Unit tests for the ChunkScrubber per-chunk execution seam (SCR-35, U2).

Exercise ``ChunkScrubber`` directly — no ``ChunkProcessor``, no recorder, no
multiprocessing queues (R6) — by patching ``screencap.scrubber.Scrubber`` with
a spy so the per-chunk construction + ``run_chunk`` delegation is provably
faithful (the U2 behavior-preserving guarantee).
"""

from types import SimpleNamespace
from unittest.mock import patch

from screencap.chunk_scrubber import ChunkScrubber


def _scrubber(tmp_path, **overrides):
    kwargs = dict(
        enabled=True,
        pipeline=object(),
        anonymizer=object(),
        evaluator=object(),
        classifier=object(),
        pixel_ratio=2.0,
    )
    kwargs.update(overrides)
    return ChunkScrubber(tmp_path, **kwargs)


def test_is_enabled_true_when_enabled_and_pipeline_present(tmp_path):
    assert _scrubber(tmp_path).is_enabled is True


def test_is_enabled_false_when_disabled(tmp_path):
    assert _scrubber(tmp_path, enabled=False).is_enabled is False


def test_is_enabled_false_when_pipeline_none(tmp_path):
    # Opted in but pipeline failed to construct → not enabled (the old
    # `_scrub_enabled and _pipeline is not None` guard, now in the seam).
    assert _scrubber(tmp_path, pipeline=None).is_enabled is False


def test_scrub_returns_none_and_builds_no_scrubber_when_off(tmp_path):
    cs = _scrubber(tmp_path, enabled=False)
    with patch("screencap.scrubber.Scrubber") as SpyScrubber:
        result = cs.scrub(0, 1.0, 2.0, None)
    assert result is None
    SpyScrubber.assert_not_called()


def test_scrub_constructs_scrubber_with_exact_config(tmp_path):
    """Behavior-preserving: every config arg reaches Scrubber unchanged."""
    pipeline, anon, ev, cl = object(), object(), object(), object()
    cs = ChunkScrubber(
        tmp_path, enabled=True, pipeline=pipeline, anonymizer=anon,
        evaluator=ev, classifier=cl, pixel_ratio=2.0,
    )
    fake_result = SimpleNamespace(audit_entries=[])
    with patch("screencap.scrubber.Scrubber") as SpyScrubber:
        SpyScrubber.return_value.run_chunk.return_value = fake_result
        out = cs.scrub(7, 100.0, 200.0, None)

    SpyScrubber.assert_called_once_with(
        tmp_path, pipeline=pipeline, anonymizer=anon, evaluator=ev,
        classifier=cl, pixel_ratio=2.0,
    )
    SpyScrubber.return_value.run_chunk.assert_called_once_with(
        idx=7, start_ts=100.0, end_ts=200.0, transcript_path=None,
    )
    assert out is fake_result


def test_scrub_returns_scrubber_result_unmodified(tmp_path):
    """The seam returns the exact ScrubResult so the caller drives the
    content-index pass off its blocked_intervals (R2)."""
    cs = _scrubber(tmp_path)
    fake_result = SimpleNamespace(audit_entries=[], blocked_intervals=["x"])
    with patch("screencap.scrubber.Scrubber") as SpyScrubber:
        SpyScrubber.return_value.run_chunk.return_value = fake_result
        out = cs.scrub(0, 1.0, 2.0, None)
    assert out is fake_result


def test_scrub_preserves_retina_pixel_ratio(tmp_path):
    """pixel_ratio=2.0 (Retina default) must travel through to Scrubber."""
    cs = _scrubber(tmp_path, pixel_ratio=2.0)
    with patch("screencap.scrubber.Scrubber") as SpyScrubber:
        SpyScrubber.return_value.run_chunk.return_value = SimpleNamespace(
            audit_entries=[]
        )
        cs.scrub(0, 1.0, 2.0, None)
    assert SpyScrubber.call_args.kwargs["pixel_ratio"] == 2.0
