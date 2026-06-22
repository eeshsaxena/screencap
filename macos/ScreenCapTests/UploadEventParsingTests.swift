import XCTest
@testable import ScreenCap

/// U7 parser tests. The contract is "tolerant decoder, ignore unknown
/// fields, drop non-JSON" — same shape as the recorder-event parser.
final class UploadEventParsingTests: XCTestCase {
    func testParsesUploadStartedPayload() {
        let line = #"{"type": "upload_started", "ts": 1700000000.0, "schema_version": 1, "total_bytes": 12345, "file_count": 3, "recording": "rec-001"}"#
        let event = UploadEventLine.parse(stderrLine: line)
        XCTAssertEqual(event?.type, "upload_started")
        XCTAssertEqual(event?.totalBytes, 12345)
        XCTAssertEqual(event?.fileCount, 3)
        XCTAssertEqual(event?.recording, "rec-001")
        XCTAssertEqual(event?.schemaVersion, 1)
    }

    func testParsesUploadFileDonePayload() {
        let line = #"{"type": "upload_file_done", "schema_version": 1, "name": "video.mp4", "bytes_uploaded_so_far": 999, "files_done": 1, "files_total": 3}"#
        let event = UploadEventLine.parse(stderrLine: line)
        XCTAssertEqual(event?.type, "upload_file_done")
        XCTAssertEqual(event?.name, "video.mp4")
        XCTAssertEqual(event?.filesDone, 1)
        XCTAssertEqual(event?.filesTotal, 3)
    }

    func testParsesUploadFinishedPayload() {
        let line = #"{"type": "upload_finished", "schema_version": 1, "uploaded": 3, "skipped": 0, "failed": 0, "total_bytes": 1024, "gcs_prefix": "gs://bucket/recording"}"#
        let event = UploadEventLine.parse(stderrLine: line)
        XCTAssertEqual(event?.type, "upload_finished")
        XCTAssertEqual(event?.uploaded, 3)
        XCTAssertEqual(event?.gcsPrefix, "gs://bucket/recording")
    }

    func testParsesUploadFailedPayload() {
        let line = #"{"type": "upload_failed", "schema_version": 1, "error": "interrupted"}"#
        let event = UploadEventLine.parse(stderrLine: line)
        XCTAssertEqual(event?.type, "upload_failed")
        XCTAssertEqual(event?.error, "interrupted")
    }

    /// SCR-158: a contended terminal-stage lock is surfaced as an
    /// `upload_busy` event (retryable, exit 0) the controller maps to `.busy`.
    func testParsesUploadBusyPayload() {
        let line = #"{"type": "upload_busy", "schema_version": 1, "recording": "rec-001", "retryable": true}"#
        let event = UploadEventLine.parse(stderrLine: line)
        XCTAssertEqual(event?.type, "upload_busy")
        XCTAssertEqual(event?.recording, "rec-001")
        XCTAssertEqual(event?.retryable, true)
    }

    func testBlankLineReturnsNil() {
        XCTAssertNil(UploadEventLine.parse(stderrLine: ""))
        XCTAssertNil(UploadEventLine.parse(stderrLine: "   "))
    }

    func testNonJSONLineReturnsNil() {
        XCTAssertNil(UploadEventLine.parse(stderrLine: "Uploading: 50% complete"))
    }

    /// Lines that look like JSON but don't have a `type` key are unparseable
    /// — the type discriminates the event, so a payload without one is not
    /// a valid event.
    func testJSONLineWithoutTypeReturnsNil() {
        XCTAssertNil(UploadEventLine.parse(stderrLine: #"{"only": "data"}"#))
    }

    /// Unknown / future fields decode-and-ignore so a Python-side schema
    /// extension doesn't brick the Swift parser.
    func testUnknownFieldsAreIgnored() {
        let line = #"{"type": "upload_started", "schema_version": 1, "file_count": 1, "future_field": "some value"}"#
        let event = UploadEventLine.parse(stderrLine: line)
        XCTAssertEqual(event?.type, "upload_started")
        XCTAssertEqual(event?.fileCount, 1)
    }
}
