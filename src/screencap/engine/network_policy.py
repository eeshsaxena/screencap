"""NetworkPolicy seam — V1.5 KEK/DEK + mitm preflight + proxy lifecycle.

Promotes the inline network-preflight block from ``_run_screen_recorder``
to a pluggable policy following the same shape as the existing seam policies.

* ``MitmProxyV15`` — lock acquisition, ``preflight_or_raise``, KEK access,
  DEK generate + wrap, KEK plaintext drop, empty-allowlist warning. Returns a
  ``NetworkMaterial`` carrying proxy port + crypto material for ``recorder_kwargs``.
* ``Null``          — used when ``--network`` is not passed; all hooks safe no-ops.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from rich.console import Console as _Console


@dataclass(frozen=True, slots=True)
class NetworkMaterial:
    """Material returned by ``NetworkPolicy.setup()``.

    ``active`` is ``True`` only when ``MitmProxyV15`` ran successfully.
    Callers gate ``recorder_kwargs`` population on ``material.active``.
    """

    proxy_port: int | None = None
    dek: bytes | None = None
    dek_wrapped: bytes | None = None
    dek_nonce: bytes | None = None
    network_config: Any | None = None

    @property
    def active(self) -> bool:
        return self.proxy_port is not None


class NetworkPolicy(Protocol):
    """V1.5 KEK/DEK + mitm preflight + proxy lifecycle. ``MitmProxyV15`` | ``Null``.

    Two-phase contract:
    * ``setup``    — acquire lock → preflight → DEK material. Lock held on return.
    * ``teardown`` — release the lock. Safe to call even if ``setup`` never ran.
    """

    def setup(self, capture_dir: Path, privacy_config: Any) -> NetworkMaterial: ...

    def teardown(self) -> None: ...


class Null:
    """No-op policy — used when ``--network`` is not passed.

    All hooks are safe no-ops; no lock is acquired, no preflight is run.
    """

    def setup(self, capture_dir: Path, privacy_config: Any) -> NetworkMaterial:
        return NetworkMaterial()

    def teardown(self) -> None:
        pass


class MitmProxyV15:
    """V1.5 mitm proxy: lock → preflight → KEK → DEK → wrap → return material.

    The lock is acquired before any user-facing prompt so concurrent
    ``screencap start --network`` invocations race out at the lock and
    never compete for the same prompts.

    SECURITY: the KEK plaintext lives only inside ``_prepare_dek_material``
    and is deleted (``del _kek``) before that method returns. It is never
    assigned to instance state — extending KEK lifetime weakens the wrapping
    invariant that downstream decryption depends on.
    """

    def __init__(self, console: "_Console | None" = None) -> None:
        self._lock_handle: Any | None = None
        # ``console=None`` defers resolution to ``_get_console`` so the
        # adapter's patchable ``screencap.recorder.console`` wins at the
        # point of printing (tests mock that attribute, and a fresh
        # ``Console()`` here would bypass the mock).
        self._console_override = console

    def _get_console(self) -> "_Console":
        if self._console_override is not None:
            return self._console_override
        from screencap.recorder import console as _adapter_console

        return _adapter_console

    def _prepare_dek_material(self) -> tuple[bytes, bytes, bytes]:
        """Fetch KEK, generate DEK, wrap — KEK lives only in this frame."""
        from screencap.network import crypto as _net_crypto

        _kek = _net_crypto.get_or_create_kek()
        dek = _net_crypto.generate_dek()
        dek_wrapped, dek_nonce = _net_crypto.wrap_dek(dek, _kek)
        del _kek
        return dek, dek_wrapped, dek_nonce

    def setup(self, capture_dir: Path, privacy_config: Any) -> NetworkMaterial:
        """Acquire lock, run preflight, generate DEK material.

        Raises ``NetworkPreflightFailed`` on any error, releasing the lock
        before the exception propagates. On success the lock is held until
        ``teardown()`` — do not wrap this call in ``try/finally teardown()``.
        """
        from screencap.config import get_network_config
        from screencap.engine.screen_recorder import NetworkPreflightFailed
        from screencap.network.blocklist import effective_capture_bodies_for
        from screencap.network.lifecycle import acquire_network_lock, preflight_or_raise

        lock_handle = None
        try:
            network_config = get_network_config()
            lock_handle = acquire_network_lock()
            proxy_port = preflight_or_raise(
                network_config, privacy_config, recording_dir=capture_dir
            )
            dek, dek_wrapped, dek_nonce = self._prepare_dek_material()

            if not effective_capture_bodies_for(network_config):
                self._get_console().print(
                    "[yellow]warning:[/yellow] effective capture-bodies "
                    "allowlist is empty; recording will be metadata-only "
                    "for all hosts."
                )
        except NetworkPreflightFailed:
            if lock_handle is not None:
                lock_handle.release()
            raise
        except Exception as exc:
            if lock_handle is not None:
                lock_handle.release()
            raise NetworkPreflightFailed(str(exc)) from exc

        self._lock_handle = lock_handle
        return NetworkMaterial(
            proxy_port=proxy_port,
            dek=dek,
            dek_wrapped=dek_wrapped,
            dek_nonce=dek_nonce,
            network_config=network_config,
        )

    def teardown(self) -> None:
        """Release the network lock. Safe to call without a prior ``setup``."""
        if self._lock_handle is not None:
            try:
                self._lock_handle.release()
            except Exception:  # noqa: BLE001
                pass
            self._lock_handle = None
