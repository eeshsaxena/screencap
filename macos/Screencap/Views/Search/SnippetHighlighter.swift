import Foundation

// SCR-177 U3 — highlight the matched query terms inside a content/transcript
// snippet. Pure (Foundation-only, no SwiftUI) so it is unit-tested without a
// render. Each term is matched independently, case- and diacritic-insensitive;
// matched ranges get a bold inline-presentation intent (SwiftUI Text renders
// `.stronglyEmphasized` as bold), which survives List hover/selection in light
// and dark mode without color math. A term absent from the snippet simply
// highlights nothing — the daemon snippet is a bounded excerpt and legitimately
// may omit some terms of a multi-token query (R5: degrade, never error).
enum SnippetHighlighter {
    /// Nil-tolerant convenience: a `nil` snippet → an empty `AttributedString`.
    static func attributed(snippet: String?, terms: [String]) -> AttributedString {
        attributed(snippet ?? "", terms: terms)
    }

    static func attributed(_ text: String, terms: [String]) -> AttributedString {
        var result = AttributedString(text)
        guard !text.isEmpty else { return result }
        let cleanedTerms = terms.filter { !$0.isEmpty }
        guard !cleanedTerms.isEmpty else { return result }

        for term in cleanedTerms {
            var searchStart = text.startIndex
            while searchStart < text.endIndex,
                  let match = text.range(
                      of: term,
                      options: [.caseInsensitive, .diacriticInsensitive],
                      range: searchStart..<text.endIndex
                  ) {
                // Map the String range onto AttributedString indices by character
                // offset — both views are grapheme-based over the same text, so a
                // diacritic-insensitive match (e.g. "cafe" → "Café") stays aligned.
                let lowerOffset = text.distance(from: text.startIndex, to: match.lowerBound)
                let upperOffset = text.distance(from: text.startIndex, to: match.upperBound)
                let lower = result.index(result.startIndex, offsetByCharacters: lowerOffset)
                let upper = result.index(result.startIndex, offsetByCharacters: upperOffset)
                result[lower..<upper].inlinePresentationIntent = .stronglyEmphasized

                searchStart = match.upperBound > match.lowerBound ? match.upperBound : text.index(after: match.lowerBound)
            }
        }
        return result
    }
}
