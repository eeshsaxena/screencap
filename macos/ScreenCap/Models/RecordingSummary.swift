import Foundation

/// One row from `screencap list --json` (Unit 4 schema). Optional fields
/// tolerate older recordings on disk that were created before the Unit 4
/// schema extension landed.
struct RecordingSummary: Decodable, Identifiable, Hashable {
    let name: String
    let date: String
    let duration: String
    let sizeMB: String
    let hasAudio: Bool
    let transcribed: Bool
    let uploaded: Bool
    let isStub: Bool
    let chunksTotal: Int?
    let chunksUploaded: Int?
    let intent: String?
    let startedAt: Double?
    let durationSeconds: Double?

    var id: String { name }

    /// Local-timezone calendar day derived from `startedAt`. Falls back to the
    /// legacy `date` string ("YYYY-MM-DD") if `startedAt` is missing.
    var startedDay: Date? {
        if let ts = startedAt {
            let d = Date(timeIntervalSince1970: ts)
            return Calendar.current.startOfDay(for: d)
        }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd"
        formatter.timeZone = .current
        return formatter.date(from: date).map { Calendar.current.startOfDay(for: $0) }
    }

    /// HH:mm in the local timezone, derived from `startedAt`. Returns "—" if
    /// the timestamp is missing.
    var startedTimeOfDay: String {
        guard let ts = startedAt else { return "—" }
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm"
        formatter.timeZone = .current
        return formatter.string(from: Date(timeIntervalSince1970: ts))
    }

    enum CodingKeys: String, CodingKey {
        case name
        case date
        case duration
        case sizeMB = "size_mb"
        case hasAudio = "has_audio"
        case transcribed
        case uploaded
        case isStub = "is_stub"
        case chunksTotal = "chunks_total"
        case chunksUploaded = "chunks_uploaded"
        case intent
        case startedAt = "started_at"
        case durationSeconds = "duration_seconds"
    }

    /// Newest-first comparator. Recordings without a `startedAt` sort to the
    /// end (treated as 0). Used by `RecordingsIndex` and `RecordingsListView`.
    static func newestFirst(_ a: RecordingSummary, _ b: RecordingSummary) -> Bool {
        (a.startedAt ?? 0) > (b.startedAt ?? 0)
    }
}
