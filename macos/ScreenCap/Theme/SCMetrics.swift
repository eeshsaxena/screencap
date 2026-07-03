import CoreGraphics

/// Spacing + corner-radius tokens for the ScreenCap design-token foundation
/// (SCR-197). Density lives in one place — tune here, not at call sites.
///
/// Character constraint ("calm instrument", R4): the ramp calibrates toward the
/// *relaxed* end of the native macOS range (not compact), and radii are
/// *structural* (not pill/capsule) by default. Capsule is reserved for the single
/// primary action (`AuroraButtonStyle`), so it is intentionally not a radius step.
enum SCMetrics {

    // MARK: - Spacing ramp (strictly increasing)

    static let space1: CGFloat = 4
    static let space2: CGFloat = 8
    static let space3: CGFloat = 12
    static let space4: CGFloat = 16
    static let space5: CGFloat = 20
    static let space6: CGFloat = 24
    static let space7: CGFloat = 32
    /// Onboarding / hero padding (prototype uses 40px gutters).
    static let space8: CGFloat = 40

    // MARK: - Corner radii (structural; sm < md < lg)

    static let radiusSm: CGFloat = 6
    static let radiusMd: CGFloat = 10
    static let radiusLg: CGFloat = 16

    // MARK: - Screencap Prototype radii (KTD-1 window radius + the design ramp)
    //
    // The prototype's radius ramp, named by the surface each size dresses. `pill`
    // is the 999 fully-rounded token (badges, chips, primary pills); the numeric
    // ramp 4 → 16 is strictly increasing so the token tests can pin its order.

    /// Fully-rounded pill / capsule (badges, filter chips, primary buttons).
    static let radiusPill: CGFloat = 999
    /// Hairline detail radius (`4`).
    static let radiusHairline: CGFloat = 4
    /// Tight inner radius (`6`).
    static let radiusTight: CGFloat = 6
    /// Inner element radius — thumbnails-within-cards, tiles (`8`).
    static let radiusInner: CGFloat = 8
    /// Chip / small control radius (`10`).
    static let radiusChip: CGFloat = 10
    /// Standard control / window radius (`12`).
    static let radiusControl: CGFloat = 12
    /// Panel / sheet radius (`14`).
    static let radiusPanel: CGFloat = 14
    /// Card radius (`16`).
    static let radiusCard: CGFloat = 16
    /// Main-window corner radius (`12`, KTD-1/KTD-4 window chrome).
    static let radiusWindow: CGFloat = 12
}
