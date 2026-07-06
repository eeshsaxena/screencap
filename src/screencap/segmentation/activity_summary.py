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
from typing import Iterable, Protocol

log = logging.getLogger(__name__)

MAX_ACTIVITY_ENTRIES = 200  # cap activity timeline entries for LLM context


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
# Activity summary derivation
# ---------------------------------------------------------------------------

def build_activity_summary(
    recording_name: str,
    manifests: list[dict],
    source: ActivitySource,
) -> dict | None:
    """Build a compact activity summary from an injected event/transcript source.

    Stream-consumes events from ``source.iter_events()`` and pulls per-chunk
    transcripts via ``source.read_transcript()``. Returns a dict with keys:
    summary, entries, time_map, session_start, session_end (plus raw fallback
    data). Returns ``None`` if no window activity exists.

    Source-agnostic: this is the single chokepoint where the activity summary
    is built, so every consumer (cloud, on-device, future) reads only what the
    injected source yields. GCS-specific reads live in the caller's source.
    """
    session_start = min(m["chunk_start"] for m in manifests)
    session_end = max(m["chunk_end"] for m in manifests)

    entries: list[dict] = []
    current_entry: dict | None = None
    # Also collect raw data for fallback segmentation (avoids re-reading source)
    raw_timestamps: list[float] = []
    raw_window_events: list[dict] = []

    for evt in source.iter_events():
        evt_type = evt.get("type", "")
        ts = evt.get("timestamp", 0)

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

    # Gather transcript snippets
    sorted_manifests = sorted(manifests, key=lambda m: m["chunk_index"])
    transcript_snippets: list[dict] = []
    for manifest in sorted_manifests:
        chunk_idx = manifest["chunk_index"]
        chunk_start = manifest["chunk_start"]
        transcript = source.read_transcript(chunk_idx)
        if transcript is None:
            continue
        for seg in transcript.get("segments", [])[:20]:
            abs_ts = chunk_start + seg.get("start", 0)
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

    return {
        "summary": summary,
        "entries": merged,
        "time_map": time_map,
        "session_start": session_start,
        "session_end": session_end,
        # Raw data for fallback segmentation (avoids re-reading the source)
        "raw_timestamps": raw_timestamps,
        "raw_window_events": raw_window_events,
    }
