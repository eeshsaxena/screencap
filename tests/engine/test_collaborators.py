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
# U4b — capture-time video blocking is gated behind the masked-video flag
# ---------------------------------------------------------------------------


def test_build_recorder_privacy_filter_blocks_video_by_default(tmp_path):
    """Flag OFF (default): the screen_filter blocks video at capture.

    This is the byte-for-byte-today proof: with
    ``get_masked_video_upload_enabled()`` returning its default False, the
    constructed filter has ``block_video=True`` and drops video frames for a
    sensitive app exactly as it does today. A cloud-intent recording is used
    because that is the path the prior capture-time blocking guarded.
    """
    from screencap.engine.collaborators import RecordingCollaborators
    from screencap.engine.config import RecordingConfig
    from screencap.engine.screen_recorder import (
        IpcChannels,
        LegacyOptions,
        RecordingRequest,
    )
    from screencap.privacy.policy import PrivacyConfig, PrivacyMode

    request = RecordingRequest(
        name="cloud", config=RecordingConfig(), cloud_intent=True,
    )
    helper = RecordingCollaborators(
        request=request, legacy=LegacyOptions(), channels=IpcChannels.create(),
    )

    capture_dir = tmp_path / "rec"
    capture_dir.mkdir()
    base_cfg = PrivacyConfig(
        mode=PrivacyMode.PUBLIC,
        exclude_apps=frozenset({"com.1password.1password"}),
    )
    with (
        mock.patch(
            "screencap.config.get_privacy_config", return_value=base_cfg,
        ),
        # Default (False) — do not rely on ambient config/env.
        mock.patch(
            "screencap.config.get_masked_video_upload_enabled",
            return_value=False,
        ),
    ):
        screen_filter, _, _ = helper.build_recorder_privacy_filter(
            capture_dir=capture_dir, capture_window_data=True,
        )

    assert screen_filter is not None
    # An excluded app must drop video frames at capture (today's behavior).
    screen_filter.on_window_event({
        "app_bundle_id": "com.1password.1password", "title": "1Password",
    })
    disp = screen_filter.get_capture_disposition()
    assert disp.video_allowed is False
    assert disp.screen_allowed is False


def test_build_recorder_privacy_filter_rich_video_when_flag_on(tmp_path):
    """Flag ON: the screen_filter no longer blocks video (rich capture).

    With ``get_masked_video_upload_enabled()`` True, capture-time VIDEO
    blocking is disabled — even a sensitive app's video frames are captured
    (the rich input U6 masks post-hoc) — while PUBLIC-forcing and the
    filter's screenshot/keystroke gating remain active.
    """
    from screencap.engine.collaborators import RecordingCollaborators
    from screencap.engine.config import RecordingConfig
    from screencap.engine.screen_recorder import (
        IpcChannels,
        LegacyOptions,
        RecordingRequest,
    )
    from screencap.privacy.policy import PrivacyConfig, PrivacyMode

    request = RecordingRequest(
        name="cloud", config=RecordingConfig(), cloud_intent=True,
    )
    helper = RecordingCollaborators(
        request=request, legacy=LegacyOptions(), channels=IpcChannels.create(),
    )

    capture_dir = tmp_path / "rec"
    capture_dir.mkdir()
    base_cfg = PrivacyConfig(
        mode=PrivacyMode.PUBLIC,
        exclude_apps=frozenset({"com.1password.1password"}),
    )
    with (
        mock.patch(
            "screencap.config.get_privacy_config", return_value=base_cfg,
        ),
        mock.patch(
            "screencap.config.get_masked_video_upload_enabled",
            return_value=True,
        ),
    ):
        screen_filter, privacy_config, _ = helper.build_recorder_privacy_filter(
            capture_dir=capture_dir, capture_window_data=True,
        )

    assert screen_filter is not None
    # PUBLIC-forcing for cloud is UNCHANGED — events/screenshot scrubbing is
    # a separate mechanism U6 does not replace.
    assert privacy_config.mode is PrivacyMode.PUBLIC

    screen_filter.on_window_event({
        "app_bundle_id": "com.1password.1password", "title": "1Password",
    })
    disp = screen_filter.get_capture_disposition()
    # Video captured rich (flag ON) ...
    assert disp.video_allowed is True
    # ... but screenshot gating + keystroke nulling still fire.
    assert disp.screen_allowed is False
    assert disp.keystrokes_allowed is False


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


def test_scrub_worker_failure_hard_errors_for_cloud_intent(tmp_path):
    """ScrubWorker failure must SystemExit when the recording is cloud-bound.

    The user is not running with ``--verbose`` by default; a silent
    ``self._scrub_worker = None`` would let an upload ship un-scrubbed PII
    to GCS with no signal to the user.
    """
    import pytest

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

    request = RecordingRequest(
        name="rec", config=RecordingConfig(),
        cloud_intent=True, scrub_enabled=True,
    )
    helper = RecordingCollaborators(
        request=request, legacy=LegacyOptions(), channels=IpcChannels.create(),
    )
    fake_recorder = _FakeEngineRecorder()

    with (
        mock.patch(
            "screencap.privacy.scrub_worker.ScrubWorker",
            side_effect=RuntimeError("scrub init failed"),
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        helper.start(
            recorder=fake_recorder,
            capture_dir=capture_dir,
            screen_filter=None,
            privacy_config=None,
            chunking_enabled=False,
        )

    assert exc_info.value.code == 1


def test_scrub_worker_failure_tears_down_chunk_processor(tmp_path):
    """Cloud + chunking: if scrub_worker startup fails, chunk_processor must not leak.

    ``ChunkProcessor.start()`` spawns a non-daemon worker thread; if
    ``_build_scrub_worker`` later raises ``SystemExit(1)`` for the
    cloud-bound case, that thread keeps the process alive past the
    intended hard-fail. ``start()`` must catch the failure, send the
    poison pill, and join the chunk thread before re-raising.
    """
    import pytest

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

    request = RecordingRequest(
        name="rec", config=RecordingConfig(),
        cloud_intent=True, scrub_enabled=True,
    )
    helper = RecordingCollaborators(
        request=request, legacy=LegacyOptions(), channels=IpcChannels.create(),
    )
    fake_recorder = _FakeEngineRecorder()

    with (
        mock.patch(
            "screencap.privacy.scrub_worker.ScrubWorker",
            side_effect=RuntimeError("scrub init failed"),
        ),
        pytest.raises(SystemExit),
    ):
        helper.start(
            recorder=fake_recorder,
            capture_dir=capture_dir,
            screen_filter=None,
            privacy_config=None,
            chunking_enabled=True,
        )

    # Chunk processor handle must be cleared and its worker thread joined.
    assert helper.chunk_processor is None, (
        "chunk_processor must be torn down when a later collaborator fails"
    )


def test_scrub_worker_failure_warns_for_local_recording(tmp_path):
    """Local recording: scrub-worker failure must not block recording but must surface."""
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

    request = RecordingRequest(name="rec", config=RecordingConfig())  # cloud_intent=False
    helper = RecordingCollaborators(
        request=request, legacy=LegacyOptions(verbose=False), channels=IpcChannels.create(),
    )
    fake_recorder = _FakeEngineRecorder()
    fake_console = mock.MagicMock()

    with mock.patch(
        "screencap.privacy.scrub_worker.ScrubWorker",
        side_effect=RuntimeError("scrub init failed"),
    ):
        helper.start(
            recorder=fake_recorder,
            capture_dir=capture_dir,
            screen_filter=None,
            privacy_config=None,
            chunking_enabled=False,
            console=fake_console,
        )

    assert helper.scrub_worker is None
    assert fake_console.print.called, "warning must be printed even without --verbose"


# ---------------------------------------------------------------------------
# finalize_uploads — partial-upload path must not NameError on missing chunks
# ---------------------------------------------------------------------------


class _StubChunkProcessor:
    """Minimal ``ChunkProcessor`` stand-in for the finalize_uploads contract.

    Exposes only the attributes / methods ``finalize_uploads`` actually
    reads. The partial-upload branch is the one that historically
    referenced an unbound ``n_uploaded`` local — keeping the stub
    deliberately thin makes that path easy to exercise.
    """

    def __init__(
        self,
        *,
        all_uploaded: bool = False,
        force_stopped: bool = False,
        summary: tuple[int, int] = (0, 0),
        upload_warning: str | None = None,
    ) -> None:
        self._all_uploaded = all_uploaded
        self.was_force_stopped = force_stopped
        self._summary = summary
        self.upload_warning = upload_warning

    def all_chunks_uploaded(self) -> bool:
        return self._all_uploaded

    def upload_summary(self) -> tuple[int, int]:
        return self._summary

    def reconcile_against_gcs(self) -> int:
        return 0


def test_finalize_uploads_partial_path_does_not_nameerror(tmp_path):
    """Short recording with no chunk files must finalize without NameError.

    Regression: a rename of the local from ``n_uploaded`` to ``n_emitted``
    missed the ``.upload_followup.json`` write site, so the partial-upload
    branch raised ``NameError`` mid-finalize. The worker then exited
    before writing ``.recording_ready``, the daemon never emitted the
    "finalized" event, and the SwiftUI stop timed out with
    "Stop is still finalizing in the background." This test pins the
    contract: the partial-upload branch must complete and write the
    follow-up JSON with the documented ``n_uploaded`` key.
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

    request = RecordingRequest(name="short", config=RecordingConfig())
    helper = RecordingCollaborators(
        request=request,
        legacy=LegacyOptions(live_upload=True),
        channels=IpcChannels.create(),
    )
    helper._chunk_processor = _StubChunkProcessor(summary=(0, 0))

    result = helper.finalize_uploads(
        capture_dir=capture_dir,
        stop_reason="graceful",
        recording_name="short",
    )

    assert result["n_uploaded"] == 0
    assert result["n_total"] == 0
    followup_path = capture_dir / ".upload_followup.json"
    assert followup_path.exists(), (
        "partial-upload branch must persist .upload_followup.json for "
        "print_upload_followup to surface the deferred warning"
    )
    payload = json.loads(followup_path.read_text())
    assert payload["n_uploaded"] == 0
    assert payload["n_total"] == 0
