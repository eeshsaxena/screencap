import Foundation

/// Pure gating + copy for the SCR-258 upgrade-migration surfaces (U10, KTD-18).
/// Factored out so the banner-visibility rule and the state copy (including the
/// distinct auto-resuming paused wording) are unit-testable without SwiftUI.
enum VaultMigrationPolicy {
    /// Whether the one-shot Library banner should show.
    ///
    /// Only when the container is enabled and the library is not yet encrypted. An
    /// active or paused migration is ALWAYS surfaced (even after a decline — the
    /// user should see progress / the disk pause); the initial offer respects the
    /// persisted decline and needs a plaintext library present. A completed
    /// migration never shows.
    static func shouldShowBanner(
        containerEnabled: Bool,
        storeEncrypted: Bool,
        recordingsPresent: Bool,
        state: PrivacyController.EncryptMigrationState,
        dismissed: Bool
    ) -> Bool {
        guard containerEnabled, !storeEncrypted else { return false }
        switch state {
        case .succeeded:
            return false
        case .migrating, .paused:
            return true
        case .idle, .failed:
            return recordingsPresent && !dismissed
        }
    }

    /// The one-line status under the banner / settings entry for a given state.
    /// The `.paused` copy names the auto-resume so an ENOSPC pause never reads as a
    /// failure (KTD-18).
    static func statusLine(for state: PrivacyController.EncryptMigrationState) -> String? {
        switch state {
        case .idle:
            return nil
        case .migrating(let p):
            let total = max(p.total, 1)
            return "Encrypting… \(p.verified) of \(total) recordings secured."
        case .paused(let reason):
            if reason == "insufficient_disk" {
                return "Waiting for free disk space — will resume automatically."
            }
            return "Paused (\(reason)) — will resume automatically."
        case .succeeded:
            return "Your recordings are now encrypted at rest on this Mac."
        case .failed(let message):
            return message
        }
    }

    /// The banner headline + body. Costs are stated up front (R12): the migration
    /// transiently needs free disk on the order of the library size and takes time.
    static let bannerTitle = "Encrypt your recordings at rest"
    static let bannerBody =
        "Move your existing recordings into an encrypted container so they're "
        + "ciphertext on disk. This needs temporary free space about the size of "
        + "your library and can take a while — recording keeps working throughout, "
        + "and nothing is deleted until every recording is safely copied."

    /// A fallback human message when a refusal envelope carries no `message`.
    static func refusalFallback(code: String) -> String {
        switch code {
        case "custom_recordings_dir":
            return "This install uses a custom recordings location, which stays "
                + "plaintext and is outside encrypted storage."
        case "container_disabled":
            return "Encrypted storage isn't enabled on this install."
        default:
            return "Couldn't start encrypting your recordings (\(code))."
        }
    }
}
