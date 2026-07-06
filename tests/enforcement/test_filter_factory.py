"""Tests for the privacy filter factory in ``screencap.enforcement.window_filter``.

Covers Unit 1 of the unified-export-callable refactor:

- ``build_cloud_window_filter`` factory: returns ``None`` for non-cloud,
  cloud-mode filter for cloud (forces PUBLIC mode).
- ``build_privacy_filter`` null-bundle-id fail-closed (R17 regression):
  None / empty / whitespace-only ``app_bundle_id`` → suppressed.
- Load-bearing architecture invariants: EXCLUDE → suppressed; MASK_WINDOW
  → ``window_title == app_name`` AND ``domain is None``; ALLOW → unchanged.
- ``.menubar_overrides.json``: missing file is fine; malformed JSON falls
  back to policy evaluation without raising.
- Import paths: ``build_privacy_filter`` and ``build_cloud_window_filter``
  are importable from the canonical ``screencap.enforcement.window_filter``
  location (the SCR-33 U3 home). The historical ``screencap.exporter``
  re-export was removed in todo 019.
"""

from __future__ import annotations

import pytest

import json
import re
from unittest.mock import patch

from screencap.engine.events import WindowSwitchEvent
from screencap.enforcement.window_filter import (
    build_cloud_window_filter,
    build_privacy_filter,
)
from screencap.privacy.policy import ContextClass, PrivacyConfig, PrivacyMode

pytestmark = pytest.mark.privacy


def _public_config():
    """Patch ``get_privacy_config`` to return a clean PUBLIC-mode config.

    Avoids picking up whatever the developer has in ``~/.screencap/config.toml``
    so test outcomes are deterministic.
    """
    return patch(
        "screencap.config.get_privacy_config",
        return_value=PrivacyConfig(mode=PrivacyMode.PUBLIC),
    )


def _make_event(
    *,
    bundle_id: str | None = "com.apple.Terminal",
    app_name: str = "Terminal",
    window_title: str = "bash — 80x24",
    domain: str | None = None,
) -> WindowSwitchEvent:
    return WindowSwitchEvent(
        timestamp=1000.0,
        app_name=app_name,
        app_bundle_id=bundle_id,
        window_title=window_title,
        window_id="100",
        x=0,
        y=0,
        width=1512,
        height=982,
        domain=domain,
    )


# ---------------------------------------------------------------------------
# build_cloud_window_filter — factory dispatch
# ---------------------------------------------------------------------------


class TestCloudWindowFilterFactory:
    def test_cloud_bound_false_returns_none(self, tmp_path):
        """``cloud_bound=False`` returns ``None`` so callers can wire
        the factory unconditionally without an ``if/else``."""
        result = build_cloud_window_filter(
            cloud_bound=False,
            privacy_mode="internal",
            capture_dir=tmp_path,
        )
        assert result is None

    def test_cloud_bound_true_returns_callable(self, tmp_path):
        """``cloud_bound=True`` returns a callable filter."""
        with _public_config():
            pf = build_cloud_window_filter(
                cloud_bound=True,
                privacy_mode="internal",
                capture_dir=tmp_path,
            )
        assert pf is not None
        assert callable(pf)

    def test_cloud_bound_true_forces_public_mode(self, tmp_path):
        """``cloud_bound=True`` ignores the supplied ``privacy_mode`` and
        applies PUBLIC mode (cloud_intent → PUBLIC override). Slack
        (CHAT) is TEXT_REDACT under INTERNAL but MASK_WINDOW under PUBLIC."""
        with _public_config():
            pf = build_cloud_window_filter(
                cloud_bound=True,
                privacy_mode="internal",
                capture_dir=tmp_path,
            )

        slack_event = _make_event(
            bundle_id="com.tinyspeck.slackmacgap",
            app_name="Slack",
            window_title="#secret-channel — Slack",
        )
        result = pf(slack_event)

        # PUBLIC mode for CHAT → MASK_WINDOW → title masked to app_name,
        # domain nulled. Under INTERNAL the title would have passed through.
        assert result is not None
        assert result.window_title == "Slack"
        assert result.domain is None


# ---------------------------------------------------------------------------
# R17: null/empty/whitespace bundle_id is fail-closed
# ---------------------------------------------------------------------------


class TestNullBundleIdFailClosed:
    """Regression suite for the pre-existing fail-open at
    ``docs/tickets/2026-03-20-fix-null-bundle-id-privacy-leak.md``.

    Before R17, a window event with ``app_bundle_id=None`` reached the
    classifier as the empty string, was classified as UNKNOWN, and most
    modes routed UNKNOWN to ALLOW — leaking the original window title.
    """

    def test_none_bundle_id_returns_none(self, tmp_path):
        with _public_config():
            pf = build_privacy_filter(
                privacy_mode="public", capture_dir=tmp_path,
            )
        event = _make_event(
            bundle_id=None,
            app_name="Passwords",
            window_title="Passwords Passwords",
        )
        assert pf(event) is None

    def test_empty_string_bundle_id_returns_none(self, tmp_path):
        with _public_config():
            pf = build_privacy_filter(
                privacy_mode="public", capture_dir=tmp_path,
            )
        event = _make_event(
            bundle_id="",
            app_name="Passwords",
            window_title="Passwords Passwords",
        )
        assert pf(event) is None

    def test_whitespace_only_bundle_id_returns_none(self, tmp_path):
        with _public_config():
            pf = build_privacy_filter(
                privacy_mode="public", capture_dir=tmp_path,
            )
        event = _make_event(
            bundle_id="   ",
            app_name="Passwords",
            window_title="Passwords Passwords",
        )
        assert pf(event) is None

    def test_cloud_factory_also_rejects_null_bundle_id(self, tmp_path):
        """The factory inherits the fail-closed guard (it composes
        ``build_privacy_filter`` underneath)."""
        with _public_config():
            pf = build_cloud_window_filter(
                cloud_bound=True,
                privacy_mode="internal",
                capture_dir=tmp_path,
            )
        event = _make_event(
            bundle_id=None,
            app_name="Slack",
            window_title="#secret-channel — Slack",
        )
        assert pf(event) is None


# ---------------------------------------------------------------------------
# Action-matrix outcomes — load-bearing architecture invariants
# ---------------------------------------------------------------------------


class TestActionMatrixInvariants:
    def test_exclude_app_returns_none(self, tmp_path):
        """1Password (PASSWORD_MANAGER) → EXCLUDE in every mode → None."""
        with _public_config():
            pf = build_privacy_filter(
                privacy_mode="public", capture_dir=tmp_path,
            )
        event = _make_event(
            bundle_id="com.1password.1password",
            app_name="1Password",
            window_title="1Password — Vault",
        )
        assert pf(event) is None

    def test_mask_window_nulls_title_and_domain(self, tmp_path):
        """MASK_WINDOW must replace ``window_title`` with ``app_name`` AND
        null ``domain``. Both halves are load-bearing."""
        with _public_config():
            pf = build_privacy_filter(
                privacy_mode="public", capture_dir=tmp_path,
            )
        slack_event = _make_event(
            bundle_id="com.tinyspeck.slackmacgap",
            app_name="Slack",
            window_title="#secret-channel — Slack",
            domain="slack.com",
        )
        result = pf(slack_event)
        assert result is not None
        # Compare against the input's app_name rather than a hardcoded string
        # so a regression in dict_to_window_switch's app_name derivation
        # (e.g., bundle "com.tinyspeck.slackmacgap" → "Slackmacgap") would
        # surface here. Mirrors test_chunk_processor.py:885.
        assert result.window_title == slack_event.app_name
        assert result.domain is None
        # Other fields preserved
        assert result.app_bundle_id == "com.tinyspeck.slackmacgap"
        assert result.window_id == "100"

    def test_allow_app_passes_through_unchanged(self, tmp_path):
        """Terminal (CODE_EDITOR_TERMINAL + INTERNAL → ALLOW) is returned
        as-is, including the original title."""
        with _public_config():
            pf = build_privacy_filter(
                privacy_mode="internal", capture_dir=tmp_path,
            )
        event = _make_event(
            bundle_id="com.apple.Terminal",
            app_name="Terminal",
            window_title="bash — 80x24",
        )
        result = pf(event)
        assert result is not None
        assert result.window_title == "bash — 80x24"
        assert result.app_bundle_id == "com.apple.Terminal"


# ---------------------------------------------------------------------------
# .menubar_overrides.json — present / absent / malformed
# ---------------------------------------------------------------------------


class TestMenubarOverridesLoading:
    def test_no_overrides_file_present(self, tmp_path):
        """Absence of ``.menubar_overrides.json`` is the common case and
        must not raise. The filter still works via policy evaluation."""
        assert not (tmp_path / ".menubar_overrides.json").exists()

        with _public_config():
            pf = build_privacy_filter(
                privacy_mode="internal", capture_dir=tmp_path,
            )
        event = _make_event(
            bundle_id="com.apple.Terminal",
            app_name="Terminal",
            window_title="bash — 80x24",
        )
        # Falls through to matrix → ALLOW (Terminal in INTERNAL).
        assert pf(event) is not None

    def test_malformed_overrides_file_does_not_raise(self, tmp_path):
        """Garbage JSON in the overrides file is logged at debug and
        ignored — the filter falls back to the policy matrix."""
        (tmp_path / ".menubar_overrides.json").write_text(
            "this is not valid json {"
        )

        with _public_config():
            pf = build_privacy_filter(
                privacy_mode="internal", capture_dir=tmp_path,
            )
        event = _make_event(
            bundle_id="com.apple.Terminal",
            app_name="Terminal",
            window_title="bash — 80x24",
        )
        # No exception; filter still produces a verdict from the matrix.
        assert pf(event) is not None

    def test_capture_dir_none_skips_override_loading(self):
        """``capture_dir=None`` skips override loading entirely (used by
        tests and ad-hoc callers)."""
        with _public_config():
            pf = build_privacy_filter(
                privacy_mode="public", capture_dir=None,
            )
        event = _make_event(
            bundle_id="com.apple.Terminal",
            app_name="Terminal",
            window_title="bash — 80x24",
        )
        # Terminal under PUBLIC → TEXT_REDACT → passes through.
        assert pf(event) is not None

    def test_override_exclude_is_honored(self, tmp_path):
        """When the overrides file marks an app as ``exclude``, the
        filter returns None even if the matrix would have allowed it."""
        overrides = {"com.microsoft.VSCode": "exclude"}
        (tmp_path / ".menubar_overrides.json").write_text(json.dumps(overrides))

        with _public_config():
            pf = build_privacy_filter(
                privacy_mode="internal", capture_dir=tmp_path,
            )
        event = _make_event(
            bundle_id="com.microsoft.VSCode",
            app_name="Visual Studio Code",
            window_title="main.py",
        )
        assert pf(event) is None


# ---------------------------------------------------------------------------
# Integration — import paths
# ---------------------------------------------------------------------------


class TestImportPaths:
    """``build_privacy_filter`` and ``build_cloud_window_filter`` are
    importable from the canonical ``screencap.enforcement.window_filter``
    location (the SCR-33 U3 home). The legacy ``screencap.exporter``
    re-export was removed in todo 019; ``screencap.enforcement.window_filter``
    is now the single source of truth.
    """

    def test_import_from_privacy_filter_module(self):
        from screencap.enforcement.window_filter import (  # noqa: F401
            build_cloud_window_filter,
            build_privacy_filter,
        )

    def test_chunk_processor_import_path_works(self):
        """Chunk processor imports from the canonical location."""
        from screencap.enforcement.window_filter import build_privacy_filter

        with _public_config():
            pf = build_privacy_filter(
                privacy_mode="internal", cloud_intent=True,
            )
        assert callable(pf)


# ---------------------------------------------------------------------------
# Sanity: cloud_intent=True still forces PUBLIC even on direct call
# ---------------------------------------------------------------------------


class TestCloudIntentOverride:
    """Verifies the load-bearing ``cloud_intent → PUBLIC`` override at
    ``filter.py:`` is preserved verbatim from its original location at
    ``exporter.py:142-143``. Slack is the canonical case post-Unit-7a:
    CHAT under INTERNAL is MASK_WINDOW (title masked). Under PUBLIC it
    is also MASK_WINDOW. Both modes mask the title; the cloud_intent
    uplift is invisible for CHAT specifically — see other classes for
    a visible internal-vs-public differential."""

    def test_internal_mode_masks_chat_window_title(self, tmp_path):
        """Post-Unit-7a: CHAT under internal = MASK_WINDOW. Slack title
        masked to app_name regardless of cloud_intent."""
        with _public_config():
            pf = build_privacy_filter(
                privacy_mode="internal",
                cloud_intent=False,
                capture_dir=tmp_path,
            )
        event = _make_event(
            bundle_id="com.tinyspeck.slackmacgap",
            app_name="Slack",
            window_title="#secret-channel — Slack",
        )
        result = pf(event)
        assert result is not None
        assert result.window_title == "Slack"

    def test_internal_mode_with_cloud_intent_masks_chat(self, tmp_path):
        with _public_config():
            pf = build_privacy_filter(
                privacy_mode="internal",
                cloud_intent=True,
                capture_dir=tmp_path,
            )
        event = _make_event(
            bundle_id="com.tinyspeck.slackmacgap",
            app_name="Slack",
            window_title="#secret-channel — Slack",
        )
        result = pf(event)
        assert result is not None
        assert result.window_title == "Slack"
        assert result.domain is None


# ---------------------------------------------------------------------------
# Todo 005: cloud_intent=True must skip .menubar_overrides.json entirely
# ---------------------------------------------------------------------------


class TestCloudIntentSkipsMenubarOverrides:
    """Cloud-bound exports MUST NOT honor ``.menubar_overrides.json``.

    The override file is user-writable in the recording directory and was
    designed for capture-time / local-recording posture. Without this guard,
    an ``allow`` entry for a chat app (e.g. Slack) would bypass the matrix's
    MASK_WINDOW decision and leak the original window title into cloud-bound
    JSONL — defeating the cloud-intent → PUBLIC mode escalation.

    Local (non-cloud) behavior is unchanged: overrides still apply.
    """

    def _slack_event(self):
        return _make_event(
            bundle_id="com.tinyspeck.slackmacgap",
            app_name="Slack",
            window_title="#secret-channel — Slack",
        )

    def test_cloud_bound_ignores_allow_override_for_chat(self, tmp_path):
        """``cloud_bound=True`` + ``.menubar_overrides.json`` with an
        ``allow`` entry for Slack — title is the masked app name, not
        the original (matrix MASK_WINDOW verdict applied)."""
        overrides = {"com.tinyspeck.slackmacgap": "allow"}
        (tmp_path / ".menubar_overrides.json").write_text(json.dumps(overrides))

        with _public_config():
            pf = build_cloud_window_filter(
                cloud_bound=True,
                privacy_mode="internal",
                capture_dir=tmp_path,
            )

        slack_event = self._slack_event()
        result = pf(slack_event)
        assert result is not None
        # Compare against the event's app_name rather than hardcoding
        # "Slack" so the assertion stays accurate if MASK_WINDOW
        # ever changes its replacement strategy.
        assert result.window_title == slack_event.app_name
        assert result.domain is None

    def test_local_filter_still_honors_allow_override(self, tmp_path):
        """``cloud_bound=False`` (and direct ``cloud_intent=False``):
        the override file is still loaded and ``allow`` still bypasses
        matrix verdicts. This preserves the local-recording UX where
        users can toggle apps via the menubar."""
        overrides = {"com.tinyspeck.slackmacgap": "allow"}
        (tmp_path / ".menubar_overrides.json").write_text(json.dumps(overrides))

        with _public_config():
            pf = build_privacy_filter(
                privacy_mode="public",
                cloud_intent=False,
                capture_dir=tmp_path,
            )

        result = pf(self._slack_event())
        # ``allow`` override returns the event unchanged — original title
        # passes through (under PUBLIC matrix Slack would be MASK_WINDOW).
        assert result is not None
        assert result.window_title == "#secret-channel — Slack"

    def test_cloud_bound_ignores_exclude_override_too(self, tmp_path):
        """Recommended posture (Option A): ``cloud_intent=True`` skips
        ALL overrides — including ``exclude`` entries that would tighten
        the matrix verdict. Cloud-bound posture is fully driven by the
        matrix; if a user wants to exclude an app from cloud, they must
        configure ``exclude_apps`` in ``config.toml``.

        The asymmetry: ``exclude`` overrides could in principle still
        tighten (they only restrict, never loosen the matrix), but the
        simpler "skip ALL overrides for cloud" rule is preferred — it
        makes the cloud boundary trivially auditable. We pick a
        non-EXCLUDE-by-matrix bundle (Visual Studio Code → TEXT_REDACT
        under PUBLIC) so the test verifies the override is NOT applied
        (the event would be suppressed if it were applied)."""
        overrides = {"com.microsoft.VSCode": "exclude"}
        (tmp_path / ".menubar_overrides.json").write_text(json.dumps(overrides))

        with _public_config():
            pf = build_cloud_window_filter(
                cloud_bound=True,
                privacy_mode="internal",
                capture_dir=tmp_path,
            )

        event = _make_event(
            bundle_id="com.microsoft.VSCode",
            app_name="Visual Studio Code",
            window_title="main.py",
        )
        result = pf(event)
        # Exclude override NOT applied — VSCode under PUBLIC mode is
        # TEXT_REDACT, which passes through unchanged via the matrix.
        # If the override had been honored, result would be None.
        assert result is not None

    def test_cloud_bound_with_no_override_file_works(self, tmp_path):
        """No override file at all + ``cloud_bound=True`` — sanity check
        that the override-skip path doesn't raise on a missing file."""
        assert not (tmp_path / ".menubar_overrides.json").exists()

        with _public_config():
            pf = build_cloud_window_filter(
                cloud_bound=True,
                privacy_mode="internal",
                capture_dir=tmp_path,
            )

        slack_event = self._slack_event()
        result = pf(slack_event)
        assert result is not None
        assert result.window_title == slack_event.app_name


# ---------------------------------------------------------------------------
# P3: cloud_intent=True must neutralize ``allow_apps`` from PrivacyConfig
# ---------------------------------------------------------------------------


class TestCloudIntentSkipsAllowApps:
    """Cloud-bound exports MUST NOT honor ``[privacy].allow_apps`` from
    ``config.toml``.

    ``DefaultPolicyEvaluator.evaluate`` lets ``allow_apps`` win over the
    matrix unless the matrix verdict is EXCLUDE — so ``allow_apps``
    containing a chat app (e.g. Slack, ``CHAT`` → MASK_WINDOW under PUBLIC)
    would otherwise return ALLOW and leak the original window title into
    cloud-bound JSONL. This is the same loosening vector as the menubar
    override fix (todo 005), but at the policy-config layer.

    Other config knobs (``exclude_apps``, ``mask_domains``,
    ``mask_title_patterns``) only TIGHTEN the matrix and remain in effect
    for cloud-bound exports.
    """

    def _slack_event(self):
        return _make_event(
            bundle_id="com.tinyspeck.slackmacgap",
            app_name="Slack",
            window_title="#secret-channel — Slack",
            domain="slack.com",
        )

    def test_cloud_bound_neutralizes_allow_apps_for_chat(self, tmp_path):
        """Cloud-bound + ``allow_apps=['com.tinyspeck.slackmacgap']`` →
        Slack title still masked. The matrix verdict (CHAT, PUBLIC =
        MASK_WINDOW) wins because cloud_intent neutralizes ``allow_apps``."""
        cfg = PrivacyConfig(
            mode=PrivacyMode.INTERNAL,
            allow_apps=frozenset(["com.tinyspeck.slackmacgap"]),
        )
        with patch(
            "screencap.config.get_privacy_config", return_value=cfg,
        ):
            pf = build_cloud_window_filter(
                cloud_bound=True,
                privacy_mode="internal",
                capture_dir=tmp_path,
            )

        slack_event = self._slack_event()
        result = pf(slack_event)
        # MASK_WINDOW posture: title replaced with app_name, domain nulled.
        # If allow_apps had won, the original title would have passed through.
        assert result is not None
        assert result.window_title == slack_event.app_name
        assert result.domain is None

    def test_local_filter_respects_matrix_floor_over_allow_apps(self, tmp_path):
        """Non-cloud (``cloud_intent=False``) + ``allow_apps`` containing
        Slack under PUBLIC mode → title still masked. The runtime
        evaluator's strictness floor (Finding 3) blocks ``allow_apps`` from
        loosening matrix MASK_WINDOW / TEXT_REDACT just like the CLI add-time
        guard rejects new additions. An EXISTING allow_apps entry from
        before that floor was added must NOT silently bypass the matrix."""
        cfg = PrivacyConfig(
            mode=PrivacyMode.PUBLIC,
            allow_apps=frozenset(["com.tinyspeck.slackmacgap"]),
        )
        with patch(
            "screencap.config.get_privacy_config", return_value=cfg,
        ):
            pf = build_privacy_filter(
                privacy_mode="public",
                cloud_intent=False,
                capture_dir=tmp_path,
            )

        slack_event = self._slack_event()
        result = pf(slack_event)
        # MASK_WINDOW wins over allow_apps. Title masked to app_name,
        # domain nulled.
        assert result is not None
        assert result.window_title == "Slack"
        assert result.domain is None

    def test_cloud_bound_still_honors_exclude_apps(self, tmp_path):
        """Regression: ``exclude_apps`` is a TIGHTENING knob and must
        still apply for cloud-bound exports. VSCode under PUBLIC matrix
        is TEXT_REDACT (passes through), but with ``exclude_apps`` set it
        is dropped."""
        cfg = PrivacyConfig(
            mode=PrivacyMode.INTERNAL,
            exclude_apps=frozenset(["com.microsoft.VSCode"]),
        )
        with patch(
            "screencap.config.get_privacy_config", return_value=cfg,
        ):
            pf = build_cloud_window_filter(
                cloud_bound=True,
                privacy_mode="internal",
                capture_dir=tmp_path,
            )

        event = _make_event(
            bundle_id="com.microsoft.VSCode",
            app_name="Visual Studio Code",
            window_title="main.py",
        )
        # exclude_apps wins (highest precedence) — event suppressed.
        assert pf(event) is None

    def test_cloud_bound_still_honors_mask_domains(self, tmp_path):
        """Regression: ``mask_domains`` is a TIGHTENING knob and must
        still apply for cloud-bound exports. Chrome on a domain in
        ``mask_domains`` → MASK_WINDOW (forced)."""
        cfg = PrivacyConfig(
            mode=PrivacyMode.INTERNAL,
            mask_domains=frozenset(["example.com"]),
            app_classes={
                "com.google.Chrome": ContextClass.BROWSER_UNVERIFIED,
            },
        )
        with patch(
            "screencap.config.get_privacy_config", return_value=cfg,
        ):
            pf = build_cloud_window_filter(
                cloud_bound=True,
                privacy_mode="internal",
                capture_dir=tmp_path,
            )

        event = _make_event(
            bundle_id="com.google.Chrome",
            app_name="Google Chrome",
            window_title="Example — Chrome",
            domain="example.com",
        )
        result = pf(event)
        # mask_domains forces at least MASK_WINDOW — title masked, domain nulled.
        assert result is not None
        assert result.window_title == event.app_name
        assert result.domain is None

    def test_cloud_bound_still_honors_mask_title_patterns(self, tmp_path):
        """Regression: ``mask_title_patterns`` is a TIGHTENING knob and
        must still apply for cloud-bound exports. A title matching a
        configured pattern → MASK_WINDOW (forced)."""
        cfg = PrivacyConfig(
            mode=PrivacyMode.INTERNAL,
            mask_title_patterns=(re.compile(r"\bSECRET\b"),),
        )
        with patch(
            "screencap.config.get_privacy_config", return_value=cfg,
        ):
            pf = build_cloud_window_filter(
                cloud_bound=True,
                privacy_mode="internal",
                capture_dir=tmp_path,
            )

        event = _make_event(
            bundle_id="com.apple.Terminal",
            app_name="Terminal",
            window_title="bash — SECRET project",
        )
        result = pf(event)
        # title pattern forces MASK_WINDOW — title masked to app_name.
        assert result is not None
        assert result.window_title == event.app_name
        assert result.domain is None

    def test_cloud_bound_browser_in_allow_apps_still_neutralized(self, tmp_path):
        """Browser-refinement edge case: even if a non-chat bundle were
        marked as ``BROWSER_UNVERIFIED`` and put in ``allow_apps``, the
        cloud-bound filter neutralizes ``allow_apps`` so the matrix
        decides. Sanity check that the browser-refinement code path in
        ``DefaultPolicyEvaluator`` is moot when ``allow_apps`` is empty."""
        cfg = PrivacyConfig(
            mode=PrivacyMode.INTERNAL,
            allow_apps=frozenset(["com.tinyspeck.slackmacgap"]),
            # Silly classification but exercises the browser-refinement branch.
            app_classes={
                "com.tinyspeck.slackmacgap": ContextClass.BROWSER_UNVERIFIED,
            },
        )
        with patch(
            "screencap.config.get_privacy_config", return_value=cfg,
        ):
            pf = build_cloud_window_filter(
                cloud_bound=True,
                privacy_mode="internal",
                capture_dir=tmp_path,
            )

        slack_event = self._slack_event()
        result = pf(slack_event)
        # BROWSER_UNVERIFIED under PUBLIC is MASK_WINDOW. With allow_apps
        # neutralized, the matrix decides → title masked.
        assert result is not None
        assert result.window_title == slack_event.app_name
        assert result.domain is None


# ---------------------------------------------------------------------------
# SCR-235 KTD5: cloud posture honors confirmed allow entries only
# ---------------------------------------------------------------------------


class TestCloudConfirmedAllowShaping:
    def _config(self, **kwargs):
        return patch(
            "screencap.config.get_privacy_config",
            return_value=PrivacyConfig(mode=PrivacyMode.INTERNAL, **kwargs),
        )

    def test_confirmed_app_passes_cloud_filter_unmasked(self, tmp_path):
        """Covers AE1 (cloud half): a confirmed chat app's window title
        survives into cloud-bound events — the confirmed allow beats the
        forced-PUBLIC matrix."""
        with self._config(
            allow_apps=frozenset({"com.tinyspeck.slackmacgap"}),
            confirmed_allow_apps=frozenset({"com.tinyspeck.slackmacgap"}),
        ):
            pf = build_cloud_window_filter(
                cloud_bound=True, privacy_mode="internal", capture_dir=tmp_path,
            )
        event = _make_event(
            bundle_id="com.tinyspeck.slackmacgap",
            app_name="Slack",
            window_title="#secret-channel — Slack",
        )
        result = pf(event)
        assert result is not None
        assert result.window_title == "#secret-channel — Slack"

    def test_legacy_allow_still_masked_for_cloud(self, tmp_path):
        """A legacy (unconfirmed) allow entry is dropped from cloud posture
        exactly as before: the matrix masks the title."""
        with self._config(
            allow_apps=frozenset({"com.tinyspeck.slackmacgap"}),
        ):
            pf = build_cloud_window_filter(
                cloud_bound=True, privacy_mode="internal", capture_dir=tmp_path,
            )
        event = _make_event(
            bundle_id="com.tinyspeck.slackmacgap",
            app_name="Slack",
            window_title="#secret-channel — Slack",
        )
        result = pf(event)
        assert result is not None
        assert result.window_title == "Slack"
        assert result.domain is None

    def test_confirmed_password_manager_passes_cloud_filter(self, tmp_path):
        """R1 holds in the forced-PUBLIC cloud path: a confirmed
        EXCLUDE-class app's events are kept, not dropped."""
        with self._config(
            allow_apps=frozenset({"com.1password.1password"}),
            confirmed_allow_apps=frozenset({"com.1password.1password"}),
        ):
            pf = build_cloud_window_filter(
                cloud_bound=True, privacy_mode="internal", capture_dir=tmp_path,
            )
        event = _make_event(
            bundle_id="com.1password.1password",
            app_name="1Password",
            window_title="Vault — 1Password",
        )
        result = pf(event)
        assert result is not None
        assert result.window_title == "Vault — 1Password"

    def test_unlisted_sensitive_app_still_excluded_for_cloud(self, tmp_path):
        """Covers AE3 (cloud half): unlisted 1Password stays excluded."""
        with self._config():
            pf = build_cloud_window_filter(
                cloud_bound=True, privacy_mode="internal", capture_dir=tmp_path,
            )
        event = _make_event(
            bundle_id="com.1password.1password",
            app_name="1Password",
            window_title="Vault — 1Password",
        )
        assert pf(event) is None
