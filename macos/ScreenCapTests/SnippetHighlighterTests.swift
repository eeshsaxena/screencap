import Foundation
import XCTest
@testable import ScreenCap

// SCR-177 U3 — each query term is bolded in the snippet (case- and
// diacritic-insensitive), absent terms highlight nothing, and nil/empty inputs
// degrade to plain text (R5). Asserts the applied attribute, not just ranges.
final class SnippetHighlighterTests: XCTestCase {

    /// The substrings carrying the bold (stronglyEmphasized) inline intent, in
    /// text order.
    private func boldedSubstrings(_ attributed: AttributedString) -> [String] {
        attributed.runs.compactMap { run in
            run.inlinePresentationIntent?.contains(.stronglyEmphasized) == true
                ? String(attributed[run.range].characters)
                : nil
        }
    }

    func testSingleTermBolded() {
        let attributed = SnippetHighlighter.attributed("error in the vendor portal", terms: ["vendor"])
        XCTAssertEqual(boldedSubstrings(attributed), ["vendor"])
    }

    func testMultipleTermsEachBolded() {
        let attributed = SnippetHighlighter.attributed("salesforce refund error", terms: ["salesforce", "error"])
        XCTAssertEqual(boldedSubstrings(attributed), ["salesforce", "error"])
    }

    func testMultiTokenQueryHighlightsOnlyPresentTerms() {
        let attributed = SnippetHighlighter.attributed("the vendor portal", terms: ["vendor", "missing"])
        XCTAssertEqual(boldedSubstrings(attributed), ["vendor"])
    }

    func testAbsentTermLeavesTextPlain() {
        let attributed = SnippetHighlighter.attributed("hello world", terms: ["xyz"])
        XCTAssertTrue(boldedSubstrings(attributed).isEmpty)
        XCTAssertEqual(String(attributed.characters), "hello world")
    }

    func testCaseAndDiacriticInsensitiveMatch() {
        let attributed = SnippetHighlighter.attributed("Visited the Café today", terms: ["cafe"])
        XCTAssertEqual(boldedSubstrings(attributed), ["Café"])
    }

    func testRepeatedOccurrencesEachBolded() {
        let attributed = SnippetHighlighter.attributed("go go go", terms: ["go"])
        XCTAssertEqual(boldedSubstrings(attributed), ["go", "go", "go"])
    }

    func testEmptyAndNilInputsDegradeToPlain() {
        XCTAssertTrue(boldedSubstrings(SnippetHighlighter.attributed("hello", terms: [])).isEmpty)
        XCTAssertEqual(String(SnippetHighlighter.attributed("", terms: ["x"]).characters), "")
        XCTAssertEqual(String(SnippetHighlighter.attributed(snippet: nil, terms: ["x"]).characters), "")
    }
}
