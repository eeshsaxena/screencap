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

    func testDaemonTransportDoesNotBlockStartOnAppProcessPermissions() async {
        let previousSocket = getenv("SCREENCAP_DAEMON_SOCKET").map { String(cString: $0) }
        setenv("SCREENCAP_DAEMON_SOCKET", "/tmp/sc-missing-\(UUID().uuidString).sock", 1)
        defer {
            if let previousSocket {
                setenv("SCREENCAP_DAEMON_SOCKET", previousSocket, 1)
            } else {
                unsetenv("SCREENCAP_DAEMON_SOCKET")
            }
        }

        let permissions = PermissionController()
        permissions._testSetRequiredPermissionsGranted(false)

        let recorder = RecorderController()
        recorder.bindPermissions(permissions)
        recorder._testSetTransport(.daemon)

        recorder.start(name: "daemon-owned")

        XCTAssertEqual(recorder.state, .starting)
        XCTAssertNil(recorder.lastError)
        try? await Task.sleep(nanoseconds: 100_000_000)
    }

    func testCLIFallbackStillBlocksStartOnAppProcessPermissions() {
        let permissions = PermissionController()
        permissions._testSetRequiredPermissionsGranted(false)

        let recorder = RecorderController()
        recorder.bindPermissions(permissions)
        recorder._testSetTransport(.cliFallback)

        recorder.start(name: "cli-owned")

        XCTAssertEqual(recorder.state, .idle)
        XCTAssertEqual(recorder.lastError, RecorderController.requiredPermissionsErrorMessage)
    }

    func testDaemonTransportPermissionWatchdogIgnoresAppProcessPermissions() {
        let permissions = PermissionController()
        permissions._testSetRequiredPermissionsGranted(false)

        let recorder = RecorderController()
        recorder.bindPermissions(permissions)
        recorder._testSetTransport(.daemon)
        recorder._testSetPresentation(state: .recording(elapsed: 3))

        recorder._testCheckPermissionsDuringRecording()

        XCTAssertEqual(recorder.state, .recording(elapsed: 3))
        XCTAssertNil(recorder.lastError)
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
