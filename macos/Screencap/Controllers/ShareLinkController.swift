import AppKit
import Combine
import Foundation

// MARK: - Pure policy (SCR-299 U3)
//
// Everything in this section is a pure function of its inputs so the rules the
// Inspect menu renders from are unit-testable without a SwiftUI render or a
// live daemon — the same extraction `InspectShareAffordance` and
// `AccountErrorCopy` already use.

/// Whether this Mac holds a usable share link for a recording, and the token
/// needed to revoke it.
///
/// Scoped to **this Mac** by construction: the daemon's share record is a local
/// file and there is no cross-Mac share index (KTD4), so a link minted
/// elsewhere resolves to `.none` here and cannot be revoked from this app. The
/// rendered copy must not imply otherwise.
enum ShareLinkState: Equatable {
    case none
    case active(token: String)
}

enum ShareLinkPolicy {
    /// Resolve the share state for `recording` from this Mac's share records.
    ///
    /// A record counts as active when it is neither revoked nor past its
    /// expiry. When several records exist for one recording (each create mints
    /// a fresh token), the first still-active one wins — revoking any live
    /// token is better than offering none.
    static func state(
        for recording: String,
        in records: [ShareRecord],
        now: Date = Date()
    ) -> ShareLinkState {
        for record in records
        where record.recording == recording
            && record.revoked != true
            && !isExpired(record.expiresAt, now: now)
        {
            return .active(token: record.token)
        }
        return .none
    }

    /// Whether an `expires_at` stamp has passed.
    ///
    /// **Fails open** — an absent or unparseable stamp reads as NOT expired.
    /// This is deliberately the opposite of the server's fail-closed reading of
    /// the same field, because the consequences are opposite: the server
    /// refusing to serve a corrupt record is safe, but the app hiding Revoke
    /// would strand a user unable to stop sharing a link that is still live.
    /// The server remains the authority on whether a link actually resolves;
    /// this only decides whether to offer the button.
    static func isExpired(_ expiresAt: String?, now: Date) -> Bool {
        guard let expiresAt, let expiry = parseTimestamp(expiresAt) else { return false }
        return expiry <= now
    }

    /// Parse the daemon's ISO-8601 `expires_at`. It originates from Python's
    /// `datetime.isoformat()`, which includes fractional seconds only when
    /// nonzero, so both shapes have to be accepted.
    private static func parseTimestamp(_ raw: String) -> Date? {
        let withFraction = ISO8601DateFormatter()
        withFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let parsed = withFraction.date(from: raw) { return parsed }

        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        return plain.date(from: raw)
    }
}

/// The single mapping seam for share failures (SCR-299 R13 / KTD2).
///
/// Keyed on the daemon's machine-readable error `code` — never on message
/// text, which drifts silently past fakes — and on the Swift-side transport
/// error where no envelope ever arrived. Every `message` is a STATIC literal
/// with no interpolation, so a backend string can never carry raw server text
/// into rendered copy. This mirrors `AccountErrorCopy`, for the same reason.
enum ShareLinkErrorCopy: Equatable, CaseIterable {
    /// No stored cloud credential — the fix is signing in, not retrying.
    case notSignedIn
    /// Nothing has been uploaded for this recording yet. Terminal: retrying
    /// cannot help, so no retry is offered.
    case nothingToShare
    /// The share backend failed or was unreachable. Retry is honest advice.
    case backendUnavailable
    /// The daemon socket was unreachable — the app, not the cloud, is the
    /// problem, so the copy points at the helper rather than the network.
    case helperUnavailable
    /// Anything unrecognized (absent/future code, unexpected Swift error).
    /// Static by construction: no field of the failure is embedded.
    case unknown

    /// The recovery affordance rendered next to the message.
    ///
    /// The no-recovery case is spelled `noRecovery` rather than `none` so it
    /// can never be misread as `Optional.none` at a call site.
    enum Action: Equatable {
        case retry
        case signIn
        /// No useful recovery — the message alone is the whole answer.
        case noRecovery
    }

    var message: String {
        switch self {
        case .notSignedIn:
            return "Sign in to your Screencap account to share a recording by link."
        case .nothingToShare:
            return "This recording hasn't finished uploading yet, so there's nothing to link to."
        case .backendUnavailable:
            return "Couldn't reach Screencap just now. Try again in a moment."
        case .helperUnavailable:
            return "Screencap's background helper isn't running, so links can't be created."
        case .unknown:
            return "Something went wrong. Try again in a moment."
        }
    }

    var action: Action {
        switch self {
        case .notSignedIn: return .signIn
        // Retrying cannot make an un-uploaded recording shareable.
        case .nothingToShare: return .noRecovery
        case .backendUnavailable, .helperUnavailable, .unknown: return .retry
        }
    }

    /// Map a thrown error to its copy. Unrecognized codes and error types fall
    /// through to `.unknown` rather than surfacing the underlying text.
    static func resolve(_ error: Error) -> ShareLinkErrorCopy {
        switch error {
        case DaemonClientError.envelopeError(let code, _):
            return fromCode(code)
        case DaemonClientError.socketUnavailable, DaemonClientError.connectionFailed:
            return .helperUnavailable
        case DaemonClientError.timedOut:
            return .backendUnavailable
        default:
            return .unknown
        }
    }

    /// The daemon's typed share vocabulary (SCR-299 U1). Kept as a separate
    /// entry point so the code contract can be tested without constructing a
    /// transport error.
    static func fromCode(_ code: String) -> ShareLinkErrorCopy {
        switch code {
        case "not_signed_in": return .notSignedIn
        case "share_unavailable": return .nothingToShare
        case "share_backend_unavailable": return .backendUnavailable
        default: return .unknown
        }
    }
}

// MARK: - Controller (SCR-299 U4)

/// The daemon calls the controller needs, behind a protocol so tests drive it
/// with a fake instead of a live socket.
@MainActor
protocol ShareLinkService {
    func create(recording: String) async throws -> RecordingShareResponse
    func revoke(token: String) async throws -> RecordingShareResponse
    func list() async throws -> [ShareRecord]
}

@MainActor
struct LiveShareLinkService: ShareLinkService {
    func create(recording: String) async throws -> RecordingShareResponse {
        try await DaemonClient.shareCreate(recording: recording)
    }

    func revoke(token: String) async throws -> RecordingShareResponse {
        try await DaemonClient.shareRevoke(token: token)
    }

    func list() async throws -> [ShareRecord] {
        try await DaemonClient.shareList().shares ?? []
    }
}

/// Drives copy/revoke for one recording's share link, exposing a single state
/// the Inspect menu renders from.
///
/// **The link is never logged.** No statement in this type interpolates a share
/// URL, token, or fragment, and none is marked public in a log annotation — the
/// unified system log is readable well beyond this process and the fragment is
/// the decryption key, so a routine logging habit here would defeat the entire
/// encryption model. That is why this controller has no `Logger` at all.
@MainActor
final class ShareLinkController: ObservableObject {
    /// Which action failed. Carried on `.failed` so the window can title the
    /// alert and route its retry correctly — without it, a failed revoke
    /// reported itself as a failed create and "Try Again" minted a new link
    /// instead of retrying the revoke.
    enum Operation: Equatable {
        case create
        case revoke
    }

    enum Phase: Equatable {
        /// No link this Mac knows about.
        case noLink
        /// A share exists and can be copied again or revoked.
        case hasLink(token: String)
        case creating
        case revoking
        case failed(ShareLinkErrorCopy, during: Operation)
    }

    @Published private(set) var phase: Phase = .noLink
    /// The most recent link, held for the OS share sheet. Never logged.
    @Published private(set) var lastCreatedURL: URL?

    private let recordingName: String
    private let service: ShareLinkService
    /// Injected so tests can prove the link reaches the pasteboard without
    /// clobbering the developer's real clipboard mid-suite. Main-actor isolated
    /// like its only caller, so it stays legal under strict concurrency.
    private let writeToPasteboard: @MainActor (String) -> Void
    /// The token a revoke is acting on, held separately from `phase` so it
    /// survives a failed revoke — `activeToken` reads nil once the phase is
    /// `.failed`, which would otherwise make the retry a silent no-op.
    private var revokeTargetToken: String?

    init(
        recordingName: String,
        service: ShareLinkService = LiveShareLinkService(),
        writeToPasteboard: @escaping @MainActor (String) -> Void =
            ShareLinkController.systemPasteboardWrite
    ) {
        self.recordingName = recordingName
        self.service = service
        self.writeToPasteboard = writeToPasteboard
    }

    static func systemPasteboardWrite(_ value: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(value, forType: .string)
    }

    /// True while a daemon call is in flight — the menu disables its actions so
    /// a slow create (fetch + re-encrypt + upload) can't be double-fired.
    var isBusy: Bool {
        phase == .creating || phase == .revoking
    }

    var activeToken: String? {
        if case .hasLink(let token) = phase { return token }
        return nil
    }

    /// Read this Mac's share records and resolve whether a live link exists.
    /// Best-effort: a failure here leaves the menu in its current state rather
    /// than showing an error, because nothing the user asked for has failed yet.
    func refresh() async {
        guard !isBusy else { return }
        guard let records = try? await service.list() else { return }
        switch ShareLinkPolicy.state(for: recordingName, in: records) {
        case .active(let token):
            phase = .hasLink(token: token)
        case .none:
            // Don't clobber a failure the user hasn't acknowledged.
            if case .failed = phase { return }
            phase = .noLink
        }
    }

    /// Mint a link and put it on the pasteboard (R1, AE5).
    ///
    /// Re-entry while a create is in flight is a no-op rather than a second
    /// racing call — the same guard `InspectWindowViewModel` uses, and it
    /// matters more here because a create can run for minutes.
    func copyLink() async {
        guard !isBusy else { return }
        phase = .creating
        do {
            let response = try await service.create(recording: recordingName)
            guard let urlString = response.url, let url = URL(string: urlString) else {
                phase = .failed(.unknown, during: .create)
                return
            }
            writeToPasteboard(urlString)
            lastCreatedURL = url
            if let token = response.token, !token.isEmpty {
                phase = .hasLink(token: token)
            } else {
                // The link copied fine, but with no token there is nothing to
                // revoke WITH — offering the action would hand the daemon an
                // empty token and fail confusingly. Fall back to a refresh,
                // which picks up the record if the daemon wrote one.
                phase = .noLink
                await refresh()
            }
        } catch {
            // Nothing reaches the pasteboard on failure, and no stale link is
            // left behind for the share sheet to hand out.
            lastCreatedURL = nil
            phase = .failed(ShareLinkErrorCopy.resolve(error), during: .create)
        }
    }

    /// Copy an already-minted link again without paying for a fresh create.
    func copyExistingLink() {
        guard let url = lastCreatedURL else { return }
        writeToPasteboard(url.absoluteString)
    }

    /// Revoke the active link (R4, AE6). Refreshes afterwards so the menu stops
    /// offering revoke.
    func revoke() async {
        guard !isBusy, let token = activeToken ?? revokeTargetToken else { return }
        revokeTargetToken = token
        phase = .revoking
        do {
            _ = try await service.revoke(token: token)
            revokeTargetToken = nil
            lastCreatedURL = nil
            phase = .noLink
            await refresh()
        } catch {
            phase = .failed(ShareLinkErrorCopy.resolve(error), during: .revoke)
        }
    }

    /// Retry whichever action failed. Routing on the recorded operation is what
    /// stops a failed revoke's "Try Again" from minting a fresh link.
    func retryFailedOperation() async {
        guard case .failed(_, let operation) = phase else { return }
        switch operation {
        case .create: await copyLink()
        case .revoke: await revoke()
        }
    }

    /// Clear a failure so the menu returns to its normal actions. The refresh
    /// re-derives the real state, so a dismissed revoke failure correctly goes
    /// back to showing the link as still live.
    func dismissFailure() async {
        guard case .failed = phase else { return }
        revokeTargetToken = nil
        phase = .noLink
        await refresh()
    }
}
