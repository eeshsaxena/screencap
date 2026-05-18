import Combine
import Foundation

/// Owner of privacy-related state shared by the Privacy pane
/// (`PrivacyPaneView`), the first-run banner gate (`MainWindow`), and the
/// menu bar attention dot (`ScreenCapApp.MenuBarExtra`). Single source of
/// truth — bound as a singleton `@StateObject` in `ScreenCapApp` and pushed
/// into the environment.
///
/// All writes flow through the existing `screencap settings privacy` CLI
/// surface (R16 invariant — mode is never silently mutated by exclude/allow
/// writes, and the advisory flock + symmetric-envelope semantics live in
/// one place in Python).
@MainActor
final class PrivacyController: ObservableObject {
    @Published private(set) var apps: [InstalledApp] = []
    @Published private(set) var status: PrivacyStatus?
    @Published private(set) var isLoading: Bool = false
    @Published private(set) var lastError: String?

    /// Pluggable CLI invoker. Tests inject a fixture closure; production
    /// resolves to `CLIClient.runJSONRaw`. Keeping the seam at the controller
    /// boundary (rather than mocking `Process`) means tests can assert on
    /// exact argv vectors — the contract that ships to Python — without
    /// touching Foundation pipe machinery.
    typealias JSONInvoker = @Sendable ([String]) async throws -> Data
    private let invoke: JSONInvoker

    /// In-flight toggles serialized per bundle_id. A double-tap on the toggle
    /// during the CLI round-trip would otherwise fire two writes against the
    /// same bundle and leave the UI optimistic state out of sync with disk.
    private var pendingToggles: Set<String> = []

    /// Latches once `ensureFirstLaunchModeWritten()` has finished its work
    /// (either confirmed the section exists, or succeeded in writing it).
    /// Crucially, set only after the work succeeds — a CLI failure mid-launch
    /// must leave the latch open so the next .task fire can retry instead of
    /// stranding the user in a half-initialized state for the session.
    private var firstLaunchWriteCompleted: Bool = false

    /// Serializes overlapping `markSetupComplete` invocations. Both banner
    /// CTAs plus the pane's `.onAppear` plus a rapid double-tap on the same
    /// CTA can all reach the controller concurrently — the underlying CLI
    /// write is idempotent, but firing it 4× per dismiss wastes a flock
    /// acquisition and emits noise into the telemetry stream.
    private var setupCompleteInFlight: Bool = false

    /// True when the first-run banner should be shown. Driven by `status`:
    /// hidden until status loads (UI fail-closed — the banner is the
    /// disclosure surface, not a security boundary) and again once the user
    /// dismisses or completes setup.
    var bannerActive: Bool {
        guard let s = status else { return false }
        return !s.setupSkipped
    }

    init(invoke: @escaping JSONInvoker = PrivacyController.defaultInvoke) {
        self.invoke = invoke
    }

    /// Default invoker — talks to the bundled `screencap` binary via
    /// `CLIClient`.
    static let defaultInvoke: JSONInvoker = { args in
        try await CLIClient.runJSONRaw(args)
    }

    // MARK: - Reads

    /// Re-fetch the installed app list. Failures surface in `lastError` and
    /// leave `apps` unchanged so a transient CLI hiccup doesn't blank the
    /// pane while the user is mid-scroll.
    func refreshApps() async {
        isLoading = true
        defer { isLoading = false }
        do {
            let data = try await invoke(["apps", "--json"])
            let envelope = try JSONDecoder().decode(AppsEnvelope.self, from: data)
            if !envelope.ok {
                lastError = envelope.error ?? "apps --json reported ok=false"
                return
            }
            apps = envelope.apps
            lastError = nil
        } catch {
            lastError = error.localizedDescription
        }
    }

    /// Re-fetch the privacy `status` block. A missing `privacy` block in the
    /// payload (older CLI without the v2 settings schema) leaves `status` as
    /// nil — the banner stays hidden rather than appearing on a stale CLI
    /// where its dismiss CTAs can't write the matching field.
    func refreshStatus() async {
        do {
            let data = try await invoke(["settings", "--json"])
            let envelope = try JSONDecoder().decode(SettingsEnvelope.self, from: data)
            guard let p = envelope.settings.privacy else { return }
            status = p
            lastError = nil
        } catch {
            lastError = error.localizedDescription
        }
    }

    // MARK: - Writes

    /// Toggle `exclude_apps` membership for `bundleId`. Idempotent at the CLI
    /// layer — `add` of an already-present value is a no-op exit 0, ditto for
    /// `remove` of an absent value. On success the app list is refreshed so
    /// the row's optimistic state converges with disk truth; on failure the
    /// error is surfaced and the caller's optimistic toggle gets reseeded
    /// when the next `refreshApps` overwrites the row.
    func toggleExclude(bundleId: String, excluded: Bool) async {
        guard !pendingToggles.contains(bundleId) else { return }
        pendingToggles.insert(bundleId)
        defer { pendingToggles.remove(bundleId) }

        let op = excluded ? "add" : "remove"
        do {
            _ = try await invoke(["settings", "privacy", "exclude_apps", op, bundleId, "--json"])
            lastError = nil
        } catch {
            lastError = error.localizedDescription
        }
        // Refresh after both success and failure paths: success ensures
        // `in_exclude_apps` reflects the write; failure reseeds the row's
        // optimistic toggle from disk so the user doesn't see a toggle
        // position that contradicts the configured state.
        await refreshApps()
    }

    /// Mark first-run setup complete (`setup_skipped = true`). Both banner
    /// CTAs and the pane's `.onAppear` call this — overlapping invocations
    /// short-circuit so the CLI write fires exactly once per dismiss.
    ///
    /// Optimistically flips `status.setupSkipped` to true after the CLI
    /// write succeeds, before the follow-up `refreshStatus()`. Without that,
    /// a failure on the status refresh would resurrect the banner the user
    /// just dismissed, even though the underlying CLI write succeeded.
    func markSetupComplete() async {
        if setupCompleteInFlight { return }
        setupCompleteInFlight = true
        defer { setupCompleteInFlight = false }
        do {
            _ = try await invoke(["settings", "privacy", "setup_skipped", "set", "true", "--json"])
            if var current = status {
                current = PrivacyStatus(
                    mode: current.mode,
                    setupSkipped: true,
                    hasPrivacySection: current.hasPrivacySection
                )
                status = current
            }
            await refreshStatus()
        } catch {
            lastError = error.localizedDescription
        }
    }

    /// First-launch fail-closed: when no `[privacy]` section exists on disk,
    /// write `mode = internal` before the banner appears so a user who never
    /// touches the dismiss CTAs still gets the stricter default. Probes the
    /// on-disk state itself (does not rely on `self.status`) because the
    /// caller invokes this before the first explicit `refreshStatus()`.
    ///
    /// The latch is set ONLY after the work completes — a transient CLI
    /// failure mid-launch must leave it open so the next `.task` fire (e.g.
    /// after the user reopens the window from the menu bar) can retry.
    func ensureFirstLaunchModeWritten() async {
        if firstLaunchWriteCompleted { return }
        do {
            let data = try await invoke(["settings", "--json"])
            let envelope = try JSONDecoder().decode(SettingsEnvelope.self, from: data)
            guard let p = envelope.settings.privacy, !p.hasPrivacySection else {
                // Either the CLI is too old to return the v2 privacy block, or
                // the [privacy] section already exists — both are no-write
                // states. Latch only on the present-section case; an old CLI
                // is a transient state that may resolve on the next refresh.
                if let p = envelope.settings.privacy, p.hasPrivacySection {
                    firstLaunchWriteCompleted = true
                }
                return
            }
            _ = try await invoke(["settings", "privacy", "mode", "set", "internal", "--json"])
            firstLaunchWriteCompleted = true
        } catch {
            lastError = error.localizedDescription
            // Do NOT latch on failure — the next .task fire should retry.
        }
    }
}
