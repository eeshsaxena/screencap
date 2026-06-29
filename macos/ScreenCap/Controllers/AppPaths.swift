import Foundation

/// Single source of truth for the `~/.screencap` base directory and the
/// well-known paths beneath it. Controllers derive their paths from here so the
/// root has exactly one definition rather than being re-spelled at each site.
///
/// The app is not sandboxed (see `ScreenCap.entitlements`), so `NSHomeDirectory()`
/// resolves to the user's real home — the same location the daemon and CLI use
/// when they read/write `~/.screencap`.
enum AppPaths {
    /// `~/.screencap` — the screencap home directory.
    static var screencapHome: URL {
        URL(fileURLWithPath: NSHomeDirectory(), isDirectory: true)
            .appendingPathComponent(".screencap", isDirectory: true)
    }

    /// `~/.screencap/recordings` — root of per-recording directories.
    static var recordingsRoot: URL {
        screencapHome.appendingPathComponent("recordings", isDirectory: true)
    }

    /// `~/.screencap/run/api.sock` — the daemon's UNIX-domain control socket.
    static var daemonSocket: URL {
        screencapHome
            .appendingPathComponent("run", isDirectory: true)
            .appendingPathComponent("api.sock")
    }
}
