import Combine
import XCTest
@testable import ScreenCap

@MainActor
final class FakeInspectDataLoader: InspectDataLoader {
    var nextEnvelope: ReviewDataEnvelope?
    var nextError: Error?
    private(set) var loadCallCount = 0

    func load(name: String) async throws -> ReviewDataEnvelope {
        loadCallCount += 1
        if let err = nextError {
            nextError = nil
            throw err
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

private enum FakeInspectLoadError: Error, LocalizedError {
    case boom
    var errorDescription: String? { "loader exploded" }
}

@MainActor
final class InspectWindowViewModelTests: XCTestCase {

    private func makeModel(loader: FakeInspectDataLoader) -> InspectWindowViewModel {
        InspectWindowViewModel(recordingName: "rec-001", loader: loader)
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
