"""Parity test for the ``engine.ScreenRecorder`` seam (SCR-38).

Slice 2 of SCR-31 ports the body of ``screencap.recorder.start_recording``
onto ``ScreenRecorder.run()`` verbatim. This test pins that the seam
class, when constructed directly (no wrapper), produces the same on-disk
artifacts as the wrapper path. Future slices peel each policy axis off
into its own object — this test fences the *output* contract so those
slices can refactor without regressing artifacts.

External hardware boundaries (the engine ``Recorder``, macOS permission
check, orphaned-process probe, disk preflight) are mocked. Internal
modules (config, pidfile, catalog) run for real against ``tmp_path``.
"""

from __future__ import annotations

from collections import namedtuple
from unittest import mock

from tests.conftest import FakeRecorder

DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])
_PLENTY_OF_DISK = DiskUsage(total=500e9, used=100e9, free=400e9)


def _record_artifact_set(capture_dir):
    """Snapshot the deterministic artifacts a fresh recording leaves on disk.

    Excludes timestamp-laden files and engine-internal SQLite content;
    asserts on file presence + the immutable ``.recording_id`` so the test
    does not turn into a brittle byte-equality check.
    """
    files = sorted(p.name for p in capture_dir.iterdir() if p.is_file())
    recording_id = (capture_dir / ".recording_id").read_text()
    return {"files": files, "recording_id": recording_id}


def _common_mocks():
    """Shared external-boundary mocks for both wrapper and seam paths.

    ``_check_macos_permissions`` is called by ``MacOSTCC.preflight()`` (wrapper
    path). Disk thresholds are patched at ``screencap.config`` because
    ``MonitorAndStop.preflight()`` imports them from there via deferred import.
    The seam path uses ``Noop`` for both policies so those mocks are harmless.
    """
    return [
        mock.patch("screencap.recorder._check_macos_permissions"),
        mock.patch("screencap.recorder.get_audio_default", return_value=False),
        mock.patch("screencap.recorder.get_wifi_metrics", return_value=False),
        mock.patch("screencap.recorder.get_app_versions", return_value=False),
        mock.patch("screencap.config.get_disk_warn_mb", return_value=2000),
        mock.patch("screencap.config.get_disk_stop_mb", return_value=500),
        mock.patch("shutil.disk_usage", return_value=_PLENTY_OF_DISK),
        mock.patch("screencap.pidfile.find_orphaned_processes", return_value=[]),
        mock.patch("screencap.pidfile.claim_lock"),
        mock.patch("screencap.pidfile.write_pidfile"),
        mock.patch("screencap.pidfile.delete_pidfile"),
        mock.patch("screencap.engine.recorder.Recorder", FakeRecorder),
    ]


class _NetworkNullSpy:
    """``Null`` network policy that records ``setup``/``teardown`` calls.

    Lets the parity test prove the seam was actually traversed (not just
    the wrapper preludes), without spawning a real menubar subprocess.
    """

    def __init__(self) -> None:
        self.setup_calls = 0
        self.teardown_calls = 0

    def setup(self, capture_dir, privacy_config):
        from screencap.engine.network_policy import NetworkMaterial

        self.setup_calls += 1
        return NetworkMaterial()

    def teardown(self) -> None:
        self.teardown_calls += 1


def test_seam_direct_path_matches_wrapper(tmp_path):
    """``ScreenRecorder().run()`` produces the same artifacts as ``start_recording()``."""
    from screencap.engine.config import RecordingConfig
    from screencap.engine.disk_policy import Noop as DiskNoop
    from screencap.engine.lock_policy import InheritLock
    from screencap.engine.menubar_policy import Noop as MenubarNoop
    from screencap.engine.permission_policy import Noop as PermNoop
    from screencap.engine.screen_recorder import (
        IpcChannels,
        LegacyOptions,
        NoopSignalPolicy,
        RecordingPolicies,
        RecordingRequest,
        RecordingResult,
        ScreenRecorder,
    )
    from screencap.recorder import start_recording

    wrapper_dir = tmp_path / "wrapper"
    seam_dir = tmp_path / "seam"

    # ---- Wrapper path (legacy entry) -------------------------------------
    # ``MenubarNoop`` keeps the parity assertion apples-to-apples without
    # spawning a real menubar subprocess (which would race the
    # FakeRecorder's immediate stop on slow / headless CI runners).
    mocks = _common_mocks()
    for m in mocks:
        m.start()
    try:
        capture_dir, elapsed, _mb_proc, _mb_state = start_recording(
            "parity", output_dir=wrapper_dir,
            _menubar_policy=MenubarNoop(),
            _signal_policy=NoopSignalPolicy(),
        )
    finally:
        for m in mocks:
            m.stop()

    assert capture_dir == wrapper_dir
    assert isinstance(elapsed, float)
    assert wrapper_dir.exists()
    wrapper_artifacts = _record_artifact_set(wrapper_dir)

    # ---- Seam-direct path -----------------------------------------------
    request = RecordingRequest(name="parity", config=RecordingConfig())
    channels = IpcChannels.create()
    seam_network = _NetworkNullSpy()
    policies = RecordingPolicies(
        signal=NoopSignalPolicy(), lock=InheritLock(), menubar=MenubarNoop(),
        permission=PermNoop(), disk=DiskNoop(), network=seam_network,
    )
    legacy = LegacyOptions(output_dir=seam_dir)
    rec = ScreenRecorder(
        request=request, channels=channels, policies=policies, legacy=legacy,
    )

    mocks = _common_mocks()
    for m in mocks:
        m.start()
    try:
        result = rec.run()
    finally:
        for m in mocks:
            m.stop()

    assert isinstance(result, RecordingResult)
    assert result.capture_dir == seam_dir
    assert isinstance(result.elapsed, float)
    assert seam_dir.exists()
    seam_artifacts = _record_artifact_set(seam_dir)

    # ---- Seam was actually traversed -------------------------------------
    # The bare artifact-equality assertion below is largely tautological
    # because both legs route through ``_run_screen_recorder`` — the
    # network policy spy proves a policy method was called, so a future
    # refactor that bypassed the seam entirely would fail this test.
    assert seam_network.setup_calls == 1
    assert seam_network.teardown_calls == 1

    # ---- Parity ----------------------------------------------------------
    assert wrapper_artifacts == seam_artifacts, (
        f"Seam diverges from wrapper:\n"
        f"  wrapper: {wrapper_artifacts}\n"
        f"  seam:    {seam_artifacts}"
    )


def _network_mocks():
    """Mocks for the MitmProxyV15 dependency chain."""
    return [
        mock.patch("screencap.config.get_network_config", return_value=mock.MagicMock()),
        mock.patch("screencap.network.lifecycle.acquire_network_lock", return_value=mock.MagicMock()),
        mock.patch("screencap.network.lifecycle.preflight_or_raise", return_value=8080),
        mock.patch("screencap.network.crypto.get_or_create_kek", return_value=b"k" * 32),
        mock.patch("screencap.network.crypto.generate_dek", return_value=b"d" * 32),
        mock.patch("screencap.network.crypto.wrap_dek", return_value=(b"w" * 48, b"n" * 12)),
        mock.patch("screencap.network.blocklist.effective_capture_bodies_for", return_value=frozenset(["*"])),
    ]


def test_seam_mitm_proxy_v15_path_matches_wrapper(tmp_path):
    """``ScreenRecorder().run()`` with ``MitmProxyV15`` produces the same
    on-disk artifacts as ``start_recording(network=True)``.

    Both paths go through ``_run_screen_recorder``; this pins that the seam
    correctly wires MitmProxyV15 and populates network recorder_kwargs without
    diverging from the wrapper's artifact set.
    """
    from screencap.engine.config import RecordingConfig
    from screencap.engine.disk_policy import Noop as DiskNoop
    from screencap.engine.lock_policy import InheritLock
    from screencap.engine.menubar_policy import Noop as MenubarNoop
    from screencap.engine.network_policy import MitmProxyV15
    from screencap.engine.permission_policy import Noop as PermNoop
    from screencap.engine.screen_recorder import (
        IpcChannels,
        LegacyOptions,
        NoopSignalPolicy,
        RecordingPolicies,
        RecordingRequest,
        RecordingResult,
        ScreenRecorder,
    )
    from screencap.recorder import start_recording

    wrapper_dir = tmp_path / "wrapper-net"
    seam_dir = tmp_path / "seam-net"

    # ---- Wrapper path (start_recording with network=True) -------------------
    all_mocks = _common_mocks() + _network_mocks()
    for m in all_mocks:
        m.start()
    try:
        capture_dir, elapsed, _mb_proc, _mb_state = start_recording(
            "parity-net", output_dir=wrapper_dir, network=True,
            _menubar_policy=MenubarNoop(),
            _signal_policy=NoopSignalPolicy(),
        )
    finally:
        for m in all_mocks:
            m.stop()

    assert wrapper_dir.exists()
    wrapper_artifacts = _record_artifact_set(wrapper_dir)

    # ---- Seam-direct path ---------------------------------------------------
    request = RecordingRequest(name="parity-net", config=RecordingConfig())
    channels = IpcChannels.create()
    policies = RecordingPolicies(
        signal=NoopSignalPolicy(), lock=InheritLock(), menubar=MenubarNoop(),
        permission=PermNoop(), disk=DiskNoop(), network=MitmProxyV15(),
    )
    legacy = LegacyOptions(output_dir=seam_dir)

    all_mocks = _common_mocks() + _network_mocks()
    for m in all_mocks:
        m.start()
    try:
        result = ScreenRecorder(
            request=request, channels=channels, policies=policies, legacy=legacy,
        ).run()
    finally:
        for m in all_mocks:
            m.stop()

    assert isinstance(result, RecordingResult)
    assert seam_dir.exists()
    seam_artifacts = _record_artifact_set(seam_dir)

    # ---- Parity -------------------------------------------------------------
    assert wrapper_artifacts == seam_artifacts, (
        f"Seam (MitmProxyV15) diverges from wrapper (network=True):\n"
        f"  wrapper: {wrapper_artifacts}\n"
        f"  seam:    {seam_artifacts}"
    )
