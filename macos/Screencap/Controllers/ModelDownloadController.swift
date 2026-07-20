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

    /// KTD3 — the daemon-reachability input to the on-device row's state
    /// matrix. Set when a `refreshStatus` read fails (the invocation throws or
    /// its payload doesn't decode), cleared by any successful read. Distinct
    /// from `lastError`, which also carries write-verb failures: the pane
    /// renders the disabled-download row from this flag alone, separate from
    /// the whole-pane error state a failed `IntelligenceController.refresh()`
    /// produces.
    @Published private(set) var daemonUnreachable: Bool = false

    typealias JSONInvoker = @Sendable ([String]) async throws -> Data
    private let invoke: JSONInvoker

    /// The status-poll loop that runs only while a download is in flight; nil when
    /// idle/terminal. See ``startPollingIfNeeded()``.
    private var pollTask: Task<Void, Never>?

    init(invoke: @escaping JSONInvoker = ModelDownloadController.defaultInvoke) {
        self.invoke = invoke
    }

    deinit {
        pollTask?.cancel()
    }

    static let defaultInvoke: JSONInvoker = { args in
        // `model status --json` returns a JSON envelope; the write verbs
        // (`model download` / `model cancel`) emit no JSON, so route them through
        // the non-asserting runner — `runJSONRaw` asserts `--json` is present and
        // would abort the app in Debug when a write verb (no `--json`) is passed.
        if args.contains("--json") {
            return try await CLIClient.runJSONRaw(args)
        }
        try await CLIClient.runAwaitingExit(args)
        return Data()
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
            daemonUnreachable = false
            // KTD8 — a download started elsewhere (onboarding) must animate
            // here too: observing `.downloading` starts the poll loop, not just
            // `startDownload`. No-op when already polling or not downloading.
            startPollingIfNeeded()
        } catch {
            lastError = error.localizedDescription
            daemonUnreachable = true
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
        startPollingIfNeeded()
    }

    /// Cancel an in-flight download; state converges to `.cancelled` / `.idle`.
    func cancel() async {
        stopPolling()
        do {
            _ = try await invoke(["model", "cancel"])
            lastError = nil
        } catch {
            lastError = error.localizedDescription
        }
        await refreshStatus()
        // The daemon-side cancel converges lazily (the underlying snapshot
        // download is not interruptible mid-transfer), so the post-cancel
        // status read can still say `downloading` — and refreshStatus's KTD8
        // auto-start would resurrect the loop the user just stopped. A later
        // refresh (pane focus/reopen, retry) restarts polling if the download
        // genuinely continues.
        stopPolling()
    }

    /// Test seam (U2) — whether the status-poll loop is active. Internal so
    /// `@testable` tests can pin KTD8 (a refresh that observes `.downloading`
    /// leaves the loop running) without reaching into the private task.
    var isPolling: Bool { pollTask != nil }

    /// Poll `model status` on a ~1s cadence while a download is in flight so the
    /// pane's ProgressView advances. The `model download` verb returns immediately
    /// (the daemon job runs in the background), so without this the bar would
    /// freeze at its first reading. Self-cancels once state leaves `.downloading`.
    private func startPollingIfNeeded() {
        guard state.isDownloading, pollTask == nil else { return }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 1_000_000_000)
                guard let self, !Task.isCancelled else { return }
                await self.refreshStatus()
                if !self.state.isDownloading {
                    self.stopPolling()
                    return
                }
            }
        }
    }

    private func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
    }
}
