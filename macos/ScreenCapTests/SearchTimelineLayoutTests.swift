import CoreGraphics
import XCTest
@testable import ScreenCap

// SCR-181 U1 — table-driven tests for the pure timeline geometry + clustering
// core. No SwiftUI render: placement, binning, and inverse hit-testing are pure
// math (mirrors TimelinePaneScrubTests). Fixed bounds/width keep every assertion
// deterministic.
final class SearchTimelineLayoutTests: XCTestCase {

    // A 24h day on a 1000pt axis: 1ms of wall-clock maps to a fixed x, so noon
    // (half the span) lands at x=500. Bounds chosen so ratios are round numbers.
    private let dayStart = 0
    private let dayEnd = 86_400_000          // 24h in ms
    private let width: CGFloat = 1000

    /// Build an anchored item at an absolute ms (the only field placement reads;
    /// the rest are inert but valid).
    private func item(_ ms: Int?, id: String = "x", stream: SearchResultItem.Stream = .activity) -> SearchResultItem {
        SearchResultItem(
            id: id, stream: stream, recording: "rec", anchorMs: ms,
            approximate: false, score: 0, snippet: nil, app: "Safari", title: "t"
        )
    }

    // MARK: - placeMarkers

    func testPlaceMarkersMapsTimeToXRatio() {
        // Covers AE1 — a day's hits become positioned markers on the axis.
        let markers = SearchTimelineLayout.placeMarkers(
            [item(0, id: "a"), item(43_200_000, id: "b"), item(86_400_000, id: "c")],
            startMs: dayStart, endMs: dayEnd, width: width
        )
        XCTAssertEqual(markers.count, 3)
        XCTAssertEqual(markers.first { $0.item.id == "a" }?.x ?? .nan, 0, accuracy: 0.01)
        XCTAssertEqual(markers.first { $0.item.id == "b" }?.x ?? .nan, 500, accuracy: 0.01)
        XCTAssertEqual(markers.first { $0.item.id == "c" }?.x ?? .nan, 1000, accuracy: 0.01)
    }

    func testPlaceMarkersClampsOutOfBounds() {
        let markers = SearchTimelineLayout.placeMarkers(
            [item(-1000, id: "before"), item(90_000_000, id: "after")],
            startMs: dayStart, endMs: dayEnd, width: width
        )
        XCTAssertEqual(markers.first { $0.item.id == "before" }?.x ?? .nan, 0, accuracy: 0.01)
        XCTAssertEqual(markers.first { $0.item.id == "after" }?.x ?? .nan, 1000, accuracy: 0.01)
    }

    func testPlaceMarkersSkipsUnanchored() {
        let markers = SearchTimelineLayout.placeMarkers(
            [item(nil, id: "u"), item(43_200_000, id: "b")],
            startMs: dayStart, endMs: dayEnd, width: width
        )
        XCTAssertEqual(markers.map(\.item.id), ["b"])
    }

    func testPlaceMarkersGuardsZeroWidthAndSpan() {
        // No NaN, no divide-by-zero (mirror TimelinePaneScrub guards).
        let zeroWidth = SearchTimelineLayout.placeMarkers(
            [item(43_200_000)], startMs: dayStart, endMs: dayEnd, width: 0
        )
        XCTAssertEqual(zeroWidth.first?.x, 0)
        let zeroSpan = SearchTimelineLayout.placeMarkers(
            [item(43_200_000)], startMs: 5, endMs: 5, width: width
        )
        XCTAssertEqual(zeroSpan.first?.x, 0)
        XCTAssertFalse(zeroWidth.contains { $0.x.isNaN })
        XCTAssertFalse(zeroSpan.contains { $0.x.isNaN })
    }

    func testPlaceMarkersEmpty() {
        XCTAssertTrue(
            SearchTimelineLayout.placeMarkers([], startMs: dayStart, endMs: dayEnd, width: width).isEmpty
        )
    }

    // MARK: - cluster

    func testTwoNearMarkersClusterThirdFarStaysSingle() {
        let markers = [
            SearchTimelineLayout.Marker(item: item(0, id: "a"), x: 100),
            SearchTimelineLayout.Marker(item: item(0, id: "b"), x: 104),  // within 8 of a
            SearchTimelineLayout.Marker(item: item(0, id: "c"), x: 400),  // far
        ]
        let nodes = SearchTimelineLayout.cluster(markers, thresholdPx: 8)
        XCTAssertEqual(nodes.count, 2)
        let cluster = nodes.first { $0.isCluster }
        XCTAssertEqual(cluster?.count, 2)
        XCTAssertEqual(Set(cluster?.items.map(\.id) ?? []), ["a", "b"])
        XCTAssertEqual(nodes.first { !$0.isCluster }?.items.first?.id, "c")
    }

    func testAllSameInstantCollapseToOneCluster() {
        let markers = (0..<5).map {
            SearchTimelineLayout.Marker(item: item(43_200_000, id: "i\($0)"), x: 500)
        }
        let nodes = SearchTimelineLayout.cluster(markers, thresholdPx: 8)
        XCTAssertEqual(nodes.count, 1)
        XCTAssertEqual(nodes.first?.count, 5)
        XCTAssertEqual(nodes.first?.x ?? 0, 500, accuracy: 0.01)
    }

    func testSingleMarkerIsSingleNode() {
        let nodes = SearchTimelineLayout.cluster(
            [SearchTimelineLayout.Marker(item: item(0, id: "only"), x: 300)], thresholdPx: 8
        )
        XCTAssertEqual(nodes.count, 1)
        XCTAssertFalse(nodes.first?.isCluster ?? true)
        XCTAssertEqual(nodes.first?.representative.id, "only")
    }

    func testClusterBoundsNodeCountAtDensity() {
        // R5a — a busy day (200 markers spread across the axis) must collapse to a
        // bounded, legible node count (≪ markers) at a realistic threshold.
        let dense = SearchTimelineLayout.placeMarkers(
            (0..<200).map { item($0 * (dayEnd / 200), id: "d\($0)") },
            startMs: dayStart, endMs: dayEnd, width: width
        )
        let nodes = SearchTimelineLayout.cluster(dense, thresholdPx: 12)
        XCTAssertLessThanOrEqual(nodes.count, Int(width / 12) + 1)
        XCTAssertLessThan(nodes.count, 200)
        // Every marker is accounted for exactly once across the nodes.
        XCTAssertEqual(nodes.reduce(0) { $0 + $1.count }, 200)
    }

    func testClusterZeroThresholdKeepsMarkersSeparate() {
        let markers = [
            SearchTimelineLayout.Marker(item: item(0, id: "a"), x: 100),
            SearchTimelineLayout.Marker(item: item(0, id: "b"), x: 104),
        ]
        let nodes = SearchTimelineLayout.cluster(markers, thresholdPx: 0)
        XCTAssertEqual(nodes.count, 2)
    }

    // MARK: - nodeAt

    func testNodeAtReturnsNearest() {
        let nodes = SearchTimelineLayout.cluster(
            [
                SearchTimelineLayout.Marker(item: item(0, id: "a"), x: 100),
                SearchTimelineLayout.Marker(item: item(0, id: "b"), x: 500),
            ],
            thresholdPx: 8
        )
        XCTAssertEqual(SearchTimelineLayout.nodeAt(120, in: nodes)?.representative.id, "a")
        XCTAssertEqual(SearchTimelineLayout.nodeAt(480, in: nodes)?.representative.id, "b")
        // Exactly between → the closer (lower) one; tie resolves deterministically.
        XCTAssertNotNil(SearchTimelineLayout.nodeAt(300, in: nodes))
    }

    func testNodeAtEmptyIsNil() {
        XCTAssertNil(SearchTimelineLayout.nodeAt(100, in: []))
    }

    // MARK: - dayBounds

    func testDayBoundsSpans24hFromLocalMidnight() {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "America/New_York")!
        // 2026-06-29 15:00 in epoch ms (value picked only to land mid-day).
        let anchor = Int(Date(timeIntervalSince1970: 1_782_500_000).timeIntervalSince1970 * 1000)
        let bounds = SearchTimelineLayout.dayBounds(forAnchorMs: anchor, calendar: cal)
        XCTAssertEqual(bounds.endMs - bounds.startMs, 86_400_000)
        XCTAssertLessThanOrEqual(bounds.startMs, anchor)
        XCTAssertLessThan(anchor, bounds.endMs)
    }
}
