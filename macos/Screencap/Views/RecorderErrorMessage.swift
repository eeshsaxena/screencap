import SwiftUI

/// Shared text treatment for user-facing recorder warnings/errors surfaced in
/// both the main window and the menu bar flow.
struct RecorderErrorMessage: View {
    let message: String

    var body: some View {
        Text(message)
            .font(.caption)
            .fixedSize(horizontal: false, vertical: true)
    }
}
