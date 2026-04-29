import SwiftUI

/// In-window banner pinned to the top of the main detail area while a recording
/// is active. Shows elapsed time + Stop button (Unit 13).
struct RecordingBanner: View {
    @EnvironmentObject private var recorder: RecorderController

    var body: some View {
        if recorder.state.isRecording {
            HStack(spacing: 12) {
                Circle()
                    .fill(Color.red)
                    .frame(width: 10, height: 10)
                    .opacity(pulseOpacity)
                    .animation(.easeInOut(duration: 1.0).repeatForever(autoreverses: true), value: pulseOpacity)

                Text(label)
                    .font(.headline)
                    .monospacedDigit()

                Spacer()

                if let remaining = recorder.quitProgressSecondsRemaining {
                    Text("Finalizing — \(remaining)s remaining")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                Button("Stop") { recorder.stop() }
                    .buttonStyle(.borderedProminent)
                    .tint(.red)
                    .disabled({
                        if case .stopping = recorder.state { return true }
                        return false
                    }())
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .background(
                RoundedRectangle(cornerRadius: 10)
                    .fill(Color(nsColor: .controlBackgroundColor))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 10)
                    .stroke(Color.red.opacity(0.4), lineWidth: 1)
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

    private var pulseOpacity: Double {
        // Toggling the value drives the repeating animation modifier above.
        recorder.state.isRecording ? 1.0 : 0.4
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
