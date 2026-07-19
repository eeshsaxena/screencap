import SwiftUI

/// The prominent, honest note shown before a clip's unmasked video leaves the
/// masked-preview world (SCR-219 / U11): the exported clip's video is
/// capture-blocked (window-level) but NOT text-masked and carries the original,
/// unredacted audio, so it must only be shared with people the operator would
/// show their screen to.
///
/// Single source of truth for the wording so the Review window's clip-export
/// consent (`ReviewWindow.clipHonestyNote`) and the Day-timeline range Clip/Share
/// consent (U11) can never drift. Uses system fonts/colours (not the SC design
/// system) so it renders identically in the Review window's Cocoa-styled panes
/// and the shell.
struct ClipHonestyNote: View {
    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
            VStack(alignment: .leading, spacing: 2) {
                Text("The exported clip's video is not text-masked")
                    .font(.callout.weight(.semibold))
                Text("These preview frames are redacted, but the exported video "
                    + "shows on-screen text that is not masked and includes the "
                    + "original audio. Share this clip only with people you'd show "
                    + "your screen to.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.orange.opacity(0.12))
    }
}
