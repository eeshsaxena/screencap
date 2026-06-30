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

    // MARK: - Corner radii (structural; sm < md < lg)

    static let radiusSm: CGFloat = 6
    static let radiusMd: CGFloat = 10
    static let radiusLg: CGFloat = 16
}
