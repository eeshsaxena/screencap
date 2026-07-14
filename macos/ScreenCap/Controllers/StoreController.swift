import Combine
import Foundation

/// Owns the app's Lock / Unlock affordances for the SCR-258 encrypted store
/// (U10, KTD-15/KTD-16). The store *state* itself lives on `RecordingsIndex`
/// (`storeState`); this controller owns the ACTIONS plus the menu-facing phase.
///
/// Unlock is gated by a **store-scoped** `PresenceGate` — its own instance, own
/// reason copy, and NO grace window (a fresh present-user check every time), never
/// the corpus display gate. Present-user auth runs only on the explicit Unlock tap
/// (no eager LocalAuthentication on render / menu open — the keychain-eager-decrypt
/// learning). On success the unlock verb is called (the daemon trusts its same-EUID
/// caller); on failure/cancel the store stays sealed with no verb call (fail-closed).
///
/// Lock gives immediate "Locking…" feedback decoupled from the async
/// stop→quiesce→detach chain (the HUD-freeze learning): `phase` flips on tap and the
/// `await` only drives error/retry.
@MainActor
final class StoreController: ObservableObject {
    enum Phase: Equatable {
        case idle
        case locking
        case unlocking
        case initializing
    }

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var lastError: String?

    /// The store-scoped present-user gate (KTD-16). Grace window 0 → every unlock
    /// re-prompts ("present-user means present"); a separate instance so it never
    /// shares the corpus display gate's grace.
    let presenceGate: PresenceGate

    /// Refreshed after every action so the surfaces re-read `storeState`.
    private weak var index: RecordingsIndex?

    // Injectable action seams (tests supply fakes; production talks to the
    // daemon with a CLI fallback).
    private let lockAction: @Sendable () async throws -> Void
    private let unlockAction: @Sendable () async throws -> Void
    private let initAction: @Sendable () async throws -> Void

    init(
        presenceGate: PresenceGate = PresenceGate(graceWindow: 0),
        lockAction: @escaping @Sendable () async throws -> Void = StoreController.defaultLock,
        unlockAction: @escaping @Sendable () async throws -> Void = StoreController.defaultUnlock,
        initAction: @escaping @Sendable () async throws -> Void = StoreController.defaultInit
    ) {
        self.presenceGate = presenceGate
        self.lockAction = lockAction
        self.unlockAction = unlockAction
        self.initAction = initAction
    }

    /// Bind the recordings index so completed actions refresh `storeState`.
    func bind(index: RecordingsIndex) {
        self.index = index
    }

    /// Lock (menu / Library). Immediate feedback; the await only drives error.
    func lock() {
        guard phase == .idle else { return }
        Task { await performLock() }
    }

    /// Unlock (menu / Library). Fail-closed: runs the store-scoped `PresenceGate`
    /// first, and only on success calls the unlock verb.
    func unlock() {
        guard phase == .idle else { return }
        Task { await performUnlock() }
    }

    /// Set up encrypted storage from the `.absent` onboarding state.
    func initializeStore() {
        guard phase == .idle else { return }
        Task { await performInitialize() }
    }

    // MARK: - Awaitable bodies (public entry points wrap these in a Task; tests
    // call them directly). Immediate feedback: `phase` flips first, the await only
    // drives error/retry (the HUD-freeze learning).

    func performLock() async {
        phase = .locking
        lastError = nil
        do {
            try await lockAction()
        } catch {
            lastError = error.localizedDescription
        }
        await index?.refresh()
        phase = .idle
    }

    /// Runs the store-scoped `PresenceGate` FIRST (only on the explicit tap); on
    /// success calls the unlock verb, then refreshes. On auth cancel/failure the
    /// store stays sealed and NO verb fires (fail-closed, KTD-16).
    func performUnlock() async {
        lastError = nil
        let present = await presenceGate.ensurePresent(
            reason: "Unlock your encrypted recordings"
        )
        guard present else { return }  // fail-closed: stays sealed, no verb call
        phase = .unlocking
        do {
            try await unlockAction()
        } catch {
            lastError = error.localizedDescription
        }
        await index?.refresh()
        phase = .idle
    }

    func performInitialize() async {
        phase = .initializing
        lastError = nil
        do {
            try await initAction()
        } catch {
            lastError = error.localizedDescription
        }
        await index?.refresh()
        phase = .idle
    }

    // MARK: - Default (production) actions

    /// Daemon-first lock with a CLI fallback for a no-daemon install. The CLI
    /// fallback writes the sealed sentinel and never force-detaches.
    static let defaultLock: @Sendable () async throws -> Void = {
        do {
            _ = try await DaemonClient.storageLock()
        } catch DaemonClientError.socketUnavailable, DaemonClientError.connectionFailed {
            try await CLIClient.runAwaitingExit(["storage", "lock"], timeout: 60)
        }
    }

    /// Daemon-first unlock. Present-user auth already happened in the app's
    /// `PresenceGate`, so the daemon verb (same-EUID trust) is the primary path.
    /// The rare CLI fallback re-evaluates LocalAuthentication itself — a possible
    /// second prompt on a no-daemon install (documented).
    static let defaultUnlock: @Sendable () async throws -> Void = {
        do {
            _ = try await DaemonClient.storageUnlock()
        } catch DaemonClientError.socketUnavailable, DaemonClientError.connectionFailed {
            try await CLIClient.runAwaitingExit(["storage", "unlock"], timeout: 30)
        }
    }

    /// Set up encrypted storage: create the store, then adopt it into the running
    /// daemon. Creation is foreground-only (the first Keychain write triggers a
    /// one-time ACL prompt), so the key + bundle are minted via the bundled CLI —
    /// the single creation code path (KTD-5). But the daemon resolved its
    /// `store_state` ONCE at bind time and would keep serving the stale `absent`
    /// (Library refresh + `recording.start` stay refused) until a restart, since
    /// the CLI init runs out-of-band. So we then call `storage.mount` to make the
    /// already-running daemon re-mount and flip its cached state `absent → mounted`.
    /// Daemon-unavailable is fine: the no-daemon `list` fallback re-resolves the
    /// store fresh on the next refresh, so there is nothing to adopt.
    static let defaultInit: @Sendable () async throws -> Void = {
        try await CLIClient.runAwaitingExit(["storage", "init"], timeout: 30)
        do {
            _ = try await DaemonClient.storageMount()
        } catch DaemonClientError.socketUnavailable, DaemonClientError.connectionFailed {
            // No daemon to notify — the CLI-fallback refresh re-resolves fresh.
        }
    }
}
