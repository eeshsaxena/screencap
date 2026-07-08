import AVFoundation
import CoreGraphics
import Foundation

// SCR-177 U1 — resolve a `(recording, anchorMs)` search pointer to a concrete
// local frame URL under `~/.screencap/recordings/<name>/screenshots/`. Pointer
// models stay pointer-only (R8/R4); the image bytes are resolved and read
// entirely app-side and never upload (R6). Unlike the content-index OCR pass
// (narrowed-R7, which indexes ALLOW frames only), thumbnails surface every local
// frame — including MASK_WINDOW-flagged windows, whose masking is scoped to
// upload, not to local viewing of the operator's own screen. The governing
// boundary here is therefore the same-EUID trust boundary (see SECURITY.md), not
// narrowed-R7.

/// A single captured frame: its on-disk URL and epoch-millisecond timestamp,
/// parsed from the recorder's `{epoch:.6f}.jpg` filename convention.
struct FrameRef: Equatable {
    let url: URL
    let ms: Int
}

/// Pure nearest-frame selection over a recording's sorted frame list. Extracted
/// so the mapping is unit-tested without disk IO (mirrors `ScreenshotTruth`).
enum FrameSelection {
    /// The frame closest to `toMs` by absolute distance, or `nil` for an empty
    /// list. Not nearest-*prior*: a search anchor can sit just after the last
    /// relevant frame, so absolute-nearest is the right recognition aid.
    static func nearest(toMs: Int, in frames: [FrameRef]) -> FrameRef? {
        frames.min { abs($0.ms - toMs) < abs($1.ms - toMs) }
    }
}

/// Resolves search pointers to local frame URLs, caching each recording's frame
/// listing for the lifetime of the owning view (the Recall palette shares the
/// cache across every result set while it is open, not reset per search). An
/// `actor` so cache access is serialized; each recording's directory enumeration
/// runs in a detached task off the actor (cached as the in-flight `Task` so
/// concurrent resolves for one recording coalesce instead of blocking the actor).
///
/// Three ways a pointer resolves to `nil` (→ the row shows the stream-icon
/// placeholder, R5): a `nil` anchor (unanchored transcript hit), a nearest
/// frame beyond the staleness cap (a sparse recording or a chunk-coarse
/// transcript anchor that would otherwise show a misleading moment), or a
/// missing/unsafe recording dir. A `nil`/far anchor never resolves to frame-0.
actor RecordingFrameIndex {
    private let recordingsRoot: URL
    private let stalenessCapMs: Int
    /// Per-recording frame listing cached as its in-flight `Task` so the disk
    /// enumeration runs once and off the actor; concurrent resolves for the same
    /// recording await the same task rather than serializing behind a blocking
    /// `contentsOfDirectory` on the actor executor.
    private var cache: [String: Task<[FrameRef], Never>] = [:]
    /// Per-recording extracted video poster, cached as its in-flight `Task` so a
    /// recording's poster is decoded once and shared across coalesced callers
    /// (same discipline as `cache`). Separate from `cache` because a poster comes
    /// from the video chunk, not the flat-frame listing.
    private var posterCache: [String: Task<ThumbnailImage?, Never>] = [:]
    /// Bounds concurrent video poster extractions. The poster path is the COMMON
    /// case (default capture records video, not flat frames), so a fast scroll
    /// over distinct recordings would otherwise fire one full AVFoundation decode
    /// per visible card at once — the same thrash the JPEG loader's `DecodeGate`
    /// guards against, but heavier. A small permit count keeps it capped.
    private let posterGate = DecodeGate(permits: 3)

    /// `stalenessCapMs` (~30s default): content hits map essentially exactly,
    /// while timeline / transcript anchors snap to a captured moment that can
    /// sit between frames — beyond the cap we show the placeholder instead of a
    /// frame minutes from the matched moment.
    init(recordingsRoot: URL? = nil, stalenessCapMs: Int = 30_000) {
        self.recordingsRoot = recordingsRoot ?? AppPaths.recordingsRoot
        self.stalenessCapMs = stalenessCapMs
    }

    /// Resolve a pointer to a frame URL, or `nil` (→ placeholder). The entry
    /// point takes `Int?` so a `nil` anchor short-circuits before `nearest` and
    /// can never silently resolve to an arbitrary first frame. `async` so the
    /// per-recording disk enumeration runs off the actor (see `framesTask`).
    func resolve(recording: String, anchorMs: Int?) async -> URL? {
        guard let anchorMs else { return nil }
        let task = framesTask(for: recording)
        let frames = await task.value
        // Don't retain an empty listing: a still-processing recording can gain
        // frames after a first miss, and a cached empty would stick the row on
        // the placeholder for the whole session (mirrors `ThumbnailLoader`'s
        // nil-not-cached policy). Only evict if a newer enumeration hasn't
        // already replaced this task.
        if frames.isEmpty {
            if cache[recording] == task { cache[recording] = nil }
            return nil
        }
        guard let chosen = FrameSelection.nearest(toMs: anchorMs, in: frames) else { return nil }
        guard abs(chosen.ms - anchorMs) <= stalenessCapMs else { return nil }
        return chosen.url
    }

    /// The recording's earliest captured frame URL — the Library card thumbnail
    /// (U5), or `nil` when the recording has no frames yet / its dir is
    /// unreadable (→ the hatched placeholder, R5). Unlike `resolve`, there is no
    /// anchor and no staleness cap: a card wants the first real frame however old,
    /// not a moment near a search anchor. Reuses the same cached enumeration and
    /// the empty-not-retained policy so a still-processing recording that gains
    /// frames after a first miss isn't stuck on the placeholder for the session.
    func firstFrameURL(recording: String) async -> URL? {
        let task = framesTask(for: recording)
        let frames = await task.value
        if frames.isEmpty {
            if cache[recording] == task { cache[recording] = nil }
            return nil
        }
        return frames.first?.url
    }

    /// A downsampled poster frame extracted from the recording's first local
    /// video chunk — the card thumbnail's fallback when no flat `screenshots/*.jpg`
    /// frame exists. This is the common case, not an edge: the default capture
    /// config records video (`RECORD_VIDEO`), not flat frames (`RECORD_IMAGES` is
    /// off), so a finished recording usually has an empty `screenshots/` dir but a
    /// real `chunk_0000.mp4`. Returns `nil` when there is no local video (a
    /// legacy single-file recording, or an uploaded-and-evicted stub) → the
    /// hatched placeholder (R5).
    ///
    /// Same trust boundary as `firstFrameURL`: the poster is read from the LOCAL,
    /// unmasked video the app already plays in the Day/Review panes, so it adds no
    /// exposure beyond same-EUID (masking is upload-scoped; see SECURITY.md). The
    /// extraction runs in a detached task so the AVFoundation decode never blocks
    /// the actor; a miss is not retained so a still-processing recording that
    /// gains its first chunk after an early miss isn't stuck on the placeholder.
    func posterFrame(recording: String, maxPixelSize: Int = 320) async -> ThumbnailImage? {
        if let existing = posterCache[recording] { return await existing.value }
        let root = recordingsRoot
        let gate = posterGate
        let task = Task<ThumbnailImage?, Never>.detached(priority: .userInitiated) {
            // Find the chunk before taking a permit — the directory glob is cheap
            // and shouldn't hold a decode slot; only the AVFoundation decode is gated.
            guard let url = Self.firstVideoChunkURL(root: root, recording: recording) else { return nil }
            await gate.wait()
            let image = await Self.extractPoster(url: url, maxPixelSize: maxPixelSize)
            await gate.signal()
            return image
        }
        posterCache[recording] = task
        let image = await task.value
        if image == nil, posterCache[recording] == task { posterCache[recording] = nil }
        return image
    }

    /// The cached (or freshly started) enumeration task for a recording. The disk
    /// listing runs in a detached task so a recording with many frames never
    /// blocks the actor; concurrent resolves for the same recording share it.
    private func framesTask(for recording: String) -> Task<[FrameRef], Never> {
        if let existing = cache[recording] { return existing }
        let root = recordingsRoot
        let task = Task.detached(priority: .userInitiated) {
            Self.loadFrames(root: root, recording: recording)
        }
        cache[recording] = task
        return task
    }

    /// Static loader so it is testable with an explicit root. Applies the
    /// path-containment guard, then parses `{epoch}.jpg` stems → ms (skipping
    /// non-numeric stems, drift-resilient), sorted ascending by time.
    static func loadFrames(root: URL, recording: String) -> [FrameRef] {
        guard let dir = screenshotsDir(root: root, recording: recording) else { return [] }
        guard let entries = try? FileManager.default.contentsOfDirectory(
            at: dir, includingPropertiesForKeys: nil, options: [.skipsHiddenFiles]
        ) else { return [] }
        return entries
            .filter { $0.pathExtension.lowercased() == "jpg" }
            .compactMap { url -> FrameRef? in
                guard let ts = Double(url.deletingPathExtension().lastPathComponent) else { return nil }
                return FrameRef(url: url, ms: Int((ts * 1000).rounded()))
            }
            .sorted { $0.ms < $1.ms }
    }

    /// The recording's `screenshots/` dir, or `nil` when `recording` is not a
    /// safe single path component contained under the recordings root. This is
    /// the Swift-side analogue of the daemon's `resolve_recording_dir`: the name
    /// arrives daemon-sourced over the UDS, so it is path-contained before any
    /// disk read (R6 — never reads outside the recordings tree).
    static func screenshotsDir(root: URL, recording: String) -> URL? {
        guard !recording.isEmpty,
              !recording.contains("/"),
              !recording.contains("\\"),
              recording != ".",
              recording != ".." else { return nil }
        // Resolve symlinks (not just `.`/`..`) before the containment check so a
        // recording-dir or `screenshots` symlink pointing outside the tree is
        // rejected — matching the daemon's `resolve_recording_dir` realpath
        // semantics. `standardizedFileURL` is lexical-only and would let a
        // symlinked dir escape the root.
        let dir = root.appendingPathComponent(recording, isDirectory: true)
            .appendingPathComponent("screenshots", isDirectory: true)
            .resolvingSymlinksInPath()
        let rootResolved = root.resolvingSymlinksInPath()
        guard dir.path == rootResolved.path || dir.path.hasPrefix(rootResolved.path + "/") else { return nil }
        return dir
    }

    /// The recording's own dir, path-contained under the recordings root with the
    /// same symlink-resolved guard as `screenshotsDir`. Used to locate the local
    /// video chunk for the poster fallback.
    static func recordingDir(root: URL, recording: String) -> URL? {
        guard !recording.isEmpty,
              !recording.contains("/"),
              !recording.contains("\\"),
              recording != ".",
              recording != ".." else { return nil }
        let dir = root.appendingPathComponent(recording, isDirectory: true)
            .resolvingSymlinksInPath()
        let rootResolved = root.resolvingSymlinksInPath()
        guard dir.path == rootResolved.path || dir.path.hasPrefix(rootResolved.path + "/") else { return nil }
        return dir
    }

    /// The recording's first local video chunk (`chunk_0000.mp4`, else the
    /// lexically-first `chunk_*.mp4`), or `nil` when the dir is unsafe or holds no
    /// local chunk. Static + explicit root so it is testable without disk fixtures
    /// under a real recordings tree.
    static func firstVideoChunkURL(root: URL, recording: String) -> URL? {
        guard let dir = recordingDir(root: root, recording: recording) else { return nil }
        guard let entries = try? FileManager.default.contentsOfDirectory(
            at: dir, includingPropertiesForKeys: nil, options: [.skipsHiddenFiles]
        ) else { return nil }
        return entries
            .filter { $0.pathExtension.lowercased() == "mp4" && $0.lastPathComponent.hasPrefix("chunk_") }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
            .first
    }

    /// Extract a downsampled `CGImage` roughly one second into the video — past a
    /// possible black lead-in — with generous seek tolerance (the card wants a
    /// recognizable poster, not an exact frame). Falls back to the first frame for
    /// a sub-second chunk, and returns `nil` on any AVFoundation failure (an
    /// unreadable or still-being-written chunk) → the placeholder.
    static func extractPoster(url: URL, maxPixelSize: Int) async -> ThumbnailImage? {
        let generator = AVAssetImageGenerator(asset: AVURLAsset(url: url))
        generator.appliesPreferredTrackTransform = true
        generator.maximumSize = CGSize(width: maxPixelSize, height: maxPixelSize)
        generator.requestedTimeToleranceBefore = CMTime(seconds: 1, preferredTimescale: 600)
        generator.requestedTimeToleranceAfter = CMTime(seconds: 2, preferredTimescale: 600)
        if let image = try? await generator.image(at: CMTime(seconds: 1, preferredTimescale: 600)).image {
            return ThumbnailImage(cgImage: image)
        }
        if let image = try? await generator.image(at: .zero).image {
            return ThumbnailImage(cgImage: image)
        }
        return nil
    }
}
