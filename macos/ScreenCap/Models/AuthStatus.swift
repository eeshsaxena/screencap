import Foundation

/// Decoded shape of the `screencap whoami --json` (and `login --json`)
/// envelope (plan U4 / U6). Mirrors `ReviewDataEnvelope`'s tolerant contract:
/// every field is optional and the `ok` discriminator separates a success
/// payload from the `{ok:false, error}` error variant. A drift on the Python
/// side (a new field, or a new event type) never requires a Swift change to
/// keep decoding — only a consumer that wants the new field has to change.
///
/// The CLI wraps `auth.whoami()` as `{ok, schema_version, **info}` where
/// `info` is `{signed_in, uid?, email?, stale?}`; the signed-out case carries
/// only `signed_in: false`. See `cli/__init__.py::_AUTH_SCHEMA_VERSION` and
/// `auth.py::WhoAmI`.
struct AuthWhoAmIEnvelope: Decodable, Equatable {
    let ok: Bool?
    let schemaVersion: Int?
    let signedIn: Bool?
    let uid: String?
    let email: String?
    let stale: Bool?
    /// Cloud-paywall entitlement (billing U5/U8). Display/UX only — the signer's
    /// hard gate is the real enforcement. Additive/optional, so an older CLI that
    /// omits it decodes fine and resolves to "not subscribed". Kept for
    /// compatibility; under the two-tier split it is the DERIVED cloud signal
    /// (`subscribed == (tier == "cloud")`), never read independently of `tier`.
    let subscribed: Bool?
    /// Two-tier entitlement (paid-only launch, KTD-1/U6): the OPEN `tier` claim
    /// the webhook resolves from the paid price — `"local"` (Local Pro) or
    /// `"cloud"` (Cloud) today; a future `"free_capped"` slots in without a Swift
    /// change. Additive/optional; absent → nil (fresh not-entitled). When the
    /// token is `stale` (offline), `tier` is deliberately nil WITH `stale: true`
    /// — the two cases are told apart only by `stale`, so a paying-but-offline
    /// user is never read as "not entitled".
    let tier: String?
    /// The subscription's `trial_end` (epoch seconds) the webhook writes while
    /// `trialing` (KTD-2/KTD-3), for the "days left" trial UI. Absent when the
    /// token carries no trial (converted, lapsed, or never trialed).
    let trialEnd: Int?
    /// Client paywall flag (billing KTD-6). Config-driven and independent of
    /// sign-in, so it rides every whoami envelope (incl. signed-out). Gates
    /// whether the app shows pricing / the soft gate at all; absent → OFF, i.e.
    /// pre-billing behavior. Distinct from `subscribed` (the per-user grant).
    let paywallEnabled: Bool?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case signedIn = "signed_in"
        case uid
        case email
        case stale
        case subscribed
        case tier
        case trialEnd = "trial_end"
        case paywallEnabled = "paywall_enabled"
        case error
    }

    /// Drift-resilient parse. Returns nil on empty / non-object / non-decoding
    /// data so a Rich-console banner that leaks onto stdout cannot be mistaken
    /// for an auth envelope (mirrors `UploadEventLine.parse`). Callers map a
    /// nil result to the safe signed-out state.
    static func parse(_ data: Data) -> AuthWhoAmIEnvelope? {
        guard !data.isEmpty else { return nil }
        return try? JSONDecoder().decode(AuthWhoAmIEnvelope.self, from: data)
    }
}

/// Resolved cloud sign-in state the UI reads (plan U6). Built from an
/// envelope; any malformed / error / missing envelope resolves to
/// `.signedOut` — the safe default. We never claim signed-in without a
/// positive `signed_in: true`, and we never crash on bad input.
enum AuthStatus: Equatable {
    /// Not yet checked — the launch-time `whoami` refresh has not returned.
    /// Distinct from `.signedOut` so the account surface can avoid flashing a
    /// "Sign In" affordance before the first check lands.
    case unknown
    case signedOut
    /// Signed in. `stale` is true when a refresh was needed but failed (e.g.
    /// offline): the stored credential exists but identity couldn't be
    /// confirmed just now, so `uid`/`email` may both be nil (`whoami` returns
    /// `{signed_in: true, uid: null, email: null, stale: true}` in that case).
    case signedIn(email: String?, uid: String?, stale: Bool)

    /// Maps a decoded envelope to a status. A nil envelope (decode miss /
    /// empty output), `ok == false`, or a non-true `signed_in` all resolve to
    /// `.signedOut`.
    static func from(envelope: AuthWhoAmIEnvelope?) -> AuthStatus {
        guard let env = envelope, env.ok != false, env.signedIn == true else {
            return .signedOut
        }
        return .signedIn(email: env.email, uid: env.uid, stale: env.stale ?? false)
    }

    var isSignedIn: Bool {
        if case .signedIn = self { return true }
        return false
    }

    /// Account identifier for the menu line — email preferred, uid as the
    /// fallback. Nil when signed out, or signed-in-but-stale with no cached
    /// identity (the view shows an "offline" label in that case).
    var accountLabel: String? {
        guard case let .signedIn(email, uid, _) = self else { return nil }
        if let email, !email.isEmpty { return email }
        if let uid, !uid.isEmpty { return uid }
        return nil
    }
}

/// The resolved two-tier entitlement the paid-only picker reads (U11, KTD-1).
/// Mapped from the `whoami` envelope's OPEN `tier` string, keeping the client
/// forward-compatible with tiers it doesn't render yet (an unknown value maps to
/// `.none`, so a future `free_capped` never crashes or over-grants a gated
/// surface). `cloud` implies `local` (R3): a Cloud subscriber has the full local
/// recall surface plus upload.
enum EntitlementTier: Equatable {
    /// No positively-resolved paid tier — a fresh not-entitled or lapsed account
    /// (told apart from offline only by `AuthStatus`'s `stale`, never by this).
    case none
    /// Local Pro — unlimited local recording + recall, no cloud upload.
    case localPro
    /// Cloud — everything in Local Pro plus cloud upload/sync/AI (server-readable).
    case cloud

    /// Map the envelope's open `tier` string. Absent/unknown → `.none` so an
    /// older CLI or a future tier the app doesn't render can't be mistaken for an
    /// entitled state (fail-closed for display; the daemon lease is the real
    /// gate). The Python side writes `"local"` / `"cloud"` (auth.py `WhoAmI`).
    static func from(claim: String?) -> EntitlementTier {
        switch claim {
        case "local": return .localPro
        case "cloud": return .cloud
        default: return .none
        }
    }

    /// The tier string the checkout call selects the Stripe price with (U3). This
    /// is price-selection ONLY — the webhook re-derives the entitlement from the
    /// paid price and is the sole authority (KTD-2), so a spoofed value here can
    /// never over-grant. `.none` has no checkout tier.
    var checkoutTier: String? {
        switch self {
        case .localPro: return "local"
        case .cloud: return "cloud"
        case .none: return nil
        }
    }
}

/// Where a user sits in the trial → convert → lapse lifecycle, derived purely
/// from the envelope's `trial_end` and the resolved `tier` (U11). There is no
/// prior `AuthStatus` precedent for this, so the states are enumerated here and
/// unit-tested via `TrialState.from`.
///
/// `trial_end` is present ONLY while Stripe reports `trialing` (KTD-3); once the
/// trial auto-converts to paid, the claim carries a tier but no `trial_end`, so
/// `.subscribed` (a converted payer) is the natural "no trial_end but entitled"
/// case. A lapsed account clears the tier AND the trial_end → `.lapsed`.
enum TrialState: Equatable {
    /// Not signed in / paywall off / offline-stale — no trial claim to reason
    /// about. The picker must NOT render "trial expired" here (KTD-4 grace).
    case indeterminate
    /// Active paid subscription (converted, or grandfathered) — no trial banner.
    case subscribed
    /// In a trial with comfortable runway. `daysLeft` ≥ the near-expiry cutoff.
    case active(daysLeft: Int)
    /// Trial nearing expiry — escalate the conversion prompt (calm → urgent).
    case nearExpiry(daysLeft: Int)
    /// Last day / hours of the trial — the most urgent, pre-charge prompt.
    case lastDay(hoursLeft: Int)
    /// No active tier and no live trial — a fresh not-entitled OR a lapsed
    /// trial/subscription. The gated re-subscribe surface (routes into U12).
    case lapsed

    /// Days below which the prompt escalates from calm to `.nearExpiry`.
    static let nearExpiryDayCutoff = 3

    /// Derive the lifecycle state from the resolved tier, an optional
    /// `trial_end` (epoch seconds), and whether the auth status is `stale`
    /// (offline — the KTD-4 grace case that must never read as expired). `now`
    /// is injectable for deterministic tests.
    ///
    /// Precedence: stale → `.indeterminate` (grace, never a spurious lockout);
    /// an entitled tier with no live `trial_end` → `.subscribed`; a live
    /// `trial_end` in the future → active/near-expiry/last-day by remaining time;
    /// everything else (no tier, or a `trial_end` already in the past) → `.lapsed`.
    static func from(
        tier: EntitlementTier,
        trialEnd: Int?,
        stale: Bool,
        now: Date = Date()
    ) -> TrialState {
        // KTD-4 / R7: an offline payer's token is stale with a nil tier — never
        // render "expired". The lease keeps the daemon grace-allowing.
        if stale { return .indeterminate }

        let entitled = tier != .none

        // A live trial: `trial_end` in the future. Its urgency is by remaining
        // time, independent of whether `tier` already reads entitled (a trialing
        // token carries the tier it will convert into, KTD-2/KTD-3).
        if let trialEnd {
            let remaining = Date(timeIntervalSince1970: TimeInterval(trialEnd))
                .timeIntervalSince(now)
            if remaining > 0 {
                let hoursLeft = Int(remaining / 3600)
                if hoursLeft < 24 {
                    return .lastDay(hoursLeft: max(hoursLeft, 0))
                }
                // Ceil to whole days so "1.4 days" reads as "2 days left".
                let daysLeft = Int((remaining / 86_400).rounded(.up))
                return daysLeft <= nearExpiryDayCutoff
                    ? .nearExpiry(daysLeft: daysLeft)
                    : .active(daysLeft: daysLeft)
            }
            // trial_end already passed: fall through — entitled means it
            // converted, otherwise it lapsed.
        }

        if entitled { return .subscribed }
        return .lapsed
    }
}
