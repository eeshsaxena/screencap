"""Dict-based event conversion for DB rows.

Converts plain dicts (from sqlite3.Row or SQLAlchemy ORM) to Pydantic
event models.  Used by both CaptureSession (ORM) and the chunk processor
(raw sqlite3) so both paths share identical conversion logic.
"""

from __future__ import annotations

from urllib.parse import urlparse

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
    WindowSwitchEvent,
)


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
