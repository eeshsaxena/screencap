"""Network proxy logging config (V1).

Parses the ``[network]`` section of ``~/.screencap/config.toml`` into a
frozen :class:`NetworkConfig`. V1 fields only — body capture (the
``capture_bodies_for`` field) is V1.5 and is not stored on the dataclass.
If the user has it set in their config, we log a one-time warning and
ignore it.
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
    """Parsed ``[network]`` section from ``config.toml`` (V1 fields only).

    V1 fields:
        extra_blocklist: Lowercase host suffixes (frozenset). Suffix-match
            semantics mirror :attr:`PrivacyConfig.mask_domains`.
        proxy_port: TCP port the local mitmproxy listens on.
        override_default_blocklist: When True, the curated
            :data:`DEFAULT_BLOCKLIST` is NOT applied; only ``extra_blocklist``
            and ``privacy.mask_domains`` are honored (plus the IP-literal
            anchors that always run).
        body_size_cap: Streaming/hashing threshold in bytes. V1 doesn't
            retain body bytes, but the addon still needs a number to decide
            whether to install the chunk-hashing transformer vs buffer for
            sha256.
    """

    extra_blocklist: frozenset[str] = field(default_factory=frozenset)
    proxy_port: int = 8080
    override_default_blocklist: bool = False
    body_size_cap: int = 100_000


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

    # proxy_port
    raw_port = section.get("proxy_port", 8080)
    # NB: bool is a subclass of int — reject it explicitly so a stray
    # ``proxy_port = true`` doesn't slip through.
    if not isinstance(raw_port, int) or isinstance(raw_port, bool):
        raise InvalidNetworkConfigError(
            f"network.proxy_port must be an integer, got {type(raw_port).__name__}"
        )
    if not (_PROXY_PORT_MIN <= raw_port <= _PROXY_PORT_MAX):
        raise InvalidNetworkConfigError(
            f"network.proxy_port must be in [{_PROXY_PORT_MIN}, "
            f"{_PROXY_PORT_MAX}], got {raw_port}"
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

    # capture_bodies_for — V1.5 field, ignored in V1 with a one-time warning.
    if "capture_bodies_for" in section:
        console.print(
            "[yellow]warning:[/yellow] body capture is V1.5; "
            "`capture_bodies_for` is currently ignored"
        )

    return NetworkConfig(
        extra_blocklist=extra_blocklist,
        proxy_port=raw_port,
        override_default_blocklist=raw_override,
        body_size_cap=raw_cap,
    )
