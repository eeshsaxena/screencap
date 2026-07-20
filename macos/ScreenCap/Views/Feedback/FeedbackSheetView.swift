import AppKit
import SwiftUI
import UniformTypeIdentifiers

/// The in-app feedback form (feat/in-app-feedback-form U4): type picker,
/// message, optional email, attachments with selection-time rejection copy,
/// an always-visible metadata line, sending progress, a copyable issue link on
/// success, and per-kind error copy — never one generic "couldn't send" (R9).
///
/// Embeddable body per the `AccountSheetView` pattern: no window-chrome
/// assumptions. Draft state lives on the injected `FeedbackController`, which
/// is owned above the sheet (MainWindow scope, KTD-8) so a dismissed draft
/// survives for the session.
struct FeedbackSheetView: View {
    @ObservedObject var controller: FeedbackController
    @ObservedObject var auth: CloudAuthController
    /// Dismisses the presenting container. In the idle draft state dismissal
    /// is harmless (draft preserved); while `.sending` interactive dismissal
    /// is disabled and only the explicit Cancel button aborts the send.
    var onDismiss: (() -> Void)? = nil

    /// Transient "Copied" confirmation for the success link.
    @State private var copiedIssueLink = false

    private var isSending: Bool { controller.state == .sending }

    var body: some View {
        Group {
            if case .success(let issueURL) = controller.state {
                successContent(issueURL: issueURL)
            } else {
                formContent
            }
        }
        .padding(24)
        .frame(width: 480)
        // An accidental Esc / outside-click must not silently abort an
        // in-flight upload — cancel is the explicit Cancel button (U4).
        .interactiveDismissDisabled(isSending)
        .onAppear { controller.prepareForPresentation(auth: auth.status) }
        // Auth resolves lazily; if it lands while the form is open, the
        // one-shot prefill still gets its chance (KTD-10 — status only).
        .onChange(of: auth.status) { newStatus in
            controller.prefillEmailIfNeeded(from: newStatus)
        }
    }

    // MARK: - Form

    private var formContent: some View {
        VStack(alignment: .leading, spacing: 16) {
            header
            typePicker
            messageEditor
            emailField
            attachmentsSection
            errorSection
            footer
        }
    }

    private var header: some View {
        VStack(spacing: 8) {
            Image(systemName: "bubble.left.and.exclamationmark.bubble.right")
                .font(.largeTitle)
                .foregroundStyle(.secondary)
                .accessibilityHidden(true)
            Text("Send Feedback")
                .font(.headline)
            Text("Report a bug, share feedback, or request a feature — it goes straight to the ScreenCap team.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 400)
        }
        .frame(maxWidth: .infinity)
    }

    private var typePicker: some View {
        Picker("Request type", selection: $controller.requestType) {
            ForEach(FeedbackRequestType.allCases) { type in
                Text(type.label).tag(type)
            }
        }
        .pickerStyle(.segmented)
        .labelsHidden()
        .disabled(isSending)
    }

    /// The message prompt adapts to the selected type so the empty form still
    /// tells the user what to write.
    private var messagePrompt: String {
        switch controller.requestType {
        case .bug: return "What happened? What did you expect instead?"
        case .feedback: return "What's on your mind?"
        case .feature: return "What would you like ScreenCap to do?"
        }
    }

    private var messageEditor: some View {
        VStack(alignment: .leading, spacing: 4) {
            ZStack(alignment: .topLeading) {
                TextEditor(text: $controller.message)
                    .font(.body)
                    .scrollContentBackground(.hidden)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 4)
                    .frame(minHeight: 110, maxHeight: 170)
                    .disabled(isSending)
                    // VoiceOver hears the same guidance the visual placeholder
                    // shows — the overlay itself is hidden from the tree.
                    .accessibilityLabel(
                        controller.message.isEmpty
                            ? "Feedback message. \(messagePrompt)"
                            : "Feedback message"
                    )
                if controller.message.isEmpty {
                    Text(messagePrompt)
                        .font(.body)
                        .foregroundStyle(.tertiary)
                        .padding(.horizontal, 11)
                        .padding(.vertical, 12)
                        .allowsHitTesting(false)
                        .accessibilityHidden(true)
                }
            }
            .background(
                RoundedRectangle(cornerRadius: 6)
                    .fill(Color(nsColor: .textBackgroundColor))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 6)
                    .strokeBorder(.quaternary)
            )
            // The cap silently gates canSend; the disabled Send button must
            // never be the only signal (mirrors the email field's inline copy).
            if FeedbackFormPolicy.isMessageOverCap(controller.message) {
                Text("Your message is over the \(FeedbackCaps.maxMessageChars.formatted()) character limit — trim it to send.")
                    .font(.caption)
                    .foregroundStyle(.red)
            }
        }
    }

    @ViewBuilder
    private var emailField: some View {
        VStack(alignment: .leading, spacing: 4) {
            TextField("Email (optional — so we can follow up)", text: $controller.email)
                .textFieldStyle(.roundedBorder)
                .disabled(isSending)
            if !FeedbackFormPolicy.isEmailAcceptable(controller.email) {
                Text("That doesn't look like an email address. Clear it to send anonymously.")
                    .font(.caption)
                    .foregroundStyle(.red)
            }
        }
    }

    // MARK: - Attachments (R4/R5)

    @ViewBuilder
    private var attachmentsSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(controller.attachments) { attachment in
                attachmentRow(attachment)
            }
            if let rejection = controller.attachmentRejection {
                Text(rejection.message)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Button {
                presentOpenPanel()
            } label: {
                Label("Add Screenshot or Clip…", systemImage: "paperclip")
            }
            .disabled(isSending || controller.attachments.count >= FeedbackCaps.maxAttachments)
            Text("Up to \(FeedbackCaps.maxAttachments) files · 25 MB each · 60 MB total. Clips around 30 seconds work best.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private func attachmentRow(_ attachment: FeedbackAttachment) -> some View {
        HStack(spacing: 8) {
            Image(systemName: attachment.isVideo ? "film" : "photo")
                .foregroundStyle(.secondary)
                .accessibilityHidden(true)
            Text(attachment.fileName)
                .lineLimit(1)
                .truncationMode(.middle)
            Spacer()
            Text(ByteCountFormatter.string(
                fromByteCount: Int64(attachment.sizeBytes), countStyle: .file
            ))
            .font(.caption)
            .foregroundStyle(.secondary)
            // Per-row remove (U4): a mis-attached file can be dropped
            // before send.
            Button {
                controller.removeAttachment(id: attachment.id)
            } label: {
                Image(systemName: "xmark.circle.fill")
                    .foregroundStyle(.secondary)
            }
            .buttonStyle(.plain)
            .disabled(isSending)
            .accessibilityLabel("Remove \(attachment.fileName)")
        }
        .padding(.vertical, 5)
        .padding(.horizontal, 10)
        .background(
            RoundedRectangle(cornerRadius: 6)
                .strokeBorder(.quaternary)
        )
    }

    /// `NSOpenPanel` intake (launch scope; drag-and-drop is a fast-follow).
    /// Presented synchronously on the main thread per the `ClipsView` /
    /// `PrivacySettingsView` idiom. The panel filters to the accepted types,
    /// but selection-time validation in the controller remains the authority
    /// (caps, count, and anything the filter misses).
    private func presentOpenPanel() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = true
        panel.allowedContentTypes = FeedbackCaps.allowedUTTypes
        panel.prompt = "Attach"
        panel.message = "Choose screenshots or short clips to attach"
        if panel.runModal() == .OK {
            controller.addAttachments(urls: panel.urls)
        }
    }

    // MARK: - Failure copy (R9 / KTD-7)

    @ViewBuilder
    private var errorSection: some View {
        if case .failure(let failure) = controller.state {
            VStack(alignment: .leading, spacing: 4) {
                Text(failure.message)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
                if let detail = failure.detail, !detail.isEmpty {
                    Text(detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    // MARK: - Metadata + actions

    private var footer: some View {
        VStack(alignment: .leading, spacing: 10) {
            // R8: the user sees exactly what identifying metadata rides along,
            // before sending — rendered always, never buried in a disclosure.
            Text("Includes: \(controller.metadataSummary)")
                .font(.caption)
                .foregroundStyle(.secondary)
            HStack(spacing: 8) {
                if isSending {
                    ProgressView()
                        .controlSize(.small)
                    Text("Sending…")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                if isSending {
                    // No Esc shortcut here: an accidental Esc must not abort
                    // an in-flight upload — aborting takes an explicit click.
                    Button("Cancel") { controller.cancelSend() }
                } else {
                    Button("Cancel") { onDismiss?() }
                        .keyboardShortcut(.cancelAction)
                }
                Button(retryable ? "Try Again" : "Send") { controller.send() }
                    .buttonStyle(.borderedProminent)
                    // ⌘↩ (not bare ↩ — Return belongs to the message editor).
                    .keyboardShortcut(.return, modifiers: .command)
                    .disabled(!controller.canSend)
            }
        }
    }

    /// Whether the settled failure invites a retry — flips the send button's
    /// label so the recovery action names itself.
    private var retryable: Bool {
        if case .failure(let failure) = controller.state { return failure.retryable }
        return false
    }

    // MARK: - Success (R3)

    private func successContent(issueURL: String?) -> some View {
        VStack(spacing: 16) {
            Image(systemName: "checkmark.circle.fill")
                .font(.largeTitle)
                .foregroundStyle(.green)
                .accessibilityHidden(true)
            Text("Thanks — your feedback was sent.")
                .font(.headline)
            Text("It landed in the team's triage queue.")
                .font(.callout)
                .foregroundStyle(.secondary)
            if let issueURL {
                HStack(spacing: 6) {
                    Text(issueURL)
                        .font(.caption.monospaced())
                        .textSelection(.enabled)
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .frame(maxWidth: 320)
                    Button {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(issueURL, forType: .string)
                        copiedIssueLink = true
                    } label: {
                        Image(systemName: copiedIssueLink ? "checkmark" : "doc.on.doc")
                    }
                    .buttonStyle(.plain)
                    .help("Copy link")
                    .accessibilityLabel(copiedIssueLink ? "Link copied" : "Copy issue link")
                }
            }
            Button("Done") {
                controller.acknowledgeSuccess()
                onDismiss?()
            }
            .buttonStyle(.borderedProminent)
            .keyboardShortcut(.defaultAction)
        }
        .frame(maxWidth: .infinity)
    }
}
