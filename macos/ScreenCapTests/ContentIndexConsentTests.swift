import XCTest
@testable import ScreenCap

/// SCR-174 U7 — the pure consent reducer that backs the Search on-screen-text
/// indexing prompt. Covers the success-latch (a failed write never advances the
/// flag) and the decode-failure preservation (a read failure never resurfaces a
/// dismissed banner) — the two invariants called out in review #10.
final class ContentIndexConsentTests: XCTestCase {
    private let neverAsked = ContentIndexConsentState(indexEnabled: false, declined: false)

    func testBannerVisibleOnlyWhenFreeTextAndNeitherFlagSet() {
        XCTAssertTrue(neverAsked.bannerVisible(hasFreeText: true))
        XCTAssertFalse(neverAsked.bannerVisible(hasFreeText: false))

        let enabled = ContentIndexConsentState(indexEnabled: true, declined: false)
        XCTAssertFalse(enabled.bannerVisible(hasFreeText: true))

        let declined = ContentIndexConsentState(indexEnabled: false, declined: true)
        XCTAssertFalse(declined.bannerVisible(hasFreeText: true))
    }

    // MARK: - Enable write-failure latch

    func testEnableWriteFailureRevertsToDisabled() {
        let optimistic = ContentIndexConsent.optimisticEnable(neverAsked)
        XCTAssertTrue(optimistic.indexEnabled)

        // The persist failed — the flag must NOT stay latched on.
        let reverted = ContentIndexConsent.enableDidFail(optimistic)
        XCTAssertFalse(reverted.indexEnabled, "a failed enable write must not advance the latch")
        XCTAssertFalse(reverted.declined)
    }

    // MARK: - Decline write-failure latch

    func testDeclineWriteFailureRevertsSoBannerReappears() {
        let optimistic = ContentIndexConsent.optimisticDecline(neverAsked)
        XCTAssertTrue(optimistic.declined)

        let reverted = ContentIndexConsent.declineDidFail(optimistic)
        XCTAssertFalse(reverted.declined, "a failed decline write must not falsely suppress the banner")
        XCTAssertTrue(reverted.bannerVisible(hasFreeText: true))
    }

    // MARK: - Read-failure preservation (#10)

    func testLoadFailurePreservesPriorDecline() {
        let priorDecline = ContentIndexConsentState(indexEnabled: false, declined: true)
        let afterFailedRead = ContentIndexConsent.loadDidFail(priorDecline)
        XCTAssertTrue(afterFailedRead.declined, "a transient read failure must preserve a prior decline")
        XCTAssertFalse(afterFailedRead.indexEnabled)
        // And crucially the dismissed banner stays dismissed.
        XCTAssertFalse(afterFailedRead.bannerVisible(hasFreeText: true))
    }

    func testLoadFailureAssumesIndexingOff() {
        let priorEnabled = ContentIndexConsentState(indexEnabled: true, declined: false)
        let afterFailedRead = ContentIndexConsent.loadDidFail(priorEnabled)
        XCTAssertFalse(afterFailedRead.indexEnabled)
    }

    func testApplyLoadedMapsNilFieldsToConservativeDefaults() {
        let s = ContentIndexConsent.applyLoaded(indexEnabled: nil, declined: nil, into: neverAsked)
        XCTAssertFalse(s.indexEnabled)
        XCTAssertFalse(s.declined)

        let s2 = ContentIndexConsent.applyLoaded(indexEnabled: true, declined: true, into: neverAsked)
        XCTAssertTrue(s2.indexEnabled)
        XCTAssertTrue(s2.declined)
    }
}
