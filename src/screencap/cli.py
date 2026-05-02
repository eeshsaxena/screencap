"""ScreenCap CLI — Click-based entry point."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys

# Line-buffered stderr is part of the SwiftUI cross-language event contract
# (todo 012). PyInstaller-frozen binaries don't always honour
# `sys.stderr.flush()` alone — the env var propagates to all spawn workers
# and child processes via inheritance, so the recorder's hot loop and any
# subprocess (chunk_processor, scrub_worker) emit events line-by-line as
# SwiftUI's RecorderController.spawn expects. Set BEFORE any import that
# might cache buffering state.
#
# Force-set rather than `setdefault`: a stale `PYTHONUNBUFFERED=0` from a
# parent shell or launchd plist would otherwise leave stderr block-buffered
# and the SwiftUI line-reader would stall waiting for `started` until the
# pipe buffer fills. Line-buffered stderr is non-negotiable on this CLI.
os.environ["PYTHONUNBUFFERED"] = "1"

import click
from dotenv import load_dotenv

load_dotenv()  # auto-load .env if present
from rich.console import Console
from rich.table import Table

from screencap import __version__

console = Console()
logger = logging.getLogger(__name__)


def _stdin_is_tty() -> bool:
    """Check if stdin is a real TTY (not piped or redirected)."""
    return sys.stdin.isatty()


# Per-endpoint schema versions for `--json` output. Independent from the
# stderr-event schema (todo 009) so a future stderr-event change doesn't
# silently bump the version SwiftUI reads from `status --json`, and a status
# payload tweak can be signalled without disturbing the event stream.
_STATUS_SCHEMA_VERSION = 1
_APPS_SCHEMA_VERSION = 2
_SETTINGS_PRIVACY_SCHEMA_VERSION = 2
_SETTINGS_SCHEMA_VERSION = 1
_STOP_SCHEMA_VERSION = 1


def _should_default_to_json() -> bool:
    """Default value for ``--json`` flags on read-only commands.

    True when stdout is NOT a TTY — i.e., output is being piped to a file
    or another command. Auto-detection eliminates a footgun where an agent
    forgets the flag and parses Rich-formatted output (todo 038). Tests can
    monkey-patch this helper to override the auto-detect.
    """
    return not sys.stdout.isatty()


_RECORD_EXTRAS_MSG = (
    "[red]Error: This command requires recording dependencies.[/red]\n"
    "Install them with: [bold]pip install screencap\\[record][/bold]"
)


@click.group()
@click.version_option(version=__version__, prog_name="screencap")
@click.option("--no-update-check", is_flag=True, hidden=True,
              help="Skip auto-update check.")
@click.pass_context
def cli(ctx, no_update_check):
    """ScreenCap — macOS screen capture."""
    if not no_update_check:
        from screencap.updater import maybe_check_for_update
        maybe_check_for_update()


def _report_unclassified_apps(capture_dir) -> None:
    """Report apps seen during recording that are not in privacy config."""
    from screencap.catalog import get_seen_bundle_ids
    from screencap.config import get_privacy_config
    from screencap.privacy.context import BUNDLE_ID_MAP

    seen_bids = get_seen_bundle_ids([capture_dir])
    if not seen_bids:
        return

    try:
        privacy_config = get_privacy_config()
    except Exception:
        return

    known_bids = (
        set(BUNDLE_ID_MAP.keys())
        | set(privacy_config.exclude_apps)
        | set(privacy_config.allow_apps)
        | set(privacy_config.app_classes.keys())
    )

    unclassified = sorted(seen_bids - known_bids)
    if not unclassified:
        return

    console.print(
        f"\n[yellow]Note:[/yellow] {len(unclassified)} app(s) seen during recording "
        f"are not classified:"
    )
    for bid in unclassified[:10]:
        console.print(f"  {bid}")
    if len(unclassified) > 10:
        console.print(f"  ... and {len(unclassified) - 10} more")
    console.print("  Run [bold]screencap setup --scan[/bold] to classify them.")


def _download_nlp_models() -> None:
    """Download GLiNER + spaCy models. Thin wrapper for testability."""
    from screencap.setup_wizard import _download_nlp_models as _do_download
    _do_download()


# ---------------------------------------------------------------------------
# Unit 8a: structured stderr event contract
# ---------------------------------------------------------------------------
#
# SwiftUI's RecorderController.spawn parses these line-buffered JSON events
# off the screencap subprocess's stderr to drive UI state transitions. Schema
# is the cross-language contract — see
# docs/research/2026-04-28-stderr-event-schema.md.
#
# Events: started, chunk_finalized, recording_finalized, disk_full,
#         permission_lost, stopped
# Exit codes: 0=clean, 2=lock-held (Unit 3), 3=permission_lost (Unit 8),
#             4=disk_full
#
# ``permission_lost`` is emitted from src/screencap/recorder.py (Unit 8).
# ``chunk_finalized`` is emitted from src/screencap/session.py.
# ``disk_full`` is emitted from src/screencap/session.py (DiskFullError catch).
# All other events are emitted from the screencap start command flow below.

# `_emit_event` lives in the stdlib-only `screencap._stderr_events` module so
# spawn workers and the recording hot loop can import it without dragging
# Click + rich Console into the child process.
from screencap._stderr_events import (  # noqa: E402
    EVENT_STOPPED,
    emit_event as _emit_event,
)


def _maybe_download_nlp_models() -> None:
    """Prompt to download GLiNER + spaCy models if not already cached."""
    from screencap.privacy import are_nlp_models_cached

    if are_nlp_models_cached():
        return  # fully cached

    console.print(
        "\n[bold]Privacy models not yet downloaded.[/bold] "
        "These are needed for scrubbing and cloud upload."
    )
    if click.confirm("Download now?", default=True):
        _download_nlp_models()
    else:
        console.print(
            "[dim]Skipped. Models will download on first scrub or upload.[/dim]"
        )


_MATRIX_ACK_KEY = "matrix_acknowledged_v2026_04"


def _config_lock_path():
    """Return the path of the advisory config lock (sibling of recording.lock).

    Lazy resolution so test fixtures that monkey-patch ``_DEFAULT_BASE``
    pick up the right path each call.
    """
    from screencap.config import _DEFAULT_BASE
    return _DEFAULT_BASE / "run" / "config.lock"


_PRIVACY_CONFIG_FLOCK_TIMEOUT_S = 5.0


class PrivacyConfigLockTimeout(RuntimeError):
    """Raised when the advisory flock on config.lock can't be acquired in
    ``_PRIVACY_CONFIG_FLOCK_TIMEOUT_S``. Surfaces a stuck holder (e.g., a
    crashed peer on NFS / sshfs) so ``screencap start`` and
    ``screencap settings privacy`` fail fast instead of hanging."""


@contextlib.contextmanager
def _privacy_config_writer():
    """Read-modify-write the privacy config under an advisory flock (todo 025).

    Holds an exclusive flock on ``~/.screencap/run/config.lock`` for the
    duration of the read → mutate → save cycle. Without this, two concurrent
    ``screencap settings privacy`` invocations race on read-modify-write and
    silently drop one of the writes (todo 015). ``_save_config_atomic``
    provides write-atomicity, not lost-update protection — the flock does.

    The acquire is non-blocking with a bounded retry loop (todo 006). A
    blocking ``flock(LOCK_EX)`` could hang ``screencap start`` indefinitely
    if a stale holder kept the lock — Python's ``fcntl.flock`` only raises
    on signal interruption. We poll every 100ms up to
    ``_PRIVACY_CONFIG_FLOCK_TIMEOUT_S``; on timeout we raise
    ``PrivacyConfigLockTimeout`` so the caller can surface a clear error
    rather than stalling silently.

    Yields the tomlkit doc. The caller mutates in-place; on context exit
    (without exception) the doc is atomically saved and the config cache is
    invalidated. On exception the file is left untouched.
    """
    import fcntl as _fcntl
    import time as _time

    from screencap.config import _CONFIG_PATH, invalidate_config_cache
    from screencap.setup_wizard import _load_config_toml, _save_config_atomic

    lock_path = _config_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = _time.monotonic() + _PRIVACY_CONFIG_FLOCK_TIMEOUT_S
        while True:
            try:
                _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if _time.monotonic() >= deadline:
                    raise PrivacyConfigLockTimeout(
                        f"Could not acquire {lock_path} within "
                        f"{_PRIVACY_CONFIG_FLOCK_TIMEOUT_S:.0f}s — another "
                        f"screencap process may be holding the lock or have "
                        f"exited without releasing it. Re-run after the other "
                        f"process completes, or remove {lock_path} if no "
                        f"screencap process is active."
                    )
                _time.sleep(0.1)
        doc = _load_config_toml(_CONFIG_PATH)
        yield doc
        # Only reached on the no-exception path.
        _save_config_atomic(_CONFIG_PATH, doc)
        invalidate_config_cache()
    finally:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def _write_privacy_flag(key: str, value: object) -> None:
    """Write a single [privacy] scalar via tomlkit, preserving comments and order.

    Used by the matrix-acknowledgement flow and by setup-skip — both need to
    set a single bool without disturbing other [privacy] keys (R16 invariant).
    """
    import tomlkit

    with _privacy_config_writer() as doc:
        if "privacy" not in doc:
            doc.add("privacy", tomlkit.table())
        doc["privacy"][key] = value


def _maybe_prompt_privacy_setup(*, cloud_intent: bool = False) -> None:
    """Prompt for privacy setup on first run if [privacy] section is missing."""
    import sys as _sys  # use real sys, not the module-level reference

    if not _sys.stdin.isatty():
        return  # non-interactive: skip silently, use defaults

    from screencap.config import _CONFIG_PATH, _load_toml

    is_new_user = True
    if _CONFIG_PATH.exists():
        cfg = _load_toml()
        privacy_section = cfg.get("privacy")
        if privacy_section is not None:
            # Has a [privacy] section — check if NLP models need downloading.
            # Skip for cloud-intent: the cloud gate handles model download.
            is_new_user = False
            if not cloud_intent:
                _maybe_download_nlp_models()

    if not is_new_user:
        return

    console.print(
        "\n[bold]Privacy setup not configured.[/bold] "
        "Run the setup wizard to classify apps for privacy protection."
    )
    if click.confirm("Run setup now?", default=True):
        from screencap.setup_wizard import run_setup_wizard
        run_setup_wizard()
    else:
        _write_privacy_flag("setup_skipped", True)
        console.print(
            "[dim]Skipped. Recordings will stay local with default privacy settings. "
            "Run 'screencap setup' anytime.[/dim]"
        )

    # New-user path always pre-acknowledges the matrix correction so the
    # migration prompt never fires for someone who has only ever seen the
    # corrected matrix (Unit 7a release sequencing).
    try:
        _write_privacy_flag(_MATRIX_ACK_KEY, True)
    except Exception:
        # Log at debug — disk full / permission denied here makes the prompt
        # fire on every subsequent start, so a quiet diagnostic helps debug
        # "why does the prompt keep appearing" without affecting UX (todo 031).
        logger.debug("Failed to pre-set matrix ack flag", exc_info=True)


def _maybe_prompt_matrix_acknowledgement() -> None:
    """One-time on-upgrade acknowledgement of the privacy matrix correction.

    The Unit 7a matrix change tightens CHAT/EMAIL/CALENDAR/VIDEO_CALL under
    ``mode = internal`` from TEXT_REDACT to MASK_WINDOW. Existing CLI users
    who relied on text-redacted transcripts of conversation apps will see a
    real workflow change (video frames blocked, keystrokes nulled, screenshots
    full-window-blurred). This prints a one-line note + 5-second prompt the
    first time after upgrade so they aren't surprised. The flag is written
    regardless of the user's keystroke; recording continues either way.

    Skipped silently when:
      - flag already set (acknowledged on a prior run, or pre-set for new users)
      - mode is not ``internal`` (matrix change doesn't apply)
      - stdin is not a TTY (non-interactive — e.g., SwiftUI subprocess)
      - ``SCREENCAP_MATRIX_ACK=true`` (SwiftUI sets this; the flag still gets
        written so future invocations don't re-check)
    """
    import sys as _sys

    from screencap.config import _CONFIG_PATH, _load_toml
    from screencap.privacy.policy import PrivacyMode

    if not _CONFIG_PATH.exists():
        return
    cfg = _load_toml()
    privacy_section = cfg.get("privacy") or {}
    if privacy_section.get(_MATRIX_ACK_KEY):
        return

    mode_str = (privacy_section.get("mode") or "internal").lower()
    try:
        mode = PrivacyMode(mode_str)
    except ValueError:
        return
    if mode is not PrivacyMode.INTERNAL:
        # Matrix change doesn't affect non-internal modes for chat/email/cal/vc.
        return

    import os as _os
    env_ack = _os.environ.get("SCREENCAP_MATRIX_ACK", "").lower() == "true"
    # Treat any known scripted parent (SwiftUI subprocess spawn) as
    # auto-acknowledged so a 5-second `select.select` doesn't stall first
    # start (todo 019). SwiftUI may not have plumbed SCREENCAP_MATRIX_ACK
    # explicitly yet — SCREENCAP_PARENT=swiftui is sufficient evidence
    # the prompt would never be displayed to a human anyway.
    parent_swiftui = _os.environ.get("SCREENCAP_PARENT") == "swiftui"
    interactive = _sys.stdin.isatty() and not env_ack and not parent_swiftui

    # Defer the disclosure to SwiftUI via a structured stderr event (todo
    # 013). Without this, the SwiftUI shell silently auto-acks the matrix
    # tightening on every first start, and users only discover the
    # behavior change when their Slack / Gmail recordings come back empty
    # or their AI conversations land unredacted in the cloud. Emitting
    # ``matrix_disclosure_required`` lets SwiftUI render a one-time modal
    # the next time the app foregrounds; the integrator is expected to
    # re-launch with ``SCREENCAP_MATRIX_ACK=true`` after the user clears
    # the modal so this branch falls through to the flag write below.
    if parent_swiftui and not env_ack:
        try:
            from screencap._stderr_events import (
                EVENT_MATRIX_DISCLOSURE_REQUIRED,
                emit_event as _emit_event,
            )
            _emit_event(
                EVENT_MATRIX_DISCLOSURE_REQUIRED,
                changes=[
                    "chat_email_calendar_video_call_mask_window",
                    "ai_assistant_browser_unverified",
                ],
                opt_out_command_examples=[
                    "screencap settings privacy exclude_apps add com.openai.chat",
                    "screencap settings privacy exclude_apps add com.anthropic.claudefordesktop",
                    "screencap settings privacy exclude_apps add ai.perplexity.mac",
                ],
            )
        except Exception:
            pass

    if interactive:
        console.print(
            "[yellow]Privacy default changed:[/yellow] chat / email / calendar / "
            "video-call apps under [bold]mode = internal[/bold] now mask the window "
            "instead of text-redacting it."
        )
        # Disclosure for the AI-assistant reclassification (todo 031). The
        # plan deliberately keeps these as BROWSER_UNVERIFIED → ALLOW so a
        # friend recording \"let me show you my AI tool\" stays useful, but
        # an upgrading user deserves to know their conversation contents
        # will be captured raw before the flag flips silently.
        console.print(
            "[yellow]Also new:[/yellow] ChatGPT, Claude, and Perplexity desktop "
            "apps now classify as [bold]browser_unverified[/bold] — under "
            "[bold]mode = internal[/bold] their conversation contents are "
            "[bold]captured unredacted[/bold]. Opt out per-app with: "
            "[dim]screencap settings privacy exclude_apps add com.openai.chat[/dim] "
            "(or com.anthropic.claudefordesktop, ai.perplexity.mac)."
        )
        console.print(
            "Press [bold]Y[/bold] within 5s to acknowledge. Recording "
            "continues either way."
        )
        try:
            import select

            select.select([_sys.stdin], [], [], 5.0)
        except BaseException:
            # Widen to BaseException so KeyboardInterrupt during the 5s
            # wait does not skip the ack-flag write below — otherwise the
            # prompt re-fires on every subsequent ``screencap start`` until
            # the user lets it time out.
            pass

    try:
        _write_privacy_flag(_MATRIX_ACK_KEY, True)
    except Exception:
        # Same rationale as the new-user pre-set: log so debugging is possible
        # without breaking the user's recording (todo 031).
        logger.debug("Failed to write matrix ack flag", exc_info=True)


@cli.command()
@click.option("--name", "-n", default=None, help="Recording name (skips auto-naming).")
@click.option("--description", "-d", default=None, help="Task description.")
@click.option("--no-audio", is_flag=True, default=False, help="Disable audio capture.")
@click.option("--no-video", is_flag=True, default=False, help="Disable video capture.")
@click.option("--no-images", is_flag=True, default=False, help="Disable screenshot capture.")
@click.option("--no-window-data", is_flag=True, default=False, help="Disable window/accessibility data.")
@click.option("--output", "-o", type=click.Path(), default=None, help="Custom output directory.")
@click.option("--no-wifi-metrics", is_flag=True, default=False, help="Disable WiFi metrics collection.")
@click.option("--no-app-versions", is_flag=True, default=False, help="Disable running app version capture.")
@click.option("--no-auto-name", is_flag=True, default=False, help="Skip LLM auto-naming after recording.")
@click.option("--local-only", is_flag=True, default=False, help="Restrict LLM naming to local providers (Ollama).")
@click.option("--force", is_flag=True, default=False, help="Auto-clean orphaned processes before starting.")
@click.option("--verbose", "-v", is_flag=True, default=False, help="Show all info/debug output during recording.")
@click.option("--chunk-duration", type=float, default=None,
              help="Auto-cut recording at this interval (seconds). Default: 900 (15 min). Set 0 to disable chunking.")
@click.option("--no-live-upload", is_flag=True, default=False,
              help="Disable background upload of chunks during recording.")
@click.option("--cloud", "destination", flag_value="cloud", default=None,
              help="Record for cloud upload (forces public privacy mode).")
@click.option("--local", "destination", flag_value="local",
              help="Record for local use only (uses configured privacy mode).")
@click.option("--both", "destination", flag_value="both",
              help="Upload to cloud AND keep local copy (public privacy mode, no auto-delete).")
@click.option("--segmentation-mode", type=click.Choice(["llm", "idle"], case_sensitive=False),
              default=None, help="Task segmentation: 'llm' (server-side) or 'idle' (gap detection).")
@click.option("--no-scrub", is_flag=True, default=False,
              help="Disable PII/secrets scrubbing for this recording.")
@click.option(
    "--network",
    is_flag=True,
    default=False,
    help=(
        "Capture HTTP/HTTPS request/response metadata as a system proxy. "
        "Off by default. First use installs a 30-day CA into your login "
        "Keychain (one prompt) and configures the system web proxy (one "
        "admin prompt). Bodies are NOT retained in V1; metadata only. "
        "Run `screencap network restore` to recover proxy state after a crash."
    ),
)
@click.option("--unlisted", is_flag=True, default=False,
              help="Hide this recording from the website (still uploads, just not listed).")
def start(
    name, description, no_audio, no_video, no_images, no_window_data,
    output, no_wifi_metrics, no_app_versions,
    no_auto_name, local_only, force, verbose, chunk_duration, no_live_upload,
    destination, segmentation_mode, no_scrub, network, unlisted,
):
    """Record a screen capture session. Ctrl+C to stop.

    \b
    Visibility:
      Cloud recordings are shown on the website by default.
      --unlisted                hide this recording (still uploads, just not listed)
      screencap settings        view/change the default visibility setting

    \b
    Exit codes (cross-language contract for SwiftUI / agent consumers):
      0  Clean stop
      1  Generic failure (uncaught exception, child crash)
      2  Lock-already-held (another recording is active)
      3  Permission lost mid-recording (Screen Recording / Accessibility / Input Monitoring)
      4  Disk full
      5  User-initiated force-quit (2-tap Ctrl+C)

    Lifecycle events are emitted as line-buffered JSON on stderr — see
    docs/research/2026-04-28-stderr-event-schema.md.
    """
    from datetime import datetime

    from screencap.config import (
        get_audio_default,
        get_auto_name,
        get_auto_name_local_only,
        get_segmentation_mode,
    )

    # Resolve segmentation mode: CLI flag > config.toml > default
    seg_mode = segmentation_mode or get_segmentation_mode()

    # Determine if auto-naming is enabled
    user_provided_name = name is not None
    auto_name_enabled = get_auto_name() and not no_auto_name and not user_provided_name
    local_only = local_only or get_auto_name_local_only()

    if not name:
        if no_auto_name:
            # Restore old interactive prompt behavior
            if not sys.stdin.isatty():
                console.print("[red]Error: --name is required with --no-auto-name in non-interactive mode.[/red]")
                raise SystemExit(1)
            name = click.prompt("Recording name")
            description = description or click.prompt("Description (optional)", default="", show_default=False)
        else:
            # Generate timestamp-based temp name
            name = f"rec-{datetime.now().strftime('%Y%m%dT%H%M%S')}"

    # --no-audio wins; otherwise fall back to the persisted default so the
    # menu-bar "Audio (next recording)" toggle actually reaches new sessions.
    audio = False if no_audio else get_audio_default()
    wifi_metrics = not no_wifi_metrics
    app_versions = not no_app_versions

    # Capture flags: default to True for images and window data (opt-out)
    capture_video = False if no_video else None  # None = use upstream default (True)
    capture_images = False if no_images else True  # Default ON (overrides upstream False)
    capture_window_data = False if no_window_data else None  # None = upstream default (True)
    # First-run privacy setup detection
    _maybe_prompt_privacy_setup(cloud_intent=destination == "cloud")
    _maybe_prompt_matrix_acknowledgement()

    # --- Resolve recording destination (cloud/local) ---
    from screencap.config import get_upload_default

    intent_source = "flag"
    if destination is None:
        upload_default = get_upload_default()
        if not sys.stdin.isatty():
            # Non-interactive: always default to local regardless of config
            destination = "local"
            intent_source = "non_interactive_default"
        elif upload_default == "ask":
            console.print(
                "\n[bold]Recording destination:[/bold]"
            )
            console.print(
                "  [bold]cloud[/bold]  — upload to cloud (public privacy mode)"
            )
            console.print(
                "  [bold]local[/bold]  — stay on this machine (configured privacy mode)"
            )
            console.print(
                "  [bold]both[/bold]   — upload to cloud AND keep a local copy\n"
            )
            destination = click.prompt(
                "Destination",
                type=click.Choice(["cloud", "local", "both"], case_sensitive=False),
                default="local",
            )
            intent_source = "prompt"
        else:
            destination = upload_default
            intent_source = "config_default"
    # else: destination was set by --cloud, --local, or --both flag, intent_source stays "flag"

    is_cloud = destination in ("cloud", "both")
    keep_local = destination in ("local", "both")
    force_mode = None
    if is_cloud:
        from screencap.privacy.policy import PrivacyMode
        force_mode = PrivacyMode.PUBLIC

    # --- Cloud NLP model gate ---
    if is_cloud:
        from screencap.privacy import are_nlp_models_cached
        if not are_nlp_models_cached():
            if not _stdin_is_tty():
                console.print(
                    "[red]Error:[/] NLP models required for cloud recording "
                    "are not installed. Run [bold]screencap setup --scan[/bold] first."
                )
                raise SystemExit(1)
            if click.confirm(
                "NLP models needed for cloud PII scrubbing are not installed. "
                "Download now?",
                default=True,
            ):
                try:
                    _download_nlp_models()
                except (OSError, RuntimeError, ImportError) as exc:
                    console.print(f"[yellow]Warning:[/] Download failed: {exc}")
            if not are_nlp_models_cached():
                if click.confirm(
                    "Record locally instead? You can 'screencap upload' later "
                    "once models are installed.",
                    default=True,
                ):
                    is_cloud = False
                    force_mode = None  # revert to configured mode
                else:
                    console.print("[red]Aborted.[/]")
                    raise SystemExit(1)

    # --- Redaction prompt ---
    scrub_enabled = not no_scrub
    if scrub_enabled and _stdin_is_tty():
        scrub_enabled = click.confirm(
            "Redact sensitive info (passwords, emails, keys) from this recording?\n"
            "  Note: redaction adds extra post-processing time after recording stops",
            default=True,
        )

    # --- Website visibility ---
    from screencap.config import _CONFIG_PATH, _load_toml, get_show_on_website

    if unlisted:
        show_on_website = False
    elif is_cloud:
        # First cloud recording: prompt if show_on_website has never been set
        cfg = _load_toml()
        if "show_on_website" not in cfg and _stdin_is_tty():
            console.print()
            console.print(
                "[bold]Would you like your recordings to be visible on the website?[/bold]"
            )
            console.print(
                "  [dim]Yes = your sessions will appear in the public viewer\n"
                "  No  = recordings still upload, but won't be listed on the site\n"
                "  Change later with: screencap settings --set show_on_website=true\n"
                "  Or pass --unlisted to hide individual recordings.[/dim]"
            )
            show_on_website = click.confirm(
                "Show recordings on the website?",
                default=True,
            )
            from screencap.config import invalidate_config_cache
            from screencap.setup_wizard import _load_config_toml, _save_config_atomic

            doc = _load_config_toml(_CONFIG_PATH)
            doc["show_on_website"] = show_on_website
            _save_config_atomic(_CONFIG_PATH, doc)
            invalidate_config_cache()
            if show_on_website:
                console.print("[dim]Saved. Recordings will be visible. Use --unlisted to hide individual ones.[/dim]")
            else:
                console.print("[dim]Saved. Recordings will be hidden from the site by default.[/dim]")
        else:
            show_on_website = get_show_on_website()
    else:
        show_on_website = True  # irrelevant for local-only recordings

    # --- Hand off to the Session Controller ---
    # ``screencap start`` is a long-lived session: the controller spawns
    # the menu bar once, runs each recording in its own Recording Worker
    # subprocess, and detaches post-processing into a separate
    # Post-Process Worker subprocess so a second recording can start
    # while the previous one is still transcribing / uploading.
    #
    # Setting ``SCREENCAP_LEGACY_START=1`` falls back to the classic
    # one-shot code path. This is used by the test suite (which mocks
    # ``screencap.recorder.start_recording`` at module level — the mock
    # does not cross the subprocess boundary of the session controller)
    # and as an escape hatch if the controller regresses in production.
    import os as _os_start

    if _os_start.environ.get("SCREENCAP_LEGACY_START") == "1":
        _legacy_start_recording(
            name=name,
            description=description,
            audio=audio,
            output=output,
            wifi_metrics=wifi_metrics,
            app_versions=app_versions,
            force=force,
            capture_video=capture_video,
            capture_images=capture_images,
            capture_window_data=capture_window_data,
            verbose=verbose,
            chunk_duration=chunk_duration,
            no_live_upload=no_live_upload,
            force_mode=force_mode,
            is_cloud=is_cloud,
            keep_local=keep_local,
            intent_source=intent_source,
            seg_mode=seg_mode,
            scrub_enabled=scrub_enabled,
            show_on_website=show_on_website,
            auto_name_enabled=auto_name_enabled,
            local_only=local_only,
        )
        return

    try:
        from screencap.session import SessionController
    except ImportError:
        console.print(_RECORD_EXTRAS_MSG)
        raise SystemExit(1)

    # Pin spawn mode at CLI entry BEFORE any mp.Queue/mp.Process is constructed.
    # AES-GCM nonce safety (V1.5+) depends on os.urandom being independently
    # seeded in the child; under fork mode the child inherits parent state.
    # Asserting only inside run_proxy() is too late -- the parent has already
    # forked/spawned by then. The `allow_none=True` is load-bearing: without
    # it, get_start_method freezes the start-method context as a side effect.
    if network:
        import multiprocessing as _mp_init
        _current_start = _mp_init.get_start_method(allow_none=True)
        if _current_start is None:
            _mp_init.set_start_method("spawn", force=True)
        elif _current_start != "spawn":
            console.print(
                f"[red]Error:[/red] multiprocessing start method is "
                f"{_current_start!r}; --network requires 'spawn'."
            )
            raise SystemExit(1)

    cli_args = {
        "name": name,
        "description": description or None,
        "audio": audio,
        "output": output,
        "wifi_metrics": wifi_metrics,
        "app_versions": app_versions,
        "force_clean": force,
        "capture_video": capture_video,
        "capture_images": capture_images,
        "capture_window_data": capture_window_data,
        "verbose": verbose,
        "chunk_duration": chunk_duration,
        "live_upload": not no_live_upload,
        "force_mode": force_mode,
        "cloud_intent": is_cloud,
        "keep_local": keep_local,
        "intent_source": intent_source,
        "segmentation_mode": seg_mode,
        "scrub_enabled": scrub_enabled,
        "show_on_website": show_on_website,
        "auto_name_enabled": auto_name_enabled,
        "local_only": local_only,
        "network": network,
    }

    # SessionController(...) is INSIDE the try block so a SystemExit raised
    # from __init__ (e.g. exit code 2 on lock contention, exit code 3 on
    # permission_lost) goes through the same `stopped` event emission path.
    # Without this, exit-2 silently bypassed the terminal event (todo 004).
    exit_code = 0
    try:
        controller = SessionController(cli_args)
        # SessionController.__init__ already claimed the lock + emitted `started`
        # via the path inside session.py — no need to re-emit here.
        controller.run()
    except SystemExit as se:
        exit_code = int(getattr(se, "code", 0) or 0)
        _emit_event(EVENT_STOPPED, exit_code=exit_code)
        raise
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Session controller error:[/red] {exc}")
        _emit_event(EVENT_STOPPED, exit_code=1, error=str(exc))
        raise SystemExit(1)
    _emit_event(EVENT_STOPPED, exit_code=exit_code)


def _legacy_start_recording(
    *,
    name,
    description,
    audio,
    output,
    wifi_metrics,
    app_versions,
    force,
    capture_video,
    capture_images,
    capture_window_data,
    verbose,
    chunk_duration,
    no_live_upload,
    force_mode,
    is_cloud,
    keep_local,
    intent_source,
    seg_mode,
    scrub_enabled,
    show_on_website,
    auto_name_enabled,
    local_only,
) -> None:
    """Classic one-shot ``screencap start`` code path (pre-Session Controller).

    Kept for the test suite and for ``SCREENCAP_LEGACY_START=1`` users.
    Functionally identical to the previous inline body of ``start()``.
    """
    try:
        from screencap.recorder import (
            DiskFullError,
            _kill_menubar,
            print_summary,
            start_recording,
        )
    except ImportError:
        console.print(_RECORD_EXTRAS_MSG)
        raise SystemExit(1)

    # Cross-language event contract (todo 016): the SwiftUI shell relies on
    # ``started`` and ``stopped`` to drive UI state. The legacy path is a
    # documented escape hatch (``SCREENCAP_LEGACY_START=1``) and does not
    # build a SessionController, so the events are emitted here directly.
    # Without these wires, a SwiftUI launch falling onto the legacy path
    # would line-read silence then EOF and never transition out of
    # "starting" state.
    try:
        from screencap._stderr_events import (
            EVENT_STARTED,
            emit_event as _emit_event_legacy,
            resolve_claimant,
        )
        _emit_event_legacy(EVENT_STARTED, claimant=resolve_claimant())
    except Exception:
        pass

    disk_full = False
    _menubar_proc = None
    _menubar_state_file = None
    try:
        capture_dir, elapsed, _menubar_proc, _menubar_state_file = start_recording(
            name, description or None, audio, output,
            wifi_metrics=wifi_metrics, app_versions=app_versions,
            force_clean=force,
            capture_video=capture_video, capture_images=capture_images,
            capture_window_data=capture_window_data,
            verbose=verbose,
            chunk_duration=chunk_duration,
            live_upload=not no_live_upload,
            force_mode=force_mode,
            cloud_intent=is_cloud,
            keep_local=keep_local,
            intent_source=intent_source,
            segmentation_mode=seg_mode,
            scrub_enabled=scrub_enabled,
            show_on_website=show_on_website,
        )
    except DiskFullError as e:
        capture_dir, elapsed = e.capture_dir, e.elapsed
        _menubar_proc, _menubar_state_file = e.menubar_proc, e.menubar_state_file
        disk_full = True
        console.print(
            "[yellow]Skipping auto-naming/transcription: disk space is low.[/yellow]"
        )
    except ImportError:
        console.print(_RECORD_EXTRAS_MSG)
        raise SystemExit(1)

    final_name = name
    final_dir = capture_dir

    try:
        from screencap.menubar import RENAME_FILENAME
        _rename_file = capture_dir / RENAME_FILENAME
        if _rename_file.exists():
            _new_name = _rename_file.read_text().strip()
            if _new_name and _new_name != name:
                new_dir = capture_dir.parent / _new_name
                if not new_dir.exists():
                    capture_dir.rename(new_dir)
                    final_name = _new_name
                    final_dir = new_dir
                    capture_dir = new_dir
            _rename_file.unlink(missing_ok=True)
    except Exception:
        pass

    try:
        _auto_export(capture_dir)
    except KeyboardInterrupt:
        console.print("[yellow]Export cancelled.[/yellow]")

    if auto_name_enabled and not disk_full and final_name == name:
        has_chunk_transcripts = any(capture_dir.glob("transcript_*.txt"))
        audio_path = capture_dir / "audio.flac"
        if (
            audio
            and not has_chunk_transcripts
            and audio_path.exists()
            and audio_path.stat().st_size >= 1024
        ):
            try:
                _auto_transcribe(capture_dir, audio_path)
            except KeyboardInterrupt:
                console.print("[yellow]Transcription cancelled.[/yellow]")

        skip_rename = output is not None
        try:
            from screencap.namer import auto_name as do_auto_name

            with console.status("[bold]Generating name...[/bold]"):
                final_dir = do_auto_name(
                    capture_dir,
                    local_only=local_only,
                    skip_rename=skip_rename,
                )
            final_name = final_dir.name
        except KeyboardInterrupt:
            console.print(
                "[yellow]Naming cancelled — keeping timestamp name[/yellow]"
            )

    from screencap.recorder import print_upload_followup
    print_upload_followup(final_name, final_dir)

    print_summary(final_name, final_dir, elapsed)

    try:
        _report_unclassified_apps(final_dir)
    except Exception:
        pass

    _kill_menubar(_menubar_proc, _menubar_state_file)

    # Mirror the SessionController exit path so SwiftUI sees `stopped`
    # before the process disappears (todo 016). Use ``os._exit`` after the
    # emit so any background threads (chunk_processor watcher, post-
    # processing pool) don't keep the interpreter alive.
    try:
        from screencap._stderr_events import (
            EVENT_STOPPED,
            emit_event as _emit_event_legacy,
        )
        _emit_event_legacy(EVENT_STOPPED, exit_code=0)
    except Exception:
        pass
    import os as _os
    _os._exit(0)


def _auto_export(capture_dir: Path) -> None:
    """Auto-export events.jsonl for downstream scrubbing."""
    jsonl_path = capture_dir / "events.jsonl"
    try:
        from screencap.exporter import build_export_metadata, export_recording

        with console.status("[dim]Exporting events...[/dim]"):
            meta = build_export_metadata(exclude_moves=False)
            count = export_recording(
                capture_dir, str(jsonl_path), exclude_moves=False, metadata=meta,
            )
        console.print(f"  [dim]Exported {count} events to events.jsonl[/dim]")
        if count == 0:
            console.print("[yellow]Warning:[/yellow] Recording contains no events.")
    except Exception as e:
        console.print(
            f"[yellow]Warning:[/yellow] Could not auto-export events.jsonl ({e}). "
            f"Run 'screencap export {capture_dir.name}' manually."
        )


def _auto_transcribe(capture_dir, audio_path):
    """Auto-transcribe audio using the fastest available backend."""
    import os

    transcript_path = capture_dir / "transcript.txt"
    transcript_json_path = capture_dir / "transcript.json"

    # Skip if already transcribed
    if transcript_path.exists():
        return

    # Try faster-whisper first
    try:
        import faster_whisper  # noqa: F401
        from screencap.engine.cli import _transcribe_faster_whisper

        with console.status("[bold]Transcribing audio...[/bold]"):
            # Suppress print() calls from vendored code
            _orig = sys.stdout
            sys.stdout = open(os.devnull, "w")
            try:
                _transcribe_faster_whisper(audio_path, transcript_path, transcript_json_path, "base")
            finally:
                sys.stdout.close()
                sys.stdout = _orig
        return
    except ImportError:
        pass

    # Try openai-whisper
    try:
        from screencap.engine.cli import _transcribe_local

        with console.status("[bold]Transcribing audio...[/bold]"):
            _orig = sys.stdout
            sys.stdout = open(os.devnull, "w")
            try:
                _transcribe_local(audio_path, transcript_path, transcript_json_path, "base")
            finally:
                sys.stdout.close()
                sys.stdout = _orig
        return
    except ImportError:
        pass

    # Try OpenAI API
    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key:
        try:
            with console.status("[bold]Transcribing audio via API...[/bold]"):
                _transcribe_api_inline(api_key, audio_path, transcript_path, transcript_json_path)
            return
        except Exception:
            pass

    # No transcription backend available — not an error


@cli.command("list")
@click.option("--json", "as_json", is_flag=True,
              default=lambda: _should_default_to_json(),
              help="Output as JSON. Auto-detected when stdout is not a TTY (todo 030).")
@click.option(
    "--sort",
    type=click.Choice(["name", "date", "duration"], case_sensitive=False),
    default="date",
    help="Sort column.",
)
@click.option("--remote", is_flag=True, help="List remote processed sessions.")
@click.option("--tag", "filter_tag", default=None, help="Filter remote sessions by tag.")
@click.option("--category", "filter_category", default=None,
              type=click.Choice(["development", "communication", "research",
                                 "admin", "creative", "other"], case_sensitive=False),
              help="Filter remote sessions by category.")
def list_cmd(as_json, sort, remote, filter_tag, filter_category):
    """List all recordings."""
    if not remote and (filter_tag or filter_category):
        console.print("[yellow]--tag and --category require --remote[/yellow]")
        return

    if remote:
        from screencap.download import list_remote_sessions

        try:
            sessions = list_remote_sessions(tag=filter_tag, category=filter_category)
        except Exception as e:
            console.print(f"[red]Error:[/red] {e}")
            sys.exit(1)

        if not sessions:
            console.print("[dim]No remote sessions found.[/dim]")
            return

        if as_json:
            import dataclasses
            click.echo(json.dumps(
                [dataclasses.asdict(s) for s in sessions], indent=2,
            ))
            return

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

        table = Table(show_header=True, header_style="bold #60a5fa")
        table.add_column("#", justify="right")
        table.add_column("Name")
        table.add_column("Date")
        table.add_column("Duration")
        table.add_column("Focus")
        table.add_column("Tags")
        table.add_column("Tasks", justify="right")

        for i, s in enumerate(sessions, 1):
            date_display = s.processed_at[:10] if s.processed_at else "-"
            tags_display = ", ".join(s.tags[:5]) if s.tags else "-"
            if len(s.tags) > 5:
                tags_display += ", ..."
            table.add_row(
                str(i),
                s.name,
                date_display,
                _duration_human(s.total_duration_s),
                s.primary_focus,
                tags_display,
                str(s.total_tasks),
            )

        console.print(table)
        return

    from screencap.catalog import list_recordings

    recordings = list_recordings()

    if not recordings:
        # JSON consumers (e.g. the SwiftUI shell's RecordingsIndex) need a
        # parseable empty list, not Rich-styled prose. Plain-text output
        # stays for human callers.
        if as_json:
            click.echo(json.dumps([]))
        else:
            console.print("[dim]No recordings found.[/dim]")
        return

    # Sort
    sort_keys = {
        "name": lambda r: r.name,
        "date": lambda r: r.date,
        "duration": lambda r: r.duration,
    }
    recordings.sort(key=sort_keys[sort])

    if as_json:
        click.echo(
            json.dumps(
                [r._asdict() for r in recordings],
                indent=2,
            )
        )
        return

    table = Table(show_header=True, header_style="bold #60a5fa")
    table.add_column("#", justify="right")
    table.add_column("Name")
    table.add_column("Date")
    table.add_column("Duration")
    table.add_column("Size")
    table.add_column("Intent")
    table.add_column("Audio")
    table.add_column("Transcribed")
    table.add_column("Uploaded")

    for i, r in enumerate(recordings, 1):
        name_display = r.name
        if r.drops:
            name_display += " [yellow]\u26a0[/yellow]"
        intent_display = r.intent or "-"
        table.add_row(
            str(i),
            name_display,
            r.date,
            r.duration,
            r.size_mb,
            intent_display,
            "[green]\u2713[/green]" if r.has_audio else "[dim]\u2717[/dim]",
            "[green]\u2713[/green]" if r.transcribed else "[dim]\u2717[/dim]",
            "[green]\u2713[/green]" if r.uploaded else "[dim]\u2717[/dim]",
        )

    console.print(table)


@cli.command()
@click.argument("name")
@click.option("--regenerate", is_flag=True, help="Delete cached viewer.html and regenerate.")
@click.option("--max-events", type=int, default=500, help="Max events in viewer (0 for all).")
def view(name, regenerate, max_events):
    """Open recording viewer in browser."""
    from screencap.viewer import open_viewer

    try:
        open_viewer(name, regenerate=regenerate, max_events=max_events)
        console.print(f"[dim]Opening {name}/viewer.html ...[/dim]")
    except ImportError:
        console.print(_RECORD_EXTRAS_MSG)
        sys.exit(1)
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


@cli.command()
@click.argument("name")
@click.option("--json", "as_json", is_flag=True,
              default=lambda: _should_default_to_json(),
              help="Output as JSON. Auto-detected when stdout is not a TTY (todo 030).")
def info(name, as_json):
    """Show details and system metrics for a recording."""
    from screencap.catalog import find_db, read_drops, _read_recording_meta
    from screencap.config import get_recordings_dir

    try:
        from screencap.metrics import METRICS_FILENAME
    except ImportError:
        console.print(_RECORD_EXTRAS_MSG)
        raise SystemExit(1)

    recording_dir = get_recordings_dir() / name
    if not recording_dir.exists():
        console.print(f"[red]Error:[/red] Recording not found: {name}")
        sys.exit(1)

    # Read recording intent
    from screencap.catalog import read_intent
    intent_data = None
    intent_path = recording_dir / ".recording_intent"
    if intent_path.exists():
        try:
            intent_data = json.loads(intent_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    # Read DB metadata
    db_path = find_db(recording_dir)
    rec_meta = {}
    if db_path:
        from datetime import datetime

        started, duration = _read_recording_meta(db_path)
        if started:
            rec_meta["date"] = datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M:%S")
        if duration:
            m, s = divmod(int(duration), 60)
            h, m = divmod(m, 60)
            rec_meta["duration"] = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"

    # Read drop counts
    drops = read_drops(recording_dir)

    # Read metrics
    metrics_path = recording_dir / METRICS_FILENAME
    metrics = None
    if metrics_path.exists():
        try:
            metrics = json.loads(metrics_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    if as_json:
        click.echo(json.dumps({"recording": rec_meta, "metrics": metrics, "drops": drops, "intent": intent_data}, indent=2))
        return

    # Human-readable output
    from rich.panel import Panel

    console.print(Panel(f"[bold]{name}[/bold]", title="Recording"))

    if intent_data:
        console.print(f"  [#60a5fa]destination:[/#60a5fa] {intent_data.get('destination', '?')}")
        console.print(f"  [#60a5fa]privacy mode:[/#60a5fa] {intent_data.get('privacy_mode', '?')}")
        console.print(f"  [#60a5fa]intent source:[/#60a5fa] {intent_data.get('source', '?')}")

    if rec_meta:
        for key, val in rec_meta.items():
            console.print(f"  [#60a5fa]{key}:[/#60a5fa] {val}")
    else:
        console.print("  [dim]No recording metadata available.[/dim]")

    if isinstance(drops, dict) and any(v > 0 for v in drops.values()):
        console.print(f"\n  [yellow]Events dropped during recording:[/yellow]")
        for event_type, count in drops.items():
            if count > 0:
                console.print(f"    [yellow]{event_type}:[/yellow] {count}")

    if metrics is None:
        console.print("\n[dim]No system metrics available (recorded before metrics feature).[/dim]")
        return

    static = metrics.get("static", {})
    if static:
        console.print(Panel("[bold]System Info[/bold]"))
        for key, val in static.items():
            if key == "displays":
                for i, d in enumerate(val):
                    console.print(f"  [#60a5fa]display {i}:[/#60a5fa] {d.get('width')}x{d.get('height')}")
            elif key == "locale" and isinstance(val, dict):
                console.print(f"  [#60a5fa]locale:[/#60a5fa]")
                for lk, lv in val.items():
                    if isinstance(lv, list):
                        console.print(f"    [#60a5fa]{lk}:[/#60a5fa] {', '.join(str(x) for x in lv)}")
                    elif isinstance(lv, dict):
                        console.print(f"    [#60a5fa]{lk}:[/#60a5fa] {lv}")
                    else:
                        console.print(f"    [#60a5fa]{lk}:[/#60a5fa] {lv}")
            elif key == "running_applications" and isinstance(val, list):
                console.print(f"  [#60a5fa]running apps:[/#60a5fa]")
                for app in val:
                    v = f" v{app['version']}" if app.get("version") else ""
                    console.print(f"    {app['name']} ({app['bundle_id']}){v}")
            elif key == "wifi" and isinstance(val, dict):
                console.print(f"  [#60a5fa]wifi:[/#60a5fa]")
                for wk, wv in val.items():
                    console.print(f"    [#60a5fa]{wk}:[/#60a5fa] {wv}")
            else:
                console.print(f"  [#60a5fa]{key}:[/#60a5fa] {val}")

    for phase in ("start", "end"):
        snapshot = metrics.get(phase)
        if snapshot:
            console.print(Panel(f"[bold]{phase.title()} Snapshot[/bold]"))
            for key, val in snapshot.items():
                if key == "wifi" and isinstance(val, dict):
                    console.print(f"  [#60a5fa]wifi:[/#60a5fa]")
                    for wk, wv in val.items():
                        console.print(f"    [#60a5fa]{wk}:[/#60a5fa] {wv}")
                else:
                    console.print(f"  [#60a5fa]{key}:[/#60a5fa] {val}")
        elif phase == "end":
            console.print(f"\n  [dim]No end snapshot (recording may have been interrupted).[/dim]")


def _export_one(
    recording_dir,
    output_path,
    exclude_moves,
    err_console,
    privacy_filter=None,
    *,
    include_network: bool = False,
):
    """Export a single recording. Returns event count, or -1 on error.

    When *privacy_filter* is supplied, it is applied to window.switch events
    during export — used by the ``--privacy-filter`` flag (Unit 4d).

    ``include_network`` defaults to ``False`` for cloud safety. The CLI
    ``screencap export`` command sets it to True ONLY when the user
    explicitly directed the output to ``--stdout`` or to a custom path
    via ``-o``. When the export writes to the default
    ``<recording_dir>/events.jsonl``, the file is the same one
    ``screencap upload`` later picks up, so emitting network rows there
    would leak metadata to the cloud bucket — bypassing the
    "no network rows reach cloud until V1.75" gate.

    V1.5: when ``include_network=True`` AND the recording has encrypted
    network bodies (a ``network_event`` row with non-NULL
    ``body_ciphertext`` exists), construct a
    :class:`NetworkScrubPipeline` for decrypt+scrub of bodies at the
    row-conversion boundary. Pipeline construction triggers a Keychain
    prompt on first call and a silent read thereafter ("Always Allow"
    trusted-binary extension); failure raises
    :class:`KekUnavailableError` and we fail-loud per the V1.5 ticket's
    "KEK-missing semantics" lock.
    """
    import logging
    from pathlib import Path

    from screencap.exporter import ExportError, build_export_metadata, export_recording
    from screencap.network.export_pipeline import (
        KekUnavailableError,
        NetworkScrubPipeline,
        recording_has_encrypted_bodies,
    )

    logger = logging.getLogger(__name__)

    meta = build_export_metadata(exclude_moves)

    # V1.5: per-recording pipeline construction. The cheap ciphertext
    # check skips the Keychain prompt for V1-vintage AND for V1.5
    # metadata-only recordings (allowlist-miss). When ``include_network``
    # is False (the cloud-safe default for ``<recording_dir>/events.jsonl``)
    # we skip pipeline construction entirely — there's no point paying
    # the Keychain prompt cost only to drop the rows we'd decrypt.
    network_scrub_pipeline = None
    # The caller sets include_network only when the user has explicitly
    # directed output away from the cloud-pickup default. We may still
    # need to drop it back to False in the broad-Exception fallback
    # path below: when pipeline construction fails defensively, leaving
    # include_network=True with a null pipeline would feed capture-side
    # events with body_ciphertext bytes to Pydantic JSON serialization,
    # violating the V1.5 invariant that ciphertext can never reach
    # JSONL by construction. Track the safe-to-emit flag locally so
    # the fallback can degrade by suppressing network rows entirely.
    db_path = str(Path(recording_dir) / "recording.db")
    try:
        if include_network and Path(db_path).exists():
            from screencap.engine.db import (
                _ensure_network_tables,
                get_engine,
                get_session_for_path,
            )
            from screencap.engine.db.models import Recording

            # Ensure the table exists (legacy DBs predate the feature).
            try:
                engine = get_engine(f"sqlite:///{db_path}")
                _ensure_network_tables(engine)
                engine.dispose()
            except Exception:
                pass
            # Look up the recording_id from the DB and check for the
            # meta row. The recording_id is a fixed integer per
            # recording.db; one Recording row per file.
            session = get_session_for_path(db_path)
            try:
                rec = session.query(Recording).first()
                recording_id = rec.id if rec is not None else None
            finally:
                session.close()
            if recording_id is not None and recording_has_encrypted_bodies(
                db_path, recording_id,
            ):
                try:
                    network_scrub_pipeline = NetworkScrubPipeline(
                        db_path, recording_id,
                    )
                except KekUnavailableError as e:
                    err_console.print(
                        f"[red]Error:[/red] Cannot decrypt network bodies: {e}. "
                        "Run `screencap network uninstall && screencap start "
                        "--network` to regenerate (existing encrypted bodies "
                        "will be lost).",
                    )
                    return -1
    except KekUnavailableError:
        # Re-raise wrapped errors -- the inner block already printed
        # the actionable message. Fall through to return -1.
        return -1
    except Exception as e:
        # Defensive: any unexpected error setting up the pipeline check
        # should not crash the export. Drop network rows entirely from
        # this export (include_network=False) — keeping include_network
        # true with pipeline=None would feed capture-side events with
        # raw ciphertext bytes to Pydantic's JSON serializer, violating
        # the V1.5 schema invariant. KekUnavailableError above is the
        # fail-loud branch; this is the fail-closed branch for
        # everything else.
        logger.debug("Skipping network rows in export: %s", e)
        if include_network:
            err_console.print(
                "[yellow]Warning:[/yellow] could not set up network scrub "
                "pipeline; export will omit network events for this "
                "recording.",
            )
            include_network = False

    try:
        return export_recording(
            recording_dir,
            output_path,
            exclude_moves,
            metadata=meta,
            privacy_filter=privacy_filter,
            include_network=include_network,
            network_scrub_pipeline=network_scrub_pipeline,
        )
    except ExportError as e:
        err_console.print(f"[red]Error:[/red] {e}")
        return -1


def _build_export_privacy_filter(recording_dir):
    """Build a privacy filter for ``--privacy-filter`` exports.

    Resolves the recording's privacy mode from ``<recording_dir>/.recording_intent``
    first (todo 001) — that's the mode the user was recording under at capture
    time and the only mode whose semantics correctly describe what's safe to
    export. Falls back to the current ``[privacy].mode`` from ``config.toml``
    only when the intent file is absent (legacy recordings predating the
    intent file); a stderr warning surfaces the fallback.

    ``cloud_intent=False`` because CLI exports are local-only by default;
    cloud-bound paths use the dedicated ``build_cloud_window_filter``
    constructor.
    """
    from pathlib import Path as _Path

    from screencap.privacy.filter import build_local_window_filter

    err_console = Console(stderr=True)
    mode = None

    # Recording-time mode from .recording_intent (canonical source).
    intent_path = _Path(recording_dir) / ".recording_intent"
    if intent_path.exists():
        try:
            import json as _json
            intent_data = _json.loads(intent_path.read_text())
            recording_mode = intent_data.get("privacy_mode")
            if recording_mode:
                mode = str(recording_mode)
        except (OSError, ValueError):
            pass

    if mode is None:
        # Fallback for recordings without .recording_intent — surface to the
        # user that we're using current config, which may be stricter or
        # looser than the recording-time posture.
        try:
            from screencap.config import _load_toml
            privacy_section = (_load_toml().get("privacy") or {})
            mode = privacy_section.get("mode") or "internal"
        except Exception:
            mode = "internal"
        rec_name = (
            recording_dir.name if hasattr(recording_dir, "name")
            else str(recording_dir)
        )
        err_console.print(
            f"[yellow]Warning:[/yellow] No .recording_intent in "
            f"{rec_name} — "
            f"applying current config mode={mode!r}. Re-record under the desired "
            f"mode for accurate filtering."
        )
        # Machine-parseable mirror of the warning (todo 010): when stdout is
        # piped to a parser the Rich prose above is unreadable, so emit a
        # tagged JSON line on stderr that an agent batch-running
        # ``export --all --privacy-filter`` can grep for to enumerate
        # legacy-fallback recordings without screen-scraping rich output.
        if not sys.stdout.isatty():
            try:
                import json as _json
                sys.stderr.write(_json.dumps({
                    "type": "warn",
                    "code": "no_recording_intent",
                    "recording": rec_name,
                    "fallback_mode": mode,
                }) + "\n")
                sys.stderr.flush()
            except Exception:
                pass

    return build_local_window_filter(
        privacy_mode=mode,
        capture_dir=recording_dir,
    )


def _find_exportable_dirs(base_dir):
    """Find recording directories eligible for export under *base_dir*."""
    if not base_dir.is_dir():
        return []
    return sorted(
        d for d in base_dir.iterdir()
        if d.is_dir()
        and (d / "recording.db").exists()
    )


@cli.command()
@click.argument("name", required=False, default=None)
@click.option("--all", "all_recordings", is_flag=True, help="Export all recordings.")
@click.option("--downloads", is_flag=True, help="Include downloaded recordings (with --all) or search downloads dir.")
@click.option("--output", "-o", type=click.Path(), default=None,
              help="Output file path. Default: events.jsonl in the recording directory.")
@click.option("--stdout", "use_stdout", is_flag=True, default=False,
              help="Write to stdout instead of a file.")
@click.option("--exclude-moves", is_flag=True, default=False,
              help="Exclude mouse move events from output.")
@click.option("--privacy-filter", "privacy_filter_enabled", is_flag=True, default=False,
              help="Apply window-event privacy filter (suppress EXCLUDE app windows, "
                   "mask MASK_WINDOW titles). Off by default — turn on for SwiftUI viewer "
                   "or other downstream consumers that need privacy-filtered events.")
def export(name, all_recordings, downloads, output, use_stdout, exclude_moves, privacy_filter_enabled):
    """Export recording events as JSONL for training.

    WARNING: Export includes all captured keystrokes (passwords, API keys,
    private messages). Review recordings for sensitive data before sharing.
    """
    from screencap.config import get_recordings_dir, resolve_recording_dir

    err_console = Console(stderr=True)

    batch_mode = all_recordings or (downloads and not name)

    if not name and not batch_mode:
        err_console.print("[red]Error:[/red] Provide a recording name or use --all / --downloads.")
        sys.exit(1)

    if batch_mode and (use_stdout or output):
        err_console.print("[red]Error:[/red] --all/--downloads cannot be used with --stdout or -o.")
        sys.exit(1)

    try:
        from screencap.engine import Capture  # noqa: F401
    except ImportError:
        err_console.print(_RECORD_EXTRAS_MSG)
        raise SystemExit(1)

    err_console.print(
        "[yellow]Warning:[/yellow] Export includes all captured keystrokes. "
        "Review recordings for sensitive data before sharing.",
        highlight=False,
    )

    if batch_mode:
        from screencap.config import get_downloads_dir

        dirs = []
        if all_recordings:
            dirs.extend(_find_exportable_dirs(get_recordings_dir()))
        if downloads:
            dirs.extend(_find_exportable_dirs(get_downloads_dir()))

        if not dirs:
            err_console.print("[dim]No recordings found.[/dim]")
            return

        total = len(dirs)
        exported = 0
        failed = 0
        for i, rec_dir in enumerate(dirs, 1):
            err_console.print(f"\n[bold][{i}/{total}][/bold] {rec_dir.name}")
            out = str(rec_dir / "events.jsonl")
            pf = _build_export_privacy_filter(rec_dir) if privacy_filter_enabled else None
            # --all writes to recording_dir/events.jsonl, the same file
            # `screencap upload` picks up. Network rows must NOT land
            # there per the V1.5 cloud-safety gate.
            count = _export_one(
                rec_dir,
                out,
                exclude_moves,
                err_console,
                privacy_filter=pf,
                include_network=False,
            )
            if count >= 0:
                err_console.print(f"Exported {count} events to [bold]{out}[/bold]")
                if count == 0:
                    err_console.print(f"[yellow]Warning:[/yellow] Recording '{rec_dir.name}' contains no events.")
                exported += 1
            else:
                failed += 1

        err_console.print(f"\n[bold]Done.[/bold] {exported} exported, {failed} failed.")
        if failed:
            sys.exit(1)
        return

    # Single recording — check recordings dir first, then downloads
    try:
        recording_dir = resolve_recording_dir(name)
    except ValueError:
        err_console.print("[red]Error:[/red] Invalid recording name.")
        sys.exit(1)

    if not recording_dir.exists() and downloads:
        from screencap.config import get_downloads_dir
        recording_dir = get_downloads_dir() / name

    if not recording_dir.exists():
        err_console.print(f"[red]Error:[/red] Recording not found: {name}")
        sys.exit(1)

    # Resolve output destination. Network rows are emitted ONLY when
    # the user explicitly directs the output somewhere other than the
    # default ``<recording_dir>/events.jsonl`` — that file is the same
    # one ``screencap upload`` picks up, and emitting network metadata
    # there would bypass the V1.5 cloud-safety gate.
    if use_stdout:
        output_path = None
        include_network = True
    elif output:
        output_path = output
        include_network = True
    else:
        output_path = str(recording_dir / "events.jsonl")
        include_network = False

    pf = _build_export_privacy_filter(recording_dir) if privacy_filter_enabled else None
    count = _export_one(
        recording_dir,
        output_path,
        exclude_moves,
        err_console,
        privacy_filter=pf,
        include_network=include_network,
    )
    if count < 0:
        sys.exit(1)
    if output_path:
        err_console.print(f"Exported {count} events to [bold]{output_path}[/bold]")
    if count == 0:
        err_console.print("[yellow]Warning:[/yellow] Recording contains no events.")


@cli.command()
@click.option("--json", "as_json", is_flag=True,
              default=lambda: _should_default_to_json(),
              help="Emit machine-readable JSON to stdout (no styling, no rich output). "
                   "Auto-detected when stdout is not a TTY.")
@click.option("--include-spotlight", is_flag=True, default=False,
              help="Also scan via mdfind (slower, more complete). Default: filesystem-only.")
def apps(as_json, include_spotlight):
    """List installed macOS apps with their privacy-resolved actions.

    Wraps app_discovery.discover_installed_apps() and evaluates the privacy
    matrix (with user overrides) for each. Designed for the SwiftUI Privacy
    pane to render per-app toggles and state badges.

    Output (per app, when --json is set):
      bundle_id, display_name, path, icon_path, context_class,
      classification_source, resolved_action, in_exclude_apps,
      in_allow_apps, is_matrix_exclude, has_per_frame_overrides

    has_per_frame_overrides is true when ``mask_domains`` or
    ``mask_title_patterns`` is non-empty in config.toml. The per-app
    ``resolved_action`` is computed without per-frame domain / title
    context, so an ``allow`` row can still produce ``mask_window`` at
    runtime when one of those overrides fires. SwiftUI should decorate
    "Allowed*" badges accordingly.
    """
    import json as _json

    err_console = Console(stderr=True)

    try:
        from screencap.app_discovery import auto_classify_detailed, discover_installed_apps
        from screencap.privacy.actions import PrivacyAction
        from screencap.privacy.policy import (
            ContextResult,
            DefaultPolicyEvaluator,
            FrameMetadata,
            PrivacyMode,
            get_matrix_action,
        )
    except ImportError:
        err_console.print(_RECORD_EXTRAS_MSG)
        raise SystemExit(1)

    try:
        from screencap.config import get_privacy_config
        privacy_cfg = get_privacy_config()
    except Exception:
        from screencap.privacy.policy import PrivacyConfig
        privacy_cfg = PrivacyConfig()

    evaluator = DefaultPolicyEvaluator(privacy_cfg)

    try:
        installed = discover_installed_apps(use_spotlight=include_spotlight)
    except Exception as exc:
        # Uniform JSON envelope (todo 020) + non-zero exit on JSON error
        # (todo 019) — agents that check exit code first don't silently
        # skip a discovery failure as if it were an empty result.
        if as_json:
            sys.stdout.write(_json.dumps({
                "ok": False,
                "schema_version": _APPS_SCHEMA_VERSION,
                "apps": [],
                "error": str(exc),
            }) + "\n")
            sys.stdout.flush()
        else:
            err_console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1)

    # ``has_per_frame_overrides`` (todo 008, schema v2): per-app
    # ``resolved_action`` is computed from FrameMetadata that only carries
    # ``bundle_id`` + ``display_name``, so it cannot reflect ``mask_domains``
    # (policy step 3) or ``mask_title_patterns`` (policy step 4) — those
    # only fire against per-frame ``domain`` / ``window_title`` at capture
    # time. Surface the existence of those rules so a SwiftUI badge for an
    # ``allow``-resolved app can decorate "*" with a tooltip warning.
    has_per_frame_overrides = bool(
        privacy_cfg.mask_domains or privacy_cfg.mask_title_patterns,
    )

    rows = []
    for meta in installed:
        classification = auto_classify_detailed(meta)
        ctx_class = classification.context_class
        is_matrix_exclude = all(
            get_matrix_action(ctx_class, m) == PrivacyAction.EXCLUDE
            for m in PrivacyMode
        )
        frame = FrameMetadata(bundle_id=meta.bundle_id, window_title=meta.display_name)
        # auto_classify_detailed returns ClassificationResult; the evaluator
        # expects ContextResult — bridge the two by constructing a fresh
        # ContextResult that carries the bundle ID as evidence.
        ctx_result = ContextResult(
            context_class=ctx_class,
            confidence="bundle_id",
            evidence=meta.bundle_id,
        )
        decision = evaluator.evaluate(ctx_result, frame)

        rows.append({
            "bundle_id": meta.bundle_id,
            "display_name": meta.display_name,
            "path": meta.path,
            "icon_path": "",  # populated by SwiftUI from .app/Contents/Resources/<icon>
            "context_class": ctx_class.value,
            "classification_source": classification.source,
            "resolved_action": decision.action.value,
            "in_exclude_apps": meta.bundle_id in privacy_cfg.exclude_apps,
            "in_allow_apps": meta.bundle_id in privacy_cfg.allow_apps,
            "is_matrix_exclude": is_matrix_exclude,
            "has_per_frame_overrides": has_per_frame_overrides,
        })

    if as_json:
        sys.stdout.write(_json.dumps({
            "ok": True,
            "schema_version": _APPS_SCHEMA_VERSION,
            "apps": rows,
        }) + "\n")
        sys.stdout.flush()
        return

    console.print(f"\n[bold]{len(rows)} apps installed[/bold]\n")
    for row in rows:
        badge = "[red]EXCLUDE[/red]" if row["resolved_action"] == "exclude" else (
            "[yellow]MASK[/yellow]" if "mask" in row["resolved_action"] else "[green]ALLOW[/green]"
        )
        console.print(f"  {badge} {row['display_name']} ({row['bundle_id']})")
    console.print()


@cli.command()
@click.option("--json", "as_json", is_flag=True,
              default=lambda: _should_default_to_json(),
              help="Emit machine-readable JSON to stdout (no styling, no rich output). "
                   "Auto-detected when stdout is not a TTY.")
@click.option("--no-nlp-check", is_flag=True, default=False,
              help="Skip the are_nlp_models_cached() probe. SwiftUI / agents "
                   "polling at 1Hz can pass this to shave fixed cost off the "
                   "hot path (todo 018). nlp_models_cached field is reported "
                   "as null when skipped.")
def status(as_json, no_nlp_check):
    """Report recording state without IPC.

    Reads the flock-protected ``recording.lock`` content (Unit 3) and the
    config flags. Designed for SwiftUI's 1Hz poll loop — light dependencies,
    no SessionController spawn, no AppKit. Always exits 0.
    """
    import json as _json
    import time as _time
    from typing import TypedDict

    from screencap.pidfile import LOCK_FILE, lock_is_active, read_lock_metadata

    class StatusPayload(TypedDict):
        """Schema for `screencap status --json` output.

        Symmetric: every key is always present so SwiftUI's parser doesn't
        need conditional unwraps. Unknown values are ``None`` / ``False``.
        Uniform envelope (todo 020): `ok` + `schema_version` lead the payload.
        """
        ok: bool
        schema_version: int
        is_recording: bool
        started_at: float | None
        elapsed: float | None
        capture_dir: str | None
        claimant: str | None
        warning: str | None
        privacy_configured: bool
        nlp_models_cached: bool | None  # None when --no-nlp-check is set

    # Two distinct facts about the lock:
    #   1. lock_is_active() — flock probe. True iff a process holds the lock.
    #      In session mode this is the controller's lifetime, NOT a single
    #      recording's lifetime.
    #   2. recording_started_at in metadata — set by SessionController on
    #      _on_start_click, cleared on _on_stop_click. The canonical
    #      "is a recording capture in progress?" signal.
    #
    # SwiftUI's elapsed-time UI must read recording_started_at, not the
    # controller-init started_at. Without this distinction, the previous
    # design left status reporting is_recording=true and a stale elapsed
    # time after the user clicked Stop in the menubar (the controller
    # was still alive between recordings).
    flock_held = lock_is_active()
    metadata = read_lock_metadata()
    recording_started_at = None
    if metadata is not None:
        rec_ts = metadata.get("recording_started_at")
        if isinstance(rec_ts, (int, float)):
            recording_started_at = float(rec_ts)
    is_recording = flock_held and recording_started_at is not None

    payload: StatusPayload = {
        "ok": True,
        # Independent from stderr-event schema version (todo 009) — status
        # payload evolves separately.
        "schema_version": _STATUS_SCHEMA_VERSION,
        "is_recording": is_recording,
        "started_at": None,
        "elapsed": None,
        "capture_dir": None,
        "claimant": None,
        "warning": None,
        "privacy_configured": False,
        "nlp_models_cached": False,
    }
    if no_nlp_check:
        # Caller opted out of the cache probe (todo 018). Report null so
        # consumers can distinguish "skipped by request" from "checked,
        # not cached" (False).
        payload["nlp_models_cached"] = None

    if is_recording and metadata is not None:
        # started_at + elapsed track THE recording, not the controller —
        # so back-to-back recordings each report a fresh elapsed time.
        payload["started_at"] = recording_started_at
        payload["elapsed"] = max(0.0, _time.time() - recording_started_at)
        if metadata.get("capture_dir"):
            payload["capture_dir"] = metadata["capture_dir"]
        if metadata.get("claimant"):
            payload["claimant"] = metadata["claimant"]
    elif not flock_held and LOCK_FILE.exists():
        # Lock file exists but flock probe says no holder. Distinguish:
        #   - file unparseable (corrupt JSON) → metadata is None
        #   - file present + parseable + no holder → metadata is dict
        if metadata is None:
            payload["warning"] = "lock_unparseable"
        else:
            payload["warning"] = "lock_stale"

    # Config readiness flags — cheap and useful for first-run UI.
    try:
        from screencap.config import _load_toml
        privacy_section = (_load_toml().get("privacy") or {})
        payload["privacy_configured"] = bool(privacy_section)
    except Exception:
        pass

    if not no_nlp_check:
        try:
            from screencap.privacy import are_nlp_models_cached
            payload["nlp_models_cached"] = bool(are_nlp_models_cached())
        except Exception:
            pass

    if as_json:
        sys.stdout.write(_json.dumps(payload) + "\n")
        sys.stdout.flush()
        return

    # Pretty output for human callers.
    if payload["is_recording"]:
        elapsed = payload.get("elapsed")
        if elapsed is not None:
            console.print(f"[#22d3ee]Recording[/#22d3ee] — {int(elapsed)}s elapsed")
        else:
            console.print("[#22d3ee]Recording[/#22d3ee] — (start time unknown)")
        if payload.get("capture_dir"):
            console.print(f"  Capture dir: {payload['capture_dir']}")
        if payload.get("claimant"):
            console.print(f"  Claimant: {payload['claimant']}")
    else:
        console.print("[dim]Not recording.[/dim]")


@cli.command()
@click.option("--force", is_flag=True, help="Skip SIGTERM and go straight to SIGKILL.")
@click.option("--json", "as_json", is_flag=True,
              default=lambda: _should_default_to_json(),
              help="Emit machine-readable JSON to stdout instead of prose. "
                   "Auto-detected when stdout is not a TTY (todo 009).")
def stop(force, as_json):
    """Stop recording processes.

    \b
    Without --force: send SIGTERM to the lock owner, wait up to 30s for
    a clean shutdown, then fall through to an orphan-children scan.

    \b
    With --force: SIGKILL the lock owner directly (so a hung-but-alive
    SessionController can be taken down — find_orphaned_processes returns
    nothing while the parent lives), then run the orphan-children scan.
    Use this only when the graceful path has already failed; previously
    --force was a no-op against a live parent and is now actively
    destructive against the recording session.

    \b
    --json envelope (todo 009):
      {ok, schema_version, action, pid, killed,
       orphans_terminated, error}
    action ∈ {"sigterm", "sigkill", "none", "no_owner"}.
    """
    import os as _os
    import signal as _signal
    import time as _time

    # Track outcome across exit points so the JSON envelope describes the
    # actual lifecycle (todo 009). Updated in place by the SIGTERM /
    # SIGKILL / orphan-scan branches; emitted from the helper below before
    # every return / SystemExit.
    _stop_outcome: dict = {
        "action": "none",
        "pid": None,
        "killed": False,
        "orphans_terminated": 0,
        "error": None,
    }

    def _emit_stop_result(*, ok: bool, exit_code: int = 0) -> None:
        if as_json:
            import json as _json
            payload = {
                "ok": ok,
                "schema_version": _STOP_SCHEMA_VERSION,
                **_stop_outcome,
            }
            sys.stdout.write(_json.dumps(payload) + "\n")
            sys.stdout.flush()
        if exit_code:
            raise SystemExit(exit_code)

    try:
        from screencap.pidfile import (
            _is_screencap_process,
            _pid_exists,
            delete_pidfile,
            find_orphaned_processes,
            read_lock_metadata,
            read_pidfile,
            terminate_processes,
        )
    except ImportError:
        if not as_json:
            console.print(_RECORD_EXTRAS_MSG)
        _stop_outcome["error"] = "record_extras_not_installed"
        _emit_stop_result(ok=False, exit_code=1)
        return

    # Identify the lock owner. Prefer the flock-protected lock metadata
    # (Unit 3) — it's the canonical owner and works correctly for
    # multiprocessing.spawn workers (per
    # docs/tickets/high-2026-03-10-fix-orphan-detection-spawn-workers.md).
    # Fall back to legacy recording.pid for back-compat with any holder
    # that hasn't migrated.
    #
    # This identification step runs in BOTH the graceful and --force paths.
    # Without it, --force would skip straight to find_orphaned_processes(),
    # which returns [] when the SessionController parent is alive — meaning
    # `screencap stop --force` against a hung-but-alive controller did
    # nothing.
    lock_meta = read_lock_metadata()
    parent_pid = None
    lock_started_at = None
    if lock_meta and lock_meta.get("pid"):
        parent_pid = lock_meta["pid"]
        lock_started_at = lock_meta.get("started_at")
    else:
        data = read_pidfile()
        if data and data.get("parent_pid"):
            parent_pid = data["parent_pid"]
            lock_started_at = data.get("started_at")

    # PID-recycle guard (todo 007) — verify the live PID's create_time is
    # within tolerance of the lock metadata's started_at. macOS recycles
    # PIDs aggressively (~99k space), so a long-dead recorder's PID could
    # belong to an unrelated process by the time the user runs `stop`.
    # Applies to both graceful and --force paths — force-killing the wrong
    # process is even worse than SIGTERM-ing it.
    if parent_pid is not None and lock_started_at is not None:
        try:
            import psutil as _psutil
            proc_create_time = _psutil.Process(parent_pid).create_time()
            # 60s tolerance (todo 014): both lock_started_at (time.time at
            # claim_lock) and psutil.Process.create_time on macOS are wall-
            # clock — a forward NTP step between claim and stop (common on
            # laptops resuming from sleep) would otherwise null the legit
            # parent_pid and silently turn `screencap stop` into a no-op
            # because find_orphaned_processes() returns [] while the parent
            # lives. Real PID recycles after process death involve much
            # larger elapsed times; 60s absorbs routine NTP jumps without
            # weakening the guard's intent.
            if proc_create_time > float(lock_started_at) + 60.0:
                console.print(
                    f"[yellow]Warning:[/yellow] PID {parent_pid} appears recycled "
                    f"(process started >60s after the recording's lock metadata). "
                    f"Skipping direct kill; falling through to orphan scan."
                )
                parent_pid = None
        except (_psutil.NoSuchProcess, _psutil.AccessDenied, _psutil.ZombieProcess):
            # No process at that PID — nothing to stop directly; fall
            # through to the orphan scan.
            parent_pid = None
        except Exception:
            # Unexpected psutil failure: stay conservative and skip the
            # direct kill rather than risk killing an unrelated process.
            parent_pid = None

    if parent_pid is not None and _pid_exists(parent_pid) and _is_screencap_process(parent_pid):
        _stop_outcome["pid"] = int(parent_pid)
        if force:
            # --force path: SIGKILL the lock owner directly. This is the
            # only way to take down a hung-but-alive SessionController, since
            # find_orphaned_processes() returns [] while the parent lives.
            _stop_outcome["action"] = "sigkill"
            if not as_json:
                console.print(f"[yellow]Force-killing recording (PID {parent_pid})...[/yellow]")
            try:
                _os.kill(parent_pid, _signal.SIGKILL)
            except (ProcessLookupError, PermissionError) as exc:
                # Surface as a non-zero exit (todo 009): an agent that
                # invokes ``stop --force`` and gets exit 0 will assume the
                # session is dead. PermissionError most commonly means the
                # PID belongs to a different uid (recycled across users).
                _stop_outcome["error"] = f"sigkill_failed:{type(exc).__name__}:{exc}"
                if not as_json:
                    console.print(f"[red]Failed to SIGKILL PID {parent_pid}: {exc}[/red]")
                _emit_stop_result(ok=False, exit_code=1)
                return
            else:
                # Brief wait for the kernel to reap; then confirm.
                for _ in range(10):
                    if not _pid_exists(parent_pid):
                        break
                    _time.sleep(0.1)
                if not _pid_exists(parent_pid):
                    _stop_outcome["killed"] = True
                    if not as_json:
                        console.print("[#22d3ee]Recording force-stopped.[/#22d3ee]")
                else:
                    _stop_outcome["error"] = "sigkill_uninterruptible"
                    if not as_json:
                        console.print(
                            f"[yellow]Warning:[/yellow] PID {parent_pid} still alive after "
                            "SIGKILL — process may be uninterruptible (D-state)."
                        )
            delete_pidfile()
            # Fall through to orphan scan in case force-kill left children.
        else:
            _stop_outcome["action"] = "sigterm"
            if not as_json:
                console.print(f"Sending stop signal to recording (PID {parent_pid})...")
            try:
                _os.kill(parent_pid, _signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            else:
                # Wait for graceful shutdown (up to 30s) with progress
                _timed_out = True
                try:
                    with console.status(
                        "[dim]Waiting for recording to stop "
                        "(post-processing may take a moment)...[/dim]"
                    ) as _wait_status:
                        for _tick in range(60):
                            if not _pid_exists(parent_pid):
                                _timed_out = False
                                break
                            if _tick == 20:  # 10s elapsed
                                _wait_status.update(
                                    "[dim]Still waiting... use [bold]screencap stop --force[/bold] "
                                    "to kill immediately[/dim]"
                                )
                            _time.sleep(0.5)
                except KeyboardInterrupt:
                    if not as_json:
                        console.print(
                            "\n[yellow]Interrupted — escalating to force kill.[/yellow]\n"
                            "[dim]Tip: [bold]screencap stop --force[/bold] "
                            "skips the graceful wait[/dim]"
                        )
                if not _timed_out or not _pid_exists(parent_pid):
                    _stop_outcome["killed"] = True
                    if not as_json:
                        console.print("[#22d3ee]Recording stopped.[/#22d3ee]")
                    delete_pidfile()
                    _emit_stop_result(ok=True)
                    return
                if not as_json:
                    console.print("[yellow]Graceful stop timed out — falling back to force kill.[/yellow]")

    orphans = find_orphaned_processes()
    if not orphans:
        if _stop_outcome["action"] == "none":
            _stop_outcome["action"] = "no_owner"
        if not as_json:
            console.print("[dim]No orphaned recording processes found.[/dim]")
        _emit_stop_result(ok=True)
        return

    if not as_json:
        console.print(f"Found {len(orphans)} orphaned recording process(es).")
    terminated = terminate_processes(orphans, force=force)
    _stop_outcome["orphans_terminated"] = len(terminated)
    if terminated:
        _stop_outcome["killed"] = True

    if not as_json:
        for entry in terminated:
            console.print(f"  Terminated {entry.get('name', 'unknown')} (PID {entry['pid']})... done")
        if terminated:
            console.print(f"Cleaned up {len(terminated)} process(es).")
        else:
            console.print("[yellow]Could not terminate any processes.[/yellow]")

    delete_pidfile()
    _emit_stop_result(ok=True)


@cli.command()
def update():
    """Check for and install updates."""
    from screencap.updater import (
        get_latest_version,
        is_update_available,
        perform_update,
    )

    if not getattr(sys, "frozen", False):
        console.print("[yellow]Self-update is only available for standalone binary installs.[/yellow]")
        console.print("Use [bold]pip install --upgrade screencap[/bold] instead.")
        return

    console.print(f"Current version: {__version__}")
    latest = get_latest_version()

    if latest is None:
        console.print("[red]Could not reach update server.[/red]")
        return

    if not is_update_available(latest):
        console.print("[green]Already up to date.[/green]")
        return

    console.print(f"New version available: {latest}")
    if perform_update(latest):
        console.print("Restart screencap to use the new version.")


# ---------------------------------------------------------------------------
# transcribe
# ---------------------------------------------------------------------------

_WHISPER_MODELS = {
    "1": ("tiny", "~39 MB", "Fast, lower accuracy"),
    "2": ("base", "~140 MB", "Good balance (recommended)"),
    "3": ("small", "~466 MB", "Better accuracy"),
    "4": ("medium", "~1.5 GB", "High accuracy"),
    "5": ("large", "~2.9 GB", "Best accuracy"),
}


def _resolve_backend_interactive() -> tuple[str, str | None]:
    """Discover API key / prompt user and return (backend, api_key_or_model).

    Returns:
        ("api", api_key) — use OpenAI API with this key
        ("local", model_name) — use local whisper
    """
    import os

    # 1. Check for existing API key
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        try:
            from screencap.engine.config import settings
            api_key = settings.openai_api_key
        except Exception:
            pass

    if api_key:
        console.print("[green]Found OpenAI API key[/green]")
        if click.confirm(
            "Use OpenAI API for transcription? (faster, costs ~$0.006/min)",
            default=True,
        ):
            return "api", api_key
        # user declined — fall through to local
        return _select_local_model()

    # 2. No key found — offer choices
    console.print("[dim]No OpenAI API key found.[/dim]")
    choice = click.prompt(
        "Choose an option\n"
        "  1. Enter OpenAI API key\n"
        "  2. Transcribe locally with Whisper\n"
        "Choice",
        type=click.IntRange(1, 2),
        default=2,
    )

    if choice == 1:
        key = click.prompt("OpenAI API key", hide_input=True)
        return "api", key

    return _select_local_model()


_MODEL_RANK = ["large", "medium", "small", "base", "tiny"]


def _detect_cached_models() -> list[str]:
    """Return whisper model names already downloaded, best-quality first."""
    from pathlib import Path
    import os

    found = set()

    # faster-whisper: ~/.cache/huggingface/hub/models--Systran--faster-whisper-{name}/
    hf_cache = Path(os.environ.get("HF_HUB_CACHE", "")) if os.environ.get("HF_HUB_CACHE") else (
        Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    )
    if hf_cache.is_dir():
        for entry in hf_cache.iterdir():
            if entry.is_dir() and entry.name.startswith("models--Systran--faster-whisper-"):
                model = entry.name.split("faster-whisper-", 1)[1]
                if model in _MODEL_RANK:
                    found.add(model)

    # openai-whisper: ~/.cache/whisper/{name}.pt
    whisper_cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "whisper"
    if whisper_cache.is_dir():
        for entry in whisper_cache.iterdir():
            if entry.suffix == ".pt":
                model = entry.stem.split(".")[0]  # handles "base.en.pt" → "base"
                if model in _MODEL_RANK:
                    found.add(model)

    return [m for m in _MODEL_RANK if m in found]


def _ensure_whisper_backend() -> None:
    """Make sure at least one local whisper backend is installed."""
    try:
        import faster_whisper  # noqa: F401
        return
    except ImportError:
        pass
    try:
        import whisper  # noqa: F401
        return
    except ImportError:
        pass

    if getattr(sys, 'frozen', False):
        console.print("[red]Whisper dependencies missing from binary. Reinstall screencap.[/red]")
    else:
        console.print("[red]Local transcription requires whisper dependencies.[/red]")
        console.print("Run: pip install faster-whisper")
    raise SystemExit(1)


def _select_local_model(model: str | None = None) -> tuple[str, str]:
    """Pick a local whisper model. Auto-detects cached models to skip prompts.

    Args:
        model: Explicit model name (from --model flag). Skips all detection/prompts.

    Returns ("local", model_name).
    """
    _ensure_whisper_backend()

    # Explicit --model flag: use it directly
    if model:
        return "local", model

    # Auto-detect cached models
    cached = _detect_cached_models()
    if cached:
        best = cached[0]
        console.print(f"[green]Using cached Whisper model:[/green] {best}")
        if len(cached) > 1:
            others = ", ".join(cached[1:])
            console.print(f"[dim]Also available locally: {others}. Use --model to switch.[/dim]")
        return "local", best

    # Nothing cached — show selection menu
    console.print("\nAvailable models:")
    for num, (name, size, desc) in _WHISPER_MODELS.items():
        console.print(f"  {num}. {name:8s} ({size:8s}) — {desc}")

    choice = click.prompt(
        "Select model",
        type=click.IntRange(1, 5),
        default=2,
    )
    model_name = _WHISPER_MODELS[str(choice)][0]
    return "local", model_name


def _transcribe_api_inline(api_key, audio_path, transcript_path, transcript_json_path):
    """Transcribe using OpenAI Whisper API with an explicit api_key.

    Avoids delegating to screencap.engine (whose pydantic-settings singleton
    ignores os.environ changes after import).
    """
    from openai import OpenAI

    client = OpenAI(api_key=api_key)

    with open(audio_path, "rb") as audio_file:
        result = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            response_format="verbose_json",
            timestamp_granularities=["segment"],
        )

    transcript = result.text.strip()

    segments = []
    for segment in getattr(result, "segments", []) or []:
        segments.append({
            "start": segment.start,
            "end": segment.end,
            "text": segment.text.strip(),
        })

    # Save files directly (no print() calls — caller controls output)
    transcript_path.write_text(transcript, encoding="utf-8")
    transcript_json_path.write_text(
        json.dumps({"text": transcript, "segments": segments}, indent=2),
        encoding="utf-8",
    )


def _run_api_transcription(api_key, audio_path, transcript_path, transcript_json_path):
    """Try API transcription, retry with new key on auth failure, or fall back to local."""
    while True:
        try:
            with console.status("[bold]Transcribing with OpenAI API ...[/bold]"):
                _transcribe_api_inline(
                    api_key, audio_path, transcript_path, transcript_json_path
                )
            return  # success
        except Exception as e:
            is_auth_error = "401" in str(e) or "invalid_api_key" in str(e)
            if not is_auth_error:
                console.print(f"[red]Transcription failed:[/red] {e}")
                sys.exit(1)

            console.print(f"[red]Invalid API key.[/red]")
            choice = click.prompt(
                "What would you like to do?\n"
                "  1. Enter a different API key\n"
                "  2. Transcribe locally with Whisper instead\n"
                "  3. Cancel\n"
                "Choice",
                type=click.IntRange(1, 3),
                default=1,
            )

            if choice == 1:
                api_key = click.prompt("OpenAI API key", hide_input=True).strip()
                continue
            elif choice == 2:
                _, model = _select_local_model()
                try:
                    try:
                        import faster_whisper  # noqa: F401
                        from screencap.engine.cli import _transcribe_faster_whisper
                        local_fn = _transcribe_faster_whisper
                    except ImportError:
                        from screencap.engine.cli import _transcribe_local
                        local_fn = _transcribe_local

                    console.print(f"[dim]Using Whisper ({model} model)...[/dim]")
                    local_fn(audio_path, transcript_path, transcript_json_path, model)
                except Exception as exc:
                    console.print(f"[red]Transcription failed:[/red] {exc}")
                    sys.exit(1)
                return
            else:
                console.print("[dim]Transcription cancelled.[/dim]")
                sys.exit(0)


def _recover_chunk_metadata(
    recording_dir: Path, console: "Console", *, force: bool = False,
    cloud_bound: bool,
) -> None:
    """Generate per-chunk manifests + events JSONL when chunks exist but metadata doesn't.

    This is a recovery path for when ChunkProcessor failed during recording
    but chunk video files were created. Uses recording.db to derive chunk
    time ranges and generate the metadata files the Cloud Run processor needs.

    The ``cloud_bound`` keyword argument is REQUIRED (no default) — omitting
    it raises ``TypeError`` at the call site rather than silently falling
    open. ``screencap upload`` always passes ``cloud_bound=True`` regardless
    of ``.recording_intent`` content, because at upload time the data IS
    becoming cloud-bound by user choice. This closes the local-then-uploaded
    threat case (recording captured as ``destination=local``, later uploaded).

    LOAD-BEARING ORDERING: ``_recover_chunk_metadata`` MUST be followed by
    ``scrub_recording`` for cloud-bound recordings before upload. The
    scrub-layer pointer suppression (Unit 2) only protects recovered
    cloud-bound JSONL when this ordering holds. The upload command's
    ``cli.py:1614 → 1641-1646`` sequencing satisfies this. Reordering or
    adding a recovery path that bypasses the scrubber MUST replicate the
    in-interval ``mouse.move`` drop at the engine layer or the cloud-bound
    privacy posture silently degrades.

    Skip-on-error policy: if ``unified_export_events`` raises mid-chunk on
    a corrupt action_event row that trips ``process_events`` aggregate-state
    or ``interleave_window_events``, we log a warning and skip writing
    that chunk's ``events_NNNN.jsonl`` (no file written). Other chunks
    proceed normally. ``screencap upload --force`` re-runs recovery once
    underlying data is fixed. Recovery's previous "raw dump tolerates
    everything" behaviour is intentionally retired by this refactor.
    """
    from screencap.recording_db import Row, has_column, has_table, open_recording_db

    chunk_videos = sorted(recording_dir.glob("chunk_*.mp4"))
    if not chunk_videos:
        return  # not a chunked recording

    db_path = recording_dir / "recording.db"
    if not db_path.exists():
        return

    # Check which chunks are missing manifests and/or events
    missing_manifests = []
    missing_events = []
    for vf in chunk_videos:
        # Extract index from filename: chunk_0000.mp4 → 0
        idx_str = vf.stem.split("_")[1]
        try:
            idx = int(idx_str)
        except ValueError:
            continue
        if not (recording_dir / f"chunk_{idx:04d}_manifest.json").exists() or force:
            missing_manifests.append(idx)
        if not (recording_dir / f"events_{idx:04d}.jsonl").exists() or force:
            missing_events.append(idx)

    if not missing_manifests and not missing_events:
        return

    # Stale .tmp cleanup sweep: a previous SIGKILL/OOM may have left
    # .tmp files for chunks we're about to re-recover.
    # write_events_jsonl / generate_manifest also clean their own .tmp,
    # but this defense-in-depth sweep covers chunks we plan to skip
    # (e.g. corrupt-row failures) where the writer is never reached.
    for idx in missing_events:
        (recording_dir / f"events_{idx:04d}.jsonl.tmp").unlink(missing_ok=True)
    for idx in missing_manifests:
        (recording_dir / f"chunk_{idx:04d}_manifest.json.tmp").unlink(missing_ok=True)

    # Derive chunk time ranges from recording.db
    # Per-recording click thresholds (P2 fix): the chunk processor and the
    # CLI export both pass the recording's own ``double_click_*`` values to
    # the unified callable. Recovery must do the same — otherwise a
    # recording with non-default thresholds (e.g. interval=0.3s) would have
    # its recovered JSONL emit different click merges than the original
    # chunk export, breaking the byte-identical contract for non-default
    # configurations. Falls back to engine defaults (0.5s / 5px) when the
    # threshold columns are NULL or the row is absent.
    double_click_interval = 0.5
    double_click_distance = 5.0
    try:
        with open_recording_db(db_path, row_factory=Row) as conn:
            # Build the SELECT column list, gating per-recording click
            # thresholds on their presence in the schema. Older recording.db
            # files predate these columns; without the guard, an unconditional
            # SELECT raises OperationalError which the outer except catches
            # and silently aborts recovery for the entire recording (P2 bug).
            # Mirrors chunk_processor._load_click_thresholds' fail-soft
            # fallback to engine defaults (0.5s / 5px).
            has_thresholds = (
                has_column(conn, "recording", "double_click_interval_seconds")
                and has_column(conn, "recording", "double_click_distance_pixels")
            )
            if has_thresholds:
                rec = conn.execute(
                    "SELECT timestamp, double_click_interval_seconds, "
                    "double_click_distance_pixels FROM recording LIMIT 1"
                ).fetchone()
                if rec:
                    if rec["double_click_interval_seconds"] is not None:
                        double_click_interval = float(rec["double_click_interval_seconds"])
                    if rec["double_click_distance_pixels"] is not None:
                        double_click_distance = float(rec["double_click_distance_pixels"])
            else:
                rec = conn.execute(
                    "SELECT timestamp FROM recording LIMIT 1"
                ).fetchone()
            if not rec:
                return
            rec_start = rec["timestamp"]

            # Get the first and last action event timestamps
            first_evt = conn.execute("SELECT MIN(timestamp) as ts FROM action_event").fetchone()
            last_evt = conn.execute("SELECT MAX(timestamp) as ts FROM action_event").fetchone()
            if not first_evt or first_evt["ts"] is None:
                return

            first_ts = first_evt["ts"]
            last_ts = last_evt["ts"]
            n_chunks = len(chunk_videos)

            # Determine chunk duration from config
            from screencap.config import get_chunk_duration
            chunk_dur = get_chunk_duration()
            if chunk_dur <= 0:
                # Estimate from recording span and chunk count
                chunk_dur = (last_ts - first_ts) / max(n_chunks, 1)

            # Compute chunk boundaries: chunk N covers [start + N*dur, start + (N+1)*dur)
            # Use recording start (or first event) as the base
            base_ts = min(rec_start, first_ts)
            chunk_ranges = []
            for idx in range(n_chunks):
                c_start = base_ts + idx * chunk_dur
                c_end = base_ts + (idx + 1) * chunk_dur
                if idx == n_chunks - 1:
                    c_end = max(c_end, last_ts + 1.0)  # last chunk extends to cover all events
                chunk_ranges.append((idx, c_start, c_end))
    except Exception as e:
        console.print(f"  [yellow]Warning:[/yellow] Could not derive chunk ranges: {e}")
        return

    # Generate missing manifests
    if missing_manifests:
        with console.status("[dim]Generating chunk manifests...[/dim]"):
            from screencap.config import get_segmentation_mode
            from screencap.task_manifest import generate_manifest

            generated = 0
            seg_mode = get_segmentation_mode()
            for idx, c_start, c_end in chunk_ranges:
                if idx in missing_manifests:
                    try:
                        generate_manifest(recording_dir, idx, c_start, c_end, segmentation_mode=seg_mode)
                        generated += 1
                    except Exception as e:
                        console.print(f"  [yellow]Warning:[/yellow] Manifest generation failed for chunk {idx}: {e}")
            if generated:
                console.print(f"  [dim]Generated {generated} chunk manifest(s)[/dim]")

    # Generate missing per-chunk events via the unified export pipeline
    if missing_events:
        with console.status("[dim]Exporting per-chunk events...[/dim]"):
            from screencap.engine.export import unified_export_events
            from screencap.exporter import build_export_metadata, write_events_jsonl
            from screencap.privacy.filter import build_cloud_window_filter

            # Resolve privacy_mode: prefer the locked-at-record-time value
            # in .recording_intent (matches what the live chunk processor
            # used) and fall back to current config when the intent file
            # is missing/corrupt.
            privacy_mode = _read_intent_privacy_mode(recording_dir)
            if privacy_mode is None:
                try:
                    from screencap.config import get_privacy_config
                    privacy_mode = get_privacy_config().mode.value
                except Exception:
                    privacy_mode = "internal"

            exported = 0
            try:
                with open_recording_db(db_path, row_factory=Row) as conn:
                    action_disabled_clause = (
                        " AND (disabled IS NULL OR NOT disabled)"
                        if has_column(conn, "action_event", "disabled") else ""
                    )
                    has_window_table = has_table(conn, "window_event")
                    window_disabled_clause = (
                        " AND (disabled IS NULL OR NOT disabled)"
                        if has_window_table and has_column(conn, "window_event", "disabled")
                        else ""
                    )

                    for idx, c_start, c_end in chunk_ranges:
                        if idx not in missing_events:
                            continue

                        jsonl_path = recording_dir / f"events_{idx:04d}.jsonl"

                        try:
                            # Build the cloud window filter inside the
                            # per-chunk try so a config-load failure
                            # (e.g. InvalidPrivacyConfigError, which is
                            # not caught inside build_privacy_filter)
                            # marks one chunk as failed instead of
                            # aborting the whole recovery (R5/Unit 1).
                            window_filter = build_cloud_window_filter(
                                cloud_bound=cloud_bound,
                                privacy_mode=privacy_mode,
                                capture_dir=recording_dir,
                            )

                            # Action SELECT with R16 disabled filter
                            action_rows = [
                                dict(r) for r in conn.execute(
                                    "SELECT * FROM action_event "
                                    "WHERE timestamp >= ? AND timestamp < ?"
                                    + action_disabled_clause
                                    + " ORDER BY timestamp",
                                    (c_start, c_end),
                                ).fetchall()
                            ]

                            # Window SELECT with R16 disabled filter
                            window_rows: list[dict] = []
                            initial_window_row: dict | None = None
                            if has_window_table:
                                window_rows = [
                                    dict(r) for r in conn.execute(
                                        "SELECT * FROM window_event "
                                        "WHERE timestamp >= ? AND timestamp < ?"
                                        + window_disabled_clause
                                        + " ORDER BY timestamp",
                                        (c_start, c_end),
                                    ).fetchall()
                                ]
                                # Initial window context: last window event
                                # before chunk start, with timestamp rewritten
                                # to fall just before the chunk so it appears
                                # first after dedup/interleave.
                                init_row = conn.execute(
                                    "SELECT * FROM window_event "
                                    "WHERE timestamp < ?"
                                    + window_disabled_clause
                                    + " ORDER BY timestamp DESC LIMIT 1",
                                    (c_start,),
                                ).fetchone()
                                if init_row is not None:
                                    initial_window_row = dict(init_row)
                                    initial_window_row["timestamp"] = c_start - 0.001

                            # Run the unified callable. Returns an iterator;
                            # write_events_jsonl streams it to disk atomically.
                            # Pass per-recording click thresholds (P2 fix) so
                            # recovery's merge behaviour matches the live chunk
                            # processor for recordings with non-default
                            # double_click_* values.
                            events_iter = unified_export_events(
                                action_rows,
                                window_rows,
                                initial_window_row=initial_window_row,
                                double_click_interval=double_click_interval,
                                double_click_distance=double_click_distance,
                                window_filter=window_filter,
                            )

                            meta = build_export_metadata(exclude_moves=False)
                            write_events_jsonl(jsonl_path, events_iter, meta)
                            exported += 1
                        except Exception as e:
                            # Skip-on-error: a corrupt row that trips
                            # process_events or interleave invalidates this
                            # chunk's export. Other chunks proceed normally.
                            # write_events_jsonl already removed any partial
                            # .tmp; no events_NNNN.jsonl is written.
                            console.print(
                                f"  [yellow]Warning:[/yellow] Event export failed for chunk {idx}: {e}"
                            )
            except Exception as e:
                console.print(f"  [yellow]Warning:[/yellow] Could not export chunk events: {e}")
            if exported:
                console.print(f"  [dim]Exported events for {exported} chunk(s)[/dim]")


def _read_intent_privacy_mode(recording_dir: Path) -> str | None:
    """Read ``privacy_mode`` from ``.recording_intent``, or ``None`` if absent.

    Mirrors :func:`screencap.catalog.read_intent`'s fail-safe behaviour:
    parse errors, missing keys, and missing files all return ``None`` so
    the caller falls back to whatever default it considers safe.
    """
    intent_path = recording_dir / ".recording_intent"
    if not intent_path.exists():
        return None
    try:
        data = json.loads(intent_path.read_text())
    except Exception:
        return None
    mode = data.get("privacy_mode")
    if isinstance(mode, str):
        return mode
    return None


@cli.command()
@click.argument("names", nargs=-1)
@click.option("--all", "all_recordings", is_flag=True, help="Upload all recordings.")
@click.option("--dry-run", is_flag=True, help="Show files and sizes without uploading.")
@click.option("--force", is_flag=True, help="Re-upload even if already uploaded.")
@click.option("--jobs", "-j", type=click.IntRange(min=1), default=4,
              help="Parallel file transfers per recording (default: 4).")
@click.option("--no-delete", is_flag=True, default=False,
              help="Keep local recording files after upload instead of auto-deleting.")
def upload(names, all_recordings, dry_run, force, jobs, no_delete):
    """Upload recordings to cloud storage."""
    from screencap.upload import resolve_recording_dirs, upload_recording, _fmt_size

    try:
        dirs = resolve_recording_dirs(names, all_recordings=all_recordings)
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    # --- Intent warnings ---
    from screencap.catalog import read_intent

    for d in dirs:
        intent = read_intent(d)
        if intent == "local" and not dry_run:
            console.print(
                f"  [yellow]Note:[/yellow] {d.name} is local-intent — post-hoc "
                "scrubbing provides weaker guarantees than capture-time enforcement."
            )

    total_count = len(dirs)
    all_uploaded = 0
    all_skipped = 0
    all_failed = 0
    all_bytes = 0

    # Warn if any recordings will need export
    if not dry_run:
        needs_export = any(
            not (d / "events.jsonl").exists() or force for d in dirs
        )
        if needs_export:
            console.print(
                "[yellow]Warning:[/yellow] Auto-export includes all captured keystrokes. "
                "Review recordings for sensitive data before sharing.",
                highlight=False,
            )

    for i, d in enumerate(dirs, 1):
        if total_count > 1:
            console.print(f"\n[bold][{i}/{total_count}][/bold] {d.name}")

        # Auto-export events.jsonl if missing (or --force)
        # Skip if per-chunk JSONL already exists (chunked mode)
        if not dry_run:
            has_chunk_events = any(d.glob("events_*.jsonl"))
            jsonl_path = d / "events.jsonl"
            if not has_chunk_events and (not jsonl_path.exists() or force):
                with console.status("[dim]Exporting events...[/dim]"):
                    try:
                        from screencap.exporter import export_recording, build_export_metadata
                        meta = build_export_metadata(exclude_moves=False)
                        count = export_recording(d, str(jsonl_path), exclude_moves=False, metadata=meta)
                        console.print(f"  [dim]Exported {count} events to events.jsonl[/dim]")
                        if count == 0:
                            console.print("[yellow]Warning:[/yellow] Recording contains no events.")
                    except Exception as e:
                        console.print(f"  [yellow]Warning:[/yellow] Export failed ({e}), uploading without events.jsonl")

            # Recovery: generate per-chunk manifests + events if chunks exist but metadata doesn't.
            # cloud_bound=True UNCONDITIONALLY: at upload time, the data IS becoming
            # cloud-bound by user choice — regardless of what `.recording_intent`
            # records. This closes the local-then-uploaded threat case (a recording
            # captured as destination=local, later uploaded by the user). The
            # `cloud_bound` keyword is REQUIRED on _recover_chunk_metadata so a
            # forgotten argument is a TypeError, not a silent fail-OPEN.
            #
            # LOAD-BEARING ORDERING (do not reorder): this `_recover_chunk_metadata`
            # call MUST be followed by `scrub_recording` below (lines 1782-1786).
            # The scrub-layer pointer suppression (Unit 2) only protects recovered
            # cloud-bound JSONL when this ordering holds. Reordering or adding a
            # recovery path that bypasses the scrubber must replicate the
            # in-interval mouse.move drop at the engine layer.
            _recover_chunk_metadata(d, console, force=force, cloud_bound=True)

            # Recovery: generate sentinel file if missing (crash/force-quit recovery)
            _sentinel_path = d / "recording_complete.json"
            if not _sentinel_path.exists() and any(d.glob("chunk_*_manifest.json")):
                try:
                    from screencap.chunk_processor import _build_sentinel_data
                    _rec_id_path = d / ".recording_id"
                    _rec_name = _rec_id_path.read_text().strip() if _rec_id_path.exists() else d.name
                    _chunk_count = len(list(d.glob("chunk_*_manifest.json")))
                    show_on_website = True
                    _intent_path = d / ".recording_intent"
                    if _intent_path.exists():
                        try:
                            show_on_website = json.loads(_intent_path.read_text()).get("show_on_website", True)
                        except Exception:
                            pass
                    _sentinel_data = _build_sentinel_data(
                        recording_name=_rec_name,
                        stop_reason="manual_upload",
                        chunks_expected=_chunk_count,
                        show_on_website=show_on_website,
                    )
                    _sentinel_path.write_text(json.dumps(_sentinel_data, indent=2))
                except Exception:
                    pass

            # Always scrub before upload
            try:
                from screencap.scrubber import scrub_recording

                with console.status(f"[bold]Scrubbing {d.name} for upload...[/bold]"):
                    scrub_result = scrub_recording(d.name)
                entity_total = sum(scrub_result.entity_counts.values())
                console.print(
                    f"  Scrubbed copy at [dim]{scrub_result.output_dir.name}/[/dim] "
                    f"({entity_total} redaction(s) applied)."
                )
                d = scrub_result.output_dir
            except Exception as e:
                console.print(
                    f"[red]Error:[/red] Scrubbing failed: {e}\n"
                    "Upload skipped — cannot upload without scrubbing."
                )
                all_failed += 1
                continue

        try:
            result = upload_recording(d, dry_run=dry_run, force=force, jobs=jobs)
            all_uploaded += len(result.uploaded)
            all_skipped += len(result.skipped)
            all_failed += len(result.failed)
            all_bytes += result.total_bytes

            if not dry_run and not result.failed:
                parts = []
                if result.uploaded:
                    parts.append(f"{len(result.uploaded)} new")
                if result.skipped:
                    parts.append(f"{len(result.skipped)} skipped")
                summary = ", ".join(parts) if parts else "0 files"
                if result.gcs_prefix:
                    console.print(
                        f"\n[green]Uploaded {d.name}[/green] -> {result.gcs_prefix} ({summary})"
                    )
                else:
                    console.print(f"\n[green]Uploaded {d.name}[/green] ({summary})")
                # Print viewer URLs
                _rec_name = result.recording or d.name
                _raw_url = f"https://screencap.sh/?source=recordings&recording={_rec_name}#data"
                _session_url = f"https://screencap.sh/?source=sessions&recording={_rec_name}#data"
                console.print(
                    f"  [dim]View (raw):[/dim] [link={_raw_url}]{_raw_url}[/link]"
                )
                console.print(
                    f"  [dim]View (processed, ~2 min):[/dim] [link={_session_url}]{_session_url}[/link]"
                )
        except FileNotFoundError as e:
            console.print(f"[red]Error:[/red] {e}")
            all_failed += 1
        except RuntimeError as e:
            console.print(f"[red]Error:[/red] {e}")
            sys.exit(1)

    if total_count > 1 and not dry_run:
        console.print(
            f"\n[bold]Done.[/bold] {all_uploaded} uploaded, "
            f"{all_skipped} skipped, {all_failed} failed "
            f"({_fmt_size(all_bytes)} total)"
        )


@cli.command()
@click.argument("names", nargs=-1)
@click.option("--dest", default=None, help="Destination directory (default: ~/.screencap/downloads/).")
@click.option("--dry-run", is_flag=True, help="Show what would be downloaded without downloading.")
@click.option("--force", is_flag=True, help="Re-download all recordings, ignoring markers.")
@click.option("--jobs", "-j", type=click.IntRange(min=1), default=4,
              help="Parallel file transfers per recording (default: 4).")
@click.option("--sessions", is_flag=True, help="Download processed sessions instead of raw recordings.")
@click.option("--category", "filter_category", default=None,
              type=click.Choice(["development", "communication", "research",
                                 "admin", "creative", "other"], case_sensitive=False),
              help="Only download task folders matching this category (requires --sessions).")
def download(names, dest, dry_run, force, jobs, sessions, filter_category):
    """Download recordings from cloud storage.

    Optionally pass one or more recording NAMES to download only those.
    With no names, all remote recordings are listed and downloaded.
    """
    from screencap.download import (
        _fmt_size,
        _resolve_dest_dir,
        download_recording,
        list_remote_recordings,
    )

    if filter_category and not sessions:
        console.print("[red]Error:[/red] --category requires --sessions")
        sys.exit(1)

    source = "sessions" if sessions else "recordings"

    if sessions and not dest:
        from screencap.config import get_sessions_dir
        try:
            dest_dir = get_sessions_dir()
        except OSError as e:
            console.print(f"[red]Error:[/red] Cannot create sessions directory: {e}")
            sys.exit(1)
    else:
        try:
            dest_dir = _resolve_dest_dir(dest)
        except RuntimeError as e:
            console.print(f"[red]Error:[/red] {e}")
            sys.exit(1)

    if names:
        from types import SimpleNamespace
        remote = [SimpleNamespace(name=n, total_size=0, file_count=0) for n in names]
    else:
        try:
            remote = list_remote_recordings(source=source)
        except RuntimeError as e:
            console.print(f"[red]Error:[/red] {e}")
            sys.exit(1)

        if not remote:
            label = "sessions" if sessions else "recordings"
            console.print(f"No {label} available for download.")
            return

    label = "session" if sessions else "recording"
    console.print(
        f"Found [bold]{len(remote)}[/bold] {label}(s) to download."
    )

    all_downloaded = 0
    all_skipped = 0
    all_failed = 0
    all_bytes = 0

    for i, rec in enumerate(remote, 1):
        if len(remote) > 1:
            console.print(
                f"\n[bold][{i}/{len(remote)}][/bold] {rec.name} "
                f"({_fmt_size(rec.total_size)}, {rec.file_count} files)"
            )
        try:
            result = download_recording(
                rec.name, dest_dir, dry_run=dry_run, force=force, jobs=jobs,
                source=source, category_filter=filter_category,
            )
            all_downloaded += len(result.downloaded)
            all_skipped += len(result.skipped)
            all_failed += len(result.failed)
            all_bytes += result.total_bytes

            if not dry_run and not result.failed and result.downloaded:
                console.print(
                    f"  [green]Downloaded {rec.name}[/green] "
                    f"({len(result.downloaded)} files, {_fmt_size(result.total_bytes)})"
                )
        except FileNotFoundError as e:
            console.print(f"  [red]Error:[/red] {e}")
            all_failed += 1
        except RuntimeError as e:
            console.print(f"  [red]Error:[/red] {e}")
            all_failed += 1

    if not dry_run:
        console.print(
            f"\n[bold]Done.[/bold] {all_downloaded} downloaded, "
            f"{all_skipped} skipped, {all_failed} failed "
            f"({_fmt_size(all_bytes)} total)"
        )


@cli.command()
@click.argument("name")
@click.option(
    "--model", "-m",
    type=click.Choice(["tiny", "base", "small", "medium", "large"], case_sensitive=False),
    default=None,
    help="Whisper model to use (skips prompts, forces local).",
)
def transcribe(name, model):
    """Transcribe audio from a recording using Whisper."""
    from screencap.config import get_recordings_dir

    recording_dir = get_recordings_dir() / name
    audio_path = recording_dir / "audio.flac"

    if not audio_path.exists():
        console.print(
            f"[red]Error:[/red] No audio found for recording '{name}'. "
            "Was it recorded with audio enabled?"
        )
        sys.exit(1)

    # Fix #8: reject empty/corrupt audio
    if audio_path.stat().st_size < 1024:
        console.print(
            "[red]Error:[/red] Audio file is empty or too small to transcribe."
        )
        sys.exit(1)

    transcript_path = recording_dir / "transcript.txt"
    transcript_json_path = recording_dir / "transcript.json"

    # Fix #7: warn before overwriting
    if transcript_path.exists() or transcript_json_path.exists():
        if not click.confirm("Transcript already exists. Overwrite?", default=False):
            console.print("[dim]Transcription cancelled.[/dim]")
            return

    # --model flag → skip all interactive prompts, go straight to local
    if model:
        backend, backend_param = _select_local_model(model)
    else:
        backend, backend_param = _resolve_backend_interactive()

    if backend == "api":
        _run_api_transcription(
            backend_param, audio_path, transcript_path, transcript_json_path
        )
    else:
        try:
            # Fix #3: resolve import FIRST, then call
            try:
                import faster_whisper  # noqa: F401
                from screencap.engine.cli import _transcribe_faster_whisper
                local_fn = _transcribe_faster_whisper
            except ImportError:
                from screencap.engine.cli import _transcribe_local
                local_fn = _transcribe_local

            # Fix #4: no console.status() — screencap.engine prints its own progress
            console.print(f"[dim]Using Whisper ({backend_param} model)...[/dim]")
            local_fn(audio_path, transcript_path, transcript_json_path, backend_param)
        except Exception as e:
            console.print(f"[red]Transcription failed:[/red] {e}")
            sys.exit(1)

    # Fix #2: hard-check that output was actually created
    if not transcript_path.exists():
        console.print(
            "[red]Error:[/red] Transcription completed but no transcript file was created. "
            "Check the logs above for errors."
        )
        sys.exit(1)

    # Show results
    console.print(f"\n[green]Transcript saved:[/green] {transcript_path}")
    console.print(f"[green]Timestamps saved:[/green] {transcript_json_path}")
    text = transcript_path.read_text().strip()
    if text:
        preview = text[:500] + ("..." if len(text) > 500 else "")
        console.print(f"\n[bold]Preview:[/bold]\n{preview}")


@cli.command()
@click.option("--scan", is_flag=True, help="Rescan and show only new (unconfigured) apps.")
@click.option("--show", is_flag=True, help="Display current classifications (read-only).")
@click.option("--reset", is_flag=True, help="Remove [privacy] section after confirmation.")
def setup(scan, show, reset):
    """Configure privacy settings with an interactive wizard."""
    from screencap.setup_wizard import (
        reset_privacy_config,
        run_setup_wizard,
        show_current_config,
    )

    if show:
        show_current_config()
        return

    if reset:
        reset_privacy_config()
        return

    run_setup_wizard(scan_only=scan)


@cli.command()
@click.argument("name")
@click.option(
    "--pii-engine",
    type=click.Choice(["presidio", "presidio-gliner"]),
    default=None,
    help="PII detection engine (default: auto-detect, prefers GLiNER).",
)
def scrub(name: str, pii_engine: str | None) -> None:
    """Create a privacy-scrubbed copy of a recording."""
    from screencap.scrubber import scrub_recording

    try:
        scrub_recording(name, pii_engine=pii_engine)
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1)
    except ImportError as e:
        console.print(
            "[red]Error: Privacy dependencies are missing.[/red]\n"
            "Reinstall or update screencap."
        )
        console.print(f"[dim]{e}[/dim]")
        raise SystemExit(1)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1)


@cli.group(invoke_without_command=True)
@click.option("--set", "set_pair", default=None, metavar="KEY=VALUE",
              help="Change a setting, e.g. --set show_on_website=true")
@click.option("--json", "as_json", is_flag=True,
              default=lambda: _should_default_to_json(),
              help="Emit settings as JSON. Auto-detected when stdout is not a TTY (todo 012).")
@click.pass_context
def settings(ctx, set_pair, as_json):
    """Show or change ScreenCap configuration.

    \b
    View all settings:
      screencap settings

    \b
    Change a setting:
      screencap settings --set show_on_website=false
      screencap settings --set audio_default=true
      screencap settings --set upload_default=cloud

    \b
    Mutate a privacy list (Unit 4b):
      screencap settings privacy exclude_apps add com.example.foo
      screencap settings privacy allow_apps remove com.tinyspeck.slackmacgap
      screencap settings privacy mode set internal

    \b
    Changeable keys:
      show_on_website    Show recordings on the website (true/false)
      audio_default      Record audio by default (true/false)
      auto_name          LLM auto-naming after recording (true/false)
      upload_default     Default destination (local/cloud/both/ask)
    """
    if ctx.invoked_subcommand is not None:
        # privacy subcommand path — defer to the subcommand handler.
        return
    from screencap.config import (
        _CONFIG_PATH,
        get_audio_default,
        get_auto_delete_after_upload,
        get_auto_name,
        get_chunk_duration,
        get_recordings_dir,
        get_rest_threshold,
        get_show_on_website,
        get_upload_default,
        invalidate_config_cache,
    )

    # --- Set a value ---
    if set_pair:
        if "=" not in set_pair:
            console.print("[red]Error:[/red] Use KEY=VALUE format, e.g. --set show_on_website=false")
            raise SystemExit(1)

        key, raw_value = set_pair.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip()

        # Validate key and parse value
        _BOOL_KEYS = {"show_on_website", "audio_default", "auto_name", "auto_name_local_only",
                       "auto_update", "auto_delete_after_upload", "wifi_metrics", "app_versions"}
        _CHOICE_KEYS = {"upload_default": ("local", "cloud", "both", "ask"),
                         "segmentation_mode": ("llm", "idle")}

        if key in _BOOL_KEYS:
            if raw_value.lower() in ("true", "1", "yes"):
                value = True
            elif raw_value.lower() in ("false", "0", "no"):
                value = False
            else:
                console.print(f"[red]Error:[/red] {key} must be true or false, got: {raw_value}")
                raise SystemExit(1)
        elif key in _CHOICE_KEYS:
            valid = _CHOICE_KEYS[key]
            if raw_value.lower() not in valid:
                console.print(f"[red]Error:[/red] {key} must be one of {valid}, got: {raw_value}")
                raise SystemExit(1)
            value = raw_value.lower()
        else:
            all_keys = sorted(_BOOL_KEYS | set(_CHOICE_KEYS.keys()))
            console.print(f"[red]Error:[/red] Unknown setting: {key}")
            console.print(f"[dim]Available: {', '.join(all_keys)}[/dim]")
            raise SystemExit(1)

        from screencap.setup_wizard import _load_config_toml, _save_config_atomic

        doc = _load_config_toml(_CONFIG_PATH)
        # upload_default lives under [privacy], others are top-level
        if key == "upload_default":
            import tomlkit
            if "privacy" not in doc:
                doc.add("privacy", tomlkit.table())
            doc["privacy"]["upload_default"] = value
        else:
            doc[key] = value
        _save_config_atomic(_CONFIG_PATH, doc)
        invalidate_config_cache()
        console.print(f"  [bold]{key}[/bold] = {value}")
        return

    # --- Display all settings ---
    chunk = get_chunk_duration()
    rest = get_rest_threshold()
    show = get_show_on_website()

    # Structured payload first so the same field set drives both JSON and
    # prose paths. ``settings --json`` (todo 012) is the read-side analogue
    # of the existing ``settings privacy --json`` mutation surface — agents
    # need a stable shape they can diff.
    settings_payload = {
        "show_on_website": bool(show),
        "upload_default": str(get_upload_default()),
        "audio_default": bool(get_audio_default()),
        "auto_name": bool(get_auto_name()),
        "chunk_duration": float(chunk),
        "auto_delete_after_upload": bool(get_auto_delete_after_upload()),
        "rest_threshold_seconds": float(rest),
        "recordings_dir": str(get_recordings_dir()),
    }

    if as_json:
        import json as _json
        sys.stdout.write(_json.dumps({
            "ok": True,
            "schema_version": _SETTINGS_SCHEMA_VERSION,
            "settings": settings_payload,
        }) + "\n")
        sys.stdout.flush()
        return

    if chunk >= 3600:
        chunk_str = f"{chunk:.0f}s ({chunk / 3600:.1f} hour)"
    elif chunk >= 60:
        chunk_str = f"{chunk:.0f}s ({chunk / 60:.0f} min)"
    elif chunk > 0:
        chunk_str = f"{chunk:.0f}s"
    else:
        chunk_str = "disabled (legacy single-file)"

    console.print("\n[bold]ScreenCap Settings[/bold]\n")
    console.print(f"  Show on website:          {'yes' if show else 'no'}")
    console.print(f"  Upload default:           {get_upload_default()}")
    console.print(f"  Audio default:            {'enabled' if get_audio_default() else 'disabled'}")
    console.print(f"  Auto-name:                {'enabled' if get_auto_name() else 'disabled'}")
    console.print(f"  Chunk duration:           {chunk_str}")
    console.print(f"  Auto-delete after upload: {'enabled' if get_auto_delete_after_upload() else 'disabled'}")
    console.print(f"  Rest threshold:           {rest:.0f}s ({rest / 60:.0f} min)")
    console.print(f"  Recordings dir:           {get_recordings_dir()}")
    console.print()
    console.print("[dim]  Change with: screencap settings --set KEY=VALUE[/dim]")
    console.print()


_PRIVACY_LIST_FIELDS = ("exclude_apps", "allow_apps", "mask_domains", "mask_title_patterns")
# `matrix_acknowledged_v2026_04` is NOT exposed here (todo 012) — it's an
# internal migration flag written by `_maybe_prompt_matrix_acknowledgement`
# and should not be flippable from a `screencap settings` invocation.
_PRIVACY_SCALAR_FIELDS = ("mode", "setup_skipped")
_PRIVACY_MAP_FIELDS = ("app_classes",)
# `shared` is reserved for MASK_REGION (not yet implemented); accepting it
# would write an unenforceable value that crashes the next start (todo 011).
_PRIVACY_MODE_VALUES = ("public", "internal")


def _privacy_list_field_value(value: str) -> str:
    """Normalize a list-field value before adding/removing."""
    return value.strip()


def _matrix_blocks_allow_for_class(ctx_class, configured_mode: str) -> "PrivacyAction | None":
    """Return the matrix action if it blocks ``allow_apps`` at this mode, else None.

    Blocks loosening via ``allow_apps`` when the matrix at the user's configured
    mode produces EXCLUDE / MASK_WINDOW / TEXT_REDACT for this class. Without
    this guard, ``allow_apps add com.tinyspeck.slackmacgap`` (CHAT, MASK_WINDOW
    under ``internal``) would silently bypass Unit 7a's strengthening.

    PASSWORD_MANAGER (EXCLUDE in every mode) is always blocked. BANKING
    (MASK_WINDOW under ``internal``) is also blocked. BROWSER_UNVERIFIED
    (ALLOW under ``internal``) is *not* blocked — users can still allow
    a browser explicitly.
    """
    from screencap.privacy.policy import (
        PrivacyAction,
        PrivacyMode,
        get_matrix_action,
    )

    blocking = (
        PrivacyAction.EXCLUDE,
        PrivacyAction.MASK_WINDOW,
        PrivacyAction.TEXT_REDACT,
    )
    try:
        mode = PrivacyMode(configured_mode)
    except ValueError:
        mode = PrivacyMode.INTERNAL
    action = get_matrix_action(ctx_class, mode)
    return action if action in blocking else None


@settings.command("privacy")
@click.argument("field")
@click.argument("op", type=click.Choice(["add", "remove", "set"]))
@click.argument("value")
@click.option("--json", "as_json", is_flag=True,
              default=lambda: _should_default_to_json(),
              help="Emit machine-readable JSON to stdout instead of prose to stderr. "
                   "Auto-detected when stdout is not a TTY (todo 021).")
def settings_privacy(field, op, value, as_json):
    """Mutate a [privacy] field in config.toml (Unit 4b).

    \b
    Valid FIELD names (todo 022):
      list fields (add/remove): exclude_apps, allow_apps, mask_domains,
                                mask_title_patterns
      scalar fields (set):      mode (public|internal), setup_skipped (bool)
      map fields (BUNDLE=CLASS): app_classes

    \b
    Examples:
      screencap settings privacy exclude_apps add com.example.foo
      screencap settings privacy allow_apps remove com.example.bar
      screencap settings privacy mode set internal
      screencap settings privacy app_classes set com.example.foo=chat

    Writes through tomlkit so existing comments and key order are preserved
    (R16 invariant). Idempotent: add of an already-present value is a no-op,
    remove of an absent value is a no-op (both exit 0).

    Validation (todo 022): rejects ``allow_apps add`` for any bundle ID whose
    matrix action at the configured mode is EXCLUDE, MASK_WINDOW, or
    TEXT_REDACT — not just EXCLUDE. Use ``screencap apps --json`` to check
    ``resolved_action`` before attempting allow_apps add.
    """
    import json as _json

    import tomlkit

    # Diagnostics / status messages go on stderr so stdout stays clean for
    # any future structured output.
    err_console = Console(stderr=True)

    def _result(
        ok: bool,
        *,
        exit_code: int = 0,
        error: str | None = None,
        changed: bool = False,
    ):
        """Emit the result and exit. Prose to stderr; JSON to stdout when --json.

        Symmetric envelope (todo 011, schema v2): every payload carries the
        same key set on success AND error — ``ok``, ``schema_version``,
        ``changed``, ``field``, ``op``, ``value``, ``error``. Absent values
        serialize as JSON null. Eliminates the asymmetric branch agents had
        to write under v1, where ``error`` was missing on success and
        ``field``/``op``/``value`` were missing on error. Mirrors the
        "every key always present" contract of ``status --json``.

        ``field``, ``op``, and ``value`` are always populated from the
        outer-scope arguments — the callsite never has to thread them
        through. The post-normalization values are reported (e.g., scalar
        ``parsed_value`` becomes a real bool / lowercased string), which
        is what an agent will care about.
        """
        if as_json:
            try:
                _value: object = parsed_value if is_scalar else value
            except NameError:
                _value = value
            payload = {
                "ok": ok,
                "schema_version": _SETTINGS_PRIVACY_SCHEMA_VERSION,
                "changed": bool(changed),
                "field": field,
                "op": op,
                "value": _value,
                "error": error,
            }
            click.echo(_json.dumps(payload))
        # Prose was already printed via err_console at the call site (or the
        # success block at the end); nothing to do here for prose mode.
        if exit_code:
            raise SystemExit(exit_code)

    field = field.strip()
    is_list = field in _PRIVACY_LIST_FIELDS
    is_scalar = field in _PRIVACY_SCALAR_FIELDS
    is_map = field in _PRIVACY_MAP_FIELDS

    if not (is_list or is_scalar or is_map):
        all_fields = sorted(_PRIVACY_LIST_FIELDS + _PRIVACY_SCALAR_FIELDS + _PRIVACY_MAP_FIELDS)
        err_console.print(f"[red]Error:[/red] Unknown privacy field: {field}")
        err_console.print(f"[dim]Available: {', '.join(all_fields)}[/dim]")
        _result(False, exit_code=1, error=f"unknown_field:{field}")

    if is_list and op == "set":
        err_console.print(f"[red]Error:[/red] {field} is a list — use add/remove, not set.")
        _result(False, exit_code=1, error=f"list_field_set_op:{field}")
    if is_scalar and op != "set":
        err_console.print(f"[red]Error:[/red] {field} is a scalar — use set, not {op}.")
        _result(False, exit_code=1, error=f"scalar_field_bad_op:{field}={op}")

    value = _privacy_list_field_value(value)

    # Scalar normalization & validation
    parsed_value: object = value
    if is_scalar:
        if field == "mode":
            if value.lower() not in _PRIVACY_MODE_VALUES:
                err_console.print(
                    f"[red]Error:[/red] mode must be one of "
                    f"{_PRIVACY_MODE_VALUES}, got: {value}"
                )
                _result(False, exit_code=1, error=f"invalid_mode:{value}")
            parsed_value = value.lower()
        elif field == "setup_skipped":
            if value.lower() in ("true", "1", "yes"):
                parsed_value = True
            elif value.lower() in ("false", "0", "no"):
                parsed_value = False
            else:
                err_console.print(
                    f"[red]Error:[/red] {field} must be true/false, got: {value}"
                )
                _result(False, exit_code=1, error=f"invalid_bool:{field}={value}")

    # Open the config under an advisory flock (todo 015 + 025). The
    # context manager handles load → flock → mutate → atomic-save →
    # invalidate-cache. Two concurrent `screencap settings privacy`
    # invocations now serialize at the flock instead of racing on the
    # read-modify-write cycle.
    with _privacy_config_writer() as doc:
        if "privacy" not in doc:
            doc.add("privacy", tomlkit.table())
        privacy_tbl = doc["privacy"]

        changed = _settings_privacy_apply(
            privacy_tbl=privacy_tbl,
            field=field,
            op=op,
            value=value,
            is_list=is_list,
            is_scalar=is_scalar,
            parsed_value=parsed_value,
            err_console=err_console,
            tomlkit=tomlkit,
            _result=_result,
        )

    if changed:
        err_console.print(f"  [bold]privacy.{field}[/bold] {op} {value}")
        _result(True, changed=True)


def _settings_privacy_apply(
    *,
    privacy_tbl,
    field: str,
    op: str,
    value: str,
    is_list: bool,
    is_scalar: bool,
    parsed_value,
    err_console,
    tomlkit,
    _result,
) -> bool:
    """Apply a single privacy mutation to the in-memory tomlkit privacy table.

    Returns ``True`` when the table was actually changed (caller should
    surface the success result), ``False`` for an idempotent no-op (the
    no-op result was already emitted in-place via ``_result``). Hard
    errors raise ``SystemExit`` via ``_result(exit_code=...)`` and never
    return.

    Extracted from settings_privacy so the read-modify-write helper
    (`_privacy_config_writer`) can wrap it with an advisory flock without
    leaking a hundred-line block into the context manager body. Mutates
    list fields in-place via tomlkit Array's append/remove (todo 016) so
    inline comments and per-item formatting survive.
    """
    # Matrix-invariant guard: reject loosening any matrix-blocked class via
    # allow_apps (todo 005). Evaluated at the *configured mode* — under
    # `internal` this catches CHAT/EMAIL/CALENDAR/VIDEO_CALL (MASK_WINDOW)
    # in addition to PASSWORD_MANAGER (EXCLUDE). Defaults to "internal" when
    # mode is unset.
    #
    # Also consults the on-disk app_classes overrides (todo 030) so that a
    # bundle absent from BUNDLE_ID_MAP but reclassified by the user as a
    # sensitive class (e.g., `app_classes set com.example.foo=password_manager`)
    # cannot be allow-listed in a follow-up call.
    if is_list and field == "allow_apps" and op == "add":
        from screencap.privacy.context import BROWSER_BUNDLE_IDS, BUNDLE_ID_MAP
        from screencap.privacy.policy import ContextClass
        configured_mode = str(privacy_tbl.get("mode") or "internal")

        # Effective class: app_classes override > BUNDLE_ID_MAP > BROWSER_BUNDLE_IDS > None.
        # BROWSER_BUNDLE_IDS resolves to BROWSER_UNVERIFIED at runtime via
        # the classifier's browser detection, so a browser bundle is
        # legitimately allow-listable even though it isn't in BUNDLE_ID_MAP.
        effective_class = None
        app_classes_overrides = dict(privacy_tbl.get("app_classes", {}))
        override_str = app_classes_overrides.get(value)
        if override_str:
            try:
                effective_class = ContextClass(str(override_str).lower())
            except ValueError:
                effective_class = None
        if effective_class is None:
            effective_class = BUNDLE_ID_MAP.get(value)
        if effective_class is None and value in BROWSER_BUNDLE_IDS:
            effective_class = ContextClass.BROWSER_UNVERIFIED

        if effective_class is not None:
            blocking_action = _matrix_blocks_allow_for_class(
                effective_class, configured_mode,
            )
            if blocking_action is not None:
                err_console.print(
                    f"[red]Error:[/red] '{value}' is in {effective_class.value} which the "
                    f"privacy matrix at mode={configured_mode!r} produces "
                    f"{blocking_action.value} — allow_apps cannot loosen this. "
                    f"Set mode=public to capture broadly, or override at the "
                    f"per-app level via app_classes (subject to the same guard)."
                )
                _result(
                    False,
                    exit_code=1,
                    error=f"matrix_blocks_allow:{effective_class.value}@{configured_mode}",
                )
        else:
            # Unknown bundle (no BUNDLE_ID_MAP entry, no app_classes
            # override) — fail-closed (Finding 001 Variant A). The runtime
            # evaluator's strictness floor (policy.py allow_apps step)
            # already blocks loosening when a downstream classifier
            # resolves the bundle to CHAT/EMAIL/etc., so the harm window
            # is narrow. But the CLI add-time path stays explicit: require
            # the user to classify the bundle first, so the matrix can
            # reason about it. Keeps the privacy-first posture symmetric
            # with how PASSWORD_MANAGER and friends are handled.
            err_console.print(
                f"[red]Error:[/red] '{value}' is not in BUNDLE_ID_MAP and has no "
                f"app_classes override — refusing to allow-list an unclassified "
                f"bundle. Run [bold]screencap settings privacy app_classes set "
                f"{value}=<class>[/bold] first (e.g., browser_unverified for an "
                f"AI tool), then add to allow_apps if needed."
            )
            _result(
                False,
                exit_code=1,
                error=f"unknown_bundle_id:{value}",
            )

    if is_list:
        # Mutate the tomlkit Array in place (todo 016) so per-item inline
        # comments and multi-line formatting survive. The previous
        # `list(privacy_tbl.get(field, []))` + reassign approach silently
        # destroyed all tomlkit metadata.
        arr = privacy_tbl.get(field)
        if arr is None:
            arr = tomlkit.array()
            privacy_tbl[field] = arr
        if op == "add":
            if value in arr:
                # Idempotent no-op
                err_console.print(f"[dim]{field} already contains {value} — no change.[/dim]")
                _result(True, changed=False)
                return False
            arr.append(value)
        else:  # remove
            if value not in arr:
                err_console.print(f"[dim]{field} does not contain {value} — no change.[/dim]")
                _result(True, changed=False)
                return False
            arr.remove(value)
    elif is_scalar:
        privacy_tbl[field] = parsed_value
    else:
        # Map field (app_classes) — `add`/`set` use BUNDLE=CLASS syntax;
        # `remove` accepts just the bundle ID (no class required for removal).
        if op == "remove":
            bundle = value.split("=", 1)[0].strip()
            cur = dict(privacy_tbl.get(field, {}))
            existing_override = cur.get(bundle)
            # Matrix-floor guard for `remove` (Finding 001 Variant C).
            # The two-step bypass: `set X=password_manager` (accepted as
            # tightening) → `remove X` (no check) → `allow_apps add X`
            # (effective_class is now None → guard at line 3172 short-
            # circuits → bundle ALLOWed at runtime). Reject the remove if
            # it would loosen the matrix at the configured mode (e.g.,
            # PASSWORD_MANAGER → BUNDLE_ID_MAP fallback or UNKNOWN).
            if existing_override:
                from screencap.privacy.actions import _ACTION_SEVERITY
                from screencap.privacy.context import BUNDLE_ID_MAP as _BUNDLE_MAP
                from screencap.privacy.policy import (
                    ContextClass,
                    PrivacyMode,
                    get_matrix_action,
                )
                configured_mode = str(privacy_tbl.get("mode") or "internal")
                try:
                    _mode_enum = PrivacyMode(configured_mode)
                except ValueError:
                    _mode_enum = PrivacyMode.INTERNAL
                try:
                    old_class = ContextClass(str(existing_override).lower())
                except ValueError:
                    old_class = None
                fallback_class = _BUNDLE_MAP.get(bundle, ContextClass.UNKNOWN)
                if old_class is not None:
                    old_action = get_matrix_action(old_class, _mode_enum)
                    new_action = get_matrix_action(fallback_class, _mode_enum)
                    if _ACTION_SEVERITY[new_action] > _ACTION_SEVERITY[old_action]:
                        err_console.print(
                            f"[red]Error:[/red] removing the '{bundle}' classification "
                            f"would revert it from {old_class.value} ({old_action.value}) "
                            f"to {fallback_class.value} ({new_action.value}) at "
                            f"mode={configured_mode!r} — that loosens the matrix and is "
                            f"rejected. To intentionally weaken, set the bundle to a "
                            f"more permissive class explicitly via app_classes set, "
                            f"which is subject to the same severity guard."
                        )
                        _result(
                            False,
                            exit_code=1,
                            error=(
                                f"matrix_invariant_blocks_remove:"
                                f"{old_class.value}→{fallback_class.value}@{configured_mode}"
                            ),
                        )
            cur.pop(bundle, None)
            privacy_tbl[field] = cur
        else:  # add or set
            if "=" not in value:
                err_console.print(
                    f"[red]Error:[/red] map field {field} requires BUNDLE_ID=CLASS for {op}, got: {value}"
                )
                _result(False, exit_code=1, error=f"map_value_missing_eq:{field}={value}")
            bundle, ctx_str = value.split("=", 1)
            bundle, ctx_str = bundle.strip(), ctx_str.strip()
            # Validate CLASS against ContextClass enum so we don't silently
            # corrupt config with a typo that crashes the next start
            # (todo 010). Accept upper/lower case input; normalize to value.
            from screencap.privacy.context import BUNDLE_ID_MAP
            from screencap.privacy.policy import ContextClass
            valid_classes = {c.value for c in ContextClass}
            normalized = ctx_str.lower()
            if normalized not in valid_classes:
                err_console.print(
                    f"[red]Error:[/red] Unknown context class: {ctx_str}"
                )
                err_console.print(
                    f"[dim]Available: {', '.join(sorted(valid_classes))}[/dim]"
                )
                _result(False, exit_code=1, error=f"unknown_context_class:{ctx_str}")

            # Matrix-EXCLUDE invariant for app_classes (todo 006). Without
            # this guard, a user could `app_classes set com.1password.1password=unknown`
            # to reclassify the bundle out of PASSWORD_MANAGER, then
            # `allow_apps add com.1password.1password` (matrix now ALLOW).
            # Reject any reclassification that would loosen a currently-
            # blocked class. The check evaluates the OLD class (from
            # BUNDLE_ID_MAP or a prior app_classes override) against the
            # matrix at the configured mode — if it's a blocked class, the
            # new class must not be more permissive at that mode.
            old_class = None
            existing_overrides = dict(privacy_tbl.get(field, {}))
            existing_override = existing_overrides.get(bundle)
            if existing_override:
                try:
                    old_class = ContextClass(str(existing_override).lower())
                except ValueError:
                    old_class = None
            if old_class is None:
                old_class = BUNDLE_ID_MAP.get(bundle)
            # Implicit baseline for unknown bundles is UNKNOWN (Finding 001
            # Variant B). The previous gate (`if old_class is not None`)
            # short-circuited for any bundle absent from BUNDLE_ID_MAP and
            # without a prior override, accepting any reclassification —
            # including writing the same UNKNOWN class back, or escalating
            # in either direction without comparison. Treat unknown bundles
            # as if their effective class were UNKNOWN so the severity
            # comparison below applies symmetrically.
            if old_class is None:
                old_class = ContextClass.UNKNOWN

            if old_class is not None:
                configured_mode = str(privacy_tbl.get("mode") or "internal")
                from screencap.privacy.actions import _ACTION_SEVERITY
                from screencap.privacy.policy import (
                    PrivacyMode,
                    get_matrix_action,
                )
                try:
                    _mode_enum = PrivacyMode(configured_mode)
                except ValueError:
                    _mode_enum = PrivacyMode.INTERNAL
                new_class = ContextClass(normalized)
                old_action = get_matrix_action(old_class, _mode_enum)
                new_action = get_matrix_action(new_class, _mode_enum)
                # Compare action SEVERITY directly (not just whether the new
                # action is "blocking"). Higher severity = looser. Reject
                # any reclassification that loosens the matrix at the
                # configured mode — covers EXCLUDE→MASK_WINDOW (e.g.,
                # password_manager → chat) which the previous
                # blocking-vs-non-blocking check missed because both endpoints
                # were "blocking".
                if _ACTION_SEVERITY[new_action] > _ACTION_SEVERITY[old_action]:
                    err_console.print(
                        f"[red]Error:[/red] reclassifying '{bundle}' from "
                        f"{old_class.value} to {new_class.value} would loosen "
                        f"the matrix at mode={configured_mode!r} from "
                        f"{old_action.value} to {new_action.value} — rejected. "
                        f"Set mode=public if you want broader capture, or "
                        f"keep the existing classification."
                    )
                    _result(
                        False,
                        exit_code=1,
                        error=(
                            f"matrix_invariant_blocks_reclassify:"
                            f"{old_class.value}→{new_class.value}@{configured_mode}"
                        ),
                    )

            cur = existing_overrides
            cur[bundle] = normalized
            privacy_tbl[field] = cur

    return True


# ---------------------------------------------------------------------------
# _smoke-test (hidden) — validate critical subsystems load in frozen binary
# ---------------------------------------------------------------------------


def _check_presidio_analyzer() -> tuple[str, bool, str]:
    """Instantiate AnalyzerEngine with no-op NLP (validates bundled YAML recognizer configs).

    Uses a stub NLP engine to avoid triggering spaCy model loading, which in
    a frozen binary calls sys.executable -m pip (i.e. screencap -m pip) and
    crashes with a Click UsageError.  spaCy model loading is tested separately
    by _check_spacy_model.
    """
    import traceback as _tb
    name = "presidio_analyzer"
    try:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngine, NlpArtifacts

        class _NoOpNlpEngine(NlpEngine):
            def load(self): pass
            def is_loaded(self): return True
            def process_text(self, text, language):
                return NlpArtifacts(
                    entities=[], tokens=[], lemmas=[],
                    tokens_indices=[], dependencies=[],
                    keywords=[], language=language,
                )
            def process_batch(self, texts, language, **kwargs):
                for text in texts:
                    yield text, self.process_text(text, language)
            def is_stopword(self, word, language): return False
            def is_punct(self, word, language): return False
            def get_supported_entities(self): return []
            def get_supported_languages(self): return ["en"]

        analyzer = AnalyzerEngine(nlp_engine=_NoOpNlpEngine())
        recognizers = analyzer.registry.get_recognizers(
            language="en", all_fields=True,
        )
        if not recognizers:
            return name, False, "No recognizers loaded — YAML config missing?"
        return name, True, ""
    except Exception:
        return name, False, _tb.format_exc()


def _check_fast_gliner() -> tuple[str, bool, str]:
    """Import fast_gliner (verifies Rust extension + static ONNX)."""
    import traceback as _tb
    name = "fast_gliner"
    try:
        from fast_gliner import FastGLiNER  # noqa: F401
        return name, True, ""
    except Exception:
        return name, False, _tb.format_exc()


def _check_detect_secrets_plugins() -> tuple[str, bool, str]:
    """Instantiate DetectSecretsDetector (validates all plugin submodules load)."""
    import traceback as _tb
    name = "detect_secrets_plugins"
    try:
        from screencap.privacy.secrets import DetectSecretsDetector
        DetectSecretsDetector()
        return name, True, ""
    except Exception:
        return name, False, _tb.format_exc()


def _check_spacy_model() -> tuple[str, bool, str]:
    """Load en_core_web_sm spaCy model (verifies bundled model data).

    In frozen binaries, spacy.load() uses importlib.util.find_spec() which
    can't locate bundled packages.  Importing the package first puts it in
    sys.modules, where spacy.load() checks before find_spec().
    """
    import traceback as _tb
    name = "spacy_model"
    try:
        import en_core_web_sm  # noqa: F401 — ensures sys.modules entry for frozen binary
        import spacy
        spacy.load("en_core_web_sm")
        return name, True, ""
    except Exception:
        return name, False, _tb.format_exc()


def _check_av_codecs() -> tuple[str, bool, str]:
    """Import av and verify libx264 codec (verifies ffmpeg dylibs)."""
    import traceback as _tb
    name = "av_codecs"
    try:
        import av
        av.codec.Codec("libx264", "w")
        return name, True, ""
    except Exception:
        return name, False, _tb.format_exc()


def _check_pynput() -> tuple[str, bool, str]:
    """Import pynput keyboard and mouse listeners (import only)."""
    import traceback as _tb
    name = "pynput"
    try:
        from pynput.keyboard import Listener as KL  # noqa: F401
        from pynput.mouse import Listener as ML  # noqa: F401
        return name, True, ""
    except Exception:
        return name, False, _tb.format_exc()


def _check_sounddevice() -> tuple[str, bool, str]:
    """Import sounddevice (import only — device enumeration needs permission)."""
    import traceback as _tb
    name = "sounddevice"
    try:
        import sounddevice  # noqa: F401
        return name, True, ""
    except Exception:
        return name, False, _tb.format_exc()


def _check_domain_index() -> tuple[str, bool, str]:
    """Instantiate DefaultContextClassifier (exercises build_domain_index → load_ut1_domains)."""
    import traceback as _tb
    name = "domain_index"
    try:
        from screencap.privacy.context import DefaultContextClassifier
        DefaultContextClassifier()
        return name, True, ""
    except Exception:
        return name, False, _tb.format_exc()


def _check_onnxruntime_excluded() -> tuple[str, bool, str]:
    """Verify onnxruntime is NOT importable (confirms exclusion in spec)."""
    name = "onnxruntime_excluded"
    try:
        import onnxruntime  # noqa: F401
        return name, False, "onnxruntime should not be importable but was"
    except ImportError:
        return name, True, ""
    except Exception:
        import traceback as _tb
        return name, False, _tb.format_exc()


def _check_keyring_macos_backend() -> tuple[str, bool, str]:
    """Verify the macOS Keychain backend is importable + selected.

    V1.5 stores the network-body KEK in the user's login keychain via
    keyring. PyInstaller cannot trace `keyring.get_keyring()`'s string-
    based backend lookup, so the spec adds `keyring.backends.macOS` as
    an explicit hidden import. This check confirms the bundle picked it
    up — the import is itself the test, plus a sanity check that the
    runtime backend is not the in-memory fallback (which would silently
    lose the KEK across recorder runs).
    """
    name = "keyring_macos_backend"
    try:
        import traceback as _tb
        import keyring  # noqa: PLC0415
        import keyring.backends.macOS  # noqa: PLC0415, F401
        backend = keyring.get_keyring()
        backend_name = type(backend).__name__
        # On non-Darwin or in test environments the backend may not be
        # the macOS one — but in a real frozen binary on macOS we expect
        # `Keyring` from `keyring.backends.macOS`. Accept any non-fail
        # backend; surface the name in the message for visibility.
        if backend_name in ("fail", "Null"):
            return name, False, (
                f"keyring backend is {backend_name} (no usable backend); "
                f"frozen binary failed to bundle keyring.backends.macOS"
            )
        return name, True, ""
    except Exception:
        return name, False, _tb.format_exc()


@cli.group("network")
def network_group() -> None:
    """Manage the network capture CA + recover from crashes."""


@network_group.command("uninstall")
def network_uninstall_cmd() -> None:
    """Remove the screencap proxy CA from your Keychain + clean up state.

    Restores any orphaned proxy state FIRST (so your network is left
    working regardless of how you got here), then uninstalls the CA
    and deletes ~/.screencap/proxy/.

    Never touches ~/.mitmproxy/ (which may belong to a separate
    mitmproxy install).
    """
    from screencap.network.lifecycle import full_uninstall

    console.print("[bold]Uninstalling screencap network capture...[/bold]")
    full_uninstall()
    console.print("[green]Done.[/green]")


@network_group.command("preload-pin")
@click.argument("host")
def network_preload_pin_cmd(host: str) -> None:
    """Add HOST to the persistent known-pinned-hosts cache.

    The capture addon detects cert-pinned hosts at runtime and adds
    them to ``~/.screencap/known_pinned_hosts.json`` so the next
    recording skips the MITM attempt and tunnels them directly.
    Use this command to seed a host manually without having to
    record once and fail (e.g. an internal banking app you already
    know is pinned).

    Idempotent: re-adding an existing host is a no-op.
    """
    from screencap.network.pinned_hosts import (
        add_known_pinned_host,
        load_known_pinned_hosts,
    )

    host_lc = host.strip().lower()
    if not host_lc:
        console.print("[red]Error:[/red] host must be non-empty")
        sys.exit(1)
    if add_known_pinned_host(host_lc):
        console.print(
            f"[green]Added[/green] {host_lc} to "
            f"~/.screencap/known_pinned_hosts.json. Next --network "
            f"recording will tunnel it directly."
        )
    else:
        existing = load_known_pinned_hosts()
        if host_lc in existing:
            console.print(f"[dim]{host_lc} already in known-pinned-hosts list.[/dim]")
        else:
            console.print(f"[red]Failed to persist[/red] {host_lc}")
            sys.exit(1)


@network_group.command("remove-kek")
@click.option(
    "--force",
    is_flag=True,
    help="Proceed even if encrypted recordings exist on disk.",
)
def network_remove_kek_cmd(force: bool) -> None:
    """Delete the network-body KEK from your Keychain.

    The KEK is the long-lived key that wraps every recording's per-recording
    DEK. Without it, the V1.5+ network bodies in past recordings cannot be
    decrypted — they become permanently inaccessible. This is the right
    command to run when:

    \b
    - Rotating the KEK (you are about to start fresh; existing encrypted
      recordings will become undecryptable).
    - Selling/disposing the device (combined with `screencap network uninstall`
      and shredding the recordings directory).

    Safety check (unless --force):
        Scans the configured recordings directory for any recording with
        at least one ``network_event`` row carrying non-NULL
        ``body_ciphertext`` and refuses to proceed if any are found,
        listing them. V1.5 metadata-only recordings (where the user
        only browsed non-allowlisted hosts) have no ciphertext rows
        and do NOT block removal — those have a wrapped DEK on disk
        but nothing to decrypt with it.

    Limitation: recordings written under `--output <custom-path>` are NOT
    discovered by this scan because we do not track custom output paths
    after the recording ends. If you have ever used `--output`, you must
    audit those locations yourself before passing --force.
    """
    from screencap.catalog import list_recordings
    from screencap.config import get_recordings_dir
    from screencap.engine.db import get_session_for_path
    from screencap.engine.db.models import NetworkEvent, Recording
    from screencap.network import crypto

    # Safety scan policy: KEK deletion is irreversible for every
    # encrypted body on disk. If we can't determine whether a recording
    # has ciphertext (DB unreadable, query fails for any reason), the
    # safe default is fail-CLOSED — treat the recording as if it has
    # encrypted bodies and require the user to either fix the recording
    # or pass --force. Failing open here would silently delete the KEK
    # while a recording the user couldn't even open might still need
    # decryption.
    encrypted_recordings: list[str] = []
    unreadable_recordings: list[str] = []
    recordings_dir = get_recordings_dir()
    for rec in list_recordings():
        db_path = recordings_dir / rec.name / "recording.db"
        if not db_path.exists():
            # No DB at all → nothing to lose by deleting KEK for this
            # recording; skip safely.
            continue
        try:
            session = get_session_for_path(str(db_path))
        except Exception as exc:
            unreadable_recordings.append(f"{rec.name} ({type(exc).__name__})")
            continue
        try:
            recording_row = session.query(Recording).first()
            if recording_row is None:
                # Unusual: DB opens but has no Recording row. Treat as
                # unreadable rather than silently safe.
                unreadable_recordings.append(f"{rec.name} (no Recording row)")
                has_ciphertext = False
            else:
                has_ciphertext = (
                    session.query(NetworkEvent.id)
                    .filter(NetworkEvent.recording_id == recording_row.id)
                    .filter(NetworkEvent.body_ciphertext.isnot(None))
                    .first()
                    is not None
                )
        except Exception as exc:
            # Query itself failed (corrupted DB, schema mismatch, etc).
            # Add to unreadable bucket so the user has to acknowledge
            # rather than silently treating as safe.
            unreadable_recordings.append(f"{rec.name} ({type(exc).__name__})")
            has_ciphertext = False
        finally:
            session.close()
        if has_ciphertext:
            encrypted_recordings.append(rec.name)

    blocking_recordings = encrypted_recordings + [
        f"[unreadable] {entry}" for entry in unreadable_recordings
    ]
    if blocking_recordings and not force:
        if encrypted_recordings:
            console.print(
                f"[red]Refusing to delete KEK:[/red] "
                f"{len(encrypted_recordings)} recording(s) on disk have "
                f"encrypted network bodies that depend on this KEK:"
            )
            for name in encrypted_recordings:
                console.print(f"  • {name}")
        if unreadable_recordings:
            console.print(
                f"[red]Refusing to delete KEK:[/red] "
                f"{len(unreadable_recordings)} recording(s) could not be "
                f"scanned for encrypted bodies and may still need this KEK:"
            )
            for entry in unreadable_recordings:
                console.print(f"  • {entry}")
            console.print(
                "[dim]Unreadable recordings fail closed — re-run after "
                "removing the bad recordings, or pass [bold]--force[/bold] "
                "to delete the KEK anyway.[/dim]"
            )
        console.print(
            "\n[yellow]Deleting the KEK will make these recordings' "
            "network bodies permanently undecryptable.[/yellow] Either "
            "export them first (`screencap export <name>`) or pass "
            "[bold]--force[/bold] to proceed anyway."
        )
        console.print(
            "\n[dim]Note:[/dim] recordings written with --output <custom-path> "
            "are NOT included in this scan."
        )
        sys.exit(1)

    if blocking_recordings and force:
        if encrypted_recordings:
            console.print(
                f"[yellow]--force given;[/yellow] {len(encrypted_recordings)} "
                f"encrypted recording(s) will become undecryptable."
            )
        if unreadable_recordings:
            console.print(
                f"[yellow]--force given;[/yellow] "
                f"{len(unreadable_recordings)} recording(s) could not be "
                f"scanned and will lose access to this KEK:"
            )
            for entry in unreadable_recordings:
                console.print(f"  • {entry}")

    try:
        import keyring  # noqa: PLC0415
        keyring.delete_password(crypto.SERVICE, crypto.KEK_ACCOUNT)
        console.print("[green]KEK removed from Keychain.[/green]")
    except Exception as exc:
        # PasswordDeleteError is the typical "no such password" — treat
        # as a no-op success so the command is idempotent.
        msg = str(exc).lower()
        if "no such password" in msg or "not found" in msg or "passworddeleteerror" in type(exc).__name__.lower():
            console.print("[dim]No KEK present in Keychain (already removed).[/dim]")
        else:
            console.print(f"[red]Failed to delete KEK:[/red] {exc}")
            sys.exit(1)


@network_group.command("restore")
def network_restore_cmd() -> None:
    """Restore system proxy state after a recording crash.

    Idempotent: safe to run anytime. Scans for orphaned snapshots
    (global sentinel + durable copies + per-recording-dir scan) and
    restores via osascript admin. No-op if no orphans are found.
    """
    from screencap.network.lifecycle import restore_orphaned_proxy_state

    console.print("[bold]Scanning for orphaned proxy state...[/bold]")
    restored = restore_orphaned_proxy_state()
    if restored:
        console.print(
            f"[green]Restored proxy state for {len(restored)} orphaned "
            f"recording(s):[/green]"
        )
        for path in restored:
            console.print(f"  • {path}")
    else:
        console.print("No orphaned proxy state found.")


_SMOKE_CHECKS = [
    _check_presidio_analyzer,
    _check_fast_gliner,
    _check_detect_secrets_plugins,
    _check_spacy_model,
    _check_av_codecs,
    _check_pynput,
    _check_sounddevice,
    _check_domain_index,
    _check_onnxruntime_excluded,
    _check_keyring_macos_backend,
]


@cli.command("_smoke-test", hidden=True)
@click.option("--verbose", "-v", is_flag=True, help="Show full tracebacks.")
def smoke_test(verbose):
    """Validate critical subsystems load correctly (internal)."""
    import traceback

    frozen = getattr(sys, "frozen", False)
    mode = "frozen binary" if frozen else "dev install"
    console.print(f"\nscreencap smoke test ({mode})\n")

    results: list[tuple[str, bool, str]] = []
    for check_fn in _SMOKE_CHECKS:
        try:
            result = check_fn()
        except BaseException:
            result = (check_fn.__name__.replace("_check_", ""), False, traceback.format_exc())
        results.append(result)

        name, passed, err = result
        if passed:
            console.print(f"  [green]\\[PASS][/green] {name}")
        else:
            # err contains the full traceback; show last line for summary,
            # full traceback when --verbose
            err_summary = err.strip().rsplit("\n", 1)[-1]
            console.print(f"  [red]\\[FAIL][/red] {name} — {err_summary}")
            if verbose:
                console.print(err)

    passed_count = sum(1 for _, p, _ in results if p)
    total = len(results)
    failed_count = total - passed_count

    console.print()
    if failed_count:
        console.print(f"{passed_count}/{total} checks passed, {failed_count} failed")
        sys.exit(1)
    else:
        console.print(f"All {total} checks passed")


# ---------------------------------------------------------------------------
# _network-dump (hidden) - inspect captured network_event rows
# ---------------------------------------------------------------------------


@cli.command("_network-dump", hidden=True)
@click.argument("name")
@click.option("--limit", "limit", type=int, default=50, show_default=True,
              help="Maximum number of rows to print.")
@click.option("--kind", "kind", type=click.Choice([
                  "request", "response", "ws_upgrade", "ws_frame", "drop_burst",
              ]), default=None,
              help="Filter rows by network event kind.")
@click.option("--host", "host_substr", default=None,
              help="Case-insensitive host substring filter.")
@click.option("--verbose", "-v", is_flag=True, default=False,
              help="Also print headers and details_json payloads.")
def network_dump(name, limit, kind, host_substr, verbose):
    """Inspect captured network_event rows for a recording (V1 debug helper).

    V1 ships network capture as DB-only - events.jsonl contains zero
    ``network.*`` lines. This subcommand reads the recording's
    ``recording.db`` directly and prints the seeded rows for premise
    validation. JSONL emission lands in V1.75 alongside the cloud-bound
    network filter.
    """
    import sqlite3
    from datetime import datetime

    from screencap.config import get_recordings_dir

    recording_dir = get_recordings_dir() / name
    if not recording_dir.exists():
        console.print(f"[red]Error:[/red] Recording not found: {name}")
        sys.exit(1)
    db_path = recording_dir / "recording.db"
    if not db_path.is_file():
        console.print(
            f"[red]Error:[/red] recording.db not found in {recording_dir}",
        )
        sys.exit(1)

    # Read-only URI mode keeps the helper safe alongside any concurrent
    # reader (V1 has no live writer at inspection time, but URI ro is
    # explicit about intent).
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError as exc:
        console.print(f"[red]Error:[/red] failed to open {db_path}: {exc}")
        sys.exit(1)
    try:
        conn.row_factory = sqlite3.Row
        # Friendly message for pre-feature DBs / recordings made without --network.
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='network_event'",
        )
        if cur.fetchone() is None:
            console.print(
                "No network events captured in this recording. "
                "Did you start with --network?",
            )
            return

        sql = "SELECT * FROM network_event WHERE 1 = 1"
        params: list[object] = []
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        if host_substr:
            sql += " AND lower(host) LIKE ?"
            params.append(f"%{host_substr.lower()}%")
        # ``ORDER BY timestamp_ns`` is load-bearing - without it the rows
        # are not guaranteed to be time-ordered (the table has an index
        # on timestamp_ns but no implicit ordering on a SELECT *).
        sql += " ORDER BY timestamp_ns LIMIT ?"
        params.append(int(limit))
        rows = conn.execute(sql, params).fetchall()

        if not rows:
            console.print(
                "No network events captured in this recording. "
                "Did you start with --network?",
            )
            return

        for row in rows:
            ts = row["timestamp"]
            try:
                ts_iso = datetime.fromtimestamp(float(ts)).isoformat(
                    timespec="milliseconds",
                )
            except (TypeError, ValueError, OSError):
                ts_iso = str(ts)

            row_kind = row["kind"] or ""
            method = row["method"] or "-"
            host = row["host"] or "-"
            status = row["status"] if row["status"] is not None else "-"
            body_size = row["body_size"] if row["body_size"] is not None else 0
            sha = row["body_sha256"]
            if isinstance(sha, (bytes, bytearray, memoryview)):
                sha_hex = bytes(sha).hex()
                short_sha = sha_hex[:12]
            else:
                short_sha = "-"

            url = row["url"] or "-"
            if len(url) > 120:
                url = url[:117] + "..."

            console.print(
                f"{ts_iso} {row_kind} {method} {host} {status} "
                f"{body_size}B sha={short_sha}... {url}"
            )

            if verbose:
                headers_json = row["headers_json"]
                if headers_json:
                    try:
                        headers = json.loads(headers_json)
                    except (TypeError, ValueError):
                        headers = None
                    if headers:
                        console.print("    [dim]headers:[/dim]")
                        for entry in headers:
                            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                                console.print(f"      {entry[0]}: {entry[1]}")
                            else:
                                console.print(f"      {entry!r}")
                details_json = row["details_json"]
                if details_json:
                    try:
                        details = json.loads(details_json)
                    except (TypeError, ValueError):
                        details = details_json
                    console.print(f"    [dim]details:[/dim] {details!r}")
    finally:
        conn.close()


if __name__ == "__main__":
    cli()
