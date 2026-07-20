import AVFoundation
import Foundation

// U6 — the New-recording sheet's live microphone level meter. The AVAudioEngine
// input tap lives behind the `MicLevelSource` seam so the meter is testable with
// a fake source, and so the view depends only on the published `level`. The meter
// is started ONLY when mic TCC is already granted (NewRecordingSheetPolicy.
// shouldRunMeter) — installing a tap on the input node is a passive read, but the
// gate keeps the sheet from ever poking the audio subsystem without a grant.

/// The audio-tap seam: begins delivering normalized RMS levels (0...1) via
/// `onLevel`. `@Sendable` callback because the live source fires it from the
/// realtime audio thread.
protocol MicLevelSource: AnyObject {
    func start(onLevel: @escaping @Sendable (Double) -> Void)
    func stop()
}

@MainActor
final class MicLevelMeter: ObservableObject {
    /// Normalized level 0...1, lightly smoothed, for the 5-bar meter.
    @Published private(set) var level: Double = 0

    private let source: MicLevelSource
    private var running = false

    init(source: MicLevelSource = AVAudioEngineMicLevelSource()) {
        self.source = source
    }

    /// Idempotent — a second `start()` while running is a no-op.
    func start() {
        guard !running else { return }
        running = true
        source.start { [weak self] value in
            let clamped = min(1, max(0, value))
            Task { @MainActor [weak self] in
                guard let self else { return }
                // Attack fast, release slow so the bars feel responsive but don't
                // strobe on every buffer.
                self.level = clamped > self.level ? clamped : self.level * 0.8 + clamped * 0.2
            }
        }
    }

    func stop() {
        guard running else { return }
        running = false
        source.stop()
        level = 0
    }
}

/// Live source: an `AVAudioEngine` input-node tap that computes per-buffer RMS.
/// Not `@MainActor` — its methods are driven from the meter (main) but the tap
/// block runs on the realtime audio thread and calls the `@Sendable` callback
/// directly. `@unchecked Sendable` because `AVAudioEngine` is not `Sendable` but
/// is confined to this object's own start/stop lifecycle.
final class AVAudioEngineMicLevelSource: MicLevelSource, @unchecked Sendable {
    private let engine = AVAudioEngine()
    private var installed = false

    func start(onLevel: @escaping @Sendable (Double) -> Void) {
        guard !installed else { return }
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        // A zero-channel / zero-rate format means no usable input device; bail so
        // we neither crash installing a tap nor start a dead engine.
        guard format.channelCount > 0 else { return }
        // 4096-frame buffers (~11 callbacks/sec at 44.1kHz) keep the meter smooth
        // while cutting the per-buffer MainActor hop rate the smoothing needs.
        input.installTap(onBus: 0, bufferSize: 4096, format: format) { buffer, _ in
            guard let channel = buffer.floatChannelData else { return }
            let frames = Int(buffer.frameLength)
            guard frames > 0 else { return }
            let samples = channel[0]
            var sumSquares: Float = 0
            for i in 0..<frames { sumSquares += samples[i] * samples[i] }
            let rms = sqrtf(sumSquares / Float(frames))
            // ~20× gain maps typical speech RMS into a legible 0...1 span.
            onLevel(Double(min(1, rms * 20)))
        }
        installed = true
        do {
            try engine.start()
        } catch {
            // No meter rather than a crash — the sheet still records fine.
            input.removeTap(onBus: 0)
            installed = false
        }
    }

    func stop() {
        guard installed else { return }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        installed = false
    }
}
