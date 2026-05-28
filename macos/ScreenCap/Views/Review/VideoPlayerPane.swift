import AppKit
import AVFoundation
import AVKit
import Combine
import Foundation
import SwiftUI

/// Status of the underlying media load. AVPlayer surfaces failure via its
/// `currentItem.status == .failed`; the model translates that into the
/// `failed` case so the parent view (U8) can render a load-error state
/// without reaching into AVKit.
enum VideoLoadStatus: Equatable {
    case loading
    case ready
    case failed(String)
}

/// Test seam around AVPlayer (plan U5). Production wraps a real `AVPlayer`
/// in `LiveVideoPlaybackEngine`; unit tests inject `FakeVideoPlaybackEngine`
/// so the pane's bindings and lifecycle can be asserted without a media file.
@MainActor
protocol VideoPlaybackEngine: AnyObject {
    var currentSeconds: Double { get }
    var loadStatus: VideoLoadStatus { get }

    func seek(toSeconds seconds: Double)
    func play()
    func pause()
    func startObservingTime(interval: Double, onTick: @escaping @MainActor (Double) -> Void)
    func stopObservingTime()
    /// Optional registration for load-status transitions so the model can
    /// republish through its `loadStatus` binding without polling.
    func observeLoadStatus(_ onChange: @escaping @MainActor (VideoLoadStatus) -> Void)
}

/// Live AVPlayer-backed engine. Owns the periodic time observer token so
/// `stopObservingTime` can detach it cleanly — without explicit detach, the
/// observer retains the AVPlayer and the closure, leaking the model after
/// the window closes.
@MainActor
final class LiveVideoPlaybackEngine: VideoPlaybackEngine {
    let player: AVPlayer
    private var timeObserverToken: Any?
    private var statusObserver: NSKeyValueObservation?

    init(url: URL) {
        self.player = AVPlayer(url: url)
    }

    var currentSeconds: Double {
        let t = player.currentTime()
        guard t.isValid, !t.isIndefinite else { return 0 }
        return CMTimeGetSeconds(t)
    }

    var loadStatus: VideoLoadStatus {
        guard let item = player.currentItem else { return .loading }
        switch item.status {
        case .readyToPlay: return .ready
        case .failed:
            let msg = item.error?.localizedDescription ?? "Failed to load media."
            return .failed(msg)
        default: return .loading
        }
    }

    func seek(toSeconds seconds: Double) {
        let time = CMTime(seconds: max(0, seconds), preferredTimescale: 600)
        player.seek(to: time, toleranceBefore: .zero, toleranceAfter: .zero)
    }

    func play() { player.play() }
    func pause() { player.pause() }

    func startObservingTime(interval: Double, onTick: @escaping @MainActor (Double) -> Void) {
        stopObservingTime()
        let cmInterval = CMTime(seconds: interval, preferredTimescale: 600)
        timeObserverToken = player.addPeriodicTimeObserver(
            forInterval: cmInterval,
            queue: .main
        ) { time in
            let secs = CMTimeGetSeconds(time)
            MainActor.assumeIsolated {
                onTick(secs.isFinite ? secs : 0)
            }
        }
    }

    func stopObservingTime() {
        if let token = timeObserverToken {
            player.removeTimeObserver(token)
            timeObserverToken = nil
        }
        statusObserver?.invalidate()
        statusObserver = nil
    }

    func observeLoadStatus(_ onChange: @escaping @MainActor (VideoLoadStatus) -> Void) {
        // Initial dispatch so the parent observes the current state.
        onChange(loadStatus)
        statusObserver?.invalidate()
        statusObserver = player.observe(\.currentItem?.status, options: [.new]) { [weak self] _, _ in
            guard let self else { return }
            let status = self.loadStatus
            DispatchQueue.main.async {
                MainActor.assumeIsolated { onChange(status) }
            }
        }
    }
}

/// `@MainActor` viewmodel owning playback state and bridging the underlying
/// AVPlayer-shaped engine. The parent owns the `currentTime` / `isPlaying`
/// bindings so U6 (timeline) can scrub and U8 (composition) can observe.
@MainActor
final class VideoPlayerPaneModel: ObservableObject {
    @Published private(set) var currentTime: Double = 0
    @Published private(set) var loadStatus: VideoLoadStatus = .loading
    /// External flag — flipping it from outside calls `play()` / `pause()`
    /// on the engine. Reading it reflects the most-recent caller intent,
    /// not whether the engine is actually rendering frames.
    @Published var isPlaying: Bool = false {
        didSet {
            guard oldValue != isPlaying else { return }
            if isPlaying { engine.play() } else { engine.pause() }
        }
    }

    let engine: VideoPlaybackEngine

    /// True while `seek(toSeconds:)` is in flight from a user-driven scrub.
    /// The timeline (U6) writes through this flag to suppress the
    /// playback-cursor → timeline feedback loop the plan calls out under
    /// its risks table.
    private(set) var isSeekingFromScrub = false

    init(engine: VideoPlaybackEngine, observeIntervalSeconds: Double = 0.1) {
        self.engine = engine
        engine.startObservingTime(interval: observeIntervalSeconds) { [weak self] secs in
            guard let self else { return }
            // Only propagate if the parent isn't currently driving a scrub.
            // Without this, the periodic observer can publish a value that
            // races a fresh user seek and snaps the cursor back.
            if !self.isSeekingFromScrub {
                self.currentTime = secs
            }
        }
        engine.observeLoadStatus { [weak self] status in
            self?.loadStatus = status
        }
    }

    func seek(toSeconds seconds: Double) {
        isSeekingFromScrub = true
        engine.seek(toSeconds: seconds)
        currentTime = seconds
        // The seek lands quickly; clear the flag on the next runloop turn so
        // the periodic observer's first post-seek tick can resume cursor
        // updates without clobbering the user's drag endpoint.
        DispatchQueue.main.async { [weak self] in
            self?.isSeekingFromScrub = false
        }
    }

    /// Called from the view's `.onDisappear`. Removes the periodic-time
    /// observer so the engine doesn't retain the model after the pane
    /// disappears.
    func tearDown() {
        engine.stopObservingTime()
    }
}

/// Plan U5: AVKit-backed video pane embedded in the review window.
/// The view itself is intentionally thin — all behavior lives on
/// `VideoPlayerPaneModel` so the bindings and lifecycle are unit-testable
/// without a real media asset.
struct VideoPlayerPane: View {
    @ObservedObject var model: VideoPlayerPaneModel

    var body: some View {
        ZStack {
            switch model.loadStatus {
            case .loading:
                VStack(spacing: 8) {
                    ProgressView()
                    Text("Loading video…")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            case .ready:
                // Cast is safe: production composition always builds the model
                // with a LiveVideoPlaybackEngine (AVPlayerNSView requires a
                // real AVPlayer). Tests drive the model with a fake engine
                // and don't render this view.
                //
                // We deliberately use AVKit's AVPlayerView (wrapped via
                // NSViewRepresentable) instead of SwiftUI's `VideoPlayer`
                // primitive: on macOS 26 / SwiftUI 7.5.3 the latter aborts
                // in `getSuperclassMetadata` inside `_AVKit_SwiftUI` when
                // it's composed under a layout modifier and a transition
                // (see crash dump in PR #194 thread). AVPlayerView handles
                // its own aspect-correct sizing inside whatever frame the
                // parent gives it, so no .aspectRatio modifier is needed.
                if let live = model.engine as? LiveVideoPlaybackEngine {
                    AVPlayerNSView(player: live.player)
                } else {
                    Color.black
                }
            case .failed(let message):
                VStack(spacing: 8) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                    Text("Couldn't load this recording.")
                        .font(.body)
                    Text(message)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }
                .padding()
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.black.opacity(0.05))
        .onDisappear { model.tearDown() }
    }
}

/// AVKit's `AVPlayerView` wrapped as an `NSViewRepresentable` (plan U5).
///
/// Direct replacement for SwiftUI's `VideoPlayer` primitive. The latter
/// crashes on macOS 26 / SwiftUI 7.5.3 with a Swift runtime metadata
/// fatal error (`getSuperclassMetadata + 828` → `swift::fatalError` in
/// `_AVKit_SwiftUI`) when its view body gets composed under a layout
/// modifier (e.g. `.aspectRatio`) and a transition. AVPlayerView is the
/// AppKit-native control AVKit recommends for macOS playback anyway —
/// it gives full control over the chrome and sizes itself aspect-
/// correctly inside its parent frame.
struct AVPlayerNSView: NSViewRepresentable {
    let player: AVPlayer

    func makeNSView(context: Context) -> AVPlayerView {
        let view = AVPlayerView()
        view.player = player
        // `.inline` mounts standard playback controls (play/pause, scrub,
        // volume) at the bottom of the view. Operators expect them for
        // pre-upload review; `.none` would force them to rely entirely on
        // the timeline pane for transport.
        view.controlsStyle = .inline
        view.showsFullScreenToggleButton = false
        // Default behavior is aspect-fit inside the frame, which is what we
        // want — no explicit aspect ratio modifier needed on the SwiftUI
        // side.
        return view
    }

    func updateNSView(_ view: AVPlayerView, context: Context) {
        // Reassign only on identity change. AVPlayer is a reference type, so
        // pointer equality is the right check; assigning the same player
        // unconditionally would still work but triggers an unnecessary
        // AVPlayerView teardown/setup cycle.
        if view.player !== player {
            view.player = player
        }
    }
}
