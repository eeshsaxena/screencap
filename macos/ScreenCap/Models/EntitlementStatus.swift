import Foundation

/// The cloud entitlement envelope the app reads from `/v0/auth.entitlements`
/// (U3) or `screencap cloud entitlements --json` (U7). Drift-tolerant: every
/// contract-optional field decodes as optional so a null / absent value never
/// fails the parse or gates UI readiness (KTD7; the nullable-JSON-contract
/// learning). Mirrors `auth.get_entitlements` / `AuthEntitlementsResponse`.
struct AuthEntitlementsEnvelope: Decodable, Equatable {
    let ok: Bool?
    let schemaVersion: Int?
    let plan: String?
    let active: Bool?
    /// Always null in v1 (populated when billing adds lapse). Optional so a JSON
    /// `null` decodes cleanly rather than failing the whole envelope.
    let expires: Double?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case plan
        case active
        case expires
        case error
    }

    /// Drift-resilient parse. Returns nil on empty / non-object / non-decoding
    /// data so a stray banner line on stdout can't be mistaken for an
    /// entitlement envelope (mirrors `AuthWhoAmIEnvelope.parse`).
    static func parse(_ data: Data) -> AuthEntitlementsEnvelope? {
        guard !data.isEmpty else { return nil }
        return try? JSONDecoder().decode(AuthEntitlementsEnvelope.self, from: data)
    }
}

/// The resolved cloud entitlement the app UI reads (U9). A conservative,
/// fail-open model: an unparseable / errored / absent envelope resolves to
/// `.free` (never a failed state that would gate readiness). The Cloud Function
/// remains the authoritative upload gate — this only drives what plan status the
/// app shows and whether it offers "set up cloud".
enum EntitlementStatus: Equatable {
    /// Not yet fetched — the launch-time entitlements read has not returned.
    case unknown
    /// Signed out or no active plan.
    case free
    /// On an active cloud plan (founding or, later, paid). `plan` is the raw tier.
    case active(plan: String)

    /// Map a decoded envelope to a status. A nil envelope, `ok == false`, or an
    /// inactive/absent plan all resolve to `.free`; only an explicitly active
    /// plan yields `.active`.
    static func from(envelope: AuthEntitlementsEnvelope?) -> EntitlementStatus {
        guard let env = envelope, env.ok != false else { return .free }
        if env.active == true {
            return .active(plan: env.plan ?? "founding")
        }
        return .free
    }

    /// True when the account holds an active cloud plan (authorized to upload).
    var isActive: Bool {
        if case .active = self { return true }
        return false
    }

    /// The raw plan tier for display ("free" / "founding" / …).
    var planLabel: String {
        switch self {
        case .unknown: return "…"
        case .free: return "free"
        case .active(let plan): return plan
        }
    }
}
