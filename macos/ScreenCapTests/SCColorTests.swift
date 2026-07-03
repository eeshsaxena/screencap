import AppKit
import SwiftUI
import XCTest
@testable import ScreenCap

/// U1 (SCR-197) — the color-role tokens are a pure asset-catalog mapping, so they
/// are directly assertable: every authored role resolves in light *and* dark, the
/// high-contrast text/accent variants actually diverge, and the two accessibility
/// invariants (amber recording carries legible dark text; the error fg/surface
/// split can't re-collapse) hold at the authored values.
final class SCColorTests: XCTestCase {

    // The app bundle owns the asset catalog; the test runner does not. Resolve
    // against the same bundle the `Color` extension uses.
    private let bundle = Color.scBundle

    private let light = NSAppearance(named: .aqua)!
    private let dark = NSAppearance(named: .darkAqua)!

    /// Resolve a named catalog color to concrete sRGB components under a given
    /// appearance. The `NSColor(named:)` lookup binds its appearance at creation
    /// time, so it must be evaluated *inside* the appearance block for the
    /// high-contrast / dark variants to resolve (creating it outside captures the
    /// process's current appearance instead).
    private func resolve(_ name: String, _ appearance: NSAppearance) -> NSColor? {
        var out: NSColor?
        appearance.performAsCurrentDrawingAppearance {
            out = NSColor(named: name, bundle: bundle)?.usingColorSpace(.sRGB)
        }
        return out
    }

    /// Every authored role + the global accent.
    private let authoredRoles = [
        "AccentColor",
        "SCRecording",
        "SCErrorFg",
        "SCErrorSurface",
        "SCSuccessFg",
        "SCSurface",
        "SCSurfaceElevated",
        "SCTextPrimary",
        "SCTextSecondary",
        "SCOnAccent",
        "SCDisabledOnAccentFg",
    ]

    /// The prototype warm palette (U1). Authored as universal color sets — a single
    /// fixed warm aesthetic, no light/dark/high-contrast split — so each resolves
    /// under *both* appearances (that is the whole contract for these; high-contrast
    /// divergence is deliberately NOT authored here, per the plan).
    private let warmPaletteRoles = [
        "SCPaper", "SCCanvas", "SCFillSubtle", "SCBorderWarm",
        "SCInk", "SCInkSecondary", "SCInkMuted", "SCInkFaint",
        "SCTeal", "SCTealHover", "SCTealSoft",
        "SCAmber", "SCAmberText", "SCAmberHUD", "SCRust",
        "SCDarkCanvas", "SCHUDSurface", "SCHUDSurfaceRaised", "SCHUDMuted",
        "SCTile1", "SCTile2", "SCTile3", "SCTile4", "SCTile5",
    ]

    // MARK: - Happy path: roles resolve in light + dark

    func testEveryAuthoredRoleResolvesInLightAndDark() {
        for role in authoredRoles {
            XCTAssertNotNil(resolve(role, light), "\(role) did not resolve in light")
            XCTAssertNotNil(resolve(role, dark), "\(role) did not resolve in dark")
        }
    }

    func testWarmPaletteRolesResolveInLightAndDark() {
        for role in warmPaletteRoles {
            XCTAssertNotNil(resolve(role, light), "\(role) did not resolve in light")
            XCTAssertNotNil(resolve(role, dark), "\(role) did not resolve in dark")
        }
    }

    /// The warm roles are the fixed brand aesthetic — they must render the SAME
    /// under light and dark (universal, no appearance split). Guards against a
    /// future edit accidentally adding a dark variant that would split the brand.
    func testWarmPaletteRolesAreAppearanceInvariant() {
        for role in warmPaletteRoles {
            let l = resolve(role, light)
            let d = resolve(role, dark)
            XCTAssertEqual(l, d, "\(role) must resolve identically in light and dark")
        }
    }

    /// The five tile colors must be distinct so the stable-hash tile picker spreads
    /// apps across the palette rather than collapsing them onto one color.
    func testTilePaletteColorsAreDistinct() {
        let tiles = ["SCTile1", "SCTile2", "SCTile3", "SCTile4", "SCTile5"]
        let resolved = tiles.compactMap { resolve($0, light) }
        XCTAssertEqual(resolved.count, tiles.count, "every tile color must resolve")
        XCTAssertEqual(Set(resolved).count, tiles.count, "tile colors must be distinct")
    }

    /// `SCColor.tileColor(for:)` is deterministic across calls (stable across
    /// launches — it must not depend on Swift's per-process Hasher seed).
    func testTileColorPickerIsDeterministic() {
        for key in ["com.apple.Safari", "1password", "Slack", "unknown-app"] {
            XCTAssertEqual(
                Color.tileColor(for: key), Color.tileColor(for: key),
                "tileColor(for:) must be stable for \(key)"
            )
        }
    }

    // MARK: - High-contrast variants diverge for text + accent

    /// Catalog high-contrast resolution keys off the *system* increase-contrast
    /// accessibility flag, which cannot be toggled in-process — so the resolved
    /// `NSColor` is unreadable from a unit-test host (same class of limitation as
    /// reading a SwiftUI color back). Assert the authored contract instead: each
    /// text/accent role's source color set declares a high-contrast entry whose
    /// components differ from its base. This guards us actually *authoring* the
    /// variant; the OS owns resolving it.
    func testHighContrastVariantsAreAuthoredAndDifferFromBase() throws {
        for role in ["SCTextPrimary", "SCTextSecondary", "AccentColor"] {
            let entries = try colorEntries(forRole: role)
            let base = entries.first { $0.appearances.isEmpty }
            let highContrast = entries.first {
                $0.appearances.contains(where: { $0 == ["contrast": "high"] })
            }
            XCTAssertNotNil(base, "\(role) is missing a base (light) color")
            XCTAssertNotNil(highContrast, "\(role) is missing a high-contrast variant")
            XCTAssertNotEqual(
                base?.components, highContrast?.components,
                "\(role) high-contrast components should differ from base"
            )
        }
    }

    private struct ColorEntry {
        let appearances: [[String: String]]
        let components: [String: String]
    }

    /// Parse a color set's source `Contents.json` (located relative to this test
    /// file) into its appearance-tagged entries.
    private func colorEntries(forRole role: String) throws -> [ColorEntry] {
        let url = TestSourcePaths.macosFile(
            "ScreenCap/Assets.xcassets/\(role).colorset/Contents.json"
        )
        let data = try Data(contentsOf: url)
        let json = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        let colors = json["colors"] as! [[String: Any]]
        return colors.map { entry in
            let appearances = (entry["appearances"] as? [[String: String]] ?? []).map {
                [$0["appearance"]!: $0["value"]!]
            }
            let comps = ((entry["color"] as? [String: Any])?["components"]) as? [String: String] ?? [:]
            return ColorEntry(appearances: appearances, components: comps)
        }
    }

    // MARK: - Accessibility invariants

    func testRecordingFillCarriesLegibleDarkText() {
        // Warm amber fills need dark text to hold WCAG 4.5:1 (R7).
        for appearance in [light, dark] {
            guard
                let fill = resolve("SCRecording", appearance),
                let text = resolve("SCOnAccent", appearance)
            else {
                XCTFail("recording/on-accent did not resolve")
                continue
            }
            XCTAssertGreaterThanOrEqual(
                contrastRatio(fill, text), 4.5,
                "recording amber vs on-accent text below 4.5:1"
            )
        }
    }

    func testOnAccentLabelIsADarkToken() {
        // AuroraButtonStyle paints its label with `scOnAccent` (never .white /
        // .primary) so dark-text-on-lime/amber contrast holds (SCR-197 U3). Assert
        // the token is genuinely dark rather than white.
        for appearance in [light, dark] {
            guard let onAccent = resolve("SCOnAccent", appearance) else {
                XCTFail("scOnAccent did not resolve")
                continue
            }
            XCTAssertLessThan(
                relativeLuminance(onAccent), 0.2,
                "scOnAccent must be a dark label token"
            )
        }
    }

    func testErrorForegroundAndSurfaceAreDistinct() {
        // Guards the fg/surface split from re-collapsing into one role (R2 contract).
        for appearance in [light, dark] {
            let fg = resolve("SCErrorFg", appearance)
            let surface = resolve("SCErrorSurface", appearance)
            XCTAssertNotEqual(fg, surface, "errorFg and errorSurface must differ")
        }
    }

    // MARK: - WCAG relative-luminance contrast

    private func contrastRatio(_ a: NSColor, _ b: NSColor) -> CGFloat {
        let la = relativeLuminance(a)
        let lb = relativeLuminance(b)
        let hi = max(la, lb), lo = min(la, lb)
        return (hi + 0.05) / (lo + 0.05)
    }

    private func relativeLuminance(_ color: NSColor) -> CGFloat {
        func lin(_ c: CGFloat) -> CGFloat {
            c <= 0.03928 ? c / 12.92 : pow((c + 0.055) / 1.055, 2.4)
        }
        let r = lin(color.redComponent)
        let g = lin(color.greenComponent)
        let b = lin(color.blueComponent)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    }
}
