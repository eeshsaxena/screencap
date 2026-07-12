"""ScreenCap CLI — Click-based entry point."""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import TYPE_CHECKING, Any

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
from rich.markup import escape
from rich.table import Table

from screencap import __version__

if TYPE_CHECKING:
    from screencap.cli._daemon_client import DaemonHTTPClient

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
_APPS_SCHEMA_VERSION = 3
_SETTINGS_PRIVACY_SCHEMA_VERSION = 2
# v2 (todo 012 follow-up, SCR-17): adds the `privacy` block to the payload so
# the SwiftUI first-run banner can read `mode`, `setup_skipped`, and
# `has_privacy_section` in a single round-trip without touching config.toml.
# Additive: every v1 field is unchanged.
# v3: drops the `auto_name` field (legacy LLM auto-naming removed; the
# feature was dead — its flags never reached the daemon).
_SETTINGS_SCHEMA_VERSION = 3
# `settings intelligence --json` envelope (ok + schema_version + the current
# provider / cloud provider / per-task consent rows), read by the Swift
# Intelligence pane (U9) to reconcile its optimistic toggles. SCR local-first
# intelligence, U8.
# v2 (BYO cloud, U2): adds per-vendor ``<vendor>_key_present`` booleans so the
# Swift pane can render "connected" without ever seeing a key. Additive: every
# v1 field is unchanged.
# v3 (BYO cloud, U4): adds per-vendor ``<vendor>_cli_available`` booleans (the
# ``*-cli`` delegation availability, existence/stat only) so the pane can render
# each CLI option available / needs-attention (R5). Additive over v2.
_SETTINGS_INTELLIGENCE_SCHEMA_VERSION = 3
_STOP_SCHEMA_VERSION = 1
# `whoami --json` envelope (ok + schema_version + signed_in/uid/email/subscribed/
# tier/trial_end), read by the SwiftUI shell to gate the Upload affordance on
# auth + entitlement and drive the two-tier picker + trial UI (U11).
# Bumped 1 -> 2 for U6: the two-tier `tier` + `trial_end` claims joined the
# envelope shape (and the daemon `auth.whoami` handler, previously dropping
# `subscribed`, now forwards all three). The Swift drift check is warn-only, so
# the bump is a low-cost signal that the shape grew — a conscious call for a
# real shape change (contrast `subscribed`, which was left unbumped as it never
# altered the handler-forwarded set). Additive + defaulted, so older app builds
# stay compatible.
_AUTH_SCHEMA_VERSION = 2
# `backfill status --json` envelope (ok + schema_version + the privacy-safe
# status snapshot the daemon publishes). SCR-178 U6.
_BACKFILL_SCHEMA_VERSION = 1


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
    from screencap.privacy.classify import BUNDLE_ID_MAP

    seen_bids = get_seen_bundle_ids([capture_dir])
    if not seen_bids:
        return

    try:
        privacy_config = get_privacy_config()
    except Exception:
        return

    # Case-insensitive comparison (SCR-235): the config sets are lowered at
    # parse while seen_bids carry OS casing — an exact subtraction falsely
    # reports classified mixed-case apps as unclassified.
    known_bids_lower = (
        {b.lower() for b in BUNDLE_ID_MAP}
        | set(privacy_config.exclude_apps)
        | set(privacy_config.allow_apps)
        | set(privacy_config.app_classes.keys())
    )

    unclassified = sorted(b for b in seen_bids if b.lower() not in known_bids_lower)
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


@cli.command("serve")
@click.option("--self-test", is_flag=True, hidden=True)
@click.option(
    "--socket",
    "socket_path",
    default=None,
    hidden=True,
    help="Override socket path (test-only).",
)
@click.option("--install", is_flag=True, help="Install LaunchAgent and start daemon.")
@click.option("--uninstall", is_flag=True, help="Stop daemon and remove LaunchAgent.")
@click.option("--status", "show_status", is_flag=True, help="Print daemon LaunchAgent status.")
@click.option(
    "--idle-shutdown",
    "idle_shutdown",
    type=int,
    default=None,
    hidden=True,
    help=(
        "Exit after N idle seconds (no requests / subscribers / active "
        "recording). CLI auto-spawn uses this; LaunchAgent-managed "
        "daemons omit it and run all day."
    ),
)
def serve(
    socket_path: str | None,
    self_test: bool,
    install: bool,
    uninstall: bool,
    show_status: bool,
    idle_shutdown: int | None,
) -> None:
    """Run the ScreenCap daemon, or manage its LaunchAgent."""
    if sum(bool(flag) for flag in (install, uninstall, show_status)) > 1:
        raise click.UsageError("--install, --uninstall, and --status are mutually exclusive.")

    if install or uninstall or show_status:
        from screencap.daemon import launchagent

        if install:
            result = launchagent.install()
            if result.state == launchagent.STATE_INSTALLED_AND_RUNNING:
                # Proactively register the daemon's Screen Recording +
                # Accessibility rows and clear decoy/orphan rows now that the
                # daemon is up (U3/U4). Best-effort: never let this fail the
                # otherwise-successful install.
                launchagent.run_proactive_setup()
                console.print(f"[green]{result.state}[/green]: {result.plist_path}")
                return
            if result.state == launchagent.STATE_INSTALL_FAILED_ALREADY_RUNNING:
                console.print(f"[red]{result.state}[/red]: {escape(str(result.detail))}")
                # Surface the actionable remediation: if `result.detail`
                # carried a `pid={N}` clause from launchagent.install(),
                # the operator can `kill {pid}` directly. Otherwise
                # `screencap serve --uninstall` is the safe fallback.
                import re

                match = re.search(r"\bpid=(\d+)\b", result.detail)
                if match is not None:
                    console.print(
                        f"Try [bold]kill {match.group(1)}[/bold] to clear the rogue process, "
                        "or [bold]screencap serve --uninstall[/bold] to remove the installed agent."
                    )
                else:
                    console.print(
                        "Try [bold]screencap serve --uninstall[/bold] to remove the "
                        "installed agent, then re-run [bold]screencap serve --install[/bold]."
                    )
                raise SystemExit(1)
            console.print(f"[red]{result.state}[/red]: {escape(str(result.detail))}")
            raise SystemExit(1)

        if uninstall:
            result = launchagent.uninstall()
            if result.state == launchagent.STATE_UNINSTALLED:
                console.print(f"[green]{result.state}[/green]: {result.plist_path}")
                return
            console.print(f"[red]{result.state}[/red]: {escape(str(result.detail))}")
            raise SystemExit(1)

        result = launchagent.status()
        if result.state == launchagent.STATE_LOADED:
            state_detail = result.launchd_state or "unknown"
            console.print(f"[green]{launchagent.STATE_LOADED}[/green]: {escape(str(state_detail))}")
            return
        if result.state == launchagent.STATE_NOT_LOADED:
            console.print(f"[yellow]{launchagent.STATE_NOT_LOADED}[/yellow]")
            return
        console.print(f"[red]{result.state}[/red]: {escape(str(result.detail))}")
        raise SystemExit(1)

    from screencap.daemon.server import serve as _serve

    raise SystemExit(
        _serve(
            socket_path=socket_path,
            self_test=self_test,
            idle_shutdown_seconds=idle_shutdown,
        )
    )


@cli.command("mcp")
def mcp() -> None:
    """Run the MCP stdio server (for Claude Desktop / Codex / other agents).

    Exposes ScreenCap's retrieval surface as MCP tools that forward to the
    daemon over its UNIX socket. Communicates over stdin/stdout via JSON-RPC,
    so it is launched as a subprocess by the MCP client, not run interactively.
    See ``docs/mcp-client-setup.md``.
    """
    # Defer the mcp/httpx imports so ``screencap --help`` stays fast and only
    # the ``mcp`` invocation pays for the SDK. stdout belongs to the JSON-RPC
    # stream — the server logs to stderr only.
    from screencap.mcp.server import run_stdio

    run_stdio()


@cli.command("_engine-worker", hidden=True)
@click.argument("encoded_args")
def _engine_worker_cmd(encoded_args: str) -> None:
    """Hidden subprocess entry point spawned by the daemon supervisor."""
    import base64
    import multiprocessing
    import threading
    from pathlib import Path

    args = json.loads(base64.b64decode(encoded_args).decode("utf-8"))
    from screencap._stderr_events import (
        EVENT_RECORDING_FINALIZED,
        EVENT_STARTED,
        emit_event,
    )
    from screencap.pidfile import CLAIMANT_DAEMON
    from screencap.session import run_recording_worker

    queues = [
        multiprocessing.Queue(),
        multiprocessing.Queue(),
        multiprocessing.Queue(),
    ]
    args.setdefault("_window_feed_q", queues[0])
    args.setdefault("_override_q", queues[1])
    args.setdefault("_disable_q", queues[2])
    args.setdefault("_network_handoff_ready", None)

    def drain_queue(q: multiprocessing.Queue) -> None:
        while True:
            try:
                q.get()
            # `EOFError` / `OSError` close the underlying pipe; `ValueError`
            # is raised on get() against a closed queue. Anything else is a
            # real bug — let it propagate.
            except (EOFError, OSError, ValueError):
                return

    for q in queues:
        threading.Thread(target=drain_queue, args=(q,), daemon=True).start()

    emit_event(EVENT_STARTED, claimant=CLAIMANT_DAEMON)
    run_recording_worker(args)

    ready_meta: dict[str, Any] = {}
    capture_dir = args.get("capture_dir_hint") or args.get("output_dir")
    if capture_dir:
        try:
            ready_path = Path(str(capture_dir)) / ".recording_ready"
            if ready_path.exists():
                ready_meta = json.loads(ready_path.read_text() or "{}")
        except Exception:
            ready_meta = {}
    emit_event(
        EVENT_RECORDING_FINALIZED,
        name=args.get("name"),
        duration_seconds=float(ready_meta.get("elapsed", 0.0)),
        force_stopped=bool(ready_meta.get("force_stopped", False)),
        disk_full=bool(ready_meta.get("disk_full", False)),
    )


@cli.command("_permission-probe", hidden=True)
def _permission_probe_cmd() -> None:
    """Hidden entry point: print live TCC grant state as one JSON line.

    Spawned by ``screencap.daemon.permission_probe`` so the long-lived daemon
    can read *live* TCC state (R9). Because this runs in a freshly spawned
    process that has made no prior TCC call, each in-process ``CGPreflight*``
    reads live state — the same property ``session.run_recording_worker``
    relies on. The probe invoker passes ``--no-update-check`` so the only line
    on stdout is the result JSON: ``{permission: granted|denied|indeterminate}``.
    """
    from screencap.daemon.permission_probe import run_probe_checks

    click.echo(json.dumps(run_probe_checks()))


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
#         permission_lost, permission_required, stopped
# Exit codes: 0=clean, 2=lock-held (Unit 3), 3=permission denied/lost
#             (permission_required start-time block OR permission_lost
#             mid-recording), 4=disk_full
#
# ``permission_lost`` is emitted from src/screencap/recorder.py (Unit 8).
# ``chunk_finalized`` is emitted from src/screencap/session.py.
# ``disk_full`` is emitted from src/screencap/session.py (DiskFullError catch).
# All other events are emitted from the screencap start command flow below.

# `_emit_event` lives in the stdlib-only `screencap._stderr_events` module so
# spawn workers and the recording hot loop can import it without dragging
# Click + rich Console into the child process.
from screencap._stderr_events import (  # noqa: E402
    EVENT_PERMISSION_REQUIRED,
    EVENT_STOPPED,
    PERMISSION_DISPLAY,
)
from screencap._stderr_events import (
    emit_event as _emit_event,
)


@cli.command()
@click.option("--name", "-n", default=None, help="Recording name.")
@click.option("--description", "-d", default=None, help="Task description.")
@click.option("--no-audio", is_flag=True, default=False, help="Disable audio capture.")
@click.option("--no-video", is_flag=True, default=False, help="Disable video capture.")
@click.option("--no-images", is_flag=True, default=False, help="Disable screenshot capture.")
@click.option("--no-window-data", is_flag=True, default=False, help="Disable window/accessibility data.")
@click.option("--output", "-o", type=click.Path(), default=None, help="Custom output directory.")
@click.option("--no-wifi-metrics", is_flag=True, default=False, help="Disable WiFi metrics collection.")
@click.option("--no-app-versions", is_flag=True, default=False, help="Disable running app version capture.")
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
    verbose, chunk_duration, no_live_upload,
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
        get_segmentation_mode,
    )

    # Resolve segmentation mode: CLI flag > config.toml > default
    seg_mode = segmentation_mode or get_segmentation_mode()

    if not name:
        # Generate timestamp-based temp name
        name = f"rec-{datetime.now().strftime('%Y%m%dT%H%M%S')}"

    # --no-audio wins; otherwise fall back to the persisted default so the
    # menu-bar "Audio (next recording)" toggle actually reaches new sessions.
    audio = False if no_audio else get_audio_default()
    wifi_metrics = not no_wifi_metrics
    app_versions = not no_app_versions

    # Capture flags: default to True for images and window data (opt-out)
    capture_video = False if no_video else None  # None = use upstream default (True)
    # Search U8: route the CLI's stills default through the daemon's readiness gate
    # instead of forcing True — ``None`` (unset) lets ``build_engine_worker_args``
    # resolve default-on iff the guardrails are ready (encrypted + disclosure +
    # retention), so a headless CLI no longer accumulates plaintext, never-evicted
    # stills. ``--no-images`` stays an explicit opt-out.
    capture_images = False if no_images else None
    capture_window_data = False if no_window_data else None  # None = upstream default (True)
    # First-run privacy setup detection
    from screencap.privacy_settings import (
        _maybe_prompt_matrix_acknowledgement,
        _maybe_prompt_privacy_setup,
    )
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
        from screencap.redaction import are_nlp_models_cached
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
                    console.print(f"[yellow]Warning:[/] Download failed: {escape(str(exc))}")
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

    # --- Hand off to the daemon ---
    # Phase 2 U1 makes the daemon the sole engine spawner. ``screencap
    # start`` is now a thin HTTP client: ensure the daemon is up
    # (auto-spawn for F3 if no LaunchAgent), POST recording.start,
    # stream events back to stderr (preserving the cross-language
    # event contract SwiftUI / agents pattern-match against), and map
    # the terminal event to a process exit code per existing taxonomy.
    _exit_code = _run_start_via_daemon(
        name=name,
        description=description,
        audio=audio,
        output=output,
        wifi_metrics=wifi_metrics,
        app_versions=app_versions,
        capture_video=capture_video,
        capture_images=capture_images,
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
        network=network,
    )
    _emit_event(EVENT_STOPPED, exit_code=_exit_code)
    if _exit_code:
        raise SystemExit(_exit_code)


def _run_start_via_daemon(
    *,
    name: str | None,
    description: str | None,
    audio: bool | None,
    output: str | None,
    wifi_metrics: bool | None,
    app_versions: bool | None,
    capture_video: bool | None,
    capture_images: bool | None,
    capture_window_data: bool | None,
    verbose: bool,
    chunk_duration: float | None,
    live_upload: bool,
    force_mode: str | None,
    cloud_intent: bool,
    keep_local: bool,
    intent_source: str,
    segmentation_mode: str | None,
    scrub_enabled: bool,
    show_on_website: bool,
    network: bool,
) -> int:
    """POST recording.start, stream events, map terminal event to exit code.

    Returns the exit code per Phase 1's taxonomy:
      0=clean, 1=generic failure, 2=lock-held, 3=permission_lost,
      4=disk_full, 5=user-initiated force-quit (2-tap Ctrl+C).
    """
    import json as _json
    import signal as _signal
    import sys as _sys

    from screencap.cli._autospawn import (
        DaemonAutoSpawnError,
        LaunchAgentNotRunningError,
        ensure_daemon_or_spawn,
    )
    from screencap.cli._daemon_client import (
        DaemonAPIError,
        DaemonHTTPClient,
        DaemonUnreachableError,
        SchemaMismatchError,
    )

    # ``screencap start`` is the primary auto-spawn trigger (origin F3).
    try:
        ensure_daemon_or_spawn(
            auto_spawn=True,
            stderr_emitter=lambda line: click.echo(line, err=True),
        )
    except LaunchAgentNotRunningError as exc:
        click.echo(str(exc), err=True)
        return 1
    except DaemonAutoSpawnError as exc:
        click.echo(f"Error: {exc}", err=True)
        if exc.log_tail:
            click.echo(exc.log_tail, err=True)
        return 1

    # ``force_mode`` may be a PrivacyMode enum from Phase 1; daemon
    # consumes a string. Coerce safely.
    force_mode_str = None
    if force_mode is not None:
        force_mode_str = getattr(force_mode, "value", str(force_mode))

    payload = {
        "name": name,
        "description": description or None,
        "audio": audio,
        "output_dir": output,
        "wifi_metrics": wifi_metrics,
        "app_versions": app_versions,
        "capture_video": capture_video,
        "capture_images": capture_images,
        "capture_window_data": capture_window_data,
        "verbose": verbose,
        "chunk_duration": chunk_duration,
        "live_upload": live_upload,
        "force_mode": force_mode_str,
        "cloud_intent": cloud_intent,
        "keep_local": keep_local,
        "intent_source": intent_source,
        "segmentation_mode": segmentation_mode,
        "scrub_enabled": scrub_enabled,
        "show_on_website": show_on_website,
        "network": network,
    }

    interrupt_state = {"count": 0, "stop_sent": False, "client": None}

    def _sigint_handler(_signum, _frame):
        interrupt_state["count"] += 1
        client = interrupt_state["client"]
        if interrupt_state["count"] >= 2:
            # Second Ctrl+C always escalates to force-quit regardless of
            # whether a graceful stop was already sent — the graceful stop
            # may be in-flight and the user wants an immediate exit.
            click.echo("\nSecond Ctrl+C — force-stopping.", err=True)
            if client is not None:
                try:
                    # Short 3s timeout: the user wants an immediate exit;
                    # don't block the signal handler for the full 30s default.
                    client.stop(force=True, timeout=3.0)
                except Exception:
                    pass
            return
        if client is not None and not interrupt_state["stop_sent"]:
            try:
                # Short 3s timeout: signal handlers should not block
                # indefinitely.
                client.stop(force=False, timeout=3.0)
                interrupt_state["stop_sent"] = True
                click.echo(
                    "Stopping (Ctrl+C again to force-quit)...", err=True
                )
            except DaemonAPIError as e:
                logger.debug("stop(force=False) returned daemon error: %s", e)
            except Exception as e:
                logger.debug("stop(force=False) failed: %s", e)

    try:
        with DaemonHTTPClient() as client:
            interrupt_state["client"] = client
            try:
                start_result = client.start(**{k: v for k, v in payload.items() if v is not None})
            except DaemonAPIError as exc:
                code = exc.envelope.get("error", "unknown")
                if code == "lock_contended":
                    owner = exc.envelope.get("owner", {})
                    click.echo(
                        f"[red]Another recording is already active "
                        f"(owner: {owner.get('claimant', 'unknown')}).[/red]",
                        err=True,
                    )
                    return 2
                if code == "subscription_required":
                    # SCR (paid-only launch, U10): the daemon's local paywall
                    # (``SCREENCAP_LOCAL_PAYWALL_ENFORCE``) refused the start
                    # with a 402 ``subscription_required`` envelope. Render a
                    # concise upgrade message via ``rich.console.Console``
                    # (mirroring the upload-side ``SubscriptionRequired`` tone)
                    # and exit non-zero with no traceback — never the generic
                    # "Daemon rejected start" line below. With the flag off the
                    # daemon never emits this code, so the start path is
                    # byte-identical to today.
                    console.print(
                        "[red]Recording requires an active subscription — "
                        "upgrade in the app to continue.[/red]"
                    )
                    return 1
                if code == "permission_required":
                    # SCR-142: the daemon's pre-spawn permission gate rejected
                    # the start and named every denied permission in ``missing``.
                    # Re-emit that list as a structured ``permission_required``
                    # stderr event (mirroring the daemon envelope) and exit 3 —
                    # NOT the generic exit 1 below, which would discard the
                    # permission identity and force the CLI-fallback SwiftUI
                    # shell back to its hedged "Screen Recording, Accessibility,
                    # or Input Monitoring" message. With the event + exit 3 the
                    # shell routes into the same precise grant flow the daemon
                    # transport already uses for this envelope.
                    # Defensive: the daemon's permission_required_envelope
                    # always sends a list, but guard against a non-list (a buggy
                    # or drifted daemon sending a bare string) so we don't
                    # iterate characters into garbage single-char "permissions".
                    raw_missing = exc.envelope.get("missing")
                    missing = (
                        [str(p) for p in raw_missing]
                        if isinstance(raw_missing, list)
                        else []
                    )
                    _emit_event(EVENT_PERMISSION_REQUIRED, missing=missing)
                    names = (
                        ", ".join(
                            PERMISSION_DISPLAY.get(p, p.replace("_", " ").title())
                            for p in missing
                        )
                        if missing
                        else "a required permission"
                    )
                    click.echo(
                        f"Permission required before recording: {names}. "
                        "Grant in System Settings → Privacy & Security.",
                        err=True,
                    )
                    return 3
                click.echo(f"Daemon rejected start: {code}", err=True)
                return 1
            except SchemaMismatchError as exc:
                click.echo(f"Error: {exc}", err=True)
                return 1
            except DaemonUnreachableError as exc:
                click.echo(f"Error: daemon unreachable: {exc}", err=True)
                return 1

            cursor = start_result.get("cursor", 0)
            console.print(
                f"[#22d3ee]Recording[/#22d3ee] "
                f"[dim](session_id={start_result.get('session_id')!r})[/dim]"
            )

            previous_sigint = _signal.signal(_signal.SIGINT, _sigint_handler)
            terminal_payload: dict | None = None
            permission_lost = False
            try:
                while terminal_payload is None:
                    try:
                        with client.events(since=cursor) as stream:
                            for event in stream:
                                # Mirror the engine's stderr line shape so
                                # SwiftUI / agent consumers parse the same
                                # JSON whether the producer is the daemon
                                # or the engine stderr.
                                _sys.stderr.write(_json.dumps(event) + "\n")
                                _sys.stderr.flush()

                                event_type = event.get("type")
                                if isinstance(event.get("cursor"), int):
                                    cursor = event["cursor"]
                                if event_type == "permission_lost":
                                    permission_lost = True
                                if event_type == "recording_finalized":
                                    terminal_payload = event
                                    break
                                if event_type == "_close":
                                    # Daemon shut the stream; reopen from
                                    # last seen cursor.
                                    break
                    except DaemonAPIError as exc:
                        if exc.envelope.get("error") == "cursor_unknown":
                            # Daemon recycled mid-recording (idle-shutdown
                            # window or restart). Re-fetch the snapshot for
                            # a fresh cursor and resume.
                            try:
                                snap = client.snapshot()
                            except Exception:
                                return 1
                            # If the recording is already gone (daemon
                            # force-terminated on restart), exit cleanly
                            # rather than blocking forever.
                            if snap.get("is_recording") is False:
                                click.echo(
                                    "Recording ended (daemon restarted mid-session).",
                                    err=True,
                                )
                                return 1
                            cursor = snap.get("cursor", 0)
                            continue
                        click.echo(f"Daemon error: {exc}", err=True)
                        return 1
                    except DaemonUnreachableError:
                        click.echo(
                            "Daemon disconnected mid-recording; "
                            "see ~/.screencap/run/auto-serve.log",
                            err=True,
                        )
                        return 1
            finally:
                _signal.signal(_signal.SIGINT, previous_sigint)

            if permission_lost:
                return 3
            if terminal_payload and terminal_payload.get("disk_full"):
                return 4
            if interrupt_state["count"] >= 2:
                return 5
            # Engine crash / OOM / SIGKILL: daemon synthesizes
            # recording_finalized with force_stopped=True but without
            # disk_full or permission_lost.
            if (
                terminal_payload
                and terminal_payload.get("force_stopped")
                and not terminal_payload.get("disk_full")
                and not permission_lost
            ):
                return 1
            return 0
    finally:
        interrupt_state["client"] = None


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
            f"[yellow]Warning:[/yellow] Could not auto-export events.jsonl ({escape(str(e))}). "
            f"Run 'screencap export {escape(str(capture_dir.name))}' manually."
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
            from screencap.transcription import _transcribe_api_inline

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
def list_cmd(as_json, sort):
    """List all recordings."""
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
        console.print(f"[dim]Opening {escape(str(name))}/viewer.html ...[/dim]")
    except ImportError:
        console.print(_RECORD_EXTRAS_MSG)
        sys.exit(1)
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        console.print(f"[red]Error:[/red] {escape(str(e))}")
        sys.exit(1)


@cli.command()
@click.argument("name")
@click.option("--json", "as_json", is_flag=True,
              default=lambda: _should_default_to_json(),
              help="Output as JSON. Auto-detected when stdout is not a TTY (todo 030).")
def info(name, as_json):
    """Show details and system metrics for a recording."""
    from screencap.catalog import _read_recording_meta, find_db, read_drops
    from screencap.config import get_recordings_dir

    try:
        from screencap.metrics import METRICS_FILENAME
    except ImportError:
        console.print(_RECORD_EXTRAS_MSG)
        raise SystemExit(1)

    recording_dir = get_recordings_dir() / name
    if not recording_dir.exists():
        console.print(f"[red]Error:[/red] Recording not found: {escape(str(name))}")
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

        started, duration, _ = _read_recording_meta(db_path)
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

    console.print(Panel(f"[bold]{escape(str(name))}[/bold]", title="Recording"))

    if intent_data:
        console.print(f"  [#60a5fa]destination:[/#60a5fa] {escape(str(intent_data.get('destination', '?')))}")
        console.print(f"  [#60a5fa]privacy mode:[/#60a5fa] {escape(str(intent_data.get('privacy_mode', '?')))}")
        console.print(f"  [#60a5fa]intent source:[/#60a5fa] {escape(str(intent_data.get('source', '?')))}")

    if rec_meta:
        for key, val in rec_meta.items():
            console.print(f"  [#60a5fa]{key}:[/#60a5fa] {escape(str(val))}")
    else:
        console.print("  [dim]No recording metadata available.[/dim]")

    if isinstance(drops, dict) and any(v > 0 for v in drops.values()):
        console.print("\n  [yellow]Events dropped during recording:[/yellow]")
        for event_type, count in drops.items():
            if count > 0:
                console.print(f"    [yellow]{escape(str(event_type))}:[/yellow] {count}")

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
                console.print("  [#60a5fa]locale:[/#60a5fa]")
                for lk, lv in val.items():
                    if isinstance(lv, list):
                        console.print(f"    [#60a5fa]{lk}:[/#60a5fa] {escape(', '.join(str(x) for x in lv))}")
                    elif isinstance(lv, dict):
                        console.print(f"    [#60a5fa]{lk}:[/#60a5fa] {escape(str(lv))}")
                    else:
                        console.print(f"    [#60a5fa]{lk}:[/#60a5fa] {escape(str(lv))}")
            elif key == "running_applications" and isinstance(val, list):
                console.print("  [#60a5fa]running apps:[/#60a5fa]")
                for app in val:
                    v = f" v{escape(str(app['version']))}" if app.get("version") else ""
                    console.print(f"    {escape(str(app['name']))} ({escape(str(app['bundle_id']))}){v}")
            elif key == "wifi" and isinstance(val, dict):
                console.print("  [#60a5fa]wifi:[/#60a5fa]")
                for wk, wv in val.items():
                    console.print(f"    [#60a5fa]{wk}:[/#60a5fa] {escape(str(wv))}")
            else:
                console.print(f"  [#60a5fa]{key}:[/#60a5fa] {escape(str(val))}")

    for phase in ("start", "end"):
        snapshot = metrics.get(phase)
        if snapshot:
            console.print(Panel(f"[bold]{phase.title()} Snapshot[/bold]"))
            for key, val in snapshot.items():
                if key == "wifi" and isinstance(val, dict):
                    console.print("  [#60a5fa]wifi:[/#60a5fa]")
                    for wk, wv in val.items():
                        console.print(f"    [#60a5fa]{wk}:[/#60a5fa] {escape(str(wv))}")
                else:
                    console.print(f"  [#60a5fa]{key}:[/#60a5fa] {escape(str(val))}")
        elif phase == "end":
            console.print("\n  [dim]No end snapshot (recording may have been interrupted).[/dim]")


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
                        f"[red]Error:[/red] Cannot decrypt network bodies: {escape(str(e))}. "
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
        err_console.print(f"[red]Error:[/red] {escape(str(e))}")
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

    from screencap.enforcement.window_filter import build_local_window_filter

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
            f"{escape(str(rec_name))} — "
            f"applying current config mode={escape(repr(mode))}. Re-record under the desired "
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


@cli.command("review-data")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True,
              default=lambda: _should_default_to_json(),
              help="Output as JSON. Auto-detected when stdout is not a TTY.")
def review_data_cmd(name, as_json):
    """Prepare a recording for native review and emit a JSON envelope.

    Called by the SwiftUI shell's review window when the operator clicks
    Upload on a row. Concatenates chunked recordings and re-encodes the
    video for AVKit compatibility if needed — all in-process via PyAV, so
    it works with no ffmpeg/ffprobe on PATH — and ensures an events.jsonl
    exists. Returns paths the Swift side feeds into the AVKit player and
    the timeline pane.

    The envelope shape matches `screencap list --json` and
    `screencap info --json`: ok + schema_version + payload, or
    ok=false + error on failure. A genuine undecodable source yields a
    structural "can't process this video" error, never an "install
    ffmpeg" message.
    """
    from screencap.review import REVIEW_SCHEMA_VERSION, ReviewPrepareError, prepare_review_data

    try:
        envelope = prepare_review_data(name)
    except ReviewPrepareError as e:
        # A busy terminal_lock surfaces here as ReviewPrepareBusy (a subclass);
        # the review window (unlike inspect) has no auto-retry consumer, so it is
        # handled uniformly as a hard failure — no retryable flag is emitted
        # (it would be dead on the review surface). Wiring review-window retry is
        # a deliberate follow-up, not part of this inspect-focused fix.
        err_payload = {"ok": False, "schema_version": REVIEW_SCHEMA_VERSION, "error": str(e)}
        if as_json:
            click.echo(json.dumps(err_payload))
        else:
            console.print(f"[red]Error:[/red] {escape(str(e))}")
        sys.exit(1)

    if as_json:
        click.echo(json.dumps(envelope))
        return

    # Human-readable fallback for the rare CLI-direct user. The
    # SwiftUI shell always passes --json (auto-detected via non-TTY
    # stdout when spawned as a subprocess).
    console.print(f"[bold]{escape(str(name))}[/bold]")
    console.print(f"  video:  [dim]{escape(str(envelope['video_path']))}[/dim]")
    console.print(f"  events: [dim]{escape(str(envelope['events_path']))}[/dim]")
    if envelope.get("video_pixfmt_remediated"):
        console.print("  [dim](video remediated for AVKit compatibility)[/dim]")


@cli.command("inspect-data")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True,
              default=lambda: _should_default_to_json(),
              help="Output as JSON. Auto-detected when stdout is not a TTY.")
def inspect_data_cmd(name, as_json):
    """Prepare a recording for read-only native inspection and emit a JSON envelope.

    The no-scrub sibling of ``review-data``: called by the SwiftUI shell's
    inspect window (opened from a search result or a recordings-list click —
    "just looking", not uploading). Reads the LOCAL recording — video + events +
    timing — from the original dir without running the scrubber, so it returns
    in normal CLI latency rather than the minutes a scrub can take. Nothing here
    ever leaves the device; masking is an upload concept and is absent.

    The envelope shape matches ``review-data`` (the Swift decoder is shared), but
    ``redaction``/``coverage`` are null and ``screenshots`` is empty. On a hard
    failure: ok=false + error + non-zero exit, never a raw traceback. The ONE
    exception is the transient "still finalizing" case (``ReviewPrepareBusy``),
    which exits ZERO with ``retryable=true`` — see below.
    """
    from screencap.review import (
        REVIEW_SCHEMA_VERSION,
        ReviewPrepareBusy,
        ReviewPrepareError,
        prepare_inspect_data,
    )

    try:
        envelope = prepare_inspect_data(name)
    except ReviewPrepareBusy as e:
        # Recording still finalizing (the terminal_lock is held right after
        # stop): a TRANSIENT "not ready yet", not a command failure. Exit ZERO
        # with retryable=true so the SwiftUI shell actually RECEIVES the
        # envelope and auto-retries — ``CLIClient.runJSONRaw`` throws on a
        # non-zero exit and DISCARDS stdout, so a non-zero exit here would drop
        # the retryable envelope on the floor and surface the very "Could not
        # load this recording." failure this exists to prevent. Must precede the
        # generic ``ReviewPrepareError`` arm (it is a subclass).
        payload = {
            "ok": False, "schema_version": REVIEW_SCHEMA_VERSION,
            "error": str(e), "retryable": True,
        }
        if as_json:
            click.echo(json.dumps(payload))
        else:
            console.print(f"[yellow]{escape(str(e))}[/yellow]")
        return
    except ReviewPrepareError as e:
        err_payload = {"ok": False, "schema_version": REVIEW_SCHEMA_VERSION, "error": str(e)}
        if as_json:
            click.echo(json.dumps(err_payload))
        else:
            console.print(f"[red]Error:[/red] {escape(str(e))}")
        sys.exit(1)

    if as_json:
        click.echo(json.dumps(envelope))
        return

    # Human-readable fallback for the rare CLI-direct user. The SwiftUI shell
    # always passes --json (auto-detected via non-TTY stdout when spawned as a
    # subprocess). Dynamic segments are escaped (SCR-117/SCR-169): a recording
    # name carrying Rich-markup metacharacters must not crash or be stripped.
    console.print(f"[bold]{escape(str(name))}[/bold]")
    console.print(f"  video:  [dim]{escape(str(envelope['video_path']))}[/dim]")
    # events_path may be None for inspect (video-first: a recording with no
    # action events still opens), so show an explicit "(no events)" rather than
    # a bare "None".
    events_display = envelope["events_path"] or "(no events)"
    console.print(f"  events: [dim]{escape(str(events_display))}[/dim]")
    if envelope.get("video_pixfmt_remediated"):
        console.print("  [dim](video remediated for AVKit compatibility)[/dim]")


@cli.command("login")
@click.option("--json", "as_json", is_flag=True,
              default=lambda: _should_default_to_json(),
              help="Output as JSON. Auto-detected when stdout is not a TTY.")
def login_cmd(as_json):
    """Sign in to your ScreenCap cloud account (opens your browser).

    Local recording, scrubbing, and playback never require sign-in — this is
    only needed to upload to the cloud. The long-lived refresh token is stored
    in your macOS Keychain; the short-lived ID token stays in memory.
    """
    from screencap import auth

    try:
        state = auth.login()
    except auth.AuthError as e:
        if as_json:
            click.echo(json.dumps(
                {"ok": False, "schema_version": _AUTH_SCHEMA_VERSION, "error": str(e)}
            ))
        else:
            console.print(f"[red]Sign-in failed:[/red] {escape(str(e))}")
        sys.exit(1)
    # E2EE slice: create the device-held cloud key in this foreground process.
    # The one-time Keychain ACL prompt needs a foreground identity — the daemon
    # and engine can only read a delivered key, never create it. Flag-gated; a
    # Keychain hiccup here must never fail the sign-in (the key is (re)created on
    # the next login attempt).
    from screencap.config import get_cloud_e2ee_enabled

    if get_cloud_e2ee_enabled():
        try:
            from screencap import cloud_crypto

            cloud_crypto.get_or_create_cloud_kek()
        except Exception:  # noqa: BLE001 — never fail sign-in over key setup
            import logging

            logging.getLogger(__name__).warning(
                "could not create cloud E2EE key at login (will retry next login)",
                exc_info=True,
            )
    if as_json:
        click.echo(json.dumps({
            "ok": True, "schema_version": _AUTH_SCHEMA_VERSION,
            "signed_in": True, "uid": state.uid, "email": state.email,
        }))
        return
    console.print(f"[green]Signed in[/green] as [bold]{escape(str(state.email or state.uid))}[/bold].")


@cli.command("logout")
def logout_cmd():
    """Sign out and remove your stored cloud credentials."""
    from screencap import auth

    if auth.logout():
        console.print("[green]Signed out.[/green]")
    else:
        console.print("[yellow]No stored credentials (already signed out).[/yellow]")


@cli.command("whoami")
@click.option("--json", "as_json", is_flag=True,
              default=lambda: _should_default_to_json(),
              help="Output as JSON. Auto-detected when stdout is not a TTY.")
@click.option("--force-refresh", "force_refresh", is_flag=True,
              help="Force an ID-token re-mint before reading state, so a "
                   "just-granted subscription claim is picked up immediately.")
def whoami_cmd(as_json, force_refresh):
    """Show the signed-in cloud account (or 'not signed in')."""
    from screencap import auth

    if force_refresh:
        # Post-checkout path: re-mint the ID token so a webhook-set ``subscribed``
        # claim is visible on this and subsequent reads. A refresh failure is not
        # fatal — whoami() below reports stale rather than crashing.
        try:
            auth.get_id_token(force_refresh=True)
        except Exception:
            pass

    try:
        info = auth.whoami()
    except Exception as e:
        # whoami() is built not to raise, but an envelope contract needs an
        # ok:false path so an agent/CI consumer never has to parse a crash.
        if as_json:
            click.echo(json.dumps(
                {"ok": False, "schema_version": _AUTH_SCHEMA_VERSION, "error": str(e)}
            ))
            sys.exit(1)
        console.print(f"[red]Error checking sign-in state:[/red] {escape(str(e))}")
        sys.exit(1)

    # U14 (KTD-4): refresh the last-known-good entitlement lease the local gates
    # (U8/U9) read. This is the app's actual post-checkout signal path — the macOS
    # app polls `whoami --json` and calls `whoami --force-refresh` on return from
    # Stripe — so a just-converted user's freshly-materialized `tier` re-arms the
    # lease here (a shared on-disk file the daemon gates read). A definitive
    # not-entitled clears it; a stale/offline result preserves it. Best-effort —
    # lease upkeep must never break `whoami`.
    try:
        from screencap.daemon import entitlement_lease

        entitlement_lease.reconcile_from_whoami(dict(info))
    except Exception:  # noqa: BLE001 — lease upkeep must never break `whoami`
        pass
    if as_json:
        envelope = {"ok": True, "schema_version": _AUTH_SCHEMA_VERSION, **info}
        click.echo(json.dumps(envelope))
        return
    if info.get("signed_in"):
        who = info.get("email") or info.get("uid") or "(unknown account)"
        suffix = " [dim](offline — could not refresh)[/dim]" if info.get("stale") else ""
        console.print(f"Signed in as [bold]{escape(str(who))}[/bold]{suffix}")
    else:
        console.print("Not signed in. Run [bold]screencap login[/bold] to upload to the cloud.")


@cli.command("checkout-url")
@click.option("--tier", type=click.Choice(["local", "cloud"]), required=True,
              help="Which paid tier to check out: local (unlimited local) or cloud.")
@click.option("--json", "as_json", is_flag=True,
              default=lambda: _should_default_to_json(),
              help="Output as JSON. Auto-detected when stdout is not a TTY.")
def checkout_url_cmd(tier, as_json):
    """Print a hosted Stripe Checkout URL for the given paid tier.

    Requires sign-in (the uid is derived server-side from the bearer token). The
    macOS app opens the printed URL in the browser; on return it force-refreshes
    the entitlement (`whoami --force-refresh`) to pick up the granted plan. The
    tier selects the price only — the webhook re-derives entitlement from the
    paid price. The error envelope carries a machine-readable `code`
    (`not_signed_in`, `network`) alongside `error`, so the app's error mapper
    keys on code, never message text (KTD-5) — same contract as `portal-url`.
    """
    from screencap import upload

    try:
        url = upload.request_checkout_url(tier)
    except Exception as e:
        if as_json:
            click.echo(json.dumps({
                "ok": False,
                "schema_version": _AUTH_SCHEMA_VERSION,
                "error": str(e),
                "code": getattr(e, "code", "unknown"),
            }))
            sys.exit(1)
        console.print(f"[red]Couldn't start checkout:[/red] {escape(str(e))}")
        sys.exit(1)
    if as_json:
        click.echo(json.dumps(
            {"ok": True, "schema_version": _AUTH_SCHEMA_VERSION, "url": url}
        ))
        return
    console.print(url)


@cli.command("portal-url")
@click.option("--json", "as_json", is_flag=True,
              default=lambda: _should_default_to_json(),
              help="Output as JSON. Auto-detected when stdout is not a TTY.")
def portal_url_cmd(as_json):
    """Print a hosted Stripe customer-portal URL for managing the subscription.

    Requires sign-in (the uid is derived server-side from the bearer token) and
    an active or trialing subscription. The macOS app opens the printed URL in
    the browser; on return it force-refreshes the entitlement to pick up any plan
    change. The error envelope carries a machine-readable `code`
    (`no_subscription`, `not_signed_in`, `network`) alongside `error`, so the
    app's error mapper keys on code, never message text (KTD-5).
    """
    from screencap import upload

    try:
        url = upload.request_portal_url()
    except Exception as e:
        if as_json:
            click.echo(json.dumps({
                "ok": False,
                "schema_version": _AUTH_SCHEMA_VERSION,
                "error": str(e),
                "code": getattr(e, "code", "unknown"),
            }))
            sys.exit(1)
        console.print(
            f"[red]Couldn't open subscription management:[/red] {escape(str(e))}"
        )
        sys.exit(1)
    if as_json:
        click.echo(json.dumps(
            {"ok": True, "schema_version": _AUTH_SCHEMA_VERSION, "url": url}
        ))
        return
    console.print(url)


@cli.command("reconcile-entitlement")
@click.option("--json", "as_json", is_flag=True,
              default=lambda: _should_default_to_json(),
              help="Output as JSON. Auto-detected when stdout is not a TTY.")
def reconcile_entitlement_cmd(as_json):
    """Self-heal a dropped webhook: grant the subscription if Stripe confirms it.

    GRANT-ONLY, sign-in required (the uid is derived server-side from the bearer
    token). The macOS app calls this from "I've paid — check now" BEFORE
    force-refreshing `whoami`, so a paying customer whose checkout webhook was
    dropped is never permanently stuck. Prints the resolved `subscribed` state.
    """
    from screencap import upload

    try:
        subscribed = upload.request_reconcile_entitlement()
    except Exception as e:
        if as_json:
            click.echo(json.dumps(
                {"ok": False, "schema_version": _AUTH_SCHEMA_VERSION, "error": str(e)}
            ))
            sys.exit(1)
        console.print(f"[red]Couldn't check subscription:[/red] {escape(str(e))}")
        sys.exit(1)
    if as_json:
        click.echo(json.dumps(
            {"ok": True, "schema_version": _AUTH_SCHEMA_VERSION, "subscribed": subscribed}
        ))
        return
    console.print("Subscription active." if subscribed else "No active subscription found.")


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
                    err_console.print(f"[yellow]Warning:[/yellow] Recording '{escape(str(rec_dir.name))}' contains no events.")
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
        err_console.print(f"[red]Error:[/red] Recording not found: {escape(str(name))}")
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
      in_allow_apps, allow_confirmed, confirmation_required,
      is_matrix_exclude, has_per_frame_overrides

    Schema v3 (SCR-235): ``allow_confirmed`` is true when the entry is an
    authoritative confirmed allow (present in both allow_apps and
    confirmed_allow_apps); ``confirmation_required`` is true when the app's
    class needs --confirm-sensitive to allow (matrix EXCLUDE in any mode).
    ``is_matrix_exclude`` (EXCLUDE in *every* mode) is kept for backward
    compatibility but no longer drives row locking.

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
            requires_confirmed_allow,
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
            err_console.print(f"[red]Error:[/red] {escape(str(exc))}")
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
        classification_source = classification.source
        # User app_classes overrides win (SCR-235): the CLI gate and the
        # runtime classifier both resolve them first, so the payload must
        # agree — an override-blind row routes a user-reclassified sensitive
        # app through the wrong transition (plain allow instead of the
        # confirmation dialog) and dead-ends on the gate's refusal.
        override_class = privacy_cfg.app_class_for(meta.bundle_id)
        if override_class is not None:
            ctx_class = override_class
            classification_source = "user_config"
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
            "classification_source": classification_source,
            "resolved_action": decision.action.value,
            # Accessor methods, not raw set membership — the config sets are
            # case-normalized (SCR-235) and mixed-case bundle IDs must match.
            "in_exclude_apps": privacy_cfg.is_excluded_app(meta.bundle_id),
            "in_allow_apps": privacy_cfg.is_allowed_app(meta.bundle_id),
            "allow_confirmed": privacy_cfg.is_confirmed_allowed_app(meta.bundle_id),
            "confirmation_required": requires_confirmed_allow(ctx_class),
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
        console.print(f"  {badge} {escape(str(row['display_name']))} ({escape(str(row['bundle_id']))})")
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
    """Report recording state by querying the daemon.

    Thin client of ``GET /v0/session.snapshot``. When the daemon isn't
    reachable (no LaunchAgent installed and no auto-spawned daemon —
    common on a fresh CLI-only install), report ``is_recording=false``
    rather than surfacing a daemon error, because "no daemon" and "not
    recording" are equivalent observed states for the user.
    """
    import json as _json
    import time as _time
    from typing import TypedDict

    from screencap.cli._autospawn import (
        LaunchAgentNotRunningError,
        ensure_daemon_or_spawn,
    )
    from screencap.cli._daemon_client import (
        DaemonAPIError,
        DaemonHTTPClient,
        DaemonUnreachableError,
        SchemaMismatchError,
    )

    class StatusPayload(TypedDict):
        ok: bool
        schema_version: int
        is_recording: bool
        started_at: float | None
        elapsed: float | None
        recording_name: str | None
        capture_dir: str | None
        claimant: str | None
        daemon_reachable: bool
        privacy_configured: bool
        nlp_models_cached: bool | None

    payload: StatusPayload = {
        "ok": True,
        "schema_version": _STATUS_SCHEMA_VERSION,
        "is_recording": False,
        "started_at": None,
        "elapsed": None,
        "recording_name": None,
        "capture_dir": None,
        "claimant": None,
        "daemon_reachable": False,
        "privacy_configured": False,
        "nlp_models_cached": False,
    }
    if no_nlp_check:
        payload["nlp_models_cached"] = None

    # Config readiness flags — cheap and useful for first-run UI. These
    # do not require the daemon.
    try:
        from screencap.config import _load_toml
        privacy_section = (_load_toml().get("privacy") or {})
        payload["privacy_configured"] = bool(privacy_section)
    except Exception:
        pass

    if not no_nlp_check:
        try:
            from screencap.redaction import are_nlp_models_cached
            payload["nlp_models_cached"] = bool(are_nlp_models_cached())
        except Exception:
            pass

    # ``auto_spawn=False``: status of a missing daemon is "not recording";
    # spawning one just to confirm "no, nothing's happening" is wasteful.
    try:
        ensure_daemon_or_spawn(auto_spawn=False)
    except LaunchAgentNotRunningError as exc:
        # Surface the kickstart guidance to stderr even on the --json
        # path so an operator running ``screencap status`` notices the
        # mismatch between launchd's installed state and the daemon's
        # live state.
        click.echo(str(exc), err=True)

    snapshot: dict | None = None
    try:
        with DaemonHTTPClient() as client:
            snapshot = client.snapshot()
            payload["daemon_reachable"] = True
    except DaemonUnreachableError:
        snapshot = None
    except SchemaMismatchError as exc:
        click.echo(
            f"Error: {exc}. Update the daemon: launchctl kickstart -kp "
            f"gui/$UID/com.screencap.daemon",
            err=True,
        )
        snapshot = None
    except DaemonAPIError as exc:
        click.echo(f"Daemon error: {exc.envelope.get('error', 'unknown')}", err=True)
        snapshot = None

    if snapshot and snapshot.get("is_recording"):
        started_at = snapshot.get("started_at")
        if isinstance(started_at, (int, float)):
            payload["is_recording"] = True
            payload["started_at"] = float(started_at)
            payload["elapsed"] = max(0.0, _time.time() - float(started_at))
        payload["recording_name"] = snapshot.get("recording_name")
        # Reconstruct capture_dir from recording_name for backwards
        # compatibility with consumers that grep it. The daemon snapshot
        # itself does not carry the path; ``~/.screencap/recordings/`` is
        # the canonical root.
        name = payload["recording_name"]
        if isinstance(name, str) and name:
            from pathlib import Path as _Path
            payload["capture_dir"] = str(_Path.home() / ".screencap" / "recordings" / name)
        payload["claimant"] = snapshot.get("claimant")

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
        if payload.get("recording_name"):
            console.print(f"  Recording: {escape(str(payload['recording_name']))}")
        if payload.get("claimant"):
            console.print(f"  Claimant: {escape(str(payload['claimant']))}")
    elif not payload["daemon_reachable"]:
        console.print("[dim]Not recording. (Daemon not running.)[/dim]")
    else:
        console.print("[dim]Not recording.[/dim]")


@cli.command()
@click.option("--force", is_flag=True, help="Skip SIGTERM and go straight to SIGKILL.")
@click.option("--json", "as_json", is_flag=True,
              default=lambda: _should_default_to_json(),
              help="Emit machine-readable JSON to stdout instead of prose. "
                   "Auto-detected when stdout is not a TTY (todo 009).")
def stop(force, as_json):
    """Stop the active recording via the daemon.

    Thin client of ``POST /v0/recording.stop``. The daemon owns engine
    supervision after Phase 2 U1, including SIGTERM grace, SIGKILL
    escalation, and orphan teardown — the CLI is purely a remote that
    surfaces the result envelope.

    Without ``--force`` the daemon performs a graceful stop. With
    ``--force`` the daemon SIGKILLs the engine subprocess directly.

    JSON envelope:
      {ok, schema_version, action, stopped, final_state, error}
    where ``action`` ∈ {"sigterm", "sigkill", "no_daemon", "no_recording"}.
    """
    from screencap.cli._autospawn import (
        LaunchAgentNotRunningError,
        ensure_daemon_or_spawn,
    )
    from screencap.cli._daemon_client import (
        DaemonAPIError,
        DaemonHTTPClient,
        DaemonUnreachableError,
        SchemaMismatchError,
    )

    _stop_outcome: dict = {
        "action": "none",
        "stopped": False,
        "final_state": None,
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

    # Stopping a non-existent daemon is a no-op — don't spawn one just
    # to confirm there's nothing to stop.
    try:
        ensure_daemon_or_spawn(auto_spawn=False)
    except LaunchAgentNotRunningError as exc:
        click.echo(str(exc), err=True)
        _stop_outcome["action"] = "no_daemon"
        _stop_outcome["error"] = "launchagent_not_running"
        _emit_stop_result(ok=False, exit_code=1)
        return

    _stop_outcome["action"] = "sigkill" if force else "sigterm"

    try:
        with DaemonHTTPClient() as client:
            result = client.stop(force=force)
    except DaemonUnreachableError:
        _stop_outcome["action"] = "no_daemon"
        if not as_json:
            console.print("[dim]No active recording. (Daemon not running.)[/dim]")
        _emit_stop_result(ok=True)
        return
    except SchemaMismatchError as exc:
        _stop_outcome["error"] = "schema_mismatch"
        if not as_json:
            console.print(f"[red]Error:[/red] {escape(str(exc))}")
            console.print(
                "Update the daemon: [bold]launchctl kickstart -kp "
                "gui/$UID/com.screencap.daemon[/bold]"
            )
        _emit_stop_result(ok=False, exit_code=1)
        return
    except DaemonAPIError as exc:
        code = exc.envelope.get("error", "unknown")
        if code == "not_owned_by_daemon":
            _stop_outcome["action"] = "no_recording"
            _stop_outcome["error"] = "not_owned_by_daemon"
            if not as_json:
                console.print(
                    "[dim]No active recording owned by the daemon.[/dim]"
                )
            _emit_stop_result(ok=True)
            return
        _stop_outcome["error"] = code
        if not as_json:
            console.print(f"[red]Daemon error:[/red] {escape(str(code))}")
        _emit_stop_result(ok=False, exit_code=1)
        return

    _stop_outcome["stopped"] = bool(result.get("stopped"))
    _stop_outcome["final_state"] = result.get("final_state")
    if not as_json:
        if _stop_outcome["stopped"]:
            label = (
                "force-stopped" if _stop_outcome["final_state"] == "force_stopped"
                else "stopped"
            )
            console.print(f"[#22d3ee]Recording {label}.[/#22d3ee]")
        else:
            console.print(
                f"[yellow]Daemon reported final_state={escape(repr(_stop_outcome['final_state']))}.[/yellow]"
            )
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


@cli.command()
@click.argument("names", nargs=-1)
@click.option("--all", "all_recordings", is_flag=True, help="Upload all recordings.")
@click.option("--dry-run", is_flag=True, help="Show files and sizes without uploading.")
@click.option("--force", is_flag=True, help="Re-upload even if already uploaded.")
@click.option("--jobs", "-j", type=click.IntRange(min=1), default=4,
              help="Parallel file transfers per recording (default: 4).")
@click.option("--no-delete", is_flag=True, default=False,
              help="Keep local recording files after upload instead of auto-deleting.")
@click.option("--lock-timeout", type=click.FloatRange(min=0), default=None,
              help="Seconds to wait for a contended per-recording terminal lock "
                   "before reporting it busy (default: 600). The interactive app "
                   "passes a short value (well under its 120s watchdog) so a "
                   "contended lock surfaces as retryable rather than timing out.")
def upload(names, all_recordings, dry_run, force, jobs, no_delete, lock_timeout):
    """Upload recordings to cloud storage.

    Exit codes: 0 = all recordings uploaded (or a retryable "already in progress"
    busy-lock skip); 1 = one or more recordings failed to upload (SCR-79).
    """
    from screencap.upload import resolve_recording_dirs

    try:
        dirs = resolve_recording_dirs(names, all_recordings=all_recordings)
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]Error:[/red] {escape(str(e))}")
        sys.exit(1)

    # Cloud upload requires a signed-in account (R2). Check once up-front so we
    # refuse with a clear prompt and touch NOTHING on disk (no auto-export, no
    # delete), rather than failing mid-upload — R3/AE3. Dry-run is local-only and
    # never needs auth.
    if not dry_run:
        import keyring.errors

        from screencap import auth

        try:
            auth.get_id_token()
        except auth.NotSignedIn:
            console.print(
                "[red]Not signed in.[/red] Run [bold]screencap login[/bold] to "
                "upload to the cloud. Your recordings stay local — nothing was changed."
            )
            sys.exit(1)
        except keyring.errors.KeyringError:
            # The Keychain itself is unreadable (locked, backend error) — distinct
            # from "not signed in". Refuse cleanly and touch nothing on disk.
            console.print(
                "[red]Couldn't read your saved credentials (Keychain locked?).[/red] "
                "Unlock the Keychain and try again — your recordings stay local, "
                "nothing was changed."
            )
            sys.exit(1)
        except auth.AuthError:
            # Transient (offline / token-service hiccup) — don't block on a blip.
            # request_signed_urls refreshes-and-retries, and a real failure
            # surfaces there without deleting anything (fail-closed).
            pass

    # --- Intent warnings ---
    from screencap.catalog import read_intent

    for d in dirs:
        intent = read_intent(d)
        if intent == "local" and not dry_run:
            console.print(
                f"  [yellow]Note:[/yellow] {escape(str(d.name))} is local-intent — post-hoc "
                "scrubbing provides weaker guarantees than capture-time enforcement."
            )

    if not dry_run:
        console.print(
            "[yellow]Warning:[/yellow] Uploads include all captured keystrokes. "
            "Review recordings for sensitive data before sharing.",
            highlight=False,
        )

    # SCR-125 U5: every recording converges through the SINGLE terminal stage.
    # The CLI no longer calls scrub_recording / upload_recording /
    # assert_promotable_to_cloud / recovery directly — run_terminal_stage owns
    # recovery, scrub-reuse, the AE8 hole-refusal, upload (never recording.db),
    # the sentinel, and retention, behind the per-recording flock. ``screencap
    # upload`` is an EXPLICIT promotion: force_destination=cloud uploads a
    # local/legacy/no-intent recording that would otherwise route LOCAL → no-op.
    import signal as _signal

    from screencap._stderr_events import (
        EVENT_UPLOAD_BUSY,
        EVENT_UPLOAD_FAILED,
        EVENT_UPLOAD_PREPARING,
        emit_event,
    )
    from screencap.pipeline_policy import Destination, RetentionPolicy
    from screencap.terminal_stage import (
        _DEFAULT_LOCK_TIMEOUT,
        PromotionRefused,
        TerminalStageBusy,
        run_terminal_stage,
    )

    # SCR-165: keep terminal_stage the single source of truth for the default
    # lock-timeout. The click option defaults to None (sentinel) so we don't
    # import terminal_stage at module-import time (CLAUDE.md: deferred heavy
    # imports keep ``screencap --help`` fast); resolve it here, where the import
    # already happens. Help text still advertises "(default: 600)".
    if lock_timeout is None:
        lock_timeout = _DEFAULT_LOCK_TIMEOUT

    # SCR-94: install a top-level SIGTERM handler BEFORE the terminal stage runs
    # so a cancel during the multi-second pre-upload prep phase (GCS reconcile,
    # recovery, scrub/export — all inside run_terminal_stage, which runs BEFORE
    # upload_recording installs its own finer handler) still emits a terminal
    # ``upload_failed(error="interrupted")`` event. Without it the child dies on
    # the default SIGTERM disposition with no event, and the SwiftUI
    # UploadController surfaces a raw "exited with code N" instead of a clean
    # cancel (the review window's close-as-cancel terminate()s this subprocess).
    #
    # upload_recording swaps in — and restores — its own SIGTERM handler for the
    # transfer phase, so exactly one of the two is installed at any instant: a
    # cancel emits exactly one terminal event, by construction. signal.signal
    # requires the main thread (the CLI path always is); an off-main-thread
    # caller skips both install and restore.
    _current_name: str | None = None
    _interrupt_emitted = False

    def _emit_interrupted_and_raise(_signum, _frame):
        nonlocal _interrupt_emitted
        if not _interrupt_emitted:
            _interrupt_emitted = True
            fields: dict[str, str] = {"error": "interrupted"}
            if _current_name is not None:
                fields["recording"] = _current_name
            emit_event(EVENT_UPLOAD_FAILED, **fields)
        raise KeyboardInterrupt

    def _on_progress(phase: str) -> None:
        # SCR-175: surface run_terminal_stage's otherwise-silent pre-upload prep
        # (the contended-lock handoff, the GCS reconcile, the scrub/mask
        # produce) as a lightweight stderr event. The Swift UploadController
        # arms a single 120s inactivity watchdog reset only on a parsed event;
        # without this the lock wait is subtracted from the same budget the
        # silent converge draws on and a contended-then-released lock can
        # re-trip the watchdog as a false `.failed("upload timed out")`. Reads
        # `_current_name` at call time so it labels the recording in flight.
        # NOT a terminal event (distinct from upload_started, which still fires
        # at transfer); tolerant consumers just need *an* event to reset.
        emit_event(EVENT_UPLOAD_PREPARING, recording=_current_name, phase=phase)

    # Install INSIDE the outer try so a SIGTERM arriving in the gap between
    # install and try-entry still routes through the restoring finally
    # (mirrors upload.upload_recording's proven handler). _UNSET / the
    # previous-handler sentinel are set BEFORE the try so the finally can
    # tell "install succeeded with a None (C-set) previous handler" from
    # "install never ran" (off-main-thread ValueError).
    _UNSET: object = object()
    _previous_sigterm: object = _UNSET

    total_count = len(dirs)
    n_ok = 0
    n_failed = 0
    # A recording whose finalize lock is held by another process is NOT a failure
    # (it's being uploaded elsewhere / is retryable) — tracked apart from n_failed
    # so it never flips the exit code (SCR-79).
    n_busy = 0
    try:
        try:
            _previous_sigterm = _signal.signal(
                _signal.SIGTERM, _emit_interrupted_and_raise
            )
        except ValueError:
            pass  # off-main-thread — the restore guard below no-ops.

        for i, d in enumerate(dirs, 1):
            _current_name = d.name
            if total_count > 1:
                console.print(f"\n[bold][{i}/{total_count}][/bold] {d.name}")
            try:
                result = run_terminal_stage(
                    d,
                    console=console,
                    force=force,
                    dry_run=dry_run,
                    # SCR-165: blocking-with-timeout bound for the per-recording
                    # terminal lock. Default 600s preserves the CLI/agent
                    # converge-over-winner behavior; the interactive Swift path
                    # passes a short value so a contended lock raises
                    # TerminalStageBusy (→ upload_busy) well under its 120s
                    # inactivity watchdog instead of being SIGTERMed mid-wait.
                    lock_timeout=lock_timeout,
                    force_destination=Destination.CLOUD,
                    # --no-delete keeps local media after upload (a per-run override).
                    retention_override=(
                        RetentionPolicy.KEEP_FOREVER if no_delete else None
                    ),
                    # SCR-175: emit upload_preparing during the silent prep so the
                    # interactive Swift watchdog sees the lock handoff + reconcile +
                    # scrub as activity instead of timing them out as a wedged child.
                    on_progress=_on_progress,
                )
            except PromotionRefused as e:
                console.print(
                    f"[red]Error:[/red] {escape(str(e))}\n"
                    "[dim]Upload skipped — nothing was changed.[/dim]"
                )
                n_failed += 1
                continue
            except TerminalStageBusy:
                # SCR-158: a contended terminal lock is a RETRYABLE skip, not a
                # failure (exit stays 0). Emit a structured terminal stderr event
                # BEFORE the human-readable line so the event-first Swift
                # UploadController maps it to a retry-friendly state instead of
                # rendering exit-0-without-an-event as a hard failure, and so an
                # autonomous agent can tell busy-skip apart from a successful
                # no-op without scraping rich console text. NOT upload_failed —
                # SCR-79 ties that event to a non-zero exit.
                emit_event(EVENT_UPLOAD_BUSY, recording=d.name, retryable=True)
                console.print(
                    f"  [yellow]{escape(str(d.name))}: upload already in progress[/yellow] — a "
                    "recording is finalizing, or another upload / daemon resume holds "
                    "the lock. Try again shortly."
                )
                n_busy += 1
                continue
            except FileNotFoundError as e:
                console.print(f"[red]Error:[/red] {escape(str(e))}")
                n_failed += 1
                continue
            except RuntimeError as e:
                # SCR-159: a generic RuntimeError reaching THIS handler is
                # per-recording, not a batch-global failure — count it and continue
                # rather than abandon the rest of the batch. Note the transient
                # upload errors (auth blip, timeout, service error) and scrub/mask
                # failures do NOT arrive here: run_terminal_stage absorbs them in its
                # own catchers and surfaces them as result.upload_warning (handled
                # below). What escapes to here is a RuntimeError from
                # run_terminal_stage's reconcile / ledger / retention / sentinel
                # logic — still per-recording. Treat it like the PromotionRefused /
                # FileNotFoundError handlers above; the `if n_failed: sys.exit(1)`
                # gate below still yields the non-zero exit code (SCR-79).
                console.print(f"[red]Error:[/red] {escape(str(e))}")
                n_failed += 1
                continue

            if dry_run:
                console.print(
                    f"  [dim]Would upload {d.name} → cloud (dry run; nothing changed).[/dim]"
                )
                continue

            # A FAILED chunk (scrub/mask fail-closed) or an upload warning means the
            # recording did not fully converge — local media is preserved.
            if result.failed_indices or result.upload_warning:
                if result.upload_warning:
                    console.print(f"  [yellow]Warning:[/yellow] {escape(str(result.upload_warning))}")
                if result.failed_indices:
                    console.print(
                        f"  [yellow]{escape(str(d.name))}: {len(result.failed_indices)} chunk(s) could "
                        "not be prepared — local media preserved, sentinel withheld.[/yellow]"
                    )
                n_failed += 1
                continue

            n_ok += 1
            note = " (stitching triggered)" if result.sentinel_uploaded else ""
            console.print(
                f"\n[green]Uploaded {d.name}[/green] "
                f"({result.n_uploaded} chunk(s) uploaded, {result.n_skipped} skipped){note}"
            )

        if total_count > 1 and not dry_run:
            busy_note = f", {n_busy} in progress" if n_busy else ""
            console.print(
                f"\n[bold]Done.[/bold] {n_ok} uploaded, {n_failed} failed{busy_note}"
            )

        # SCR-79: pin the exit-code contract. Any recording that failed to upload —
        # a per-file upload failure (which emits the terminal ``upload_failed``
        # event), a refused promotion (holes), or a missing recording — makes the
        # command exit non-zero, so a consumer that trusts the exit code (the U7
        # Swift UploadController reads ``terminationStatus`` alongside the stderr
        # event) agrees with the terminal event. A ``TerminalStageBusy`` skip is
        # retryable, not a failure (n_busy), and keeps exit 0. A --dry-run is a
        # read-only preview that touches nothing, so it never fails-exits (mirrors
        # the ``total_count > 1 and not dry_run`` guard on the Done summary above).
        if n_failed and not dry_run:
            sys.exit(1)
    except KeyboardInterrupt:
        # Reached two ways during the prep phase. On a SIGTERM (window-close
        # cancel) our handler ran first, so it already emitted the terminal
        # upload_failed(interrupted) event before raising. A native Ctrl+C /
        # SIGINT, by contrast, raises KeyboardInterrupt directly WITHOUT
        # invoking our SIGTERM handler, so it reaches here with no event
        # emitted (out of scope for SCR-94 — the SwiftUI cancel path is
        # SIGTERM). Either way, exit non-zero without a traceback so the batch
        # stops cleanly.
        sys.exit(130)
    finally:
        # Restore only if install actually succeeded (_UNSET ⇒ off-main-thread).
        # A genuine None previous handler (C-set) restores to SIG_DFL.
        if _previous_sigterm is not _UNSET:
            try:
                _signal.signal(
                    _signal.SIGTERM,
                    _previous_sigterm
                    if _previous_sigterm is not None
                    else _signal.SIG_DFL,
                )
            except ValueError:
                pass


@cli.command()
@click.argument("names", nargs=-1)
@click.option("--dest", default=None, help="Destination directory (default: ~/.screencap/downloads/).")
@click.option("--dry-run", is_flag=True, help="Show what would be downloaded without downloading.")
@click.option("--force", is_flag=True, help="Re-download all recordings, ignoring markers.")
@click.option("--jobs", "-j", type=click.IntRange(min=1), default=4,
              help="Parallel file transfers per recording (default: 4).")
def download(names, dest, dry_run, force, jobs):
    """Download recordings from cloud storage.

    Optionally pass one or more recording NAMES to download only those.
    With no names, all of your remote recordings are listed and downloaded.
    """
    from screencap.download import (
        _fmt_size,
        _resolve_dest_dir,
        download_recording,
        list_remote_recordings,
    )

    try:
        dest_dir = _resolve_dest_dir(dest)
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] {escape(str(e))}")
        sys.exit(1)

    if names:
        from types import SimpleNamespace
        remote = [SimpleNamespace(name=n, total_size=0, file_count=0) for n in names]
    else:
        try:
            remote = list_remote_recordings()
        except RuntimeError as e:
            console.print(f"[red]Error:[/red] {escape(str(e))}")
            sys.exit(1)

        if not remote:
            console.print("No recordings available for download.")
            return

    console.print(
        f"Found [bold]{len(remote)}[/bold] recording(s) to download."
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
            console.print(f"  [red]Error:[/red] {escape(str(e))}")
            all_failed += 1
        except RuntimeError as e:
            console.print(f"  [red]Error:[/red] {escape(str(e))}")
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
            f"[red]Error:[/red] No audio found for recording '{escape(str(name))}'. "
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

    from screencap.transcription import (
        _resolve_backend_interactive,
        _run_api_transcription,
        _select_local_model,
    )

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
            console.print(f"[red]Transcription failed:[/red] {escape(str(e))}")
            sys.exit(1)

    # Fix #2: hard-check that output was actually created
    if not transcript_path.exists():
        console.print(
            "[red]Error:[/red] Transcription completed but no transcript file was created. "
            "Check the logs above for errors."
        )
        sys.exit(1)

    # Show results
    console.print(f"\n[green]Transcript saved:[/green] {escape(str(transcript_path))}")
    console.print(f"[green]Timestamps saved:[/green] {escape(str(transcript_json_path))}")
    text = transcript_path.read_text().strip()
    if text:
        preview = text[:500] + ("..." if len(text) > 500 else "")
        console.print(f"\n[bold]Preview:[/bold]\n{escape(preview)}")


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
        console.print(f"[red]Error:[/red] {escape(str(e))}")
        raise SystemExit(1)
    except ImportError as e:
        console.print(
            "[red]Error: Privacy dependencies are missing.[/red]\n"
            "Reinstall or update screencap."
        )
        console.print(f"[dim]{escape(str(e))}[/dim]")
        raise SystemExit(1)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {escape(str(e))}")
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
      upload_default     Default destination (local/cloud/both/ask)
      content_index_enabled  Index on-screen text for local search (true/false)
    """
    if ctx.invoked_subcommand is not None:
        # privacy subcommand path — defer to the subcommand handler.
        return
    from screencap.config import (
        _CONFIG_PATH,
        get_audio_default,
        get_auto_delete_after_upload,
        get_chunk_duration,
        get_cloud_e2ee_enabled,
        get_content_index_backfill_declined,
        get_content_index_consent_declined,
        get_content_index_enabled,
        get_corpus_encrypted,
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
        _BOOL_KEYS = {"show_on_website", "audio_default",
                       "auto_update", "auto_delete_after_upload", "wifi_metrics", "app_versions",
                       "content_index_enabled", "content_index_consent_declined",
                       "content_index_backfill_declined", "cloud_e2ee_enabled"}
        _CHOICE_KEYS = {"upload_default": ("local", "cloud", "both", "ask"),
                         "segmentation_mode": ("llm", "idle")}

        if key in _BOOL_KEYS:
            if raw_value.lower() in ("true", "1", "yes"):
                value = True
            elif raw_value.lower() in ("false", "0", "no"):
                value = False
            else:
                console.print(f"[red]Error:[/red] {escape(str(key))} must be true or false, got: {escape(str(raw_value))}")
                raise SystemExit(1)
        elif key in _CHOICE_KEYS:
            valid = _CHOICE_KEYS[key]
            if raw_value.lower() not in valid:
                console.print(f"[red]Error:[/red] {escape(str(key))} must be one of {valid}, got: {escape(str(raw_value))}")
                raise SystemExit(1)
            value = raw_value.lower()
        else:
            all_keys = sorted(_BOOL_KEYS | set(_CHOICE_KEYS.keys()))
            console.print(f"[red]Error:[/red] Unknown setting: {escape(str(key))}")
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
        console.print(f"  [bold]{escape(str(key))}[/bold] = {escape(str(value))}")
        return

    # --- Display all settings ---
    chunk = get_chunk_duration()
    rest = get_rest_threshold()
    show = get_show_on_website()

    # Structured payload first so the same field set drives both JSON and
    # prose paths. ``settings --json`` (todo 012) is the read-side analogue
    # of the existing ``settings privacy --json`` mutation surface — agents
    # need a stable shape they can diff.
    from screencap.privacy_settings import _build_privacy_settings_block
    settings_payload = {
        "show_on_website": bool(show),
        "upload_default": str(get_upload_default()),
        "audio_default": bool(get_audio_default()),
        "chunk_duration": float(chunk),
        "auto_delete_after_upload": bool(get_auto_delete_after_upload()),
        "rest_threshold_seconds": float(rest),
        "recordings_dir": str(get_recordings_dir()),
        "content_index_enabled": bool(get_content_index_enabled()),
        "content_index_consent_declined": bool(get_content_index_consent_declined()),
        "content_index_backfill_declined": bool(get_content_index_backfill_declined()),
        "cloud_e2ee_enabled": bool(get_cloud_e2ee_enabled()),
        # Search U8: whether the recall corpus is encrypted (guardrails on) — the app
        # gates present-user auth on corpus-still display only when this is true.
        "corpus_encrypted": bool(get_corpus_encrypted()),
        "privacy": _build_privacy_settings_block(),
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
    console.print(f"  Chunk duration:           {chunk_str}")
    console.print(f"  Auto-delete after upload: {'enabled' if get_auto_delete_after_upload() else 'disabled'}")
    console.print(f"  Rest threshold:           {rest:.0f}s ({rest / 60:.0f} min)")
    console.print(f"  Recordings dir:           {get_recordings_dir()}")
    console.print()
    console.print("[dim]  Change with: screencap settings --set KEY=VALUE[/dim]")
    console.print()


@settings.command("privacy")
@click.argument("field")
@click.argument("op", type=click.Choice(["add", "remove", "set"]))
@click.argument("value")
@click.option("--json", "as_json", is_flag=True,
              default=lambda: _should_default_to_json(),
              help="Emit machine-readable JSON to stdout instead of prose to stderr. "
                   "Auto-detected when stdout is not a TTY (todo 021).")
@click.option("--confirm-sensitive", "confirm_sensitive", is_flag=True,
              help="Confirm allow-listing a sensitive app (password manager, "
                   "banking, auth/payment flows). A confirmed allow is "
                   "authoritative over the privacy matrix in every mode "
                   "(SCR-235).")
def settings_privacy(field, op, value, as_json, confirm_sensitive):
    """Mutate a [privacy] field in config.toml (Unit 4b).

    \b
    Valid FIELD names (todo 022):
      list fields (add/remove): exclude_apps, allow_apps, confirmed_allow_apps,
                                mask_domains, mask_title_patterns
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

    ``allow_apps add`` writes a *confirmed* allow (SCR-235): the entry is
    authoritative over the privacy matrix in every mode. Sensitive classes
    (excluded by the matrix in any mode) require ``--confirm-sensitive``;
    unclassified bundles are refused until classified via ``app_classes``.
    ``confirmed_allow_apps add`` is gated identically. ``allow_apps remove``
    also drops the confirmation, so re-allowing a sensitive app re-prompts.
    """
    import json as _json

    import tomlkit

    from screencap.privacy_settings import (
        _PRIVACY_LIST_FIELDS,
        _PRIVACY_MAP_FIELDS,
        _PRIVACY_MODE_VALUES,
        _PRIVACY_SCALAR_FIELDS,
        _privacy_config_writer,
        _privacy_list_field_value,
        _settings_privacy_apply,
    )

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
        err_console.print(f"[red]Error:[/red] Unknown privacy field: {escape(str(field))}")
        err_console.print(f"[dim]Available: {', '.join(all_fields)}[/dim]")
        _result(False, exit_code=1, error=f"unknown_field:{field}")

    if is_list and op == "set":
        err_console.print(f"[red]Error:[/red] {escape(str(field))} is a list — use add/remove, not set.")
        _result(False, exit_code=1, error=f"list_field_set_op:{field}")
    if is_scalar and op != "set":
        err_console.print(f"[red]Error:[/red] {escape(str(field))} is a scalar — use set, not {escape(str(op))}.")
        _result(False, exit_code=1, error=f"scalar_field_bad_op:{field}={op}")

    value = _privacy_list_field_value(value)

    # Scalar normalization & validation
    parsed_value: object = value
    if is_scalar:
        if field == "mode":
            if value.lower() not in _PRIVACY_MODE_VALUES:
                err_console.print(
                    f"[red]Error:[/red] mode must be one of "
                    f"{_PRIVACY_MODE_VALUES}, got: {escape(str(value))}"
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
                    f"[red]Error:[/red] {escape(str(field))} must be true/false, got: {escape(str(value))}"
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
            confirm_sensitive=confirm_sensitive,
        )

    if changed:
        err_console.print(f"  [bold]privacy.{escape(str(field))}[/bold] {escape(str(op))} {escape(str(value))}")
        _result(True, changed=True)


# ---------------------------------------------------------------------------
# settings intelligence — the Intelligence provider + per-task consent surface
# (U8). The write side of config.get_llm_provider / get_llm_cloud_provider /
# get_summary_cloud_consent / get_recall_cloud_consent; the Swift pane (U9)
# reads it back via --json.
# ---------------------------------------------------------------------------


def _build_intelligence_settings_block() -> dict:
    """Return the current ``[intelligence]`` settings, via the config getters.

    Pointer-only, secret-free: the active provider, the configured cloud
    provider (or ``None``), and each cloud-consent row. The frames/images row
    is reported as a fixed ``false`` (never-cloud, R9) so the Swift pane can
    render it as a non-interactive "always off" without a special case.
    """
    from screencap import config
    from screencap.segmentation.endpoint import classify_endpoint

    endpoint = config.get_local_server_endpoint()
    try:
        from screencap.models import is_model_installed

        downloaded_installed = is_model_installed()
    except Exception:
        downloaded_installed = False

    return {
        "provider": config.get_llm_provider(),
        "cloud_provider": config.get_llm_cloud_provider(),
        "summary_cloud_consent": config.get_summary_cloud_consent(),
        "recall_cloud_consent": config.get_recall_cloud_consent(),
        # Fixed guards (R7/R9) — surfaced so the pane needn't hard-code them.
        "day_split_cloud_consent": False,
        "frames_cloud_consent": False,
        # SCR-239 BYO endpoint (redacted — never echo userinfo/query) + its
        # LOCAL/REMOTE classification, and the downloaded-model install state.
        "local_server_endpoint": _redact_url(endpoint),
        "endpoint_classification": classify_endpoint(endpoint) if endpoint else None,
        "downloaded_model_installed": downloaded_installed,
        # BYO API-key presence (U2). A per-vendor *presence boolean only* — the key
        # value is NEVER exposed here (R3). ``*-cli`` delegation ids hold no key.
        **_byo_key_presence(),
        # BYO CLI-delegation availability (U4). A per-vendor *availability boolean*
        # so the pane can render each ``*-cli`` option available / needs-attention
        # (R5). Existence/stat only — never opens the vendor auth files (KTD1).
        **_byo_cli_availability(),
    }


def _byo_key_presence() -> dict:
    """Return ``{"<vendor>_key_present": bool}`` for each BYO API-key vendor.

    Presence only — the key value never appears in the read-back (R3). Fails
    closed (a Keychain error reports ``False``) so a locked Keychain degrades the
    flag rather than crashing the read. The whole map defaults to all-``False`` if
    the secrets module can't be imported at all.
    """
    try:
        from screencap.segmentation import secrets as byo_secrets
    except Exception:
        return {}
    return {
        f"{vendor}_key_present": byo_secrets.has_key(vendor)
        for vendor in byo_secrets.KEY_VENDORS
    }


def _byo_cli_availability() -> dict:
    """Return ``{"<vendor>_cli_available": bool}`` for each BYO CLI-delegation id.

    Availability = the vendor CLI binary resolves AND a vendor auth artifact
    exists on disk (existence/stat only — never reads the auth files, KTD1). The
    Swift pane renders an unavailable option as needs-attention with an
    install/sign-in path (R5). Fails closed (any error → ``False``) so a probe
    failure degrades the flag rather than crashing the read; the whole map
    defaults to empty if the module can't be imported.
    """
    try:
        from screencap.segmentation.providers import cli_delegate
    except Exception:
        return {}
    out: dict = {}
    for vendor in cli_delegate.VENDOR_IDS:
        # ``vendor`` is e.g. "openai-cli" → field "openai_cli_available".
        field = vendor.replace("-", "_") + "_available"
        try:
            out[field] = cli_delegate.CliDelegateProvider(vendor).available()
        except Exception:  # noqa: BLE001 — a probe error must not fail the read
            out[field] = False
    return out


def _redact_url(url):
    """Strip userinfo/query/fragment from a URL so a token can't leak (SCR-239)."""
    if not url:
        return url
    from urllib.parse import urlparse, urlunparse

    try:
        p = urlparse(url)
        netloc = p.hostname or ""
        if p.port:
            netloc = f"{netloc}:{p.port}"
        return urlunparse(p._replace(netloc=netloc, params="", query="", fragment=""))
    except Exception:
        return "<redacted>"


# The out-of-band BYO-key delivery channel: a 0o600 file whose *path* (not the
# secret) is passed via this env var, mirroring ``auth.ENGINE_TOKEN_FILE_ENV``.
# The default key-set path reads the secret from **stdin**; this env var is the
# alternative for a caller that would rather hand off a temp file. Either way the
# secret NEVER transits argv (KTD3 — argv is world-readable via ``ps``).
_BYO_KEY_FILE_ENV = "SCREENCAP_BYO_KEY_FILE"


def _read_byo_key_from_stdin_or_file() -> str | None:
    """Read a BYO API key from the :data:`_BYO_KEY_FILE_ENV` file, else stdin.

    Returns the stripped key, or ``None`` if nothing was supplied (empty stdin /
    missing-or-empty file). The secret is read from a 0o600 file path or piped on
    stdin — **never** from an argv value (KTD3). A trailing newline (the shape a
    ``echo`` / ``here-string`` pipe produces) is stripped.

    The file channel is rejected unless it is **exactly mode 0o600** (owner
    read/write only), matching the engine-token file pattern
    (:data:`screencap.auth.ENGINE_TOKEN_FILE_ENV`): a world/group-readable secret
    file is a leak, so we fail closed (``None``) rather than read it.
    """
    path = os.environ.get(_BYO_KEY_FILE_ENV)
    if path:
        import stat as _stat
        from pathlib import Path

        p = Path(path)
        try:
            st = p.stat()
        except OSError:
            return None
        # Reject anything the group or world can read (or a non-regular file).
        if not _stat.S_ISREG(st.st_mode) or (st.st_mode & 0o077):
            from rich.console import Console

            Console(stderr=True).print(
                f"[red]Error:[/red] {_BYO_KEY_FILE_ENV} must point at a mode-0o600 "
                "regular file (owner read/write only); refusing to read a "
                "world/group-readable secret file."
            )
            return None
        try:
            raw = p.read_text()
        except OSError:
            return None
        return raw.strip() or None
    # Fall back to stdin. A TTY with nothing piped would block forever, so treat
    # an interactive stdin as "no key supplied" rather than hang.
    if sys.stdin.isatty():
        return None
    raw = sys.stdin.read()
    return raw.strip() or None


@settings.command("intelligence")
@click.argument("row", required=False)
@click.argument("op", required=False, type=click.Choice(["set"]))
@click.argument("value", required=False)
@click.option("--json", "as_json", is_flag=True,
              default=lambda: _should_default_to_json(),
              help="Emit the current intelligence settings as JSON (read-back "
                   "for the Swift pane), or the write result. Auto-detected "
                   "when stdout is not a TTY.")
@click.option("--set-key", "set_key_vendor", default=None,
              help="Store a BYO API key for VENDOR (openai|anthropic|gemini). The "
                   "key is read from stdin (or the SCREENCAP_BYO_KEY_FILE file) — "
                   "NEVER from an argument. Add --validate to check it first.")
@click.option("--clear-key", "clear_key_vendor", default=None,
              help="Remove the stored BYO API key for VENDOR.")
@click.option("--validate", "validate_key", is_flag=True,
              help="With --set-key: validate the key against the vendor before "
                   "storing (and report valid/invalid/unknown).")
def settings_intelligence(row, op, value, as_json, set_key_vendor, clear_key_vendor, validate_key):
    """Show or change the Intelligence provider + per-task cloud consent.

    \b
    Read the current settings (Swift reads this back):
      screencap settings intelligence --json

    \b
    Select the active provider (on-device | gemini):
      screencap settings intelligence provider set on-device

    \b
    Configure the cloud fallback provider (gemini) or clear it:
      screencap settings intelligence cloud_provider set gemini
      screencap settings intelligence cloud_provider set none

    \b
    Enable/disable a cloud-consent row (summary/title or recall-answer only):
      screencap settings intelligence summary_cloud_consent set true
      screencap settings intelligence recall_cloud_consent set false

    \b
    Store / clear a BYO API key (the key is read from STDIN, never argv):
      printf %s "$KEY" | screencap settings intelligence --set-key openai --validate
      screencap settings intelligence --clear-key openai

    Writes land in the ``[intelligence]`` section of config.toml through the
    same advisory-flock + tomlkit path as ``settings privacy``. Defense in
    depth over the U6 consent policy: the day-split/label row and the
    frames/images row are **never** settable to cloud/on (R7/R9); only
    summary/title and recall-answer rows may be consented to cloud (R8/R10).
    Unknown provider values are rejected. Errors exit non-zero.
    """
    import json as _json

    from screencap import config

    err_console = Console(stderr=True)

    # --- BYO API-key management (secret over stdin/file, never argv) ---------
    if set_key_vendor is not None or clear_key_vendor is not None:
        _handle_byo_key(
            set_key_vendor, clear_key_vendor, validate_key, as_json, err_console
        )
        return

    # --- Read-back: no positional args → emit the current settings ----------
    if row is None:
        payload = _build_intelligence_settings_block()
        if as_json:
            sys.stdout.write(_json.dumps({
                "ok": True,
                "schema_version": _SETTINGS_INTELLIGENCE_SCHEMA_VERSION,
                "intelligence": payload,
            }) + "\n")
            sys.stdout.flush()
            return
        console.print("\n[bold]Intelligence Settings[/bold]\n")
        console.print(f"  Provider:              {payload['provider']}")
        console.print(f"  Cloud provider:        {payload['cloud_provider'] or 'none'}")
        console.print(f"  Summaries & titles:    {'cloud-consented' if payload['summary_cloud_consent'] else 'on-device only'}")
        console.print(f"  Recall answers:        {'cloud-consented' if payload['recall_cloud_consent'] else 'on-device only'}")
        console.print("  Day-splitting/labels:  on-device only [dim](fixed)[/dim]")
        console.print("  Screen frames/images:  never sent to cloud [dim](fixed)[/dim]")
        console.print()
        return

    # --- Write path: <row> set <value> --------------------------------------
    row = row.strip()

    def _emit_error(error: str) -> None:
        """Print a rich error (stderr / JSON) and exit non-zero."""
        if as_json:
            sys.stdout.write(_json.dumps({
                "ok": False,
                "schema_version": _SETTINGS_INTELLIGENCE_SCHEMA_VERSION,
                "row": row,
                "op": op,
                "value": value,
                "error": error,
            }) + "\n")
            sys.stdout.flush()
        raise SystemExit(1)

    if op is None or value is None:
        err_console.print(
            "[red]Error:[/red] Setting a row requires ROW set VALUE, "
            "e.g. [bold]settings intelligence provider set on-device[/bold]."
        )
        _emit_error("missing_op_or_value")

    value = value.strip()

    # The forbidden cloud rows: day-split/label and frames. Rejected before
    # anything is written — defense in depth over U6's policy (R7/R9).
    _FORBIDDEN_CLOUD_ROWS = ("day_split_cloud_consent", "frames_cloud_consent")

    if row == "provider":
        if value not in config._VALID_LLM_PROVIDERS:
            err_console.print(
                f"[red]Error:[/red] provider must be one of "
                f"{config._VALID_LLM_PROVIDERS}, got: {escape(str(value))}"
            )
            _emit_error(f"invalid_provider:{value}")
        # SCR-239: a local-server active provider must point at a LOCAL endpoint —
        # a REMOTE endpoint is treated as cloud and can never be the day-split
        # provider (R5). Defense in depth over the U8 routing.
        if value == "local-server":
            from screencap.segmentation.endpoint import LOCAL, classify_endpoint

            endpoint = config.get_local_server_endpoint()
            if not endpoint:
                err_console.print(
                    "[red]Error:[/red] provider 'local-server' requires an endpoint; "
                    "set it first: [bold]settings intelligence local_server_endpoint "
                    "set http://127.0.0.1:11434[/bold]"
                )
                _emit_error("local_server_requires_endpoint")
            if classify_endpoint(endpoint) != LOCAL:
                err_console.print(
                    "[red]Error:[/red] the configured endpoint is remote — a remote "
                    "server is treated as cloud and cannot be the active day-split "
                    "provider (R5). Point at a loopback server (127.0.0.1 / localhost)."
                )
                _emit_error("remote_endpoint_not_day_split_provider")
        config.set_intelligence_provider(value)
        err_console.print(f"  [bold]intelligence.provider[/bold] set {escape(str(value))}")

    elif row == "local_server_endpoint":
        from screencap.segmentation.endpoint import LOCAL, classify_endpoint

        if value.lower() in ("none", ""):
            config.set_intelligence_endpoint(None)
            err_console.print("  [bold]intelligence.local_server_endpoint[/bold] cleared")
            value = None
        else:
            from urllib.parse import urlparse

            if urlparse(value).scheme not in ("http", "https"):
                err_console.print(
                    f"[red]Error:[/red] endpoint must be an http(s) URL, got: "
                    f"{escape(_redact_url(value) or str(value))}"
                )
                _emit_error("invalid_endpoint_scheme")
            config.set_intelligence_endpoint(value)
            cls = classify_endpoint(value)
            redacted = _redact_url(value)
            note = (
                "local — day-split on-device" if cls == LOCAL
                else "remote — treated as cloud (day-split off)"
            )
            err_console.print(
                f"  [bold]intelligence.local_server_endpoint[/bold] set "
                f"{escape(str(redacted))} [dim]({note})[/dim]"
            )
            value = redacted  # echo the REDACTED url (never the token)

    elif row == "cloud_provider":
        # ``none`` / empty clears the configured cloud backend.
        if value.lower() in ("none", ""):
            config.set_intelligence_cloud_provider(None)
            err_console.print("  [bold]intelligence.cloud_provider[/bold] cleared")
            value = None  # normalize for the JSON echo below
        elif value not in config._VALID_CLOUD_PROVIDERS:
            err_console.print(
                f"[red]Error:[/red] cloud_provider must be one of "
                f"{config._VALID_CLOUD_PROVIDERS} (or 'none'), got: "
                f"{escape(str(value))}"
            )
            _emit_error(f"invalid_cloud_provider:{value}")
        else:
            config.set_intelligence_cloud_provider(value)
            err_console.print(f"  [bold]intelligence.cloud_provider[/bold] set {escape(str(value))}")

    elif row in _FORBIDDEN_CLOUD_ROWS:
        # R7 (day-split/label) / R9 (frames) — on-device / never-cloud, fixed.
        err_console.print(
            f"[red]Error:[/red] {escape(str(row))} cannot be consented to "
            "cloud — day-splitting/labeling stays on-device and screen "
            "frames/images are never sent to any cloud provider."
        )
        _emit_error(f"row_never_cloud:{row}")

    elif row in config._CLOUD_CONSENT_ROWS:
        parsed = _parse_intelligence_consent_bool(value)
        if parsed is None:
            err_console.print(
                f"[red]Error:[/red] {escape(str(row))} must be true or false, "
                f"got: {escape(str(value))}"
            )
            _emit_error(f"invalid_bool:{row}={value}")
        config.set_intelligence_consent(row, parsed)
        err_console.print(f"  [bold]intelligence.{escape(str(row))}[/bold] set {parsed}")
        value = parsed  # echo the normalized bool

    else:
        _known = (
            "provider", "cloud_provider", "local_server_endpoint",
            *config._CLOUD_CONSENT_ROWS,
        )
        err_console.print(f"[red]Error:[/red] Unknown intelligence row: {escape(str(row))}")
        err_console.print(f"[dim]Settable: {', '.join(_known)}[/dim]")
        _emit_error(f"unknown_row:{row}")

    if as_json:
        sys.stdout.write(_json.dumps({
            "ok": True,
            "schema_version": _SETTINGS_INTELLIGENCE_SCHEMA_VERSION,
            "row": row,
            "op": op,
            "value": value,
            "error": None,
        }) + "\n")
        sys.stdout.flush()


def _parse_intelligence_consent_bool(value: str) -> bool | None:
    """Parse a truthy/falsey consent value; ``None`` on unrecognized input."""
    low = value.lower()
    if low in ("true", "1", "yes", "on"):
        return True
    if low in ("false", "0", "no", "off"):
        return False
    return None


def _handle_byo_key(set_vendor, clear_vendor, validate, as_json, err_console) -> None:
    """Store / clear a per-vendor BYO API key (U2).

    The secret reaches this process over **stdin** (or the 0o600
    ``SCREENCAP_BYO_KEY_FILE`` file) and is stored in the shared Keychain under a
    per-vendor service name — it is **never** an argv value (KTD3) and is never
    echoed back (the JSON result carries only a presence flag / validation
    status, never the key). Exits non-zero on any error.
    """
    import json as _json

    from screencap.segmentation import secrets as byo_secrets

    def _emit(ok: bool, **fields) -> None:
        if as_json:
            sys.stdout.write(_json.dumps({
                "ok": ok,
                "schema_version": _SETTINGS_INTELLIGENCE_SCHEMA_VERSION,
                **fields,
            }) + "\n")
            sys.stdout.flush()
        if not ok:
            raise SystemExit(1)

    if set_vendor is not None and clear_vendor is not None:
        err_console.print("[red]Error:[/red] pass only one of --set-key / --clear-key.")
        _emit(False, error="set_and_clear_conflict")
        return

    vendor = (set_vendor or clear_vendor).strip()
    if vendor not in byo_secrets.KEY_VENDORS:
        err_console.print(
            f"[red]Error:[/red] BYO API keys are only stored for "
            f"{byo_secrets.KEY_VENDORS}; the *-cli providers hold no key. "
            f"Got: {escape(str(vendor))}"
        )
        _emit(False, vendor=vendor, error=f"unknown_key_vendor:{vendor}")
        return

    # --- clear ---------------------------------------------------------------
    if clear_vendor is not None:
        try:
            byo_secrets.delete_key(vendor)
        except Exception as exc:  # noqa: BLE001 — surface a clean error, never the key
            err_console.print(
                f"[red]Error:[/red] couldn't clear the {escape(vendor)} key "
                f"({escape(type(exc).__name__)})."
            )
            _emit(False, vendor=vendor, error="key_clear_failed")
            return
        err_console.print(f"  [bold]intelligence.{escape(vendor)}[/bold] key cleared")
        _emit(True, vendor=vendor, key_present=False, cleared=True)
        return

    # --- set (read secret from stdin/file, optionally validate) --------------
    key = _read_byo_key_from_stdin_or_file()
    if not key:
        err_console.print(
            "[red]Error:[/red] no API key supplied — pipe it on stdin "
            "(printf %s \"$KEY\" | ... --set-key VENDOR) or set "
            f"{_BYO_KEY_FILE_ENV} to a 0o600 file path."
        )
        _emit(False, vendor=vendor, error="no_key_supplied")
        return

    validation = None
    if validate:
        validation = byo_secrets.validate_key(vendor, key)
        if validation == byo_secrets.INVALID:
            err_console.print(
                f"[red]Error:[/red] the {escape(vendor)} key was rejected by the "
                "vendor (invalid). Not stored."
            )
            _emit(False, vendor=vendor, validation=validation, error="key_invalid")
            return
        # VALID or UNKNOWN (couldn't reach vendor) → store it; the UI surfaces the
        # unknown state so the user knows it wasn't confirmed.

    try:
        byo_secrets.store_key(vendor, key)
    except Exception as exc:  # noqa: BLE001 — surface a clean error, never the key
        err_console.print(
            f"[red]Error:[/red] couldn't store the {escape(vendor)} key "
            f"({escape(type(exc).__name__)})."
        )
        _emit(False, vendor=vendor, validation=validation, error="key_store_failed")
        return

    note = f" [dim](validation: {validation})[/dim]" if validation else ""
    err_console.print(f"  [bold]intelligence.{escape(vendor)}[/bold] key stored{note}")
    _emit(True, vendor=vendor, key_present=True, validation=validation)


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
        from presidio_analyzer.nlp_engine import NlpArtifacts, NlpEngine

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
        from screencap.redaction.secrets import DetectSecretsDetector
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


def _check_av_review_pipeline() -> tuple[str, bool, str]:
    """Run the review concat + yuv420p remediation on a tiny in-memory fixture.

    Goes beyond ``_check_av_codecs`` (which only loads libx264): proves the
    bundled wheel can actually *run* the native-review video pipeline end to
    end — chunk concat (remux) and the yuv444p→yuv420p re-encode — so a frozen
    binary that loads but cannot mux/encode is caught at smoke time.
    "Test what you bundle."
    """
    import traceback as _tb
    name = "av_review_pipeline"
    try:
        import shutil
        import tempfile
        from pathlib import Path

        import av
        from PIL import Image

        from screencap.engine.video import (
            VideoWriter,
            concat_video_chunks,
            read_pixel_format,
            remediate_pixfmt_for_review,
        )

        tmp = Path(tempfile.mkdtemp(prefix="screencap_smoke_review_"))
        try:
            # Two tiny yuv444p chunks, mirroring a chunked recording.
            for idx, color in enumerate([(200, 0, 0), (0, 0, 200)]):
                writer = VideoWriter(
                    str(tmp / f"chunk_{idx:04d}.mp4"), width=64, height=64, fps=24
                )
                for i in range(4):
                    writer.write_frame(Image.new("RGB", (64, 64), color=color), i / 24)
                writer.close()

            concat_video_chunks(tmp)  # chunks → video.mp4 (side effect)
            review_path, remediated = remediate_pixfmt_for_review(tmp)
            if not remediated:
                return name, False, "expected yuv444p source to be remediated"
            review_pix_fmt = read_pixel_format(review_path)
            if review_pix_fmt != "yuv420p":
                return name, False, f"review pix_fmt not yuv420p: {review_pix_fmt}"
            container = av.open(str(review_path))
            try:
                frames = sum(1 for _ in container.decode(video=0))
            finally:
                container.close()
            if frames <= 0:
                return name, False, "remediated review video has no decodable frames"
            return name, True, ""
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
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


def _check_daemon_load() -> tuple[str, bool, str]:
    """Construct the daemon ASGI app and verify required routes register."""
    name = "daemon_load"
    try:
        from screencap.daemon.app import build_app

        app = build_app()
        if not app.routes:
            return name, False, "app constructed but has no routes"
        paths = {getattr(route, "path", None) for route in app.routes}
        required = {
            "/v0/daemon.info",
            "/v0/recording.list",
            "/v0/session.snapshot",
            "/v0/events",
            "/v0/recording.start",
            "/v0/recording.stop",
        }
        missing = required - paths
        if missing:
            return name, False, f"missing routes: {sorted(missing)}"
        return name, True, ""
    except BaseException:
        import traceback as _tb

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
            console.print(f"[red]Failed to persist[/red] {escape(str(host_lc))}")
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
                console.print(f"  • {escape(str(name))}")
        if unreadable_recordings:
            console.print(
                f"[red]Refusing to delete KEK:[/red] "
                f"{len(unreadable_recordings)} recording(s) could not be "
                f"scanned for encrypted bodies and may still need this KEK:"
            )
            for entry in unreadable_recordings:
                console.print(f"  • {escape(str(entry))}")
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
                console.print(f"  • {escape(str(entry))}")

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
            console.print(f"[red]Failed to delete KEK:[/red] {escape(str(exc))}")
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


# Two privacy-related checks live in privacy_settings.py (SCR-156); the registry
# stays here so the `screencap.cli._SMOKE_CHECKS` patch path used by the
# smoke-test cases keeps working. privacy_settings defers all heavy imports, so
# this module-level reference does not regress `screencap --help`.
from screencap.privacy_settings import (  # noqa: E402
    _check_domain_index,
    _check_onnxruntime_excluded,
)

_SMOKE_CHECKS = [
    _check_presidio_analyzer,
    _check_fast_gliner,
    _check_detect_secrets_plugins,
    _check_spacy_model,
    _check_av_codecs,
    _check_av_review_pipeline,
    _check_pynput,
    _check_sounddevice,
    _check_domain_index,
    _check_onnxruntime_excluded,
    _check_keyring_macos_backend,
    _check_daemon_load,
]


@cli.group("backfill")
def backfill_group() -> None:
    """Index on-screen text from your existing recordings (SCR-178).

    Thin one-shot HTTP clients of the daemon's ``backfill.*`` verbs over
    the UNIX socket. The backfill OCR-indexes recordings made *before*
    on-screen-text indexing was enabled, honoring the same privacy skips
    as live indexing. It is local-only, cancellable, and resumable.

    The consent-flow UI (in the macOS app) is the supported live-progress
    surface; this CLI ships ``start`` / ``status`` / ``cancel`` for
    headless use and scripting (poll ``status --json`` for progress).
    """


def _backfill_snapshot_line(snapshot: dict) -> str:
    """Render a one-line human summary of a backfill status snapshot."""
    state = str(snapshot.get("state", "unknown"))
    done = snapshot.get("done", 0)
    total = snapshot.get("total", 0)
    skipped = snapshot.get("skipped", 0)
    failed = snapshot.get("failed", 0)
    return (
        f"Backfill {escape(state)} — {done}/{total} indexed "
        f"({skipped} skipped, {failed} failed)"
    )


def _backfill_client_or_exit(*, auto_spawn: bool = True) -> "DaemonHTTPClient":
    """Auto-spawn (F3) then return an open ``DaemonHTTPClient``, or exit.

    Mirrors the live-state commands: reuse ``ensure_daemon_or_spawn`` so a
    headless install works exactly like ``screencap status``, and surface
    the LaunchAgent-not-running kickstart hint as a non-zero exit.
    """
    from screencap.cli._autospawn import (
        DaemonAutoSpawnError,
        LaunchAgentNotRunningError,
        ensure_daemon_or_spawn,
    )
    from screencap.cli._daemon_client import DaemonHTTPClient

    try:
        ensure_daemon_or_spawn(auto_spawn=auto_spawn)
    except LaunchAgentNotRunningError as exc:
        console.print(f"[red]Error:[/red] {escape(str(exc))}")
        raise SystemExit(1) from exc
    except DaemonAutoSpawnError as exc:
        console.print(f"[red]Error:[/red] {escape(str(exc))}")
        if exc.log_tail:
            console.print(escape(exc.log_tail))
        raise SystemExit(1) from exc

    return DaemonHTTPClient()


def _backfill_call_or_exit(call) -> dict[str, Any]:
    """Run a daemon verb, translating transport/schema/API failures to exits.

    Mirrors the live-state commands' daemon-down UX: a transport failure
    prints the standard "daemon not reachable" message and exits non-zero.
    """
    from screencap.cli._daemon_client import (
        DaemonAPIError,
        DaemonUnreachableError,
        SchemaMismatchError,
    )

    try:
        return call()
    except DaemonUnreachableError as exc:
        console.print(
            f"[red]Error:[/red] could not reach the ScreenCap daemon: "
            f"{escape(str(exc))}"
        )
        raise SystemExit(1) from exc
    except SchemaMismatchError as exc:
        console.print(f"[red]Error:[/red] {escape(str(exc))}")
        console.print(
            "Update the daemon: [bold]launchctl kickstart -kp "
            "gui/$UID/com.screencap.daemon[/bold]"
        )
        raise SystemExit(1) from exc
    except DaemonAPIError as exc:
        code = exc.envelope.get("error", "unknown")
        console.print(f"[red]Daemon error:[/red] {escape(str(code))}")
        raise SystemExit(1) from exc


@backfill_group.command("start")
def backfill_start_cmd() -> None:
    """Start (or resume) indexing your existing recordings.

    Thin client of ``POST /v0/backfill.start``. Idempotent on the daemon
    side — starting while a run is already in flight reports the existing
    job's state instead of launching a second run, and a previously
    paused/cancelled run resumes from where it left off.
    """
    client = _backfill_client_or_exit()
    with client:
        snapshot = _backfill_call_or_exit(client.backfill_start)

    state = str(snapshot.get("state", "unknown"))
    if state == "running":
        if snapshot.get("done") or snapshot.get("skipped") or snapshot.get("failed"):
            console.print("[#22d3ee]Backfill resumed.[/#22d3ee]")
        else:
            console.print("[#22d3ee]Backfill started.[/#22d3ee]")
    elif state == "completed":
        console.print("[green]Backfill already complete — nothing to index.[/green]")
    else:
        console.print(f"[#22d3ee]Backfill {escape(state)}.[/#22d3ee]")
    console.print(f"  {_backfill_snapshot_line(snapshot)}")


@backfill_group.command("status")
@click.option("--json", "as_json", is_flag=True,
              default=lambda: _should_default_to_json(),
              help="Emit the raw status payload as JSON to stdout (no styling). "
                   "Auto-detected when stdout is not a TTY.")
def backfill_status_cmd(as_json: bool) -> None:
    """Report the current backfill state and progress.

    Thin client of ``GET /v0/backfill.status``. Default prints a
    human-readable line; ``--json`` emits the raw privacy-safe status
    payload (state + done/skipped/failed/total/current_unit_index) for
    scripting / polling.
    """
    # ``auto_spawn=False``: querying status of a missing daemon should not
    # spin one up just to report "idle" — mirror ``screencap status``.
    client = _backfill_client_or_exit(auto_spawn=False)
    with client:
        snapshot = _backfill_call_or_exit(client.backfill_status)

    if as_json:
        payload = {
            "ok": True,
            "schema_version": _BACKFILL_SCHEMA_VERSION,
            "state": snapshot.get("state"),
            "done": snapshot.get("done"),
            "skipped": snapshot.get("skipped"),
            "failed": snapshot.get("failed"),
            "total": snapshot.get("total"),
            "current_unit_index": snapshot.get("current_unit_index"),
        }
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()
        return

    console.print(_backfill_snapshot_line(snapshot))


@backfill_group.command("cancel")
def backfill_cancel_cmd() -> None:
    """Cancel the in-flight backfill (resumable later).

    Thin client of ``POST /v0/backfill.cancel``. The daemon signals the
    run to stop; partial progress is preserved on disk so a later
    ``backfill start`` resumes from where it stopped. A no-op when no run
    is in flight.
    """
    client = _backfill_client_or_exit()
    with client:
        snapshot = _backfill_call_or_exit(client.backfill_cancel)

    state = str(snapshot.get("state", "unknown"))
    if state == "cancelled":
        console.print("[#22d3ee]Backfill cancelled.[/#22d3ee] You can resume it later.")
    else:
        console.print(f"[#22d3ee]Backfill {escape(state)}.[/#22d3ee]")
    console.print(f"  {_backfill_snapshot_line(snapshot)}")


@cli.group("search")
def search_group() -> None:
    """On-device recording search (search-by-default guardrails).

    ``enable`` acknowledges the disclosure and turns search on safely: it encrypts
    the existing recall corpus (stills + content index) and flips the default so new
    recordings are captured, secrets-scrubbed, retention-bounded, and searchable.
    ``status`` prints the current gate state. Headless companion to the macOS app's
    onboarding disclosure step.
    """


@search_group.command("enable")
def search_enable_cmd() -> None:
    """Acknowledge the disclosure and enable encrypted on-device search."""
    from rich.console import Console

    from screencap import config, corpus_migrate

    console = Console()
    console.print("Enabling on-device search — encrypting your existing recordings...")
    config.set_search_disclosure_acknowledged(True)
    try:
        report = corpus_migrate.flip_corpus_to_encrypted()
    except Exception as exc:  # noqa: BLE001 — surface the failure with a clean exit
        console.print(f"[red]Could not enable search: {escape(str(exc))}[/red]")
        raise SystemExit(1)
    console.print(
        f"[green]Search enabled.[/green] Encrypted {report.stills_encrypted} still(s) "
        f"across {report.recordings_scanned} recording(s); index rekeyed: "
        f"{report.index_rekeyed}. New recordings are searchable and secrets-scrubbed."
    )


@search_group.command("status")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def search_status_cmd(as_json: bool) -> None:
    """Show the search-by-default gate state."""
    import json as _json

    from rich.console import Console

    from screencap import config

    state = {
        "corpus_encrypted": config.get_corpus_encrypted(),
        "disclosure_acknowledged": config.get_search_disclosure_acknowledged(),
        "consent_declined": config.get_content_index_consent_declined(),
        "content_index_enabled": config.get_content_index_enabled(),
        "screenshot_retention_days": config.get_screenshot_retention_days(),
    }
    console = Console()
    if as_json:
        console.print_json(_json.dumps(state))
        return
    for key, value in state.items():
        console.print(f"{key}: {value}")


@cli.group("e2ee")
def e2ee_group() -> None:
    """End-to-end encryption for cloud copies (beta, SCR-220).

    ``enable`` creates the device-held encryption key first and only then
    flips ``cloud_e2ee_enabled`` on — a key failure leaves the flag untouched
    so the account is never half-configured (flag on, no key, every upload
    failing closed). ``disable`` writes an explicit ``false`` and keeps the
    key so re-enabling never mints a second key (prior ciphertext stays
    readable). ``status`` is read-only. Headless companion to the macOS
    Privacy-settings toggle, which shells out to these verbs.
    """


def _set_cloud_e2ee_flag(value: bool) -> None:
    """Persist ``cloud_e2ee_enabled`` through the same write machinery as
    ``settings --set`` (atomic tomlkit write + in-process cache invalidation).

    Always writes the literal value — ``disable`` must land an explicit
    ``false`` in config.toml, never unset the key: an unset value would be
    silently re-enrolled by a future default flip (Stage 3, U9).
    """
    from screencap.config import _CONFIG_PATH, invalidate_config_cache
    from screencap.setup_wizard import _load_config_toml, _save_config_atomic

    doc = _load_config_toml(_CONFIG_PATH)
    doc["cloud_e2ee_enabled"] = value
    _save_config_atomic(_CONFIG_PATH, doc)
    invalidate_config_cache()


def _read_cloud_kek_id() -> str | None:
    """Hex key id of the stored cloud KEK, or ``None`` (absent or unreadable).

    Read-only — must never create a key (``status``/``disable`` paths)."""
    from screencap import cloud_crypto

    try:
        key = cloud_crypto.get_cloud_kek()
    except Exception:  # noqa: BLE001 — KeyringError etc.: report as absent
        return None
    if key is None:
        return None
    return cloud_crypto.cloud_key_id(key).hex()


@e2ee_group.command("enable")
@click.option("--json", "as_json", is_flag=True,
              default=lambda: _should_default_to_json(),
              help="Emit machine-readable JSON. Auto-detected when stdout "
                   "is not a TTY.")
def e2ee_enable_cmd(as_json: bool) -> None:
    """Create the encryption key (if needed) and turn on E2EE for cloud copies.

    Key first, flag second: the one-time Keychain prompt may appear here (this
    is a foreground process; the daemon can only read the key, never create
    it). If key creation fails the flag is left untouched and the command
    exits non-zero.
    """
    from screencap import cloud_crypto

    try:
        key = cloud_crypto.get_or_create_cloud_kek()
    except Exception as exc:  # noqa: BLE001 — KeyringError etc.
        if as_json:
            click.echo(json.dumps({
                "error": f"{type(exc).__name__}: {exc}",
                "enabled": False,
                "key_present": False,
            }))
        else:
            console.print(
                f"[red]Could not create the encryption key:[/red] {escape(str(exc))}"
            )
            console.print("[dim]E2EE was NOT enabled; settings are unchanged.[/dim]")
        raise SystemExit(1)

    _set_cloud_e2ee_flag(True)
    key_id = cloud_crypto.cloud_key_id(key).hex()
    if as_json:
        click.echo(json.dumps(
            {"enabled": True, "key_present": True, "key_id": key_id}
        ))
        return
    console.print("[green]E2EE enabled[/green] for cloud copies (beta).")
    console.print(f"  Key id: [bold]{key_id}[/bold] (held only in this Mac's Keychain)")


@e2ee_group.command("disable")
@click.option("--json", "as_json", is_flag=True,
              default=lambda: _should_default_to_json(),
              help="Emit machine-readable JSON. Auto-detected when stdout "
                   "is not a TTY.")
def e2ee_disable_cmd(as_json: bool) -> None:
    """Turn off E2EE for new cloud copies. The key is kept.

    Writes an explicit ``cloud_e2ee_enabled = false`` (never unsets it) and
    leaves the Keychain key in place so existing encrypted recordings stay
    readable and re-enabling reuses the same key.
    """
    _set_cloud_e2ee_flag(False)
    key_id = _read_cloud_kek_id()
    if as_json:
        click.echo(json.dumps(
            {"enabled": False, "key_present": key_id is not None, "key_id": key_id}
        ))
        return
    console.print("[yellow]E2EE disabled[/yellow] for new cloud copies.")
    if key_id is not None:
        console.print(f"  Key kept (id [bold]{key_id}[/bold]) — re-enabling reuses it.")


@e2ee_group.command("status")
@click.option("--json", "as_json", is_flag=True,
              default=lambda: _should_default_to_json(),
              help="Emit machine-readable JSON. Auto-detected when stdout "
                   "is not a TTY.")
def e2ee_status_cmd(as_json: bool) -> None:
    """Show the E2EE flag, key presence, and key id. Never creates a key."""
    from screencap.config import get_cloud_e2ee_enabled

    enabled = bool(get_cloud_e2ee_enabled())
    key_id = _read_cloud_kek_id()
    if as_json:
        click.echo(json.dumps(
            {"enabled": enabled, "key_present": key_id is not None, "key_id": key_id}
        ))
        return
    console.print(f"  E2EE for cloud copies: {'enabled' if enabled else 'disabled'}")
    console.print(f"  Encryption key:        {'present (id ' + key_id + ')' if key_id else 'none'}")


@cli.group("storage")
def storage_group() -> None:
    """Manage where recordings are stored (SCR-228).

    Thin one-shot HTTP client of the daemon's ``storage.migrate`` verb over
    the UNIX socket. The macOS Privacy pane is the supported UI; this CLI
    ships ``migrate`` for headless use and scripting.
    """


@storage_group.command("migrate")
@click.argument("path", type=click.Path())
@click.option(
    "--json", "as_json", is_flag=True,
    default=lambda: _should_default_to_json(),
    help="Emit the raw result payload as JSON (no styling). Auto-detected "
         "when stdout is not a TTY.",
)
def storage_migrate_cmd(path: str, as_json: bool) -> None:
    """Move your recordings library to a new folder on the SAME disk.

    Thin client of ``POST /v0/storage.migrate``. The move is atomic and
    near-instant (a same-volume rename). Cross-volume / external-drive
    targets are refused for now. Recording must not be in progress.
    """
    import json as _json
    import os

    from screencap.cli._autospawn import (
        DaemonAutoSpawnError,
        LaunchAgentNotRunningError,
        ensure_daemon_or_spawn,
    )
    from screencap.cli._daemon_client import (
        DaemonClientError,
        DaemonHTTPClient,
        DaemonUnreachableError,
        SchemaMismatchError,
    )

    target = os.path.abspath(os.path.expanduser(path))

    try:
        ensure_daemon_or_spawn(auto_spawn=True)
    except (LaunchAgentNotRunningError, DaemonAutoSpawnError) as exc:
        console.print(f"[red]Error:[/red] {escape(str(exc))}")
        if isinstance(exc, DaemonAutoSpawnError) and exc.log_tail:
            console.print(escape(exc.log_tail))
        raise SystemExit(1) from exc

    with DaemonHTTPClient() as client:
        try:
            payload = client.storage_migrate(target)
        except DaemonUnreachableError as exc:
            console.print(
                f"[red]Error:[/red] could not reach the ScreenCap daemon: "
                f"{escape(str(exc))}"
            )
            raise SystemExit(1) from exc
        except SchemaMismatchError as exc:
            console.print(f"[red]Error:[/red] {escape(str(exc))}")
            raise SystemExit(1) from exc
        except DaemonClientError as exc:
            # storage_migration_failed carries a human `message` + `reason`.
            env = exc.envelope
            if as_json:
                click.echo(_json.dumps({
                    "ok": False,
                    "error": env.get("error"),
                    "reason": env.get("reason"),
                    "message": env.get("message"),
                }))
            else:
                msg = env.get("message") or env.get("error", "migration failed")
                console.print(
                    f"[red]Couldn't move recordings:[/red] {escape(str(msg))}"
                )
            raise SystemExit(1) from exc

    if as_json:
        click.echo(_json.dumps(payload))
    else:
        console.print(
            f"[green]Recordings moved to[/green] "
            f"{escape(str(payload.get('moved_to', target)))}"
        )


@cli.group("model")
def model_group() -> None:
    """Download and manage the optional local Intelligence model (SCR-239).

    Thin one-shot HTTP clients of the daemon's ``model.download.*`` /
    ``model.status`` verbs over the UNIX socket. The download is opt-in, size-
    disclosed, and integrity-verified. The macOS app's Intelligence settings pane
    is the supported live-progress surface; this CLI ships ``download`` /
    ``status`` / ``cancel`` for headless use (poll ``status --json`` for progress).
    """


def _model_download_line(snapshot: dict) -> str:
    state = str(snapshot.get("state", "unknown"))
    done = snapshot.get("bytes_done", 0) or 0
    total = snapshot.get("bytes_total", 0) or 0
    pct = f" — {100 * done // total}%" if total else ""
    reason = snapshot.get("reason")
    tail = f" ({escape(str(reason))})" if reason and state == "failed" else ""
    return f"Model download {escape(state)}{pct}{tail}"


@model_group.command("download")
@click.argument("model_id", required=False)
def model_download_cmd(model_id: str | None) -> None:
    """Download the local model (opt-in). Idempotent on the daemon side."""
    client = _backfill_client_or_exit()
    with client:
        snapshot = _model_call_or_exit(lambda: client.model_download_start(model_id))
    state = str(snapshot.get("state", "unknown"))
    if state == "installed":
        console.print("[green]Model already installed.[/green]")
    elif state == "downloading":
        console.print("[#22d3ee]Model download started.[/#22d3ee]")
    elif state == "failed":
        console.print(f"[red]Model download failed:[/red] {escape(str(snapshot.get('reason')))}")
        raise SystemExit(1)
    else:
        console.print(f"[#22d3ee]Model download {escape(state)}.[/#22d3ee]")
    console.print(f"  {_model_download_line(snapshot)}")


@model_group.command("status")
@click.option("--json", "as_json", is_flag=True,
              default=lambda: _should_default_to_json(),
              help="Emit the raw status payload as JSON.")
def model_status_cmd(as_json: bool) -> None:
    """Report the download state + which models are installed."""
    client = _backfill_client_or_exit(auto_spawn=False)
    with client:
        dl = _model_call_or_exit(client.model_download_status)
        installed = _model_call_or_exit(client.model_status)
    if as_json:
        console.print_json(data={"download": dl, "installed": installed})
        return
    console.print(f"  {_model_download_line(dl)}")
    for m in installed.get("models", []):
        mark = "installed" if m.get("installed") else "not installed"
        gb = (m.get("size_bytes", 0) or 0) / (1024**3)
        console.print(f"  {escape(str(m.get('model_id')))}: {mark} (~{gb:.1f} GB)")


@model_group.command("cancel")
def model_cancel_cmd() -> None:
    """Cancel the in-flight model download."""
    client = _backfill_client_or_exit(auto_spawn=False)
    with client:
        snapshot = _model_call_or_exit(client.model_download_cancel)
    console.print(f"  {_model_download_line(snapshot)}")


def _model_call_or_exit(call):
    """Reuse the backfill daemon-call error translation for model verbs."""
    return _backfill_call_or_exit(call)


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

        check_name, passed, err = result
        if passed:
            console.print(f"  [green]\\[PASS][/green] {check_name}")
        else:
            # err contains the full traceback; show last line for summary,
            # full traceback when --verbose
            err_summary = err.strip().rsplit("\n", 1)[-1]
            console.print(f"  [red]\\[FAIL][/red] {escape(str(check_name))} — {escape(str(err_summary))}")
            if verbose:
                console.print(escape(str(err)))

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
# _auth-config-check (hidden) — fail-closed release guard for credential injection
# ---------------------------------------------------------------------------


@cli.command("_auth-config-check", hidden=True)
def auth_config_check() -> None:
    """Fail a RELEASE build whose BUNDLED cloud-auth creds are still placeholders.

    The single fail-closed guard for build-time credential injection (U2). Run by
    the release job against the BUILT binary, after scripts/generate_provisioned.py:
    if the injected screencap._provisioned module is missing/empty, the bundled creds
    fall back to the REPLACE_WITH_PROVISIONED_* sentinels and every sign-in would
    fail — so such a binary must never ship.

    Checks ``auth.bundled_credentials()`` (``_provisioned`` > placeholder) and
    deliberately IGNORES the env-var layer: an end user has no env override, so a
    build-shell env var or a stray ``.env`` (read by ``load_dotenv`` at startup) must
    not be able to mask a ``_provisioned`` bundling failure. Enforcement is gated on
    the SCREENCAP_RELEASE_BUILD marker so PR/dev builds (no secrets) stay green with
    placeholders. The tag-triggered release workflow sets the marker unconditionally
    on every (release-only) run, making this assertion unskippable on a real release.
    """
    from screencap import auth

    api_key, client_id = auth.bundled_credentials()
    checked = (
        ("Firebase Web API key", api_key),
        ("OAuth client id", client_id),
    )
    placeholders = [
        label for label, value in checked if auth.is_placeholder_credential(value)
    ]
    release_build = os.environ.get("SCREENCAP_RELEASE_BUILD", "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
    )

    if not placeholders:
        console.print(
            "[green]auth-config-check:[/green] resolved cloud-auth credentials are provisioned."
        )
        return

    joined = ", ".join(placeholders)
    if release_build:
        console.print(
            f"[red]auth-config-check FAILED:[/red] release build resolved placeholder "
            f"credential(s): {joined}. Run scripts/generate_provisioned.py with "
            "SCREENCAP_OAUTH_CLIENT_ID + SCREENCAP_FIREBASE_API_KEY set before the build "
            "(see docs/runbooks/cloud-auth-setup.md)."
        )
        raise SystemExit(1)

    console.print(
        f"[yellow]auth-config-check:[/yellow] placeholder credential(s) present ({joined}) — "
        "OK for a dev/PR build (SCREENCAP_RELEASE_BUILD unset). A release build fails this check."
    )


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
        console.print(f"[red]Error:[/red] Recording not found: {escape(str(name))}")
        sys.exit(1)
    db_path = recording_dir / "recording.db"
    if not db_path.is_file():
        console.print(
            f"[red]Error:[/red] recording.db not found in {escape(str(recording_dir))}",
        )
        sys.exit(1)

    # Read-only URI mode keeps the helper safe alongside any concurrent
    # reader (V1 has no live writer at inspection time, but URI ro is
    # explicit about intent).
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError as exc:
        console.print(f"[red]Error:[/red] failed to open {escape(str(db_path))}: {escape(str(exc))}")
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
                                console.print(f"      {escape(str(entry[0]))}: {escape(str(entry[1]))}")
                            else:
                                console.print(f"      {escape(repr(entry))}")
                details_json = row["details_json"]
                if details_json:
                    try:
                        details = json.loads(details_json)
                    except (TypeError, ValueError):
                        details = details_json
                    console.print(f"    [dim]details:[/dim] {escape(repr(details))}")
    finally:
        conn.close()


if __name__ == "__main__":
    cli()
