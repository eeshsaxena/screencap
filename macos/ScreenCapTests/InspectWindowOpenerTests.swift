import XCTest
@testable import ScreenCap

/// U3 tests — pin the `InspectWindowOpener` behavior + the share-affordance
/// gate so a future SDK regression or bridge refactor is caught early. The
/// actual SwiftUI `WindowGroup` materialization is not driven from XCTest
/// (SCR-55); the opener closure and the pure gate are the seams we can observe.
@MainActor
final class InspectWindowOpenerTests: XCTestCase {
    override func tearDown() {
        InspectWindowOpener.shared.openInspect = nil
        InspectWindowOpener.shared.pendingSeekMs = [:]
        super.tearDown()
    }

    func testOpenForwardsRecordingNameToRegisteredClosure() {
        var receivedNames: [String] = []
        InspectWindowOpener.shared.openInspect = { receivedNames.append($0) }

        let opened = InspectWindowOpener.shared.open(recordingName: "rec-2026-06-26-001")

        XCTAssertTrue(opened)
        XCTAssertEqual(receivedNames, ["rec-2026-06-26-001"])
    }

    func testOpeningDistinctNamesInvokesClosureOncePerCall() {
        var receivedNames: [String] = []
        InspectWindowOpener.shared.openInspect = { receivedNames.append($0) }

        InspectWindowOpener.shared.open(recordingName: "rec-A")
        InspectWindowOpener.shared.open(recordingName: "rec-B")

        XCTAssertEqual(receivedNames, ["rec-A", "rec-B"])
    }

    /// The opener layer does NOT dedupe — repeat calls dispatch repeat
    /// invocations into SwiftUI, which decides per-platform whether to focus the
    /// existing window or open a new one. Pinning this catches an accidental
    /// dedup at this layer that would regress the multi-window contract (R7).
    func testRepeatedOpenForSameNameDispatchesEachCall() {
        var callCount = 0
        InspectWindowOpener.shared.openInspect = { _ in callCount += 1 }

        InspectWindowOpener.shared.open(recordingName: "rec-X")
        InspectWindowOpener.shared.open(recordingName: "rec-X")
        InspectWindowOpener.shared.open(recordingName: "rec-X")

        XCTAssertEqual(callCount, 3)
    }

    func testOpenWithoutRegisteredClosureIsSafeNoOpAndReportsFailure() {
        InspectWindowOpener.shared.openInspect = nil

        let opened = InspectWindowOpener.shared.open(recordingName: "rec-not-ready")

        XCTAssertFalse(opened)
    }

    /// The out-of-band seek delivery (SCR-174): a caller sets `pendingSeekMs`
    /// before opening so the window stays keyed on the recording name. Pin the
    /// dictionary semantics the `InspectWindow` read-and-clear depends on.
    func testPendingSeekMsCarriesPerRecordingTarget() {
        InspectWindowOpener.shared.pendingSeekMs["rec-seek"] = 1716800123_456

        XCTAssertEqual(InspectWindowOpener.shared.pendingSeekMs["rec-seek"], 1716800123_456)
        XCTAssertNil(InspectWindowOpener.shared.pendingSeekMs["rec-unset"])
    }

    /// Ensures the published scene id and the opener-bridge id agree — a typo on
    /// either side would silently break the search/row → inspect-window dispatch.
    func testInspectWindowIDConstantIsStable() {
        XCTAssertEqual(InspectWindowID, "inspect")
    }

    // MARK: - Share/Upload hand-off gate

    /// The hand-off is reachable ONLY in `.ready` — never during preparing/
    /// failed, so a rapid click can't open the consent window for a recording
    /// that just failed to load locally (R9).
    func testShareAffordanceEnabledOnlyWhenReady() {
        let ready = InspectData(
            videoURL: URL(fileURLWithPath: "/tmp/v.mp4"),
            eventsURLs: [],
            startedAt: 0,
            durationSeconds: 0,
            timingStatus: .ok
        )
        XCTAssertFalse(InspectShareAffordance.isEnabled(for: .preparing))
        XCTAssertFalse(InspectShareAffordance.isEnabled(for: .failed(message: "boom")))
        XCTAssertTrue(InspectShareAffordance.isEnabled(for: .ready(ready)))
    }
}
