import SwiftUI

// SCR-219 (U5) — "Clip this moment" bounds selection on the Day timeline.
//
// This file carries three things:
//   1. `ClipRange` — the resolved `[startMs, endMs)` selection (absolute unix
//      ms), the value carried out-of-band to the Review window via
//      `ReviewWindowOpener.pendingClipRange` (KTD5).
//   2. `ClipSpan` — the fixed-window fallback lengths (15s / 30s / 2m total).
//   3. `ClipBoundsResolver` — the PURE bounds math (snap-to-moment, fixed
//      window, edge clamp, drag adjust, long-clip threshold). It is a
//      file-scope value enum with no SwiftUI / AVFoundation / daemon
//      dependency, mirroring `DayMediaMap`, so `ClipBoundsTests` exercises the
//      whole selection contract without a running app.
// `ClipBoundsView` is the thin SwiftUI surface that renders the readout, the
// span picker (fallback only), the drag handles, the long-clip nudge, and the
// named "Review clip" / "Cancel" controls over that pure core.

// MARK: - Clip domain model

/// A resolved clip selection in absolute unix-epoch **milliseconds**, half-open
/// `[startMs, endMs)` to match every other UI time in the app (chunk windows,
/// `pendingSeekMs`, `DaySegmentRecording`). Carried out-of-band to the Review
/// window (`ReviewWindowOpener.pendingClipRange`) so the review `WindowGroup`
/// scene stays keyed on the recording name alone (KTD5) — a clip review and a
/// whole-recording review of the same recording therefore address the same
/// window key without changing the scene's `String` value type.
struct ClipRange: Equatable, Hashable {
    /// Inclusive start (absolute unix ms).
    let startMs: Int
    /// Exclusive end (absolute unix ms).
    let endMs: Int

    /// Clip length in ms, never negative.
    var durationMs: Int { max(0, endMs - startMs) }
}

/// The fixed-window fallback lengths (R3): TOTAL durations centered on the
/// playhead when no labeled moment sits under it. Default `.thirtySeconds`
/// (AE1).
enum ClipSpan: String, CaseIterable, Identifiable, Hashable {
    case fifteenSeconds
    case thirtySeconds
    case twoMinutes

    var id: String { rawValue }

    /// The window's TOTAL length in ms (centered on the timestamp).
    var totalMs: Int {
        switch self {
        case .fifteenSeconds: return 15_000
        case .thirtySeconds: return 30_000
        case .twoMinutes: return 120_000
        }
    }

    /// Short control label.
    var label: String {
        switch self {
        case .fifteenSeconds: return "15s"
        case .thirtySeconds: return "30s"
        case .twoMinutes: return "2m"
        }
    }
}

// MARK: - Pure bounds math (unit-tested core)

/// Snap-to-moment resolution, fixed-window fallback, edge clamping, drag
/// adjustment, and the long-clip threshold. Pure + deterministic — the whole
/// U5 selection contract, testable without SwiftUI or a daemon.
enum ClipBoundsResolver {

    /// Minimum clip length (ms) a drag can leave — a drag can tighten a range
    /// but never collapse it to zero (AE1: never a zero-length clip).
    static let minDurationMs = 1_000

    /// Clips strictly longer than this get the non-blocking nudge (R2). A fresh
    /// 2-minute fixed window (exactly `120_000`) is intentionally NOT long — the
    /// nudge is for ranges that EXCEED two minutes (a long labeled moment or an
    /// extended drag), not for the largest fixed-window preset.
    static let longClipThresholdMs = 120_000

    // MARK: Snap-to-moment

    /// The `RecordingTask` whose half-open `[startTs, endTs)` contains the
    /// playhead, if any. `RecordingTask` times are unix **seconds**; the
    /// playhead is unix **ms** — converted here.
    ///
    /// Tie-break when several tasks overlap the playhead (KD2): the
    /// **innermost / most-recently-started** task. Ordered deterministically
    /// regardless of input order — largest `startTs` wins; ties broken by the
    /// smaller `endTs` (tighter enclosure), then by the larger `taskIndex`
    /// (unique per recording) so the choice never depends on array order.
    static func containingTask(tasks: [RecordingTask], playheadMs: Int) -> RecordingTask? {
        tasks
            .filter { task in
                let startMs = Int((task.startTs * 1000).rounded())
                let endMs = Int((task.endTs * 1000).rounded())
                return startMs <= playheadMs && playheadMs < endMs
            }
            .max(by: { a, b in
                if a.startTs != b.startTs { return a.startTs < b.startTs }
                if a.endTs != b.endTs { return a.endTs > b.endTs }
                return a.taskIndex < b.taskIndex
            })
    }

    /// Default bounds from a snapped task's `[startTs, endTs)`, clamped to the
    /// recording's available footage.
    static func momentBounds(
        task: RecordingTask,
        footageStartMs: Int,
        footageEndMs: Int
    ) -> ClipRange {
        let start = Int((task.startTs * 1000).rounded())
        let end = Int((task.endTs * 1000).rounded())
        return clamp(startMs: start, endMs: end, footageStartMs: footageStartMs, footageEndMs: footageEndMs)
    }

    // MARK: Fixed-window fallback

    /// A fixed window of `span` TOTAL length **centered** on `centerMs`,
    /// truncated ("clamped") to available footage at the recording edges (R3).
    /// The center stays fixed; near an edge the window is shortened, never
    /// slid — so the duration readout honestly reflects the clamp.
    static func fixedWindow(
        centerMs: Int,
        span: ClipSpan,
        footageStartMs: Int,
        footageEndMs: Int
    ) -> ClipRange {
        let total = span.totalMs
        let half = total / 2
        let start = centerMs - half
        let end = centerMs + (total - half)
        return clamp(startMs: start, endMs: end, footageStartMs: footageStartMs, footageEndMs: footageEndMs)
    }

    // MARK: Clamp + drag adjust

    /// Clamp an arbitrary `[startMs, endMs)` into the recording's footage
    /// window, preserving `start <= end`.
    static func clamp(
        startMs: Int,
        endMs: Int,
        footageStartMs: Int,
        footageEndMs: Int
    ) -> ClipRange {
        let lo = min(footageStartMs, footageEndMs)
        let hi = max(footageStartMs, footageEndMs)
        let clampedStart = min(max(startMs, lo), hi)
        let clampedEnd = min(max(endMs, lo), hi)
        return ClipRange(
            startMs: min(clampedStart, clampedEnd),
            endMs: max(clampedStart, clampedEnd)
        )
    }

    /// Move the START handle to `newStartMs` (drag): clamped to footage on the
    /// left and to `endMs - minDurationMs` on the right, so the handle can
    /// tighten OR extend but never crosses the end handle (R2).
    static func adjustStart(
        _ range: ClipRange,
        toMs newStartMs: Int,
        footageStartMs: Int,
        footageEndMs: Int
    ) -> ClipRange {
        let lo = min(footageStartMs, footageEndMs)
        let ceiling = range.endMs - minDurationMs
        let clamped = min(max(newStartMs, lo), ceiling)
        return ClipRange(startMs: clamped, endMs: range.endMs)
    }

    /// Move the END handle to `newEndMs` (drag): clamped to footage on the
    /// right and to `startMs + minDurationMs` on the left (R2).
    static func adjustEnd(
        _ range: ClipRange,
        toMs newEndMs: Int,
        footageStartMs: Int,
        footageEndMs: Int
    ) -> ClipRange {
        let hi = max(footageStartMs, footageEndMs)
        let floor = range.startMs + minDurationMs
        let clamped = max(min(newEndMs, hi), floor)
        return ClipRange(startMs: range.startMs, endMs: clamped)
    }

    // MARK: Long-clip nudge

    /// Whether `range` is long enough to warrant the non-blocking nudge (R2).
    static func isLongClip(_ range: ClipRange) -> Bool {
        range.durationMs > longClipThresholdMs
    }

    // MARK: Formatting

    /// `m:ss` duration readout for the selection.
    static func durationLabel(ms: Int) -> String {
        let totalSeconds = max(0, ms) / 1000
        return String(format: "%d:%02d", totalSeconds / 60, totalSeconds % 60)
    }
}

// MARK: - Clip-bounds overlay view

/// The bottom-of-playback-pane panel shown while the user is choosing a clip's
/// bounds. Renders a proportional footage track (mirroring `DayStripView`) with
/// two draggable handles that tighten OR extend the selection, a live duration
/// readout, the fixed-window span picker (fallback only), a dismissible
/// long-clip nudge, and the named "Review clip" / "Cancel" controls.
///
/// The bounds MATH lives entirely in `ClipBoundsResolver`; this view only maps
/// drags to ms and forwards them through that pure core, then hands the final
/// range to the parent's confirm/cancel closures.
struct ClipBoundsView: View {
    @Binding var range: ClipRange
    let footageStartMs: Int
    let footageEndMs: Int
    /// True when the range was snapped to a labeled moment — the span picker is
    /// irrelevant (bounds came from the task) and is hidden.
    let snappedToTask: Bool
    let span: ClipSpan
    /// Whether to show the long-clip nudge (parent tracks dismissal).
    let showNudge: Bool
    let onSelectSpan: (ClipSpan) -> Void
    let onDismissNudge: () -> Void
    let onReview: () -> Void
    let onCancel: () -> Void

    private static let trackHeight: CGFloat = 34
    private static let handleWidth: CGFloat = 12
    private let trackSpace = "clipBoundsTrack"

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if showNudge { nudge }
            header
            track
            controls
        }
        .padding(14)
        .frame(maxWidth: 520)
        .background(Color.scInk.opacity(0.9), in: RoundedRectangle(cornerRadius: 12))
        .padding(14)
    }

    // MARK: Header (title + duration readout)

    private var header: some View {
        HStack {
            Text(snappedToTask ? "Clip this moment" : "Clip a window")
                .font(SCTypography.sans(size: 12, weight: .semibold))
                .foregroundStyle(Color.scCanvas)
            Spacer()
            Text(ClipBoundsResolver.durationLabel(ms: range.durationMs))
                .font(SCTypography.mono(size: 12))
                .foregroundStyle(Color.scCanvas)
                .accessibilityLabel("Clip duration \(ClipBoundsResolver.durationLabel(ms: range.durationMs))")
        }
    }

    // MARK: Proportional footage track with two drag handles

    private var track: some View {
        GeometryReader { geo in
            let width = geo.size.width
            let startX = x(forMs: range.startMs, width: width)
            let endX = x(forMs: range.endMs, width: width)
            ZStack(alignment: .leading) {
                // Footage baseline.
                RoundedRectangle(cornerRadius: 4)
                    .fill(Color.scCanvas.opacity(0.18))
                // Selected span.
                RoundedRectangle(cornerRadius: 4)
                    .fill(Color.scTeal.opacity(0.5))
                    .frame(width: max(2, endX - startX))
                    .offset(x: startX)
                handle(atX: startX, isStart: true, width: width)
                handle(atX: endX, isStart: false, width: width)
            }
        }
        .frame(height: Self.trackHeight)
        .coordinateSpace(name: trackSpace)
    }

    private func handle(atX centerX: CGFloat, isStart: Bool, width: CGFloat) -> some View {
        RoundedRectangle(cornerRadius: 3)
            .fill(Color.scCanvas)
            .frame(width: Self.handleWidth, height: Self.trackHeight)
            .overlay(RoundedRectangle(cornerRadius: 3).strokeBorder(Color.scTeal, lineWidth: 1))
            .offset(x: centerX - Self.handleWidth / 2)
            .gesture(
                DragGesture(minimumDistance: 0, coordinateSpace: .named(trackSpace))
                    .onChanged { value in
                        let ms = self.ms(forX: value.location.x, width: width)
                        if isStart {
                            range = ClipBoundsResolver.adjustStart(
                                range, toMs: ms,
                                footageStartMs: footageStartMs, footageEndMs: footageEndMs
                            )
                        } else {
                            range = ClipBoundsResolver.adjustEnd(
                                range, toMs: ms,
                                footageStartMs: footageStartMs, footageEndMs: footageEndMs
                            )
                        }
                    }
            )
            .accessibilityLabel(isStart ? "Clip start handle" : "Clip end handle")
    }

    // MARK: Controls (span picker + review/cancel)

    private var controls: some View {
        HStack(spacing: 10) {
            if !snappedToTask {
                Picker("Length", selection: Binding(
                    get: { span },
                    set: { onSelectSpan($0) }
                )) {
                    ForEach(ClipSpan.allCases) { s in
                        Text(s.label).tag(s)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(maxWidth: 180)
            }
            Spacer()
            Button("Cancel") { onCancel() }
                .buttonStyle(.plain)
                .font(SCTypography.sans(size: 12))
                .foregroundStyle(Color.scCanvas.opacity(0.75))
                .padding(.horizontal, 12)
                .padding(.vertical, 6)
            Button("Review clip") { onReview() }
                .buttonStyle(.plain)
                .font(SCTypography.sans(size: 12, weight: .semibold))
                .foregroundStyle(Color.scInk)
                .padding(.horizontal, 14)
                .padding(.vertical, 6)
                .background(Color.scTeal, in: Capsule())
                .help("Review this clip's range before exporting")
        }
    }

    // MARK: Long-clip nudge (non-blocking, dismissible)

    private var nudge: some View {
        HStack(spacing: 8) {
            Image(systemName: "clock")
                .font(.system(size: 11))
                .foregroundStyle(Color.scAmberHUD)
            Text("This clip is over 2 minutes — consider tightening it.")
                .font(SCTypography.sans(size: 11))
                .foregroundStyle(Color.scCanvas.opacity(0.85))
            Spacer()
            Button {
                onDismissNudge()
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(Color.scCanvas.opacity(0.7))
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Dismiss long-clip notice")
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
        .background(Color.scCanvas.opacity(0.1), in: RoundedRectangle(cornerRadius: 8))
    }

    // MARK: ms <-> x mapping over the full footage window

    private func x(forMs ms: Int, width: CGFloat) -> CGFloat {
        let footageSpan = max(1, footageEndMs - footageStartMs)
        let frac = CGFloat(ms - footageStartMs) / CGFloat(footageSpan)
        return max(0, min(1, frac)) * width
    }

    private func ms(forX x: CGFloat, width: CGFloat) -> Int {
        let footageSpan = max(1, footageEndMs - footageStartMs)
        let frac = max(0, min(1, x / max(1, width)))
        return footageStartMs + Int((frac * CGFloat(footageSpan)).rounded())
    }
}
