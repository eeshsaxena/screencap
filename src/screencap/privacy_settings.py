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
from typing import TYPE_CHECKING

import click
from rich.console import Console
from rich.markup import escape

if TYPE_CHECKING:
    from screencap.privacy.policy import PrivacyAction

console = Console()
logger = logging.getLogger(__name__)

_MATRIX_ACK_KEY = "matrix_acknowledged_v2026_04"

_PRIVACY_LIST_FIELDS = ("exclude_apps", "allow_apps", "mask_domains", "mask_title_patterns")
# `matrix_acknowledged_v2026_04` is NOT exposed here (todo 012) — it's an
# internal migration flag written by `_maybe_prompt_matrix_acknowledgement`
# and should not be flippable from a `screencap settings` invocation.
_PRIVACY_SCALAR_FIELDS = ("mode", "setup_skipped")
_PRIVACY_MAP_FIELDS = ("app_classes",)
# `shared` is reserved for MASK_REGION (not yet implemented); accepting it
# would write an unenforceable value that crashes the next start (todo 011).
_PRIVACY_MODE_VALUES = ("public", "internal")


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


def _build_privacy_settings_block() -> dict:
    """Return the `privacy` block for `settings --json` (SCR-17).

    Reads the raw `[privacy]` section from config.toml directly so the
    `has_privacy_section` flag can distinguish "never written" from
    "present with default values". The SwiftUI first-run banner gates its
    first-launch ``mode = internal`` write on this flag — using the parsed
    ``PrivacyConfig`` would conflate the two cases (defaults fill in for
    missing sections) and the banner would never write the fail-closed mode.

    Permissive on bad values: an invalid ``mode`` falls back to ``"internal"``
    rather than raising, mirroring ``PrivacyConfig``'s tolerance. The pane's
    job is to surface state, not to reject malformed configs at read time.
    """
    from screencap.config import _load_toml

    cfg = _load_toml()
    has_section = isinstance(cfg, dict) and "privacy" in cfg
    section = cfg.get("privacy") if has_section else None
    if not isinstance(section, dict):
        section = {}

    raw_mode = section.get("mode", "internal")
    mode = raw_mode.lower() if isinstance(raw_mode, str) else "internal"
    if mode not in _PRIVACY_MODE_VALUES:
        mode = "internal"

    raw_skipped = section.get("setup_skipped", False)
    setup_skipped = raw_skipped if isinstance(raw_skipped, bool) else False

    return {
        "mode": mode,
        "setup_skipped": setup_skipped,
        "has_privacy_section": has_section,
    }


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
        from screencap.privacy.classify import BROWSER_BUNDLE_IDS, BUNDLE_ID_MAP
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
                    f"[red]Error:[/red] '{escape(str(value))}' is in {effective_class.value} which the "
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
                f"[red]Error:[/red] '{escape(str(value))}' is not in BUNDLE_ID_MAP and has no "
                f"app_classes override — refusing to allow-list an unclassified "
                f"bundle. Run [bold]screencap settings privacy app_classes set "
                f"{escape(str(value))}=<class>[/bold] first (e.g., browser_unverified for an "
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
                err_console.print(f"[dim]{escape(str(field))} already contains {escape(str(value))} — no change.[/dim]")
                _result(True, changed=False)
                return False
            arr.append(value)
        else:  # remove
            if value not in arr:
                err_console.print(f"[dim]{escape(str(field))} does not contain {escape(str(value))} — no change.[/dim]")
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
                from screencap.privacy.classify import BUNDLE_ID_MAP as _BUNDLE_MAP
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
                            f"[red]Error:[/red] removing the '{escape(str(bundle))}' classification "
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
                    f"[red]Error:[/red] map field {escape(str(field))} requires BUNDLE_ID=CLASS for {escape(str(op))}, got: {escape(str(value))}"
                )
                _result(False, exit_code=1, error=f"map_value_missing_eq:{field}={value}")
            bundle, ctx_str = value.split("=", 1)
            bundle, ctx_str = bundle.strip(), ctx_str.strip()
            # Validate CLASS against ContextClass enum so we don't silently
            # corrupt config with a typo that crashes the next start
            # (todo 010). Accept upper/lower case input; normalize to value.
            from screencap.privacy.classify import BUNDLE_ID_MAP
            from screencap.privacy.policy import ContextClass
            valid_classes = {c.value for c in ContextClass}
            normalized = ctx_str.lower()
            if normalized not in valid_classes:
                err_console.print(
                    f"[red]Error:[/red] Unknown context class: {escape(str(ctx_str))}"
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
                        f"[red]Error:[/red] reclassifying '{escape(str(bundle))}' from "
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
