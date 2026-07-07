"""Source-agnostic activity-summary builder for session segmentation.

Lifted from the Cloud Run processor (``scripts/process-recording/main.py``),
with the one structural change that makes it reusable off-cloud: the
event/transcript *source* is injected (``ActivitySource``) rather than
hard-wired to GCS. The cloud processor passes a GCS-backed source; a local
caller passes a filesystem / in-memory source.

The module is deliberately cloud-free — no ``google.cloud`` / ``storage`` /
``genai`` imports — so it stays importable inside the daemon. The app-category
classification tables are inlined here (the same "Cloud Run doesn't have the
screencap pkg" copies that lived in the script), keeping the package
self-contained.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Iterable, Protocol, Union

log = logging.getLogger(__name__)

MAX_ACTIVITY_ENTRIES = 200  # cap activity timeline entries for LLM context

# A ``blocked_source`` (see ``build_activity_summary``) is either an already-built
# ``is_blocked(ts) -> bool`` predicate, or a recording directory ``Path`` from
# which one is derived over the recording's local ``recording.db``.
BlockedPredicate = Callable[[float], bool]
BlockedSource = Union[BlockedPredicate, Path, str]


# ---------------------------------------------------------------------------
# Injected source of events + transcripts (replaces hard-wired GCS reads)
# ---------------------------------------------------------------------------

class ActivitySource(Protocol):
    """The event/transcript source ``build_activity_summary`` reads from.

    Implementations decide where the bytes come from — GCS (cloud processor),
    the local filesystem / ``recording.db`` (local pipeline), or an in-memory
    fixture (tests). The builder itself never touches storage directly.
    """

    def iter_events(self) -> Iterable[dict]:
        """Yield parsed event dicts in chunk order (``_meta`` rows excluded)."""
        ...

    def read_transcript(self, chunk_index: int) -> dict | None:
        """Return the parsed transcript dict for a chunk, or ``None`` if absent.

        Implementations own decode/JSON-parse error handling and return
        ``None`` on any failure, matching the cloud processor's
        skip-on-error behavior.
        """
        ...


# ---------------------------------------------------------------------------
# Relative-time formatting
# ---------------------------------------------------------------------------

def _format_relative_time(seconds: float) -> str:
    """Format seconds as H:MM:SS relative timestamp."""
    h, rem = divmod(int(max(0, seconds)), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _duration_human(secs: float) -> str:
    h, rem = divmod(int(secs), 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# App category classification (inlined — the package is screencap-pkg-free by
# rule, and the cloud processor historically kept its own copies too).
# Synced from screencap.privacy.classify BUNDLE_ID_MAP + BROWSER_BUNDLE_IDS.
# ---------------------------------------------------------------------------

_BUNDLE_CATEGORY: dict[str, str] = {
    # CODE — editors, terminals, IDEs, DB tools
    "com.microsoft.VSCode": "CODE",
    "com.microsoft.VSCodeInsiders": "CODE",
    "com.apple.Terminal": "CODE",
    "com.googlecode.iterm2": "CODE",
    "co.zeit.hyper": "CODE",
    "dev.warp.Warp-Stable": "CODE",
    "com.mitchellh.ghostty": "CODE",
    "io.alacritty": "CODE",
    "net.kovidgoyal.kitty": "CODE",
    "com.github.nicegraphic.rio": "CODE",
    "com.jetbrains.intellij": "CODE",
    "com.jetbrains.pycharm": "CODE",
    "com.jetbrains.WebStorm": "CODE",
    "com.jetbrains.goland": "CODE",
    "com.jetbrains.CLion": "CODE",
    "com.jetbrains.rider": "CODE",
    "com.jetbrains.rubymine": "CODE",
    "com.jetbrains.datagrip": "CODE",
    "com.sublimetext.4": "CODE",
    "com.sublimetext.3": "CODE",
    "com.sublimehq.Sublime-Merge": "CODE",
    "com.todesktop.230313mzl4w4u92": "CODE",  # Cursor
    "dev.zed.Zed": "CODE",
    "com.github.atom": "CODE",
    "com.panic.Nova": "CODE",
    "com.barebones.bbedit": "CODE",
    "com.coteditor.CotEditor": "CODE",
    "com.macromates.TextMate": "CODE",
    "com.apple.dt.Xcode": "CODE",
    "com.neovide.neovide": "CODE",
    "org.gnu.Emacs": "CODE",
    "com.codeux.irc.textual5": "CODE",
    "abnerworks.Typora": "CODE",
    "com.github.Electron": "CODE",
    # CODE — admin/DB consoles
    "com.amazon.awsvpnclient": "CODE",
    "com.pgadmin.pgadmin4": "CODE",
    "com.sequel-pro.sequel-pro": "CODE",
    "com.tableplus.TablePlus": "CODE",
    # BROWSER
    "com.apple.Safari": "BROWSER",
    "com.google.Chrome": "BROWSER",
    "org.mozilla.firefox": "BROWSER",
    "com.brave.Browser": "BROWSER",
    "com.operasoftware.Opera": "BROWSER",
    "company.thebrowser.Browser": "BROWSER",  # Arc
    "org.chromium.Chromium": "BROWSER",
    "com.microsoft.edgemac": "BROWSER",
    "com.vivaldi.Vivaldi": "BROWSER",
    "com.nickvision.nicegx.nicegx": "BROWSER",  # Orion
    "org.waterfoxproject.waterfox": "BROWSER",
    "org.torproject.torbrowser": "BROWSER",
    # CHAT — messaging, video calls, calendar
    "com.tinyspeck.slackmacgap": "CHAT",
    "com.hnc.Discord": "CHAT",
    "com.facebook.archon": "CHAT",  # Messenger
    "ru.keepcoder.Telegram": "CHAT",
    "net.whatsapp.WhatsApp": "CHAT",
    "com.apple.MobileSMS": "CHAT",  # Messages
    "com.microsoft.teams2": "CHAT",
    "us.zoom.xos": "CHAT",
    "com.skype.skype": "CHAT",
    "org.whispersystems.signal-desktop": "CHAT",
    "jp.naver.line.mac": "CHAT",
    "com.viber.osx": "CHAT",
    "com.tencent.xinWeChat": "CHAT",
    "im.riot.app": "CHAT",  # Element
    "com.beeper.beeper": "CHAT",
    "com.cisco.webexmeetings": "CHAT",
    "com.google.chat": "CHAT",
    "com.mattermost.desktop": "CHAT",
    "com.wire.WireForOSX": "CHAT",
    # CHAT — video calls
    "us.zoom.xos.meeting": "CHAT",
    "com.google.meet": "CHAT",
    "com.cisco.webex.meetingmanager": "CHAT",
    "com.logmein.GoToMeeting": "CHAT",
    "com.apple.FaceTime": "CHAT",
    # CHAT — calendar
    "com.apple.iCal": "CHAT",
    "com.flexibits.fantastical2.mac": "CHAT",
    "com.flexibits.fantastical": "CHAT",
    "com.busymac.busycal3": "CHAT",
    # EMAIL
    "com.apple.mail": "EMAIL",
    "com.microsoft.Outlook": "EMAIL",
    "com.readdle.smartemail-macos": "EMAIL",
    "com.freron.MailMate": "EMAIL",
    "com.superhuman.electron": "EMAIL",
    "com.mimestream.Mimestream": "EMAIL",
    "it.bloop.airmail2": "EMAIL",
    "com.postbox-inc.postbox": "EMAIL",
    "com.canarymail.mac": "EMAIL",
    "org.mozilla.thunderbird": "EMAIL",
    # DOCS
    "com.apple.iWork.Pages": "DOCS",
    "com.apple.iWork.Numbers": "DOCS",
    "com.apple.iWork.Keynote": "DOCS",
    "com.microsoft.Word": "DOCS",
    "com.microsoft.Excel": "DOCS",
    "com.microsoft.Powerpoint": "DOCS",
    "md.obsidian": "DOCS",
    "com.craft.craft": "DOCS",
    "com.electron.logseq": "DOCS",
    # DESIGN
    "com.figma.Desktop": "DESIGN",
    "com.bohemiancoding.sketch3": "DESIGN",
    # MEDIA
    "com.apple.Music": "MEDIA",
    "com.spotify.client": "MEDIA",
    "com.apple.QuickTimePlayerX": "MEDIA",
    # SYSTEM
    "com.apple.finder": "SYSTEM",
    "com.apple.systempreferences": "SYSTEM",
    "com.apple.ActivityMonitor": "SYSTEM",
}

_DOMAIN_CATEGORY: dict[str, str] = {
    # CODE
    "github.com": "CODE",
    "gitlab.com": "CODE",
    "stackoverflow.com": "CODE",
    "bitbucket.org": "CODE",
    "codepen.io": "CODE",
    "replit.com": "CODE",
    "codesandbox.io": "CODE",
    "jsfiddle.net": "CODE",
    "npmjs.com": "CODE",
    "pypi.org": "CODE",
    "crates.io": "CODE",
    "pkg.go.dev": "CODE",
    "rubygems.org": "CODE",
    "hub.docker.com": "CODE",
    "vercel.com": "CODE",
    "netlify.com": "CODE",
    "heroku.com": "CODE",
    "railway.app": "CODE",
    "console.cloud.google.com": "CODE",
    "console.aws.amazon.com": "CODE",
    "portal.azure.com": "CODE",
    # EMAIL
    "mail.google.com": "EMAIL",
    "outlook.live.com": "EMAIL",
    "outlook.office.com": "EMAIL",
    "outlook.office365.com": "EMAIL",
    "mail.yahoo.com": "EMAIL",
    "mail.proton.me": "EMAIL",
    "app.fastmail.com": "EMAIL",
    # CHAT
    "slack.com": "CHAT",
    "app.slack.com": "CHAT",
    "discord.com": "CHAT",
    "teams.microsoft.com": "CHAT",
    "web.whatsapp.com": "CHAT",
    "web.telegram.org": "CHAT",
    "meet.google.com": "CHAT",
    "zoom.us": "CHAT",
    # DOCS
    "docs.google.com": "DOCS",
    "sheets.google.com": "DOCS",
    "slides.google.com": "DOCS",
    "notion.so": "DOCS",
    "www.notion.so": "DOCS",
    "coda.io": "DOCS",
    "airtable.com": "DOCS",
    "linear.app": "DOCS",
    "clickup.com": "DOCS",
    "asana.com": "DOCS",
    "trello.com": "DOCS",
    "jira.atlassian.com": "DOCS",
    "confluence.atlassian.com": "DOCS",
    # DESIGN
    "figma.com": "DESIGN",
    "www.figma.com": "DESIGN",
    "canva.com": "DESIGN",
    "www.canva.com": "DESIGN",
    "dribbble.com": "DESIGN",
    # MEDIA
    "youtube.com": "MEDIA",
    "www.youtube.com": "MEDIA",
    "open.spotify.com": "MEDIA",
    "music.apple.com": "MEDIA",
    "soundcloud.com": "MEDIA",
    "twitch.tv": "MEDIA",
    "www.twitch.tv": "MEDIA",
    "netflix.com": "MEDIA",
    # SOCIAL
    "twitter.com": "SOCIAL",
    "x.com": "SOCIAL",
    "linkedin.com": "SOCIAL",
    "www.linkedin.com": "SOCIAL",
    "reddit.com": "SOCIAL",
    "www.reddit.com": "SOCIAL",
    "facebook.com": "SOCIAL",
    "www.facebook.com": "SOCIAL",
    "instagram.com": "SOCIAL",
    "www.instagram.com": "SOCIAL",
    "news.ycombinator.com": "SOCIAL",
}

# Bundle-ID prefix heuristics for apps not in the static map.
_BUNDLE_PREFIX_CATEGORY: list[tuple[str, str]] = [
    ("com.jetbrains.", "CODE"),
    ("com.sublimetext.", "CODE"),
    ("com.sublimehq.", "CODE"),
    ("com.apple.dt.", "CODE"),       # Xcode tools (Instruments, etc.)
    ("com.apple.iWork.", "DOCS"),
    ("com.microsoft.Word", "DOCS"),
    ("com.microsoft.Excel", "DOCS"),
    ("com.microsoft.Powerpoint", "DOCS"),
]


def _classify_app(bundle_id: str, title: str = "", domain: str = "") -> str:
    """Classify an app into a category from bundle ID, title, or domain."""
    # 1. Exact bundle ID match
    if bundle_id in _BUNDLE_CATEGORY:
        return _BUNDLE_CATEGORY[bundle_id]
    # 2. Bundle ID prefix heuristics
    for prefix, cat in _BUNDLE_PREFIX_CATEGORY:
        if bundle_id.startswith(prefix):
            return cat
    # 3. Windows exe name classification
    exe_lower = bundle_id.lower()
    if exe_lower.endswith(".exe"):
        exe_name = exe_lower[:-4]
        _EXE_CATEGORY = {
            "code": "CODE", "vscode": "CODE", "cursor": "CODE",
            "cmd": "CODE", "powershell": "CODE", "windowsterminal": "CODE",
            "wt": "CODE", "python": "CODE", "python3": "CODE",
            "node": "CODE", "git-bash": "CODE", "mintty": "CODE",
            "devenv": "CODE",  # Visual Studio
            "notepad": "DOCS", "notepad++": "CODE", "wordpad": "DOCS",
            "chrome": "BROWSER", "firefox": "BROWSER", "msedge": "BROWSER",
            "brave": "BROWSER", "opera": "BROWSER", "vivaldi": "BROWSER",
            "slack": "CHAT", "discord": "CHAT", "teams": "CHAT",
            "zoom": "CHAT", "outlook": "EMAIL",
            "explorer": "SYSTEM", "taskmgr": "SYSTEM",
            "winword": "DOCS", "excel": "DOCS", "powerpnt": "DOCS",
            "screencap": "CODE",
        }
        if exe_name in _EXE_CATEGORY:
            return _EXE_CATEGORY[exe_name]
    # 4. Domain match
    if domain:
        if domain in _DOMAIN_CATEGORY:
            return _DOMAIN_CATEGORY[domain]
        bare = domain.removeprefix("www.")
        if bare in _DOMAIN_CATEGORY:
            return _DOMAIN_CATEGORY[bare]
    return "OTHER"


def _app_name_short(bundle_id: str) -> str:
    """Extract short, human-readable app name from bundle ID or exe name."""
    if not bundle_id:
        return "Unknown"
    # Windows exe names: "Notepad.exe", "screencap.exe", "python.exe"
    if bundle_id.lower().endswith(".exe"):
        return bundle_id[:-4]
    # macOS bundle IDs: "com.mitchellh.ghostty" → "Ghostty"
    parts = bundle_id.split(".")
    name = parts[-1] if len(parts) >= 3 else bundle_id
    # Known display names for common apps
    _DISPLAY_NAMES = {
        "ghostty": "Ghostty",
        "VSCode": "VS Code",
        "VSCodeInsiders": "VS Code Insiders",
        "Terminal": "Terminal",
        "iterm2": "iTerm",
        "Chrome": "Chrome",
        "Safari": "Safari",
        "firefox": "Firefox",
        "slackmacgap": "Slack",
        "Discord": "Discord",
        "finder": "Finder",
        "mail": "Mail",
        "Outlook": "Outlook",
        "teams2": "Teams",
    }
    return _DISPLAY_NAMES.get(name, name.replace("-", " ").title())


# ---------------------------------------------------------------------------
# Privacy strip (R11 / U3) — the single chokepoint before ANY model
# ---------------------------------------------------------------------------
#
# This is the ONE place masked/blocked content is stripped from the activity
# summary before it reaches a provider (local on-device OR cloud). Because the
# strip runs at the event-consumption boundary of ``build_activity_summary`` —
# the single chokepoint where the summary is built (U1) — every derived
# structure (``entries``, ``timeline``, ``time_map``, the raw-fallback lists,
# and transcript snippets) inherits it, so no consumer or future code path can
# bypass it.
#
# The activity summary is TEXT ONLY — timeline entries carry app bundle IDs,
# window titles, action counts, and timestamps; transcript snippets carry text.
# It never carries frame/image bytes or references, so stripping the blocked
# TIMESTAMP ranges removes the sensitive content wholesale.
#
# ALLOW-only + fail-closed: mirrors ``frame_blocked.build_is_blocked``. Blocked
# geometry is re-derived from the recording's intact local ``recording.db`` via
# ``backfill.skip_intervals.derive_skip_intervals(require_canonical=True)`` (the
# same reader ``frame.nearest`` and the backfill use, so the strip cannot drift
# from capture-time block semantics — KTD4). On a missing/unreadable/partial DB
# read — or any failure building the privacy machinery — the predicate flags
# EVERY timestamp as blocked, so an ambiguous read excludes rather than leaks.


def _always_blocked(_ts: float) -> bool:
    """Fail-closed sentinel predicate: every timestamp is treated as blocked."""
    return True


def _never_blocked(_ts: float) -> bool:
    """Allow-all predicate: the default when no ``blocked_source`` is given."""
    return False


def _resolve_blocked_predicate(
    blocked_source: BlockedSource,
    window: tuple[float, float],
    strip_tss: list[float],
) -> BlockedPredicate:
    """Return an ``is_blocked(ts) -> bool`` predicate from ``blocked_source``.

    * A callable ``blocked_source`` is used directly (test / caller override, or
      a pre-built predicate).
    * A ``Path``/``str`` is treated as a recording directory: blocked intervals
      are re-derived from its local ``recording.db`` over ``window`` (the
      recording's ``[session_start, session_end)``), then the scrubber's
      ``find_blocked_interval`` membership test wraps them.

    ``strip_tss`` are the exact timestamps the strip will test — the activity
    summary's own event/transcript timestamps. They are forwarded to
    ``derive_skip_intervals`` as ``screenshot_timestamps`` so the fail-closed
    **uncovered-gap** and **orphan-screenshot** residual protects precisely the
    timestamps being stripped: a timestamp with no covering ``window_event``
    (its rows were retroactively deleted) is treated as blocked, exactly as
    ``frame_blocked.build_is_blocked`` protects the frame timestamps. Without
    this, an event before the first surviving window would silently under-block.

    Fail-closed on ANY error (missing/unreadable/partial ``recording.db``, a
    ``require_canonical`` raise, or a failure building the privacy machinery):
    returns ``_always_blocked`` so an ambiguous read excludes rather than leaks.
    Heavy imports are deferred to the call so this module stays light.
    """
    if callable(blocked_source):
        return blocked_source

    recording_dir = Path(blocked_source)
    if not strip_tss:
        # No timestamped content → the predicate is never consulted; an allow-all
        # keeps the contract total without forcing an all-blocked strip.
        return _never_blocked

    try:
        from screencap.backfill.skip_intervals import (
            build_classifier_evaluator,
            derive_skip_intervals,
        )
        from screencap.scrubber import find_blocked_interval

        db_path = recording_dir / "recording.db"
        classifier, evaluator = build_classifier_evaluator(recording_dir)
        intervals = derive_skip_intervals(
            db_path,
            classifier=classifier,
            evaluator=evaluator,
            time_range=window,
            # Forward the exact strip timestamps so the uncovered-gap / orphan
            # residual protects them (fail-closed) — an event before the first
            # surviving window would otherwise slip through unblocked.
            screenshot_timestamps=list(strip_tss),
            # require_canonical makes derive_skip_intervals RAISE on a partial
            # canonical read rather than silently returning the under-blocked
            # set — the except below maps that to the all-blocked sentinel
            # (fail-closed, SCR-198), matching frame_blocked.build_is_blocked.
            require_canonical=True,
        )
        starts = [iv.start for iv in intervals]

        def is_blocked(ts: float) -> bool:
            return find_blocked_interval(ts, intervals, starts) is not None

        return is_blocked
    except Exception:
        # Deliberately broad: privacy fail-closed MUST win over surfacing the
        # error, so ANY failure (the CanonicalDerivationError partial-read signal
        # from require_canonical=True, but also an unexpected programming error)
        # maps to _always_blocked. The heavy skip_intervals/scrubber import stays
        # inside the try on purpose — importing it at module top would defeat this
        # module's light import surface. Logged at ERROR (not warning) so a
        # systematic failure or a programming bug still surfaces loudly rather
        # than hiding behind a silently-all-blocked recording.
        log.error(
            "activity-summary privacy strip: blocked-interval derivation failed "
            "for %s; failing closed (all content treated as blocked)",
            recording_dir.name,
            exc_info=True,
        )
        return _always_blocked


# ---------------------------------------------------------------------------
# Activity summary derivation
# ---------------------------------------------------------------------------

def build_activity_summary(
    recording_name: str,
    manifests: list[dict],
    source: ActivitySource,
    *,
    blocked_source: BlockedSource | None = None,
) -> dict | None:
    """Build a compact activity summary from an injected event/transcript source.

    Stream-consumes events from ``source.iter_events()`` and pulls per-chunk
    transcripts via ``source.read_transcript()``. Returns a dict with keys:
    summary, entries, time_map, session_start, session_end (plus raw fallback
    data). Returns ``None`` if no window activity exists.

    Source-agnostic: this is the single chokepoint where the activity summary
    is built, so every consumer (cloud, on-device, future) reads only what the
    injected source yields. GCS-specific reads live in the caller's source.

    ``blocked_source`` (R11 / U3 privacy strip — the ONE place stripping happens,
    so every consumer inherits it): OPT-IN and defaults to ``None`` (no strip).
    When ``None``, output is byte-identical to the pre-strip cloud path — the
    cloud caller passes nothing (its uploaded events are already scrubbed and it
    has no local ``recording.db``), so cloud behavior is preserved. When
    provided, any event OR transcript segment whose timestamp falls inside a
    blocked/masked interval is dropped BEFORE it reaches ``entries`` / the
    timeline / ``time_map`` / the raw-fallback lists / transcript snippets, so
    the summary handed to ANY model is ALLOW-only. ``blocked_source`` is either
    an already-built ``is_blocked(ts) -> bool`` predicate, or a recording-dir
    ``Path`` from which one is re-derived over ``[session_start, session_end)``
    (fail-closed: an ambiguous/partial read excludes rather than leaks). The
    LOCAL segmentation stage (U4) passes the recording's local dir here.
    """
    session_start = min(m["chunk_start"] for m in manifests)
    session_end = max(m["chunk_end"] for m in manifests)

    # Resolve the privacy-strip predicate ONCE (R11 / U3). Defaults to allow-all
    # when no ``blocked_source`` is given, so the cloud path stays byte-identical
    # (single pass over the source, no materialization) and cloud behavior is
    # preserved. When a strip IS requested we materialize the events up front so
    # the derived predicate can be handed the exact timestamps it will test
    # (needed for the fail-closed uncovered-gap / orphan residual — see
    # ``_resolve_blocked_predicate``); the cloud path never pays this cost.
    sorted_manifests = sorted(manifests, key=lambda m: m["chunk_index"])
    if blocked_source is None:
        is_blocked: BlockedPredicate = _never_blocked
        events_iter: Iterable[dict] = source.iter_events()
    else:
        events_list = list(source.iter_events())
        # The exact timestamps the strip will test: every event timestamp plus
        # every transcript-segment absolute timestamp. Forwarded so the residual
        # protects precisely these, ALLOW-only + fail-closed.
        strip_tss: list[float] = [
            e.get("timestamp", 0) for e in events_list if e.get("timestamp", 0)
        ]
        for manifest in sorted_manifests:
            transcript = source.read_transcript(manifest["chunk_index"])
            if transcript is None:
                continue
            chunk_start = manifest["chunk_start"]
            for seg in transcript.get("segments", [])[:20]:
                strip_tss.append(chunk_start + seg.get("start", 0))
        is_blocked = _resolve_blocked_predicate(
            blocked_source, (session_start, session_end), strip_tss,
        )
        events_iter = events_list

    entries: list[dict] = []
    current_entry: dict | None = None
    # Also collect raw data for fallback segmentation (avoids re-reading source)
    raw_timestamps: list[float] = []
    raw_window_events: list[dict] = []

    for evt in events_iter:
        evt_type = evt.get("type", "")
        ts = evt.get("timestamp", 0)

        # Privacy strip (R11): drop any event inside a blocked/masked interval
        # BEFORE it contributes to any derived structure. ALLOW-only; fail-closed
        # via the predicate. This is the single chokepoint — no consumer can
        # bypass it. A falsy/zero timestamp cannot be range-checked, so it is
        # left to the existing ``ts > 0`` gates below (an event with no usable
        # timestamp carries no locatable content to strip).
        if ts and is_blocked(ts):
            continue

        # Collect raw data for fallback
        if ts > 0 and evt_type != "mouse.move":
            raw_timestamps.append(ts)
        if evt_type == "window.switch":
            raw_window_events.append({
                "timestamp": ts,
                "bundle_id": evt.get("app_bundle_id", ""),
                "title": evt.get("window_title", ""),
            })

        if evt_type == "window.switch":
            if current_entry is not None:
                current_entry["end_ts"] = ts
                entries.append(current_entry)

            bundle_id = evt.get("app_bundle_id", "")
            title = evt.get("window_title", "")
            domain = evt.get("domain", "") or ""

            current_entry = {
                "start_ts": ts, "end_ts": ts,
                "app": _app_name_short(bundle_id),
                "bundle_id": bundle_id,
                "title": title[:80], "domain": domain,
                "cat": _classify_app(bundle_id, title, domain),
                "typed": [], "shortcuts": [],
                "clicks": 0, "scrolls": 0,
            }

        elif evt_type == "key.type" and current_entry is not None:
            text = evt.get("text", "")
            if text and len(current_entry["typed"]) < 5:
                current_entry["typed"].append(text[:50])

        elif evt_type == "key.shortcut" and current_entry is not None:
            combo = evt.get("text", "") or evt.get("combo", "")
            if combo and len(current_entry["shortcuts"]) < 10:
                current_entry["shortcuts"].append(combo)

        elif evt_type in ("mouse.singleclick", "mouse.doubleclick") and current_entry is not None:
            current_entry["clicks"] += 1

        elif evt_type == "mouse.scroll" and current_entry is not None:
            current_entry["scrolls"] += 1

    # Close final entry
    if current_entry is not None:
        current_entry["end_ts"] = session_end
        entries.append(current_entry)

    if not entries:
        return None

    # Merge consecutive entries with same app+title
    merged: list[dict] = [entries[0]]
    for e in entries[1:]:
        prev = merged[-1]
        if prev["bundle_id"] == e["bundle_id"] and prev["title"] == e["title"]:
            prev["end_ts"] = e["end_ts"]
            prev["typed"].extend(e["typed"])
            prev["shortcuts"].extend(e["shortcuts"])
            prev["clicks"] += e["clicks"]
            prev["scrolls"] += e["scrolls"]
            prev["typed"] = prev["typed"][:5]
            prev["shortcuts"] = prev["shortcuts"][:10]
        else:
            merged.append(e)

    # Build time_map: relative timestamp string → unix timestamp
    time_map: dict[str, float] = {}

    # Build compact timeline for LLM consumption (capped for context limits)
    capped = merged[:MAX_ACTIVITY_ENTRIES]
    if len(merged) > MAX_ACTIVITY_ENTRIES:
        log.info("Activity entries capped: %d → %d", len(merged), MAX_ACTIVITY_ENTRIES)
    timeline: list[dict] = []
    for e in capped:
        rel_start = e["start_ts"] - session_start
        rel_end = e["end_ts"] - session_start
        dur = rel_end - rel_start

        rel_str = _format_relative_time(rel_start)
        time_map[rel_str] = e["start_ts"]

        entry: dict = {
            "t": rel_str, "dur": _duration_human(dur),
            "app": e["app"], "title": e["title"],
        }
        if e["domain"]:
            entry["domain"] = e["domain"]
        entry["cat"] = e["cat"]
        if e["typed"]:
            entry["typed"] = e["typed"]
        if e["shortcuts"]:
            entry["shortcuts"] = e["shortcuts"]
        if e["clicks"]:
            entry["clicks"] = e["clicks"]
        timeline.append(entry)

    # Gather transcript snippets (``sorted_manifests`` computed above)
    transcript_snippets: list[dict] = []
    for manifest in sorted_manifests:
        chunk_idx = manifest["chunk_index"]
        chunk_start = manifest["chunk_start"]
        transcript = source.read_transcript(chunk_idx)
        if transcript is None:
            continue
        for seg in transcript.get("segments", [])[:20]:
            abs_ts = chunk_start + seg.get("start", 0)
            # Privacy strip (R11): a transcript segment whose absolute timestamp
            # falls in a blocked/masked interval is dropped too, so spoken
            # content from a blocked app never reaches the model.
            if is_blocked(abs_ts):
                continue
            rel = abs_ts - session_start
            text = seg.get("text", "").strip()
            if text:
                snippet_rel = _format_relative_time(rel)
                time_map[snippet_rel] = abs_ts
                transcript_snippets.append({"t": snippet_rel, "text": text[:100]})

    # Register session end
    end_rel = _format_relative_time(session_end - session_start)
    time_map[end_rel] = session_end

    summary = {
        "recording": recording_name,
        "duration": _duration_human(session_end - session_start),
        "timeline": timeline,
    }
    if transcript_snippets:
        summary["transcript"] = transcript_snippets

    result = {
        "summary": summary,
        "entries": merged,
        "time_map": time_map,
        "session_start": session_start,
        "session_end": session_end,
        # Raw data for fallback segmentation (avoids re-reading the source)
        "raw_timestamps": raw_timestamps,
        "raw_window_events": raw_window_events,
    }
    if blocked_source is not None:
        # Mark the summary stripped AUTHORITATIVELY — the on-device provider's
        # fail-closed gate trusts this flag, so it is set here (and ONLY when a
        # ``blocked_source`` was applied) rather than by the caller, tying the
        # claim to the strip actually having run. Omitted entirely on the cloud
        # path (``blocked_source is None``) so the golden stays byte-identical.
        result["stripped"] = True
    return result
