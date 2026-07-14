import Foundation

// SCR-214 U12 — the runtime-control models for always-on ambient capture. These
// mirror the daemon's `/v0/ambient.status` (read) and `/v0/ambient.set` (mutate)
// verbs. Ambient is opt-in and off by default (R1); enabling it starts a single
// continuous per-day recording the user can pause/resume/stop and auto-start
// (R2). Everything ambient captures stays local — these verbs only move the
// enabled/autostart toggles and read the live supervision state.
//
// One Swift model decodes BOTH verbs: `ambient.set` echoes the same
// `AmbientStatusResponse` payload after applying the change (the daemon's
// `AmbientSetResponse` subclasses `AmbientStatusResponse`), so the app reads one
// shape whether it just polled status or just mutated it.

/// The app's runtime view of ambient supervision (`ambient.status` /
/// `ambient.set`). `enabled` / `autostart` are the persisted config toggles;
/// `active` is a live ambient recording; `paused` is that recording's CONFIRMED
/// pause state (written only by the engine's `recording_paused` /
/// `recording_resumed` event — never the optimistic request echo), `false` when
/// nothing ambient is live; `degraded` is a human-readable blocked/ceiling
/// reason (`nil` = healthy) so the app can show WHY an enabled ambient is not
/// recording rather than a silent on-with-nothing-captured toggle; `recording`
/// is the live ambient recording's name (so the app can target `recording.pause`
/// / `.resume` at it), or `nil` when nothing ambient is live.
struct AmbientStatus: Decodable, Sendable, Equatable {
    let ok: Bool
    let schemaVersion: Int
    let daemonVersion: String
    let apiSchemaVersion: Int
    let enabled: Bool
    let autostart: Bool
    let active: Bool
    let paused: Bool
    let degraded: String?
    let recording: String?

    init(
        ok: Bool = true,
        schemaVersion: Int = 1,
        daemonVersion: String = "test",
        apiSchemaVersion: Int = 1,
        enabled: Bool,
        autostart: Bool,
        active: Bool,
        paused: Bool,
        degraded: String? = nil,
        recording: String? = nil
    ) {
        self.ok = ok
        self.schemaVersion = schemaVersion
        self.daemonVersion = daemonVersion
        self.apiSchemaVersion = apiSchemaVersion
        self.enabled = enabled
        self.autostart = autostart
        self.active = active
        self.paused = paused
        self.degraded = degraded
        self.recording = recording
    }

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case daemonVersion = "daemon_version"
        case apiSchemaVersion = "api_schema_version"
        case enabled
        case autostart
        case active
        case paused
        case degraded
        case recording
    }
}

/// `ambient.set` input: toggle ambient (and/or its auto-start-on-login) at
/// runtime. Both fields are OPTIONAL — the synthesized `Encodable` omits a nil
/// key (via `encodeIfPresent`), so a request applies only the keys it carries
/// (the app can flip `autostart` without touching `enabled`, and vice versa).
/// `enabled == true` persists the opt-in and STARTS ambient now; `enabled ==
/// false` persists the opt-out and STOPS the running ambient recording now.
struct AmbientSetRequest: Encodable, Sendable {
    let enabled: Bool?
    let autostart: Bool?

    init(enabled: Bool? = nil, autostart: Bool? = nil) {
        self.enabled = enabled
        self.autostart = autostart
    }

    enum CodingKeys: String, CodingKey {
        case enabled
        case autostart
    }
}
