"""Privacy setup wizard — interactive app classification and config persistence.

Called by `screencap setup`. Discovers installed apps, auto-classifies them,
presents grouped summary for user review, and saves to ~/.screencap/config.toml.

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
from screencap.privacy.context import _BUNDLE_ID_MAP
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


def _classify_with_overrides(
    apps: list[AppMetadata],
    existing_app_classes: dict[str, ContextClass] | None = None,
    existing_exclude_apps: frozenset[str] | None = None,
) -> dict[str, tuple[AppMetadata, ContextClass, str]]:
    """Classify all apps, respecting existing config and hardcoded map.

    Priority: existing config > auto_classify_detailed (which checks _BUNDLE_ID_MAP).

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


def _display_and_review(
    groups: dict[str, list[tuple[AppMetadata, ContextClass, str]]],
    auto_allowed_count: int,
) -> dict[str, ContextClass]:
    """Display classified apps and interactively review unclassified ones.

    Returns: dict of bundle_id -> ContextClass for user decisions on
    unclassified apps. UNKNOWN = allow, PASSWORD_MANAGER = block.
    """
    visible = sum(len(g) for g in groups.values())
    console.print(
        f"\nFound {visible} apps on your system. "
        "Here's how they've been classified:\n"
    )

    # Column width for alignment
    max_name = 24

    # Blocked group
    blocked = groups.get("blocked", [])
    if blocked:
        console.print("  [bold]Always blocked (sensitive):[/bold]")
        for meta, cls, _source in blocked:
            label = _CLASS_LABELS.get(cls, cls.value)
            name = meta.display_name[:max_name].ljust(max_name)
            console.print(f"    [red]×[/red] {name} [dim]({label})[/dim]")
        console.print()

    # Communication group
    comm = groups.get("communication", [])
    if comm:
        console.print("  [bold]Communication (masked in public mode):[/bold]")
        for meta, cls, _source in comm:
            label = _CLASS_LABELS.get(cls, cls.value)
            name = meta.display_name[:max_name].ljust(max_name)
            console.print(f"    [yellow]~[/yellow] {name} [dim]({label})[/dim]")
        console.print()

    # Safe / code group
    safe = groups.get("safe", [])
    if safe:
        console.print("  [bold]Safe (captured normally):[/bold]")
        for meta, cls, _source in safe:
            label = _CLASS_LABELS.get(cls, cls.value)
            name = meta.display_name[:max_name].ljust(max_name)
            console.print(f"    [green]✓[/green] {name} [dim]({label})[/dim]")
        console.print()

    if auto_allowed_count > 0:
        console.print(
            f"  [dim]({auto_allowed_count} system/utility apps auto-allowed, not shown)[/dim]\n"
        )

    # Unclassified — interactive
    unclassified = groups.get("unclassified", [])
    user_decisions: dict[str, ContextClass] = {}

    if unclassified:
        console.print(f"  [bold]Unclassified ({len(unclassified)} apps):[/bold]")

        for meta, cls, _source in unclassified:
            name = meta.display_name[:max_name].ljust(max_name)
            choice = click.prompt(
                f"    [blue]?[/blue] {name}",
                type=click.Choice(["a", "b", "s"], case_sensitive=False),
                default="a",
                prompt_suffix=" [a=allow / b=block / s=skip]: ",
                show_choices=False,
            )
            if choice.lower() == "b":
                user_decisions[meta.bundle_id] = ContextClass.PASSWORD_MANAGER
                console.print(f"      [red]→ blocked[/red]")
            elif choice.lower() == "a":
                user_decisions[meta.bundle_id] = ContextClass.UNKNOWN
                console.print(f"      [green]→ allowed[/green]")
            # skip: not added to decisions

        console.print()

    return user_decisions


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
) -> tomlkit.TOMLDocument:
    """Update the TOML document with privacy settings."""
    # Ensure [privacy] section exists
    if "privacy" not in doc:
        doc.add("privacy", tomlkit.table())

    privacy = doc["privacy"]
    privacy["mode"] = mode.value

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

    # Mode selection (skip in scan-only mode)
    if scan_only:
        try:
            mode = PrivacyMode(existing_mode_str)
        except ValueError:
            mode = PrivacyMode.INTERNAL
    else:
        console.print("\nWelcome to ScreenCap! Let's configure your privacy settings.\n")
        console.print("Privacy mode:")
        console.print("  1. Public   — strictest, for sharing publicly")
        console.print("  2. Internal — permissive, for personal use\n")
        mode_choice = click.prompt("  Choice", type=click.IntRange(1, 2), default=2)
        mode = PrivacyMode.PUBLIC if mode_choice == 1 else PrivacyMode.INTERNAL

    # Discover apps
    with console.status("Scanning installed apps..."):
        apps = discover_installed_apps(use_spotlight=True, spotlight_timeout=5.0)

    if not apps:
        console.print("[yellow]No apps found.[/yellow]")
        return False

    # Classify
    classified = _classify_with_overrides(apps, existing_ac, existing_exclude)

    # In scan-only mode, filter to only truly unknown new apps
    if scan_only:
        configured_bids = set(existing_ac.keys()) | existing_exclude | existing_allow
        new_classified = {
            bid: (meta, cls, source)
            for bid, (meta, cls, source) in classified.items()
            if bid not in configured_bids and source == "unknown"
        }
        if not new_classified:
            console.print("[green]All apps are already classified.[/green]")
            return False
        console.print(f"\nFound {len(new_classified)} new app(s).")
        classified = new_classified

    groups, auto_allowed = _group_apps(classified)

    # Display and review
    user_decisions = _display_and_review(groups, len(auto_allowed))

    # Build final config from groups
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

    # Process visible groups
    for meta, cls, source in groups.get("blocked", []):
        bid = meta.bundle_id
        if bid not in final_exclude:
            final_exclude.append(bid)
        final_app_classes.pop(bid, None)
        if bid in final_allow:
            final_allow.remove(bid)

    for meta, cls, source in groups.get("communication", []):
        bid = meta.bundle_id
        if bid not in existing_ac and bid not in _BUNDLE_ID_MAP:
            final_app_classes[bid] = cls.value
        if bid in final_allow:
            final_allow.remove(bid)

    for meta, cls, source in groups.get("safe", []):
        bid = meta.bundle_id
        if bid not in existing_ac and bid not in _BUNDLE_ID_MAP:
            final_app_classes[bid] = cls.value
        if bid in final_allow:
            final_allow.remove(bid)

    # Unclassified: apply user decisions
    for meta, cls, source in groups.get("unclassified", []):
        bid = meta.bundle_id
        if bid in user_decisions:
            decision = user_decisions[bid]
            if decision in _BLOCKED_CLASSES:
                if bid not in final_exclude:
                    final_exclude.append(bid)
                final_app_classes.pop(bid, None)
                if bid in final_allow:
                    final_allow.remove(bid)
            else:
                # allowed
                if bid not in final_allow and bid not in final_exclude:
                    final_allow.append(bid)
        else:
            # skipped — still add to allow_apps (user saw it, didn't block)
            if bid not in final_allow and bid not in final_exclude:
                final_allow.append(bid)

    final_exclude.sort()
    final_allow.sort()

    # Save
    if not click.confirm("\n  Save these preferences?", default=True):
        console.print("  [dim]Setup cancelled, no changes saved.[/dim]")
        return False

    doc = _build_save_doc(doc, mode, final_exclude, final_allow, final_app_classes)
    _save_config_atomic(config_path, doc)

    # Invalidate cache
    from screencap.config import invalidate_config_cache
    invalidate_config_cache()

    console.print(f"\n  [green]Privacy settings saved to {config_path}[/green]")
    console.print("  Run 'screencap start' to begin recording.")
    return True


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
