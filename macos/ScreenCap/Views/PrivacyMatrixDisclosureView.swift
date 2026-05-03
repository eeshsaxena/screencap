import SwiftUI

struct PrivacyMatrixDisclosureView: View {
    @EnvironmentObject private var recorder: RecorderController

    let disclosure: PrivacyMatrixDisclosure

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            VStack(alignment: .leading, spacing: 6) {
                Text("Privacy behavior changed")
                    .font(.title2.bold())
                Text("Some app categories now use different capture-time privacy handling. Review this before continuing to record.")
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            VStack(alignment: .leading, spacing: 10) {
                ForEach(displayChanges, id: \.self) { change in
                    Label(change, systemImage: "lock.shield")
                        .labelStyle(.titleAndIcon)
                }
            }

            if !disclosure.optOutCommandExamples.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Opt out per app")
                        .font(.headline)
                    ForEach(disclosure.optOutCommandExamples, id: \.self) { command in
                        Text(command)
                            .font(.system(.caption, design: .monospaced))
                            .textSelection(.enabled)
                            .padding(8)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(.secondary.opacity(0.12), in: RoundedRectangle(cornerRadius: 6))
                    }
                }
            }

            HStack {
                Spacer()
                Button("Continue") {
                    recorder.dismissMatrixDisclosure()
                }
                .keyboardShortcut(.defaultAction)
                .buttonStyle(.borderedProminent)
            }
        }
        .padding(28)
        .frame(width: 560)
    }

    private var displayChanges: [String] {
        let values = disclosure.changes.map(Self.describe)
        return values.isEmpty ? ["ScreenCap will apply the updated privacy matrix for this recording."] : values
    }

    private static func describe(_ change: String) -> String {
        switch change {
        case "chat_email_calendar_video_call_mask_window":
            return "Chat, email, calendar, and video-call windows are masked in internal mode."
        case "ai_assistant_browser_unverified":
            return "AI assistant apps are treated as unverified browser contexts in internal mode."
        default:
            return change.replacingOccurrences(of: "_", with: " ")
        }
    }
}
