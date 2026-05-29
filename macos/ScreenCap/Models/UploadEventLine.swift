import Foundation

/// Decoded shape of one stderr line emitted by `screencap upload` (plan U1).
/// Mirrors `RecorderEventLine`'s drift-resilient contract: every field is
/// optional, unknown event types decode into `.unknown`, and the parser
/// returns nil for non-JSON or blank lines so Rich progress-bar bleed on
/// stdout doesn't poison the event stream.
///
/// Adding a new event field (or even a new event type) on the Python side
/// never requires a Swift update to keep parsing — only the consumer code
/// that wants to read the new field has to change.
struct UploadEventLine: Decodable, Equatable {
    let type: String
    let schemaVersion: Int?
    let ts: Double?

    // upload_started fields
    let totalBytes: Int?
    let fileCount: Int?
    let recording: String?

    // upload_file_done fields
    let name: String?
    let bytesUploadedSoFar: Int?
    let filesDone: Int?
    let filesTotal: Int?

    // upload_finished fields
    let uploaded: Int?
    let skipped: Int?
    let failed: Int?
    let gcsPrefix: String?

    // upload_failed fields
    let error: String?

    enum CodingKeys: String, CodingKey {
        case type
        case schemaVersion = "schema_version"
        case ts
        case totalBytes = "total_bytes"
        case fileCount = "file_count"
        case recording
        case name
        case bytesUploadedSoFar = "bytes_uploaded_so_far"
        case filesDone = "files_done"
        case filesTotal = "files_total"
        case uploaded
        case skipped
        case failed
        case gcsPrefix = "gcs_prefix"
        case error
    }

    /// Drift-resilient parser. Returns nil on blank lines, lines that aren't
    /// JSON objects, or lines that don't decode against the schema. The
    /// `type` field is required (an event with no type is undecidable);
    /// every other field is optional.
    static func parse(stderrLine: String) -> UploadEventLine? {
        let trimmed = stderrLine.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed.hasPrefix("{") else { return nil }
        guard let data = trimmed.data(using: .utf8) else { return nil }
        return try? JSONDecoder().decode(UploadEventLine.self, from: data)
    }
}
