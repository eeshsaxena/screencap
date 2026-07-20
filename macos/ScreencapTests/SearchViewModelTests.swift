import Combine
import Foundation
import XCTest
@testable import Screencap

/// SCR-174 U4 — drives `SearchViewModel` against a fake `SearchService` (no live
/// socket). Covers fan-out, client-side time filtering, transcript→timeline
/// correlation, recency ranking, the coverage matrix, consent-trigger
/// derivation, partial-stream failure, and daemon-down.
@MainActor
final class SearchViewModelTests: XCTestCase {
    private final class FakeSearchService: SearchService, @unchecked Sendable {
        var contentResponse: ContentSearchResponse?
        var contentError: Error?
        var transcriptResponse: TranscriptSearchResponse?
        var transcriptError: Error?
        var timelineResponse: TimelineQueryResponse?
        var timelineError: Error?
        /// Recording dir -> event timestamps returned for the per-recording
        /// correlation query (req.recording != nil).
        var perRecordingTimestamps: [String: [Int]] = [:]
        /// SCR-182 U1 — fired at the top of every `timelineQuery` so a test can
        /// cancel the surrounding search mid-flight (latest-wins coverage).
        var onTimelineQuery: (@Sendable () -> Void)?
        /// SCR-182 U1 — count of per-recording correlation queries issued, to
        /// assert a superseded search abandons its transcript fan-out.
        var perRecordingQueryCount = 0
        /// SCR-179 U5 — vocabulary fetch behavior + a call counter to assert the
        /// vocabulary is fetched once per session, not per keystroke.
        var appsListResponse: AppsListResponse?
        var appsListError: Error?
        var appsListCallCount = 0

        func contentSearch(_ req: ContentSearchRequest) async throws -> ContentSearchResponse {
            if let contentError { throw contentError }
            return contentResponse ?? ContentSearchResponse(hits: [], indexState: .noMatch)
        }

        func transcriptSearch(_ req: TranscriptSearchRequest) async throws -> TranscriptSearchResponse {
            if let transcriptError { throw transcriptError }
            return transcriptResponse ?? TranscriptSearchResponse(hits: [], coverage: .bestEffort)
        }

        func timelineQuery(_ req: TimelineQueryRequest) async throws -> TimelineQueryResponse {
            onTimelineQuery?()
            if let rec = req.recording {
                perRecordingQueryCount += 1
                let rows = (perRecordingTimestamps[rec] ?? [])
                    .map { TimelineRow(recording: rec, timestampMs: $0, app: nil, title: nil) }
                return TimelineQueryResponse(rows: rows, coverage: .authoritative)
            }
            if let timelineError { throw timelineError }
            return timelineResponse ?? TimelineQueryResponse(rows: [], coverage: .authoritative)
        }

        func appsList() async throws -> AppsListResponse {
            appsListCallCount += 1
            if let appsListError { throw appsListError }
            return appsListResponse ?? AppsListResponse(appNames: [], hostnames: [])
        }

        func tasksList(_ req: TasksListRequest) async throws -> TasksListResponse {
            TasksListResponse(recording: req.recording, tasks: [])
        }

        // SCR-214 U11 — SearchViewModel never issues the write verbs; trivial
        // stubs keep the fake conforming to the extended `SearchService`.
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

    /// Holds the search `Task` so a fake-service hook can cancel it mid-flight.
    private final class TaskHolder: @unchecked Sendable {
        var task: Task<Void, Never>?
    }

    private var cal: Calendar = {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(secondsFromGMT: 0)!
        c.firstWeekday = 1
        return c
    }()
    // 2026-06-24 12:00 UTC.
    private lazy var fixedNow: Date = cal.date(
        from: DateComponents(year: 2026, month: 6, day: 24, hour: 12)
    )!

    private func makeVM(_ fake: FakeSearchService, chunkDurationSeconds: Double = 300) -> SearchViewModel {
        let nowValue = fixedNow
        return SearchViewModel(
            service: fake,
            parser: QueryParser(calendar: cal),
            chunkDurationSeconds: chunkDurationSeconds,
            now: { nowValue }
        )
    }

    private func loaded(_ vm: SearchViewModel) -> SearchResults? {
        if case .loaded(let r) = vm.phase { return r }
        return nil
    }

    // SCR-180 — blended relevance + recency. The content (bm25) and transcript
    // (text) hits now outrank the newer-but-unrelated activity row; recency
    // orders within the relevance tier. (Pre-SCR-180 this asserted the
    // recency-only order [.activity, .screen, .audio] — that flip is the point.)
    func testAllStreamsMergeAndRankByRelevanceThenRecency() async {
        let fake = FakeSearchService()
        fake.timelineResponse = TimelineQueryResponse(
            rows: [TimelineRow(recording: "rec", timestampMs: 3000, app: "Salesforce", title: "Cases")],
            coverage: .authoritative
        )
        fake.contentResponse = ContentSearchResponse(
            hits: [ContentHit(recording: "rec", timestampMs: 2000, snippet: "refund", score: -1.2, matchSource: nil)],
            indexState: .ok
        )
        fake.transcriptResponse = TranscriptSearchResponse(
            hits: [TranscriptHit(recording: "rec", chunkIndex: 0, snippet: "refund call")],
            coverage: .bestEffort
        )
        fake.perRecordingTimestamps = ["rec": [1000]]  // chunk 0 -> snaps to 1000

        let vm = makeVM(fake)
        // SCR-176 — the app token ("slack") gives the query an app filter so the
        // timeline stream is legitimately fetched and can join the merge; a pure
        // free-text query no longer fans out to timeline (see
        // testPureFreeTextExcludesTimelineFlood). No time window, so content/audio
        // are not client-side time-filtered.
        await vm.search("slack refund", contentIndexEnabled: true)

        let results = loaded(vm)
        XCTAssertEqual(results?.items.map(\.anchorMs), [2000, 1000, 3000])
        XCTAssertEqual(results?.items.map(\.stream), [.screen, .audio, .activity])
        XCTAssertFalse(results?.consentNeeded ?? true)
    }

    // SCR-180 R3 — end-to-end: a relevant content hit that is OLDER than an
    // unrelated activity row still leads the results.
    func testRelevantContentOutranksNewerActivityEndToEnd() async {
        let fake = FakeSearchService()
        fake.timelineResponse = TimelineQueryResponse(
            rows: [TimelineRow(recording: "rec", timestampMs: 9000, app: "Slack", title: "general")],
            coverage: .authoritative
        )
        fake.contentResponse = ContentSearchResponse(
            hits: [ContentHit(recording: "rec", timestampMs: 1000, snippet: "refund policy", score: -2.0, matchSource: nil)],
            indexState: .ok
        )
        let vm = makeVM(fake)
        // SCR-176 — an app token keeps the timeline stream (the unrelated newer
        // activity row) in the fan-out; pure free-text no longer merges timeline.
        await vm.search("slack refund", contentIndexEnabled: true)

        let items = loaded(vm)?.items ?? []
        XCTAssertEqual(items.first?.stream, .screen, "the relevant text hit must lead despite being older")
        XCTAssertEqual(items.map(\.anchorMs), [1000, 9000])
    }

    // SCR-180 R5 — a pure time/app query carries no relevance signal, so the
    // blend collapses to recency-only ordering (newest first).
    func testPureTimeQueryStaysRecencyOrdered() async {
        let fake = FakeSearchService()
        fake.timelineResponse = TimelineQueryResponse(
            rows: [
                TimelineRow(recording: "rec", timestampMs: 1000, app: "Slack", title: nil),
                TimelineRow(recording: "rec", timestampMs: 3000, app: "Jira", title: nil),
                TimelineRow(recording: "rec", timestampMs: 2000, app: "Mail", title: nil),
            ],
            coverage: .authoritative
        )
        let vm = makeVM(fake)
        await vm.search("today", contentIndexEnabled: false)

        XCTAssertEqual(loaded(vm)?.items.map(\.anchorMs), [3000, 2000, 1000])
    }

    func testConsentNeededWhenFlagOffAndFreeTextPresent() async {
        let fake = FakeSearchService()
        fake.contentResponse = ContentSearchResponse(hits: [], indexState: .notIndexed)
        let vm = makeVM(fake)
        await vm.search("refund macro", contentIndexEnabled: false)

        XCTAssertEqual(loaded(vm)?.consentNeeded, true)
    }

    func testNoConsentPromptForPureTimeOrAppQuery() async {
        // No free text -> content/transcript not run -> consent never relevant.
        let fake = FakeSearchService()
        fake.timelineResponse = TimelineQueryResponse(
            rows: [TimelineRow(recording: "rec", timestampMs: 5000, app: "Slack", title: nil)],
            coverage: .authoritative
        )
        let vm = makeVM(fake)
        await vm.search("today", contentIndexEnabled: false)

        let results = loaded(vm)
        XCTAssertEqual(results?.consentNeeded, false)
        XCTAssertEqual(results?.coverage.screen, .notRun)
        XCTAssertEqual(results?.coverage.audio, .notRun)
        if case .ok = results?.coverage.activity {} else { XCTFail("activity should be ok") }
    }

    func testTimeWindowFiltersContentClientSide() async {
        let startOfDay = cal.startOfDay(for: fixedNow)
        let insideMs = Int(cal.date(byAdding: .hour, value: 1, to: startOfDay)!.timeIntervalSince1970 * 1000)
        let outsideMs = Int(cal.date(byAdding: .hour, value: -1, to: startOfDay)!.timeIntervalSince1970 * 1000)

        let fake = FakeSearchService()
        fake.contentResponse = ContentSearchResponse(
            hits: [
                ContentHit(recording: "rec", timestampMs: insideMs, snippet: "in", score: -1, matchSource: nil),
                ContentHit(recording: "rec", timestampMs: outsideMs, snippet: "out", score: -1, matchSource: nil),
            ],
            indexState: .ok
        )
        let vm = makeVM(fake)
        await vm.search("today refund", contentIndexEnabled: true)

        let screenItems = loaded(vm)?.items.filter { $0.stream == .screen } ?? []
        XCTAssertEqual(screenItems.map(\.anchorMs), [insideMs])
    }

    func testTranscriptCorrelationSnapsToNearestTimelineEvent() async {
        let base = 1_000_000_000_000
        let fake = FakeSearchService()
        fake.transcriptResponse = TranscriptSearchResponse(
            hits: [TranscriptHit(recording: "rec-x", chunkIndex: 2, snippet: "meeting")],
            coverage: .bestEffort
        )
        // chunk 2 @ 300s -> estimate base+600_000; nearest event is exactly that.
        fake.perRecordingTimestamps = ["rec-x": [base, base + 250_000, base + 600_000, base + 1_000_000]]

        let vm = makeVM(fake, chunkDurationSeconds: 300)
        await vm.search("meeting", contentIndexEnabled: true)

        let audio = loaded(vm)?.items.filter { $0.stream == .audio } ?? []
        XCTAssertEqual(audio.count, 1)
        XCTAssertEqual(audio.first?.anchorMs, base + 600_000)
        XCTAssertEqual(audio.first?.approximate, true)
    }

    func testTranscriptUnanchoredWhenRecordingHasNoTimeline() async {
        let fake = FakeSearchService()
        fake.transcriptResponse = TranscriptSearchResponse(
            hits: [TranscriptHit(recording: "rec-y", chunkIndex: 1, snippet: "note")],
            coverage: .bestEffort
        )
        // No perRecordingTimestamps for rec-y -> unanchored.
        let vm = makeVM(fake)
        await vm.search("note", contentIndexEnabled: true)

        let audio = loaded(vm)?.items.filter { $0.stream == .audio } ?? []
        XCTAssertEqual(audio.first?.anchorMs, nil)
    }

    func testDaemonDownWhenTimelineSocketFails() async {
        let fake = FakeSearchService()
        fake.timelineError = DaemonClientError.socketUnavailable(path: "/tmp/x.sock")
        let vm = makeVM(fake)
        await vm.search("today refund", contentIndexEnabled: true)

        XCTAssertEqual(vm.phase, .daemonDown)
        XCTAssertFalse(vm.isSearching, "isSearching must clear on the daemon-down exit")
    }

    // U12 — the daemon's 402 `subscription_required` on the always-attempted
    // timeline verb resolves to the dedicated `.subscriptionRequired` phase,
    // DISTINCT from `.daemonDown` (helper unreachable) and per-stream
    // `.unavailable` (a store error), so lapsed search reads as "upgrade", not
    // "broken".
    func testSubscriptionRequiredWhenTimelineGated() async {
        let fake = FakeSearchService()
        fake.timelineError = DaemonClientError.envelopeError(
            code: "subscription_required", rawBody: Data()
        )
        let vm = makeVM(fake)
        await vm.search("today refund", contentIndexEnabled: true)

        XCTAssertEqual(vm.phase, .subscriptionRequired)
        XCTAssertNotEqual(vm.phase, .daemonDown, "gated search must not read as daemon-down")
        XCTAssertFalse(vm.isSearching, "isSearching must clear on the subscription-required exit")
    }

    // U12 — the gate returns 402 before any results assemble, so a lapsed search
    // publishes no `.loaded` results (no partial leak of a gated recall surface).
    // SCR-176 — a pure free-text query skips timeline, so the 402 arrives on the
    // content verb; the paywall gates all five recall verbs together. A sibling
    // stream returning a row must still not leak into a published result.
    func testSubscriptionRequiredDoesNotPublishResults() async {
        let fake = FakeSearchService()
        fake.contentError = DaemonClientError.envelopeError(
            code: "subscription_required", rawBody: Data()
        )
        fake.transcriptResponse = TranscriptSearchResponse(
            hits: [TranscriptHit(recording: "rec", chunkIndex: 0, snippet: "x")],
            coverage: .bestEffort
        )
        let vm = makeVM(fake)
        await vm.search("refund", contentIndexEnabled: true)

        XCTAssertNil(loaded(vm), "a gated search must not publish .loaded results")
        XCTAssertEqual(vm.phase, .subscriptionRequired)
    }

    // SCR-176 — a pure free-text query (no time window, no app filter) must NOT
    // fan out to timeline.query. That verb is time/app-filtered only (no text
    // term) and truncates earliest-first, so an unbounded fetch would flood the
    // results with up to `streamFetchLimit` of the OLDEST activity rows, unrelated
    // to the query. Only the relevant content hit should surface.
    func testPureFreeTextExcludesTimelineFlood() async {
        let fake = FakeSearchService()
        // If timeline were (wrongly) attempted, these oldest rows would flood in.
        fake.timelineResponse = TimelineQueryResponse(
            rows: (0..<SearchViewModel.streamFetchLimit).map {
                TimelineRow(recording: "rec", timestampMs: 1000 + $0, app: "Slack", title: nil)
            },
            coverage: .authoritative
        )
        fake.contentResponse = ContentSearchResponse(
            hits: [ContentHit(recording: "rec", timestampMs: 5000, snippet: "refund", score: -1, matchSource: nil)],
            indexState: .ok
        )
        let vm = makeVM(fake)
        await vm.search("refund", contentIndexEnabled: true)

        let results = loaded(vm)
        XCTAssertEqual(results?.items.filter { $0.stream == .activity }.count, 0,
                       "pure free-text must not merge timeline activity rows")
        XCTAssertEqual(results?.coverage.activity, .notRun,
                       "the timeline stream is skipped for pure free-text")
        XCTAssertEqual(results?.items.map(\.stream), [.screen],
                       "only the relevant content hit should surface")
        XCTAssertEqual(results?.truncated, false,
                       "a skipped timeline fetch must not flag truncation")
    }

    // SCR-176 — daemon-down is still detected for a pure free-text query even
    // though timeline is skipped: it is derived from the content/transcript
    // streams, which share the same socket.
    func testDaemonDownOnPureFreeTextWhenSocketFails() async {
        let fake = FakeSearchService()
        fake.contentError = DaemonClientError.socketUnavailable(path: "/tmp/x.sock")
        let vm = makeVM(fake)
        await vm.search("refund", contentIndexEnabled: true)

        XCTAssertEqual(vm.phase, .daemonDown)
        XCTAssertFalse(vm.isSearching, "isSearching must clear on the daemon-down exit")
    }

    func testPartialContentErrorStillReturnsTimeline() async {
        let fake = FakeSearchService()
        fake.contentError = DaemonClientError.envelopeError(code: "store_unavailable", rawBody: Data())
        fake.timelineResponse = TimelineQueryResponse(
            rows: [TimelineRow(recording: "rec", timestampMs: 7000, app: "Jira", title: nil)],
            coverage: .authoritative
        )
        let vm = makeVM(fake)
        await vm.search("today refund", contentIndexEnabled: true)

        let results = loaded(vm)
        XCTAssertEqual(results?.coverage.screen, .unavailable)
        XCTAssertEqual(results?.items.filter { $0.stream == .activity }.count, 1)
    }

    func testEmptyQueryStaysIdle() async {
        let vm = makeVM(FakeSearchService())
        await vm.search("   ", contentIndexEnabled: true)
        XCTAssertEqual(vm.phase, .idle)
        XCTAssertFalse(vm.isSearching, "empty-query early return must leave isSearching false")
    }

    // SCR-182 U2 — the first search from idle shows the full-screen spinner
    // (transitions through `.searching`); isSearching clears on completion.
    func testFirstSearchFromIdleTransitionsThroughSearching() async {
        let fake = FakeSearchService()
        fake.timelineResponse = TimelineQueryResponse(
            rows: [TimelineRow(recording: "rec", timestampMs: 3000, app: "A", title: "T")],
            coverage: .authoritative
        )
        let vm = makeVM(fake)
        var phases: [SearchViewModel.Phase] = []
        let c = vm.$phase.dropFirst().sink { phases.append($0) }
        defer { c.cancel() }

        await vm.search("first", contentIndexEnabled: true)

        XCTAssertTrue(phases.contains(.searching), "first search should flash the spinner state")
        XCTAssertNotNil(loaded(vm))
        XCTAssertFalse(vm.isSearching)
    }

    // SCR-182 U2 — a refresh over already-loaded results keeps the prior results
    // visible (never transitions to `.searching`) and clears isSearching after.
    func testRefreshOverLoadedKeepsPriorResultsVisible() async {
        let fake = FakeSearchService()
        fake.timelineResponse = TimelineQueryResponse(
            rows: [TimelineRow(recording: "rec", timestampMs: 3000, app: "A", title: "T")],
            coverage: .authoritative
        )
        let vm = makeVM(fake)
        await vm.search("first", contentIndexEnabled: true)
        XCTAssertNotNil(loaded(vm))

        var phases: [SearchViewModel.Phase] = []
        let c = vm.$phase.dropFirst().sink { phases.append($0) }
        defer { c.cancel() }

        await vm.search("second", contentIndexEnabled: true)

        XCTAssertFalse(phases.contains(.searching),
                       "an in-place refresh must not swap to the full-screen spinner")
        XCTAssertNotNil(loaded(vm))
        XCTAssertFalse(vm.isSearching)
    }

    // SCR-182 U3 — a main stream returning exactly the fetch cap flags truncation.
    func testTruncatedWhenMainStreamHitsCap() async {
        let fake = FakeSearchService()
        let rows = (0..<SearchViewModel.streamFetchLimit).map {
            TimelineRow(recording: "rec", timestampMs: 1000 + $0, app: nil, title: nil)
        }
        fake.timelineResponse = TimelineQueryResponse(rows: rows, coverage: .authoritative)
        let vm = makeVM(fake)
        await vm.search("today", contentIndexEnabled: true)
        XCTAssertEqual(loaded(vm)?.truncated, true)
    }

    func testNotTruncatedBelowCap() async {
        let fake = FakeSearchService()
        fake.timelineResponse = TimelineQueryResponse(
            rows: [TimelineRow(recording: "rec", timestampMs: 1000, app: nil, title: nil)],
            coverage: .authoritative
        )
        let vm = makeVM(fake)
        await vm.search("today", contentIndexEnabled: true)
        XCTAssertEqual(loaded(vm)?.truncated, false)
    }

    // SCR-182 U3 — the per-recording correlation fetch always requests the cap;
    // it must NOT feed the truncation heuristic (only the main fetches do).
    func testPerRecordingCorrelationCapDoesNotMarkTruncated() async {
        let fake = FakeSearchService()
        fake.timelineResponse = TimelineQueryResponse(
            rows: [TimelineRow(recording: "rec", timestampMs: 5000, app: nil, title: nil)],
            coverage: .authoritative
        )
        fake.transcriptResponse = TranscriptSearchResponse(
            hits: [TranscriptHit(recording: "recX", chunkIndex: 0, snippet: "x")],
            coverage: .bestEffort
        )
        fake.perRecordingTimestamps = ["recX": (0..<SearchViewModel.streamFetchLimit).map { 1000 + $0 }]
        let vm = makeVM(fake)
        await vm.search("refund", contentIndexEnabled: true)
        XCTAssertEqual(loaded(vm)?.truncated, false,
                       "a full per-recording correlation fetch must not flag truncation")
    }

    // SCR-182 U1 — a search cancelled mid-flight (superseded by a newer one)
    // must not publish its result over the newer search. The fake cancels the
    // surrounding task on the main timeline fetch; the model's pre-publish
    // `Task.isCancelled` guard should then bail, leaving phase at `.searching`.
    // SCR-176 — the query carries a time term so the main timeline stream is
    // fetched (a pure free-text query skips it, and with it the cancel hook).
    func testCancelledSearchDoesNotPublishResults() async {
        let fake = FakeSearchService()
        fake.timelineResponse = TimelineQueryResponse(
            rows: [TimelineRow(recording: "rec", timestampMs: 3000, app: "Salesforce", title: "Cases")],
            coverage: .authoritative
        )
        let vm = makeVM(fake)
        let holder = TaskHolder()
        fake.onTimelineQuery = { holder.task?.cancel() }

        holder.task = Task { @MainActor in
            await vm.search("today refund", contentIndexEnabled: true)
        }
        await holder.task?.value

        XCTAssertNil(loaded(vm), "a cancelled search must not overwrite with .loaded")
        XCTAssertEqual(vm.phase, .searching, "phase should remain .searching, not publish stale results")
    }

    // SCR-182 U1 — a superseded search must stop issuing per-recording
    // timeline.query correlation calls, not run the whole fan-out to completion.
    func testCancelledSearchStopsTranscriptCorrelationFanOut() async {
        let fake = FakeSearchService()
        fake.timelineResponse = TimelineQueryResponse(
            rows: [TimelineRow(recording: "rec", timestampMs: 3000, app: nil, title: nil)],
            coverage: .authoritative
        )
        fake.transcriptResponse = TranscriptSearchResponse(
            hits: [
                TranscriptHit(recording: "recA", chunkIndex: 0, snippet: "x"),
                TranscriptHit(recording: "recB", chunkIndex: 0, snippet: "y"),
            ],
            coverage: .bestEffort
        )
        let vm = makeVM(fake)
        let holder = TaskHolder()
        // Cancel on the main timeline fetch — correlation runs strictly after,
        // so a cancellation-aware fan-out should issue zero per-recording calls.
        // SCR-176 — the query carries a time term so the main timeline stream is
        // fetched (pure free-text skips it, moving the cancel point into
        // correlation and defeating this assertion).
        fake.onTimelineQuery = { holder.task?.cancel() }

        holder.task = Task { @MainActor in
            await vm.search("today refund", contentIndexEnabled: true)
        }
        await holder.task?.value

        XCTAssertEqual(fake.perRecordingQueryCount, 0,
                       "superseded search must not fan out per-recording correlation calls")
    }

    // MARK: - SCR-179 U5: index-sourced parser vocabulary

    func testIndexVocabularyIsInjectedIntoParser() async {
        // "acmecorp" is not in the static seed; the injected vocabulary makes it
        // recognized as an app filter. Proves the live path actually injects the
        // index vocabulary — not merely that the static fallback works.
        let fake = FakeSearchService()
        fake.appsListResponse = AppsListResponse(appNames: ["AcmeCorp"], hostnames: [])
        let vm = makeVM(fake)

        await vm.search("acmecorp dashboard", contentIndexEnabled: false)

        XCTAssertEqual(loaded(vm)?.appFilter, "acmecorp")
        XCTAssertEqual(fake.appsListCallCount, 1)
    }

    func testVocabularyFetchFailureFallsBackToStaticParser() async {
        // apps.list throws (older daemon 404 / daemon down) → static vocabulary;
        // "acmecorp" is unrecognized, search still completes normally.
        struct Boom: Error {}
        let fake = FakeSearchService()
        fake.appsListError = Boom()
        let vm = makeVM(fake)

        await vm.search("acmecorp dashboard", contentIndexEnabled: false)

        let results = loaded(vm)
        XCTAssertNotNil(results, "search must still complete when vocabulary fetch fails")
        XCTAssertNil(results?.appFilter, "unknown term stays free text under static fallback")
    }

    func testVocabularyFetchedOncePerSession() async {
        let fake = FakeSearchService()
        fake.appsListResponse = AppsListResponse(appNames: ["acmecorp"], hostnames: [])
        let vm = makeVM(fake)

        await vm.search("acmecorp today", contentIndexEnabled: false)
        await vm.search("slack yesterday", contentIndexEnabled: false)

        XCTAssertEqual(fake.appsListCallCount, 1, "vocabulary must be fetched once per session")
    }

    func testHostnameVocabularyNormalizesToBrandFilter() async {
        // A visited hostname becomes a recognized brand token the user can type.
        let fake = FakeSearchService()
        fake.appsListResponse = AppsListResponse(appNames: [], hostnames: ["acme.io"])
        let vm = makeVM(fake)

        await vm.search("acme notes", contentIndexEnabled: false)

        XCTAssertEqual(loaded(vm)?.appFilter, "acme")
    }
}
