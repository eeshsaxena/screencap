import Foundation

/// One row from `screencap apps --json` (schema v3 — see
/// `src/screencap/cli/__init__.py:apps`). Drives the per-app Privacy pane row.
///
/// `iconPath` is populated as `""` by the Python side by design — SwiftUI
/// loads icons via `NSWorkspace.shared.icon(forFile: path)` instead of
/// shipping bytes through the JSON envelope.
///
/// SCR-235 (schema v3) adds `allowConfirmed` (the entry is an authoritative
/// confirmed allow) and `confirmationRequired` (allowing this class needs the
/// explicit confirmation). Both decode leniently so a stale daemon that emits
/// schema v2 degrades to today's rendering instead of failing the whole
/// `apps` array decode (the documented stale-daemon-after-app-update skew):
/// `allowConfirmed` defaults to `false`, `confirmationRequired` falls back to
/// `isMatrixExclude` (the old locked-row driver).
struct InstalledApp: Decodable, Identifiable, Hashable {
    let bundleId: String
    let displayName: String
    let path: String
    let iconPath: String
    let contextClass: String
    let classificationSource: String
    let resolvedAction: String
    let inExcludeApps: Bool
    let inAllowApps: Bool
    let allowConfirmed: Bool
    let confirmationRequired: Bool
    let isMatrixExclude: Bool
    let hasPerFrameOverrides: Bool

    var id: String { bundleId }

    enum CodingKeys: String, CodingKey {
        case bundleId = "bundle_id"
        case displayName = "display_name"
        case path
        case iconPath = "icon_path"
        case contextClass = "context_class"
        case classificationSource = "classification_source"
        case resolvedAction = "resolved_action"
        case inExcludeApps = "in_exclude_apps"
        case inAllowApps = "in_allow_apps"
        case allowConfirmed = "allow_confirmed"
        case confirmationRequired = "confirmation_required"
        case isMatrixExclude = "is_matrix_exclude"
        case hasPerFrameOverrides = "has_per_frame_overrides"
    }
}

extension InstalledApp {
    // Custom decode lives in an extension so the memberwise initializer stays
    // synthesized for tests and previews.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        bundleId = try c.decode(String.self, forKey: .bundleId)
        displayName = try c.decode(String.self, forKey: .displayName)
        path = try c.decode(String.self, forKey: .path)
        iconPath = try c.decode(String.self, forKey: .iconPath)
        contextClass = try c.decode(String.self, forKey: .contextClass)
        classificationSource = try c.decode(String.self, forKey: .classificationSource)
        resolvedAction = try c.decode(String.self, forKey: .resolvedAction)
        inExcludeApps = try c.decode(Bool.self, forKey: .inExcludeApps)
        inAllowApps = try c.decode(Bool.self, forKey: .inAllowApps)
        isMatrixExclude = try c.decode(Bool.self, forKey: .isMatrixExclude)
        hasPerFrameOverrides = try c.decode(Bool.self, forKey: .hasPerFrameOverrides)
        // Schema v3 fields — lenient with stale-daemon defaults (SCR-235 KTD8).
        allowConfirmed = try c.decodeIfPresent(Bool.self, forKey: .allowConfirmed) ?? false
        confirmationRequired = try c.decodeIfPresent(Bool.self, forKey: .confirmationRequired)
            ?? (try c.decode(Bool.self, forKey: .isMatrixExclude))
    }
}

/// Envelope returned by `screencap apps --json`.
struct AppsEnvelope: Decodable {
    let ok: Bool
    let schemaVersion: Int
    let apps: [InstalledApp]
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case apps
        case error
    }
}
