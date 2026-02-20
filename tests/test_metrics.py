"""Tests for screencap.metrics."""

import json
from unittest import mock

from screencap.metrics import (
    METRICS_FILENAME,
    collect_dynamic_metrics,
    collect_static_metrics,
    save_metrics,
)


def test_collect_static_metrics_returns_expected_keys():
    with (
        mock.patch("screencap.metrics.socket.gethostname", return_value="test-host"),
        mock.patch("screencap.metrics.platform.mac_ver", return_value=("15.3", ("", "", ""), "")),
        mock.patch(
            "screencap.metrics.platform.uname",
            return_value=mock.Mock(system="Darwin", release="24.6.0"),
        ),
        mock.patch(
            "screencap.metrics.subprocess.run",
            return_value=mock.Mock(stdout='{"SPDisplaysDataType": [{"sppci_model": "Apple M2"}]}', returncode=0),
        ),
        mock.patch("screencap.metrics.psutil.cpu_count", return_value=10),
        mock.patch(
            "screencap.metrics.psutil.virtual_memory",
            return_value=mock.Mock(total=16 * 1024**3),
        ),
        mock.patch("screencap.metrics.mss.mss") as mock_mss,
    ):
        mock_sct = mock.MagicMock()
        mock_sct.monitors = [
            {"width": 5120, "height": 1600},  # "all monitors" entry
            {"width": 2560, "height": 1600},
        ]
        mock_mss.return_value.__enter__ = mock.Mock(return_value=mock_sct)
        mock_mss.return_value.__exit__ = mock.Mock(return_value=False)

        result = collect_static_metrics()

    expected_keys = {
        "hostname",
        "macos_version",
        "kernel_version",
        "cpu_model",
        "cpu_cores_physical",
        "cpu_cores_logical",
        "memory_total_gb",
        "gpu_model",
        "python_version",
        "screencap_version",
        "displays",
        "display_count",
    }
    assert expected_keys == set(result.keys())
    assert result["hostname"] == "test-host"
    assert result["display_count"] == 1
    assert result["displays"][0]["width"] == 2560


def test_collect_dynamic_metrics_returns_expected_keys():
    with (
        mock.patch("screencap.metrics.psutil.cpu_percent", return_value=12.5),
        mock.patch(
            "screencap.metrics.psutil.virtual_memory",
            return_value=mock.Mock(used=9.2 * 1024**3, percent=57.5),
        ),
        mock.patch(
            "screencap.metrics.psutil.disk_usage",
            return_value=mock.Mock(total=494 * 1024**3, free=120 * 1024**3),
        ),
        mock.patch(
            "screencap.metrics.psutil.sensors_battery",
            return_value=mock.Mock(percent=87, power_plugged=True),
        ),
    ):
        result = collect_dynamic_metrics()

    expected_keys = {
        "collected_at",
        "cpu_percent",
        "memory_used_gb",
        "memory_percent",
        "disk_total_gb",
        "disk_available_gb",
        "battery_percent",
        "battery_charging",
    }
    assert expected_keys == set(result.keys())
    assert result["cpu_percent"] == 12.5
    assert result["battery_percent"] == 87
    assert result["battery_charging"] is True


def test_save_metrics_start_creates_file(tmp_path):
    with (
        mock.patch("screencap.metrics.collect_static_metrics", return_value={"hostname": "h"}),
        mock.patch("screencap.metrics.collect_dynamic_metrics", return_value={"cpu_percent": 10}),
    ):
        save_metrics(tmp_path, "start")

    metrics_file = tmp_path / METRICS_FILENAME
    assert metrics_file.exists()

    data = json.loads(metrics_file.read_text())
    assert data["schema_version"] == 1
    assert data["static"] == {"hostname": "h"}
    assert data["start"] == {"cpu_percent": 10}
    assert data["end"] is None


def test_save_metrics_end_updates_file(tmp_path):
    # Write start first
    start_data = {
        "schema_version": 1,
        "static": {"hostname": "h"},
        "start": {"cpu_percent": 10},
        "end": None,
    }
    metrics_file = tmp_path / METRICS_FILENAME
    metrics_file.write_text(json.dumps(start_data))

    with mock.patch(
        "screencap.metrics.collect_dynamic_metrics",
        return_value={"cpu_percent": 25},
    ):
        save_metrics(tmp_path, "end")

    data = json.loads(metrics_file.read_text())
    assert data["start"] == {"cpu_percent": 10}  # unchanged
    assert data["end"] == {"cpu_percent": 25}
    assert data["static"] == {"hostname": "h"}  # unchanged


def test_save_metrics_end_without_start(tmp_path):
    with mock.patch(
        "screencap.metrics.collect_dynamic_metrics",
        return_value={"cpu_percent": 5},
    ):
        save_metrics(tmp_path, "end")

    metrics_file = tmp_path / METRICS_FILENAME
    assert metrics_file.exists()

    data = json.loads(metrics_file.read_text())
    assert data["schema_version"] == 1
    assert data["start"] is None
    assert data["end"] == {"cpu_percent": 5}


def test_save_metrics_survives_partial_failure(tmp_path):
    """If one collector raises, other fields should still be populated."""
    with (
        mock.patch("screencap.metrics.socket.gethostname", side_effect=OSError("fail")),
        mock.patch("screencap.metrics.platform.mac_ver", return_value=("15.3", ("", "", ""), "")),
        mock.patch(
            "screencap.metrics.platform.uname",
            return_value=mock.Mock(system="Darwin", release="24.6.0"),
        ),
        mock.patch(
            "screencap.metrics.subprocess.run",
            return_value=mock.Mock(stdout='{"SPDisplaysDataType": []}', returncode=0),
        ),
        mock.patch("screencap.metrics.psutil.cpu_count", return_value=8),
        mock.patch(
            "screencap.metrics.psutil.virtual_memory",
            return_value=mock.Mock(total=16 * 1024**3, used=8 * 1024**3, percent=50.0),
        ),
        mock.patch(
            "screencap.metrics.psutil.disk_usage",
            return_value=mock.Mock(total=500 * 1024**3, free=200 * 1024**3),
        ),
        mock.patch(
            "screencap.metrics.psutil.sensors_battery",
            return_value=None,
        ),
        mock.patch("screencap.metrics.psutil.cpu_percent", return_value=5.0),
        mock.patch("screencap.metrics.mss.mss") as mock_mss,
    ):
        mock_sct = mock.MagicMock()
        mock_sct.monitors = [{"width": 3840, "height": 2160}]
        mock_mss.return_value.__enter__ = mock.Mock(return_value=mock_sct)
        mock_mss.return_value.__exit__ = mock.Mock(return_value=False)

        save_metrics(tmp_path, "start")

    data = json.loads((tmp_path / METRICS_FILENAME).read_text())
    assert data["static"]["hostname"] is None  # failed field
    assert data["static"]["macos_version"] == "15.3"  # other fields ok
    assert data["start"]["cpu_percent"] == 5.0


def test_gpu_detection_timeout(tmp_path):
    """GPU subprocess timeout should produce null, not crash."""
    with (
        mock.patch("screencap.metrics.socket.gethostname", return_value="h"),
        mock.patch("screencap.metrics.platform.mac_ver", return_value=("15.3", ("", "", ""), "")),
        mock.patch(
            "screencap.metrics.platform.uname",
            return_value=mock.Mock(system="Darwin", release="24.6.0"),
        ),
        mock.patch(
            "screencap.metrics.subprocess.run",
            side_effect=subprocess_side_effect,
        ),
        mock.patch("screencap.metrics.psutil.cpu_count", return_value=8),
        mock.patch(
            "screencap.metrics.psutil.virtual_memory",
            return_value=mock.Mock(total=16 * 1024**3, used=8 * 1024**3, percent=50.0),
        ),
        mock.patch(
            "screencap.metrics.psutil.disk_usage",
            return_value=mock.Mock(total=500 * 1024**3, free=200 * 1024**3),
        ),
        mock.patch(
            "screencap.metrics.psutil.sensors_battery",
            return_value=None,
        ),
        mock.patch("screencap.metrics.psutil.cpu_percent", return_value=5.0),
        mock.patch("screencap.metrics.mss.mss") as mock_mss,
    ):
        mock_sct = mock.MagicMock()
        mock_sct.monitors = [{"width": 3840, "height": 2160}]
        mock_mss.return_value.__enter__ = mock.Mock(return_value=mock_sct)
        mock_mss.return_value.__exit__ = mock.Mock(return_value=False)

        save_metrics(tmp_path, "start")

    data = json.loads((tmp_path / METRICS_FILENAME).read_text())
    # CPU model from sysctl succeeds, GPU from system_profiler times out
    assert data["static"]["cpu_model"] == "Apple M2 Pro"
    assert data["static"]["gpu_model"] is None


def subprocess_side_effect(*args, **kwargs):
    """Return CPU model for sysctl, timeout for system_profiler."""
    import subprocess

    cmd = args[0] if args else kwargs.get("args", [])
    if "sysctl" in cmd:
        return mock.Mock(stdout="Apple M2 Pro", returncode=0)
    if "system_profiler" in cmd:
        raise subprocess.TimeoutExpired(cmd, 5)
    return mock.Mock(stdout="", returncode=0)
