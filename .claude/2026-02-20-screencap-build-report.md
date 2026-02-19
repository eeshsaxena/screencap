# ScreenCap Build Report — 2026-02-20

## Summary
Complete build of screencap CLI tool wrapping openadapt-capture + openadapt-privacy.
4 subcommands (start, list, view, scrub), 18 passing tests, Homebrew formula included.

## What Was Built

### Core Files
| File | Purpose |
|------|---------|
| `src/screencap/cli.py` | Click CLI: start, list, view, scrub |
| `src/screencap/config.py` | Config from ~/.screencap/config.toml + env vars |
| `src/screencap/recorder.py` | Wraps openadapt-capture Recorder, SIGINT handling |
| `src/screencap/catalog.py` | Scans recordings dir, reads metadata from recording.db |
| `src/screencap/scrubber.py` | Copies recording, scrubs text/images/transcript |
| `src/screencap/viewer.py` | Opens viewer.html via macOS `open` |
| `homebrew/screencap.rb` | Homebrew formula (needs sha256 on release) |

### Key Design Decisions
- DB file is `recording.db` (not capture.db as spec assumed)
- Audio defaults ON (inverts openadapt-capture default)
- Scrubbed copies at `<name>-scrubbed/` — originals never mutated
- Video scrubbing NOT supported (documented limitation)
- viewer.html opened as-is — no regeneration in view command
- Scrubber regenerates viewer.html in scrubbed copy only

### Test Results
18/18 passing: config (5), catalog (6), cli (7)

## Quick Start
```bash
cd /Users/yogeshshahi/Documents/zkmail/screencap
source .venv/bin/activate
screencap --help
screencap start --name test-recording
screencap list
screencap view test-recording
```

## Assumptions Made
1. recording.db always has `recording` and `action_event` tables
2. Duration = MAX(action_event.timestamp) - recording.timestamp
3. Audio file is always `audio.flac`
4. Transcript format matches Whisper output (text, segments, words keys)
5. element_state column stores JSON that can be parsed as dict
