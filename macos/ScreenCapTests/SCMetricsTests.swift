import SwiftUI
import XCTest
@testable import ScreenCap

/// U2 (SCR-197) — the spacing/radius ramps and the type scale are pure value
/// tokens, so the assertable contract is structural: spacing is monotonic, radii
/// are ordered, and every named type step maps to a real `Font`.
final class SCMetricsTests: XCTestCase {

    func testSpacingRampIsStrictlyIncreasing() {
        let ramp = [
            SCMetrics.space1, SCMetrics.space2, SCMetrics.space3,
            SCMetrics.space4, SCMetrics.space5, SCMetrics.space6, SCMetrics.space7,
        ]
        for (a, b) in zip(ramp, ramp.dropFirst()) {
            XCTAssertLessThan(a, b, "spacing ramp must be strictly increasing")
        }
    }

    func testRadiusStepsAreOrdered() {
        XCTAssertLessThan(SCMetrics.radiusSm, SCMetrics.radiusMd)
        XCTAssertLessThan(SCMetrics.radiusMd, SCMetrics.radiusLg)
    }

    func testSpacingRampExtendsToOnboardingGutter() {
        XCTAssertLessThan(SCMetrics.space7, SCMetrics.space8)
        XCTAssertEqual(SCMetrics.space8, 40)
    }

    /// The prototype radius ramp is pinned to the design's exact values, and its
    /// numeric steps 4 → 16 are strictly increasing (the token tests' contract).
    func testPrototypeRadiiPinnedToDesignValues() {
        XCTAssertEqual(SCMetrics.radiusPill, 999)
        XCTAssertEqual(SCMetrics.radiusHairline, 4)
        XCTAssertEqual(SCMetrics.radiusTight, 6)
        XCTAssertEqual(SCMetrics.radiusInner, 8)
        XCTAssertEqual(SCMetrics.radiusChip, 10)
        XCTAssertEqual(SCMetrics.radiusControl, 12)
        XCTAssertEqual(SCMetrics.radiusPanel, 14)
        XCTAssertEqual(SCMetrics.radiusCard, 16)
        XCTAssertEqual(SCMetrics.radiusWindow, 12)
    }

    func testPrototypeRadiusRampIsStrictlyIncreasing() {
        let ramp = [
            SCMetrics.radiusHairline, SCMetrics.radiusTight, SCMetrics.radiusInner,
            SCMetrics.radiusChip, SCMetrics.radiusControl, SCMetrics.radiusPanel,
            SCMetrics.radiusCard,
        ]
        for (a, b) in zip(ramp, ramp.dropFirst()) {
            XCTAssertLessThan(a, b, "prototype radius ramp must be strictly increasing")
        }
        XCTAssertGreaterThan(SCMetrics.radiusPill, SCMetrics.radiusCard)
    }

    func testTypeScaleStepsAreDistinctFonts() {
        // Guards against an accidental copy-paste of the same `.system(...)` across
        // steps — e.g. `monoTimer` losing its `monospacedDigit()` and collapsing
        // onto `labelPrimary`. Font is Equatable, so distinct steps must compare
        // unequal.
        XCTAssertNotEqual(SCTypography.display, SCTypography.title)
        XCTAssertNotEqual(SCTypography.title, SCTypography.body)
        XCTAssertNotEqual(SCTypography.body, SCTypography.labelSecondary)
        XCTAssertNotEqual(SCTypography.labelSecondary, SCTypography.metadata)
        XCTAssertNotEqual(SCTypography.labelPrimary, SCTypography.monoTimer)
    }
}
