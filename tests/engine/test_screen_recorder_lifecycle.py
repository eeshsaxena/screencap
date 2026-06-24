"""Coverage for the live-loop branches in ``_run_screen_recorder``.

Pins the load-bearing wiring that consumes ``PermissionRevoked`` and
``DiskSpaceCritical`` mid-recording: ``_stop_reason`` is set, the
``permission_lost`` stderr event is emitted (the SwiftUI shell's only
signal), and ``recorder.stop()`` is called so writer processes finalize.

Tests in ``test_permission_policy.py`` / ``test_disk_policy.py`` only
verify the *policies raise*; this file exercises the consumer branch.
"""

from __future__ import annotations

import json
from collections import namedtuple
from pathlib import Path
from unittest import mock

import pytest

from tests.conftest import FakeRecorder

DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])
_PLENTY_OF_DISK = DiskUsage(total=500e9, used=100e9, free=400e9)


class _TickingFakeRecorder(FakeRecorder):
    """``FakeRecorder`` that runs the live loop for one tick."""

    def __init__(self, capture_dir_str, **kwargs):
        super().__init__(capture_dir_str, **kwargs)
        self._is_recording = True

    @property
    def is_recording(self) -> bool:  # type: ignore[override]
        return self._is_recording

    @is_recording.setter
    def is_recording(self, value: bool) -> None:
        self._is_recording = value

    def stop(self):
        self._is_recording = False
        self._stopped = True


class _OneShotPermissionPolicy:
    """Raises ``PermissionRevoked`` on the first ``poll()`` call."""

    def __init__(self, missing: str = "screen_recording") -> None:
        self._missing = missing
        self._raised = False

    def preflight(self) -> None:
        pass

    def poll(self, now: float) -> None:
        if self._raised:
            return
        self._raised = True
        from screencap.engine.screen_recorder import PermissionRevoked

        raise PermissionRevoked(self._missing)

    @property
    def next_poll_at(self) -> float:
        return 0.0


class _OneShotDiskPolicy:
    """Raises ``DiskSpaceCritical`` on the first ``poll()`` call."""

    def __init__(self, free_mb: float = 100.0) -> None:
        self._free_mb = free_mb
        self._raised = False

    def bind(self, capture_dir: Path) -> None:
        pass

    def preflight(self) -> None:
        pass

    def poll(self, now: float) -> None:
        if self._raised:
            return
        self._raised = True
        from screencap.engine.disk_policy import DiskSpaceCritical

        raise DiskSpaceCritical(self._free_mb)

    @property
    def next_poll_at(self) -> float:
        return 0.0

    @property
    def warning(self) -> str:
        return ""


def _build_seam(tmp_path, *, permission, disk):
    from screencap.engine.config import RecordingConfig
    from screencap.engine.lock_policy import InheritLock
    from screencap.engine.menubar_policy import Noop as MenubarNoop
    from screencap.engine.network_policy import Null as NetworkNull
    from screencap.engine.screen_recorder import (
        IpcChannels,
        LegacyOptions,
        NoopSignalPolicy,
        RecordingPolicies,
        RecordingRequest,
        ScreenRecorder,
    )

    request = RecordingRequest(name="lifecycle", config=RecordingConfig())
    channels = IpcChannels.create()
    policies = RecordingPolicies(
        signal=NoopSignalPolicy(),
        lock=InheritLock(),
        menubar=MenubarNoop(),
        permission=permission,
        disk=disk,
        network=NetworkNull(),
    )
    legacy = LegacyOptions(output_dir=tmp_path / "rec")
    return ScreenRecorder(
        request=request, channels=channels, policies=policies, legacy=legacy,
    )


def _common_mocks():
    return [
        mock.patch("screencap.recorder._check_macos_permissions"),
        mock.patch("screencap.recorder.get_audio_default", return_value=False),
        mock.patch("screencap.recorder.get_wifi_metrics", return_value=False),
        mock.patch("screencap.recorder.get_app_versions", return_value=False),
        mock.patch("screencap.config.get_disk_warn_mb", return_value=2000),
        mock.patch("screencap.config.get_disk_stop_mb", return_value=500),
        mock.patch("shutil.disk_usage", return_value=_PLENTY_OF_DISK),
        mock.patch("screencap.engine.recorder.Recorder", _TickingFakeRecorder),
    ]


def test_permission_revoked_mid_loop_emits_stderr_event_and_stops(tmp_path, capfd):
    """``PermissionRevoked`` mid-loop → ``permission_lost`` stderr event + ``recorder.stop()``."""
    from screencap.engine.disk_policy import Noop as DiskNoop

    rec = _build_seam(
        tmp_path,
        permission=_OneShotPermissionPolicy(missing="screen_recording"),
        disk=DiskNoop(),
    )

    mocks = _common_mocks()
    for m in mocks:
        m.start()
    try:
        rec.run()
    finally:
        for m in mocks:
            m.stop()

    err = capfd.readouterr().err
    events = [
        json.loads(line) for line in err.splitlines()
        if line.strip().startswith("{") and "permission_lost" in line
    ]
    assert events, (
        "the SwiftUI shell relies on the ``permission_lost`` stderr event; "
        f"got stderr={err!r}"
    )
    assert events[0]["permission"] == "screen_recording"

    # ``terminated_reason`` is the post-loop side of the same wiring.
    capture_dir = tmp_path / "rec"
    meta = json.loads((capture_dir / ".recording_stop_meta.json").read_text())
    assert meta["terminated_reason"] == "permission_lost"


def test_fresh_screen_watch_revocation_drives_loop_teardown(tmp_path, capfd):
    """A real ``FreshScreenWatch`` revocation drives the run()-level teardown.

    The seam-level tests (``test_permission_policy.py``) only prove the watch
    *raises* ``PermissionRevoked`` from ``poll()`` directly; the
    ``permission_lost`` consumer branch is otherwise exercised only through the
    ``MacOSTCC``-shaped ``_OneShotPermissionPolicy`` fake. This pins that a
    genuine ``FreshScreenWatch`` — driven through the supervisor loop so its own
    debounce trips — flows through the same ``permission_lost`` + ``stop()``
    teardown. ``interval=0.0`` lets the loop poll every tick so the scripted
    denials reach the debounce.
    """
    from screencap.engine.disk_policy import Noop as DiskNoop
    from screencap.engine.permission_policy import FreshScreenWatch

    watch = FreshScreenWatch(
        interval=0.0,
        debounce=2,
        # Deny on every probe; the debounce of 2 trips on the second loop tick.
        probe=lambda: False,
        enabled=True,
    )
    rec = _build_seam(tmp_path, permission=watch, disk=DiskNoop())

    mocks = _common_mocks()
    for m in mocks:
        m.start()
    try:
        rec.run()
    finally:
        for m in mocks:
            m.stop()

    err = capfd.readouterr().err
    events = [
        json.loads(line) for line in err.splitlines()
        if line.strip().startswith("{") and "permission_lost" in line
    ]
    assert events, (
        "a FreshScreenWatch revocation must emit the ``permission_lost`` stderr "
        f"event the SwiftUI shell relies on; got stderr={err!r}"
    )
    assert events[0]["permission"] == "screen_recording"

    capture_dir = tmp_path / "rec"
    meta = json.loads((capture_dir / ".recording_stop_meta.json").read_text())
    assert meta["terminated_reason"] == "permission_lost"


def test_disk_space_critical_mid_loop_marks_stop_reason(tmp_path):
    """``DiskSpaceCritical`` mid-loop → ``terminated_reason='disk_full'`` + ``DiskFullError``."""
    from screencap.engine.permission_policy import Noop as PermNoop
    from screencap.recorder import DiskFullError

    rec = _build_seam(
        tmp_path,
        permission=PermNoop(),
        disk=_OneShotDiskPolicy(free_mb=42.0),
    )

    mocks = _common_mocks()
    for m in mocks:
        m.start()
    try:
        with pytest.raises(DiskFullError):
            rec.run()
    finally:
        for m in mocks:
            m.stop()

    capture_dir = tmp_path / "rec"
    meta = json.loads((capture_dir / ".recording_stop_meta.json").read_text())
    assert meta["terminated_reason"] == "disk_full"


def test_finalize_pipeline_runs_before_chunk_processor_stop(tmp_path):
    """Engine pipeline drains BEFORE the chunk-processor poison pill.

    Pins the load-bearing ordering: ``recorder.finalize_pipeline()``
    joins the record/fanout threads (flushing the ``final_chunk`` rotation
    onto ``_chunk_process_q``) before the collaborators-side
    ``stop_chunk_processor`` enqueues a poison pill behind it. Reversing
    the order races the two — a poison pill ahead of ``final_chunk`` would
    silently drop the trailing chunk on every recording.

    Uses ``_OneShotDiskPolicy`` to force the live loop to exit on the
    first tick so the lifecycle reaches the post-loop drain block where
    the ordering invariant lives.
    """
    from screencap.engine.permission_policy import Noop as PermNoop
    from screencap.recorder import DiskFullError

    rec = _build_seam(
        tmp_path, permission=PermNoop(), disk=_OneShotDiskPolicy(free_mb=42.0),
    )

    call_order: list[str] = []

    real_finalize = _TickingFakeRecorder.finalize_pipeline

    def _spy_finalize(self):
        call_order.append("finalize_pipeline")
        return real_finalize(self)

    with mock.patch.object(
        _TickingFakeRecorder, "finalize_pipeline", _spy_finalize, create=True,
    ), mock.patch(
        "screencap.engine.collaborators.RecordingCollaborators.stop_chunk_processor",
        autospec=True,
        side_effect=lambda *a, **kw: call_order.append("stop_chunk_processor"),
    ):
        mocks = _common_mocks()
        for m in mocks:
            m.start()
        try:
            # ``_OneShotDiskPolicy`` triggers the disk-full stop path,
            # which raises ``DiskFullError`` after the drain block runs.
            with pytest.raises(DiskFullError):
                rec.run()
        finally:
            for m in mocks:
                m.stop()

    assert "finalize_pipeline" in call_order, (
        "engine.Recorder.finalize_pipeline must be invoked from the seam "
        "before collaborators stop"
    )
    assert "stop_chunk_processor" in call_order, (
        "stop_chunk_processor must be invoked at end-of-recording"
    )
    assert call_order.index("finalize_pipeline") < call_order.index("stop_chunk_processor"), (
        f"finalize_pipeline must run before stop_chunk_processor; got {call_order}"
    )
