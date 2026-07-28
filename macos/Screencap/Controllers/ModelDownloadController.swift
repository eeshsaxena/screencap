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

/// SCR-293 — what a download-*offer* surface should render: the onboarding step
/// and the first-recording beat, both of which present the download on its own
/// rather than as one row in a model picker. Pure over the controller's
/// published state so the two surfaces can't drift and the matrix is testable
/// without a UI test. The Settings pane keeps its own richer matrix
/// (`IntelligenceSelectionModel.onDeviceRowRender`), which folds in the
/// Apple-Intelligence probe these surfaces don't consult.
enum ModelDownloadOffer: Equatable {
    case installed
    case downloading(fractionComplete: Double?)
    /// A download was in flight as of the last good read, but status reads have
    /// stopped landing — the progress bar is stale. Offer recovery instead of a
    /// frozen bar that still reads as advancing.
    ///
    /// Scoped to *unreadable status*, *not* a hung transfer: a download that is
    /// genuinely stuck while `model status` keeps answering renders as a healthy
    /// `.downloading` bar here. Detecting that is SCR-292, daemon-side.
    case stalled
    /// The CLI can't be reached at all, so there is no download to offer — an
    /// enabled button that silently does nothing is the same swallowed error.
    case unavailable
    case failed(reason: String)
    case offer

    /// The user-facing reason for `.stalled`. "Background helper" is the app's
    /// established word for the daemon (`DaysView`); "may have stopped" is the
    /// honest claim — the download's real state is exactly what we can't read.
    static let stalledReason =
        "Lost contact with the background helper — this download may have stopped."

    /// The user-facing reason for `.unavailable`. Mirrors what the Settings
    /// pane says through `IntelligenceSelectionModel.startDaemonDownloadReason`,
    /// in these surfaces' own vocabulary (they never say "daemon").
    static let unavailableReason =
        "Can't reach the background helper — the download can't start right now."

    static func render(
        state: ModelDownloadState, progressStale: Bool, daemonUnreachable: Bool
    ) -> ModelDownloadOffer {
        switch state {
        case .installed:
            return .installed
        case .downloading:
            // Stale outranks the bar: a frozen ProgressView is the lie, since
            // it's indistinguishable from a slow but healthy download.
            return progressStale ? .stalled : .downloading(fractionComplete: state.fractionComplete)
        case let .failed(reason):
            return .failed(reason: reason)
        case .idle, .cancelled:
            return daemonUnreachable ? .unavailable : .offer
        }
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

    /// SCR-293 — consecutive failed CLI round-trips since the last good one,
    /// across every verb. Published because the stale-progress signal derived
    /// from it changes while `state` deliberately does not: without this, a
    /// download whose status reads have dried up emits no change at all and the
    /// surfaces keep rendering a frozen bar. Deliberately separate from
    /// `daemonUnreachable`, which keeps its first-failure semantics for the
    /// Settings pane's KTD3 matrix.
    @Published private(set) var consecutiveCLIFailures: Int = 0

    /// Consecutive failures before a rendered download counts as stale. The
    /// poll cadence is ~1s, so a single miss is ordinary jitter; requiring two
    /// keeps one blip from flashing an error over a healthy download.
    static let staleProgressFailureThreshold = 2

    /// SCR-293 — the rendered download progress can no longer be trusted: a
    /// download was in flight as of the last good read, but the CLI has stopped
    /// answering since. Surfaces that render `.downloading` must show recovery
    /// instead of a bar frozen at its last reading.
    var isDownloadProgressStale: Bool {
        guard state.isDownloading else { return false }
        // The debounce buys its second opinion from the poll loop's next tick.
        // With no loop left — an explicit Cancel stops it and KTD8 forbids
        // resurrecting it — there is no next tick, so one failed read is already
        // terminal and waiting for a second would just restore the frozen bar
        // this whole signal exists to remove.
        let threshold = isPolling ? Self.staleProgressFailureThreshold : 1
        return consecutiveCLIFailures >= threshold
    }

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
            consecutiveCLIFailures = 0
            // KTD8 — a download started elsewhere (onboarding) must animate
            // here too: observing `.downloading` starts the poll loop, not just
            // `startDownload`. No-op when already polling or not downloading.
            startPollingIfNeeded()
        } catch {
            lastError = error.localizedDescription
            daemonUnreachable = true
            consecutiveCLIFailures += 1
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
            consecutiveCLIFailures = 0
        } catch {
            lastError = error.localizedDescription
            consecutiveCLIFailures += 1
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
            consecutiveCLIFailures = 0
        } catch {
            // SCR-293 — a failed cancel counts toward staleness precisely
            // because this path also `stopPolling()`s: with the CLI down, the
            // state stays `.downloading` and the loop that would have healed it
            // is gone, so the surfaces must reach `.stalled` (and its Retry)
            // from here rather than sitting on a frozen bar forever.
            lastError = error.localizedDescription
            consecutiveCLIFailures += 1
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

    /// Healthy poll cadence — fast enough that the ProgressView reads as smooth.
    private static let pollIntervalNanos: UInt64 = 1_000_000_000
    /// Ceiling for the backed-off cadence during a sustained outage.
    private static let maxPollIntervalNanos: UInt64 = 30_000_000_000

    /// SCR-293 — the delay before the next status read. Test seam: internal so
    /// `@testable` tests can pin the curve without running a real timer.
    ///
    /// A healthy download keeps the ~1s cadence, and so does the debounce window
    /// (the second opinion `isDownloadProgressStale` waits for must arrive
    /// promptly, or the frozen bar just lasts longer). Past the threshold there
    /// is no longer anything to animate and every attempt spawns a CLI
    /// subprocess that can burn its full 10s timeout, so back off exponentially
    /// to a 30s ceiling. Any successful read zeroes the counter and restores 1s.
    var nextPollDelayNanos: UInt64 {
        let overage = consecutiveCLIFailures - Self.staleProgressFailureThreshold
        guard overage >= 0 else { return Self.pollIntervalNanos }
        // Clamp the shift before it can overflow on a long outage.
        let shift = UInt64(min(overage + 1, 5))
        return min(Self.pollIntervalNanos << shift, Self.maxPollIntervalNanos)
    }

    /// Poll `model status` while a download is in flight so the pane's
    /// ProgressView advances. The `model download` verb returns immediately (the
    /// daemon job runs in the background), so without this the bar would freeze
    /// at its first reading. Cadence comes from ``nextPollDelayNanos``.
    /// Self-cancels once state leaves `.downloading`.
    private func startPollingIfNeeded() {
        guard state.isDownloading, pollTask == nil else { return }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                // Read the cadence weakly so the loop never holds `self` across
                // the sleep — `self` owns this task, and a strong capture would
                // keep both alive past `deinit`.
                guard let delay = self?.nextPollDelayNanos else { return }
                try? await Task.sleep(nanoseconds: delay)
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
