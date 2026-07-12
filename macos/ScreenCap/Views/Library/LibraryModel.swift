import SwiftUI

// U5 — the Library grid's pure model layer: the filter chips, the card status
// badge, and the visible-rows derivation. Factored out of `LibraryView` so the
// KTD-6 chip semantics and the KTD-9 honesty-substituted badge copy are directly
// unit-testable (LibraryFilterTests) without a render.

/// The Library filter chips (design 355–359, logic 691–700), remapped to local
/// storage semantics per KTD-6: the prototype's "Mine"/"Shared" become
/// "Local"/"Uploaded" until team semantics exist (SCR-221, KTD-9). Ordered as the
/// row renders them.
enum LibraryChip: String, CaseIterable, Identifiable, Hashable {
    case all = "All"
    case local = "Local"
    case uploaded = "Uploaded"
    case needsReview = "Needs review"

    var id: String { rawValue }
    var label: String { rawValue }

    /// Whether `rec` belongs under this chip (KTD-6): Local = not uploaded;
    /// Uploaded = uploaded to the user's own cloud; Needs review = still
    /// processing/draft (anything not yet `ready`, including an in-progress
    /// recording). `.all` matches everything.
    ///
    /// The predicates deliberately overlap the way the prototype's do — a
    /// processing local recording appears under both Local and Needs review,
    /// mirroring the design's draft card, which is both `kind: 'Mine'` and a
    /// `Needs review` match (logic 710–711).
    func matches(_ rec: RecordingSummary) -> Bool {
        switch self {
        case .all: return true
        case .local: return !rec.uploaded
        case .uploaded: return rec.uploaded
        case .needsReview: return !rec.isReady
        }
    }
}

/// A Library card's status badge (design 370, logic 702–710). The copy is the
/// honesty-substituted vocabulary (KTD-9): the prototype's "shared · encrypted"
/// becomes "uploaded" — this type never emits "shared" (forbidden until team
/// semantics exist, SCR-221). "encrypted" is emitted only from the recording's
/// frozen per-recording E2EE intent (`cloudE2EE`, KTD-4) — never from live flag
/// or ledger state — so the badge claims encryption exactly when that
/// recording's uploads are ciphertext (R3). Pure and synchronous (no Keychain
/// reads, no I/O); the view maps `tone` to the design's border/foreground
/// colors so the mapping is testable without SwiftUI `Color`.
struct LibraryBadge: Equatable {
    enum Tone: Equatable {
        /// Teal outline — uploaded to the user's own cloud.
        case uploaded
        /// Amber outline — still processing / draft (or actively recording).
        case draft
        /// Muted outline — a finished local-only recording.
        case local
    }

    let text: String
    let tone: Tone

    /// uploaded → teal "uploaded" ("uploaded · encrypted" when the frozen
    /// intent bit is true); not-yet-`ready` (processing / recording) → amber
    /// "draft · local"; else muted "local" (U5 approach). Uploaded wins over
    /// draft so a cloud recording still finishing reads as "uploaded",
    /// matching the sidebar's `allLocal` gate. `cloudE2EE` nil/false (older
    /// daemons, pre-arc recordings) leaves today's copy unchanged; the bit is
    /// about uploaded copies, so local rows ignore it entirely.
    static func forRecording(_ rec: RecordingSummary) -> LibraryBadge {
        if rec.uploaded {
            let text = rec.cloudE2EE == true ? "uploaded · encrypted" : "uploaded"
            return LibraryBadge(text: text, tone: .uploaded)
        }
        if !rec.isReady { return LibraryBadge(text: "draft · local", tone: .draft) }
        return LibraryBadge(text: "local", tone: .local)
    }
}

/// What submitting the Rename… sheet should do (U6). Kept pure + `Equatable` so
/// the no-op-on-unchanged-default rule is assertable without a render.
enum RenameDecision: Equatable {
    /// Dismiss WITHOUT calling `recording.rename`: the field is unchanged and
    /// the current title is the derived default, so writing it would freeze the
    /// date/time default as a permanent user title.
    case noOp
    /// Call `recording.rename` with this (already length-capped) title. An empty
    /// string is a valid submission — it clears the rename back to the default.
    case submit(String)
}

/// Pure rules for the Library-card Rename… affordance (U6), factored out of the
/// `RenameSheet` view so the character cap and the no-op-on-unchanged-default
/// decision are directly unit-testable (LibraryCardRenameTests) without a render.
enum RenameModel {
    /// The display-title cap enforced live in the field, mirroring the daemon's
    /// `validate_recording_title` bound (<=200 chars).
    static let maxTitleLength = 200

    /// Truncate `text` to the display-title cap, counting Unicode scalars (code
    /// points) so it matches the daemon's `validate_recording_title`, which caps
    /// by Python `len()` (also code points). Counting grapheme clusters instead
    /// would let the field accept a multi-scalar-emoji title the daemon then
    /// rejects. ASCII is unaffected (1 Character == 1 scalar). Enforced live on
    /// every keystroke.
    static func cap(_ text: String) -> String {
        guard text.unicodeScalars.count > maxTitleLength else { return text }
        return String(String.UnicodeScalarView(text.unicodeScalars.prefix(maxTitleLength)))
    }

    /// Decide what submitting `draft` should do given the recording's current
    /// resolved title and whether that title is a user-set rename.
    ///
    /// No-op ONLY when the (capped) draft equals the current title AND that
    /// title is NOT user-set (`titleIsUserSet != true`, so nil/absent counts as
    /// the derived default). This is the rule that keeps "open Rename…, hit Save
    /// unchanged" from silently promoting the date/time default into a frozen
    /// user title. Any real edit — or clearing to empty — submits.
    static func decide(draft: String, currentTitle: String, titleIsUserSet: Bool?) -> RenameDecision {
        let capped = cap(draft)
        if capped == currentTitle, titleIsUserSet != true {
            return .noOp
        }
        return .submit(capped)
    }
}

/// Pure derivations for the grid, kept out of the view so ordering + identity are
/// assertable.
enum LibraryGrid {
    /// The rows visible for `chip`, newest-first. The draft / actively-recording
    /// row carries the most-recent `startedAt`, so it lands at the grid head —
    /// the design's draft-card injection (logic 710). The grid `ForEach` keys on
    /// each row's `stableID` (recording_id), so the post-stop auto-name directory
    /// rename updates a card in place instead of spawning a duplicate.
    static func visible(_ recordings: [RecordingSummary], chip: LibraryChip) -> [RecordingSummary] {
        recordings.filter(chip.matches).sorted(by: RecordingSummary.newestFirst)
    }
}
