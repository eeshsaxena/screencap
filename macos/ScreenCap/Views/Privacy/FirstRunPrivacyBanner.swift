import SwiftUI

/// First-run, non-blocking banner pinned at the top of the main detail area
/// (SCR-17 / U5). Discloses the as-configured privacy behavior
/// honestly — including the fact that browser content and AI tools remain
/// visible in playback — and offers two CTAs plus a dismiss `[x]`.
///
/// State is owned by `PrivacyController`; this view is stateless and takes
/// closures for the three exit paths so the gate logic in `MainWindow` can
/// route the navigation side-effects.
struct FirstRunPrivacyBanner: View {
    let onReview: () -> Void
    let onDismiss: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: "lock.shield")
                .font(.title2)
                .foregroundStyle(.tint)
                .padding(.top, 2)

            VStack(alignment: .leading, spacing: 6) {
                Text("Set up your privacy")
                    .font(.headline)
                Text("ScreenCap captures your screen with default privacy rules: password managers are always blocked; banking, payment, and authentication apps are window-masked. **Browser content and AI tools (ChatGPT, Claude) are visible in playback.**")
                    .font(.body)
                    .foregroundStyle(.primary)
                    .fixedSize(horizontal: false, vertical: true)
                Text("For finer control (mask URLs, mask window titles), run `screencap setup` in Terminal.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                HStack(spacing: 8) {
                    Button {
                        onReview()
                    } label: {
                        Text("Review what's captured")
                    }
                    .buttonStyle(.borderedProminent)

                    Button {
                        onDismiss()
                    } label: {
                        Text("I'll configure later")
                    }
                    .buttonStyle(.bordered)
                }
                .padding(.top, 4)
            }

            Spacer(minLength: 0)

            Button {
                onDismiss()
            } label: {
                Image(systemName: "xmark")
                    .font(.caption.bold())
                    .foregroundStyle(.secondary)
                    .padding(6)
            }
            .buttonStyle(.borderless)
            .help("Dismiss")
        }
        .padding(14)
        .background(
            RoundedRectangle(cornerRadius: 10)
                .fill(Color(nsColor: .controlBackgroundColor))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .stroke(.tint.opacity(0.3), lineWidth: 1)
        )
    }
}
