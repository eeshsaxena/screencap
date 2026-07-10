import Combine
import Foundation
import XCTest
@testable import ScreenCap

/// Conversational-recall U8 — drives `ChatViewModel` against a fake `ChatService`
/// (no live socket), the same injection seam `SearchViewModel` uses via
/// `SearchService`. Covers: a send appends an answered turn with sources; a
/// follow-up forwards prior-turn POINTERS (KTD6, not prose); a refusal / coverage
/// state renders; daemon-down surfaces the error state (not a crash); and the
/// deep-link opener fires when a source is selected.
@MainActor
final class ChatViewModelTests: XCTestCase {

    /// Records the last request and returns a scripted response (or throws).
    private final class FakeChatService: ChatService, @unchecked Sendable {
        var response: ChatAnswerResponse?
        var error: Error?
        /// A stack of scripted responses, popped per call (for the multi-turn test).
        var responseQueue: [ChatAnswerResponse] = []
        /// Every request the model issued, in order — asserted for prior-turn
        /// pointer forwarding.
        private(set) var requests: [ChatAnswerRequest] = []

        func chatAnswer(_ req: ChatAnswerRequest) async throws -> ChatAnswerResponse {
            requests.append(req)
            if let error { throw error }
            if !responseQueue.isEmpty { return responseQueue.removeFirst() }
            return response ?? ChatAnswerResponse(
                answer: "", sources: [],
                coverage: ChatCoverage(state: .ok, note: "")
            )
        }

        var lastRequest: ChatAnswerRequest? { requests.last }
    }

    // MARK: - Fixtures

    private func source(_ recording: String, _ ms: Int, _ stream: ChatSourceStream = .content) -> ChatSource {
        ChatSource(recording: recording, timestampMs: ms, stream: stream)
    }

    private func answer(
        _ text: String, sources: [ChatSource] = [], refusal: Bool = false,
        coverage: ChatCoverage = ChatCoverage(state: .ok, note: "")
    ) -> ChatAnswerResponse {
        ChatAnswerResponse(
            answer: text, sources: sources, coverage: coverage,
            refusal: refusal, questionKind: .point, target: "on_device"
        )
    }

    // MARK: - Send appends an answered turn with sources

    func testSendAppendsAnsweredTurnWithSources() async {
        let fake = FakeChatService()
        fake.response = answer(
            "You hit a 500 on the vendor portal at 2:03pm.",
            sources: [source("rec-1", 1000), source("rec-1", 2000, .timeline)]
        )
        let vm = ChatViewModel(service: fake)

        await vm.send("what was that vendor-portal error?")

        XCTAssertEqual(vm.turns.count, 1)
        XCTAssertEqual(vm.turns.first?.question, "what was that vendor-portal error?")
        let payload = vm.turns.first?.answer
        XCTAssertEqual(payload?.text, "You hit a 500 on the vendor portal at 2:03pm.")
        XCTAssertEqual(payload?.sources.count, 2)
        XCTAssertFalse(vm.isLoading, "isLoading must clear after the turn resolves")
    }

    func testEmptyQuestionIsIgnored() async {
        let fake = FakeChatService()
        let vm = ChatViewModel(service: fake)
        await vm.send("   ")
        XCTAssertTrue(vm.turns.isEmpty)
        XCTAssertTrue(fake.requests.isEmpty, "an empty question must not hit the daemon")
    }

    func testCanSendGating() {
        let vm = ChatViewModel(service: FakeChatService())
        XCTAssertFalse(vm.canSend(""), "empty input disables send")
        XCTAssertFalse(vm.canSend("   \n"), "whitespace-only input disables send")
        XCTAssertTrue(vm.canSend("recap my morning"))
    }

    // MARK: - Follow-up forwards prior-turn POINTERS (KTD6)

    func testFollowUpSendsPriorTurnPointers() async {
        let fake = FakeChatService()
        fake.responseQueue = [
            answer("Yesterday afternoon you were in Salesforce.",
                   sources: [source("rec-a", 5000, .timeline), source("rec-a", 5500, .content)]),
            answer("In the morning you were in Jira.", sources: [source("rec-b", 100)]),
        ]
        let vm = ChatViewModel(service: fake)

        await vm.send("what did I do yesterday afternoon?")
        await vm.send("what about the morning?")

        XCTAssertEqual(vm.turns.count, 2)
        // The FIRST request carries no prior context.
        XCTAssertEqual(fake.requests.first?.priorTurns.count, 0)
        // The SECOND request carries the first turn's TWO source pointers — as
        // pointers, never prose (the request type has no prose field).
        let follow = fake.requests.last
        XCTAssertEqual(follow?.question, "what about the morning?")
        XCTAssertEqual(follow?.priorTurns.count, 2)
        XCTAssertEqual(follow?.priorTurns.first?.recording, "rec-a")
        XCTAssertEqual(follow?.priorTurns.first?.timestampMs, 5000)
        XCTAssertEqual(follow?.priorTurns.first?.stream, ChatSourceStream.timeline.rawValue)
        XCTAssertEqual(follow?.priorTurns.last?.stream, ChatSourceStream.content.rawValue)
    }

    // MARK: - Refusal / coverage state renders

    func testRefusalTurnRenders() async {
        let fake = FakeChatService()
        fake.response = answer(
            "I don't have anything recorded that answers that.",
            sources: [], refusal: true,
            coverage: ChatCoverage(state: .noMatchingMoments, note: "No matching moments.")
        )
        let vm = ChatViewModel(service: fake)

        await vm.send("what did I say in the meeting on Mars?")

        let payload = vm.turns.first?.answer
        XCTAssertEqual(payload?.refusal, true)
        XCTAssertTrue(payload?.sources.isEmpty ?? false, "a refusal shows no unsourced claims")
        XCTAssertEqual(payload?.coverage.state, .noMatchingMoments)
    }

    func testOcrOffCoverageSuggestsConsent() async {
        let fake = FakeChatService()
        fake.response = answer(
            "On-screen text isn't indexed yet, so I can't answer content questions.",
            sources: [],
            coverage: ChatCoverage(state: .notIndexed, note: "On-screen text indexing is off.")
        )
        let vm = ChatViewModel(service: fake)

        await vm.send("what error was on my screen?")

        let payload = vm.turns.first?.answer
        XCTAssertEqual(payload?.coverage.state, .notIndexed)
        XCTAssertTrue(payload?.suggestsOcrConsent ?? false,
                      "a not-indexed coverage state should surface the OCR-consent affordance (R13)")
    }

    // MARK: - Daemon-down surfaces an error state, not a crash

    func testDaemonDownSurfacesErrorState() async {
        let fake = FakeChatService()
        fake.error = DaemonClientError.socketUnavailable(path: "/tmp/x.sock")
        let vm = ChatViewModel(service: fake)

        await vm.send("recap my morning")

        XCTAssertEqual(vm.turns.count, 1, "the failed turn is preserved, not dropped")
        XCTAssertNotNil(vm.turns.first?.errorMessage)
        XCTAssertTrue(vm.lastTurnDaemonDown, "daemon-unreachable is called out distinctly")
        XCTAssertFalse(vm.isLoading)
    }

    func testGenericErrorSurfacesRetryableFailure() async {
        struct Boom: Error {}
        let fake = FakeChatService()
        fake.error = Boom()
        let vm = ChatViewModel(service: fake)

        await vm.send("recap my morning")

        XCTAssertNotNil(vm.turns.first?.errorMessage)
        XCTAssertFalse(vm.lastTurnDaemonDown, "a generic failure is not the daemon-down state")
    }

    // MARK: - Retry preserves prior turns

    func testRetryPreservesPriorTurnsAndResolves() async {
        let fake = FakeChatService()
        // First turn succeeds, second turn fails, then the retry succeeds.
        fake.responseQueue = [answer("First answer.", sources: [source("rec-a", 1)])]
        let vm = ChatViewModel(service: fake)
        await vm.send("first question")

        fake.responseQueue = []
        fake.error = DaemonClientError.timedOut(seconds: 10)
        await vm.send("second question")
        XCTAssertNotNil(vm.turns.last?.errorMessage)
        XCTAssertEqual(vm.turns.count, 2, "the failed turn stays in the transcript")

        // Now the daemon is back; retry the failed turn in place.
        fake.error = nil
        fake.response = answer("Second answer, at last.", sources: [source("rec-b", 2)])
        await vm.retry(turnID: vm.turns.last!.id)

        XCTAssertEqual(vm.turns.count, 2, "retry must not append a new turn")
        XCTAssertEqual(vm.turns.first?.answer?.text, "First answer.", "prior turns are preserved")
        XCTAssertEqual(vm.turns.last?.answer?.text, "Second answer, at last.")
    }

    // MARK: - Source deep-link opener

    func testSelectingSourceTriggersDeepLinkOpener() async {
        // The deep-link seam is `InspectWindowOpener.shared` — assert that setting
        // pendingSeekMs + open() fires, mirroring the Library/palette call site.
        // This mirrors what ChatView.openSource does.
        let openerFired = expectation(description: "openInspect called")
        var openedName: String?
        InspectWindowOpener.shared.openInspect = { name in
            openedName = name
            openerFired.fulfill()
        }
        defer { InspectWindowOpener.shared.openInspect = nil }

        let src = source("rec-deep", 4242, .timeline)
        InspectWindowOpener.shared.pendingSeekMs[src.recording] = src.timestampMs
        let didOpen = InspectWindowOpener.shared.open(recordingName: src.recording)

        await fulfillment(of: [openerFired], timeout: 1)
        XCTAssertTrue(didOpen)
        XCTAssertEqual(openedName, "rec-deep")
        XCTAssertEqual(InspectWindowOpener.shared.pendingSeekMs["rec-deep"], 4242,
                       "the seek target is staged for the Inspect window to consume")
    }

    // MARK: - Wire decoding (optional-tolerant)

    func testResponseDecodesWithOptionalFields() throws {
        // A minimal envelope missing target / question_kind / per_stream must
        // still decode (the nullable-timing lesson), never crash.
        let json = """
        {"ok":true,"schema_version":1,"daemon_version":"x","api_schema_version":1,
         "answer":"ok","sources":[{"recording":"r","timestamp_ms":9,"stream":"transcript"}],
         "coverage":{"state":"ok","note":""},"refusal":false,"question_kind":"aggregate"}
        """
        let decoded = try JSONDecoder().decode(ChatAnswerResponse.self, from: Data(json.utf8))
        XCTAssertEqual(decoded.answer, "ok")
        XCTAssertEqual(decoded.questionKind, .aggregate)
        XCTAssertNil(decoded.target, "a missing target decodes as nil, not a crash")
        // A transcript-stream source still carries a resolvable timestamp.
        XCTAssertEqual(decoded.sources.first?.timestampMs, 9)
        XCTAssertEqual(decoded.sources.first?.stream, .transcript)
        XCTAssertTrue(decoded.coverage.perStream.isEmpty)
    }

    func testRequestEncodesPointerOnlyPriorTurns() throws {
        let req = ChatAnswerRequest(
            question: "follow up",
            priorTurns: [ChatPriorTurnPointer(recording: "r", timestampMs: 7, stream: .content)]
        )
        let data = try JSONEncoder().encode(req)
        let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        XCTAssertEqual(obj?["question"] as? String, "follow up")
        let priors = obj?["prior_turns"] as? [[String: Any]]
        XCTAssertEqual(priors?.count, 1)
        XCTAssertEqual(priors?.first?["recording"] as? String, "r")
        XCTAssertEqual(priors?.first?["timestamp_ms"] as? Int, 7)
        // The pointer shape has NO text field — client prose can't round-trip.
        XCTAssertNil(priors?.first?["text"], "prior-turn pointers carry no prose (KTD6)")
    }

    // MARK: - Honest-state mapping (U4, R5/R6/R7)

    private func chatAnswer(
        refusal: Bool, reason: ChatRefusalReason?,
        coverage: ChatCoverageState = .ok
    ) -> ChatAnswer {
        ChatAnswer(
            text: "…", sources: [],
            coverage: ChatCoverage(state: coverage, note: "n"),
            refusal: refusal, reason: reason
        )
    }

    func testAnsweredMapsToAnswered() {
        XCTAssertEqual(chatAnswer(refusal: false, reason: nil).honestState, .answered)
    }

    func testNoBackendReasonMapsToNoBackend() {
        XCTAssertEqual(chatAnswer(refusal: true, reason: .noBackend).honestState, .noBackend)
    }

    func testNoEvidenceReasonMapsToNoMatchingMoments() {
        XCTAssertEqual(
            chatAnswer(refusal: true, reason: .noEvidence).honestState, .noMatchingMoments
        )
    }

    func testUnsupportedAndBlockedMapToSafeRefusal() {
        XCTAssertEqual(chatAnswer(refusal: true, reason: .unsupported).honestState, .safeRefusal)
        XCTAssertEqual(chatAnswer(refusal: true, reason: .blocked).honestState, .safeRefusal)
    }

    func testUnknownReasonPrefersCoverageThenSafeRefusal() {
        // reason nil (e.g. a graceful-refusal or unknown wire value): fall back to
        // coverage, else a generic safe refusal — never a no-backend affordance.
        XCTAssertEqual(chatAnswer(refusal: true, reason: nil).honestState, .safeRefusal)
        XCTAssertEqual(
            chatAnswer(refusal: true, reason: nil, coverage: .noMatchingMoments).honestState,
            .noMatchingMoments
        )
    }

    func testNoBackendSuppressesOcrConsentAffordance() {
        // R7: even with a thin-coverage state that would normally suggest OCR
        // consent, a no-backend refusal must NOT tell the user to turn on indexing.
        let a = chatAnswer(refusal: true, reason: .noBackend, coverage: .notIndexed)
        XCTAssertEqual(a.honestState, .noBackend)
        XCTAssertFalse(a.suggestsOcrConsent, "no-backend must suppress the OCR-consent affordance")
    }

    func testReasonDecodesFromWire() throws {
        let json = """
        {"answer":"x","sources":[],"coverage":{"state":"ok","note":"","per_stream":{}},
         "refusal":true,"question_kind":"point","target":"none","reason":"no_backend"}
        """
        let decoded = try JSONDecoder().decode(ChatAnswerResponse.self, from: Data(json.utf8))
        XCTAssertEqual(ChatAnswer(response: decoded).reason, .noBackend)
        XCTAssertEqual(ChatAnswer(response: decoded).honestState, .noBackend)
    }
}
