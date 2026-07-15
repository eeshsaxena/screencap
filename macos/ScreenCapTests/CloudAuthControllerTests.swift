import AppKit
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
    /// The tier the last `fetchCheckoutURL(tier:)` was called with (U11) — lets
    /// a test assert Local Pro passes `"local"` and Cloud passes `"cloud"`.
    private(set) var lastCheckoutTier: String?
    /// When true, each `fetchCheckoutURL` suspends until the test resumes it
    /// via `releaseNextHeldCheckout()` (FIFO) — lets a test interleave a
    /// superseded attempt's LATE failure with a live attempt, pinning the
    /// checkout generation guard.
    var holdCheckoutCalls = false
    private var heldCheckouts: [CheckedContinuation<Void, Never>] = []
    /// How many `fetchCheckoutURL` calls are currently suspended.
    var suspendedCheckoutCount: Int { heldCheckouts.count }

    func releaseNextHeldCheckout() {
        guard !heldCheckouts.isEmpty else { return }
        heldCheckouts.removeFirst().resume()
    }

    func fetchCheckoutURL(tier: String) async throws -> Data {
        checkoutURLCallCount += 1
        lastCheckoutTier = tier
        if holdCheckoutCalls {
            await withCheckedContinuation { heldCheckouts.append($0) }
        }
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

    // portal (account sheet U3) — the hosted customer-portal URL mint.
    var portalURLData = Data()
    var portalURLError: Error?
    private(set) var portalURLCallCount = 0

    func fetchPortalURL() async throws -> Data {
        portalURLCallCount += 1
        if let portalURLError { throw portalURLError }
        return portalURLData
    }
}

/// Records what the controller's browser-open seam would launch, so tests can
/// assert on the exact URL (and that nothing opens on the guarded paths)
/// without `NSWorkspace` actually spawning a browser.
@MainActor
final class URLOpenRecorder {
    private(set) var opened: [URL] = []
    func open(_ url: URL) { opened.append(url) }
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

private let signedInEnvelope = #"{"ok": true, "schema_version": 2, "signed_in": true, "uid": "abc123", "email": "user@example.com"}"#
private let signedOutEnvelope = #"{"ok": true, "schema_version": 2, "signed_in": false}"#
private let staleEnvelope = #"{"ok": true, "schema_version": 2, "signed_in": true, "uid": null, "email": null, "stale": true}"#
private let loginErrorEnvelope = #"{"ok": false, "schema_version": 2, "error": "state mismatch"}"#

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
        service.whoamiData = Data(#"{"ok":true,"schema_version":2,"signed_in":true,"uid":"abc123","email":null}"#.utf8)
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
        service.whoamiData = Data(#"{"ok":true,"schema_version":2,"signed_in":false,"paywall_enabled":true}"#.utf8)
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
        service.reconcileData = Data(#"{"ok":true,"schema_version":2,"subscribed":true}"#.utf8)
        service.forceRefreshData = Data(#"{"ok":true,"schema_version":2,"signed_in":true,"uid":"u","email":"e@x.io","subscribed":true,"paywall_enabled":true}"#.utf8)
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
        service.forceRefreshData = Data(#"{"ok":true,"schema_version":2,"signed_in":true,"uid":"u","email":"e@x.io","subscribed":true}"#.utf8)
        let controller = CloudAuthController(service: service)

        await controller.reconcileEntitlement()

        XCTAssertEqual(service.forceRefreshCallCount, 1)
        XCTAssertTrue(controller.isSubscribed)
    }

    // MARK: - two-tier entitlement + trial state (paid-only launch, U11)

    /// A `tier=cloud` token resolves to `.cloud` AND `subscribed=true` (the
    /// derived signal); `tier=local` resolves to `.localPro` with `subscribed`
    /// false (Local Pro has no cloud upload — R3/KTD-1).
    func testRefreshReadsTwoTierEntitlement() async {
        let service = FakeCloudAuthService()
        service.whoamiData = Data(#"{"ok":true,"schema_version":2,"signed_in":true,"uid":"u","email":"e@x.io","subscribed":true,"tier":"cloud"}"#.utf8)
        let controller = CloudAuthController(service: service)

        await controller.refresh()
        XCTAssertEqual(controller.tier, .cloud)
        XCTAssertTrue(controller.isSubscribed)

        service.whoamiData = Data(#"{"ok":true,"schema_version":2,"signed_in":true,"uid":"u","email":"e@x.io","subscribed":false,"tier":"local"}"#.utf8)
        await controller.refresh()
        XCTAssertEqual(controller.tier, .localPro)
        XCTAssertFalse(controller.isSubscribed, "Local Pro is not the cloud entitlement")
    }

    /// No tier claim → `.none`; the app treats this as not-entitled (fresh or
    /// lapsed, disambiguated from offline by `stale`).
    func testRefreshNoTierIsNone() async {
        let service = FakeCloudAuthService()
        service.whoamiData = Data(#"{"ok":true,"schema_version":2,"signed_in":true,"uid":"u","email":"e@x.io"}"#.utf8)
        let controller = CloudAuthController(service: service)

        await controller.refresh()
        XCTAssertEqual(controller.tier, .none)
        XCTAssertEqual(controller.trialState, .lapsed, "signed-in, no tier, no trial → lapsed/not-entitled")
    }

    func testLegacySubscribedClaimIsCloudAndNotGatedForLapse() async {
        let service = FakeCloudAuthService()
        service.whoamiData = Data(#"{"ok":true,"schema_version":2,"signed_in":true,"uid":"u","email":"e@x.io","subscribed":true,"paywall_enabled":true}"#.utf8)
        let controller = CloudAuthController(service: service)

        await controller.refresh()

        XCTAssertTrue(controller.isSubscribed)
        XCTAssertEqual(controller.tier, .cloud)
        XCTAssertEqual(controller.trialState, .subscribed)
        XCTAssertFalse(controller.isGatedForLapse)
    }

    func testExplicitLocalTierOverridesLegacySubscribedClaim() async {
        let service = FakeCloudAuthService()
        service.whoamiData = Data(#"{"ok":true,"schema_version":2,"signed_in":true,"uid":"u","email":"e@x.io","subscribed":true,"tier":"local","paywall_enabled":true}"#.utf8)
        let controller = CloudAuthController(service: service)

        await controller.refresh()

        XCTAssertFalse(controller.isSubscribed)
        XCTAssertEqual(controller.tier, .localPro)
        XCTAssertEqual(controller.trialState, .subscribed)
        XCTAssertFalse(controller.isGatedForLapse)
    }

    func testUnknownExplicitTierDoesNotUseLegacySubscribedFallback() async {
        let service = FakeCloudAuthService()
        service.whoamiData = Data(#"{"ok":true,"schema_version":2,"signed_in":true,"uid":"u","email":"e@x.io","subscribed":true,"tier":"future","paywall_enabled":true}"#.utf8)
        let controller = CloudAuthController(service: service)

        await controller.refresh()

        XCTAssertFalse(controller.isSubscribed)
        XCTAssertEqual(controller.tier, .none)
        XCTAssertEqual(controller.trialState, .lapsed)
        XCTAssertTrue(controller.isGatedForLapse)
    }

    func testSignedOutEnvelopeDoesNotUseLegacySubscribedFallback() async {
        let service = FakeCloudAuthService()
        service.whoamiData = Data(#"{"ok":true,"schema_version":2,"signed_in":false,"subscribed":true,"paywall_enabled":true}"#.utf8)
        let controller = CloudAuthController(service: service)

        await controller.refresh()

        XCTAssertEqual(controller.status, .signedOut)
        XCTAssertFalse(controller.isSubscribed)
        XCTAssertEqual(controller.tier, .none)
    }

    /// Offline-stale must NOT read as "lapsed/expired" (KTD-4 grace): `tier` is
    /// nil WITH `stale=true`, so the trial state is `.indeterminate` and no
    /// spurious paywall surfaces for an offline payer.
    func testStaleEntitlementIsIndeterminateNotLapsed() async {
        let service = FakeCloudAuthService()
        service.whoamiData = Data(#"{"ok":true,"schema_version":2,"signed_in":true,"uid":null,"email":null,"stale":true}"#.utf8)
        let controller = CloudAuthController(service: service)

        await controller.refresh()
        XCTAssertEqual(controller.tier, .none)
        XCTAssertEqual(controller.trialState, .indeterminate, "a stale token must not read as expired")
    }

    /// A live `trial_end` in the future drives the active-trial state; a
    /// converted subscription (tier present, no `trial_end`) is `.subscribed`.
    func testTrialEndDrivesTrialState() async {
        let service = FakeCloudAuthService()
        let farFuture = Int(Date().timeIntervalSince1970) + 10 * 86_400
        service.whoamiData = Data(#"{"ok":true,"schema_version":2,"signed_in":true,"uid":"u","email":"e@x.io","subscribed":true,"tier":"cloud","trial_end":\#(farFuture)}"#.utf8)
        let controller = CloudAuthController(service: service)

        await controller.refresh()
        if case .active(let days) = controller.trialState {
            XCTAssertGreaterThan(days, 3, "10 days out is a calm active trial, not near-expiry")
        } else {
            XCTFail("expected .active trial, got \(controller.trialState)")
        }

        // Converted: tier present, no trial_end → subscribed (no trial banner).
        service.whoamiData = Data(#"{"ok":true,"schema_version":2,"signed_in":true,"uid":"u","email":"e@x.io","subscribed":true,"tier":"cloud"}"#.utf8)
        await controller.refresh()
        XCTAssertEqual(controller.trialState, .subscribed)
    }

    /// Sign out clears the two-tier entitlement too (not just `status`).
    func testSignOutClearsTierAndTrialState() async {
        let service = FakeCloudAuthService()
        service.whoamiData = Data(#"{"ok":true,"schema_version":2,"signed_in":true,"uid":"u","email":"e@x.io","subscribed":true,"tier":"cloud"}"#.utf8)
        let controller = CloudAuthController(service: service)
        await controller.refresh()
        XCTAssertEqual(controller.tier, .cloud)

        await controller.signOut()
        XCTAssertEqual(controller.tier, .none)
        XCTAssertEqual(controller.trialState, .indeterminate)
    }

    // MARK: - lapse gating (U12)

    /// A definitively lapsed user (signed-in, no tier, no live trial) WITH the
    /// paywall enabled gates the record + recall affordances.
    func testIsGatedForLapseWhenLapsedAndPaywallOn() async {
        let service = FakeCloudAuthService()
        service.whoamiData = Data(#"{"ok":true,"schema_version":2,"signed_in":true,"uid":"u","email":"e@x.io","paywall_enabled":true}"#.utf8)
        let controller = CloudAuthController(service: service)

        await controller.refresh()
        XCTAssertEqual(controller.trialState, .lapsed)
        XCTAssertTrue(controller.isGatedForLapse, "lapsed + paywall on must gate")
    }

    /// The paywall flag is the master switch: a lapsed user with the paywall OFF
    /// (pre-billing / dark) is never gated.
    func testNotGatedWhenPaywallOff() async {
        let service = FakeCloudAuthService()
        service.whoamiData = Data(#"{"ok":true,"schema_version":2,"signed_in":true,"uid":"u","email":"e@x.io"}"#.utf8)
        let controller = CloudAuthController(service: service)

        await controller.refresh()
        XCTAssertEqual(controller.trialState, .lapsed)
        XCTAssertFalse(controller.isGatedForLapse, "paywall off → never gate (dark)")
    }

    /// KTD-4 grace (plan point 4): an offline-stale token reads `.indeterminate`,
    /// never `.lapsed`, so an offline payer within the daemon lease is NOT gated
    /// even with the paywall on.
    func testNotGatedWhenStaleEvenWithPaywallOn() async {
        let service = FakeCloudAuthService()
        service.whoamiData = Data(#"{"ok":true,"schema_version":2,"signed_in":true,"uid":null,"email":null,"stale":true,"paywall_enabled":true}"#.utf8)
        let controller = CloudAuthController(service: service)

        await controller.refresh()
        XCTAssertEqual(controller.trialState, .indeterminate)
        XCTAssertFalse(controller.isGatedForLapse, "an offline-stale payer must not be gated (KTD-4)")
    }

    /// An entitled user (active trial or converted subscriber) is never gated,
    /// paywall on or off.
    func testNotGatedWhenEntitled() async {
        let service = FakeCloudAuthService()
        service.whoamiData = Data(#"{"ok":true,"schema_version":2,"signed_in":true,"uid":"u","email":"e@x.io","subscribed":false,"tier":"local","paywall_enabled":true}"#.utf8)
        let controller = CloudAuthController(service: service)

        await controller.refresh()
        XCTAssertEqual(controller.tier, .localPro)
        XCTAssertFalse(controller.isGatedForLapse, "an entitled Local Pro user must not be gated")

        service.whoamiData = Data(#"{"ok":true,"schema_version":2,"signed_in":true,"uid":"u","email":"e@x.io","subscribed":true,"tier":"cloud","paywall_enabled":true}"#.utf8)
        await controller.refresh()
        XCTAssertFalse(controller.isGatedForLapse, "an entitled Cloud user must not be gated")
    }

    /// Checkout passes the chosen tier to the price-selection call: Local Pro
    /// sends `"local"`, Cloud sends `"cloud"` (U11 / KTD-2 — price only).
    func testStartCheckoutPassesTier() async {
        let service = FakeCloudAuthService()
        service.checkoutURLData = Data(#"{"ok":true,"url":"https://example.com/checkout"}"#.utf8)
        let recorder = URLOpenRecorder()
        let controller = CloudAuthController(service: service, openURL: { recorder.open($0) })

        controller.startCheckout(tier: .localPro)
        await waitUntil("local checkout must run") { service.lastCheckoutTier == "local" }

        controller.startCheckout(tier: .cloud)
        await waitUntil("cloud checkout must run") { service.lastCheckoutTier == "cloud" }
    }

    /// A `.none` checkout tier (no plan chosen) never opens a checkout and
    /// surfaces a "pick a plan" reason rather than minting a URL.
    func testStartCheckoutRejectsNoneTier() async {
        let service = FakeCloudAuthService()
        let controller = CloudAuthController(service: service)
        var failure: String?

        controller.startCheckout(tier: .none) { failure = $0 }
        await Task.yield()

        XCTAssertEqual(service.checkoutURLCallCount, 0, "no tier → no Stripe call")
        XCTAssertNotNil(failure)
    }

    /// End-to-end regression for the checkout error path: `checkout-url` reports
    /// failure as a `{ok:false, error}` envelope on **stdout** and exits 1 with
    /// *empty* stderr. This drives the real `LiveCloudAuthService` → `CLIClient`
    /// path (which the `FakeCloudAuthService` seam bypasses) against a fake
    /// `screencap`, and asserts `startCheckout` surfaces the error seam's static
    /// copy — never the bare "screencap exited with code 1:" (the pre-
    /// `allowNonZeroExit` bug) and never the envelope's dynamic `error` text,
    /// which here carries a terminal instruction the app must not render (R10;
    /// the raw-reason passthrough this test used to pin was the KTD-5 leak).
    func testStartCheckoutSurfacesEnvelopeErrorNotBareExitCode() async throws {
        fakeCLI = try FakeCLIBinary(
            stdout: #"{"ok": false, "error": "Sign in to upgrade: run `screencap login`."}"#,
            exitCode: 1
        )
        let recorder = URLOpenRecorder()
        let controller = CloudAuthController(
            service: LiveCloudAuthService(), openURL: { recorder.open($0) }
        )

        let failure = await withCheckedContinuation { (cont: CheckedContinuation<String, Never>) in
            controller.startCheckout(tier: .localPro) { cont.resume(returning: $0) }
        }

        XCTAssertEqual(failure, AccountErrorCopy.unknown.message)
        XCTAssertFalse(
            failure.contains("exited with code"),
            "mapped copy must surface, never a bare CLI exit code"
        )
        XCTAssertFalse(
            failure.contains("screencap login"),
            "envelope text with terminal instructions must never render (R10)"
        )
        XCTAssertTrue(recorder.opened.isEmpty)
    }

    // MARK: - portal / Manage Subscription (account sheet U3)

    /// Plan scenario: `startManageSubscription` happy path — the portal URL is
    /// minted and handed to the browser-open seam exactly once, the return
    /// refresh is armed, and no error surfaces. The URL value itself lives only
    /// in the envelope → open call (it is never written to os_log/print — a
    /// portal link grants access to the user's billing page).
    func testStartManageSubscriptionOpensPortalURL() async {
        let service = FakeCloudAuthService()
        service.portalURLData = Data(#"{"ok":true,"schema_version":2,"url":"https://billing.stripe.com/p/session_abc"}"#.utf8)
        let recorder = URLOpenRecorder()
        let controller = CloudAuthController(service: service, openURL: { recorder.open($0) })

        controller.startManageSubscription()
        await waitUntil("portal URL must reach the open seam") { recorder.opened.count == 1 }

        XCTAssertEqual(recorder.opened.first?.absoluteString, "https://billing.stripe.com/p/session_abc")
        XCTAssertEqual(service.portalURLCallCount, 1)
        XCTAssertNil(controller.accountError)
        XCTAssertTrue(controller.portalReturnPending, "a successful open must arm the browser-return refresh (R7)")
    }

    /// Plan scenario (AE4): a portal mint failure surfaces the MAPPED copy —
    /// keyed on the envelope's `code`, never its dynamic `error` text.
    func testStartManageSubscriptionFailureSurfacesMappedCopyNotRawText() async {
        let service = FakeCloudAuthService()
        service.portalURLData = Data(#"{"ok":false,"schema_version":2,"error":"Subscription service error: upstream 502 gobbledygook","code":"network"}"#.utf8)
        let recorder = URLOpenRecorder()
        let controller = CloudAuthController(service: service, openURL: { recorder.open($0) })

        let copy = await withCheckedContinuation { (cont: CheckedContinuation<AccountErrorCopy, Never>) in
            controller.startManageSubscription { cont.resume(returning: $0) }
        }

        XCTAssertEqual(copy, .network)
        XCTAssertEqual(copy.action, .retry)
        XCTAssertFalse(copy.message.contains("502"), "raw envelope text must never surface")
        XCTAssertEqual(controller.accountError, .network, "the seam's published output must carry the same mapped copy")
        XCTAssertTrue(recorder.opened.isEmpty)
        XCTAssertFalse(controller.portalReturnPending, "a failed mint must not arm the return refresh")
    }

    /// Plan scenario (AE8): the backend's fail-closed `no_subscription` 4xx maps
    /// to the eventual-consistency copy — it offers a retry and never asserts no
    /// subscription exists (Stripe search lags checkout by ~1 min; a just-paid
    /// user must not be told to buy again).
    func testPortalNoSubscriptionMapsToNothingToManageCopy() async {
        let service = FakeCloudAuthService()
        service.portalURLData = Data(#"{"ok":false,"schema_version":2,"error":"No subscription found for this account yet. If you just subscribed, try again in a minute.","code":"no_subscription"}"#.utf8)
        let controller = CloudAuthController(service: service, openURL: { _ in })

        let copy = await withCheckedContinuation { (cont: CheckedContinuation<AccountErrorCopy, Never>) in
            controller.startManageSubscription { cont.resume(returning: $0) }
        }

        XCTAssertEqual(copy, .noSubscription)
        XCTAssertEqual(copy.action, .retry)
        XCTAssertTrue(copy.message.contains("try again in a minute"), "the copy must leave the just-subscribed door open")
    }

    /// End-to-end via `FakeCLIBinary`: `portal-url` exits 1 with an `{ok:false,
    /// code}` envelope on stdout (empty stderr). Drives the real
    /// `LiveCloudAuthService` → `CLIClient` path and pins `allowNonZeroExit` —
    /// without it the non-zero exit throws and the machine-readable `code` is
    /// lost, so this would map to `.unknown` instead of `.noSubscription`. The
    /// fake also pins that the controller invoked `portal-url` (not another
    /// subcommand).
    func testPortalURLEnvelopeErrorSurfacesMappedCopyEndToEnd() async throws {
        fakeCLI = try FakeCLIBinary(
            stdout: #"{"ok": false, "schema_version": 2, "error": "No subscription found for this account yet.", "code": "no_subscription"}"#,
            exitCode: 1,
            expectedSubcommand: "portal-url"
        )
        let recorder = URLOpenRecorder()
        let controller = CloudAuthController(
            service: LiveCloudAuthService(), openURL: { recorder.open($0) }
        )

        let copy = await withCheckedContinuation { (cont: CheckedContinuation<AccountErrorCopy, Never>) in
            controller.startManageSubscription { cont.resume(returning: $0) }
        }

        XCTAssertEqual(copy, .noSubscription, "the envelope's code must survive the non-zero exit (allowNonZeroExit)")
        XCTAssertTrue(recorder.opened.isEmpty)
    }

    /// The https open-guard, portal side: a `file://` URL in an `ok:true`
    /// envelope is never handed to the opener — it routes to the error seam
    /// (an arbitrary scheme through `NSWorkspace` could launch a local app).
    func testPortalFileSchemeURLNeverOpened() async {
        let service = FakeCloudAuthService()
        service.portalURLData = Data(#"{"ok":true,"schema_version":2,"url":"file:///etc/passwd"}"#.utf8)
        let recorder = URLOpenRecorder()
        let controller = CloudAuthController(service: service, openURL: { recorder.open($0) })

        let copy = await withCheckedContinuation { (cont: CheckedContinuation<AccountErrorCopy, Never>) in
            controller.startManageSubscription { cont.resume(returning: $0) }
        }

        XCTAssertTrue(recorder.opened.isEmpty, "non-https URLs must never open")
        XCTAssertEqual(copy, .unknown)
        XCTAssertFalse(controller.portalReturnPending)
    }

    /// The https open-guard, checkout side: a custom-scheme URL never opens and
    /// the mint fails through the seam, clearing the pending machinery.
    func testCheckoutCustomSchemeURLNeverOpened() async {
        let service = FakeCloudAuthService()
        service.checkoutURLData = Data(#"{"ok":true,"url":"screencap://not-a-checkout"}"#.utf8)
        let recorder = URLOpenRecorder()
        let controller = CloudAuthController(service: service, openURL: { recorder.open($0) })

        let failure = await withCheckedContinuation { (cont: CheckedContinuation<String, Never>) in
            controller.startCheckout(tier: .cloud) { cont.resume(returning: $0) }
        }

        XCTAssertTrue(recorder.opened.isEmpty, "non-https URLs must never open")
        XCTAssertEqual(failure, AccountErrorCopy.unknown.message)
        XCTAssertFalse(controller.checkoutPending, "a rejected URL is a mint failure — pending must clear")
        XCTAssertNil(controller.checkoutTargetTier)
    }

    /// The literal upload.py sign-in error string (it embeds a terminal
    /// instruction) fed through the mapper surfaces ONLY static copy — with no
    /// `code` it takes the fallback, and with its real `code` it takes the
    /// static sign-in case; the dynamic text never rides along either way.
    func testUploadPySignInStringSurfacesOnlyStaticFallback() async {
        let service = FakeCloudAuthService()
        let recorder = URLOpenRecorder()
        let controller = CloudAuthController(service: service, openURL: { recorder.open($0) })

        // No code → static fallback, envelope text discarded.
        service.portalURLData = Data(#"{"ok":false,"schema_version":2,"error":"Sign in to manage your subscription: run `screencap login`."}"#.utf8)
        var copy = await withCheckedContinuation { (cont: CheckedContinuation<AccountErrorCopy, Never>) in
            controller.startManageSubscription { cont.resume(returning: $0) }
        }
        XCTAssertEqual(copy, .unknown)
        XCTAssertFalse(copy.message.contains("screencap login"), "terminal instructions must never render (R10)")

        // With the real code → the static sign-in case, action `.signIn`.
        service.portalURLData = Data(#"{"ok":false,"schema_version":2,"error":"Sign in to manage your subscription: run `screencap login`.","code":"not_signed_in"}"#.utf8)
        copy = await withCheckedContinuation { (cont: CheckedContinuation<AccountErrorCopy, Never>) in
            controller.startManageSubscription { cont.resume(returning: $0) }
        }
        XCTAssertEqual(copy, .notSignedIn)
        XCTAssertEqual(copy.action, .signIn)
        XCTAssertFalse(copy.message.contains("screencap login"))
    }

    /// The thrown-error mapper: a `CLIError.timedOut` (shell-out hung, no
    /// envelope ever arrived) reads as network trouble — retry is the honest
    /// advice — while any other error takes the static fallback.
    func testTimedOutErrorMapsToNetworkCopy() {
        XCTAssertEqual(AccountErrorCopy.from(error: CLIError.timedOut(seconds: 30)), .network)
        XCTAssertEqual(AccountErrorCopy.from(error: FakeCloudAuthError.offline), .unknown)
    }

    // MARK: - controller-owned checkout/portal pending machinery (KTD-6 / R13)

    /// Checkout-pending is set (with the remembered target) the moment checkout
    /// starts, a re-tap replaces the target in place, and neither the
    /// webhook-lag window (refresh reports no tier yet), nor a refresh
    /// reporting a DIFFERENT tier, nor an offline-stale (`.indeterminate`)
    /// refresh settles it prematurely.
    func testCheckoutPendingSetAndSurvivesLagAndTrialingRefreshes() async {
        let service = FakeCloudAuthService()
        service.checkoutURLData = Data(#"{"ok":true,"url":"https://example.com/checkout"}"#.utf8)
        let recorder = URLOpenRecorder()
        let controller = CloudAuthController(service: service, openURL: { recorder.open($0) })

        controller.startCheckout(tier: .localPro)
        XCTAssertTrue(controller.checkoutPending, "pending is set up front, before the mint round-trip")
        XCTAssertEqual(controller.checkoutTargetTier, .localPro)

        // Re-tap replaces the remembered target (an abandoned Stripe tab stays
        // recoverable in place — tier buttons remain active while pending).
        controller.startCheckout(tier: .cloud)
        XCTAssertEqual(controller.checkoutTargetTier, .cloud)
        await waitUntil("checkout must open") { recorder.opened.count >= 1 }

        // Webhook-lag window: the post-return refresh reports no tier yet.
        service.forceRefreshData = Data(#"{"ok":true,"schema_version":2,"signed_in":true,"uid":"u","email":"e@x.io"}"#.utf8)
        await controller.refreshEntitlement()
        XCTAssertTrue(controller.checkoutPending, "an unresolved refresh must not clear pending (webhook lag)")

        // A refresh reporting a DIFFERENT tier (a stale claim, or an older
        // purchase's webhook landing) must not settle this attempt.
        let farFuture = Int(Date().timeIntervalSince1970) + 5 * 86_400
        service.forceRefreshData = Data(#"{"ok":true,"schema_version":2,"signed_in":true,"uid":"u","email":"e@x.io","subscribed":false,"tier":"local","trial_end":\#(farFuture)}"#.utf8)
        await controller.refreshEntitlement()
        XCTAssertTrue(controller.checkoutPending, "a non-target tier must not settle the checkout")
        XCTAssertEqual(controller.checkoutTargetTier, .cloud)

        // Target tier but offline-stale → `.indeterminate`: nothing positive
        // has resolved yet, so pending stays armed (KTD-4 grace, not a settle).
        service.forceRefreshData = Data(#"{"ok":true,"schema_version":2,"signed_in":true,"uid":null,"email":null,"stale":true,"tier":"cloud"}"#.utf8)
        await controller.refreshEntitlement()
        XCTAssertEqual(controller.trialState, .indeterminate)
        XCTAssertTrue(controller.checkoutPending, "an indeterminate refresh must not settle the checkout")
        XCTAssertEqual(controller.checkoutTargetTier, .cloud)
    }

    /// The checkout success path: every purchase goes through the mandatory
    /// card-required trial (billing.py always mints `trialing` subscriptions),
    /// so a completed checkout resolves to the TARGET tier with a live future
    /// `trial_end`. That must settle pending — requiring `.subscribed` would
    /// leave the flag stuck (and Manage Subscription hidden) for the whole
    /// 7-day trial.
    func testCheckoutPendingSettlesOnTargetTierStillTrialing() async {
        let service = FakeCloudAuthService()
        service.checkoutURLData = Data(#"{"ok":true,"url":"https://example.com/checkout"}"#.utf8)
        let recorder = URLOpenRecorder()
        let controller = CloudAuthController(service: service, openURL: { recorder.open($0) })

        controller.startCheckout(tier: .cloud)
        await waitUntil("checkout must open") { recorder.opened.count == 1 }
        XCTAssertTrue(controller.checkoutPending)

        let farFuture = Int(Date().timeIntervalSince1970) + 7 * 86_400
        service.forceRefreshData = Data(#"{"ok":true,"schema_version":2,"signed_in":true,"uid":"u","email":"e@x.io","subscribed":true,"tier":"cloud","trial_end":\#(farFuture)}"#.utf8)
        await controller.refreshEntitlement()

        XCTAssertFalse(controller.checkoutPending, "target tier + live trial = the checkout's normal success — must settle")
        XCTAssertNil(controller.checkoutTargetTier)
    }

    /// Checkout-pending clears when the entitlement resolves to the remembered
    /// target tier with `trial_end` absent (converted/paid — `.subscribed`).
    func testCheckoutPendingClearsOnTargetTierResolvedWithTrialEndAbsent() async {
        let service = FakeCloudAuthService()
        service.checkoutURLData = Data(#"{"ok":true,"url":"https://example.com/checkout"}"#.utf8)
        let recorder = URLOpenRecorder()
        let controller = CloudAuthController(service: service, openURL: { recorder.open($0) })

        controller.startCheckout(tier: .localPro)
        await waitUntil("checkout must open") { recorder.opened.count == 1 }

        service.forceRefreshData = Data(#"{"ok":true,"schema_version":2,"signed_in":true,"uid":"u","email":"e@x.io","subscribed":false,"tier":"local"}"#.utf8)
        await controller.refreshEntitlement()

        XCTAssertFalse(controller.checkoutPending, "target tier + no live trial = resolved")
        XCTAssertNil(controller.checkoutTargetTier)
    }

    /// Checkout-pending clears on mint failure — the browser never opened, so
    /// there is no return to wait on and the user must be able to retry.
    func testCheckoutPendingClearsOnMintFailure() async {
        let service = FakeCloudAuthService()
        service.checkoutURLError = FakeCloudAuthError.offline
        let controller = CloudAuthController(service: service, openURL: { _ in })

        let failure = await withCheckedContinuation { (cont: CheckedContinuation<String, Never>) in
            controller.startCheckout(tier: .cloud) { cont.resume(returning: $0) }
        }

        XCTAssertEqual(failure, AccountErrorCopy.unknown.message)
        XCTAssertFalse(controller.checkoutPending)
        XCTAssertNil(controller.checkoutTargetTier)
        XCTAssertEqual(controller.accountError, .unknown)
    }

    /// Generation guard on the checkout mint (mirrors `loginGeneration`):
    /// re-tap-replaces-target is a supported flow, so attempt A's LATE mint
    /// failure, landing after attempt B replaced it, must not clear B's armed
    /// pending state or surface A's error. B's own (current-generation)
    /// failure still clears normally.
    func testSupersededCheckoutLateFailureDoesNotClobberLiveAttempt() async {
        let service = FakeCloudAuthService()
        service.holdCheckoutCalls = true
        service.checkoutURLError = FakeCloudAuthError.offline
        let controller = CloudAuthController(service: service, openURL: { _ in })
        var failureA: String?
        var failureB: String?

        // Attempt A suspends inside the mint call.
        controller.startCheckout(tier: .localPro) { failureA = $0 }
        await waitUntil("attempt A must reach the mint call") { service.suspendedCheckoutCount == 1 }

        // Re-tap: attempt B supersedes A and re-arms pending with the new target.
        controller.startCheckout(tier: .cloud) { failureB = $0 }
        await waitUntil("attempt B must reach the mint call") { service.suspendedCheckoutCount == 2 }
        XCTAssertTrue(controller.checkoutPending)
        XCTAssertEqual(controller.checkoutTargetTier, .cloud)

        // A's late failure lands now — the stale generation must be dropped.
        service.releaseNextHeldCheckout() // A (FIFO)
        await waitUntil("attempt A must finish") { service.suspendedCheckoutCount == 1 }
        try? await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertTrue(controller.checkoutPending, "a superseded attempt's failure must not clear the live attempt")
        XCTAssertEqual(controller.checkoutTargetTier, .cloud)
        XCTAssertNil(controller.accountError, "a superseded attempt must not surface its error")
        XCTAssertNil(failureA, "a superseded attempt's onFailure must not fire")

        // B's failure is current-generation: it clears pending as usual (also
        // proves the release plumbing above exercised a real failure path).
        service.releaseNextHeldCheckout() // B
        await waitUntil("the live attempt's failure must clear pending") { !controller.checkoutPending }
        XCTAssertNil(controller.checkoutTargetTier)
        XCTAssertEqual(controller.accountError, .unknown)
        XCTAssertEqual(failureB, AccountErrorCopy.unknown.message)
    }

    /// Sign-out (and thus any account switch) clears both pending flags AND the
    /// remembered target — a different account later resolving the same tier
    /// must not settle the old account's attempt.
    func testSignOutClearsPendingFlagsAndRememberedTier() async {
        let service = FakeCloudAuthService()
        service.whoamiData = Data(signedInEnvelope.utf8)
        service.checkoutURLData = Data(#"{"ok":true,"url":"https://example.com/checkout"}"#.utf8)
        service.portalURLData = Data(#"{"ok":true,"schema_version":2,"url":"https://billing.stripe.com/p/s"}"#.utf8)
        let recorder = URLOpenRecorder()
        let controller = CloudAuthController(service: service, openURL: { recorder.open($0) })
        await controller.refresh()

        controller.startCheckout(tier: .localPro)
        controller.startManageSubscription()
        await waitUntil("both flows must open") { recorder.opened.count == 2 }
        XCTAssertTrue(controller.checkoutPending)
        XCTAssertTrue(controller.portalReturnPending)

        await controller.signOut()

        XCTAssertFalse(controller.checkoutPending)
        XCTAssertNil(controller.checkoutTargetTier)
        XCTAssertFalse(controller.portalReturnPending)
        XCTAssertNil(controller.accountError)
    }

    /// `startManageSubscription` arms the browser-return flag, and the next app
    /// activation triggers exactly one entitlement refresh (the R7 return path),
    /// after which portal-pending clears — the manual reconcile affordance is
    /// the recourse if that refresh lost the race with the webhook.
    func testPortalReturnActivationTriggersRefreshThenClears() async {
        let service = FakeCloudAuthService()
        service.portalURLData = Data(#"{"ok":true,"schema_version":2,"url":"https://billing.stripe.com/p/s"}"#.utf8)
        service.forceRefreshData = Data(signedInEnvelope.utf8)
        let center = NotificationCenter()
        let recorder = URLOpenRecorder()
        let controller = CloudAuthController(
            service: service, notificationCenter: center, openURL: { recorder.open($0) }
        )

        controller.startManageSubscription()
        await waitUntil("portal must open") { controller.portalReturnPending }

        center.post(name: NSApplication.didBecomeActiveNotification, object: nil)
        await waitUntil("activation must refresh entitlement") { service.forceRefreshCallCount == 1 }
        await waitUntil("portal-pending must clear after the post-return refresh") {
            controller.portalReturnPending == false
        }
    }

    /// The launch-path invariant, extended to app switches: an activation with
    /// NOTHING pending does zero auth work — no `whoami`, no force-refresh, no
    /// Keychain decrypt. (An "always" gate would shell out on every app switch.)
    func testActivationWithNothingPendingDoesNoAuthWork() async {
        let service = FakeCloudAuthService()
        service.whoamiData = Data(signedInEnvelope.utf8)
        let center = NotificationCenter()
        let controller = CloudAuthController(service: service, notificationCenter: center)

        center.post(name: NSApplication.didBecomeActiveNotification, object: nil)
        // Give the (gated) activation task a beat to run — it must no-op.
        try? await Task.sleep(nanoseconds: 100_000_000)
        await Task.yield()

        XCTAssertEqual(service.whoamiCallCount, 0, "idle activation must not decrypt the Keychain")
        XCTAssertEqual(service.forceRefreshCallCount, 0)
        XCTAssertEqual(controller.status, .unknown)
    }

    /// The reconcile round-trip publishes an in-flight signal the sheet renders
    /// (U3) and lowers it when done, success or failure.
    func testReconcilePublishesInFlightSignal() async {
        let service = FakeCloudAuthService()
        service.reconcileError = FakeCloudAuthError.offline
        service.forceRefreshData = Data(signedInEnvelope.utf8)
        let controller = CloudAuthController(service: service)
        XCTAssertFalse(controller.isReconciling)

        async let reconcile: Void = controller.reconcileEntitlement()
        await waitUntil("reconcile must publish in-flight") {
            controller.isReconciling || service.forceRefreshCallCount == 1
        }
        await reconcile

        XCTAssertFalse(controller.isReconciling, "the signal must lower once the round-trip settles")
        XCTAssertEqual(service.forceRefreshCallCount, 1)
    }

    // MARK: - helpers

    /// Poll until `condition` holds (or the timeout passes) — the detached
    /// checkout / portal / activation Tasks have no completion to await
    /// directly. Mirrors `RecorderControllerTests.waitUntil`.
    private func waitUntil(
        _ message: String,
        timeout: TimeInterval = 3,
        file: StaticString = #filePath,
        line: UInt = #line,
        _ condition: @escaping @MainActor () -> Bool
    ) async {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return }
            await Task.yield()
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        XCTFail(message, file: file, line: line)
    }

    // MARK: - fake CLI injection (for LiveCloudAuthService integration)

    private var fakeCLI: FakeCLIBinary?

    override func tearDown() {
        fakeCLI?.remove()
        fakeCLI = nil
        super.tearDown()
    }
}
