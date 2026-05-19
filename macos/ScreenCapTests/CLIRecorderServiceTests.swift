import XCTest
@testable import ScreenCap

/// Parser-level coverage for the CLI service. The full
/// spawn → stderr → onEvent integration is exercised end-to-end by
/// `RecorderControllerTests` via `_testHandleStderrLine`.
final class CLIRecorderServiceTests: XCTestCase {
    func testParsesValidEventLine() {
        let event = RecorderEventLine.parse(
            stderrLine: #"{"type":"started","schema_version":1,"cursor":3,"ts":12.0}"#
        )

        XCTAssertEqual(event?.type, "started")
        XCTAssertEqual(event?.schemaVersion, 1)
        XCTAssertEqual(event?.cursor, 3)
    }

    func testParsesForceStoppedFlagOnFinalizedEvent() {
        let event = RecorderEventLine.parse(
            stderrLine: #"{"type":"recording_finalized","schema_version":1,"force_stopped":true}"#
        )

        XCTAssertEqual(event?.type, "recording_finalized")
        XCTAssertEqual(event?.forceStopped, true)
    }

    func testParsesMatrixDisclosureEvent() {
        let event = RecorderEventLine.parse(
            stderrLine: #"{"type":"matrix_disclosure_required","schema_version":1,"changes":["chat_email"],"opt_out_command_examples":["screencap settings privacy exclude"]}"#
        )

        XCTAssertEqual(event?.type, "matrix_disclosure_required")
        XCTAssertEqual(event?.changes, ["chat_email"])
        XCTAssertEqual(event?.optOutCommandExamples, ["screencap settings privacy exclude"])
    }

    func testReturnsNilForBlankLine() {
        XCTAssertNil(RecorderEventLine.parse(stderrLine: ""))
        XCTAssertNil(RecorderEventLine.parse(stderrLine: "   "))
    }

    func testReturnsNilForNonJSONLine() {
        XCTAssertNil(RecorderEventLine.parse(stderrLine: "INFO: starting recording engine"))
        XCTAssertNil(RecorderEventLine.parse(stderrLine: "[2026-05-19] starting"))
    }

    func testReturnsNilForMalformedJSON() {
        XCTAssertNil(RecorderEventLine.parse(stderrLine: "{type"))
        XCTAssertNil(RecorderEventLine.parse(stderrLine: "{\"type\":"))
    }

    func testToleratesLeadingAndTrailingWhitespace() {
        let event = RecorderEventLine.parse(
            stderrLine: "   {\"type\":\"stopped\",\"schema_version\":1}   \n"
        )

        XCTAssertEqual(event?.type, "stopped")
    }

    func testUnknownEventTypeStillDecodesSoStateMachineCanIgnoreIt() {
        // Unknown event types are not the parser's concern — they decode and
        // the state machine logs and drops them.
        let event = RecorderEventLine.parse(
            stderrLine: #"{"type":"newly_added_event","schema_version":1}"#
        )

        XCTAssertEqual(event?.type, "newly_added_event")
    }
}
