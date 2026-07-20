"""Tests for the server-derived ``started_by`` classifier (Phase 2 U2.1).

The classifier function ``classify_path_and_argv`` is pure — exercise
it directly without needing macOS-only ctypes plumbing. The
``derive_started_by`` socket-level entry point delegates to it after
resolving peer PID + proc path + argv via macOS APIs that don't exist
on the Linux CI runner.
"""

from __future__ import annotations

from screencap.daemon import provenance


def test_swiftui_app_bundle_path_classifies_as_swiftui():
    assert provenance.classify_path_and_argv(
        "/Applications/Screencap.app/Contents/MacOS/screencap",
        ["screencap", "list"],
    ) == provenance.STARTED_BY_SWIFTUI


def test_swiftui_user_install_bundle_classifies_as_swiftui():
    assert provenance.classify_path_and_argv(
        "/Users/alice/Applications/Screencap.app/Contents/MacOS/screencap",
        ["screencap"],
    ) == provenance.STARTED_BY_SWIFTUI


def test_cli_subcommand_classifies_as_cli():
    assert provenance.classify_path_and_argv(
        "/opt/homebrew/bin/screencap",
        ["screencap", "start", "--name", "demo"],
    ) == provenance.STARTED_BY_CLI


def test_mcp_subcommand_classifies_as_mcp():
    # Same binary path, only argv distinguishes the two — that's the
    # whole point of the KERN_PROCARGS2 path picked at U2 impl time.
    assert provenance.classify_path_and_argv(
        "/opt/homebrew/bin/screencap",
        ["screencap", "mcp"],
    ) == provenance.STARTED_BY_MCP


def test_cli_status_and_stop_subcommands_also_classify_as_cli():
    assert provenance.classify_path_and_argv(
        "/usr/local/bin/screencap",
        ["screencap", "status", "--json"],
    ) == provenance.STARTED_BY_CLI
    assert provenance.classify_path_and_argv(
        "/usr/local/bin/screencap",
        ["screencap", "stop"],
    ) == provenance.STARTED_BY_CLI


def test_unknown_path_with_unrecognized_verb_classifies_as_unknown():
    assert provenance.classify_path_and_argv(
        "/usr/local/bin/screencap",
        ["screencap", "weird-future-verb"],
    ) == provenance.STARTED_BY_UNKNOWN


def test_bare_invocation_with_no_argv_classifies_as_unknown():
    assert provenance.classify_path_and_argv(
        "/opt/homebrew/bin/screencap",
        [],
    ) == provenance.STARTED_BY_UNKNOWN


def test_bare_invocation_one_argv_entry_classifies_as_cli():
    """When argv carries only argv[0] (no subcommand), treat as CLI."""
    assert provenance.classify_path_and_argv(
        "/opt/homebrew/bin/screencap",
        ["screencap"],
    ) == provenance.STARTED_BY_CLI


def test_none_path_with_empty_argv_classifies_as_unknown():
    assert provenance.classify_path_and_argv(None, []) == provenance.STARTED_BY_UNKNOWN


def test_swiftui_path_wins_over_argv():
    """If the path matches the SwiftUI bundle, the classifier picks
    swiftui regardless of what the bundled CLI was invoked with."""
    assert provenance.classify_path_and_argv(
        "/Applications/Screencap.app/Contents/MacOS/screencap",
        ["screencap", "mcp"],
    ) == provenance.STARTED_BY_SWIFTUI


def test_classifier_is_case_insensitive_on_path():
    """Filesystem on macOS is case-insensitive by default; the
    classifier should not break on a path with mixed case."""
    assert provenance.classify_path_and_argv(
        "/Applications/SCREENCAP.app/Contents/MacOS/screencap",
        ["screencap"],
    ) == provenance.STARTED_BY_SWIFTUI


def test_derive_started_by_falls_through_to_unknown_on_invalid_fd(monkeypatch):
    """Linux-side smoke test: the ctypes plumbing for getsockopt(SOL_LOCAL,
    LOCAL_PEEREPID) won't resolve a peer for a stdin-style fd. ``derive_started_by``
    must not raise; it returns ``unknown``."""
    # Force the libc calls to behave as if peer lookup failed.
    monkeypatch.setattr(provenance, "_get_peer_pid", lambda _fd: None)
    assert provenance.derive_started_by(0) == provenance.STARTED_BY_UNKNOWN


def test_derive_started_by_chains_through_classifier(monkeypatch):
    """When the peer PID resolves, the function feeds path + argv to
    the classifier and returns its verdict."""
    monkeypatch.setattr(provenance, "_get_peer_pid", lambda _fd: 4242)
    monkeypatch.setattr(provenance, "_get_proc_path", lambda _pid: "/opt/homebrew/bin/screencap")
    monkeypatch.setattr(provenance, "_get_proc_argv", lambda _pid: ["screencap", "mcp"])
    assert provenance.derive_started_by(99) == provenance.STARTED_BY_MCP


def test_derive_started_by_handles_dead_peer(monkeypatch):
    """If proc_pidpath returns None (peer exited between accept and probe),
    the classifier still produces a defensible result via argv-only logic.
    When both fail, fall through to unknown."""
    monkeypatch.setattr(provenance, "_get_peer_pid", lambda _fd: 4242)
    monkeypatch.setattr(provenance, "_get_proc_path", lambda _pid: None)
    monkeypatch.setattr(provenance, "_get_proc_argv", lambda _pid: [])
    assert provenance.derive_started_by(99) == provenance.STARTED_BY_UNKNOWN
