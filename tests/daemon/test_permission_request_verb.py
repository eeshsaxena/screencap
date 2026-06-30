"""U8: on-demand daemon-driven ``permission.request`` registration verb.

When the user clicks **Grant**, the app calls this verb so the daemon registers
*itself* (its TCC identity) for the named permission, becoming a toggleable
Settings entry (R5/R6). The verb is per-permission (not screen-recording-only),
validates the permission against the canonical allowlist (typed 4xx otherwise),
runs the registration mechanism off the event loop, and is audited on every exit
path like the other mutating verbs.

The registration mechanism itself (``permission_register.register_permission``)
is stubbed in the route tests so the real ``CGRequestScreenCaptureAccess`` /
``AXIsProcessTrustedWithOptions`` / event-tap calls never fire a TCC prompt
during the suite. Per-permission dispatch is asserted directly against
``permission_register`` with ``DarwinPlatform`` stubbed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from screencap.daemon import (
    audit_log,
    errors,
    permission_probe,
    permission_register,
    provenance,
)
from screencap.daemon.app import build_app


@pytest.fixture
def audit_log_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the audit log to a tmp path so tests don't touch ~/.screencap."""
    target = tmp_path / "audit.log"
    monkeypatch.setattr(audit_log, "_audit_log_path", lambda: target)
    return target


def _stub_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        provenance,
        "derive_peer_descriptor_from_asgi_scope",
        lambda _scope: provenance.PeerDescriptor(
            pid=4242,
            path="/Applications/ScreenCap.app/Contents/MacOS/screencap",
            classification=provenance.STARTED_BY_SWIFTUI,
        ),
    )


def _stub_register(
    monkeypatch: pytest.MonkeyPatch, *, returns: bool = False
) -> list[str]:
    """Replace the registration mechanism with a recorder (no real TCC calls)."""
    calls: list[str] = []

    def _fake(permission: str) -> bool:
        calls.append(permission)
        return returns

    monkeypatch.setattr(permission_register, "register_permission", _fake)
    return calls


async def _post_permission(app, body: dict[str, Any]):
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post("/v0/permission.request", json=body)


# -- Route behavior ---------------------------------------------------------


@pytest.mark.asyncio
async def test_permission_request_runs_mechanism_and_returns_ack(
    audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AE2 (daemon half): Grant invokes the daemon verb for that permission and
    # acks; the app opens the pane on the ack (asserted Swift-side).
    _stub_peer(monkeypatch)
    calls = _stub_register(monkeypatch, returns=True)
    app = build_app()

    response = await _post_permission(app, {"permission": "screen_recording"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["permission"] == "screen_recording"
    assert body["already_granted"] is True
    assert calls == ["screen_recording"]

    record = json.loads(audit_log_at.read_text(encoding="utf-8"))
    assert record["verb"] == "permission.request"
    assert record["outcome"] == "ok"
    assert record["permission"] == "screen_recording"
    assert record["peer_pid"] == 4242
    assert record["classification"] == provenance.STARTED_BY_SWIFTUI


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "permission",
    ["screen_recording", "accessibility", "input_monitoring"],
)
async def test_each_permission_routes_to_its_own_mechanism(
    audit_log_at: Path, monkeypatch: pytest.MonkeyPatch, permission: str
) -> None:
    # R6: each of the three permissions is requestable — not screen-recording
    # only. The verb forwards exactly the requested permission to the registrar.
    _stub_peer(monkeypatch)
    calls = _stub_register(monkeypatch)
    app = build_app()

    response = await _post_permission(app, {"permission": permission})

    assert response.status_code == 200, response.text
    assert response.json()["permission"] == permission
    assert calls == [permission]


@pytest.mark.asyncio
async def test_out_of_allowlist_permission_returns_typed_4xx_and_skips_dispatch(
    audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An unexpected value (microphone is excluded; anything else) must return a
    # typed invalid_permission 4xx and never reach the registration dispatch.
    _stub_peer(monkeypatch)
    calls = _stub_register(monkeypatch)
    app = build_app()

    response = await _post_permission(app, {"permission": "microphone"})

    assert response.status_code == 400
    body = response.json()
    assert body["ok"] is False
    assert body["error"] == errors.INVALID_PERMISSION
    assert calls == []  # dispatch never ran

    record = json.loads(audit_log_at.read_text(encoding="utf-8"))
    assert record["outcome"] == errors.INVALID_PERMISSION
    # The rejected value is still captured for forensics.
    assert record["permission"] == "microphone"


@pytest.mark.asyncio
async def test_unhandled_mechanism_error_audited_as_internal_error(
    audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_peer(monkeypatch)

    def _boom(_permission: str) -> bool:
        raise RuntimeError("quartz exploded")

    monkeypatch.setattr(permission_register, "register_permission", _boom)
    app = build_app()

    response = await _post_permission(app, {"permission": "accessibility"})

    assert response.status_code == 500
    record = json.loads(audit_log_at.read_text(encoding="utf-8"))
    assert record["verb"] == "permission.request"
    assert record["outcome"] == errors.ERROR_CODE_INTERNAL
    assert record["permission"] == "accessibility"


@pytest.mark.asyncio
async def test_register_runs_off_the_event_loop(
    audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The blocking request/event-tap calls must run via asyncio.to_thread, not
    on the event loop — assert the registrar executes on a worker thread."""
    import threading

    _stub_peer(monkeypatch)
    main_thread = threading.get_ident()
    observed: dict[str, int] = {}

    def _fake(permission: str) -> bool:
        observed["thread"] = threading.get_ident()
        return False

    monkeypatch.setattr(permission_register, "register_permission", _fake)
    app = build_app()

    response = await _post_permission(app, {"permission": "input_monitoring"})

    assert response.status_code == 200, response.text
    assert observed["thread"] != main_thread


# -- permission_register dispatch (DarwinPlatform stubbed) ------------------


def test_register_permission_dispatches_per_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each canonical permission calls its matching DarwinPlatform mechanism —
    Screen Recording via the request API, Accessibility via the trusted-prompt,
    Input Monitoring via the real event-tap registration helper (U7 Decision A).
    """
    from screencap.engine.platform import darwin

    called: list[str] = []
    monkeypatch.setattr(
        darwin.DarwinPlatform,
        "request_screen_recording_access",
        staticmethod(lambda: called.append("screen_recording") or True),
    )
    monkeypatch.setattr(
        darwin.DarwinPlatform,
        "request_accessibility_access",
        staticmethod(lambda: called.append("accessibility") or True),
    )
    monkeypatch.setattr(
        darwin.DarwinPlatform,
        "register_input_monitoring_access",
        staticmethod(lambda: called.append("input_monitoring") or False),
    )

    assert permission_register.register_permission("screen_recording") is True
    assert permission_register.register_permission("accessibility") is True
    assert permission_register.register_permission("input_monitoring") is False
    assert called == ["screen_recording", "accessibility", "input_monitoring"]


def test_register_permission_unknown_raises_value_error() -> None:
    with pytest.raises(ValueError):
        permission_register.register_permission("microphone")


def test_register_permission_fails_soft_when_mechanism_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mechanism raise degrades to False (couldn't confirm) — never
    propagates. Registration is best-effort; the app re-probes for real state."""
    from screencap.engine.platform import darwin

    def _raise() -> bool:
        raise RuntimeError("pyobjc hiccup")

    monkeypatch.setattr(
        darwin.DarwinPlatform, "request_screen_recording_access", staticmethod(_raise)
    )
    assert permission_register.register_permission("screen_recording") is False


def test_register_permission_allowlist_matches_probe_keys() -> None:
    # The verb's allowlist is the probe's canonical key set — keep them coupled
    # so detection and registration never drift on permission identity.
    assert set(permission_probe.PERMISSION_KEYS) == {
        "screen_recording",
        "accessibility",
        "input_monitoring",
    }


# -- permission.cleanup_decoys verb (SCR-200 U4) ----------------------------


@pytest.mark.asyncio
@pytest.mark.privacy
async def test_cleanup_decoys_runs_sweep_and_acks(
    audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The verb runs the identity-scoped sweep daemon-side and acks. The actual
    # tccutil argv is asserted in test_tcc_cleanup; here we assert the verb wires
    # run_decoy_cleanup and audits the outcome.
    from screencap.daemon import tcc_cleanup

    _stub_peer(monkeypatch)
    calls: list[bool] = []
    monkeypatch.setattr(tcc_cleanup, "run_decoy_cleanup", lambda: calls.append(True))
    app = build_app()

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/v0/permission.cleanup_decoys")

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert calls == [True]

    record = json.loads(audit_log_at.read_text(encoding="utf-8"))
    assert record["verb"] == "permission.cleanup_decoys"
    assert record["outcome"] == "ok"


@pytest.mark.asyncio
@pytest.mark.privacy
async def test_cleanup_decoys_unhandled_error_audited_as_internal(
    audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from screencap.daemon import tcc_cleanup

    _stub_peer(monkeypatch)

    def _boom() -> None:
        raise RuntimeError("tccutil exploded unexpectedly")

    monkeypatch.setattr(tcc_cleanup, "run_decoy_cleanup", _boom)
    app = build_app()

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/v0/permission.cleanup_decoys")

    assert response.status_code == 500
    record = json.loads(audit_log_at.read_text(encoding="utf-8"))
    assert record["verb"] == "permission.cleanup_decoys"
    assert record["outcome"] == errors.ERROR_CODE_INTERNAL
