import Foundation

// SCR-178 U8 — test seam for the content-index backfill verbs + progress
// stream, mirroring `SearchService` (the established repo pattern for wrapping
// `DaemonClient` behind a protocol). `SearchViewModel`'s backfill affordance
// state machine depends on this protocol so it can be unit-tested against a
// fake — no live UNIX socket, no real daemon.
//
// The progress surface is intentionally `BackfillProgressEvent`-shaped: a
// count-only + opaque-ordinal payload (R9 — no recording directory name ever
// crosses the EventBus). `progressEvents()` is called BEFORE `start()` so the
// listener is attached before the run seeds its closed set, avoiding the
// late-listener race (see the EventBus replay learning).

protocol BackfillService: Sendable {
    /// Subscribe to the run's progress before triggering it. The returned stream
    /// yields one element per progress tick and terminates after a terminal
    /// event (completed / paused / cancelled / failed). Call this *before*
    /// `start()` to avoid missing the initial events.
    func progressEvents() -> AsyncStream<BackfillProgressEvent>
    /// Start (or resume) the backfill. Idempotent daemon-side.
    func start() async throws -> BackfillStatus
    /// Current privacy-safe snapshot.
    func status() async throws -> BackfillStatus
    /// Signal the in-flight run to stop; resolves to the resulting snapshot.
    func cancel() async throws -> BackfillStatus
}

/// Live implementation: forwards to the daemon over the UNIX socket. The progress
/// stream subscribes to `/v0/events` (with no cursor, so the daemon's replay
/// buffer delivers events published just after the subscription opens) and
/// re-decodes each streamed line through `DaemonClient.backfillProgressEvent`,
/// surfacing only `backfill.*` events and terminating on the first terminal one.
struct LiveBackfillService: BackfillService {
    func progressEvents() -> AsyncStream<BackfillProgressEvent> {
        AsyncStream { continuation in
            let task = Task {
                do {
                    for try await line in DaemonClient.subscribeRawLines() {
                        guard let event = DaemonClient.backfillProgressEvent(fromLine: line) else {
                            continue
                        }
                        continuation.yield(event)
                        if event.isTerminal {
                            continuation.finish()
                            return
                        }
                    }
                    continuation.finish()
                } catch {
                    // A dropped subscription is non-fatal: the affordance falls
                    // back to its last known state and the user can re-check via
                    // a fresh search; never surface a stream error as a crash.
                    continuation.finish()
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    func start() async throws -> BackfillStatus {
        try await DaemonClient.backfillStart().status
    }

    func status() async throws -> BackfillStatus {
        try await DaemonClient.backfillStatus().status
    }

    func cancel() async throws -> BackfillStatus {
        try await DaemonClient.backfillCancel().status
    }
}
