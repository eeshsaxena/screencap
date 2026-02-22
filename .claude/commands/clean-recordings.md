Delete all recordings in the screencap recordings directory (`~/.screencap/recordings/`).

Steps:
1. List the contents of `~/.screencap/recordings/` so the user can see what will be deleted.
2. If the directory is empty or doesn't exist, inform the user and stop.
3. Ask the user to confirm before deleting.
4. Delete all subdirectories inside `~/.screencap/recordings/` (each recording is a subdirectory).
5. Report what was deleted.

Important:
- Only delete the contents inside `~/.screencap/recordings/`, NOT the directory itself.
- Use `rm -rf` on each subdirectory.
- Respect `SCREENCAP_RECORDINGS_DIR` env var if set — check it first and use that path instead of the default if present.
