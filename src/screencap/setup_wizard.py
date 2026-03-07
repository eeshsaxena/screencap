"""Privacy setup wizard — interactive app classification and config persistence.

Called by `screencap setup`. Discovers installed apps, auto-classifies them,
presents grouped summary for user review (approve-by-exception), and saves
to ~/.screencap/config.toml.

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

# Human-friendly group labels (only groups shown to the user)
_GROUP_LABELS = {
    "blocked": "Blocked",
    "communication": "Communication",
    "code": "Code & Terminals",
    "needs_review": "Needs review",
}

# Short target names for the edit command
_MOVE_TARGETS = {
    "block": "blocked",
    "allow": "needs_review",  # move to needs_review = implicitly allowed on accept
    "comm": "communication",
    "code": "code",
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
        "code": [],
        "needs_review": [],
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
            groups["code"].append((meta, cls, source))
        elif source == "unknown":
            groups["needs_review"].append((meta, cls, source))
        else:
            # known_app, apple_sensitive, pattern_rule, category_map
            # that don't map to a specific sensitive class -> auto-allow
            auto_allowed.append((meta, cls, source))

    for group in groups.values():
        group.sort(key=lambda x: x[0].display_name.lower())

    return groups, auto_allowed


def _print_grouped_summary(
    groups: dict[str, list[tuple[AppMetadata, ContextClass, str]]],
    auto_allowed_count: int,
) -> None:
    """Print grouped classification summary."""
    visible = sum(len(g) for g in groups.values())
    console.print(f"\n{visible} apps to review:\n")

    for key, label in _GROUP_LABELS.items():
        apps = groups.get(key, [])
        if not apps:
            continue
        names = [a.display_name for a, _, _ in apps]
        preview = ", ".join(names[:5])
        if len(names) > 5:
            preview += f"... (+{len(names) - 5} more)"
        console.print(f"  [bold]{label} ({len(apps)} apps):[/bold] {preview}")

    if auto_allowed_count > 0:
        console.print(
            f"\n  [dim]({auto_allowed_count} safe apps auto-allowed, not shown)[/dim]"
        )


def _approve_or_edit(
    groups: dict[str, list[tuple[AppMetadata, ContextClass, str]]],
    auto_allowed_count: int,
) -> dict[str, list[tuple[AppMetadata, ContextClass, str]]]:
    """Approve-by-exception editing loop.

    Returns updated groups after user edits.
    """
    while True:
        _print_grouped_summary(groups, auto_allowed_count)

        console.print()
        choice = click.prompt(
            "Looks good?",
            type=click.Choice(["Y", "e"], case_sensitive=False),
            default="Y",
            prompt_suffix=" [Y to accept, e to edit]: ",
        )

        if choice.lower() == "y":
            return groups

        # Edit mode: show group menu
        group_keys = [k for k in _GROUP_LABELS if groups.get(k)]
        if not group_keys:
            console.print("  [dim]No groups to edit.[/dim]")
            return groups

        console.print("\nEdit groups:")
        for i, key in enumerate(group_keys, 1):
            label = _GROUP_LABELS[key]
            count = len(groups[key])
            console.print(f"  {i}. {label} ({count} apps)")

        group_choice = click.prompt(
            "\nSelect group",
            type=click.IntRange(1, len(group_keys)),
            default=None,
            prompt_suffix=f" [1-{len(group_keys)}, q to go back]: ",
        )

        selected_key = group_keys[group_choice - 1]
        _edit_group(groups, selected_key)


def _edit_group(
    groups: dict[str, list[tuple[AppMetadata, ContextClass, str]]],
    group_key: str,
) -> None:
    """Let user move apps between groups within a selected group."""
    while True:
        apps = groups[group_key]
        label = _GROUP_LABELS[group_key]
        console.print(f"\n{label} ({len(apps)} apps):")
        for i, (meta, cls, source) in enumerate(apps, 1):
            console.print(f"  {i}. {meta.display_name}")

        console.print(
            "\n  [dim]Enter '<number> <target>' to move (e.g. '5 block'), "
            "or 'q' to go back.[/dim]"
        )
        console.print(
            "  [dim]Targets: block, allow, comm, code[/dim]"
        )

        raw = click.prompt("  >", default="q", prompt_suffix=" ")
        if raw.strip().lower() == "q":
            return

        parts = raw.strip().split(None, 1)
        if len(parts) != 2:
            console.print("  [red]Invalid. Use '<number> <target>', e.g. '5 block'[/red]")
            continue

        try:
            idx = int(parts[0])
        except ValueError:
            console.print("  [red]Invalid number.[/red]")
            continue

        if idx < 1 or idx > len(apps):
            console.print(f"  [red]Number must be 1-{len(apps)}.[/red]")
            continue

        target = parts[1].lower()
        if target not in _MOVE_TARGETS:
            console.print(f"  [red]Invalid target. Use: {', '.join(_MOVE_TARGETS)}[/red]")
            continue

        target_key = _MOVE_TARGETS[target]
        if target_key == group_key:
            console.print("  [dim]Already in that group.[/dim]")
            continue

        # Move the app
        moved = apps.pop(idx - 1)
        meta, old_cls, source = moved

        # Assign appropriate ContextClass for the target group
        if target_key == "blocked":
            new_cls = ContextClass.PASSWORD_MANAGER
        elif target_key == "communication":
            new_cls = ContextClass.CHAT
        elif target_key == "code":
            new_cls = ContextClass.CODE_EDITOR_TERMINAL
        else:  # needs_review (allowed on accept)
            new_cls = ContextClass.UNKNOWN

        groups[target_key].append((meta, new_cls, "user_edit"))
        groups[target_key].sort(key=lambda x: x[0].display_name.lower())
        console.print(
            f"  Moved '{meta.display_name}' to {_GROUP_LABELS[target_key]}"
        )


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
        console.print("\nWelcome to ScreenCap privacy setup.\n")
        console.print("Privacy mode determines how aggressively apps are protected:")
        console.print("  1. Public   — strictest, blocks sensitive apps, masks communication")
        console.print("  2. Internal — permissive, captures most apps normally\n")
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

    # Approve-or-edit loop
    groups = _approve_or_edit(groups, len(auto_allowed))

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

    for meta, cls, source in groups.get("code", []):
        bid = meta.bundle_id
        if bid not in existing_ac and bid not in _BUNDLE_ID_MAP:
            final_app_classes[bid] = cls.value
        if bid in final_allow:
            final_allow.remove(bid)

    # needs_review apps are implicitly accepted as safe (user saw them and hit Y)
    for meta, cls, source in groups.get("needs_review", []):
        bid = meta.bundle_id
        if bid not in final_allow and bid not in final_exclude:
            final_allow.append(bid)

    final_exclude.sort()
    final_allow.sort()

    # Save
    if not click.confirm("\n  Save to config?", default=True):
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
