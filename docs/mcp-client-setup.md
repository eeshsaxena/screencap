# Connecting an agent to ScreenCap (MCP)

`screencap mcp` is a local [Model Context Protocol](https://modelcontextprotocol.io)
server that lets an AI agent **search your ScreenCap recordings** — on-screen
text, audio transcripts, and an app/window timeline. It runs on your machine,
talks only to the local ScreenCap daemon over its UNIX socket, and returns **text
snippets plus `(recording, timestamp)` pointers — never screenshots or video**.

## Prerequisites

- ScreenCap installed and on your `PATH` (`screencap --version` works).
- At least one recording. On-screen **content** search additionally requires the
  content index to be enabled and recordings made while it was on — see
  [Enabling on-screen content search](#enabling-on-screen-content-search).

You do **not** need to start the daemon yourself: the first tool call
auto-spawns it if it is not already running.

## Register the server

The MCP client launches `screencap mcp` as a stdio subprocess. Add an entry
pointing at your `screencap` binary. **Use an absolute path** — MCP clients do
not inherit your interactive shell's `PATH`, so a bare `screencap` often fails to
launch. Find it with `which screencap`.

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "screencap": {
      "command": "/opt/homebrew/bin/screencap",
      "args": ["mcp"]
    }
  }
}
```

Restart Claude Desktop. "screencap" should appear in the tools list.

### Codex / other stdio clients

For a TOML-configured client (e.g. Codex `~/.codex/config.toml`):

```toml
[mcp_servers.screencap]
command = "/opt/homebrew/bin/screencap"
args = ["mcp"]
```

Any client that launches MCP servers as a `command` + `args` stdio subprocess
works the same way.

## What the agent can and cannot see

| Tool | Returns | Recall |
|------|---------|--------|
| `search_screen_content` | On-screen text snippets + `(recording, timestamp_ms)` | **Best-effort** |
| `search_transcript` | Audio-transcript snippets + `(recording, chunk_index)` + chunk timing | **Best-effort** |
| `query_timeline` | Structured app / window / time rows | **Authoritative** |
| `resolve_frame` | The nearest screenshot **stem** for a `(recording, timestamp_ms)` pointer (+ `encrypted` flag) | — |
| `read_frame` | Decrypted JPEG **bytes** (base64) for an ALLOW, scrubbed frame, when the corpus is encrypted | — |
| `list_recordings` | Recording names + metadata, incl. cloud `owner_uid` + `upload_warning` | — |
| `whoami` | The cloud account signed in on this machine (`uid` / `email`) | **Authoritative** |

- **Text and pointers only — never pixels.** No tool returns a screenshot or
  video frame. A search pointer is a recording name plus a timestamp the agent
  can mention back to you. `resolve_frame` turns such a pointer into the nearest
  screenshot **stem** (e.g. `1719400010.000000`) — a filename component, never a
  path or image bytes — which the agent expands to
  `~/.screencap/recordings/<recording>/screenshots/<stem>.jpg` and reads with the
  same-user filesystem access it already has.
- **`resolve_frame` is ALLOW-filtered.** It never resolves to a frame the privacy
  pipeline masked, excluded, or secure-field-redacted: a masked moment resolves to
  the nearest unmasked frame, or to a `null` miss. If the recording's privacy
  state can't be determined, it fails closed to a miss. This preserves the same
  ALLOW-only guarantee the content index gives (see `SECURITY.md`).
- **Encrypted corpora: read bytes via `read_frame`, not the path.** When search runs
  with the on-by-default guardrails, screenshots are stored encrypted
  (`<stem>.jpg.enc`) and `resolve_frame` returns `encrypted: true`. In that case the
  `.jpg` path won't be readable — call `read_frame(recording, stem)` and the daemon
  decrypts and serves the JPEG bytes (base64). `read_frame` applies the same
  ALLOW-only filter, additionally refuses a frame whose chunk hasn't been
  secrets-scrubbed yet (fail-closed), is size-capped, and is **audit-logged** per
  call. Same-user access, no biometric prompt for the agent (the app's own display
  gates on Touch ID separately) — see `SECURITY.md`.
- **Resolving a transcript hit is chunk-granular.** A transcript hit carries
  `timestamp_ms` (the chunk's start), `timestamp_granularity: "chunk"`, and
  `chunk_duration_ms`. Chunks default to 15 minutes and have no per-word timing,
  so pass `staleness_cap_ms = chunk_duration_ms` to `resolve_frame` to get a frame
  representative of the chunk. Its `delta_ms` is the offset from the chunk start —
  **not** the distance to the matched word. Content and timeline pointers carry an
  exact `timestamp_ms` and need no special cap.
- **Content recall is best-effort; the timeline is authoritative.** On-screen
  content comes from OCR over action-gated frames and is subject to OCR limits,
  frame de-duplication, and redaction — so "no result" is not "it never
  happened." The timeline is read straight from the event tables and has no such
  loss. Each response carries a coverage/`index_state` field so the agent can
  tell "no data" (`not_indexed` / `store_unavailable`) apart from "no match."
- **Same-machine, same-user.** The server gives the agent the same read access
  any program running as you already has (see `SECURITY.md`). It adds no network
  surface — it only talks to the local daemon socket.

## Enabling on-screen content search

`search_transcript` and `query_timeline` work on existing recordings with no
extra setup. `search_screen_content` needs the **content index**, which is
**off by default** and only populates recordings made while it is on.

Turn it on by setting the config flag, then make a recording:

```toml
# ~/.screencap/config.toml
content_index_enabled = true
```

or per-process: `SCREENCAP_CONTENT_INDEX=1`.

The index requires scrubbing to be enabled (it reuses the scrub pass to skip
secure-field / excluded-app frames). It is built locally during recording, is
**never uploaded**, and is purged when you delete a recording or retroactively
disable an app. Recordings made *before* enabling it are not retro-indexed
(backfill is future work).

## Troubleshooting

- **No "screencap" tools appear.** Check the `command` path is absolute and
  executable (`which screencap`). Check the client's MCP logs.
- **`search_screen_content` always returns `not_indexed`.** The content index is
  off, or the recording predates enabling it. See
  [Enabling on-screen content search](#enabling-on-screen-content-search).
- **Tools error with "could not reach the ScreenCap daemon."** A LaunchAgent is
  installed but its daemon is not running — kickstart it (`screencap serve
  --status` to check), or remove the LaunchAgent so the CLI can auto-spawn.
