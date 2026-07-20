import SwiftUI

// Search U8 (R6): the on-by-default disclosure surface — an onboarding step on new
// installs and a one-time post-update sheet on existing installs. Copy + actions
// live in `SearchDisclosureController` / `SearchDisclosureCopy` (string-asserted by
// tests); this is the thin render layer.

struct SearchDisclosureView: View {
    @ObservedObject var controller: SearchDisclosureController
    /// The configured retention bound, shown in the copy (default 30 days).
    var retentionDays: Int = 30
    /// Called after the user acknowledges (enabled) or declines, so the host can
    /// advance the wizard / dismiss the sheet.
    var onResolved: () -> Void = {}

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Label(SearchDisclosureCopy.title, systemImage: "magnifyingglass")
                .font(.title2.weight(.semibold))

            Text(SearchDisclosureCopy.intro)
                .foregroundStyle(.secondary)

            VStack(alignment: .leading, spacing: 10) {
                ForEach(SearchDisclosureCopy.points(retentionDays: retentionDays), id: \.self) { point in
                    Label {
                        Text(point)
                    } icon: {
                        Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                    }
                    .font(.callout)
                }
            }

            Text(SearchDisclosureCopy.limitNote)
                .font(.footnote)
                .foregroundStyle(.secondary)

            if case .failed(let message) = controller.phase {
                Text(message).font(.callout).foregroundStyle(.red)
            }

            HStack {
                Button(SearchDisclosureCopy.declineButton) {
                    Task { await controller.decline(); resolveIfSettled() }
                }
                .keyboardShortcut(.cancelAction)

                Spacer()

                Button(SearchDisclosureCopy.enableButton) {
                    Task { await controller.enable(); resolveIfSettled() }
                }
                .keyboardShortcut(.defaultAction)
                .buttonStyle(.borderedProminent)
                .disabled(controller.phase == .enabling)
            }
        }
        .padding(24)
        .frame(minWidth: 460)
    }

    private func resolveIfSettled() {
        switch controller.phase {
        case .enabled, .declined:
            onResolved()
        default:
            break
        }
    }
}
