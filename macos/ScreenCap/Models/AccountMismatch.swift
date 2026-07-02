import Foundation

/// A resolved account-mismatch signal (U9 / R16): the signed-in account no
/// longer matches the account that owns an in-flight cloud recording. Drives the
/// re-login prompt. Carries both gate-authoritative uids and a best-effort email
/// for the message.
struct AccountMismatch: Equatable {
    let ownerUid: String
    let signedInUid: String
    let signedInEmail: String?

    /// Resolve a mismatch from a decoded `/v0/events` line. Returns non-nil ONLY
    /// for a genuine `account_mismatch` where both uids are present and DIFFER —
    /// an equal or partially-absent pair is not a mismatch (fail-safe: never
    /// prompt re-login on ambiguous data).
    static func from(event: RecorderEventLine) -> AccountMismatch? {
        guard event.type == "account_mismatch",
              let owner = event.ownerUid, !owner.isEmpty,
              let signedIn = event.signedInUid, !signedIn.isEmpty,
              owner != signedIn
        else { return nil }
        return AccountMismatch(
            ownerUid: owner,
            signedInUid: signedIn,
            signedInEmail: event.signedInEmail
        )
    }

    /// Resolve a mismatch by DIRECTLY comparing a recording's pinned owner uid
    /// against the currently signed-in uid — the 410 reconcile path (KTD7). Used
    /// when the replay cursor has aged out, so the ephemeral event can't be
    /// replayed and the app compares uids itself instead.
    static func reconcile(ownerUid: String?, signedInUid: String?, signedInEmail: String?) -> AccountMismatch? {
        guard let owner = ownerUid, !owner.isEmpty,
              let signedIn = signedInUid, !signedIn.isEmpty,
              owner != signedIn
        else { return nil }
        return AccountMismatch(ownerUid: owner, signedInUid: signedIn, signedInEmail: signedInEmail)
    }
}
