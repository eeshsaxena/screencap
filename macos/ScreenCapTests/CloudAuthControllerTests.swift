import XCTest
@testable import ScreenCap

/// Fake `CloudAuthService` that lets a test feed `whoami` output, drive the
/// `login` subprocess lifecycle by hand, and observe `logout`. `login` is
/// modelled on the real spawn seam: `startLogin` hands back a fake process
/// handle, and `emitLogin` plays the stdout-envelope-then-exit sequence.
@MainActor
final class FakeCloudAuthService: CloudAuthService {
    // whoami
    var whoamiData = Data()
    var whoamiError: Error?
    private(set) var whoamiCallCount = 0

    func fetchWhoAmI() async throws -> Data {
        whoamiCallCount += 1
        if let whoamiError { throw whoamiError }
        return whoamiData
    }

    // login
    var loginThrows: Error?
    var fakeProcess = FakeSpawnedProcessHandle(isRunning: true, pid: 555, forceKillReturnValue: true)
    private var onStdoutLine: ((String) -> Void)?
    private var onTerminated: ((Int32) -> Void)?
    private(set) var startLoginCount = 0

    func startLogin(
        onStdoutLine: @escaping @MainActor (String) -> Void,
        onTerminated: @escaping @MainActor (Int32) -> Void
    ) throws -> SpawnedProcessHandle {
        startLoginCount += 1
        if let loginThrows { throw loginThrows }
        self.onStdoutLine = onStdoutLine
        self.onTerminated = onTerminated
        return fakeProcess
    }

    /// Drive the login subprocess: optionally emit a stdout line, then exit.
    func emitLogin(line: String?, exitCode: Int32) {
        if let line { onStdoutLine?(line) }
        fakeProcess.isRunning = false
        onTerminated?(exitCode)
    }

    /// Capture the currently-registered termination callback so a test can fire
    /// a *stale* termination after a newer flow has started — the fake otherwise
    /// only remembers the latest flow's callbacks.
    func captureTermination() -> ((Int32) -> Void)? { onTerminated }

    /// Capture the currently-registered stdout callback so a test can fire a
    /// *stale* stdout line after a newer flow has started — mirrors
    /// `captureTermination()` for the generation guard on `appendLoginStdout`.
    func captureStdoutLine() -> ((String) -> Void)? { onStdoutLine }

    // logout
    var logoutThrows: Error?
    private(set) var signOutCount = 0

    func signOut() async throws {
        signOutCount += 1
        if let logoutThrows { throw logoutThrows }
    }

    // billing (U9/U14) — force-refresh whoami, hosted-checkout URL, and the
    // grant-only reconcile self-heal. (The U9 methods gained protocol
    // requirements in the paywall work without the fake being updated; reconcile
    // is added here alongside.)
    var forceRefreshData = Data()
    var forceRefreshError: Error?
    private(set) var forceRefreshCallCount = 0

    func fetchWhoAmIForceRefresh() async throws -> Data {
        forceRefreshCallCount += 1
        if let forceRefreshError { throw forceRefreshError }
        return forceRefreshData
    }

    var checkoutURLData = Data()
    var checkoutURLError: Error?
    private(set) var checkoutURLCallCount = 0

    func fetchCheckoutURL() async throws -> Data {
        checkoutURLCallCount += 1
        if let checkoutURLError { throw checkoutURLError }
        return checkoutURLData
    }

    var reconcileData = Data()
    var reconcileError: Error?
    private(set) var reconcileCallCount = 0

    func fetchReconcileEntitlement() async throws -> Data {
        reconcileCallCount += 1
        if let reconcileError { throw reconcileError }
        return reconcileData
    }
}

enum FakeCloudAuthError: Error, LocalizedError {
    case offline
    case launchFailed
    var errorDescription: String? {
        switch self {
        case .offline: return "offline"
        case .launchFailed: return "launch failed"
        }
    }
}

private let signedInEnvelope = #"{"ok": true, "schema_version": 1, "signed_in": true, "uid": "abc123", "email": "user@example.com"}"#
private let signedOutEnvelope = #"{"ok": true, "schema_version": 1, "signed_in": false}"#
private let staleEnvelope = #"{"ok": true, "schema_version": 1, "signed_in": true, "uid": null, "email": null, "stale": true}"#
private let loginErrorEnvelope = #"{"ok": false, "schema_version": 1, "error": "state mismatch"}"#

@MainActor
final class CloudAuthControllerTests: XCTestCase {

    // MARK: - whoami refresh

    /// Plan scenario: `whoami --json` signed-in → status reflects the account.
    func testRefreshSignedInPopulatesAccount() async {
        let service = FakeCloudAuthService()
        service.whoamiData = Data(signedInEnvelope.utf8)
        let controller = CloudAuthController(service: service)

        await controller.refresh()

        XCTAssertTrue(controller.isSignedIn)
        XCTAssertEqual(controller.status, .signedIn(email: "user@example.com", uid: "abc123", stale: false))
        XCTAssertEqual(controller.status.accountLabel, "user@example.com")
    }

    /// Plan scenario: `whoami --json` signed-out → status is signed out.
    func testRefreshSignedOut() async {
        let service = FakeCloudAuthService()
        service.whoamiData = Data(signedOutEnvelope.utf8)
        let controller = CloudAuthController(service: service)

        await controller.refresh()

        XCTAssertFalse(controller.isSignedIn)
        XCTAssertEqual(controller.status, .signedOut)
    }

    /// Plan scenario: malformed / empty `whoami` output is tolerated — the
    /// controller lands on a safe signed-out state, never crashes.
    func testRefreshMalformedOutputResolvesToSignedOut() async {
        let service = FakeCloudAuthService()
        service.whoamiData = Data("not json at all {".utf8)
        let controller = CloudAuthController(service: service)

        await controller.refresh()
        XCTAssertEqual(controller.status, .signedOut)

        // Empty output (e.g. the CLI printed nothing) is also tolerated.
        service.whoamiData = Data()
        await controller.refresh()
        XCTAssertEqual(controller.status, .signedOut)
    }

    /// A `whoami` shell-out failure (launch error / timeout / non-zero) is
    /// treated as signed-out rather than surfaced — sign-in state must not
    /// crash a surface.
    func testRefreshServiceErrorResolvesToSignedOut() async {
        let service = FakeCloudAuthService()
        service.whoamiError = FakeCloudAuthError.offline
        let controller = CloudAuthController(service: service)

        await controller.refresh()

        XCTAssertEqual(controller.status, .signedOut)
    }

    /// A newer `schema_version` than the Swift side knows is processed anyway
    /// (drift-tolerant, like the upload/daemon event consumers) — a Python-side
    /// envelope bump must not lock the user out.
    func testRefreshToleratesFutureSchemaVersion() async {
        let service = FakeCloudAuthService()
        service.whoamiData = Data(#"{"ok": true, "schema_version": 99, "signed_in": true, "uid": "u", "email": "e@x.io"}"#.utf8)
        let controller = CloudAuthController(service: service)

        await controller.refresh()

        XCTAssertTrue(controller.isSignedIn)
        XCTAssertEqual(controller.status.accountLabel, "e@x.io")
    }

    /// Stale (offline-but-credentialed): `signed_in: true` with null identity
    /// still counts as signed in, with no account label to show.
    func testRefreshStaleIsSignedInWithoutLabel() async {
        let service = FakeCloudAuthService()
        service.whoamiData = Data(staleEnvelope.utf8)
        let controller = CloudAuthController(service: service)

        await controller.refresh()

        XCTAssertTrue(controller.isSignedIn)
        XCTAssertEqual(controller.status, .signedIn(email: nil, uid: nil, stale: true))
        XCTAssertNil(controller.status.accountLabel)
    }

    /// Signed in with a uid but no email → the account label falls back to the
    /// uid (email is preferred when present, uid otherwise).
    func testRefreshUidOnlyAccountLabelFallsBackToUid() async {
        let service = FakeCloudAuthService()
        service.whoamiData = Data(#"{"ok":true,"schema_version":1,"signed_in":true,"uid":"abc123","email":null}"#.utf8)
        let controller = CloudAuthController(service: service)

        await controller.refresh()

        XCTAssertTrue(controller.isSignedIn)
        XCTAssertEqual(controller.status.accountLabel, "abc123")
    }

    // MARK: - lazy refresh (SCR-241: no eager Keychain decrypt at launch)

    /// A freshly-constructed controller does NO auth work: status stays
    /// `.unknown` and no `whoami` shell-out (hence no Keychain decrypt) fires
    /// until something calls `refreshIfNeeded`/`refresh`. This is the invariant
    /// the launch-prompt fix rests on — the app must be able to build the
    /// controller at launch without touching the Keychain.
    func testInitialStateDoesNoAuthWork() {
        let service = FakeCloudAuthService()
        service.whoamiData = Data(signedInEnvelope.utf8)

        let controller = CloudAuthController(service: service)

        XCTAssertEqual(controller.status, .unknown)
        XCTAssertEqual(service.whoamiCallCount, 0, "constructing the controller must not decrypt the Keychain")
    }

    /// From the initial `.unknown` state, `refreshIfNeeded` resolves status by
    /// shelling out to `whoami` exactly once — the lazy replacement for the
    /// launch-time probe that decrypts the Keychain only when a cloud surface
    /// actually appears.
    func testRefreshIfNeededResolvesFromUnknown() async {
        let service = FakeCloudAuthService()
        service.whoamiData = Data(signedInEnvelope.utf8)
        let controller = CloudAuthController(service: service)
        XCTAssertEqual(controller.status, .unknown)

        await controller.refreshIfNeeded()

        XCTAssertEqual(service.whoamiCallCount, 1)
        XCTAssertTrue(controller.isSignedIn)
    }

    /// Once status has resolved, `refreshIfNeeded` is a no-op — it must NOT
    /// re-decrypt the Keychain on every subsequent cloud-surface appearance
    /// (menu reopened, another review window), which is exactly the repeated
    /// prompting this fix exists to avoid.
    func testRefreshIfNeededNoOpsOnceResolved() async {
        let service = FakeCloudAuthService()
        service.whoamiData = Data(signedInEnvelope.utf8)
        let controller = CloudAuthController(service: service)

        await controller.refreshIfNeeded()
        await controller.refreshIfNeeded()
        await controller.refreshIfNeeded()

        XCTAssertEqual(service.whoamiCallCount, 1, "resolved status must not re-shell whoami")
    }

    /// Two cloud surfaces appearing at once (e.g. the menu-bar account section
    /// while cloud onboarding is on screen) must share a single in-flight
    /// refresh — two concurrent decrypts would risk two authorization prompts.
    func testRefreshIfNeededCoalescesConcurrentCallers() async {
        let service = FakeCloudAuthService()
        service.whoamiData = Data(signedInEnvelope.utf8)
        let controller = CloudAuthController(service: service)

        async let first: Void = controller.refreshIfNeeded()
        async let second: Void = controller.refreshIfNeeded()
        _ = await (first, second)

        XCTAssertEqual(service.whoamiCallCount, 1, "concurrent callers must not each decrypt the Keychain")
        XCTAssertTrue(controller.isSignedIn)
    }

    /// A failed lazy refresh (offline) still resolves status away from
    /// `.unknown` (to signed-out, matching `refresh`), so `refreshIfNeeded`
    /// doesn't retry-shell on every appearance while offline.
    func testRefreshIfNeededTreatsFailureAsResolved() async {
        let service = FakeCloudAuthService()
        service.whoamiError = FakeCloudAuthError.offline
        let controller = CloudAuthController(service: service)

        await controller.refreshIfNeeded()
        await controller.refreshIfNeeded()

        XCTAssertEqual(service.whoamiCallCount, 1)
        XCTAssertEqual(controller.status, .signedOut)
    }

    // MARK: - sign in

    /// Plan scenario: a successful `login` shell-out signs the user in and the
    /// completion fires true (so the gated Upload can proceed).
    func testSignInSuccessSignsInAndReportsTrue() {
        let service = FakeCloudAuthService()
        let controller = CloudAuthController(service: service)
        var reported: Bool?

        controller.startSignIn { reported = $0 }
        XCTAssertEqual(controller.signInFlow, .inProgress)

        service.emitLogin(line: signedInEnvelope, exitCode: 0)

        XCTAssertEqual(reported, true)
        XCTAssertEqual(controller.signInFlow, .idle)
        XCTAssertTrue(controller.isSignedIn)
        XCTAssertEqual(controller.status.accountLabel, "user@example.com")
    }

    /// Plan scenario: `login` exits non-zero → the flow surfaces the reason and
    /// the completion fires false; no crash, no frozen state.
    func testSignInFailureSurfacesReasonAndReportsFalse() {
        let service = FakeCloudAuthService()
        let controller = CloudAuthController(service: service)
        var reported: Bool?

        controller.startSignIn { reported = $0 }
        service.emitLogin(line: loginErrorEnvelope, exitCode: 1)

        XCTAssertEqual(reported, false)
        XCTAssertEqual(controller.signInFlow, .failed("state mismatch"))
        XCTAssertFalse(controller.isSignedIn)
    }

    /// A non-zero exit with no parseable envelope (e.g. browser closed, the CLI
    /// printed nothing structured) still lands on a failed flow with a generic
    /// reason — never silent.
    func testSignInFailureWithoutEnvelopeUsesGenericReason() {
        let service = FakeCloudAuthService()
        let controller = CloudAuthController(service: service)
        var reported: Bool?

        controller.startSignIn { reported = $0 }
        service.emitLogin(line: nil, exitCode: 1)

        XCTAssertEqual(reported, false)
        if case .failed(let reason) = controller.signInFlow {
            XCTAssertFalse(reason.isEmpty)
        } else {
            XCTFail("expected failed flow, got \(controller.signInFlow)")
        }
    }

    /// A spawn launch failure (binary not found) maps to a failed flow
    /// immediately rather than hanging in `.inProgress`.
    func testSignInLaunchFailureLandsOnFailed() {
        let service = FakeCloudAuthService()
        service.loginThrows = FakeCloudAuthError.launchFailed
        let controller = CloudAuthController(service: service)
        var reported: Bool?

        controller.startSignIn { reported = $0 }

        XCTAssertEqual(reported, false)
        XCTAssertEqual(controller.signInFlow, .failed("launch failed"))
    }

    /// Cancel terminates the in-flight login and resets to idle (not failed) —
    /// cancelling reads as "never mind", and the subsequent SIGTERM-driven exit
    /// must not flip the flow to failed.
    func testCancelSignInTerminatesAndResetsToIdle() {
        let service = FakeCloudAuthService()
        let controller = CloudAuthController(service: service)
        var reported: Bool?

        controller.startSignIn { reported = $0 }
        controller.cancelSignIn()

        XCTAssertEqual(service.fakeProcess.terminateInvocations, 1)
        XCTAssertEqual(controller.signInFlow, .idle)

        // The SIGTERM-driven process exit arrives afterward; it must keep the
        // flow idle and report false, not surface a failure.
        service.emitLogin(line: nil, exitCode: 130)
        XCTAssertEqual(controller.signInFlow, .idle)
        XCTAssertEqual(reported, false)
        XCTAssertFalse(controller.isSignedIn)
    }

    /// Regression: cancelling then immediately restarting must not let the
    /// cancelled login's delayed exit clobber the fresh attempt. The stale
    /// termination is ignored (generation guard), and the new flow completes
    /// normally.
    func testCancelThenRestartIgnoresStaleTermination() {
        let service = FakeCloudAuthService()
        let controller = CloudAuthController(service: service)
        var firstResult: Bool?
        var secondResult: Bool?

        // Flow A: start, capture its termination callback, then cancel.
        controller.startSignIn { firstResult = $0 }
        let staleTermination = service.captureTermination()
        controller.cancelSignIn()
        XCTAssertEqual(firstResult, false)
        XCTAssertEqual(controller.signInFlow, .idle)

        // Flow B: a fresh attempt (new process handle).
        service.fakeProcess = FakeSpawnedProcessHandle(isRunning: true, pid: 777, forceKillReturnValue: true)
        controller.startSignIn { secondResult = $0 }
        XCTAssertEqual(controller.signInFlow, .inProgress)

        // Flow A's delayed SIGTERM-exit lands now — it must be ignored.
        staleTermination?(130)
        XCTAssertEqual(controller.signInFlow, .inProgress, "stale exit must not touch the live flow")
        XCTAssertNil(secondResult, "Flow B must not be completed by Flow A's exit")

        // Flow B completes normally.
        service.emitLogin(line: signedInEnvelope, exitCode: 0)
        XCTAssertEqual(secondResult, true)
        XCTAssertTrue(controller.isSignedIn)
    }

    /// A clean (exit 0) `login` whose final envelope reports signed-out is a
    /// failure, not a success: the user closed the browser / declined, so the
    /// flow surfaces a reason, stays signed out, and reports false — never a
    /// false-positive "signed in".
    func testSignInCleanExitButNotSignedInIsFailure() {
        let service = FakeCloudAuthService()
        let controller = CloudAuthController(service: service)
        var reported: Bool?

        controller.startSignIn { reported = $0 }
        service.emitLogin(line: signedOutEnvelope, exitCode: 0)

        if case .failed(let reason) = controller.signInFlow {
            XCTAssertFalse(reason.isEmpty)
        } else {
            XCTFail("expected failed flow, got \(controller.signInFlow)")
        }
        XCTAssertFalse(controller.isSignedIn)
        XCTAssertEqual(reported, false)
    }

    /// Regression: a stale stdout line from a cancelled flow must not feed the
    /// live flow's accumulated login output (generation guard on
    /// `appendLoginStdout`). Flow A's leftover signed-in envelope, fired after
    /// Flow B starts, must not drive Flow B's result.
    func testStaleStdoutDoesNotAffectLiveFlow() {
        let service = FakeCloudAuthService()
        let controller = CloudAuthController(service: service)
        var firstResult: Bool?
        var secondResult: Bool?

        // Flow A: start, capture its stdout callback, then cancel.
        controller.startSignIn { firstResult = $0 }
        let staleStdout = service.captureStdoutLine()
        controller.cancelSignIn()
        XCTAssertEqual(firstResult, false)

        // Flow B: a fresh attempt (new process handle).
        service.fakeProcess = FakeSpawnedProcessHandle(isRunning: true, pid: 888, forceKillReturnValue: true)
        controller.startSignIn { secondResult = $0 }
        XCTAssertEqual(controller.signInFlow, .inProgress)

        // Flow A's delayed signed-in stdout lands now — the generation guard
        // must drop it so it never enters Flow B's accumulated output.
        staleStdout?(signedInEnvelope)

        // Flow B exits non-zero with no envelope of its own: if the stale line
        // had leaked in, the controller would resolve signed-in; it must not.
        service.emitLogin(line: nil, exitCode: 1)
        XCTAssertEqual(secondResult, false, "stale envelope must not drive the live flow's result")
        XCTAssertFalse(controller.isSignedIn)
    }

    /// Dismissing a lingering `.failed` flow (sheet Cancel/Close from the failed
    /// state) resets it to `.idle` so the failure doesn't stick on the shared
    /// menu-bar surface.
    func testCancelClearsLingeringFailure() {
        let service = FakeCloudAuthService()
        let controller = CloudAuthController(service: service)

        controller.startSignIn()
        service.emitLogin(line: loginErrorEnvelope, exitCode: 1)
        XCTAssertEqual(controller.signInFlow, .failed("state mismatch"))

        controller.cancelSignIn()
        XCTAssertEqual(controller.signInFlow, .idle)
    }

    /// A second `startSignIn` while one is in flight is a no-op (one browser
    /// round-trip at a time): no second spawn, and the second caller's
    /// completion reports false rather than hijacking the live attempt's gate.
    func testSecondSignInWhileInProgressIsNoOp() {
        let service = FakeCloudAuthService()
        let controller = CloudAuthController(service: service)
        var secondResult: Bool?

        controller.startSignIn()
        controller.startSignIn { secondResult = $0 }

        XCTAssertEqual(service.startLoginCount, 1)
        XCTAssertEqual(secondResult, false, "the spurned second caller must be told not-now")
    }

    // MARK: - sign out + upload coordination

    /// Plan scenario: Sign Out is disabled while an upload is in flight, and
    /// re-enabled once it finishes. The active-upload count now lives in
    /// `UploadCoordinator`; the controller reads it through the injected
    /// in-flight closure for `canSignOut`.
    func testCanSignOutGatesOnActiveUploads() async {
        let service = FakeCloudAuthService()
        service.whoamiData = Data(signedInEnvelope.utf8)
        let coordinator = UploadCoordinator()
        let controller = CloudAuthController(
            service: service,
            isUploadInFlight: { coordinator.isUploadInFlight }
        )
        await controller.refresh()

        XCTAssertTrue(controller.canSignOut)

        coordinator.uploadDidStart()
        XCTAssertFalse(controller.canSignOut, "sign out must be disabled mid-upload")

        coordinator.uploadDidFinish()
        XCTAssertTrue(controller.canSignOut)
    }

    /// `UploadCoordinator.uploadDidFinish` never drives the count below zero (an
    /// unbalanced finish must not silently re-enable while another upload runs).
    func testUploadCountClampsAtZero() async {
        let service = FakeCloudAuthService()
        service.whoamiData = Data(signedInEnvelope.utf8)
        let coordinator = UploadCoordinator()
        let controller = CloudAuthController(
            service: service,
            isUploadInFlight: { coordinator.isUploadInFlight }
        )
        await controller.refresh()

        coordinator.uploadDidFinish() // unbalanced
        coordinator.uploadDidStart()
        XCTAssertFalse(controller.canSignOut, "a stray finish must not mask a real start")
    }

    /// Sign out clears local state and invokes the CLI logout.
    func testSignOutClearsStatus() async {
        let service = FakeCloudAuthService()
        service.whoamiData = Data(signedInEnvelope.utf8)
        let controller = CloudAuthController(service: service)
        await controller.refresh()
        XCTAssertTrue(controller.isSignedIn)

        await controller.signOut()

        XCTAssertEqual(service.signOutCount, 1)
        XCTAssertEqual(controller.status, .signedOut)
    }

    /// Sign out refuses while an upload is in flight (defensive — the menu also
    /// disables the control) so an in-flight signed-URL request can't hit
    /// NotSignedIn mid-upload.
    func testSignOutNoOpsDuringUpload() async {
        let service = FakeCloudAuthService()
        service.whoamiData = Data(signedInEnvelope.utf8)
        let coordinator = UploadCoordinator()
        let controller = CloudAuthController(
            service: service,
            isUploadInFlight: { coordinator.isUploadInFlight }
        )
        await controller.refresh()
        coordinator.uploadDidStart()

        await controller.signOut()

        XCTAssertEqual(service.signOutCount, 0)
        XCTAssertTrue(controller.isSignedIn)
    }

    /// Even if the CLI logout shell-out fails, local state is cleared — leaving
    /// the user "signed in" after they asked to sign out is the worse failure.
    func testSignOutClearsStateEvenWhenLogoutThrows() async {
        let service = FakeCloudAuthService()
        service.whoamiData = Data(signedInEnvelope.utf8)
        service.logoutThrows = FakeCloudAuthError.launchFailed
        let controller = CloudAuthController(service: service)
        await controller.refresh()

        await controller.signOut()

        XCTAssertEqual(controller.status, .signedOut)
    }

    // MARK: - entitlement: paywall flag + reconcile (billing KTD-6 / U14)

    /// The client paywall flag (KTD-6) rides the whoami envelope and drives the
    /// app's soft gate. Present → on; absent → off (pre-billing behavior).
    func testRefreshReadsPaywallEnabledFlag() async {
        let service = FakeCloudAuthService()
        service.whoamiData = Data(#"{"ok":true,"schema_version":1,"signed_in":false,"paywall_enabled":true}"#.utf8)
        let controller = CloudAuthController(service: service)

        await controller.refresh()
        XCTAssertTrue(controller.paywallEnabled)

        // An envelope without the key resolves to off (older CLI / flag off).
        service.whoamiData = Data(signedOutEnvelope.utf8)
        await controller.refresh()
        XCTAssertFalse(controller.paywallEnabled, "absent paywall_enabled must resolve to off")
    }

    /// "I've paid — check now" reconciles FIRST (grant-only self-heal for a
    /// dropped checkout webhook) and THEN force-refreshes, so a just-granted
    /// claim is observed locally without a re-login (U14).
    func testReconcileEntitlementReconcilesThenRefreshes() async {
        let service = FakeCloudAuthService()
        service.reconcileData = Data(#"{"ok":true,"schema_version":1,"subscribed":true}"#.utf8)
        service.forceRefreshData = Data(#"{"ok":true,"schema_version":1,"signed_in":true,"uid":"u","email":"e@x.io","subscribed":true,"paywall_enabled":true}"#.utf8)
        let controller = CloudAuthController(service: service)

        await controller.reconcileEntitlement()

        XCTAssertEqual(service.reconcileCallCount, 1)
        XCTAssertEqual(service.forceRefreshCallCount, 1, "must force-refresh after reconcile to observe the grant")
        XCTAssertTrue(controller.isSubscribed)
        XCTAssertTrue(controller.paywallEnabled)
    }

    /// Reconcile failure is non-fatal: the controller still force-refreshes (the
    /// webhook may have already landed), so a transient reconcile error never
    /// blocks a paying customer from being recognized.
    func testReconcileEntitlementStillRefreshesWhenReconcileFails() async {
        let service = FakeCloudAuthService()
        service.reconcileError = FakeCloudAuthError.offline
        service.forceRefreshData = Data(#"{"ok":true,"schema_version":1,"signed_in":true,"uid":"u","email":"e@x.io","subscribed":true}"#.utf8)
        let controller = CloudAuthController(service: service)

        await controller.reconcileEntitlement()

        XCTAssertEqual(service.forceRefreshCallCount, 1)
        XCTAssertTrue(controller.isSubscribed)
    }
}
