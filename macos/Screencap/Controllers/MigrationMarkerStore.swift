import Foundation

/// One-time on-disk marker for the Phase 1c TCC-helper migration (SCR-49).
///
/// When the marker file exists, the `DaemonMigrationView` upgrade banner has
/// already been shown and the migration completed, so it is never shown again.
/// The marker lives at `~/.screencap/.tcc-migrated-v1`, alongside the daemon's
/// own state, using the same ad-hoc `NSHomeDirectory()` path idiom as the rest
/// of the app (e.g. `DaemonClient`'s socket path).
///
/// **The marker is UX-only.** It suppresses the *banner*, never the daemon's
/// independent TCC verification: the daemon's startup `_check_macos_permissions()`
/// remains the sole authority on whether permissions are actually present, so a
/// spoofed or stale marker is harmless — a missing grant still surfaces a
/// `permission_lost` event regardless of the marker (R4). Deleting the marker by
/// hand simply re-shows the banner once.
///
/// The base directory is injectable so tests exercise the read/write contract
/// against a temp directory without touching the real `~/.screencap`.
struct MigrationMarkerStore {
    /// Marker filename under the base directory. Versioned so a future
    /// re-migration (e.g. the individual→org Team ID switch) can bump to
    /// `.tcc-migrated-v2` and re-show the banner without colliding with this one.
    static let markerName = ".tcc-migrated-v1"

    /// `~/.screencap` (or an injected base for tests).
    private let baseDirectory: URL
    private let fileManager: FileManager

    init(
        baseDirectory: URL = MigrationMarkerStore.defaultBaseDirectory,
        fileManager: FileManager = .default
    ) {
        self.baseDirectory = baseDirectory
        self.fileManager = fileManager
    }

    /// `~/.screencap`. Mirrors `DaemonClient`'s `NSHomeDirectory()`-based path
    /// construction — there is no central Swift `AppPaths` helper, so each
    /// consumer builds the directory the same way.
    static var defaultBaseDirectory: URL {
        URL(fileURLWithPath: NSHomeDirectory(), isDirectory: true)
            .appendingPathComponent(".screencap", isDirectory: true)
    }

    private var markerURL: URL {
        baseDirectory.appendingPathComponent(Self.markerName, isDirectory: false)
    }

    /// True once the migration banner has been acknowledged (marker present).
    /// A non-existent file (or an unreadable base dir) reads as "not migrated"
    /// so the banner errs toward being shown — informing the user — rather than
    /// being silently skipped.
    func isMigrated() -> Bool {
        fileManager.fileExists(atPath: markerURL.path)
    }

    /// Write the marker, creating `~/.screencap` if absent. Idempotent: a second
    /// call is a harmless atomic re-write over the existing file. Throws on a
    /// genuine IO failure so the caller can log it; the banner re-showing once on
    /// the next launch is the acceptable fallback (it never gates recording).
    func markMigrated() throws {
        try fileManager.createDirectory(
            at: baseDirectory, withIntermediateDirectories: true
        )
        // The marker's existence is the entire signal; the contents are unused.
        try Data().write(to: markerURL, options: .atomic)
    }
}
