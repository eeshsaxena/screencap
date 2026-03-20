"""Privacy setup wizard -- interactive app classification and config persistence.

Called by `screencap setup`. Discovers installed apps, auto-classifies them,
presents a curses TUI for interactive review, and saves to ~/.screencap/config.toml.

Config writes are atomic (temp file + os.rename) and preserve existing
non-privacy sections and comments via tomlkit.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import click
import tomlkit
from rich.console import Console
from rich.table import Table

from screencap.app_discovery import (
    AppMetadata,
    ClassificationResult,
    auto_classify,
    auto_classify_detailed,
    discover_installed_apps,
    is_background_app,
)
from screencap.privacy.context import BUNDLE_ID_MAP
from screencap.privacy.policy import ContextClass, PrivacyMode

console = Console()

# ContextClasses that map to "blocked" (never captured)
_BLOCKED_CLASSES = frozenset({ContextClass.PASSWORD_MANAGER, ContextClass.BANKING})

# ContextClasses that are communication-related (masked in public mode)
_COMM_CLASSES = frozenset({
    ContextClass.EMAIL,
    ContextClass.CHAT,
    ContextClass.CALENDAR,
    ContextClass.VIDEO_CALL,
})

# ContextClasses that are code/dev (captured normally)
_CODE_CLASSES = frozenset({
    ContextClass.CODE_EDITOR_TERMINAL,
    ContextClass.ADMIN_CONSOLE,
    ContextClass.BROWSER_UNVERIFIED,
})

# Sources that indicate a "safe" auto-classification (no specific privacy class)
_SAFE_SOURCES = frozenset({
    "apple_prefix",
    "dev_runtime",
    "browser_pwa",
    "system_service",
    "input_method",
    "lifecycle",
    "decoration",
    "category_safe",
})

# Human-friendly class label
_CLASS_LABELS: dict[ContextClass, str] = {
    ContextClass.PASSWORD_MANAGER: "password manager",
    ContextClass.BANKING: "banking",
    ContextClass.EMAIL: "email",
    ContextClass.CHAT: "chat",
    ContextClass.CALENDAR: "calendar",
    ContextClass.VIDEO_CALL: "video call",
    ContextClass.BROWSER_UNVERIFIED: "browser",
    ContextClass.CODE_EDITOR_TERMINAL: "code editor",
    ContextClass.ADMIN_CONSOLE: "admin console",
    ContextClass.UNKNOWN: "unknown",
}

# Group definitions: key, label, symbol, color_key
_GROUP_DEFS = [
    ("blocked", "Always blocked (sensitive)", "\u00d7", "pink"),
    ("communication", "Communication (masked in public mode)", "~", "purple"),
    ("safe", "Safe (captured normally)", "\u2713", "cyan"),
    ("unclassified", "Needs your input", "?", "indigo"),
]


def _classify_with_overrides(
    apps: list[AppMetadata],
    existing_app_classes: dict[str, ContextClass] | None = None,
    existing_exclude_apps: frozenset[str] | None = None,
) -> dict[str, tuple[AppMetadata, ContextClass, str]]:
    """Classify all apps, respecting existing config and hardcoded map.

    Priority: existing config > auto_classify_detailed (which checks BUNDLE_ID_MAP).

    Returns: dict of bundle_id -> (metadata, context_class, source)
    """
    existing_app_classes = existing_app_classes or {}
    existing_exclude_apps = existing_exclude_apps or frozenset()
    result: dict[str, tuple[AppMetadata, ContextClass, str]] = {}

    for app in apps:
        bid = app.bundle_id

        # Existing config takes priority
        if bid in existing_app_classes:
            result[bid] = (app, existing_app_classes[bid], "user_config")
        elif bid in existing_exclude_apps:
            result[bid] = (app, ContextClass.PASSWORD_MANAGER, "user_config")
        else:
            cr = auto_classify_detailed(app)
            result[bid] = (app, cr.context_class, cr.source)

    return result


def _group_apps(
    classified: dict[str, tuple[AppMetadata, ContextClass, str]],
) -> tuple[
    dict[str, list[tuple[AppMetadata, ContextClass, str]]],
    list[tuple[AppMetadata, ContextClass, str]],
]:
    """Group classified apps into display categories.

    Background apps and safe auto-classified apps are collected separately
    (silently auto-allowed, not shown in wizard).

    Returns:
        (groups, auto_allowed) where groups are the visible display groups
        and auto_allowed are safe/background apps to silently allow.
    """
    groups: dict[str, list[tuple[AppMetadata, ContextClass, str]]] = {
        "blocked": [],
        "communication": [],
        "safe": [],
        "unclassified": [],
    }
    auto_allowed: list[tuple[AppMetadata, ContextClass, str]] = []

    for _bid, (meta, cls, source) in classified.items():
        # Background apps and safe-classified apps are silently auto-allowed
        if is_background_app(meta) or source in _SAFE_SOURCES:
            auto_allowed.append((meta, cls, source))
            continue

        if cls in _BLOCKED_CLASSES:
            groups["blocked"].append((meta, cls, source))
        elif cls in _COMM_CLASSES:
            groups["communication"].append((meta, cls, source))
        elif cls in _CODE_CLASSES:
            groups["safe"].append((meta, cls, source))
        elif source == "unknown":
            groups["unclassified"].append((meta, cls, source))
        else:
            # known_app, apple_sensitive, pattern_rule, category_map
            # that don't map to a specific sensitive class -> auto-allow
            auto_allowed.append((meta, cls, source))

    for group in groups.values():
        group.sort(key=lambda x: x[0].display_name.lower())

    return groups, auto_allowed


def _load_config_toml(config_path: Path) -> tomlkit.TOMLDocument:
    """Load existing config.toml or return empty document."""
    if config_path.exists():
        return tomlkit.parse(config_path.read_text())
    return tomlkit.document()


def _save_config_atomic(config_path: Path, doc: tomlkit.TOMLDocument) -> None:
    """Write config atomically via temp file + rename."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(config_path.parent),
        suffix=".toml.tmp",
    )
    closed = False
    try:
        os.write(fd, tomlkit.dumps(doc).encode())
        os.close(fd)
        closed = True
        os.rename(tmp_path, str(config_path))
    except Exception:
        if not closed:
            os.close(fd)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _build_save_doc(
    doc: tomlkit.TOMLDocument,
    mode: PrivacyMode,
    exclude_apps: list[str],
    allow_apps: list[str],
    app_classes: dict[str, str],
    upload_default: str = "ask",
) -> tomlkit.TOMLDocument:
    """Update the TOML document with privacy settings."""
    # Ensure [privacy] section exists
    if "privacy" not in doc:
        doc.add("privacy", tomlkit.table())

    privacy = doc["privacy"]
    privacy["mode"] = mode.value
    privacy["upload_default"] = upload_default

    if exclude_apps:
        privacy["exclude_apps"] = exclude_apps
    elif "exclude_apps" in privacy:
        del privacy["exclude_apps"]

    if allow_apps:
        privacy["allow_apps"] = allow_apps
    elif "allow_apps" in privacy:
        del privacy["allow_apps"]

    if app_classes:
        ac_table = tomlkit.table()
        for bid, cls_str in sorted(app_classes.items()):
            ac_table.add(bid, cls_str)
        privacy["app_classes"] = ac_table
    elif "app_classes" in privacy:
        del privacy["app_classes"]

    return doc


# ---------------------------------------------------------------------------
# Curses TUI for interactive app review
# ---------------------------------------------------------------------------

# Brand palette (ANSI-256 equivalents of the hex colors used in recorder.py):
#   #60a5fa (brand blue)  -> 111    #818cf8 (indigo)      -> 105
#   #a78bfa (purple)      -> 141    #f472b6 (pink)        -> 211
#   #22d3ee (cyan)        ->  80    #f0f4ff (near-white)  -> 255
_ANSI = {"blue": 111, "indigo": 105, "purple": 141, "pink": 211, "cyan": 80}
_COLOR_PAIR = {"pink": 1, "purple": 2, "cyan": 3, "indigo": 4, "blue": 5}


def _toggle_override(
    overrides: dict[str, str],
    group_key: str,
    bundle_id: str,
) -> None:
    """Toggle an app's override in-place (no group mutation).

    First toggle: blocked/unclassified -> allow, others -> block.
    Subsequent toggles flip between allow and block.
    """
    if bundle_id in overrides:
        overrides[bundle_id] = (
            "allow" if overrides[bundle_id] == "block" else "block"
        )
    elif group_key in ("blocked", "unclassified"):
        overrides[bundle_id] = "allow"
    else:
        overrides[bundle_id] = "block"


def _app_visual(
    group_key: str,
    bundle_id: str,
    overrides: dict[str, str],
    default_sym: str,
    default_col: str,
) -> tuple[str, str]:
    """Return (symbol, color_key) for an app, considering overrides."""
    if bundle_id in overrides:
        if overrides[bundle_id] == "block":
            return "\u00d7", "pink"
        return "\u2713", "cyan"
    return default_sym, default_col


def _run_tui(
    groups: dict[str, list[tuple[AppMetadata, ContextClass, str]]],
    auto_allowed_count: int,
) -> dict[str, str] | None:
    """Curses TUI for interactive app review.

    Arrow keys navigate, Enter/Space toggle icons in-place, s saves, q cancels.
    Returns overrides dict on save (bundle_id -> "allow"|"block"), None on cancel.
    Groups are never mutated; overrides are applied by the caller on save.
    """
    import curses

    def _build_items(grps, expanded):
        """Build flat list of navigable items from groups."""
        items = []
        for key, label, symbol, color in _GROUP_DEFS:
            apps = grps.get(key, [])
            if not apps:
                continue
            items.append(("group", key, label, symbol, color, len(apps)))
            if expanded.get(key):
                for app_meta, cls, src in apps:
                    items.append(("app", key, app_meta, cls, src, symbol, color))
        return items

    def _main(stdscr):
        curses.curs_set(0)
        curses.use_default_colors()
        # Use 256-color palette matching the app's brand colors
        if curses.COLORS >= 256:
            curses.init_pair(1, _ANSI["pink"], -1)     # blocked
            curses.init_pair(2, _ANSI["purple"], -1)   # communication
            curses.init_pair(3, _ANSI["cyan"], -1)     # safe
            curses.init_pair(4, _ANSI["indigo"], -1)   # unclassified
            curses.init_pair(5, _ANSI["blue"], -1)     # brand/title
        else:
            curses.init_pair(1, curses.COLOR_RED, -1)
            curses.init_pair(2, curses.COLOR_MAGENTA, -1)
            curses.init_pair(3, curses.COLOR_CYAN, -1)
            curses.init_pair(4, curses.COLOR_BLUE, -1)
            curses.init_pair(5, curses.COLOR_BLUE, -1)

        expanded = {k: True for k in groups if groups[k]}
        overrides: dict[str, str] = {}
        cursor = 0
        scroll = 0

        # Start cursor on "Needs your input" group if it exists
        init_items = _build_items(groups, expanded)
        for idx, item in enumerate(init_items):
            if item[0] == "group" and item[1] == "unclassified":
                cursor = idx
                break

        while True:
            stdscr.erase()
            h, w = stdscr.getmaxyx()

            if h < 6 or w < 40:
                try:
                    stdscr.addstr(0, 0, "Terminal too small. Resize and retry.")
                except curses.error:
                    pass
                stdscr.refresh()
                ch = stdscr.getch()
                if ch == ord("q") or ch == 27:
                    return None
                continue

            items = _build_items(groups, expanded)

            if not items:
                try:
                    stdscr.addstr(1, 1, "No apps to review.")
                    stdscr.addstr(3, 1, "Press s to save, q to cancel.")
                except curses.error:
                    pass
                stdscr.refresh()
                ch = stdscr.getch()
                if ch == ord("s"):
                    return overrides
                if ch == ord("q") or ch == 27:
                    return None
                continue

            cursor = max(0, min(cursor, len(items) - 1))

            header_h = 2
            footer_h = 3
            content_h = max(1, h - header_h - footer_h)

            if cursor < scroll:
                scroll = cursor
            if cursor >= scroll + content_h:
                scroll = cursor - content_h + 1

            # --- Header ---
            brand = curses.color_pair(_COLOR_PAIR["blue"])
            indigo = curses.color_pair(_COLOR_PAIR["indigo"])
            visible = sum(len(g) for g in groups.values())
            try:
                title = "\u25c9 ScreenCap"
                stdscr.addnstr(0, 1, title, w - 2, brand | curses.A_BOLD)
                sub = "Privacy Setup"
                x = len(title) + 3
                stdscr.addnstr(0, x, sub, max(0, w - x - 1), curses.A_BOLD)
                info = f"({visible} apps)"
                x2 = x + len(sub) + 1
                stdscr.addnstr(0, x2, info, max(0, w - x2 - 1), curses.A_DIM)
                stdscr.addnstr(1, 0, "\u2500" * (w - 1), w - 1, indigo)
            except curses.error:
                pass

            # --- Content ---
            for i in range(scroll, min(len(items), scroll + content_h)):
                y = header_h + (i - scroll)
                if y >= h - footer_h:
                    break
                item = items[i]
                sel = i == cursor

                if item[0] == "group":
                    _, key, label, _sym, col, cnt = item
                    arrow = "\u25bc" if expanded.get(key) else "\u25b6"
                    text = f" {arrow} {label} ({cnt})"
                    cp = curses.color_pair(_COLOR_PAIR.get(col, 0))
                    attr = cp | curses.A_BOLD
                    if sel:
                        attr |= curses.A_REVERSE
                    try:
                        stdscr.addnstr(y, 0, text, w - 1, attr)
                    except curses.error:
                        pass
                else:
                    _, grp_key, app_meta, cls, _src, sym, col = item
                    sym, col = _app_visual(
                        grp_key, app_meta.bundle_id, overrides, sym, col,
                    )
                    cls_lbl = _CLASS_LABELS.get(cls, cls.value)
                    name = app_meta.display_name
                    if len(name) > 24:
                        name = name[:23] + "\u2026"

                    sym_part = f"    {sym} "
                    name_part = f"{name:<24}"
                    cls_part = f" ({cls_lbl})"

                    if sel:
                        full = sym_part + name_part + cls_part
                        try:
                            stdscr.addnstr(y, 0, full, w - 1, curses.A_REVERSE)
                        except curses.error:
                            pass
                    else:
                        cp = curses.color_pair(_COLOR_PAIR.get(col, 0))
                        try:
                            stdscr.addnstr(y, 0, sym_part, w - 1, cp)
                            x = len(sym_part)
                            stdscr.addnstr(
                                y, x, name_part, max(0, w - 1 - x)
                            )
                            x += len(name_part)
                            stdscr.addnstr(
                                y, x, cls_part, max(0, w - 1 - x),
                                curses.A_DIM,
                            )
                        except curses.error:
                            pass

            # --- Footer ---
            fy = h - footer_h
            if auto_allowed_count > 0:
                aa_info = (
                    f" ({auto_allowed_count} system/utility apps auto-allowed)"
                )
                try:
                    stdscr.addnstr(fy, 0, aa_info, w - 1, curses.A_DIM)
                except curses.error:
                    pass

            # Context-sensitive action hint
            action = ""
            if cursor < len(items):
                cur = items[cursor]
                if cur[0] == "group":
                    action = "Enter/Space Expand/Collapse"
                else:
                    bid = cur[2].bundle_id
                    grp = cur[1]
                    # Determine what next toggle will produce
                    if bid in overrides:
                        will_be = "allow" if overrides[bid] == "block" else "block"
                    elif grp in ("blocked", "unclassified"):
                        will_be = "allow"
                    else:
                        will_be = "block"
                    if will_be == "allow":
                        action = "Enter/Space Allow \u2713"
                    else:
                        action = "Enter/Space Block \u00d7"

            try:
                stdscr.addnstr(fy + 1, 0, "\u2500" * (w - 1), w - 1, indigo)
                # Render hints with brand-colored keys and dim descriptions
                hint_x = 1
                key_parts = [
                    ("\u2191\u2193", "Navigate"),
                    (action.split(" ", 1)[0] if action else "", action.split(" ", 1)[1] if " " in action else ""),
                    ("s", "Save"),
                    ("q", "Quit"),
                ]
                for key_text, desc_text in key_parts:
                    if not key_text:
                        continue
                    stdscr.addnstr(fy + 2, hint_x, key_text, max(0, w - 1 - hint_x), brand)
                    hint_x += len(key_text)
                    label = f" {desc_text}  "
                    stdscr.addnstr(fy + 2, hint_x, label, max(0, w - 1 - hint_x), curses.A_DIM)
                    hint_x += len(label)
            except curses.error:
                pass

            stdscr.refresh()

            # --- Input ---
            ch = stdscr.getch()
            if ch == ord("q") or ch == 27:
                return None
            elif ch == ord("s"):
                return overrides
            elif ch in (curses.KEY_UP, ord("k")):
                cursor = max(0, cursor - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                cursor = min(len(items) - 1, cursor + 1)
            elif ch == curses.KEY_HOME:
                cursor = 0
            elif ch == curses.KEY_END:
                cursor = max(0, len(items) - 1)
            elif ch == curses.KEY_PPAGE:
                cursor = max(0, cursor - content_h)
            elif ch == curses.KEY_NPAGE:
                cursor = min(len(items) - 1, cursor + content_h)
            elif ch in (curses.KEY_ENTER, 10, 13):
                if cursor < len(items):
                    cur = items[cursor]
                    if cur[0] == "group":
                        key = cur[1]
                        expanded[key] = not expanded.get(key, True)
                    else:
                        _toggle_override(overrides, cur[1], cur[2].bundle_id)
            elif ch == ord(" "):
                if cursor < len(items):
                    cur = items[cursor]
                    if cur[0] == "app":
                        _toggle_override(overrides, cur[1], cur[2].bundle_id)
                    elif cur[0] == "group":
                        key = cur[1]
                        expanded[key] = not expanded.get(key, True)
            elif ch == curses.KEY_RESIZE:
                pass  # re-render on next iteration

    return curses.wrapper(_main)


def run_setup_wizard(
    config_path: Path | None = None,
    scan_only: bool = False,
) -> bool:
    """Run the full interactive setup wizard.

    Args:
        config_path: Path to config.toml. Defaults to ~/.screencap/config.toml.
        scan_only: If True, skip mode selection and only show new apps.

    Returns:
        True if config was saved, False otherwise.
    """
    if not sys.stdin.isatty():
        console.print(
            "Setup requires an interactive terminal. "
            "Run interactively or edit ~/.screencap/config.toml directly."
        )
        return False

    if config_path is None:
        from screencap.config import _CONFIG_PATH
        config_path = _CONFIG_PATH

    doc = _load_config_toml(config_path)
    existing_privacy = doc.get("privacy", {})

    # Load existing settings
    existing_mode_str = existing_privacy.get("mode", "internal") if isinstance(existing_privacy, dict) else "internal"
    existing_exclude = frozenset(existing_privacy.get("exclude_apps", [])) if isinstance(existing_privacy, dict) else frozenset()
    existing_allow = frozenset(existing_privacy.get("allow_apps", [])) if isinstance(existing_privacy, dict) else frozenset()
    existing_ac = {}
    if isinstance(existing_privacy, dict):
        raw_ac = existing_privacy.get("app_classes", {})
        if isinstance(raw_ac, dict):
            for bid, cls_str in raw_ac.items():
                try:
                    existing_ac[bid] = ContextClass(cls_str)
                except ValueError:
                    pass

    # Destination + privacy mode (skip in scan-only mode)
    existing_upload_default = (
        existing_privacy.get("upload_default", "ask")
        if isinstance(existing_privacy, dict) else "ask"
    )
    if scan_only:
        try:
            mode = PrivacyMode(existing_mode_str)
        except ValueError:
            mode = PrivacyMode.INTERNAL
        upload_default = existing_upload_default
    else:
        console.print(f"\n[bold #60a5fa]\u25c9 ScreenCap[/bold #60a5fa] [dim #a78bfa]Privacy Setup[/dim #a78bfa]\n")
        console.print("[bold]Where will your recordings go?[/bold]\n")
        console.print("  [#818cf8]1.[/#818cf8] Cloud          \u2192 uploads to Claude (public privacy mode)")
        console.print("  [#818cf8]2.[/#818cf8] Local          \u2192 stays on this machine (internal privacy mode)")
        console.print("  [#818cf8]3.[/#818cf8] Ask every time\n")

        _default_choice = {"cloud": 1, "local": 2, "ask": 3}.get(existing_upload_default, 2)
        dest_choice = click.prompt("  Choice", type=click.IntRange(1, 3), default=_default_choice)

        mode, upload_default = {
            1: (PrivacyMode.PUBLIC, "cloud"),
            2: (PrivacyMode.INTERNAL, "local"),
            3: (PrivacyMode.INTERNAL, "ask"),
        }[dest_choice]

    # In scan-only mode, check recordings first to avoid unnecessary Spotlight scan
    recording_unclassified: set[str] | None = None
    if scan_only:
        from screencap.catalog import get_seen_bundle_ids

        seen_bids = get_seen_bundle_ids()
        known_bids = (
            set(BUNDLE_ID_MAP.keys())
            | set(existing_ac.keys())
            | existing_exclude
            | existing_allow
        )
        recording_unclassified = seen_bids - known_bids

        if not recording_unclassified:
            console.print("[green]No unclassified apps found in recordings.[/green]")
            return False

    # Discover apps
    with console.status("Scanning installed apps..."):
        apps = discover_installed_apps(use_spotlight=True, spotlight_timeout=5.0)

    if not apps:
        console.print("[yellow]No apps found.[/yellow]")
        return False

    # Classify
    classified = _classify_with_overrides(apps, existing_ac, existing_exclude)

    # In scan-only mode, filter to recording-seen unclassified apps
    if scan_only and recording_unclassified is not None:
        new_classified = {
            bid: info for bid, info in classified.items()
            if bid in recording_unclassified
        }

        # Warn about apps seen in recordings but not found on disk
        missing = recording_unclassified - set(classified.keys())
        if missing:
            for bid in sorted(missing):
                console.print(
                    f"[dim]Note: {bid} was seen in a recording but is not "
                    f"currently installed — skipping.[/dim]"
                )

        if not new_classified:
            console.print("[green]All recording-seen apps are already classified.[/green]")
            return False

        console.print(f"\nFound {len(new_classified)} app(s) from recordings to classify.")
        classified = new_classified

    groups, auto_allowed = _group_apps(classified)

    # Interactive review via curses TUI
    try:
        overrides = _run_tui(groups, len(auto_allowed))
    except Exception:
        console.print(
            "[red]Interactive mode unavailable.[/red] "
            "Edit ~/.screencap/config.toml directly."
        )
        return False

    if overrides is None:
        # Save destination preference even though app review was skipped
        doc = _load_config_toml(config_path)
        if "privacy" not in doc:
            doc.add("privacy", tomlkit.table())
        doc["privacy"]["mode"] = mode.value
        doc["privacy"]["upload_default"] = upload_default
        _save_config_atomic(config_path, doc)
        from screencap.config import invalidate_config_cache
        invalidate_config_cache()
        console.print(
            "  [dim]App review skipped. Destination preference saved.[/dim] "
            "Run [bold]screencap setup[/bold] to classify apps."
        )
        return False

    # Build final config from groups + overrides
    final_exclude: list[str] = sorted(existing_exclude)
    final_allow: list[str] = sorted(existing_allow)
    final_app_classes: dict[str, str] = dict(
        (bid, cls.value) for bid, cls in existing_ac.items()
    )

    # Auto-allowed apps (background + safe) -> allow_apps
    for meta, cls, source in auto_allowed:
        bid = meta.bundle_id
        if bid not in final_allow and bid not in final_exclude:
            final_allow.append(bid)

    # Process visible groups, applying user overrides
    for group_key in ("blocked", "communication", "safe", "unclassified"):
        for meta, cls, source in groups.get(group_key, []):
            bid = meta.bundle_id

            # User override takes priority over group default
            if bid in overrides:
                if overrides[bid] == "block":
                    if bid not in final_exclude:
                        final_exclude.append(bid)
                    final_app_classes.pop(bid, None)
                    if bid in final_allow:
                        final_allow.remove(bid)
                else:  # "allow"
                    if bid not in final_allow and bid not in final_exclude:
                        final_allow.append(bid)
                continue

            # No override — use group default
            if group_key == "blocked":
                if bid not in final_exclude:
                    final_exclude.append(bid)
                final_app_classes.pop(bid, None)
                if bid in final_allow:
                    final_allow.remove(bid)
            elif group_key == "communication":
                if bid not in existing_ac and bid not in BUNDLE_ID_MAP:
                    final_app_classes[bid] = cls.value
                if bid in final_allow:
                    final_allow.remove(bid)
            elif group_key == "safe":
                if bid not in existing_ac and bid not in BUNDLE_ID_MAP:
                    final_app_classes[bid] = cls.value
                if bid not in final_allow and bid not in final_exclude:
                    final_allow.append(bid)
            elif group_key == "unclassified":
                if bid not in final_allow and bid not in final_exclude:
                    final_allow.append(bid)

    final_exclude.sort()
    final_allow.sort()

    # Save
    doc = _build_save_doc(doc, mode, final_exclude, final_allow, final_app_classes, upload_default)
    _save_config_atomic(config_path, doc)

    # Invalidate cache
    from screencap.config import invalidate_config_cache
    invalidate_config_cache()

    console.print(f"\n  [bold #22d3ee]\u2705 Privacy settings saved to {config_path}[/bold #22d3ee]")

    # Pre-download NLP models so first scrub doesn't block on network
    _download_nlp_models()

    console.print("  Run [bold #22d3ee]screencap start[/bold #22d3ee] to begin recording.")
    return True


def _download_nlp_models() -> None:
    """Download GLiNER + spaCy models if not already cached."""
    try:
        from screencap.privacy.pii import PiiDetector

        console.print("  Downloading privacy models (this may take a few minutes)...")
        PiiDetector()  # triggers HuggingFace download + spaCy model load
        console.print("  [bold #22d3ee]\u2705 Privacy models downloaded[/bold #22d3ee]")

        from screencap.privacy import _cleanup_stale_onnx_blobs
        try:
            _cleanup_stale_onnx_blobs()
        except Exception:
            pass  # cleanup is best-effort
    except Exception as e:
        console.print(
            f"  [yellow]Could not download privacy models: {e}[/yellow]\n"
            "  Models will download on first [bold]screencap scrub[/bold]."
        )


def show_current_config(config_path: Path | None = None) -> None:
    """Display current privacy classifications (read-only)."""
    if config_path is None:
        from screencap.config import _CONFIG_PATH
        config_path = _CONFIG_PATH

    if not config_path.exists():
        console.print("[dim]No privacy config found. Run 'screencap setup' first.[/dim]")
        return

    doc = _load_config_toml(config_path)
    privacy = doc.get("privacy", {})
    if not privacy:
        console.print("[dim]No [privacy] section in config. Run 'screencap setup' first.[/dim]")
        return

    mode = privacy.get("mode", "internal")
    console.print(f"\n[bold]Privacy mode:[/bold] {mode}")

    exclude_apps = privacy.get("exclude_apps", [])
    if exclude_apps:
        console.print(f"\n[bold]Excluded apps ({len(exclude_apps)}):[/bold]")
        for bid in exclude_apps:
            console.print(f"  {bid}")

    allow_apps = privacy.get("allow_apps", [])
    if allow_apps:
        console.print(f"\n[bold]Allowed apps ({len(allow_apps)}):[/bold]")
        for bid in allow_apps:
            console.print(f"  {bid}")

    app_classes = privacy.get("app_classes", {})
    if app_classes:
        table = Table(title="App Classifications")
        table.add_column("Bundle ID")
        table.add_column("Class")
        for bid, cls in sorted(app_classes.items()):
            table.add_row(bid, cls)
        console.print()
        console.print(table)


def reset_privacy_config(config_path: Path | None = None) -> bool:
    """Remove [privacy] section after confirmation."""
    if config_path is None:
        from screencap.config import _CONFIG_PATH
        config_path = _CONFIG_PATH

    if not config_path.exists():
        console.print("[dim]No config file found.[/dim]")
        return False

    doc = _load_config_toml(config_path)
    if "privacy" not in doc:
        console.print("[dim]No [privacy] section to remove.[/dim]")
        return False

    if not click.confirm("Remove all privacy settings?", default=False):
        console.print("[dim]Reset cancelled.[/dim]")
        return False

    del doc["privacy"]
    _save_config_atomic(config_path, doc)

    from screencap.config import invalidate_config_cache
    invalidate_config_cache()

    console.print("[green]Privacy settings removed.[/green]")
    return True
