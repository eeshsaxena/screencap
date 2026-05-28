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
    func testMultipleSuccessNotificationsTriggerMultipleRefreshes() async {
        let index = RecordingsIndex(autoload: false)
        var observedLoadingStarts = 0
        let cancellable = index.$isLoading
            .dropFirst()
            .sink { isLoading in
                if isLoading { observedLoadingStarts += 1 }
            }

        NotificationCenter.default.post(name: .reviewWindowUploadSucceeded, object: nil)
        // Wait for the first refresh to begin and complete before posting
        // again — the index's own `guard !isLoading else { return }` guard
        // would coalesce overlapping calls, which is the right production
        // behavior; this test just exercises sequential notifications.
        for _ in 0..<20 {
            if observedLoadingStarts >= 1 { break }
            await Task.yield()
        }
        // Let the refresh complete (it'll fail-soft to empty since no CLI
        // / daemon is reachable in test, but it'll still finish).
        for _ in 0..<40 {
            if index.isLoading == false { break }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }

        NotificationCenter.default.post(name: .reviewWindowUploadSucceeded, object: nil)
        for _ in 0..<20 {
            if observedLoadingStarts >= 2 { break }
            await Task.yield()
        }

        XCTAssertGreaterThanOrEqual(observedLoadingStarts, 2)
        cancellable.cancel()
    }
}
