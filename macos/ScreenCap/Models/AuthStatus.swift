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
    /// omits it decodes fine and resolves to "not subscribed".
    let subscribed: Bool?
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
