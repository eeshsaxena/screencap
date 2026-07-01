import Foundation
import XCTest
@testable import ScreenCap

// SCR-187 — `ResultRow.makePrimaryText` is the per-stream dispatch relocated out
// of a `body`-read computed var so the highlight is computed exactly once in
// `init`. These lock the invariant that moved: activity rows are NEVER
// highlighted (their lead line is the app name, not a searchable snippet), while
// screen/audio rows bold the matched query terms. The highlighter's own edge
// cases live in `SnippetHighlighterTests`.
final class ResultRowPrimaryTextTests: XCTestCase {

    /// Substrings carrying the bold (stronglyEmphasized) inline intent, in order.
    private func boldedSubstrings(_ attributed: AttributedString) -> [String] {
        attributed.runs.compactMap { run in
            run.inlinePresentationIntent?.contains(.stronglyEmphasized) == true
                ? String(attributed[run.range].characters)
                : nil
        }
    }

    private func item(
        stream: SearchResultItem.Stream,
        snippet: String? = nil,
        app: String? = nil
    ) -> SearchResultItem {
        SearchResultItem(
            id: "id",
            stream: stream,
            recording: "rec",
            anchorMs: 0,
            approximate: false,
            score: 1,
            snippet: snippet,
            app: app,
            title: "title"
        )
    }

    func testActivityRowNeverHighlightsEvenWhenTermMatchesApp() {
        let attributed = ResultRow.makePrimaryText(
            item: item(stream: .activity, app: "Vendor Portal"),
            queryTerms: ["vendor"]
        )
        XCTAssertEqual(String(attributed.characters), "Vendor Portal")
        XCTAssertTrue(boldedSubstrings(attributed).isEmpty)
    }

    func testScreenRowHighlightsMatchedTermsInSnippet() {
        let attributed = ResultRow.makePrimaryText(
            item: item(stream: .screen, snippet: "error in the vendor portal"),
            queryTerms: ["vendor"]
        )
        XCTAssertEqual(boldedSubstrings(attributed), ["vendor"])
    }

    func testAudioRowHighlightsMatchedTermsInSnippet() {
        let attributed = ResultRow.makePrimaryText(
            item: item(stream: .audio, snippet: "heard the vendor mention it"),
            queryTerms: ["vendor"]
        )
        XCTAssertEqual(boldedSubstrings(attributed), ["vendor"])
    }
}
