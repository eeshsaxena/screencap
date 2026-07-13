# ScreenCap

ScreenCap is a local-first macOS recorder for building high-quality demonstration data for automation and agent training. It captures screen, input, and context in one run, then lets you export ML-ready events.

ScreenCap is designed for teams who need more than video clips: reproducible recordings, structured interaction data, and a streamlined pipeline from capture to training.

## What We Offer

- **One-command capture workflow**: Start recording immediately with `screencap start`, then auto-transcribe on stop.
- **ML-ready outputs**: Export processed interaction events to JSONL with `screencap export` for downstream training pipelines.
- **Local-first by default**: Run from a standalone binary, keep recordings on disk, and use optional cloud sync only when needed.
- **Structured recording data**: Recording artifacts and schema are designed for ML training pipelines.

## Requirements

- macOS
- Python >= 3.10
- Accessibility permissions (System Settings → Privacy & Security → Accessibility)
- Screen Recording permissions (System Settings → Privacy & Security → Screen Recording)

## Installation

### Binary (recommended)

```bash
curl -sSfL https://get.screencap.sh | sh
```

This installs a standalone binary to `~/.screencap/bin/`. No Python required.

To **upgrade**, run the same command — it overwrites the existing binary with the latest version.

To install a specific version:

```bash
curl -sSfL https://get.screencap.sh | SCREENCAP_VERSION=0.2.0 sh
```

### From source

```bash
git clone https://github.com/proteus-computer-use/screencap.git
cd screencap
pip install .

# editable (dev) install
pip install -e ".[dev]"
```

## Quick Start

```bash
# start recording immediately — no prompts needed
screencap start

# screencap stop (or Ctrl+C) to stop → auto-transcribes audio
# recording directory: rec-20260222T143000/

# explicit name
screencap start -n my-session -d "testing login flow"

# list recordings
screencap list

# view in browser
screencap view rec-20260222T143000

# transcribe audio (interactive — offers API or local Whisper)
screencap transcribe my-session
```

### Stopping a recording

The recommended way to stop a recording is `screencap stop` — it works from any terminal, any context (scripts, agents, background processes), and has a built-in fallback chain:

```bash
screencap stop          # graceful stop (sends SIGTERM, waits up to 30s)
screencap stop --force  # force kill all recording processes immediately
```

If you're in the same terminal where the recording is running, **Ctrl+C** also works as a shortcut:
- **Ctrl+C** once → graceful stop (same as `screencap stop`)
- **Ctrl+C** twice → force quit (kills child processes, then exits)

## Commands

### `screencap start`

Record screen, mouse, keyboard, and optionally audio. Starts immediately with no prompts. After recording, auto-transcribes audio and uses an LLM to generate a descriptive name from the recording context.

| Flag | Description |
|------|-------------|
| `-n, --name TEXT` | Recording name |
| `-d, --description TEXT` | Task description |
| `--no-audio` | Disable audio capture |
| `--no-video` | Disable video capture |
| `--no-images` | Disable screenshot capture |
| `--no-window-data` | Disable window/accessibility data capture |
| `-o, --output PATH` | Custom output directory (skips directory rename) |
| `--no-wifi-metrics` | Disable WiFi metrics collection |
| `--no-app-versions` | Disable running app version capture |

Output files: `video.mp4`, `audio.flac`, `recording.db`, `viewer.html`

#### Disk space protection

ScreenCap checks available disk space before and during recording to prevent filling your drive:

1. **Pre-recording gate** — Refuses to start if free space is below the warning threshold (default: 2 GB).
2. **Periodic check** — Every 30 seconds during recording, checks free space. Shows a warning in the live display when space is low.
3. **Auto-stop** — If free space drops below the stop threshold (default: 500 MB), the recording stops automatically and saves what was captured.

Both thresholds are configurable via environment variables or `config.toml`. Set to `0` to disable.

```bash
# lower the warning threshold to 500 MB
SCREENCAP_DISK_WARN_MB=500 screencap start

# disable disk checks entirely
SCREENCAP_DISK_WARN_MB=0 SCREENCAP_DISK_STOP_MB=0 screencap start
```

#### Auto-transcription

After stopping, if audio was captured it is transcribed using the fastest available backend (faster-whisper → openai-whisper → OpenAI API → skip). Uses the `base` model for local backends.

Recordings keep their timestamp name (`rec-YYYYMMDDTHHMMSS`) unless `--name` is provided.

### `screencap list`

| Flag | Description |
|------|-------------|
| `--json` | Output as JSON array |
| `--sort {name,date,duration}` | Sort column (default: `date`) |

### `screencap view <name>`

Opens the recording's `viewer.html` in your default browser.

### `screencap transcribe <name>`

Transcribe a recording's audio using Whisper. Interactively offers OpenAI API or local model.

- If `OPENAI_API_KEY` is set, prompts to use the API (~$0.006/min)
- Otherwise offers to enter a key or transcribe locally
- Auto-installs `faster-whisper` if no local backend is found
- Produces `transcript.txt` and `transcript.json` (with timestamps)

| Model | Size | Notes |
|-------|------|-------|
| tiny | ~39 MB | Fast, lower accuracy |
| base | ~140 MB | Good balance (recommended) |
| small | ~466 MB | Better accuracy |
| medium | ~1.5 GB | High accuracy |
| large | ~2.9 GB | Best accuracy |

### `screencap export <name>`

Export processed events as JSONL for ML training. Runs the full processing pipeline (click detection, drag detection, keyboard merge, shortcut detection) and writes one JSON object per line.

```bash
# export to the recording directory (default)
screencap export my-session
# → ~/.screencap/recordings/my-session/events.jsonl

# export all recordings
screencap export --all

# export all downloaded recordings
screencap export --downloads

# export both local and downloaded recordings
screencap export --all --downloads

# export a specific download by name
screencap export my-download --downloads

# stream to stdout (for piping to jq, etc.)
screencap export my-session --stdout

# custom output path
screencap export my-session -o ~/training-data/events.jsonl

# exclude mouse move events
screencap export my-session --exclude-moves
```

| Flag | Description |
|------|-------------|
| `--all` | Export all local recordings (each gets its own `events.jsonl`) |
| `--downloads` | Include downloaded recordings (`~/.screencap/downloads/`). Use alone for downloads only, or with `--all` for both. |
| `-o, --output PATH` | Custom output file path |
| `--stdout` | Write to stdout instead of a file |
| `--exclude-moves` | Omit mouse move events from output |

**Warning:** Export includes all captured keystrokes (passwords, API keys, private messages). Review recordings for sensitive data before sharing.

### `screencap upload [names...]`

Upload recordings to cloud storage (GCS). Tracks upload status locally via `.upload_status.json` — re-running skips already-uploaded recordings without hitting the server.

```bash
# upload a single recording
screencap upload my-session

# upload multiple
screencap upload session-1 session-2

# upload all recordings
screencap upload --all

# re-upload even if already uploaded
screencap upload my-session --force

# preview what would be uploaded
screencap upload my-session --dry-run
```

| Flag | Description |
|------|-------------|
| `--all` | Upload all recordings |
| `--dry-run` | Show files and sizes without uploading |
| `--force` | Re-upload even if already uploaded locally |

### `screencap download`

Download recordings from cloud storage (GCS). Tracks download status locally via `.download_status.json` — re-running skips already-downloaded recordings.

```bash
# download all new recordings
screencap download

# download to a custom directory
screencap download --dest ~/ml-data

# preview what would be downloaded
screencap download --dry-run

# re-download everything
screencap download --force
```

| Flag | Description |
|------|-------------|
| `--dest DIR` | Override destination directory (default: `~/.screencap/downloads/`) |
| `--dry-run` | Show recording names and file counts without downloading |
| `--force` | Re-download all recordings, ignoring markers |

### `screencap setup`

Configure privacy settings with an interactive wizard. On first run, `screencap start` automatically prompts you to run this.

The wizard scans your installed applications, classifies them into privacy categories, and writes a `[privacy]` section to `~/.screencap/config.toml`. Apps that ScreenCap already knows (password managers, email clients, code editors, etc.) are pre-classified. Apps it doesn't recognize are shown for your review.

```bash
# run the full setup wizard
screencap setup

# re-scan for new apps without resetting existing config
screencap setup --scan

# show current privacy configuration
screencap setup --show

# remove privacy configuration
screencap setup --reset
```

| Flag | Description |
|------|-------------|
| `--scan` | Re-scan installed apps and update classifications |
| `--show` | Display current privacy configuration |
| `--reset` | Remove the `[privacy]` section from config |

### Privacy Policy

ScreenCap has a built-in privacy policy that controls what gets captured based on which app is in the foreground. The system works at two levels:

1. **Capture-time filtering** — During recording, ScreenCap monitors the active window and blocks capture in real-time for excluded apps. Blocked screenshots are never written to disk, and keystroke content is nulled.

2. **Post-recording scrubbing** — After recording, `screencap scrub` applies additional redaction (PII detection, secret scanning, screenshot masking) as defense-in-depth.

#### Privacy Modes

The privacy mode controls how aggressively different app categories are handled:

| Mode | Use case | Behavior |
|------|----------|----------|
| `public` | Recordings shared externally | Strictest — masks or excludes most app categories |
| `internal` | Internal/personal use | Permissive — only excludes password managers |

Set the mode in `config.toml` or via environment variable:

```toml
[privacy]
mode = "public"
```

```bash
SCREENCAP_PRIVACY_MODE=public screencap start
```

The environment variable can only **tighten** the mode (e.g., set `public` when config says `internal`), never loosen it. This prevents accidental exposure.

#### App Classification

Apps are classified into context categories that determine the privacy action:

| Category | Public mode | Internal mode |
|----------|-------------|---------------|
| Password manager | Exclude (never captured) | Exclude (never captured) |
| Banking | Exclude | Mask window |
| Email, Chat, Calendar, Video call | Mask window | Text redact |
| Browser (no verified domain) | Mask window | Allow |
| Code editor, Terminal | OCR fallback | Allow |
| Admin console (AWS, DB tools) | OCR fallback | Allow |
| Unknown / unclassified | Mask window | Allow |

Apps are classified by: user config overrides > known bundle ID > browser domain matching > window title heuristics.

#### Privacy Actions (strictest to most permissive)

| Action | What it does |
|--------|--------------|
| **Exclude** | Screenshot deleted, keystrokes nulled. Nothing stored. |
| **Mask window** | Full window area blurred in screenshot. |
| **Text redact** | PII/secrets redacted from text fields. |
| **OCR fallback** | Text extracted via OCR and redacted. |
| **Allow** | No modification. |

#### Additional Protections

- **Secure Input detection** — macOS Secure Input mode (e.g., password fields in Safari) automatically blocks capture.
- **AXSecureTextField** — Password fields detected via accessibility attributes block capture with a hold timer.
- **Transition hold** — After switching away from a blocked app, capture stays blocked for 1 second to cover macOS app-switch animations.
- **Fail-closed** — The filter starts blocked until the first window event arrives. If an error occurs during filtering, capture blocks until the next successful event.

#### Config Options

```toml
[privacy]
mode = "public"                              # "public" or "internal"
exclude_apps = ["com.example.secret-app"]     # always block these apps
allow_apps = ["com.example.safe-app"]         # always allow these apps
mask_domains = ["internal.company.com"]       # mask browser tabs on these domains
mask_title_patterns = ["(?i)\\bconfidential\\b"]  # mask windows matching these patterns

[privacy.app_classes]
"com.example.app" = "banking"                 # override classification for an app
```

**Precedence:** exclude_apps > allow_apps > mask_domains > mask_title_patterns > action matrix. The stricter action always wins when rules conflict.

### `screencap scrub <name>`

Redact PII and secrets from a recording's database and transcript files. Creates a scrubbed copy at `<name>-scrubbed/`.

```bash
# scrub with auto-detected engine (prefers GLiNER, falls back to spaCy)
screencap scrub my-session

# use a specific PII engine
screencap scrub my-session --pii-engine presidio-gliner
screencap scrub my-session --pii-engine presidio
```

| Flag | Description |
|------|-------------|
| `--pii-engine {presidio,presidio-gliner}` | PII detection engine (default: auto-detect, prefers GLiNER) |

#### PII Engine

Install with `pip install 'screencap[privacy]'`. This installs Presidio with GLiNER NER backend (`presidio-analyzer[gliner]`) plus `detect-secrets` for API key and secrets detection.

| Component | What it detects |
|-----------|----------------|
| GLiNER NER (default) | Names, emails, phone numbers, SSNs, credit cards, addresses |
| spaCy NER (fallback) | Same entities, lower accuracy on short text |
| detect-secrets | API keys, private keys, JWTs, connection strings, passwords |
| Regex patterns | Emails, URLs, credit cards, SSNs, phone numbers |

The detection pipeline applies a priority-based resolver to handle overlapping detections from different engines, followed by heuristic filters to reject common false positives.

### `screencap stop`

Stop the active recording gracefully. This is the recommended way to stop any recording — it works from any terminal, scripts, agents, and background processes.

1. Reads the pidfile to find the recording's parent process
2. Sends SIGTERM for a graceful shutdown (flushes data, finalizes video, writes metrics)
3. Waits up to 30 seconds for the process to exit
4. If graceful stop times out, falls back to force-killing orphaned processes

Also cleans up orphaned recording processes left behind by a crash or forced quit.

| Flag | Description |
|------|-------------|
| `--force` | Skip SIGTERM, go straight to SIGKILL |

### `screencap --version`

Print current version.

## Configuration

Config file: `~/.screencap/config.toml`

```toml
recordings_dir = "/custom/path/to/recordings"
downloads_dir = "/custom/path/to/downloads"
audio_default = false
disk_warn_mb = 2000           # free MB to start / warn (0 = disable)
disk_stop_mb = 500            # free MB to auto-stop (0 = disable)
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SCREENCAP_RECORDINGS_DIR` | `~/.screencap/recordings` | Override recordings directory |
| `SCREENCAP_DOWNLOADS_DIR` | `~/.screencap/downloads` | Override downloads directory |
| `SCREENCAP_AUDIO_DEFAULT` | `true` | Default audio capture on/off |
| `SCREENCAP_DISK_WARN_MB` | `2000` | Free MB required to start recording / trigger warning (0 = disable) |
| `SCREENCAP_DISK_STOP_MB` | `500` | Free MB threshold to auto-stop recording (0 = disable) |
| `SCREENCAP_PRIVACY_MODE` | — | Override privacy mode (can only tighten, never loosen) |
| `ANTHROPIC_API_KEY` | — | Enables Anthropic API as a naming provider |
| `OPENAI_API_KEY` | — | Enables OpenAI API as a naming provider (also used for API transcription) |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server address |

Environment variables take precedence over `config.toml`.

## Recordings Directory Structure

```
~/.screencap/recordings/
└── rec-20260222T143000/         # recording directory (timestamp-named)
    ├── recording.db            # SQLite: events, screenshots & metadata
    ├── video.mp4               # Screen recording
    ├── audio.flac              # Audio (if enabled)
    ├── transcript.txt          # Plain text transcript (auto or manual)
    ├── transcript.json         # Timestamped transcript (auto or manual)
    ├── events.jsonl             # Exported events (created by `screencap export`)
    ├── system_metrics.json     # CPU, memory, display info
    ├── .upload_status.json     # Upload tracking (auto-created)
    └── viewer.html             # Interactive web viewer
```

## Testing

```bash
pytest tests/
pytest tests/ -v --cov
```

## Security

ScreenCap's recording daemon trusts any process running as the same macOS user. The trust boundary is your user account; processes from other accounts cannot reach the daemon socket or read recordings. See [`SECURITY.md`](SECURITY.md) for the full threat model, alternatives considered, and instructions for reporting a vulnerability.

ScreenCap can also store the entire local library inside an app-managed **encrypted container** (an AES-256 sparse bundle), ciphertext at rest and mountable only by the entitled app daemon and its bundled CLI, with an optional Touch ID-gated **Lock** for a deliberately sealed state. See the "on-disk vault container" section of [`SECURITY.md`](SECURITY.md) for what it protects (backups, copies, disk images, FileVault-off machines, other local users, the sealed state) and its boundaries (a live same-user process while mounted; the unlock gate is a present-user UX gate, not a cryptographic boundary; lost key means unrecoverable recordings).

## License

ScreenCap is dual-licensed:

- **Open source:** GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later). See [`LICENSE`](LICENSE). If you use, modify, or offer ScreenCap as a network service, you must release your modifications under the same license.
- **Commercial:** If the AGPL terms don't fit your use case (e.g. embedding ScreenCap in a closed-source product or offering it as a hosted service without source disclosure), a commercial license is available. Email aayushgupta5000@gmail.com.
