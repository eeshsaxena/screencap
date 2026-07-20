import SwiftUI

/// Named type scale for the Screencap design-token foundation (SCR-197).
///
/// A scale *on the system font* — no custom/brand typeface (R3). Every step maps
/// to a SwiftUI text style, so Dynamic Type is preserved for free; the scale just
/// gives the raw `.font(.system(size:))` literals on the launch surfaces a named,
/// one-place-tunable home. Existing semantic styles (`.headline`, `.caption`) are
/// already Dynamic-Type-compliant and these names sit on top of them.
enum SCTypography {

    /// Hero / largest heading.
    static let display: Font = .system(.largeTitle, design: .default).weight(.bold)

    /// Section / sheet title.
    static let title: Font = .system(.title, design: .default).weight(.semibold)

    /// Default body copy.
    static let body: Font = .body

    /// Primary control / row label.
    static let labelPrimary: Font = .headline

    /// Secondary control / row label.
    static let labelSecondary: Font = .subheadline

    /// Small tracked metadata / captions.
    static let metadata: Font = .caption

    /// Monospaced-digit elapsed timer (recording banner) — steady width so the
    /// clock doesn't jitter as digits change.
    static let monoTimer: Font = .system(.headline, design: .default).monospacedDigit()

    // MARK: - Screencap Prototype named scale (bundled families — KTD-3)
    //
    // The prototype pairs four typefaces to four jobs (see `SCFonts`):
    //   • Newsreader (serif, 500)   — display headings, day headings, brand mark
    //   • Space Grotesk (600/700)   — screen headings, initials tiles
    //   • IBM Plex Mono (400/500)   — metadata, badges, timestamps
    //   • Public Sans (400–700)     — body copy
    // Steps resolve to the bundled PostScript names; `Font.custom` falls back to
    // the system font automatically if a face is somehow unregistered, so a
    // packaging slip degrades to legible system type rather than blank text.
    //
    // The design uses many exact point sizes, so the family *factories* below are
    // the primary surface (call `SCTypography.mono(size: 10.5)` for any design
    // size); the named steps are the common, load-bearing sizes.

    // Family factories — request any design size at a chosen weight.

    /// Newsreader serif (Medium 500 is the only display weight the design uses;
    /// pass `.regular` for running serif, `.italic` for the italic instance).
    static func serif(size: CGFloat, weight: SerifWeight = .medium) -> Font {
        .custom(weight.postScriptName, size: size)
    }
    /// Space Grotesk — screen headings and tile initials.
    static func grotesk(size: CGFloat, weight: GroteskWeight = .semibold) -> Font {
        .custom(weight.postScriptName, size: size)
    }
    /// IBM Plex Mono — metadata, badges, timestamps.
    static func mono(size: CGFloat, weight: MonoWeight = .regular) -> Font {
        .custom(weight.postScriptName, size: size)
    }
    /// Public Sans — body copy.
    static func sans(size: CGFloat, weight: SansWeight = .regular) -> Font {
        .custom(weight.postScriptName, size: size)
    }

    enum SerifWeight {
        case regular, medium, italic
        var postScriptName: String {
            switch self {
            case .regular: return SCFonts.Newsreader.regular
            case .medium: return SCFonts.Newsreader.medium
            case .italic: return SCFonts.Newsreader.italic
            }
        }
    }
    enum GroteskWeight {
        case regular, medium, semibold, bold
        var postScriptName: String {
            switch self {
            case .regular: return SCFonts.SpaceGrotesk.regular
            case .medium: return SCFonts.SpaceGrotesk.medium
            case .semibold: return SCFonts.SpaceGrotesk.semibold
            case .bold: return SCFonts.SpaceGrotesk.bold
            }
        }
    }
    enum MonoWeight {
        case regular, medium
        var postScriptName: String {
            switch self {
            case .regular: return SCFonts.IBMPlexMono.regular
            case .medium: return SCFonts.IBMPlexMono.medium
            }
        }
    }
    enum SansWeight {
        case regular, medium, semibold, bold
        var postScriptName: String {
            switch self {
            case .regular: return SCFonts.PublicSans.regular
            case .medium: return SCFonts.PublicSans.medium
            case .semibold: return SCFonts.PublicSans.semibold
            case .bold: return SCFonts.PublicSans.bold
            }
        }
    }

    // Named steps — the design's recurring sizes.

    /// Welcome-screen serif hero (Newsreader 500, 40pt).
    static let serifDisplay: Font = serif(size: 40)
    /// Onboarding step / serif screen heading (Newsreader 500, 34pt).
    static let serifHeading: Font = serif(size: 34)
    /// Day / Journal serif heading (Newsreader 500, 26pt).
    static let serifDayHeading: Font = serif(size: 26)
    /// Sidebar brand mark "Screencap" (Newsreader 500, 18pt).
    static let serifBrand: Font = serif(size: 18)

    /// Library / Journal screen heading (Space Grotesk 600, 26pt).
    static let screenHeading: Font = grotesk(size: 26, weight: .semibold)
    /// Settings pane heading (Space Grotesk 600, 21pt).
    static let paneHeading: Font = grotesk(size: 21, weight: .semibold)
    /// Card / section title (Space Grotesk 600, 15pt).
    static let sectionTitle: Font = grotesk(size: 15, weight: .semibold)

    /// Metadata / badge / timestamp (IBM Plex Mono 400, 11pt).
    static let metaMono: Font = mono(size: 11)
    /// Small mono label — chips, section eyebrows (IBM Plex Mono 400, 10.5pt).
    static let metaMonoSmall: Font = mono(size: 10.5)

    /// Body copy (Public Sans 400, 13pt).
    static let bodyText: Font = sans(size: 13)
    /// Emphasized body / row label (Public Sans 600, 14pt).
    static let bodyStrong: Font = sans(size: 14, weight: .semibold)
}
