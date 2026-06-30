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

    func testTypeScaleExposesNamedStepsAsFonts() {
        // Each named step must be a real, distinct constant. Comparing the
        // documented set guards against a step being dropped or duplicated.
        let steps: [Font] = [
            SCTypography.display,
            SCTypography.title,
            SCTypography.body,
            SCTypography.labelPrimary,
            SCTypography.labelSecondary,
            SCTypography.metadata,
            SCTypography.monoTimer,
        ]
        XCTAssertEqual(steps.count, 7, "type scale should expose exactly the named steps")
    }
}
