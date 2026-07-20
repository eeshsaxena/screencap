import Foundation

// SCR-214 U12 — test seam for the ambient runtime-control verbs, mirroring
// `SearchService` (the established repo pattern for wrapping `DaemonClient`
// behind a protocol). `AmbientController` depends on this protocol so it can be
// unit-tested against a fake without a live UNIX socket.

protocol AmbientService: Sendable {
    /// The runtime ambient-supervision snapshot. Read-only.
    func ambientStatus() async throws -> AmbientStatus
    /// Toggle ambient / autostart at runtime; returns the NEW status payload.
    func ambientSet(enabled: Bool?, autostart: Bool?) async throws -> AmbientStatus
    /// Pause capture on the running ambient recording (U4). A throw surfaces a
    /// visible error at the call site; the controller re-reads `ambientStatus`
    /// for the CONFIRMED pause state rather than trusting the request echo.
    func recordingPause() async throws
    /// Resume capture on the paused ambient recording (U4). See `recordingPause`.
    func recordingResume() async throws
}

/// Live implementation: forwards to the daemon over the UNIX socket.
struct LiveAmbientService: AmbientService {
    func ambientStatus() async throws -> AmbientStatus {
        try await DaemonClient.ambientStatus()
    }

    func ambientSet(enabled: Bool?, autostart: Bool?) async throws -> AmbientStatus {
        try await DaemonClient.ambientSet(enabled: enabled, autostart: autostart)
    }

    func recordingPause() async throws {
        _ = try await DaemonClient.recordingPause()
    }

    func recordingResume() async throws {
        _ = try await DaemonClient.recordingResume()
    }
}
