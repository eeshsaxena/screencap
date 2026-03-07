"""Context association and classification for privacy v3.

Bridges screenshots to app/window/browser context via timestamp
correlation, then classifies the context for policy decisions.

Owns:
- screenshot timestamp parsing
- nearest-event lookup (bisect-based)
- bundle-ID → ContextClass mapping
- browser domain evidence
- title heuristic enrichment
- deterministic state machine with temporal hold/decay
"""

from __future__ import annotations

import bisect
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from screencap.privacy.policy import ContextClass, ContextResult, FrameMetadata


# ---------------------------------------------------------------------------
# Screenshot timestamp parsing
# ---------------------------------------------------------------------------


def parse_screenshot_timestamp(filename: str) -> float | None:
    """Extract Unix timestamp from a screenshot filename or path.

    Handles both bare filenames (``1709745600.123456.jpg``) and DB
    image_path values with a directory prefix (``screenshots/1709745600.123456.jpg``).
    Returns None if the name doesn't match the expected format.
    """
    # Strip directory prefix — image_path in the DB is "screenshots/{ts}.jpg"
    basename = filename.rsplit("/", 1)[-1] if "/" in filename else filename
    if basename.endswith(".jpg"):
        stem = basename[: basename.rfind(".jpg")]
    elif basename.endswith(".jpeg"):
        stem = basename[: basename.rfind(".jpeg")]
    else:
        return None
    try:
        return float(stem)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Nearest-event lookup
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowContext:
    """A window_event row relevant for context association."""

    timestamp: float
    app_bundle_id: str
    title: str
    window_id: str = ""


@dataclass(frozen=True)
class BrowserContext:
    """A browser_event row relevant for context association."""

    timestamp: float
    url: str
    domain: str


def _active_at_index(timestamps: list[float], target: float) -> int | None:
    """Return index of the latest timestamp at or before target.

    Window/browser events represent state transitions, so the active
    state at any time T is the most recent event with timestamp <= T.
    Returns None if no event is at or before target.
    """
    if not timestamps:
        return None
    pos = bisect.bisect_right(timestamps, target)
    if pos == 0:
        return None
    return pos - 1


def find_nearest_window(
    window_events: list[WindowContext],
    target_ts: float,
    max_delta: float = 5.0,
    _timestamps: list[float] | None = None,
) -> WindowContext | None:
    """Find the window_event active at target_ts within max_delta seconds.

    Uses "latest at or before" semantics since window events represent
    state transitions — the active window at time T is the most recent
    event with timestamp <= T.

    Pass _timestamps to avoid rebuilding the list on every call.
    """
    if not window_events:
        return None
    if _timestamps is None:
        _timestamps = [w.timestamp for w in window_events]
    idx = _active_at_index(_timestamps, target_ts)
    if idx is None:
        return None
    if abs(window_events[idx].timestamp - target_ts) > max_delta:
        return None
    return window_events[idx]


def find_nearest_browser(
    browser_events: list[BrowserContext],
    target_ts: float,
    max_delta: float = 5.0,
    _timestamps: list[float] | None = None,
) -> BrowserContext | None:
    """Find the browser_event active at target_ts within max_delta seconds.

    Uses "latest at or before" semantics since browser events represent
    state transitions.

    Pass _timestamps to avoid rebuilding the list on every call.
    """
    if not browser_events:
        return None
    if _timestamps is None:
        _timestamps = [b.timestamp for b in browser_events]
    idx = _active_at_index(_timestamps, target_ts)
    if idx is None:
        return None
    if abs(browser_events[idx].timestamp - target_ts) > max_delta:
        return None
    return browser_events[idx]


# ---------------------------------------------------------------------------
# DB loaders (raw sqlite3, consistent with screencap layer)
# ---------------------------------------------------------------------------


def load_window_events(db_path: Path) -> list[WindowContext]:
    """Load window_event rows sorted by timestamp."""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        tables = {r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "window_event" not in tables:
            return []
        cur.execute(
            "SELECT timestamp, app_bundle_id, title, window_id "
            "FROM window_event "
            "WHERE timestamp IS NOT NULL "
            "ORDER BY timestamp"
        )
        results = []
        for row in cur:
            results.append(WindowContext(
                timestamp=float(row[0]),
                app_bundle_id=row[1] or "",
                title=row[2] or "",
                window_id=row[3] or "",
            ))
        return results
    finally:
        conn.close()


def load_browser_events(db_path: Path) -> list[BrowserContext]:
    """Load browser_event rows sorted by timestamp, extracting URL/domain."""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        tables = {r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "browser_event" not in tables:
            return []
        cur.execute(
            "SELECT timestamp, message FROM browser_event "
            "WHERE timestamp IS NOT NULL "
            "ORDER BY timestamp"
        )
        results = []
        for row in cur:
            ts = float(row[0])
            message = row[1]
            url = _extract_url_from_message(message)
            if url:
                domain = _domain_from_url(url)
                results.append(BrowserContext(timestamp=ts, url=url, domain=domain))
        return results
    finally:
        conn.close()


def _extract_url_from_message(message) -> str:
    """Extract URL from a browser_event message (JSON column)."""
    import json

    if isinstance(message, str):
        try:
            message = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            return ""
    if isinstance(message, dict):
        return message.get("url", "")
    return ""


def _domain_from_url(url: str) -> str:
    """Extract the domain (hostname) from a URL."""
    try:
        parsed = urlparse(url)
        return parsed.hostname or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Known bundle ID → ContextClass mapping
# ---------------------------------------------------------------------------

# Verified macOS bundle IDs for well-known app categories.
BUNDLE_ID_MAP: dict[str, ContextClass] = {
    # Password managers
    "com.1password.1password": ContextClass.PASSWORD_MANAGER,
    "com.agilebits.onepassword7": ContextClass.PASSWORD_MANAGER,
    "com.lastpass.LastPass": ContextClass.PASSWORD_MANAGER,
    "com.bitwarden.desktop": ContextClass.PASSWORD_MANAGER,
    "com.dashlane.Dashlane": ContextClass.PASSWORD_MANAGER,
    "org.keepassxc.keepassxc": ContextClass.PASSWORD_MANAGER,
    "com.sinew.Enpass-Desktop": ContextClass.PASSWORD_MANAGER,
    "com.nordpass.NordPass": ContextClass.PASSWORD_MANAGER,
    "com.apple.Passwords": ContextClass.PASSWORD_MANAGER,
    # Banking / finance
    "com.robinhood.Robinhood": ContextClass.BANKING,
    "com.coinbase.Coinbase": ContextClass.BANKING,
    "com.wealthfront.wealthfront": ContextClass.BANKING,
    "com.personalcapital.pcap": ContextClass.BANKING,
    "com.intuit.quicken": ContextClass.BANKING,
    "com.coppernic.coppernic": ContextClass.BANKING,
    # Email
    "com.apple.mail": ContextClass.EMAIL,
    "com.microsoft.Outlook": ContextClass.EMAIL,
    "com.readdle.smartemail-macos": ContextClass.EMAIL,
    "com.freron.MailMate": ContextClass.EMAIL,
    "com.superhuman.electron": ContextClass.EMAIL,
    "com.mimestream.Mimestream": ContextClass.EMAIL,
    "it.bloop.airmail2": ContextClass.EMAIL,
    "com.postbox-inc.postbox": ContextClass.EMAIL,
    "com.canarymail.mac": ContextClass.EMAIL,
    "org.mozilla.thunderbird": ContextClass.EMAIL,
    # Chat / messaging
    "com.tinyspeck.slackmacgap": ContextClass.CHAT,
    "com.hnc.Discord": ContextClass.CHAT,
    "com.facebook.archon": ContextClass.CHAT,  # Messenger
    "ru.keepcoder.Telegram": ContextClass.CHAT,
    "net.whatsapp.WhatsApp": ContextClass.CHAT,
    "com.apple.MobileSMS": ContextClass.CHAT,  # Messages
    "com.microsoft.teams2": ContextClass.CHAT,
    "us.zoom.xos": ContextClass.CHAT,
    "com.skype.skype": ContextClass.CHAT,
    "org.whispersystems.signal-desktop": ContextClass.CHAT,
    "jp.naver.line.mac": ContextClass.CHAT,
    "com.viber.osx": ContextClass.CHAT,
    "com.tencent.xinWeChat": ContextClass.CHAT,
    "im.riot.app": ContextClass.CHAT,  # Element
    "com.beeper.beeper": ContextClass.CHAT,
    "com.cisco.webexmeetings": ContextClass.CHAT,  # Webex Teams/Messaging
    "com.google.chat": ContextClass.CHAT,
    "com.mattermost.desktop": ContextClass.CHAT,
    "com.wire.WireForOSX": ContextClass.CHAT,
    # Calendar
    "com.apple.iCal": ContextClass.CALENDAR,
    "com.flexibits.fantastical2.mac": ContextClass.CALENDAR,
    "com.busymac.busycal3": ContextClass.CALENDAR,
    "com.flexibits.fantastical": ContextClass.CALENDAR,
    # Video call (standalone video apps)
    "us.zoom.xos.meeting": ContextClass.VIDEO_CALL,
    "com.google.meet": ContextClass.VIDEO_CALL,
    "com.cisco.webex.meetingmanager": ContextClass.VIDEO_CALL,
    "com.logmein.GoToMeeting": ContextClass.VIDEO_CALL,
    "com.apple.FaceTime": ContextClass.VIDEO_CALL,
    # Code editors / terminals
    "com.microsoft.VSCode": ContextClass.CODE_EDITOR_TERMINAL,
    "com.apple.Terminal": ContextClass.CODE_EDITOR_TERMINAL,
    "com.googlecode.iterm2": ContextClass.CODE_EDITOR_TERMINAL,
    "co.zeit.hyper": ContextClass.CODE_EDITOR_TERMINAL,
    "dev.warp.Warp-Stable": ContextClass.CODE_EDITOR_TERMINAL,
    "com.jetbrains.intellij": ContextClass.CODE_EDITOR_TERMINAL,
    "com.jetbrains.pycharm": ContextClass.CODE_EDITOR_TERMINAL,
    "com.jetbrains.WebStorm": ContextClass.CODE_EDITOR_TERMINAL,
    "com.jetbrains.goland": ContextClass.CODE_EDITOR_TERMINAL,
    "com.jetbrains.CLion": ContextClass.CODE_EDITOR_TERMINAL,
    "com.jetbrains.rider": ContextClass.CODE_EDITOR_TERMINAL,
    "com.jetbrains.rubymine": ContextClass.CODE_EDITOR_TERMINAL,
    "com.jetbrains.datagrip": ContextClass.CODE_EDITOR_TERMINAL,
    "com.sublimetext.4": ContextClass.CODE_EDITOR_TERMINAL,
    "com.todesktop.230313mzl4w4u92": ContextClass.CODE_EDITOR_TERMINAL,  # Cursor
    "dev.zed.Zed": ContextClass.CODE_EDITOR_TERMINAL,
    "com.github.atom": ContextClass.CODE_EDITOR_TERMINAL,
    "com.panic.Nova": ContextClass.CODE_EDITOR_TERMINAL,
    "com.codeux.irc.textual5": ContextClass.CODE_EDITOR_TERMINAL,
    "io.alacritty": ContextClass.CODE_EDITOR_TERMINAL,
    "net.kovidgoyal.kitty": ContextClass.CODE_EDITOR_TERMINAL,
    "com.mitchellh.ghostty": ContextClass.CODE_EDITOR_TERMINAL,
    "com.github.nicegraphic.rio": ContextClass.CODE_EDITOR_TERMINAL,
    "com.barebones.bbedit": ContextClass.CODE_EDITOR_TERMINAL,
    "com.coteditor.CotEditor": ContextClass.CODE_EDITOR_TERMINAL,
    "com.macromates.TextMate": ContextClass.CODE_EDITOR_TERMINAL,
    "com.apple.dt.Xcode": ContextClass.CODE_EDITOR_TERMINAL,
    "com.neovide.neovide": ContextClass.CODE_EDITOR_TERMINAL,
    "org.gnu.Emacs": ContextClass.CODE_EDITOR_TERMINAL,
    "com.microsoft.VSCodeInsiders": ContextClass.CODE_EDITOR_TERMINAL,
    "com.sublimetext.3": ContextClass.CODE_EDITOR_TERMINAL,
    "com.sublimehq.Sublime-Merge": ContextClass.CODE_EDITOR_TERMINAL,
    # Admin consoles
    "com.amazon.awsvpnclient": ContextClass.ADMIN_CONSOLE,
    "com.pgadmin.pgadmin4": ContextClass.ADMIN_CONSOLE,
    "com.sequel-pro.sequel-pro": ContextClass.ADMIN_CONSOLE,
    "com.tableplus.TablePlus": ContextClass.ADMIN_CONSOLE,
}

# Known browser bundle IDs.
BROWSER_BUNDLE_IDS: frozenset[str] = frozenset({
    "com.apple.Safari",
    "com.google.Chrome",
    "org.mozilla.firefox",
    "com.microsoft.edgemac",
    "com.brave.Browser",
    "com.operasoftware.Opera",
    "com.vivaldi.Vivaldi",
    "company.thebrowser.Browser",  # Arc
    "org.chromium.Chromium",
    "com.nickvision.nicegx.nicegx",  # Orion
    "org.waterfoxproject.waterfox",
    "org.torproject.torbrowser",
})

# Domain → ContextClass for browser classification with verified domain.
_DOMAIN_CLASS_MAP: dict[str, ContextClass] = {
    # Email
    "mail.google.com": ContextClass.EMAIL,
    "outlook.live.com": ContextClass.EMAIL,
    "outlook.office.com": ContextClass.EMAIL,
    "outlook.office365.com": ContextClass.EMAIL,
    "mail.yahoo.com": ContextClass.EMAIL,
    "mail.proton.me": ContextClass.EMAIL,
    "app.fastmail.com": ContextClass.EMAIL,
    # Chat
    "app.slack.com": ContextClass.CHAT,
    "discord.com": ContextClass.CHAT,
    "web.whatsapp.com": ContextClass.CHAT,
    "web.telegram.org": ContextClass.CHAT,
    "teams.microsoft.com": ContextClass.CHAT,
    # Calendar
    "calendar.google.com": ContextClass.CALENDAR,
    # Video call
    "meet.google.com": ContextClass.VIDEO_CALL,
    "zoom.us": ContextClass.VIDEO_CALL,
    # Banking (US majors)
    "chase.com": ContextClass.BANKING,
    "secure.bankofamerica.com": ContextClass.BANKING,
    "online.citi.com": ContextClass.BANKING,
    "wellsfargo.com": ContextClass.BANKING,
    # Password managers
    "vault.bitwarden.com": ContextClass.PASSWORD_MANAGER,
    "my.1password.com": ContextClass.PASSWORD_MANAGER,
    # Admin consoles
    "console.aws.amazon.com": ContextClass.ADMIN_CONSOLE,
    "console.cloud.google.com": ContextClass.ADMIN_CONSOLE,
    "portal.azure.com": ContextClass.ADMIN_CONSOLE,
    "vercel.com": ContextClass.ADMIN_CONSOLE,
    "dashboard.heroku.com": ContextClass.ADMIN_CONSOLE,
    # Code
    "github.com": ContextClass.CODE_EDITOR_TERMINAL,
    "gitlab.com": ContextClass.CODE_EDITOR_TERMINAL,
}

# Title patterns for heuristic enrichment.
# These do NOT replace verified domain evidence — they provide a hint
# when no better signal is available.
_TITLE_HEURISTICS: list[tuple[re.Pattern[str], ContextClass]] = [
    (re.compile(r"(?i)\binbox\b"), ContextClass.EMAIL),
    (re.compile(r"(?i)\bmail\b"), ContextClass.EMAIL),
    (re.compile(r"(?i)\bgmail\b"), ContextClass.EMAIL),
    (re.compile(r"(?i)\bslack\b"), ContextClass.CHAT),
    (re.compile(r"(?i)\bdiscord\b"), ContextClass.CHAT),
    (re.compile(r"(?i)\bcalendar\b"), ContextClass.CALENDAR),
    (re.compile(r"(?i)\bmeeting\b"), ContextClass.VIDEO_CALL),
    (re.compile(r"(?i)\bzoom\b"), ContextClass.VIDEO_CALL),
    (re.compile(r"(?i)\b1password\b"), ContextClass.PASSWORD_MANAGER),
    (re.compile(r"(?i)\bbitwarden\b"), ContextClass.PASSWORD_MANAGER),
    (re.compile(r"(?i)\bbank\b"), ContextClass.BANKING),
]


def _classify_domain(domain: str) -> ContextClass | None:
    """Classify by exact domain match or parent-domain match."""
    domain = domain.lower()
    if domain in _DOMAIN_CLASS_MAP:
        return _DOMAIN_CLASS_MAP[domain]
    # Check parent domains (e.g., "app.slack.com" matches "slack.com")
    for known_domain, ctx_class in _DOMAIN_CLASS_MAP.items():
        if domain.endswith("." + known_domain):
            return ctx_class
    return None


def _classify_title(title: str) -> tuple[ContextClass, str] | None:
    """Classify by title heuristic. Returns (class, matched_pattern) or None."""
    for pattern, ctx_class in _TITLE_HEURISTICS:
        if pattern.search(title):
            return ctx_class, pattern.pattern
    return None


# ---------------------------------------------------------------------------
# Context classifier
# ---------------------------------------------------------------------------


class DefaultContextClassifier:
    """Deterministic context classifier.

    Classification priority:
    1. User config app_classes override → direct class
    2. Bundle ID in known app map → direct class
    3. Bundle ID is a known browser:
       a. With verified domain → domain-based class
       b. Without domain → browser_unverified
       c. With title heuristic (weaker signal) → heuristic class
    4. Title heuristic for non-browser apps
    5. unknown (explicit, never implicit fallthrough)
    """

    def __init__(
        self,
        app_classes: dict[str, "ContextClass"] | None = None,
    ) -> None:
        self._app_classes = app_classes or {}

    def classify(self, metadata: FrameMetadata) -> ContextResult:
        bundle_id = metadata.bundle_id
        domain = metadata.domain
        title = metadata.window_title

        # 1. User config override
        if bundle_id and bundle_id in self._app_classes:
            return ContextResult(
                context_class=self._app_classes[bundle_id],
                confidence="user_config",
                evidence=bundle_id,
            )

        # 2. Known app bundle ID
        if bundle_id and bundle_id in BUNDLE_ID_MAP:
            return ContextResult(
                context_class=BUNDLE_ID_MAP[bundle_id],
                confidence="bundle_id",
                evidence=bundle_id,
            )

        # 2. Known browser
        if bundle_id and bundle_id in BROWSER_BUNDLE_IDS:
            # 2a. Verified domain
            if domain:
                domain_class = _classify_domain(domain)
                if domain_class is not None:
                    return ContextResult(
                        context_class=domain_class,
                        confidence="domain",
                        evidence=domain,
                    )
                # Domain known but not in our map — still a verified browser
                # but we don't know the class, fall through to title or unverified

            # 2b. Title heuristic (weaker than domain)
            if title:
                title_result = _classify_title(title)
                if title_result is not None:
                    ctx_class, pattern = title_result
                    return ContextResult(
                        context_class=ctx_class,
                        confidence="title",
                        evidence=f"browser_title: {pattern}",
                    )

            # 2c. No domain evidence → browser_unverified
            return ContextResult(
                context_class=ContextClass.BROWSER_UNVERIFIED,
                confidence="bundle_id",
                evidence=f"browser_no_domain: {bundle_id}",
            )

        # 3. Unknown app — title heuristic
        if title:
            title_result = _classify_title(title)
            if title_result is not None:
                ctx_class, pattern = title_result
                return ContextResult(
                    context_class=ctx_class,
                    confidence="title",
                    evidence=f"title: {pattern}",
                )

        # 4. Explicit unknown
        return ContextResult(
            context_class=ContextClass.UNKNOWN,
            confidence="none",
            evidence="no_matching_signal",
        )



# ---------------------------------------------------------------------------
# High-level association: screenshot → FrameMetadata
# ---------------------------------------------------------------------------


def associate_screenshot(
    screenshot_ts: float,
    window_events: list[WindowContext],
    browser_events: list[BrowserContext],
    max_delta: float = 5.0,
    _window_timestamps: list[float] | None = None,
    _browser_timestamps: list[float] | None = None,
) -> FrameMetadata:
    """Build FrameMetadata for a screenshot by finding the active context.

    Uses "latest at or before" semantics: window/browser events are state
    transitions, so the active state at time T is the most recent event
    with timestamp <= T.

    Browser domain is only attached when the contemporaneous window is a
    known browser. This prevents stale browser domains from leaking into
    non-browser frames (e.g. after switching from Chrome to Finder), which
    would cause the policy evaluator's mask_domains check to misfire.

    Args:
        screenshot_ts: Unix timestamp of the screenshot.
        window_events: Pre-loaded, sorted window events.
        browser_events: Pre-loaded, sorted browser events.
        max_delta: Maximum time delta (seconds) for a valid association.
        _window_timestamps: Pre-computed window timestamps (avoids rebuilding per call).
        _browser_timestamps: Pre-computed browser timestamps (avoids rebuilding per call).

    Returns:
        FrameMetadata populated with best-available context.
    """
    window = find_nearest_window(
        window_events, screenshot_ts, max_delta, _timestamps=_window_timestamps
    )

    # Only look up browser domain when the window is a known browser.
    domain: str | None = None
    bundle_id = window.app_bundle_id if window else ""
    if bundle_id in BROWSER_BUNDLE_IDS:
        browser = find_nearest_browser(
            browser_events, screenshot_ts, max_delta, _timestamps=_browser_timestamps
        )
        domain = browser.domain if browser else None

    return FrameMetadata(
        bundle_id=bundle_id,
        window_title=window.title if window else "",
        domain=domain,
        timestamp=screenshot_ts,
    )
