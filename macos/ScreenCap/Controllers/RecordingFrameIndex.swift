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
/// listing for the lifetime of the `SearchView` instance (the cache is shared
/// across every result set while Search is open, not reset per search). An
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
}
