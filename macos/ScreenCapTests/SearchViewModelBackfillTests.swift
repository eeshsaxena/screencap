import Foundation
import XCTest
@testable import ScreenCap

/// SCR-178 U8 — drives `SearchViewModel`'s backfill affordance state machine
/// against a fake `BackfillService` (no live socket, no daemon). Covers the
/// offer → accept → starting → indexing → terminal flow, the `skipped`-is-not-an-
/// error rule, cancel + resume, paused, start-failure, subscribe-before-start
/// ordering, and Skip persistence. The Recall palette renders these states
/// (U10); the retired Search pane's VoiceOver announcement builder is gone.
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

    /// Records every value written through the injected declined-flag seam
    /// (Skip persists `true`; the empty-state Index-now accept clears with
    /// `false` — SCR-261 U3).
    private final class PersistRecorder: @unchecked Sendable {
        var values: [Bool] = []
    }

    private func makeVM(
        _ fake: FakeBackfillService,
        persist: PersistRecorder = PersistRecorder()
    ) -> SearchViewModel {
        SearchViewModel(
            service: SearchViewModelBackfillTests.NoopSearchService(),
            backfill: fake,
            persistBackfillDeclined: { persist.values.append($0) }
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
        func appsList() async throws -> AppsListResponse {
            AppsListResponse(appNames: [], hostnames: [])
        }
        func tasksList(_ req: TasksListRequest) async throws -> TasksListResponse {
            TasksListResponse(recording: req.recording, tasks: [])
        }
        // SCR-214 U11 — trivial write-verb stubs to keep the fake conforming.
        func tasksCreate(_ req: TasksCreateRequest) async throws -> TasksCreateResponse {
            TasksCreateResponse(
                recording: req.recording,
                task: RecordingTask(taskIndex: 0, startTs: req.startTs, endTs: req.endTs, name: req.name)
            )
        }
        func tasksUpdate(_ req: TasksUpdateRequest) async throws -> TasksUpdateResponse {
            TasksUpdateResponse(recording: req.recording, taskIndex: req.taskIndex)
        }
        func tasksDelete(_ req: TasksDeleteRequest) async throws -> TasksDeleteResponse {
            TasksDeleteResponse(recording: req.recording, deleted: true)
        }
        func tasksMerge(_ req: TasksMergeRequest) async throws -> TasksMergeResponse {
            TasksMergeResponse(recording: req.recording, taskIndex: 0)
        }
        func tasksSplit(_ req: TasksSplitRequest) async throws -> TasksSplitResponse {
            TasksSplitResponse(recording: req.recording, taskIndices: [0, 1])
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

    func testPartialFailureReachesDoneWithFailedCountAndSkippedIsNotError() async {
        let fake = FakeBackfillService()
        let vm = makeVM(fake)
        vm.acceptBackfill()
        await wait(for: vm) { $0 == .starting }

        // skipped>0 alone is NOT an error: stays plain indexing/done.
        fake.emit(progress("backfill.progress", state: .running, done: 2, skipped: 5, total: 4))
        await wait(for: vm) { $0 == .indexing(done: 2, total: 4, failed: 0) }

        // The failed count rides the done state (the palette renders the
        // hedged copy from it); skipped never appears as an error.
        fake.emit(progress("backfill.completed", state: .completed, done: 3, skipped: 5, failed: 1, total: 4))
        await wait(for: vm) { $0 == .done(done: 3, total: 4, failed: 1) }
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
        while persist.values.isEmpty && Date() < deadline {
            try? await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTAssertEqual(persist.values, [true])
        XCTAssertFalse(fake.callOrder.contains("start"))
    }

    func testOfferSuppressedWhenAlreadyDeclined() {
        let fake = FakeBackfillService()
        let vm = makeVM(fake)
        vm.offerBackfill(alreadyDeclined: true)
        XCTAssertEqual(vm.backfillState, .hidden)
    }

    // MARK: - SCR-261 U3 — empty-state Index-now (explicit accept) + completion hook

    /// KTD5 pin: the empty-state CTA is an explicit accept, so it must start
    /// the run even for a prior decliner AND clear the persisted declined flag.
    /// This fails if the CTA were routed through `offerBackfill(alreadyDeclined:
    /// true)`, which no-ops to `.hidden` and never reaches `start()`.
    func testEmptyStateIndexNowStartsForPriorDeclinerAndClearsPersistedDecline() async {
        let fake = FakeBackfillService()
        let persist = PersistRecorder()
        let vm = makeVM(fake, persist: persist)

        // A prior decliner: the offer path would no-op…
        vm.offerBackfill(alreadyDeclined: true)
        XCTAssertEqual(vm.backfillState, .hidden)

        // …but the explicit accept starts the run anyway.
        vm.startBackfillFromEmptyState()
        await wait(for: vm) { $0 == .starting }
        await wait(for: vm) { _ in fake.callOrder.contains("start") }
        // The subscribe-before-start ordering holds on this path too.
        XCTAssertEqual(Array(fake.callOrder.prefix(2)), ["subscribe", "start"])

        // And the persisted decline is cleared (false), so a stale flag can't
        // suppress future offers.
        let deadline = Date().addingTimeInterval(1)
        while persist.values.isEmpty && Date() < deadline {
            try? await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTAssertEqual(persist.values, [false])
    }

    /// KTD9: reaching `.done` fires the completion hook exactly once — and
    /// in-progress ticks never fire it.
    func testCompletionHookFiresExactlyOnceOnDone() async {
        let fake = FakeBackfillService()
        let vm = makeVM(fake)
        var fired = 0
        vm.onBackfillCompleted = { fired += 1 }

        vm.acceptBackfill()
        await wait(for: vm) { $0 == .starting }
        fake.emit(progress("backfill.progress", state: .running, done: 1, total: 3))
        await wait(for: vm) { $0 == .indexing(done: 1, total: 3, failed: 0) }
        XCTAssertEqual(fired, 0, "in-progress ticks must not fire the hook")

        fake.emit(progress("backfill.completed", state: .completed, done: 3, total: 3))
        await wait(for: vm) { $0 == .done(done: 3, total: 3, failed: 0) }
        XCTAssertEqual(fired, 1)

        // No further transition → no further fire.
        try? await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(fired, 1)
    }

    /// KTD9: a duplicate `.completed` snapshot must not re-fire the hook — the
    /// `wasDone` guard in `applyStatus` only fires on the transition INTO
    /// `.done`. The run task's drain loop returns on the first terminal event,
    /// so the duplicate is delivered as a completed *seed* status (`start()`
    /// resolves `.completed` → first `applyStatus`) followed by a completed
    /// stream event (→ second `applyStatus`) — the exact "seed status after a
    /// terminal event" shape the guard's comment calls out.
    func testCompletionHookNotRefiredOnDuplicateCompletedStatus() async {
        let fake = FakeBackfillService()
        fake.startStatus = BackfillStatus(state: .completed, done: 3, total: 3)
        let vm = makeVM(fake)
        var fired = 0
        vm.onBackfillCompleted = { fired += 1 }

        vm.acceptBackfill()
        // First `.completed` (the seed snapshot) lands us on `.done` and fires.
        await wait(for: vm) { $0 == .done(done: 3, total: 3, failed: 0) }
        XCTAssertEqual(fired, 1)

        // Second `.completed` (a stream event) reaches applyStatus with the
        // state already `.done` — no re-fire, state stays `.done`.
        fake.emit(progress("backfill.completed", state: .completed, done: 3, total: 3))
        try? await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(fired, 1, "duplicate completed snapshot must not re-fire the hook")
        XCTAssertEqual(vm.backfillState, .done(done: 3, total: 3, failed: 0))
    }

    /// KTD9: done-with-partial-failures is still done — the hook fires (the
    /// index did change; a refresh is warranted).
    func testCompletionHookFiresOnDoneWithFailures() async {
        let fake = FakeBackfillService()
        let vm = makeVM(fake)
        var fired = 0
        vm.onBackfillCompleted = { fired += 1 }

        vm.acceptBackfill()
        await wait(for: vm) { $0 == .starting }
        fake.emit(progress("backfill.completed", state: .completed, done: 2, failed: 1, total: 3))
        await wait(for: vm) { $0 == .done(done: 2, total: 3, failed: 1) }
        XCTAssertEqual(fired, 1)
    }

    /// KTD9: non-done transitions (starting / indexing / cancelled / paused)
    /// never fire the hook.
    func testCompletionHookSilentForNonDoneTransitions() async {
        let fake = FakeBackfillService()
        let vm = makeVM(fake)
        var fired = 0
        vm.onBackfillCompleted = { fired += 1 }

        vm.acceptBackfill()
        await wait(for: vm) { $0 == .starting }
        fake.emit(progress("backfill.progress", state: .running, done: 1, total: 3))
        await wait(for: vm) { $0 == .indexing(done: 1, total: 3, failed: 0) }

        vm.cancelBackfill()
        await wait(for: vm) { $0 == .cancelled(done: 1, total: 3) }

        vm.resumeBackfill()
        await wait(for: vm) { $0 == .starting }
        fake.emit(progress("backfill.paused", state: .paused, done: 2, total: 3))
        await wait(for: vm) { $0 == .paused(done: 2, total: 3) }

        XCTAssertEqual(fired, 0)
    }

}
