import Combine
import Foundation

/// The `download` block of `screencap model status --json` (SCR-239 U6). Byte
/// counters + a state string; no recording context.
struct ModelDownloadStatus: Decodable, Equatable {
    let state: String
    let bytesDone: Int
    let bytesTotal: Int
    let reason: String?

    enum CodingKeys: String, CodingKey {
        case state
        case bytesDone = "bytes_done"
        case bytesTotal = "bytes_total"
        case reason
    }
}

/// The `installed` block of `screencap model status --json` — which models are
/// installed and their disclosed sizes, for the pane's "Downloaded model" row.
struct ModelInstalledInfo: Decodable, Equatable {
    struct Model: Decodable, Equatable {
        let modelId: String
        let sizeBytes: Int
        let installed: Bool
        enum CodingKeys: String, CodingKey {
            case modelId = "model_id"
            case sizeBytes = "size_bytes"
            case installed
        }
    }
    let models: [Model]
}

/// Outer shape emitted by `screencap model status --json`.
struct ModelStatusEnvelope: Decodable {
    let download: ModelDownloadStatus
    let installed: ModelInstalledInfo
}

/// The download lifecycle the pane renders (U10, KTD11). Mirrors the
/// `DaemonInstallController` state-machine shape; `failed` carries the backend
/// cause (disk / network / integrity) so the pane can surface it with Retry.
enum ModelDownloadState: Equatable {
    case idle
    case downloading(bytesDone: Int, bytesTotal: Int)
    case installed
    case failed(reason: String)
    case cancelled

    init(_ status: ModelDownloadStatus) {
        switch status.state {
        case "downloading":
            self = .downloading(bytesDone: status.bytesDone, bytesTotal: status.bytesTotal)
        case "installed":
            self = .installed
        case "failed":
            self = .failed(reason: status.reason ?? "unknown")
        case "cancelled":
            self = .cancelled
        default:
            self = .idle
        }
    }

    var isDownloading: Bool {
        if case .downloading = self { return true }
        return false
    }

    /// Download progress in [0, 1], or nil when total is unknown.
    var fractionComplete: Double? {
        if case let .downloading(done, total) = self, total > 0 {
            return Double(done) / Double(total)
        }
        return nil
    }
}

/// Drives the opt-in model download from the Intelligence pane (U10). Mirrors
/// `IntelligenceController`'s pluggable CLI-invoker seam so the state machine is
/// unit-testable without a running daemon. Actions issue the `model` CLI verbs;
/// state comes from polling `model status --json`.
@MainActor
final class ModelDownloadController: ObservableObject {
    @Published private(set) var state: ModelDownloadState = .idle
    @Published private(set) var installed: [ModelInstalledInfo.Model] = []
    @Published private(set) var lastError: String?

    typealias JSONInvoker = @Sendable ([String]) async throws -> Data
    private let invoke: JSONInvoker

    init(invoke: @escaping JSONInvoker = ModelDownloadController.defaultInvoke) {
        self.invoke = invoke
    }

    static let defaultInvoke: JSONInvoker = { args in
        try await CLIClient.runJSONRaw(args)
    }

    /// Whether the default (only) model is installed — drives the pane's row.
    var isDefaultModelInstalled: Bool {
        installed.first?.installed ?? false
    }

    /// The disclosed size (bytes) of the default model, for size disclosure.
    var disclosedSizeBytes: Int? {
        installed.first?.sizeBytes
    }

    /// Re-fetch the download + install state. A failure surfaces in `lastError`
    /// and leaves the last state intact (a transient CLI hiccup doesn't blank it).
    func refreshStatus() async {
        do {
            let data = try await invoke(["model", "status", "--json"])
            let env = try JSONDecoder().decode(ModelStatusEnvelope.self, from: data)
            installed = env.installed.models
            // An `installed` download-state is authoritative; otherwise reflect
            // the install flag so a fresh pane (idle download) shows "installed".
            if env.download.state == "idle", isDefaultModelInstalled {
                state = .installed
            } else {
                state = ModelDownloadState(env.download)
            }
            lastError = nil
        } catch {
            lastError = error.localizedDescription
        }
    }

    /// Start the opt-in download, then reconcile state. A start that immediately
    /// fails (e.g. insufficient disk) surfaces as `.failed` via the status read.
    func startDownload(modelID: String? = nil) async {
        do {
            var args = ["model", "download"]
            if let modelID { args.append(modelID) }
            _ = try await invoke(args)
            lastError = nil
        } catch {
            lastError = error.localizedDescription
        }
        await refreshStatus()
    }

    /// Cancel an in-flight download; state converges to `.cancelled` / `.idle`.
    func cancel() async {
        do {
            _ = try await invoke(["model", "cancel"])
            lastError = nil
        } catch {
            lastError = error.localizedDescription
        }
        await refreshStatus()
    }
}
