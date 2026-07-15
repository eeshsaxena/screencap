# Follow-ups: occupied-mountpoint serving state (SCR-258 regression fix)

Deferred findings from the multi-agent review of the mountpoint_occupied fix
(daemon serves typed `error/mountpoint_occupied` instead of exiting 1; `storage
init` plaintext-library guard). None block the fix; all are verified-real gaps.

## 1. App-side dead end: the migration offer is hidden in exactly the wedged state (P1)

`settings --json` computes `store_encrypted` from bundle existence
(`cli/__init__.py` `_store_bundle_exists`), which reads **true** on a wedged
install (bundle exists, library still plaintext at the mountpoint). So
`VaultMigrationPolicy.swift` (`guard containerEnabled, !storeEncrypted`)
suppresses the migration banner and PrivacySettingsView hides the Encrypt
button — while `StoreState.swift` maps the unknown `mountpoint_occupied` reason
to `.unknown` (retryable), showing a Retry that can never succeed. Fix shape:

- Add a `mountpointOccupied` case to `StoreErrorReason` (StoreState.swift:73)
  with migrate-don't-retry copy and a "Migrate now" action.
- Key the migration offer on `store_state`/`store_reason` (daemon truth), not
  bundle existence.

## 2. Daemon serves stale `mountpoint_occupied` after the migration cure completes (P2)

`resolve_store_state` runs once at bind; `EncryptJob._on_terminal`
(`daemon/encrypt_job.py:359`) only publishes events. After `storage.encrypt.start`
completes the cutover, a still-running daemon keeps serving the cached
`error/mountpoint_occupied` (recording.start refused) until restart or an
explicit `storage.mount` call nothing currently issues. Fix shape: on terminal
COMPLETED, re-resolve and propagate (`supervisor.set_store_state` +
`app.state.store_state/store_reason`), mirroring `storage_mount`'s propagation
block in `daemon/app.py`. Add the end-to-end test: occupied ERROR ->
encrypt.start -> COMPLETED -> store observably MOUNTED.

## 3. MCP tool results omit `store_reason` (pre-existing)

`src/screencap/mcp/server.py` result models carry `store_state` but never
`store_reason`, so an agent seeing `store_state="error"` can't tell which of
the five reasons it is (or that data is intact). Plumb `store_reason` through
the MCP result models.

## 4. Custom-recordings installs get bounced between two refusals (P2, advisory)

The occupied-state guidance prescribes `storage encrypt start`, which refuses
custom-recordings installs (`custom_recordings_dir`, `daemon/app.py:~3368`).
Detect the custom-recordings case in the init guard and `_store_state_guidance`
and emit the accurate next step.

## 5. Symlinked recordings dir misclassifies as occupied (pre-existing, P2)

`os.path.ismount` is False for a symlink to a mountpoint, so a healthy
persistent mount behind a symlinked recordings dir reads as occupied on
restart (`store_lifecycle.mountpoint_is_occupied`). Consider evaluating
`ismount` on `os.path.realpath(mountpoint)` — in the shared predicate so guard
and resolver stay in parity.

## 6. `storage.lock` unwind hard-codes MOUNTED (pre-existing residue)

The upfront `store_not_mounted` guard added in this fix closes the reachable
hole, but `supervisor.abort_lock()` and the handler unwinds still hard-code
MOUNTED rather than restoring the entry state. Harmless while the guard holds
(entry is always MOUNTED); worth restoring entry state if lock ever becomes
callable from other states.

## 7. Consider a docs/solutions entry

"Occupied mountpoint is a typed serving error, not an exit (KTD-14)" — so the
next SCR-258-adjacent regression finds the rationale via grep instead of only
code comments.
