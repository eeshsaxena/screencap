import Foundation

// SCR-178 — typed Swift models for the content-index backfill lifecycle verbs
// (`/v0/backfill.start`, `/v0/backfill.status`, `/v0/backfill.cancel`) and the
// `backfill.*` progress events on `/v0/events`.
//
// PRIVACY (R9): every model here is structurally pointer-/count-only. The
// progress surface carries the run state, the frozen-denominator counts
// (`done`/`skipped`/`failed`/`total`), and an OPAQUE ordinal
// (`currentUnitIndex`) — and NOTHING ELSE. No recording directory name ever
// crosses this boundary: the daemon omits it from every payload because the
// EventBus is readable by any same-EUID subscriber (incl. the MCP `/v0/events`
// stream), and a dir name encodes timing/context. The Swift models mirror that
// by simply having no such property.

/// The backfill run state, mirrored from the daemon's `BackfillStatusResponse`.
/// Decoded tolerantly via `init(wire:)`: an unknown or missing token defaults
/// to `.idle` (the neutral "nothing in flight" state — never a misleading
/// `running`/`failed`) so a future run-state value from a newer daemon never
/// bricks the parser. Mirrors `ContentIndexState(wire:)` /
/// `DaemonGrantState(wire:)`.
enum BackfillState: String, Sendable, Equatable {
    case idle
    case running
    case completed
    case paused
    case cancelled
    case failed

    init(wire: String?) {
        self = BackfillState(rawValue: wire ?? "") ?? .idle
    }
}

/// The privacy-safe backfill status snapshot. Count-only + opaque ordinal —
/// no recording directory name (R9). `state` applies tolerant defaulting via
/// `BackfillState(wire:)` over the raw wire value, while preserving the raw
/// string for forward-compat diagnostics.
struct BackfillStatus: Codable, Equatable, Sendable {
    /// Raw wire token (e.g. `"running"`). Kept verbatim so an unrecognized
    /// future state is still observable for diagnostics; `state` is the typed,
    /// tolerant interpretation.
    let stateRaw: String
    let done: Int
    let skipped: Int
    let failed: Int
    let total: Int
    let currentUnitIndex: Int

    var state: BackfillState { BackfillState(wire: stateRaw) }

    init(
        state: BackfillState,
        done: Int = 0,
        skipped: Int = 0,
        failed: Int = 0,
        total: Int = 0,
        currentUnitIndex: Int = 0
    ) {
        self.stateRaw = state.rawValue
        self.done = done
        self.skipped = skipped
        self.failed = failed
        self.total = total
        self.currentUnitIndex = currentUnitIndex
    }

    enum CodingKeys: String, CodingKey {
        case stateRaw = "state"
        case done
        case skipped
        case failed
        case total
        case currentUnitIndex = "current_unit_index"
    }
}

/// Response envelope for `backfill.start` / `backfill.status` / `backfill.cancel`.
/// Combines the standard envelope fields with the inline `BackfillStatus`
/// payload (the daemon flattens the status fields onto the envelope, so the
/// payload counts decode off the same top-level object — mirroring how
/// `ListResponse`/`SessionSnapshotResponse` inline their domain fields).
struct BackfillStatusResponse: Decodable, Sendable {
    let ok: Bool
    let schemaVersion: Int
    let daemonVersion: String
    let apiSchemaVersion: Int
    let status: BackfillStatus

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = try c.decode(Bool.self, forKey: .ok)
        schemaVersion = try c.decode(Int.self, forKey: .schemaVersion)
        daemonVersion = try c.decode(String.self, forKey: .daemonVersion)
        apiSchemaVersion = try c.decode(Int.self, forKey: .apiSchemaVersion)
        // The status fields are flattened onto the envelope; decode the inline
        // payload from the same top-level container.
        status = try BackfillStatus(from: decoder)
    }

    init(
        ok: Bool = true,
        schemaVersion: Int = 1,
        daemonVersion: String = "test",
        apiSchemaVersion: Int = 1,
        status: BackfillStatus
    ) {
        self.ok = ok
        self.schemaVersion = schemaVersion
        self.daemonVersion = daemonVersion
        self.apiSchemaVersion = apiSchemaVersion
        self.status = status
    }

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
    }
}

/// A single `backfill.*` progress event from `/v0/events`. The `type` is one of
/// `backfill.progress` / `backfill.completed` / `backfill.paused` /
/// `backfill.cancelled` / `backfill.failed`; the remaining fields mirror
/// `BackfillStatus` (count-only + opaque ordinal — no recording name, R9).
///
/// Decoded from a `RecorderEventLine`-shaped flat JSON line on the subscribe
/// stream; `DaemonClient.backfillProgressEvent(from:)` is the seam that turns a
/// streamed line into one of these, returning nil for non-`backfill.*` types.
struct BackfillProgressEvent: Decodable, Equatable, Sendable {
    let type: String
    let stateRaw: String
    let done: Int
    let skipped: Int
    let failed: Int
    let total: Int
    let currentUnitIndex: Int

    var state: BackfillState { BackfillState(wire: stateRaw) }

    /// True for any of the terminal event types (the UI announces these once;
    /// `backfill.progress` ticks do not announce — see U8).
    var isTerminal: Bool {
        switch type {
        case "backfill.completed", "backfill.paused",
             "backfill.cancelled", "backfill.failed":
            return true
        default:
            return false
        }
    }

    enum CodingKeys: String, CodingKey {
        case type
        case stateRaw = "state"
        case done
        case skipped
        case failed
        case total
        case currentUnitIndex = "current_unit_index"
    }
}
