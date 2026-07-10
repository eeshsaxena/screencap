import Combine
import XCTest
@testable import ScreenCap

@MainActor
final class FakeInspectDataLoader: InspectDataLoader {
    var nextEnvelope: ReviewDataEnvelope?
    var nextError: Error?
    /// Envelopes returned in order, one per `load` call, before falling back to
    /// `nextEnvelope`/the default success. Lets a test script a "still finalizing
    /// then ready" sequence for the auto-retry path.
    var envelopeQueue: [ReviewDataEnvelope] = []
    private(set) var loadCallCount = 0

    func load(name: String) async throws -> ReviewDataEnvelope {
        loadCallCount += 1
        if let err = nextError {
            nextError = nil
            throw err
        }
        if !envelopeQueue.isEmpty {
            return envelopeQueue.removeFirst()
        }
        return nextEnvelope ?? .init(
            ok: true,
            schemaVersion: 3,
            videoPath: "/tmp/video.mp4",
            eventsPath: "/tmp/events.jsonl",
            startedAt: 1700000000,
            durationSeconds: 30,
            videoPixfmtRemediated: false,
            error: nil,
            eventsPaths: ["/tmp/events.jsonl"]
        )
    }
}

/// An `ok=false` + `retryable` envelope — the "recording is still finalizing"
/// transient the CLI emits when the terminal_lock is held right after stop.
@MainActor
private func busyFinalizingEnvelope() -> ReviewDataEnvelope {
    .init(
        ok: false, schemaVersion: 3,
        videoPath: nil, eventsPath: nil,
        startedAt: nil, durationSeconds: nil,
        videoPixfmtRemediated: nil,
        error: "Recording is still finalizing — try again in a moment.",
        retryable: true
    )
}

private enum FakeInspectLoadError: Error, LocalizedError {
    case boom
    var errorDescription: String? { "loader exploded" }
}

@MainActor
final class InspectWindowViewModelTests: XCTestCase {

    private func makeModel(
        loader: FakeInspectDataLoader,
        retryDelaySeconds: TimeInterval = 0,
        maxRetryableAttempts: Int = 8
    ) -> InspectWindowViewModel {
        // retryDelaySeconds: 0 keeps the auto-retry loop instant in tests.
        InspectWindowViewModel(
            recordingName: "rec-001",
            loader: loader,
            retryDelaySeconds: retryDelaySeconds,
            maxRetryableAttempts: maxRetryableAttempts
        )
    }

    func test_startsInPreparing() {
        let model = makeModel(loader: FakeInspectDataLoader())
        XCTAssertEqual(model.state, .preparing)
    }

    func test_okWithVideoAndEvents_landsReady() async {
        let loader = FakeInspectDataLoader()
        let model = makeModel(loader: loader)

        await model.loadInspectData()

        guard case .ready(let data) = model.state else {
            return XCTFail("expected ready, got \(model.state)")
        }
        XCTAssertEqual(data.videoURL, URL(fileURLWithPath: "/tmp/video.mp4"))
        XCTAssertEqual(data.eventsURLs, [URL(fileURLWithPath: "/tmp/events.jsonl")])
        XCTAssertEqual(data.startedAt, 1700000000)
        XCTAssertEqual(data.durationSeconds, 30)
    }

    /// The inspect-specific contract: a recording with NO events is still worth
    /// looking at, so an empty event set lands `.ready` (video-first) rather
    /// than `.failed`. This is where inspect diverges from review.
    func test_emptyEvents_stillLandsReady_videoFirst() async {
        let loader = FakeInspectDataLoader()
        loader.nextEnvelope = .init(
            ok: true, schemaVersion: 3,
            videoPath: "/tmp/video.mp4",
            eventsPath: nil,
            startedAt: 1700000000, durationSeconds: 30,
            videoPixfmtRemediated: false, error: nil,
            eventsPaths: []
        )
        let model = makeModel(loader: loader)

        await model.loadInspectData()

        guard case .ready(let data) = model.state else {
            return XCTFail("expected ready (video-first), got \(model.state)")
        }
        XCTAssertEqual(data.videoURL, URL(fileURLWithPath: "/tmp/video.mp4"))
        XCTAssertTrue(data.eventsURLs.isEmpty)
    }

    /// SCR-102 regression: null timing must NOT gate readiness — it falls back
    /// to 0 (which the panes handle), not `.failed`.
    func test_nullTiming_landsReadyWithZeroFallback() async {
        let loader = FakeInspectDataLoader()
        loader.nextEnvelope = .init(
            ok: true, schemaVersion: 3,
            videoPath: "/tmp/video.mp4",
            eventsPath: "/tmp/events.jsonl",
            startedAt: nil, durationSeconds: nil,
            videoPixfmtRemediated: false, error: nil,
            eventsPaths: ["/tmp/events.jsonl"]
        )
        let model = makeModel(loader: loader)

        await model.loadInspectData()

        guard case .ready(let data) = model.state else {
            return XCTFail("null timing must still be ready, got \(model.state)")
        }
        XCTAssertEqual(data.startedAt, 0)
        XCTAssertEqual(data.durationSeconds, 0)
    }

    func test_timingStatusResolvesFromEnvelope() async {
        let loader = FakeInspectDataLoader()
        loader.nextEnvelope = .init(
            ok: true, schemaVersion: 3,
            videoPath: "/tmp/video.mp4",
            eventsPath: "/tmp/events.jsonl",
            startedAt: nil, durationSeconds: nil,
            videoPixfmtRemediated: false, error: nil,
            eventsPaths: ["/tmp/events.jsonl"],
            timingStatus: "corrupt"
        )
        let model = makeModel(loader: loader)

        await model.loadInspectData()

        guard case .ready(let data) = model.state else {
            return XCTFail("expected ready, got \(model.state)")
        }
        XCTAssertEqual(data.timingStatus, .corrupt)
    }

    // U4: schema-4 blocked/protected intervals map into InspectData; a payload
    // without them (older CLI / review shape) decodes with empty arrays.
    func test_blockedIntervalsMapIntoInspectData() async {
        let loader = FakeInspectDataLoader()
        loader.nextEnvelope = .init(
            ok: true, schemaVersion: 4,
            videoPath: "/tmp/video.mp4",
            eventsPath: "/tmp/events.jsonl",
            startedAt: 1700000000, durationSeconds: 30,
            videoPixfmtRemediated: false, error: nil,
            eventsPaths: ["/tmp/events.jsonl"],
            blockedIntervals: [CapturedInterval(startMs: 1000, endMs: 2000)],
            protectedIntervals: [CapturedInterval(startMs: 1000, endMs: 3000)]
        )
        let model = makeModel(loader: loader)

        await model.loadInspectData()

        guard case .ready(let data) = model.state else {
            return XCTFail("expected ready, got \(model.state)")
        }
        XCTAssertEqual(data.blockedIntervals, [CapturedInterval(startMs: 1000, endMs: 2000)])
        XCTAssertEqual(data.protectedIntervals, [CapturedInterval(startMs: 1000, endMs: 3000)])
    }

    func test_missingBlockedIntervals_decodeToEmpty_backCompat() async {
        // The default envelope carries no interval fields (older schema).
        let loader = FakeInspectDataLoader()
        let model = makeModel(loader: loader)

        await model.loadInspectData()

        guard case .ready(let data) = model.state else {
            return XCTFail("expected ready, got \(model.state)")
        }
        XCTAssertTrue(data.blockedIntervals.isEmpty)
        XCTAssertTrue(data.protectedIntervals.isEmpty)
    }

    // The snake_case CodingKeys decode the Python `{start_ms, end_ms}` payload
    // (schema v4); an envelope omitting them still decodes (nil → empty).
    func test_envelopeJSONDecodesBlockedIntervals() throws {
        let json = """
        {
          "ok": true,
          "schema_version": 4,
          "video_path": "/tmp/video.mp4",
          "blocked_intervals": [{"start_ms": 1000, "end_ms": 2000}],
          "protected_intervals": [{"start_ms": 1000, "end_ms": 3000}]
        }
        """.data(using: .utf8)!

        let env = try JSONDecoder().decode(ReviewDataEnvelope.self, from: json)
        XCTAssertEqual(env.blockedIntervals, [CapturedInterval(startMs: 1000, endMs: 2000)])
        XCTAssertEqual(env.protectedIntervals, [CapturedInterval(startMs: 1000, endMs: 3000)])

        let older = """
        {"ok": true, "schema_version": 3, "video_path": "/tmp/video.mp4"}
        """.data(using: .utf8)!
        let olderEnv = try JSONDecoder().decode(ReviewDataEnvelope.self, from: older)
        XCTAssertNil(olderEnv.blockedIntervals)
        XCTAssertNil(olderEnv.protectedIntervals)
    }

    func test_okFalse_landsFailedWithEnvelopeError() async {
        let loader = FakeInspectDataLoader()
        loader.nextEnvelope = .init(
            ok: false, schemaVersion: 3,
            videoPath: nil, eventsPath: nil,
            startedAt: nil, durationSeconds: nil,
            videoPixfmtRemediated: false,
            error: "can't process this video"
        )
        let model = makeModel(loader: loader)

        await model.loadInspectData()

        guard case .failed(let message) = model.state else {
            return XCTFail("expected failed, got \(model.state)")
        }
        XCTAssertEqual(message, "can't process this video")
    }

    func test_missingVideoPath_landsFailed() async {
        let loader = FakeInspectDataLoader()
        loader.nextEnvelope = .init(
            ok: true, schemaVersion: 3,
            videoPath: nil,
            eventsPath: "/tmp/events.jsonl",
            startedAt: nil, durationSeconds: nil,
            videoPixfmtRemediated: false, error: nil,
            eventsPaths: ["/tmp/events.jsonl"]
        )
        let model = makeModel(loader: loader)

        await model.loadInspectData()

        guard case .failed = model.state else {
            return XCTFail("missing video must fail, got \(model.state)")
        }
    }

    func test_loaderThrows_landsFailed() async {
        let loader = FakeInspectDataLoader()
        loader.nextError = FakeInspectLoadError.boom
        let model = makeModel(loader: loader)

        await model.loadInspectData()

        guard case .failed(let message) = model.state else {
            return XCTFail("expected failed, got \(model.state)")
        }
        XCTAssertEqual(message, "loader exploded")
    }

    /// The view-during-finalization fix: a `retryable` ("still finalizing")
    /// envelope must NOT surface `.failed` — the model stays in `preparing` and
    /// auto-retries until finalization completes, then lands `.ready`. Regression
    /// for the spurious "Could not load this recording." right after stop.
    func test_retryableStillFinalizing_autoRetriesThenLandsReady() async {
        let loader = FakeInspectDataLoader()
        // Two "still finalizing" responses, then the queue drains to the default
        // success envelope on the third call.
        loader.envelopeQueue = [busyFinalizingEnvelope(), busyFinalizingEnvelope()]
        let model = makeModel(loader: loader)

        await model.loadInspectData()

        XCTAssertEqual(loader.loadCallCount, 3, "must retry past the finalizing responses")
        guard case .ready = model.state else {
            return XCTFail("still-finalizing must retry to ready, got \(model.state)")
        }
    }

    /// A recording that never finishes finalizing (always `retryable`) gives up
    /// after the bounded attempt count and surfaces the finalizing message —
    /// never an infinite retry loop.
    func test_retryableStillFinalizing_exhaustsToFailed() async {
        let loader = FakeInspectDataLoader()
        loader.nextEnvelope = busyFinalizingEnvelope()  // every call is "busy"
        let model = makeModel(loader: loader, maxRetryableAttempts: 3)

        await model.loadInspectData()

        XCTAssertEqual(loader.loadCallCount, 3, "must stop after maxRetryableAttempts")
        guard case .failed(let message) = model.state else {
            return XCTFail("exhausted retries must fail, got \(model.state)")
        }
        XCTAssertTrue(
            message.lowercased().contains("finalizing"),
            "the surfaced message should explain it is still finalizing, got: \(message)"
        )
    }

    func test_retryAfterFailure_resetsToPreparingAndReloads() async {
        let loader = FakeInspectDataLoader()
        loader.nextError = FakeInspectLoadError.boom
        let model = makeModel(loader: loader)

        await model.loadInspectData()
        guard case .failed = model.state else {
            return XCTFail("expected first load to fail")
        }

        // Second call (the Try-Again path): no error queued → success.
        await model.loadInspectData()

        XCTAssertEqual(loader.loadCallCount, 2)
        guard case .ready = model.state else {
            return XCTFail("retry must reload to ready, got \(model.state)")
        }
    }
}
