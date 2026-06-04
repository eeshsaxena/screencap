import Combine
import Foundation
import OSLog

private let authLogger = Logger(subsystem: "com.screencap.macos", category: "cloud-auth")

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
}

/// Owns cloud sign-in state for the app shell (plan U6). Surfaces:
///   - `status`: the persistent signed-in/out fact (from `whoami`/`login`).
///   - `signInFlow`: the transient login-in-progress / failed flow state.
///   - `activeUploadCount`: how many review windows are mid-upload, so Sign Out
///     can be disabled while an `request_signed_urls` is in flight (design
///     review state c — avoids a `NotSignedIn` mid-upload).
///
/// Token handling lives entirely in the Python layer; this controller only
/// triggers `login`/`logout`/`whoami` and reads their JSON. Local recording is
/// never gated on any of this (R3).
@MainActor
final class CloudAuthController: ObservableObject {
    @Published private(set) var status: AuthStatus = .unknown
    @Published private(set) var signInFlow: SignInFlowState = .idle
    @Published private(set) var activeUploadCount: Int = 0

    private let service: CloudAuthService
    private var loginHandle: SpawnedProcessHandle?
    private var loginStdout: [String] = []
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

    init(service: CloudAuthService = LiveCloudAuthService()) {
        self.service = service
    }

    var isSignedIn: Bool { status.isSignedIn }

    /// Sign Out is offered only when signed in AND no upload is in flight.
    var canSignOut: Bool { status.isSignedIn && activeUploadCount == 0 }

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
        } catch {
            authLogger.debug("whoami refresh failed: \(error.localizedDescription, privacy: .public); treating as signed out")
            status = .signedOut
        }
    }

    // MARK: - Sign in

    /// Starts the browser sign-in flow. Non-blocking: returns immediately and
    /// publishes `.inProgress`; the result lands via `signInFlow` + `status`
    /// when `login` exits. `onResult(true)` fires once signed in, so a caller
    /// (the Upload gate) can proceed afterward. A second call while one is
    /// already in flight does not launch a second browser flow.
    func startSignIn(onResult: ((Bool) -> Void)? = nil) {
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
        } catch {
            // Spawn failure (binary not found, launch error) — terminal failure,
            // no process to clean up.
            loginHandle = nil
            signInFlow = .failed(error.localizedDescription)
            finishResult(false)
        }
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
        guard activeUploadCount == 0 else { return }
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

    // MARK: - Upload coordination

    /// Called by a review window when its upload enters flight, so Sign Out is
    /// disabled until it finishes. Balanced by `uploadDidFinish()`.
    func uploadDidStart() { activeUploadCount += 1 }
    func uploadDidFinish() { activeUploadCount = max(0, activeUploadCount - 1) }
}
