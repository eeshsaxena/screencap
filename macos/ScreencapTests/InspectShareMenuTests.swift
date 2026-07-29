import Foundation
import XCTest
@testable import Screencap

/// SCR-299 U5 — which share actions the Inspect menu offers, as pure rules so
/// they are testable without a SwiftUI render (the extraction
/// `InspectShareAffordance` already established).
final class InspectShareMenuTests: XCTestCase {

    /// The pre-existing rule is untouched: the whole menu still waits for the
    /// recording to load. Restated here so a change to the link actions can't
    /// quietly weaken the gate the upload hand-off depends on (R9).
    func testMenuStillGatesOnRecordingHavingLoaded() {
        let ready = InspectData(
            videoURL: URL(fileURLWithPath: "/tmp/v.mp4"),
            eventsURLs: [],
            startedAt: 0,
            durationSeconds: 0,
            timingStatus: .ok,
            blockedIntervals: [],
            protectedIntervals: []
        )
        XCTAssertFalse(InspectShareAffordance.isEnabled(for: .preparing))
        XCTAssertFalse(InspectShareAffordance.isEnabled(for: .failed(message: "boom")))
        XCTAssertTrue(InspectShareAffordance.isEnabled(for: .ready(ready)))
    }

    /// AE3: a recording with no cloud copy offers no link actions at all,
    /// rather than showing them and failing when tapped.
    func testLinkActionsAreHiddenForALocalOnlyRecording() {
        XCTAssertFalse(
            InspectShareAffordance.showsLinkActions(for: makeSummary(uploaded: false))
        )
    }

    func testLinkActionsAreShownForAnUploadedRecording() {
        XCTAssertTrue(
            InspectShareAffordance.showsLinkActions(for: makeSummary(uploaded: true))
        )
    }

    /// Until the recordings index resolves this recording there is no basis to
    /// claim it is shareable — absence must read as not-yet, not as yes.
    func testUnresolvedRecordingOffersNoLinkActions() {
        XCTAssertFalse(InspectShareAffordance.showsLinkActions(for: nil))
    }

    /// The link gate is NARROWER than the menu gate. A local-only recording
    /// keeps its route to the consent window even though it has nothing to
    /// link to — gating the new actions must not disable the pre-existing
    /// upload path.
    func testLocalOnlyRecordingStillReachesTheUploadHandOff() {
        let ready = InspectData(
            videoURL: URL(fileURLWithPath: "/tmp/v.mp4"),
            eventsURLs: [],
            startedAt: 0,
            durationSeconds: 0,
            timingStatus: .ok,
            blockedIntervals: [],
            protectedIntervals: []
        )
        let localOnly = makeSummary(uploaded: false)

        XCTAssertTrue(InspectShareAffordance.isEnabled(for: .ready(ready)))
        XCTAssertFalse(InspectShareAffordance.showsLinkActions(for: localOnly))
    }

    /// A stub is uploaded-then-locally-deleted; its cloud copy is what a share
    /// reads, so the menu gate must not be the thing that excludes it.
    func testStubStillOffersLinkActions() {
        XCTAssertTrue(
            InspectShareAffordance.showsLinkActions(for: makeSummary(uploaded: true, isStub: true))
        )
    }

    private func makeSummary(uploaded: Bool, isStub: Bool = false) -> RecordingSummary {
        let payload: [String: Any] = [
            "name": "rec-1",
            "date": "2026-05-29",
            "duration": "00:01:00",
            "size_mb": "10",
            "has_audio": false,
            "transcribed": false,
            "uploaded": uploaded,
            "is_stub": isStub,
        ]
        let data = try! JSONSerialization.data(withJSONObject: payload)
        return try! JSONDecoder().decode(RecordingSummary.self, from: data)
    }
}
