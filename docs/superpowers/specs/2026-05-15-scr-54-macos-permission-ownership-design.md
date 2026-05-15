# SCR-54 macOS Permission Ownership Design

## Goal

Fix the mismatch between the permission subject shown in the SwiftUI first-run flow and the process that actually records. In daemon-backed mode, the helper/daemon owns the recording permissions and must be the authority. The app should not block daemon recording because the Swift app process lacks or has stale TCC grants.

## User Experience

The UI should speak in product terms: ScreenCap needs Screen Recording, Accessibility, and Input Monitoring. Users should not have to understand app-vs-helper ownership unless macOS exposes separate entries in System Settings. When helper-specific copy is necessary, use it only to guide the user to enable the `ScreenCap helper` entry.

`Done` in the first-run sheet should mean the helper setup flow is complete enough to leave the sheet. It must not depend on cached app-process TCC checks when the app is configured for daemon-backed recording.

## Architecture

`PermissionController` continues to represent the Swift app process's local TCC state. That state is useful for app-owned prompts and CLI fallback, but it is not authoritative for daemon recording.

`RecorderController` gates start differently by transport:

- `.daemon`: do not pre-block on `PermissionController.allRequiredGranted`; call daemon start and let daemon/engine permission checks report the real failure.
- `.cliFallback`: keep the existing app/CLI-facing permission gate, because the fallback path does not have the daemon helper as the recording owner.

The in-recording Swift watchdog should also avoid stopping daemon-backed recordings based on app-process permission checks. Daemon-backed revocation is handled by the daemon event stream and existing `permission_lost` handling.

## Error Handling

Daemon start errors remain surfaced through `handleDaemonOperationFailure`. If the daemon reports a permission failure through a replayed or live event, existing `permission_lost` handling opens the relevant System Settings pane.

CLI fallback keeps the existing message: `Grant Screen Recording, Accessibility, and Input Monitoring permissions before recording.`

## Testing

Add Swift unit coverage for:

- daemon transport starts without app-process permissions;
- CLI fallback still blocks when app-process permissions are missing;
- the permission watchdog ignores app-process permission state while daemon transport is active.

Use test hooks rather than real TCC APIs so tests remain deterministic.
