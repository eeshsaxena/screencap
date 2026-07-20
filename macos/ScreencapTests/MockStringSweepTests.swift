import XCTest
@testable import Screencap

/// U14 — the durable form of the plan's mock-string sweep: none of the design
/// prototype's sample data may ship in app sources (R4 — every rendered value
/// traces to a real backend). Scans every Swift file under `Screencap/`
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
        let sourcesRoot = TestSourcePaths.macosFile("Screencap")
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

    // MARK: - AE4 — no recording identifier on browsing / citation surfaces

    /// U12 (R5/R13/R14, AE4) — the browsing + citation surfaces (Days, Tasks,
    /// Clips, Chat, the Recall palette) and the day-reached Review window must
    /// never RENDER a recording entity: no `rec-<timestamp>` name, recording
    /// card, per-recording chip, or window title keyed on the recording name.
    /// Pointers stay `(recording, timestamp_ms)` internally (KTD-1) — but the
    /// recording dir name is a storage key, not a user-facing string.
    ///
    /// A pure substring scan can't reason about semantics, so it pins the exact
    /// render anti-patterns this unit removed: SwiftUI `Text(...)` / window-title
    /// calls whose argument is the recording identifier. Cache keys and pointer
    /// resolution (`frameIndex.resolve(recording:)`, `thumbKey`) legitimately
    /// name the recording and are NOT matched — only user-facing renders are.
    private let bannedRecordingNameRenders = [
        // Chat source card (fixed to day + time).
        "Text(source.recording)",
        // Recall palette hit title (fixed: title/app/moment, never recording).
        "Text(item.recording)",
        "?? item.recording",
        // Review window title (fixed to a day + time via ReviewTitle).
        ".navigationTitle(recordingName)",
        "Text(recordingName)",
        // Generic recording-name renders that would leak a name onto a card.
        "Text(rec.name)",
        "Text(recording.name)",
    ]

    /// The browsing + citation surface source dirs/files (relative to
    /// `Screencap/`) plus the Review window — AE4's scope.
    private let ae4SurfacePaths = [
        "Views/Chat",
        "Views/Palette",
        "Views/Days",
        "Views/Tasks",
        "Views/Clips",
        "Views/Review/ReviewWindow.swift",
    ]

    func testNoRecordingNameRendersOnBrowsingAndCitationSurfaces() throws {
        let root = TestSourcePaths.macosFile("Screencap")
        var scanned = 0
        for relative in ae4SurfacePaths {
            let base = root.appendingPathComponent(relative)
            for url in swiftFiles(under: base) {
                let text = try String(contentsOf: url, encoding: .utf8)
                scanned += 1
                for banned in bannedRecordingNameRenders {
                    XCTAssertFalse(
                        text.contains(banned),
                        "\(url.lastPathComponent) renders a recording identifier "
                            + "(\(banned)) on a browsing/citation surface (AE4)"
                    )
                }
            }
        }
        // Guard the guard: a broken path resolution would scan nothing and pass
        // vacuously. The scoped surfaces always include at least Chat + palette +
        // Days + Clips + the Review window.
        XCTAssertGreaterThan(scanned, 5, "expected to scan the AE4 surface files")
    }

    /// Every `.swift` under `base` (which may itself be a file).
    private func swiftFiles(under base: URL) -> [URL] {
        var isDir: ObjCBool = false
        guard FileManager.default.fileExists(atPath: base.path, isDirectory: &isDir) else {
            return []
        }
        if !isDir.boolValue {
            return base.pathExtension == "swift" ? [base] : []
        }
        var out: [URL] = []
        let enumerator = FileManager.default.enumerator(at: base, includingPropertiesForKeys: nil)
        while let url = enumerator?.nextObject() as? URL {
            guard url.pathExtension == "swift" else { continue }
            out.append(url)
        }
        return out
    }
}
