"""SCR-118 ``screencap mcp`` — a thin MCP stdio server over the daemon API.

The server holds no query logic of its own: each MCP tool forwards to a daemon
``/v0/*`` read verb over the daemon's UNIX socket and re-wraps the response as a
typed, pointer-only result. See ``server.py`` for the FastMCP app and ``_client``
for the async UDS client.
"""
