import Combine
import Foundation

/// Cache for `screencap list --json` output. Reloaded on init and on demand
/// (`refresh()` runs when the `recording_finalized` event fires).
///
/// This is intentionally a state-only ObservableObject — no lifecycle, no
/// timers. Consumers: the Library grid (U5) and the Journal's day sections
/// (U8), both reading `recordings` and deriving their own groupings.
@MainActor
final class RecordingsIndex: ObservableObject {
    /// Why the last load failed — lets the Library grid (U5) distinguish a
    /// stale daemon left over after an app update (HTTP 5xx → offer "Restart
    /// helper") from a plain transport failure (offer "Retry"). `nil` when the
    /// last load succeeded.
    enum LoadErrorKind: Equatable {
        /// A reachable daemon returned HTTP 5xx — the SCR-121 stale-daemon case
        /// (old bundle still serving after an app update). Restarting it heals.
        case staleDaemon
        /// Neither the daemon nor the CLI fallback could produce data.
        case unreachable
        /// Any other failure (decode error, unexpected status).
        case other
    }

    @Published private(set) var recordings: [RecordingSummary] = []
    @Published private(set) var isLoading: Bool = false
    @Published private(set) var lastError: String?
    /// Classification of `lastError` for the Library error state (U5). `nil`
    /// whenever `lastError` is `nil`.
    @Published private(set) var lastErrorKind: LoadErrorKind?
    /// True when the most recent successful load came from the `screencap list
    /// --json` CLI because the daemon socket was unreachable — the Library shows
    /// a non-error "running without the background helper" advisory (U5). Reset
    /// to false whenever the daemon path serves the load.
    @Published private(set) var usingCLIFallback: Bool = false

    private var uploadSucceededObserver: NSObjectProtocol?

    init(autoload: Bool = true) {
        if autoload {
            Task { await refresh() }
        }
        // Plan U9: refresh after a successful upload from any review window
        // so the source row's `isUploadEligible` predicate flips off
        // `uploaded` and the Upload button disappears on the next render.
        // ReviewWindow posts this notification via LiveReviewWindowEffects.
        uploadSucceededObserver = NotificationCenter.default.addObserver(
            forName: .reviewWindowUploadSucceeded,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                await self?.refresh()
            }
        }
    }

    deinit {
        if let uploadSucceededObserver {
            NotificationCenter.default.removeObserver(uploadSucceededObserver)
        }
    }

    func refresh() async {
        guard !isLoading else { return }
        isLoading = true
        defer { isLoading = false }
        do {
            let rows: [RecordingSummary]
            do {
                rows = try await DaemonClient.recordingList().recordings
                usingCLIFallback = false
            } catch DaemonClientError.socketUnavailable, DaemonClientError.connectionFailed {
                rows = try await CLIClient.runJSON(["list", "--json"])
                usingCLIFallback = true
            }
            recordings = rows
            lastError = nil
            lastErrorKind = nil
        } catch {
            // Clear the cache even on failure so a delete-everything sweep
            // doesn't leave stale cards in the Library. The Library detail
            // distinguishes "loading", "error", and "empty" so the user sees
            // a banner with a Retry button instead of a misleading welcome
            // state.
            recordings = []
            lastError = error.localizedDescription
            lastErrorKind = Self.classify(error)
        }
    }

    /// Map a load failure to the actionable kind the Library error state keys on
    /// (U5). A reachable-but-broken daemon (HTTP 5xx) is the stale-after-update
    /// case; a socket/connection failure is unreachable; everything else is other.
    static func classify(_ error: Error) -> LoadErrorKind {
        switch error {
        case DaemonClientError.httpError(let status, _) where status >= 500:
            return .staleDaemon
        case DaemonClientError.socketUnavailable, DaemonClientError.connectionFailed:
            return .unreachable
        default:
            return .other
        }
    }

    /// Lets the UI dismiss a stale error banner without triggering another
    /// refresh. Used by the "Dismiss" button in MainWindow's error state.
    func clearError() {
        lastError = nil
        lastErrorKind = nil
    }
}
