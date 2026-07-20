import CoreGraphics
import Foundation
import ImageIO

// SCR-177 U2 — decode a small, downsampled thumbnail for a resolved frame URL
// cheaply: off the main actor, downsampled at decode, cached, with in-flight
// coalescing and bounded concurrency so a flick-scroll through 200 rows can't
// thrash (learning #1 — never a subprocess, never one decode per row). Bytes are
// read only from the URL handed in by U1 (always under `.../screenshots/`).

/// A decoded thumbnail crossing the loader's actor boundary. `CGImage` is an
/// immutable, thread-safe Core Foundation type, so wrapping it is safe under
/// strict concurrency (an `NSImage` could not cross — see `ScreenshotTruthPane`,
/// which keeps only `Data` on its detached boundary for the same reason).
struct ThumbnailImage: @unchecked Sendable {
    let cgImage: CGImage
}

/// A minimal async counting semaphore so a loader can bound how many decodes
/// run at once (the fork-bomb guard from learning #1). Shared with
/// `RecordingFrameIndex`, which gates its video poster-frame extraction the same
/// way (video decode is heavier than a JPEG downsample, so it needs the cap too).
actor DecodeGate {
    private var permits: Int
    private var waiters: [CheckedContinuation<Void, Never>] = []

    init(permits: Int) { self.permits = max(1, permits) }

    func wait() async {
        if permits > 0 { permits -= 1; return }
        await withCheckedContinuation { waiters.append($0) }
    }

    func signal() {
        if waiters.isEmpty { permits += 1 } else { waiters.removeFirst().resume() }
    }
}

/// Loads downsampled thumbnails for frame URLs, caching by URL and coalescing
/// concurrent requests for the same URL into one decode. An `actor` so the cache
/// and the in-flight table are race-free; the decode itself runs in a detached
/// task (off the main actor) and is bounded by `DecodeGate`.
actor ThumbnailLoader {
    /// Injectable so cache/coalescing behavior is testable without real JPEGs.
    typealias Decode = @Sendable (URL, Int) -> ThumbnailImage?

    private final class CacheBox { let image: ThumbnailImage; init(_ image: ThumbnailImage) { self.image = image } }

    private let maxPixelSize: Int
    private let decode: Decode
    private let gate: DecodeGate
    private let cache = NSCache<NSURL, CacheBox>()
    private var inFlight: [URL: Task<ThumbnailImage?, Never>] = [:]

    init(
        maxPixelSize: Int = 160,
        maxConcurrentDecodes: Int = 4,
        decode: Decode? = nil
    ) {
        self.maxPixelSize = maxPixelSize
        // Search U6: default to the decrypting decode so an encrypted `*.jpg.enc`
        // still is decrypted in-memory (CryptoKit) before downsampling. The corpus
        // key is read once from the shared Keychain group; when it is unavailable
        // (dev builds without the entitlement, or the guardrails aren't on) encrypted
        // frames decode to `nil` → the row shows the placeholder (R5). Tests inject
        // an explicit `decode` to bypass real crypto.
        self.decode = decode ?? ThumbnailLoader.makeDecryptingDecode(corpus: try? CorpusCrypto())
        self.gate = DecodeGate(permits: maxConcurrentDecodes)
        cache.countLimit = 512
    }

    /// The cached thumbnail, or a freshly decoded one; `nil` for an
    /// unreadable/missing file (→ the row shows the placeholder, R5). Concurrent
    /// callers for the same URL await a single shared decode.
    func thumbnail(for url: URL) async -> ThumbnailImage? {
        if let box = cache.object(forKey: url as NSURL) { return box.image }
        // The calling row already scrolled away before this decode's turn — skip
        // spawning work for an off-screen frame. (Once started, the detached decode
        // is shared across coalesced callers and cannot be cancelled per-caller.)
        if Task.isCancelled { return nil }
        if let existing = inFlight[url] { return await existing.value }

        let decode = self.decode
        let gate = self.gate
        let maxPixelSize = self.maxPixelSize
        let task = Task<ThumbnailImage?, Never>.detached(priority: .userInitiated) {
            await gate.wait()
            let image = decode(url, maxPixelSize)
            await gate.signal()
            return image
        }
        inFlight[url] = task
        let image = await task.value
        inFlight[url] = nil
        if let image { cache.setObject(CacheBox(image), forKey: url as NSURL) }
        return image
    }

    /// Downsample at decode via `CGImageSourceCreateThumbnailAtIndex` — far
    /// cheaper than decoding a full-resolution screen JPEG and resizing. Returns
    /// `nil` (never throws) for a missing/unreadable file.
    static func decodeDownsampled(url: URL, maxPixelSize: Int) -> ThumbnailImage? {
        let sourceOptions = [kCGImageSourceShouldCache: false] as CFDictionary
        guard let source = CGImageSourceCreateWithURL(url as CFURL, sourceOptions) else { return nil }
        return downsample(source: source, maxPixelSize: maxPixelSize)
    }

    /// Downsample from in-memory bytes — the decrypted-still path (search U6).
    static func decodeDownsampled(data: Data, maxPixelSize: Int) -> ThumbnailImage? {
        let sourceOptions = [kCGImageSourceShouldCache: false] as CFDictionary
        guard let source = CGImageSourceCreateWithData(data as CFData, sourceOptions) else { return nil }
        return downsample(source: source, maxPixelSize: maxPixelSize)
    }

    private static func downsample(source: CGImageSource, maxPixelSize: Int) -> ThumbnailImage? {
        let thumbOptions: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: maxPixelSize,
        ]
        guard let cgImage = CGImageSourceCreateThumbnailAtIndex(source, 0, thumbOptions as CFDictionary) else {
            return nil
        }
        return ThumbnailImage(cgImage: cgImage)
    }

    /// A decode that decrypts `*.jpg.enc` via `corpus` (CryptoKit) before
    /// downsampling, and reads `*.jpg` from the URL directly (search U6). Returns
    /// `nil` for an encrypted still when `corpus` is absent or decryption fails
    /// (e.g. dev builds where the Keychain group entitlement is stripped) → the row
    /// shows the placeholder rather than leaking or crashing.
    static func makeDecryptingDecode(corpus: CorpusCrypto?) -> Decode {
        return { url, maxPixelSize in
            if CorpusCrypto.isEncryptedStill(url) {
                guard let corpus, let data = try? corpus.decryptStill(at: url) else { return nil }
                return decodeDownsampled(data: data, maxPixelSize: maxPixelSize)
            }
            return decodeDownsampled(url: url, maxPixelSize: maxPixelSize)
        }
    }
}
