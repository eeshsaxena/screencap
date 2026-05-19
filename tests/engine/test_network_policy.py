"""Tests for the ``NetworkPolicy`` seam (SCR-43, slice 6 of SCR-31).

Promotes the V1.5 mitm/KEK/DEK preflight from inline code in
``_run_screen_recorder`` to a pluggable policy.

* ``MitmProxyV15`` — lock acquisition, ``preflight_or_raise``, KEK access,
  DEK generate + wrap, KEK plaintext drop, empty-allowlist warning.
* ``Null``          — used when ``--network`` is not passed; all hooks safe no-ops.

These tests pin the seam-level behavioural contract. The wiring into
``_run_screen_recorder`` is exercised by the Cycle 9 test here and the
parity tests in ``test_screen_recorder_parity``.
"""

from __future__ import annotations

from unittest import mock


# ---------------------------------------------------------------------------
# Cycle 1 — smoke test
# ---------------------------------------------------------------------------


def test_network_policy_module_exposes_protocol_and_impls():
    """Smoke: the module exists with the four documented names.

    Both ``MitmProxyV15`` and ``Null`` must be constructible with no args.
    ``NetworkMaterial`` must be constructible with no args and report inactive.
    """
    from screencap.engine.network_policy import (  # noqa: F401
        MitmProxyV15,
        NetworkMaterial,
        NetworkPolicy,
        Null,
    )

    MitmProxyV15()
    Null()
    mat = NetworkMaterial()
    assert not mat.active


# ---------------------------------------------------------------------------
# Cycle 2 — Null lifecycle is safe
# ---------------------------------------------------------------------------


def test_null_setup_returns_inactive_material(tmp_path):
    """``Null.setup()`` returns an inactive ``NetworkMaterial`` without
    calling any network functions."""
    from screencap.engine.network_policy import Null

    policy = Null()
    with (
        mock.patch("screencap.network.lifecycle.acquire_network_lock") as lock,
        mock.patch("screencap.network.lifecycle.preflight_or_raise") as preflight,
    ):
        material = policy.setup(tmp_path, privacy_config=None)

    assert not material.active
    assert material.proxy_port is None
    assert material.dek is None
    lock.assert_not_called()
    preflight.assert_not_called()


def test_null_teardown_is_safe():
    """``Null.teardown()`` does not raise."""
    from screencap.engine.network_policy import Null

    Null().teardown()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_DEK = b"d" * 32
_FAKE_DEK_WRAPPED = b"w" * 48
_FAKE_DEK_NONCE = b"n" * 12
_FAKE_KEK = b"k" * 32
_FAKE_PORT = 8080


def _network_mocks(*, allowlist=frozenset(["*.example.com"])):
    """Return a list of mock patches covering all MitmProxyV15 dependencies."""
    return [
        mock.patch("screencap.config.get_network_config", return_value=mock.MagicMock()),
        mock.patch("screencap.network.lifecycle.acquire_network_lock", return_value=mock.MagicMock()),
        mock.patch("screencap.network.lifecycle.preflight_or_raise", return_value=_FAKE_PORT),
        mock.patch("screencap.network.crypto.get_or_create_kek", return_value=_FAKE_KEK),
        mock.patch("screencap.network.crypto.generate_dek", return_value=_FAKE_DEK),
        mock.patch("screencap.network.crypto.wrap_dek", return_value=(_FAKE_DEK_WRAPPED, _FAKE_DEK_NONCE)),
        mock.patch("screencap.network.blocklist.effective_capture_bodies_for", return_value=allowlist),
    ]


def _start_mocks(patches, overrides: dict | None = None):
    """Start a list of patches, optionally replacing by index."""
    if overrides:
        for idx, patch in overrides.items():
            patches[idx] = patch
    for p in patches:
        p.start()
    return patches


def _stop_mocks(patches):
    for p in patches:
        p.stop()


# ---------------------------------------------------------------------------
# Cycle 3 — MitmProxyV15.setup(): success path
# ---------------------------------------------------------------------------


def test_mitm_proxy_v15_setup_returns_active_material(tmp_path):
    """``MitmProxyV15.setup()`` returns a populated, active ``NetworkMaterial``
    on the success path."""
    from screencap.engine.network_policy import MitmProxyV15

    policy = MitmProxyV15()
    patches = _network_mocks()
    _start_mocks(patches)
    try:
        material = policy.setup(tmp_path, privacy_config=None)
    finally:
        _stop_mocks(patches)

    assert material.active
    assert material.proxy_port == _FAKE_PORT
    assert material.dek == _FAKE_DEK
    assert material.dek_wrapped == _FAKE_DEK_WRAPPED
    assert material.dek_nonce == _FAKE_DEK_NONCE
    assert material.network_config is not None


def test_mitm_proxy_v15_setup_holds_lock_on_success(tmp_path):
    """``MitmProxyV15.setup()`` does NOT release the lock when setup succeeds —
    the lock must be held for the recording lifetime and released by teardown."""
    from screencap.engine.network_policy import MitmProxyV15

    fake_lock = mock.MagicMock()
    policy = MitmProxyV15()
    patches = _network_mocks()
    _start_mocks(patches, overrides={1: mock.patch(
        "screencap.network.lifecycle.acquire_network_lock", return_value=fake_lock
    )})
    try:
        policy.setup(tmp_path, privacy_config=None)
    finally:
        _stop_mocks(patches)

    fake_lock.release.assert_not_called()


# ---------------------------------------------------------------------------
# Cycle 4 — MitmProxyV15.setup(): lock released on failure
# ---------------------------------------------------------------------------


def test_mitm_proxy_v15_setup_releases_lock_on_preflight_failure(tmp_path):
    """If ``preflight_or_raise`` raises, the lock is released before the
    exception propagates as ``NetworkPreflightFailed``."""
    import pytest

    from screencap.engine.network_policy import MitmProxyV15
    from screencap.engine.screen_recorder import NetworkPreflightFailed

    fake_lock = mock.MagicMock()

    with (
        mock.patch("screencap.config.get_network_config", return_value=mock.MagicMock()),
        mock.patch("screencap.network.lifecycle.acquire_network_lock", return_value=fake_lock),
        mock.patch("screencap.network.lifecycle.preflight_or_raise", side_effect=RuntimeError("port busy")),
    ):
        with pytest.raises(NetworkPreflightFailed):
            MitmProxyV15().setup(tmp_path, privacy_config=None)

    fake_lock.release.assert_called_once()


def test_mitm_proxy_v15_setup_releases_lock_on_kek_failure(tmp_path):
    """If ``get_or_create_kek`` raises (Keychain denied), the lock is released
    before the exception propagates as ``NetworkPreflightFailed``."""
    import pytest

    from screencap.engine.network_policy import MitmProxyV15
    from screencap.engine.screen_recorder import NetworkPreflightFailed

    fake_lock = mock.MagicMock()

    with (
        mock.patch("screencap.config.get_network_config", return_value=mock.MagicMock()),
        mock.patch("screencap.network.lifecycle.acquire_network_lock", return_value=fake_lock),
        mock.patch("screencap.network.lifecycle.preflight_or_raise", return_value=_FAKE_PORT),
        mock.patch("screencap.network.crypto.get_or_create_kek", side_effect=RuntimeError("keychain locked")),
    ):
        with pytest.raises(NetworkPreflightFailed):
            MitmProxyV15().setup(tmp_path, privacy_config=None)

    fake_lock.release.assert_called_once()


def test_mitm_proxy_v15_setup_releases_lock_on_wrap_failure(tmp_path):
    """If ``wrap_dek`` raises, the lock is released before the exception
    propagates. Covers the DEK wrap step running AFTER the KEK is fetched."""
    import pytest

    from screencap.engine.network_policy import MitmProxyV15
    from screencap.engine.screen_recorder import NetworkPreflightFailed

    fake_lock = mock.MagicMock()

    with (
        mock.patch("screencap.config.get_network_config", return_value=mock.MagicMock()),
        mock.patch("screencap.network.lifecycle.acquire_network_lock", return_value=fake_lock),
        mock.patch("screencap.network.lifecycle.preflight_or_raise", return_value=_FAKE_PORT),
        mock.patch("screencap.network.crypto.get_or_create_kek", return_value=_FAKE_KEK),
        mock.patch("screencap.network.crypto.generate_dek", return_value=_FAKE_DEK),
        mock.patch("screencap.network.crypto.wrap_dek", side_effect=ValueError("wrong key length")),
    ):
        with pytest.raises(NetworkPreflightFailed):
            MitmProxyV15().setup(tmp_path, privacy_config=None)

    fake_lock.release.assert_called_once()


# ---------------------------------------------------------------------------
# Cycle 5 — MitmProxyV15.teardown(): releases the lock
# ---------------------------------------------------------------------------


def _run_successful_setup(policy, tmp_path, fake_lock):
    patches = _network_mocks()
    _start_mocks(patches, overrides={1: mock.patch(
        "screencap.network.lifecycle.acquire_network_lock", return_value=fake_lock
    )})
    try:
        policy.setup(tmp_path, privacy_config=None)
    finally:
        _stop_mocks(patches)


def test_mitm_proxy_v15_teardown_releases_lock(tmp_path):
    """``MitmProxyV15.teardown()`` releases the lock acquired by ``setup()``."""
    from screencap.engine.network_policy import MitmProxyV15

    fake_lock = mock.MagicMock()
    policy = MitmProxyV15()
    _run_successful_setup(policy, tmp_path, fake_lock)

    fake_lock.release.assert_not_called()
    policy.teardown()
    fake_lock.release.assert_called_once()


def test_mitm_proxy_v15_teardown_safe_without_setup():
    """``MitmProxyV15.teardown()`` does not raise when ``setup()`` was never called."""
    from screencap.engine.network_policy import MitmProxyV15

    MitmProxyV15().teardown()


# ---------------------------------------------------------------------------
# Cycle 6 — KEK plaintext lifetime: never stored on instance
# ---------------------------------------------------------------------------


def test_mitm_proxy_v15_kek_not_stored_on_instance(tmp_path):
    """KEK plaintext must not be reachable from any instance attribute after
    ``setup()`` returns — it lives only inside ``_prepare_dek_material``
    and is deleted before that method returns."""
    from screencap.engine.network_policy import MitmProxyV15

    fake_kek = b"k" * 32
    policy = MitmProxyV15()
    patches = _network_mocks()
    _start_mocks(patches, overrides={3: mock.patch(
        "screencap.network.crypto.get_or_create_kek", return_value=fake_kek
    )})
    try:
        policy.setup(tmp_path, privacy_config=None)
    finally:
        _stop_mocks(patches)

    for attr_val in vars(policy).values():
        assert attr_val is not fake_kek, (
            f"KEK plaintext found on instance: {attr_val!r}"
        )
        if isinstance(attr_val, (tuple, list)):
            assert fake_kek not in attr_val


# ---------------------------------------------------------------------------
# Cycle 7 — empty-allowlist warning
# ---------------------------------------------------------------------------


def test_mitm_proxy_v15_warns_on_empty_allowlist(tmp_path):
    """``MitmProxyV15.setup()`` prints the metadata-only warning when the
    effective capture-bodies allowlist is empty."""
    import io

    from rich.console import Console

    from screencap.engine.network_policy import MitmProxyV15

    buf = io.StringIO()
    policy = MitmProxyV15(console=Console(file=buf, highlight=False))

    patches = _network_mocks(allowlist=frozenset())
    _start_mocks(patches)
    try:
        policy.setup(tmp_path, privacy_config=None)
    finally:
        _stop_mocks(patches)

    assert "allowlist is empty" in buf.getvalue()


def test_mitm_proxy_v15_no_warning_with_non_empty_allowlist(tmp_path):
    """``MitmProxyV15.setup()`` does NOT print the warning when the allowlist
    has at least one entry."""
    import io

    from rich.console import Console

    from screencap.engine.network_policy import MitmProxyV15

    buf = io.StringIO()
    policy = MitmProxyV15(console=Console(file=buf, highlight=False))

    patches = _network_mocks(allowlist=frozenset(["*.github.com"]))
    _start_mocks(patches)
    try:
        policy.setup(tmp_path, privacy_config=None)
    finally:
        _stop_mocks(patches)

    assert "allowlist is empty" not in buf.getvalue()


# ---------------------------------------------------------------------------
# Cycle 9 — Wiring: ScreenRecorder.run() calls network_policy.setup + teardown
# ---------------------------------------------------------------------------


def test_screen_recorder_calls_network_policy_setup_and_teardown(tmp_path):
    """``ScreenRecorder.run()`` must call ``network_policy.setup(capture_dir,
    privacy_config)`` during setup and ``network_policy.teardown()`` in the
    finally block.

    Pins the wiring contract: setup delivers capture_dir so the policy can
    pass recording_dir to preflight_or_raise; teardown ensures the lock is
    released regardless of how the recording ends.
    """
    from screencap.engine.config import RecordingConfig
    from screencap.engine.disk_policy import Noop as DiskNoop
    from screencap.engine.lock_policy import InheritLock
    from screencap.engine.menubar_policy import Noop as MenubarNoop
    from screencap.engine.network_policy import NetworkMaterial
    from screencap.engine.permission_policy import Noop as PermNoop
    from screencap.engine.screen_recorder import (
        IpcChannels,
        LegacyOptions,
        NoopSignalPolicy,
        RecordingPolicies,
        RecordingRequest,
        ScreenRecorder,
    )
    from tests.conftest import FakeRecorder

    class SpyNetworkPolicy:
        def __init__(self):
            self.setup_calls: list = []
            self.teardown_count = 0

        def setup(self, capture_dir, privacy_config):
            self.setup_calls.append((capture_dir, privacy_config))
            return NetworkMaterial()

        def teardown(self):
            self.teardown_count += 1

    spy = SpyNetworkPolicy()
    capture_dir = tmp_path / "net-spy"
    request = RecordingRequest(name="net-spy", config=RecordingConfig())
    channels = IpcChannels.create()
    policies = RecordingPolicies(
        signal=NoopSignalPolicy(),
        lock=InheritLock(),
        menubar=MenubarNoop(),
        permission=PermNoop(),
        disk=DiskNoop(),
        network=spy,
    )
    legacy = LegacyOptions(output_dir=capture_dir)

    with (
        mock.patch("screencap.recorder.get_audio_default", return_value=False),
        mock.patch("screencap.recorder.get_wifi_metrics", return_value=False),
        mock.patch("screencap.recorder.get_app_versions", return_value=False),
        mock.patch("screencap.engine.recorder.Recorder", FakeRecorder),
    ):
        ScreenRecorder(
            request=request, channels=channels, policies=policies, legacy=legacy,
        ).run()

    assert len(spy.setup_calls) == 1, "setup must be called exactly once"
    assert spy.setup_calls[0][0] == capture_dir, "setup must receive the resolved capture_dir"
    assert spy.teardown_count == 1, "teardown must be called exactly once"
