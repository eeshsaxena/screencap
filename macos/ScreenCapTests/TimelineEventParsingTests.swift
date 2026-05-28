import XCTest
@testable import ScreenCap

/// U6 parser tests — the timeline pane is only useful if event decoding is
/// tolerant (mirrors `RecorderEventLine`'s drift-resilient shape) and the
/// relative-time axis is correctly derived from `started_at`.
final class TimelineEventParsingTests: XCTestCase {
    private let startedAt: Double = 1_700_000_000.0

    func testParsesAllEventsInOrder() {
        let jsonl = """
        {"_meta": true, "format": "events.jsonl", "version": 1}
        {"type": "mouse.click", "timestamp": 1700000001.0}
        {"type": "key.type", "timestamp": 1700000002.5}
        {"type": "window.switch", "timestamp": 1700000005.0}
        {"type": "screen.frame", "timestamp": 1700000007.0}
        {"type": "mouse.scroll", "timestamp": 1700000010.0}
        """

        let events = TimelineEventParser.parse(jsonl: jsonl, recordingStartedAt: startedAt)

        XCTAssertEqual(events.count, 5)
        XCTAssertEqual(events.map(\.relativeSeconds), [1.0, 2.5, 5.0, 7.0, 10.0])
        XCTAssertEqual(events.map(\.type), [
            "mouse.click", "key.type", "window.switch", "screen.frame", "mouse.scroll",
        ])
    }

    func testMetaOnlyFileReturnsEmpty() {
        let jsonl = #"{"_meta": true, "format": "events.jsonl", "version": 1}"#
        let events = TimelineEventParser.parse(jsonl: jsonl, recordingStartedAt: startedAt)
        XCTAssertEqual(events, [])
    }

    /// Unknown event types must round-trip into the `.other` bucket so the
    /// timeline keeps rendering even when the recorder adds a new event
    /// type the Swift side hasn't been taught about.
    func testUnknownEventTypeFallsIntoOtherBucket() {
        let jsonl = """
        {"_meta": true}
        {"type": "experimental.future_event_v2", "timestamp": 1700000003.0}
        """
        let events = TimelineEventParser.parse(jsonl: jsonl, recordingStartedAt: startedAt)
        XCTAssertEqual(events.count, 1)
        XCTAssertEqual(events.first?.category, .other)
    }

    func testCategoriesAreDerivedFromTypePrefix() {
        XCTAssertEqual(TimelineEvent.Category.from(eventType: "mouse.click"), .mouse)
        XCTAssertEqual(TimelineEvent.Category.from(eventType: "key.down"), .key)
        XCTAssertEqual(TimelineEvent.Category.from(eventType: "window.switch"), .window)
        XCTAssertEqual(TimelineEvent.Category.from(eventType: "screen.frame"), .screen)
        XCTAssertEqual(TimelineEvent.Category.from(eventType: "audio.chunk"), .other)
    }

    func testMalformedLineInMiddleIsSkipped() {
        let jsonl = """
        {"_meta": true}
        {"type": "mouse.click", "timestamp": 1700000001.0}
        {not a json line at all}
        {"type": "key.type", "timestamp": 1700000003.0}
        """
        let events = TimelineEventParser.parse(jsonl: jsonl, recordingStartedAt: startedAt)
        XCTAssertEqual(events.map(\.relativeSeconds), [1.0, 3.0])
    }

    /// Events that arrive before the recording-start timestamp (clock skew,
    /// rounding, etc.) clamp to 0 so they still render at the timeline's
    /// leading edge instead of off-screen to the left.
    func testNegativeRelativeTimestampClampsToZero() {
        let jsonl = """
        {"_meta": true}
        {"type": "screen.frame", "timestamp": 1699999999.0}
        """
        let events = TimelineEventParser.parse(jsonl: jsonl, recordingStartedAt: startedAt)
        XCTAssertEqual(events.first?.relativeSeconds, 0)
    }

    func testOutOfOrderTimestampsAreSortedAscending() {
        let jsonl = """
        {"_meta": true}
        {"type": "mouse.click", "timestamp": 1700000010.0}
        {"type": "mouse.click", "timestamp": 1700000003.0}
        {"type": "mouse.click", "timestamp": 1700000007.0}
        """
        let events = TimelineEventParser.parse(jsonl: jsonl, recordingStartedAt: startedAt)
        XCTAssertEqual(events.map(\.relativeSeconds), [3.0, 7.0, 10.0])
    }

    /// Per the plan: "extremely long recording (e.g., 1 hour) → parser
    /// handles tens of thousands of events without blocking". This test
    /// exercises throughput on the parse path itself — the SwiftUI
    /// off-main hop is the caller's responsibility (U8 uses a background
    /// `Task`). The throughput target is well under a second for 10k
    /// events on any modern Mac; the assertion just makes sure we don't
    /// regress into something quadratic.
    func testThousandsOfEventsParseInReasonableTime() {
        var lines = [#"{"_meta": true}"#]
        for i in 0..<10_000 {
            let ts = startedAt + Double(i) * 0.36
            lines.append(#"{"type": "mouse.move", "timestamp": \#(ts)}"#)
        }
        let jsonl = lines.joined(separator: "\n")

        let start = Date()
        let events = TimelineEventParser.parse(jsonl: jsonl, recordingStartedAt: startedAt)
        let elapsed = Date().timeIntervalSince(start)

        XCTAssertEqual(events.count, 10_000)
        XCTAssertLessThan(elapsed, 2.0, "parser regressed into something pathological")
    }

    func testFileReadFailureReturnsEmptyRatherThanThrowing() {
        let url = URL(fileURLWithPath: "/tmp/screencap-timeline-does-not-exist.jsonl")
        let events = TimelineEventParser.parse(url: url, recordingStartedAt: startedAt)
        XCTAssertEqual(events, [])
    }
}
