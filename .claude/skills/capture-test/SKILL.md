---
name: capture-test
description: End-to-end capture pipeline sanity check. Runs a real screencap recording, asks the user to perform actions, then validates the captured data (files, DB, screenshots). Use when testing capture changes, verifying the recording pipeline works, or after modifying recorder/catalog code. Triggers on "test capture", "capture test", "sanity check", "test recording pipeline", or after changes to recorder.py, catalog.py, or screencap.engine.
---

# Capture Pipeline Test

End-to-end sanity check for the screencap recording pipeline. Runs a real capture, then validates everything was recorded correctly.

## How It Works

1. **Gather context** — Read conversation history or accept explicit args to determine what to test (e.g., "audio recording", "no-video mode", "default settings")
2. **Start recording** — Run `screencap start` in the background with appropriate flags
3. **User action phase** — Inform the user the recording is live and ask them to perform some actions (move mouse, type, click)
4. **Stop recording** — Run `screencap stop` to gracefully stop after the user signals they're done (or after a timeout)
5. **Validate capture** — Check all expected outputs exist and are valid
6. **Visual inspection** — Read a sample of captured screenshots to visually confirm they contain screen content
7. **Report** — Structured pass/fail summary

## Instructions

### Step 1: Determine Test Scenario

**The test must exercise the code path that was actually changed.** A sanity check that disables everything the fix touches is useless. Derive the scenario from what was modified in the current session:

Check conversation context for what's being tested. If the user provided args, use those. Otherwise, derive from context:

- If changes touch `chunk_processor.py`, sentinel upload, chunk rotation, or the post-stop shutdown path in `recorder.py` → **test with video + audio + chunked mode** (`--chunk-duration 10`). This is the only way to exercise the ChunkProcessor pipeline. Without video, no chunks rotate. Without audio, the audio ack wait blocks for 60s per chunk.
- If changes touch upload/cloud logic → **test with `--cloud`**. The upload bucket is public — no credentials needed. This exercises `live_upload=True` + `cloud_intent=True`, including sentinel upload and chunk upload gating.
- If changes touch audio processing → test with audio enabled
- If changes touch screenshot capture → focus validation on screenshot quality
- If changes touch `recorder.py` signal handling or lifecycle → test default capture settings
- If no specific context → run default sanity check (images + actions, no audio, no video)

**Default test flags:** `--no-auto-name --no-wifi-metrics --no-app-versions`
Additional flags depend on context as described above. Only add `--no-video` or `--no-audio` when the code under test does not depend on those subsystems.

**Duration guidance:** For chunked tests, tell the user to perform actions for **30-40 seconds** (enough for 3-4 chunk rotations at `--chunk-duration 10`). For non-chunked tests, 10-15 seconds is sufficient.

### Step 2: Start Recording

Generate a test name: `capture-test-<YYYYMMDD-HHMMSS>`

Run in a background shell:
```bash
screencap start --name <test-name> --no-auto-name [flags from step 1]
```

IMPORTANT: Use `run_in_background: true` for the Bash tool since recording blocks until stopped.

### Step 3: User Action Phase

Tell the user:
```
Recording is live! Please perform some actions for the next 10-15 seconds:
- Move your mouse around
- Click on a few things
- Type some text (in any app)

When you're done, let me know and I'll stop the recording.
```

Wait for the user to respond. Do NOT automatically stop — let the user control when they're done.

### Step 4: Stop Recording

Use `screencap stop` — the recommended way to stop any recording:
```bash
screencap stop
```

This sends SIGTERM to the parent process (graceful shutdown), waits up to 30 seconds, and falls back to force-killing orphaned processes if needed. It reads the pidfile automatically — no PID management required.

If `screencap stop` hangs or fails, fall back to `screencap stop --force` which skips SIGTERM and goes straight to SIGKILL on all recording processes.

### Step 5: Validate Capture

Run these checks against the recording directory (`~/.screencap/recordings/<test-name>/`):

#### 5a. File Structure Checks
- [ ] Recording directory exists
- [ ] Database file exists (`recording.db`)
- [ ] `screenshots/` directory exists (if images were enabled)
- [ ] Screenshots directory has at least 1 file
- [ ] If audio was enabled: `audio.flac` exists and is > 1KB
- [ ] If video was enabled: `video.mp4` exists and is > 1KB

#### 5b. Database Checks
Open the SQLite DB and verify:
- [ ] Expected tables exist (`recording` + `action_event`, or `capture` + `events`)
- [ ] Recording/capture row exists with a valid timestamp
- [ ] At least 1 action event was recorded
- [ ] Event timestamps are within a reasonable range (between recording start and now)
- [ ] Multiple event types present (mouse moves, clicks, key presses — at least 2 types)

#### 5c. Screenshot Validation
- [ ] Screenshot files are non-zero size
- [ ] At least one screenshot is a valid image (use Read tool to visually inspect)
- [ ] Screenshots show actual screen content (not black/corrupt frames)
- [ ] Screenshot dimensions are reasonable (match a real display resolution)

#### 5d. Timing Checks
- [ ] Recording duration in DB roughly matches wall-clock time (within 5s tolerance)
- [ ] Event timestamps span most of the recording duration (not all bunched at start/end)

### Step 6: Visual Inspection

Use the Read tool to look at 2-3 screenshots from the capture:
- One from near the start
- One from the middle
- One from near the end

Verify they show real screen content and are visually distinct (proving the pipeline captured changing content over time).

### Step 7: Report Results

Print a structured report:

```
## Capture Test Results: <test-name>

### Summary
- Status: PASS / FAIL
- Duration: Xs
- Screenshots: N captured
- Events: N recorded (M types)

### Checks
[x] Recording directory created
[x] Database exists (recording.db)
[x] Screenshots captured (47 files)
[x] Action events recorded (312 events, 4 types)
[x] Audio file exists (if applicable)
[x] Timestamps valid
[x] Screenshots show real content

### Visual Samples
(describe what was seen in the sampled screenshots)

### Issues (if any)
- Description of any failures
```

### Step 8: Cleanup

Ask the user if they want to keep or delete the test recording:
- Keep → leave it in `~/.screencap/recordings/`
- Delete → `rm -rf ~/.screencap/recordings/<test-name>/`

**Cloud recordings (--cloud):** If the test used `--cloud`, also clean up files uploaded to GCS. The bucket is `screencap-recordings` and files are stored under `recordings/<recording-name>/`.

```bash
# 1. Find the recording name (from .recording_id or the --name flag)
RECORDING_NAME="<test-name>"

# 2. Verify only our test recording files are listed (ALWAYS check before deleting)
gsutil ls "gs://screencap-recordings/recordings/${RECORDING_NAME}/"

# 3. Delete only our test recording
gsutil -m rm -r "gs://screencap-recordings/recordings/${RECORDING_NAME}/"
```

**IMPORTANT:** Always list files first and visually confirm they belong to the test recording before deleting. Never use wildcards that could match other recordings. The bucket also has a `sessions/` prefix for processed recordings — check there too if the cloud stitcher has already processed the sentinel:
```bash
gsutil ls "gs://screencap-recordings/sessions/${RECORDING_NAME}/" 2>/dev/null
gsutil -m rm -r "gs://screencap-recordings/sessions/${RECORDING_NAME}/" 2>/dev/null
```

**Note on cloud_intent + privacy pipeline:** If the privacy scrubbing pipeline fails to initialize (missing deps, config error), `ChunkProcessor` disables uploads silently (fail-closed). Chunks are marked as "success" locally but nothing reaches GCS. In this case there's nothing to clean up on GCS. Check: if `stub_recording()` ran (no local .mp4 files) but `gsutil ls` shows no files, uploads were silently disabled.

## Handling Args

The skill accepts optional args to customize the test:

- `/capture-test` — default sanity check (images, actions, no audio/video)
- `/capture-test audio` — test with audio capture enabled
- `/capture-test video` — test with video capture enabled
- `/capture-test full` — test with all capture modes (images + audio + video)
- `/capture-test "custom description"` — use description as context for what to focus on

## Error Handling

- If `screencap start` fails to launch → report the error, check if deps are installed
- If recording produces no output → check permissions (Screen Recording, Accessibility, Input Monitoring)
- If screenshots are black/empty → likely a Screen Recording permission issue on macOS
- If no action events → Accessibility or Input Monitoring permission missing
- If audio is empty → check microphone permission

## Future Enhancements

These are planned improvements to make the test more powerful. Captured here for context.

### Automated Synthetic Actions (Priority: Medium)
Instead of asking the user to perform actions manually, use macOS automation:
- `cliclick` (Homebrew) — CLI tool for mouse moves, clicks, keystrokes
- `osascript` / AppleScript — open apps, type text, click UI elements
- `pyautogui` or `pyobjc` — Python-native automation

Benefits:
- Fully deterministic — know exact actions to assert in DB
- Can run unattended (CI/CD)
- Can test specific scenarios (e.g., "type in a password field" for privacy testing)

Challenges:
- Requires Accessibility permission for the automation tool itself
- Another dependency and failure surface
- Less realistic than human input

### Deterministic Assertions (Priority: Medium)
With automated actions, we could assert:
- "Clicked at (500, 300) → action_event row with coordinates near (500, 300)"
- "Typed 'hello world' → key events for each character"
- "Opened Safari → window_data event with Safari bundle ID"

### Scrub Quality Checker (Priority: High)
Separate skill (`/scrub-test`) that:
1. Runs `/capture-test` to create a recording with known PII in view
2. Runs `screencap scrub <name>` on it
3. Reads original + scrubbed screenshots side-by-side
4. Visually verifies PII was redacted and non-PII preserved
5. Checks DB text fields were scrubbed

### Viewer Visual Test (Priority: Low)
- Open `viewer.html` with test recording data
- Use agent-browser/Playwright to screenshot the rendered page
- Verify timeline, controls, and screenshot display render correctly

### CI Integration (Priority: Low)
- Package as a pytest fixture that sets up a virtual display (Xvfb on Linux)
- Run automated actions
- Assert capture results
- Would need to solve the macOS permission problem for headless environments
