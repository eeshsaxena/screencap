import Foundation

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
            phase = .failed("Couldn't turn on search. Please try again.")
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
