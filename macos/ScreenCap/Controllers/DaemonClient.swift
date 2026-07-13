import Darwin
import Foundation
import Network
import os

private let daemonLogger = Logger(subsystem: "com.screencap.macos", category: "daemon-client")

private final class DaemonStreamConnectionBox: @unchecked Sendable {
    private let lock = NSLock()
    private var connection: NWConnection?

    func set(_ connection: NWConnection?) {
        lock.lock()
        self.connection = connection
        lock.unlock()
    }

    func cancel() {
        lock.lock()
        let connection = self.connection
        self.connection = nil
        lock.unlock()
        connection?.cancel()
    }
}

enum DaemonClientError: LocalizedError {
    case socketUnavailable(path: String)
    case connectionFailed(underlying: Error)
    case httpError(status: Int, body: String)
    case decode(underlying: Error, raw: String)
    case schemaMismatch(expected: Int, got: Int)
    // `rawBody` is the original response body (after envelope parse confirmed
    // it was a daemon error). Callers that need fields beyond `code` decode
    // it themselves — keeping the bytes here means the error case stays
    // `Sendable` and avoids a `[String: Any]` payload that breaks under
    // SWIFT_STRICT_CONCURRENCY: complete.
    case envelopeError(code: String, rawBody: Data)
    case timedOut(seconds: TimeInterval)
    case streamClosed(reason: String)

    var errorDescription: String? {
        switch self {
        case .socketUnavailable(let path):
            return "ScreenCap daemon socket is not available at \(path)."
        case .connectionFailed(let underlying):
            return "Failed to connect to ScreenCap daemon: \(underlying.localizedDescription)"
        case .httpError(let status, let body):
            return "ScreenCap daemon returned HTTP \(status): \(body)"
        case .decode(let underlying, _):
            return "Failed to decode ScreenCap daemon response: \(underlying.localizedDescription)"
        case .schemaMismatch(let expected, let got):
            return "ScreenCap daemon API schema mismatch. Expected \(expected), got \(got)."
        case .envelopeError(let code, _):
            return "ScreenCap daemon returned \(code)."
        case .timedOut(let seconds):
            return "ScreenCap daemon request timed out after \(Int(seconds))s."
        case .streamClosed(let reason):
            return "ScreenCap daemon event stream closed: \(reason)."
        }
    }
}

/// Tri-state daemon TCC grant, mirrored from the Python probe
/// (`screencap.daemon.permission_probe`). Decoding is deliberately tolerant:
/// any unknown or missing token maps to `.indeterminate` — never `.denied` — so
/// a grant block we can't interpret (an older daemon, a future token) never
/// blocks recording or nags the user. Mirrors the engine's fail-open-on-None
/// revocation rule (review-data-nullable-timing learning).
enum DaemonGrantState: String, Sendable, Equatable {
    case granted
    case denied
    case indeterminate

    init(wire: String?) {
        switch wire {
        case "granted": self = .granted
        case "denied": self = .denied
        default: self = .indeterminate
        }
    }
}

/// The additive `permissions` block on `daemon.info` (U2). Reports the daemon's
/// *live* TCC grant state for the three required permissions. This answers a
/// different question than `PermissionController`'s app-process statuses — it is
/// the daemon's (TCC subject's) state, not the app's.
struct DaemonPermissionGrants: Decodable, Equatable, Sendable {
    let screenRecording: DaemonGrantState
    let accessibility: DaemonGrantState
    let inputMonitoring: DaemonGrantState

    enum CodingKeys: String, CodingKey {
        case screenRecording = "screen_recording"
        case accessibility = "accessibility"
        case inputMonitoring = "input_monitoring"
    }

    init(
        screenRecording: DaemonGrantState,
        accessibility: DaemonGrantState,
        inputMonitoring: DaemonGrantState
    ) {
        self.screenRecording = screenRecording
        self.accessibility = accessibility
        self.inputMonitoring = inputMonitoring
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        screenRecording = DaemonGrantState(wire: try c.decodeIfPresent(String.self, forKey: .screenRecording))
        accessibility = DaemonGrantState(wire: try c.decodeIfPresent(String.self, forKey: .accessibility))
        inputMonitoring = DaemonGrantState(wire: try c.decodeIfPresent(String.self, forKey: .inputMonitoring))
    }

    /// Used when the daemon omits the block entirely (older daemon) or the
    /// payload can't be read. Indeterminate never blocks or nags.
    static let allIndeterminate = DaemonPermissionGrants(
        screenRecording: .indeterminate,
        accessibility: .indeterminate,
        inputMonitoring: .indeterminate
    )

    /// The required daemon grants — Screen Recording + Accessibility — are
    /// confirmed granted. Indeterminate is NOT granted; this is the positive
    /// "everything's good" predicate that drives auto-close of the walkthrough.
    ///
    /// Input Monitoring is deliberately excluded (SCR-196 follow-up): on
    /// macOS 26.x the daemon helper cannot register a toggleable Input
    /// Monitoring row by any mechanism (request API, the `CGEventTapCreate`
    /// listen-only touch, or a real capture's tap — all verified on-device to
    /// produce no row), so requiring it stranded every user on a grant that is
    /// impossible to obtain. IM is advisory, not capture-fatal (the worker's
    /// `FreshScreenWatch` gates capture on Screen Recording alone), so it is
    /// treated like the microphone: optional, never blocking onboarding.
    var allRequiredGranted: Bool {
        screenRecording == .granted
            && accessibility == .granted
    }

    /// Screen Recording specifically reports denied — the one permission fatal
    /// to capture, so the recording-start block keys on it (U4/U6 decision:
    /// hard-block start on Screen Recording only; advisory for the other two).
    var screenRecordingDenied: Bool {
        screenRecording == .denied
    }

    /// A required grant (Screen Recording or Accessibility) reports denied —
    /// drives whether the walkthrough is surfaced (R3). Input Monitoring is
    /// excluded for the same reason as `allRequiredGranted`: it can't be
    /// registered for the helper on macOS 26.x, so keying on it kept the
    /// walkthrough/recovery banner surfaced forever even after the user had
    /// granted everything that is actually grantable.
    var anyRequiredDenied: Bool {
        screenRecording == .denied
            || accessibility == .denied
    }
}

struct DaemonInfoResponse: Decodable {
    let ok: Bool
    let schemaVersion: Int
    let daemonVersion: String
    let apiSchemaVersion: Int
    let build: String?
    let startedAt: Double
    /// Additive (U2). Absent on older daemons; callers map nil to
    /// `.allIndeterminate`.
    let permissions: DaemonPermissionGrants?
    /// SCR-258 U4 (KTD-14): the encrypted-store state — `"mounted"` / `"locked"` /
    /// `"absent"` / `"error"`. Absent on older daemons → treated as `"mounted"`.
    let storeState: String?
    /// The `store_state=error` sub-cause (`key_missing` / `entitlement_mismatch` /
    /// `keychain_locked` / `downgrade_unsupported`); nil otherwise. Only
    /// `daemon.info` carries it — `recording.list` does not — so the app reads the
    /// reason here when the list envelope reports `error`.
    let storeReason: String?

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
        case build
        case startedAt = "started_at"
        case permissions
        case storeState = "store_state"
        case storeReason = "store_reason"
    }

    /// The typed store state, tolerant of an older daemon that omits the fields.
    var resolvedStoreState: StoreState {
        StoreState.from(state: storeState, reason: storeReason)
    }
}

struct ListResponse: Decodable {
    let ok: Bool
    let schemaVersion: Int
    let daemonVersion: String
    let apiSchemaVersion: Int
    let recordings: [RecordingSummary]
    /// SCR-258 U4/U10 (KTD-20): the store state on the SUCCESS envelope. A locked /
    /// absent / error store returns the normal envelope with an EMPTY `recordings`
    /// list and this set — never a 500. Absent on an older daemon → `"mounted"`.
    /// Note: `recording.list` does NOT carry `store_reason`; the caller enriches an
    /// `error` state from `daemon.info`.
    let storeState: String?

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
        case recordings
        case storeState = "store_state"
    }

    /// The typed store state, tolerant of an older daemon that omits the field.
    var resolvedStoreState: StoreState {
        StoreState.from(state: storeState)
    }
}

// MARK: - SCR-258 storage lifecycle verbs (U9/U6, KTD-15/KTD-18)

/// Response shape for `storage.lock` / `storage.unlock` — the standard envelope
/// plus the resulting `store_state` and the `sealed` flag.
struct StorageStateResponse: Decodable {
    let ok: Bool
    let schemaVersion: Int
    let daemonVersion: String
    let apiSchemaVersion: Int
    let storeState: String
    let sealed: Bool?

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
        case storeState = "store_state"
        case sealed
    }
}

/// Privacy-safe migration snapshot from `storage.encrypt.start|status|cancel`
/// (R9 — counts + run state + cutover phase + a non-identifying pause reason, NEVER
/// a recording name). `pausedReason == "insufficient_disk"` is the auto-resuming
/// disk pause the app renders distinctly from `.failed` (KTD-18).
struct StorageEncryptResponse: Decodable {
    let ok: Bool
    let schemaVersion: Int
    let daemonVersion: String
    let apiSchemaVersion: Int
    let state: String
    let phase: String
    let pending: Int
    let copied: Int
    let verified: Int
    let deleted: Int
    let total: Int
    let pausedReason: String?

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
        case state
        case phase
        case pending
        case copied
        case verified
        case deleted
        case total
        case pausedReason = "paused_reason"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = try c.decode(Bool.self, forKey: .ok)
        schemaVersion = try c.decode(Int.self, forKey: .schemaVersion)
        daemonVersion = try c.decode(String.self, forKey: .daemonVersion)
        apiSchemaVersion = try c.decode(Int.self, forKey: .apiSchemaVersion)
        state = try c.decodeIfPresent(String.self, forKey: .state) ?? "idle"
        phase = try c.decodeIfPresent(String.self, forKey: .phase) ?? "not_started"
        pending = try c.decodeIfPresent(Int.self, forKey: .pending) ?? 0
        copied = try c.decodeIfPresent(Int.self, forKey: .copied) ?? 0
        verified = try c.decodeIfPresent(Int.self, forKey: .verified) ?? 0
        deleted = try c.decodeIfPresent(Int.self, forKey: .deleted) ?? 0
        total = try c.decodeIfPresent(Int.self, forKey: .total) ?? 0
        pausedReason = try c.decodeIfPresent(String.self, forKey: .pausedReason)
    }
}

struct SessionSnapshotResponse: Decodable {
    let ok: Bool
    let schemaVersion: Int
    let daemonVersion: String
    let apiSchemaVersion: Int
    let isRecording: Bool?
    let daemonOwned: Bool
    let recordingName: String?
    let startedAt: Double?
    let claimant: String?
    let recovering: Bool
    let claimantPID: Int?
    let claimantStartedAt: Double?
    let enginePID: Int?
    let framesWritten: Int?
    let cursor: Int
    /// SCR-254 (U7): the recording's CONFIRMED mic-mute state. Additive — absent
    /// until the first confirmed `audio_muted` / `audio_unmuted` event, so an
    /// unmuted (or pre-first-mute) recording and a stale daemon both omit it. The
    /// controller maps a missing value to `false` (unmuted), mirroring the
    /// additive `audio`-echo back-compat rule on `RecordingStartResponse`.
    let muted: Bool?

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
        case isRecording = "is_recording"
        case daemonOwned = "daemon_owned"
        case recordingName = "recording_name"
        case startedAt = "started_at"
        case claimant
        case recovering
        case claimantPID = "claimant_pid"
        case claimantStartedAt = "claimant_started_at"
        case enginePID = "engine_pid"
        case framesWritten = "frames_written"
        case cursor
        case muted
    }
}

struct RecordingStartRequest: Encodable {
    let name: String?
    let startedBy: String?
    /// U6: explicit audio choice from the New-recording sheet. `nil` omits the
    /// field so the daemon falls back to `audio_default` (the plain toolbar /
    /// menu-bar start path); `true`/`false` force the engine's audio on/off. A
    /// stale daemon ignores the unknown field and its response omits the echo —
    /// the app then treats the effective state as audio-on.
    let audio: Bool?
    /// Search U8: explicit stills override. `nil` omits the field so the daemon's
    /// readiness gate decides (default-on when the guardrails are ready);
    /// `true`/`false` force stills on/off for this recording (the pause/resume
    /// affordance). A stale daemon ignores the unknown field.
    let captureImages: Bool?

    init(name: String? = nil, startedBy: String? = nil, audio: Bool? = nil, captureImages: Bool? = nil) {
        self.name = name
        self.startedBy = startedBy
        self.audio = audio
        self.captureImages = captureImages
    }

    enum CodingKeys: String, CodingKey {
        case name
        case startedBy = "started_by"
        case audio
        case captureImages = "capture_images"
    }
}

struct RecordingStartResponse: Decodable {
    let ok: Bool
    let schemaVersion: Int
    let daemonVersion: String
    let apiSchemaVersion: Int
    let sessionID: String
    let startedAt: Double
    /// Required in API schema v1: the daemon always returns the spawned
    /// engine's PID on a successful start (`supervisor.spawn`). Non-optional
    /// by contract — contrast `SessionSnapshotResponse.enginePID`, which is
    /// optional because it is nil when no recording is in flight.
    let enginePID: Int
    /// Bus cursor captured BEFORE the engine spawn — feed into
    /// `/v0/events?since=<cursor>` to receive the `started` event without
    /// an extra `session.snapshot` round-trip.
    let cursor: Int
    /// U6: the EFFECTIVE audio state the engine started with (echo of the
    /// request's `audio`, or the daemon's `audio_default` when the request left
    /// it unspecified). `nil` when a stale daemon omits the field entirely — the
    /// app then assumes audio-on.
    let audio: Bool?

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
        case sessionID = "session_id"
        case startedAt = "started_at"
        case enginePID = "engine_pid"
        case cursor
        case audio
    }
}

struct RecordingStopRequest: Encodable {
    let force: Bool

    init(force: Bool = false) {
        self.force = force
    }
}

struct RecordingStopResponse: Decodable {
    let ok: Bool
    let schemaVersion: Int
    let daemonVersion: String
    let apiSchemaVersion: Int
    let stopped: Bool
    let finalState: String

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
        case stopped
        case finalState = "final_state"
    }
}

/// Body for the mid-recording mic-mute verb (SCR-254 U7). `muted` is the
/// ABSOLUTE desired state (true = muted), not a toggle, so a dropped/retried
/// request can never desync app vs engine (mirrors the daemon's
/// `RecordingMuteRequest`).
struct RecordingMuteRequest: Encodable {
    let muted: Bool

    init(muted: Bool) {
        self.muted = muted
    }
}

struct RecordingMuteResponse: Decodable {
    let ok: Bool
    let schemaVersion: Int
    let daemonVersion: String
    let apiSchemaVersion: Int
    /// ECHO of the REQUESTED state for transport bookkeeping only. The app must
    /// NOT treat this as confirmation — confirmed mute state arrives on the
    /// events stream / snapshot after the engine actually stops/starts capture
    /// (KTD4). `RecorderController.toggleMute` deliberately ignores it.
    let muted: Bool
    /// Bus cursor captured BEFORE the command was forwarded, so a fresh
    /// subscriber could `/v0/events?since=<cursor>` without missing the
    /// confirming `audio_muted` / `audio_unmuted` event. The app already holds a
    /// persistent `/v0/events` subscription (`consumeEventStream`) that catches
    /// the confirming event, so it does not re-subscribe from this cursor;
    /// decoded for completeness / diagnostics.
    let cursor: Int

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
        case muted
        case cursor
    }
}

/// Ack for the SCR-200 decoy-cleanup verb. The envelope carries no payload
/// beyond the standard fields — the sweep is fire-and-forget on the daemon.
struct CleanupDecoysResponse: Decodable {
    let ok: Bool
    let schemaVersion: Int
    let daemonVersion: String
    let apiSchemaVersion: Int

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
    }
}

/// Body for the on-demand daemon-driven registration verb (U8). `permission`
/// is one of the canonical strings (`screen_recording` / `accessibility` /
/// `input_monitoring`); the daemon validates it against the allowlist and
/// returns a typed `invalid_permission` error for anything else.
struct PermissionRequestRequest: Encodable {
    let permission: String

    init(permission: String) {
        self.permission = permission
    }
}

struct PermissionRequestResponse: Decodable {
    let ok: Bool
    let schemaVersion: Int
    let daemonVersion: String
    let apiSchemaVersion: Int
    let permission: String
    /// The daemon's request-API immediate grant bool. Advisory only — the
    /// daemon's in-process TCC state can be stale, so the app re-probes
    /// `daemon.info` for authoritative post-grant state rather than gating on
    /// this. Decoded for completeness/diagnostics.
    let alreadyGranted: Bool

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
        case permission
        case alreadyGranted = "already_granted"
    }
}

// MARK: - SCR-174 search verb requests (SCR-118 retrieval surface)

struct ContentSearchRequest: Encodable {
    let query: String
    let recording: String?
    let limit: Int?

    init(query: String, recording: String? = nil, limit: Int? = nil) {
        self.query = query
        self.recording = recording
        self.limit = limit
    }
}

struct TranscriptSearchRequest: Encodable {
    let query: String
    let recording: String?
    let limit: Int?

    init(query: String, recording: String? = nil, limit: Int? = nil) {
        self.query = query
        self.recording = recording
        self.limit = limit
    }
}

/// `recording.rename` input (editable titles U6). `recordingId` addresses the
/// target by its stable `recording_id` OR, for legacy recordings predating that
/// sidecar, by directory name (the daemon tries both). `title` is free DISPLAY
/// text — an EMPTY string clears the rename back to the derived default.
struct RecordingRenameRequest: Encodable {
    let recordingId: String
    let title: String

    enum CodingKeys: String, CodingKey {
        case recordingId = "recording_id"
        case title
    }
}

/// `recording.rename` output (editable titles U6). `title` is the value the
/// client should now show — the stripped user title when set, else the
/// freshly-recomputed date/time default (so a cleared rename returns the same
/// default `recording.list` would). `titleIsUserSet` is true only when a
/// non-empty title was persisted. `cursor` is the bus cursor captured BEFORE
/// the write.
struct RecordingRenameResponse: Decodable {
    let title: String
    let titleIsUserSet: Bool
    let cursor: Int

    enum CodingKeys: String, CodingKey {
        case title
        case titleIsUserSet = "title_is_user_set"
        case cursor
    }
}

/// `timeline.query` input. v1 always passes an explicit `limit` — the verb
/// defaults to 50 and truncates earliest-first by `timestamp_ms`, so without a
/// raised limit + bounding window the recency ranking would silently drop the
/// most recent hits.
struct TimelineQueryRequest: Encodable {
    let startMs: Int?
    let endMs: Int?
    let app: String?
    let recording: String?
    let limit: Int?

    init(
        startMs: Int? = nil,
        endMs: Int? = nil,
        app: String? = nil,
        recording: String? = nil,
        limit: Int? = nil
    ) {
        self.startMs = startMs
        self.endMs = endMs
        self.app = app
        self.recording = recording
        self.limit = limit
    }

    enum CodingKeys: String, CodingKey {
        case startMs = "start_ms"
        case endMs = "end_ms"
        case app
        case recording
        case limit
    }
}

/// `timeline.day` input (U3/U9): a local calendar date plus the caller's UTC
/// offset so the daemon computes the same local-midnight window the UI shows.
struct TimelineDayRequest: Encodable {
    let date: String
    let tzOffsetSeconds: Int

    enum CodingKeys: String, CodingKey {
        case date
        case tzOffsetSeconds = "tz_offset_seconds"
    }
}

/// A blocked span on the day timeline, absolute unix ms.
struct DayBlockedInterval: Decodable, Sendable, Hashable {
    let startMs: Int
    let endMs: Int

    init(startMs: Int, endMs: Int) {
        self.startMs = startMs
        self.endMs = endMs
    }

    enum CodingKeys: String, CodingKey {
        case startMs = "start_ms"
        case endMs = "end_ms"
    }
}

/// One recording's day-clamped span + honest blocked-interval split (U3).
/// `blockedProven` may be hatched "blocked"; `unverifiable` must render as a
/// neutral gap, never labelled "blocked" (R7).
struct DaySegmentRecording: Decodable, Sendable, Hashable {
    let name: String
    let recordingId: String?
    let state: String
    let startMs: Int
    let endMs: Int
    let blockedProven: [DayBlockedInterval]
    let unverifiable: [DayBlockedInterval]

    init(
        name: String, recordingId: String? = nil, state: String = "ready",
        startMs: Int, endMs: Int,
        blockedProven: [DayBlockedInterval] = [], unverifiable: [DayBlockedInterval] = []
    ) {
        self.name = name
        self.recordingId = recordingId
        self.state = state
        self.startMs = startMs
        self.endMs = endMs
        self.blockedProven = blockedProven
        self.unverifiable = unverifiable
    }

    enum CodingKeys: String, CodingKey {
        case name
        case recordingId = "recording_id"
        case state
        case startMs = "start_ms"
        case endMs = "end_ms"
        case blockedProven = "blocked_proven"
        case unverifiable
    }
}

/// `timeline.day` response.
struct TimelineDayResponse: Decodable, Sendable {
    let ok: Bool
    let schemaVersion: Int
    let daemonVersion: String
    let apiSchemaVersion: Int
    let date: String
    let recordings: [DaySegmentRecording]

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
        case date
        case recordings
    }
}

private struct APIEnvelopeProbe: Decodable {
    let ok: Bool?
    let apiSchemaVersion: Int?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok
        case apiSchemaVersion = "api_schema_version"
        case error
    }
}

enum DaemonClient {
    static func socketPath() -> String {
        if let override = ProcessInfo.processInfo.environment["SCREENCAP_DAEMON_SOCKET"],
           !override.isEmpty {
            return override
        }
        return AppPaths.daemonSocket.path
    }

    static func request<T: Decodable>(
        method: String,
        path: String,
        body: Data? = nil,
        timeout: TimeInterval = 10
    ) async throws -> T {
        let socket = socketPath()
        guard FileManager.default.fileExists(atPath: socket) else {
            throw DaemonClientError.socketUnavailable(path: socket)
        }

        let connection = NWConnection(to: .unix(path: socket), using: .tcp)
        return try await withTimeout(seconds: timeout, connection: connection) {
            try await connect(connection)
            try await sendRequest(connection: connection, method: method, path: path, body: body)
            let responseBytes = try await readAll(connection: connection)
            connection.cancel()
            let response = try parseHTTPResponse(responseBytes)
            return try decodeResponse(T.self, response: response)
        }
    }

    static func subscribe(
        path: String = "/v0/events",
        sinceCursor: Int? = nil,
        socketPathOverride: String? = nil
    ) -> AsyncThrowingStream<RecorderEventLine, Error> {
        AsyncThrowingStream { continuation in
            let connectionBox = DaemonStreamConnectionBox()

            let task = Task {
                let queryPath: String
                if let sinceCursor {
                    queryPath = "\(path)?since=\(sinceCursor)"
                } else {
                    queryPath = path
                }

                let socket = socketPathOverride ?? socketPath()
                guard FileManager.default.fileExists(atPath: socket) else {
                    continuation.finish(throwing: DaemonClientError.socketUnavailable(path: socket))
                    return
                }

                let connection = NWConnection(to: .unix(path: socket), using: .tcp)
                connectionBox.set(connection)
                do {
                    try await connect(connection)
                    try await sendRequest(connection: connection, method: "GET", path: queryPath, body: nil)
                    let reader = ConnectionByteReader(connection: connection)
                    let headerBytes = try await reader.readUntil(Data("\r\n\r\n".utf8))
                    let headers = try parseHTTPHeaders(headerBytes)
                    guard (200..<300).contains(headers.status) else {
                        let body = try await reader.readToEOF()
                        throw makeHTTPError(status: headers.status, body: body)
                    }
                    guard headers.headers["transfer-encoding"]?.lowercased().contains("chunked") == true else {
                        throw DaemonClientError.streamClosed(reason: "missing chunked transfer encoding")
                    }

                    var lines = NDJSONLineBuffer()
                    while !Task.isCancelled {
                        let lengthLine = try await reader.readUntil(Data("\r\n".utf8))
                        let hexText = String(data: lengthLine.dropLast(2), encoding: .utf8)?
                            .split(separator: ";", maxSplits: 1)
                            .first
                            .map(String.init)?
                            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                        guard let chunkLength = Int(hexText, radix: 16) else {
                            throw DaemonClientError.streamClosed(reason: "invalid chunk length")
                        }
                        if chunkLength == 0 {
                            _ = try? await reader.readUntil(Data("\r\n".utf8))
                            continuation.finish()
                            connection.cancel()
                            return
                        }

                        let chunk = try await reader.readExactly(chunkLength)
                        let crlf = try await reader.readExactly(2)
                        guard crlf == Data("\r\n".utf8) else {
                            throw DaemonClientError.streamClosed(reason: "invalid chunk terminator")
                        }
                        for line in try lines.feed(chunk) {
                            let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
                            guard !trimmed.isEmpty, let data = trimmed.data(using: .utf8) else { continue }
                            do {
                                let event = try JSONDecoder().decode(RecorderEventLine.self, from: data)
                                continuation.yield(event)
                            } catch {
                                throw DaemonClientError.decode(underlying: error, raw: trimmed)
                            }
                        }
                    }
                    connectionBox.cancel()
                    continuation.finish()
                } catch {
                    connectionBox.cancel()
                    continuation.finish(throwing: error)
                }
            }

            continuation.onTermination = { _ in
                task.cancel()
                connectionBox.cancel()
            }
        }
    }

    /// Like `subscribe(...)` but yields each NDJSON line's raw UTF-8 bytes
    /// instead of a decoded `RecorderEventLine`. SCR-178 (U8): the backfill
    /// progress events carry count fields `RecorderEventLine` does not model, so
    /// the affordance re-decodes the raw line through
    /// `backfillProgressEvent(fromLine:)`. A line that fails to parse is dropped
    /// (never throws) so an unrelated event shape can't kill the stream.
    static func subscribeRawLines(
        path: String = "/v0/events",
        sinceCursor: Int? = nil,
        socketPathOverride: String? = nil
    ) -> AsyncThrowingStream<Data, Error> {
        AsyncThrowingStream { continuation in
            let connectionBox = DaemonStreamConnectionBox()

            let task = Task {
                let queryPath: String
                if let sinceCursor {
                    queryPath = "\(path)?since=\(sinceCursor)"
                } else {
                    queryPath = path
                }

                let socket = socketPathOverride ?? socketPath()
                guard FileManager.default.fileExists(atPath: socket) else {
                    continuation.finish(throwing: DaemonClientError.socketUnavailable(path: socket))
                    return
                }

                let connection = NWConnection(to: .unix(path: socket), using: .tcp)
                connectionBox.set(connection)
                do {
                    try await connect(connection)
                    try await sendRequest(connection: connection, method: "GET", path: queryPath, body: nil)
                    let reader = ConnectionByteReader(connection: connection)
                    let headerBytes = try await reader.readUntil(Data("\r\n\r\n".utf8))
                    let headers = try parseHTTPHeaders(headerBytes)
                    guard (200..<300).contains(headers.status) else {
                        let body = try await reader.readToEOF()
                        throw makeHTTPError(status: headers.status, body: body)
                    }
                    guard headers.headers["transfer-encoding"]?.lowercased().contains("chunked") == true else {
                        throw DaemonClientError.streamClosed(reason: "missing chunked transfer encoding")
                    }

                    var lines = NDJSONLineBuffer()
                    while !Task.isCancelled {
                        let lengthLine = try await reader.readUntil(Data("\r\n".utf8))
                        let hexText = String(data: lengthLine.dropLast(2), encoding: .utf8)?
                            .split(separator: ";", maxSplits: 1)
                            .first
                            .map(String.init)?
                            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                        guard let chunkLength = Int(hexText, radix: 16) else {
                            throw DaemonClientError.streamClosed(reason: "invalid chunk length")
                        }
                        if chunkLength == 0 {
                            _ = try? await reader.readUntil(Data("\r\n".utf8))
                            continuation.finish()
                            connection.cancel()
                            return
                        }

                        let chunk = try await reader.readExactly(chunkLength)
                        let crlf = try await reader.readExactly(2)
                        guard crlf == Data("\r\n".utf8) else {
                            throw DaemonClientError.streamClosed(reason: "invalid chunk terminator")
                        }
                        for line in try lines.feed(chunk) {
                            let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
                            guard !trimmed.isEmpty, let data = trimmed.data(using: .utf8) else { continue }
                            continuation.yield(data)
                        }
                    }
                    connectionBox.cancel()
                    continuation.finish()
                } catch {
                    connectionBox.cancel()
                    continuation.finish(throwing: error)
                }
            }

            continuation.onTermination = { _ in
                task.cancel()
                connectionBox.cancel()
            }
        }
    }

    static func daemonInfo() async throws -> DaemonInfoResponse {
        try await request(method: "GET", path: "/v0/daemon.info")
    }

    static func recordingList() async throws -> ListResponse {
        try await request(method: "GET", path: "/v0/recording.list")
    }

    static func sessionSnapshot() async throws -> SessionSnapshotResponse {
        try await request(method: "GET", path: "/v0/session.snapshot")
    }

    // MARK: - SCR-258 storage lifecycle verbs

    /// Seal the store (SCR-258 U9). The daemon runs the bounded
    /// stop→quiesce→detach→seal chain, which can take up to ~30s for an active
    /// recording's chunk to finalize plus the quiesce grace — so the client budget
    /// exceeds that ceiling (mirrors `recordingStop`). On a failed detach the
    /// daemon returns a typed `store_lock_failed`/`detach_failed` envelope
    /// (`.envelopeError`) with the store left mounted+unlocked.
    static func storageLock() async throws -> StorageStateResponse {
        try await request(
            method: "POST", path: "/v0/storage.lock", body: Data("{}".utf8), timeout: 60
        )
    }

    /// Unseal the store (SCR-258 U9). The SURFACE performs present-user auth first
    /// (the app's store-scoped `PresenceGate`); this verb trusts its same-EUID
    /// caller (KTD-16). A locked Keychain surfaces as a retryable
    /// `.envelopeError(code: "keychain_locked", ...)` with the sentinel intact.
    static func storageUnlock() async throws -> StorageStateResponse {
        try await request(
            method: "POST", path: "/v0/storage.unlock", body: Data("{}".utf8), timeout: 30
        )
    }

    /// Begin (or resume) the plaintext→container upgrade migration (SCR-258 U6).
    /// Idempotent daemon-side; refuses a custom-recordings install / plaintext-only
    /// build with a typed `.envelopeError`.
    static func storageEncryptStart() async throws -> StorageEncryptResponse {
        try await request(
            method: "POST", path: "/v0/storage.encrypt.start", body: Data("{}".utf8), timeout: 30
        )
    }

    /// Privacy-safe migration snapshot (SCR-258 U6). Read-only; counts + state +
    /// phase + a non-identifying pause reason.
    static func storageEncryptStatus() async throws -> StorageEncryptResponse {
        try await request(method: "GET", path: "/v0/storage.encrypt.status")
    }

    /// Signal the in-flight migration to stop (resumable; plaintext stays intact).
    static func storageEncryptCancel() async throws -> StorageEncryptResponse {
        try await request(
            method: "POST", path: "/v0/storage.encrypt.cancel", body: Data("{}".utf8), timeout: 15
        )
    }

    static func recordingStart(_ req: RecordingStartRequest) async throws -> RecordingStartResponse {
        let body = try JSONEncoder().encode(req)
        return try await request(method: "POST", path: "/v0/recording.start", body: body)
    }

    static func recordingStop(_ req: RecordingStopRequest) async throws -> RecordingStopResponse {
        let body = try JSONEncoder().encode(req)
        // The daemon's `supervisor.stop()` awaits `recording_finalized` for up
        // to `SCREENCAP_DAEMON_STOP_TIMEOUT` (default 30 s) before responding.
        // Use a budget that exceeds that ceiling so the HTTP client doesn't
        // surface "request timed out" while the daemon is still finalizing
        // cleanly. 35 s gives 5 s of headroom for the response round-trip.
        return try await request(
            method: "POST",
            path: "/v0/recording.stop",
            body: body,
            timeout: 35
        )
    }

    /// Set the mic-mute state on the running recording (SCR-254 U7). Forwards to
    /// the engine over the daemon's stdin control channel; the response echoes the
    /// REQUESTED state (not confirmation — see `RecordingMuteResponse.muted`) plus
    /// the pre-forward bus cursor. Uses the default 10 s budget: the daemon only
    /// forwards one control line (it does not await the engine's stop), so this is
    /// a fast round-trip unlike `recordingStop`.
    static func recordingMute(_ req: RecordingMuteRequest) async throws -> RecordingMuteResponse {
        let body = try JSONEncoder().encode(req)
        return try await request(method: "POST", path: "/v0/recording.mute", body: body)
    }

    /// On-demand daemon-driven TCC registration (U8). The daemon runs the
    /// matching request mechanism in *its own* process so the Settings entry is
    /// attributed to the daemon identity, not the app. The app awaits this ack
    /// before opening the matching pane (so the user never lands on a pane with
    /// no helper row — the AE2 failure mode).
    static func permissionRequest(_ permission: String) async throws -> PermissionRequestResponse {
        let body = try JSONEncoder().encode(PermissionRequestRequest(permission: permission))
        // Daemon-side registration runs the TCC request mechanism in its own
        // process (`CGEventTapCreate` / `CGRequestScreenCaptureAccess`), which
        // can block on TCC syscalls. Pass the timeout explicitly — matching
        // `recordingStart`'s 10 s budget — rather than relying on the implicit
        // default, so the budget for this blocking call is visible at the call
        // site. (The daemon also bounds its own response at 8 s.)
        return try await request(
            method: "POST",
            path: "/v0/permission.request",
            body: body,
            timeout: 10
        )
    }

    /// SCR-200 (U4): trigger the daemon's identity-scoped decoy/orphan TCC
    /// cleanup so exactly one "ScreenCap" row remains per pane (R5/R6/R8). The
    /// hardened `tccutil` allowlist lives daemon-side (`tcc_cleanup`), so the app
    /// never carries a second copy of the destructive reset logic. Best-effort:
    /// the daemon swallows individual `tccutil` failures and acks once the sweep
    /// has run. No request body — the verb resets a fixed, allowlisted set.
    static func cleanupDecoys() async throws -> CleanupDecoysResponse {
        return try await request(
            method: "POST",
            path: "/v0/permission.cleanup_decoys",
            timeout: 10
        )
    }

    // MARK: - SCR-174 search verbs

    /// On-screen-text search (SCR-118). Daemon-only, local, pointer-only. On
    /// `socketUnavailable`/`connectionFailed` the caller surfaces a "daemon not
    /// running" state — there is no CLI fallback for these verbs.
    static func contentSearch(_ req: ContentSearchRequest) async throws -> ContentSearchResponse {
        let body = try JSONEncoder().encode(req)
        return try await request(method: "POST", path: "/v0/content.search", body: body)
    }

    /// Set or clear a recording's editable display title (editable titles U6). A
    /// post-hoc, additive mutating verb over the LOCAL-only `recording.title`
    /// (never uploaded — R8). An empty `title` clears the rename back to the
    /// derived default. The daemon refuses a rename of the currently-active
    /// recording with a `.envelopeError(code: "recording_active", ...)` (HTTP
    /// 409) — the caller disables the affordance mid-recording, so that path is
    /// only a belt-and-suspenders guard.
    static func recordingRename(recording: String, title: String) async throws -> RecordingRenameResponse {
        let body = try JSONEncoder().encode(RecordingRenameRequest(recordingId: recording, title: title))
        return try await request(method: "POST", path: "/v0/recording.rename", body: body)
    }

    /// Transcript keyword search (SCR-118). Hits are chunk-granular (no
    /// `timestamp_ms`).
    static func transcriptSearch(_ req: TranscriptSearchRequest) async throws -> TranscriptSearchResponse {
        let body = try JSONEncoder().encode(req)
        return try await request(method: "POST", path: "/v0/transcript.search", body: body)
    }

    /// Structured timeline query (SCR-118, authoritative). Returns a typed
    /// `invalid_range` envelope error (HTTP 400) when `start_ms > end_ms`,
    /// surfaced here as `.envelopeError(code: "invalid_range", ...)`.
    static func timelineQuery(_ req: TimelineQueryRequest) async throws -> TimelineQueryResponse {
        let body = try JSONEncoder().encode(req)
        return try await request(method: "POST", path: "/v0/timeline.query", body: body)
    }

    /// Day-scoped spans + honest blocked-interval split for the Day timeline
    /// (U3/U9). Read-only; a malformed date surfaces as a typed 400 envelope
    /// error, and `socketUnavailable`/`connectionFailed` puts the day view in
    /// its daemon-unavailable state (no CLI fallback for this verb).
    static func timelineDay(_ req: TimelineDayRequest) async throws -> TimelineDayResponse {
        let body = try JSONEncoder().encode(req)
        return try await request(method: "POST", path: "/v0/timeline.day", body: body)
    }

    /// A LOCAL recording's named task segments (U10, local-first intelligence).
    /// Read-only, pointer/data-only over the recording's local-only tasks store
    /// (`pipeline_task_segments` in `recording.db`, never uploaded — R4/R8). A
    /// recording with no tasks store returns an empty `tasks` list (not an
    /// error). On `socketUnavailable`/`connectionFailed` the caller degrades
    /// silently — the task breakdown is simply omitted (nullable end to end),
    /// never an error state. No CLI fallback for this verb.
    static func tasksList(_ req: TasksListRequest) async throws -> TasksListResponse {
        let body = try JSONEncoder().encode(req)
        return try await request(method: "POST", path: "/v0/tasks.list", body: body)
    }

    /// Conversational-recall answer (conversational-recall U5). Daemon-only,
    /// local, POINTER-ONLY response (R2/R8 — sources carry `(recording,
    /// timestamp_ms, stream)`, never image bytes). Prior-turn context is carried
    /// as source pointers only (KTD6); the daemon re-derives snippet text
    /// server-side, so raw captured text never round-trips through the client.
    ///
    /// Fail-safe by contract: the verb degrades a downstream miss to a graceful
    /// refusal envelope (200 with `refusal=true`), never a 500 — so the only
    /// errors surfaced here are a malformed request (typed 4xx envelope error) or
    /// the daemon being unreachable (`socketUnavailable`/`connectionFailed`). On
    /// those the caller surfaces a "daemon not running" state; there is no CLI
    /// fallback for this verb.
    static func chatAnswer(_ req: ChatAnswerRequest) async throws -> ChatAnswerResponse {
        let body = try JSONEncoder().encode(req)
        return try await request(method: "POST", path: "/v0/chat.answer", body: body)
    }

    /// Query-parser vocabulary (SCR-179). Read-only GET, no parameters. Returns
    /// distinct app names / bundle ids + bare `browser_url` hostnames — never a
    /// full URL. A same-EUID-only browsing-profile artifact (R6): the caller must
    /// not persist, sync, or log it. On `socketUnavailable`/`connectionFailed`
    /// (older daemon without the route → 404) the caller falls back to the static
    /// parser vocabulary.
    static func appsList() async throws -> AppsListResponse {
        try await request(method: "GET", path: "/v0/apps.list")
    }

    // MARK: - SCR-178 content-index backfill verbs

    /// Start (or resume) the content-index backfill (SCR-178). Idempotent on the
    /// daemon side — a second `start` while a run is in flight returns the
    /// existing job's status rather than spawning a second run. The request
    /// carries no parameters (the backfill enumerates the local library itself),
    /// so the body is an empty JSON object. Pointer-/count-only response — no
    /// recording directory name (R9). On `socketUnavailable`/`connectionFailed`
    /// the caller surfaces a "daemon not running" state; on an error envelope it
    /// surfaces the existing `DaemonClientError.envelopeError`.
    static func backfillStart() async throws -> BackfillStatusResponse {
        try await request(method: "POST", path: "/v0/backfill.start", body: Data("{}".utf8))
    }

    /// Current privacy-safe backfill snapshot (SCR-178). Count-only + opaque
    /// ordinal; no recording name (R9). Read-only — not a recording-mutating
    /// verb (the daemon excludes it from `_ACTIVITY_PATHS`).
    static func backfillStatus() async throws -> BackfillStatusResponse {
        try await request(method: "GET", path: "/v0/backfill.status")
    }

    /// Signal the in-flight backfill to stop (SCR-178). The run flushes its
    /// ledger and transitions to `cancelled` (a cancelled run does NOT
    /// auto-resume on daemon restart — re-trigger is an explicit `backfillStart`).
    static func backfillCancel() async throws -> BackfillStatusResponse {
        try await request(method: "POST", path: "/v0/backfill.cancel", body: Data("{}".utf8))
    }

    /// Decode a single streamed event line into a `BackfillProgressEvent`,
    /// returning nil for any non-`backfill.*` event or a payload that fails to
    /// decode. The `subscribe(...)` stream yields `RecorderEventLine` (a flat
    /// recorder-event shape that does not carry the backfill counts), so the UI
    /// re-decodes the raw line bytes through this seam: feed each streamed line
    /// here and act only on the non-nil results. Tolerant by design — an
    /// unknown or malformed event is ignored, never fatal (U7 edge case).
    static func backfillProgressEvent(fromLine line: Data) -> BackfillProgressEvent? {
        guard let event = try? JSONDecoder().decode(BackfillProgressEvent.self, from: line),
              event.type.hasPrefix("backfill.")
        else {
            return nil
        }
        return event
    }

    /// Convenience overload: decode a backfill progress event from a streamed
    /// line's UTF-8 string form. Mirrors `backfillProgressEvent(fromLine:)`.
    static func backfillProgressEvent(fromLine line: String) -> BackfillProgressEvent? {
        guard let data = line.data(using: .utf8) else { return nil }
        return backfillProgressEvent(fromLine: data)
    }

    private static func connect(_ connection: NWConnection) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            let lock = NSLock()
            var resumed = false
            let resume: (Result<Void, Error>) -> Void = { result in
                lock.lock()
                if resumed {
                    lock.unlock()
                    return
                }
                resumed = true
                connection.stateUpdateHandler = nil
                lock.unlock()
                switch result {
                case .success:
                    continuation.resume()
                case .failure(let error):
                    continuation.resume(throwing: error)
                }
            }
            connection.stateUpdateHandler = { state in
                switch state {
                case .ready:
                    resume(.success(()))
                case .failed(let error):
                    resume(.failure(DaemonClientError.connectionFailed(underlying: error)))
                case .cancelled:
                    resume(.failure(DaemonClientError.streamClosed(reason: "connection cancelled")))
                default:
                    break
                }
            }
            connection.start(queue: DispatchQueue.global(qos: .userInitiated))
        }
    }

    private static func sendRequest(
        connection: NWConnection,
        method: String,
        path: String,
        body: Data?
    ) async throws {
        var request = "\(method) \(path) HTTP/1.1\r\n"
        request += "Host: localhost\r\n"
        request += "Connection: close\r\n"
        if let body {
            request += "Content-Type: application/json\r\n"
            request += "Content-Length: \(body.count)\r\n"
        }
        request += "\r\n"
        var bytes = Data(request.utf8)
        if let body { bytes.append(body) }

        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            connection.send(content: bytes, completion: .contentProcessed { error in
                if let error {
                    continuation.resume(throwing: DaemonClientError.connectionFailed(underlying: error))
                } else {
                    continuation.resume()
                }
            })
        }
    }

    private static func readAll(connection: NWConnection) async throws -> Data {
        var bytes = Data()
        while true {
            guard let chunk = try await receive(connection: connection) else { return bytes }
            bytes.append(chunk)
        }
    }

    private static func receive(connection: NWConnection) async throws -> Data? {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Data?, Error>) in
            connection.receive(minimumIncompleteLength: 1, maximumLength: 64 * 1024) { data, _, isComplete, error in
                if let error {
                    continuation.resume(throwing: DaemonClientError.connectionFailed(underlying: error))
                    return
                }
                if let data, !data.isEmpty {
                    continuation.resume(returning: data)
                    return
                }
                if isComplete {
                    continuation.resume(returning: nil)
                } else {
                    continuation.resume(returning: Data())
                }
            }
        }
    }

    private static func parseHTTPResponse(_ data: Data) throws -> HTTPResponse {
        guard let separator = data.range(of: Data("\r\n\r\n".utf8)) else {
            throw DaemonClientError.streamClosed(reason: "missing HTTP response headers")
        }
        let headerBytes = data[..<separator.upperBound]
        let headers = try parseHTTPHeaders(Data(headerBytes))
        let body = data.subdata(in: separator.upperBound..<data.endIndex)
        return HTTPResponse(status: headers.status, headers: headers.headers, body: body)
    }

    private static func parseHTTPHeaders(_ data: Data) throws -> HTTPHeaders {
        guard let text = String(data: data, encoding: .utf8) else {
            throw DaemonClientError.streamClosed(reason: "invalid HTTP header encoding")
        }
        let normalized = text.hasSuffix("\r\n\r\n") ? String(text.dropLast(4)) : String(text.dropLast(2))
        let lines = normalized.components(separatedBy: "\r\n")
        guard let statusLine = lines.first else {
            throw DaemonClientError.streamClosed(reason: "missing HTTP status line")
        }
        let parts = statusLine.split(separator: " ", maxSplits: 2)
        guard parts.count >= 2, let status = Int(parts[1]) else {
            throw DaemonClientError.streamClosed(reason: "invalid HTTP status line")
        }
        var headers: [String: String] = [:]
        for line in lines.dropFirst() {
            guard let colon = line.firstIndex(of: ":") else { continue }
            let key = String(line[..<colon]).trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            let value = String(line[line.index(after: colon)...]).trimmingCharacters(in: .whitespacesAndNewlines)
            headers[key] = value
        }
        return HTTPHeaders(status: status, headers: headers)
    }

    private static func decodeResponse<T: Decodable>(_ type: T.Type, response: HTTPResponse) throws -> T {
        if !(200..<300).contains(response.status) {
            throw makeHTTPError(status: response.status, body: response.body)
        }

        do {
            let probe = try JSONDecoder().decode(APIEnvelopeProbe.self, from: response.body)
            if let got = probe.apiSchemaVersion, got != SUPPORTED_API_SCHEMA_VERSION {
                daemonLogger.warning("Daemon API schema mismatch. Expected \(SUPPORTED_API_SCHEMA_VERSION, privacy: .public), got \(got, privacy: .public).")
                throw DaemonClientError.schemaMismatch(expected: SUPPORTED_API_SCHEMA_VERSION, got: got)
            }
            if probe.ok == false {
                throw try envelopeError(from: response.body, fallbackStatus: response.status)
            }
            return try JSONDecoder().decode(type, from: response.body)
        } catch let error as DaemonClientError {
            throw error
        } catch {
            let raw = String(data: response.body, encoding: .utf8) ?? "<binary>"
            throw DaemonClientError.decode(underlying: error, raw: raw)
        }
    }

    private static func makeHTTPError(status: Int, body: Data) -> DaemonClientError {
        if let error = try? envelopeError(from: body, fallbackStatus: status) {
            return error
        }
        let raw = String(data: body, encoding: .utf8) ?? "<binary>"
        return .httpError(status: status, body: raw)
    }

    private static func envelopeError(from body: Data, fallbackStatus: Int) throws -> DaemonClientError {
        let object = try JSONSerialization.jsonObject(with: body)
        guard let payload = object as? [String: Any],
              let code = payload["error"] as? String
        else {
            let raw = String(data: body, encoding: .utf8) ?? "<binary>"
            return .httpError(status: fallbackStatus, body: raw)
        }
        // Carry the raw body forward; callers reach for typed fields by
        // decoding off `rawBody` when they need them.
        return .envelopeError(code: code, rawBody: body)
    }

    private static func withTimeout<T>(
        seconds: TimeInterval,
        connection: NWConnection,
        operation: @escaping @Sendable () async throws -> T
    ) async throws -> T {
        try await withThrowingTaskGroup(of: T.self) { group in
            group.addTask {
                try await operation()
            }
            group.addTask {
                try await Task.sleep(nanoseconds: UInt64(max(0, seconds) * 1_000_000_000))
                throw DaemonClientError.timedOut(seconds: seconds)
            }
            do {
                let result = try await group.next()!
                group.cancelAll()
                return result
            } catch {
                group.cancelAll()
                connection.cancel()
                throw error
            }
        }
    }
}

private struct HTTPResponse {
    let status: Int
    let headers: [String: String]
    let body: Data
}

private struct HTTPHeaders {
    let status: Int
    let headers: [String: String]
}

private final class ConnectionByteReader {
    private let connection: NWConnection
    private var buffer = Data()

    init(connection: NWConnection) {
        self.connection = connection
    }

    func readUntil(_ separator: Data) async throws -> Data {
        while true {
            if let range = buffer.range(of: separator) {
                let end = range.upperBound
                let result = buffer.subdata(in: buffer.startIndex..<end)
                buffer.removeSubrange(buffer.startIndex..<end)
                return result
            }
            guard let chunk = try await DaemonClient.receiveForReader(connection: connection) else {
                throw DaemonClientError.streamClosed(reason: "unexpected EOF")
            }
            buffer.append(chunk)
        }
    }

    func readExactly(_ count: Int) async throws -> Data {
        while buffer.count < count {
            guard let chunk = try await DaemonClient.receiveForReader(connection: connection) else {
                throw DaemonClientError.streamClosed(reason: "unexpected EOF")
            }
            buffer.append(chunk)
        }
        let result = buffer.subdata(in: buffer.startIndex..<(buffer.startIndex + count))
        buffer.removeSubrange(buffer.startIndex..<(buffer.startIndex + count))
        return result
    }

    func readToEOF() async throws -> Data {
        var result = buffer
        buffer.removeAll()
        while let chunk = try await DaemonClient.receiveForReader(connection: connection) {
            result.append(chunk)
        }
        return result
    }
}

extension DaemonClient {
    fileprivate static func receiveForReader(connection: NWConnection) async throws -> Data? {
        try await receive(connection: connection)
    }
}

private struct NDJSONLineBuffer {
    // Cap a single un-terminated line at 1 MiB so a malformed (or hostile)
    // peer cannot grow `pending` without bound. Daemon NDJSON events are
    // tens of bytes; 1 MiB is several orders of magnitude above any
    // legitimate frame.
    static let maxLineBytes = 1_048_576

    private var pending = Data()

    mutating func feed(_ data: Data) throws -> [String] {
        pending.append(data)
        var lines: [String] = []
        while let nl = pending.firstIndex(of: 0x0A) {
            let line = pending.subdata(in: pending.startIndex..<nl)
            pending.removeSubrange(pending.startIndex...nl)
            if let s = String(data: line, encoding: .utf8) {
                lines.append(s)
            }
        }
        if pending.count > Self.maxLineBytes {
            throw DaemonClientError.streamClosed(reason: "line too long")
        }
        return lines
    }
}
