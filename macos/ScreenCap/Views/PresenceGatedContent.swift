import SwiftUI

// Search U6 (R4): a reusable wrapper that hides corpus content behind present-user
// auth. A surface that shows corpus stills (search-result previews, the truth pane,
// any full-size still) wraps its content in `PresenceGatedContent(gate:)`; the
// content renders only while the shared `PresenceGate` is unlocked, otherwise a lock
// affordance prompts (Touch ID / password) and, on success, opens the session grace
// window so the rest of the grid renders without re-prompting.
//
// The gate is passed in (owned as an @StateObject by the shell), so one grace window
// is shared across every gated surface. NOT applied to video playback, the event
// timeline, or library-card poster thumbnails (those are not corpus stills).

struct PresenceGatedContent<Content: View>: View {
    let gate: PresenceGate
    var reason: String = "View your recording history"
    @ViewBuilder var content: () -> Content

    @State private var unlocked = false
    @State private var prompting = false

    var body: some View {
        Group {
            if unlocked || gate.isUnlocked {
                content()
            } else {
                lockPlaceholder
            }
        }
        .task { unlocked = gate.isUnlocked }
    }

    private var lockPlaceholder: some View {
        VStack(spacing: 12) {
            Image(systemName: "lock.fill")
                .font(.system(size: 28))
                .foregroundStyle(.secondary)
            Text("Locked")
                .font(.headline)
            Text("Unlock to view your recording history.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Button("Unlock") {
                guard !prompting else { return }
                prompting = true
                Task {
                    let ok = await gate.ensurePresent(reason: reason)
                    unlocked = ok
                    prompting = false
                }
            }
            .buttonStyle(.borderedProminent)
            .disabled(prompting)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(24)
    }
}
