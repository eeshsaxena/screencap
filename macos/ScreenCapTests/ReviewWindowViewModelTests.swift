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
    /// Fresh per-test cross-window upload registry (SCR-89). These tests all
    /// review the same `"rec-001"` name, so without isolation a controller
    /// left mid-upload in one case would keep its claim on the production
    /// `.shared` registry and make the next case's upload refuse.
    private var registry: UploadRegistry!

    override func setUp() {
        super.setUp()
        registry = UploadRegistry()
    }

    /// Builds an upload controller bound to this test's isolated registry.
    private func makeController(service: UploadService = FakeUploadService()) -> UploadController {
        UploadController(service: service, registry: registry)
    }

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
        let controller = makeController()
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
        let controller = makeController(service: service)
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
        let controller = makeController(service: service)
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
        let controller = makeController(service: service)
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

    /// SCR-155 — when another window already owns this recording's upload, the
    /// cross-window guard (SCR-89) refuses this window's upload. That refusal
    /// must surface as the distinct `.refused` state, NOT `.failed`: a
    /// `.failed` would render an enabled Retry that silently re-refuses for as
    /// long as the owner holds the claim. The refusal still carries the panes
    /// `data` so the window keeps rendering the review content.
    func testCrossWindowRefusalSurfacesRefusedStateWithPanesData() async {
        // Another window (owner) claims "rec-001" on the shared registry first.
        let owner = makeController(service: FakeUploadService())
        owner.start(name: "rec-001")

        let model = makeModel()
        await model.loadReviewData()
        model.startUpload()
        await Task.yield()

        if case .refused(let message, let data) = model.state {
            XCTAssertTrue(message.contains("another window"), "got: \(message)")
            XCTAssertNotNil(data, "refusal must keep panes data so the window still renders")
        } else {
            XCTFail("expected refused, got \(model.state)")
        }
        // Keep `owner` alive across the awaits above: if ARC released it
        // early, its deinit would free the claim and this window would upload
        // instead of being refused.
        withExtendedLifetime(owner) {}
    }

    /// SCR-155 — the reason it is safe to drop the Retry button: a `.refused`
    /// window must never silently re-upload. The guarantee is enforced at the
    /// state machine, not the view — re-invoking the upload entry point from
    /// `.refused` re-claims the registry, is refused again, stays `.refused`,
    /// and never spawns a process. This is the regression guard against the
    /// old `.failed` behavior whose enabled Retry re-refused for as long as
    /// the owner held the claim.
    func testReuploadFromRefusedReRefusesAndNeverSpawns() async {
        // Owner window claims "rec-001" first and holds it across the awaits.
        let owner = makeController(service: FakeUploadService())
        owner.start(name: "rec-001")

        let service = FakeUploadService()
        let model = makeModel(controller: makeController(service: service))
        await model.loadReviewData()

        // First attempt is refused.
        model.startUpload()
        await Task.yield()
        guard case .refused = model.state else {
            return XCTFail("expected refused after first attempt, got \(model.state)")
        }

        // Re-invoking upload from `.refused` (the path the old Retry took) must
        // re-refuse — not strand on `.uploading` and not spawn a process.
        model.startUpload()
        await Task.yield()
        if case .refused = model.state {} else {
            XCTFail("re-attempt from refused must stay refused, got \(model.state)")
        }
        XCTAssertEqual(service.startedNames, [], "a refused window must never spawn an upload")

        withExtendedLifetime(owner) {}
    }

    /// Covers AE4: window dismissed while uploading → controller.cancel()
    /// is called; the Python side's SIGTERM handler then emits
    /// upload_failed(error: "interrupted") and state lands on failed.
    func testWindowCloseDuringUploadInvokesCancel() async {
        let service = FakeUploadService()
        let controller = makeController(service: service)
        let model = makeModel(controller: controller)

        await model.loadReviewData()
        model.startUpload()
        service.emit(#"{"type": "upload_started", "schema_version": 1, "file_count": 1}"#)

        model.windowDidClose()

        XCTAssertEqual(service.fakeProcess.terminateInvocations, 1)
    }

    func testWindowCloseDuringPreparingDoesNotCrashAndDoesNotTerminate() async {
        let service = FakeUploadService()
        let controller = makeController(service: service)
        let model = makeModel(controller: controller)

        // No loadReviewData → state remains .preparing.
        model.windowDidClose()

        XCTAssertEqual(service.fakeProcess.terminateInvocations, 0)
    }

    /// Todo #008 — `loadReviewData()` re-entry after a prep failure. The
    /// retry path resets `.failed → .preparing` and re-runs the loader;
    /// a successful second call must land on `.ready`. Without this
    /// regression coverage, breaking the `if case .failed = state` guard
    /// would silently leave the window stuck in `.failed` on the user's
    /// retry attempt.
    func testLoadReviewDataReentersFromFailedAndSucceeds() async {
        let loader = FakeReviewDataLoader()
        loader.nextError = FakeReviewLoadError.boom
        let model = makeModel(loader: loader)

        await model.loadReviewData()
        if case .failed = model.state {
            // pass
        } else {
            XCTFail("expected failed after first call, got \(model.state)")
        }

        // Second call: loader configured to succeed (default envelope).
        await model.loadReviewData()
        if case .ready = model.state {
            // pass
        } else {
            XCTFail("expected ready after retry, got \(model.state)")
        }
        XCTAssertEqual(loader.loadCallCount, 2)
    }

    /// Todo #020 — envelope reports `ok: true` but a required field is
    /// nil. Falls through the guard to `.failed("Failed to prepare
    /// recording.")` because `envelope.error` is also nil. Distinct path
    /// from the existing `ok: false` test.
    func testOkTrueWithNilVideoPathLandsOnGenericFailure() async {
        let loader = FakeReviewDataLoader()
        loader.nextEnvelope = .init(
            ok: true,
            schemaVersion: 1,
            videoPath: nil,
            eventsPath: "/tmp/events.jsonl",
            startedAt: 1700000000,
            durationSeconds: 30,
            videoPixfmtRemediated: false,
            error: nil
        )
        let model = makeModel(loader: loader)

        await model.loadReviewData()

        if case .failed(let message, let retry) = model.state {
            XCTAssertEqual(message, "Failed to prepare recording.")
            XCTAssertNil(retry)
        } else {
            XCTFail("expected failed, got \(model.state)")
        }
    }

    /// SCR-102 — `ok: true` with valid paths but null timing metadata
    /// (`started_at` / `duration_seconds` serialized as JSON `null`) is a
    /// legitimate, *playable* recording with no action events (Python
    /// `review.py` → `_read_recording_meta` returns `None`). It must land
    /// on `.ready` with a fallback origin/duration of 0 — null timing on a
    /// playable video is not a preparation failure. `.failed` stays
    /// reserved for `ok: false` / missing-path envelopes (see
    /// `testOkTrueWithNilVideoPathLandsOnGenericFailure`).
    func testOkTrueWithNullTimingMetadataLandsOnReady() async {
        let loader = FakeReviewDataLoader()
        loader.nextEnvelope = .init(
            ok: true,
            schemaVersion: 1,
            videoPath: "/tmp/video.mp4",
            eventsPath: "/tmp/events.jsonl",
            startedAt: nil,
            durationSeconds: nil,
            videoPixfmtRemediated: false,
            error: nil
        )
        let model = makeModel(loader: loader)

        await model.loadReviewData()

        if case .ready(let data) = model.state {
            XCTAssertEqual(data.videoURL.path, "/tmp/video.mp4")
            XCTAssertEqual(data.startedAt, 0, "null started_at falls back to origin 0")
            XCTAssertEqual(data.durationSeconds, 0, "null duration_seconds falls back to unknown (0)")
            XCTAssertFalse(data.timingError, "benign event-free null is not a read failure")
        } else {
            XCTFail("expected ready for playable recording with null timing, got \(model.state)")
        }
    }

    /// SCR-107 — `ok: true` with null timing AND `timing_error: true` is a
    /// playable recording whose `recording.db` couldn't be read (corrupt /
    /// unreadable). It must still land on `.ready` (the video plays) but carry
    /// `timingError` so the panes surface a non-blocking advisory — NOT
    /// `.failed`, and NOT silently indistinguishable from a clean event-free
    /// recording. This is the discriminator the SCR-102 fix erased.
    func testTimingErrorEnvelopeLandsOnReadyWithAdvisoryFlag() async {
        let loader = FakeReviewDataLoader()
        loader.nextEnvelope = .init(
            ok: true,
            schemaVersion: 2,
            videoPath: "/tmp/video.mp4",
            eventsPath: "/tmp/events.jsonl",
            startedAt: nil,
            durationSeconds: nil,
            videoPixfmtRemediated: false,
            error: nil,
            timingError: true
        )
        let model = makeModel(loader: loader)

        await model.loadReviewData()

        if case .ready(let data) = model.state {
            XCTAssertTrue(data.timingError, "DB-read failure must set the advisory flag")
            XCTAssertEqual(data.startedAt, 0, "null timing still falls back to origin 0")
            XCTAssertEqual(data.durationSeconds, 0)
        } else {
            XCTFail("expected ready with advisory for unreadable-DB recording, got \(model.state)")
        }
    }

    /// Auto-close handle cancellation prevents the dismiss callback from
    /// firing after the user manually closed the window.
    func testWindowCloseBeforeAutoCloseFiresCancelsTheTimer() async {
        let service = FakeUploadService()
        let controller = makeController(service: service)
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

    /// SCR-90 — defensive disarm. Once `.succeeded` arms the auto-close timer,
    /// a fresh upload-state `.uploading` (the controller's Retry-after-success
    /// publish) must cancel the pending dismiss so a stale success timer can't
    /// auto-close a window the user is now interacting with. This asserts the
    /// *upload state* moving off `.succeeded`; the viewmodel's own `ReviewState`
    /// intentionally stays `.succeeded` here (`currentReviewData()` returns nil
    /// for `.succeeded`, so the `.uploading` branch disarms but does not
    /// advance). That restart-from-success path is not user-reachable today —
    /// this is defensive hardening, and a future change that makes it reachable
    /// will trip the `.succeeded` state assertion below.
    func testTransitionAwayFromSucceededCancelsPendingAutoClose() async {
        let service = FakeUploadService()
        let controller = makeController(service: service)
        let effects = FakeReviewWindowEffects()
        let model = makeModel(controller: controller, effects: effects)
        var dismissCalls = 0
        model.dismissHandler = { dismissCalls += 1 }

        await model.loadReviewData()
        model.startUpload()
        service.emit(#"{"type": "upload_started", "schema_version": 1, "file_count": 1}"#)
        service.emit(#"{"type": "upload_finished", "schema_version": 1, "uploaded": 1, "skipped": 0, "failed": 0}"#)
        await Task.yield()

        // Sanity: success armed the auto-close timer.
        XCTAssertNotNil(effects.pendingAutoClose, "success should arm the auto-close timer")

        // A fresh upload starts after success — the controller publishes a new
        // `.uploading` upload-state, so the viewmodel's observer runs its
        // `.uploading` branch and must disarm the pending dismiss.
        controller.start(name: "rec-001")
        await Task.yield()

        XCTAssertNil(
            effects.pendingAutoClose,
            "a fresh upload after success must cancel the auto-close timer")

        // The viewmodel's ReviewState intentionally stays `.succeeded`:
        // `currentReviewData()` returns nil for `.succeeded`, so the
        // `.uploading` observer branch disarms but does not advance the state.
        // Pinning this documents the current (unreachable) behavior, so a
        // future change that makes restart-after-success reachable trips here.
        if case .succeeded = model.state {} else {
            XCTFail("state should stay .succeeded after restart-from-success, got \(model.state)")
        }

        // Even if the stale timer somehow fired, no dismiss should occur.
        effects.fireAutoClose()
        XCTAssertEqual(dismissCalls, 0, "no dismiss fires after the disarm")
    }

    /// Covers AE5 (U6): Cancel on the ready review screen uploads nothing and
    /// is inert — no upload is started, no process is terminated, and the state
    /// stays ready (the original on-disk recording is never touched because no
    /// upload subprocess ran).
    func testCancelFromReadyUploadsNothing() async {
        let service = FakeUploadService()
        let controller = makeController(service: service)
        let model = makeModel(controller: controller)

        await model.loadReviewData()
        guard case .ready = model.state else {
            return XCTFail("expected ready, got \(model.state)")
        }

        model.cancel()

        XCTAssertEqual(service.fakeProcess.terminateInvocations, 0, "no upload to terminate")
        if case .ready = model.state {} else {
            XCTFail("Cancel before upload must be inert, got \(model.state)")
        }
    }

    /// Covers AE7 (U8): advisory risky-moment flags + redaction evidence never
    /// gate Upload. A ready envelope carrying a secure-field interval, redaction
    /// counts, and a fail-closed marker still uploads on intent.
    func testUploadStaysEnabledRegardlessOfRedactionFlags() async {
        let controller = makeController()
        let loader = FakeReviewDataLoader()
        loader.nextEnvelope = .init(
            ok: true, schemaVersion: 2,
            videoPath: "/tmp/video.mp4", eventsPath: "/tmp/events.jsonl",
            startedAt: 0, durationSeconds: 10, videoPixfmtRemediated: false, error: nil,
            eventsPaths: nil, screenshots: nil,
            redaction: ReviewRedaction(
                summary: ["EMAIL_ADDRESS": 2],
                markers: [ReviewMarker(t: 1, category: "secure_field_detected")],
                blockedIntervals: [ReviewBlockedInterval(
                    start: 1, end: 3, action: "exclude", reason: "secure_field_detected")],
                failClosed: [ReviewFailClosed(t: 2, surface: "event")]
            ),
            coverage: nil
        )
        let model = makeModel(loader: loader, controller: controller)

        await model.loadReviewData()
        model.startUpload()

        if case .uploading = model.state {} else {
            XCTFail("advisory flags must not gate Upload, got \(model.state)")
        }
    }

    // MARK: - U5: enriched envelope decode + raised timeout

    /// Happy path: a full v2 envelope carries the enriched fields onto
    /// ReviewData (events set, masked screenshots, redaction evidence, coverage).
    func testEnrichedEnvelopeCarriesAllFieldsToReady() async {
        let loader = FakeReviewDataLoader()
        loader.nextEnvelope = .init(
            ok: true,
            schemaVersion: 2,
            videoPath: "/tmp/video.mp4",
            eventsPath: "/tmp/rec-scrubbed/events_0000.jsonl",
            startedAt: 1700000000,
            durationSeconds: 30,
            videoPixfmtRemediated: false,
            error: nil,
            eventsPaths: [
                "/tmp/rec-scrubbed/events_0000.jsonl",
                "/tmp/rec-scrubbed/events_0001.jsonl",
            ],
            screenshots: ["/tmp/rec-scrubbed/screenshots/1.0.jpg"],
            redaction: ReviewRedaction(
                summary: ["EMAIL_ADDRESS": 2],
                markers: [ReviewMarker(t: 5, category: "secure_field_detected")],
                blockedIntervals: [ReviewBlockedInterval(
                    start: 1, end: 3, action: "exclude", reason: "blocked_app_exclude")],
                failClosed: [ReviewFailClosed(t: 9, surface: "event")]
            ),
            coverage: ReviewCoverage(
                videoLocalOnly: true, audioLocalOnly: true,
                transcriptUploadedScrubbed: false, screenshotsUploaded: true,
                allowedAppScreenshotPiiManualReview: true
            )
        )
        let model = makeModel(loader: loader)

        await model.loadReviewData()

        guard case .ready(let data) = model.state else {
            return XCTFail("expected ready, got \(model.state)")
        }
        XCTAssertEqual(data.eventsURLs.count, 2)
        XCTAssertEqual(data.screenshotURLs.map(\.path), ["/tmp/rec-scrubbed/screenshots/1.0.jpg"])
        XCTAssertEqual(data.redaction?.summary?["EMAIL_ADDRESS"], 2)
        XCTAssertEqual(data.redaction?.markers?.first?.category, "secure_field_detected")
        XCTAssertEqual(data.redaction?.blockedIntervals?.first?.end, 3)
        XCTAssertEqual(data.redaction?.failClosed?.first?.t, 9)
        XCTAssertEqual(data.coverage?.allowedAppScreenshotPiiManualReview, true)
    }

    /// A minimal envelope (the enriched fields absent) still reaches `.ready` —
    /// readiness is never gated on the new fields. eventsURLs falls back to the
    /// single primary path; screenshots/redaction/coverage default to empty/nil.
    func testMinimalEnvelopeWithoutEnrichedFieldsReachesReady() async {
        let model = makeModel()  // default fake omits the new fields

        await model.loadReviewData()

        guard case .ready(let data) = model.state else {
            return XCTFail("expected ready, got \(model.state)")
        }
        XCTAssertEqual(data.eventsURLs, [URL(fileURLWithPath: "/tmp/events.jsonl")])
        XCTAssertTrue(data.screenshotURLs.isEmpty)
        XCTAssertNil(data.redaction)
        XCTAssertNil(data.coverage)
    }

    /// The review-data shell-out timeout is raised well above the legacy 60s so
    /// the in-command NER scrub isn't SIGTERM'd mid-pass.
    func testReviewDataTimeoutIsRaisedAboveLegacy60s() {
        XCTAssertGreaterThanOrEqual(
            LiveReviewDataLoader.reviewDataTimeout, 600,
            "timeout must accommodate the in-command NER scrub on large recordings")
    }

    /// Pins the U3↔U5 JSON contract: snake_case keys decode, and an open-ended
    /// blocked interval (`end: null`) decodes to a nil `end`.
    func testDecodesEnrichedEnvelopeFromRawJSON() throws {
        let json = """
        {"ok": true, "schema_version": 2, "video_path": "/v.mp4",
         "events_path": "/s/events_0000.jsonl",
         "events_paths": ["/s/events_0000.jsonl"],
         "screenshots": ["/s/screenshots/1.0.jpg"],
         "redaction": {"summary": {"PERSON": 1},
                        "markers": [{"t": 2.5, "category": "policy_excluded_app"}],
                        "blocked_intervals": [{"start": 1.0, "end": null, "action": "exclude", "reason": "blocked_app_exclude"}],
                        "fail_closed": [{"t": 4.0, "surface": "event"}]},
         "coverage": {"video_local_only": true, "audio_local_only": true,
                       "transcript_uploaded_scrubbed": false, "screenshots_uploaded": true,
                       "allowed_app_screenshot_pii_manual_review": true},
         "started_at": null, "duration_seconds": null, "video_pixfmt_remediated": false,
         "timing_error": true}
        """
        let env = try JSONDecoder().decode(ReviewDataEnvelope.self, from: Data(json.utf8))
        XCTAssertEqual(env.schemaVersion, 2)
        XCTAssertEqual(env.eventsPaths?.count, 1)
        XCTAssertEqual(env.redaction?.summary?["PERSON"], 1)
        XCTAssertEqual(env.redaction?.markers?.first?.category, "policy_excluded_app")
        XCTAssertNil(env.redaction?.blockedIntervals?.first?.end, "null end → nil")
        XCTAssertEqual(env.coverage?.transcriptUploadedScrubbed, false)
        XCTAssertEqual(env.timingError, true)
    }

    // MARK: - Helpers

    private func makeModel(
        loader: ReviewDataLoader = FakeReviewDataLoader(),
        controller: UploadController? = nil,
        effects: ReviewWindowEffects = FakeReviewWindowEffects(),
        autoCloseSeconds: Double = 2.0
    ) -> ReviewWindowViewModel {
        // Default arguments can't call instance methods, so resolve the
        // registry-bound controller here when the caller didn't supply one.
        ReviewWindowViewModel(
            recordingName: "rec-001",
            uploadController: controller ?? makeController(),
            loader: loader,
            effects: effects,
            autoCloseSeconds: autoCloseSeconds
        )
    }
}
