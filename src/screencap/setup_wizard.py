"""Privacy setup wizard — interactive app classification and config persistence.

Called by `screencap setup`. Discovers installed apps, auto-classifies them,
presents results for user review, and saves to ~/.screencap/config.toml.

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

from screencap.app_discovery import AppMetadata, auto_classify, discover_installed_apps
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
_CODE_CLASSES = frozenset({ContextClass.CODE_EDITOR_TERMINAL})

# Human-friendly group labels
_GROUP_LABELS = {
    "blocked": "Blocked (never captured)",
    "communication": "Communication (masked in public mode)",
    "code": "Code / Terminal (captured normally)",
    "unclassified": "Unclassified",
}


def _classify_with_overrides(
    apps: list[AppMetadata],
    existing_app_classes: dict[str, ContextClass] | None = None,
    existing_exclude_apps: frozenset[str] | None = None,
) -> dict[str, tuple[AppMetadata, ContextClass]]:
    """Classify all apps, respecting existing config and hardcoded map.

    Priority: existing config > hardcoded _BUNDLE_ID_MAP > auto_classify.

    Returns: dict of bundle_id -> (metadata, context_class)
    """
    existing_app_classes = existing_app_classes or {}
    existing_exclude_apps = existing_exclude_apps or frozenset()
    result: dict[str, tuple[AppMetadata, ContextClass]] = {}

    for app in apps:
        bid = app.bundle_id

        # Existing config takes priority
        if bid in existing_app_classes:
            result[bid] = (app, existing_app_classes[bid])
        elif bid in existing_exclude_apps:
            result[bid] = (app, ContextClass.PASSWORD_MANAGER)  # treat as blocked
        elif bid in _BUNDLE_ID_MAP:
            result[bid] = (app, _BUNDLE_ID_MAP[bid])
        else:
            result[bid] = (app, auto_classify(app))

    return result


def _group_apps(
    classified: dict[str, tuple[AppMetadata, ContextClass]],
) -> dict[str, list[tuple[AppMetadata, ContextClass]]]:
    """Group classified apps into display categories."""
    groups: dict[str, list[tuple[AppMetadata, ContextClass]]] = {
        "blocked": [],
        "communication": [],
        "code": [],
        "unclassified": [],
    }
    for _bid, (meta, cls) in classified.items():
        if cls in _BLOCKED_CLASSES:
            groups["blocked"].append((meta, cls))
        elif cls in _COMM_CLASSES:
            groups["communication"].append((meta, cls))
        elif cls in _CODE_CLASSES:
            groups["code"].append((meta, cls))
        elif cls == ContextClass.UNKNOWN:
            groups["unclassified"].append((meta, cls))
        else:
            # Other classes (admin_console, browser_unverified, etc.)
            # group with code/captured-normally
            groups["code"].append((meta, cls))

    for group in groups.values():
        group.sort(key=lambda x: x[0].display_name.lower())

    return groups


def _print_summary(groups: dict[str, list[tuple[AppMetadata, ContextClass]]]) -> None:
    """Print classification summary by group."""
    for key, label in _GROUP_LABELS.items():
        apps = groups.get(key, [])
        if not apps:
            continue
        names = ", ".join(a.display_name for a, _ in apps)
        console.print(f"\n  [bold]{label} ({len(apps)} apps):[/bold]")
        console.print(f"    {names}")


def _review_unclassified(
    unclassified: list[tuple[AppMetadata, ContextClass]],
) -> dict[str, ContextClass | None]:
    """Interactive review of unclassified apps.

    Returns: dict of bundle_id -> new ContextClass, or None for skipped.
    """
    if not unclassified:
        return {}

    console.print(f"\n  [bold]Unclassified ({len(unclassified)} apps):[/bold]")
    choice = click.prompt(
        "  Review unclassified apps?",
        type=click.Choice(["y", "n", "allow-all", "block-all"], case_sensitive=False),
        default="y",
    )

    if choice == "n":
        return {}
    if choice == "allow-all":
        return {meta.bundle_id: ContextClass.UNKNOWN for meta, _ in unclassified}
    if choice == "block-all":
        return {meta.bundle_id: ContextClass.PASSWORD_MANAGER for meta, _ in unclassified}

    # Individual review in batches of 10
    result: dict[str, ContextClass | None] = {}
    batch_size = 10
    for i in range(0, len(unclassified), batch_size):
        batch = unclassified[i : i + batch_size]
        for meta, _cls in batch:
            resp = click.prompt(
                f"    {meta.display_name:30s}",
                type=click.Choice(["1", "2", "s"], case_sensitive=False),
                default="s",
                prompt_suffix=" [1=allow  2=block  s=skip]: ",
            )
            if resp == "1":
                result[meta.bundle_id] = ContextClass.UNKNOWN
            elif resp == "2":
                result[meta.bundle_id] = ContextClass.PASSWORD_MANAGER
            else:
                result[meta.bundle_id] = None  # skipped

        remaining = len(unclassified) - (i + len(batch))
        if remaining > 0:
            if not click.confirm(f"\n    {remaining} more. Continue?", default=True):
                break

    return result


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

    if app_classes:
        # Use inline table or regular table
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

    # In scan-only mode, filter to only new (unconfigured) apps
    if scan_only:
        configured_bids = set(existing_ac.keys()) | existing_exclude | set(_BUNDLE_ID_MAP.keys())
        new_classified = {
            bid: (meta, cls)
            for bid, (meta, cls) in classified.items()
            if bid not in configured_bids
        }
        if not new_classified:
            console.print("[green]All apps are already classified.[/green]")
            return False
        console.print(f"\nFound {len(new_classified)} new app(s).")
        classified = new_classified

    groups = _group_apps(classified)

    console.print(f"\nFound {len(classified)} apps. Classification summary:")
    _print_summary(groups)

    # Review unclassified
    user_decisions = _review_unclassified(groups.get("unclassified", []))

    # Build final app_classes and exclude_apps
    final_exclude: list[str] = sorted(existing_exclude)
    final_app_classes: dict[str, str] = dict(
        (bid, cls.value) for bid, cls in existing_ac.items()
    )

    # Apply auto-classification results (non-unknown, non-hardcoded)
    for bid, (meta, cls) in classified.items():
        if bid in existing_ac or bid in existing_exclude or bid in _BUNDLE_ID_MAP:
            continue
        if cls == ContextClass.UNKNOWN and bid not in user_decisions:
            continue
        if cls in _BLOCKED_CLASSES:
            if bid not in final_exclude:
                final_exclude.append(bid)
        elif cls != ContextClass.UNKNOWN:
            final_app_classes[bid] = cls.value

    # Apply user decisions
    for bid, decision in user_decisions.items():
        if decision is None:
            continue  # skipped
        if decision in _BLOCKED_CLASSES:
            if bid not in final_exclude:
                final_exclude.append(bid)
            final_app_classes.pop(bid, None)
        else:
            final_app_classes[bid] = decision.value
            if bid in final_exclude:
                final_exclude.remove(bid)

    final_exclude.sort()

    # Count stats
    auto_count = sum(
        1 for bid, (_, cls) in classified.items()
        if cls != ContextClass.UNKNOWN
        and bid not in existing_ac
        and bid not in existing_exclude
    )
    review_count = sum(1 for d in user_decisions.values() if d is not None)
    skip_count = sum(1 for d in user_decisions.values() if d is None)
    console.print(
        f"\n  {auto_count} apps classified automatically, "
        f"{review_count} reviewed manually, {skip_count} skipped."
    )

    # Save
    if not click.confirm("\n  Save to config?", default=True):
        console.print("  [dim]Setup cancelled, no changes saved.[/dim]")
        return False

    doc = _build_save_doc(doc, mode, final_exclude, final_app_classes)
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
