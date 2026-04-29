"""Network proxy logging config (V1 + V1.5).

Parses the ``[network]`` section of ``~/.screencap/config.toml`` into a
frozen :class:`NetworkConfig`.

V1 fields: ``extra_blocklist``, ``proxy_port``, ``override_default_blocklist``,
``body_size_cap``.

V1.5 fields: ``capture_bodies_for``, ``override_default_capture_bodies_for``.
The ``body_size_cap`` field is unchanged in V1.5 — it gains body-retention
semantics on top of its V1 streaming/hashing-threshold meaning, with no
schema change. See :data:`screencap.network.blocklist.DEFAULT_CAPTURE_BODIES_FOR`
for the curated default allowlist and the inclusion criteria.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.console import Console

console = Console()

# Body-size cap bounds (bytes). The lower bound keeps the streaming
# threshold large enough to be useful; the upper bound caps mitmproxy
# memory pressure for the chunk-hashing transformer.
_BODY_SIZE_CAP_MIN = 1024
_BODY_SIZE_CAP_MAX = 10 * 1024 * 1024

# Proxy port bounds — restrict to the unprivileged TCP range.
_PROXY_PORT_MIN = 1024
_PROXY_PORT_MAX = 65535


class InvalidNetworkConfigError(Exception):
    """Raised when the ``[network]`` config section is malformed."""


@dataclass(frozen=True)
class NetworkConfig:
    """Parsed ``[network]`` section from ``config.toml``.

    V1 fields:
        extra_blocklist: Lowercase host suffixes (frozenset). Suffix-match
            semantics mirror :attr:`PrivacyConfig.mask_domains`.
        proxy_port: TCP port the local mitmproxy listens on. Default ``0``
            means "auto-negotiate the first free port in 8080-8090". Set
            to a specific value (1024-65535) to pin a port; if pinned and
            busy, pre-flight aborts with an actionable error rather than
            falling back.
        override_default_blocklist: When True, the curated
            :data:`DEFAULT_BLOCKLIST` is NOT applied; only ``extra_blocklist``
            and ``privacy.mask_domains`` are honored (plus the IP-literal
            anchors that always run).
        body_size_cap: Streaming/hashing threshold in bytes. In V1 this
            controls only whether the addon installs the chunk-hashing
            transformer vs buffers for sha256; no body bytes are retained.
            In V1.5 the same value layers body-retention semantics on top:
            bodies > cap stay metadata-only across all hosts, bodies ≤ cap
            from a host in the effective :data:`DEFAULT_CAPTURE_BODIES_FOR`
            allowlist become candidates for encryption.

    V1.5 fields:
        capture_bodies_for: Lowercase host suffixes (frozenset) to retain
            body bytes for, on top of (or replacing — see the override
            field below) :data:`DEFAULT_CAPTURE_BODIES_FOR`. Supports the
            ``*.example.com`` wildcard form (stripped to the bare apex by
            :func:`_matches_suffix`). The blocklist always wins on overlap
            (a host in both this allowlist and any blocklist source is
            blocked, never body-captured).
        override_default_capture_bodies_for: When True, the user's
            ``capture_bodies_for`` REPLACES :data:`DEFAULT_CAPTURE_BODIES_FOR`
            entirely. When False (default), the two are unioned. An empty
            effective allowlist (override=True + empty user list) is a
            valid "metadata-only for all hosts" posture and triggers a
            pre-flight warning; see
            :func:`screencap.network.blocklist.effective_capture_bodies_for`.
    """

    extra_blocklist: frozenset[str] = field(default_factory=frozenset)
    proxy_port: int = 0  # 0 = auto-negotiate within 8080-8090
    override_default_blocklist: bool = False
    body_size_cap: int = 100_000
    capture_bodies_for: frozenset[str] = field(default_factory=frozenset)
    override_default_capture_bodies_for: bool = False


def parse_network_config(section: dict | None) -> NetworkConfig:
    """Parse the ``[network]`` section of a TOML config dict.

    Args:
        section: The ``[network]`` sub-table (not the full TOML root). May
            be ``None`` or missing — defaults are returned in that case.

    Returns:
        :class:`NetworkConfig` with validated settings.

    Raises:
        InvalidNetworkConfigError: On malformed values.
    """
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise InvalidNetworkConfigError(
            f"[network] must be a table, got {type(section).__name__}"
        )

    # extra_blocklist
    raw_blocklist = section.get("extra_blocklist", [])
    if not isinstance(raw_blocklist, list):
        raise InvalidNetworkConfigError(
            "network.extra_blocklist must be a list, got "
            f"{type(raw_blocklist).__name__}"
        )
    for i, host in enumerate(raw_blocklist):
        if not isinstance(host, str):
            raise InvalidNetworkConfigError(
                f"network.extra_blocklist[{i}] must be a string, got "
                f"{type(host).__name__}"
            )
    extra_blocklist = frozenset(h.lower() for h in raw_blocklist)

    # proxy_port (0 = auto-negotiate; non-zero = pin to that exact port)
    raw_port = section.get("proxy_port", 0)
    # NB: bool is a subclass of int — reject it explicitly so a stray
    # ``proxy_port = true`` doesn't slip through.
    if not isinstance(raw_port, int) or isinstance(raw_port, bool):
        raise InvalidNetworkConfigError(
            f"network.proxy_port must be an integer, got {type(raw_port).__name__}"
        )
    if raw_port != 0 and not (_PROXY_PORT_MIN <= raw_port <= _PROXY_PORT_MAX):
        raise InvalidNetworkConfigError(
            f"network.proxy_port must be 0 (auto-negotiate) or in "
            f"[{_PROXY_PORT_MIN}, {_PROXY_PORT_MAX}], got {raw_port}"
        )

    # override_default_blocklist
    raw_override = section.get("override_default_blocklist", False)
    if not isinstance(raw_override, bool):
        raise InvalidNetworkConfigError(
            "network.override_default_blocklist must be a bool, got "
            f"{type(raw_override).__name__}"
        )

    # body_size_cap
    raw_cap = section.get("body_size_cap", 100_000)
    if not isinstance(raw_cap, int) or isinstance(raw_cap, bool):
        raise InvalidNetworkConfigError(
            f"network.body_size_cap must be an integer, got {type(raw_cap).__name__}"
        )
    if not (_BODY_SIZE_CAP_MIN <= raw_cap <= _BODY_SIZE_CAP_MAX):
        raise InvalidNetworkConfigError(
            f"network.body_size_cap must be in [{_BODY_SIZE_CAP_MIN}, "
            f"{_BODY_SIZE_CAP_MAX}], got {raw_cap}"
        )

    # capture_bodies_for (V1.5)
    raw_capture = section.get("capture_bodies_for", [])
    if not isinstance(raw_capture, list):
        raise InvalidNetworkConfigError(
            "network.capture_bodies_for must be a list, got "
            f"{type(raw_capture).__name__}"
        )
    for i, host in enumerate(raw_capture):
        if not isinstance(host, str):
            raise InvalidNetworkConfigError(
                f"network.capture_bodies_for[{i}] must be a string, got "
                f"{type(host).__name__}"
            )
    capture_bodies_for = frozenset(h.lower() for h in raw_capture)

    # override_default_capture_bodies_for (V1.5)
    raw_override_capture = section.get("override_default_capture_bodies_for", False)
    if not isinstance(raw_override_capture, bool):
        raise InvalidNetworkConfigError(
            "network.override_default_capture_bodies_for must be a bool, got "
            f"{type(raw_override_capture).__name__}"
        )

    return NetworkConfig(
        extra_blocklist=extra_blocklist,
        proxy_port=raw_port,
        override_default_blocklist=raw_override,
        body_size_cap=raw_cap,
        capture_bodies_for=capture_bodies_for,
        override_default_capture_bodies_for=raw_override_capture,
    )
