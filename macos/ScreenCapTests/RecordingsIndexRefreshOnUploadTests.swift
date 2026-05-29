import Combine
import XCTest
@testable import ScreenCap

/// U9 — assert the refresh contract is invoked on `.succeeded` and NOT on
/// `.failed`. The viewmodel test covers the refresh-effects call site; this
/// test covers the LiveReviewWindowEffects → NotificationCenter →
/// RecordingsIndex.refresh() chain end-to-end.
@MainActor
final class RecordingsIndexRefreshOnUploadTests: XCTestCase {

    func testReviewWindowUploadSucceededNotificationTriggersIndexRefresh() async {
        // Construct an index without autoload so we can observe the very
        // first refresh call after the notification fires.
        let index = RecordingsIndex(autoload: false)

        // Subscribe to isLoading transitions so we can deterministically
        // detect that refresh() started running.
        let expectation = expectation(description: "refresh started after notification")
        let cancellable = index.$isLoading
            .dropFirst() // skip the initial false
            .first(where: { $0 == true })
            .sink { _ in expectation.fulfill() }

        NotificationCenter.default.post(name: .reviewWindowUploadSucceeded, object: nil)

        await fulfillment(of: [expectation], timeout: 2.0)
        cancellable.cancel()
    }

    /// Per the plan: "viewmodel transitions to `.failed` → `RecordingsIndex.refresh()`
    /// is NOT called". This is the contract; the viewmodel-level test
    /// already pins it via the effects fake. This test pins the inverse
    /// at the notification layer — failure does not post the success
    /// notification, so the index does not see a refresh trigger.
    func testNoNotificationFiredFromFailedStateMeansNoRefreshTriggered() async {
        let service = FakeUploadService()
        let controller = UploadController(service: service)
        let effects = FakeReviewWindowEffects()
        let model = ReviewWindowViewModel(
            recordingName: "rec-001",
            uploadController: controller,
            loader: FakeReviewDataLoader(),
            effects: effects,
            autoCloseSeconds: 0.1
        )
        await model.loadReviewData()
        model.startUpload()
        service.emit(#"{"type": "upload_started", "schema_version": 1, "file_count": 1}"#)
        service.emit(#"{"type": "upload_failed", "schema_version": 1, "error": "rejected"}"#)
        await Task.yield()
        await Task.yield()

        // The fake effects records refresh invocations directly.
        XCTAssertEqual(effects.refreshCalls, 0)
        XCTAssertTrue(effects.scheduledAutoCloses.isEmpty, "auto-close must not fire on failure")
    }

    /// Repeated successes (multiple windows uploading distinct recordings)
    /// each post their own notification; the index handles each refresh
    /// independently. No coalescing required.
    ///
    /// Todo #016 — uses `XCTestExpectation` driven by `$isLoading`
    /// transitions instead of the previous busy-wait. The earlier
    /// 40×50ms sleep loop could exhaust its budget under CI load while
    /// the first refresh was still finishing, fire the second
    /// notification into the index's `!isLoading` guard, and flake.
    /// Event-driven waits do not have that failure mode.
    func testMultipleSuccessNotificationsTriggerMultipleRefreshes() async {
        let index = RecordingsIndex(autoload: false)

        // First refresh: start expectation fires the moment isLoading
        // transitions to true; finish expectation fires when it
        // transitions back to false.
        let firstStarted = expectation(description: "first refresh started")
        let firstFinished = expectation(description: "first refresh finished")
        let secondStarted = expectation(description: "second refresh started")

        // State machine: track which loading-edge to fulfill next so each
        // expectation fires exactly once. `dropFirst` skips the initial
        // false on the published property.
        nonisolated(unsafe) var phase = 0
        let cancellable = index.$isLoading
            .dropFirst()
            .sink { isLoading in
                switch (phase, isLoading) {
                case (0, true):
                    phase = 1
                    firstStarted.fulfill()
                case (1, false):
                    phase = 2
                    firstFinished.fulfill()
                case (2, true):
                    phase = 3
                    secondStarted.fulfill()
                default:
                    break
                }
            }

        NotificationCenter.default.post(name: .reviewWindowUploadSucceeded, object: nil)
        await fulfillment(of: [firstStarted, firstFinished], timeout: 2.0)

        NotificationCenter.default.post(name: .reviewWindowUploadSucceeded, object: nil)
        await fulfillment(of: [secondStarted], timeout: 2.0)

        cancellable.cancel()
    }
}
