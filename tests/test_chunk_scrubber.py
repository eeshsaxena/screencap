"""Unit tests for the ChunkScrubber per-chunk execution seam (SCR-35, U2).

Exercise ``ChunkScrubber`` directly — no ``ChunkProcessor``, no recorder, no
multiprocessing queues (R6) — by patching ``screencap.scrubber.Scrubber`` with
a spy so the per-chunk construction + ``run_chunk`` delegation is provably
faithful (the U2 behavior-preserving guarantee).
"""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from screencap.chunk_scrubber import ChunkScrubber, ScrubInit


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


# ---------------------------------------------------------------------------
# U3: ChunkScrubber.create factory — the scrub/masking-init decision matrix.
# ---------------------------------------------------------------------------


@contextmanager
def _patched_deps(*, pipeline_exc=None, privacy_exc=None):
    """Patch the scrub/masking deps the factory imports.

    Mirrors the patch targets the existing chunk_processor init tests use.
    Inject ``pipeline_exc`` to fail create_default_pipeline, or ``privacy_exc``
    to fail get_privacy_config (the masking classifier/evaluator init).
    """
    patches = []
    if pipeline_exc is not None:
        patches.append(patch(
            "screencap.redaction.engine.create_default_pipeline",
            side_effect=pipeline_exc,
        ))
    else:
        patches.append(patch(
            "screencap.redaction.engine.create_default_pipeline",
            return_value=MagicMock(),
        ))
        patches.append(patch("screencap.redaction.engine.Anonymizer", MagicMock()))
    if privacy_exc is not None:
        patches.append(patch(
            "screencap.config.get_privacy_config", side_effect=privacy_exc,
        ))
    else:
        patches.append(patch(
            "screencap.privacy.policy.DefaultPolicyEvaluator",
            return_value=MagicMock(),
        ))
        patches.append(patch(
            "screencap.privacy.classify.DefaultContextClassifier",
            return_value=MagicMock(),
        ))
    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in patches:
            p.stop()


def test_create_inert_when_not_opted_in(tmp_path):
    """Neither cloud-intent nor opt-in → inert seam, no upload-policy change."""
    seam, init = ChunkScrubber.create(
        tmp_path, cloud_intent=False, upload_enabled=True, scrub_enabled=False,
    )
    assert seam.is_enabled is False
    assert seam.has_masking_context is False
    assert init == ScrubInit(disable_uploads_reason=None)


def test_create_local_optin_deps_present(tmp_path):
    with _patched_deps():
        seam, init = ChunkScrubber.create(
            tmp_path, cloud_intent=False, upload_enabled=False, scrub_enabled=True,
        )
    assert seam.is_enabled is True
    assert seam.has_masking_context is True
    assert init.disable_uploads_reason is None


def test_create_cloud_intent_deps_missing_disables_uploads(tmp_path):
    """Cloud-intent pipeline-deps failure → fail-closed: uploads disabled."""
    with _patched_deps(pipeline_exc=ImportError("no privacy deps")):
        seam, init = ChunkScrubber.create(
            tmp_path, cloud_intent=True, upload_enabled=True, scrub_enabled=False,
        )
    assert seam.is_enabled is False
    assert init.disable_uploads_reason is not None
    assert "Privacy deps" in init.disable_uploads_reason


def test_create_local_optin_deps_missing_keeps_uploads(tmp_path):
    """Local opt-in deps failure → scrubbing silently off, uploads untouched."""
    with _patched_deps(pipeline_exc=ImportError("no privacy deps")):
        seam, init = ChunkScrubber.create(
            tmp_path, cloud_intent=False, upload_enabled=False, scrub_enabled=True,
        )
    assert seam.is_enabled is False
    assert init.disable_uploads_reason is None


def test_create_cloud_intent_masking_failure_disables_uploads(tmp_path):
    """Cloud-intent masking-init failure → uploads disabled AND no masking context."""
    with _patched_deps(privacy_exc=RuntimeError("masking init failed")):
        seam, init = ChunkScrubber.create(
            tmp_path, cloud_intent=True, upload_enabled=True, scrub_enabled=True,
        )
    assert init.disable_uploads_reason is not None
    assert "Masking classifier" in init.disable_uploads_reason
    assert seam.has_masking_context is False


def test_create_cloud_intent_uses_public_masking_mode(tmp_path):
    """Cloud uploads force PrivacyMode.PUBLIC so masked apps get MASK_WINDOW."""
    from screencap.privacy.policy import PrivacyMode

    with patch(
        "screencap.redaction.engine.create_default_pipeline", return_value=MagicMock(),
    ), patch("screencap.redaction.engine.Anonymizer", MagicMock()), patch(
        "screencap.privacy.policy.DefaultPolicyEvaluator", return_value=MagicMock(),
    ) as MockEval, patch(
        "screencap.privacy.classify.DefaultContextClassifier", return_value=MagicMock(),
    ):
        ChunkScrubber.create(
            tmp_path, cloud_intent=True, upload_enabled=True, scrub_enabled=True,
        )
    # The evaluator is built from a config whose mode was overridden to PUBLIC.
    cfg = MockEval.call_args.args[0]
    assert cfg.mode == PrivacyMode.PUBLIC
