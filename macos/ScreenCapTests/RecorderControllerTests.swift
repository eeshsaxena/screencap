import XCTest
@testable import ScreenCap

@MainActor
final class RecorderControllerTests: XCTestCase {
    func testForceStoppedWarningSurvivesCleanProcessTermination() {
        let recorder = RecorderController()

        recorder._testSetPresentation(state: .stopping(quitting: false))
        recorder._testHandleStderrLine(
            #"{"type":"recording_finalized","schema_version":1,"force_stopped":true}"#
        )
        recorder._testHandleProcessTerminated(exitCode: 0)

        XCTAssertEqual(
            recorder.lastError,
            "Recording stopped, but some data may not have uploaded. Run `screencap upload` to retry."
        )
    }

    func testRecorderErrorMessageCarriesSharedRecorderWarningText() {
        let view = RecorderErrorMessage(message: "Disk is full — recording stopped.")

        XCTAssertEqual(view.message, "Disk is full — recording stopped.")
    }
}
