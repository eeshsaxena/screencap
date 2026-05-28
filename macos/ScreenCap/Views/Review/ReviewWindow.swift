import SwiftUI

/// Stable scene id for the per-recording review `WindowGroup`. Shared by the
/// scene declaration in `ScreenCapApp` and the row-button callsite in
/// `RecordingsListView` so producer and consumers can't drift.
let ReviewWindowID = "review"

/// Per-recording review window scaffold (plan U3). A `WindowGroup` scene keyed
/// on the recording name materializes a fresh window for every
/// `openWindow(id: ReviewWindowID, value: name)` call, satisfying R3
/// (multiple concurrent windows). The singleton `Window` used by the main
/// scene is the wrong primitive here — see
/// docs/solutions/ui-bugs/swiftui-windowgroup-vs-window-singleton-scene-2026-05-18.md
/// (SCR-55) for why the two scene types are not interchangeable.
///
/// U3 ships the scaffold only — the body is a placeholder showing the
/// recording's name. U5 (video) + U6 (timeline) + U7 (upload) + U8
/// (composition) replace this body with the live review surface. Closing the
/// window emits no upload action — Cancel is the default exit per R7.
struct ReviewWindow: View {
    let recordingName: String

    var body: some View {
        VStack(spacing: 12) {
            Text("Review")
                .font(.headline)
                .foregroundStyle(.secondary)
            Text(recordingName)
                .font(.title2)
                .textSelection(.enabled)
            Text("Preparing review surface…")
                .font(.caption)
                .foregroundStyle(.tertiary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(24)
        // Sized for the eventual video-on-top + timeline-below composition.
        // The min-frame keeps the scaffold useful for manual smoke even before
        // U5/U6 land; the real panes will override these constraints when they
        // replace the body.
        .frame(minWidth: 720, minHeight: 540)
        .navigationTitle(recordingName)
    }
}
