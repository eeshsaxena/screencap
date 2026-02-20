"""Tests for screencap.metrics."""

import json
from unittest import mock

from screencap.metrics import (
    METRICS_FILENAME,
    _collect_locale,
    _collect_wifi_dynamic,
    _collect_wifi_static,
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
        mock.patch(
            "screencap.metrics._collect_wifi_static",
            return_value={"connected": True, "phy_mode": "802.11ax"},
        ),
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
        "locale",
        "wifi",
    }
    assert expected_keys == set(result.keys())
    assert result["hostname"] == "test-host"
    assert result["display_count"] == 1
    assert result["displays"][0]["width"] == 2560
    assert result["wifi"] == {"connected": True, "phy_mode": "802.11ax"}


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
        mock.patch(
            "screencap.metrics._collect_wifi_dynamic",
            return_value={"rssi_dbm": -55, "tx_rate_mbps": 540.0},
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
        "wifi",
    }
    assert expected_keys == set(result.keys())
    assert result["cpu_percent"] == 12.5
    assert result["battery_percent"] == 87
    assert result["battery_charging"] is True
    assert result["wifi"] == {"rssi_dbm": -55, "tx_rate_mbps": 540.0}


def test_save_metrics_start_creates_file(tmp_path):
    with (
        mock.patch("screencap.metrics.collect_static_metrics", return_value={"hostname": "h"}),
        mock.patch("screencap.metrics.collect_dynamic_metrics", return_value={"cpu_percent": 10}),
    ):
        save_metrics(tmp_path, "start")

    metrics_file = tmp_path / METRICS_FILENAME
    assert metrics_file.exists()

    data = json.loads(metrics_file.read_text())
    assert data["schema_version"] == 3
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
    assert data["schema_version"] == 3
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
    """Return CPU model for sysctl, timeout for system_profiler, mock defaults."""
    import subprocess

    cmd = args[0] if args else kwargs.get("args", [])
    if "sysctl" in cmd:
        return mock.Mock(stdout="Apple M2 Pro", returncode=0)
    if "system_profiler" in cmd:
        raise subprocess.TimeoutExpired(cmd, 5)
    if "defaults" in cmd:
        return _defaults_side_effect(cmd, **kwargs)
    return mock.Mock(stdout="", returncode=0)


def _defaults_side_effect(cmd, **kwargs):
    """Handle defaults read/export commands for locale tests."""
    import plistlib

    cmd_str = " ".join(cmd)
    if "AppleLocale" in cmd:
        return mock.Mock(stdout="en_US@currency=USD\n", returncode=0)
    if "AppleLanguages" in cmd:
        return mock.Mock(
            stdout='(\n    "en-US",\n    "pt-BR"\n)\n',
            returncode=0,
        )
    if "AppleCurrentKeyboardLayoutInputSourceID" in cmd:
        return mock.Mock(stdout="com.apple.keylayout.US\n", returncode=0)
    if "export" in cmd and "HIToolbox" in cmd_str:
        plist_bytes = plistlib.dumps({
            "AppleEnabledInputSources": [
                {"KeyboardLayout Name": "U.S."},
            ],
        })
        return mock.Mock(stdout=plist_bytes, returncode=0)
    if "AppleICUDateFormatStrings" in cmd:
        return mock.Mock(
            stdout='{\n    1 = "M/d/yy";\n    2 = "MMM d, y";\n}\n',
            returncode=0,
        )
    if "AppleICUNumberSymbols" in cmd:
        return mock.Mock(
            stdout='{\n    0 = ".";\n    1 = ",";\n}\n',
            returncode=0,
        )
    return mock.Mock(stdout="", returncode=0)


def test_collect_locale_happy_path():
    """All 9 locale fields populate when subprocess calls succeed."""
    with mock.patch(
        "screencap.metrics.subprocess.run",
        side_effect=_defaults_side_effect,
    ):
        result = _collect_locale()

    assert set(result.keys()) == {
        "system_locale",
        "preferred_languages",
        "keyboard_layout",
        "input_sources",
        "timezone",
        "timezone_offset",
        "date_format",
        "number_format",
        "currency_code",
    }
    assert result["system_locale"] == "en_US"
    assert result["preferred_languages"] == ["en-US", "pt-BR"]
    assert result["keyboard_layout"] == "com.apple.keylayout.US"
    assert result["input_sources"] == ["U.S."]
    assert result["timezone"] is not None  # depends on host
    assert result["timezone_offset"] is not None
    assert result["date_format"] == "M/d/yy"
    assert result["number_format"] == {"decimal_separator": ".", "grouping_separator": ","}
    assert result["currency_code"] == "USD"


def test_collect_locale_partial_failure():
    """If one defaults call fails, other fields still populate."""
    call_count = 0

    def flaky_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        cmd = args[0] if args else kwargs.get("args", [])
        # Fail the first subprocess call (AppleLocale for system_locale)
        if call_count == 1:
            raise OSError("simulated failure")
        return _defaults_side_effect(cmd, **kwargs)

    with mock.patch(
        "screencap.metrics.subprocess.run",
        side_effect=flaky_side_effect,
    ):
        result = _collect_locale()

    # The first field (system_locale) should be None due to the failure
    assert result["system_locale"] is None
    # Other subprocess-based fields should still succeed
    assert result["preferred_languages"] == ["en-US", "pt-BR"]
    assert result["keyboard_layout"] == "com.apple.keylayout.US"
    assert result["timezone"] is not None


# --- WiFi metrics tests ---


def _make_mock_wifi_interface(*, ssid="MyNet", phy_mode=6, rssi=-55, tx_rate=540.0):
    """Create a mock CWInterface with configurable values."""
    iface = mock.Mock()
    iface.ssid.return_value = ssid
    iface.activePHYMode.return_value = phy_mode
    iface.rssiValue.return_value = rssi
    iface.transmitRate.return_value = tx_rate
    return iface


def _make_mock_wifi_client(iface=None):
    """Create a mock CWWiFiClient.sharedWiFiClient()."""
    client = mock.Mock()
    client.interface.return_value = iface
    return client


def test_collect_wifi_static_happy_path():
    """Connected WiFi returns connected=True and phy_mode string."""
    iface = _make_mock_wifi_interface()
    client = _make_mock_wifi_client(iface)

    mock_module = mock.MagicMock()
    mock_module.CWWiFiClient.sharedWiFiClient.return_value = client

    with mock.patch.dict("sys.modules", {"CoreWLAN": mock_module}):
        result = _collect_wifi_static()

    assert result == {"connected": True, "phy_mode": "802.11ax"}


def test_collect_wifi_static_ssid_nil_but_rssi_nonzero():
    """macOS 14+ returns nil ssid without Location Services; RSSI fallback detects connected."""
    iface = _make_mock_wifi_interface(ssid=None, phy_mode=6, rssi=-44)
    client = _make_mock_wifi_client(iface)

    mock_module = mock.MagicMock()
    mock_module.CWWiFiClient.sharedWiFiClient.return_value = client

    with mock.patch.dict("sys.modules", {"CoreWLAN": mock_module}):
        result = _collect_wifi_static()

    assert result["connected"] is True
    assert result["phy_mode"] == "802.11ax"


def test_collect_wifi_static_no_interface():
    """No WiFi hardware returns None fields."""
    client = _make_mock_wifi_client(iface=None)

    mock_module = mock.MagicMock()
    mock_module.CWWiFiClient.sharedWiFiClient.return_value = client

    with mock.patch.dict("sys.modules", {"CoreWLAN": mock_module}):
        result = _collect_wifi_static()

    assert result == {"connected": None, "phy_mode": None}


def test_collect_wifi_static_not_connected():
    """WiFi interface exists but not connected (ssid=None and rssi=0)."""
    iface = _make_mock_wifi_interface(ssid=None, rssi=0)
    client = _make_mock_wifi_client(iface)

    mock_module = mock.MagicMock()
    mock_module.CWWiFiClient.sharedWiFiClient.return_value = client

    with mock.patch.dict("sys.modules", {"CoreWLAN": mock_module}):
        result = _collect_wifi_static()

    assert result["connected"] is False


def test_collect_wifi_static_import_error():
    """CoreWLAN not installed returns None."""
    with mock.patch.dict("sys.modules", {"CoreWLAN": None}):
        result = _collect_wifi_static()

    assert result is None


def test_collect_wifi_dynamic_happy_path():
    """Connected WiFi returns RSSI and TX rate."""
    iface = _make_mock_wifi_interface(rssi=-55, tx_rate=540.0)
    client = _make_mock_wifi_client(iface)

    mock_module = mock.MagicMock()
    mock_module.CWWiFiClient.sharedWiFiClient.return_value = client

    with mock.patch.dict("sys.modules", {"CoreWLAN": mock_module}):
        result = _collect_wifi_dynamic()

    assert result == {"rssi_dbm": -55, "tx_rate_mbps": 540.0}


def test_collect_wifi_dynamic_rssi_zero_sentinel():
    """RSSI of 0 is treated as invalid and stored as None."""
    iface = _make_mock_wifi_interface(rssi=0, tx_rate=867.0)
    client = _make_mock_wifi_client(iface)

    mock_module = mock.MagicMock()
    mock_module.CWWiFiClient.sharedWiFiClient.return_value = client

    with mock.patch.dict("sys.modules", {"CoreWLAN": mock_module}):
        result = _collect_wifi_dynamic()

    assert result["rssi_dbm"] is None
    assert result["tx_rate_mbps"] == 867.0


def test_collect_wifi_dynamic_no_interface():
    """No WiFi hardware returns None fields for dynamic metrics."""
    client = _make_mock_wifi_client(iface=None)

    mock_module = mock.MagicMock()
    mock_module.CWWiFiClient.sharedWiFiClient.return_value = client

    with mock.patch.dict("sys.modules", {"CoreWLAN": mock_module}):
        result = _collect_wifi_dynamic()

    assert result == {"rssi_dbm": None, "tx_rate_mbps": None}


def test_collect_wifi_dynamic_import_error():
    """CoreWLAN not installed returns None for dynamic."""
    with mock.patch.dict("sys.modules", {"CoreWLAN": None}):
        result = _collect_wifi_dynamic()

    assert result is None


def test_wifi_metrics_disabled():
    """When wifi_metrics=False, wifi key is omitted from both static and dynamic."""
    with (
        mock.patch("screencap.metrics.socket.gethostname", return_value="h"),
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

        static = collect_static_metrics(wifi_metrics=False)
        dynamic = collect_dynamic_metrics(wifi_metrics=False)

    assert "wifi" not in static
    assert "wifi" not in dynamic


def test_get_wifi_metrics_config_default():
    """Default returns True."""
    from screencap.config import get_wifi_metrics

    with mock.patch("screencap.config._load_toml", return_value={}):
        assert get_wifi_metrics() is True


def test_get_wifi_metrics_config_env_var():
    """Env var SCREENCAP_WIFI_METRICS overrides config."""
    from screencap.config import get_wifi_metrics

    with mock.patch.dict("os.environ", {"SCREENCAP_WIFI_METRICS": "false"}):
        assert get_wifi_metrics() is False

    with mock.patch.dict("os.environ", {"SCREENCAP_WIFI_METRICS": "true"}):
        assert get_wifi_metrics() is True


def test_get_wifi_metrics_config_toml():
    """config.toml wifi_metrics=false disables WiFi collection."""
    from screencap.config import get_wifi_metrics

    with (
        mock.patch.dict("os.environ", {}, clear=True),
        mock.patch("screencap.config._load_toml", return_value={"wifi_metrics": False}),
    ):
        assert get_wifi_metrics() is False
