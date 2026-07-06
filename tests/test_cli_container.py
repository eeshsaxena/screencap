"""U5 — CLI funnel guard + `store` command group (SCR-236).

The daemon-independent CLI commands resolve the recordings dir through the
container mount (``get_recordings_dir`` funnel), and a typed store failure
renders as a clear CLI error rather than a stack trace or a silent plaintext
fallback. The ``store`` group owns init / lock / unlock / compact.

Privacy-marked + Vision-free so the CI privacy lane runs it.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

import screencap.cli as clim
from screencap import config, container
from screencap.cli import cli

pytestmark = pytest.mark.privacy


class _FakeProc:
    def __init__(self, returncode, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# get_recordings_dir funnel (KTD-3)
# ---------------------------------------------------------------------------


def test_funnel_mounts_when_active_and_unmounted(monkeypatch, tmp_path):
    monkeypatch.delenv("SCREENCAP_RECORDINGS_DIR", raising=False)
    monkeypatch.setattr(config, "container_active", lambda: True)
    monkeypatch.setattr(config, "get_data_root", lambda: tmp_path / "recordings")
    called = []
    monkeypatch.setattr(container, "ensure_store_mounted", lambda **k: called.append(1))
    config.get_recordings_dir()
    assert called == [1]  # not a mount → mounts on demand


def test_funnel_fast_path_when_our_store_mounted(monkeypatch, tmp_path):
    monkeypatch.delenv("SCREENCAP_RECORDINGS_DIR", raising=False)
    monkeypatch.setattr(config, "container_active", lambda: True)
    monkeypatch.setattr(config, "get_data_root", lambda: tmp_path / "recordings")
    monkeypatch.setattr(container, "mount_is_ours", lambda r: True)  # identity verified
    called = []
    monkeypatch.setattr(container, "ensure_store_mounted", lambda **k: called.append(1))
    config.get_recordings_dir()
    assert called == []  # our store already mounted → no hdiutil reshell


def test_funnel_no_container_calls_when_inactive(monkeypatch, tmp_path):
    monkeypatch.delenv("SCREENCAP_RECORDINGS_DIR", raising=False)
    monkeypatch.setattr(config, "container_active", lambda: False)
    monkeypatch.setattr(config, "_load_toml", lambda: {"recordings_dir": str(tmp_path / "rec")})
    called = []
    monkeypatch.setattr(container, "ensure_store_mounted", lambda **k: called.append(1))
    assert config.get_recordings_dir() == tmp_path / "rec"
    assert called == []  # flag off → zero container calls (regression)


# ---------------------------------------------------------------------------
# The CLI group renders a typed store failure (AE2) instead of a stack trace
# ---------------------------------------------------------------------------


def test_group_renders_container_error_and_exits():
    grp = clim._ScreencapGroup(name="t")

    @grp.command("boom")
    def _boom():
        raise container.ContainerKeyMissingError("its Keychain key is gone (see SECURITY.md)")

    result = CliRunner().invoke(grp, ["boom"])
    assert result.exit_code == 1  # operator-fatal
    assert "store unavailable" in result.output.lower()
    assert "keychain key is gone" in result.output.lower()


def test_group_reraises_non_container_errors():
    grp = clim._ScreencapGroup(name="t")

    @grp.command("boom")
    def _boom():
        raise ValueError("unrelated")

    result = CliRunner().invoke(grp, ["boom"])
    assert isinstance(result.exception, ValueError)  # not swallowed


# ---------------------------------------------------------------------------
# store lock / unlock — the user-locked sentinel (KTD-4)
# ---------------------------------------------------------------------------


def test_store_lock_sets_sentinel_then_unlock_clears(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "_run_dir", lambda: tmp_path / "run")
    monkeypatch.setattr(config, "container_active", lambda: True)
    monkeypatch.setattr(clim, "_recording_active", lambda: False)
    monkeypatch.setattr(container, "container_mountpoint", lambda b: None)  # not mounted

    r1 = CliRunner().invoke(cli, ["store", "lock"])
    assert r1.exit_code == 0
    assert container.is_user_locked()

    # A cron-style auto-spawn refuses to remount a user-locked store.
    monkeypatch.setattr(config, "get_data_root", lambda: tmp_path / "recordings")
    monkeypatch.setattr(container, "default_bundle_path", lambda: tmp_path / "store.sparsebundle")
    with pytest.raises(container.StoreLockedError):
        container.ensure_store_mounted()

    r2 = CliRunner().invoke(cli, ["store", "unlock"])
    assert r2.exit_code == 0
    assert not container.is_user_locked()


def test_store_lock_refuses_while_recording(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "_run_dir", lambda: tmp_path / "run")
    monkeypatch.setattr(config, "container_active", lambda: True)
    monkeypatch.setattr(clim, "_recording_active", lambda: True)
    r = CliRunner().invoke(cli, ["store", "lock"])
    assert r.exit_code == 1
    assert not container.is_user_locked()  # never locked mid-recording


def test_recording_active_uses_disk_probe_when_daemon_down(monkeypatch):
    """A daemon-independent recording (disk pidfile lock held) blocks
    lock/compact even when the daemon is unreachable."""
    import screencap.catalog as catmod

    monkeypatch.setattr(catmod, "_active_recording_name", lambda: "rec-123")
    assert clim._recording_active() is True  # short-circuits before the daemon check


def test_store_lock_uses_graceful_only_detach(monkeypatch, tmp_path):
    """`store lock` never force-detaches past a live direct reader (KTD-4)."""
    monkeypatch.setattr(container, "_run_dir", lambda: tmp_path / "run")
    monkeypatch.setattr(config, "container_active", lambda: True)
    monkeypatch.setattr(clim, "_recording_active", lambda: False)
    monkeypatch.setattr(container, "container_mountpoint", lambda b: "/Volumes/ScreenCapStore")
    detach_kwargs = []
    monkeypatch.setattr(container, "detach_container", lambda mp, **kw: detach_kwargs.append(kw))
    r = CliRunner().invoke(cli, ["store", "lock"])
    assert r.exit_code == 0
    assert container.is_user_locked()
    assert detach_kwargs == [{"force": False}]  # graceful only


# ---------------------------------------------------------------------------
# store init — create then no-op
# ---------------------------------------------------------------------------


def test_store_init_creates_then_noop(monkeypatch, tmp_path):
    bundle = tmp_path / "store.sparsebundle"
    monkeypatch.setattr(config, "container_active", lambda: True)
    monkeypatch.setattr(container, "default_bundle_path", lambda: bundle)

    def fake_ensure(**k):
        assert k == {"allow_create": True}
        bundle.mkdir(exist_ok=True)

    monkeypatch.setattr(container, "ensure_store_mounted", fake_ensure)

    r1 = CliRunner().invoke(cli, ["store", "init"])
    assert r1.exit_code == 0 and "created" in r1.output.lower()

    r2 = CliRunner().invoke(cli, ["store", "init"])
    assert r2.exit_code == 0 and "already" in r2.output.lower()


def test_store_init_noop_when_inactive(monkeypatch):
    monkeypatch.setattr(config, "container_active", lambda: False)
    called = []
    monkeypatch.setattr(container, "ensure_store_mounted", lambda **k: called.append(1))
    r = CliRunner().invoke(cli, ["store", "init"])
    assert r.exit_code == 0 and called == []


# ---------------------------------------------------------------------------
# store compact — quiescent-only, never force-detach (KTD-12)
# ---------------------------------------------------------------------------


def test_store_compact_refuses_while_recording(monkeypatch):
    monkeypatch.setattr(config, "container_active", lambda: True)
    monkeypatch.setattr(clim, "_recording_active", lambda: True)
    ran = []
    monkeypatch.setattr(container, "compact_store", lambda: ran.append(1))
    r = CliRunner().invoke(cli, ["store", "compact"])
    assert r.exit_code == 1 and ran == []
    assert "recording is active" in r.output.lower()


def test_store_compact_busy_is_not_now(monkeypatch):
    monkeypatch.setattr(config, "container_active", lambda: True)
    monkeypatch.setattr(clim, "_recording_active", lambda: False)

    def busy():
        raise container.ContainerBusyError("in use")

    monkeypatch.setattr(container, "compact_store", busy)
    r = CliRunner().invoke(cli, ["store", "compact"])
    assert r.exit_code == 75  # retryable, not a hard failure


def test_store_compact_success(monkeypatch):
    monkeypatch.setattr(config, "container_active", lambda: True)
    monkeypatch.setattr(clim, "_recording_active", lambda: False)
    monkeypatch.setattr(container, "compact_store", lambda: 5_000_000)
    r = CliRunner().invoke(cli, ["store", "compact"])
    assert r.exit_code == 0 and "reclaimed" in r.output.lower()


def test_compact_store_never_force_detaches(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "_run_dir", lambda: tmp_path / "run")
    monkeypatch.setattr(config, "get_data_root", lambda: tmp_path / "recordings")
    monkeypatch.setattr(container, "default_bundle_path", lambda: tmp_path / "store.sparsebundle")
    monkeypatch.setattr(container, "get_container_key", lambda: b"KEY")
    monkeypatch.setattr(container, "container_mountpoint", lambda b: "/Volumes/ScreenCapStore")
    calls = []

    def fake_run(args, *, key=None, timeout):
        calls.append(args)
        return _FakeProc(1, b"", b"resource busy")  # graceful detach fails

    monkeypatch.setattr(container, "_run_hdiutil", fake_run)
    with pytest.raises(container.ContainerBusyError):
        container.compact_store()
    assert calls == [["detach", "/Volumes/ScreenCapStore"]]  # graceful only
    assert not any("-force" in a for a in calls)  # NEVER escalates to force
