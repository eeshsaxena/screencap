import Combine
import Foundation

/// Owner of runtime ambient-capture state for the Ambient settings section
/// (SCR-214 U12). Reads `ambient.status` and writes through `ambient.set` /
/// `recording.pause` / `.resume` behind the `AmbientService` seam (no live
/// socket needed in tests). Mirrors `IntelligenceController`'s discipline:
/// reflect the daemon's CONFIRMED state, guard concurrent writes, and surface a
/// PENDING state while a request is in flight.
///
/// Two invariants drive the UI:
///   1. Confirmed-not-optimistic — `enabled`/`autostart` come from the payload
///      `ambient.set` returns, and `paused` comes from a fresh `ambient.status`
///      read after a pause/resume (the verb echoes only the requested state, so
///      the engine's `recording_paused`/`recording_resumed` event settling
///      `ambient.status` is the only source of truth — R2/U4 KTD7).
///   2. Consent-gated — the first enable is blocked until the user accepts the
///      always-on-audio consent (R1). `enableWithConsentGate()` returns
///      `.needsConsent` WITHOUT any daemon write when consent is missing.
@MainActor
final class AmbientController: ObservableObject {
    /// The last-read confirmed status, or nil until the first `load()`.
    @Published private(set) var status: AmbientStatus?
    /// True while a mutating request round-trips — drives the control's
    /// disabled/spinner state so the user can't double-fire a toggle.
    @Published private(set) var pending: Bool = false
    /// Inline error after a failed request (the confirmed state is reconciled).
    @Published private(set) var lastError: String?
    /// Whether the user has accepted the first-run always-on-audio consent (R1).
    /// Persisted in `UserDefaults` so the consent sheet is shown exactly once.
    @Published private(set) var hasAudioConsent: Bool

    private let service: AmbientService
    private let defaults: UserDefaults

    /// `UserDefaults` key holding the one-time always-on-audio consent flag (R1).
    static let audioConsentKey = "ambientAudioConsented"

    init(service: AmbientService = LiveAmbientService(), defaults: UserDefaults = .standard) {
        self.service = service
        self.defaults = defaults
        self.hasAudioConsent = defaults.bool(forKey: Self.audioConsentKey)
    }

    // MARK: - Derived confirmed state (nil status → off/idle)

    var enabled: Bool { status?.enabled ?? false }
    var autostart: Bool { status?.autostart ?? false }
    var active: Bool { status?.active ?? false }
    var paused: Bool { status?.paused ?? false }
    var degraded: String? { status?.degraded }
    var recordingName: String? { status?.recording }

    /// Enabled but the daemon reports a blocked/ceiling reason
    /// (permission-denied / paywall-blocked / crash-ceiling). The section shows
    /// this as an error state with copy — NEVER a silent on-with-nothing-recording.
    var isBlocked: Bool { enabled && (status?.degraded != nil) }

    /// The first enable must clear the always-on-audio consent gate (R1).
    var needsAudioConsent: Bool { !hasAudioConsent }

    // MARK: - Read

    /// Re-fetch `ambient.status`. A failure surfaces in `lastError` and leaves
    /// `status` unchanged so a transient daemon hiccup doesn't blank the section.
    func load() async {
        do {
            status = try await service.ambientStatus()
            lastError = nil
        } catch {
            lastError = error.localizedDescription
        }
    }

    // MARK: - Writes

    /// Persist + apply the enable toggle. Reflects the CONFIRMED payload the verb
    /// returns (not an optimistic flip). This is the post-consent write path —
    /// the consent gate lives in `enableWithConsentGate()`, so callers turning
    /// ambient ON for the first time must route through that instead.
    @discardableResult
    func setEnabled(_ on: Bool) async -> Bool {
        await applySet(enabled: on, autostart: nil)
    }

    /// Persist + apply the auto-start-on-login toggle, independently of `enabled`.
    @discardableResult
    func setAutostart(_ on: Bool) async -> Bool {
        await applySet(enabled: nil, autostart: on)
    }

    /// The shared `ambient.set` write: guard concurrent writes, reflect the
    /// confirmed returned status, and reconcile against the daemon on failure so
    /// a toggle never sticks on an optimistic value that never landed.
    private func applySet(enabled: Bool?, autostart: Bool?) async -> Bool {
        guard !pending else { return false }
        pending = true
        defer { pending = false }
        do {
            status = try await service.ambientSet(enabled: enabled, autostart: autostart)
            lastError = nil
            return true
        } catch {
            lastError = error.localizedDescription
            await load()
            return false
        }
    }

    // MARK: - Pause / resume (U4)

    func pause() async { await setPaused(true) }
    func resume() async { await setPaused(false) }

    /// Drive the pause verb, then re-read `ambient.status` so the UI reflects the
    /// engine's CONFIRMED `paused` (the verb echoes only the requested state).
    /// A no-op when nothing ambient is live or a request is already in flight.
    private func setPaused(_ paused: Bool) async {
        guard !pending, active else { return }
        pending = true
        defer { pending = false }
        do {
            if paused {
                try await service.recordingPause()
            } else {
                try await service.recordingResume()
            }
            lastError = nil
        } catch {
            lastError = error.localizedDescription
        }
        // Always reconcile — confirmed pause state only exists on `ambient.status`.
        await load()
    }

    // MARK: - First-run consent gate (R1)

    /// The outcome of an enable attempt that runs through the consent gate.
    enum EnableGate: Equatable {
        /// Consent is required; NO daemon write happened. The caller must present
        /// the always-on-audio consent sheet before ambient can start.
        case needsConsent
        /// Consent was already on record; the enable write was issued.
        case applied
    }

    /// Enable ambient, enforcing the first-run always-on-audio consent (R1). When
    /// consent is missing this returns `.needsConsent` and issues NO daemon
    /// write — ambient stays off until the user accepts. When consent is already
    /// recorded it fires the enable write and returns `.applied`.
    @discardableResult
    func enableWithConsentGate() async -> EnableGate {
        guard hasAudioConsent else { return .needsConsent }
        await setEnabled(true)
        return .applied
    }

    /// Record the always-on-audio consent (persisted once) and enable ambient.
    /// Called only from the consent sheet's accept action.
    func acceptAudioConsentAndEnable() async {
        recordAudioConsent()
        await setEnabled(true)
    }

    /// Persist the one-time consent flag. Idempotent.
    func recordAudioConsent() {
        defaults.set(true, forKey: Self.audioConsentKey)
        hasAudioConsent = true
    }
}
