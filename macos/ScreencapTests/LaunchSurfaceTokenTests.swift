import XCTest
@testable import Screencap

/// U4 (SCR-197) — the "no inline color literals remain on the launch surfaces"
/// guarantee is the only deterministic, automatable check for the application
/// pass: the repo's SwiftUI test host cannot read a `Color` or a shape back, so
/// color/shape *correctness* is the manual light/dark gate. This source-level
/// guard asserts the four launch-visible surfaces reference roles, not the
/// overloaded `.red` / `.orange` / `.yellow` / `.green` / `.blue` literals — and
/// that the retired menubar attention pip stays gone.
final class LaunchSurfaceTokenTests: XCTestCase {

    /// The four launch-visible surfaces (R8), relative to the macOS source root.
    private let surfaces = [
        "Screencap/ScreencapApp.swift",
        "Screencap/Views/MainWindow.swift",
        "Screencap/Views/Privacy/PermissionSetupTakeover.swift",
        "Screencap/Views/Onboarding/OnboardingPermissionsStep.swift",
    ]

    /// Color literals that R7 retires. `.primary` / `.secondary` are intentionally
    /// allowed — they are adaptive system semantics, not overloaded hues.
    private let bannedColorPatterns = [
        #"Color\.(red|orange|yellow|green|blue)\b"#,
        #"\.tint\(\.(red|orange|yellow|green|blue)\)"#,
        #"\.foregroundStyle\(\.(red|orange|yellow|green|blue)\)"#,
        #"\.background\(\.(red|orange|yellow|green|blue)"#,
        #"\.(fill|stroke)\(\.(red|orange|yellow|green|blue)\)"#,
        #"return \.(red|orange|yellow|green|blue)\b"#,
    ]

    private func source(_ relativePath: String) throws -> String {
        try String(contentsOf: TestSourcePaths.macosFile(relativePath), encoding: .utf8)
    }

    func testLaunchSurfacesContainNoBannedColorLiterals() throws {
        for surface in surfaces {
            let text = try source(surface)
            for pattern in bannedColorPatterns {
                let regex = try NSRegularExpression(pattern: pattern)
                let matches = regex.numberOfMatches(
                    in: text, range: NSRange(text.startIndex..., in: text)
                )
                XCTAssertEqual(
                    matches, 0,
                    "\(surface) still contains a banned color literal matching /\(pattern)/"
                )
            }
        }
    }

    func testMenubarAttentionPipIsRetired() throws {
        let text = try source("Screencap/ScreencapApp.swift")
        XCTAssertFalse(
            text.contains("privacyAttention"),
            "the orange menubar privacy-attention pip should be retired (R7)"
        )
    }
}
