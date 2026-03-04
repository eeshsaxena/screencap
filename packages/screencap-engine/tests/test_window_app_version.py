"""Tests for app version lookup in window events (_macos.py)."""

from unittest import mock


def _make_quartz_meta(*, owner_name="Safari", window_name="My Page",
                      pid=1234, window_id=42,
                      bounds=None):
    """Create a mock Quartz window metadata dict."""
    if bounds is None:
        bounds = {"X": 0, "Y": 0, "Width": 1920, "Height": 1080}
    return {
        "kCGWindowOwnerName": owner_name,
        "kCGWindowName": window_name,
        "kCGWindowOwnerPID": pid,
        "kCGWindowNumber": window_id,
        "kCGWindowBounds": bounds,
        "kCGWindowLayer": 0,
    }


def test_window_state_includes_app_version():
    """get_active_window_state returns app_bundle_id and app_version."""
    meta = _make_quartz_meta(owner_name="Safari", pid=1234)

    with (
        mock.patch(
            "sc_engine.window._macos.get_active_window_meta",
            return_value=meta,
        ),
        mock.patch(
            "sc_engine.window._macos._get_app_version_info",
            return_value=("com.apple.Safari", "18.2"),
        ),
    ):
        from sc_engine.window._macos import get_active_window_state
        result = get_active_window_state(read_window_data=False)

    assert result is not None
    assert result["app_bundle_id"] == "com.apple.Safari"
    assert result["app_version"] == "18.2"
    assert result["title"] == "Safari My Page"


def test_window_state_version_none_on_failure():
    """If version lookup fails, version fields are None."""
    meta = _make_quartz_meta(pid=1234)

    with (
        mock.patch(
            "sc_engine.window._macos.get_active_window_meta",
            return_value=meta,
        ),
        mock.patch(
            "sc_engine.window._macos._get_app_version_info",
            return_value=(None, None),
        ),
    ):
        from sc_engine.window._macos import get_active_window_state
        result = get_active_window_state(read_window_data=False)

    assert result is not None
    assert result["app_bundle_id"] is None
    assert result["app_version"] is None


def test_window_state_bundle_id_without_version():
    """App with bundle_id but no version (e.g. no bundleURL)."""
    meta = _make_quartz_meta(pid=1234)

    with (
        mock.patch(
            "sc_engine.window._macos.get_active_window_meta",
            return_value=meta,
        ),
        mock.patch(
            "sc_engine.window._macos._get_app_version_info",
            return_value=("com.example.headless", None),
        ),
    ):
        from sc_engine.window._macos import get_active_window_state
        result = get_active_window_state(read_window_data=False)

    assert result["app_bundle_id"] == "com.example.headless"
    assert result["app_version"] is None


def test_window_data_surfaces_app_fields():
    """get_active_window_data() includes app_bundle_id and app_version at top level."""
    mock_state = {
        "title": "Safari My Page",
        "left": 0, "top": 0, "width": 1920, "height": 1080,
        "window_id": 42,
        "app_bundle_id": "com.apple.Safari",
        "app_version": "18.2",
        "meta": {},
        "data": {},
    }

    with mock.patch(
        "sc_engine.window.get_active_window_state",
        return_value=mock_state,
    ):
        from sc_engine.window import get_active_window_data
        result = get_active_window_data()

    assert result["app_bundle_id"] == "com.apple.Safari"
    assert result["app_version"] == "18.2"
    assert result["title"] == "Safari My Page"
