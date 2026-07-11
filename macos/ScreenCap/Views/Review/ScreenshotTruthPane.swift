import SwiftUI

/// One masked screenshot from the scrubbed copy, with its recording-relative
/// timestamp parsed from the filename (`{epoch:.6f}.jpg`, the recorder's
/// convention — same time space as `started_at`).
struct ReviewScreenshot: Equatable {
    let url: URL
    let relativeSeconds: Double
}

/// Which masked frame (if any) the truth view shows for the current time.
enum ScreenshotSelection: Equatable {
    /// The recording shipped no screenshots at all.
    case empty
    /// The current time precedes the first captured frame — there is no
    /// uploaded frame to show here. NEVER falls back to an original/unmasked
    /// screenshot (R1/R15: the truth view only ever shows what uploads).
    case beforeFirst
    /// The nearest-prior masked frame and how stale it is (sparse capture).
    case frame(url: URL, offsetSeconds: Double)
}

/// Pure selection logic, extracted so the nearest-prior mapping is unit-tested
/// without a SwiftUI render (mirrors `TimelinePaneScrub`).
enum ScreenshotTruth {
    /// Parse the envelope's screenshot paths into timestamped frames, sorted by
    /// time. A filename whose stem isn't a number is skipped (drift-resilient).
    static func screenshots(from urls: [URL], startedAt: Double) -> [ReviewScreenshot] {
        let stamped = urls.compactMap { url -> (URL, Double)? in
            guard let ts = Double(url.deletingPathExtension().lastPathComponent) else {
                return nil
            }
            return (url, ts)
        }
        guard !stamped.isEmpty else { return [] }
        // When started_at is unknown (review.py emits null → the ViewModel falls
        // back to 0), anchor frames to the earliest captured frame so they still
        // land on the 0-based playback axis. Without this, every frame's
        // relativeSeconds would be its raw epoch (~1.7e9), so `selection(at:)`
        // returns .beforeFirst for the whole timeline and the "what uploads"
        // surface wrongly reads as empty.
        let origin = startedAt > 0 ? startedAt : (stamped.map(\.1).min() ?? 0)
        return stamped
            .map { ReviewScreenshot(url: $0.0, relativeSeconds: max(0, $0.1 - origin)) }
            .sorted { $0.relativeSeconds < $1.relativeSeconds }
    }

    /// Map a recording-relative time to the nearest-prior masked frame. Sparse
    /// capture means most times land *between* frames; we show the most recent
    /// prior frame plus how stale it is, so the operator knows it isn't
    /// real-time. Times before the first frame return `.beforeFirst` (a defined
    /// boundary state — not the original unmasked frame).
    static func selection(at time: Double, screenshots: [ReviewScreenshot]) -> ScreenshotSelection {
        guard !screenshots.isEmpty else { return .empty }
        var chosen: ReviewScreenshot?
        for shot in screenshots {
            if shot.relativeSeconds <= time {
                chosen = shot
            } else {
                break  // sorted ascending — no later frame can be prior
            }
        }
        guard let chosen else { return .beforeFirst }
        return .frame(url: chosen.url, offsetSeconds: max(0, time - chosen.relativeSeconds))
    }

    /// "0:14" style mm:ss for a non-negative seconds value.
    static func clockLabel(_ seconds: Double) -> String {
        let total = Int(seconds.rounded(.down))
        return String(format: "%d:%02d", total / 60, total % 60)
    }
}

/// The primary "what actually uploads" surface (R15): renders the masked
/// screenshot from the scrubbed copy that is nearest-prior to the current
/// timeline position, with a staleness label. The local video pane beside it
/// is the secondary navigation aid, labeled local-only.
struct ScreenshotTruthPane: View {
    let screenshots: [ReviewScreenshot]
    let currentTime: Double

    /// Decoded-frame cache. The selected frame changes only when playback
    /// crosses a screenshot boundary (capture is sparse), so loading it lazily
    /// via `.task(id:)` avoids re-reading + re-decoding the JPEG from disk on
    /// every ~10Hz playback tick — and moves the decode off the main thread.
    /// `image == nil` records a load that was attempted and failed.
    @State private var loaded: LoadedFrame?

    private struct LoadedFrame {
        let url: URL
        let image: NSImage?
    }

    private var selection: ScreenshotSelection {
        ScreenshotTruth.selection(at: currentTime, screenshots: screenshots)
    }

    private var currentFrameURL: URL? {
        if case .frame(let url, _) = selection { return url }
        return nil
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            frameArea
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        // Runs once per distinct frame URL (not per render), so the frame is
        // loaded only when playback crosses a screenshot boundary. The disk read
        // runs off the main actor via Task.detached (Data is Sendable); NSImage
        // builds on return and decodes lazily at draw time.
        .task(id: currentFrameURL) {
            guard let url = currentFrameURL, loaded?.url != url else { return }
            // Search U6: decrypt a `*.jpg.enc` corpus still transparently (plaintext
            // `*.jpg` reads unchanged). Runs off the main actor; `Data` is Sendable.
            let data = await Task.detached(priority: .userInitiated) {
                CorpusCrypto.readStillData(at: url)
            }.value
            if !Task.isCancelled {
                loaded = LoadedFrame(url: url, image: data.flatMap { NSImage(data: $0) })
            }
        }
    }

    private var header: some View {
        HStack(spacing: 6) {
            Image(systemName: "checkmark.shield.fill")
                .foregroundStyle(.green)
            Text("What actually uploads")
                .font(.caption.weight(.semibold))
            Spacer()
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(.green.opacity(0.08))
    }

    @ViewBuilder
    private var frameArea: some View {
        switch selection {
        case .empty:
            placeholder(
                icon: "photo.on.rectangle.angled",
                title: "No uploaded frames",
                detail: "This recording has no screenshots — nothing visual will upload."
            )
        case .beforeFirst:
            placeholder(
                icon: "clock.arrow.circlepath",
                title: "No uploaded frame here",
                detail: "Capture starts later in the recording."
            )
        case .frame(let url, let offsetSeconds):
            maskedFrame(url: url, offsetSeconds: offsetSeconds)
        }
    }

    @ViewBuilder
    private func maskedFrame(url: URL, offsetSeconds: Double) -> some View {
        VStack(spacing: 0) {
            if loaded?.url == url {
                if let image = loaded?.image {
                    Image(nsImage: image)
                        .resizable()
                        .scaledToFit()
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .background(Color.black)
                } else {
                    // The frame is in the uploaded set but unreadable on disk —
                    // be honest rather than silently showing nothing.
                    placeholder(
                        icon: "exclamationmark.triangle",
                        title: "Couldn't load this frame",
                        detail: url.lastPathComponent
                    )
                }
            } else {
                // Selected but not yet decoded (the `.task` is loading it).
                Color.black
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .overlay(ProgressView().controlSize(.small))
            }
            staleness(offsetSeconds: offsetSeconds, at: url)
        }
    }

    private func staleness(offsetSeconds: Double, at url: URL) -> some View {
        // Recover the frame's own clock position for the "Screenshot from M:SS"
        // half of the label.
        let frameTime = currentTime - offsetSeconds
        let agoSeconds = Int(offsetSeconds.rounded())
        let agoText = agoSeconds <= 0 ? "now" : "\(agoSeconds)s ago"
        return HStack {
            Text("Screenshot from \(ScreenshotTruth.clockLabel(frameTime)) — \(agoText)")
                .font(.caption2)
                .foregroundStyle(.secondary)
            Spacer()
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 4)
    }

    private func placeholder(icon: String, title: String, detail: String) -> some View {
        VStack(spacing: 8) {
            Image(systemName: icon)
                .font(.title2)
                .foregroundStyle(.secondary)
            Text(title).font(.callout.weight(.medium))
            Text(detail)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding()
    }
}
