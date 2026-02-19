"""Tests for screencap CLI argument parsing."""

from unittest import mock

from click.testing import CliRunner

from screencap.cli import cli


def test_version():
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "screencap" in result.output


def test_list_empty(tmp_path):
    runner = CliRunner()
    with mock.patch("screencap.catalog.get_recordings_dir", return_value=tmp_path):
        result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        assert "No recordings" in result.output


def test_list_json_empty(tmp_path):
    runner = CliRunner()
    with mock.patch("screencap.catalog.get_recordings_dir", return_value=tmp_path):
        result = runner.invoke(cli, ["list", "--json"])
        assert result.exit_code == 0
        # Empty list still outputs "No recordings" message
        assert "No recordings" in result.output


def test_start_interactive():
    """Test that start command prompts when no --name given."""
    runner = CliRunner()
    with mock.patch("screencap.recorder.start_recording") as mock_rec:
        result = runner.invoke(
            cli,
            ["start"],
            input="my-test\nsome desc\ny\n",
        )
        assert result.exit_code == 0
        mock_rec.assert_called_once()
        args = mock_rec.call_args
        assert args[0][0] == "my-test"  # name
        assert args[0][1] == "some desc"  # description
        assert args[0][2] is True  # audio


def test_start_with_flags():
    """Test start with all flags."""
    runner = CliRunner()
    with mock.patch("screencap.recorder.start_recording") as mock_rec:
        result = runner.invoke(
            cli,
            ["start", "--name", "test-rec", "--no-audio", "-d", "demo"],
        )
        assert result.exit_code == 0
        mock_rec.assert_called_once_with("test-rec", "demo", False, None)


def test_view_not_found(tmp_path):
    runner = CliRunner()
    with mock.patch("screencap.config.get_recordings_dir", return_value=tmp_path):
        result = runner.invoke(cli, ["view", "nonexistent"])
        assert result.exit_code == 1
        assert "Error" in result.output


def test_scrub_missing():
    runner = CliRunner()
    with mock.patch("screencap.config.get_recordings_dir", return_value=mock.MagicMock()):
        with mock.patch("screencap.scrubber.scrub_recording", side_effect=SystemExit(1)):
            result = runner.invoke(cli, ["scrub", "missing"])
            assert result.exit_code == 1
