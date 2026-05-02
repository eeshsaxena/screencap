import AppKit
import Combine
import Foundation

/// State machine for the recording lifecycle. Unit 13 fills in the transitions;
/// Unit 9 only exposes the read surface that AppDelegate / MenuBarMenu / MainWindow
/// need to render correctly.
enum RecordingState: Equatable {
    case idle
    case starting
    case recording(elapsed: TimeInterval)
    case stopping

    var isRecording: Bool {
        switch self {
        case .recording, .stopping, .starting: return true
        case .idle: return false
        }
    }

    var elapsed: TimeInterval {
        if case .recording(let e) = self { return e }
        return 0
    }
}

/// Status payload from `screencap status --json` (Unit 4 schema v1).
struct CLIStatus: Decodable {
    let ok: Bool
    let schemaVersion: Int
    let isRecording: Bool
    let startedAt: Double?
    let elapsed: Double?
    let captureDir: String?
    let claimant: String?
    let warning: String?
    let privacyConfigured: Bool?
    let nlpModelsCached: Bool?

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case isRecording = "is_recording"
        case startedAt = "started_at"
        case elapsed
        case captureDir = "capture_dir"
        case claimant
        case warning
        case privacyConfigured = "privacy_configured"
        case nlpModelsCached = "nlp_models_cached"
    }
}

/// Skeleton recorder controller. Unit 13 implements `start`, `stop`, the stderr
/// event loop, and the Cmd+Q terminate-later semantics.
@MainActor
final class RecorderController: ObservableObject {
    @Published private(set) var state: RecordingState = .idle
    @Published private(set) var lastError: String?

    private weak var index: RecordingsIndex?

    /// Allows Unit 13's `recording_finalized` handler to refresh the cached
    /// recordings list without owning a strong reference. Wired up by
    /// `ScreenCapApp` on first appear.
    func bindIndex(_ index: RecordingsIndex) {
        self.index = index
    }

    /// Surface used by MenuBarMenu / MainWindow Start buttons. Wired up in Unit 13.
    func start(name: String? = nil) {
        // Implemented in Unit 13.
    }

    /// In-app Stop button path (30s wall-clock fallback). Wired up in Unit 13.
    func stop() {
        // Implemented in Unit 13.
    }

    /// Cmd+Q path: returns `.terminateLater` while finalization runs (5min ceiling).
    /// Unit 13 implements the NSAlert prompt and `stopped` event wait.
    func confirmQuitWhileRecording() -> NSApplication.TerminateReply {
        .terminateNow
    }

    /// Convenience used by Unit 9 for the smoke test: round-trips `screencap status`.
    func smokeStatus() async -> CLIStatus? {
        do {
            return try await CLIClient.runJSON(["status", "--json"])
        } catch {
            lastError = error.localizedDescription
            return nil
        }
    }
}
