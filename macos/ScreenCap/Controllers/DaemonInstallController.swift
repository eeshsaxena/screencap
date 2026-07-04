import AppKit
import Foundation
import ServiceManagement
import os

/*
 * Manual smoke (run after first build install):
 * 1. Delete any existing ~/.screencap/run/api.sock
 * 2. Reset TCC: tccutil reset All com.screencap.daemon
 * 3. Launch ScreenCap.app from a fresh build.
 * 4. Verify: SwiftUI shows "Approve ScreenCap helper" -> user clicks ->
 *    System Settings -> Login Items shows ScreenCap helper, user approves.
 * 5. Verify: SwiftUI advances to TCC checklist; user grants three permissions
 *    targeting com.screencap.daemon.
 * 6. Verify: recording works.
 * 7. (AE3 path) From a fresh terminal: `screencap serve --install` -> repeat
 *    steps 5-6 from the CLI side; then `screencap record` works end-to-end
 *    without ever launching ScreenCap.app.
 */

private let daemonInstallLogger = Logger(subsystem: "com.screencap.macos", category: "daemon-install")

extension Notification.Name {
    static let screenCapDaemonInstalledAndRunning = Notification.Name("ScreenCapDaemonInstalledAndRunning")
}

/// The exit status and captured stderr of a `/bin/launchctl` invocation.
struct LaunchctlResult {
    let terminationStatus: Int32
    let stderr: String
}

/// Runs `/bin/launchctl` with the given arguments and returns its exit status
/// plus trimmed stderr. The single home for the launchctl/Process spawn idiom
/// shared by `LaunchctlDaemonTerminator.bootout()` and
/// `LiveDaemonSessionService.reload()` — each keeps only its own arguments and
/// exit-code policy. stdout is wired to /dev/null (launchctl's stdout is unused
/// and an undrained Pipe could otherwise back-pressure the child); stderr is the
/// only stream callers care about. Throws only if the subprocess fails to spawn
/// (`Process.run()`); a non-zero exit is reported via `terminationStatus`, not a
/// throw.
func runLaunchctl(_ arguments: [String]) async throws -> LaunchctlResult {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
    process.arguments = arguments
    process.standardInput = FileHandle.nullDevice
    process.standardOutput = FileHandle.nullDevice
    let stderrPipe = Pipe()
    process.standardError = stderrPipe
    try process.run()
    await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
        DispatchQueue.global(qos: .userInitiated).async {
            process.waitUntilExit()
            continuation.resume()
        }
    }
    let stderr = (try? stderrPipe.fileHandleForReading.readToEnd())
        .flatMap { String(data: $0, encoding: .utf8) }?
        .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    return LaunchctlResult(terminationStatus: process.terminationStatus, stderr: stderr)
}

@MainActor
protocol DaemonRegistrationService {
    func register(plistName: String) async throws -> SMAppService.Status
    func refresh(plistName: String) async throws -> SMAppService.Status
    func currentStatus(plistName: String) -> SMAppService.Status
}

@MainActor
protocol DaemonProbe {
    /// The reachable daemon's reported version, or nil if no daemon answered.
    /// Callers compare this against the bundled (expected) version to detect a
    /// stale/foreign daemon squatting api.sock (SCR-121).
    func probe(timeout: TimeInterval) async -> String?
}

@MainActor
protocol DaemonTerminator {
    /// Force-terminate the launchd-managed daemon (`launchctl bootout`) so a
    /// stale/squatting process releases api.sock before the reinstall
    /// re-registers (SCR-135). Idempotent: a not-loaded label is a successful
    /// no-op. Must never throw — bootout is best-effort cleanup.
    func bootout() async
}

@MainActor
final class DaemonInstallController: ObservableObject {
    enum State: Equatable {
        case idle
        case registering
        case polling
        case pollingFailed(reason: String)
        case installedAndRunning
        case requiresApproval
        case installFailed(InstallFailureReason)
    }

    enum InstallFailureReason: String, Equatable {
        case plistWriteFailed = "plist_write_failed"
        case launchctlBootstrapFailed = "launchctl_bootstrap_failed"
        case daemonDidNotStart = "daemon_did_not_start"
        case daemonSigningInvalid = "daemon_signing_invalid"
        case diskFull = "disk_full"
        /// A reachable daemon reports a version other than the one this app
        /// bundles, and a reinstall did not dislodge it (SCR-121).
        case daemonVersionMismatch = "daemon_version_mismatch"
        case unknown
    }

    @Published private(set) var state: State = .idle

    /// Process-wide single-flight latch for `install()`. The auto-start guard
    /// (`OnboardingStepPolicy.shouldAutoStartHelperInstall`) checks `.idle`
    /// per-instance, but the wizard and the permission-repair takeover each own
    /// their own controller while both mutate the SAME process-global
    /// SMAppService label — two interleaved install() state machines
    /// (register / destructive refresh / bootout at await points) can corrupt
    /// each other's outcome. A late-comer returns immediately, leaving its own
    /// controller `.idle` (the card keeps its Approve button); the winning
    /// install's completion posts `.screenCapDaemonInstalledAndRunning`, which
    /// flips transport for every surface. @MainActor-confined, so a plain Bool
    /// is race-free.
    private static var installInFlight = false

    static let plistName = "com.screencap.daemon.plist"
    static let registrationAttemptedKey = "com.screencap.macos.daemonInstallAttempted"
    /// Records the daemon version for which proactive TCC setup (SCR-200 U3/U4:
    /// register SR + Accessibility, clear decoys) last ran, so it fires once per
    /// install/version and NOT on every `installedAndRunning` poll (which posts on
    /// every reconnect/relaunch).
    static let proactiveTccSetupVersionKey = "com.screencap.macos.proactiveTccSetupVersion"

    /// Proactive TCC setup run once per version after the daemon comes up:
    /// register the daemon's Screen Recording + Accessibility rows and clear
    /// decoy/orphan rows. The default round-trips daemon verbs so registration
    /// attributes to the daemon (R6); injectable so tests assert the once-per-
    /// version gate without real socket calls. No-op under XCTest by default.
    static let defaultProactiveTccSetup: @Sendable () async -> Void = {
        guard ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] == nil else { return }
        // Registration (R1/R6): the daemon runs each request in its own process,
        // so the row attributes to com.screencap.daemon, not the app. Input
        // Monitoring is excluded (out of the required flow). Best-effort — a
        // hiccup must never strand the user; on-demand Grant + U6 block-with-Retry
        // remain as recovery.
        for permission in ["screen_recording", "accessibility"] {
            do {
                _ = try await DaemonClient.permissionRequest(permission)
            } catch {
                daemonInstallLogger.error("Proactive registration for \(permission, privacy: .public) failed: \(String(describing: error), privacy: .public)")
            }
        }
        // Cleanup (R5/R6/R8): identity-scoped decoy removal, daemon-side (the
        // hardened tccutil allowlist lives in tcc_cleanup).
        do {
            _ = try await DaemonClient.cleanupDecoys()
        } catch {
            daemonInstallLogger.error("Proactive decoy cleanup failed: \(String(describing: error), privacy: .public)")
        }
    }

    private let registrationService: DaemonRegistrationService
    private let probe: DaemonProbe
    /// Force-terminates a stale/squatting daemon (`launchctl bootout`) so it
    /// releases api.sock before the reinstall re-registers (SCR-135). A fresh
    /// daemon cannot bind while the old one answers (`socket.py`
    /// `DaemonAlreadyRunning`), so re-registration alone does not dislodge it.
    private let terminator: DaemonTerminator
    /// The daemon version this app bundles, used to reject a reachable-but-stale
    /// daemon (SCR-121). nil means "could not determine" — the version gate then
    /// fails safe to the historical reachable-implies-running behavior.
    private let expectedDaemonVersion: String?
    private let sleep: (UInt64) async -> Void
    /// Injectable clock so the poll deadline is deterministic under test: the
    /// convergence budget (SCR-135) advances it through the stubbed `sleep`.
    private let now: () -> Date
    /// UserDefaults backing the once-per-version proactive-setup gate (injectable
    /// so the gate is testable in isolation).
    private let defaults: UserDefaults
    private let proactiveTccSetup: @Sendable () async -> Void

    init(
        registrationService: DaemonRegistrationService = SMAppServiceRegistration(),
        probe: DaemonProbe = LiveDaemonProbe(),
        terminator: DaemonTerminator = LaunchctlDaemonTerminator(),
        expectedDaemonVersion: String? = BundledDaemonVersion.expected(),
        sleep: @escaping (UInt64) async -> Void = { nanoseconds in
            try? await Task.sleep(nanoseconds: nanoseconds)
        },
        now: @escaping () -> Date = Date.init,
        defaults: UserDefaults = .standard,
        proactiveTccSetup: @escaping @Sendable () async -> Void = DaemonInstallController.defaultProactiveTccSetup
    ) {
        self.registrationService = registrationService
        self.probe = probe
        self.terminator = terminator
        self.expectedDaemonVersion = expectedDaemonVersion
        self.sleep = sleep
        self.now = now
        self.defaults = defaults
        self.proactiveTccSetup = proactiveTccSetup
    }

    /// SCR-200 U3/U4: once per installed daemon version, register the daemon's
    /// Screen Recording + Accessibility rows and clear decoy/orphan rows, so the
    /// user finds a single pre-populated "ScreenCap" row (R1/R2/R6) with no
    /// look-alikes (R5). Gated by version because `pollDaemon` posts
    /// `installedAndRunning` on every successful poll (app launch, reconnect,
    /// version reconciliation), not just first install — without the gate the
    /// resets would re-fire and could clear a row the user is mid-interaction
    /// with. Detached + best-effort: never blocks or fails the install path.
    func fireProactiveTccSetupIfNeeded() {
        let version = expectedDaemonVersion ?? "unknown"
        guard defaults.string(forKey: Self.proactiveTccSetupVersionKey) != version else { return }
        defaults.set(version, forKey: Self.proactiveTccSetupVersionKey)
        let setup = proactiveTccSetup
        Task.detached { await setup() }
    }

    func install(
        timeoutSeconds: TimeInterval = 10,
        probeIntervalSeconds: TimeInterval = 0.5,
        approvalTimeoutSeconds: TimeInterval = 60,
        approvalPollIntervalSeconds: TimeInterval = 2,
        // The post-refresh convergence poll gets its own, longer budget than the
        // generic reachability `timeoutSeconds` (SCR-135): the daemon's launchd
        // ExitTimeOut is 30s (`launchagent.py` `ExitTimeOut=30`), so a stale
        // daemon booted out during the swap can keep answering the wrong version
        // for up to ~30s before it exits and the fresh one binds. Failing the
        // mismatch at the 10s reachability budget surfaced a false
        // `.daemonVersionMismatch`. The shell sibling `confirm_fresh_daemon`
        // polls 30s for the same reason.
        convergenceTimeoutSeconds: TimeInterval = 30
    ) async {
        // Single-flight per process (see installInFlight): a concurrent install
        // from another surface's controller owns the label — do not interleave.
        guard !Self.installInFlight else { return }
        Self.installInFlight = true
        defer { Self.installInFlight = false }

        state = .registering
        let status: SMAppService.Status
        do {
            status = try await registrationService.register(plistName: Self.plistName)
        } catch {
            daemonInstallLogger.error("Daemon registration failed: \(String(describing: error), privacy: .public)")
            state = .installFailed(Self.failureReason(from: error))
            return
        }

        await handleRegisteredStatus(
            status,
            timeoutSeconds: timeoutSeconds,
            probeIntervalSeconds: probeIntervalSeconds,
            approvalTimeoutSeconds: approvalTimeoutSeconds,
            approvalPollIntervalSeconds: approvalPollIntervalSeconds,
            convergenceTimeoutSeconds: convergenceTimeoutSeconds,
            allowRegistrationRefresh: true
        )
    }

    func retry(
        timeoutSeconds: TimeInterval = 10,
        probeIntervalSeconds: TimeInterval = 0.5,
        approvalTimeoutSeconds: TimeInterval = 60,
        approvalPollIntervalSeconds: TimeInterval = 2,
        convergenceTimeoutSeconds: TimeInterval = 30
    ) async {
        await install(
            timeoutSeconds: timeoutSeconds,
            probeIntervalSeconds: probeIntervalSeconds,
            approvalTimeoutSeconds: approvalTimeoutSeconds,
            approvalPollIntervalSeconds: approvalPollIntervalSeconds,
            convergenceTimeoutSeconds: convergenceTimeoutSeconds
        )
    }

    static func registerDaemonOnFirstLaunchIfNeeded(
        defaults: UserDefaults = .standard,
        registrationService: DaemonRegistrationService = SMAppServiceRegistration()
    ) {
        guard ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] == nil else { return }
        guard !defaults.bool(forKey: registrationAttemptedKey) else { return }
        defaults.set(true, forKey: registrationAttemptedKey)

        Task { @MainActor in
            let status = registrationService.currentStatus(plistName: plistName)
            guard status == .notRegistered || status == .notFound else { return }
            do {
                _ = try await registrationService.register(plistName: plistName)
            } catch {
                daemonInstallLogger.error("First-launch daemon registration failed: \(String(describing: error), privacy: .public)")
            }
        }
    }

    /// A reachable daemon's process-start time plus whether it is mid-recording.
    /// Returned by the stale-daemon probe; a `nil` probe result means "no daemon
    /// answered", which short-circuits the staleness check to a no-op.
    struct StaleDaemonProbe: Equatable {
        /// Unix seconds the daemon PROCESS started (`daemon.info.started_at`).
        let startedAt: Double
        /// A daemon-owned recording is in flight — restarting would drop it.
        let isRecording: Bool
    }

    /// Live probe: `daemon.info` for the process-start time and `session.snapshot`
    /// for an in-flight recording. Both are non-lazy-import verbs, so a stale
    /// daemon (whose lazy-import verbs 500) still answers them. Returns nil when
    /// no daemon is reachable, and no-ops under XCTest so the app-hosted unit
    /// suite never reaches a real socket (tests inject their own probe).
    static let liveStaleDaemonProbe: () async -> StaleDaemonProbe? = {
        guard ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] == nil else { return nil }
        do {
            let info = try await DaemonClient.daemonInfo()
            // Best-effort: a stale daemon answers session.snapshot, so a failure
            // here is a genuine transport blip (or the daemon is already down) —
            // treat as "not recording" rather than let it block healing.
            let recording = (try? await DaemonClient.sessionSnapshot())
                .map { $0.isRecording == true && $0.daemonOwned } ?? false
            return StaleDaemonProbe(startedAt: info.startedAt, isRecording: recording)
        } catch {
            daemonInstallLogger.debug("stale-daemon probe: no daemon reachable: \(String(describing: error), privacy: .public)")
            return nil
        }
    }

    /// mtime of the daemon executable this app would run — the embedded LoginItem
    /// helper. An app update rewrites it, so a daemon that started before this
    /// timestamp is running the PREVIOUS bundle. Returns nil (→ skip the check)
    /// when the path can't be resolved or stat'd, so an unexpected bundle layout
    /// never triggers a false restart.
    static let liveDaemonBundleModifiedAt: () -> Date? = {
        let helper = Bundle.main.bundleURL.appendingPathComponent(
            "Contents/Library/LoginItems/ScreencapDaemon.app/Contents/MacOS/screencap"
        )
        guard
            let attrs = try? FileManager.default.attributesOfItem(atPath: helper.path),
            let mtime = attrs[.modificationDate] as? Date
        else { return nil }
        return mtime
    }

    /// Restart the daemon in place via `launchctl kickstart -k` so it reloads the
    /// current on-disk bundle. Returns true on a clean kickstart. No-ops under
    /// XCTest (defense-in-depth against an app-hosted test tearing down a real
    /// daemon; tests inject their own restart).
    static let liveKickstartRestart: () async -> Bool = {
        guard ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] == nil else { return false }
        do {
            let result = try await runLaunchctl(["kickstart", "-k", "gui/\(getuid())/com.screencap.daemon"])
            if result.terminationStatus != 0 {
                daemonInstallLogger.error("stale-daemon restart: launchctl kickstart -k exit \(result.terminationStatus, privacy: .public): \(result.stderr, privacy: .public)")
                return false
            }
            return true
        } catch {
            daemonInstallLogger.error("stale-daemon restart: launchctl spawn failed: \(String(describing: error), privacy: .public)")
            return false
        }
    }

    /// Restart the daemon if it predates the installed bundle (SCR: stale daemon
    /// after app update).
    ///
    /// After an app update the previously-running daemon keeps serving the OLD
    /// on-disk bundle — SMAppService does not restart the LoginItem on update.
    /// PyInstaller loads deferred (lazy) imports from the archive at request
    /// time, so once the bundle is replaced underneath the old process, every
    /// verb that lazy-imports (recording.list, apps.list, content.search,
    /// transcript.search, timeline.query) raises ImportError → HTTP 500 → the
    /// "Couldn't load recordings" the user hits. The SCR-121 version gate misses
    /// this because the daemon *version string* is unchanged across an app update
    /// (the app version and the daemon/CLI version are separate schemes), so the
    /// gate adopts the same-version stale process as healthy.
    ///
    /// Detect it directly and version-independently: a reachable daemon whose
    /// PROCESS start predates the daemon binary we would run is running the old
    /// bundle — restart it in place so it reloads the fresh code. Runs on every
    /// launch; best-effort and fail-safe — an unreachable daemon, an
    /// undeterminable bundle time, or an active recording each short-circuit to a
    /// no-op. Returns whether a restart was triggered (for the caller and tests).
    @discardableResult
    static func restartStaleDaemonIfNeeded(
        probe: () async -> StaleDaemonProbe? = liveStaleDaemonProbe,
        bundleModifiedAt: () -> Date? = liveDaemonBundleModifiedAt,
        restart: () async -> Bool = liveKickstartRestart,
        now: () -> Date = Date.init
    ) async -> Bool {
        guard let bundleMTime = bundleModifiedAt() else { return false }
        // A bundle mtime in the FUTURE (build-machine clock skew, or an updater
        // that preserved a future archive timestamp) can't be trusted as a
        // staleness reference: it would make even a just-restarted daemon look
        // stale and re-trigger a restart on every launch. Skip rather than churn.
        guard bundleMTime <= now() else { return false }
        guard let info = await probe() else { return false }
        let daemonStart = Date(timeIntervalSince1970: info.startedAt)
        // A fresh daemon starts AFTER its binary was written; only a daemon that
        // predates the current binary is running a superseded bundle.
        guard daemonStart < bundleMTime else { return false }
        if info.isRecording {
            daemonInstallLogger.info("Stale daemon detected (started \(daemonStart, privacy: .public), bundle \(bundleMTime, privacy: .public)) but a recording is active; deferring restart")
            return false
        }
        daemonInstallLogger.error("Daemon started \(daemonStart, privacy: .public) predates installed bundle \(bundleMTime, privacy: .public); restarting stale daemon after app update")
        return await restart()
    }

    static func openLoginItemsSettings() {
        if #available(macOS 14.0, *) {
            SMAppService.openSystemSettingsLoginItems()
        } else if let url = URL(string: "x-apple.systempreferences:com.apple.LoginItems-Settings.extension") {
            NSWorkspace.shared.open(url)
        } else {
            NSWorkspace.shared.open(PrivacyPane.fallbackURL)
        }
    }

    private func handleRegisteredStatus(
        _ status: SMAppService.Status,
        timeoutSeconds: TimeInterval,
        probeIntervalSeconds: TimeInterval,
        approvalTimeoutSeconds: TimeInterval,
        approvalPollIntervalSeconds: TimeInterval,
        convergenceTimeoutSeconds: TimeInterval,
        allowRegistrationRefresh: Bool,
        // The wrong version that drove us into the post-refresh pass, or nil if we
        // got here for any other reason (first pass, or a plain reachability
        // timeout). Lets the second-pass `.timedOut` branch tell "the reinstall of
        // a mismatched daemon never converged" apart from "a daemon simply never
        // came up", and surface the actionable mismatch state for the former
        // (SCR-136).
        priorMismatchVersion: String? = nil
    ) async {
        switch status {
        case .enabled:
            // The post-refresh poll (allowRegistrationRefresh == false) runs while
            // a daemon swap is in flight, so it gets a convergence budget for a
            // lingering version mismatch (SCR-135). The first poll fails the
            // mismatch fast so the reinstall starts promptly.
            let outcome = await pollDaemon(
                timeoutSeconds: timeoutSeconds,
                probeIntervalSeconds: probeIntervalSeconds,
                convergenceTimeoutSeconds: convergenceTimeoutSeconds,
                convergeOnMismatch: !allowRegistrationRefresh
            )
            switch outcome {
            case .running:
                break  // pollDaemon set .installedAndRunning and posted the notice
            case .timedOut:
                if allowRegistrationRefresh {
                    await refreshRegistrationAfterFailedPoll(
                        timeoutSeconds: timeoutSeconds,
                        probeIntervalSeconds: probeIntervalSeconds,
                        approvalTimeoutSeconds: approvalTimeoutSeconds,
                        approvalPollIntervalSeconds: approvalPollIntervalSeconds,
                        convergenceTimeoutSeconds: convergenceTimeoutSeconds
                    )
                } else if let priorMismatchVersion {
                    // We entered this post-refresh pass because a version-mismatched
                    // daemon was squatting the socket. The reinstall booted it out,
                    // but the replacement never answered before the convergence
                    // budget expired — so the swap did not complete. The user's real
                    // problem is still the stale helper, so surface the actionable
                    // `.daemonVersionMismatch` ("reinstall the bundled helper")
                    // rather than the generic `.pollingFailed` ("did not respond")
                    // that pollDaemon left in `state` (SCR-136). This mirrors the
                    // cached-`lastMismatch` deadline resolution in pollDaemon for the
                    // case where the stale daemon vanishes before the second pass
                    // ever probes it.
                    daemonInstallLogger.error("Daemon never reappeared after reinstalling over version \(priorMismatchVersion, privacy: .public); expected \(self.expectedDaemonVersion ?? "unknown", privacy: .public)")
                    state = .installFailed(.daemonVersionMismatch)
                }
            case .versionMismatch(let running):
                // A stale/foreign daemon is squatting api.sock. Try the reinstall
                // path once (refresh re-registers this bundle's agent); if it
                // still answers with the wrong version, surface it rather than
                // silently driving the old daemon (SCR-121).
                if allowRegistrationRefresh {
                    daemonInstallLogger.error("Reachable daemon version \(running, privacy: .public) != expected \(self.expectedDaemonVersion ?? "unknown", privacy: .public); dislodging stale daemon and reinstalling")
                    // unregister()/register() alone does not reliably evict the
                    // squatter (it is async and the daemon's ExitTimeOut is 30s),
                    // and a fresh daemon cannot bind while the old one answers
                    // (SCR-135). Bootout first so the socket is free, mirroring
                    // reconcile_daemon_version in build_and_run.sh.
                    await terminator.bootout()
                    await refreshRegistrationAfterFailedPoll(
                        timeoutSeconds: timeoutSeconds,
                        probeIntervalSeconds: probeIntervalSeconds,
                        approvalTimeoutSeconds: approvalTimeoutSeconds,
                        approvalPollIntervalSeconds: approvalPollIntervalSeconds,
                        convergenceTimeoutSeconds: convergenceTimeoutSeconds,
                        priorMismatchVersion: running
                    )
                } else {
                    daemonInstallLogger.error("Daemon still reports version \(running, privacy: .public) after reinstall; expected \(self.expectedDaemonVersion ?? "unknown", privacy: .public)")
                    state = .installFailed(.daemonVersionMismatch)
                }
            }
        case .requiresApproval:
            state = .requiresApproval
            switch await waitForApproval(
                timeoutSeconds: approvalTimeoutSeconds,
                intervalSeconds: approvalPollIntervalSeconds
            ) {
            case .approved:
                await handleRegisteredStatus(
                    .enabled,
                    timeoutSeconds: timeoutSeconds,
                    probeIntervalSeconds: probeIntervalSeconds,
                    approvalTimeoutSeconds: approvalTimeoutSeconds,
                    approvalPollIntervalSeconds: approvalPollIntervalSeconds,
                    convergenceTimeoutSeconds: convergenceTimeoutSeconds,
                    allowRegistrationRefresh: allowRegistrationRefresh,
                    priorMismatchVersion: priorMismatchVersion
                )
            case .stillPending:
                // Deadline expired with the label still awaiting approval —
                // user inaction, not a failure. Stay in .requiresApproval so
                // the card keeps the honest "approve in Login Items" copy
                // (Open Login Items + Retry) instead of a misleading "could
                // not be installed". Matters doubly now that install()
                // auto-fires on step appearance: the budget starts with no
                // click, so a user reading the screen or off doing the TCC
                // drags routinely outlives it.
                break
            case .registrationGone:
                state = .installFailed(.unknown)
            }
        case .notFound:
            state = .installFailed(.plistWriteFailed)
        case .notRegistered:
            state = .installFailed(.launchctlBootstrapFailed)
        @unknown default:
            state = .installFailed(.unknown)
        }
    }

    private func refreshRegistrationAfterFailedPoll(
        timeoutSeconds: TimeInterval,
        probeIntervalSeconds: TimeInterval,
        approvalTimeoutSeconds: TimeInterval,
        approvalPollIntervalSeconds: TimeInterval,
        convergenceTimeoutSeconds: TimeInterval,
        // Carries the wrong version forward only when the refresh was triggered by
        // a version mismatch (not a plain reachability timeout), so the post-refresh
        // pass can resolve a non-converging swap to `.daemonVersionMismatch`
        // (SCR-136).
        priorMismatchVersion: String? = nil
    ) async {
        state = .registering
        let refreshedStatus: SMAppService.Status
        do {
            refreshedStatus = try await registrationService.refresh(plistName: Self.plistName)
        } catch {
            daemonInstallLogger.error("Daemon registration refresh failed: \(String(describing: error), privacy: .public)")
            state = .installFailed(Self.failureReason(from: error))
            return
        }

        await handleRegisteredStatus(
            refreshedStatus,
            timeoutSeconds: timeoutSeconds,
            probeIntervalSeconds: probeIntervalSeconds,
            approvalTimeoutSeconds: approvalTimeoutSeconds,
            approvalPollIntervalSeconds: approvalPollIntervalSeconds,
            convergenceTimeoutSeconds: convergenceTimeoutSeconds,
            allowRegistrationRefresh: false,
            priorMismatchVersion: priorMismatchVersion
        )
    }

    /// How an approval wait ended. `stillPending` (deadline expiry with the
    /// label still `.requiresApproval`) is deliberately distinct from
    /// `registrationGone` (the label vanished): the former is user inaction and
    /// must not render as an install failure.
    private enum ApprovalWaitOutcome {
        case approved
        case stillPending
        case registrationGone
    }

    private func waitForApproval(
        timeoutSeconds: TimeInterval, intervalSeconds: TimeInterval
    ) async -> ApprovalWaitOutcome {
        let deadline = Date().addingTimeInterval(timeoutSeconds)
        while true {
            let status = registrationService.currentStatus(plistName: Self.plistName)
            if status == .enabled { return .approved }
            if status == .notFound || status == .notRegistered { return .registrationGone }
            if Date() >= deadline { return .stillPending }
            await sleep(Self.nanoseconds(for: intervalSeconds))
        }
    }

    private enum DaemonPollOutcome: Equatable {
        case running                          // reachable and version-matched
        case versionMismatch(running: String) // reachable but the wrong version
        case timedOut                         // never became reachable
    }

    private func pollDaemon(
        timeoutSeconds: TimeInterval,
        probeIntervalSeconds: TimeInterval,
        convergenceTimeoutSeconds: TimeInterval,
        convergeOnMismatch: Bool
    ) async -> DaemonPollOutcome {
        state = .polling
        // The post-refresh convergence poll uses the longer convergence budget so
        // a stale daemon slow to exit after bootout (launchd ExitTimeOut=30s)
        // can't surface a false `.daemonVersionMismatch` past the 10s reachability
        // budget (SCR-135). The first poll keeps the generic reachability budget
        // and its fast-fail behavior. Matches the 30s `confirm_fresh_daemon` poll.
        let budget = convergeOnMismatch ? convergenceTimeoutSeconds : timeoutSeconds
        let deadline = now().addingTimeInterval(budget)
        // Last wrong version seen, so a mismatch that is momentarily unreachable
        // at the exact deadline tick still resolves to .versionMismatch rather
        // than a misleading .timedOut (SCR-135).
        var lastMismatch: String?
        while true {
            if let version = await probe.probe(timeout: min(1, max(0.1, probeIntervalSeconds))) {
                // "Socket reachable" is necessary but NOT sufficient (SCR-121): a
                // stale/foreign daemon squatting api.sock answers daemon.info too.
                // Only adopt it when its version matches the bundle we would
                // install. When the expected version is unknown (nil) we fail safe
                // to the historical reachable-implies-running behavior rather than
                // risk a false "out of date" on a healthy daemon.
                if let expected = expectedDaemonVersion, version != expected {
                    // A daemon swap is not instantaneous: after a reinstall the
                    // stale daemon can keep answering until it exits and the fresh
                    // one binds api.sock. Give that swap a convergence budget
                    // instead of failing on the first probe (SCR-135), mirroring
                    // the existing reachability budget below. The first poll
                    // (convergeOnMismatch == false) still fails fast to trigger
                    // the reinstall promptly.
                    if convergeOnMismatch, now() < deadline {
                        lastMismatch = version
                        await sleep(Self.nanoseconds(for: probeIntervalSeconds))
                        continue
                    }
                    return .versionMismatch(running: version)
                }
                state = .installedAndRunning
                NotificationCenter.default.post(name: .screenCapDaemonInstalledAndRunning, object: nil)
                // SCR-200 U3/U4: pre-populate the daemon's rows and clear decoys
                // now that it is up — once per version (the gate lives inside).
                fireProactiveTccSetupIfNeeded()
                return .running
            }
            if now() >= deadline {
                if let lastMismatch {
                    return .versionMismatch(running: lastMismatch)
                }
                let seconds = Int(budget)
                state = .pollingFailed(reason: "Daemon did not respond within \(seconds)s")
                return .timedOut
            }
            await sleep(Self.nanoseconds(for: probeIntervalSeconds))
        }
    }

    private static func nanoseconds(for seconds: TimeInterval) -> UInt64 {
        UInt64(max(0, seconds) * 1_000_000_000)
    }

    #if DEBUG
    // The single-flight latch is process-global static state; tests that
    // exercise it must be able to set and restore it deterministically.
    static func _testSetInstallInFlight(_ inFlight: Bool) { installInFlight = inFlight }
    static var _testInstallInFlight: Bool { installInFlight }
    #endif

    private static func failureReason(from error: Error) -> InstallFailureReason {
        let nsError = error as NSError
        switch nsError.code {
        case kSMErrorInvalidSignature:
            return .daemonSigningInvalid
        case kSMErrorJobPlistNotFound, kSMErrorInvalidPlist, kSMErrorToolNotValid:
            return .plistWriteFailed
        default:
            let text = "\(nsError.localizedDescription) \(nsError.localizedFailureReason ?? "")".lowercased()
            if text.contains("signature") || text.contains("signing") {
                return .daemonSigningInvalid
            }
            if text.contains("no space") || text.contains("disk full") {
                return .diskFull
            }
            if text.contains("bootstrap") || text.contains("launchctl") {
                return .launchctlBootstrapFailed
            }
            return .unknown
        }
    }
}

@MainActor
final class SMAppServiceRegistration: DaemonRegistrationService {
    func register(plistName: String) async throws -> SMAppService.Status {
        let service = SMAppService.agent(plistName: plistName)
        if service.status == .enabled || service.status == .requiresApproval {
            return service.status
        }
        do {
            try service.register()
        } catch {
            let nsError = error as NSError
            if nsError.code != kSMErrorAlreadyRegistered {
                throw error
            }
        }
        return service.status
    }

    /// Destructive: unregisters the LaunchAgent, then re-registers. If the
    /// `register()` step fails after `unregister()` succeeded, the user is
    /// left WITHOUT a registered helper — any pre-existing TCC approval for
    /// the daemon's bundle identifier may need to be re-granted (the user
    /// will be prompted again on next install). Callers must handle the
    /// thrown error by surfacing a concrete failure reason via
    /// `state = .installFailed(...)` so the UI can guide a manual retry.
    /// Prefer non-destructive recovery (e.g. `launchctl kickstart`) when
    /// the daemon socket already exists and only its process is wedged.
    func refresh(plistName: String) async throws -> SMAppService.Status {
        let service = SMAppService.agent(plistName: plistName)
        if service.status == .enabled || service.status == .requiresApproval {
            try await service.unregister()
        }
        do {
            try service.register()
        } catch {
            let nsError = error as NSError
            if nsError.code != kSMErrorAlreadyRegistered {
                throw error
            }
        }
        return service.status
    }

    func currentStatus(plistName: String) -> SMAppService.Status {
        SMAppService.agent(plistName: plistName).status
    }
}

@MainActor
final class LiveDaemonProbe: DaemonProbe {
    func probe(timeout: TimeInterval) async -> String? {
        do {
            let response: DaemonInfoResponse = try await DaemonClient.request(
                method: "GET",
                path: "/v0/daemon.info",
                timeout: timeout
            )
            return response.daemonVersion
        } catch {
            daemonInstallLogger.debug("daemon.info probe failed: \(String(describing: error), privacy: .public)")
            return nil
        }
    }
}

/// Evicts a stale/squatting daemon via `launchctl bootout` so it releases
/// api.sock before the reinstall re-registers (SCR-135). Mirrors the
/// `reconcile_daemon_version` dislodge in `build_and_run.sh`.
@MainActor
final class LaunchctlDaemonTerminator: DaemonTerminator {
    func bootout() async {
        // Never bootout from the test host process — the unit tests inject a
        // fake terminator; this guard is defense-in-depth against a missed
        // injection accidentally tearing down a developer's real daemon.
        guard ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] == nil else { return }

        let label = "gui/\(getuid())/com.screencap.daemon"
        do {
            let result = try await runLaunchctl(["bootout", label])
            // `bootout` exits non-zero when the label simply isn't loaded — the
            // desired end state, not a failure. Distinguish a real bootout
            // failure (the squatter is still registered and blocks the fresh
            // daemon from binding) from the benign "already gone" case by
            // re-checking with `launchctl print`: if print also fails, the label
            // is not registered and the non-zero bootout was the no-op we want.
            // Only log when the label persists — mirrors reconcile_daemon_version
            // in build_and_run.sh (L392-401).
            if result.terminationStatus != 0 {
                let printResult = try? await runLaunchctl(["print", label])
                if printResult?.terminationStatus == 0 {
                    daemonInstallLogger.error("launchctl bootout exit \(result.terminationStatus, privacy: .public) but \(label, privacy: .public) is still registered; stale daemon may block the fresh one: \(result.stderr, privacy: .public)")
                }
            }
        } catch {
            daemonInstallLogger.debug("launchctl bootout spawn failed: \(String(describing: error), privacy: .public)")
        }
    }
}

/// Resolves the daemon version this app bundles, stamped into the app bundle at
/// build time by `Scripts/embed-cli.sh` (`Contents/Resources/screencap-cli-version`).
/// Returns nil — disabling the version gate — when it cannot be determined with
/// confidence, so the gate never produces a false "out of date" (SCR-121).
enum BundledDaemonVersion {
    static let resourceName = "screencap-cli-version"

    static func expected() -> String? {
        // Under XCTest, Bundle.main is the host app bundle; tests inject the
        // expected version explicitly, so don't compare against the real stamp.
        guard ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] == nil else { return nil }
        // Dev-source mode runs the daemon from in-repo source, whose version can
        // legitimately differ from the (possibly stale) bundled CLI. build_and_run.sh
        // owns staleness there; skip the gate to avoid false mismatches.
        guard ProcessInfo.processInfo.environment["SCREENCAP_DAEMON_USE_DEV_SOURCE"] != "1" else { return nil }
        guard
            let url = Bundle.main.url(forResource: resourceName, withExtension: nil),
            let raw = try? String(contentsOf: url, encoding: .utf8)
        else {
            return nil
        }
        let version = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        return version.isEmpty ? nil : version
    }
}
