import Foundation
import XCTest
@testable import ScreenCap

/// SCR-178 U8 — drives `SearchViewModel`'s backfill affordance state machine
/// against a fake `BackfillService` (no live socket, no daemon). Covers the
/// offer → accept → starting → indexing → terminal flow, the `skipped`-is-not-an-
/// error rule, cancel + resume, paused, start-failure, subscribe-before-start
/// ordering, Skip persistence, and the terminal-only VoiceOver announcement.
@MainActor
final class SearchViewModelBackfillTests: XCTestCase {

    /// A controllable fake: the test pushes progress events into a continuation,
    /// records the order of `progressEvents()` vs `start()`, and can make `start`
    /// throw. Counts seen `start`/`cancel` calls so "no job started" is assertable.
    private final class FakeBackfillService: BackfillService, @unchecked Sendable {
        // Recorded call order: "subscribe" on progressEvents(), "start", "cancel".
        private(set) var callOrder: [String] = []
        var startError: Error?
        /// Snapshot `start()` resolves to (the seed snapshot — usually total==0).
        var startStatus = BackfillStatus(state: .running, total: 0)
        var cancelStatus = BackfillStatus(state: .cancelled, done: 1, total: 3)

        private var continuation: AsyncStream<BackfillProgressEvent>.Continuation?

        func progressEvents() -> AsyncStream<BackfillProgressEvent> {
            callOrder.append("subscribe")
            // Re-subscribe (resume) drops the prior stream, mirroring the live
            // service where a new subscription supersedes the old one — so the
            // prior run's consumer loop ends instead of dangling.
            continuation?.finish()
            return AsyncStream { cont in
                self.continuation = cont
            }
        }

        func start() async throws -> BackfillStatus {
            callOrder.append("start")
            if let startError { throw startError }
            return startStatus
        }

        func status() async throws -> BackfillStatus { startStatus }

        func cancel() async throws -> BackfillStatus {
            callOrder.append("cancel")
            return cancelStatus
        }

        /// Push a progress event onto the subscribed stream.
        func emit(_ event: BackfillProgressEvent) {
            continuation?.yield(event)
        }
    }

    private func progress(
        _ type: String,
        state: BackfillState,
        done: Int = 0,
        skipped: Int = 0,
        failed: Int = 0,
        total: Int = 0,
        unit: Int = 0
    ) -> BackfillProgressEvent {
        // Build via JSON so we exercise the real Codable wire shape.
        let json = """
        {"type":"\(type)","state":"\(state.rawValue)","done":\(done),"skipped":\(skipped),\
        "failed":\(failed),"total":\(total),"current_unit_index":\(unit)}
        """
        return try! JSONDecoder().decode(BackfillProgressEvent.self, from: Data(json.utf8))
    }

    private final class PersistRecorder: @unchecked Sendable {
        var calls = 0
    }

    private func makeVM(
        _ fake: FakeBackfillService,
        persist: PersistRecorder = PersistRecorder()
    ) -> SearchViewModel {
        SearchViewModel(
            service: SearchViewModelBackfillTests.NoopSearchService(),
            backfill: fake,
            persistBackfillDeclined: { persist.calls += 1 }
        )
    }

    private final class NoopSearchService: SearchService, @unchecked Sendable {
        func contentSearch(_ req: ContentSearchRequest) async throws -> ContentSearchResponse {
            ContentSearchResponse(hits: [], indexState: .noMatch)
        }
        func transcriptSearch(_ req: TranscriptSearchRequest) async throws -> TranscriptSearchResponse {
            TranscriptSearchResponse(hits: [], coverage: .bestEffort)
        }
        func timelineQuery(_ req: TimelineQueryRequest) async throws -> TimelineQueryResponse {
            TimelineQueryResponse(rows: [], coverage: .authoritative)
        }
    }

    /// Poll until `predicate` holds (or time out) — the run task and stream
    /// consumption hop the main actor, so transitions land asynchronously.
    private func wait(
        for vm: SearchViewModel,
        timeout: TimeInterval = 2,
        _ predicate: @escaping (SearchViewModel.BackfillUIState) -> Bool
    ) async {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if predicate(vm.backfillState) { return }
            try? await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail("Timed out waiting for backfill state; was \(vm.backfillState)")
    }

    // MARK: - Tests

    func testOfferThenAcceptGoesStartingThenIndexingThenDone() async {
        let fake = FakeBackfillService()
        let vm = makeVM(fake)

        vm.offerBackfill(alreadyDeclined: false)
        XCTAssertEqual(vm.backfillState, .offering)

        vm.acceptBackfill()
        // Before any event with total>0, we are in `starting` (indeterminate).
        await wait(for: vm) { $0 == .starting }

        fake.emit(progress("backfill.progress", state: .running, done: 1, total: 3))
        await wait(for: vm) { $0 == .indexing(done: 1, total: 3, failed: 0) }

        fake.emit(progress("backfill.completed", state: .completed, done: 3, total: 3))
        await wait(for: vm) { $0 == .done(done: 3, total: 3, failed: 0) }
    }

    func testStartingBeforeFirstEventIsIndeterminateNotZeroZero() async {
        let fake = FakeBackfillService()
        let vm = makeVM(fake)
        vm.acceptBackfill()
        await wait(for: vm) { $0 == .starting }
        // Never a determinate 0/0 before the first total>0 event.
        XCTAssertEqual(vm.backfillState, .starting)
    }

    func testPartialFailureRendersHedgedDoneAndSkippedIsNotError() async {
        let fake = FakeBackfillService()
        let vm = makeVM(fake)
        vm.acceptBackfill()
        await wait(for: vm) { $0 == .starting }

        // skipped>0 alone is NOT an error: stays plain indexing/done.
        fake.emit(progress("backfill.progress", state: .running, done: 2, skipped: 5, total: 4))
        await wait(for: vm) { $0 == .indexing(done: 2, total: 4, failed: 0) }

        fake.emit(progress("backfill.completed", state: .completed, done: 3, skipped: 5, failed: 1, total: 4))
        await wait(for: vm) { $0 == .done(done: 3, total: 4, failed: 1) }
        // Hedged copy reflects the failure; skipped never appears as an error.
        XCTAssertEqual(
            SearchAccessibility.backfillAnnouncement(for: vm.backfillState),
            "Indexed 3 of 4 recordings. 1 could not be indexed."
        )
    }

    func testCancelThenResume() async {
        let fake = FakeBackfillService()
        let vm = makeVM(fake)
        vm.acceptBackfill()
        await wait(for: vm) { $0 == .starting }
        fake.emit(progress("backfill.progress", state: .running, done: 1, total: 3))
        await wait(for: vm) { $0 == .indexing(done: 1, total: 3, failed: 0) }

        vm.cancelBackfill()
        await wait(for: vm) { $0 == .cancelled(done: 1, total: 3) }
        await wait(for: vm) { _ in fake.callOrder.contains("cancel") }

        // Resume re-subscribes + re-starts; a fresh event returns us to indexing.
        vm.resumeBackfill()
        await wait(for: vm) { $0 == .starting }
        fake.emit(progress("backfill.progress", state: .running, done: 2, total: 3))
        await wait(for: vm) { $0 == .indexing(done: 2, total: 3, failed: 0) }
    }

    func testPausedRendersResumeAffordance() async {
        let fake = FakeBackfillService()
        let vm = makeVM(fake)
        vm.acceptBackfill()
        await wait(for: vm) { $0 == .starting }

        fake.emit(progress("backfill.paused", state: .paused, done: 2, total: 10))
        await wait(for: vm) { $0 == .paused(done: 2, total: 10) }
    }

    func testStartFailureGoesToStartFailed() async {
        let fake = FakeBackfillService()
        fake.startError = DaemonClientError.socketUnavailable(path: "/x")
        let vm = makeVM(fake)
        vm.acceptBackfill()
        await wait(for: vm) { $0 == .startFailed }
        // Search remains usable: a start failure does not touch `phase`.
        XCTAssertEqual(vm.phase, .idle)
    }

    func testSubscribesBeforeStarting() async {
        let fake = FakeBackfillService()
        let vm = makeVM(fake)
        vm.acceptBackfill()
        await wait(for: vm) { _ in fake.callOrder.contains("start") }
        XCTAssertEqual(Array(fake.callOrder.prefix(2)), ["subscribe", "start"])
    }

    func testSkipPersistsDeclineAndStartsNoJob() async {
        let fake = FakeBackfillService()
        let persist = PersistRecorder()
        let vm = makeVM(fake, persist: persist)
        vm.offerBackfill(alreadyDeclined: false)

        vm.skipBackfill()
        XCTAssertEqual(vm.backfillState, .hidden)

        // The decline is persisted (so it doesn't re-prompt) and no run started.
        let deadline = Date().addingTimeInterval(1)
        while persist.calls == 0 && Date() < deadline {
            try? await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTAssertEqual(persist.calls, 1)
        XCTAssertFalse(fake.callOrder.contains("start"))
    }

    func testOfferSuppressedWhenAlreadyDeclined() {
        let fake = FakeBackfillService()
        let vm = makeVM(fake)
        vm.offerBackfill(alreadyDeclined: true)
        XCTAssertEqual(vm.backfillState, .hidden)
    }

    func testTerminalTransitionsAnnounceInProgressTicksDoNot() {
        // In-progress ticks are silent (no announcement flooding).
        XCTAssertNil(SearchAccessibility.backfillAnnouncement(for: .hidden))
        XCTAssertNil(SearchAccessibility.backfillAnnouncement(for: .offering))
        XCTAssertNil(SearchAccessibility.backfillAnnouncement(for: .starting))
        XCTAssertNil(SearchAccessibility.backfillAnnouncement(for: .indexing(done: 1, total: 3, failed: 0)))

        // Every terminal state returns a non-nil phrase.
        XCTAssertNotNil(SearchAccessibility.backfillAnnouncement(for: .done(done: 3, total: 3, failed: 0)))
        XCTAssertNotNil(SearchAccessibility.backfillAnnouncement(for: .paused(done: 1, total: 3)))
        XCTAssertNotNil(SearchAccessibility.backfillAnnouncement(for: .cancelled(done: 1, total: 3)))
        XCTAssertNotNil(SearchAccessibility.backfillAnnouncement(for: .startFailed))

        XCTAssertEqual(
            SearchAccessibility.backfillAnnouncement(for: .done(done: 3, total: 3, failed: 0)),
            "Done \u{2014} your recording history is now searchable."
        )
    }
}
