import Foundation
import XCTest
@testable import Screencap

/// SCR-214 U12 — the runtime ambient-control seam: `AmbientStatus` decode (incl.
/// null/missing `degraded`/`recording`), the `ambient.set` request encoding
/// (only-present-keys), and `AmbientController`'s confirmed-state / pending /
/// blocked / consent-gate behaviour. Driven against a `FakeAmbientService` (no
/// live socket), mirroring the `SearchService` fake pattern.
///
/// `@MainActor` so the tests can read the `@MainActor` controller's state
/// synchronously; the fakes stay `@unchecked Sendable` per the repo convention.
@MainActor
final class AmbientControlsTests: XCTestCase {

    // MARK: - AmbientStatus decode

    func testDecodesFullStatus() throws {
        let json = #"""
        {"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,
         "enabled":true,"autostart":true,"active":true,"paused":false,
         "degraded":null,"recording":"2026-07-14"}
        """#
        let status = try JSONDecoder().decode(AmbientStatus.self, from: Data(json.utf8))
        XCTAssertTrue(status.enabled)
        XCTAssertTrue(status.autostart)
        XCTAssertTrue(status.active)
        XCTAssertFalse(status.paused)
        XCTAssertNil(status.degraded, "explicit JSON null decodes to nil")
        XCTAssertEqual(status.recording, "2026-07-14")
    }

    func testDecodesDegradedReasonAndNullRecording() throws {
        let json = #"""
        {"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,
         "enabled":true,"autostart":false,"active":false,"paused":false,
         "degraded":"Screen Recording permission denied","recording":null}
        """#
        let status = try JSONDecoder().decode(AmbientStatus.self, from: Data(json.utf8))
        XCTAssertTrue(status.enabled)
        XCTAssertFalse(status.active)
        XCTAssertEqual(status.degraded, "Screen Recording permission denied")
        XCTAssertNil(status.recording)
    }

    func testDecodesWhenNullableKeysOmitted() throws {
        // A daemon that omits `degraded`/`recording` entirely still decodes —
        // optional decoding treats a missing key the same as null.
        let json = #"""
        {"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,
         "enabled":false,"autostart":true,"active":false,"paused":false}
        """#
        let status = try JSONDecoder().decode(AmbientStatus.self, from: Data(json.utf8))
        XCTAssertFalse(status.enabled)
        XCTAssertNil(status.degraded)
        XCTAssertNil(status.recording)
    }

    // MARK: - AmbientSetRequest encode

    func testSetRequestEncodesOnlyPresentKeys() throws {
        let data = try JSONEncoder().encode(AmbientSetRequest(enabled: true, autostart: nil))
        let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        XCTAssertEqual(obj?["enabled"] as? Bool, true)
        XCTAssertFalse(obj?.keys.contains("autostart") ?? true,
                       "a nil field is omitted so the daemon touches only what changed")
    }

    func testSetRequestEncodesAutostartWithoutEnabled() throws {
        let data = try JSONEncoder().encode(AmbientSetRequest(enabled: nil, autostart: false))
        let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        XCTAssertEqual(obj?["autostart"] as? Bool, false)
        XCTAssertFalse(obj?.keys.contains("enabled") ?? true)
    }

    // MARK: - Controller: confirmed state

    func testLoadReflectsConfirmedStatus() async {
        let fake = FakeAmbientService()
        fake.statusResult = AmbientStatus(
            enabled: true, autostart: true, active: true, paused: false, recording: "day-1"
        )
        let controller = AmbientController(service: fake, defaults: makeDefaults())

        await controller.load()

        XCTAssertTrue(controller.enabled)
        XCTAssertTrue(controller.active)
        XCTAssertEqual(controller.recordingName, "day-1")
        XCTAssertFalse(controller.isBlocked)
        XCTAssertNil(controller.lastError)
    }

    func testSetEnabledReflectsReturnedConfirmedStatusNotOptimism() async {
        let fake = FakeAmbientService()
        // The daemon confirms enabled but ambient is not yet active (detached
        // spawn still in flight) — the controller must show exactly that.
        fake.setResult = AmbientStatus(
            enabled: true, autostart: true, active: false, paused: false
        )
        let controller = AmbientController(service: fake, defaults: makeDefaults())
        controller.recordAudioConsent()

        let ok = await controller.setEnabled(true)

        XCTAssertTrue(ok)
        XCTAssertEqual(fake.setCalls.count, 1)
        XCTAssertEqual(fake.setCalls.first?.enabled ?? nil, true)
        XCTAssertTrue(controller.enabled)
        XCTAssertFalse(controller.active, "confirmed active=false, not an optimistic true")
        XCTAssertFalse(controller.pending)
    }

    func testBlockedStateWhenDegradedReasonPresent() async {
        let fake = FakeAmbientService()
        fake.statusResult = AmbientStatus(
            enabled: true, autostart: true, active: false, paused: false,
            degraded: "Upgrade required to record", recording: nil
        )
        let controller = AmbientController(service: fake, defaults: makeDefaults())

        await controller.load()

        XCTAssertTrue(controller.isBlocked, "enabled + degraded reason → blocked, not silent-on")
        XCTAssertEqual(controller.degraded, "Upgrade required to record")
    }

    func testDegradedWithoutEnabledIsNotBlocked() async {
        let fake = FakeAmbientService()
        fake.statusResult = AmbientStatus(
            enabled: false, autostart: true, active: false, paused: false,
            degraded: "stale reason", recording: nil
        )
        let controller = AmbientController(service: fake, defaults: makeDefaults())

        await controller.load()

        XCTAssertFalse(controller.isBlocked)
    }

    // MARK: - Controller: pause reflects CONFIRMED status, not the request echo

    func testPauseReflectsConfirmedStatusFromReread() async {
        let fake = FakeAmbientService()
        // Live + unpaused before the pause; the RE-READ after pause reports the
        // engine-confirmed paused=true. The pause verb itself carries no state
        // the controller trusts — it re-reads ambient.status.
        fake.statusResult = AmbientStatus(
            enabled: true, autostart: true, active: true, paused: false, recording: "day-1"
        )
        let controller = AmbientController(service: fake, defaults: makeDefaults())
        await controller.load()

        fake.statusResult = AmbientStatus(
            enabled: true, autostart: true, active: true, paused: true, recording: "day-1"
        )
        await controller.pause()

        XCTAssertEqual(fake.pauseCalls, 1)
        XCTAssertTrue(controller.paused, "confirmed via re-read ambient.status")
        XCTAssertFalse(controller.pending)
    }

    func testPauseIsNoOpWhenNotActive() async {
        let fake = FakeAmbientService()
        fake.statusResult = AmbientStatus(
            enabled: true, autostart: true, active: false, paused: false
        )
        let controller = AmbientController(service: fake, defaults: makeDefaults())
        await controller.load()

        await controller.pause()

        XCTAssertEqual(fake.pauseCalls, 0, "nothing live to pause")
    }

    // MARK: - Controller: first-run consent gate (R1)

    func testEnableBlockedUntilConsented() async {
        let fake = FakeAmbientService()
        let controller = AmbientController(service: fake, defaults: makeDefaults())
        XCTAssertTrue(controller.needsAudioConsent)

        let gate = await controller.enableWithConsentGate()

        XCTAssertEqual(gate, .needsConsent)
        XCTAssertEqual(fake.setCalls.count, 0, "no daemon write happens before consent")
        XCTAssertFalse(controller.enabled)
    }

    func testAcceptConsentPersistsAndEnables() async {
        let defaults = makeDefaults()
        let fake = FakeAmbientService()
        fake.setResult = AmbientStatus(
            enabled: true, autostart: true, active: false, paused: false
        )
        let controller = AmbientController(service: fake, defaults: defaults)

        await controller.acceptAudioConsentAndEnable()

        XCTAssertFalse(controller.needsAudioConsent)
        XCTAssertTrue(defaults.bool(forKey: AmbientController.audioConsentKey),
                      "consent is persisted so the sheet shows only once")
        XCTAssertEqual(fake.setCalls.count, 1)
        XCTAssertEqual(fake.setCalls.first?.enabled ?? nil, true)
        XCTAssertTrue(controller.enabled)
    }

    func testConsentedEnableAppliesWithoutSheet() async {
        let fake = FakeAmbientService()
        fake.setResult = AmbientStatus(
            enabled: true, autostart: true, active: true, paused: false
        )
        let controller = AmbientController(service: fake, defaults: makeDefaults())
        controller.recordAudioConsent()

        let gate = await controller.enableWithConsentGate()

        XCTAssertEqual(gate, .applied)
        XCTAssertEqual(fake.setCalls.count, 1)
    }

    // MARK: - Controller: pending in-flight state

    func testPendingIsTrueWhileEnableInFlight() async {
        let enterGate = TestGate()
        let releaseGate = TestGate()
        let fake = FakeAmbientService()
        fake.enterSet = enterGate
        fake.releaseSet = releaseGate
        fake.setResult = AmbientStatus(
            enabled: true, autostart: true, active: false, paused: false
        )
        let controller = AmbientController(service: fake, defaults: makeDefaults())
        controller.recordAudioConsent()

        let task = Task { await controller.setEnabled(true) }
        // Deterministic: the fake opens `enterSet` the moment it is inside the
        // call, so once this returns the write is provably in flight.
        await enterGate.wait()
        XCTAssertTrue(controller.pending)

        await releaseGate.open()
        _ = await task.value
        XCTAssertFalse(controller.pending)
        XCTAssertTrue(controller.enabled)
    }

    func testFailedWriteReconcilesAndSurfacesError() async {
        let fake = FakeAmbientService()
        fake.statusResult = AmbientStatus(
            enabled: false, autostart: true, active: false, paused: false
        )
        fake.setError = FakeAmbientError.boom
        let controller = AmbientController(service: fake, defaults: makeDefaults())
        controller.recordAudioConsent()

        let ok = await controller.setEnabled(true)

        XCTAssertFalse(ok)
        XCTAssertNotNil(controller.lastError)
        // Reconciled against the daemon's confirmed (still-disabled) state.
        XCTAssertFalse(controller.enabled)
        XCTAssertFalse(controller.pending)
    }

    // MARK: - Helpers

    private func makeDefaults() -> UserDefaults {
        UserDefaults(suiteName: "ambient-controls-tests-\(UUID().uuidString)")!
    }
}

// MARK: - Fakes

private enum FakeAmbientError: Error { case boom }

/// Minimal `AmbientService` fake, mirroring the `SearchService` fakes
/// (`final class … @unchecked Sendable` with `private(set) var` recording state).
private final class FakeAmbientService: AmbientService, @unchecked Sendable {
    var statusResult = AmbientStatus(enabled: false, autostart: true, active: false, paused: false)
    var setResult: AmbientStatus?
    var setError: Error?

    private(set) var setCalls: [(enabled: Bool?, autostart: Bool?)] = []
    private(set) var pauseCalls = 0
    private(set) var resumeCalls = 0

    /// Optional gates so a test can hold a call open and assert on `pending`.
    var enterSet: TestGate?
    var releaseSet: TestGate?

    func ambientStatus() async throws -> AmbientStatus {
        statusResult
    }

    func ambientSet(enabled: Bool?, autostart: Bool?) async throws -> AmbientStatus {
        setCalls.append((enabled: enabled, autostart: autostart))
        await enterSet?.open()
        await releaseSet?.wait()
        if let setError { throw setError }
        return setResult
            ?? AmbientStatus(
                enabled: enabled ?? statusResult.enabled,
                autostart: autostart ?? statusResult.autostart,
                active: false,
                paused: false
            )
    }

    func recordingPause() async throws {
        pauseCalls += 1
    }

    func recordingResume() async throws {
        resumeCalls += 1
    }
}

/// A one-shot async gate: `wait()` suspends until `open()`, and `open()` before
/// any waiter makes subsequent `wait()`s pass through. Used to hold a fake call
/// open (or to signal call entry) so the pending-state test is deterministic.
private actor TestGate {
    private var opened = false
    private var waiters: [CheckedContinuation<Void, Never>] = []

    func wait() async {
        if opened { return }
        await withCheckedContinuation { waiters.append($0) }
    }

    func open() {
        opened = true
        let pending = waiters
        waiters.removeAll()
        for continuation in pending { continuation.resume() }
    }
}
