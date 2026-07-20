import XCTest
@testable import Screencap

// U6 — the no-terminal sweep (R10/R11, KTD-5). Sibling of
// `MockStringSweepTests`, but at the STRING-LITERAL level: a whole-file
// substring scan false-positives on comments (`CloudAuthController` documents
// the `screencap login` spawn) and identifiers, so the sweep extracts Swift
// string literals with a small scanner and bans terminal-instruction phrasing
// only where it could actually render.

/// Minimal single-pass Swift string-literal extractor. Handles:
/// - `//` line comments and nested `/* */` block comments (stripped BEFORE
///   extraction, so commented-out strings never count);
/// - regular `"..."` literals with escapes (`\"`, `\\`, ...);
/// - `\(...)` interpolation — contents are CODE and are excluded (nested
///   string literals inside an interpolation are skipped, not extracted);
/// - multiline `"""` literals (raw content; no indent stripping — irrelevant
///   for substring matching).
///
/// Raw string literals (`#"..."#`) are NOT parsed — the codebase has none
/// (verified by grep at U6 time) and `rawStringMarkers` guards that this
/// stays true: the sweep fails loudly rather than mis-scanning one.
enum SwiftStringLiteralScanner {

    struct Result {
        var literals: [String] = []
        /// Count of `#"` sequences seen in code position (raw-string opener).
        var rawStringMarkers = 0
    }

    static func scan(_ source: String) -> Result {
        let chars = Array(source)
        let n = chars.count
        var i = 0
        var result = Result()

        func peek(_ k: Int) -> Character? { i + k < n ? chars[i + k] : nil }

        while i < n {
            let c = chars[i]
            if c == "/", peek(1) == "/" {
                i += 2
                while i < n, chars[i] != "\n" { i += 1 }
                continue
            }
            if c == "/", peek(1) == "*" {
                i += 2
                var depth = 1
                while i < n, depth > 0 {
                    if chars[i] == "/", peek(1) == "*" { depth += 1; i += 2 }
                    else if chars[i] == "*", peek(1) == "/" { depth -= 1; i += 2 }
                    else { i += 1 }
                }
                continue
            }
            if c == "#", peek(1) == "\"" {
                result.rawStringMarkers += 1
                i += 2
                continue
            }
            if c == "\"" {
                let multiline = peek(1) == "\"" && peek(2) == "\""
                i += multiline ? 3 : 1
                result.literals.append(
                    consumeString(chars, &i, multiline: multiline)
                )
                continue
            }
            i += 1
        }
        return result
    }

    /// `i` points just past the opening delimiter; returns the literal's
    /// content (interpolations excluded) and leaves `i` past the close.
    private static func consumeString(
        _ chars: [Character], _ i: inout Int, multiline: Bool
    ) -> String {
        let n = chars.count
        var out = ""
        while i < n {
            let c = chars[i]
            if c == "\\" {
                guard i + 1 < n else { i += 1; continue }
                let next = chars[i + 1]
                if next == "(" {
                    // Interpolation: skip balanced parens; a nested string
                    // literal is consumed (and discarded) so a ")" inside it
                    // can't unbalance the count.
                    i += 2
                    var depth = 1
                    while i < n, depth > 0 {
                        let d = chars[i]
                        if d == "(" { depth += 1; i += 1 }
                        else if d == ")" { depth -= 1; i += 1 }
                        else if d == "\"" {
                            let inner = i + 2 < n
                                && chars[i + 1] == "\"" && chars[i + 2] == "\""
                            i += inner ? 3 : 1
                            _ = consumeString(chars, &i, multiline: inner)
                        } else { i += 1 }
                    }
                    continue
                }
                switch next {
                case "n": out.append("\n")
                case "t": out.append("\t")
                case "r": out.append("\r")
                case "0": out.append("\0")
                default: out.append(next) // \" \\ \' and future escapes
                }
                i += 2
                continue
            }
            if multiline {
                if c == "\"", i + 2 < n, chars[i + 1] == "\"", chars[i + 2] == "\"" {
                    i += 3
                    return out
                }
                out.append(c)
                i += 1
            } else {
                if c == "\"" { i += 1; return out }
                if c == "\n" { i += 1; return out } // malformed; bail safely
                out.append(c)
                i += 1
            }
        }
        return out
    }
}

/// The banned terminal-instruction phrasing (case-insensitive; R10/R11).
/// Deliberately NOT a bare "terminal" — identifiers and legitimate domain use
/// (`sawTerminalEvent`, terminal pipeline stage) would false-positive.
enum TerminalSweepPolicy {

    static let bannedPhrases = [
        "screencap login",
        "run `",
        "command line",
        "in the terminal",
        "in a terminal",
        "from the terminal",
    ]

    static func bannedHits(in literal: String) -> [String] {
        let lower = literal.lowercased()
        return bannedPhrases.filter { lower.contains($0) }
    }
}

final class TerminalStringSweepTests: XCTestCase {

    // MARK: - Grandfathered pre-existing literals (ratchet)

    /// Terminal-instruction literals that PRE-DATE the account-sheet plan, in
    /// surfaces outside its scope (recorder stop fallback, download stub,
    /// upload retry, privacy banner, agent-CLI connect guidance). The sweep
    /// bans everything else; this list may only SHRINK — the test fails if an
    /// entry stops matching (delete it here) or if any account-surface file
    /// ever appears in it.
    ///
    /// All six pre-existing sites were rewritten to app-native / informational
    /// phrasing (no terminal instruction) — see git history for the fix; this
    /// list is empty on purpose and should stay that way.
    private static let grandfathered: [(file: String, phrase: String)] = []

    // MARK: - 1+2: literal-level sweep + vacuity guards

    func testNoTerminalInstructionStringLiteralsInAppSources() throws {
        // The plan's own surface may never be grandfathered (R10/R11 are
        // absolute there) — guard the allowlist itself first.
        for entry in Self.grandfathered {
            XCTAssertFalse(
                entry.file.contains("Views/Account/")
                    || entry.file.contains("CloudAuthController"),
                "account-sheet surface may not carry grandfathered terminal copy: \(entry.file)"
            )
        }

        let sourcesRoot = TestSourcePaths.macosFile("Screencap")
        let enumerator = FileManager.default.enumerator(
            at: sourcesRoot, includingPropertiesForKeys: nil
        )
        var scannedFiles = 0
        var totalLiterals = 0
        var violations: [String] = []
        var coveredGrandfathers = Set<Int>()

        while let url = enumerator?.nextObject() as? URL {
            guard url.pathExtension == "swift" else { continue }
            let text = try String(contentsOf: url, encoding: .utf8)
            scannedFiles += 1
            let scanned = SwiftStringLiteralScanner.scan(text)
            XCTAssertEqual(
                scanned.rawStringMarkers, 0,
                "\(url.lastPathComponent) uses a raw string literal (#\"...\"#) — the sweep's scanner does not parse those; teach it before introducing them"
            )
            totalLiterals += scanned.literals.count
            for literal in scanned.literals {
                let hits = TerminalSweepPolicy.bannedHits(in: literal)
                guard !hits.isEmpty else { continue }
                let matching = Self.grandfathered.enumerated().filter {
                    url.path.hasSuffix($0.element.file)
                        && literal.contains($0.element.phrase)
                }
                if matching.isEmpty {
                    violations.append(
                        "\(url.lastPathComponent): banned \(hits) in literal \"\(literal)\""
                    )
                } else {
                    matching.forEach { coveredGrandfathers.insert($0.offset) }
                }
            }
        }

        XCTAssertTrue(
            violations.isEmpty,
            "terminal-instruction phrasing in rendered string literals (R10/R11):\n"
                + violations.joined(separator: "\n")
        )

        // Ratchet: a grandfathered literal that no longer exists must be
        // removed from the list, so the allowlist only shrinks.
        for (index, entry) in Self.grandfathered.enumerated()
        where !coveredGrandfathers.contains(index) {
            XCTFail(
                "grandfathered terminal copy no longer present — delete its entry: \(entry.file) / \"\(entry.phrase)\""
            )
        }

        // Guard the guard: an empty glob or a broken parser can't fake a pass.
        XCTAssertGreaterThanOrEqual(
            scannedFiles, 100, "expected to scan the app's Swift sources"
        )
        XCTAssertGreaterThanOrEqual(
            totalLiterals, 500, "expected to extract the app's string literals"
        )
    }

    // MARK: - 3: mapper + copy-catalog coverage (KTD-5 string half)

    func testEveryAccountErrorCopyMessageIsTerminalFree() {
        for errorCase in AccountErrorCopy.allCases {
            XCTAssertTrue(
                TerminalSweepPolicy.bannedHits(in: errorCase.message).isEmpty,
                "AccountErrorCopy.\(errorCase) message carries terminal-instruction phrasing: \"\(errorCase.message)\""
            )
        }
    }

    func testEveryAccountSheetRenderedStringIsTerminalFree() {
        for string in AccountSheetCopy.renderedStrings {
            XCTAssertTrue(
                TerminalSweepPolicy.bannedHits(in: string).isEmpty,
                "AccountSheetCopy renders terminal-instruction phrasing: \"\(string)\""
            )
        }
        // The paywall-aware confirm body is a function; audit both branches.
        for paywallEnabled in [true, false] {
            let body = AccountSheetCopy.signOutConfirmBody(paywallEnabled: paywallEnabled)
            XCTAssertTrue(TerminalSweepPolicy.bannedHits(in: body).isEmpty)
        }
    }

    // MARK: - 4: extractor self-test

    func testScannerExtractsLiteralsAndSkipsCommentsAndInterpolation() {
        let q3 = String(repeating: "\"", count: 3)
        // Built by concatenation so the fixture's escaping stays readable.
        let snippet = [
            "// comment says \"run `screencap login`\" and must vanish",
            "let a = \"plain one\"",
            "/* block \"in the terminal\" /* nested */ still comment */",
            "let b = \"with \\(interp(\"inner\")) middle\"",
            "let c = \(q3)",
            "multi \\(x) line",
            "with \"quote\" inside",
            "\(q3)",
            "let d = \"escaped \\\" quote\"",
        ].joined(separator: "\n")

        let result = SwiftStringLiteralScanner.scan(snippet)
        XCTAssertEqual(result.rawStringMarkers, 0)
        XCTAssertEqual(
            result.literals,
            [
                "plain one",
                "with  middle",
                "\nmulti  line\nwith \"quote\" inside\n",
                "escaped \" quote",
            ]
        )
        // Comment content stayed out; a planted banned phrase in a comment
        // must not trip the sweep.
        for literal in result.literals {
            XCTAssertTrue(TerminalSweepPolicy.bannedHits(in: literal).isEmpty)
        }
    }
}
