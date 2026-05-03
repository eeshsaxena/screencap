import XCTest
@testable import ScreenCap

@MainActor
final class RecorderControllerTests: XCTestCase {
    func testMatrixDisclosureEventIsCapturedForSwiftUIPresentation() {
        let recorder = RecorderController()

        recorder._testHandleStderrLine(
            #"{"type":"matrix_disclosure_required","schema_version":1,"changes":["chat_email_calendar_video_call_mask_window"],"opt_out_command_examples":["screencap settings privacy exclude_apps add com.openai.chat"]}"#
        )

        XCTAssertEqual(
            recorder.matrixDisclosure?.changes,
            ["chat_email_calendar_video_call_mask_window"]
        )
        XCTAssertEqual(
            recorder.matrixDisclosure?.optOutCommandExamples,
            ["screencap settings privacy exclude_apps add com.openai.chat"]
        )
    }

    func testDismissMatrixDisclosureClearsPresentationState() {
        let recorder = RecorderController()

        recorder._testHandleStderrLine(
            #"{"type":"matrix_disclosure_required","schema_version":1,"changes":["ai_assistant_browser_unverified"],"opt_out_command_examples":[]}"#
        )
        recorder.dismissMatrixDisclosure()

        XCTAssertNil(recorder.matrixDisclosure)
    }

    func testMissingPermissionsMessageNamesEveryRequiredPermission() {
        XCTAssertEqual(
            RecorderController.requiredPermissionsErrorMessage,
            "Grant Screen Recording, Accessibility, and Input Monitoring permissions before recording."
        )
    }

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
