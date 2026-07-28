import AVFoundation
import Foundation
import SQLite3

// U9 — day playback across chunked video and multiple recordings (KTD-12).
// The seek map is built client-side from each recording's
// `chunk_NNNN_manifest.json` files + the screenshots frame index (the same
// direct-read pattern as ThumbnailLoader / RecordingFrameIndex — no new daemon
// verb). One AVPlayer; `replaceCurrentItem` on chunk/recording boundary
// crossings; gaps and missing media resolve to placeholder states, never errors.
//
// The pure pieces (manifest → chunk building, day-time → target resolution) are
// file-scope value types so DayPlaybackEngineTests exercises the seek math over
// fake manifests without AVFoundation or disk.

/// One parsed `chunk_NNNN_manifest.json`, reduced to what seek needs. Manifest
/// `chunk_start` / `chunk_end` are epoch **seconds** on disk; this type carries
/// epoch ms like every other UI time.
struct DayChunkManifest: Equatable {
    let index: Int
    let startMs: Int
    let endMs: Int

    /// Decode a manifest's JSON bytes (tolerating unknown fields / versions);
    /// nil when the required keys are missing — the chunk is then skipped,
    /// resolving as a neutral placeholder rather than an error.
    static func decode(_ data: Data) -> DayChunkManifest? {
        struct Wire: Decodable {
            let chunkIndex: Int
            let chunkStart: Double
            let chunkEnd: Double
            enum CodingKeys: String, CodingKey {
                case chunkIndex = "chunk_index"
                case chunkStart = "chunk_start"
                case chunkEnd = "chunk_end"
            }
        }
        guard let wire = try? JSONDecoder().decode(Wire.self, from: data) else { return nil }
        return DayChunkManifest(
            index: wire.chunkIndex,
            startMs: Int((wire.chunkStart * 1000).rounded()),
            endMs: Int((wire.chunkEnd * 1000).rounded())
        )
    }
}

/// A playable (or provably unplayable) slice of the day: one chunk of one
/// recording in wall-clock coordinates.
struct DayPlayableChunk: Equatable {
    let recording: String
    /// The chunk's mp4, or nil when the media is gone (evicted after upload).
    let fileURL: URL?
    let startMs: Int
    let endMs: Int
    /// Wall-clock time of the chunk's **first written frame** — the mp4's PTS
    /// origin. Under action-gated capture this is later than `startMs`
    /// (`offset = t − chunk_start` mis-seeks on idle-starting chunks, KTD-12).
    /// nil = frame-less chunk (nothing captured; placeholder).
    let anchorMs: Int?
}

/// Why a day instant has no playable media (design: neutral placeholder pane).
enum DayPlaceholderReason: Equatable {
    /// No recording covered this instant, or the covering chunk captured no
    /// frames.
    case nothingCaptured
    /// A recording covered it but its local media was evicted after upload —
    /// "nothing captured" would be a false claim here (R7).
    case mediaUnavailable
}

/// Where a day-time seek lands.
enum DaySeekTarget: Equatable {
    case media(chunk: DayPlayableChunk, offsetSeconds: Double)
    case placeholder(DayPlaceholderReason)
}

/// Pure chunk-map building + seek resolution (the unit-tested core).
enum DayMediaMap {

    /// Build the playable chunks for one chunked recording. `framesMs` is the
    /// recording's ascending screenshots frame list; each chunk's PTS anchor is
    /// its first frame within `[startMs, endMs)`.
    static func chunks(
        recording: String,
        manifests: [DayChunkManifest],
        videoURL: (Int) -> URL?,
        framesMs: [Int]
    ) -> [DayPlayableChunk] {
        manifests
            .sorted { $0.startMs < $1.startMs }
            .map { manifest in
                DayPlayableChunk(
                    recording: recording,
                    fileURL: videoURL(manifest.index),
                    startMs: manifest.startMs,
                    endMs: manifest.endMs,
                    anchorMs: framesMs.first { $0 >= manifest.startMs && $0 < manifest.endMs }
                )
            }
    }

    /// A legacy single-`video.mp4` recording seeks as one chunk anchored at
    /// `recording.db`'s `video_start_time` (KTD-12). nil when the span can't be
    /// placed on the day at all.
    static func legacyChunk(
        recording: String,
        videoURL: URL?,
        videoStartMs: Int?,
        durationMs: Int?
    ) -> DayPlayableChunk? {
        guard let start = videoStartMs, let duration = durationMs, duration > 0 else { return nil }
        return DayPlayableChunk(
            recording: recording,
            fileURL: videoURL,
            startMs: start,
            endMs: start + duration,
            anchorMs: start
        )
    }

    /// Resolve a day instant to a seek target over the (sorted or unsorted)
    /// chunk list. Chunk windows are half-open `[startMs, endMs)`, so a seek at
    /// an exact chunk boundary lands in the *next* chunk at offset 0. The
    /// offset is anchored at the chunk's first written frame, clamped ≥ 0 for
    /// instants inside the chunk's leading idle stretch.
    static func target(atMs t: Int, in chunks: [DayPlayableChunk]) -> DaySeekTarget {
        guard let chunk = chunks
            .filter({ $0.startMs <= t && t < $0.endMs })
            .min(by: { $0.startMs < $1.startMs })
        else {
            return .placeholder(.nothingCaptured)
        }
        guard let fileURL = chunk.fileURL else { return .placeholder(.mediaUnavailable) }
        guard let anchor = chunk.anchorMs else { return .placeholder(.nothingCaptured) }
        return .media(chunk: chunk, offsetSeconds: max(0, Double(t - anchor) / 1000))
    }

    /// The next playable chunk strictly after `ms` — auto-advance when playback
    /// drains a chunk.
    static func nextChunk(afterMs ms: Int, in chunks: [DayPlayableChunk]) -> DayPlayableChunk? {
        chunks
            .filter { $0.startMs >= ms && $0.fileURL != nil && $0.anchorMs != nil }
            .min { $0.startMs < $1.startMs }
    }
}

/// Reads the on-disk pieces the map is built from. Static + root-parameterized
/// so tests run it over fixture directories.
enum DayMediaLoader {

    /// Build the playable chunks for a recording directory: chunk manifests +
    /// mp4s when chunked, the `video.mp4` + `recording.db` anchor when legacy.
    /// `startedAtMs` / `durationMs` come from the catalog row and back-stop a
    /// legacy recording whose `recording.db` is unreadable.
    static func loadChunks(
        root: URL,
        recording: String,
        startedAtMs: Int?,
        durationMs: Int?
    ) -> [DayPlayableChunk] {
        guard let dir = recordingDir(root: root, recording: recording) else { return [] }
        let fm = FileManager.default
        let entries = (try? fm.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)) ?? []

        let manifests = entries
            .filter { $0.lastPathComponent.hasPrefix("chunk_") && $0.lastPathComponent.hasSuffix("_manifest.json") }
            .compactMap { url -> DayChunkManifest? in
                guard let data = try? Data(contentsOf: url) else { return nil }
                return DayChunkManifest.decode(data)
            }

        if manifests.isEmpty {
            let videoURL = dir.appendingPathComponent("video.mp4")
            let exists = fm.fileExists(atPath: videoURL.path)
            let anchorMs = videoStartMs(dbURL: dir.appendingPathComponent("recording.db")) ?? startedAtMs
            return DayMediaMap.legacyChunk(
                recording: recording,
                videoURL: exists ? videoURL : nil,
                videoStartMs: anchorMs,
                durationMs: durationMs
            ).map { [$0] } ?? []
        }

        // Frame anchors (KTD-12): the flat `screenshots/*.jpg` index when it
        // exists, else the `screenshot` table timestamps in `recording.db` —
        // recordings without the flat export (scrub off) still carry every
        // captured frame's wall-clock time there.
        var framesMs = RecordingFrameIndex.loadFrames(root: root, recording: recording).map(\.ms)
        if framesMs.isEmpty {
            framesMs = screenshotTimestampsMs(dbURL: dir.appendingPathComponent("recording.db"))
        }
        return DayMediaMap.chunks(
            recording: recording,
            manifests: manifests,
            videoURL: { index in
                let url = dir.appendingPathComponent(String(format: "chunk_%04d.mp4", index))
                return fm.fileExists(atPath: url.path) ? url : nil
            },
            framesMs: framesMs
        )
    }

    /// Captured-frame wall-clock times (epoch ms, ascending) from the
    /// `screenshot` table — the anchor source when the flat screenshots dir is
    /// empty. Read-only; any failure returns [] and the affected chunks resolve
    /// to the neutral placeholder (fail-open, never an error).
    static func screenshotTimestampsMs(dbURL: URL) -> [Int] {
        var db: OpaquePointer?
        guard sqlite3_open_v2(dbURL.path, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK, let db else {
            sqlite3_close(db)
            return []
        }
        defer { sqlite3_close(db) }
        var stmt: OpaquePointer?
        let sql = "SELECT timestamp FROM screenshot WHERE timestamp IS NOT NULL ORDER BY timestamp"
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK, let stmt else { return [] }
        defer { sqlite3_finalize(stmt) }
        var out: [Int] = []
        while sqlite3_step(stmt) == SQLITE_ROW {
            let seconds = sqlite3_column_double(stmt, 0)
            if seconds > 0 { out.append(Int((seconds * 1000).rounded())) }
        }
        return out
    }

    /// `video_start_time` (fallback: `timestamp`) from a recording.db, in epoch
    /// ms — the legacy single-video PTS anchor (KTD-12). Read-only open; any
    /// failure (locked, corrupt, pre-`video_start_time` schema) returns nil and
    /// the caller falls back to the catalog's `started_at`.
    static func videoStartMs(dbURL: URL) -> Int? {
        var db: OpaquePointer?
        guard sqlite3_open_v2(dbURL.path, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK, let db else {
            sqlite3_close(db)
            return nil
        }
        defer { sqlite3_close(db) }
        var stmt: OpaquePointer?
        let sql = "SELECT video_start_time, timestamp FROM recording LIMIT 1"
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK, let stmt else { return nil }
        defer { sqlite3_finalize(stmt) }
        guard sqlite3_step(stmt) == SQLITE_ROW else { return nil }
        func column(_ i: Int32) -> Double? {
            sqlite3_column_type(stmt, i) == SQLITE_NULL ? nil : sqlite3_column_double(stmt, i)
        }
        guard let seconds = column(0) ?? column(1), seconds > 0 else { return nil }
        return Int((seconds * 1000).rounded())
    }

    /// Path-contained recording dir (the Swift-side `resolve_recording_dir`
    /// analogue, mirroring `RecordingFrameIndex.screenshotsDir`).
    static func recordingDir(root: URL, recording: String) -> URL? {
        guard !recording.isEmpty,
              !recording.contains("/"),
              !recording.contains("\\"),
              recording != ".",
              recording != ".." else { return nil }
        let dir = root.appendingPathComponent(recording, isDirectory: true).resolvingSymlinksInPath()
        let rootResolved = root.resolvingSymlinksInPath()
        guard dir.path.hasPrefix(rootResolved.path + "/") else { return nil }
        return dir
    }
}

/// The day's playback controller: one AVPlayer, item-swapped across
/// chunk/recording boundaries (KTD-12). Thin by design — the seek math lives in
/// `DayMediaMap`; this class owns only AVPlayer lifecycle and published state.
@MainActor
final class DayPlaybackEngine: ObservableObject {
    /// What the playback pane shows right now.
    @Published private(set) var target: DaySeekTarget = .placeholder(.nothingCaptured)
    /// The playhead's wall-clock position (epoch ms), driven by seeks and the
    /// periodic time observer while media plays.
    @Published private(set) var currentDayMs: Int?

    /// SCR-297 — the aspect ratio of the footage under the playhead, or nil
    /// when it is not (yet) known. `DayTimelineView.playbackPane` gives itself
    /// this shape so the video stops pillarboxing inside an ill-fitting pane
    /// and the pane's overlay chrome lands on the video rather than on black.
    ///
    /// nil is a first-class, safe state, not an error: it is what the item
    /// reports before `.readyToPlay`, and it renders exactly as the pane did
    /// before this existed. See `PlaybackAspect.resolve`.
    @Published private(set) var sourceAspect: CGFloat?

    let player = AVPlayer()
    private(set) var chunks: [DayPlayableChunk] = []
    private var currentChunk: DayPlayableChunk?
    private var timeObserverToken: Any?
    private var endObserver: NSObjectProtocol?
    private var presentationSizeObservation: NSKeyValueObservation?

    /// Resolved aspect per recording. Chunks of one recording captured the same
    /// display, so caching here turns the brief unresolved window into a
    /// once-per-recording event instead of once-per-chunk — without it the pane
    /// visibly pops to full width at every chunk boundary crossing. Mirrors
    /// `tasksCache` below.
    private var aspectCache: [String: CGFloat] = [:]

    /// The recording under the playhead (for "Share from here").
    var currentRecording: String? { currentChunk?.recording }

    /// SCR-219 (U5) — named task segments for the recording currently under the
    /// playhead, the source of "Clip this moment"'s snap-to-moment bounds
    /// (`/v0/tasks.list`, KD2). Empty on a miss / daemon hiccup, in which case
    /// the clip falls back to a centered fixed window — never an error state,
    /// matching `DayTasks`. Cached per recording so re-seeking within one
    /// recording doesn't refetch.
    @Published private(set) var currentRecordingTasks: [RecordingTask] = []
    private var tasksCache: [String: [RecordingTask]] = [:]

    /// Ensure `currentRecordingTasks` reflects the recording under the playhead.
    /// Cheap + idempotent: one read-only `tasks.list` per recording, cached.
    /// Silent on failure (leaves the cache empty so a later attempt retries) —
    /// the clip UI treats an empty result as "no labeled moment" and falls back
    /// to the fixed window. Invoked on demand when the user enters clip mode.
    func refreshTasksForCurrentRecording() async {
        guard let name = currentRecording else {
            currentRecordingTasks = []
            return
        }
        if let cached = tasksCache[name] {
            currentRecordingTasks = cached
            return
        }
        guard let response = try? await DaemonClient.tasksList(TasksListRequest(recording: name)) else {
            // Don't cache a failure — a later entry into clip mode retries.
            if currentRecording == name { currentRecordingTasks = [] }
            return
        }
        tasksCache[name] = response.tasks
        // Guard against the playhead having moved to another recording during
        // the await.
        if currentRecording == name { currentRecordingTasks = response.tasks }
    }

    /// How far the *initial* landing may snap forward to reach a playable
    /// frame. Covers a card click at a recording's `started_at`, which under
    /// action-gated capture precedes the first written frame — without
    /// teleporting a search hit that genuinely sits in a long gap.
    private static let initialSnapCapMs = 120_000

    func load(chunks: [DayPlayableChunk], seekToMs: Int?) {
        self.chunks = chunks.sorted { $0.startMs < $1.startMs }
        startObservingIfNeeded()
        guard let ms = seekToMs ?? self.chunks.first?.anchorMs else { return }
        seek(toDayMs: ms)
        if case .placeholder(.nothingCaptured) = target,
           let next = DayMediaMap.nextChunk(afterMs: ms, in: self.chunks),
           let anchor = next.anchorMs, anchor - ms <= Self.initialSnapCapMs {
            seek(toDayMs: anchor)
        }
    }

    /// Seek the day playhead. Swaps the player item only on chunk boundary
    /// crossings; placeholder targets clear the item (neutral pane, no error).
    func seek(toDayMs ms: Int) {
        currentDayMs = ms
        let resolved = DayMediaMap.target(atMs: ms, in: chunks)
        target = resolved
        switch resolved {
        case .media(let chunk, let offsetSeconds):
            if chunk != currentChunk {
                let crossedRecording = chunk.recording != currentChunk?.recording
                currentChunk = chunk
                // fileURL is non-nil by DayMediaMap.target's contract for .media.
                player.replaceCurrentItem(with: chunk.fileURL.map(AVPlayerItem.init(url:)))
                // SCR-297: a new item reports its own presentation size, so the
                // observation has to follow the item. Seed from the cache first
                // so re-entering a known recording never flashes full-width.
                if crossedRecording {
                    sourceAspect = aspectCache[chunk.recording]
                }
                observePresentationSize(of: player.currentItem, for: chunk.recording)
            }
            let time = CMTime(seconds: offsetSeconds, preferredTimescale: 600)
            let tolerance = CMTime(seconds: 0.1, preferredTimescale: 600)
            player.seek(to: time, toleranceBefore: tolerance, toleranceAfter: tolerance)
        case .placeholder:
            currentChunk = nil
            player.pause()
            player.replaceCurrentItem(with: nil)
            // A placeholder is a page state, not footage: it carries no aspect
            // and fills the pane, so any previously resolved shape must go.
            presentationSizeObservation = nil
            sourceAspect = nil
        }
    }

    /// Track the item's display size and publish it as `sourceAspect`.
    ///
    /// `presentationSize` (rather than the asset's `naturalSize`) because it
    /// already has the preferred transform and pixel aspect ratio applied, so
    /// rotated and non-square-pixel captures need no arithmetic here. It is
    /// `.zero` until the item is ready, which `PlaybackAspect.resolve` maps to
    /// "unresolved" — the pane keeps filling its box until real numbers arrive.
    private func observePresentationSize(of item: AVPlayerItem?, for recording: String) {
        presentationSizeObservation = nil
        guard let item else { return }
        presentationSizeObservation = item.observe(
            \.presentationSize, options: [.initial, .new]
        ) { [weak self] item, _ in
            // KVO gives no queue guarantee, so hop rather than assume main
            // (unlike the periodic time observer above, which is handed an
            // explicit .main queue and can use MainActor.assumeIsolated).
            // Only the CGSize crosses the boundary; `self` stays weak through
            // the nested capture.
            let size = item.presentationSize
            Task { @MainActor in
                self?.applyReportedSize(size, forRecording: recording)
            }
        }
    }

    /// Fold a reported display size into `sourceAspect` and the cache.
    ///
    /// Internal rather than private so the tests can drive the state machine
    /// directly: the `presentationSize` path needs a real, ready `AVPlayerItem`
    /// and cannot be exercised hermetically, and `@testable` reaches internal
    /// but not private. The KVO wiring itself is proven at runtime.
    func applyReportedSize(_ size: CGSize, forRecording recording: String) {
        // The playhead can move to another recording while an observation is
        // in flight; a late size must not repaint the current one.
        guard recording == currentRecording else { return }
        guard let aspect = PlaybackAspect.resolve(reportedSize: size) else { return }
        aspectCache[recording] = aspect
        sourceAspect = aspect
    }

    /// Called from the view's `.onDisappear` — releases the time observer so
    /// the player doesn't retain the engine (mirrors LiveVideoPlaybackEngine).
    func tearDown() {
        if let token = timeObserverToken {
            player.removeTimeObserver(token)
            timeObserverToken = nil
        }
        if let endObserver {
            NotificationCenter.default.removeObserver(endObserver)
            self.endObserver = nil
        }
        // SCR-297: the KVO observation retains its target item; releasing it
        // here keeps this in lockstep with the other observers rather than
        // leaving a live reference behind after teardown (cf. SCR-93).
        presentationSizeObservation = nil
        sourceAspect = nil
        player.pause()
        player.replaceCurrentItem(with: nil)
        currentChunk = nil
    }

    private func startObservingIfNeeded() {
        guard timeObserverToken == nil else { return }
        let interval = CMTime(seconds: 0.25, preferredTimescale: 600)
        timeObserverToken = player.addPeriodicTimeObserver(forInterval: interval, queue: .main) { [weak self] time in
            let seconds = CMTimeGetSeconds(time)
            MainActor.assumeIsolated {
                guard let self, let anchor = self.currentChunk?.anchorMs, seconds.isFinite else { return }
                self.currentDayMs = anchor + Int(seconds * 1000)
            }
        }
        endObserver = NotificationCenter.default.addObserver(
            forName: .AVPlayerItemDidPlayToEndTime, object: nil, queue: .main
        ) { [weak self] note in
            MainActor.assumeIsolated {
                guard let self,
                      let item = note.object as? AVPlayerItem,
                      item === self.player.currentItem,
                      let ended = self.currentChunk
                else { return }
                // Auto-advance: continue in the next playable chunk (which may
                // belong to the next recording); park on the placeholder when
                // the day's media is drained.
                if let next = DayMediaMap.nextChunk(afterMs: ended.endMs, in: self.chunks) {
                    self.seek(toDayMs: next.anchorMs ?? next.startMs)
                    self.player.play()
                } else {
                    self.seek(toDayMs: ended.endMs)
                }
            }
        }
    }

    deinit {
        // Defensive backstop (mirrors LiveVideoPlaybackEngine.deinit): the
        // observer normally detaches in tearDown; AVPlayer requires an explicit
        // removeTimeObserver before release.
        if let timeObserverToken {
            player.removeTimeObserver(timeObserverToken)
        }
        if let endObserver {
            NotificationCenter.default.removeObserver(endObserver)
        }
    }
}
