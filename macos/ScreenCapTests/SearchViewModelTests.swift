import Foundation
import XCTest
@testable import ScreenCap

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

        func contentSearch(_ req: ContentSearchRequest) async throws -> ContentSearchResponse {
            if let contentError { throw contentError }
            return contentResponse ?? ContentSearchResponse(hits: [], indexState: .noMatch)
        }

        func transcriptSearch(_ req: TranscriptSearchRequest) async throws -> TranscriptSearchResponse {
            if let transcriptError { throw transcriptError }
            return transcriptResponse ?? TranscriptSearchResponse(hits: [], coverage: .bestEffort)
        }

        func timelineQuery(_ req: TimelineQueryRequest) async throws -> TimelineQueryResponse {
            if let rec = req.recording {
                let rows = (perRecordingTimestamps[rec] ?? [])
                    .map { TimelineRow(recording: rec, timestampMs: $0, app: nil, title: nil) }
                return TimelineQueryResponse(rows: rows, coverage: .authoritative)
            }
            if let timelineError { throw timelineError }
            return timelineResponse ?? TimelineQueryResponse(rows: [], coverage: .authoritative)
        }
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

    func testAllStreamsMergeAndRankByRecency() async {
        let fake = FakeSearchService()
        fake.timelineResponse = TimelineQueryResponse(
            rows: [TimelineRow(recording: "rec", timestampMs: 3000, app: "Salesforce", title: "Cases")],
            coverage: .authoritative
        )
        fake.contentResponse = ContentSearchResponse(
            hits: [ContentHit(recording: "rec", timestampMs: 2000, snippet: "refund", score: -1.2)],
            indexState: .ok
        )
        fake.transcriptResponse = TranscriptSearchResponse(
            hits: [TranscriptHit(recording: "rec", chunkIndex: 0, snippet: "refund call")],
            coverage: .bestEffort
        )
        fake.perRecordingTimestamps = ["rec": [1000]]  // chunk 0 -> snaps to 1000

        let vm = makeVM(fake)
        await vm.search("refund", contentIndexEnabled: true)

        let results = loaded(vm)
        XCTAssertEqual(results?.items.map(\.anchorMs), [3000, 2000, 1000])
        XCTAssertEqual(results?.items.map(\.stream), [.activity, .screen, .audio])
        XCTAssertFalse(results?.consentNeeded ?? true)
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
                ContentHit(recording: "rec", timestampMs: insideMs, snippet: "in", score: -1),
                ContentHit(recording: "rec", timestampMs: outsideMs, snippet: "out", score: -1),
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
    }

    // MARK: - Coverage matrix (#15)

    func testIndexDegradedWithMatchReportsOk() async {
        let fake = FakeSearchService()
        fake.contentResponse = ContentSearchResponse(
            hits: [ContentHit(recording: "rec", timestampMs: 9000, snippet: "hit", score: -1)],
            indexState: .indexDegraded
        )
        let vm = makeVM(fake)
        await vm.search("refund", contentIndexEnabled: true)

        if case .ok(let count) = loaded(vm)?.coverage.screen {
            XCTAssertEqual(count, 1)
        } else {
            XCTFail("degraded index with a match should report .ok")
        }
    }

    func testIndexDegradedWithNoMatchReportsDegraded() async {
        let fake = FakeSearchService()
        fake.contentResponse = ContentSearchResponse(hits: [], indexState: .indexDegraded)
        let vm = makeVM(fake)
        await vm.search("refund", contentIndexEnabled: true)

        XCTAssertEqual(loaded(vm)?.coverage.screen, .degraded)
    }

    func testNoMatchWithFlagOffStillSurfacesConsent() async {
        // notIndexed + free-text + flag off → the consent CTA must fire even
        // though the content stream returned nothing.
        let fake = FakeSearchService()
        fake.contentResponse = ContentSearchResponse(hits: [], indexState: .notIndexed)
        let vm = makeVM(fake)
        await vm.search("refund macro", contentIndexEnabled: false)

        let results = loaded(vm)
        XCTAssertEqual(results?.consentNeeded, true)
        XCTAssertEqual(results?.coverage.screen, .notIndexed)
    }

    func testAuthoritativeEmptyWithTimeWindowIsNothingRecordedThen() async {
        // R9: a time-scoped query whose authoritative timeline is empty is
        // "nothing recorded then" — encoded as activity == .empty with a window.
        let fake = FakeSearchService()
        fake.timelineResponse = TimelineQueryResponse(rows: [], coverage: .authoritative)
        let vm = makeVM(fake)
        await vm.search("yesterday", contentIndexEnabled: true)

        let results = loaded(vm)
        XCTAssertNotNil(results?.timeWindow)
        XCTAssertEqual(results?.coverage.activity, .empty)
        XCTAssertTrue(results?.items.isEmpty ?? false)
    }

    func testPartialStreamFailureOneSocketUnavailableOthersSucceed() async {
        // Transcript fails at the socket level while content + timeline succeed.
        // A single non-timeline stream being down must NOT flip the whole search
        // to daemonDown (only the always-attempted timeline call does that) —
        // the others still publish, and the failed stream reports unavailable.
        let startOfDay = cal.startOfDay(for: fixedNow)
        let inWindowMs = Int(cal.date(byAdding: .hour, value: 2, to: startOfDay)!.timeIntervalSince1970 * 1000)

        let fake = FakeSearchService()
        fake.transcriptError = DaemonClientError.socketUnavailable(path: "/tmp/x.sock")
        fake.contentResponse = ContentSearchResponse(
            hits: [ContentHit(recording: "rec", timestampMs: inWindowMs, snippet: "ok", score: -1)],
            indexState: .ok
        )
        fake.timelineResponse = TimelineQueryResponse(
            rows: [TimelineRow(recording: "rec", timestampMs: inWindowMs, app: "Slack", title: nil)],
            coverage: .authoritative
        )
        let vm = makeVM(fake)
        await vm.search("today refund", contentIndexEnabled: true)

        let results = loaded(vm)
        XCTAssertNotNil(results, "one non-timeline stream down must not collapse to daemonDown")
        XCTAssertEqual(results?.coverage.audio, .unavailable)
        if case .ok = results?.coverage.screen {} else { XCTFail("content should be ok") }
        if case .ok = results?.coverage.activity {} else { XCTFail("activity should be ok") }
    }
}
