# ScreenCap

macOS CLI for screen recording with built-in privacy scrubbing. Wraps [OpenAdapt](https://github.com/OpenAdaptAI) for capture and PII redaction.

## Requirements

- macOS
- Python >= 3.10
- Accessibility permissions (System Settings → Privacy & Security → Accessibility)

## Installation

```bash
# clone & install
git clone https://github.com/Divide-By-0/screencap.git
cd screencap
pip install .

# editable (dev) install
pip install -e ".[dev]"
```

## Quick Start

```bash
# interactive — prompts for name, description, audio
screencap start

# non-interactive
screencap start -n my-session -d "testing login flow"

# list recordings
screencap list

# view in browser
screencap view my-session

# scrub PII (first run installs ~500 MB of deps)
screencap scrub my-session

# transcribe audio (interactive — offers API or local Whisper)
screencap transcribe my-session
```

Press **Ctrl+C** once to stop recording gracefully. Double Ctrl+C to force quit.

## Commands

### `screencap start`

Record screen, mouse, keyboard, and optionally audio.

| Flag | Description |
|------|-------------|
| `-n, --name TEXT` | Recording name (prompts if omitted) |
| `-d, --description TEXT` | Task description |
| `--no-audio` | Disable audio capture |
| `-o, --output PATH` | Custom output directory |

Output files: `video.mp4`, `audio.flac`, `recording.db`, `viewer.html`, `screenshots/`

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

### `screencap --version`

Print current version.

## Configuration

Config file: `~/.screencap/config.toml`

```toml
recordings_dir = "/custom/path/to/recordings"
audio_default = false
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SCREENCAP_RECORDINGS_DIR` | `~/.screencap/recordings` | Override recordings directory |
| `SCREENCAP_AUDIO_DEFAULT` | `true` | Default audio capture on/off |

Environment variables take precedence over `config.toml`.

## Recordings Directory Structure

```
~/.screencap/recordings/
└── my-session/
    ├── recording.db       # SQLite: events & metadata
    ├── video.mp4          # Screen recording
    ├── audio.flac         # Audio (if enabled)
    ├── transcript.json    # Whisper transcription (optional)
    ├── screenshots/       # PNG screenshots
    └── viewer.html        # Interactive web viewer
```

## Testing

```bash
pytest tests/
pytest tests/ -v --cov
```

## License

MIT
