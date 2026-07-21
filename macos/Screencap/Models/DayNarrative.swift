import Foundation

// U8 (day diary) — the Swift READ model for the app-only `/v0/day.narrative`
// verb (U4). Mirrors the daemon's `DayNarrativeRequest` / `DayNarrativeResponse`
// (src/screencap/daemon/schema.py). Local-only, read-only (R4/R8, R14): the
// narrative prose stays on the Mac — deliberately NOT mirrored over MCP in v1.
//
// The narrative is EVIDENCE-BOUND (R9): a mechanical-only / nothing-to-name day
// writes no narrative row, so `narrative` is `nil`; a legacy / pre-U4 recording
// has no row either; and a locked/absent vault returns `nil` with a degraded
// `storeState`. The app renders the `nil` case gracefully — never a blank slot.
//
// Every field an older daemon might omit is decoded with `decodeIfPresent`, and
// the model carries a memberwise init so tests can construct fixtures without a
// live socket (the established `TasksListResponse` / `TasksQueryResponse` pattern).

/// `day.narrative` input: the recording whose day narrative to read. The daemon
/// validates `recording` with the canonical name validator (traversal-safe).
struct DayNarrativeRequest: Encodable, Sendable {
    let recording: String

    init(recording: String) {
        self.recording = recording
    }
}

/// `day.narrative` response. `narrative` is the sanitized prose, or `nil` when
/// the recording produced none (mechanical-only / nothing-to-name day, a legacy
/// recording, or a locked/absent vault). `reason` is the observable prose
/// source/fallback marker (KTD-10): `nil` when a model narrated, a marker string
/// on the honest heuristic degrade — carried through purely for observability.
/// `generatedAt` is the wall-clock write time (Unix seconds). `storeState`
/// carries the vault state (KTD-14): a locked / absent / error store returns a
/// null narrative + degraded state on a 200, never a 500. Absent on an older
/// daemon → `"mounted"`.
struct DayNarrativeResponse: Decodable, Sendable {
    let ok: Bool
    let schemaVersion: Int
    let daemonVersion: String
    let apiSchemaVersion: Int
    let recording: String
    let narrative: String?
    let generatedAt: Double?
    let reason: String?
    let storeState: String?

    init(
        ok: Bool = true,
        schemaVersion: Int = 1,
        daemonVersion: String = "test",
        apiSchemaVersion: Int = 1,
        recording: String,
        narrative: String? = nil,
        generatedAt: Double? = nil,
        reason: String? = nil,
        storeState: String? = "mounted"
    ) {
        self.ok = ok
        self.schemaVersion = schemaVersion
        self.daemonVersion = daemonVersion
        self.apiSchemaVersion = apiSchemaVersion
        self.recording = recording
        self.narrative = narrative
        self.generatedAt = generatedAt
        self.reason = reason
        self.storeState = storeState
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = try c.decode(Bool.self, forKey: .ok)
        schemaVersion = try c.decode(Int.self, forKey: .schemaVersion)
        daemonVersion = try c.decode(String.self, forKey: .daemonVersion)
        apiSchemaVersion = try c.decode(Int.self, forKey: .apiSchemaVersion)
        recording = try c.decode(String.self, forKey: .recording)
        narrative = try c.decodeIfPresent(String.self, forKey: .narrative)
        generatedAt = try c.decodeIfPresent(Double.self, forKey: .generatedAt)
        reason = try c.decodeIfPresent(String.self, forKey: .reason)
        storeState = try c.decodeIfPresent(String.self, forKey: .storeState)
    }

    /// The typed store state, tolerant of an older daemon that omits the field.
    var resolvedStoreState: StoreState { StoreState.from(state: storeState) }

    /// The sanitized narrative text when present and non-blank — a
    /// present-but-whitespace narrative is treated as no narrative so it never
    /// renders as an empty section.
    var text: String? {
        guard let t = narrative?.trimmingCharacters(in: .whitespacesAndNewlines),
              !t.isEmpty else { return nil }
        return t
    }

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
        case recording
        case narrative
        case generatedAt = "generated_at"
        case reason
        case storeState = "store_state"
    }
}

/// The resolved presentation state for the day view's narrative section (R8/R9).
/// The three cases are DISTINCT so a thin/mechanical day never shows a blank slot
/// that reads as "nothing to say":
/// - `.narrative` — at least one recording narrated; the composed prose is shown.
/// - `.stillComposing` — real diary blocks exist but no narrative is generated
///   yet (the live day is being written); a quiet "still composing" placeholder.
/// - `.hidden` — nothing to say (no blocks, mechanical-only, absent/legacy): the
///   narrative section is omitted entirely, deferring to the strip's own honest
///   states.
enum DayNarrativeState: Equatable {
    case narrative(text: String)
    case stillComposing
    case hidden
}

/// Pure composition of a day's per-recording narratives into ONE section state,
/// factored out of the view so the gating rule is directly unit-testable.
enum DayNarrativeComposition {
    /// Compose the section state from the per-recording narrative responses and
    /// whether the day carries real diary blocks.
    ///
    /// - A day with any non-blank narrative composes a PARTIAL narrative covering
    ///   only the recordings that produced one (KTD-10) — joined in the given
    ///   order, so a mixed day (one produced-tasks recording + one mechanical)
    ///   still reads the real half.
    /// - No narrative anywhere, but real blocks exist → `.stillComposing` (the
    ///   narrative row isn't generated yet — distinct from "intentionally none").
    /// - No narrative and no real blocks → `.hidden` (mechanical-only / absent /
    ///   legacy day: the section is dropped, not shown blank).
    ///
    /// - Parameters:
    ///   - responses: one `day.narrative` response per recording on the day.
    ///   - hasBlocks: whether the day has at least one real consolidated block
    ///     (a task row carrying a `block_id`, or the live open block).
    static func compose(
        responses: [DayNarrativeResponse],
        hasBlocks: Bool
    ) -> DayNarrativeState {
        let texts = responses.compactMap(\.text)
        if !texts.isEmpty {
            return .narrative(text: texts.joined(separator: "\n\n"))
        }
        return hasBlocks ? .stillComposing : .hidden
    }
}
