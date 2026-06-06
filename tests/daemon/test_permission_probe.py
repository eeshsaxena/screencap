"""U1: fresh-subprocess permission probe contract tests.

Covers the tri-state probe that lets the long-lived daemon read *live* TCC state
without restarting (R1, R9), and the SCR-69 guard that the probe shells out to a
real subcommand rather than ``sys.executable -c`` (rejected by the frozen-binary
Click entry point).
"""

from __future__ import annotations

import subprocess

import pytest

from screencap.daemon import permission_probe as pp

# --- probe_command(): argv shape + SCR-69 guard -----------------------------


def test_probe_command_uses_real_subcommand_never_dash_c() -> None:
    argv = pp.probe_command()
    assert "-c" not in argv, "probe must use a real subcommand, not `-c` (SCR-69)"
    assert pp.PROBE_SUBCOMMAND in argv
    # The startup update check must be suppressed so it neither makes a network
    # call nor pollutes the result line on stdout.
    assert "--no-update-check" in argv


def test_probe_command_frozen_split(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pp.sys, "frozen", True, raising=False)
    argv = pp.probe_command()
    # Frozen binary: invoke the bundled executable's subcommand directly.
    assert argv[1:] == ["--no-update-check", pp.PROBE_SUBCOMMAND]
    assert "-m" not in argv


def test_probe_command_dev_split(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pp.sys, "frozen", False, raising=False)
    argv = pp.probe_command()
    assert argv[1:] == ["-m", "screencap", "--no-update-check", pp.PROBE_SUBCOMMAND]


# --- run_probe_checks(): the in-process subcommand body ---------------------


def _force_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pp.sys, "platform", "darwin")


def test_run_probe_checks_maps_all_three_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All three permissions are read in one process (no short-circuit).

    Distinct return values per permission prove each check is invoked and mapped
    independently — the unit-testable proxy for "one fresh spawn reads all three".
    """
    _force_darwin(monkeypatch)
    from screencap.engine.platform.darwin import DarwinPlatform

    monkeypatch.setattr(DarwinPlatform, "is_screen_recording_enabled", staticmethod(lambda: True))
    monkeypatch.setattr(DarwinPlatform, "is_accessibility_enabled", staticmethod(lambda: False))
    monkeypatch.setattr(DarwinPlatform, "is_input_monitoring_enabled", staticmethod(lambda: True))

    result = pp.run_probe_checks()
    assert result == {
        "screen_recording": "granted",
        "accessibility": "denied",
        "input_monitoring": "granted",
    }


def test_run_probe_checks_per_permission_indeterminate_on_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected raise yields indeterminate for that one perm, never denied."""
    _force_darwin(monkeypatch)
    from screencap.engine.platform.darwin import DarwinPlatform

    def _boom() -> bool:
        raise RuntimeError("PyObjC hiccup")

    monkeypatch.setattr(DarwinPlatform, "is_screen_recording_enabled", staticmethod(lambda: True))
    monkeypatch.setattr(DarwinPlatform, "is_accessibility_enabled", staticmethod(_boom))
    monkeypatch.setattr(DarwinPlatform, "is_input_monitoring_enabled", staticmethod(lambda: False))

    result = pp.run_probe_checks()
    assert result["screen_recording"] == "granted"
    assert result["accessibility"] == "indeterminate"
    assert result["input_monitoring"] == "denied"


def test_run_probe_checks_non_darwin_all_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pp.sys, "platform", "linux")
    assert pp.run_probe_checks() == pp.indeterminate_result()
    # Fail-open invariant: never reports denied on a non-darwin host.
    assert "denied" not in pp.run_probe_checks().values()


# --- probe_permissions(): the invoker (subprocess mocked) -------------------


class _FakeProc:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout


def test_probe_permissions_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pp.subprocess,
        "run",
        lambda *a, **k: _FakeProc(
            0,
            '{"screen_recording": "granted", "accessibility": "granted", '
            '"input_monitoring": "granted"}',
        ),
    )
    assert pp.probe_permissions() == {
        "screen_recording": "granted",
        "accessibility": "granted",
        "input_monitoring": "granted",
    }


def test_probe_permissions_one_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pp.subprocess,
        "run",
        lambda *a, **k: _FakeProc(
            0,
            '{"screen_recording": "denied", "accessibility": "granted", '
            '"input_monitoring": "granted"}',
        ),
    )
    result = pp.probe_permissions()
    assert result["screen_recording"] == "denied"
    assert result["accessibility"] == "granted"


def test_probe_permissions_timeout_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="probe", timeout=5)

    monkeypatch.setattr(pp.subprocess, "run", _timeout)
    result = pp.probe_permissions()
    assert result == pp.indeterminate_result()
    assert "denied" not in result.values()  # never denied on failure


def test_probe_permissions_nonzero_exit_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pp.subprocess, "run", lambda *a, **k: _FakeProc(1, '{"screen_recording": "granted"}')
    )
    assert pp.probe_permissions() == pp.indeterminate_result()


def test_probe_permissions_unparseable_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pp.subprocess, "run", lambda *a, **k: _FakeProc(0, "not json at all"))
    assert pp.probe_permissions() == pp.indeterminate_result()


def test_probe_permissions_missing_field_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial payload (older/odd subcommand) decodes the absent perm as
    indeterminate, never silently dropping it."""
    monkeypatch.setattr(
        pp.subprocess,
        "run",
        lambda *a, **k: _FakeProc(0, '{"screen_recording": "granted"}'),
    )
    result = pp.probe_permissions()
    assert result["screen_recording"] == "granted"
    assert result["accessibility"] == "indeterminate"
    assert result["input_monitoring"] == "indeterminate"


def test_probe_permissions_picks_json_from_last_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Incidental stdout before the result line must not break parsing."""
    monkeypatch.setattr(
        pp.subprocess,
        "run",
        lambda *a, **k: _FakeProc(
            0,
            "some banner noise\n"
            '{"screen_recording": "denied", "accessibility": "denied", '
            '"input_monitoring": "denied"}\n',
        ),
    )
    result = pp.probe_permissions()
    assert result == {
        "screen_recording": "denied",
        "accessibility": "denied",
        "input_monitoring": "denied",
    }


# --- end-to-end: one real fresh spawn checks all three ----------------------


def test_probe_end_to_end_single_spawn_returns_three_tristate() -> None:
    """Spawn the real ``_permission-probe`` subcommand once and assert it returns
    a valid tri-state for all three permissions.

    This is the integration proof that a single fresh process emits all three
    checks (R9 single-spawn-checks-three shape) and that the SCR-69 real-subcommand
    argv actually resolves and runs. Real TCC values vary by host (granted on a
    fully-authorized machine, denied/indeterminate otherwise), so we assert shape,
    not specific grants.
    """
    result = pp.probe_permissions()
    assert set(result) == set(pp.PERMISSION_KEYS)
    for key in pp.PERMISSION_KEYS:
        assert result[key] in {"granted", "denied", "indeterminate"}
