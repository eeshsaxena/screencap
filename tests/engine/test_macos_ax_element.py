"""Tests for AX element resolution in screencap.engine.window._macos.

Covers ``get_active_element_state``'s hit-test path, which resolves the AX
element under a point via the Accessibility API directly (PyObjC).  These pin
the behaviour that the previous ``oa-atomacos`` wrapper could not express: it
raised on every non-success error code instead of returning, so the caller's
"no element here" branch was unreachable and every miss surfaced as a warning.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.macos_hw

# The marker alone is not enough: it deselects *after* collection, while the
# import below runs *during* it. On Linux `_macos` raises ImportError for the
# missing PyObjC bindings, which fails collection outright and takes the whole
# suite with it rather than skipping this module. Guarding the way
# `test_stats.py` and `test_comparison.py` already do keeps the skip a skip.
pytest.importorskip(
    "AppKit",
    exc_type=ImportError,
    reason="macOS PyObjC bindings are not available",
)

from screencap.engine.window import _macos

# Real AX error codes (ApplicationServices).
K_AX_ERROR_SUCCESS = 0
K_AX_ERROR_API_DISABLED = -25211
K_AX_ERROR_NO_VALUE = -25212


def _mock_ax(error_code: int = K_AX_ERROR_SUCCESS, element=None) -> MagicMock:
    """Build a mock ApplicationServices whose hit-test returns (code, element)."""
    mock_as = MagicMock()
    mock_as.AXUIElementCreateApplication.return_value = MagicMock(name="app_ref")
    mock_as.AXUIElementSetMessagingTimeout.return_value = None
    mock_as.AXUIElementCopyElementAtPosition.return_value = (error_code, element)
    # Read by the branch that distinguishes actionable permission failures.
    mock_as.kAXErrorAPIDisabled = K_AX_ERROR_API_DISABLED
    return mock_as


def _patched(mock_as: MagicMock, dump_state: MagicMock):
    """Patch the module globals the hit-test path touches.

    ``deepconvert_objc`` is stubbed to identity: it is a separate ObjC->Python
    concern, and its isinstance() checks require the real ApplicationServices
    types that ``mock_as`` replaces.
    """
    return patch.dict(
        _macos.__dict__,
        {
            "ApplicationServices": mock_as,
            "get_active_window_meta": lambda: {"kCGWindowOwnerPID": 501},
            "dump_state": dump_state,
            "deepconvert_objc": lambda value: value,
        },
    )


class TestGetActiveElementState:
    def test_returns_dumped_state_and_passes_the_raw_element_ref(self):
        """The hit-test result is handed to dump_state directly, not unwrapped."""
        element = MagicMock(name="ax_element")
        mock_as = _mock_ax(K_AX_ERROR_SUCCESS, element)
        dump_state = MagicMock(return_value={"AXRole": "AXButton"})

        with _patched(mock_as, dump_state):
            state = _macos.get_active_element_state(10, 20, max_depth=3)

        assert state == {"AXRole": "AXButton"}
        assert dump_state.call_args.args[0] is element
        # The anti-hang timeout must still be applied to the app ref.
        mock_as.AXUIElementSetMessagingTimeout.assert_called_once()

    def test_no_element_at_position_returns_empty_without_raising(self):
        """kAXErrorNoValue is the common benign miss — desktop, Canvas, WebGL."""
        mock_as = _mock_ax(K_AX_ERROR_NO_VALUE, None)
        dump_state = MagicMock()

        with _patched(mock_as, dump_state):
            state = _macos.get_active_element_state(10, 20)

        assert state == {}
        dump_state.assert_not_called()

    def test_accessibility_permission_denied_returns_empty(self):
        """kAXErrorAPIDisabled is actionable and must not be treated as a miss."""
        mock_as = _mock_ax(K_AX_ERROR_API_DISABLED, None)
        dump_state = MagicMock()

        with _patched(mock_as, dump_state), patch.object(_macos, "logger") as log:
            state = _macos.get_active_element_state(10, 20)

        assert state == {}
        dump_state.assert_not_called()
        assert log.warning.called, "permission failure should warn, not debug"

    def test_success_code_with_null_element_returns_empty(self):
        """A success code with no element must not reach dump_state."""
        mock_as = _mock_ax(K_AX_ERROR_SUCCESS, None)
        dump_state = MagicMock()

        with _patched(mock_as, dump_state):
            state = _macos.get_active_element_state(10, 20)

        assert state == {}
        dump_state.assert_not_called()

    def test_no_foreground_window_short_circuits(self):
        """A bare desktop yields {} before any AX call (SCR-103)."""
        mock_as = _mock_ax()
        dump_state = MagicMock()

        with patch.dict(
            _macos.__dict__,
            {
                "ApplicationServices": mock_as,
                "get_active_window_meta": lambda: {},
                "dump_state": dump_state,
            },
        ):
            state = _macos.get_active_element_state(10, 20)

        assert state == {}
        mock_as.AXUIElementCopyElementAtPosition.assert_not_called()
