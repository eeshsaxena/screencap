import SwiftUI

/// In-window banner pinned to the top of the main detail area while a recording
/// is active. Shows elapsed time + Stop button (Unit 13).
struct RecordingBanner: View {
    @EnvironmentObject private var recorder: RecorderController
    /// Drives the red-dot pulse. Toggled on `.onAppear` so SwiftUI sees a
    /// value change and starts the repeating animation; reading `recorder.state`
    /// directly produced a constant value while recording, which the animation
    /// modifier treats as "no change" and never animates.
    @State private var pulsing = false

    var body: some View {
        if recorder.state.isRecording {
            HStack(spacing: SCMetrics.space3) {
                Circle()
                    // Warm-amber recording fill (R7) — distinguished from error
                    // (red triangle) by both hue and shape.
                    .fill(Color.scRecording)
                    .frame(width: 10, height: 10)
                    .opacity(pulsing ? 1.0 : 0.35)
                    .animation(.easeInOut(duration: 1.0).repeatForever(autoreverses: true), value: pulsing)
                    .onAppear { pulsing = true }
                    .onDisappear { pulsing = false }

                Text(label)
                    .font(SCTypography.monoTimer)

                Spacer()

                if let remaining = recorder.quitProgressSecondsRemaining {
                    Text("Finalizing — \(remaining)s remaining")
                        .font(SCTypography.metadata)
                        .foregroundStyle(.secondary)
                }

                Button("Stop") { recorder.stop() }
                    .buttonStyle(AuroraButtonStyle())
                    .disabled({
                        // `stop()` only acts on `.recording`. During `.starting`
                        // (before the `started` stderr event) and `.stopping`
                        // (already in flight) the click would silently no-op.
                        switch recorder.state {
                        case .starting, .stopping: return true
                        case .recording, .idle: return false
                        }
                    }())
            }
            .padding(.horizontal, SCMetrics.space4)
            .padding(.vertical, SCMetrics.space3)
            .background(
                RoundedRectangle(cornerRadius: SCMetrics.radiusMd)
                    .fill(Color.scSurfaceElevated)
            )
            .overlay(
                RoundedRectangle(cornerRadius: SCMetrics.radiusMd)
                    .stroke(Color.scRecording.opacity(0.4), lineWidth: 1)
            )
        }
    }

    private var label: String {
        switch recorder.state {
        case .starting:
            return "Starting…"
        case .recording(let elapsed):
            return "Recording — \(format(elapsed))"
        case .stopping(let quitting):
            return quitting ? "Finalizing for quit…" : "Stopping…"
        case .idle:
            return ""
        }
    }

    private func format(_ seconds: TimeInterval) -> String {
        let total = Int(seconds)
        let h = total / 3600
        let m = (total % 3600) / 60
        let s = total % 60
        if h > 0 {
            return String(format: "%d:%02d:%02d", h, m, s)
        }
        return String(format: "%02d:%02d", m, s)
    }
}
