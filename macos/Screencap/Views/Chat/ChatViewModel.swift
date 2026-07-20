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
    /// Why the turn refused (U2), or `nil` on a real answer. Drives `honestState`.
    let reason: ChatRefusalReason?

    init(response: ChatAnswerResponse) {
        self.text = response.answer
        self.sources = response.sources
        self.coverage = response.coverage
        self.refusal = response.refusal
        self.questionKind = response.questionKind
        self.target = response.target
        self.reason = ChatRefusalReason(wire: response.reason)
    }

    init(
        text: String, sources: [ChatSource], coverage: ChatCoverage,
        refusal: Bool = false, questionKind: ChatQuestionKind = .point,
        target: String? = "on_device", reason: ChatRefusalReason? = nil
    ) {
        self.text = text
        self.sources = sources
        self.coverage = coverage
        self.refusal = refusal
        self.questionKind = questionKind
        self.target = target
        self.reason = reason
    }

    /// The single honest state this answer renders as (R5/R7) — a unified mapping
    /// from the refusal reason and the coverage state so the view never derives it
    /// ad hoc. (The daemon/helper-unreachable state is a TRANSPORT failure — a
    /// `.failed` turn — resolved at the turn level, not from a `ChatAnswer`.)
    ///
    /// Precedence: a real answer is `.answered`; else `no_backend` → `.noBackend`;
    /// else `no_evidence` (or a coverage `no_matching_moments`) → `.noMatchingMoments`;
    /// else (`unsupported` / `blocked` / unknown) → `.safeRefusal`.
    var honestState: ChatHonestState {
        guard refusal else { return .answered }
        switch reason {
        case .noBackend:
            return .noBackend
        case .noEvidence:
            return .noMatchingMoments
        case .unsupported, .blocked, .none:
            return coverage.state == .noMatchingMoments ? .noMatchingMoments : .safeRefusal
        }
    }

    /// True when this turn's coverage indicates OCR indexing would help and the
    /// UI should surface the one-time consent affordance inline on THIS turn (R13).
    /// Suppressed for `.noBackend` (R7): telling the user to turn on indexing when
    /// the real blocker is that no AI backend exists re-creates the exact
    /// conflation the honest-state work removes.
    var suggestsOcrConsent: Bool {
        honestState != .noBackend && coverage.state.suggestsOcrConsent
    }
}

/// The distinct user-facing states a Chat turn renders as (R5). `daemonUnreachable`
/// is a transport failure (a `.failed` turn); the rest derive from a `ChatAnswer`.
enum ChatHonestState: Sendable, Equatable {
    /// A grounded answer was produced.
    case answered
    /// No answer backend was available (on-device gated off + no consented cloud) —
    /// the transparent both-paths affordance (R6).
    case noBackend
    /// Retrieval ran but nothing matched — distinct from no-backend (R5).
    case noMatchingMoments
    /// A safety refusal (attribution rejection / egress guard / unknown reason) —
    /// render the daemon's own refusal text, not the no-backend affordance.
    case safeRefusal
    /// The daemon or helper couldn't be reached (transport failure).
    case daemonUnreachable
}

// MARK: - No-backend affordance guidance (R6)

/// The selected intelligence model, narrowed to what the no-backend affordance
/// needs to know: whether the user picked Apple's built-in on-device model —
/// whose runtime availability is a *system* switch (Apple Intelligence) the app
/// can't flip — versus anything else, whose fix lives in Intelligence Settings.
enum SelectedIntelligence: Equatable, Sendable {
    /// The built-in Apple Foundation Models ("On-device model"). Gated at runtime
    /// by the system-wide Apple Intelligence switch, not by any in-app setting.
    case appleOnDevice
    /// Any other selection (cloud/BYO provider, the downloaded model, a local
    /// server) — or unknown/unloaded.
    case other

    /// Map an `[intelligence].provider` id (see `IntelligenceSettings.provider`)
    /// to the affordance-relevant kind. Only `"on-device"` is Apple's system-gated
    /// model; `"downloaded"` runs WITHOUT Apple Intelligence, so it is `.other`.
    init(provider: String?) {
        self = (provider == "on-device") ? .appleOnDevice : .other
    }
}

/// Pure, testable copy + actions for the "no AI backend" affordance (R5/R6). The
/// mapping — selected model × whether this OS can run Apple's on-device model —
/// lives here so it is unit-tested without SwiftUI; `ChatTurnView` renders it.
struct NoBackendGuidance: Equatable, Sendable {
    let title: String
    let body: String
    /// Show the "Open System Settings" button — only when Apple's on-device model
    /// is selected on an OS that can run it, where the block is the system-wide
    /// Apple Intelligence switch (which the app can only deep-link to, not flip).
    let showsSystemSettings: Bool

    /// - Parameters:
    ///   - selected: the model the user picked (from `IntelligenceSettings.provider`).
    ///   - onDeviceStatus: the live Apple-on-device availability (probed natively).
    ///     Only consulted when the on-device model is selected; it also encodes OS
    ///     capability (`.osUnsupported` == below the macOS-26 floor).
    static func make(
        selected: SelectedIntelligence, onDeviceStatus: OnDeviceModelStatus
    ) -> NoBackendGuidance {
        // The transparent both-paths affordance (copy unchanged from before the
        // per-status work): a cloud/BYO/downloaded selection, or on-device below the
        // macOS-26 floor. Never hides a path; never a system-toggle nudge.
        func generic() -> NoBackendGuidance {
            let capable = onDeviceStatus != .osUnsupported
            return NoBackendGuidance(
                title: "No AI model is set up to answer yet",
                body: capable
                    ? "Answer on this Mac with Apple Intelligence, or turn on cloud recall in "
                        + "Intelligence Settings (your data leaves this Mac, only with your consent)."
                    : "To answer here, turn on cloud recall in Intelligence Settings (your data "
                        + "leaves this Mac, only with your consent). On-device answers need macOS 26 "
                        + "with Apple Intelligence.",
                showsSystemSettings: false
            )
        }

        // Only the on-device model has a system-gated backend to explain; every
        // other selection uses the generic affordance.
        guard selected == .appleOnDevice else { return generic() }

        switch onDeviceStatus {
        case .appleIntelligenceOff:
            // The confirmed system-toggle case — name the exact step and deep-link.
            // The generic "no model set up" copy reads as "but I already picked
            // on-device" and strands the user (the bug this feature fixes).
            return NoBackendGuidance(
                title: "Turn on Apple Intelligence to answer on this Mac",
                body: "You picked the on-device model, but Apple Intelligence is turned off in "
                    + "System Settings. Turn it on under Apple Intelligence & Siri to answer here "
                    + "— nothing leaves this Mac. You can also turn on cloud recall in Intelligence "
                    + "Settings (your data leaves this Mac, only with your consent).",
                showsSystemSettings: true
            )
        case .modelDownloading:
            return NoBackendGuidance(
                title: "Apple Intelligence is getting ready",
                body: "Apple Intelligence is still downloading its on-device model — answers on "
                    + "this Mac will work once it finishes. In the meantime you can turn on cloud "
                    + "recall in Intelligence Settings (your data leaves this Mac, only with your "
                    + "consent).",
                showsSystemSettings: false
            )
        case .notEligible:
            return NoBackendGuidance(
                title: "This Mac can't run the on-device model",
                body: "This Mac isn't eligible for Apple Intelligence, so it can't answer "
                    + "on-device. Turn on cloud recall in Intelligence Settings to answer here "
                    + "(your data leaves this Mac, only with your consent).",
                showsSystemSettings: false
            )
        case .unknown:
            // Probe couldn't determine the state — on-device is selected, so the
            // likely fix is enabling Apple Intelligence; keep the hedged copy (the
            // pre-probe #364 behavior) and still offer the deep link.
            return NoBackendGuidance(
                title: "Turn on Apple Intelligence to answer on this Mac",
                body: "You picked the on-device model, but Apple Intelligence may be turned off "
                    + "in System Settings. Turn it on under Apple Intelligence & Siri to answer "
                    + "here — nothing leaves this Mac. If it's already on, its model may still be "
                    + "downloading. You can also turn on cloud recall in Intelligence Settings.",
                showsSystemSettings: true
            )
        case .osUnsupported, .available:
            // Below the macOS-26 floor → the generic OS-gated copy. `.available`
            // shouldn't pair with a no-backend refusal, but if it does the specific
            // Apple-Intelligence copy would be misleading, so fall back to generic.
            return generic()
        }
    }
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
            return "Screencap isn't running. Start Screencap's background helper to ask about your history."
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
        return msg.hasPrefix("Screencap isn't running")
    }
}
