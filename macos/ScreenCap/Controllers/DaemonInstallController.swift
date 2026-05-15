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

@MainActor
protocol DaemonRegistrationService {
    func register(plistName: String) async throws -> SMAppService.Status
    func refresh(plistName: String) async throws -> SMAppService.Status
    func currentStatus(plistName: String) -> SMAppService.Status
}

@MainActor
protocol DaemonProbe {
    func probe(timeout: TimeInterval) async -> Bool
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
        case unknown
    }

    @Published private(set) var state: State = .idle

    static let plistName = "com.screencap.daemon.plist"
    static let registrationAttemptedKey = "com.screencap.macos.daemonInstallAttempted"

    private let registrationService: DaemonRegistrationService
    private let probe: DaemonProbe
    private let sleep: (UInt64) async -> Void

    init(
        registrationService: DaemonRegistrationService = SMAppServiceRegistration(),
        probe: DaemonProbe = LiveDaemonProbe(),
        sleep: @escaping (UInt64) async -> Void = { nanoseconds in
            try? await Task.sleep(nanoseconds: nanoseconds)
        }
    ) {
        self.registrationService = registrationService
        self.probe = probe
        self.sleep = sleep
    }

    func install(
        timeoutSeconds: TimeInterval = 10,
        probeIntervalSeconds: TimeInterval = 0.5,
        approvalTimeoutSeconds: TimeInterval = 60,
        approvalPollIntervalSeconds: TimeInterval = 2
    ) async {
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
            allowRegistrationRefresh: true
        )
    }

    func retry(
        timeoutSeconds: TimeInterval = 10,
        probeIntervalSeconds: TimeInterval = 0.5,
        approvalTimeoutSeconds: TimeInterval = 60,
        approvalPollIntervalSeconds: TimeInterval = 2
    ) async {
        await install(
            timeoutSeconds: timeoutSeconds,
            probeIntervalSeconds: probeIntervalSeconds,
            approvalTimeoutSeconds: approvalTimeoutSeconds,
            approvalPollIntervalSeconds: approvalPollIntervalSeconds
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
        allowRegistrationRefresh: Bool
    ) async {
        switch status {
        case .enabled:
            let didStart = await pollDaemon(timeoutSeconds: timeoutSeconds, probeIntervalSeconds: probeIntervalSeconds)
            if !didStart && allowRegistrationRefresh {
                await refreshRegistrationAfterFailedPoll(
                    timeoutSeconds: timeoutSeconds,
                    probeIntervalSeconds: probeIntervalSeconds,
                    approvalTimeoutSeconds: approvalTimeoutSeconds,
                    approvalPollIntervalSeconds: approvalPollIntervalSeconds
                )
            }
        case .requiresApproval:
            state = .requiresApproval
            let approved = await waitForApproval(
                timeoutSeconds: approvalTimeoutSeconds,
                intervalSeconds: approvalPollIntervalSeconds
            )
            if approved {
                await handleRegisteredStatus(
                    .enabled,
                    timeoutSeconds: timeoutSeconds,
                    probeIntervalSeconds: probeIntervalSeconds,
                    approvalTimeoutSeconds: approvalTimeoutSeconds,
                    approvalPollIntervalSeconds: approvalPollIntervalSeconds,
                    allowRegistrationRefresh: allowRegistrationRefresh
                )
            } else {
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
        approvalPollIntervalSeconds: TimeInterval
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
            allowRegistrationRefresh: false
        )
    }

    private func waitForApproval(timeoutSeconds: TimeInterval, intervalSeconds: TimeInterval) async -> Bool {
        let deadline = Date().addingTimeInterval(timeoutSeconds)
        while true {
            let status = registrationService.currentStatus(plistName: Self.plistName)
            if status == .enabled { return true }
            if status == .notFound || status == .notRegistered { return false }
            if Date() >= deadline { return false }
            await sleep(Self.nanoseconds(for: intervalSeconds))
        }
    }

    private func pollDaemon(timeoutSeconds: TimeInterval, probeIntervalSeconds: TimeInterval) async -> Bool {
        state = .polling
        let deadline = Date().addingTimeInterval(timeoutSeconds)
        while true {
            if await probe.probe(timeout: min(1, max(0.1, probeIntervalSeconds))) {
                state = .installedAndRunning
                NotificationCenter.default.post(name: .screenCapDaemonInstalledAndRunning, object: nil)
                return true
            }
            if Date() >= deadline {
                let seconds = Int(timeoutSeconds)
                state = .pollingFailed(reason: "Daemon did not respond within \(seconds)s")
                return false
            }
            await sleep(Self.nanoseconds(for: probeIntervalSeconds))
        }
    }

    private static func nanoseconds(for seconds: TimeInterval) -> UInt64 {
        UInt64(max(0, seconds) * 1_000_000_000)
    }

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
    func probe(timeout: TimeInterval) async -> Bool {
        do {
            let _: DaemonInfoResponse = try await DaemonClient.request(
                method: "GET",
                path: "/v0/daemon.info",
                timeout: timeout
            )
            return true
        } catch {
            daemonInstallLogger.debug("daemon.info probe failed: \(String(describing: error), privacy: .public)")
            return false
        }
    }
}
