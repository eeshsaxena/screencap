import AppKit
import Combine
import Foundation
import OSLog

private let authLogger = Logger(subsystem: "com.screencap.macos", category: "cloud-auth")

/// Decoded `screencap checkout-url --json` envelope (billing U9): `{ok, url}` on
/// success or `{ok:false, error}`. Tolerant parse like `AuthWhoAmIEnvelope`.
/// `code` is additive (the CLI doesn't emit it for checkout today); absent, the
/// error mapper resolves to its static fallback — never to the `error` text.
private struct CheckoutURLEnvelope: Decodable {
    let ok: Bool?
    let url: String?
    let error: String?
    let code: String?

    static func parse(_ data: Data) -> CheckoutURLEnvelope? {
        guard !data.isEmpty else { return nil }
        return try? JSONDecoder().decode(CheckoutURLEnvelope.self, from: data)
    }
}

/// Decoded `screencap portal-url --json` envelope (account sheet U2/U3):
/// `{ok, schema_version, url}` on success, or exit 1 with `{ok:false,
/// schema_version, error, code}` where `code` ∈ no_subscription |
/// not_signed_in | network | unknown. Tolerant parse like
/// `CheckoutURLEnvelope`; every field is nullable per state and consumers gate
/// only on what their state needs — success needs `url`, the failure path needs
/// only `code` (`AccountErrorCopy` keys on it; `error` text is NEVER rendered).
private struct PortalURLEnvelope: Decodable {
    let ok: Bool?
    let schemaVersion: Int?
    let url: String?
    let error: String?
    let code: String?

    enum CodingKeys: String, CodingKey {
        case ok, url, error, code
        case schemaVersion = "schema_version"
    }

    static func parse(_ data: Data) -> PortalURLEnvelope? {
        guard !data.isEmpty else { return nil }
        return try? JSONDecoder().decode(PortalURLEnvelope.self, from: data)
    }
}

/// The single error-mapping seam for account/billing failures (KTD-5 / R10).
///
/// Keyed on the CLI envelope's machine-readable `code` — never on message
/// text, which drifts silently past fakes — and on the Swift-side `CLIError`
/// for failures where no envelope ever arrived. Every `message` is a STATIC
/// literal with no dynamic interpolation, so a runtime envelope string (or
/// `CLIError.nonZeroExit.localizedDescription`, which embeds raw stderr) can
/// never carry raw backend text or terminal instructions into rendered copy.
enum AccountErrorCopy: Equatable, CaseIterable {
    /// The backend's fail-closed "no active/trialing subscription" 4xx (AE8).
    /// Stripe's subscription search is eventually consistent (~1 min after
    /// checkout), so the copy never asserts that no subscription exists — a
    /// just-paid user must not read "no subscription" and buy again.
    case noSubscription
    /// No stored credential — the fix is signing in, not retrying.
    case notSignedIn
    /// Transient network/auth-refresh trouble — retry is the honest advice.
    case network
    /// Anything unrecognized (absent/future `code`, unexpected Swift error).
    /// Static fallback by construction: no field of the failure is embedded.
    case unknown

    /// The recovery affordance the sheet renders next to the message.
    enum Action: Equatable {
        case retry
        case signIn
    }

    var message: String {
        switch self {
        case .noSubscription:
            return "No subscription found yet — if you just subscribed, try again in a minute."
        case .notSignedIn:
            return "Sign in to manage your subscription."
        case .network:
            return "Couldn't reach the subscription service. Check your connection and try again."
        case .unknown:
            return "Something went wrong. Try again."
        }
    }

    var action: Action {
        switch self {
        case .noSubscription, .network, .unknown:
            return .retry
        case .notSignedIn:
            return .signIn
        }
    }

    /// Map an envelope's `code` field. Absent or unrecognized → the static
    /// fallback (never a peek at the `error` message text).
    static func from(code: String?) -> AccountErrorCopy {
        switch code {
        case "no_subscription": return .noSubscription
        case "not_signed_in": return .notSignedIn
        case "network": return .network
        default: return .unknown
        }
    }

    /// Map a thrown Swift-side error — the shell-out failed before any envelope
    /// existed. A timeout reads as network trouble; everything else takes the
    /// static fallback. Deliberately never surfaces `localizedDescription`:
    /// `CLIError.nonZeroExit`'s description embeds raw stderr (the R10 leak
    /// this seam exists to close).
    static func from(error: Error) -> AccountErrorCopy {
        if case CLIError.timedOut = error { return .network }
        return .unknown
    }
}

/// Sign-in flow state, distinct from `AuthStatus` (which is the persistent
/// signed-in/out fact). Drives the "Waiting for sign-in in your browser…"
/// indicator and the post-failure re-prompt (plan U6 design-review states a/b).
enum SignInFlowState: Equatable {
    case idle
    /// `screencap login` is running and the browser is open. The UI shows a
    /// non-blocking spinner + a Cancel that terminates the shell-out.
    case inProgress
    /// `login` exited non-zero (state mismatch, network error, browser closed,
    /// or the U4 loopback timeout). Carries a short reason for the re-prompt.
    case failed(String)
}

/// Test seam over the `screencap` auth shell-outs so `CloudAuthController` can
/// be exercised without spawning real processes. Production wiring is
/// `LiveCloudAuthService`, which routes through `CLIClient`.
///
/// All token handling stays in Python (plan invariant): Swift only triggers
/// the commands and reads their `--json` state.
@MainActor
protocol CloudAuthService {
    /// Raw stdout of `screencap whoami --json`. Returns the bytes; the
    /// controller decodes (and tolerates garbage → signed-out).
    func fetchWhoAmI() async throws -> Data

    /// Spawns `screencap login --json` (opens the browser). Long-lived and
    /// cancellable: the returned handle's `terminate()` aborts the flow.
    /// `onStdoutLine` receives the final `--json` envelope line; `onTerminated`
    /// fires with the exit code after the process exits.
    func startLogin(
        onStdoutLine: @escaping @MainActor (String) -> Void,
        onTerminated: @escaping @MainActor (Int32) -> Void
    ) throws -> SpawnedProcessHandle

    /// Runs `screencap logout` (best-effort; the CLI swallows the common
    /// "nothing stored" case and exits zero).
    func signOut() async throws

    /// Raw stdout of `screencap whoami --force-refresh --json` — re-mints the ID
    /// token first so a just-granted `subscribed` claim is visible immediately
    /// (billing U5/U9, post-checkout).
    func fetchWhoAmIForceRefresh() async throws -> Data

    /// Raw stdout of `screencap checkout-url --tier <local|cloud> --json` — a
    /// hosted Stripe Checkout URL for the chosen paid tier (billing U9 / paid-only
    /// U11). `tier` selects the Stripe PRICE only (`--tier local` → Local Pro,
    /// `--tier cloud` → Cloud); the webhook re-derives the entitlement from the
    /// paid price and is the sole authority (KTD-2), so the client-supplied tier
    /// can never over-grant. Token handling stays in Python.
    func fetchCheckoutURL(tier: String) async throws -> Data

    /// Raw stdout of `screencap reconcile-entitlement --json` — a GRANT-ONLY
    /// dropped-webhook self-heal (billing U14). If Stripe confirms an active
    /// subscription but the claim is missing, the server grants it, so a paying
    /// customer is never permanently stuck. The caller re-mints the token
    /// (force-refresh whoami) afterward to observe the grant locally.
    func fetchReconcileEntitlement() async throws -> Data

    /// Raw stdout of `screencap portal-url --json` — a hosted Stripe
    /// customer-portal URL for managing the subscription (account sheet U3).
    /// Requires sign-in and an active/trialing subscription; the failure
    /// envelope carries a machine-readable `code` the error seam keys on
    /// (KTD-5). Token handling stays in Python.
    func fetchPortalURL() async throws -> Data
}

@MainActor
final class LiveCloudAuthService: CloudAuthService {
    func fetchWhoAmI() async throws -> Data {
        try await CLIClient.runJSONRaw(["whoami", "--json"], timeout: 15)
    }

    func startLogin(
        onStdoutLine: @escaping @MainActor (String) -> Void,
        onTerminated: @escaping @MainActor (Int32) -> Void
    ) throws -> SpawnedProcessHandle {
        // `screencap login` blocks on the loopback callback and self-times-out
        // at LOGIN_TIMEOUT_SECONDS (180s, U4) so the waiting state can't hang
        // forever even without an explicit Cancel. `spawn` (not `runJSON`) is
        // used precisely so Cancel can SIGTERM the in-flight flow.
        try CLIClient.spawn(
            args: ["login", "--json"],
            onStderrLine: { _ in },
            onStdoutLine: { line in
                DispatchQueue.main.async { MainActor.assumeIsolated { onStdoutLine(line) } }
            },
            onTerminated: { code in
                DispatchQueue.main.async { MainActor.assumeIsolated { onTerminated(code) } }
            }
        )
    }

    func signOut() async throws {
        // `logout` has no --json mode; it always exits zero, so awaiting exit
        // (not decoding) is the right primitive.
        try await CLIClient.runAwaitingExit(["logout"], timeout: 15)
    }

    func fetchWhoAmIForceRefresh() async throws -> Data {
        try await CLIClient.runJSONRaw(["whoami", "--force-refresh", "--json"], timeout: 30)
    }

    func fetchCheckoutURL(tier: String) async throws -> Data {
        // `--tier` selects the Stripe price (U3); the webhook stays the
        // entitlement authority. Passed as an argument, not baked in, so a
        // price change is a config change, not a code change (KTD-7). The CLI's
        // `checkout-url --tier <local|cloud>` option is required and validated.
        //
        // `allowNonZeroExit`: on failure the CLI writes a `{ok:false, error}`
        // envelope to stdout and exits 1 with *empty* stderr. Without this, the
        // non-zero exit throws `nonZeroExit(stderr: "")` and `startCheckout`
        // surfaces a bare "screencap exited with code 1:"; tolerating it lets the
        // envelope through so the real reason (sign-in, offline, backend) shows.
        try await CLIClient.runJSONRaw(
            ["checkout-url", "--tier", tier, "--json"], timeout: 30, allowNonZeroExit: true
        )
    }

    func fetchReconcileEntitlement() async throws -> Data {
        try await CLIClient.runJSONRaw(["reconcile-entitlement", "--json"], timeout: 30)
    }

    func fetchPortalURL() async throws -> Data {
        // `allowNonZeroExit`: like `checkout-url`, on failure the CLI writes the
        // `{ok:false, error, code}` envelope to stdout and exits 1 with *empty*
        // stderr. Without this, the non-zero exit throws `nonZeroExit(stderr:
        // "")`, discarding the envelope — and with it the machine-readable
        // `code` the error seam maps, leaving only the static fallback.
        try await CLIClient.runJSONRaw(
            ["portal-url", "--json"], timeout: 30, allowNonZeroExit: true
        )
    }
}

/// Owns cloud sign-in state for the app shell (plan U6). Surfaces:
///   - `status`: the persistent signed-in/out fact (from `whoami`/`login`).
///   - `signInFlow`: the transient login-in-progress / failed flow state.
///
/// Upload bookkeeping (the active-upload count that gates Sign Out) lives in a
/// separate `UploadCoordinator`; this controller reads it through
/// `isUploadInFlight` so `canSignOut` can still derive from it without owning
/// the count.
///
/// Token handling lives entirely in the Python layer; this controller only
/// triggers `login`/`logout`/`whoami` and reads their JSON. Local recording is
/// never gated on any of this (R3).
@MainActor
final class CloudAuthController: ObservableObject {
    @Published private(set) var status: AuthStatus = .unknown
    @Published private(set) var signInFlow: SignInFlowState = .idle
    /// Cloud-paywall entitlement (billing U8). Display/UX only — the signer's
    /// hard gate is the real enforcement. Read from the `whoami` envelope's
    /// `subscribed` field; false when signed out or absent. Under the two-tier
    /// split it is the DERIVED cloud signal (`tier == .cloud`) — kept for
    /// compatibility with the existing upload / onboarding gates.
    @Published private(set) var isSubscribed: Bool = false
    /// Two-tier entitlement (paid-only launch, U11 / KTD-1). Resolved from the
    /// `whoami` envelope's open `tier` string; `.none` when signed out, absent,
    /// or offline-stale (the picker reads `status`'s `stale` to avoid a spurious
    /// "lapsed" — never `tier` presence alone, per KTD-4).
    @Published private(set) var tier: EntitlementTier = .none
    /// The trial → convert → lapse lifecycle position (U11), derived from `tier`,
    /// the envelope's `trial_end`, and whether `status` is stale. Drives the
    /// active / near-expiry / last-day / lapsed picker copy and the R12
    /// auto-conversion disclosure. `.indeterminate` while unresolved or stale.
    @Published private(set) var trialState: TrialState = .indeterminate
    /// Client paywall flag (billing KTD-6). When OFF, the app shows no pricing /
    /// soft gate / checkout — cloud onboarding behaves exactly as before billing.
    /// Read from the `whoami` envelope's `paywall_enabled`; false when absent.
    @Published private(set) var paywallEnabled: Bool = false
    /// Browser-return flag for an in-flight Stripe Checkout (account-sheet
    /// KTD-6, relocated from `OnboardingAccountStep`'s view-local copy —
    /// additive until U5 switches onboarding over). Set when `startCheckout`
    /// runs; drives the "unlocks automatically" pending rendering and gates the
    /// app-activation entitlement refresh. Cleared when the entitlement resolves
    /// to `checkoutTargetTier` with no live trial, on mint failure, and on
    /// sign-out/account switch.
    @Published private(set) var checkoutPending = false
    /// The paid tier the pending checkout targets (KTD-6). Remembered so the
    /// webhook-lag window can be told apart from "resolved": only an envelope
    /// that reports THIS tier (and no live `trial_end`) settles the flag.
    /// Re-tapping a tier replaces it — an abandoned Stripe tab stays
    /// recoverable in place. Nil whenever `checkoutPending` is false.
    @Published private(set) var checkoutTargetTier: EntitlementTier?
    /// Browser-return flag for an opened customer portal (KTD-6 / R7). Unlike
    /// checkout there is no target state to converge on (the user may have
    /// cancelled, downgraded, or done nothing), so this clears after the single
    /// post-return refresh; the manual reconcile affordance remains the recourse
    /// if that refresh lost the race with the webhook.
    @Published private(set) var portalReturnPending = false
    /// In-flight signal for the "I've paid — check now" reconcile round-trip so
    /// the sheet (U4) can render a spinner instead of a dead-feeling button.
    @Published private(set) var isReconciling = false
    /// The error seam's rendered output (KTD-5 / R10): the last account/billing
    /// failure, already mapped to app-native static copy + a recovery action.
    /// Views render THIS — never `env.error` and never a raw
    /// `localizedDescription`. Set by portal/checkout mint failures, the
    /// https open-guard, and sign-in failures; cleared when a new attempt
    /// starts, on `clearAccountError()`, and on sign-out.
    @Published private(set) var accountError: AccountErrorCopy?

    private let service: CloudAuthService
    /// Read-only window onto the upload count that gates Sign Out. Owned by the
    /// app-wide `UploadCoordinator` (a separate observable) so the auth
    /// controller stays focused on sign-in flow + status.
    private let isUploadInFlight: () -> Bool
    private var loginHandle: SpawnedProcessHandle?
    private var loginStdout: [String] = []
    /// Swift-side watchdog for an in-flight `login`. `CLIClient.spawn` has no
    /// timeout, so a `login` that hangs before the CLI's own 180s timer
    /// (U4 `LOGIN_TIMEOUT_SECONDS`) would otherwise leave `signInFlow` stuck
    /// `.inProgress`. Fires just past 180s and is generation-guarded so a stale
    /// watchdog can't touch a live attempt. Cancelled when the flow settles.
    private var loginWatchdog: Task<Void, Never>?
    /// Monotonic per-attempt token. Each `startSignIn` bumps it and the spawn
    /// callbacks capture the value; a cancelled or superseded flow bumps it
    /// again, so a stale `onTerminated`/`onStdoutLine` from the old process is
    /// ignored rather than corrupting the live attempt (e.g. clobbering the new
    /// `loginHandle` or flipping the new flow to `.failed`).
    private var loginGeneration = 0
    /// Completion for the in-flight attempt (the Upload gate's "proceed once
    /// signed in"). Fired exactly once per attempt — on success, failure, or
    /// cancel — then cleared.
    private var pendingResult: ((Bool) -> Void)?
    /// Coalesces concurrent `refreshIfNeeded()` callers onto a single `whoami`
    /// shell-out. Two cloud surfaces appearing at once (e.g. the menu-bar
    /// account section while the cloud-onboarding step is on screen) must not
    /// each decrypt the Keychain — a second decrypt would risk a second
    /// authorization prompt (SCR-241). Held only for the duration of one
    /// lazy refresh; nil the rest of the time.
    private var pendingLazyRefresh: Task<Void, Never>?

    /// Watchdog deadline: just past the CLI's own 180s `login` self-timeout so
    /// the Swift side only fires when the subprocess has genuinely hung before
    /// its own timer (rather than racing it).
    private static let loginWatchdogSeconds: TimeInterval = 210

    /// Browser-open seam for the billing URLs (checkout + portal). Injected so
    /// tests can observe what would open without launching a real browser; the
    /// default routes to `NSWorkspace`. Only ever invoked through
    /// `openBillingURL`, which enforces the https guard first.
    private let openURL: @MainActor (URL) -> Void
    /// Injected so tests can post `didBecomeActiveNotification` on a private
    /// center without touching the process-global one (other observers in the
    /// test host must not react to a synthetic activation).
    private let notificationCenter: NotificationCenter
    /// Token for the app-activation subscription; removed in `deinit`.
    private var activationObserver: NSObjectProtocol?

    /// - Parameter isUploadInFlight: reads the app-wide `UploadCoordinator`'s
    ///   in-flight flag so `canSignOut` can gate on uploads without this
    ///   controller owning the count. Defaults to "no upload" for tests /
    ///   surfaces that don't wire a coordinator.
    init(
        service: CloudAuthService = LiveCloudAuthService(),
        isUploadInFlight: @escaping () -> Bool = { false },
        notificationCenter: NotificationCenter = .default,
        openURL: @escaping @MainActor (URL) -> Void = { _ = NSWorkspace.shared.open($0) }
    ) {
        self.service = service
        self.isUploadInFlight = isUploadInFlight
        self.notificationCenter = notificationCenter
        self.openURL = openURL
        // Browser-return hook (account-sheet KTD-6): re-check entitlement when
        // the app comes back to the front after a Stripe Checkout / portal
        // visit. Subscribing is free — `refreshAfterBrowserReturnIfPending` is
        // gated pending-only, so an activation with nothing pending does ZERO
        // auth work (no `whoami` shell-out, no Keychain decrypt). That keeps
        // the `testInitialStateDoesNoAuthWork` launch-path invariant intact and
        // avoids shelling out `whoami --force-refresh` on every app switch.
        activationObserver = notificationCenter.addObserver(
            forName: NSApplication.didBecomeActiveNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                await self?.refreshAfterBrowserReturnIfPending()
            }
        }
    }

    deinit {
        if let activationObserver {
            notificationCenter.removeObserver(activationObserver)
        }
    }

    var isSignedIn: Bool { status.isSignedIn }

    /// Sign Out is offered only when signed in AND no upload is in flight.
    var canSignOut: Bool { status.isSignedIn && !isUploadInFlight() }

    /// Whether the paid-only local paywall should gate the record + recall
    /// affordances for this user (U12). True only for a definitively lapsed /
    /// not-entitled account while the paywall is enabled — the same state the
    /// daemon's KTD-4 lease blocks on. Deliberately keyed on `trialState ==
    /// .lapsed`, NOT on `tier == .none` alone: `TrialState.from` resolves an
    /// offline-stale token to `.indeterminate` (never `.lapsed`), so an offline
    /// payer within the daemon's lease window is never gated here (KTD-4 grace,
    /// plan point 4). An active trial (`.active`/`.nearExpiry`/`.lastDay`) and a
    /// converted subscriber (`.subscribed`) both read as entitled → not gated.
    ///
    /// UX-only, like `isSubscribed`: the daemon's soft lease gate is the real
    /// enforcement and returns 402 `subscription_required` regardless. This just
    /// lets the affordances render the gated appearance + upgrade prompt up front
    /// instead of only after a round-trip. When `paywallEnabled` is off the whole
    /// feature is dark, so this is always false (pre-billing behavior).
    var isGatedForLapse: Bool {
        paywallEnabled && trialState == .lapsed
    }

    // MARK: - whoami refresh

    /// Refreshes `status` from `screencap whoami --json`. Any failure (launch
    /// error, non-zero exit, timeout, garbage output) resolves to `.signedOut`
    /// rather than surfacing — sign-in state is not alarming enough to crash a
    /// surface over, and `whoami` is built not to raise in normal operation.
    func refresh() async {
        do {
            let data = try await service.fetchWhoAmI()
            let envelope = AuthWhoAmIEnvelope.parse(data)
            warnOnSchemaDrift(envelope)
            status = AuthStatus.from(envelope: envelope)
            applyEntitlement(from: envelope, status: status)
            paywallEnabled = envelope?.paywallEnabled ?? false
        } catch {
            authLogger.debug("whoami refresh failed: \(error.localizedDescription, privacy: .public); treating as signed out")
            status = .signedOut
            clearEntitlement()
            paywallEnabled = false
        }
    }

    /// Recompute the entitlement surfaces (`isSubscribed`, `tier`, `trialState`)
    /// from a decoded envelope and the resolved `AuthStatus`. Single seam so the
    /// three read paths (`refresh`, `refreshEntitlement`, post-`login`) stay in
    /// sync. `isSubscribed` stays the derived cloud signal (`tier == .cloud`),
    /// falling back to the envelope's own `subscribed` for compatibility. The
    /// `stale` bit is sourced from the resolved status so an offline payer's
    /// trial state is `.indeterminate` (KTD-4 grace), never a spurious "lapsed".
    private func applyEntitlement(from envelope: AuthWhoAmIEnvelope?, status: AuthStatus) {
        let resolvedTier = EntitlementTier.from(claim: envelope?.tier)
        tier = resolvedTier
        // Prefer the derived signal; fall back to the raw claim so an older CLI
        // that sends `subscribed` without `tier` still reads as subscribed.
        isSubscribed = resolvedTier == .cloud || (envelope?.subscribed ?? false)
        let isStale: Bool
        if case .signedIn(_, _, let stale) = status { isStale = stale } else { isStale = false }
        let previous = trialState
        trialState = TrialState.from(
            tier: resolvedTier,
            trialEnd: envelope?.trialEnd,
            stale: isStale
        )
        announceTrialTransitionIfNeeded(from: previous, to: trialState)
        settleCheckoutPendingIfResolved()
    }

    /// Clear checkout-pending once the entitlement has actually converged on
    /// the remembered target (account-sheet KTD-6): the tier matches AND the
    /// account is no longer trialing (`trial_end` absent → `.subscribed`).
    /// Tier-match alone is NOT enough — during a same-tier trial the tier
    /// matches before any payment lands, and clearing then would drop the
    /// "unlocks automatically" reassurance mid-checkout. Runs on every
    /// entitlement recompute (refresh, force-refresh, post-login) so the
    /// webhook's grant settles the flag no matter which read path observes it.
    private func settleCheckoutPendingIfResolved() {
        guard checkoutPending, let target = checkoutTargetTier else { return }
        if tier == target, trialState == .subscribed {
            checkoutPending = false
            checkoutTargetTier = nil
        }
    }

    /// Reset entitlement state to the signed-out / unresolved baseline.
    private func clearEntitlement() {
        isSubscribed = false
        tier = .none
        trialState = .indeterminate
    }

    /// Lazily resolves sign-in state the first time a cloud surface actually
    /// needs it, instead of eagerly at app launch.
    ///
    /// `whoami` decrypts the Keychain refresh token, and on macOS a decrypt that
    /// the reading binary's ACL doesn't silently authorize raises the "ScreenCap
    /// wants to use confidential information stored in screencap-auth" prompt.
    /// Probing at launch (a scene `.task`) made that prompt fire *the moment the
    /// app opened*, before the user touched anything cloud-related. Cloud
    /// surfaces (the menu-bar account section, the Upload gate, cloud onboarding)
    /// call this on appear so the decrypt — and any prompt — only happens on a
    /// genuine cloud interaction. The durable storage-layer fix that stops the
    /// prompt entirely is tracked in SCR-241.
    ///
    /// Coalesced two ways: it no-ops once `status` has resolved away from
    /// `.unknown` (so a signed-in session decrypts at most once), and concurrent
    /// callers share one in-flight refresh via `pendingLazyRefresh` (so two
    /// surfaces appearing together can't trigger two decrypts / two prompts).
    func refreshIfNeeded() async {
        guard status == .unknown else { return }
        if let pendingLazyRefresh {
            await pendingLazyRefresh.value
            return
        }
        let task = Task { await self.refresh() }
        pendingLazyRefresh = task
        await task.value
        pendingLazyRefresh = nil
    }

    // MARK: - Entitlement + checkout (billing U8/U9)

    /// Force-refresh the ID token, then re-read `whoami`, so a just-completed
    /// checkout's `subscribed` claim is reflected without a re-login (U9). Leaves
    /// status untouched on failure (offline) rather than flipping signed-out.
    func refreshEntitlement() async {
        do {
            let data = try await service.fetchWhoAmIForceRefresh()
            let envelope = AuthWhoAmIEnvelope.parse(data)
            warnOnSchemaDrift(envelope)
            if envelope?.signedIn == true {
                status = AuthStatus.from(envelope: envelope)
                // Only recompute the tier/trial from a positively signed-in
                // envelope — an offline force-refresh must not clobber a cached
                // positive entitlement to `.none` (KTD-4).
                applyEntitlement(from: envelope, status: status)
            }
            paywallEnabled = envelope?.paywallEnabled ?? paywallEnabled
        } catch {
            authLogger.debug("entitlement refresh failed: \(error.localizedDescription, privacy: .public)")
        }
    }

    /// GRANT-ONLY dropped-webhook self-heal, then re-read entitlement (billing
    /// U14). The "I've paid — check now" action: `reconcile-entitlement` asks the
    /// server to grant `subscribed` if Stripe confirms an active subscription
    /// (repairing a dropped checkout webhook), then `refreshEntitlement` re-mints
    /// the ID token so the just-granted claim is observed locally. Reconcile
    /// failure is non-fatal — we still force-refresh in case the webhook already
    /// landed.
    func reconcileEntitlement() async {
        // Published in-flight signal (U3): the sheet's "check now" affordance
        // renders a spinner off this instead of feeling dead for two shell-outs.
        isReconciling = true
        defer { isReconciling = false }
        do {
            _ = try await service.fetchReconcileEntitlement()
        } catch {
            authLogger.debug("reconcile failed: \(error.localizedDescription, privacy: .public); refreshing anyway")
        }
        await refreshEntitlement()
    }

    /// The `didBecomeActiveNotification` hook body (account-sheet KTD-6 / R7):
    /// returning from the Stripe Checkout / portal browser tab re-checks the
    /// entitlement so a plan change lands without a manual action. Gated
    /// pending-only — an activation with nothing pending does zero auth work
    /// (an "always" gate would shell out `whoami --force-refresh` on every app
    /// switch). Checkout-pending is NOT cleared here; it settles only when the
    /// entitlement converges on the target (`settleCheckoutPendingIfResolved`).
    private func refreshAfterBrowserReturnIfPending() async {
        guard checkoutPending || portalReturnPending else { return }
        await refreshEntitlement()
        // Portal-pending clears after this one post-return refresh: there is no
        // target state to converge on, and the reconcile affordance remains the
        // user's recourse if this refresh lost the race with the webhook.
        portalReturnPending = false
    }

    /// Opens hosted Stripe Checkout for the chosen paid `tier` in the browser
    /// (U9 / paid-only U11). The tier selects the Stripe PRICE only — the webhook
    /// re-derives the entitlement from the paid price and is the sole authority
    /// (KTD-2), so this never over-grants. The URL is minted by `checkout-url`
    /// (token stays in Python).
    ///
    /// Sets the browser-return pending state up front (KTD-6): `checkoutPending`
    /// + the remembered `checkoutTargetTier`. A re-tap simply replaces the
    /// target and re-runs checkout, so an abandoned Stripe tab is recoverable in
    /// place. Mint failure clears both and routes through the error seam —
    /// `onFailure` receives the MAPPED static copy (never `env.error`, never
    /// `localizedDescription`; R10) and `accountError` publishes the full
    /// `AccountErrorCopy` for seam consumers.
    func startCheckout(
        tier: EntitlementTier,
        onFailure: @escaping @MainActor (String) -> Void = { _ in }
    ) {
        guard let checkoutTier = tier.checkoutTier else {
            // App-native static copy (no plan chosen) — not an envelope error,
            // so it doesn't ride the seam or touch the pending machinery.
            onFailure("Pick a plan to continue.")
            return
        }
        accountError = nil
        checkoutPending = true
        checkoutTargetTier = tier
        Task { [weak self] in
            guard let self else { return }
            do {
                let data = try await self.service.fetchCheckoutURL(tier: checkoutTier)
                let env = CheckoutURLEnvelope.parse(data)
                if env?.ok != false, self.openBillingURL(env?.url) {
                    return // Opened; pending settles when the entitlement resolves.
                }
                self.failCheckout(with: AccountErrorCopy.from(code: env?.code), onFailure: onFailure)
            } catch {
                self.failCheckout(with: AccountErrorCopy.from(error: error), onFailure: onFailure)
            }
        }
    }

    /// Mint-failure path for `startCheckout`: clear the pending machinery (the
    /// browser never opened, so there is no return to wait on) and surface the
    /// mapped copy through both seam outputs.
    private func failCheckout(
        with copy: AccountErrorCopy,
        onFailure: @MainActor (String) -> Void
    ) {
        checkoutPending = false
        checkoutTargetTier = nil
        accountError = copy
        onFailure(copy.message)
    }

    /// Opens the hosted Stripe customer portal in the browser (account sheet
    /// U3 / R7) — `startCheckout(tier:)` minus the tier. The URL is minted by
    /// `portal-url` (token stays in Python), treated as short-lived and
    /// single-use: fetched on tap, opened immediately, never cached — and never
    /// written to os_log/print (it grants access to the user's billing page).
    /// On a successful open, `portalReturnPending` arms the app-activation
    /// refresh (KTD-6 — without it the portal return never re-checks the
    /// entitlement and R7 silently fails). Failures land on the error seam:
    /// `accountError` + `onFailure` carry the mapped static copy, keyed on the
    /// envelope's `code`.
    func startManageSubscription(
        onFailure: @escaping @MainActor (AccountErrorCopy) -> Void = { _ in }
    ) {
        accountError = nil
        Task { [weak self] in
            guard let self else { return }
            do {
                let data = try await self.service.fetchPortalURL()
                let env = PortalURLEnvelope.parse(data)
                self.warnOnSchemaDrift(version: env?.schemaVersion)
                if env?.ok != false, self.openBillingURL(env?.url) {
                    self.portalReturnPending = true
                    return
                }
                self.failPortal(with: AccountErrorCopy.from(code: env?.code), onFailure: onFailure)
            } catch {
                self.failPortal(with: AccountErrorCopy.from(error: error), onFailure: onFailure)
            }
        }
    }

    /// Failure path for `startManageSubscription`: surface the mapped copy
    /// through both seam outputs. No pending state to clear — the portal flag
    /// is only armed after a successful open.
    private func failPortal(
        with copy: AccountErrorCopy,
        onFailure: @MainActor (AccountErrorCopy) -> Void
    ) {
        accountError = copy
        onFailure(copy)
    }

    /// The ONE shared browser-open path for billing URLs (checkout + portal).
    /// Validates https before handing anything to the opener: a `file://` or
    /// custom-scheme URL from a compromised/buggy envelope could launch an
    /// arbitrary local app via `NSWorkspace`, so anything non-https returns
    /// false (→ the caller routes to the error seam) and is never opened.
    /// Returns true only when the URL was actually handed to the opener.
    private func openBillingURL(_ urlString: String?) -> Bool {
        guard let urlString,
              let url = URL(string: urlString),
              url.scheme?.lowercased() == "https" else {
            return false
        }
        openURL(url)
        return true
    }

    /// Dismiss the surfaced account error (the sheet's inline dismiss path).
    func clearAccountError() {
        accountError = nil
    }

    /// Post a VoiceOver announcement when the trial lifecycle crosses into a more
    /// urgent (or lapsed) state, so a non-sighted user hears "2 days left in your
    /// trial" rather than only seeing an escalated banner (U11 accessibility;
    /// mirrors `HUDHintPanel`'s `announcementRequested` post and the
    /// `SearchViewModel` terminal-state announcement discipline — announce once
    /// on a real transition, never on every refresh tick).
    private func announceTrialTransitionIfNeeded(from previous: TrialState, to next: TrialState) {
        guard previous != next, let message = Self.trialAnnouncement(for: next) else { return }
        NSAccessibility.post(
            element: NSApp,
            notification: .announcementRequested,
            userInfo: [
                .announcement: message,
                .priority: NSAccessibilityPriorityLevel.high.rawValue,
            ]
        )
    }

    /// The spoken announcement for a trial state, or nil for states that need no
    /// spoken escalation (`.indeterminate` / `.subscribed` are calm).
    private static func trialAnnouncement(for state: TrialState) -> String? {
        switch state {
        case .active(let days):
            return "\(days) days left in your free trial."
        case .nearExpiry(let days):
            return "Your free trial ends in \(days) day\(days == 1 ? "" : "s"). It converts to a paid subscription unless you cancel."
        case .lastDay(let hours):
            return hours <= 1
                ? "Your free trial ends within the hour. It converts to a paid subscription unless you cancel."
                : "Your free trial ends in \(hours) hours. It converts to a paid subscription unless you cancel."
        case .lapsed:
            return "Your subscription has lapsed. Recording and search are paused; your recordings stay on this Mac."
        case .indeterminate, .subscribed:
            return nil
        }
    }

    // MARK: - Sign in

    /// Starts the browser sign-in flow. Non-blocking: returns immediately and
    /// publishes `.inProgress`; the result lands via `signInFlow` + `status`
    /// when `login` exits. `onResult(true)` fires once signed in, so a caller
    /// (the Upload gate) can proceed afterward. A second call while one is
    /// already in flight does not launch a second browser flow.
    func startSignIn(onResult: ((Bool) -> Void)? = nil) {
        if status.isSignedIn {
            // Already signed in — possibly on another surface that completed the
            // OAuth round-trip first. Don't open a redundant browser flow; this
            // caller's gate is already satisfied, so report success immediately.
            onResult?(true)
            return
        }
        if case .inProgress = signInFlow {
            // A sign-in is already running (possibly from another surface). Don't
            // open a second browser flow; tell this caller "not now" rather than
            // hijacking the in-flight attempt's completion.
            onResult?(false)
            return
        }
        signInFlow = .inProgress
        accountError = nil
        loginStdout = []
        pendingResult = onResult
        loginGeneration &+= 1
        let generation = loginGeneration
        do {
            loginHandle = try service.startLogin(
                onStdoutLine: { [weak self] line in self?.appendLoginStdout(line, generation: generation) },
                onTerminated: { [weak self] code in self?.handleLoginTerminated(exitCode: code, generation: generation) }
            )
            startLoginWatchdog(generation: generation)
        } catch {
            // Spawn failure (binary not found, launch error) — terminal failure,
            // no process to clean up. `signInFlow` keeps the short reason for
            // the legacy surfaces (until U5); the seam output carries only the
            // mapped static copy (KTD-5).
            loginHandle = nil
            signInFlow = .failed(error.localizedDescription)
            accountError = AccountErrorCopy.from(error: error)
            finishResult(false)
        }
    }

    /// Arms the Swift-side login watchdog for `generation`. On fire (the CLI's
    /// own 180s timer didn't), terminates the handle and surfaces a timeout —
    /// guarded by the generation token so a stale watchdog from a cancelled or
    /// superseded attempt cannot touch a live flow.
    private func startLoginWatchdog(generation: Int) {
        loginWatchdog?.cancel()
        loginWatchdog = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(Self.loginWatchdogSeconds * 1_000_000_000))
            guard !Task.isCancelled else { return }
            self?.handleLoginTimedOut(generation: generation)
        }
    }

    /// Cancels the active watchdog (the flow has settled, or is being torn down).
    private func cancelLoginWatchdog() {
        loginWatchdog?.cancel()
        loginWatchdog = nil
    }

    private func handleLoginTimedOut(generation: Int) {
        // A stale watchdog (cancelled/superseded attempt) must not touch the
        // live flow — mirror the generation guard the spawn callbacks use.
        guard generation == loginGeneration else { return }
        guard case .inProgress = signInFlow else { return }
        // Bump the generation so the SIGTERM-driven exit that follows is treated
        // as stale and can't flip us off the `.failed` state we set here.
        loginGeneration &+= 1
        loginWatchdog = nil
        loginHandle?.terminate()
        loginHandle = nil
        signInFlow = .failed("Sign-in timed out.")
        // Seam output (KTD-5): a hung login reads as network trouble — retry.
        accountError = .network
        finishResult(false)
    }

    /// Dismiss the sign-in flow: cancel an in-flight `login` (SIGTERM) or clear a
    /// lingering `.failed`, returning to `.idle`. Bumping the generation
    /// invalidates the terminated process's pending callbacks so a later
    /// re-`startSignIn` cannot be clobbered by the cancelled flow's exit. Safe
    /// to call when idle.
    func cancelSignIn() {
        switch signInFlow {
        case .inProgress:
            loginGeneration &+= 1
            cancelLoginWatchdog()
            loginHandle?.terminate()
            loginHandle = nil
            signInFlow = .idle
            finishResult(false)
        case .failed:
            signInFlow = .idle
            // Dismissing a failed flow also clears the seam's copy of it —
            // "never mind" must not leave stale error copy on the sheet.
            accountError = nil
            finishResult(false)
        case .idle:
            break
        }
    }

    private func handleLoginTerminated(exitCode: Int32, generation: Int) {
        // Ignore a stale termination from a cancelled or superseded attempt —
        // its callbacks must not touch the live flow's handle or state.
        guard generation == loginGeneration else { return }
        cancelLoginWatchdog()
        loginHandle = nil
        // `login --json` success carries the same envelope shape as `whoami`
        // (`{ok, signed_in: true, uid, email}`), so the signed-in discriminator
        // lives in one place (AuthStatus.from) and no second round-trip is
        // needed to set status.
        let envelope = latestLoginEnvelope()
        warnOnSchemaDrift(envelope)
        let resolved = AuthStatus.from(envelope: envelope)
        if exitCode == 0, resolved.isSignedIn {
            status = resolved
            applyEntitlement(from: envelope, status: resolved)
            paywallEnabled = envelope?.paywallEnabled ?? paywallEnabled
            signInFlow = .idle
            finishResult(true)
            return
        }
        let reason = envelope?.error.flatMap { $0.isEmpty ? nil : $0 }
            ?? "Sign-in failed or was cancelled."
        signInFlow = .failed(reason)
        // Seam output (KTD-5): the login envelope carries no machine-readable
        // `code`, so the mapped copy is the static fallback by construction —
        // the dynamic `reason` text above stays on the legacy `signInFlow`
        // surface (until U5) and never rides `accountError`.
        accountError = .unknown
        finishResult(false)
    }

    /// Fire the in-flight attempt's completion exactly once, then clear it.
    private func finishResult(_ success: Bool) {
        let result = pendingResult
        pendingResult = nil
        result?(success)
    }

    private func appendLoginStdout(_ line: String, generation: Int) {
        guard generation == loginGeneration else { return }
        loginStdout.append(line)
    }

    /// The most recent parseable `--json` envelope from the login subprocess's
    /// stdout. `login --json` emits exactly one line, but we scan from the end
    /// defensively in case a stray line precedes it; `parse` rejects non-JSON
    /// and empty lines, so no inline pre-filtering is needed.
    private func latestLoginEnvelope() -> AuthWhoAmIEnvelope? {
        loginStdout.reversed().lazy.compactMap { AuthWhoAmIEnvelope.parse(Data($0.utf8)) }.first
    }

    /// Logs (does not reject) an auth-envelope `schema_version` that doesn't
    /// match the Swift side — mirrors `UploadController` / `DaemonClient` so a
    /// Python-side bump is visible rather than silently ignored.
    private func warnOnSchemaDrift(_ envelope: AuthWhoAmIEnvelope?) {
        warnOnSchemaDrift(version: envelope?.schemaVersion)
    }

    /// Version-keyed variant shared with the portal envelope (which is not an
    /// `AuthWhoAmIEnvelope`) so every auth-adjacent envelope drifts loudly.
    private func warnOnSchemaDrift(version: Int?) {
        if let version, version != SUPPORTED_API_SCHEMA_VERSION {
            authLogger.warning("Auth envelope schema_version=\(version, privacy: .public) does not match SwiftUI side (\(SUPPORTED_API_SCHEMA_VERSION, privacy: .public)). Processing anyway.")
        }
    }

    // MARK: - Sign out

    /// Signs out via `screencap logout` and clears local state. No-op while an
    /// upload is in flight (defensive — the menu also disables the control).
    func signOut() async {
        guard !isUploadInFlight() else { return }
        do {
            try await service.signOut()
        } catch {
            // `logout` exits zero in normal operation; reaching here is a launch
            // failure. Clear local state anyway — the next `whoami` is the
            // source of truth, and leaving the user "signed in" after they asked
            // to sign out is the worse failure.
            authLogger.warning("logout shell-out failed: \(error.localizedDescription, privacy: .public)")
        }
        status = .signedOut
        clearEntitlement()
        // Sign-out / account switch invalidates the browser-return machinery
        // (account-sheet KTD-6): the remembered target and both pending flags
        // belong to the account that started them — a different account later
        // resolving the same tier must not settle the old attempt. Stale error
        // copy goes with them.
        checkoutPending = false
        checkoutTargetTier = nil
        portalReturnPending = false
        accountError = nil
    }
}
