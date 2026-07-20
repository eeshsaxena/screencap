import Darwin
import Foundation
import Network
import XCTest
@testable import Screencap

final class DaemonClientTests: XCTestCase {
    private var socketPath: String!
    private var server: UnixHTTPTestServer?

    override func setUp() {
        super.setUp()
        socketPath = "/tmp/sc-dc-\(UUID().uuidString.prefix(8)).sock"
        setenv("SCREENCAP_DAEMON_SOCKET", socketPath, 1)
    }

    override func tearDown() {
        server?.stop()
        server = nil
        if let socketPath {
            unlink(socketPath)
        }
        unsetenv("SCREENCAP_DAEMON_SOCKET")
        super.tearDown()
    }

    func testSupportedAPISchemaVersionIsPinnedToOne() {
        XCTAssertEqual(SUPPORTED_API_SCHEMA_VERSION, 1)
    }

    func testSocketUnavailableThrowsBeforeDialing() async {
        do {
            let _: DaemonInfoResponse = try await DaemonClient.daemonInfo()
            XCTFail("Expected socketUnavailable")
        } catch DaemonClientError.socketUnavailable(let path) {
            XCTAssertEqual(path, socketPath)
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    func testRequestFramesGETAndDecodesEnvelopeWithUnknownFields() async throws {
        let server = try startServer { request in
            XCTAssertEqual(request.method, "GET")
            XCTAssertEqual(request.path, "/v0/daemon.info")
            XCTAssertEqual(request.headers["connection"], "close")
            return .json(
                #"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"build":null,"started_at":123.5,"future_field":"ignored"}"#
            )
        }

        let response = try await DaemonClient.daemonInfo()

        XCTAssertEqual(server.requestCount, 1)
        XCTAssertEqual(response.ok, true)
        XCTAssertEqual(response.daemonVersion, "test")
        XCTAssertEqual(response.apiSchemaVersion, 1)
        XCTAssertEqual(response.startedAt, 123.5)
        // Older daemon (no permissions block) decodes to nil — no decode failure.
        XCTAssertNil(response.permissions)
    }

    // MARK: - U3: daemon grant-state decode

    func testDaemonInfoDecodesAllGrantedPermissions() async throws {
        _ = try startServer { _ in
            .json(
                #"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"build":null,"started_at":1.0,"permissions":{"screen_recording":"granted","accessibility":"granted","input_monitoring":"granted"}}"#
            )
        }

        let response = try await DaemonClient.daemonInfo()
        let grants = try XCTUnwrap(response.permissions)
        XCTAssertEqual(grants.screenRecording, .granted)
        XCTAssertEqual(grants.accessibility, .granted)
        XCTAssertEqual(grants.inputMonitoring, .granted)
        XCTAssertTrue(grants.allRequiredGranted)
        XCTAssertFalse(grants.anyRequiredDenied)
    }

    func testDaemonInfoDecodesMixedPermissionGrants() async throws {
        _ = try startServer { _ in
            .json(
                #"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"build":null,"started_at":1.0,"permissions":{"screen_recording":"denied","accessibility":"granted","input_monitoring":"indeterminate"}}"#
            )
        }

        let response = try await DaemonClient.daemonInfo()
        let grants = try XCTUnwrap(response.permissions)
        XCTAssertEqual(grants.screenRecording, .denied)
        XCTAssertEqual(grants.accessibility, .granted)
        XCTAssertEqual(grants.inputMonitoring, .indeterminate)
        XCTAssertFalse(grants.allRequiredGranted)
        XCTAssertTrue(grants.anyRequiredDenied)
        XCTAssertTrue(grants.screenRecordingDenied)
    }

    func testInputMonitoringDeniedDoesNotBlockRequiredGrants() async throws {
        // SCR-196 follow-up: Input Monitoring can't be registered for the daemon
        // helper on macOS 26.x, so it must NOT count toward the required set.
        // With Screen Recording + Accessibility granted and IM denied, onboarding
        // is complete: allRequiredGranted is true and anyRequiredDenied is false.
        // (Before the fix, IM in the required set made both flip — stranding the
        // user on a grant they could never give.)
        _ = try startServer { _ in
            .json(
                #"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"build":null,"started_at":1.0,"permissions":{"screen_recording":"granted","accessibility":"granted","input_monitoring":"denied"}}"#
            )
        }

        let response = try await DaemonClient.daemonInfo()
        let grants = try XCTUnwrap(response.permissions)
        XCTAssertEqual(grants.inputMonitoring, .denied)
        XCTAssertTrue(grants.allRequiredGranted)
        XCTAssertFalse(grants.anyRequiredDenied)
        XCTAssertFalse(grants.screenRecordingDenied)
    }

    func testDaemonInfoPartialBlockDecodesMissingKeysToIndeterminate() async throws {
        // A block with only screen_recording present — absent sub-keys must
        // decode to indeterminate, never silently granted/denied.
        _ = try startServer { _ in
            .json(
                #"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"build":null,"started_at":1.0,"permissions":{"screen_recording":"granted"}}"#
            )
        }

        let response = try await DaemonClient.daemonInfo()
        let grants = try XCTUnwrap(response.permissions)
        XCTAssertEqual(grants.screenRecording, .granted)
        XCTAssertEqual(grants.accessibility, .indeterminate)
        XCTAssertEqual(grants.inputMonitoring, .indeterminate)
    }

    func testDaemonInfoUnknownGrantTokenAndExtraKeyAreTolerant() async throws {
        // An unrecognized token maps to indeterminate (never denied); an unknown
        // extra grant key is ignored (forward-compat).
        _ = try startServer { _ in
            .json(
                #"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"build":null,"started_at":1.0,"permissions":{"screen_recording":"sometimes","accessibility":"granted","input_monitoring":"denied","future_perm":"granted"}}"#
            )
        }

        let response = try await DaemonClient.daemonInfo()
        let grants = try XCTUnwrap(response.permissions)
        XCTAssertEqual(grants.screenRecording, .indeterminate)
        XCTAssertEqual(grants.accessibility, .granted)
        XCTAssertEqual(grants.inputMonitoring, .denied)
    }

    func testProbeSurfacesGrantStateOnDaemonOutcome() async throws {
        _ = try startServer { _ in
            .json(
                #"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"build":null,"started_at":1.0,"permissions":{"screen_recording":"denied","accessibility":"granted","input_monitoring":"granted"}}"#
            )
        }

        let outcome = await LiveDaemonSessionService().probe()
        guard case .daemon(let grants) = outcome else {
            return XCTFail("Expected .daemon outcome, got \(outcome)")
        }
        XCTAssertEqual(grants.screenRecording, .denied)
        XCTAssertEqual(grants.accessibility, .granted)
        XCTAssertEqual(grants.inputMonitoring, .granted)
    }

    func testProbeMapsMissingPermissionsBlockToAllIndeterminate() async throws {
        // Older daemon: no permissions block → probe surfaces all-indeterminate
        // so nothing blocks or nags.
        _ = try startServer { _ in
            .json(
                #"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"build":null,"started_at":1.0}"#
            )
        }

        let outcome = await LiveDaemonSessionService().probe()
        XCTAssertEqual(outcome, .daemon(grants: .allIndeterminate))
    }

    func testProbeMapsPartialPermissionsBlockToIndeterminateAtOutcomeLevel() async throws {
        // A partial block (only screen_recording present) surfaced through
        // probe(): absent sub-keys must reach the .daemon grants as indeterminate,
        // never silently granted/denied. testDaemonInfoPartialBlock... pins the
        // raw decode; this pins the DaemonSessionService.probe() outcome a
        // partial block produces.
        _ = try startServer { _ in
            .json(
                #"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"build":null,"started_at":1.0,"permissions":{"screen_recording":"denied"}}"#
            )
        }

        let outcome = await LiveDaemonSessionService().probe()
        guard case .daemon(let grants) = outcome else {
            return XCTFail("Expected .daemon outcome, got \(outcome)")
        }
        XCTAssertEqual(grants.screenRecording, .denied)
        XCTAssertEqual(grants.accessibility, .indeterminate)
        XCTAssertEqual(grants.inputMonitoring, .indeterminate)
    }

    func testRequestFramesPOSTBodyAndDecodesResponse() async throws {
        _ = try startServer { request in
            XCTAssertEqual(request.method, "POST")
            XCTAssertEqual(request.path, "/v0/recording.start")
            XCTAssertEqual(request.headers["content-type"], "application/json")
            let body = String(data: request.body, encoding: .utf8) ?? ""
            XCTAssertTrue(body.contains(#""name":"demo""#), body)
            XCTAssertTrue(body.contains(#""started_by":"swiftui-via-daemon""#), body)
            return .json(
                #"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"session_id":"abc","started_at":44.0,"engine_pid":999,"cursor":7}"#
            )
        }

        let response = try await DaemonClient.recordingStart(
            RecordingStartRequest(name: "demo", startedBy: "swiftui-via-daemon")
        )

        XCTAssertEqual(response.sessionID, "abc")
        XCTAssertEqual(response.startedAt, 44.0)
        XCTAssertEqual(response.enginePID, 999)
        XCTAssertEqual(response.cursor, 7)
    }

    // MARK: - SCR-254: recording.mute verb

    /// The mute verb frames the absolute `muted` state as a POST body and decodes
    /// the echo + pre-forward cursor. The echo is a transport ack only — the app
    /// ignores it and waits for the confirming event (asserted at the controller
    /// level) — but the wire shape must still round-trip.
    func testRecordingMuteFramesPOSTBodyAndDecodesResponse() async throws {
        _ = try startServer { request in
            XCTAssertEqual(request.method, "POST")
            XCTAssertEqual(request.path, "/v0/recording.mute")
            XCTAssertEqual(request.headers["content-type"], "application/json")
            let body = String(data: request.body, encoding: .utf8) ?? ""
            XCTAssertTrue(body.contains(#""muted":true"#), body)
            return .json(
                #"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"muted":true,"cursor":12}"#
            )
        }

        let response = try await DaemonClient.recordingMute(RecordingMuteRequest(muted: true))

        XCTAssertTrue(response.muted)
        XCTAssertEqual(response.cursor, 12)
    }

    // MARK: - U8: permission.request registration verb

    func testPermissionRequestFramesPOSTBodyAndDecodesResponse() async throws {
        _ = try startServer { request in
            XCTAssertEqual(request.method, "POST")
            XCTAssertEqual(request.path, "/v0/permission.request")
            XCTAssertEqual(request.headers["content-type"], "application/json")
            let body = String(data: request.body, encoding: .utf8) ?? ""
            XCTAssertTrue(body.contains(#""permission":"input_monitoring""#), body)
            return .json(
                #"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"permission":"input_monitoring","already_granted":false}"#
            )
        }

        let response = try await DaemonClient.permissionRequest("input_monitoring")

        XCTAssertEqual(response.permission, "input_monitoring")
        XCTAssertFalse(response.alreadyGranted)
    }

    func testPermissionRequestInvalidPermissionPropagatesEnvelopeError() async throws {
        // An out-of-allowlist permission returns a typed invalid_permission 4xx;
        // the client surfaces it as an envelopeError carrying the code.
        _ = try startServer { _ in
            .json(
                #"{"ok":false,"schema_version":1,"daemon_version":"test","api_schema_version":1,"error":"invalid_permission","reason":"bad permission"}"#,
                status: 400,
                reason: "Bad Request"
            )
        }

        do {
            _ = try await DaemonClient.permissionRequest("microphone")
            XCTFail("Expected envelope error")
        } catch DaemonClientError.envelopeError(let code, _) {
            XCTAssertEqual(code, "invalid_permission")
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    func testSchemaMismatchThrowsPinnedError() async throws {
        _ = try startServer { _ in
            .json(
                #"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":99,"build":null,"started_at":1.0}"#
            )
        }

        do {
            let _: DaemonInfoResponse = try await DaemonClient.daemonInfo()
            XCTFail("Expected schema mismatch")
        } catch DaemonClientError.schemaMismatch(let expected, let got) {
            XCTAssertEqual(expected, 1)
            XCTAssertEqual(got, 99)
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    func testHTTPErrorEnvelopePropagatesDaemonErrorCode() async throws {
        _ = try startServer { _ in
            .json(
                #"{"ok":false,"schema_version":1,"daemon_version":"test","api_schema_version":1,"error":"lock_contended","owner":{"claimant":"cli"}}"#,
                status: 409,
                reason: "Conflict"
            )
        }

        do {
            let _: RecordingStartResponse = try await DaemonClient.recordingStart(
                RecordingStartRequest(name: "blocked", startedBy: "swiftui-via-daemon")
            )
            XCTFail("Expected envelope error")
        } catch DaemonClientError.envelopeError(let code, let rawBody) {
            XCTAssertEqual(code, "lock_contended")
            // Decode off the raw body — the error case carries bytes, not a
            // [String: Any] payload, to keep the enum Sendable.
            let payload = try XCTUnwrap(
                JSONSerialization.jsonObject(with: rawBody) as? [String: Any]
            )
            XCTAssertNotNil(payload["owner"])
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    func testSubscribeParsesChunkedNDJSONAndCloseFrame() async throws {
        _ = try startServer { request in
            XCTAssertEqual(request.method, "GET")
            XCTAssertEqual(request.path, "/v0/events?since=7")
            return .chunked([
                #"{"type":"subscribed","schema_version":1,"cursor":7,"ts":1.0}"# + "\n",
                #"{"type":"started","schema_version":1,"cursor":8,"ts":2.0}"# + "\n",
                #"{"type":"chunk_finalized","schema_version":1,"cursor":9,"ts":3.0}"# + "\n",
                #"{"type":"_close","reason":"shutdown","ts":4.0}"# + "\n",
            ])
        }

        var types: [String] = []
        var closeReason: String?
        for try await event in DaemonClient.subscribe(
            sinceCursor: 7,
            socketPathOverride: socketPath
        ) {
            types.append(event.type)
            if event.type == "_close" {
                closeReason = event.reason
            }
        }

        XCTAssertEqual(types, ["subscribed", "started", "chunk_finalized", "_close"])
        XCTAssertEqual(closeReason, "shutdown")
    }

    func testSubscribeThrowsWhenChunkedStreamDropsBeforeTerminator() async throws {
        _ = try startServer { _ in
            .chunked(
                [#"{"type":"subscribed","schema_version":1,"cursor":1}"# + "\n"],
                terminate: false
            )
        }

        var iterator = DaemonClient.subscribe(
            sinceCursor: nil,
            socketPathOverride: socketPath
        ).makeAsyncIterator()
        let first = try await iterator.next()
        XCTAssertEqual(first?.type, "subscribed")

        do {
            _ = try await iterator.next()
            XCTFail("Expected streamClosed")
        } catch DaemonClientError.streamClosed {
            // Expected.
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    func testRealDaemonInfoWhenPythonServeIsAvailable() async throws {
        socketPath = "/tmp/sc-real-\(UUID().uuidString.prefix(8)).sock"
        setenv("SCREENCAP_DAEMON_SOCKET", socketPath, 1)

        let repoRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .path

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ["python3", "-m", "screencap", "serve", "--socket", socketPath]
        var env = ProcessInfo.processInfo.environment
        env["PYTHONPATH"] = "\(repoRoot)/src"
        process.environment = env
        process.standardInput = FileHandle.nullDevice
        let stdout = Pipe()
        let stderr = Pipe()
        process.standardOutput = stdout
        process.standardError = stderr

        do {
            try process.run()
        } catch {
            throw XCTSkip("python3 -m screencap serve could not launch: \(error.localizedDescription)")
        }

        defer {
            if process.isRunning {
                process.terminate()
                process.waitUntilExit()
            }
            unlink(socketPath)
        }

        let deadline = Date().addingTimeInterval(5)
        while Date() < deadline, process.isRunning {
            if FileManager.default.fileExists(atPath: socketPath) {
                let response = try await DaemonClient.daemonInfo()
                XCTAssertEqual(response.apiSchemaVersion, 1)
                return
            }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }

        if process.isRunning {
            process.terminate()
            process.waitUntilExit()
        }
        let err = (try? stderr.fileHandleForReading.readToEnd()).flatMap {
            String(data: $0, encoding: .utf8)
        } ?? ""
        let out = (try? stdout.fileHandleForReading.readToEnd()).flatMap {
            String(data: $0, encoding: .utf8)
        } ?? ""
        throw XCTSkip("real daemon did not become reachable; stdout=\(out), stderr=\(err)")
    }

    @discardableResult
    private func startServer(
        _ handler: @escaping @Sendable (UnixHTTPTestServer.Request) -> UnixHTTPTestServer.Response
    ) throws -> UnixHTTPTestServer {
        let server = try UnixHTTPTestServer(socketPath: socketPath, handler: handler)
        self.server = server
        server.start()
        return server
    }
}

final class UnixHTTPTestServer: @unchecked Sendable {
    struct Request: Sendable {
        let method: String
        let path: String
        let headers: [String: String]
        let body: Data
    }

    struct Response: Sendable {
        let status: Int
        let reason: String
        let headers: [String: String]
        let body: Data
        let chunks: [Data]?
        let terminateChunked: Bool

        static func json(_ body: String, status: Int = 200, reason: String = "OK") -> Response {
            Response(
                status: status,
                reason: reason,
                headers: ["Content-Type": "application/json"],
                body: Data(body.utf8),
                chunks: nil,
                terminateChunked: true
            )
        }

        static func chunked(_ lines: [String], terminate: Bool = true) -> Response {
            Response(
                status: 200,
                reason: "OK",
                headers: ["Content-Type": "application/x-ndjson", "Transfer-Encoding": "chunked"],
                body: Data(),
                chunks: lines.map { Data($0.utf8) },
                terminateChunked: terminate
            )
        }
    }

    private let socketPath: String
    private let handler: @Sendable (Request) -> Response
    private let queue = DispatchQueue(label: "UnixHTTPTestServer", qos: .userInitiated)
    private var listenFD: Int32 = -1
    private let lock = NSLock()
    private var _requestCount = 0

    var requestCount: Int {
        lock.lock()
        defer { lock.unlock() }
        return _requestCount
    }

    init(socketPath: String, handler: @escaping @Sendable (Request) -> Response) throws {
        self.socketPath = socketPath
        self.handler = handler
        unlink(socketPath)
        listenFD = socket(AF_UNIX, SOCK_STREAM, 0)
        guard listenFD >= 0 else { throw POSIXError(.init(rawValue: errno) ?? .EIO) }

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = Array(socketPath.utf8CString)
        guard pathBytes.count <= MemoryLayout.size(ofValue: addr.sun_path) else {
            throw POSIXError(.ENAMETOOLONG)
        }
        pathBytes.withUnsafeBufferPointer { source in
            withUnsafeMutablePointer(to: &addr.sun_path.0) { destination in
                for offset in 0..<pathBytes.count {
                    destination.advanced(by: offset).pointee = source[offset]
                }
            }
        }

        let length = socklen_t(MemoryLayout<sa_family_t>.size + pathBytes.count)
        let rc = withUnsafePointer(to: &addr) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(listenFD, $0, length)
            }
        }
        guard rc == 0 else { throw POSIXError(.init(rawValue: errno) ?? .EIO) }
        guard listen(listenFD, 16) == 0 else { throw POSIXError(.init(rawValue: errno) ?? .EIO) }
    }

    func start() {
        queue.async { [weak self] in
            self?.acceptLoop()
        }
    }

    func stop() {
        if listenFD >= 0 {
            close(listenFD)
            listenFD = -1
        }
        unlink(socketPath)
    }

    private func acceptLoop() {
        while listenFD >= 0 {
            let fd = accept(listenFD, nil, nil)
            if fd < 0 { break }
            handle(fd: fd)
            close(fd)
        }
    }

    private func handle(fd: Int32) {
        guard let request = readRequest(fd: fd) else { return }
        lock.lock()
        _requestCount += 1
        lock.unlock()

        let response = handler(request)
        writeResponse(response, to: fd)
    }

    private func readRequest(fd: Int32) -> Request? {
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        var headerEnd: Range<Data.Index>?
        while headerEnd == nil {
            let n = recv(fd, &buffer, buffer.count, 0)
            if n <= 0 { return nil }
            data.append(buffer, count: n)
            headerEnd = data.range(of: Data("\r\n\r\n".utf8))
        }

        guard let headerRange = headerEnd,
              let headerText = String(data: data[..<headerRange.lowerBound], encoding: .utf8)
        else { return nil }

        let lines = headerText.components(separatedBy: "\r\n")
        guard let requestLine = lines.first else { return nil }
        let parts = requestLine.split(separator: " ", maxSplits: 2).map(String.init)
        guard parts.count >= 2 else { return nil }

        var headers: [String: String] = [:]
        for line in lines.dropFirst() {
            guard let colon = line.firstIndex(of: ":") else { continue }
            let key = String(line[..<colon]).trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            let value = String(line[line.index(after: colon)...]).trimmingCharacters(in: .whitespacesAndNewlines)
            headers[key] = value
        }

        let bodyStart = headerRange.upperBound
        let contentLength = Int(headers["content-length"] ?? "0") ?? 0
        while data.count - bodyStart < contentLength {
            let n = recv(fd, &buffer, buffer.count, 0)
            if n <= 0 { return nil }
            data.append(buffer, count: n)
        }
        let body = data.subdata(in: bodyStart..<(bodyStart + contentLength))
        return Request(method: parts[0], path: parts[1], headers: headers, body: body)
    }

    private func writeResponse(_ response: Response, to fd: Int32) {
        var headers = response.headers
        if response.chunks == nil {
            headers["Content-Length"] = "\(response.body.count)"
        }
        headers["Connection"] = "close"

        var head = "HTTP/1.1 \(response.status) \(response.reason)\r\n"
        for (key, value) in headers {
            head += "\(key): \(value)\r\n"
        }
        head += "\r\n"
        writeAll(Data(head.utf8), to: fd)

        if let chunks = response.chunks {
            for chunk in chunks {
                writeAll(Data(String(chunk.count, radix: 16).utf8), to: fd)
                writeAll(Data("\r\n".utf8), to: fd)
                writeAll(chunk, to: fd)
                writeAll(Data("\r\n".utf8), to: fd)
            }
            if response.terminateChunked {
                writeAll(Data("0\r\n\r\n".utf8), to: fd)
            }
        } else {
            writeAll(response.body, to: fd)
        }
    }

    private func writeAll(_ data: Data, to fd: Int32) {
        data.withUnsafeBytes { raw in
            guard let base = raw.baseAddress else { return }
            var sent = 0
            while sent < data.count {
                let n = Darwin.write(fd, base.advanced(by: sent), data.count - sent)
                if n <= 0 { break }
                sent += n
            }
        }
    }
}
