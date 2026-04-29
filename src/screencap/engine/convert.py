"""Dict-based event conversion for DB rows.

Converts plain dicts (from sqlite3.Row or SQLAlchemy ORM) to Pydantic
event models.  Used by both CaptureSession (ORM) and the chunk processor
(raw sqlite3) so both paths share identical conversion logic.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

from loguru import logger

from screencap.engine.events import (
    ActionEvent,
    KeyDownEvent,
    KeyUpEvent,
    MouseButton,
    MouseDownEvent,
    MouseMagnifyEvent,
    MouseMoveEvent,
    MouseRotateEvent,
    MouseScrollEvent,
    MouseSmartMagnifyEvent,
    MouseUpEvent,
    NetworkDropBurstEvent,
    NetworkEvent,
    NetworkRequestEvent,
    NetworkResponseEvent,
    NetworkWebSocketFrameEvent,
    NetworkWebSocketUpgradeEvent,
    WindowSwitchEvent,
)


def bytes_to_hex(b: bytes | None) -> str | None:
    """Convert a raw bytes digest to lowercase hex.

    Single source of truth for the bytes -> hex conversion at every
    DB-to-Pydantic boundary in the codebase. Returns None for None
    input so callers can blindly forward.
    """
    if b is None:
        return None
    return b.hex()


def hex_to_bytes(s: str | None) -> bytes | None:
    """Convert a lowercase hex digest to raw bytes.

    Inverse of :func:`bytes_to_hex`. Returns None for None input
    or for empty strings (latter mirrors NULL semantics in SQLite
    where empty BLOB is sometimes coerced to ``""``).
    """
    if s is None or s == "":
        return None
    return bytes.fromhex(s)


def dict_to_action_event(row: dict) -> ActionEvent | None:
    """Convert a dict to a Pydantic ActionEvent.

    Works with dicts from sqlite3.Row (chunk processor) or
    ORM attribute access (CaptureSession).

    Args:
        row: Dict with DB column names as keys.

    Returns:
        Pydantic event or None if unrecognized.
    """
    ts = row.get("timestamp", 0.0)
    name = row.get("name")

    if name == "move":
        return MouseMoveEvent(
            timestamp=ts,
            x=row.get("mouse_x") or 0,
            y=row.get("mouse_y") or 0,
            pressure=row.get("mouse_pressure"),
            modifier_flags=row.get("modifier_flags"),
        )
    elif name == "click":
        button_str = row.get("mouse_button_name") or "left"
        try:
            button = MouseButton(button_str)
        except ValueError:
            button = MouseButton.LEFT

        mouse_pressed = row.get("mouse_pressed")
        if mouse_pressed is True or mouse_pressed == 1:
            return MouseDownEvent(
                timestamp=ts,
                x=row.get("mouse_x") or 0,
                y=row.get("mouse_y") or 0,
                button=button,
                pressure=row.get("mouse_pressure"),
                modifier_flags=row.get("modifier_flags"),
            )
        elif mouse_pressed is False or mouse_pressed == 0:
            return MouseUpEvent(
                timestamp=ts,
                x=row.get("mouse_x") or 0,
                y=row.get("mouse_y") or 0,
                button=button,
                pressure=row.get("mouse_pressure"),
                modifier_flags=row.get("modifier_flags"),
            )
        else:
            return None
    elif name == "scroll":
        return MouseScrollEvent(
            timestamp=ts,
            x=row.get("mouse_x") or 0,
            y=row.get("mouse_y") or 0,
            dx=row.get("mouse_dx") or 0,
            dy=row.get("mouse_dy") or 0,
            modifier_flags=row.get("modifier_flags"),
            scroll_phase=row.get("scroll_phase"),
            momentum_phase=row.get("momentum_phase"),
            is_continuous=row.get("is_continuous"),
        )
    elif name == "press":
        return KeyDownEvent(
            timestamp=ts,
            key_name=row.get("key_name"),
            key_char=row.get("key_char"),
            key_vk=row.get("key_vk"),
            canonical_key_name=row.get("canonical_key_name"),
            canonical_key_char=row.get("canonical_key_char"),
            canonical_key_vk=row.get("canonical_key_vk"),
        )
    elif name == "release":
        return KeyUpEvent(
            timestamp=ts,
            key_name=row.get("key_name"),
            key_char=row.get("key_char"),
            key_vk=row.get("key_vk"),
            canonical_key_name=row.get("canonical_key_name"),
            canonical_key_char=row.get("canonical_key_char"),
            canonical_key_vk=row.get("canonical_key_vk"),
        )
    elif name == "magnify":
        return MouseMagnifyEvent(
            timestamp=ts,
            x=row.get("mouse_x") or 0,
            y=row.get("mouse_y") or 0,
            magnification=row.get("mouse_dx") or 0.0,
        )
    elif name == "rotate":
        return MouseRotateEvent(
            timestamp=ts,
            x=row.get("mouse_x") or 0,
            y=row.get("mouse_y") or 0,
            rotation=row.get("mouse_dx") or 0.0,
        )
    elif name == "smart_magnify":
        return MouseSmartMagnifyEvent(
            timestamp=ts,
            x=float(row.get("mouse_x") or 0),
            y=float(row.get("mouse_y") or 0),
        )
    return None


def dict_to_window_switch(row: dict) -> WindowSwitchEvent:
    """Convert a window_event DB row dict to a WindowSwitchEvent.

    Args:
        row: Dict with window_event DB column names.

    Returns:
        WindowSwitchEvent instance.
    """
    bundle_id = row.get("app_bundle_id") or ""
    # Derive app_name from bundle_id (last component, titlecased)
    if bundle_id:
        app_name = bundle_id.rsplit(".", 1)[-1].replace("-", " ").title()
    else:
        app_name = row.get("title") or "Unknown"

    # Extract domain from browser_url (null-safe for old recordings)
    domain: str | None = None
    browser_url = row.get("browser_url")
    if browser_url:
        try:
            domain = urlparse(browser_url).hostname or None
        except Exception:
            domain = None

    return WindowSwitchEvent(
        timestamp=row.get("timestamp", 0.0),
        app_name=app_name,
        app_bundle_id=row.get("app_bundle_id"),
        window_title=row.get("title") or "",
        window_id=str(row.get("window_id") or ""),
        x=row.get("left") or 0,
        y=row.get("top") or 0,
        width=row.get("width") or 0,
        height=row.get("height") or 0,
        domain=domain,
    )


def _parse_headers_json(value) -> list[tuple[str, str]]:
    """Parse a `headers_json` column value into a list of (name, value) tuples.

    Accepts either a JSON-encoded string or a Python list (some callers
    may have already deserialized). Returns an empty list on missing /
    malformed input.
    """
    if value is None:
        return []
    if isinstance(value, str):
        if not value:
            return []
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            logger.debug(f"dict_to_network_event: bad headers_json={value!r}")
            return []
    if not isinstance(value, list):
        return []
    out: list[tuple[str, str]] = []
    for item in value:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            name, val = item
            out.append((str(name), str(val)))
    return out


def _parse_details_json(value) -> dict | None:
    """Parse a `details_json` column value into a dict.

    Returns None on missing / malformed input. Strings get JSON-decoded;
    already-decoded dicts pass through.
    """
    if value is None:
        return None
    if isinstance(value, str):
        if not value:
            return None
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            logger.debug(f"dict_to_network_event: bad details_json={value!r}")
            return None
    if isinstance(value, dict):
        return value
    return None


def _coerce_sha256_hex(value) -> str | None:
    """Normalize a `body_sha256` column value to lowercase hex (or None).

    The DB column is `LargeBinary(32)` so the natural value is `bytes`,
    but some sqlite paths surface it as ``memoryview``. Already-hex
    strings pass through unchanged (lowercased) for forward compatibility.
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes_to_hex(bytes(value))
    if isinstance(value, memoryview):
        return bytes_to_hex(value.tobytes())
    if isinstance(value, str):
        return value.lower() or None
    return None


def dict_to_network_event(row: dict) -> NetworkEvent | None:
    """Convert a network_event DB row dict to a Pydantic network event.

    Dispatches on the `kind` column (short form: request / response /
    ws_upgrade / ws_frame / drop_burst). Mirrors the exception tolerance
    of :func:`dict_to_action_event` - returns None on unknown kind, logs
    and returns None when conversion fails.

    Args:
        row: Dict with network_event DB column names.

    Returns:
        Pydantic event or None if kind unrecognized / conversion failed.
    """
    kind = row.get("kind")
    if not kind:
        return None

    ts = row.get("timestamp", 0.0)
    ts_ns = row.get("timestamp_ns") or 0
    flow_id = row.get("flow_id") or ""
    host = row.get("host") or ""

    try:
        if kind == "request":
            return NetworkRequestEvent(
                timestamp=ts,
                timestamp_ns=ts_ns,
                flow_id=flow_id,
                method=row.get("method") or "",
                url=row.get("url") or "",
                host=host,
                headers=_parse_headers_json(row.get("headers_json")),
                body_size=row.get("body_size"),
                body_sha256_hex=_coerce_sha256_hex(row.get("body_sha256")),
                content_type=row.get("content_type"),
                http_version=row.get("http_version"),
            )
        elif kind == "response":
            return NetworkResponseEvent(
                timestamp=ts,
                timestamp_ns=ts_ns,
                flow_id=flow_id,
                host=host,
                status=int(row.get("status") or 0),
                headers=_parse_headers_json(row.get("headers_json")),
                body_size=row.get("body_size"),
                body_sha256_hex=_coerce_sha256_hex(row.get("body_sha256")),
                content_type=row.get("content_type"),
                http_version=row.get("http_version"),
            )
        elif kind == "ws_upgrade":
            return NetworkWebSocketUpgradeEvent(
                timestamp=ts,
                timestamp_ns=ts_ns,
                flow_id=flow_id,
                url=row.get("url") or "",
                host=host,
                status=int(row.get("status") or 101),
                headers=_parse_headers_json(row.get("headers_json")),
                http_version=row.get("http_version"),
                details_json=_parse_details_json(row.get("details_json")),
            )
        elif kind == "ws_frame":
            direction = row.get("direction")
            frame_type = row.get("frame_type")
            if direction not in ("sent", "received"):
                logger.debug(
                    f"dict_to_network_event: bad ws_frame direction={direction!r}"
                )
                return None
            if frame_type not in ("text", "binary"):
                logger.debug(
                    f"dict_to_network_event: bad ws_frame frame_type={frame_type!r}"
                )
                return None
            return NetworkWebSocketFrameEvent(
                timestamp=ts,
                timestamp_ns=ts_ns,
                flow_id=flow_id,
                host=host,
                direction=direction,
                frame_type=frame_type,
                body_size=row.get("body_size"),
                body_sha256_hex=_coerce_sha256_hex(row.get("body_sha256")),
            )
        elif kind == "drop_burst":
            details = _parse_details_json(row.get("details_json"))
            if details is None:
                # drop_burst details_json is REQUIRED per schema; treat as
                # malformed and skip rather than emit a partially-empty event.
                logger.debug(
                    "dict_to_network_event: drop_burst row missing details_json"
                )
                return None
            return NetworkDropBurstEvent(
                timestamp=ts,
                timestamp_ns=ts_ns,
                details_json=details,
            )
    except Exception as e:  # pragma: no cover - defensive
        logger.debug(f"dict_to_network_event: failed to convert kind={kind!r}: {e}")
        return None

    logger.debug(f"dict_to_network_event: unknown kind={kind!r}")
    return None
