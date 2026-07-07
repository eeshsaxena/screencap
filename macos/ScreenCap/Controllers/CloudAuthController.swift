import AppKit
import Combine
import Foundation
import OSLog

private let authLogger = Logger(subsystem: "com.screencap.macos", category: "cloud-auth")

/// Decoded `screencap checkout-url --json` envelope (billing U9): `{ok, url}` on
/// success or `{ok:false, error}`. Tolerant parse like `AuthWhoAmIEnvelope`.
private struct CheckoutURLEnvelope: Decodable {
    let ok: Bool?
    let url: String?
    let error: String?

    static func parse(_ data: Data) -> CheckoutURLEnvelope? {
        guard !data.isEmpty else { return nil }
        return try? JSONDecoder().decode(CheckoutURLEnvelope.self, from: data)
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

    /// Raw stdout of `screencap checkout-url --json` — a hosted Stripe Checkout
    /// URL for the $5/mo plan (billing U9). Token handling stays in Python.
    func fetchCheckoutURL() async throws -> Data
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

    func fetchCheckoutURL() async throws -> Data {
        try await CLIClient.runJSONRaw(["checkout-url", "--json"], timeout: 30)
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
    /// `subscribed` field; false when signed out or absent.
    @Published private(set) var isSubscribed: Bool = false

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

    /// Watchdog deadline: just past the CLI's own 180s `login` self-timeout so
    /// the Swift side only fires when the subprocess has genuinely hung before
    /// its own timer (rather than racing it).
    private static let loginWatchdogSeconds: TimeInterval = 210

    /// - Parameter isUploadInFlight: reads the app-wide `UploadCoordinator`'s
    ///   in-flight flag so `canSignOut` can gate on uploads without this
    ///   controller owning the count. Defaults to "no upload" for tests /
    ///   surfaces that don't wire a coordinator.
    init(
        service: CloudAuthService = LiveCloudAuthService(),
        isUploadInFlight: @escaping () -> Bool = { false }
    ) {
        self.service = service
        self.isUploadInFlight = isUploadInFlight
    }

    var isSignedIn: Bool { status.isSignedIn }

    /// Sign Out is offered only when signed in AND no upload is in flight.
    var canSignOut: Bool { status.isSignedIn && !isUploadInFlight() }

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
            isSubscribed = envelope?.subscribed ?? false
        } catch {
            authLogger.debug("whoami refresh failed: \(error.localizedDescription, privacy: .public); treating as signed out")
            status = .signedOut
            isSubscribed = false
        }
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
            }
            isSubscribed = envelope?.subscribed ?? isSubscribed
        } catch {
            authLogger.debug("entitlement refresh failed: \(error.localizedDescription, privacy: .public)")
        }
    }

    /// Opens hosted Stripe Checkout for the $5/mo plan in the browser (U9). The
    /// URL is minted by `checkout-url` (token stays in Python); `onFailure`
    /// carries a short reason for the UI when the mint fails.
    func startCheckout(onFailure: @escaping @MainActor (String) -> Void = { _ in }) {
        Task { [weak self] in
            guard let self else { return }
            do {
                let data = try await self.service.fetchCheckoutURL()
                let env = CheckoutURLEnvelope.parse(data)
                guard env?.ok != false, let urlString = env?.url,
                      let url = URL(string: urlString) else {
                    onFailure(env?.error ?? "Couldn't start checkout.")
                    return
                }
                NSWorkspace.shared.open(url)
            } catch {
                onFailure(error.localizedDescription)
            }
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
            // no process to clean up.
            loginHandle = nil
            signInFlow = .failed(error.localizedDescription)
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
            isSubscribed = envelope?.subscribed ?? false
            signInFlow = .idle
            finishResult(true)
            return
        }
        let reason = envelope?.error.flatMap { $0.isEmpty ? nil : $0 }
            ?? "Sign-in failed or was cancelled."
        signInFlow = .failed(reason)
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
        if let version = envelope?.schemaVersion, version != SUPPORTED_API_SCHEMA_VERSION {
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
    }
}
