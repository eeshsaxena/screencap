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
        XCTAssertEqual(
            RecordingHonestState.resolve(reason: "mechanical_only", verdict: usable),
            .mechanicalOnly(sessionTooLong: false)
        )
        XCTAssertEqual(RecordingHonestState.resolve(reason: "in_progress", verdict: usable), .inProgress)
        XCTAssertEqual(RecordingHonestState.resolve(reason: "nothing_to_name", verdict: usable), .nothingToName)
        XCTAssertEqual(RecordingHonestState.resolve(reason: "couldnt_run", verdict: usable), .couldntRun)
    }

    // MARK: - SCR-275 U7: partial success + the degradation detail

    func testPartialReasonMapsToPartialState() {
        let usable = IntelligenceVerdict(usable: true, target: .onDevice)
        XCTAssertEqual(
            RecordingHonestState.resolve(reason: "produced_tasks_partial", verdict: usable),
            .producedTasksPartial(sessionTooLong: false)
        )
    }

    func testContextWindowDetailThreadsIntoDegradedStates() {
        let usable = IntelligenceVerdict(usable: true, target: .onDevice)
        XCTAssertEqual(
            RecordingHonestState.resolve(
                reason: "produced_tasks_partial", detail: "context-window", verdict: usable
            ),
            .producedTasksPartial(sessionTooLong: true)
        )
        XCTAssertEqual(
            RecordingHonestState.resolve(reason: "mechanical_only", detail: "context-window", verdict: usable),
            .mechanicalOnly(sessionTooLong: true)
        )
    }

    func testNonContextWindowDetailReadsGeneric() {
        let usable = IntelligenceVerdict(usable: true, target: .onDevice)
        XCTAssertEqual(
            RecordingHonestState.resolve(reason: "mechanical_only", detail: "respond-failed", verdict: usable),
            .mechanicalOnly(sessionTooLong: false)
        )
        // Future/unknown detail vocabulary also reads generic — never a crash or misread.
        XCTAssertEqual(
            RecordingHonestState.resolve(
                reason: "produced_tasks_partial", detail: "weird_future_detail", verdict: usable
            ),
            .producedTasksPartial(sessionTooLong: false)
        )
    }

    func testDetailDoesNotDisturbOtherMappings() {
        let usable = IntelligenceVerdict(usable: true, target: .onDevice)
        // A (contract-violating) detail on produced_tasks changes nothing.
        XCTAssertEqual(
            RecordingHonestState.resolve(reason: "produced_tasks", detail: "context-window", verdict: usable),
            .producedTasks
        )
        // couldnt_run keeps its plain state regardless of detail.
        XCTAssertEqual(
            RecordingHonestState.resolve(reason: "couldnt_run", detail: "context-window", verdict: usable),
            .couldntRun
        )
        // Unknown reason strings still fall to the verdict branch (old-app compat).
        XCTAssertEqual(
            RecordingHonestState.resolve(reason: "weird_future_value", detail: "context-window", verdict: usable),
            .unknown
        )
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
        XCTAssertTrue(RecordingHonestState.mechanicalOnly(sessionTooLong: false).hasPopulatedTasks)
        XCTAssertTrue(RecordingHonestState.producedTasksPartial(sessionTooLong: false).hasPopulatedTasks)
        XCTAssertTrue(RecordingHonestState.producedTasksPartial(sessionTooLong: true).hasPopulatedTasks)
        XCTAssertFalse(RecordingHonestState.nothingToName.hasPopulatedTasks)
        XCTAssertTrue(RecordingHonestState.mechanicalOnly(sessionTooLong: false).offersSetup)
        XCTAssertFalse(
            RecordingHonestState.mechanicalOnly(sessionTooLong: true).offersSetup,
            "intelligence ran and hit the context window — 'set up' would be a false diagnosis"
        )
        XCTAssertFalse(RecordingHonestState.producedTasksPartial(sessionTooLong: false).offersSetup)
        XCTAssertTrue(RecordingHonestState.notSetUp.offersSetup)
        XCTAssertFalse(RecordingHonestState.producedTasks.offersSetup)
        XCTAssertFalse(RecordingHonestState.inProgress.offersSetup)
    }
}
