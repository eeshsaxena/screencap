import Foundation

/// Persistence for the onboarding wizard's state (U11 / KTD-10): the write-once
/// completion marker, the wizard-in-progress flags, and the prior-install
/// evidence probe that keeps existing users out of the marketing wizard.
///
/// The completion marker lives on disk at `~/.screencap/.onboarding-completed-v1`
/// (the `MigrationMarkerStore` idiom) so a reinstall of the app over an existing
/// `~/.screencap` never re-runs the wizard. The in-progress flags are
/// `UserDefaults` — they only need to survive the wizard's own Quit & Relaunch
/// loop, not a reinstall.
///
/// **The marker is UX-only.** It decides whether the wizard takeover presents;
/// it never gates recording, permissions, or the daemon's own TCC verification.
struct OnboardingMarkerStore {
    static let markerName = ".onboarding-completed-v1"
    /// Set when the wizard auto-presents on a fresh install; cleared on
    /// finish/skip. Keeps the wizard re-presenting across relaunches even after
    /// the app's own first-launch writes (e.g. `ensureFirstLaunchModeWritten`'s
    /// `config.toml`) would otherwise read as prior-install evidence.
    static let wizardPendingDefaultsKey = "com.screencap.macos.onboardingWizardPending"
    /// Set when the user advances past the welcome step, so a mid-wizard
    /// relaunch re-derives to the permissions/app-rules step rather than
    /// restarting at welcome (KTD-10).
    static let wizardStartedDefaultsKey = "com.screencap.macos.onboardingWizardStarted"

    private let baseDirectory: URL
    private let launchAgentsDirectory: URL
    private let fileManager: FileManager
    private let defaults: UserDefaults

    init(
        baseDirectory: URL = AppPaths.screencapHome,
        launchAgentsDirectory: URL = OnboardingMarkerStore.defaultLaunchAgentsDirectory,
        fileManager: FileManager = .default,
        defaults: UserDefaults = .standard
    ) {
        self.baseDirectory = baseDirectory
        self.launchAgentsDirectory = launchAgentsDirectory
        self.fileManager = fileManager
        self.defaults = defaults
    }

    /// `~/Library/LaunchAgents` — where `screencap setup` installs the daemon
    /// LaunchAgent plist (the CLI-install evidence path).
    static var defaultLaunchAgentsDirectory: URL {
        URL(fileURLWithPath: NSHomeDirectory(), isDirectory: true)
            .appendingPathComponent("Library", isDirectory: true)
            .appendingPathComponent("LaunchAgents", isDirectory: true)
    }

    private var markerURL: URL {
        baseDirectory.appendingPathComponent(Self.markerName, isDirectory: false)
    }

    // MARK: - Completion marker (disk, write-once semantics)

    func isCompleted() -> Bool {
        fileManager.fileExists(atPath: markerURL.path)
    }

    /// Idempotent atomic write; creates `~/.screencap` if absent. Throws on a
    /// genuine IO failure — the acceptable fallback is the wizard-vs-backfill
    /// decision re-running next launch.
    func markCompleted() throws {
        try fileManager.createDirectory(at: baseDirectory, withIntermediateDirectories: true)
        try Data().write(to: markerURL, options: .atomic)
    }

    // MARK: - Prior-install evidence (KTD-10)

    /// True when this machine shows signs of a pre-wizard ScreenCap install:
    /// a recordings directory, a config file, or an installed daemon
    /// LaunchAgent. Used once, before the wizard has ever presented — after
    /// that the pending flag (not evidence) keeps the wizard alive, because the
    /// app's own first-launch writes create `config.toml` and would otherwise
    /// masquerade as a prior install.
    func hasPriorInstallEvidence() -> Bool {
        let recordingsDir = baseDirectory.appendingPathComponent("recordings", isDirectory: true)
        let configFile = baseDirectory.appendingPathComponent("config.toml", isDirectory: false)
        let launchAgent = launchAgentsDirectory
            .appendingPathComponent("com.screencap.daemon.plist", isDirectory: false)
        return fileManager.fileExists(atPath: recordingsDir.path)
            || fileManager.fileExists(atPath: configFile.path)
            || fileManager.fileExists(atPath: launchAgent.path)
    }

    // MARK: - In-progress flags (UserDefaults)

    var wizardPending: Bool {
        defaults.bool(forKey: Self.wizardPendingDefaultsKey)
    }

    func setWizardPending(_ pending: Bool) {
        defaults.set(pending, forKey: Self.wizardPendingDefaultsKey)
    }

    var wizardStarted: Bool {
        defaults.bool(forKey: Self.wizardStartedDefaultsKey)
    }

    func setWizardStarted(_ started: Bool) {
        defaults.set(started, forKey: Self.wizardStartedDefaultsKey)
    }
}
