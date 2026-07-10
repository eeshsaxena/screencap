import XCTest
@testable import ScreenCap

/// The on-device availability status → Intelligence-pane badge mapping. The live
/// `probe()` reads system state (untestable in CI); this covers the pure
/// presentation the pane renders from a status value.
final class OnDeviceAvailabilityTests: XCTestCase {

    func testAvailableShowsReadyChipWithNoAction() {
        let badge = OnDeviceModelStatus.available.settingsBadge
        XCTAssertEqual(badge?.text, "Ready")
        XCTAssertEqual(badge?.tone, .ok)
        XCTAssertEqual(badge?.offersSystemSettings, false)
    }

    func testAppleIntelligenceOffWarnsAndOffersSystemSettings() {
        let badge = OnDeviceModelStatus.appleIntelligenceOff.settingsBadge
        XCTAssertEqual(badge?.tone, .warn)
        XCTAssertEqual(
            badge?.offersSystemSettings, true,
            "off is the one system-toggle case — the badge must offer the deep link"
        )
        XCTAssertTrue(badge?.text.contains("Apple Intelligence") ?? false)
    }

    func testDownloadingIsInformationalNoAction() {
        let badge = OnDeviceModelStatus.modelDownloading.settingsBadge
        XCTAssertEqual(badge?.tone, .info)
        XCTAssertEqual(badge?.offersSystemSettings, false)
    }

    func testNotEligibleWarnsWithoutSystemSettings() {
        // Not eligible can't be fixed by a system toggle — warn, but no deep link.
        let badge = OnDeviceModelStatus.notEligible.settingsBadge
        XCTAssertEqual(badge?.tone, .warn)
        XCTAssertEqual(badge?.offersSystemSettings, false)
    }

    func testOsUnsupportedIsInformationalNoAction() {
        let badge = OnDeviceModelStatus.osUnsupported.settingsBadge
        XCTAssertEqual(badge?.tone, .info)
        XCTAssertEqual(badge?.offersSystemSettings, false)
    }

    func testUnknownShowsNoBadge() {
        // Can't determine the state → show nothing rather than guess.
        XCTAssertNil(OnDeviceModelStatus.unknown.settingsBadge)
    }
}
