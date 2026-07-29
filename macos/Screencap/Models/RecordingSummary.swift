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
    /// Always emitted by the CLI (`RecordingInfo.chunks_total: int = 0`);
    /// declared non-optional with a custom decode that defaults to 0 if the
    /// field is missing on older on-disk recordings.
    let chunksTotal: Int
    /// Same contract as `chunksTotal`.
    let chunksUploaded: Int
    let intent: String?
    let startedAt: Double?
    let durationSeconds: Double?
    /// Per-source dropped-event counts (e.g. `"screen_cadence_skip": 643`).
    /// Always emitted by the CLI but may be `null` for older recordings.
    /// Unit 13 uses `drops?.values.reduce(0, +) ?? 0 > 0` for the row's
    /// drop indicator.
    let drops: [String: Int]?

    // U2 (prototype UI) additive fields. All tolerate an older daemon that omits
    // them (nullable-timing contract): readiness is never gated on their presence.
    /// Numeric byte total behind `sizeMB` — the sidebar footer sums this.
    let sizeBytes: Int
    /// The recording's `task_description` — a short summary set via the CLI
    /// `--description` flag, or nil when absent / the DB was locked at scan time.
    let summary: String?
    /// Humanized display title derived from the directory name. Falls back to
    /// the raw directory `name` when an older daemon omits it.
    let title: String
    /// Editable titles (U2/U6): true only when `title` is a user-set rename
    /// (`recording.title`), false when it is the derived date/time default.
    /// Optional so an older daemon that omits the key decodes as nil — treated
    /// as not-user-set (the derived default), never an error (nullable-timing
    /// contract). The Rename affordance keys its no-op-on-unchanged-default rule
    /// on this so submitting an untouched default never freezes it as a title.
    let titleIsUserSet: Bool?
    /// Derived lifecycle: `recording` | `processing` | `ready` (KTD-7). Defaults
    /// to `ready` so a missing value never traps a card in a spinner.
    let state: String
    /// Stable identity pinned at recording start — survives any post-stop
    /// directory rename (unlike `name`). nil for legacy recordings.
    let recordingId: String?
    /// Frozen per-recording E2EE intent (KTD-4): true only when the recording
    /// was started with cloud E2EE on, so its uploads are ciphertext. Older
    /// daemons/CLIs omit it → nil, treated as not-encrypted — never an error
    /// state (nullable-timing contract).
    let cloudE2EE: Bool?

    var id: String { name }

    /// Identity that survives a post-stop directory rename. Consumers that must
    /// hold a card in place across a rename (U5's grid diffing, the draft card)
    /// key on this rather than `name`. Falls back to `name` for legacy recordings.
    var stableID: String { recordingId ?? name }

    var isActivelyRecording: Bool { state == "recording" }
    var isProcessing: Bool { state == "processing" }
    var isReady: Bool { state == "ready" }

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
        case drops
        case sizeBytes = "size_bytes"
        case summary
        case title
        case titleIsUserSet = "title_is_user_set"
        case state
        case recordingId = "recording_id"
        case cloudE2EE = "cloud_e2ee"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decode(String.self, forKey: .name)
        date = try c.decode(String.self, forKey: .date)
        duration = try c.decode(String.self, forKey: .duration)
        sizeMB = try c.decode(String.self, forKey: .sizeMB)
        hasAudio = try c.decode(Bool.self, forKey: .hasAudio)
        transcribed = try c.decode(Bool.self, forKey: .transcribed)
        uploaded = try c.decode(Bool.self, forKey: .uploaded)
        isStub = try c.decode(Bool.self, forKey: .isStub)
        chunksTotal = try c.decodeIfPresent(Int.self, forKey: .chunksTotal) ?? 0
        chunksUploaded = try c.decodeIfPresent(Int.self, forKey: .chunksUploaded) ?? 0
        intent = try c.decodeIfPresent(String.self, forKey: .intent)
        startedAt = try c.decodeIfPresent(Double.self, forKey: .startedAt)
        durationSeconds = try c.decodeIfPresent(Double.self, forKey: .durationSeconds)
        drops = try c.decodeIfPresent([String: Int].self, forKey: .drops)
        sizeBytes = try c.decodeIfPresent(Int.self, forKey: .sizeBytes) ?? 0
        summary = try c.decodeIfPresent(String.self, forKey: .summary)
        title = try c.decodeIfPresent(String.self, forKey: .title) ?? name
        titleIsUserSet = try c.decodeIfPresent(Bool.self, forKey: .titleIsUserSet)
        state = try c.decodeIfPresent(String.self, forKey: .state) ?? "ready"
        recordingId = try c.decodeIfPresent(String.self, forKey: .recordingId)
        cloudE2EE = try c.decodeIfPresent(Bool.self, forKey: .cloudE2EE)
    }

    /// Newest-first comparator. Recordings without a `startedAt` sort to the
    /// end (treated as 0). Used by the Library grid's chip filtering (U5).
    static func newestFirst(_ a: RecordingSummary, _ b: RecordingSummary) -> Bool {
        (a.startedAt ?? 0) > (b.startedAt ?? 0)
    }

    /// Plan R2 eligibility predicate (U4): a recording can be uploaded from
    /// the review window only when it has not already been uploaded and is
    /// not a stub (uploaded + locally-deleted). Keeps the rule colocated with
    /// the model so the row view and any other future consumer share one
    /// source of truth.
    var isUploadEligible: Bool {
        !uploaded && !isStub
    }

    /// SCR-219 R8 / KTD5 eligibility predicate for "Clip this moment" (U5):
    /// a recording is clippable when it is **local, non-stub, and has video
    /// present**. A clip reads the recording's LOCAL source chunks and writes a
    /// local `.mp4` (KD1), so the media must still be on this Mac (`!isStub` —
    /// a stub is the uploaded-then-locally-deleted state) and the recording
    /// must carry actual captured footage (`durationSeconds > 0`; a screen
    /// recording always has a video stream when it has footage, and this proxy
    /// covers both chunked and legacy single-`video.mp4` recordings, unlike
    /// `chunksTotal`).
    ///
    /// Deliberately distinct from `isUploadEligible`: it **drops the
    /// `!uploaded` clause**. An already-uploaded recording is still clippable
    /// from its local chunks (the clip is a fresh local file, not a re-upload),
    /// so reusing the upload-only predicate would wrongly disable the button on
    /// every shared recording. Contention with a live recording (chunks
    /// mid-write) is not gated here — the engine `screencap clip` verb (U3)
    /// acquires the per-recording `terminal_lock` and reports `clip_busy`, so
    /// the lock, not this UI predicate, is the write-safety boundary.
    var isClippable: Bool {
        !isStub && (durationSeconds ?? 0) > 0
    }

    /// SCR-299 R11 / KTD3 eligibility predicate for the Inspect share-link
    /// actions: a recording can be shared by link once it has a **cloud copy**.
    ///
    /// A share is built from that cloud copy — the daemon fetches the masked
    /// `<name>-scrubbed` artifacts from storage when no local scrubbed sibling
    /// survives — so being uploaded is the whole requirement.
    ///
    /// Deliberately distinct from both neighbours above. It is the **inverse**
    /// of `isUploadEligible`'s `!uploaded` clause: you can only share what is
    /// already in the cloud. And unlike `isClippable` it must **not** exclude a
    /// stub — a stub is the uploaded-then-locally-deleted state, and its cloud
    /// copy is exactly what a share reads, so excluding it would disable
    /// sharing on the recordings the backend most clearly supports. (In
    /// practice a stub rarely reaches the Inspect menu, because inspect-data
    /// needs local video to load; that gate is the view's, not this predicate's.)
    var isShareable: Bool {
        uploaded
    }
}
