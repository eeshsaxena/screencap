import XCTest

@testable import ScreenCap

/// U5 (honest status): the app-composed "usable" verdict + the per-recording
/// honest-state resolver. Pure model — no SwiftUI, no daemon round-trip.
final class IntelligenceVerdictTests: XCTestCase {
    private func settings(
        provider: String = "on-device",
        cloud: String? = nil,
        summaryConsent: Bool = true,
        downloaded: Bool = false
    ) -> IntelligenceSettings {
        IntelligenceSettings(
            provider: provider, cloudProvider: cloud, summaryCloudConsent: summaryConsent,
            recallCloudConsent: true, daySplitCloudConsent: false, framesCloudConsent: false,
            downloadedModelInstalled: downloaded
        )
    }

    // MARK: - compose (the live verdict)

    func testUnresolvedWhenSettingsNil() {
        XCTAssertNil(IntelligenceVerdict.compose(probe: .available, settings: nil))
    }

    func testOnDeviceUsableWhenProbeAvailable() {
        XCTAssertEqual(
            IntelligenceVerdict.compose(probe: .available, settings: settings()),
            IntelligenceVerdict(usable: true, target: .onDevice)
        )
    }

    func testOnDeviceUsableWhenDownloadedEvenIfAppleIntelligenceOff() {
        XCTAssertEqual(
            IntelligenceVerdict.compose(probe: .appleIntelligenceOff, settings: settings(downloaded: true)),
            IntelligenceVerdict(usable: true, target: .onDevice)
        )
    }

    func testCloudUsableWhenConfiguredAndConsented() {
        XCTAssertEqual(
            IntelligenceVerdict.compose(
                probe: .appleIntelligenceOff, settings: settings(cloud: "gemini", summaryConsent: true)
            ),
            IntelligenceVerdict(usable: true, target: .cloud)
        )
    }

    func testNotUsableWhenNothingConfigured() {
        XCTAssertEqual(
            IntelligenceVerdict.compose(
                probe: .appleIntelligenceOff, settings: settings(cloud: nil, downloaded: false)
            ),
            IntelligenceVerdict(usable: false, target: .none)
        )
    }

    func testCloudNotUsableWithoutConsent() {
        XCTAssertEqual(
            IntelligenceVerdict.compose(
                probe: .osUnsupported, settings: settings(cloud: "gemini", summaryConsent: false)
            ),
            IntelligenceVerdict(usable: false, target: .none)
        )
    }

    // MARK: - resolve (the per-recording honest state)

    func testRecordedReasonAlwaysWins() {
        let usable = IntelligenceVerdict(usable: true, target: .onDevice)
        XCTAssertEqual(RecordingHonestState.resolve(reason: "produced_tasks", verdict: usable), .producedTasks)
        XCTAssertEqual(RecordingHonestState.resolve(reason: "mechanical_only", verdict: usable), .mechanicalOnly)
        XCTAssertEqual(RecordingHonestState.resolve(reason: "in_progress", verdict: usable), .inProgress)
        XCTAssertEqual(RecordingHonestState.resolve(reason: "nothing_to_name", verdict: usable), .nothingToName)
        XCTAssertEqual(RecordingHonestState.resolve(reason: "couldnt_run", verdict: usable), .couldntRun)
    }

    func testNoReasonNotUsableIsNotSetUp() {
        let notUsable = IntelligenceVerdict(usable: false, target: .none)
        XCTAssertEqual(RecordingHonestState.resolve(reason: nil, verdict: notUsable), .notSetUp)
    }

    func testNoReasonUnresolvedVerdictIsUnknown() {
        XCTAssertEqual(RecordingHonestState.resolve(reason: nil, verdict: nil), .unknown)
    }

    func testNoReasonUsableVerdictIsUnknownNotNotSetUp() {
        let usable = IntelligenceVerdict(usable: true, target: .onDevice)
        XCTAssertEqual(RecordingHonestState.resolve(reason: nil, verdict: usable), .unknown)
    }

    func testDriftedReasonFallsToVerdictBranch() {
        let notUsable = IntelligenceVerdict(usable: false, target: .none)
        XCTAssertEqual(RecordingHonestState.resolve(reason: "weird_future_value", verdict: notUsable), .notSetUp)
    }

    func testAffordanceHelpers() {
        XCTAssertTrue(RecordingHonestState.mechanicalOnly.hasPopulatedTasks)
        XCTAssertFalse(RecordingHonestState.nothingToName.hasPopulatedTasks)
        XCTAssertTrue(RecordingHonestState.mechanicalOnly.offersSetup)
        XCTAssertTrue(RecordingHonestState.notSetUp.offersSetup)
        XCTAssertFalse(RecordingHonestState.producedTasks.offersSetup)
        XCTAssertFalse(RecordingHonestState.inProgress.offersSetup)
    }
}
