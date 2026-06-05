import XCTest
@testable import ScreenCap

/// U8 redaction-summary tests. The transparency views themselves are
/// window-lifecycle / render surfaces (manual QA per the WindowGroup learning);
/// here we pin the pure `RedactionSummary` logic that drives them — the tally,
/// the protection framing, and the fail-closed / calm-state branches.
final class RedactionEvidenceViewTests: XCTestCase {

    private func redaction(
        summary: [String: Int]? = nil,
        markers: [ReviewMarker]? = nil,
        blocked: [ReviewBlockedInterval]? = nil,
        failClosed: [ReviewFailClosed]? = nil
    ) -> ReviewRedaction {
        ReviewRedaction(summary: summary, markers: markers, blockedIntervals: blocked, failClosed: failClosed)
    }

    func testHeadlineFramesCountsAsProtection() {
        let r = redaction(
            summary: ["EMAIL_ADDRESS": 3, "PERSON": 1],
            blocked: [
                ReviewBlockedInterval(start: 1, end: 2, action: "exclude", reason: "r"),
                ReviewBlockedInterval(start: 3, end: 4, action: "exclude", reason: "r"),
            ]
        )
        XCTAssertEqual(
            RedactionSummary.headline(r),
            "Protected before upload — removed 4 sensitive items, hid 2 segments."
        )
    }

    func testHeadlineSingularGrammar() {
        let r = redaction(
            summary: ["PERSON": 1],
            blocked: [ReviewBlockedInterval(start: 1, end: 2, action: "exclude", reason: "r")]
        )
        XCTAssertEqual(
            RedactionSummary.headline(r),
            "Protected before upload — removed 1 sensitive item, hid 1 segment."
        )
    }

    func testHeadlineNilWhenNothingEntityOrSegmentLevel() {
        XCTAssertNil(RedactionSummary.headline(nil))
        XCTAssertNil(RedactionSummary.headline(redaction(summary: [:], markers: [], blocked: [], failClosed: [])))
        // Fail-closed alone does NOT drive the protection headline (separate callout).
        XCTAssertNil(RedactionSummary.headline(redaction(failClosed: [ReviewFailClosed(t: 1, surface: "event")])))
    }

    func testFriendlyNamesMapKnownAndFallBack() {
        XCTAssertEqual(RedactionSummary.friendlyName("EMAIL_ADDRESS"), "Email")
        XCTAssertEqual(RedactionSummary.friendlyName("PERSON"), "Name")
        XCTAssertEqual(RedactionSummary.friendlyName("PHONE_NUMBER"), "Phone number")
        XCTAssertEqual(RedactionSummary.friendlyName("CUSTOM_THING"), "Custom Thing")
    }

    func testBreakdownSortedByCountDescending() {
        let r = redaction(summary: ["PERSON": 1, "EMAIL_ADDRESS": 3])
        let b = RedactionSummary.breakdown(r)
        XCTAssertEqual(b.map(\.label), ["Email", "Name"])
        XCTAssertEqual(b.map(\.count), [3, 1])
    }

    func testHasFailClosedReflectsPresence() {
        XCTAssertFalse(RedactionSummary.hasFailClosed(nil))
        XCTAssertFalse(RedactionSummary.hasFailClosed(redaction(failClosed: [])))
        XCTAssertTrue(RedactionSummary.hasFailClosed(redaction(failClosed: [ReviewFailClosed(t: 1, surface: "event")])))
    }

    func testIsEmptyDrivesCalmState() {
        XCTAssertTrue(RedactionSummary.isEmpty(nil))
        XCTAssertTrue(RedactionSummary.isEmpty(redaction(summary: [:], blocked: [], failClosed: [])))
        XCTAssertFalse(RedactionSummary.isEmpty(redaction(summary: ["X": 1])))
        // Fail-closed alone is NOT calm — the callout must show.
        XCTAssertFalse(RedactionSummary.isEmpty(redaction(failClosed: [ReviewFailClosed(t: 1, surface: "event")])))
    }
}
