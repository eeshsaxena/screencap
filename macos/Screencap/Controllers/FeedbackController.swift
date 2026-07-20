import Combine
import Foundation
import OSLog

private let feedbackLogger = Logger(subsystem: "com.screencap.macos", category: "feedback")

/// Test seam over the `screencap feedback send` shell-out so the controller is
/// substitutable in tests (the `CloudAuthService` precedent). Production wiring
/// is `LiveFeedbackService`, which routes through `CLIClient.runJSONRawStdin` —
/// the payload travels on stdin (never argv), and non-zero exit is tolerated so
/// the `{ok:false, error_kind}` envelope always comes back for mapping.
@MainActor
protocol FeedbackService {
    /// Raw stdout of `screencap feedback send` fed `payload` on stdin.
    /// `timeout` is the KTD-12 payload-scaled wall clock — never the 10s
    /// default, which would SIGTERM every attachment-bearing submission.
    func send(payload: Data, timeout: TimeInterval) async throws -> Data
}

@MainActor
final class LiveFeedbackService: FeedbackService {
    func send(payload: Data, timeout: TimeInterval) async throws -> Data {
        try await CLIClient.runJSONRawStdin(
            ["feedback", "send", "--json"], stdin: payload, timeout: timeout
        )
    }
}

/// Owns the feedback draft, metadata collection, submission lifecycle, and
/// error mapping (feat/in-app-feedback-form U3). Mirrors
/// `ClipExportController`'s published state-enum pattern.
///
/// Owned above the sheet (MainWindow scope, KTD-8) so the draft survives sheet
/// dismissal for the app session; nothing is persisted to disk (a persisted
/// draft would be user text on disk — deliberate v1 exclusion).
@MainActor
final class FeedbackController: ObservableObject {
    /// KTD-9: the daemon probe races this deadline and renders "unavailable" on
    /// loss — a stale-but-listening daemon that accepts and hangs is exactly
    /// the top bug-report scenario, and the default 10s client timeout would
    /// stall form open right when the user is reporting the daemon is broken.
    static let daemonProbeTimeoutSeconds: TimeInterval = 2.0

    /// The metadata literal when the daemon can't answer in time (AE4).
    static let daemonVersionUnavailable = "unavailable"

    // MARK: - Draft (survives sheet dismissal for the session)

    @Published var requestType: FeedbackRequestType = .bug
    @Published var message: String = ""
    @Published var email: String = ""
    @Published private(set) var attachments: [FeedbackAttachment] = []
    /// The most recent selection-time refusal, rendered inline at the
    /// attachment list (AE2/AE6/AE7). Cleared on the next add/remove.
    @Published private(set) var attachmentRejection: FeedbackAttachmentRejection?

    // MARK: - Lifecycle + metadata

    @Published private(set) var state: FeedbackSendState = .idle
    /// Nil while the async probe is in flight (≤ ~2s); then the daemon's
    /// version string or `daemonVersionUnavailable`. Never blocks open or send
    /// (KTD-9) — a send while nil declares "unavailable".
    @Published private(set) var daemonVersion: String?

    /// App/macOS versions are synchronous local reads (KTD-9). The two version
    /// schemes are independent — never derive one from the other (documented
    /// learning: stale-daemon-after-app-update).
    let appVersion: String
    let macOSVersion: String

    private let service: FeedbackService
    /// Injectable daemon probe (defaults to `DaemonClient.daemonInfo()`), so
    /// tests can hang or fail it without a real socket.
    private let daemonVersionFetch: @Sendable () async throws -> String
    private let daemonProbeTimeout: TimeInterval
    private var daemonFetchTask: Task<Void, Never>?
    private var sendTask: Task<Void, Never>?
    /// Prefill fires at most once per draft (AE1/AE5): a user who cleared the
    /// field mid-session must not find it repopulated on the next open.
    private var didAttemptEmailPrefill = false

    init(
        service: FeedbackService = LiveFeedbackService(),
        daemonVersionFetch: @escaping @Sendable () async throws -> String = {
            // Bound at the TRANSPORT (connection.cancel via withTimeout), not
            // just at the raceProbe deadline: the task group awaits its
            // children on exit, and NWConnection reads ignore task
            // cancellation — so an unbounded fetch would stretch the probe to
            // the client's default 10s despite the 2s race.
            try await DaemonClient.daemonInfo(
                timeout: FeedbackController.daemonProbeTimeoutSeconds
            ).daemonVersion
        },
        daemonProbeTimeout: TimeInterval = FeedbackController.daemonProbeTimeoutSeconds,
        appVersion: String = Bundle.main
            .object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "unknown",
        macOSVersion: String = {
            let v = ProcessInfo.processInfo.operatingSystemVersion
            return "\(v.majorVersion).\(v.minorVersion).\(v.patchVersion)"
        }()
    ) {
        self.service = service
        self.daemonVersionFetch = daemonVersionFetch
        self.daemonProbeTimeout = daemonProbeTimeout
        self.appVersion = appVersion
        self.macOSVersion = macOSVersion
    }

    // MARK: - Presentation

    /// Called when the sheet appears: prefill the email from the resolved auth
    /// status (KTD-10 — status only, no token access) and kick the async
    /// daemon probe. Both are non-blocking; a lingering success from a
    /// previous submission resets to a fresh draft form.
    func prepareForPresentation(auth: AuthStatus) {
        if case .success = state { state = .idle }
        prefillEmailIfNeeded(from: auth)
        fetchDaemonVersionIfNeeded()
    }

    /// AE1/AE5: signed-in with a non-nil email prefills; stale/nil yields an
    /// empty field; the user clearing it afterwards sticks (one-shot latch).
    /// `.unknown` doesn't burn the latch — auth resolves lazily, so the view
    /// re-offers the status once it lands and prefill still gets its one shot.
    func prefillEmailIfNeeded(from auth: AuthStatus) {
        guard !didAttemptEmailPrefill, auth != .unknown else { return }
        didAttemptEmailPrefill = true
        guard email.isEmpty,
              case .signedIn(let signedInEmail, _, _) = auth,
              let signedInEmail, !signedInEmail.isEmpty else { return }
        email = signedInEmail
    }

    /// Race the daemon probe against the short deadline (KTD-9). Idempotent —
    /// one resolution per session is enough; version churn mid-session isn't a
    /// bug-report-relevant signal.
    func fetchDaemonVersionIfNeeded() {
        guard daemonVersion == nil, daemonFetchTask == nil else { return }
        let fetch = daemonVersionFetch
        let deadline = daemonProbeTimeout
        daemonFetchTask = Task { [weak self] in
            let resolved = await Self.raceProbe(fetch, deadline: deadline)
            self?.daemonVersion = resolved ?? Self.daemonVersionUnavailable
        }
    }

    /// First finisher wins: the probe's version, or nil when the deadline fires
    /// or the probe throws (daemon down). The losing probe task is cancelled —
    /// a hung socket read must not outlive the form.
    private static func raceProbe(
        _ fetch: @escaping @Sendable () async throws -> String,
        deadline: TimeInterval
    ) async -> String? {
        await withTaskGroup(of: String?.self) { group in
            group.addTask { try? await fetch() }
            group.addTask {
                try? await Task.sleep(nanoseconds: UInt64(max(0, deadline) * 1_000_000_000))
                return nil
            }
            let first = await group.next() ?? nil
            group.cancelAll()
            return first
        }
    }

    // MARK: - Attachments (selection-time validation, R5/AE2)

    /// Validate and add user-picked files. Files that pass join the draft;
    /// the FIRST refusal is surfaced (with its distinct reason) while later
    /// valid picks still land — friendlier than dropping the whole selection.
    func addAttachments(urls: [URL]) {
        attachmentRejection = nil
        for url in urls {
            let fileName = url.lastPathComponent
            // Type gate first — an unsupported pick must read "unsupported"
            // even when it would also break a size cap (see the policy test).
            guard let contentType = FeedbackAttachmentPolicy.contentType(
                forExtension: url.pathExtension
            ) else {
                noteRejection(.unsupportedType(fileName: fileName))
                continue
            }
            let sizeBytes: Int
            do {
                let attrs = try FileManager.default.attributesOfItem(atPath: url.path)
                sizeBytes = (attrs[.size] as? Int) ?? 0
            } catch {
                noteRejection(.unreadable(fileName: fileName))
                continue
            }
            if let rejection = FeedbackAttachmentPolicy.rejection(
                fileName: fileName,
                fileExtension: url.pathExtension,
                sizeBytes: sizeBytes,
                existingCount: attachments.count,
                existingTotalBytes: totalAttachmentBytes
            ) {
                noteRejection(rejection)
                continue
            }
            attachments.append(FeedbackAttachment(
                id: UUID(),
                path: url.path,
                fileName: fileName,
                sizeBytes: sizeBytes,
                contentType: contentType
            ))
        }
    }

    private func noteRejection(_ rejection: FeedbackAttachmentRejection) {
        if attachmentRejection == nil { attachmentRejection = rejection }
    }

    /// Per-row remove (U4): a mis-attached file can be dropped before send;
    /// removing the last one returns the draft to text-only.
    func removeAttachment(id: UUID) {
        attachments.removeAll { $0.id == id }
        attachmentRejection = nil
    }

    var totalAttachmentBytes: Int {
        attachments.reduce(0) { $0 + $1.sizeBytes }
    }

    // MARK: - Send lifecycle

    var canSend: Bool {
        FeedbackFormPolicy.canSend(
            message: message, email: email, isSending: state == .sending
        )
    }

    /// The always-visible metadata line's content (R8: the user sees exactly
    /// what identifying metadata rides along, before sending).
    var metadataSummary: String {
        let daemon = daemonVersion ?? "checking…"
        return "app \(appVersion) · daemon \(daemon) · macOS \(macOSVersion)"
    }

    func send() {
        guard canSend else { return }
        state = .sending
        let payload = FeedbackPayload(
            type: requestType.rawValue,
            message: message,
            email: email.trimmingCharacters(in: .whitespaces),
            versions: .init(
                app: appVersion,
                daemon: daemonVersion ?? Self.daemonVersionUnavailable,
                macos: macOSVersion
            ),
            attachments: attachments.map {
                .init(path: $0.path, contentType: $0.contentType)
            }
        )
        let payloadData: Data
        do {
            payloadData = try JSONEncoder().encode(payload)
        } catch {
            // Encodable structs of strings can't realistically fail, but a
            // silent wedge on `.sending` would be worse than an honest error.
            state = .failure(Self.failure(kind: .invalid, retryable: false, detail: nil))
            return
        }
        let timeout = FeedbackSendTimeout.seconds(declaredBytes: totalAttachmentBytes)
        sendTask = Task { [weak self] in
            guard let self else { return }
            do {
                let data = try await self.service.send(payload: payloadData, timeout: timeout)
                guard !Task.isCancelled else { return }
                self.handleEnvelope(data)
            } catch {
                guard !Task.isCancelled else { return }
                self.handleSendError(error)
            }
            self.sendTask = nil
        }
    }

    /// Explicit Cancel while `.sending` (U4): abort the in-flight subprocess
    /// (task cancellation SIGTERMs the child via `runOneShot`'s onCancel) and
    /// return to the editable draft. The dedicated-issue residual — a SIGTERM
    /// landing after `submit` reached the relay — is accepted for launch
    /// (KTD-12); a retry may duplicate the issue.
    func cancelSend() {
        sendTask?.cancel()
        sendTask = nil
        if state == .sending { state = .idle }
    }

    /// Done on the success screen (or dismissal from it): back to a fresh form
    /// for the next report. The draft was already cleared when success landed.
    func acknowledgeSuccess() {
        if case .success = state { state = .idle }
    }

    private func handleEnvelope(_ data: Data) {
        guard let envelope = FeedbackSendEnvelope.parse(data) else {
            // Empty/undecodable stdout: the CLI crashed before its envelope
            // (contract violation) — an honest retryable server failure, never
            // a wedge on `.sending`.
            state = .failure(Self.failure(kind: .server, retryable: true, detail: nil))
            return
        }
        // Success keys on `ok` alone; optional fields decode as optionals
        // (never gate on `issue_url`).
        if envelope.ok == true {
            state = .success(issueURL: envelope.issueUrl)
            clearDraft()
            return
        }
        let kind = FeedbackErrorKind.from(code: envelope.errorKind)
        // KTD-7: `invalid` surfaces the relay's short field reason as detail —
        // it originates from our own relay/CLI validation, not user input.
        let detail = kind == .invalid ? envelope.message : nil
        state = .failure(Self.failure(
            kind: kind,
            retryable: envelope.retryable ?? kind.defaultRetryable,
            detail: detail
        ))
    }

    private func handleSendError(_ error: Error) {
        // KTD-12: a subprocess wall-clock timeout (SIGTERM mid-upload) reads as
        // network trouble — retry is the honest advice. Everything else (spawn
        // failure, binary missing) maps to the retryable server fallback; raw
        // `localizedDescription` never reaches the rendered copy.
        feedbackLogger.warning(
            "feedback send shell-out failed: \(String(describing: type(of: error)), privacy: .public)"
        )
        if case CLIError.timedOut = error {
            state = .failure(Self.failure(kind: .network, retryable: true, detail: nil))
        } else {
            state = .failure(Self.failure(kind: .server, retryable: true, detail: nil))
        }
    }

    private static func failure(
        kind: FeedbackErrorKind, retryable: Bool, detail: String?
    ) -> FeedbackFailure {
        FeedbackFailure(
            kind: kind, message: kind.userMessage, retryable: retryable, detail: detail
        )
    }

    /// Success clears the whole draft (U3); the email prefill latch re-arms so
    /// the next report starts from the signed-in identity again.
    private func clearDraft() {
        message = ""
        email = ""
        attachments = []
        attachmentRejection = nil
        requestType = .bug
        didAttemptEmailPrefill = false
    }
}
