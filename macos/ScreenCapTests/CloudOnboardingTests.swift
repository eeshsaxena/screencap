import XCTest
@testable import ScreenCap

// MARK: - U6: first-run plan-choice presentation policy

final class FirstRunPlanChoicePolicyTests: XCTestCase {
    func testPresentsWhenNoDecisionAndPermissionsDone() {
        XCTAssertTrue(FirstRunSetupPresentationPolicy.shouldPresentPlanChoice(
            daemonProbeCompleted: true,
            cloudDecisionMade: false,
            permissionsSheetShowing: false,
            isRecording: false
        ))
    }

    func testNotPresentedOnceDecided() {
        XCTAssertFalse(FirstRunSetupPresentationPolicy.shouldPresentPlanChoice(
            daemonProbeCompleted: true,
            cloudDecisionMade: true,
            permissionsSheetShowing: false,
            isRecording: false
        ))
    }

    func testNotStackedOverPermissionSheet() {
        // The plan choice follows the permission steps — never over them.
        XCTAssertFalse(FirstRunSetupPresentationPolicy.shouldPresentPlanChoice(
            daemonProbeCompleted: true,
            cloudDecisionMade: false,
            permissionsSheetShowing: true,
            isRecording: false
        ))
    }

    func testNotPresentedBeforeProbe() {
        XCTAssertFalse(FirstRunSetupPresentationPolicy.shouldPresentPlanChoice(
            daemonProbeCompleted: false,
            cloudDecisionMade: false,
            permissionsSheetShowing: false,
            isRecording: false
        ))
    }

    func testNotPresentedOverActiveRecording() {
        XCTAssertFalse(FirstRunSetupPresentationPolicy.shouldPresentPlanChoice(
            daemonProbeCompleted: true,
            cloudDecisionMade: false,
            permissionsSheetShowing: false,
            isRecording: true
        ))
    }
}

@MainActor
final class CloudDecisionStoreTests: XCTestCase {
    private func store() -> CloudDecisionStore {
        let suite = UserDefaults(suiteName: "cloud-decision-test-\(UUID().uuidString)")!
        return CloudDecisionStore(defaults: suite)
    }

    func testDefaultsUndecided() {
        XCTAssertFalse(store().decisionMade)
    }

    func testMarkDecidedPersistsAndIsIdempotent() {
        let s = store()
        s.markDecided()
        XCTAssertTrue(s.decisionMade)
        s.markDecided()  // idempotent
        XCTAssertTrue(s.decisionMade)
    }
}

// MARK: - U9: entitlement decode (contract-optional fields → non-failed state)

final class EntitlementStatusDecodeTests: XCTestCase {
    private func envelope(_ json: String) -> AuthEntitlementsEnvelope? {
        AuthEntitlementsEnvelope.parse(Data(json.utf8))
    }

    func testFoundingActiveWithNullExpires() {
        // Null `expires` (the v1 contract) must decode cleanly, not fail the parse.
        let env = envelope(#"{"ok":true,"schema_version":1,"plan":"founding","active":true,"expires":null}"#)
        XCTAssertNotNil(env)
        XCTAssertNil(env?.expires)
        XCTAssertEqual(EntitlementStatus.from(envelope: env), .active(plan: "founding"))
    }

    func testFreeInactive() {
        let env = envelope(#"{"ok":true,"schema_version":1,"plan":"free","active":false,"expires":null}"#)
        XCTAssertEqual(EntitlementStatus.from(envelope: env), .free)
    }

    func testMissingOptionalFieldsLandOnFreeNotFailed() {
        // A minimal / older envelope with absent plan fields decodes and resolves
        // to a non-failed (.free) state — never a crash or a gate on readiness.
        let env = envelope(#"{"ok":true,"schema_version":1}"#)
        XCTAssertNotNil(env)
        XCTAssertEqual(EntitlementStatus.from(envelope: env), .free)
    }

    func testErrorEnvelopeResolvesFree() {
        let env = envelope(#"{"ok":false,"schema_version":1,"error":"boom"}"#)
        XCTAssertEqual(EntitlementStatus.from(envelope: env), .free)
    }

    func testGarbageResolvesFree() {
        XCTAssertNil(AuthEntitlementsEnvelope.parse(Data("not json".utf8)))
        XCTAssertEqual(EntitlementStatus.from(envelope: nil), .free)
    }
}

// MARK: - U9: account-mismatch resolution

final class AccountMismatchResolveTests: XCTestCase {
    private func event(type: String, owner: String?, signedIn: String?, email: String? = nil) -> RecorderEventLine {
        RecorderEventLine(
            type: type, schemaVersion: 1, forceStopped: nil, permission: nil,
            missing: nil, changes: nil, optOutCommandExamples: nil, cursor: nil,
            reason: nil, reader: nil, ts: nil,
            ownerUid: owner, signedInUid: signedIn, signedInEmail: email
        )
    }

    func testGenuineMismatchResolves() {
        let m = AccountMismatch.from(event: event(type: "account_mismatch", owner: "A", signedIn: "B", email: "b@x.com"))
        XCTAssertEqual(m, AccountMismatch(ownerUid: "A", signedInUid: "B", signedInEmail: "b@x.com"))
    }

    func testEqualUidsIsNotAMismatch() {
        XCTAssertNil(AccountMismatch.from(event: event(type: "account_mismatch", owner: "A", signedIn: "A")))
    }

    func testMissingUidIsNotAMismatch() {
        XCTAssertNil(AccountMismatch.from(event: event(type: "account_mismatch", owner: "A", signedIn: nil)))
    }

    func testOtherEventTypeIgnored() {
        XCTAssertNil(AccountMismatch.from(event: event(type: "started", owner: "A", signedIn: "B")))
    }

    func testReconcileComparesUidsDirectly() {
        XCTAssertEqual(
            AccountMismatch.reconcile(ownerUid: "A", signedInUid: "B", signedInEmail: nil),
            AccountMismatch(ownerUid: "A", signedInUid: "B", signedInEmail: nil)
        )
        XCTAssertNil(AccountMismatch.reconcile(ownerUid: "A", signedInUid: "A", signedInEmail: nil))
    }
}

// MARK: - U9: /v0/events monitor (replay-cursor + 410 reconcile)

/// Mutable holder safe to capture in the monitor's `@Sendable` seams from a test.
/// Everything actually runs on the main actor here, so the unchecked marker is
/// sound for these single-threaded tests.
private final class Box<T>: @unchecked Sendable {
    var value: T
    init(_ value: T) { self.value = value }
}

@MainActor
final class CloudEventsMonitorTests: XCTestCase {
    private func mismatchEvent() -> RecorderEventLine {
        RecorderEventLine(
            type: "account_mismatch", schemaVersion: 1, forceStopped: nil, permission: nil,
            missing: nil, changes: nil, optOutCommandExamples: nil, cursor: nil,
            reason: nil, reader: nil, ts: nil,
            ownerUid: "A", signedInUid: "B", signedInEmail: "b@x.com"
        )
    }

    /// A mismatch published in the subscribe gap is delivered because we subscribe
    /// SINCE the captured cursor (the replay-cursor pattern).
    func testReplayFromCapturedCursorDeliversMismatch() async {
        let subscribedCursor = Box<Int?>(nil)
        let event = mismatchEvent()
        var captured: AccountMismatch?
        let monitor = CloudEventsMonitor(
            captureCursor: { 42 },
            subscribe: { since in
                subscribedCursor.value = since
                return AsyncThrowingStream { c in c.yield(event); c.finish() }
            },
            reconcile: { nil },
            onMismatch: { captured = $0 }
        )
        await monitor.runOnce()
        XCTAssertEqual(subscribedCursor.value, 42, "must subscribe since the captured cursor")
        XCTAssertEqual(captured, AccountMismatch(ownerUid: "A", signedInUid: "B", signedInEmail: "b@x.com"))
    }

    /// A 410 (`cursor_unknown`, replay window aged out) reconciles the mismatch
    /// directly from the current uids rather than relying on a snapshot.
    func test410ReconcilesDirectly() async {
        let reconcileCalled = Box(false)
        var captured: AccountMismatch?
        let monitor = CloudEventsMonitor(
            captureCursor: { 7 },
            subscribe: { _ in
                AsyncThrowingStream { c in
                    c.finish(throwing: DaemonClientError.envelopeError(code: "cursor_unknown", rawBody: Data()))
                }
            },
            reconcile: {
                reconcileCalled.value = true
                return AccountMismatch(ownerUid: "A", signedInUid: "B", signedInEmail: nil)
            },
            onMismatch: { captured = $0 }
        )
        await monitor.runOnce()
        XCTAssertTrue(reconcileCalled.value)
        XCTAssertEqual(captured?.signedInUid, "B")
    }

    /// A non-mismatch event never prompts.
    func testNonMismatchEventDoesNotPrompt() async {
        let started = RecorderEventLine(
            type: "started", schemaVersion: 1, forceStopped: nil, permission: nil,
            missing: nil, changes: nil, optOutCommandExamples: nil, cursor: nil,
            reason: nil, reader: nil, ts: nil, ownerUid: nil, signedInUid: nil, signedInEmail: nil
        )
        var captured: AccountMismatch?
        let monitor = CloudEventsMonitor(
            captureCursor: { 1 },
            subscribe: { _ in AsyncThrowingStream { c in c.yield(started); c.finish() } },
            reconcile: { nil },
            onMismatch: { captured = $0 }
        )
        await monitor.runOnce()
        XCTAssertNil(captured)
    }
}
