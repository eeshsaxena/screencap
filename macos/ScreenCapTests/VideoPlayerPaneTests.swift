import XCTest
@testable import ScreenCap

/// Fake `VideoPlaybackEngine` that records the commands the model issues so
/// the U5 tests can assert behavior without spinning up AVFoundation or
/// touching disk for a media asset.
@MainActor
final class FakeVideoPlaybackEngine: VideoPlaybackEngine {
    var currentSeconds: Double = 0
    var loadStatus: VideoLoadStatus = .loading

    private(set) var seekRequests: [Double] = []
    private(set) var playCalls = 0
    private(set) var pauseCalls = 0
    private(set) var startObserveCalls = 0
    private(set) var stopObserveCalls = 0
    private(set) var lastObserveInterval: Double?

    private var timeHandler: (@MainActor (Double) -> Void)?
    private var statusHandler: (@MainActor (VideoLoadStatus) -> Void)?

    func seek(toSeconds seconds: Double) {
        seekRequests.append(seconds)
        currentSeconds = seconds
    }

    func play() { playCalls += 1 }
    func pause() { pauseCalls += 1 }

    func startObservingTime(interval: Double, onTick: @escaping @MainActor (Double) -> Void) {
        startObserveCalls += 1
        lastObserveInterval = interval
        timeHandler = onTick
    }

    func stopObservingTime() {
        stopObserveCalls += 1
        timeHandler = nil
    }

    func observeLoadStatus(_ onChange: @escaping @MainActor (VideoLoadStatus) -> Void) {
        statusHandler = onChange
        onChange(loadStatus)
    }

    /// Test helper: simulate a periodic-time-observer tick at `t`.
    func tick(at seconds: Double) {
        currentSeconds = seconds
        timeHandler?(seconds)
    }

    /// Test helper: simulate AVPlayer load status changing under the model.
    func transitionStatus(to status: VideoLoadStatus) {
        loadStatus = status
        statusHandler?(status)
    }
}

@MainActor
final class VideoPlayerPaneTests: XCTestCase {
    func testInitialStateCleanAndObserverArmed() {
        let engine = FakeVideoPlaybackEngine()
        let model = VideoPlayerPaneModel(engine: engine)

        XCTAssertEqual(model.currentTime, 0)
        XCTAssertFalse(model.isPlaying)
        XCTAssertEqual(model.loadStatus, .loading)
        XCTAssertEqual(engine.startObserveCalls, 1)
        XCTAssertEqual(engine.lastObserveInterval ?? 0, 0.1, accuracy: 0.001)
    }

    func testPeriodicTimeObserverPublishesCurrentTime() {
        let engine = FakeVideoPlaybackEngine()
        let model = VideoPlayerPaneModel(engine: engine)

        engine.tick(at: 0.5)
        engine.tick(at: 1.0)

        XCTAssertEqual(model.currentTime, 1.0)
    }

    func testFlippingIsPlayingPlaysAndPausesEngine() {
        let engine = FakeVideoPlaybackEngine()
        let model = VideoPlayerPaneModel(engine: engine)

        model.isPlaying = true
        model.isPlaying = false

        XCTAssertEqual(engine.playCalls, 1)
        XCTAssertEqual(engine.pauseCalls, 1)
    }

    func testRepeatingSameIsPlayingValueDoesNotDoubleDispatch() {
        let engine = FakeVideoPlaybackEngine()
        let model = VideoPlayerPaneModel(engine: engine)

        model.isPlaying = true
        model.isPlaying = true

        XCTAssertEqual(engine.playCalls, 1)
        XCTAssertEqual(engine.pauseCalls, 0)
    }

    func testSeekDispatchesToEngineAndUpdatesCurrentTime() {
        let engine = FakeVideoPlaybackEngine()
        let model = VideoPlayerPaneModel(engine: engine)

        model.seek(toSeconds: 12.5)

        XCTAssertEqual(engine.seekRequests, [12.5])
        XCTAssertEqual(model.currentTime, 12.5)
    }

    /// Risk-table guard: a periodic-observer tick that arrives mid-scrub must
    /// not snap the cursor backward to a stale frame timestamp. The model
    /// flags itself as "seeking from scrub" between `seek()` and the next
    /// runloop turn — the test exercises that window.
    func testTickArrivingMidScrubDoesNotClobberSeekTarget() {
        let engine = FakeVideoPlaybackEngine()
        let model = VideoPlayerPaneModel(engine: engine)

        model.seek(toSeconds: 20.0)
        // Simulates a periodic-observer tick that fires before the engine
        // has actually completed the seek (the time reported is still the
        // pre-seek position).
        engine.tick(at: 5.0)

        XCTAssertEqual(model.currentTime, 20.0)
    }

    func testTearDownStopsTimeObserver() {
        let engine = FakeVideoPlaybackEngine()
        let model = VideoPlayerPaneModel(engine: engine)

        model.tearDown()

        XCTAssertEqual(engine.stopObserveCalls, 1)
    }

    func testLoadStatusTransitionsPublishToModel() {
        let engine = FakeVideoPlaybackEngine()
        let model = VideoPlayerPaneModel(engine: engine)

        engine.transitionStatus(to: .ready)
        XCTAssertEqual(model.loadStatus, .ready)

        engine.transitionStatus(to: .failed("missing file"))
        XCTAssertEqual(model.loadStatus, .failed("missing file"))
    }

    /// Per the plan: "pane initialized with a missing-file URL → publishes a
    /// load-failure state the parent can render (does not crash)." The
    /// production engine surfaces this via AVPlayer's status; the model
    /// must propagate it to its `loadStatus` for U8 to render.
    func testFailureLoadStatusPropagates() {
        let engine = FakeVideoPlaybackEngine()
        engine.loadStatus = .failed("File not found")
        let model = VideoPlayerPaneModel(engine: engine)

        XCTAssertEqual(model.loadStatus, .failed("File not found"))
    }

    /// Smoke-tests the live engine against a non-existent URL just to prove
    /// it does NOT crash on construction — the failure surfaces via the
    /// load-status observation path which AVPlayer drives asynchronously.
    func testLiveEngineSurvivesMissingFileURL() {
        let url = URL(fileURLWithPath: "/tmp/screencap-this-file-does-not-exist.mp4")
        let engine = LiveVideoPlaybackEngine(url: url)

        XCTAssertNotNil(engine.player)
        XCTAssertEqual(engine.currentSeconds, 0)
    }
}
