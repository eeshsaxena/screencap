import AppKit
import SwiftUI
import XCTest
@testable import ScreenCap

/// U1 (KTD-3) — the four bundled typefaces and the named type scale. The
/// load-by-PostScript-name contract is assertable in-process: register the fonts
/// straight from the source `Resources/Fonts/` tree (hermetic — independent of app
/// packaging, the same way `SCColorTests` reads the source color sets), then check
/// each PostScript name resolves to a real `NSFont` of the expected family.
final class SCTypographyTests: XCTestCase {

    override class func setUp() {
        super.setUp()
        // Register once for the whole class from the checked-in font files.
        SCFonts.register(fontsIn: TestSourcePaths.macosFile("ScreenCap/Resources/Fonts"))
    }

    // MARK: - Fonts load by PostScript name after registration

    /// Every bundled instance resolves by its PostScript name — the exact strings
    /// `SCFonts` exposes and the typography factories build `Font.custom` from.
    func testEveryBundledInstanceResolvesByPostScriptName() {
        let names = [
            SCFonts.Newsreader.regular, SCFonts.Newsreader.medium, SCFonts.Newsreader.italic,
            SCFonts.SpaceGrotesk.regular, SCFonts.SpaceGrotesk.medium,
            SCFonts.SpaceGrotesk.semibold, SCFonts.SpaceGrotesk.bold,
            SCFonts.IBMPlexMono.regular, SCFonts.IBMPlexMono.medium,
            SCFonts.PublicSans.regular, SCFonts.PublicSans.medium,
            SCFonts.PublicSans.semibold, SCFonts.PublicSans.bold,
        ]
        for name in names {
            XCTAssertNotNil(
                NSFont(name: name, size: 13),
                "\(name) did not resolve — bundled font missing or PostScript name drifted"
            )
        }
    }

    /// All four families load (the plan's headline scenario), and each resolves to
    /// the expected family name — proving the pre-baked static instances carry the
    /// right `name` table rather than an upstream variable-font default.
    func testEachFamilyResolvesToExpectedFamilyName() {
        let expected: [String: String] = [
            SCFonts.Newsreader.regular: "Newsreader",
            SCFonts.SpaceGrotesk.regular: "Space Grotesk",
            SCFonts.IBMPlexMono.regular: "IBM Plex Mono",
            SCFonts.PublicSans.regular: "Public Sans",
        ]
        XCTAssertEqual(Set(SCFonts.familyProbeNames), Set(expected.keys),
                       "familyProbeNames must cover exactly the four families")
        for (psName, family) in expected {
            guard let font = NSFont(name: psName, size: 13) else {
                XCTFail("\(psName) did not resolve"); continue
            }
            XCTAssertEqual(font.familyName, family, "\(psName) resolved to the wrong family")
        }
    }

    /// Within a family, distinct weights resolve to distinct fonts — guards the
    /// variable-font-collapse trap (Space Grotesk / Public Sans default to their
    /// thinnest weight upstream; the pre-baked instances must be genuinely distinct).
    func testWeightsWithinAFamilyAreDistinct() {
        let regular = NSFont(name: SCFonts.SpaceGrotesk.regular, size: 20)
        let bold = NSFont(name: SCFonts.SpaceGrotesk.bold, size: 20)
        XCTAssertNotNil(regular)
        XCTAssertNotNil(bold)
        XCTAssertNotEqual(weight(of: regular), weight(of: bold),
                          "Space Grotesk Regular and Bold must have different weights")
        XCTAssertGreaterThan(weight(of: bold), weight(of: regular))
    }

    private func weight(of font: NSFont?) -> CGFloat {
        guard let font,
              let traits = font.fontDescriptor.object(forKey: .traits) as? [NSFontDescriptor.TraitKey: Any],
              let w = traits[.weight] as? CGFloat
        else { return 0 }
        return w
    }

    // MARK: - Named scale maps to bundled families / is distinct

    func testNamedScaleStepsAreDistinctFonts() {
        // Guards a copy-paste that would collapse two steps (e.g. serifHeading
        // silently equal to serifDisplay). Font is Equatable.
        XCTAssertNotEqual(SCTypography.serifDisplay, SCTypography.serifHeading)
        XCTAssertNotEqual(SCTypography.serifHeading, SCTypography.serifDayHeading)
        XCTAssertNotEqual(SCTypography.serifDayHeading, SCTypography.serifBrand)
        XCTAssertNotEqual(SCTypography.screenHeading, SCTypography.paneHeading)
        XCTAssertNotEqual(SCTypography.paneHeading, SCTypography.sectionTitle)
        XCTAssertNotEqual(SCTypography.metaMono, SCTypography.metaMonoSmall)
        XCTAssertNotEqual(SCTypography.bodyText, SCTypography.bodyStrong)
        // The new bundled scale must not collapse onto the legacy system scale.
        XCTAssertNotEqual(SCTypography.bodyText, SCTypography.body)
    }

    func testFamilyFactoriesProduceDistinctFontsPerWeight() {
        XCTAssertNotEqual(SCTypography.grotesk(size: 20, weight: .regular),
                          SCTypography.grotesk(size: 20, weight: .bold))
        XCTAssertNotEqual(SCTypography.sans(size: 13, weight: .regular),
                          SCTypography.sans(size: 13, weight: .semibold))
        XCTAssertNotEqual(SCTypography.mono(size: 11, weight: .regular),
                          SCTypography.mono(size: 11, weight: .medium))
        XCTAssertNotEqual(SCTypography.serif(size: 34, weight: .regular),
                          SCTypography.serif(size: 34, weight: .medium))
    }
}
