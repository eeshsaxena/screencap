import Foundation

/// The encrypted on-disk store's serving state (SCR-258 U4/U10, KTD-14/KTD-20).
///
/// Modelled as an **independent value** the read surfaces branch on BEFORE their
/// `isEmpty` / no-results check — NOT a load-error kind. The daemon `recording.list`
/// verb (and its CLI `list --json` fallback) *succeeds* with an empty payload for a
/// locked / absent / key-missing store, so an error-kind model would never fire and
/// the Library would fall through to the "Nothing recorded yet" welcome screen (the
/// R6 data-loss look). `.mounted` is the ordinary, healthy state (plaintext install
/// or an attached container); every other case is a first-class UI state.
///
/// Decoding is deliberately tolerant: an unknown / missing wire string maps to
/// `.mounted` (never a scary state), mirroring the fail-open decode of
/// `DaemonGrantState`.
enum StoreState: Equatable {
    /// The store is available — a plaintext install, or an attached encrypted
    /// container. Recordings read normally.
    case mounted
    /// A sealed sentinel is present: the store is ciphertext-sealed and requires a
    /// present-user (Touch ID) unlock. The Library shows a first-class locked state
    /// with an Unlock affordance.
    case locked
    /// The container is enabled but no bundle exists yet (fresh install / pre-init).
    /// The surface shows onboarding guidance pointing at `storage init`.
    case absent
    /// The store exists but cannot be served — see `StoreErrorReason` for the cause.
    /// Never a plaintext fallback (R10).
    case error(reason: StoreErrorReason)

    /// Build from the wire `store_state` string plus an optional `store_reason`
    /// (only meaningful for `error`). Tolerant: unknown / nil → `.mounted`.
    static func from(state: String?, reason: String? = nil) -> StoreState {
        switch state {
        case "locked":
            return .locked
        case "absent":
            return .absent
        case "error":
            return .error(reason: StoreErrorReason(wire: reason))
        default:
            // "mounted", nil, "", or any future/unknown token → treat as the
            // healthy mounted state so a decode surprise never blanks the library.
            return .mounted
        }
    }

    var isMounted: Bool {
        if case .mounted = self { return true }
        return false
    }
}

/// The `store_state=error` sub-cause (KTD-14/KTD-22). Distinguishes an
/// unrecoverable key loss from a reachable-but-not-by-this-binary key, a locked
/// Keychain, and the downgrade-unsupported refusal — so the app renders accurate,
/// non-alarming guidance (R10) rather than a bare "error".
enum StoreErrorReason: Equatable {
    /// No key exists in any channel: the recordings are unrecoverable.
    case keyMissing
    /// The key exists in the shared access group but this binary isn't entitled to
    /// read it (KTD-22) — an install/identity problem, not data loss.
    case entitlementMismatch
    /// The login Keychain is locked; a later attempt after unlocking the Mac may
    /// succeed (retryable).
    case keychainLocked
    /// `container_enabled=false` on a bundle-present install (KTD-19) — a plaintext
    /// downgrade is refused, not silently performed.
    case downgradeUnsupported
    /// An unrecognized / future reason token.
    case unknown

    init(wire: String?) {
        switch wire {
        case "key_missing": self = .keyMissing
        case "entitlement_mismatch": self = .entitlementMismatch
        case "keychain_locked": self = .keychainLocked
        case "downgrade_unsupported": self = .downgradeUnsupported
        default: self = .unknown
        }
    }

    /// Whether re-checking the store could plausibly resolve the error within the
    /// same running binary. Only a locked Keychain (unlock the Mac, retry) and an
    /// unknown cause offer a Retry; the rest are terminal for this install.
    var isRetryable: Bool {
        switch self {
        case .keychainLocked, .unknown: return true
        case .keyMissing, .entitlementMismatch, .downgradeUnsupported: return false
        }
    }
}

/// The two shapes `screencap list --json` now emits (SCR-258 U5/U10):
///
///  - a BARE ARRAY `[{...recording...}]` for the ordinary mounted / plaintext case
///    (unchanged, back-compat);
///  - a TYPED OBJECT `{"store_state":"locked","recordings":[]}` for a non-mounted
///    state, where a bare `[]` would be indistinguishable from an empty library.
///
/// Decoding tries the array first (the common path, byte-identical to today), then
/// falls back to the typed object. A pure value type so the two-shape decode is
/// unit-testable from raw fixtures (mirrors `RecordingSummaryDecodeTests`).
struct StoreListPayload: Equatable {
    let storeState: StoreState
    let recordings: [RecordingSummary]

    /// The typed-object shape. `store_reason` is not part of `list --json` today
    /// (only `daemon.info` carries it), so an `error` state decoded here has an
    /// `unknown` reason unless enriched by a `daemon.info` follow-up.
    private struct Envelope: Decodable {
        let storeState: String?
        let storeReason: String?
        let recordings: [RecordingSummary]

        enum CodingKeys: String, CodingKey {
            case storeState = "store_state"
            case storeReason = "store_reason"
            case recordings
        }
    }

    static func decode(_ data: Data) throws -> StoreListPayload {
        let decoder = JSONDecoder()
        // Common path: the bare array a mounted / plaintext store emits.
        if let rows = try? decoder.decode([RecordingSummary].self, from: data) {
            return StoreListPayload(storeState: .mounted, recordings: rows)
        }
        // Non-mounted path: the typed envelope. Let a genuine malformed payload
        // throw so the caller surfaces a real load error (not a fake locked state).
        let env = try decoder.decode(Envelope.self, from: data)
        return StoreListPayload(
            storeState: StoreState.from(state: env.storeState, reason: env.storeReason),
            recordings: env.recordings
        )
    }
}
