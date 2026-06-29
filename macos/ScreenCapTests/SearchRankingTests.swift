import XCTest
@testable import ScreenCap

/// SCR-180 U1 — pins the blended relevance + recency ordering INVARIANTS of
/// `SearchRanking.rankBlended` (not exact blended float values, which are
/// tunable). The load-bearing property is the weight-dominance invariant: a
/// text hit always outranks a zero-relevance activity row regardless of recency.
final class SearchRankingTests: XCTestCase {

    private func item(
        _ id: String, _ stream: SearchResultItem.Stream, anchor: Int?, score: Double = 0
    ) -> SearchResultItem {
        SearchResultItem(
            id: id, stream: stream, recording: "rec", anchorMs: anchor,
            approximate: stream == .audio, score: score, snippet: nil, app: nil, title: nil
        )
    }

    private func ids(_ items: [SearchResultItem]) -> [String] { items.map(\.id) }

    // Happy path: a content (text) hit outranks a newer, unrelated activity row.
    func testContentHitBeatsNewerActivity() {
        let ranked = SearchRanking.rankBlended([
            item("activity", .activity, anchor: 3000),
            item("content", .screen, anchor: 2000, score: -2.0),
        ])
        XCTAssertEqual(ids(ranked), ["content", "activity"])
    }

    // Happy path: stronger bm25 (more negative) ranks above weaker; both above activity.
    func testStrongerBm25RanksAboveWeakerBothAboveActivity() {
        let ranked = SearchRanking.rankBlended([
            item("weak", .screen, anchor: 2500, score: -1.0),
            item("activity", .activity, anchor: 3000),
            item("strong", .screen, anchor: 2000, score: -3.0),
        ])
        XCTAssertEqual(ids(ranked), ["strong", "weak", "activity"])
    }

    // Edge: a lone content hit (min == max bm25) maps to the band top, ranks
    // above activity, and does not divide by zero.
    func testSingleContentHitMapsToBandTopNoDivideByZero() {
        let ranked = SearchRanking.rankBlended([
            item("activity", .activity, anchor: 9000),
            item("content", .screen, anchor: 1000, score: -1.5),
        ])
        XCTAssertEqual(ids(ranked), ["content", "activity"])
    }

    // Edge: a transcript (audio) text match outranks a newer activity row.
    func testTranscriptHitBeatsNewerActivity() {
        let ranked = SearchRanking.rankBlended([
            item("activity", .activity, anchor: 3000),
            item("audio", .audio, anchor: 1000),
        ])
        XCTAssertEqual(ids(ranked), ["audio", "activity"])
    }

    // Edge: anchored transcript ranks above unanchored; BOTH rank above a newer
    // activity row (unanchored keeps its text relevance, just loses recency).
    func testAnchoredAndUnanchoredTranscriptBothBeatActivity() {
        let ranked = SearchRanking.rankBlended([
            item("activity", .activity, anchor: 3000),
            item("audio-unanchored", .audio, anchor: nil),
            item("audio-anchored", .audio, anchor: 1000),
        ])
        XCTAssertEqual(ids(ranked), ["audio-anchored", "audio-unanchored", "activity"])
    }

    // Edge: with no relevance signal (all activity), the blend collapses to pure
    // recency — newest first — preserving the pre-SCR-180 behavior (R5).
    func testAllActivityIsPureRecency() {
        let ranked = SearchRanking.rankBlended([
            item("old", .activity, anchor: 1000),
            item("new", .activity, anchor: 3000),
            item("mid", .activity, anchor: 2000),
        ])
        XCTAssertEqual(ids(ranked), ["new", "mid", "old"])
    }

    // Edge: empty and single-item inputs are returned unchanged.
    func testEmptyAndSingleItem() {
        XCTAssertEqual(SearchRanking.rankBlended([]).count, 0)
        let one = [item("solo", .screen, anchor: 1000, score: -1.0)]
        XCTAssertEqual(ids(SearchRanking.rankBlended(one)), ["solo"])
    }

    // Determinism: items with identical blended score, anchor, and bm25 score
    // sort deterministically by id, and repeated calls produce identical output.
    func testDeterministicTiebreakById() {
        let input = [
            item("b", .screen, anchor: 2000, score: -1.0),
            item("a", .screen, anchor: 2000, score: -1.0),
        ]
        let first = SearchRanking.rankBlended(input)
        let second = SearchRanking.rankBlended(input)
        XCTAssertEqual(ids(first), ["a", "b"])
        XCTAssertEqual(first, second)
    }

    // Invariant: the WEAKEST content hit (relevance == textFloor) that is also
    // the OLDEST item still outranks the NEWEST activity row — the dominance
    // invariant `relevanceWeight * textFloor > recencyWeight` in action (R3).
    func testWeakestOldestTextHitBeatsNewestActivity() {
        let ranked = SearchRanking.rankBlended([
            item("activity-newest", .activity, anchor: 5000),
            item("content-strongest-newest", .screen, anchor: 4000, score: -5.0),
            item("content-weakest-oldest", .screen, anchor: 1000, score: -1.0),
        ])
        // Both content hits above activity; weakest-oldest still clears activity.
        XCTAssertEqual(ids(ranked), ["content-strongest-newest", "content-weakest-oldest", "activity-newest"])
    }
}
