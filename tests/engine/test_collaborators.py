"""Tests for the ``RecordingCollaborators`` helper (SCR-44, slice 7 of SCR-31).

Promotes the inline construction + lifecycle of ``RecorderPrivacyFilter``,
``ChunkProcessor``, and ``ScrubWorker`` from ``_run_screen_recorder`` into
an engine-owned helper. The shared flush primitives
(``_flush_requested``, ``_flush_ack_counter``, ``_engine_flush_lock``)
become engine-internal as a consequence — once the helper owns the
collaborators, no module outside the engine holds a reference to engine
queues past ``Recorder.__exit__()`` and the ``_NoCloseProxy`` workaround
is deleted.

These tests pin the helper's behavioural contract. The Tier-3 flush-lock
serialization test lives in ``test_collaborator_serialization.py``.
"""

from __future__ import annotations

from unittest import mock


def _make_helper(legacy=None):
    from screencap.engine.collaborators import RecordingCollaborators
    from screencap.engine.config import RecordingConfig
    from screencap.engine.screen_recorder import (
        IpcChannels,
        LegacyOptions,
        RecordingRequest,
    )

    request = RecordingRequest(name="t", config=RecordingConfig())
    channels = IpcChannels.create()
    return RecordingCollaborators(
        request=request,
        legacy=legacy if legacy is not None else LegacyOptions(),
        channels=channels,
    )


# ---------------------------------------------------------------------------
# Cycle 1 — smoke test
# ---------------------------------------------------------------------------


def test_recording_collaborators_module_exposes_class():
    """Smoke: the module exists and exposes ``RecordingCollaborators``.

    Constructor takes ``request`` / ``legacy`` / ``channels`` kwargs (mirrors
    the bundles already on ``ScreenRecorder``).
    """
    from screencap.engine.collaborators import RecordingCollaborators
    from screencap.engine.config import RecordingConfig
    from screencap.engine.screen_recorder import (
        IpcChannels,
        LegacyOptions,
        RecordingRequest,
    )

    request = RecordingRequest(name="smoke", config=RecordingConfig())
    channels = IpcChannels.create()
    legacy = LegacyOptions()

    helper = RecordingCollaborators(
        request=request, legacy=legacy, channels=channels,
    )
    assert helper is not None


# ---------------------------------------------------------------------------
# Cycle 2 — build_recorder_privacy_filter(): cloud_intent forces PUBLIC
# ---------------------------------------------------------------------------


def test_build_recorder_privacy_filter_cloud_intent_forces_public(tmp_path):
    """``cloud_intent=True`` tightens an INTERNAL config to PUBLIC.

    Cloud-bound recordings must run with PUBLIC mode because PUBLIC
    triggers MASK_WINDOW for email/chat/calendar (vs ALLOW in INTERNAL)
    and TEXT_REDACT for code editors (vs ALLOW). Pin the floor logic at
    the helper boundary so future refactors can't silently weaken it.
    """
    from screencap.engine.config import RecordingConfig
    from screencap.engine.screen_recorder import LegacyOptions, RecordingRequest
    from screencap.privacy.policy import PrivacyConfig, PrivacyMode

    request = RecordingRequest(
        name="cloud", config=RecordingConfig(), cloud_intent=True,
    )
    legacy = LegacyOptions()

    from screencap.engine.collaborators import RecordingCollaborators
    from screencap.engine.screen_recorder import IpcChannels

    helper = RecordingCollaborators(
        request=request, legacy=legacy, channels=IpcChannels.create(),
    )

    capture_dir = tmp_path / "recording"
    capture_dir.mkdir()

    base_cfg = PrivacyConfig(mode=PrivacyMode.INTERNAL)
    with mock.patch(
        "screencap.config.get_privacy_config", return_value=base_cfg,
    ):
        screen_filter, privacy_config, override_file = helper.build_recorder_privacy_filter(
            capture_dir=capture_dir, capture_window_data=True,
        )

    assert privacy_config.mode is PrivacyMode.PUBLIC
    assert override_file == capture_dir / ".menubar_overrides.json"
    assert screen_filter is not None


# ---------------------------------------------------------------------------
# Cycle 3 — build_recorder_privacy_filter() never loosens
# ---------------------------------------------------------------------------


def test_build_recorder_privacy_filter_does_not_loosen_stricter_config(tmp_path):
    """A configured PUBLIC mode is not loosened by ``force_mode=INTERNAL``.

    The strictness floor mirrors the env-var precedence in
    ``parse_privacy_config``: tightening is allowed, loosening is not.
    A future caller passing a less-strict ``force_mode`` (or a stale
    ``cloud_intent=False`` with a PUBLIC config) must keep PUBLIC.
    """
    from screencap.engine.config import RecordingConfig
    from screencap.engine.screen_recorder import (
        IpcChannels,
        LegacyOptions,
        RecordingRequest,
    )
    from screencap.privacy.policy import PrivacyConfig, PrivacyMode

    from screencap.engine.collaborators import RecordingCollaborators

    request = RecordingRequest(
        name="strict", config=RecordingConfig(), cloud_intent=False,
    )
    legacy = LegacyOptions(force_mode=PrivacyMode.INTERNAL)
    helper = RecordingCollaborators(
        request=request, legacy=legacy, channels=IpcChannels.create(),
    )

    capture_dir = tmp_path / "rec"
    capture_dir.mkdir()
    base_cfg = PrivacyConfig(mode=PrivacyMode.PUBLIC)
    with mock.patch(
        "screencap.config.get_privacy_config", return_value=base_cfg,
    ):
        _, privacy_config, _ = helper.build_recorder_privacy_filter(
            capture_dir=capture_dir, capture_window_data=True,
        )

    assert privacy_config.mode is PrivacyMode.PUBLIC


# ---------------------------------------------------------------------------
# Cycle 4 — build_recorder_privacy_filter() window-data gate
# ---------------------------------------------------------------------------


def test_build_recorder_privacy_filter_skips_when_window_data_disabled(tmp_path):
    """No window data → no filter, but config still resolved.

    The privacy filter cannot work without window events (it needs to
    know which app is frontmost to decide whether to block). When
    window data capture is disabled, returning a filter that silently
    blocks nothing would be the worst possible outcome — so we skip
    construction and return ``None`` for the filter.
    """
    from screencap.engine.config import RecordingConfig
    from screencap.engine.screen_recorder import (
        IpcChannels,
        LegacyOptions,
        RecordingRequest,
    )
    from screencap.privacy.policy import PrivacyConfig, PrivacyMode

    from screencap.engine.collaborators import RecordingCollaborators

    request = RecordingRequest(name="nogate", config=RecordingConfig())
    helper = RecordingCollaborators(
        request=request, legacy=LegacyOptions(), channels=IpcChannels.create(),
    )

    capture_dir = tmp_path / "rec"
    capture_dir.mkdir()
    base_cfg = PrivacyConfig(mode=PrivacyMode.INTERNAL)
    with mock.patch(
        "screencap.config.get_privacy_config", return_value=base_cfg,
    ):
        screen_filter, privacy_config, override_file = helper.build_recorder_privacy_filter(
            capture_dir=capture_dir, capture_window_data=False,
        )

    assert screen_filter is None
    assert privacy_config is base_cfg
    assert override_file == capture_dir / ".menubar_overrides.json"


# ---------------------------------------------------------------------------
# Cycle 6 — start() builds collaborators with the shared flush_lock
# ---------------------------------------------------------------------------


class _FakeEngineRecorder:
    """Stand-in for engine.Recorder exposing the attrs the helper reads.

    Real ``mp.Queue`` / ``mp.Event`` / ``mp.Value`` instances are used
    so the helper's queue-type guard (``isinstance(_cpq, mp.queues.Queue)``)
    passes — without that, ChunkProcessor is silently skipped.
    """

    def __init__(self) -> None:
        import multiprocessing as mp

        self._chunk_process_q = mp.Queue()
        self._audio_ack_q = mp.Queue()
        self._flush_requested = mp.Event()
        self._flush_ack_counter = mp.Value("i", 0)


def test_start_chunk_and_scrub_workers_share_flush_lock(tmp_path):
    """ChunkProcessor and ScrubWorker share the helper's ``flush_lock``.

    The shared lock is the load-bearing primitive that prevents the two
    consumers from racing on the engine's ``flush_ack_counter`` — without
    it, a concurrent reset by one consumer can zero out the other's
    in-flight ack count mid-poll. The helper instantiates one lock and
    threads it through both constructors.
    """
    from screencap.engine.collaborators import RecordingCollaborators
    from screencap.engine.config import RecordingConfig
    from screencap.engine.screen_recorder import (
        IpcChannels,
        LegacyOptions,
        RecordingRequest,
    )

    capture_dir = tmp_path / "rec"
    capture_dir.mkdir()
    # ChunkProcessor expects recording.db to exist for some setup paths;
    # create an empty file so __init__ doesn't fail on the open() call.
    (capture_dir / "recording.db").touch()

    request = RecordingRequest(name="rec", config=RecordingConfig())
    helper = RecordingCollaborators(
        request=request, legacy=LegacyOptions(), channels=IpcChannels.create(),
    )

    fake_recorder = _FakeEngineRecorder()
    try:
        helper.start(
            recorder=fake_recorder,
            capture_dir=capture_dir,
            screen_filter=None,
            privacy_config=None,
            chunking_enabled=True,
        )

        cp = helper.chunk_processor
        sw = helper.scrub_worker
        assert cp is not None, "chunk_processor must be created when chunking_enabled"
        assert sw is not None, "scrub_worker must always be created"
        assert cp._flush_lock is sw._flush_lock, (
            "chunk_processor and scrub_worker must share the same flush_lock; "
            "concurrent flush handshakes would otherwise race on flush_ack_counter."
        )
        assert cp._flush_lock is helper._flush_lock
    finally:
        # Drain the threads so test teardown is clean
        if helper.scrub_worker is not None:
            helper.scrub_worker.stop(timeout=2.0)
        if helper.chunk_processor is not None:
            try:
                helper.chunk_processor._q.put({"type": "poison_pill"}, timeout=1)
            except Exception:
                pass
            t = helper.chunk_processor._thread
            if t is not None:
                t.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Cycle 7 — chunking_enabled=False skips ChunkProcessor only
# ---------------------------------------------------------------------------


def test_start_skips_chunk_processor_when_chunking_disabled(tmp_path):
    """No chunking → no ChunkProcessor; ScrubWorker is independent.

    The user can disable chunking but still want scrub-on-disable behavior
    (the menubar disable feature works for non-chunked recordings too).
    """
    from screencap.engine.collaborators import RecordingCollaborators
    from screencap.engine.config import RecordingConfig
    from screencap.engine.screen_recorder import (
        IpcChannels,
        LegacyOptions,
        RecordingRequest,
    )

    capture_dir = tmp_path / "rec"
    capture_dir.mkdir()
    (capture_dir / "recording.db").touch()

    request = RecordingRequest(name="rec", config=RecordingConfig())
    helper = RecordingCollaborators(
        request=request, legacy=LegacyOptions(), channels=IpcChannels.create(),
    )
    fake_recorder = _FakeEngineRecorder()
    try:
        helper.start(
            recorder=fake_recorder,
            capture_dir=capture_dir,
            screen_filter=None,
            privacy_config=None,
            chunking_enabled=False,
        )
        assert helper.chunk_processor is None
        assert helper.scrub_worker is not None
    finally:
        if helper.scrub_worker is not None:
            helper.scrub_worker.stop(timeout=2.0)


# ---------------------------------------------------------------------------
# Cycle 8 — finalize() runs end-of-recording catch-all scrub
# ---------------------------------------------------------------------------


class _SpyScrubWorker:
    """Spy that records ``put`` ordering vs. ``stop`` so we can pin the
    catch-all-then-stop ordering invariant.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, dict | None]] = []
        self._stopped = False

    @property
    def messages(self) -> list[dict]:
        return [m for kind, m in self.events if kind == "put" and m is not None]

    def start(self) -> None:
        pass

    def stop(self, timeout: float = 30.0) -> None:
        self.events.append(("stop", None))
        self._stopped = True


class _SpyDisableQueue:
    def __init__(self, target: _SpyScrubWorker) -> None:
        self._target = target

    def put_nowait(self, msg: dict) -> None:
        self._target.events.append(("put", msg))


def test_finalize_runs_catchall_scrub_before_stopping_scrub_worker(tmp_path):
    """End-of-recording catch-all scrub queues every excluded override.

    Acceptance criterion: enumerate ``.menubar_overrides.json``, queue one
    disable message per excluded target on ``disable_q``, **then** call
    ``scrub_worker.stop()``. The "before" ordering is load-bearing — if
    ``stop()`` runs first, the catch-all messages reach an already-drained
    worker and are silently dropped.
    """
    import json

    from screencap.engine.collaborators import RecordingCollaborators
    from screencap.engine.config import RecordingConfig
    from screencap.engine.screen_recorder import (
        IpcChannels,
        LegacyOptions,
        RecordingRequest,
    )

    capture_dir = tmp_path / "rec"
    capture_dir.mkdir()
    overrides = {
        "com.apple.mail": "exclude",          # → kind=app
        "com.google.chrome::secret.example": "exclude",  # → kind=domain
        "com.spotify.client": "allow",        # NOT excluded → ignore
    }
    (capture_dir / ".menubar_overrides.json").write_text(json.dumps(overrides))

    spy_worker = _SpyScrubWorker()
    spy_queue = _SpyDisableQueue(spy_worker)

    # Construct channels with the spy queue as ``disable`` so the helper's
    # production path (``self._channels.disable.put_nowait``) drives the spy
    # without any test seam on the helper itself.
    import multiprocessing as mp
    channels = IpcChannels(
        window_feed=mp.Queue(), override=mp.Queue(), disable=spy_queue,
    )
    request = RecordingRequest(name="rec", config=RecordingConfig())
    helper = RecordingCollaborators(
        request=request, legacy=LegacyOptions(), channels=channels,
    )
    # Inject the spy ScrubWorker directly — its ``stop()`` records ordering.
    helper._scrub_worker = spy_worker

    helper.finalize_catchall_scrub(capture_dir=capture_dir)
    helper.stop_scrub_worker()

    # Two excluded overrides → two messages queued; "allow" is skipped.
    msgs = spy_worker.messages
    assert len(msgs) == 2
    kinds = {m["kind"] for m in msgs}
    assert kinds == {"app", "domain"}

    # Domain message has the bundle::domain split applied
    domain_msg = next(m for m in msgs if m["kind"] == "domain")
    assert domain_msg["bundle_id"] == "com.google.chrome"
    assert domain_msg["root_domain"] == "secret.example"

    # App message preserves the bare bundle id
    app_msg = next(m for m in msgs if m["kind"] == "app")
    assert app_msg["bundle_id"] == "com.apple.mail"

    # Ordering: every put_nowait happens BEFORE stop()
    put_indices = [i for i, (k, _) in enumerate(spy_worker.events) if k == "put"]
    stop_indices = [i for i, (k, _) in enumerate(spy_worker.events) if k == "stop"]
    assert max(put_indices) < min(stop_indices), (
        f"catch-all messages must be queued before stop; events={spy_worker.events}"
    )
