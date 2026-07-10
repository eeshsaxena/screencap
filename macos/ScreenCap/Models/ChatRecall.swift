import Foundation

// SCR-243 / conversational-recall U7 — pointer-only request/response models for
// the Chat surface, mirroring the daemon's `/v0/chat.answer` verb (U5). By shape
// they are POINTER ONLY — a source is `(recording, timestampMs, stream)`, never a
// media path or image bytes (R2/R8). The deep-link (U8) opens the Inspect window
// from `(recording, timestampMs)`.
//
// The wire contract is the authoritative source of truth:
//   `src/screencap/daemon/schema.py` — ChatAnswerRequest / ChatSourcePointer /
//   ChatCoverage / ChatAnswerResponse.
// Nullable/optional fields decode as OPTIONAL (the review-data-nullable-timing
// lesson): a missing or unknown value must never crash the decode.

// MARK: - Enumerations (tolerant string decoding)

/// Which retrieval stream a chat source came from. `timeline` is authoritative
/// (event tables, no OCR loss); `content` (screen OCR) and `transcript` (audio)
/// are best-effort. Unknown/missing defaults to `.content` so a future stream
/// never crashes the decode — the pessimistic direction here is "best-effort".
enum ChatSourceStream: String, Sendable, Equatable {
    case content
    case transcript
    case timeline

    init(wire: String?) {
        self = ChatSourceStream(rawValue: wire ?? "") ?? .content
    }
}

/// The honest coverage state for a chat answer (R12), mirrored from the daemon's
/// `CoverageState`. Unknown/missing defaults to `.storeUnavailable` — pessimistic,
/// never silently "ok" (mirrors `ContentIndexState`'s defaulting).
enum ChatCoverageState: String, Sendable, Equatable {
    case ok
    case noMatchingMoments = "no_matching_moments"
    case notIndexed = "not_indexed"
    case indexDegraded = "index_degraded"
    case storeUnavailable = "store_unavailable"

    init(wire: String?) {
        self = ChatCoverageState(rawValue: wire ?? "") ?? .storeUnavailable
    }

    /// The states where the honest answer is "coverage is limited" and a content
    /// question would benefit from turning OCR indexing on (R13). Drives the
    /// inline one-time-consent affordance on the specific thin-evidence turn.
    var suggestsOcrConsent: Bool {
        switch self {
        case .notIndexed, .indexDegraded, .storeUnavailable:
            return true
        case .ok, .noMatchingMoments:
            return false
        }
    }
}

/// A chat question is a point lookup or a period summary (R3). Unknown/missing
/// defaults to `.point`.
enum ChatQuestionKind: String, Sendable, Equatable {
    case point
    case aggregate

    init(wire: String?) {
        self = ChatQuestionKind(rawValue: wire ?? "") ?? .point
    }
}

// MARK: - Request

/// A prior-turn source POINTER the client carries forward (KTD6, R14). Multi-turn
/// memory is client-held, daemon-stateless: the client sends the pointer and the
/// daemon re-derives the snippet text SERVER-SIDE from it. There is deliberately
/// NO text field — client-supplied prose can never round-trip through the daemon
/// (matches the daemon `ChatPriorTurn` shape).
struct ChatPriorTurnPointer: Encodable, Sendable, Equatable, Hashable {
    let recording: String
    let timestampMs: Int
    let stream: String

    init(recording: String, timestampMs: Int, stream: ChatSourceStream) {
        self.recording = recording
        self.timestampMs = timestampMs
        self.stream = stream.rawValue
    }

    enum CodingKeys: String, CodingKey {
        case recording
        case timestampMs = "timestamp_ms"
        case stream
    }
}

/// `chat.answer` input: a question + optional prior-turn context. `question` is
/// the operator's question (or a follow-up). `priorTurns` are prior-turn source
/// POINTERS (R14) — never prose. `windowMs` is an optional `[startMs, endMs]`
/// pair for a period-summary (aggregate) question (R5); a point question omits it.
/// `app` narrows the aggregate window / timeline; `limit` bounds retrieval.
///
/// Optional fields are omitted from the encoded body when nil (default
/// `JSONEncoder` behavior), so a stale daemon that doesn't model them ignores
/// nothing it must read.
struct ChatAnswerRequest: Encodable, Sendable, Equatable {
    let question: String
    let priorTurns: [ChatPriorTurnPointer]
    /// `[startMs, endMs]` — encoded as a two-element JSON array to match the
    /// daemon's `tuple[int, int]`.
    let windowMs: [Int]?
    let app: String?
    let limit: Int?

    init(
        question: String,
        priorTurns: [ChatPriorTurnPointer] = [],
        windowMs: [Int]? = nil,
        app: String? = nil,
        limit: Int? = nil
    ) {
        self.question = question
        self.priorTurns = priorTurns
        self.windowMs = windowMs
        self.app = app
        self.limit = limit
    }

    enum CodingKeys: String, CodingKey {
        case question
        case priorTurns = "prior_turns"
        case windowMs = "window_ms"
        case app
        case limit
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(question, forKey: .question)
        // `prior_turns` always present (the daemon defaults it to []); encoding it
        // explicitly keeps the wire shape stable even for the empty first turn.
        try c.encode(priorTurns, forKey: .priorTurns)
        try c.encodeIfPresent(windowMs, forKey: .windowMs)
        try c.encodeIfPresent(app, forKey: .app)
        try c.encodeIfPresent(limit, forKey: .limit)
    }
}

// MARK: - Response

/// A source the answer drew from — POINTER ONLY (R2, R8). Structurally incapable
/// of carrying a media path or image bytes: `(recording, timestampMs)` is exactly
/// what `frame.nearest` resolves to a frame stem, so the sources strip deep-links
/// the moment. `timestampMs` is decoded for EVERY stream (including transcript),
/// so a transcript-stream source stays jumpable — the daemon enriches transcript
/// hits with a resolvable chunk-start `timestamp_ms` (U5 contract).
struct ChatSource: Decodable, Sendable, Equatable, Hashable, Identifiable {
    let recording: String
    /// Absolute unix ms — the deep-link seek target. Optional-tolerant on decode
    /// (a malformed/absent value yields a non-jumpable source rather than a crash),
    /// but the daemon always supplies it in v1.
    let timestampMs: Int?
    private let streamRaw: String?

    var stream: ChatSourceStream { ChatSourceStream(wire: streamRaw) }

    /// Stable identity for `ForEach` — a pointer plus its stream is unique enough
    /// within one answer's source list.
    var id: String { "\(stream.rawValue)-\(recording)-\(timestampMs.map(String.init) ?? "u")" }

    init(recording: String, timestampMs: Int?, stream: ChatSourceStream) {
        self.recording = recording
        self.timestampMs = timestampMs
        self.streamRaw = stream.rawValue
    }

    enum CodingKeys: String, CodingKey {
        case recording
        case timestampMs = "timestamp_ms"
        case streamRaw = "stream"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        recording = try c.decode(String.self, forKey: .recording)
        timestampMs = try c.decodeIfPresent(Int.self, forKey: .timestampMs)
        streamRaw = try c.decodeIfPresent(String.self, forKey: .streamRaw)
    }
}

/// The honest coverage descriptor for a chat answer (R12). `state` maps to
/// `ChatCoverageState`; `note` is a short narration string; `perStream` maps each
/// stream name to its index-state value so the UI can say "content is still
/// indexing; timeline is authoritative".
struct ChatCoverage: Decodable, Sendable, Equatable {
    private let stateRaw: String?
    let note: String
    let perStream: [String: String]

    var state: ChatCoverageState { ChatCoverageState(wire: stateRaw) }

    init(state: ChatCoverageState, note: String, perStream: [String: String] = [:]) {
        self.stateRaw = state.rawValue
        self.note = note
        self.perStream = perStream
    }

    enum CodingKeys: String, CodingKey {
        case stateRaw = "state"
        case note
        case perStream = "per_stream"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        stateRaw = try c.decodeIfPresent(String.self, forKey: .stateRaw)
        note = try c.decodeIfPresent(String.self, forKey: .note) ?? ""
        perStream = try c.decodeIfPresent([String: String].self, forKey: .perStream) ?? [:]
    }
}

/// `chat.answer` response — a grounded answer + its sources + honest coverage,
/// POINTER ONLY. `answer` is the generated prose (or the canonical refusal text
/// on a refusal). `refusal` flags a no-evidence / no-execution-target /
/// attribution-rejected turn so the client renders it distinctly. `questionKind`
/// is `point` / `aggregate`; `target` is the execution target this turn resolved
/// to (`on_device` / `cloud` / `none`), recomputed per turn.
struct ChatAnswerResponse: Decodable, Sendable, Equatable {
    let ok: Bool
    let schemaVersion: Int
    let daemonVersion: String
    let apiSchemaVersion: Int
    let answer: String
    let sources: [ChatSource]
    let coverage: ChatCoverage
    let refusal: Bool
    private let questionKindRaw: String?
    /// The execution target the turn resolved to — surfaced verbatim so the UI can
    /// show "answered on this Mac" vs "cloud". Optional-tolerant.
    let target: String?

    var questionKind: ChatQuestionKind { ChatQuestionKind(wire: questionKindRaw) }

    init(
        ok: Bool = true, schemaVersion: Int = 1, daemonVersion: String = "test",
        apiSchemaVersion: Int = 1, answer: String, sources: [ChatSource],
        coverage: ChatCoverage, refusal: Bool = false,
        questionKind: ChatQuestionKind = .point, target: String? = "on_device"
    ) {
        self.ok = ok
        self.schemaVersion = schemaVersion
        self.daemonVersion = daemonVersion
        self.apiSchemaVersion = apiSchemaVersion
        self.answer = answer
        self.sources = sources
        self.coverage = coverage
        self.refusal = refusal
        self.questionKindRaw = questionKind.rawValue
        self.target = target
    }

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
        case answer
        case sources
        case coverage
        case refusal
        case questionKindRaw = "question_kind"
        case target
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = try c.decodeIfPresent(Bool.self, forKey: .ok) ?? true
        schemaVersion = try c.decodeIfPresent(Int.self, forKey: .schemaVersion) ?? 1
        daemonVersion = try c.decodeIfPresent(String.self, forKey: .daemonVersion) ?? ""
        apiSchemaVersion = try c.decodeIfPresent(Int.self, forKey: .apiSchemaVersion) ?? 1
        answer = try c.decodeIfPresent(String.self, forKey: .answer) ?? ""
        sources = try c.decodeIfPresent([ChatSource].self, forKey: .sources) ?? []
        coverage = try c.decode(ChatCoverage.self, forKey: .coverage)
        refusal = try c.decodeIfPresent(Bool.self, forKey: .refusal) ?? false
        questionKindRaw = try c.decodeIfPresent(String.self, forKey: .questionKindRaw)
        target = try c.decodeIfPresent(String.self, forKey: .target)
    }
}
