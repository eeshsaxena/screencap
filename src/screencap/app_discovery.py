"""App discovery and auto-classification for privacy setup wizard.

Scans macOS for installed .app bundles, extracts metadata, and
auto-classifies them by privacy sensitivity using local signals only
(no network dependency).

Discovery strategy (ordered by speed):
1. Filesystem scan: /Applications + ~/Applications (<1s)
2. Spotlight enrichment: mdfind with 5s timeout (graceful fallback)

Classification priority (first match wins):
1. Bundle ID in known-apps DB (_BUNDLE_ID_MAP in context.py)
2. Apple sensitive-app overrides (com.apple.mail, etc.)
3. Apple bundle ID prefix (com.apple.* -> safe)
4. Naming pattern heuristics (Agent/Helper/IM/etc.)
5. Bundle ID / display name pattern rules (regex)
6. LSApplicationCategoryType from Info.plist
7. No match -> UNKNOWN
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
    is_background: bool = False


@dataclass(frozen=True)
class ClassificationResult:
    """Classification with provenance for wizard display."""

    context_class: ContextClass
    source: str  # "known_app", "apple_sensitive", "apple_prefix", "system_service",
                 # "input_method", "lifecycle", "decoration", "pattern_rule",
                 # "category_map", "category_safe", "unknown"


# ---------------------------------------------------------------------------
# Apple sensitive apps (overrides the generic com.apple.* -> safe rule)
# ---------------------------------------------------------------------------

_APPLE_SENSITIVE_APPS: dict[str, ContextClass] = {
    "com.apple.mail": ContextClass.EMAIL,
    "com.apple.MobileSMS": ContextClass.CHAT,
    "com.apple.Messages": ContextClass.CHAT,
    "com.apple.iCal": ContextClass.CALENDAR,
    "com.apple.CalendarAgent": ContextClass.CALENDAR,
    "com.apple.FaceTime": ContextClass.VIDEO_CALL,
    "com.apple.Passwords": ContextClass.PASSWORD_MANAGER,
}


# ---------------------------------------------------------------------------
# Naming pattern heuristics (Layer 4)
# ---------------------------------------------------------------------------

_SYSTEM_SERVICE_PATTERNS = re.compile(
    r"(?i)(Agent|Helper|UIServer|Service|Daemon|srv)$"
)
_INPUT_METHOD_PATTERNS = re.compile(
    r"(?i)(IM$|InputMethod|Typing|Kana|Romaji|Transliteration)"
)
_LIFECYCLE_PATTERNS = re.compile(
    r"(?i)(Onboarding|Setup|Installer|Updater|Update$|Migration|Rosetta)"
)
_DECORATION_PATTERNS = re.compile(
    r"(?i)(ScreenSaver|Wallpaper|Widget|TouchBar|Dock$)"
)


# ---------------------------------------------------------------------------
# Bundle ID / name pattern -> ContextClass (Layer 5)
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

# LSApplicationCategoryType -> ContextClass (Layer 6)
_CATEGORY_MAP: dict[str, ContextClass] = {
    "public.app-category.finance": ContextClass.BANKING,
    "public.app-category.wallet": ContextClass.BANKING,
    "public.app-category.social-networking": ContextClass.CHAT,
    "public.app-category.developer-tools": ContextClass.CODE_EDITOR_TERMINAL,
}

_SAFE_CATEGORIES: frozenset[str] = frozenset({
    "public.app-category.productivity",
    "public.app-category.entertainment",
    "public.app-category.music",
    "public.app-category.photography",
    "public.app-category.utilities",
    "public.app-category.education",
    "public.app-category.games",
    "public.app-category.graphics-design",
    "public.app-category.video",
    "public.app-category.news",
    "public.app-category.reference",
    "public.app-category.weather",
    "public.app-category.travel",
    "public.app-category.sports",
    "public.app-category.business",
    "public.app-category.lifestyle",
    "public.app-category.books",
    "public.app-category.food-and-drink",
})


# ---------------------------------------------------------------------------
# Background app detection
# ---------------------------------------------------------------------------


def _is_background_from_plist(app_path: str | Path) -> bool:
    """Check LSUIElement or LSBackgroundOnly in Info.plist."""
    plist_path = Path(app_path) / "Contents" / "Info.plist"
    if not plist_path.exists():
        return False
    try:
        with open(plist_path, "rb") as f:
            plist = plistlib.load(f)
    except Exception:
        return False
    return bool(plist.get("LSUIElement")) or bool(plist.get("LSBackgroundOnly"))


def is_background_app(metadata: AppMetadata) -> bool:
    """Check if an app is a background/agent app.

    Checks Info.plist keys (LSUIElement, LSBackgroundOnly) and
    naming patterns for system services that don't set these keys.
    """
    if metadata.is_background:
        return True
    # Naming pattern fallback for services without plist keys
    name = metadata.display_name
    if _SYSTEM_SERVICE_PATTERNS.search(name):
        return True
    if _INPUT_METHOD_PATTERNS.search(name):
        return True
    return False


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

    bg = bool(plist.get("LSUIElement")) or bool(plist.get("LSBackgroundOnly"))

    return AppMetadata(
        path=str(app_path),
        bundle_id=bundle_id,
        display_name=display_name,
        category=category,
        is_background=bg,
    )


# ---------------------------------------------------------------------------
# Auto-classification
# ---------------------------------------------------------------------------


def auto_classify_detailed(metadata: AppMetadata) -> ClassificationResult:
    """Classify an app using multi-layer local heuristics.

    Priority (first match wins):
    1. Known-apps DB (_BUNDLE_ID_MAP)
    2. Browser bundle IDs (BROWSER_BUNDLE_IDS)
    3. Apple sensitive-app overrides
    4. Apple bundle ID prefix (com.apple.* -> safe)
    5. Naming pattern heuristics
    6. Bundle ID / display name pattern rules
    7. LSApplicationCategoryType
    8. UNKNOWN
    """
    from screencap.privacy.context import BROWSER_BUNDLE_IDS, _BUNDLE_ID_MAP

    bid = metadata.bundle_id

    # Layer 1: Known-apps DB
    if bid in _BUNDLE_ID_MAP:
        return ClassificationResult(_BUNDLE_ID_MAP[bid], "known_app")

    # Layer 2: Browser bundle IDs
    if bid in BROWSER_BUNDLE_IDS:
        return ClassificationResult(ContextClass.BROWSER_UNVERIFIED, "known_browser")

    # Layer 3: Apple sensitive apps
    if bid in _APPLE_SENSITIVE_APPS:
        return ClassificationResult(_APPLE_SENSITIVE_APPS[bid], "apple_sensitive")

    # Layer 4: Safe bundle ID prefixes
    if bid.startswith("com.apple."):
        return ClassificationResult(ContextClass.UNKNOWN, "apple_prefix")
    if bid.startswith("org.python."):
        return ClassificationResult(ContextClass.UNKNOWN, "dev_runtime")
    # Chrome/Chromium PWAs are just web bookmarks, treat as safe
    if bid.startswith("com.google.Chrome.app."):
        return ClassificationResult(ContextClass.UNKNOWN, "browser_pwa")

    # Layer 5: Naming pattern heuristics
    name = metadata.display_name
    if _SYSTEM_SERVICE_PATTERNS.search(name):
        return ClassificationResult(ContextClass.UNKNOWN, "system_service")
    if _INPUT_METHOD_PATTERNS.search(name):
        return ClassificationResult(ContextClass.UNKNOWN, "input_method")
    if _LIFECYCLE_PATTERNS.search(name):
        return ClassificationResult(ContextClass.UNKNOWN, "lifecycle")
    if _DECORATION_PATTERNS.search(name):
        return ClassificationResult(ContextClass.UNKNOWN, "decoration")

    # Layer 6: Bundle ID patterns
    for pattern, ctx_class in _PATTERN_RULES:
        if pattern.search(bid):
            return ClassificationResult(ctx_class, "pattern_rule")

    # Layer 6b: Display name patterns
    for pattern, ctx_class in _PATTERN_RULES:
        if pattern.search(name):
            return ClassificationResult(ctx_class, "pattern_rule")

    # Layer 7: Info.plist category -> specific class
    if metadata.category and metadata.category in _CATEGORY_MAP:
        return ClassificationResult(_CATEGORY_MAP[metadata.category], "category_map")

    # Layer 7b: Info.plist category -> safe
    if metadata.category and metadata.category in _SAFE_CATEGORIES:
        return ClassificationResult(ContextClass.UNKNOWN, "category_safe")

    return ClassificationResult(ContextClass.UNKNOWN, "unknown")


def auto_classify(metadata: AppMetadata) -> ContextClass:
    """Classify an app using local signals only (backwards-compatible wrapper).

    Priority:
    1. Bundle ID pattern match
    2. Display name pattern match
    3. LSApplicationCategoryType
    4. UNKNOWN
    """
    # Preserve original behavior: only pattern rules + category map
    # (no apple prefix, no naming heuristics)
    for pattern, ctx_class in _PATTERN_RULES:
        if pattern.search(metadata.bundle_id):
            return ctx_class

    for pattern, ctx_class in _PATTERN_RULES:
        if pattern.search(metadata.display_name):
            return ctx_class

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


# Paths that contain system-internal apps (not user-visible).
# Spotlight returns apps from these locations but they are framework
# internals, not things users launch or care about.
_SPOTLIGHT_EXCLUDE_PREFIXES = (
    "/System/Library/",
    "/Library/Apple/",
    # Framework-embedded runtimes — not user-facing apps.
    # Python.app is the interpreter itself; blocking it would break
    # any Python-based tool (including ScreenCap).
    "/Library/Frameworks/",
    "/Library/Developer/",
)


def _scan_spotlight(timeout: float = 5.0) -> list[str]:
    """Use mdfind to discover all .app bundles via Spotlight index.

    Filters out system-internal apps from /System/Library/ and similar
    paths that aren't user-visible applications.

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
            and not any(line.startswith(p) for p in _SPOTLIGHT_EXCLUDE_PREFIXES)
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
        # Skip language runtimes — they're not user-facing apps.
        # org.python.* is the Python interpreter; blocking it would
        # break ScreenCap and any other Python tool.
        if meta.bundle_id.startswith("org.python."):
            continue
        seen_bundle_ids.add(meta.bundle_id)
        apps.append(meta)

    apps.sort(key=lambda a: a.display_name.lower())
    return apps
