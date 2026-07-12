import XCTest
@testable import ScreenCap

/// U4 tests — the shared stub-guard + open/seek decision both "looking" entry
/// points (search results and the Recordings list) route through. Pure logic,
/// so testable without driving SwiftUI (the actual `openWindow` dispatch is
/// manual QA per SCR-55).
final class InspectRoutingTests: XCTestCase {

    func testStubRecordingRoutesToFriendlyDownloadMessage() {
        let decision = InspectRouting.decide(
            recording: "rec-2026-06-26-001", anchorMs: 1716800123_000, isStub: true
        )
        guard case .unavailable(let message) = decision else {
            return XCTFail("a stub must not open inspect, got \(decision)")
        }
        // The message names the recording and the recovery path (parity with
        // the Recordings-list guard), regardless of any seek the tap carried.
        XCTAssertTrue(message.contains("rec-2026-06-26-001"))
        XCTAssertTrue(message.contains("developer CLI"))
    }

    func testAnchoredNonStubOpensInspectWithSeek() {
        let decision = InspectRouting.decide(
            recording: "rec-A", anchorMs: 42_000, isStub: false
        )
        XCTAssertEqual(decision, .open(recording: "rec-A", seekMs: 42_000))
    }

    func testUnanchoredNonStubOpensInspectAtStart() {
        // No moment (Recordings-list open, or an unanchored audio hit) → no seek.
        let decision = InspectRouting.decide(
            recording: "rec-B", anchorMs: nil, isStub: false
        )
        XCTAssertEqual(decision, .open(recording: "rec-B", seekMs: nil))
    }
}
