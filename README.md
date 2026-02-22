# ScreenCap

macOS CLI for screen recording with built-in privacy scrubbing. Wraps [OpenAdapt](https://github.com/OpenAdaptAI) for capture and PII redaction.

## Requirements

- macOS
- Python >= 3.10
- Accessibility permissions (System Settings → Privacy & Security → Accessibility)
- Screen Recording permissions (System Settings → Privacy & Security → Screen Recording)

## Installation

### Binary (recommended)

```bash
curl -sSf https://storage.googleapis.com/screencap-releases/releases/install.sh | sh
```

This installs a standalone binary to `~/.screencap/bin/`. No Python required.

To **upgrade**, run the same command — it overwrites the existing binary with the latest version.

To install a specific version:

```bash
SCREENCAP_VERSION=0.2.0 curl -sSf https://storage.googleapis.com/screencap-releases/releases/install.sh | sh
```

### From source

```bash
git clone https://github.com/Divide-By-0/screencap.git
cd screencap
pip install .

# editable (dev) install
pip install -e ".[dev]"
```

## Quick Start

```bash
# start recording immediately — no prompts needed
screencap start

# Ctrl+C to stop → auto-transcribes audio → LLM names the recording
# e.g. rec-20260222T143000/ → stripe-webhook-debugging/

# explicit name (skips auto-naming)
screencap start -n my-session -d "testing login flow"

# list recordings
screencap list

# view in browser
screencap view stripe-webhook-debugging

# scrub PII (first run installs ~500 MB of deps)
screencap scrub my-session

# transcribe audio (interactive — offers API or local Whisper)
screencap transcribe my-session
```

Press **Ctrl+C** once to stop recording gracefully. Double Ctrl+C to force quit.

## Commands

### `screencap start`

Record screen, mouse, keyboard, and optionally audio. Starts immediately with no prompts. After recording, auto-transcribes audio and uses an LLM to generate a descriptive name from the recording context.

| Flag | Description |
|------|-------------|
| `-n, --name TEXT` | Recording name (skips auto-naming when provided) |
| `-d, --description TEXT` | Task description |
| `--no-audio` | Disable audio capture |
| `--no-video` | Disable video capture |
| `--no-images` | Disable screenshot capture |
| `--no-window-data` | Disable window/accessibility data capture |
| `--no-browser-events` | Disable browser event capture |
| `--no-auto-name` | Skip LLM naming (prompts for name interactively) |
| `--local-only` | Restrict LLM naming to local providers (Ollama) |
| `-o, --output PATH` | Custom output directory (skips directory rename) |
| `--no-wifi-metrics` | Disable WiFi metrics collection |
| `--no-app-versions` | Disable running app version capture |
| `--force` | Auto-clean orphaned processes before starting |

Output files: `video.mp4`, `audio.flac`, `recording.db`, `viewer.html`

#### Auto-naming

After Ctrl+C, the post-recording pipeline runs:

1. **Auto-transcribe** — If audio was captured, transcribes using the fastest available backend (faster-whisper → openai-whisper → OpenAI API → skip). Uses the `base` model for local backends.
2. **LLM naming** — Assembles context (screenshots from DB, action events, window titles, transcript, running apps) and queries an LLM to generate a kebab-case directory name and description.

The LLM provider chain tries each in order, falling through on any failure:

| Priority | Provider | How it's detected |
|----------|----------|-------------------|
| 1 | `claude` CLI | `claude` on PATH |
| 2 | `chatgpt` CLI | `chatgpt` on PATH |
| 3 | Anthropic API | `ANTHROPIC_API_KEY` env var |
| 4 | OpenAI API | `OPENAI_API_KEY` env var |
| 5 | Ollama (local) | HTTP check on `localhost:11434` |
| 6 | Skip | Keeps timestamp name |

If no provider is available, the recording keeps its timestamp name (`rec-YYYYMMDDTHHMMSS`). This is not an error.

To use Ollama for fully local naming: `ollama pull qwen3-vl:4b` then `screencap start --local-only`.

### `screencap list`

| Flag | Description |
|------|-------------|
| `--json` | Output as JSON array |
| `--sort {name,date,duration}` | Sort column (default: `date`) |

### `screencap view <name>`

Opens the recording's `viewer.html` in your default browser.

| Flag | Description |
|------|-------------|
| `--scrubbed` | Open the scrubbed version instead |

### `screencap scrub <name>`

Creates a privacy-scrubbed copy at `<name>-scrubbed/`. Never mutates originals.

| Flag | Description |
|------|-------------|
| `--provider {PRESIDIO}` | Scrubbing provider (default: `PRESIDIO`) |

**What gets scrubbed:** screenshots (OCR + redaction), database text fields, transcript.json.
**Not yet supported:** video.mp4 scrubbing.
**Detected entities:** PERSON, EMAIL, PHONE, SSN, CREDIT_CARD, DATE_TIME, LOCATION.

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

### `screencap stop`

Terminate orphaned recording processes left behind by a crash or forced quit.

| Flag | Description |
|------|-------------|
| `--force` | Skip SIGTERM, go straight to SIGKILL |

### `screencap --version`

Print current version.

## Configuration

Config file: `~/.screencap/config.toml`

```toml
recordings_dir = "/custom/path/to/recordings"
audio_default = false
auto_name = true              # LLM auto-naming after recording
auto_name_local_only = false  # restrict to Ollama only
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SCREENCAP_RECORDINGS_DIR` | `~/.screencap/recordings` | Override recordings directory |
| `SCREENCAP_AUDIO_DEFAULT` | `true` | Default audio capture on/off |
| `SCREENCAP_AUTO_NAME` | `true` | Enable/disable LLM auto-naming |
| `SCREENCAP_AUTO_NAME_LOCAL_ONLY` | `false` | Restrict auto-naming to local providers (Ollama) |
| `ANTHROPIC_API_KEY` | — | Enables Anthropic API as a naming provider |
| `OPENAI_API_KEY` | — | Enables OpenAI API as a naming provider (also used for API transcription) |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server address |

Environment variables take precedence over `config.toml`.

## Recordings Directory Structure

```
~/.screencap/recordings/
└── stripe-webhook-debugging/   # auto-named by LLM (or rec-20260222T143000/ if no LLM)
    ├── recording.db            # SQLite: events, screenshots & metadata
    ├── video.mp4               # Screen recording
    ├── audio.flac              # Audio (if enabled)
    ├── transcript.txt          # Plain text transcript (auto or manual)
    ├── transcript.json         # Timestamped transcript (auto or manual)
    ├── system_metrics.json     # CPU, memory, display info
    ├── .upload_status.json     # Upload tracking (auto-created)
    └── viewer.html             # Interactive web viewer
```

## Testing

```bash
pytest tests/
pytest tests/ -v --cov
```

## License

MIT
