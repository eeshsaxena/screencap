"""App discovery and auto-classification for privacy setup wizard.

Scans macOS for installed .app bundles, extracts metadata, and
auto-classifies them by privacy sensitivity using local signals only
(no network dependency).

Discovery strategy (ordered by speed):
1. Filesystem scan: /Applications + ~/Applications (<1s)
2. Spotlight enrichment: mdfind with 5s timeout (graceful fallback)

Classification priority (first match wins):
1. Bundle ID patterns (regex)
2. App name keyword matching
3. LSApplicationCategoryType from Info.plist
4. No match → UNKNOWN
"""

from __future__ import annotations

import logging
import os
import plistlib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from screencap.privacy.policy import ContextClass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AppMetadata:
    """Metadata extracted from an .app bundle."""

    path: str
    bundle_id: str
    display_name: str
    category: str = ""  # LSApplicationCategoryType


# ---------------------------------------------------------------------------
# Bundle ID / name pattern → ContextClass
# ---------------------------------------------------------------------------

_PATTERN_RULES: list[tuple[re.Pattern[str], ContextClass]] = [
    # Password managers
    (re.compile(r"(?i)(password|vault|keychain|1password|bitwarden|lastpass|dashlane)"), ContextClass.PASSWORD_MANAGER),
    # Banking / finance
    (re.compile(r"(?i)(bank|finance|trading|invest|fidelity|schwab|chase)"), ContextClass.BANKING),
    # Email
    (re.compile(r"(?i)(mail|outlook|spark|mimestream|superhuman)"), ContextClass.EMAIL),
    # Chat / messaging
    (re.compile(r"(?i)(slack|discord|telegram|whatsapp|signal|messenger|teams)"), ContextClass.CHAT),
    # Video call
    (re.compile(r"(?i)(zoom|meet|webex|facetime)"), ContextClass.VIDEO_CALL),
    # Code editors / terminals
    (re.compile(r"(?i)(terminal|iterm|warp|hyper|ghostty|alacritty|kitty)"), ContextClass.CODE_EDITOR_TERMINAL),
    (re.compile(r"(?i)(xcode|vscode|jetbrains|sublime|cursor|zed|nova)"), ContextClass.CODE_EDITOR_TERMINAL),
]

# LSApplicationCategoryType → ContextClass
_CATEGORY_MAP: dict[str, ContextClass] = {
    "public.app-category.finance": ContextClass.BANKING,
    "public.app-category.social-networking": ContextClass.CHAT,
    "public.app-category.developer-tools": ContextClass.CODE_EDITOR_TERMINAL,
}


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------


def get_app_metadata(app_path: str | Path) -> AppMetadata | None:
    """Extract metadata from a .app bundle's Info.plist.

    Returns None if the bundle has no valid Info.plist or no bundle ID.
    """
    plist_path = Path(app_path) / "Contents" / "Info.plist"
    if not plist_path.exists():
        return None

    try:
        with open(plist_path, "rb") as f:
            plist = plistlib.load(f)
    except Exception:
        logger.debug("Failed to read plist: %s", plist_path)
        return None

    bundle_id = plist.get("CFBundleIdentifier")
    if not bundle_id:
        logger.debug("No bundle ID in %s", app_path)
        return None

    display_name = (
        plist.get("CFBundleDisplayName")
        or plist.get("CFBundleName")
        or Path(app_path).stem
    )
    category = plist.get("LSApplicationCategoryType", "")

    return AppMetadata(
        path=str(app_path),
        bundle_id=bundle_id,
        display_name=display_name,
        category=category,
    )


# ---------------------------------------------------------------------------
# Auto-classification
# ---------------------------------------------------------------------------


def auto_classify(metadata: AppMetadata) -> ContextClass:
    """Classify an app using local signals only.

    Priority:
    1. Bundle ID pattern match
    2. Display name pattern match
    3. LSApplicationCategoryType
    4. UNKNOWN
    """
    # 1. Bundle ID patterns
    for pattern, ctx_class in _PATTERN_RULES:
        if pattern.search(metadata.bundle_id):
            return ctx_class

    # 2. Display name patterns
    for pattern, ctx_class in _PATTERN_RULES:
        if pattern.search(metadata.display_name):
            return ctx_class

    # 3. Info.plist category
    if metadata.category and metadata.category in _CATEGORY_MAP:
        return _CATEGORY_MAP[metadata.category]

    return ContextClass.UNKNOWN


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _scan_filesystem() -> list[str]:
    """Scan /Applications and ~/Applications for .app bundles."""
    dirs = [Path("/Applications")]
    home_apps = Path.home() / "Applications"
    if home_apps.is_dir():
        dirs.append(home_apps)

    app_paths: list[str] = []
    for base in dirs:
        if not base.is_dir():
            continue
        for entry in base.iterdir():
            if entry.suffix == ".app" and entry.is_dir():
                app_paths.append(str(entry))
            # One level deep (e.g., /Applications/Utilities/*.app)
            if entry.is_dir() and not entry.suffix:
                for sub in entry.iterdir():
                    if sub.suffix == ".app" and sub.is_dir():
                        app_paths.append(str(sub))
    return app_paths


def _scan_spotlight(timeout: float = 5.0) -> list[str]:
    """Use mdfind to discover all .app bundles via Spotlight index.

    Returns empty list on timeout or error.
    """
    try:
        result = subprocess.run(
            ["mdfind", "kMDItemContentType == 'com.apple.application-bundle'"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return []
        return [
            line for line in result.stdout.strip().split("\n")
            if line and line.endswith(".app")
        ]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        logger.debug("Spotlight scan failed or timed out")
        return []


def discover_installed_apps(
    use_spotlight: bool = True,
    spotlight_timeout: float = 5.0,
) -> list[AppMetadata]:
    """Discover installed macOS apps and extract their metadata.

    Runs filesystem scan first (fast), then enriches with Spotlight
    results. Deduplicates by bundle ID, keeping the first-seen path.

    Args:
        use_spotlight: Whether to attempt Spotlight enrichment.
        spotlight_timeout: Timeout in seconds for the mdfind call.

    Returns:
        List of AppMetadata, sorted by display name.
    """
    # Filesystem scan (always)
    paths = set(_scan_filesystem())

    # Spotlight enrichment (optional)
    if use_spotlight:
        spotlight_paths = _scan_spotlight(timeout=spotlight_timeout)
        paths.update(spotlight_paths)

    # Extract metadata, deduplicate by bundle ID
    seen_bundle_ids: set[str] = set()
    apps: list[AppMetadata] = []

    for app_path in sorted(paths):
        meta = get_app_metadata(app_path)
        if meta is None:
            continue
        if meta.bundle_id in seen_bundle_ids:
            continue
        seen_bundle_ids.add(meta.bundle_id)
        apps.append(meta)

    apps.sort(key=lambda a: a.display_name.lower())
    return apps
