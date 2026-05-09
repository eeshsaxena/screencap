import XCTest
@testable import ScreenCap

@MainActor
final class RecorderControllerDaemonTests: XCTestCase {
    private var socketPath: String!
    private var server: UnixHTTPTestServer?

    override func setUp() {
        super.setUp()
        socketPath = "/tmp/sc-rc-\(UUID().uuidString.prefix(8)).sock"
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

    func testDaemonTransportHappyPathUsesEventStreamForStateTransitions() async throws {
        _ = try startServer { request in
            switch request.path {
            case "/v0/daemon.info":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"build":null,"started_at":1.0}"#)
            case "/v0/recording.start":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"session_id":"session-1","started_at":10.0}"#)
            case "/v0/session.snapshot":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":true,"daemon_owned":true,"recording_name":"demo","started_at":10.0,"claimant":"daemon","recovering":false,"cursor":3}"#)
            case "/v0/events?since=3":
                return .chunked([
                    #"{"type":"subscribed","schema_version":1,"cursor":3,"ts":11.0}"# + "\n",
                    #"{"type":"started","schema_version":1,"cursor":4,"ts":12.0}"# + "\n",
                ])
            default:
                XCTFail("Unexpected request path \(request.path)")
                return .json(#"{"ok":false,"schema_version":1,"daemon_version":"test","api_schema_version":1,"error":"unexpected"}"#, status: 500)
            }
        }

        let recorder = RecorderController()
        await recorder.probeDaemon()
        XCTAssertEqual(recorder.transport, .daemon)

        recorder.start(name: "demo")

        await waitUntil {
            if case .recording = recorder.state { return true }
            return false
        }
        XCTAssertNil(recorder.lastError)
    }

    func testDaemonTransportReconnectsAfterDroppedEventStream() async throws {
        let eventConnectionCount = LockedInt()
        _ = try startServer { request in
            switch request.path {
            case "/v0/daemon.info":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"build":null,"started_at":1.0}"#)
            case "/v0/recording.start":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"session_id":"session-2","started_at":10.0}"#)
            case "/v0/session.snapshot":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":true,"daemon_owned":true,"recording_name":"demo","started_at":10.0,"claimant":"daemon","recovering":false,"cursor":5}"#)
            case "/v0/events?since=5":
                if eventConnectionCount.incrementAndGet() == 1 {
                    return .chunked([
                        #"{"type":"subscribed","schema_version":1,"cursor":5,"ts":11.0}"# + "\n",
                    ], terminate: false)
                }
                return .chunked([
                    #"{"type":"subscribed","schema_version":1,"cursor":5,"ts":12.0}"# + "\n",
                    #"{"type":"started","schema_version":1,"cursor":6,"ts":13.0}"# + "\n",
                ])
            default:
                XCTFail("Unexpected request path \(request.path)")
                return .json(#"{"ok":false,"schema_version":1,"daemon_version":"test","api_schema_version":1,"error":"unexpected"}"#, status: 500)
            }
        }

        let recorder = RecorderController()
        await recorder.probeDaemon()
        recorder.start(name: "demo")

        await waitUntil {
            if case .recording = recorder.state { return true }
            return false
        }
        XCTAssertGreaterThanOrEqual(eventConnectionCount.value, 2)
    }

    func testSchemaMismatchFromProbeSetsPublishedFlagAndFallsBackToCLI() async throws {
        _ = try startServer { _ in
            .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":99,"build":null,"started_at":1.0}"#)
        }

        let recorder = RecorderController()
        await recorder.probeDaemon()

        XCTAssertEqual(recorder.transport, .cliFallback)
        XCTAssertTrue(recorder.schemaMismatchDetected)
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

    private func waitUntil(
        timeout: TimeInterval = 3,
        file: StaticString = #filePath,
        line: UInt = #line,
        _ predicate: @escaping @MainActor () -> Bool
    ) async {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if predicate() { return }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        XCTFail("Timed out waiting for condition", file: file, line: line)
    }
}

private final class LockedInt: @unchecked Sendable {
    private let lock = NSLock()
    private var storage = 0

    var value: Int {
        lock.lock()
        defer { lock.unlock() }
        return storage
    }

    func incrementAndGet() -> Int {
        lock.lock()
        defer { lock.unlock() }
        storage += 1
        return storage
    }
}
