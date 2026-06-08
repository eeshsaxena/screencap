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
        case accessibility
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

    /// All three required daemon grants are confirmed granted (microphone is
    /// not a daemon-required permission and is excluded). Indeterminate is NOT
    /// granted — this is the positive "everything's good" predicate that drives
    /// auto-close of the walkthrough.
    var allRequiredGranted: Bool {
        screenRecording == .granted
            && accessibility == .granted
            && inputMonitoring == .granted
    }

    /// Screen Recording specifically reports denied — the one permission fatal
    /// to capture, so the recording-start block keys on it (U4/U6 decision:
    /// hard-block start on Screen Recording only; advisory for the other two).
    var screenRecordingDenied: Bool {
        screenRecording == .denied
    }

    /// Any required grant reports denied — drives whether the walkthrough is
    /// surfaced (R3).
    var anyRequiredDenied: Bool {
        screenRecording == .denied
            || accessibility == .denied
            || inputMonitoring == .denied
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

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
        case build
        case startedAt = "started_at"
        case permissions
    }
}

struct ListResponse: Decodable {
    let ok: Bool
    let schemaVersion: Int
    let daemonVersion: String
    let apiSchemaVersion: Int
    let recordings: [RecordingSummary]

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
        case recordings
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
    }
}

struct RecordingStartRequest: Encodable {
    let name: String?
    let startedBy: String?

    init(name: String? = nil, startedBy: String? = nil) {
        self.name = name
        self.startedBy = startedBy
    }

    enum CodingKeys: String, CodingKey {
        case name
        case startedBy = "started_by"
    }
}

struct RecordingStartResponse: Decodable {
    let ok: Bool
    let schemaVersion: Int
    let daemonVersion: String
    let apiSchemaVersion: Int
    let sessionID: String
    let startedAt: Double
    let enginePID: Int
    /// Bus cursor captured BEFORE the engine spawn — feed into
    /// `/v0/events?since=<cursor>` to receive the `started` event without
    /// an extra `session.snapshot` round-trip.
    let cursor: Int

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
        case sessionID = "session_id"
        case startedAt = "started_at"
        case enginePID = "engine_pid"
        case cursor
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
        return "\(NSHomeDirectory())/.screencap/run/api.sock"
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

    static func daemonInfo() async throws -> DaemonInfoResponse {
        try await request(method: "GET", path: "/v0/daemon.info")
    }

    static func recordingList() async throws -> ListResponse {
        try await request(method: "GET", path: "/v0/recording.list")
    }

    static func sessionSnapshot() async throws -> SessionSnapshotResponse {
        try await request(method: "GET", path: "/v0/session.snapshot")
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

    /// On-demand daemon-driven TCC registration (U8). The daemon runs the
    /// matching request mechanism in *its own* process so the Settings entry is
    /// attributed to the daemon identity, not the app. The app awaits this ack
    /// before opening the matching pane (so the user never lands on a pane with
    /// no helper row — the AE2 failure mode).
    static func permissionRequest(_ permission: String) async throws -> PermissionRequestResponse {
        let body = try JSONEncoder().encode(PermissionRequestRequest(permission: permission))
        return try await request(method: "POST", path: "/v0/permission.request", body: body)
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
