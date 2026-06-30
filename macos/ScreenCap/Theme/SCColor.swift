import AppKit
import SwiftUI

/// Semantic color-role seam for the ScreenCap design-token foundation (SCR-197).
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
