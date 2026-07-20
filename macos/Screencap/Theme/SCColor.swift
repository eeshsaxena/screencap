import AppKit
import SwiftUI

/// Semantic color-role seam for the Screencap design-token foundation (SCR-197).
///
/// Colors are authored as asset-catalog color sets (`Assets.xcassets/SC*.colorset`)
/// so Light / Dark / High-Contrast resolve automatically — no `colorScheme`
/// branching. This `Color` extension is the ergonomic surface so call sites use
/// `Color.scRecording` etc. and never touch raw asset names. The signature accent
/// ships as the global `AccentColor` asset (wired in `project.yml`), so every
/// `.tint` / `.accentColor` site inherits the new spring-teal for free.
///
/// The one intentional exception to "no branching" is the Aurora *gradient*: a
/// `LinearGradient` cannot live in the asset catalog, so `SCGradient` carries two
/// constants and hero call sites branch on `@Environment(\.colorScheme)`.
extension Color {

    /// Anchors named-asset lookup to the app bundle. `Color("name")` defaults to
    /// `.main`, which under the unit-test host is the test runner — not the app —
    /// so role colors would fail to resolve in `SCColorTests`. Resolving against
    /// the bundle that owns this type fixes both the app and the test path.
    private final class BundleToken {}
    static let scBundle = Bundle(for: BundleToken.self)

    private static func role(_ name: String) -> Color { Color(name, bundle: scBundle) }

    // MARK: - Accent

    /// The derived solid spring-teal (selection / links / focus). Drawn from the
    /// global `AccentColor` asset so it stays identical to the system tint.
    static var scAccent: Color { role("AccentColor") }

    // MARK: - State roles

    /// Recording-active fill — warm amber. Distinguished from error by *shape*
    /// (filled dot/ring vs. triangle), not hue alone. Carries dark text (`scOnAccent`).
    static var scRecording: Color { role("SCRecording") }

    /// Error foreground (icon / text / badge). Red is reserved strictly for error.
    static var scErrorFg: Color { role("SCErrorFg") }

    /// Error surface — the tinted overlay behind an error message (alpha baked in).
    static var scErrorSurface: Color { role("SCErrorSurface") }

    /// Success foreground — soft green (granted / healthy states).
    static var scSuccessFg: Color { role("SCSuccessFg") }

    // MARK: - Structural roles

    /// Base surface (badge backgrounds, flat fills).
    static var scSurface: Color { role("SCSurface") }

    /// Elevated surface — inset cards/panels; also the neutral advisory overlay.
    static var scSurfaceElevated: Color { role("SCSurfaceElevated") }

    /// Primary text.
    static var scTextPrimary: Color { role("SCTextPrimary") }

    /// Secondary text.
    static var scTextSecondary: Color { role("SCTextSecondary") }

    // MARK: - On-fill roles

    /// Dark label/text laid on bright fills (Aurora gradient + amber recording).
    static var scOnAccent: Color { role("SCOnAccent") }

    /// Disabled label on the flat disabled primary action.
    static var scDisabledOnAccentFg: Color { role("SCDisabledOnAccentFg") }

    // MARK: - Semantic aliases (R2 contract names, no separate asset)

    /// Sidebar / list selection is driven by the accent (R2: "selection driven by
    /// the accent"), so it aliases the global tint rather than duplicating an asset.
    static var scSelection: Color { .accentColor }

    /// Advisory de-colors to neutral (R7): foreground is secondary text, surface is
    /// the neutral elevated surface — there is deliberately no distinct advisory hue.
    static var scAdvisoryFg: Color { .scTextSecondary }
    static var scAdvisorySurface: Color { .scSurfaceElevated }

    // MARK: - Screencap Prototype warm palette (SCR-197 successor / prototype UI)
    //
    // The prototype (`docs/design/screencap-prototype/`) is a single fixed warm-cream
    // aesthetic, so these roles are authored as *universal* color sets (one value,
    // no light/dark/high-contrast split) — the teal brand and warm paper read the
    // same regardless of system appearance. The older SC* roles above stay alive
    // until U14 retires the legacy views.

    /// Card / window base — the brightest warm surface (`#FFFDF7`).
    static var scPaper: Color { role("SCPaper") }
    /// Main content canvas — warm background (`#F6F2E9`).
    static var scCanvas: Color { role("SCCanvas") }
    /// Subtle inset fill (chips, hover wells) (`#EDE6D6`).
    static var scFillSubtle: Color { role("SCFillSubtle") }
    /// Hairline border on warm surfaces (`#E0D8C6`).
    static var scBorderWarm: Color { role("SCBorderWarm") }

    /// Primary ink on warm surfaces (`#1C2420`).
    static var scInk: Color { role("SCInk") }
    /// Secondary ink (`#52584F`).
    static var scInkSecondary: Color { role("SCInkSecondary") }
    /// Muted ink — mono metadata / captions (`#86795F`).
    static var scInkMuted: Color { role("SCInkMuted") }
    /// Faintest ink — de-emphasized hints (`#A99C82`).
    static var scInkFaint: Color { role("SCInkFaint") }

    /// Brand teal — primary actions, active nav, links (`#0E7C6B`).
    static var scTeal: Color { role("SCTeal") }
    /// Teal hover / pressed (`#0C6E5F`).
    static var scTealHover: Color { role("SCTealHover") }
    /// Soft teal — subtle teal accents / borders (`#6BA89E`).
    static var scTealSoft: Color { role("SCTealSoft") }

    /// Amber — recording / draft accent (`#D9A441`).
    static var scAmber: Color { role("SCAmber") }
    /// Amber text — legible amber on warm surfaces (`#A97F2E`).
    static var scAmberText: Color { role("SCAmberText") }
    /// Amber on the dark HUD (`#E3B054`).
    static var scAmberHUD: Color { role("SCAmberHUD") }

    /// Rust — the "blocked" caption / hard-stop accent (`#B4552B`).
    static var scRust: Color { role("SCRust") }

    /// Dark canvas — full-bleed dark backdrops (`#10161A`).
    static var scDarkCanvas: Color { role("SCDarkCanvas") }
    /// HUD pill surface (`#1D2421`).
    static var scHUDSurface: Color { role("SCHUDSurface") }
    /// HUD raised surface — chips within the pill (`#2A322E`).
    static var scHUDSurfaceRaised: Color { role("SCHUDSurfaceRaised") }
    /// Muted label on the dark HUD (`#9BA69E`).
    static var scHUDMuted: Color { role("SCHUDMuted") }

    /// Stable-hash palette for app initials tiles (App rules / onboarding). Index
    /// an app's identity into this array; see `SCColor.tileColor(for:)`.
    static let scTilePalette: [Color] = [
        role("SCTile1"), role("SCTile2"), role("SCTile3"), role("SCTile4"), role("SCTile5"),
    ]

    /// Pick a deterministic tile color for `key` (e.g. a bundle id) from the
    /// design's five-color tile palette. Stable across launches — same key always
    /// maps to the same tile — using a small FNV-1a hash so the choice does not
    /// depend on Swift's per-process `Hasher` seed.
    static func tileColor(for key: String) -> Color {
        var hash: UInt64 = 0xcbf29ce484222325
        for byte in key.utf8 {
            hash ^= UInt64(byte)
            hash = hash &* 0x100000001b3
        }
        return scTilePalette[Int(hash % UInt64(scTilePalette.count))]
    }

    // MARK: - R9 fast-follow stubs (named here so the contract is fixed; no asset
    // authored until a launch surface consumes them — keeps the catalog lean).
    // TODO(R9): author color set — `scBorder`
    // TODO(R9): author color set — `scSeparator`
    // TODO(R9): author color set — `scTextDisabled`
    // TODO(R9): author color set — `scFocusRing`
    // TODO(R9): author color set — `scOverlayScrim`
    // TODO(R9): author color set — `scBackgroundHover` (AuroraButtonStyle uses
    //           gradient-opacity for press feedback, so no hover/pressed surface
    //           is consumed at launch)
    // TODO(R9): author color set — `scBackgroundPressed`
}

/// The Aurora signature gradient (lime → aqua). Lives outside the asset catalog
/// because `LinearGradient` is not a color set; hero call sites pick the variant
/// via `@Environment(\.colorScheme)`. Neon endpoints fail contrast on white, so
/// light mode uses a deepened gradient.
enum SCGradient {
    /// Dark / neon variant — glows on near-black.
    static let aurora = LinearGradient(
        colors: [
            Color(.sRGB, red: 198 / 255, green: 242 / 255, blue: 61 / 255),   // #C6F23D lime
            Color(.sRGB, red: 31 / 255, green: 227 / 255, blue: 210 / 255),   // #1FE3D2 aqua
        ],
        startPoint: .topLeading,
        endPoint: .bottomTrailing
    )

    /// Deepened light variant — holds WCAG contrast on white backgrounds.
    static let auroraLight = LinearGradient(
        colors: [
            Color(.sRGB, red: 134 / 255, green: 192 / 255, blue: 14 / 255),   // #86C00E deep lime
            Color(.sRGB, red: 14 / 255, green: 158 / 255, blue: 132 / 255),   // #0E9E84 deep aqua
        ],
        startPoint: .topLeading,
        endPoint: .bottomTrailing
    )

    /// Pick the appearance-appropriate gradient for a hero surface.
    static func aurora(for scheme: ColorScheme) -> LinearGradient {
        scheme == .dark ? aurora : auroraLight
    }
}

/// The single shared button style the launch surfaces need (the primary action:
/// Stop in the recording banner / toolbar). The component vocabulary beyond this
/// is R5 fast-follow — deliberately not started here.
///
/// Resting = the colorScheme-appropriate Aurora gradient with a dark label
/// (`scOnAccent`, never `.white`/`.primary` so dark-text-on-lime contrast holds);
/// pressed = gradient at 0.85 opacity; disabled = flat `scSurface` with a muted
/// label. Capsule corner — the one place capsule is allowed (R4).
struct AuroraButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        AuroraButtonBody(configuration: configuration)
    }

    /// `ButtonStyle` is not a `View`, so `@Environment` reads must happen in a
    /// nested view — this carries the colorScheme + enabled state into the body.
    private struct AuroraButtonBody: View {
        let configuration: Configuration
        @Environment(\.colorScheme) private var scheme
        @Environment(\.isEnabled) private var isEnabled

        var body: some View {
            configuration.label
                .font(SCTypography.labelPrimary)
                .padding(.horizontal, SCMetrics.space4)
                .padding(.vertical, SCMetrics.space2)
                .foregroundStyle(isEnabled ? Color.scOnAccent : Color.scDisabledOnAccentFg)
                .background(background)
                .contentShape(Capsule())
                .opacity(isEnabled && configuration.isPressed ? 0.85 : 1.0)
        }

        @ViewBuilder
        private var background: some View {
            if isEnabled {
                Capsule().fill(SCGradient.aurora(for: scheme))
            } else {
                Capsule().fill(Color.scSurface)
            }
        }
    }
}
