"""Tests for the auto-update module."""

from unittest.mock import patch, MagicMock

from click.testing import CliRunner

from screencap.updater import (
    get_latest_version,
    is_update_available,
)


def test_get_latest_version_success():
    mock_resp = MagicMock()
    mock_resp.text = "0.5.0\n"
    mock_resp.raise_for_status = MagicMock()
    with patch("screencap.updater.requests.get", return_value=mock_resp):
        assert get_latest_version() == "0.5.0"


def test_get_latest_version_network_failure():
    with patch("screencap.updater.requests.get", side_effect=Exception("timeout")):
        assert get_latest_version() is None


def test_is_update_available_newer():
    with patch("screencap.updater.__version__", "0.4.1"):
        assert is_update_available("0.5.0") is True


def test_is_update_available_same():
    with patch("screencap.updater.__version__", "0.5.0"):
        assert is_update_available("0.5.0") is False


def test_is_update_available_older():
    with patch("screencap.updater.__version__", "0.5.0"):
        assert is_update_available("0.4.0") is False


def test_is_update_available_malformed():
    assert is_update_available("not-a-version") is False


def test_maybe_check_skipped_when_not_frozen():
    """Update check must not run for pip/dev installs."""
    with patch("screencap.updater.get_latest_version") as mock:
        from screencap.updater import maybe_check_for_update
        maybe_check_for_update()  # sys.frozen is False in test env
        mock.assert_not_called()


def test_update_command_not_frozen():
    """screencap update prints guidance for pip installs."""
    from screencap.cli import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["update"])
    assert "pip install --upgrade" in result.output
