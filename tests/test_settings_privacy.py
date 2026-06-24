"""Tests for Unit 4b: ``screencap settings privacy <field> <op> <value>``.

Verifies the privacy-list mutation surface — add/remove for list fields,
set for scalar fields, idempotency, R16 round-trip preservation of mode,
and the matrix-EXCLUDE bypass guard.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from screencap.cli import cli


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    import screencap.config

    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr(screencap.config, "_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(screencap.config, "_DEFAULT_BASE", tmp_path)
    monkeypatch.setattr(screencap.config, "_config_cache", None)
    yield


def _read_cfg() -> dict:
    from screencap.config import _CONFIG_PATH

    if not _CONFIG_PATH.exists():
        return {}
    return tomllib.loads(_CONFIG_PATH.read_text())


def _write_config(path: Path, body: str) -> None:
    """Write `body` to `path`, creating parents as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def _invoke(*args, as_json=False):
    runner = CliRunner()
    full_args = ["settings", "privacy"]
    if as_json:
        full_args.append("--json")
    full_args.extend(args)
    return runner.invoke(cli, full_args, catch_exceptions=False)


# ---------------------------------------------------------------------------
# List fields
# ---------------------------------------------------------------------------


class TestListFields:
    def test_add_to_exclude_apps(self):
        result = _invoke("exclude_apps", "add", "com.example.foo")
        assert result.exit_code == 0
        cfg = _read_cfg()
        assert "com.example.foo" in cfg["privacy"]["exclude_apps"]

    def test_add_then_remove(self):
        _invoke("exclude_apps", "add", "com.example.foo")
        _invoke("exclude_apps", "remove", "com.example.foo")
        cfg = _read_cfg()
        assert cfg.get("privacy", {}).get("exclude_apps", []) == []

    def test_add_idempotent(self):
        _invoke("exclude_apps", "add", "com.example.foo")
        result = _invoke("exclude_apps", "add", "com.example.foo")
        assert result.exit_code == 0
        cfg = _read_cfg()
        assert cfg["privacy"]["exclude_apps"].count("com.example.foo") == 1

    def test_remove_absent_value_is_noop(self):
        result = _invoke("exclude_apps", "remove", "com.example.never-added")
        assert result.exit_code == 0

    def test_set_rejected_for_list_field(self):
        result = _invoke("exclude_apps", "set", "com.example.foo")
        assert result.exit_code != 0
        assert "use add/remove" in result.output

    def test_unknown_field_rejected(self):
        result = _invoke("not_a_field", "add", "x")
        assert result.exit_code != 0
        assert "Unknown privacy field" in result.output


class TestScalarFields:
    def test_set_mode(self):
        result = _invoke("mode", "set", "internal")
        assert result.exit_code == 0
        assert _read_cfg()["privacy"]["mode"] == "internal"

    def test_set_mode_normalizes_case(self):
        _invoke("mode", "set", "INTERNAL")
        assert _read_cfg()["privacy"]["mode"] == "internal"

    def test_invalid_mode_rejected(self):
        result = _invoke("mode", "set", "paranoid")
        assert result.exit_code != 0
        assert "must be one of" in result.output

    def test_set_setup_skipped_true(self):
        result = _invoke("setup_skipped", "set", "true")
        assert result.exit_code == 0
        assert _read_cfg()["privacy"]["setup_skipped"] is True

    def test_set_setup_skipped_false(self):
        result = _invoke("setup_skipped", "set", "false")
        assert result.exit_code == 0
        assert _read_cfg()["privacy"]["setup_skipped"] is False

    def test_invalid_bool_rejected(self):
        result = _invoke("setup_skipped", "set", "maybe")
        assert result.exit_code != 0

    def test_add_rejected_for_scalar_field(self):
        result = _invoke("mode", "add", "internal")
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Matrix EXCLUDE invariant guard
# ---------------------------------------------------------------------------


class TestMatrixExcludeGuard:
    def test_allow_apps_cannot_add_password_manager(self):
        """1Password is in PASSWORD_MANAGER which the matrix unconditionally
        excludes; allow_apps cannot loosen this."""
        result = _invoke("allow_apps", "add", "com.1password.1password")
        assert result.exit_code != 0
        assert "PASSWORD_MANAGER" in result.output or "matrix" in result.output

    def test_allow_apps_cannot_add_chat_app_under_internal(self):
        """CHAT under internal is MASK_WINDOW — allow_apps cannot loosen
        Unit 7a's strengthening for conversation apps (todo 005)."""
        # default mode is internal in the test setup
        result = _invoke("allow_apps", "add", "com.tinyspeck.slackmacgap")
        assert result.exit_code != 0
        out = result.output.lower()
        assert "chat" in out and "mask_window" in out

    def test_allow_apps_can_add_chat_app_under_public(self):
        """Under mode=public the matrix produces MASK_WINDOW for CHAT, so
        allow_apps still blocks (todo 005 gates EXCLUDE/MASK_WINDOW/TEXT_REDACT)."""
        _invoke("mode", "set", "public")
        result = _invoke("allow_apps", "add", "com.tinyspeck.slackmacgap")
        assert result.exit_code != 0
        out = result.output.lower()
        assert "chat" in out

    def test_allow_apps_can_add_browser(self):
        """BROWSER_UNVERIFIED is ALLOW under internal — explicit allow OK."""
        result = _invoke("allow_apps", "add", "com.brave.Browser")
        assert result.exit_code == 0
        assert "com.brave.Browser" in _read_cfg()["privacy"]["allow_apps"]

    def test_allow_apps_cannot_add_banking_app_under_internal(self):
        """BANKING under internal is MASK_WINDOW — must be blocked."""
        result = _invoke("allow_apps", "add", "com.robinhood.release.Robinhood")
        # If Robinhood isn't in the bundle map this test is vacuous; pick a
        # known-mapped bundle. Use a representative banking bundle ID that's
        # in BUNDLE_ID_MAP.
        from screencap.privacy.classify import BUNDLE_ID_MAP
        from screencap.privacy.policy import ContextClass
        banking_bundle = next(
            (bid for bid, cls in BUNDLE_ID_MAP.items() if cls == ContextClass.BANKING),
            None,
        )
        if banking_bundle is None:
            pytest.skip("No BANKING bundle in BUNDLE_ID_MAP")
        result = _invoke("allow_apps", "add", banking_bundle)
        assert result.exit_code != 0
        out = result.output.lower()
        assert "banking" in out or "mask_window" in out

    def test_exclude_apps_can_add_password_manager(self):
        """exclude_apps can always add anything — strictening is safe."""
        result = _invoke("exclude_apps", "add", "com.1password.1password")
        assert result.exit_code == 0

    def test_app_classes_cannot_loosen_password_manager(self):
        """Todo 006 invariant: reclassifying 1Password from PASSWORD_MANAGER
        to UNKNOWN would change matrix evaluation from EXCLUDE → ALLOW under
        internal mode, defeating the password-manager guard. Must be rejected
        before persisting."""
        result = _invoke("app_classes", "set", "com.1password.1password=unknown")
        assert result.exit_code != 0
        out = result.output.lower()
        assert "matrix_invariant" in out or "loosen" in out or "rejected" in out
        # Config must NOT have been mutated.
        cfg = _read_cfg()
        assert "com.1password.1password" not in cfg.get("privacy", {}).get("app_classes", {})

    def test_app_classes_cannot_loosen_password_manager_to_chat(self):
        """Severity-based guard: reclassifying 1Password (PASSWORD_MANAGER,
        matrix=EXCLUDE) to CHAT (matrix=MASK_WINDOW under internal) would
        loosen EXCLUDE → MASK_WINDOW. The previous blocking-vs-non-blocking
        check missed this because both endpoints are 'blocking' actions."""
        result = _invoke("app_classes", "set", "com.1password.1password=chat")
        assert result.exit_code != 0
        out = result.output.lower()
        assert "loosen" in out or "matrix_invariant" in out
        # Config not mutated.
        cfg = _read_cfg()
        assert "com.1password.1password" not in cfg.get("privacy", {}).get("app_classes", {})

    def test_app_classes_cannot_loosen_chat_to_browser_unverified(self):
        """CHAT (MASK_WINDOW under internal) → BROWSER_UNVERIFIED (ALLOW)
        is a clear loosening. The original check would have caught this
        too, but the test pins the regression to keep the cross-tier
        coverage explicit."""
        result = _invoke("app_classes", "set", "com.tinyspeck.slackmacgap=browser_unverified")
        assert result.exit_code != 0

    def test_app_classes_can_strictern_browser_to_chat(self):
        """Strictening direction is always allowed: BROWSER_UNVERIFIED
        (ALLOW under internal) → CHAT (MASK_WINDOW) is the user opting
        in to stricter handling."""
        result = _invoke("app_classes", "set", "com.openai.chat=chat")
        assert result.exit_code == 0
        cfg = _read_cfg()
        assert cfg["privacy"]["app_classes"]["com.openai.chat"] == "chat"


class TestMatrixFloorUnknownBundles:
    """Finding 001 — fail-closed for bundles not in BUNDLE_ID_MAP and not
    classified via app_classes. The runtime evaluator's strictness floor
    mostly mitigates the harm, but the CLI add-time path stays explicit:
    refuse to allow-list an unclassified bundle, and prevent the two-step
    bypass (set X=password_manager → remove X → allow_apps add X).
    """

    def test_allow_apps_add_rejects_unknown_bundle(self):
        """Variant A: bundle absent from BUNDLE_ID_MAP and not overridden via
        app_classes — refuse the add. Caller must classify first."""
        result = _invoke("allow_apps", "add", "com.example.fictional-unknown")
        assert result.exit_code != 0
        out = result.output.lower()
        assert "unknown_bundle_id" in out or "unclassified" in out

    def test_allow_apps_add_succeeds_for_known_browser(self):
        """Counter-test: a real BUNDLE_ID_MAP browser entry still works
        (proves the unknown-bundle guard isn't blocking everything)."""
        result = _invoke("allow_apps", "add", "com.brave.Browser")
        assert result.exit_code == 0

    def test_allow_apps_add_succeeds_after_explicit_app_classes_set(self):
        """The fail-closed message tells the user to set app_classes first.
        Verify that path works: classify, then add."""
        ok = _invoke("app_classes", "set",
                     "com.example.fictional-tool=browser_unverified")
        assert ok.exit_code == 0
        result = _invoke("allow_apps", "add", "com.example.fictional-tool")
        assert result.exit_code == 0

    def test_app_classes_set_unknown_bundle_to_unknown_class(self):
        """Variant B partial: setting an unknown bundle to UNKNOWN class is
        a no-op transition (UNKNOWN→UNKNOWN). Severity compare allows it."""
        result = _invoke("app_classes", "set", "com.example.foo=unknown")
        assert result.exit_code == 0

    def test_app_classes_remove_rejects_loosening_password_manager(self):
        """Variant C — the load-bearing fix. Set 1Password (already in
        BUNDLE_ID_MAP as PASSWORD_MANAGER) to itself, then try to remove
        — fails because removing would still leave fallback=PASSWORD_MANAGER.
        Use a fictional bundle with a tight override instead."""
        # Step 1: classify a fictional bundle as password_manager (tightening,
        # accepted).
        ok = _invoke("app_classes", "set",
                     "com.example.fake-vault=password_manager")
        assert ok.exit_code == 0
        # Step 2: try to remove the override. Fallback would be UNKNOWN (no
        # BUNDLE_ID_MAP entry) which is ALLOW under internal — that's a
        # loosen from EXCLUDE → ALLOW. Must reject.
        result = _invoke("app_classes", "remove", "com.example.fake-vault")
        assert result.exit_code != 0
        out = result.output.lower()
        assert "matrix_invariant_blocks_remove" in out or "loosen" in out

    def test_app_classes_remove_allows_when_fallback_is_same_or_stricter(self):
        """Removing an override that re-routes to BUNDLE_ID_MAP at the same
        or stricter class is fine. Use Slack: BUNDLE_ID_MAP → CHAT
        (MASK_WINDOW under internal). Set override=email (also MASK_WINDOW
        under internal) — removing the override drops back to CHAT, same
        severity. Accepted."""
        # Slack is in BUNDLE_ID_MAP as CHAT. Override to EMAIL (also
        # MASK_WINDOW under internal — same severity).
        ok = _invoke("app_classes", "set",
                     "com.tinyspeck.slackmacgap=email")
        assert ok.exit_code == 0
        # Remove the override. Falls back to CHAT (BUNDLE_ID_MAP). Same
        # severity → accepted.
        result = _invoke("app_classes", "remove", "com.tinyspeck.slackmacgap")
        assert result.exit_code == 0

    def test_two_step_bypass_blocked_at_remove(self):
        """The full Variant C chain: set X=password_manager →
        remove X (REJECTED here) → would-be allow_apps add X never reached."""
        ok = _invoke("app_classes", "set",
                     "com.example.bypass-attempt=password_manager")
        assert ok.exit_code == 0
        result = _invoke("app_classes", "remove",
                         "com.example.bypass-attempt")
        # Bypass blocked at the remove step.
        assert result.exit_code != 0
        # Confirm config still has the override (remove was rejected).
        cfg = _read_cfg()
        assert (cfg["privacy"]["app_classes"]["com.example.bypass-attempt"]
                == "password_manager")

    def test_app_classes_can_set_chat_to_chat(self):
        """Same-class write is a no-op-shaped accept (no loosening)."""
        # Slack is already CHAT in BUNDLE_ID_MAP; setting to CHAT again is fine.
        result = _invoke("app_classes", "set", "com.tinyspeck.slackmacgap=chat")
        assert result.exit_code == 0

    def test_app_classes_can_set_unknown_to_chat(self):
        """Strictening direction: UNKNOWN bundle reclassified to CHAT (which
        has matrix=MASK_WINDOW under internal). Strictening is always allowed."""
        result = _invoke("app_classes", "set", "com.example.fictional=chat")
        assert result.exit_code == 0
        cfg = _read_cfg()
        assert cfg["privacy"]["app_classes"]["com.example.fictional"] == "chat"

    def test_allow_apps_blocked_via_app_classes_override(self):
        """Todo 030: a bundle absent from BUNDLE_ID_MAP but reclassified by
        the user as a sensitive class via app_classes cannot be allow-listed.
        The two-step bypass (set + add) must fail at the second step."""
        # Step 1: classify a fictional bundle as banking (strictening — allowed).
        ok1 = _invoke("app_classes", "set", "com.example.fakebank=banking")
        assert ok1.exit_code == 0
        # Step 2: try to allow-list it. Should reject because banking@internal
        # produces MASK_WINDOW.
        result = _invoke("allow_apps", "add", "com.example.fakebank")
        assert result.exit_code != 0
        out = result.output.lower()
        assert "banking" in out or "mask_window" in out


# ---------------------------------------------------------------------------
# R16 round-trip: mode preservation across mutations
# ---------------------------------------------------------------------------


class TestTomlkitCommentPreservation:
    """Todo 016: list-field mutations must preserve inline TOML comments
    and multi-line array formatting. The previous list()-and-reassign
    approach silently destroyed all tomlkit metadata. R16 round-trip
    invariant claims comment preservation; the tomllib-based round-trip
    test passes either way (it strips comments before assertion). This
    test reads the raw file content."""

    def test_array_inline_comment_survives_add(self):
        from screencap.config import _CONFIG_PATH

        # Write an array with an inline comment. Use a real browser bundle
        # for the new entry so the matrix-floor guard (Finding 001 Variant A)
        # accepts it — the test is about tomlkit comment preservation, not
        # the allow_apps gate.
        _write_config(_CONFIG_PATH, """
[privacy]
mode = "internal"
allow_apps = [
    "com.example.alpha",  # approved by security 2026-04
    "com.example.beta",
]
""".lstrip())

        result = _invoke("allow_apps", "add", "com.apple.Safari")
        assert result.exit_code == 0

        # The inline comment + multi-line formatting should survive.
        content = _CONFIG_PATH.read_text()
        assert "approved by security 2026-04" in content, (
            f"inline comment was destroyed. Content:\n{content}"
        )
        # The new value should be present.
        assert "com.apple.Safari" in content

    def test_section_layout_survives_remove(self):
        from screencap.config import _CONFIG_PATH

        _write_config(_CONFIG_PATH, """
# Privacy posture for this machine
[privacy]
mode = "internal"  # see docs/privacy.md for matrix
exclude_apps = ["com.example.x"]
""".lstrip())

        result = _invoke("exclude_apps", "remove", "com.example.x")
        assert result.exit_code == 0

        content = _CONFIG_PATH.read_text()
        assert "see docs/privacy.md" in content
        assert "Privacy posture for this machine" in content


def _hold_config_lock_in_child(lock_path, started_q, hold_seconds):
    """Subprocess target: hold the config flock for `hold_seconds`."""
    import fcntl as _fcntl
    import os as _os
    import time as _time

    fd = _os.open(str(lock_path), _os.O_RDWR | _os.O_CREAT, 0o600)
    _fcntl.flock(fd, _fcntl.LOCK_EX)
    started_q.put("acquired")
    _time.sleep(hold_seconds)


class TestConcurrentMutations:
    """Todo 015: concurrent settings privacy invocations must not lose
    updates. The advisory flock on ~/.screencap/run/config.lock serializes
    the read-modify-write cycle."""

    def test_concurrent_writers_do_not_lose_updates(self, tmp_path):
        """Holds the config flock from a child process; the parent
        invocation should block on the flock and complete after release,
        with both writes present."""
        import multiprocessing
        import time as _time

        from screencap.privacy_settings import _config_lock_path

        # Pre-create the directory so the child can open the lock file
        _config_lock_path().parent.mkdir(parents=True, exist_ok=True)

        ctx = multiprocessing.get_context("spawn")
        started_q = ctx.Queue()
        # Hold for 1.5s — long enough that the parent invocation must wait.
        proc = ctx.Process(
            target=_hold_config_lock_in_child,
            args=(_config_lock_path(), started_q, 1.5),
        )
        proc.start()
        try:
            assert started_q.get(timeout=5) == "acquired"

            # While the child holds the flock, the parent's invoke should
            # block on the flock acquire. Time it.
            t0 = _time.time()
            result = _invoke("exclude_apps", "add", "com.example.contended")
            elapsed = _time.time() - t0

            assert result.exit_code == 0
            # Parent had to wait for the child to release — at least ~1s
            # of the 1.5s hold should be observed.
            assert elapsed >= 0.8, (
                f"parent didn't block on flock — elapsed={elapsed:.2f}s "
                "suggests the lock is not actually serializing writes"
            )
            assert "com.example.contended" in _read_cfg()["privacy"]["exclude_apps"]
        finally:
            proc.join(timeout=5)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=5)


class TestRoundTripModePreservation:
    def test_mode_preserved_across_list_mutations(self):
        # Start with mode = public (non-default, must not be substituted)
        _invoke("mode", "set", "public")
        # 10 alternating add/remove on exclude_apps
        for i in range(10):
            _invoke("exclude_apps", "add", f"com.example.app{i}")
            _invoke("exclude_apps", "remove", f"com.example.app{i}")
        # Mode must still be public
        assert _read_cfg()["privacy"]["mode"] == "public"

    def test_other_keys_preserved_when_setting_mode(self):
        # ``allow_apps add`` requires a known/classifiable bundle (Finding 001
        # Variant A); use a real browser so the test exercises round-trip
        # preservation rather than the matrix-floor guard.
        _invoke("exclude_apps", "add", "com.example.x")
        _invoke("allow_apps", "add", "com.apple.Safari")
        _invoke("mode", "set", "internal")
        cfg = _read_cfg()["privacy"]
        assert cfg["mode"] == "internal"
        assert "com.example.x" in cfg["exclude_apps"]
        assert "com.apple.Safari" in cfg["allow_apps"]


# ---------------------------------------------------------------------------
# Map field — app_classes (todo 023)
# ---------------------------------------------------------------------------


class TestAppClassesMapField:
    def test_set_valid_context_class(self):
        # Input is case-insensitive but stored as the lowercase enum.value
        result = _invoke("app_classes", "set", "com.example.foo=BROWSER_UNVERIFIED")
        assert result.exit_code == 0
        cfg = _read_cfg()["privacy"]
        assert cfg["app_classes"]["com.example.foo"] == "browser_unverified"

    def test_add_valid_context_class(self):
        result = _invoke("app_classes", "add", "com.example.bar=CHAT")
        assert result.exit_code == 0
        assert _read_cfg()["privacy"]["app_classes"]["com.example.bar"] == "chat"

    def test_add_lowercase_valid_context_class(self):
        result = _invoke("app_classes", "add", "com.example.lower=email")
        assert result.exit_code == 0
        assert _read_cfg()["privacy"]["app_classes"]["com.example.lower"] == "email"

    def test_add_then_remove(self):
        # Use UNKNOWN as the override class so the post-remove fallback
        # (UNKNOWN for an unmapped bundle) produces the same matrix action
        # — the Finding 001 Variant C guard rejects ANY remove that loosens
        # the matrix at the configured mode. EMAIL→UNKNOWN would loosen
        # TEXT_REDACT to ALLOW under ``internal`` and is therefore blocked
        # (covered separately in ``TestMatrixFloorUnknownBundles``).
        _invoke("app_classes", "add", "com.example.gone=UNKNOWN")
        result = _invoke("app_classes", "remove", "com.example.gone")
        assert result.exit_code == 0
        assert "com.example.gone" not in _read_cfg()["privacy"].get("app_classes", {})

    def test_set_invalid_context_class_rejected(self):
        """A typo'd class string must be caught at write time, not on next
        start (todo 010). Otherwise PrivacyConfig.parse crashes the recorder."""
        result = _invoke("app_classes", "set", "com.example.bad=NOT_A_REAL_CLASS")
        assert result.exit_code != 0
        assert "Unknown context class" in result.output or "NOT_A_REAL_CLASS" in result.output

    def test_value_missing_equals_rejected(self):
        result = _invoke("app_classes", "set", "com.example.no_equals_here")
        assert result.exit_code != 0
        assert "BUNDLE_ID=CLASS" in result.output or "requires" in result.output


# ---------------------------------------------------------------------------
# Internal-flag isolation (todo 012)
# ---------------------------------------------------------------------------


class TestMatrixAckFlagNotPublic:
    def test_matrix_ack_flag_not_settable_via_cli(self):
        """The migration flag is internal — the public scalar list must not
        expose it to scripted callers."""
        result = _invoke("matrix_acknowledged_v2026_04", "set", "true")
        assert result.exit_code != 0
        assert "Unknown privacy field" in result.output

    def test_shared_mode_rejected(self):
        """`shared` is reserved for MASK_REGION; PrivacyConfig.parse rejects
        it. The CLI must not write an unenforceable mode (todo 011)."""
        result = _invoke("mode", "set", "shared")
        assert result.exit_code != 0
        assert "must be one of" in result.output


# ---------------------------------------------------------------------------
# --json output (todo 016)
# ---------------------------------------------------------------------------


def _last_json_line(text: str) -> dict:
    """Find the last line of `text` that parses as JSON. CliRunner combines
    stdout and stderr; the JSON success/error payload lives among prose."""
    import json as _json
    last = None
    for line in text.strip().splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            last = _json.loads(line)
        except _json.JSONDecodeError:
            continue
    assert last is not None, f"no JSON line found in: {text!r}"
    return last


class TestJsonOutput:
    def test_success_emits_json(self):
        result = _invoke(
            "exclude_apps", "add", "com.example.json_test", as_json=True,
        )
        assert result.exit_code == 0
        payload = _last_json_line(result.output)
        # Uniform envelope (todo 020): ok + schema_version lead, then payload.
        assert payload["ok"] is True
        assert payload["schema_version"] >= 1
        assert payload["changed"] is True
        assert payload["field"] == "exclude_apps"
        assert payload["op"] == "add"
        assert payload["value"] == "com.example.json_test"

    def test_idempotent_noop_emits_changed_false(self):
        _invoke("exclude_apps", "add", "com.example.dup")
        result = _invoke(
            "exclude_apps", "add", "com.example.dup", as_json=True,
        )
        assert result.exit_code == 0
        payload = _last_json_line(result.output)
        assert payload["ok"] is True
        assert payload["changed"] is False

    def test_error_emits_ok_false_with_error_field(self):
        result = _invoke("no_such_field", "set", "value", as_json=True)
        assert result.exit_code != 0
        payload = _last_json_line(result.output)
        assert payload["ok"] is False
        assert "error" in payload

    def test_envelope_keys_symmetric_success_and_error(self):
        """Schema v2 (todo 011) — ``settings privacy --json`` must emit the
        same key set on success AND error so an agent has one parser shape.
        """
        expected_keys = {
            "ok", "schema_version", "changed", "field", "op", "value", "error",
        }

        success = _invoke(
            "exclude_apps", "add", "com.example.symmetric", as_json=True,
        )
        success_payload = _last_json_line(success.output)
        assert set(success_payload.keys()) == expected_keys
        assert success_payload["error"] is None
        assert success_payload["schema_version"] == 2

        error = _invoke("no_such_field", "set", "value", as_json=True)
        error_payload = _last_json_line(error.output)
        assert set(error_payload.keys()) == expected_keys
        assert error_payload["field"] == "no_such_field"
        assert error_payload["op"] == "set"
        assert error_payload["value"] == "value"
        assert error_payload["changed"] is False
