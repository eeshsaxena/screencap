"""SCR-258 U9 (KTD-16/KTD-20) — MCP surface against a locked store.

Two contracts:

* AE3 (MCP arm): each MCP read tool run against a LOCKED store returns
  ``store_state=locked`` as DATA on its typed result — never a tool-call
  exception and never a silently-empty payload indistinguishable from "no
  recordings". The daemon read verbs return ``ok`` + empty results +
  ``store_state`` (the ``IndexState`` precedent), so the collapse to the generic
  ``DaemonError`` never fires for a locked store.
* KTD-16 anti-drift: the built MCP server exposes NO store-mutation tool (no
  lock/unlock, no storage mutation) — a headless agent cannot satisfy present-user
  auth, so pinning this blocks the prompt-injected-agent-unseals-the-vault exploit.
"""

from __future__ import annotations

import pytest

from screencap.mcp import server

pytestmark = pytest.mark.privacy


class _LockedDaemonClient:
    """A fake AsyncDaemonClient whose every read verb reports a locked store."""

    async def content_search(self, *a, **k):
        return {"hits": [], "index_state": "store_unavailable", "store_state": "locked"}

    async def transcript_search(self, *a, **k):
        return {"hits": [], "coverage": "best_effort", "store_state": "locked"}

    async def timeline_query(self, *a, **k):
        return {"rows": [], "coverage": "authoritative", "store_state": "locked"}

    async def frame_nearest(self, *a, **k):
        return {"stem": None, "delta_ms": None, "store_state": "locked"}

    async def list_recordings(self, *a, **k):
        return {"recordings": [], "store_state": "locked"}

    async def chat_answer(self, *a, **k):
        return {
            "answer": "The recordings store is locked.",
            "sources": [],
            "coverage": {"state": "store_unavailable", "note": "", "per_stream": {}},
            "refusal": True,
            "question_kind": "point",
            "target": "none",
            "store_state": "locked",
        }


@pytest.fixture
def _patch_locked_client(monkeypatch):
    async def _fake_client():
        return _LockedDaemonClient()

    monkeypatch.setattr(server, "_client", _fake_client)


async def test_content_search_reports_locked_as_data(_patch_locked_client):
    result = await server.search_screen_content("anything")
    assert result.store_state == "locked"
    assert result.hits == []


async def test_transcript_search_reports_locked_as_data(_patch_locked_client):
    result = await server.search_transcript("anything")
    assert result.store_state == "locked"
    assert result.hits == []


async def test_timeline_query_reports_locked_as_data(_patch_locked_client):
    result = await server.query_timeline()
    assert result.store_state == "locked"
    assert result.rows == []


async def test_resolve_frame_reports_locked_as_data(_patch_locked_client):
    result = await server.resolve_frame("rec", 1000)
    assert result.store_state == "locked"
    assert result.stem is None
    assert result.delta_ms is None


async def test_list_recordings_reports_locked_as_data(_patch_locked_client):
    result = await server.list_recordings()
    assert result.store_state == "locked"
    assert result.recordings == []


async def test_chat_answer_reports_locked_as_data(_patch_locked_client):
    result = await server.chat_answer("anything")
    assert result.store_state == "locked"
    assert result.refusal is True


async def test_no_read_tool_raises_on_locked_store(_patch_locked_client):
    """None of the read tools collapse a locked store into a tool exception."""
    # If any of these raised, pytest would fail the test — the point is that a
    # locked store is DATA, not an error.
    await server.search_screen_content("x")
    await server.search_transcript("x")
    await server.query_timeline()
    await server.resolve_frame("rec", 1000)
    await server.list_recordings()
    await server.chat_answer("x")


# ---------------------------------------------------------------------------
# KTD-16 anti-drift: the built server exposes NO store-mutation tool
# ---------------------------------------------------------------------------


def test_built_server_exposes_no_store_mutation_tool():
    srv = server.build_server()
    names = {t.name for t in srv._tool_manager.list_tools()}

    # The exact expected read-only tool set (pins additions too).
    assert names == {
        "search_screen_content",
        "search_transcript",
        "query_timeline",
        "resolve_frame",
        "read_frame",
        "list_recordings",
        "whoami",
        "chat_answer",
    }

    # No store-mutation tool by any plausible name — a headless agent must never be
    # able to lock/unlock or otherwise mutate the store (prompt-injection defense).
    forbidden_substrings = ("lock", "unlock", "seal", "storage", "migrate", "detach")
    for name in names:
        lowered = name.lower()
        assert not any(s in lowered for s in forbidden_substrings), (
            f"MCP tool {name!r} looks like a store-mutation surface (KTD-16)"
        )
