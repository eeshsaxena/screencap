"""U4: identity-scoped decoy/orphan ``tccutil`` cleanup (R5, R6, R8).

The cleanup is a destructive surface — an over-broad ``tccutil reset`` wipes
unrelated apps' grants (incl. the app's own Microphone, or the daemon's real
grants). The exact built argv IS the testable contract here, so these tests
pin it against the module's named allowlists. Marked ``privacy`` so the
security guard actually runs on CI (which only runs ``pytest -m privacy``); the
tests are Vision-free and build/inspect argv only — no real ``tccutil`` runs.
"""

from __future__ import annotations

import subprocess

import pytest

from screencap.daemon import tcc_cleanup

pytestmark = pytest.mark.privacy


# -- Happy path: the exact argv set ----------------------------------------


def test_build_cleanup_commands_exact_argv() -> None:
    # AE2: clear the orphan bare identity wholesale, then strip the legacy app's
    # one genuine stray row (Accessibility). Order and argv are pinned exactly.
    #
    # SCR-201: the app's *Screen Recording* row is deliberately NOT reset. On the
    # nested-LoginItem helper layout (SCR-196) macOS attributes the daemon's SR
    # request/capture to the responsible host app (``com.screencap.macos``), so
    # that row is the daemon's *real* SR identity, not a decoy — resetting it
    # deletes the very row registration just created (the SCR-201 symptom).
    assert tcc_cleanup.build_cleanup_commands() == [
        ["tccutil", "reset", "All", "screencap"],
        ["tccutil", "reset", "Accessibility", "com.screencap.macos"],
    ]


def test_cleanup_never_resets_app_screen_recording() -> None:
    # SCR-201 regression: the app's Screen Recording row must never be reset — it
    # is where the daemon's SR grant actually lives (attribution rolls up to the
    # host app for the nested LoginItem). Guarded two ways: it is absent from the
    # built set, and the builder itself now refuses it fail-closed.
    assert ["tccutil", "reset", "ScreenCapture", "com.screencap.macos"] not in (
        tcc_cleanup.build_cleanup_commands()
    )
    assert tcc_cleanup.SCREEN_CAPTURE_SERVICE not in tcc_cleanup.ALLOWED_SERVICES_FOR_APP
    with pytest.raises(tcc_cleanup._CleanupGuardError):
        tcc_cleanup._service_reset_command(
            tcc_cleanup.SCREEN_CAPTURE_SERVICE, "com.screencap.macos"
        )


# -- P1 guard: `All` only ever pairs with the orphan -----------------------


@pytest.mark.parametrize(
    "identity",
    ["com.screencap.macos", "com.screencap.daemon", "claude", ""],
)
def test_all_reset_refuses_non_orphan_identities(identity: str) -> None:
    # `tccutil reset All com.screencap.macos` would wipe the app's Microphone;
    # `reset All com.screencap.daemon` would wipe the daemon's real grants;
    # `reset All claude` would wipe an unrelated app. All must be refused.
    with pytest.raises(tcc_cleanup._CleanupGuardError):
        tcc_cleanup._all_reset_command(identity)


def test_all_reset_allowed_only_for_orphan() -> None:
    assert tcc_cleanup.ALLOWED_ALL_RESET_IDENTITIES == frozenset({"screencap"})
    assert tcc_cleanup._all_reset_command("screencap") == [
        "tccutil",
        "reset",
        "All",
        "screencap",
    ]


# -- Guard: per-service reset is app-only, service-allowlisted --------------


def test_service_reset_refuses_daemon_identity() -> None:
    # Never reset the daemon's own grants — the opposite of the goal.
    with pytest.raises(tcc_cleanup._CleanupGuardError):
        tcc_cleanup._service_reset_command("ScreenCapture", "com.screencap.daemon")


def test_service_reset_refuses_non_app_identity() -> None:
    with pytest.raises(tcc_cleanup._CleanupGuardError):
        tcc_cleanup._service_reset_command("ScreenCapture", "com.apple.Safari")


@pytest.mark.parametrize(
    "service", ["Microphone", "ListenEvent", "ScreenCapture", "All", ""]
)
def test_service_reset_refuses_services_outside_app_allowlist(service: str) -> None:
    # Microphone is the grant we must preserve; ListenEvent (Input Monitoring)
    # is out of scope; ScreenCapture is the app's *real* SR identity (SCR-201) and
    # must not be wiped; a literal "All" service must never slip through here.
    with pytest.raises(tcc_cleanup._CleanupGuardError):
        tcc_cleanup._service_reset_command(service, "com.screencap.macos")


# -- Whole-set invariants over the built commands --------------------------


def test_no_built_command_targets_the_daemon() -> None:
    flat = [token for argv in tcc_cleanup.build_cleanup_commands() for token in argv]
    assert "com.screencap.daemon" not in flat


def test_no_bare_service_reset_without_a_bundle_id() -> None:
    # A bare `tccutil reset <service>` (4-token argv would be the guarded form;
    # a 3-token `reset <service>` with no target) wipes every app. Every emitted
    # command must be exactly ["tccutil", "reset", <scope>, <target>].
    for argv in tcc_cleanup.build_cleanup_commands():
        assert len(argv) == 4
        assert argv[:2] == ["tccutil", "reset"]
        assert argv[3], "reset must always carry a non-empty target identity"


def test_all_only_ever_pairs_with_the_orphan() -> None:
    for argv in tcc_cleanup.build_cleanup_commands():
        scope, target = argv[2], argv[3]
        if scope == "All":
            assert target == "screencap"
        else:
            # A non-`All` scope is a service reset and must target the app only.
            assert target == "com.screencap.macos"


# -- run_decoy_cleanup: best-effort / fail-soft ----------------------------


def test_run_cleanup_tolerates_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    # "No such bundle identifier" (a decoy row that never existed) must not fail
    # the install. run_decoy_cleanup swallows non-zero exits.
    monkeypatch.setattr(tcc_cleanup.sys, "platform", "darwin")
    seen: list[list[str]] = []

    def _fake_run(argv, **_kwargs):  # type: ignore[no-untyped-def]
        seen.append(argv)
        return subprocess.CompletedProcess(argv, returncode=44, stderr="No such bundle identifier")

    monkeypatch.setattr(tcc_cleanup.subprocess, "run", _fake_run)
    tcc_cleanup.run_decoy_cleanup()  # must not raise

    assert seen == tcc_cleanup.build_cleanup_commands()


def test_run_cleanup_tolerates_spawn_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tcc_cleanup.sys, "platform", "darwin")

    def _boom(argv, **_kwargs):  # type: ignore[no-untyped-def]
        raise OSError("tccutil not found")

    monkeypatch.setattr(tcc_cleanup.subprocess, "run", _boom)
    tcc_cleanup.run_decoy_cleanup()  # must not raise


def test_run_cleanup_is_noop_off_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tcc_cleanup.sys, "platform", "linux")

    def _should_not_run(argv, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("tccutil must not run off darwin")

    monkeypatch.setattr(tcc_cleanup.subprocess, "run", _should_not_run)
    tcc_cleanup.run_decoy_cleanup()
