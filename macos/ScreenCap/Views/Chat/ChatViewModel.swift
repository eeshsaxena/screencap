import Combine
import Foundation

// Conversational-recall U8 — the multi-turn Chat surface's view model. Holds
// session-scoped turn history (`[ChatTurn]`), an in-flight flag, and an error
// state. Each send forwards PRIOR TURNS AS POINTERS (never prose — KTD6/R14) to
// `chat.answer`, matching the daemon contract; the daemon re-derives prior-turn
// evidence server-side, so raw captured text never round-trips through the
// client. The retrieval/generation itself lives entirely daemon-side.
//
// Pure logic behind a `ChatService` seam so it is unit-testable without a live
// socket (the same seam `SearchViewModel` uses via `SearchService`).

// MARK: - Service seam

/// Test seam for the `chat.answer` verb, mirroring `SearchService`. `ChatView`
/// wires the live implementation; `ChatViewModelTests` substitutes a fake.
protocol ChatService: Sendable {
    func chatAnswer(_ req: ChatAnswerRequest) async throws -> ChatAnswerResponse
}

/// Live implementation: forwards to the daemon over the UNIX socket.
struct LiveChatService: ChatService {
    func chatAnswer(_ req: ChatAnswerRequest) async throws -> ChatAnswerResponse {
        try await DaemonClient.chatAnswer(req)
    }
}

// MARK: - Turn model

/// One conversation turn: the operator's question plus its outcome. Session-scoped
/// only — v1 memory is per Chat window, not persisted across app restarts (a
/// deferred follow-up per the plan's Scope Boundaries).
struct ChatTurn: Identifiable, Equatable, Sendable {
    /// The lifecycle of a single turn's answer.
    enum State: Equatable, Sendable {
        /// Generation is in flight — the composer is locked and the turn shows a
        /// thinking indicator. (Whole-render after completion is fine for v1; the
        /// daemon returns the whole answer. Streaming would hook in here by
        /// appending partial text to a new `.streaming(String)` case.)
        case loading
        /// The daemon returned an answer (possibly a grounded refusal — see
        /// `ChatAnswer.refusal`).
        case answered(ChatAnswer)
        /// The turn failed (daemon down / decode / transport) — recoverable via
        /// retry, which preserves prior turns. Carries the human-readable reason.
        case failed(String)
    }

    let id: UUID
    let question: String
    var state: State

    init(id: UUID = UUID(), question: String, state: State = .loading) {
        self.id = id
        self.question = question
        self.state = state
    }

    /// The answer payload if this turn resolved successfully (grounded answer OR a
    /// grounded refusal — both are `.answered`).
    var answer: ChatAnswer? {
        if case .answered(let a) = state { return a }
        return nil
    }

    var isLoading: Bool {
        if case .loading = state { return true }
        return false
    }

    var errorMessage: String? {
        if case .failed(let msg) = state { return msg }
        return nil
    }
}

/// The view-facing answer payload — a decoded `ChatAnswerResponse` narrowed to
/// what the UI renders. Kept separate from the wire type so the view never
/// touches envelope plumbing.
struct ChatAnswer: Equatable, Sendable {
    let text: String
    let sources: [ChatSource]
    let coverage: ChatCoverage
    let refusal: Bool
    let questionKind: ChatQuestionKind
    let target: String?

    init(response: ChatAnswerResponse) {
        self.text = response.answer
        self.sources = response.sources
        self.coverage = response.coverage
        self.refusal = response.refusal
        self.questionKind = response.questionKind
        self.target = response.target
    }

    init(
        text: String, sources: [ChatSource], coverage: ChatCoverage,
        refusal: Bool = false, questionKind: ChatQuestionKind = .point,
        target: String? = "on_device"
    ) {
        self.text = text
        self.sources = sources
        self.coverage = coverage
        self.refusal = refusal
        self.questionKind = questionKind
        self.target = target
    }

    /// True when this turn's coverage indicates OCR indexing would help and the
    /// UI should surface the one-time consent affordance inline on THIS turn (R13).
    var suggestsOcrConsent: Bool { coverage.state.suggestsOcrConsent }
}

// MARK: - View model

@MainActor
final class ChatViewModel: ObservableObject {
    /// Session-scoped turn history, oldest first. The transcript renders this.
    @Published private(set) var turns: [ChatTurn] = []
    /// True while any turn is in flight — locks the composer + shows progress.
    @Published private(set) var isLoading = false

    private let service: ChatService
    /// Per-retrieval-turn cap forwarded to the verb (nil → daemon default).
    private let limit: Int?

    init(service: ChatService = LiveChatService(), limit: Int? = nil) {
        self.service = service
        self.limit = limit
    }

    /// Whether the composer's Send should be enabled for `text` — non-empty after
    /// trimming AND no turn currently in flight (in-flight locks input).
    func canSend(_ text: String) -> Bool {
        !isLoading && !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    /// Send a new question. Appends a `.loading` turn immediately (so the
    /// transcript threads it), forwards ALL prior turns' source pointers as
    /// context (KTD6 — pointers, not prose), and resolves the turn to
    /// `.answered` or `.failed`. A trimmed-empty question is ignored.
    func send(_ rawQuestion: String) async {
        let question = rawQuestion.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !question.isEmpty, !isLoading else { return }

        let turn = ChatTurn(question: question, state: .loading)
        turns.append(turn)
        await run(turnID: turn.id, question: question)
    }

    /// Retry a previously-failed turn WITHOUT re-appending it — prior turns stay
    /// visible and threaded (a failed turn is visibly recoverable, not dropped).
    /// A no-op if the turn isn't in a failed state or a send is in flight.
    func retry(turnID: ChatTurn.ID) async {
        guard !isLoading,
              let idx = turns.firstIndex(where: { $0.id == turnID }),
              turns[idx].errorMessage != nil
        else { return }
        let question = turns[idx].question
        turns[idx].state = .loading
        await run(turnID: turnID, question: question)
    }

    /// The shared request/resolve body for `send` and `retry`. Carries prior turns
    /// STRICTLY BEFORE the target turn as context — a retry of turn N must not feed
    /// turn N's own (empty/failed) sources back to itself, and a later turn's
    /// sources are not in scope for an earlier retry.
    private func run(turnID: ChatTurn.ID, question: String) async {
        isLoading = true
        defer { isLoading = false }

        let priorTurns = priorPointers(before: turnID)
        let req = ChatAnswerRequest(
            question: question,
            priorTurns: priorTurns,
            limit: limit
        )
        do {
            let response = try await service.chatAnswer(req)
            update(turnID: turnID, state: .answered(ChatAnswer(response: response)))
        } catch {
            update(turnID: turnID, state: .failed(Self.describe(error)))
        }
    }

    /// The prior-turn source pointers to carry forward as context — flattened from
    /// every ANSWERED turn strictly before `beforeID` (KTD6/R14). Only pointers
    /// travel; the daemon re-derives snippet text server-side.
    private func priorPointers(before beforeID: ChatTurn.ID) -> [ChatPriorTurnPointer] {
        var pointers: [ChatPriorTurnPointer] = []
        for turn in turns {
            if turn.id == beforeID { break }
            guard let answer = turn.answer else { continue }
            for source in answer.sources {
                guard let ts = source.timestampMs else { continue }
                pointers.append(
                    ChatPriorTurnPointer(
                        recording: source.recording,
                        timestampMs: ts,
                        stream: source.stream
                    )
                )
            }
        }
        return pointers
    }

    private func update(turnID: ChatTurn.ID, state: ChatTurn.State) {
        guard let idx = turns.firstIndex(where: { $0.id == turnID }) else { return }
        turns[idx].state = state
    }

    /// Map a thrown error to a short human-readable line. Daemon-unreachable is
    /// called out distinctly (its own affordance) from a generic failure.
    static func describe(_ error: Error) -> String {
        switch error {
        case DaemonClientError.socketUnavailable, DaemonClientError.connectionFailed:
            return "ScreenCap isn't running. Start ScreenCap's background helper to ask about your history."
        case let daemonError as DaemonClientError:
            return daemonError.errorDescription ?? "Something went wrong answering that."
        default:
            return "Something went wrong answering that. Please try again."
        }
    }

    /// True when the last turn failed specifically because the daemon is
    /// unreachable — drives a distinct "helper not running" affordance vs a
    /// generic retry.
    var lastTurnDaemonDown: Bool {
        guard case .failed(let msg)? = turns.last?.state else { return false }
        return msg.hasPrefix("ScreenCap isn't running")
    }
}
