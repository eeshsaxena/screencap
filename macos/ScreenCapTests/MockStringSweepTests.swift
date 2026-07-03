import XCTest
@testable import ScreenCap

/// U14 — the durable form of the plan's mock-string sweep: none of the design
/// prototype's sample data may ship in app sources (R4 — every rendered value
/// traces to a real backend). Scans every Swift file under `ScreenCap/`
/// (production only — tests legitimately use these tokens as formatter inputs)
/// for the prototype's marquee sample strings. Comment mentions count too:
/// keeping the tokens out of sources entirely is what makes the guard a plain
/// substring scan instead of a parser.
final class MockStringSweepTests: XCTestCase {

    /// Sample data lifted from `Screencap Prototype.dc.html` — recording names,
    /// people, and the hard-coded clock/duration labels its cards show.
    private let bannedMockStrings = [
        "Payroll walkthrough",
        "Payroll runs",
        "1:1 with John Meyer",
        "John Meyer",
        "Weekly 1:1\"",           // the chip literal; quoted to spare prose mentions
        "Tue 14:32",
        "\"04:32\"",
        "\"14:32\"",
        "Salesforce refunds",
        "Kickoff notes",
    ]

    func testPrototypeSampleStringsShipNowhereInAppSources() throws {
        let sourcesRoot = TestSourcePaths.macosFile("ScreenCap")
        let enumerator = FileManager.default.enumerator(
            at: sourcesRoot, includingPropertiesForKeys: nil
        )
        var scanned = 0
        while let url = enumerator?.nextObject() as? URL {
            guard url.pathExtension == "swift" else { continue }
            let text = try String(contentsOf: url, encoding: .utf8)
            scanned += 1
            for banned in bannedMockStrings {
                XCTAssertFalse(
                    text.contains(banned),
                    "\(url.lastPathComponent) contains the prototype mock string \(banned)"
                )
            }
        }
        // Guard the guard: a broken sources-root resolution would scan nothing
        // and pass vacuously.
        XCTAssertGreaterThan(scanned, 50, "expected to scan the app's Swift sources")
    }
}
