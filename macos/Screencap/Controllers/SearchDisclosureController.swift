import Foundation
import OSLog

private let searchDisclosureLogger = Logger(subsystem: "com.screencap.macos", category: "search-disclosure")

// Search U8 (R6): the on-by-default disclosure. The default never flips for a user
// who hasn't seen this — so acknowledging it here is what turns search on (via
// `screencap search enable`, which acknowledges + encrypts the corpus + flips the
// gate), and declining writes the durable `content_index_consent_declined=true`
// opt-out. Shown as an onboarding step on new installs and as a one-time
// post-update disclosure on existing installs (which never re-run onboarding).
//
// The copy is honesty-gated (calibrated to the mechanism) and lives here as
// constants so `OnboardingSearchDisclosureTests` can string-assert it without a
// render tree — mirroring `OnboardingStepPolicy`.

enum SearchDisclosureCopy {
    static let title = "Search everything you've seen"

    static let intro =
        "Your recordings on this Mac become searchable — so you can find anything you saw, "
        + "and your local agent can look things up for you."

    /// The four mechanism points the disclosure must be honest about (R6): encrypted
    /// at rest, known-secret redaction, a retention bound, and the controls.
    static func points(retentionDays: Int) -> [String] {
        [
            "Stored encrypted on this Mac — nothing about search is uploaded.",
            "Known secret formats — passwords, API keys, card numbers — are automatically detected and hidden.",
            "Screenshots are kept for \(retentionDays) days, then removed automatically.",
            "You're in control: pause any time, exclude specific apps, or turn search off.",
        ]
    }

    static let enableButton = "Turn on search"
    static let declineButton = "Not now"

    /// The one honesty caveat we must not omit (R1 limits): redaction is best-effort.
    static let limitNote =
        "Redaction covers known formats — it reduces exposure but can't catch everything."
}

/// One `screencap search enable` failure event, on the same stderr channel and shape
/// as `ClipEventLine` — `screencap._stderr_events.emit_event` gives every payload
/// `type` / `ts` / `schema_version`. Drift-resilient: `type` is required, everything
/// else optional, and a line that doesn't decode simply isn't a match.
struct SearchEnableEventLine: Decodable, Equatable {
    let type: String
    let schemaVersion: Int?
    let reason: String?
    let detail: String?

    enum CodingKeys: String, CodingKey {
        case type
        case schemaVersion = "schema_version"
        case reason
        case detail
    }

    static func parse(stderrLine: String) -> SearchEnableEventLine? {
        let trimmed = stderrLine.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed.hasPrefix("{") else { return nil }
        guard let data = trimmed.data(using: .utf8) else { return nil }
        return try? JSONDecoder().decode(SearchEnableEventLine.self, from: data)
    }

    /// The failure event in a whole captured stderr blob, if any. `runAwaitingExit`
    /// hands us the blob rather than streaming lines, so we scan it ourselves.
    static func failure(inStderr stderr: String) -> SearchEnableEventLine? {
        stderr.split(separator: "\n").lazy
            .compactMap { parse(stderrLine: String($0)) }
            .first { $0.type == SearchEnableFailure.failedEventType }
    }
}

/// Turns a failed `screencap search enable` into copy that names the actual cause.
///
/// The old behavior — one fixed "Couldn't turn on search. Please try again." for every
/// failure — left a tester able to report nothing but the fact that it failed, and
/// "try again" was wrong advice for the deterministic failures (a Keychain refusal
/// fails identically forever). The CLI now emits the reason as a structured event on
/// **stderr** (`_fail_search_enable` in `src/screencap/cli/__init__.py`), which
/// `CLIError.nonZeroExit` carries through.
///
/// Pure so `OnboardingSearchDisclosureTests` can pin the mapping without a render tree,
/// mirroring `SearchDisclosureCopy` / `SearchDisclosurePolicy`.
enum SearchEnableFailure {
    /// Cross-language contract — matches `_SEARCH_ENABLE_EVENT_FAILED` and the
    /// `SEARCH_ENABLE_REASON_*` literals in `src/screencap/cli/__init__.py`.
    static let failedEventType = "search_enable_failed"
    static let keyUnavailableReason = "corpus_key_unavailable"
    static let migrationIncompleteReason = "migration_incomplete"

    /// Reason code + user-facing message in ONE pass over the error, so the code we
    /// log and the copy we show can never disagree about what went wrong.
    ///
    /// The reason code is a closed set safe to log; the message carries the raw detail,
    /// which can name the user's recordings.
    static func classify(_ error: Error) -> (reasonCode: String, message: String) {
        guard let cliError = error as? CLIError else {
            return ("unknown", compose("Couldn't turn on search.", detail: error.localizedDescription))
        }
        switch cliError {
        case .nonZeroExit(let code, let stderr):
            guard let event = SearchEnableEventLine.failure(inStderr: stderr) else {
                // Exited non-zero but said nothing we can decode — still beats a bare
                // retry prompt: name the exit code so a report is possible.
                return ("cli_exit", compose("Couldn't turn on search.",
                                            detail: "screencap exited with code \(code)"))
            }
            if let version = event.schemaVersion, version != SUPPORTED_API_SCHEMA_VERSION {
                // Same drift diagnostic the other stderr-event consumers emit
                // (ClipExportController / UploadController / DaemonClient).
                searchDisclosureLogger.warning(
                    """
                    search_enable_failed schema_version=\(version, privacy: .public) does not \
                    match SwiftUI side (\(SUPPORTED_API_SCHEMA_VERSION, privacy: .public)). \
                    Processing anyway.
                    """
                )
            }
            let reason = event.reason ?? "cli_exit"
            return (safeReasonCode(reason), compose(headline(for: reason), detail: event.detail ?? ""))
        case .timedOut(let seconds):
            // Enabling encrypts the existing corpus first, so a big library is slow —
            // and the flip is resumable, so retrying picks up where it stopped.
            return ("timed_out", compose(
                "Turning on search took longer than \(durationPhrase(seconds)) and was stopped. "
                    + "It encrypts your existing recordings first, so a large library can take a "
                    + "while — try again to pick up where it left off.",
                detail: ""
            ))
        case .binaryNotFound, .launchFailed, .decode:
            return (cliError.shortCode, compose(
                "Couldn't turn on search — Screencap's helper didn't respond.",
                detail: cliError.errorDescription ?? ""
            ))
        }
    }

    /// The short reason code for logging — never the raw detail.
    static func reasonCode(for error: Error) -> String { classify(error).reasonCode }

    /// User-facing message: a headline naming the cause, plus the underlying detail on
    /// its own line so a tester can report something actionable. Only the headlines
    /// that are genuinely retryable invite a retry.
    static func message(for error: Error) -> String { classify(error).message }

    private static func headline(for reason: String) -> String {
        switch reason {
        case keyUnavailableReason:
            // Channel-agnostic on purpose: `corpus_crypto._persist_key` raises
            // CorpusKeyUnavailable from three channels (shared Keychain group, the
            // legacy `keyring` fallback, and the SCREENCAP_CORPUS_KEY_FILE path), and
            // they all arrive under this one reason. Naming Keychain here would state a
            // confidently wrong cause for the other two — the exact failure this whole
            // change exists to stop. The `detail` line carries the real mechanism.
            //
            // Deterministic either way: the same refusal repeats on every retry, so
            // inviting one would just loop the user.
            return "Couldn't turn on search — Screencap couldn't securely store the search "
                + "encryption key. Retrying won't help until that's resolved."
        case migrationIncompleteReason:
            return "Couldn't turn on search — some existing recordings couldn't be encrypted, "
                + "so search stayed off. Trying again resumes where it stopped."
        default:
            return "Couldn't turn on search."
        }
    }

    /// Longest CLI-supplied detail we render. The sheet draws the message in a plain
    /// `Text` with no scroll or height cap, so an unbounded detail (a Python traceback,
    /// a deep path) would grow the sheet until the buttons leave the screen. Long
    /// enough for a real exception line plus a path; short enough to stay laid out.
    private static let detailCharacterLimit = 400

    private static func compose(_ headline: String, detail: String) -> String {
        let trimmed = detail.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return headline }
        let bounded = trimmed.count > detailCharacterLimit
            ? trimmed.prefix(detailCharacterLimit) + "…"
            : trimmed[...]
        return "\(headline)\n\(bounded)"
    }

    /// The reason code is logged `privacy: .public`, so it must not be able to carry
    /// free text. The CLI's reasons are snake_case identifiers by contract, but
    /// `SearchEnableEventLine.reason` is an untyped `String?` off a JSON line — so
    /// enforce the shape here rather than trusting the producer. Anything else (a path,
    /// a sentence, an argument-order slip on the Python side) becomes a fixed sentinel.
    private static func safeReasonCode(_ reason: String) -> String {
        let isIdentifier = !reason.isEmpty && reason.count <= 40
            && reason.allSatisfy { $0.isASCII && ($0.isLowercase || $0.isNumber || $0 == "_") }
        return isIdentifier ? reason : "unrecognized"
    }

    /// "5 minutes" / "1 minute" / "45 seconds" — the timeout is a caller-supplied
    /// interval, so don't assume it divides into whole minutes.
    private static func durationPhrase(_ seconds: TimeInterval) -> String {
        guard seconds >= 60 else { return "\(Int(seconds.rounded())) seconds" }
        let minutes = Int(seconds / 60)
        return "\(minutes) minute\(minutes == 1 ? "" : "s")"
    }
}

private extension CLIError {
    /// Closed-set code for logging. Only the non-`nonZeroExit` cases need one —
    /// a non-zero exit carries the CLI's own reason.
    var shortCode: String {
        switch self {
        case .nonZeroExit: return "cli_exit"
        case .timedOut: return "timed_out"
        case .binaryNotFound: return "binary_not_found"
        case .launchFailed: return "launch_failed"
        case .decode: return "decode"
        }
    }
}

/// Whether the disclosure should be presented, given the persisted markers. Pure so
/// the presentation decision is unit-testable (mirrors `OnboardingStepPolicy`).
enum SearchDisclosurePolicy {
    static func shouldPresent(acknowledged: Bool, declined: Bool) -> Bool {
        !acknowledged && !declined
    }
}

@MainActor
final class SearchDisclosureController: ObservableObject {
    enum Phase: Equatable {
        case idle
        case enabling
        case enabled
        case declined
        case failed(String)
    }

    @Published private(set) var phase: Phase = .idle

    /// CLI seam (injectable for tests). Defaults to the real `screencap` CLI with a
    /// long timeout, since `search enable` runs the corpus migration.
    private let run: ([String]) async throws -> Void

    init(run: @escaping ([String]) async throws -> Void = { try await CLIClient.runAwaitingExit($0, timeout: 300) }) {
        self.run = run
    }

    /// Acknowledge + enable: `screencap search enable` sets the disclosure marker,
    /// encrypts the existing corpus, and flips the readiness gate on.
    func enable() async {
        phase = .enabling
        do {
            try await run(["search", "enable"])
            phase = .enabled
        } catch {
            let failure = SearchEnableFailure.classify(error)
            // Log the reason CODE publicly; the message stays private (os_log's default
            // for interpolations) because its detail can name the user's recordings.
            searchDisclosureLogger.error(
                """
                search enable failed (reason: \
                \(failure.reasonCode, privacy: .public)): \(failure.message)
                """
            )
            phase = .failed(failure.message)
        }
    }

    /// Decline: write the durable opt-out the settings UI already honors.
    func decline() async {
        do {
            try await run(["settings", "--set", "content_index_consent_declined=true"])
            phase = .declined
        } catch {
            phase = .failed("Couldn't save your choice. Please try again.")
        }
    }
}
