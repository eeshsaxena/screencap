import SwiftUI

/// Named type scale for the ScreenCap design-token foundation (SCR-197).
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
}
