"""U4 — mount orchestration + daemon integration (SCR-236).

``ensure_store_mounted`` is the one mount owner: it reuses a healthy mount,
attaches a LOCKED bundle with the Keychain key, and hard-stops on user-LOCKED,
ROGUE, KEY_MISSING (AE2), and ABSENT states with the right exit codes. The
daemon mounts before binding its socket and never unmounts on exit (KTD-4).

Privacy-marked + Vision-free so the CI privacy lane runs it.
"""

from __future__ import annotations

import inspect

import pytest

from screencap import config, container
from screencap.daemon import server

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# ensure_store_mounted state machine (via _ensure_mounted_locked)
# ---------------------------------------------------------------------------


@pytest.fixture
def sm(monkeypatch, tmp_path):
    """Wire up the state-machine primitives with recording spies."""
    calls: dict[str, list] = {
        "attach": [], "create_bundle": [], "create_key": [], "harden": [], "marker": [],
    }

    monkeypatch.setattr(container, "is_user_locked", lambda: False)
    monkeypatch.setattr(container, "container_mountpoint", lambda b: None)
    monkeypatch.setattr(container, "harden_mount", lambda mp: calls["harden"].append(mp))
    monkeypatch.setattr(container, "_write_mount_marker", lambda mp: calls["marker"].append(mp))
    monkeypatch.setattr(container, "get_container_key", lambda: b"KEY")

    def _attach(b, key, mp):
        calls["attach"].append((b, key, str(mp)))
        return str(mp)

    def _create_bundle(b, key, **kw):
        calls["create_bundle"].append((b, key))

    def _create_key(b):
        calls["create_key"].append(b)
        return b"NEWKEY"

    monkeypatch.setattr(container, "attach_container", _attach)
    monkeypatch.setattr(container, "create_container", _create_bundle)
    monkeypatch.setattr(container, "create_container_key", _create_key)
    return calls


def _run(bundle, mountpoint, *, allow_create=False):
    return container._ensure_mounted_locked(bundle, mountpoint, allow_create=allow_create)


def test_locked_bundle_attaches(sm, tmp_path):
    bundle = tmp_path / "store.sparsebundle"
    bundle.mkdir()
    mp = tmp_path / "recordings"
    assert _run(bundle, mp) == str(mp)
    assert sm["attach"] == [(bundle, b"KEY", str(mp))]


def test_key_missing_raises_and_leaves_bundle_untouched(sm, monkeypatch, tmp_path):
    """AE2: bundle present + key gone → hard stop, nothing created/destroyed."""
    bundle = tmp_path / "store.sparsebundle"
    bundle.mkdir()
    monkeypatch.setattr(container, "get_container_key", lambda: None)
    with pytest.raises(container.ContainerKeyMissingError) as ei:
        _run(bundle, tmp_path / "recordings")
    assert ei.value.exit_code == 1
    assert sm["create_bundle"] == [] and sm["create_key"] == []


def test_user_locked_refuses(sm, monkeypatch, tmp_path):
    monkeypatch.setattr(container, "is_user_locked", lambda: True)
    with pytest.raises(container.StoreLockedError):
        _run(tmp_path / "store.sparsebundle", tmp_path / "recordings")


def test_rogue_mountpoint_refuses(sm, tmp_path):
    mp = tmp_path / "recordings"
    mp.mkdir()
    (mp / "old_recording").mkdir()  # non-dot content, not a mounted volume
    with pytest.raises(container.RogueMountpointError):
        _run(tmp_path / "store.sparsebundle", mp)
    assert sm["attach"] == [] and sm["create_bundle"] == []


def test_rogue_check_ignores_dot_entries(sm, tmp_path):
    """`.store`/`.fseventsd` are ours — not rogue content."""
    mp = tmp_path / "recordings"
    mp.mkdir()
    (mp / ".store").mkdir()
    with pytest.raises(container.StoreNotInitializedError):  # ABSENT, not ROGUE
        _run(tmp_path / "store.sparsebundle", mp, allow_create=False)


def test_absent_non_interactive_names_store_init(sm, tmp_path):
    with pytest.raises(container.StoreNotInitializedError, match="store init"):
        _run(tmp_path / "store.sparsebundle", tmp_path / "recordings", allow_create=False)


def test_absent_foreground_creates_key_bundle_then_attaches(sm, monkeypatch, tmp_path):
    bundle = tmp_path / "store.sparsebundle"  # absent
    mp = tmp_path / "recordings"
    monkeypatch.setattr(container, "get_container_key", lambda: None)  # no key yet
    assert _run(bundle, mp, allow_create=True) == str(mp)
    assert sm["create_key"] == [bundle]  # durable key first
    assert sm["create_bundle"] == [(bundle, b"NEWKEY")]
    assert sm["attach"] == [(bundle, b"NEWKEY", str(mp))]


def test_absent_foreground_reuses_existing_key(sm, tmp_path):
    """Bundle absent but key present (crash after key-write / wiped bundle):
    reuse the key, don't dead-stop or mint a second."""
    bundle = tmp_path / "store.sparsebundle"  # absent
    mp = tmp_path / "recordings"
    # sm fixture's get_container_key returns b"KEY"
    _run(bundle, mp, allow_create=True)
    assert sm["create_key"] == []  # reused, not re-created
    assert sm["create_bundle"] == [(bundle, b"KEY")]


def test_existing_mount_is_reused_not_reattached(sm, monkeypatch, tmp_path):
    monkeypatch.setattr(container, "container_mountpoint", lambda b: "/Volumes/ScreenCapStore")
    assert _run(tmp_path / "store.sparsebundle", tmp_path / "recordings") == "/Volumes/ScreenCapStore"
    assert sm["attach"] == []  # winner's mount reused, never double-attached
    # Reuse refreshes the identity marker cheaply; no expensive mdutil re-harden.
    assert sm["marker"] == ["/Volumes/ScreenCapStore"]
    assert sm["harden"] == []


def test_rogue_populated_host_store_refuses(sm, tmp_path):
    """A populated host-side .store/ (plaintext sidecar leak) is ROGUE, even
    though it is a dot-entry — mounting over it would shadow plaintext PII."""
    mp = tmp_path / "recordings"
    mp.mkdir()
    host_store = mp / ".store"
    host_store.mkdir()
    (host_store / "content_index.db").write_text("plaintext OCR")  # leaked to host
    with pytest.raises(container.RogueMountpointError, match="plaintext sidecar"):
        _run(tmp_path / "store.sparsebundle", mp, allow_create=False)
    assert sm["attach"] == []


def test_attached_but_not_mounted_is_retryable(sm, monkeypatch, tmp_path):
    monkeypatch.setattr(container, "container_mountpoint", lambda b: "")
    with pytest.raises(container.ContainerBusyError):
        _run(tmp_path / "store.sparsebundle", tmp_path / "recordings")


# ---------------------------------------------------------------------------
# ensure_store_mounted is flock-serialized (KTD-3)
# ---------------------------------------------------------------------------


def test_ensure_store_mounted_holds_mount_lock(monkeypatch, tmp_path):
    order = []
    monkeypatch.setattr(config, "get_data_root", lambda: tmp_path / "recordings")
    monkeypatch.setattr(container, "default_bundle_path", lambda: tmp_path / "store.sparsebundle")
    monkeypatch.setattr(container, "open_hardened_lock", lambda p: (order.append(("lock", str(p))) or __import__("os").open("/dev/null", 0)))
    monkeypatch.setattr(container.fcntl, "flock", lambda fd, op: order.append("flock"))
    monkeypatch.setattr(container, "_ensure_mounted_locked", lambda b, m, allow_create: order.append("body") or "/mp")
    assert container.ensure_store_mounted() == "/mp"
    # lock opened + flock'd BEFORE the state machine body runs
    assert [o if isinstance(o, str) else o[0] for o in order] == ["lock", "flock", "body"]


# ---------------------------------------------------------------------------
# _mount_store_if_enabled exit-code mapping (KTD-10)
# ---------------------------------------------------------------------------


def test_mount_gate_noop_when_inactive(monkeypatch):
    monkeypatch.setattr(config, "container_active", lambda: False)
    called = []
    monkeypatch.setattr(container, "ensure_store_mounted", lambda **k: called.append(1))
    assert server._mount_store_if_enabled() is None
    assert called == []  # flag off → zero container calls


@pytest.mark.parametrize(
    "exc,expected",
    [
        (container.ContainerKeyMissingError("gone"), 1),
        (container.ContainerKeyLockedError("locked"), 75),
        (container.RogueMountpointError("rogue"), 1),
        (container.ContainerBusyError("busy"), 75),
    ],
)
def test_mount_gate_maps_exit_codes(monkeypatch, exc, expected):
    monkeypatch.setattr(config, "container_active", lambda: True)

    def _raise(**k):
        raise exc

    monkeypatch.setattr(container, "ensure_store_mounted", _raise)
    assert server._mount_store_if_enabled() == expected


def test_mount_gate_success_returns_none(monkeypatch):
    monkeypatch.setattr(config, "container_active", lambda: True)
    monkeypatch.setattr(container, "ensure_store_mounted", lambda **k: "/mp")
    assert server._mount_store_if_enabled() is None


# ---------------------------------------------------------------------------
# Daemon mounts BEFORE binding, and a mount failure blocks the bind
# ---------------------------------------------------------------------------


def test_serve_mounts_before_bind_and_failure_blocks_bind(monkeypatch, tmp_path):
    from screencap.daemon import socket as dsock

    bound = []
    monkeypatch.setattr(server, "_mount_store_if_enabled", lambda: 75)  # retryable mount failure
    monkeypatch.setattr(dsock, "bind_unix_socket", lambda p: bound.append(p))
    rc = server.serve(socket_path=str(tmp_path / "api.sock"))
    assert rc == 75  # returned the mount exit code
    assert bound == []  # never bound the socket


def test_serve_never_unmounts_on_exit(monkeypatch):
    """KTD-4: the mount outlives the daemon — no detach in the serve path."""
    src = inspect.getsource(server.serve) + inspect.getsource(server._mount_store_if_enabled)
    assert "detach" not in src


# ---------------------------------------------------------------------------
# KTD-13: the disk guard measures the HOST volume backing the bundle
# ---------------------------------------------------------------------------


def test_disk_guard_uses_host_path_when_container_active(monkeypatch, tmp_path):
    from screencap.engine.disk_policy import MonitorAndStop

    monkeypatch.setattr(config, "container_active", lambda: True)
    monkeypatch.setattr(container, "default_bundle_path", lambda: tmp_path / "store.sparsebundle")
    policy = MonitorAndStop()
    policy.bind(tmp_path / "recordings" / "rec1")  # inside the (virtual) volume
    assert policy._free_space_check_path() == tmp_path  # the bundle's parent = host


def test_disk_guard_uses_capture_dir_when_inactive(monkeypatch, tmp_path):
    from screencap.engine.disk_policy import MonitorAndStop

    monkeypatch.setattr(config, "container_active", lambda: False)
    capture = tmp_path / "recordings"
    capture.mkdir()
    policy = MonitorAndStop()
    policy.bind(capture)
    assert policy._free_space_check_path() == capture


# ---------------------------------------------------------------------------
# serve --install creates the store in the foreground before installing
# ---------------------------------------------------------------------------


def test_init_container_foreground_creates_then_noop(monkeypatch):
    from screencap.cli import _init_container_store_foreground

    monkeypatch.setattr(config, "container_active", lambda: True)
    calls = []
    monkeypatch.setattr(container, "ensure_store_mounted", lambda **k: calls.append(k))
    _init_container_store_foreground()
    assert calls == [{"allow_create": True}]  # foreground create context


def test_init_container_foreground_noop_when_inactive(monkeypatch):
    from screencap.cli import _init_container_store_foreground

    monkeypatch.setattr(config, "container_active", lambda: False)
    calls = []
    monkeypatch.setattr(container, "ensure_store_mounted", lambda **k: calls.append(k))
    _init_container_store_foreground()
    assert calls == []


def test_init_container_foreground_exits_on_error(monkeypatch):
    from screencap.cli import _init_container_store_foreground

    monkeypatch.setattr(config, "container_active", lambda: True)

    def _raise(**k):
        raise container.StoreNotInitializedError("no store")

    monkeypatch.setattr(container, "ensure_store_mounted", _raise)
    with pytest.raises(SystemExit) as ei:
        _init_container_store_foreground()
    assert ei.value.code == 1
