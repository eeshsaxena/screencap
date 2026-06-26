import Foundation

// SCR-177 U1 — resolve a `(recording, anchorMs)` search pointer to a concrete
// local frame URL under `~/.screencap/recordings/<name>/screenshots/`. Pointer
// models stay pointer-only (R8/R4); the image bytes are resolved and read
// entirely app-side. The raw local `screenshots/*.jpg` are the same sensitivity
// class as the screenshots themselves (narrowed-R7) and never upload (R6).

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
/// listing for the lifetime of a results set. An `actor` so cache access is
/// serialized and the directory enumeration runs off the main actor.
///
/// Three ways a pointer resolves to `nil` (→ the row shows the stream-icon
/// placeholder, R5): a `nil` anchor (unanchored transcript hit), a nearest
/// frame beyond the staleness cap (a sparse recording or a chunk-coarse
/// transcript anchor that would otherwise show a misleading moment), or a
/// missing/unsafe recording dir. A `nil`/far anchor never resolves to frame-0.
actor RecordingFrameIndex {
    private let recordingsRoot: URL
    private let stalenessCapMs: Int
    private var cache: [String: [FrameRef]] = [:]

    /// `stalenessCapMs` (~30s default): content hits map essentially exactly,
    /// while timeline / transcript anchors snap to a captured moment that can
    /// sit between frames — beyond the cap we show the placeholder instead of a
    /// frame minutes from the matched moment.
    init(recordingsRoot: URL? = nil, stalenessCapMs: Int = 30_000) {
        self.recordingsRoot = recordingsRoot
            ?? FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent(".screencap/recordings", isDirectory: true)
        self.stalenessCapMs = stalenessCapMs
    }

    /// Resolve a pointer to a frame URL, or `nil` (→ placeholder). The entry
    /// point takes `Int?` so a `nil` anchor short-circuits before `nearest` and
    /// can never silently resolve to an arbitrary first frame.
    func resolve(recording: String, anchorMs: Int?) -> URL? {
        guard let anchorMs else { return nil }
        guard let chosen = FrameSelection.nearest(toMs: anchorMs, in: frames(for: recording)) else {
            return nil
        }
        guard abs(chosen.ms - anchorMs) <= stalenessCapMs else { return nil }
        return chosen.url
    }

    /// List + parse + sort once per recording, cached. Empty for a missing dir,
    /// an unsafe name, or a dir with no parseable frames.
    private func frames(for recording: String) -> [FrameRef] {
        if let cached = cache[recording] { return cached }
        let loaded = Self.loadFrames(root: recordingsRoot, recording: recording)
        cache[recording] = loaded
        return loaded
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
        let dir = root.appendingPathComponent(recording, isDirectory: true)
            .appendingPathComponent("screenshots", isDirectory: true)
            .standardizedFileURL
        let rootStd = root.standardizedFileURL
        guard dir.path == rootStd.path || dir.path.hasPrefix(rootStd.path + "/") else { return nil }
        return dir
    }
}
