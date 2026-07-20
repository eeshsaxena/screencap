import XCTest
@testable import Screencap

/// U6 — the New-recording sheet's pure decision layer: the open gate, the
/// honesty-substituted header caption (KTD-9), the mic-meter gate, and the
/// initial audio choice.
final class NewRecordingSheetPolicyTests: XCTestCase {

    // MARK: - canPresent (open gate)

    func testCanPresentOnlyWhenIdle() {
        XCTAssertTrue(NewRecordingSheetPolicy.canPresent(recorderState: .idle))
        XCTAssertFalse(NewRecordingSheetPolicy.canPresent(recorderState: .starting))
        XCTAssertFalse(NewRecordingSheetPolicy.canPresent(recorderState: .recording(elapsed: 5)))
        XCTAssertFalse(NewRecordingSheetPolicy.canPresent(recorderState: .stopping(quitting: false)))
    }

    // MARK: - headerCaption (KTD-9 honesty)

    /// `local` / `ask` / unknown keep the design's "stays on this Mac" — those
    /// recordings genuinely stay local until an explicit Review-window upload.
    func testHeaderStaysLocalForLocalAskAndUnknown() {
        XCTAssertEqual(NewRecordingSheetPolicy.headerCaption(uploadDefault: "local"), "stays on this Mac")
        XCTAssertEqual(NewRecordingSheetPolicy.headerCaption(uploadDefault: "ask"), "stays on this Mac")
        XCTAssertEqual(NewRecordingSheetPolicy.headerCaption(uploadDefault: nil), "stays on this Mac")
    }

    /// A cloud / both default flips the copy to a truthful non-"stays" phrasing —
    /// never falsely claim the recording stays local.
    func testHeaderDoesNotClaimLocalForCloudOrBoth() {
        for value in ["cloud", "both"] {
            let caption = NewRecordingSheetPolicy.headerCaption(uploadDefault: value)
            XCTAssertNotEqual(caption, "stays on this Mac", "\(value) must not claim 'stays'")
            XCTAssertFalse(caption.lowercased().contains("stays"))
        }
    }

    // MARK: - Meter gate

    func testMeterRunsOnlyWhenMicGranted() {
        XCTAssertTrue(NewRecordingSheetPolicy.shouldRunMeter(micGranted: true))
        XCTAssertFalse(NewRecordingSheetPolicy.shouldRunMeter(micGranted: false))
    }

    // MARK: - Initial audio choice

    func testInitialAudioDefaultsOnWhenAbsent() {
        XCTAssertTrue(NewRecordingSheetPolicy.initialAudioOn(audioDefault: nil))
        XCTAssertTrue(NewRecordingSheetPolicy.initialAudioOn(audioDefault: true))
        XCTAssertFalse(NewRecordingSheetPolicy.initialAudioOn(audioDefault: false))
    }
}
