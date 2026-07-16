import XCTest

@testable import ScreenCap

/// U7/U8 (honest status): pure gating for the first-recording beat + dead-state banner.
final class IntelligenceSurfacePolicyTests: XCTestCase {
    private let usable = IntelligenceVerdict(usable: true, target: .onDevice)
    private let notUsable = IntelligenceVerdict(usable: false, target: .none)

    // MARK: - beat gate (catch-up)

    func testBeatFiresForNewUserWhoDidNotSeeOnboardingChoice() {
        XCTAssertTrue(IntelligenceSurfacePolicy.shouldShowFirstRecordingBeat(
            hasRecordedOnce: false, onboardingChoiceSeen: false))
    }

    func testBeatDoesNotFireAfterFirstRecording() {
        XCTAssertFalse(IntelligenceSurfacePolicy.shouldShowFirstRecordingBeat(
            hasRecordedOnce: true, onboardingChoiceSeen: false))
    }

    func testBeatDoesNotFireWhenOnboardingChoiceWasSeen() {
        XCTAssertFalse(IntelligenceSurfacePolicy.shouldShowFirstRecordingBeat(
            hasRecordedOnce: false, onboardingChoiceSeen: true))
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
}
