import AppKit
import SwiftUI

/// Presents the system share sheet (`NSSharingServicePicker`) for a URL over a
/// SwiftUI surface (U11 range Share + Clips share, F2; SCR-299 share links). A
/// zero-size anchor NSView the picker attaches to; setting `item` non-nil shows
/// the picker relative to it, and `item` is cleared once the picker is dismissed
/// (service chosen or cancelled) so the same item can be re-shared later.
///
/// Handles two kinds of URL, and the distinction matters:
///
/// * A **local file URL** — an already-cut clip (U11/F2).
/// * A **share link** — the `https` URL minted by SCR-299. Its fragment carries
///   the recording's decryption key, so whoever receives it can view the
///   recording. The window states that before the link is ever copied (R7).
///
/// **Uploading** is still deliberately NOT one of the offered services: that
/// consent boundary stays routed through the Review window (KTD-4). Handing an
/// already-minted link to Messages or Mail is not an upload — the upload already
/// happened, and the user chose to share it — so it does not cross that boundary.
struct ShareServicePresenter: NSViewRepresentable {
    @Binding var item: URL?

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> NSView {
        NSView(frame: .zero)
    }

    func updateNSView(_ nsView: NSView, context: Context) {
        // Re-bind the dismiss callback each update so it always writes through the
        // current binding (the representable struct is recreated per update).
        context.coordinator.onDismiss = { item = nil }
        // `item` only goes non-nil after a user action (a Clip/Share tap), so the
        // anchor is already in the window hierarchy by the time we present.
        guard let url = item, !context.coordinator.presenting else { return }
        context.coordinator.presenting = true
        let picker = NSSharingServicePicker(items: [url])
        picker.delegate = context.coordinator
        let rect = nsView.bounds.isEmpty ? CGRect(x: 0, y: 0, width: 1, height: 1) : nsView.bounds
        picker.show(relativeTo: rect, of: nsView, preferredEdge: .minY)
    }

    final class Coordinator: NSObject, NSSharingServicePickerDelegate {
        var presenting = false
        var onDismiss: (() -> Void)?

        func sharingServicePicker(
            _ picker: NSSharingServicePicker,
            didChoose service: NSSharingService?
        ) {
            // Fires on both a chosen service and a cancelled dismissal (nil).
            presenting = false
            onDismiss?()
        }
    }
}
