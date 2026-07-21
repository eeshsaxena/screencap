import XCTest

@testable import Screencap

/// U7/U8 (honest status): pure gating for the first-recording beat + dead-state banner.
final class IntelligenceSurfacePolicyTests: XCTestCase {
    private let usable = IntelligenceVerdict(usable: true, target: .onDevice)
    private let notUsable = IntelligenceVerdict(usable: false, target: .none)

    // MARK: - beat gate (catch-up)

    func testBeatFiresForUserWhoDidNotSeeOnboardingChoice() {
        XCTAssertTrue(IntelligenceSurfacePolicy.shouldShowFirstRecordingBeat(
            beatSeen: false, onboardingChoiceSeen: false))
    }

    func testBeatDoesNotFireAfterItWasSeen() {
        XCTAssertFalse(IntelligenceSurfacePolicy.shouldShowFirstRecordingBeat(
            beatSeen: true, onboardingChoiceSeen: false))
    }

    func testBeatDoesNotFireWhenOnboardingChoiceWasSeen() {
        XCTAssertFalse(IntelligenceSurfacePolicy.shouldShowFirstRecordingBeat(
            beatSeen: false, onboardingChoiceSeen: true))
    }

    // MARK: - beat mode (adaptive content)

    func testBeatWaitsWhileVerdictUnresolved() {
        XCTAssertEqual(IntelligenceSurfacePolicy.beatMode(verdict: nil), .awaitVerdict)
    }

    func testBeatLightConfirmWhenUsable() {
        XCTAssertEqual(IntelligenceSurfacePolicy.beatMode(verdict: usable), .lightConfirm)
    }

    func testBeatChooseModelWhenNotUsable() {
        XCTAssertEqual(IntelligenceSurfacePolicy.beatMode(verdict: notUsable), .chooseModel)
    }

    // MARK: - banner gate

    func testBannerShowsWhenNotUsableAndNotSuppressed() {
        XCTAssertTrue(IntelligenceSurfacePolicy.shouldShowDeadStateBanner(
            verdict: notUsable, suppressedThisRecording: false))
    }

    func testBannerHiddenWhenUsable() {
        XCTAssertFalse(IntelligenceSurfacePolicy.shouldShowDeadStateBanner(
            verdict: usable, suppressedThisRecording: false))
    }

    func testBannerSuppressedThisRecording() {
        XCTAssertFalse(IntelligenceSurfacePolicy.shouldShowDeadStateBanner(
            verdict: notUsable, suppressedThisRecording: true))
    }

    func testBannerHiddenWhileVerdictUnresolved() {
        XCTAssertFalse(IntelligenceSurfacePolicy.shouldShowDeadStateBanner(
            verdict: nil, suppressedThisRecording: false))
    }

    // MARK: - on-device upgrade offer (SCR-274)

    /// Covers AE4 — Apple Intelligence ready, dedicated model not downloaded.
    func testUpgradeOfferedWhenReadyAppleAndNotDownloaded() {
        XCTAssertTrue(IntelligenceSurfacePolicy.shouldOfferOnDeviceUpgrade(
            probe: .available, downloadedModelInstalled: false))
    }

    /// Covers AE5 — an already-downloaded user already has the better model.
    func testUpgradeNotOfferedWhenModelDownloaded() {
        XCTAssertFalse(IntelligenceSurfacePolicy.shouldOfferOnDeviceUpgrade(
            probe: .available, downloadedModelInstalled: true))
    }

    /// Cloud-only-usable / AI-off / ineligible users reach `.lightConfirm` but have
    /// no on-device path to upgrade — no line.
    func testUpgradeNotOfferedWhenAppleIntelligenceNotAvailable() {
        for probe: OnDeviceModelStatus in [.appleIntelligenceOff, .notEligible, .osUnsupported, .unknown, .modelDownloading] {
            XCTAssertFalse(
                IntelligenceSurfacePolicy.shouldOfferOnDeviceUpgrade(
                    probe: probe, downloadedModelInstalled: false),
                "probe \(probe)")
        }
    }
}
