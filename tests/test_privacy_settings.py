"""Direct-seam unit tests for screencap.privacy_settings (SCR-156).

These exercise the helpers extracted from cli/__init__.py at their new home,
without a CliRunner round trip. Behavioral coverage of the mutation engine and
matrix guard via the Click command lives in tests/test_settings_privacy.py;
this file targets the seams that extraction newly makes directly testable, plus
the fail-fast / rollback contracts that the CliRunner suite does not reach.
"""

from __future__ import annotations

import fcntl
import os

import pytest

pytestmark = pytest.mark.privacy


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    """Point config + lock paths at a tmp dir (mirrors test_settings_privacy)."""
    import screencap.config

    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr(screencap.config, "_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(screencap.config, "_DEFAULT_BASE", tmp_path)
    monkeypatch.setattr(screencap.config, "_config_cache", None)


class TestPrivacyConfigWriter:
    """The advisory-flock read-modify-write helper (R9 / todo 015 / todo 025)."""

    def test_raises_timeout_when_lock_is_held(self, monkeypatch):
        """A holder that never releases makes the writer fail fast rather than
        hang — surfaces a stuck peer instead of stalling `screencap start`."""
        from screencap import privacy_settings
        from screencap.privacy_settings import (
            PrivacyConfigLockTimeout,
            _config_lock_path,
            _privacy_config_writer,
        )

        # Shrink the timeout so the bounded retry loop exits quickly.
        monkeypatch.setattr(privacy_settings, "_PRIVACY_CONFIG_FLOCK_TIMEOUT_S", 0.3)

        lock_path = _config_lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Hold the flock on a separate open file description; the writer opens
        # its own fd and its LOCK_EX|LOCK_NB acquire will keep hitting EWOULDBLOCK.
        holder_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(holder_fd, fcntl.LOCK_EX)
            with pytest.raises(PrivacyConfigLockTimeout):
                with _privacy_config_writer():
                    pytest.fail("writer should never acquire the lock")
        finally:
            fcntl.flock(holder_fd, fcntl.LOCK_UN)
            os.close(holder_fd)

    def test_leaves_file_untouched_on_exception(self):
        """On an exception inside the `with`, the doc is not saved — the
        read-modify-write cycle is all-or-nothing (no partial write)."""
        from screencap.config import _CONFIG_PATH
        from screencap.privacy_settings import _privacy_config_writer

        _CONFIG_PATH.write_text('[privacy]\nmode = "internal"\n', encoding="utf-8")
        original = _CONFIG_PATH.read_text(encoding="utf-8")

        class _Boom(RuntimeError):
            pass

        with pytest.raises(_Boom):
            with _privacy_config_writer() as doc:
                doc["privacy"]["mode"] = "public"  # mutate in memory only
                raise _Boom()

        assert _CONFIG_PATH.read_text(encoding="utf-8") == original


class TestAllowRequiresConfirmation:
    """The SCR-235 confirmation gate as a pure function — mode-independent:
    a class needs the confirm flag iff the matrix excludes it in any mode.
    Behavioral coverage through `settings privacy` lives in
    test_settings_privacy.py."""

    def test_exclude_anywhere_classes_require_confirmation(self):
        from screencap.privacy.policy import ContextClass
        from screencap.privacy_settings import _allow_requires_confirmation

        for ctx in (
            ContextClass.PASSWORD_MANAGER,
            ContextClass.BANKING,
            ContextClass.AUTH_FLOW,
            ContextClass.PAYMENT_FLOW,
        ):
            assert _allow_requires_confirmation(ctx) is True

    def test_mask_class_and_allow_class_confirm_silently(self):
        from screencap.privacy.policy import ContextClass
        from screencap.privacy_settings import _allow_requires_confirmation

        for ctx in (
            ContextClass.CHAT,
            ContextClass.EMAIL,
            ContextClass.BROWSER_UNVERIFIED,
            ContextClass.CODE_EDITOR_TERMINAL,
            ContextClass.UNKNOWN,
        ):
            assert _allow_requires_confirmation(ctx) is False


class _ResultRecorder:
    """Faithful stub for the CLI ``_result`` closure.

    The real ``_result`` (built inside the ``settings privacy`` command) emits
    the JSON/prose envelope and raises ``SystemExit`` when given a non-zero
    ``exit_code``. ``_settings_privacy_apply``'s contract is that hard errors
    "raise ``SystemExit`` via ``_result(exit_code=...)`` and never return", so
    the stub mirrors that: it records every call and raises ``SystemExit`` on
    the ``exit_code`` path so the apply function's control flow matches the CLI.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, ok, *, exit_code=None, changed=None, error=None):
        self.calls.append(
            {"ok": ok, "exit_code": exit_code, "changed": changed, "error": error}
        )
        if exit_code:
            raise SystemExit(exit_code)


class TestSettingsPrivacyApply:
    """Direct-seam unit tests for ``_settings_privacy_apply`` (Finding #2).

    Calls the mutation engine with a real tomlkit table and a faithful
    ``_result`` stub — no CliRunner. Behavioral coverage through the Click
    command lives in tests/test_settings_privacy.py; this pins the apply
    function's return contract and matrix-guard signaling at the seam the
    SCR-156 extraction newly exposes.
    """

    def _apply(self, privacy_tbl, *, field, op, value, is_list, is_scalar,
               parsed_value=None, result=None, confirm_sensitive=False):
        import tomlkit as _tomlkit
        from rich.console import Console
        from screencap.privacy_settings import _settings_privacy_apply

        result = result if result is not None else _ResultRecorder()
        changed = _settings_privacy_apply(
            privacy_tbl=privacy_tbl,
            field=field,
            op=op,
            value=value,
            is_list=is_list,
            is_scalar=is_scalar,
            parsed_value=parsed_value,
            err_console=Console(stderr=True),
            tomlkit=_tomlkit,
            _result=result,
            confirm_sensitive=confirm_sensitive,
        )
        return changed, result

    def test_allow_apps_add_chat_confirms_silently(self):
        """SCR-235: a mask-class add succeeds without the flag and writes BOTH
        allow_apps and confirmed_allow_apps in the same transaction."""
        import tomlkit

        tbl = tomlkit.table()
        tbl["mode"] = "internal"

        changed, recorder = self._apply(
            tbl,
            field="allow_apps",
            op="add",
            value="com.tinyspeck.slackmacgap",  # CHAT — mask-class
            is_list=True,
            is_scalar=False,
        )

        assert changed is True
        assert recorder.calls == []
        assert "com.tinyspeck.slackmacgap" in list(tbl["allow_apps"])
        assert "com.tinyspeck.slackmacgap" in list(tbl["confirmed_allow_apps"])

    def test_allow_apps_add_sensitive_without_flag_errors(self):
        """Covers AE2 (CLI half): a confirmation-required class without the
        flag exits 1 with an actionable ``confirmation_required:*`` error and
        writes nothing."""
        import tomlkit

        tbl = tomlkit.table()
        tbl["mode"] = "internal"
        recorder = _ResultRecorder()

        with pytest.raises(SystemExit):
            self._apply(
                tbl,
                field="allow_apps",
                op="add",
                value="com.1password.1password",  # PASSWORD_MANAGER
                is_list=True,
                is_scalar=False,
                result=recorder,
            )

        assert len(recorder.calls) == 1
        call = recorder.calls[0]
        assert call["ok"] is False
        assert call["exit_code"] == 1
        assert call["error"] == "confirmation_required:password_manager"
        assert "allow_apps" not in tbl or "com.1password.1password" not in tbl.get(
            "allow_apps", []
        )
        assert "confirmed_allow_apps" not in tbl or (
            "com.1password.1password" not in tbl.get("confirmed_allow_apps", [])
        )

    def test_allow_apps_add_sensitive_with_flag_writes_both_lists(self):
        """Covers AE2 (CLI half): with the flag, the sensitive add lands in
        both lists."""
        import tomlkit

        tbl = tomlkit.table()
        tbl["mode"] = "internal"

        changed, recorder = self._apply(
            tbl,
            field="allow_apps",
            op="add",
            value="com.1password.1password",
            is_list=True,
            is_scalar=False,
            confirm_sensitive=True,
        )

        assert changed is True
        assert recorder.calls == []
        assert "com.1password.1password" in list(tbl["allow_apps"])
        assert "com.1password.1password" in list(tbl["confirmed_allow_apps"])

    def test_allow_apps_readd_confirms_legacy_entry(self):
        """Re-adding an existing legacy (unconfirmed) entry is the CLI
        upgrade path: it writes the confirmed entry and reports a change."""
        import tomlkit

        tbl = tomlkit.table()
        tbl["mode"] = "internal"
        arr = tomlkit.array()
        arr.append("com.tinyspeck.slackmacgap")
        tbl["allow_apps"] = arr

        changed, recorder = self._apply(
            tbl,
            field="allow_apps",
            op="add",
            value="com.tinyspeck.slackmacgap",
            is_list=True,
            is_scalar=False,
        )

        assert changed is True
        assert list(tbl["allow_apps"]).count("com.tinyspeck.slackmacgap") == 1
        assert "com.tinyspeck.slackmacgap" in list(tbl["confirmed_allow_apps"])

    def test_allow_apps_add_fully_confirmed_is_noop(self):
        """An entry already in both lists is an idempotent no-op."""
        import tomlkit

        tbl = tomlkit.table()
        tbl["mode"] = "internal"
        for f in ("allow_apps", "confirmed_allow_apps"):
            arr = tomlkit.array()
            arr.append("com.tinyspeck.slackmacgap")
            tbl[f] = arr

        changed, recorder = self._apply(
            tbl,
            field="allow_apps",
            op="add",
            value="com.tinyspeck.slackmacgap",
            is_list=True,
            is_scalar=False,
        )

        assert changed is False
        assert recorder.calls[0]["changed"] is False

    def test_allow_apps_remove_prunes_confirmed_entry(self):
        """Removing an allow prunes the confirmed entry (case-insensitively)
        so re-allowing a sensitive app re-prompts."""
        import tomlkit

        tbl = tomlkit.table()
        tbl["mode"] = "internal"
        arr = tomlkit.array()
        arr.append("com.1password.1password")
        tbl["allow_apps"] = arr
        conf = tomlkit.array()
        conf.append("COM.1PASSWORD.1PASSWORD")
        tbl["confirmed_allow_apps"] = conf

        changed, recorder = self._apply(
            tbl,
            field="allow_apps",
            op="remove",
            value="com.1password.1password",
            is_list=True,
            is_scalar=False,
        )

        assert changed is True
        assert "com.1password.1password" not in list(tbl["allow_apps"])
        assert list(tbl["confirmed_allow_apps"]) == []

    def test_allow_apps_readd_matches_case_variant_stored_entry(self):
        """Write-seam membership is case-insensitive (the runtime normalizes
        casing): re-adding the canonical id over a case-variant stored entry
        is a no-op, not a duplicate append."""
        import tomlkit

        tbl = tomlkit.table()
        tbl["mode"] = "internal"
        for f in ("allow_apps", "confirmed_allow_apps"):
            arr = tomlkit.array()
            arr.append("COM.TINYSPECK.SLACKMACGAP")
            tbl[f] = arr

        changed, recorder = self._apply(
            tbl,
            field="allow_apps",
            op="add",
            value="com.tinyspeck.slackmacgap",
            is_list=True,
            is_scalar=False,
        )

        assert changed is False
        assert len(list(tbl["allow_apps"])) == 1
        assert len(list(tbl["confirmed_allow_apps"])) == 1

    def test_allow_apps_case_variant_remove_finds_entry(self):
        """A case-variant remove still finds and prunes both entries."""
        import tomlkit

        tbl = tomlkit.table()
        tbl["mode"] = "internal"
        for f in ("allow_apps", "confirmed_allow_apps"):
            arr = tomlkit.array()
            arr.append("com.tinyspeck.slackmacgap")
            tbl[f] = arr

        changed, recorder = self._apply(
            tbl,
            field="allow_apps",
            op="remove",
            value="COM.TINYSPECK.SLACKMACGAP",
            is_list=True,
            is_scalar=False,
        )

        assert changed is True
        assert list(tbl["allow_apps"]) == []
        assert list(tbl["confirmed_allow_apps"]) == []

    def test_allow_apps_add_unclassified_bundle_is_permitted(self):
        """SCR-225 (KTD9) retired the unclassified-bundle refusal.

        The old rule made a tightened `default_action` unusable: it blocks every
        app the user has not ruled on, and most of a real library is absent from
        BUNDLE_ID_MAP, so the escape hatch (tap Record) hard-failed on exactly
        the apps the floor caught. An unclassified bundle now allow-lists as a
        plain confirmed entry; `_allow_requires_confirmation` still gates every
        bundle that resolves to a confirmation-required class."""
        import tomlkit

        tbl = tomlkit.table()
        tbl["mode"] = "internal"

        changed, recorder = self._apply(
            tbl,
            field="allow_apps",
            op="add",
            value="com.example.mystery",
            is_list=True,
            is_scalar=False,
        )

        assert changed is True
        assert list(tbl["allow_apps"]) == ["com.example.mystery"]
        assert list(tbl["confirmed_allow_apps"]) == ["com.example.mystery"]
        assert not any(c["exit_code"] for c in recorder.calls)

    # --- SCR-225: mask_apps and the three-way mutual exclusion -------------

    def test_mask_apps_add_needs_no_confirmation_for_a_sensitive_class(self):
        """A Mask rule is tightening-only, so it never asks for the
        allow-listing confirmation even on a password manager."""
        import tomlkit

        tbl = tomlkit.table()
        tbl["mode"] = "internal"

        changed, recorder = self._apply(
            tbl,
            field="mask_apps",
            op="add",
            value="com.1password.1password",
            is_list=True,
            is_scalar=False,
        )

        assert changed is True
        assert list(tbl["mask_apps"]) == ["com.1password.1password"]
        assert not any(c["exit_code"] for c in recorder.calls)

    def test_mask_apps_add_prunes_the_other_two_rules(self):
        """The three segments are mutually exclusive per bundle, enforced in one
        transaction so a row can never render a state evaluation contradicts."""
        import tomlkit

        tbl = tomlkit.table()
        tbl["mode"] = "internal"
        for f in ("exclude_apps", "allow_apps", "confirmed_allow_apps"):
            arr = tomlkit.array()
            arr.append("com.apple.Terminal")
            tbl[f] = arr

        changed, _ = self._apply(
            tbl,
            field="mask_apps",
            op="add",
            value="com.apple.Terminal",
            is_list=True,
            is_scalar=False,
        )

        assert changed is True
        assert list(tbl["mask_apps"]) == ["com.apple.Terminal"]
        assert list(tbl["exclude_apps"]) == []
        assert list(tbl["allow_apps"]) == []
        assert list(tbl["confirmed_allow_apps"]) == []

    def test_exclude_apps_add_prunes_a_mask_rule(self):
        import tomlkit

        tbl = tomlkit.table()
        tbl["mode"] = "internal"
        arr = tomlkit.array()
        arr.append("com.apple.Terminal")
        tbl["mask_apps"] = arr

        changed, _ = self._apply(
            tbl,
            field="exclude_apps",
            op="add",
            value="com.apple.Terminal",
            is_list=True,
            is_scalar=False,
        )

        assert changed is True
        assert list(tbl["exclude_apps"]) == ["com.apple.Terminal"]
        assert list(tbl["mask_apps"]) == []

    def test_allow_apps_add_prunes_a_mask_rule(self):
        import tomlkit

        tbl = tomlkit.table()
        tbl["mode"] = "internal"
        arr = tomlkit.array()
        arr.append("com.apple.Terminal")
        tbl["mask_apps"] = arr

        changed, _ = self._apply(
            tbl,
            field="allow_apps",
            op="add",
            value="com.apple.Terminal",
            is_list=True,
            is_scalar=False,
        )

        assert changed is True
        assert list(tbl["allow_apps"]) == ["com.apple.Terminal"]
        assert list(tbl["mask_apps"]) == []

    def test_cross_list_pruning_is_case_insensitive(self):
        """Runtime membership is case-normalized, so a case-variant stored
        entry must still be pruned — otherwise it stays live but unreachable."""
        import tomlkit

        tbl = tomlkit.table()
        tbl["mode"] = "internal"
        arr = tomlkit.array()
        arr.append("COM.APPLE.TERMINAL")
        tbl["exclude_apps"] = arr

        self._apply(
            tbl,
            field="mask_apps",
            op="add",
            value="com.apple.Terminal",
            is_list=True,
            is_scalar=False,
        )

        assert list(tbl["exclude_apps"]) == []

    def test_mask_apps_remove_leaves_the_other_rules_alone(self):
        """Pruning is an add-time concern; remove must not touch siblings."""
        import tomlkit

        tbl = tomlkit.table()
        tbl["mode"] = "internal"
        mask = tomlkit.array()
        mask.append("com.apple.Terminal")
        tbl["mask_apps"] = mask
        excl = tomlkit.array()
        excl.append("com.example.other")
        tbl["exclude_apps"] = excl

        changed, _ = self._apply(
            tbl,
            field="mask_apps",
            op="remove",
            value="com.apple.Terminal",
            is_list=True,
            is_scalar=False,
        )

        assert changed is True
        assert list(tbl["mask_apps"]) == []
        assert list(tbl["exclude_apps"]) == ["com.example.other"]

    def test_allow_apps_add_reclassified_sensitive_needs_flag(self):
        """The gate consults on-disk app_classes overrides (todo 030): a
        user-reclassified sensitive bundle requires the flag too."""
        import tomlkit

        tbl = tomlkit.table()
        tbl["mode"] = "internal"
        tbl["app_classes"] = {"com.example.helper": "banking"}
        recorder = _ResultRecorder()

        with pytest.raises(SystemExit):
            self._apply(
                tbl,
                field="allow_apps",
                op="add",
                value="com.example.helper",
                is_list=True,
                is_scalar=False,
                result=recorder,
            )

        assert recorder.calls[0]["error"] == "confirmation_required:banking"

    def test_gate_class_resolution_is_case_insensitive(self):
        """Review fix: the gate resolves the effective class the way the
        runtime does — case-insensitively. A case-variant stored override of
        a sensitive class must still require the flag (not fall back to a
        benign built-in class), and a lowercase-typed known browser must not
        be refused as unclassified."""
        import tomlkit

        # (b) case-variant sensitive override still gates
        tbl = tomlkit.table()
        tbl["mode"] = "internal"
        tbl["app_classes"] = {"com.TinySpeck.SlackMacGap": "banking"}
        recorder = _ResultRecorder()
        with pytest.raises(SystemExit):
            self._apply(
                tbl,
                field="allow_apps",
                op="add",
                value="com.tinyspeck.slackmacgap",
                is_list=True,
                is_scalar=False,
                result=recorder,
            )
        assert recorder.calls[0]["error"] == "confirmation_required:banking"

        # (a) lowercase-typed known browser resolves instead of refusing
        tbl2 = tomlkit.table()
        tbl2["mode"] = "internal"
        changed, _ = self._apply(
            tbl2,
            field="allow_apps",
            op="add",
            value="com.google.chrome",
            is_list=True,
            is_scalar=False,
        )
        assert changed is True
        assert "com.google.chrome" in list(tbl2["allow_apps"])

    def test_readd_of_confirmed_entry_is_noop_without_flag(self):
        """Review fix: idempotency — re-asserting an already-confirmed
        sensitive entry without the flag is the documented no-op (exit 0),
        not a confirmation_required error."""
        import tomlkit

        tbl = tomlkit.table()
        tbl["mode"] = "internal"
        for f in ("allow_apps", "confirmed_allow_apps"):
            arr = tomlkit.array()
            arr.append("com.1password.1password")
            tbl[f] = arr

        changed, recorder = self._apply(
            tbl,
            field="allow_apps",
            op="add",
            value="com.1password.1password",
            is_list=True,
            is_scalar=False,
        )
        assert changed is False
        assert recorder.calls[0]["changed"] is False
        assert recorder.calls[0]["exit_code"] is None

    def test_confirmed_allow_apps_add_is_gated(self):
        """Review fix: the direct `confirmed_allow_apps add` verb goes
        through the same confirmation gate — it must not be an in-product
        bypass that silently promotes a legacy allow entry."""
        import tomlkit

        tbl = tomlkit.table()
        tbl["mode"] = "internal"
        arr = tomlkit.array()
        arr.append("com.1password.1password")
        tbl["allow_apps"] = arr
        recorder = _ResultRecorder()

        with pytest.raises(SystemExit):
            self._apply(
                tbl,
                field="confirmed_allow_apps",
                op="add",
                value="com.1password.1password",
                is_list=True,
                is_scalar=False,
                result=recorder,
            )
        assert recorder.calls[0]["error"] == "confirmation_required:password_manager"
        assert "confirmed_allow_apps" not in tbl or (
            "com.1password.1password" not in tbl.get("confirmed_allow_apps", [])
        )

        # With the flag, the direct add succeeds.
        changed, _ = self._apply(
            tbl,
            field="confirmed_allow_apps",
            op="add",
            value="com.1password.1password",
            is_list=True,
            is_scalar=False,
            confirm_sensitive=True,
        )
        assert changed is True
        assert "com.1password.1password" in list(tbl["confirmed_allow_apps"])

    def test_exclude_apps_remove_matches_case_variant_entry(self):
        """Review fix: bundle-id list fields match case-insensitively at the
        write seam — a hand-edited lowercase exclude entry (live at runtime)
        must be removable with the OS-cased id."""
        import tomlkit

        tbl = tomlkit.table()
        tbl["mode"] = "internal"
        arr = tomlkit.array()
        arr.append("com.microsoft.vscode")
        tbl["exclude_apps"] = arr

        changed, _ = self._apply(
            tbl,
            field="exclude_apps",
            op="remove",
            value="com.microsoft.VSCode",
            is_list=True,
            is_scalar=False,
        )
        assert changed is True
        assert list(tbl["exclude_apps"]) == []

    def test_list_add_already_present_is_idempotent_noop(self):
        """Adding a value already in the list returns ``changed=False`` and
        signals the no-op via ``_result(True, changed=False)`` — exit 0."""
        import tomlkit

        tbl = tomlkit.table()
        tbl["mode"] = "internal"
        arr = tomlkit.array()
        arr.append("com.example.foo")
        tbl["exclude_apps"] = arr

        changed, recorder = self._apply(
            tbl,
            field="exclude_apps",
            op="add",
            value="com.example.foo",
            is_list=True,
            is_scalar=False,
        )

        assert changed is False
        assert len(recorder.calls) == 1
        call = recorder.calls[0]
        assert call["ok"] is True
        assert call["changed"] is False
        assert call["exit_code"] is None
        # Still present exactly once — no duplicate appended.
        assert list(tbl["exclude_apps"]).count("com.example.foo") == 1

    def test_exclude_apps_add_new_value_succeeds(self):
        """exclude_apps add of a fresh value returns ``changed=True`` (no
        ``_result`` no-op call) and the value lands in the table."""
        import tomlkit

        tbl = tomlkit.table()
        tbl["mode"] = "internal"

        changed, recorder = self._apply(
            tbl,
            field="exclude_apps",
            op="add",
            value="com.example.newly-excluded",
            is_list=True,
            is_scalar=False,
        )

        assert changed is True
        # A real change does not emit a no-op _result; the caller surfaces success.
        assert recorder.calls == []
        assert "com.example.newly-excluded" in list(tbl["exclude_apps"])


class TestBuildPrivacySettingsBlock:
    """The `settings --json` privacy block reader (SCR-17), now directly tested."""

    def test_no_section_reports_internal_default_and_flag_false(self):
        from screencap.privacy_settings import _build_privacy_settings_block

        # No config file written by this test.
        assert _build_privacy_settings_block() == {
            "mode": "internal",
            "setup_skipped": False,
            "has_privacy_section": False,
        }

    def test_reads_mode_and_flags_from_section(self):
        from screencap.config import _CONFIG_PATH
        from screencap.privacy_settings import _build_privacy_settings_block

        _CONFIG_PATH.write_text(
            '[privacy]\nmode = "public"\nsetup_skipped = true\n', encoding="utf-8"
        )
        block = _build_privacy_settings_block()
        assert block["mode"] == "public"
        assert block["setup_skipped"] is True
        assert block["has_privacy_section"] is True

    def test_invalid_mode_falls_back_to_internal_but_keeps_section_flag(self):
        from screencap.config import _CONFIG_PATH
        from screencap.privacy_settings import _build_privacy_settings_block

        _CONFIG_PATH.write_text('[privacy]\nmode = "bogus"\n', encoding="utf-8")
        block = _build_privacy_settings_block()
        assert block["mode"] == "internal"
        assert block["has_privacy_section"] is True

    def test_non_bool_setup_skipped_coerces_to_false(self):
        """Plan U3: a non-bool ``setup_skipped`` (e.g. integer ``1``) is not a
        valid flag value — the reader coerces it to ``False`` rather than
        surfacing a truthy-but-non-bool value to the SwiftUI pane."""
        from screencap.config import _CONFIG_PATH
        from screencap.privacy_settings import _build_privacy_settings_block

        _CONFIG_PATH.write_text(
            '[privacy]\nmode = "internal"\nsetup_skipped = 1\n', encoding="utf-8"
        )
        block = _build_privacy_settings_block()
        assert block["setup_skipped"] is False
        assert block["has_privacy_section"] is True


class TestPrivacyDiagnostics:
    """The two privacy smoke checks moved in SCR-156; the _SMOKE_CHECKS registry
    stays in cli so its existing patch path keeps working."""

    def test_domain_index_check_passes(self):
        from screencap.privacy_settings import _check_domain_index

        name, ok, err = _check_domain_index()
        assert name == "domain_index"
        assert ok is True, err

    def test_onnxruntime_excluded_check_returns_contract(self):
        from screencap.privacy_settings import _check_onnxruntime_excluded

        name, ok, err = _check_onnxruntime_excluded()
        assert name == "onnxruntime_excluded"
        # Env-robust on purpose: onnxruntime is typically importable in a dev
        # environment (ok=False) but excluded from the frozen binary (ok=True).
        # Assert only the contract shape so this is not flaky across both.
        assert isinstance(ok, bool)

    def test_both_checks_registered_in_cli_smoke_list(self):
        from screencap import cli as _cli
        from screencap.privacy_settings import (
            _check_domain_index,
            _check_onnxruntime_excluded,
        )

        assert _check_domain_index in _cli._SMOKE_CHECKS
        assert _check_onnxruntime_excluded in _cli._SMOKE_CHECKS
