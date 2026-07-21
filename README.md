# Screencap

Screencap is a private screen memory for your Mac. It records what you work on — screen, input, audio, window context — keeps everything on your machine, and makes it searchable by you and by your AI agents. Nothing leaves your Mac unless you sign in and say so.

It ships two ways: a native macOS app (the easiest way to use it) and a CLI with a background daemon (the engine underneath, useful on its own for scripting and headless setups).

## Why Screencap

Screen recorders that promise "local and private" usually mean *we redact things afterwards*. Screencap's privacy is enforced while recording, and you can verify every claim in this repo:

- **Capture-time blocking, fail-closed.** A real-time filter watches the foreground window and blocks capture for sensitive apps *before* anything is written — excluded screenshots never touch disk, keystrokes are nulled, video frames are dropped. The filter starts blocked and stays blocked on errors, so failure means less capture, never more.
- **Password managers are excluded by default, in every mode.** Banking, login, and checkout pages are excluded from cloud-bound recordings and masked in personal ones — driven by a context-classification matrix you can inspect and override per app (overriding an excluded category takes an explicit confirmation step).
- **Nothing uploads without your say-so.** Cloud sync is opt-in, per-account, and per-recording — nothing leaves your machine unless you signed in and chose a cloud destination. Cloud-bound copies are scrubbed first, the app adds a review step (the copy you approve is exactly the copy that uploads), and the local `recording.db` never leaves your machine, ever.
- **Search is consent-gated and encrypted.** On-device search only turns on after you acknowledge what it stores, and the search corpus (screenshots + text index) is encrypted at rest. It is never uploaded.
- **Action-gated capture.** Video and screenshots are written around your actual activity rather than as a 24/7 firehose, which keeps the footprint small enough to run all day.

And because the same recording is structured data — every click, keystroke, scroll, and window event in SQLite — you can export it as clean JSONL interaction traces for building automation or training computer-use agents.

## Install

### macOS app

Download from [screencap.sh](https://screencap.sh). The app bundles everything: recording, timeline, journal, search, chat over your own history, and settings — no terminal required.

### CLI

```bash
curl -sSfL https://get.screencap.sh | sh
```

This installs a standalone binary to `~/.screencap/bin` (no Python required; falls back to a pip-based install on unsupported macOS versions). Re-run the same command to upgrade, or pin a version:

```bash
curl -sSfL https://get.screencap.sh | SCREENCAP_VERSION=0.24.6 sh
```

### From source

```bash
git clone https://github.com/proteus-computer-use/screencap.git
cd screencap
pip install .            # or: pip install -e ".[dev]"
```

Requires macOS and, for source installs, Python 3.10+.

### Permissions

macOS will ask for **Screen Recording**, **Accessibility**, and **Input Monitoring** (System Settings → Privacy & Security), plus **Microphone** if you record audio. The app walks you through this; the CLI tells you what's missing when you start.

## Quick start

In the app: hit record, work, hit stop. Your day shows up in the Journal, and everything is one search away.

From the terminal:

```bash
# record — starts immediately, Ctrl+C or `screencap stop` to finish
screencap start

# with a name and task description
screencap start -n onboarding-flow -d "testing the new signup"

# check what's happening
screencap status

# see your recordings
screencap list
screencap view rec-20260713T091500     # opens the timeline viewer
```

Recording is chunked (15-minute segments by default), so a crash or force-quit costs you minutes, not the whole session.

To make your recordings searchable, turn on on-device search once:

```bash
screencap search enable
```

This is a deliberate consent step, not a config flag: it acknowledges the search disclosure (the app walks you through it during onboarding), encrypts the search corpus, and from then on new recordings are indexed automatically. `screencap search status` shows the current state, and `screencap backfill start` indexes the recordings you made before turning it on — with the same privacy rules applied.

## Search, chat, and agents

Once search is on, three surfaces sit on top of the same local index:

- **The app** — search across everything you've seen, browse the timeline, or just ask in Chat ("what was that error I hit on Tuesday?") and get answers grounded in your own history.
- **The daemon API** — read-only query verbs (`content.search`, `transcript.search`, `timeline.query`, `frame.nearest`) over a local UNIX socket, for tooling.
- **MCP** — `screencap mcp` runs a stdio MCP server, so Claude Desktop, Codex, or any MCP client can search your recordings, ask grounded questions about your history (the same capability behind the app's Chat, citations included), and pull up the exact frame where something happened. Setup guide: [docs/mcp-client-setup.md](docs/mcp-client-setup.md).

Everything here is local-only. The index lives at `~/.screencap/content_index.db`, is built only from frames the privacy policy allowed, and is never uploaded.

## Privacy model

Two layers, by design:

1. **Capture-time enforcement** — while recording, every window event is classified and the strictest applicable rule wins. Blocked content is never written.
2. **Scrub-time redaction** — after recording, PII and secrets detection (`screencap scrub`) cleans text and masks screenshots as defense-in-depth, and it always runs before anything is reviewed for upload.

### What happens per app category

Apps are classified into contexts; the action depends on your privacy mode (`public` for recordings that may leave your machine, `internal` for personal use):

| Context | `public` mode | `internal` mode |
|---|---|---|
| Password managers | Exclude | Exclude |
| Banking | Exclude | Mask window |
| Login / SSO pages | Exclude | Mask window |
| Checkout / billing pages | Exclude | Mask window |
| Email, chat, calendar, video calls | Mask window | Mask window |
| Browser (unverified domain) | Mask window | Allow |
| Cloud storage | Mask window | Allow |
| Code editors, terminals | Text redact | Allow |
| Admin consoles | Text redact | Allow |
| Everything else | Allow | Allow |

**Exclude** means nothing is stored: screenshots are never written, keystrokes are nulled, video frames are dropped. **Mask window** captures but nulls keystrokes, drops video frames, and covers the window in screenshots with an opaque fill at scrub time (a solid fill, not a blur — nothing recoverable bleeds through). **Text redact** keeps the capture and scrubs PII/secrets from text.

You control all of it: `screencap setup` runs an interactive wizard that scans your installed apps, and `screencap settings privacy` edits the exclude/allow lists directly. Your explicit rules beat the matrix — an allow you've confirmed is honored as a deliberate choice (except on sensitive browser pages like logins and checkouts); everywhere else the stricter action wins on conflict. The `SCREENCAP_PRIVACY_MODE` environment variable can tighten the configured mode but never loosen it.

### Extra protections

- macOS **Secure Input** (active while you're in a password field) blocks keystroke capture automatically; password fields detected via accessibility attributes go further and block screen, video, and keystrokes with a hold timer.
- After you switch away from a blocked app, capture stays blocked briefly to cover the app-switch animation.
- PII detection ships in the box — Presidio with a GLiNER NER backend, plus `detect-secrets` and regex patterns. No extra install step.

The daemon's trust boundary and full threat model live in [SECURITY.md](SECURITY.md).

## Cloud sync (optional)

Screencap never requires an account. Recording, scrubbing, search, and playback are fully local. If you want your recordings available across machines or shareable:

```bash
screencap login          # browser sign-in; token lives in your Keychain
screencap upload --all   # scrub → upload, per-account (the app adds a review step)
screencap download       # pull your recordings onto another machine
screencap whoami
```

- In the app, what you review is what uploads — the reviewed, scrubbed copy is reused at upload time.
- `recording.db` (the raw event database) is excluded from upload by a hard rule.
- End-to-end encryption for cloud copies is available in beta: `screencap e2ee enable` creates a device-held key first and only turns the setting on once the key exists, so the account can never end up half-configured.
- Cloud recordings are listed on your account page by default; record with `screencap start --unlisted` or set `screencap settings --set show_on_website=false` to change that.

Cloud storage is a paid feature — see [screencap.sh](https://screencap.sh) for plans.

## Structured export

Every recording is structured interaction data, not just video. `screencap export` runs the processing pipeline (click detection, drag detection, keyboard merge, shortcut detection) and writes one JSON event per line:

```bash
screencap export my-session                  # → events.jsonl in the recording dir
screencap export my-session --stdout | jq .  # pipe it
screencap export --all                       # everything
```

This is the raw material for workflow analysis, automation, and agent training. **Careful:** unscrubbed exports include captured keystrokes; pass `--privacy-filter` or export from a scrubbed copy when sharing.

## Commands

The daily set:

| Command | What it does |
|---|---|
| `screencap start` | Record. `--cloud` / `--local` / `--both` pick the destination; `--no-audio` and friends trim capture; `-o` sets a custom output dir. |
| `screencap stop` | Stop gracefully from any terminal (`--force` to kill). |
| `screencap status` | What's recording right now (`--json` for scripts). |
| `screencap list` / `view` / `info` / `rename` | Browse, open, inspect, retitle recordings. |
| `screencap search enable` / `status` | One-time consent gate for on-device search. |
| `screencap setup` | Interactive privacy wizard (`--scan`, `--show`, `--reset`). |
| `screencap settings` | Show or change config (`--set key=value`; `privacy` and `intelligence` subcommands). |
| `screencap export` | JSONL interaction events for ML / automation. |

Beyond those: `transcribe` (Whisper, local or API), `scrub` (PII/secrets redaction copy), `clip` (export a time range as .mp4), `backfill` (index old recordings), `upload` / `download` / `login` / `logout` / `whoami` (cloud), `e2ee` (encrypted cloud copies), `serve` (run/install the daemon), `mcp` (MCP server), `model` (optional local intelligence model), `storage migrate` (move the library), `network` (network-capture CA), `apps` (installed-app classifications), `update` (self-update) — plus a handful of plumbing verbs the app uses under the hood. Every command documents itself: `screencap <command> --help`.

### The daemon

The engine is supervised by a background daemon; `start`/`stop`/`status` are thin clients of its local API over `~/.screencap/run/api.sock`. The app and `screencap serve --install` (LaunchAgent) run it persistently. Without a LaunchAgent, CLI commands auto-spawn a temporary daemon that shuts itself down after 10 idle minutes — cron-driven `screencap status` won't leave processes behind.

## Configuration

Config lives at `~/.screencap/config.toml`; environment variables take precedence.

```toml
recordings_dir = "/custom/path"        # default ~/.screencap/recordings
audio_default = true
prefer_builtin_mic_over_bluetooth = true  # on AirPods, record from the built-in mic instead
disk_warn_mb = 2000                    # refuse to start / warn below this free space
disk_stop_mb = 500                     # auto-stop recording below this (0 disables)

[privacy]
mode = "internal"                      # or "public"
exclude_apps = ["com.example.secret"]
allow_apps = ["com.example.safe"]
```

| Variable | Default | Purpose |
|---|---|---|
| `SCREENCAP_RECORDINGS_DIR` | `~/.screencap/recordings` | Where recordings live |
| `SCREENCAP_DOWNLOADS_DIR` | `~/.screencap/downloads` | Where downloads land |
| `SCREENCAP_AUDIO_DEFAULT` | `true` | Audio capture default |
| `SCREENCAP_PREFER_BUILTIN_MIC_OVER_BLUETOOTH` | `true` | On Bluetooth input (AirPods), record from the built-in mic so playback stays high-quality; `false` records from the Bluetooth mic (degrades playback) |
| `SCREENCAP_DISK_WARN_MB` / `SCREENCAP_DISK_STOP_MB` | `2000` / `500` | Disk-space guardrails |
| `SCREENCAP_PRIVACY_MODE` | — | Tighten (never loosen) the privacy mode |
| `SCREENCAP_UPLOAD_DEFAULT` | `ask` | `local` / `cloud` / `both` / `ask` |
| `SCREENCAP_CHUNK_DURATION` | `900` | Chunk length in seconds (`0` disables) |
| `SCREENCAP_RETENTION_POLICY` | `keep_forever` | Or `delete_after_upload`, `delete_after_days`, `size_cap` |

**Bluetooth audio (AirPods).** Opening a Bluetooth headset's microphone forces macOS to drop it from high-quality stereo (A2DP) to telephone-quality "call" mode (HFP/SCO), degrading the audio you *hear* — not just the mic. So when your input device is a Bluetooth one, Screencap records from the built-in mic instead: your music/call playback stays untouched and you don't have to do anything. If there's no built-in mic to fall back to (e.g. a desktop Mac) and you aren't already on a call, mic capture is skipped for that stretch to protect playback. Already in a call on AirPods? Nothing changes — the mic is already in call mode, so Screencap captures normally. Set `prefer_builtin_mic_over_bluetooth = false` to always record from the Bluetooth mic anyway.

A recording directory (`~/.screencap/recordings/rec-YYYYMMDDTHHMMSS/`) contains chunked video (`chunk_0000.mp4` + manifest), audio (`audio_0000.flac`), interaction events (`events_0000.jsonl`), `screenshots/`, the local-only `recording.db`, and system metrics — with transcripts and exports added as they're produced.

## Where this is going

Screencap is built for people who work across a dozen tools a day and lose the thread between them — and increasingly, for the agents working alongside them. The near-term focus:

- **Recording you can forget about** — reliability and low overhead good enough to leave on all day.
- **Privacy that stays ahead** — deeper capture-time enforcement, E2EE from beta to default, consent that's real rather than fine print.
- **A native experience** — the macOS app is the product: journal, search, chat, and review, no terminal needed.
- **Memory for agents** — deepening the MCP and daemon surfaces that already let your tools answer "what was I doing?", so they get as good at it as you are.
- **Opt-in data, honestly sourced** — structured exports and a consented pipeline for teams building computer-use automation.

macOS only, deliberately, until it's excellent there.

## Contributing & development

```bash
pip install -e ".[dev]"
pytest tests/
```

The recording engine lives at `src/screencap/engine/`, the privacy subsystem at `src/screencap/privacy/` + `enforcement/` + `redaction/`, and the SwiftUI app at `macos/` (see [macos/README.md](macos/README.md) for app builds). `CLAUDE.md` has the full architecture map. Linting: `ruff check src/screencap/engine/`.

Screencap can also store the entire local library inside an app-managed **encrypted container** (an AES-256 sparse bundle), ciphertext at rest and mountable only by the entitled app daemon and its bundled CLI, with an optional Touch ID-gated **Lock** for a deliberately sealed state. See the "on-disk vault container" section of [`SECURITY.md`](SECURITY.md) for what it protects (backups, copies, disk images, FileVault-off machines, other local users, the sealed state) and its boundaries (a live same-user process while mounted; the unlock gate is a present-user UX gate, not a cryptographic boundary; lost key means unrecoverable recordings).

## License

Screencap is dual-licensed:

- **Open source:** GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later). See [LICENSE](LICENSE). If you use, modify, or offer Screencap as a network service, you must release your modifications under the same license.
- **Commercial:** If the AGPL terms don't fit your use case (e.g. embedding Screencap in a closed-source product or offering it as a hosted service without source disclosure), a commercial license is available. Email aayushgupta5000@gmail.com.
