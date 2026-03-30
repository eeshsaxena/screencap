"""Context association and classification for privacy v3.

Bridges screenshots to app/window context via timestamp
correlation, then classifies the context for policy decisions.

Owns:
- screenshot timestamp parsing
- nearest-event lookup (bisect-based)
- bundle-ID → ContextClass mapping
- domain evidence (via domain field on FrameMetadata)
- title heuristic enrichment
- deterministic state machine with temporal hold/decay
"""

from __future__ import annotations

import bisect
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from screencap.privacy.actions import stricter
from screencap.privacy.domain_loader import _is_tld_like
from screencap.privacy.policy import (
    ContextClass,
    ContextResult,
    FrameMetadata,
    PrivacyMode,
    get_matrix_action,
)




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
    domain: str | None = None
    browser_url: str | None = None


def _active_at_index(timestamps: list[float], target: float) -> int | None:
    """Return index of the latest timestamp at or before target.

    Window events represent state transitions, so the active
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


# ---------------------------------------------------------------------------
# DB loaders (raw sqlite3, consistent with screencap layer)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowGeometrySnapshot:
    """Per-screenshot window geometry with display context."""

    windows: list[dict]
    display_origin: tuple[float, float] = (0.0, 0.0)


# Cache table existence per connection to avoid repeated sqlite_master queries.
# Keyed by id(connection). Safe because the table set never changes during a
# scrub, and the cache is small (one entry per open connection).
_geometry_table_cache: dict[int, bool] = {}


def load_window_geometry(
    db_path: Path,
    screenshot_timestamp: float,
    conn: sqlite3.Connection | None = None,
) -> WindowGeometrySnapshot | None:
    """Load the window geometry snapshot for a screenshot timestamp.

    Queries the ``window_geometry`` table for an exact timestamp match.
    Returns a ``WindowGeometrySnapshot`` or ``None`` if unavailable
    (old recordings without the table, capture failure, etc.).

    Args:
        db_path: Path to the recording database.
        screenshot_timestamp: Exact timestamp to look up.
        conn: Optional open connection to reuse (avoids per-call overhead
            when loading geometry for many screenshots in a loop).
    """
    own_conn = conn is None
    if own_conn:
        conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        # Check table existence once per connection (graceful for old recordings).
        # The table set never changes during a scrub, so cache the result.
        conn_id = id(conn)
        if conn_id not in _geometry_table_cache:
            tables = {r[0] for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            _geometry_table_cache[conn_id] = "window_geometry" in tables
        if not _geometry_table_cache[conn_id]:
            return None

        # Use tolerance-based lookup: screenshot filenames lose float
        # precision (e.g. 1773413585.910469 vs DB 1773413585.9104693).
        # 1ms tolerance is safely within a single screenshot interval.
        cur.execute(
            "SELECT window_list_json FROM window_geometry "
            "WHERE abs(screenshot_timestamp - ?) < 0.001 "
            "ORDER BY abs(screenshot_timestamp - ?) LIMIT 1",
            (screenshot_timestamp, screenshot_timestamp),
        )
        row = cur.fetchone()
        if row is None or row[0] is None:
            return None

        data = json.loads(row[0])

        # Handle both formats:
        # New: {"windows": [...], "display_bounds": [x, y, w, h]}
        # Legacy: [...] (plain list of window dicts)
        if isinstance(data, dict):
            windows = data.get("windows", [])
            bounds = data.get("display_bounds")
            origin = (float(bounds[0]), float(bounds[1])) if bounds else (0.0, 0.0)
        else:
            windows = data
            origin = (0.0, 0.0)

        return WindowGeometrySnapshot(windows=windows, display_origin=origin)
    except (sqlite3.OperationalError, json.JSONDecodeError):
        return None
    finally:
        if own_conn:
            conn.close()


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

        # Check if browser_url column exists (backward compat with old DBs)
        we_cols = {
            r[1] for r in cur.execute("PRAGMA table_info(window_event)").fetchall()
        }
        has_browser_url = "browser_url" in we_cols

        if has_browser_url:
            cur.execute(
                "SELECT timestamp, app_bundle_id, title, window_id, browser_url "
                "FROM window_event "
                "WHERE timestamp IS NOT NULL "
                "ORDER BY timestamp"
            )
        else:
            cur.execute(
                "SELECT timestamp, app_bundle_id, title, window_id "
                "FROM window_event "
                "WHERE timestamp IS NOT NULL "
                "ORDER BY timestamp"
            )
        results = []
        for row in cur:
            domain = None
            raw_url = row[4] if has_browser_url else None
            if raw_url:
                domain = domain_from_url(raw_url)
            results.append(WindowContext(
                timestamp=float(row[0]),
                app_bundle_id=row[1] or "",
                title=row[2] or "",
                window_id=row[3] or "",
                domain=domain,
                browser_url=raw_url or None,
            ))
        return results
    finally:
        conn.close()


def domain_from_url(url: str) -> str | None:
    """Extract the domain (hostname) from a URL.

    Returns None if the URL cannot be parsed or has no hostname.
    """
    try:
        parsed = urlparse(url)
        return parsed.hostname or None
    except ValueError:
        return None



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
    # File managers
    "com.apple.finder": ContextClass.CODE_EDITOR_TERMINAL,
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
    # Admin consoles / infrastructure
    "com.electron.dockerdesktop": ContextClass.ADMIN_CONSOLE,
    "io.tailscale.ipn.macsys": ContextClass.ADMIN_CONSOLE,
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
    (re.compile(r"(?i)\blogin\b"), ContextClass.AUTH_FLOW),
    (re.compile(r"(?i)\bsign.in\b"), ContextClass.AUTH_FLOW),
    (re.compile(r"(?i)\bpasskey\b"), ContextClass.AUTH_FLOW),
    (re.compile(r"(?i)\bpassword\b"), ContextClass.AUTH_FLOW),
    (re.compile(r"(?i)\b(?:two.factor|2fa|mfa)\b"), ContextClass.AUTH_FLOW),
]


# ---------------------------------------------------------------------------
# Keyword sets for auth/payment flow detection (Phase 4)
# ---------------------------------------------------------------------------

_AUTH_KEYWORDS: frozenset[str] = frozenset({
    "login", "signin", "sign-in", "auth", "authenticate",
    "oauth", "sso", "mfa", "2fa", "verify", "recovery",
    "forgot-password", "reset-password",
})

_PAYMENT_KEYWORDS: frozenset[str] = frozenset({
    "checkout", "billing", "payment", "pay", "subscribe",
})

# Delimiters for tokenizing URL path segments and subdomain labels.
_SEGMENT_SPLIT_RE = re.compile(r"[-_.]")


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
    """Deterministic context classifier with hybrid domain + keyword layers.

    Classification priority:
    1. User config app_classes override → direct class
    2. Bundle ID in known app map → direct class
    3. Bundle ID is a known browser:
       a. Domain lookup in merged index (UT1 + supplement)
       b. Keyword detection on URL path/subdomain (auth/payment flows)
       c. When both match → stricter action wins
       d. Title heuristic (weaker signal)
       e. No domain evidence → browser_unverified
    4. Title heuristic for non-browser apps
    5. unknown (explicit, never implicit fallthrough)
    """

    def __init__(
        self,
        app_classes: dict[str, ContextClass] | None = None,
        domain_index: dict[str, ContextClass] | None = None,
    ) -> None:
        self._app_classes = app_classes or {}
        if domain_index is not None:
            self._domain_index = domain_index
        else:
            from screencap.privacy.domain_loader import build_domain_index
            self._domain_index = build_domain_index()

    def _classify_domain(self, domain: str) -> ContextClass | None:
        """O(1) domain lookup in the merged index."""
        domain = domain.lower().rstrip(".")
        if domain in self._domain_index:
            return self._domain_index[domain]
        # Walk up parent domains for cases not covered by pre-expansion
        labels = domain.split(".")
        for i in range(1, len(labels) - 1):
            parent = ".".join(labels[i:])
            if _is_tld_like(parent):
                continue
            if parent in self._domain_index:
                return self._domain_index[parent]
        return None

    def _detect_keyword_flow(
        self, url_path: str, hostname: str
    ) -> ContextClass | None:
        """Check first URL path segment and subdomain labels for auth/payment keywords."""
        # Check first path segment
        if url_path and url_path != "/":
            # Split path, take first segment after leading /
            segments = url_path.lstrip("/").split("/", 1)
            if segments:
                first_seg = unquote(segments[0]).lower()
                # Check full segment first (e.g. "forgot-password" as a whole)
                # then check tokenized parts (e.g. "login-callback" → "login")
                candidates = {first_seg} | set(_SEGMENT_SPLIT_RE.split(first_seg))
                if candidates & _AUTH_KEYWORDS:
                    return ContextClass.AUTH_FLOW
                if candidates & _PAYMENT_KEYWORDS:
                    return ContextClass.PAYMENT_FLOW

        # Check subdomain labels
        if hostname:
            labels = hostname.lower().split(".")
            # Strip the last 2 labels (registered domain + TLD) as a heuristic.
            # For two-part TLDs like co.uk, strip 3.
            suffix_len = 2
            if len(labels) >= 3:
                last_two = labels[-2]
                if last_two in ("co", "com", "org", "net", "ac", "gov", "edu"):
                    suffix_len = 3
            subdomain_labels = labels[: max(0, len(labels) - suffix_len)]
            for label in subdomain_labels:
                if label in _AUTH_KEYWORDS:
                    return ContextClass.AUTH_FLOW
                if label in _PAYMENT_KEYWORDS:
                    return ContextClass.PAYMENT_FLOW

        return None

    def _pick_stricter(
        self,
        class_a: ContextClass,
        class_b: ContextClass,
        mode: PrivacyMode = PrivacyMode.PUBLIC,
    ) -> ContextClass:
        """Return whichever ContextClass produces the stricter action."""
        action_a = get_matrix_action(class_a, mode)
        action_b = get_matrix_action(class_b, mode)
        return class_a if stricter(action_a, action_b) == action_a else class_b

    def classify(self, metadata: FrameMetadata) -> ContextResult:
        bundle_id = metadata.bundle_id
        domain = metadata.domain
        title = metadata.window_title

        # 1. User config override (non-browser apps only — browsers need
        #    domain/keyword/title refinement in step 3)
        if bundle_id and bundle_id in self._app_classes:
            cfg_class = self._app_classes[bundle_id]
            if cfg_class != ContextClass.BROWSER_UNVERIFIED:
                return ContextResult(
                    context_class=cfg_class,
                    confidence="user_config",
                    evidence=bundle_id,
                )
            # BROWSER_UNVERIFIED → fall through to step 3 for refinement

        # 2. Known app bundle ID
        if bundle_id and bundle_id in BUNDLE_ID_MAP:
            return ContextResult(
                context_class=BUNDLE_ID_MAP[bundle_id],
                confidence="bundle_id",
                evidence=bundle_id,
            )

        # 3. Known browser (hardcoded set OR user-config tagged as browser_unverified)
        is_browser = bundle_id and (
            bundle_id in BROWSER_BUNDLE_IDS
            or self._app_classes.get(bundle_id) == ContextClass.BROWSER_UNVERIFIED
        )
        if is_browser:
            if domain:
                # Extract URL path from browser_url if available
                url_path = ""
                hostname = domain
                fragment_path = ""
                if metadata.browser_url:
                    try:
                        parsed = urlparse(metadata.browser_url)
                        url_path = parsed.path or ""
                        hostname = parsed.hostname or domain
                        # SPA hash-routing: /#/login or /app#/login
                        if parsed.fragment.startswith("/"):
                            fragment_path = parsed.fragment
                    except ValueError:
                        pass

                domain_class = self._classify_domain(domain)
                keyword_class = self._detect_keyword_flow(url_path, hostname)
                # Fall back to hash-route fragment if path had no keyword
                if keyword_class is None and fragment_path:
                    keyword_class = self._detect_keyword_flow(
                        fragment_path, hostname
                    )

                if domain_class is not None and keyword_class is not None:
                    winner = self._pick_stricter(domain_class, keyword_class)
                    return ContextResult(
                        context_class=winner,
                        confidence="domain+keyword",
                        evidence=f"{domain} (stricter of {domain_class.value}, {keyword_class.value})",
                    )
                if domain_class is not None:
                    return ContextResult(
                        context_class=domain_class,
                        confidence="domain",
                        evidence=domain,
                    )
                if keyword_class is not None:
                    return ContextResult(
                        context_class=keyword_class,
                        confidence="keyword",
                        evidence=f"keyword:{domain}",
                    )

            # 3d. Title heuristic (weaker than domain)
            if title:
                title_result = _classify_title(title)
                if title_result is not None:
                    ctx_class, pattern = title_result
                    return ContextResult(
                        context_class=ctx_class,
                        confidence="title",
                        evidence=f"browser_title: {pattern}",
                    )

            # 3e. No domain evidence → browser_unverified
            return ContextResult(
                context_class=ContextClass.BROWSER_UNVERIFIED,
                confidence="bundle_id",
                evidence=f"browser_no_domain: {bundle_id}",
            )

        # 4. Unknown app — title heuristic
        if title:
            title_result = _classify_title(title)
            if title_result is not None:
                ctx_class, pattern = title_result
                return ContextResult(
                    context_class=ctx_class,
                    confidence="title",
                    evidence=f"title: {pattern}",
                )

        # 5. Explicit unknown
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
    max_delta: float = 5.0,
    _window_timestamps: list[float] | None = None,
) -> FrameMetadata:
    """Build FrameMetadata for a screenshot by finding the active context.

    Uses "latest at or before" semantics: window events are state
    transitions, so the active state at time T is the most recent event
    with timestamp <= T.

    Args:
        screenshot_ts: Unix timestamp of the screenshot.
        window_events: Pre-loaded, sorted window events.
        max_delta: Maximum time delta (seconds) for a valid association.
        _window_timestamps: Pre-computed window timestamps (avoids rebuilding per call).

    Returns:
        FrameMetadata populated with best-available context.
    """
    window = find_nearest_window(
        window_events, screenshot_ts, max_delta, _timestamps=_window_timestamps
    )

    bundle_id = window.app_bundle_id if window else ""

    return FrameMetadata(
        bundle_id=bundle_id,
        window_title=window.title if window else "",
        domain=window.domain if window else None,
        timestamp=screenshot_ts,
        browser_url=window.browser_url if window else None,
    )
