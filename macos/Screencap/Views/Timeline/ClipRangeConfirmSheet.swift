import SwiftUI

// U11 (R9/R11, F2) — the range Clip/Share consent sheet on the Day timeline. It
// shows the selected range and the single-sourced unmasked-video honesty note
// (`ClipHonestyNote`) BEFORE a durable clip is cut — the same consent boundary as
// the Review-window clip export. Confirm cuts the clip via `clip.create`; the
// sheet shows a working state while the cut runs, then dismisses: a Clip is kept
// silently in Clips, a Share hands the cut file to the system share sheet.

/// Which range action the consent sheet is confirming.
enum ClipRangeIntent: Equatable {
    case clip
    case share

    /// The confirm button label.
    var actionTitle: String { self == .share ? "Share clip" : "Save clip" }

    /// The sheet heading.
    var heading: String { self == .share ? "Share this range?" : "Keep this range as a clip?" }

    /// The one-line explainer under the range.
    var explainer: String {
        self == .share
            ? "This cuts the range into a clip, then hands it to the system share sheet. It stays in Clips too."
            : "This cuts the range into a clip and keeps it in Clips — findable there anytime."
    }
}

struct ClipRangeConfirmSheet: View {
    let rangeClockText: String
    let intent: ClipRangeIntent
    /// True while `clip.create` is running — swaps the buttons for a spinner.
    let working: Bool
    var onConfirm: () -> Void
    var onCancel: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(intent.heading)
                .font(.title2.weight(.semibold))
            Text(rangeClockText)
                .font(.headline)
                .foregroundStyle(.primary)
            Text(intent.explainer)
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            // Single-sourced with the Review window's clip export (U11).
            ClipHonestyNote()

            Divider()

            if working {
                ProgressView(intent == .share ? "Preparing clip…" : "Saving clip…")
                    .controlSize(.small)
            } else {
                HStack {
                    Button("Cancel") { onCancel() }
                        .keyboardShortcut(.cancelAction)
                    Spacer()
                    Button(intent.actionTitle) { onConfirm() }
                        .keyboardShortcut(.defaultAction)
                        .buttonStyle(.borderedProminent)
                }
            }
        }
        .padding(24)
        .frame(minWidth: 460, maxWidth: 520)
    }
}
