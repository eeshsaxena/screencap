"""Tests for src/screencap/network/system_proxy.py.

The functions are thin wrappers over `networksetup` / `osascript`; we
test by mocking subprocess.run and asserting the command sequences +
parsing behaviour.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from screencap.network.system_proxy import (
    ServiceProxyState,
    SystemProxyError,
    _build_restore_commands,
    _build_set_commands,
    list_active_services,
    read_snapshot,
    restore_all,
    services_changed_marker,
    set_proxy_all,
    snapshot_service,
    write_snapshot,
)


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


# ---------------------------------------------------------------------------
# list_active_services
# ---------------------------------------------------------------------------


class TestListActiveServices:
    def test_strips_header_and_disabled(self):
        stdout = (
            "An asterisk (*) denotes that a network service is disabled.\n"
            "Wi-Fi\n"
            "Ethernet\n"
            "*Bluetooth PAN\n"
            "Thunderbolt Bridge\n"
        )
        with patch("subprocess.run", return_value=_completed(stdout=stdout)):
            assert list_active_services() == ["Wi-Fi", "Ethernet", "Thunderbolt Bridge"]

    def test_raises_on_nonzero_exit(self):
        with patch("subprocess.run", return_value=_completed(returncode=1, stderr="bad")):
            with pytest.raises(SystemProxyError, match="exit 1"):
                list_active_services()


# ---------------------------------------------------------------------------
# snapshot_service
# ---------------------------------------------------------------------------


class TestSnapshotService:
    def test_parses_full_snapshot(self):
        web_stdout = "Enabled: No\nServer:\nPort: 0\nAuthenticated Proxy Enabled: 0\n"
        secure_stdout = "Enabled: Yes\nServer: corp.proxy\nPort: 8443\nAuthenticated Proxy Enabled: 0\n"
        bypass_stdout = "*.local\n169.254/16\n"

        def fake_run(cmd, **kwargs):
            if "-getwebproxy" in cmd:
                return _completed(stdout=web_stdout)
            if "-getsecurewebproxy" in cmd:
                return _completed(stdout=secure_stdout)
            if "-getproxybypassdomains" in cmd:
                return _completed(stdout=bypass_stdout)
            return _completed()

        with patch("subprocess.run", side_effect=fake_run):
            state = snapshot_service("Wi-Fi")
        assert state.service_name == "Wi-Fi"
        assert state.web_proxy is not None
        assert state.web_proxy["Enabled"] == "No"
        assert state.secure_web_proxy is not None
        assert state.secure_web_proxy["Server"] == "corp.proxy"
        assert state.secure_web_proxy["Port"] == 8443
        assert state.bypass_domains == ("*.local", "169.254/16")

    def test_empty_bypass_skips_placeholder(self):
        with patch("subprocess.run") as run_mock:
            run_mock.side_effect = [
                _completed(stdout="Enabled: No\n"),
                _completed(stdout="Enabled: No\n"),
                _completed(stdout="There aren't any bypass domains set on Wi-Fi.\n"),
            ]
            state = snapshot_service("Wi-Fi")
        assert state.bypass_domains == ()


# ---------------------------------------------------------------------------
# Snapshot round-trip
# ---------------------------------------------------------------------------


class TestSnapshotRoundTrip:
    def test_write_then_read(self, tmp_path: Path):
        snapshot = {
            "Wi-Fi": ServiceProxyState(
                service_name="Wi-Fi",
                web_proxy={"Enabled": "No"},
                secure_web_proxy={"Enabled": "Yes", "Server": "x", "Port": 9},
                bypass_domains=("*.local",),
            )
        }
        path = tmp_path / "state.json"
        write_snapshot(
            snapshot,
            path,
            extra={"recording_id": "rec-1", "worker_pid": 12345},
        )
        services, extra = read_snapshot(path)
        assert services["Wi-Fi"].secure_web_proxy["Server"] == "x"
        assert services["Wi-Fi"].bypass_domains == ("*.local",)
        assert extra["recording_id"] == "rec-1"
        assert extra["worker_pid"] == 12345

    def test_atomic_write_uses_replace(self, tmp_path: Path):
        snapshot = {"Wi-Fi": ServiceProxyState(service_name="Wi-Fi")}
        path = tmp_path / "state.json"
        write_snapshot(snapshot, path)
        # No leftover .tmp file.
        assert not path.with_suffix(path.suffix + ".tmp").exists()
        # Mode is 600.
        assert (path.stat().st_mode & 0o777) == 0o600

    def test_read_missing_raises_filenotfounderror(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            read_snapshot(tmp_path / "missing.json")


# ---------------------------------------------------------------------------
# _build_set_commands / _build_restore_commands — shell-injection guards
# ---------------------------------------------------------------------------


class TestShellInjectionGuards:
    def test_service_name_with_semicolon_is_quoted(self):
        # Plan F2 — service name with shell metacharacters must NOT execute as
        # injected payload. Verify the dangerous substring appears wrapped
        # in shlex.quote-style single quotes, defeating shell evaluation.
        cmd = _build_set_commands("127.0.0.1", 8080, ["Wi-Fi; rm -rf ~"])
        # The exact form shlex.quote produces for a string containing
        # special chars: wrapped in single quotes.
        assert "'Wi-Fi; rm -rf ~'" in cmd
        # And the command MUST NOT contain a stray `rm` token outside
        # quotes. Find every `'...'` quoted span and remove it before
        # checking for unquoted `rm`.
        import re

        unquoted = re.sub(r"'[^']*'", "''", cmd)
        assert " rm " not in unquoted, (
            f"unquoted 'rm' found in command — possible injection: {unquoted!r}"
        )

    def test_service_name_with_backticks_is_quoted(self):
        cmd = _build_set_commands("127.0.0.1", 8080, ["X`whoami`"])
        # shlex.quote returns the value wrapped in single quotes; backticks
        # are inert inside single quotes.
        assert "'X`whoami`'" in cmd

    def test_invalid_port_raises(self):
        with pytest.raises(ValueError):
            set_proxy_all("127.0.0.1", 99999, ["Wi-Fi"])

    def test_invalid_host_raises(self):
        with pytest.raises(ValueError):
            set_proxy_all("evil.example.com", 8080, ["Wi-Fi"])

    def test_empty_services_no_op(self):
        # _build_set_commands with empty list returns ":" no-op
        assert _build_set_commands("127.0.0.1", 8080, []) == ":"


# ---------------------------------------------------------------------------
# set_proxy_all / restore_all — single osascript admin call
# ---------------------------------------------------------------------------


class TestSetProxyAll:
    def test_runs_single_osascript_admin(self):
        with patch("subprocess.run", return_value=_completed()) as run_mock:
            set_proxy_all("127.0.0.1", 8080, ["Wi-Fi", "Ethernet"], prompt="Test prompt")
        assert run_mock.call_count == 1
        cmd = run_mock.call_args.args[0]
        assert cmd[0] == "/usr/bin/osascript"
        assert cmd[1] == "-e"
        apple_script = cmd[2]
        assert "do shell script" in apple_script
        assert "with administrator privileges" in apple_script
        assert "Test prompt" in apple_script
        # Both services configured in the same shell command.
        assert "-setwebproxy" in apple_script
        assert "-setsecurewebproxy" in apple_script
        assert "Wi-Fi" in apple_script
        assert "Ethernet" in apple_script

    def test_raises_on_osascript_failure(self):
        with patch(
            "subprocess.run",
            return_value=_completed(returncode=1, stderr="user cancelled"),
        ):
            with pytest.raises(SystemProxyError, match="exit 1"):
                set_proxy_all("127.0.0.1", 8080, ["Wi-Fi"])


class TestRestoreAll:
    def test_runs_inverse_osascript(self):
        snapshot = {
            "Wi-Fi": ServiceProxyState(
                service_name="Wi-Fi",
                web_proxy={"Enabled": "No", "Server": "", "Port": 0},
                secure_web_proxy={"Enabled": "Yes", "Server": "corp.proxy", "Port": 8443},
                bypass_domains=(),
            )
        }
        with patch("subprocess.run", return_value=_completed()) as run_mock:
            restore_all(snapshot, prompt="Restoring...")
        cmd = run_mock.call_args.args[0]
        apple_script = cmd[2]
        assert "with administrator privileges" in apple_script
        assert "Restoring..." in apple_script
        # secure-web proxy was enabled → restore enables it
        assert "-setsecurewebproxystate" in apple_script
        assert "corp.proxy" in apple_script
        # web proxy was disabled → restore turns it off
        assert "-setwebproxystate" in apple_script

    def test_empty_snapshot_no_op(self):
        with patch("subprocess.run") as run_mock:
            restore_all({})
        run_mock.assert_not_called()


# ---------------------------------------------------------------------------
# services_changed_marker
# ---------------------------------------------------------------------------


class TestServicesChangedMarker:
    def test_writes_diff_when_services_added(self, tmp_path: Path):
        marker_path = tmp_path / ".network_services_changed.json"
        wrote = services_changed_marker(
            ["Wi-Fi", "Ethernet"],
            ["Wi-Fi", "Ethernet", "USB Ethernet"],
            marker_path,
        )
        assert wrote is True
        assert marker_path.exists()
        payload = json.loads(marker_path.read_text())
        assert payload["added"] == ["USB Ethernet"]
        assert payload["removed"] == []

    def test_writes_diff_when_services_removed(self, tmp_path: Path):
        marker_path = tmp_path / ".network_services_changed.json"
        wrote = services_changed_marker(
            ["Wi-Fi", "Ethernet", "VPN"],
            ["Wi-Fi", "Ethernet"],
            marker_path,
        )
        assert wrote is True
        payload = json.loads(marker_path.read_text())
        assert payload["removed"] == ["VPN"]

    def test_no_op_when_unchanged(self, tmp_path: Path):
        marker_path = tmp_path / ".network_services_changed.json"
        wrote = services_changed_marker(
            ["Wi-Fi", "Ethernet"],
            ["Ethernet", "Wi-Fi"],
            marker_path,
        )
        assert wrote is False
        assert not marker_path.exists()


# ---------------------------------------------------------------------------
# Regression: set -e prefix so non-final command failures propagate
# (PR #156 review P1-1)
# ---------------------------------------------------------------------------


class TestSetEPropagation:
    """Without `set -e`, joining commands with `;` masks all but the
    last command's exit status. A failed mid-script `setwebproxy` would
    be silently ignored if the trailing `setwebproxystate on` succeeds,
    leaving system proxy state partially changed."""

    def test_set_commands_have_set_e_prefix(self):
        cmd = _build_set_commands("127.0.0.1", 8080, ["Wi-Fi", "Ethernet"])
        assert cmd.startswith("set -e; "), (
            f"missing 'set -e' prefix; without it, partial failures are "
            f"masked. Got: {cmd[:60]!r}"
        )

    def test_restore_commands_have_set_e_prefix(self):
        snapshot = {
            "Wi-Fi": ServiceProxyState(
                service_name="Wi-Fi",
                web_proxy={"Enabled": "No", "Server": "", "Port": 0},
                secure_web_proxy={"Enabled": "Yes", "Server": "x", "Port": 9},
                bypass_domains=(),
            ),
        }
        cmd = _build_restore_commands(snapshot)
        assert cmd.startswith("set -e; "), (
            f"missing 'set -e' prefix on restore; got: {cmd[:60]!r}"
        )

    def test_set_e_does_not_break_empty_services(self):
        # Empty-services case still returns the no-op `:` shell command.
        assert _build_set_commands("127.0.0.1", 8080, []) == ":"
