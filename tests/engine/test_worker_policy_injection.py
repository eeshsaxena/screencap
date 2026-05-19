"""Tests for SCR-46: Noop policy injection into session-worker recordings.

Verifies that ``start_recording`` honours ``_permission_policy`` and
``_disk_policy`` injection points so that session workers can bypass the
redundant TCC subprocess and ``shutil.disk_usage`` polls that the
standalone-CLI defaults perform.
"""

from __future__ import annotations

from collections import namedtuple
from unittest import mock

from tests.conftest import FakeRecorder

DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])
_PLENTY_OF_DISK = DiskUsage(total=500e9, used=100e9, free=400e9)


def _base_mocks():
    """Minimal mocks required to run start_recording with FakeRecorder."""
    return [
        mock.patch("screencap.recorder.get_audio_default", return_value=False),
        mock.patch("screencap.recorder.get_wifi_metrics", return_value=False),
        mock.patch("screencap.recorder.get_app_versions", return_value=False),
        mock.patch("screencap.engine.recorder.Recorder", FakeRecorder),
    ]


def test_noop_permission_policy_does_not_call_check_permissions_now(tmp_path):
    """When ``_permission_policy=Noop()`` is injected, ``_check_permissions_now``
    is never called during the recording loop.

    ``MacOSTCC`` (the standalone-CLI default) calls ``_check_permissions_now``
    every 5 s. Session workers pass ``Noop`` to skip that TCC subprocess.
    This test pins that the injection point actually bypasses the call.
    """
    from screencap.engine.menubar_policy import Noop as MenubarNoop
    from screencap.engine.permission_policy import Noop as PermNoop
    from screencap.engine.screen_recorder import NoopSignalPolicy
    from screencap.recorder import start_recording

    mocks = _base_mocks()
    for m in mocks:
        m.start()
    try:
        with mock.patch("screencap.recorder._check_permissions_now") as perm_spy:
            start_recording(
                "worker-perm",
                output_dir=tmp_path / "perm",
                _permission_policy=PermNoop(),
                _signal_policy=NoopSignalPolicy(),
                _menubar_policy=MenubarNoop(),
            )
    finally:
        for m in mocks:
            m.stop()

    assert perm_spy.call_count == 0, (
        f"_check_permissions_now must not be called with Noop permission policy, "
        f"got {perm_spy.call_count} call(s)"
    )


def test_noop_disk_policy_does_not_call_shutil_disk_usage(tmp_path):
    """When ``_disk_policy=Noop()`` is injected, ``shutil.disk_usage``
    is never called during the recording loop.

    ``MonitorAndStop`` (the standalone-CLI default) calls ``shutil.disk_usage``
    at preflight and every 30 s. Session workers pass ``Noop`` to skip that.
    This test pins that the injection point actually bypasses the call.
    """
    from screencap.engine.disk_policy import Noop as DiskNoop
    from screencap.engine.menubar_policy import Noop as MenubarNoop
    from screencap.engine.permission_policy import Noop as PermNoop
    from screencap.engine.screen_recorder import NoopSignalPolicy
    from screencap.recorder import start_recording

    mocks = _base_mocks()
    for m in mocks:
        m.start()
    try:
        with mock.patch("shutil.disk_usage") as disk_spy:
            start_recording(
                "worker-disk",
                output_dir=tmp_path / "disk",
                _permission_policy=PermNoop(),
                _disk_policy=DiskNoop(),
                _signal_policy=NoopSignalPolicy(),
                _menubar_policy=MenubarNoop(),
            )
    finally:
        for m in mocks:
            m.stop()

    assert disk_spy.call_count == 0, (
        f"shutil.disk_usage must not be called with Noop disk policy, "
        f"got {disk_spy.call_count} call(s)"
    )
