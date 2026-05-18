import Foundation

/// One row from `screencap apps --json` (schema v2 — see
/// `src/screencap/cli/__init__.py:apps`). Drives the per-app Privacy pane row.
///
/// `iconPath` is populated as `""` by the Python side by design — SwiftUI
/// loads icons via `NSWorkspace.shared.icon(forFile: path)` instead of
/// shipping bytes through the JSON envelope.
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
        case isMatrixExclude = "is_matrix_exclude"
        case hasPerFrameOverrides = "has_per_frame_overrides"
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
