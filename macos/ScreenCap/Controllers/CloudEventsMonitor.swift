import Foundation
import OSLog

private let cloudEventsLogger = Logger(subsystem: "com.screencap.macos", category: "cloud-events")

/// Watches `/v0/events` for `account_mismatch` and surfaces a re-login prompt
/// (U9 / R16), using the replay-cursor pattern (KTD7): the bus cursor is
/// captured BEFORE the subscribe so a mismatch published in the subscribe gap is
/// still replayed from that cursor rather than lost. `account_mismatch` is
/// ephemeral and carried in no snapshot, so a 410 (`cursor_unknown`, the replay
/// ring aged the cursor out) reconciles the mismatch DIRECTLY — comparing the
/// signed-in uid against the in-flight recording's owner uid — instead of
/// refetching a snapshot that could never carry the event.
///
/// The socket/cursor/stream seams are injected so the loop is unit-testable
/// without a real daemon (mirrors `DaemonSessionService`'s callbacks pattern).
@MainActor
final class CloudEventsMonitor {
    /// Returns the current bus cursor (e.g. off `session.snapshot`). Captured
    /// before every subscribe.
    typealias CursorSource = @Sendable () async throws -> Int
    /// Opens the `/v0/events?since=<cursor>` stream.
    typealias EventStream = @Sendable (_ sinceCursor: Int) -> AsyncThrowingStream<RecorderEventLine, Error>
    /// The 410 reconcile: compare the signed-in uid vs the in-flight recording's
    /// owner uid and return a mismatch if they differ. Nil when nothing is
    /// in-flight or the uids match.
    typealias Reconciler = @Sendable () async -> AccountMismatch?

    private let captureCursor: CursorSource
    private let subscribe: EventStream
    private let reconcile: Reconciler
    private let onMismatch: @MainActor (AccountMismatch) -> Void

    init(
        captureCursor: @escaping CursorSource,
        subscribe: @escaping EventStream,
        reconcile: @escaping Reconciler,
        onMismatch: @escaping @MainActor (AccountMismatch) -> Void
    ) {
        self.captureCursor = captureCursor
        self.subscribe = subscribe
        self.reconcile = reconcile
        self.onMismatch = onMismatch
    }

    /// Run ONE capture→subscribe cycle to completion (the stream ending, an
    /// error, or cancellation). On `cursor_unknown` (410) it reconciles directly
    /// and returns — the caller (`run`) re-enters with a fresh cursor. Isolated
    /// as a unit so the replay-race and 410 paths are directly testable.
    func runOnce() async {
        let cursor: Int
        do {
            cursor = try await captureCursor()
        } catch {
            if Task.isCancelled { return }
            cloudEventsLogger.debug("cursor capture failed: \(String(describing: error), privacy: .public)")
            return
        }
        do {
            for try await event in subscribe(cursor) {
                if Task.isCancelled { return }
                if let mismatch = AccountMismatch.from(event: event) {
                    onMismatch(mismatch)
                }
            }
        } catch let DaemonClientError.envelopeError(code, _) where code == "cursor_unknown" {
            // The replay window evicted the captured cursor. The event is gone;
            // reconcile the mismatch directly from the current uids instead.
            if Task.isCancelled { return }
            cloudEventsLogger.debug("event cursor aged out; reconciling account mismatch directly")
            if let mismatch = await reconcile() {
                onMismatch(mismatch)
            }
        } catch {
            if Task.isCancelled { return }
            cloudEventsLogger.debug("event stream ended: \(String(describing: error), privacy: .public)")
        }
    }

    /// Long-lived driver: re-runs `runOnce` (fresh cursor each cycle) with a
    /// small backoff between cycles until cancelled. The reconnect cadence is
    /// deliberately unhurried — `account_mismatch` is rare and advisory.
    func run(reconnectDelayNanoseconds: UInt64 = 2_000_000_000) async {
        while !Task.isCancelled {
            await runOnce()
            if Task.isCancelled { return }
            try? await Task.sleep(nanoseconds: reconnectDelayNanoseconds)
        }
    }
}
