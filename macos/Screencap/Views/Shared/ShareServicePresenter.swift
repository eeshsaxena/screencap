import AppKit
import SwiftUI

/// Presents the system share sheet (`NSSharingServicePicker`) for a file URL over
/// a SwiftUI surface (U11 range Share + Clips share, F2). A zero-size anchor
/// NSView the picker attaches to; setting `item` non-nil shows the picker relative
/// to it, and `item` is cleared once the picker is dismissed (service chosen or
/// cancelled) so the same file can be re-shared later.
///
/// Upload-share is deliberately NOT one of the offered services here — that
/// consent boundary stays routed through the Review window (KTD-4). This surfaces
/// only the OS-provided recipients (Messages, Mail, AirDrop, …) for the already-cut
/// local clip file.
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
