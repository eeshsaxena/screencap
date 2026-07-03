import Foundation

/// The `privacy` block emitted by `screencap settings --json` (SCR-17,
/// settings schema v2). Drives the SwiftUI first-run banner state machine
/// and the read-only mode header in `PrivacyPaneView`.
///
/// `hasPrivacySection` distinguishes "never written" from "present with
/// default values" — only the former triggers the first-launch fail-closed
/// `mode = internal` write. Reading the parsed `PrivacyConfig` instead would
/// conflate the two (defaults fill missing sections).
struct PrivacyStatus: Decodable, Equatable {
    let mode: String
    let setupSkipped: Bool
    let hasPrivacySection: Bool

    enum CodingKeys: String, CodingKey {
        case mode
        case setupSkipped = "setup_skipped"
        case hasPrivacySection = "has_privacy_section"
    }
}

/// Outer envelope for `screencap settings --json`. Only the `privacy` block
/// is consumed today; the other settings fields are decoded by other features
/// where applicable.
struct SettingsEnvelope: Decodable {
    struct Inner: Decodable {
        let privacy: PrivacyStatus?
        /// SCR-174: on-screen-text indexing flag (drives the search consent
        /// trigger). Optional/tolerant — older daemons omit it.
        let contentIndexEnabled: Bool?
        /// SCR-174: one-time consent decision, persisted as a settings bool so
        /// "declined" ≠ "off" and the prompt never re-fires (U7).
        let contentIndexConsentDeclined: Bool?
        /// SCR-178: one-time "Skip" decision on the backfill offer, persisted as
        /// a settings bool so the "also index existing recordings" affordance
        /// never re-nags after the user declines it (U8). Distinct from
        /// `contentIndexConsentDeclined`: the user may consent to forward-only
        /// indexing yet skip the historical backfill.
        let contentIndexBackfillDeclined: Bool?
        /// SCR-174: recording chunk length (seconds) — used to estimate a
        /// transcript chunk's wall-clock position before snapping to a real
        /// timeline event.
        let chunkDuration: Double?
        /// U6: the persisted default microphone choice — seeds the New-recording
        /// sheet's mic toggle. Optional/tolerant — older daemons omit it (→ the
        /// sheet defaults the toggle on).
        let audioDefault: Bool?
        /// U6/U12: the four-valued upload default (`local` | `ask` | `cloud` |
        /// `both`, KTD-11) — drives the sheet's "stays on this Mac" header copy
        /// and (later) the Privacy pane's keep-local toggle.
        let uploadDefault: String?

        enum CodingKeys: String, CodingKey {
            case privacy
            case contentIndexEnabled = "content_index_enabled"
            case contentIndexConsentDeclined = "content_index_consent_declined"
            case contentIndexBackfillDeclined = "content_index_backfill_declined"
            case chunkDuration = "chunk_duration"
            case audioDefault = "audio_default"
            case uploadDefault = "upload_default"
        }
    }

    let ok: Bool
    let settings: Inner

    enum CodingKeys: String, CodingKey {
        case ok
        case settings
    }
}
