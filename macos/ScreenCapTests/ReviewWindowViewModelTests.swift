import Combine
import XCTest
@testable import ScreenCap

@MainActor
final class FakeReviewDataLoader: ReviewDataLoader {
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
            schemaVersion: 1,
            videoPath: "/tmp/video.mp4",
            eventsPath: "/tmp/events.jsonl",
            startedAt: 1700000000,
            durationSeconds: 30,
            videoPixfmtRemediated: false,
            error: nil
        )
    }
}

@MainActor
final class FakeReviewWindowEffects: ReviewWindowEffects {
    private(set) var refreshCalls = 0
    private(set) var scheduledAutoCloses: [Double] = []
    var pendingAutoClose: (() -> Void)?
    var autoCloseHandle: AutoCloseHandle?

    func refreshIndex() async { refreshCalls += 1 }

    func scheduleAutoClose(after seconds: Double, _ action: @escaping @MainActor () -> Void) -> AutoCloseHandle {
        scheduledAutoCloses.append(seconds)
        pendingAutoClose = action
        let handle = AutoCloseHandle { [weak self] in
            self?.pendingAutoClose = nil
        }
        autoCloseHandle = handle
        return handle
    }

    /// Test helper — fire the scheduled auto-close immediately as if the
    /// timer had elapsed.
    func fireAutoClose() {
        pendingAutoClose?()
        pendingAutoClose = nil
    }
}

enum FakeReviewLoadError: Error, LocalizedError {
    case boom
    var errorDescription: String? { "loader exploded" }
}

@MainActor
final class ReviewWindowViewModelTests: XCTestCase {

    func testInitialStateIsPreparing() {
        let model = makeModel()
        if case .preparing = model.state {} else {
            XCTFail("expected preparing, got \(model.state)")
        }
    }

    func testLoadReviewDataTransitionsToReadyOnOkEnvelope() async {
        let loader = FakeReviewDataLoader()
        let model = makeModel(loader: loader)

        await model.loadReviewData()

        if case .ready(let data) = model.state {
            XCTAssertEqual(data.videoURL.path, "/tmp/video.mp4")
            XCTAssertEqual(data.durationSeconds, 30)
        } else {
            XCTFail("expected ready, got \(model.state)")
        }
        XCTAssertEqual(loader.loadCallCount, 1)
    }

    func testLoadReviewDataWithErrorEnvelopeTransitionsToFailedWithoutRetryData() async {
        let loader = FakeReviewDataLoader()
        loader.nextEnvelope = .init(
            ok: false,
            schemaVersion: 1,
            videoPath: nil, eventsPath: nil, startedAt: nil, durationSeconds: nil,
            videoPixfmtRemediated: nil,
            error: "recording not found"
        )
        let model = makeModel(loader: loader)

        await model.loadReviewData()

        if case .failed(let message, let retry) = model.state {
            XCTAssertEqual(message, "recording not found")
            XCTAssertNil(retry, "preparation failure carries no retry data")
        } else {
            XCTFail("expected failed without retry data, got \(model.state)")
        }
    }

    func testLoaderThrowsTransitionsToFailed() async {
        let loader = FakeReviewDataLoader()
        loader.nextError = FakeReviewLoadError.boom
        let model = makeModel(loader: loader)

        await model.loadReviewData()

        if case .failed(let message, _) = model.state {
            XCTAssertEqual(message, "loader exploded")
        } else {
            XCTFail("expected failed, got \(model.state)")
        }
    }

    func testStartUploadFromReadyTransitionsToUploading() async {
        let controller = UploadController(service: FakeUploadService())
        let model = makeModel(controller: controller)

        await model.loadReviewData()
        model.startUpload()

        if case .uploading(_, let data) = model.state {
            XCTAssertEqual(data.durationSeconds, 30)
        } else {
            XCTFail("expected uploading, got \(model.state)")
        }
    }

    func testUploadSuccessRefreshesIndexAndSchedulesAutoClose() async {
        let service = FakeUploadService()
        let controller = UploadController(service: service)
        let effects = FakeReviewWindowEffects()
        let model = makeModel(controller: controller, effects: effects)

        await model.loadReviewData()
        model.startUpload()
        service.emit(#"{"type": "upload_started", "schema_version": 1, "file_count": 1}"#)
        service.emit(#"{"type": "upload_finished", "schema_version": 1, "uploaded": 1, "skipped": 0, "failed": 0}"#)

        // Yield once so the @Published sink on upload state has dispatched.
        await Task.yield()

        if case .succeeded(let summary) = model.state {
            XCTAssertEqual(summary.uploaded, 1)
        } else {
            XCTFail("expected succeeded, got \(model.state)")
        }
        XCTAssertEqual(effects.scheduledAutoCloses, [2.0])
        // Refresh is fire-and-forget through Task; allow it to land.
        for _ in 0..<10 {
            if effects.refreshCalls == 1 { break }
            await Task.yield()
        }
        XCTAssertEqual(effects.refreshCalls, 1)
    }

    func testAutoCloseTimerFiringDismissesWindow() async {
        let service = FakeUploadService()
        let controller = UploadController(service: service)
        let effects = FakeReviewWindowEffects()
        let model = makeModel(controller: controller, effects: effects)
        var dismissCalls = 0
        model.dismissHandler = { dismissCalls += 1 }

        await model.loadReviewData()
        model.startUpload()
        service.emit(#"{"type": "upload_started", "schema_version": 1, "file_count": 1}"#)
        service.emit(#"{"type": "upload_finished", "schema_version": 1, "uploaded": 1, "skipped": 0, "failed": 0}"#)
        await Task.yield()

        effects.fireAutoClose()

        XCTAssertEqual(dismissCalls, 1)
    }

    /// Covers AE5: failure mid-upload keeps the window open with a Retry
    /// affordance. The viewmodel carries the prepared ReviewData forward
    /// so Retry doesn't re-spawn `review-data`.
    func testUploadFailedTransitionsToFailedWithRetryDataAndRetryStartsAgain() async {
        let service = FakeUploadService()
        let controller = UploadController(service: service)
        let model = makeModel(controller: controller)

        await model.loadReviewData()
        model.startUpload()
        service.emit(#"{"type": "upload_started", "schema_version": 1, "file_count": 1}"#)
        service.emit(#"{"type": "upload_failed", "schema_version": 1, "error": "network down"}"#)
        await Task.yield()

        if case .failed(let message, let retry) = model.state {
            XCTAssertEqual(message, "network down")
            XCTAssertNotNil(retry, "upload-time failure must carry retry data forward")
        } else {
            XCTFail("expected failed with retry data, got \(model.state)")
        }

        // Reset the fake for a second spawn cycle.
        service.terminate(exitCode: 1)
        service.fakeProcess = FakeSpawnedProcessHandle(isRunning: true, pid: 99, forceKillReturnValue: true)
        model.startUpload()
        if case .uploading = model.state {} else {
            XCTFail("expected uploading after retry, got \(model.state)")
        }
    }

    /// Covers AE4: window dismissed while uploading → controller.cancel()
    /// is called; the Python side's SIGTERM handler then emits
    /// upload_failed(error: "interrupted") and state lands on failed.
    func testWindowCloseDuringUploadInvokesCancel() async {
        let service = FakeUploadService()
        let controller = UploadController(service: service)
        let model = makeModel(controller: controller)

        await model.loadReviewData()
        model.startUpload()
        service.emit(#"{"type": "upload_started", "schema_version": 1, "file_count": 1}"#)

        model.windowDidClose()

        XCTAssertEqual(service.fakeProcess.terminateInvocations, 1)
    }

    func testWindowCloseDuringPreparingDoesNotCrashAndDoesNotTerminate() async {
        let service = FakeUploadService()
        let controller = UploadController(service: service)
        let model = makeModel(controller: controller)

        // No loadReviewData → state remains .preparing.
        model.windowDidClose()

        XCTAssertEqual(service.fakeProcess.terminateInvocations, 0)
    }

    /// Auto-close handle cancellation prevents the dismiss callback from
    /// firing after the user manually closed the window.
    func testWindowCloseBeforeAutoCloseFiresCancelsTheTimer() async {
        let service = FakeUploadService()
        let controller = UploadController(service: service)
        let effects = FakeReviewWindowEffects()
        let model = makeModel(controller: controller, effects: effects)

        await model.loadReviewData()
        model.startUpload()
        service.emit(#"{"type": "upload_started", "schema_version": 1, "file_count": 1}"#)
        service.emit(#"{"type": "upload_finished", "schema_version": 1, "uploaded": 1, "skipped": 0, "failed": 0}"#)
        await Task.yield()

        XCTAssertNotNil(effects.pendingAutoClose)

        model.windowDidClose()

        XCTAssertNil(effects.pendingAutoClose, "auto-close handle should have been cancelled")
    }

    // MARK: - Helpers

    private func makeModel(
        loader: ReviewDataLoader = FakeReviewDataLoader(),
        controller: UploadController = UploadController(service: FakeUploadService()),
        effects: ReviewWindowEffects = FakeReviewWindowEffects(),
        autoCloseSeconds: Double = 2.0
    ) -> ReviewWindowViewModel {
        ReviewWindowViewModel(
            recordingName: "rec-001",
            uploadController: controller,
            loader: loader,
            effects: effects,
            autoCloseSeconds: autoCloseSeconds
        )
    }
}
