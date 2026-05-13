"""LaunchAgent plist rendering and launchctl lifecycle tests."""

from __future__ import annotations

import errno
import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest


def _completed(
    cmd: list[str],
    returncode: int = 0,
    stderr: str = "",
    stdout: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


def _parse_plist(content: bytes) -> dict[str, object]:
    return plistlib.loads(content)


def test_render_plist_contains_required_stable_keys_and_no_launchd_extras():
    from screencap.daemon import launchagent

    content = launchagent.render_plist(program="/usr/local/bin/screencap")
    parsed = _parse_plist(content)

    assert parsed["Label"] == "com.screencap.daemon"
    assert parsed["ProgramArguments"] == ["/usr/local/bin/screencap", "serve"]
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] == {"SuccessfulExit": False, "Crashed": True}
    assert parsed["ProcessType"] == "Adaptive"
    assert parsed["ExitTimeOut"] == 30
    # Default `log_dir=None` omits StandardErrorPath/StandardOutPath because
    # launchd in macOS 26+ does NOT expand `~` or `$HOME` in path keys
    # (verified empirically: a literal `~/Library/Logs/...` causes launchd to
    # spawn the daemon with EX_CONFIG when the path is opened). The bundled
    # SMAppService plist must work for any user, so it ships without these
    # path keys — daemon stdout/stderr land in the unified system log,
    # accessible via `log show --predicate 'process == "screencap"'`. The CLI
    # install path (`screencap serve --install`) bakes an absolute log dir
    # at install time instead.
    assert "StandardErrorPath" not in parsed
    assert "StandardOutPath" not in parsed
    # SCREENCAP_RUN_DIR is intentionally absent from the default env_vars: the
    # daemon's own default_socket_path() resolves ~/.screencap/run/api.sock via
    # Path.home() at runtime, and launchd does not expand $HOME in env values.
    assert "SCREENCAP_RUN_DIR" not in parsed["EnvironmentVariables"]
    assert parsed["EnvironmentVariables"]["PATH"]

    assert "LimitLoadToSessionType" not in parsed
    assert "WatchPaths" not in parsed
    assert "Sockets" not in parsed
    assert "MachServices" not in parsed


def test_render_plist_with_log_dir_sets_absolute_path_keys(tmp_path: Path):
    # The CLI install path passes an absolute log dir resolved from
    # Path.home() at install time. Verify those values land verbatim in the
    # plist's StandardErrorPath / StandardOutPath keys (no further expansion).
    from screencap.daemon import launchagent

    content = launchagent.render_plist(
        program="/usr/local/bin/screencap",
        log_dir=str(tmp_path / "Library/Logs/ScreenCap"),
    )
    parsed = _parse_plist(content)

    expected = str(tmp_path / "Library/Logs/ScreenCap")
    assert parsed["StandardErrorPath"] == f"{expected}/daemon.err.log"
    assert parsed["StandardOutPath"] == f"{expected}/daemon.out.log"


def test_render_plist_is_deterministic_for_same_inputs(tmp_path: Path):
    from screencap.daemon import launchagent

    kwargs = {
        "program": "/opt/screencap/bin/screencap",
        "args": ("serve", "--socket", str(tmp_path / "api.sock")),
        "log_dir": tmp_path / "logs",
        "env_vars": {"PATH": "/usr/bin:/bin", "SCREENCAP_RUN_DIR": str(tmp_path / "run")},
    }

    assert launchagent.render_plist(**kwargs) == launchagent.render_plist(**kwargs)


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil unavailable")
def test_render_plist_validates_with_plutil():
    from screencap.daemon import launchagent

    content = launchagent.render_plist(program="/usr/local/bin/screencap")
    result = subprocess.run(
        ["plutil", "-lint", "-"],
        input=content,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_bundled_macos_launchagent_plist_matches_renderer():
    from screencap.daemon import launchagent

    repo_root = Path(__file__).resolve().parents[2]
    bundled = repo_root / "macos" / "ScreenCap" / "Resources" / "com.screencap.daemon.plist"

    expected = launchagent.render_plist(
        program="screencap",
        bundle_program="Contents/Resources/screencap/screencap",
    )

    assert bundled.read_bytes() == expected


def test_install_bootstraps_and_reports_running(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from screencap.daemon import launchagent

    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return _completed(cmd)

    monkeypatch.setattr(launchagent.subprocess, "run", fake_run)
    monkeypatch.setattr(launchagent, "_daemon_info_responds", lambda *_args, **_kwargs: True)

    result = launchagent.install(program="/bin/screencap", plist_path=tmp_path / "agent.plist")

    assert result.state == "installed_and_running"
    assert calls[0][:3] == ["launchctl", "bootstrap", f"gui/{launchagent.os.getuid()}"]
    assert (tmp_path / "agent.plist").exists()


def test_install_is_idempotent_when_existing_plist_has_same_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    from screencap.daemon import launchagent

    # Match install()'s rendering: it bakes an absolute log dir under the
    # current user's home, so the idempotency check requires the same
    # log_dir argument to produce byte-identical content.
    install_log_dir = str(Path.home() / "Library" / "Logs" / "ScreenCap")
    expected_content = launchagent.render_plist(
        program="/bin/screencap", log_dir=install_log_dir
    )

    plist_path = tmp_path / "agent.plist"
    plist_path.write_bytes(expected_content)
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return _completed(cmd)

    monkeypatch.setattr(launchagent.subprocess, "run", fake_run)
    monkeypatch.setattr(launchagent, "_daemon_info_responds", lambda *_args, **_kwargs: True)

    result = launchagent.install(program="/bin/screencap", plist_path=plist_path)

    assert result.state == "installed_and_running"
    assert plist_path.read_bytes() == expected_content
    assert [cmd[1] for cmd in calls] == ["bootstrap"]


def test_install_overwrites_changed_plist_and_kickstarts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    from screencap.daemon import launchagent

    plist_path = tmp_path / "agent.plist"
    plist_path.write_bytes(b"old plist")
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return _completed(cmd)

    monkeypatch.setattr(launchagent.subprocess, "run", fake_run)
    monkeypatch.setattr(launchagent, "_daemon_info_responds", lambda *_args, **_kwargs: True)

    result = launchagent.install(program="/bin/screencap", plist_path=plist_path)

    assert result.state == "installed_and_running"
    assert plist_path.read_bytes().startswith(b"<?xml")
    assert [cmd[1] for cmd in calls] == ["bootstrap", "kickstart"]


def test_install_already_loaded_kickstarts_and_reports_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    from screencap.daemon import launchagent

    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[1] == "bootstrap":
            return _completed(cmd, returncode=5, stderr="service already bootstrapped")
        return _completed(cmd)

    monkeypatch.setattr(launchagent.subprocess, "run", fake_run)
    monkeypatch.setattr(launchagent, "_daemon_info_responds", lambda *_args, **_kwargs: True)

    result = launchagent.install(program="/bin/screencap", plist_path=tmp_path / "agent.plist")

    assert result.state == "installed_and_running"
    assert [cmd[1] for cmd in calls] == ["bootstrap", "kickstart"]


def test_install_operation_not_permitted_returns_permission_required(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    from screencap.daemon import launchagent

    monkeypatch.setattr(
        launchagent.subprocess,
        "run",
        lambda cmd, **_kwargs: _completed(cmd, returncode=1, stderr="Operation not permitted"),
    )

    result = launchagent.install(program="/bin/screencap", plist_path=tmp_path / "agent.plist")

    assert result.state == "permission_required"
    assert "Operation not permitted" in result.detail


def test_install_signing_error_is_classified(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    from screencap.daemon import launchagent

    monkeypatch.setattr(
        launchagent.subprocess,
        "run",
        lambda cmd, **_kwargs: _completed(
            cmd,
            returncode=1,
            stderr="Operation not permitted: code signature invalid",
        ),
    )

    result = launchagent.install(program="/bin/screencap", plist_path=tmp_path / "agent.plist")

    assert result.state == "install_failed_daemon_signing_invalid"


def test_install_generic_bootstrap_failure_includes_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    from screencap.daemon import launchagent

    plist_path = tmp_path / "agent.plist"
    monkeypatch.setattr(
        launchagent.subprocess,
        "run",
        lambda cmd, **_kwargs: _completed(cmd, returncode=42, stderr="launchd said no"),
    )

    result = launchagent.install(program="/bin/screencap", plist_path=plist_path)

    assert result.state == "install_failed_launchctl_bootstrap_failed"
    assert "launchd said no" in result.detail
    assert not plist_path.exists()


def test_install_times_out_when_daemon_never_responds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    from screencap.daemon import launchagent

    monkeypatch.setattr(
        launchagent.subprocess,
        "run",
        lambda cmd, **_kwargs: _completed(cmd),
    )
    monkeypatch.setattr(launchagent, "_daemon_info_responds", lambda *_args, **_kwargs: False)

    result = launchagent.install(
        program="/bin/screencap",
        plist_path=tmp_path / "agent.plist",
        timeout_seconds=0,
    )

    assert result.state == "install_failed_daemon_did_not_start"


def test_install_failed_already_running_when_daemon_exits_75(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """When the daemon never responds AND launchctl reports the spawned
    process's last exit code as 75 (EX_TEMPFAIL), the install verifier
    classifies the failure as `install_failed_already_running`."""
    from screencap.daemon import launchagent

    launchctl_print_output = (
        "com.screencap.daemon = {\n"
        "\tstate = exited\n"
        "\tlast exit code = 75\n"
        "}\n"
    )

    def fake_run(cmd, **_kwargs):
        if cmd[:2] == ["launchctl", "print"]:
            return _completed(cmd, returncode=0, stdout=launchctl_print_output)
        return _completed(cmd)

    monkeypatch.setattr(launchagent.subprocess, "run", fake_run)
    monkeypatch.setattr(launchagent, "_daemon_info_responds", lambda *_args, **_kwargs: False)

    result = launchagent.install(
        program="/bin/screencap",
        plist_path=tmp_path / "agent.plist",
        timeout_seconds=0,
    )

    assert result.state == "install_failed_already_running"
    assert "75" in result.detail or "already" in result.detail.lower()


def test_install_failed_did_not_start_when_launchctl_print_unparseable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """If `launchctl print` is missing the `last exit code` field, the
    install verifier falls back to the generic did-not-start state."""
    from screencap.daemon import launchagent

    def fake_run(cmd, **_kwargs):
        if cmd[:2] == ["launchctl", "print"]:
            return _completed(cmd, returncode=0, stdout="com.screencap.daemon = {\n}\n")
        return _completed(cmd)

    monkeypatch.setattr(launchagent.subprocess, "run", fake_run)
    monkeypatch.setattr(launchagent, "_daemon_info_responds", lambda *_args, **_kwargs: False)

    result = launchagent.install(
        program="/bin/screencap",
        plist_path=tmp_path / "agent.plist",
        timeout_seconds=0,
    )

    assert result.state == "install_failed_daemon_did_not_start"


def test_parse_last_exit_code_extracts_signed_int():
    from screencap.daemon.launchagent import _parse_last_exit_code

    assert _parse_last_exit_code("\tlast exit code = 75\n") == 75
    assert _parse_last_exit_code("last exit code = 0") == 0
    assert _parse_last_exit_code("last exit code = -9") == -9
    assert _parse_last_exit_code("state = running\n") is None
    assert _parse_last_exit_code("") is None


def test_parse_last_exit_code_strips_symbolic_suffix():
    """launchctl on some macOS versions appends ``: EX_TEMPFAIL`` (or
    other symbolic suffixes) to the exit code line. The parser must
    return the leading integer in both forms."""
    from screencap.daemon.launchagent import _parse_last_exit_code

    assert _parse_last_exit_code("\tlast exit code = 75: EX_TEMPFAIL\n") == 75
    assert _parse_last_exit_code("last exit code = 0: ok") == 0


def test_install_write_failure_is_classified(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from screencap.daemon import launchagent

    def fail_replace(_src, _dst):
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(launchagent.os, "replace", fail_replace)

    result = launchagent.install(program="/bin/screencap", plist_path=tmp_path / "agent.plist")

    assert result.state == "install_failed_plist_write_failed"


def test_install_disk_full_is_classified(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from screencap.daemon import launchagent

    def fail_replace(_src, _dst):
        raise OSError(errno.ENOSPC, "no space left")

    monkeypatch.setattr(launchagent.os, "replace", fail_replace)

    result = launchagent.install(program="/bin/screencap", plist_path=tmp_path / "agent.plist")

    assert result.state == "install_failed_disk_full"


def test_uninstall_boots_out_and_removes_plist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from screencap.daemon import launchagent

    plist_path = tmp_path / "agent.plist"
    plist_path.write_text("plist", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return _completed(cmd)

    monkeypatch.setattr(launchagent.subprocess, "run", fake_run)

    result = launchagent.uninstall(plist_path=plist_path)

    assert result.state == "uninstalled"
    assert not plist_path.exists()
    assert calls == [["launchctl", "bootout", f"gui/{launchagent.os.getuid()}/com.screencap.daemon"]]


def test_uninstall_is_idempotent_when_agent_not_loaded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    from screencap.daemon import launchagent

    def fake_run(cmd, **_kwargs):
        return _completed(cmd, returncode=36, stderr="No such service")

    monkeypatch.setattr(launchagent.subprocess, "run", fake_run)

    result = launchagent.uninstall(plist_path=tmp_path / "missing.plist")

    assert result.state == "uninstalled"


def test_status_reports_loaded_state(monkeypatch: pytest.MonkeyPatch):
    from screencap.daemon import launchagent

    monkeypatch.setattr(
        launchagent.subprocess,
        "run",
        lambda cmd, **_kwargs: _completed(cmd, stdout="state = running\n"),
    )

    result = launchagent.status()

    assert result.state == "loaded"
    assert result.launchd_state == "running"


def test_status_reports_not_loaded(monkeypatch: pytest.MonkeyPatch):
    from screencap.daemon import launchagent

    monkeypatch.setattr(
        launchagent.subprocess,
        "run",
        lambda cmd, **_kwargs: _completed(cmd, returncode=113, stderr="No such service"),
    )

    result = launchagent.status()

    assert result.state == "not_loaded"


def test_daemon_smoke_check_imports_and_finds_required_routes():
    from screencap.cli import _check_daemon_load

    assert _check_daemon_load() == ("daemon_load", True, "")
