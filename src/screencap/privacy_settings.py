"""Privacy/settings config mutation: TOML writers, first-run prompts, settings
rendering, matrix validation, and the ``settings privacy`` mutation engine.

Extracted from the CLI (SCR-156, continuing SCR-32) so the privacy-config
read/modify/write helpers and the matrix-invariant validation seam are
unit-testable without a CliRunner round trip. Heavy imports (tomlkit, fcntl,
screencap.config, screencap.setup_wizard, screencap.privacy.*) stay deferred
inside function bodies to keep importing this module — and ``screencap --help``
— cheap. The ``settings privacy`` Click command itself stays in the CLI and
imports these helpers locally at its call sites.
"""

from __future__ import annotations

import contextlib
import logging
import os

import click
from rich.console import Console

console = Console()
logger = logging.getLogger(__name__)

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


def _maybe_download_nlp_models() -> None:
    """Prompt to download GLiNER + spaCy models if not already cached."""
    from screencap.redaction import are_nlp_models_cached

    if are_nlp_models_cached():
        return  # fully cached

    console.print(
        "\n[bold]Privacy models not yet downloaded.[/bold] "
        "These are needed for scrubbing and cloud upload."
    )
    if click.confirm("Download now?", default=True):
        # The download wrapper is a CLI-layer testability shim with a second
        # caller in the cloud-gate flow, so it stays in cli/__init__.py; reach
        # it lazily to keep this module free of a module-level cli dependency.
        from screencap.cli import _download_nlp_models
        _download_nlp_models()
    else:
        console.print(
            "[dim]Skipped. Models will download on first scrub or upload.[/dim]"
        )


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
            )
            from screencap._stderr_events import (
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
