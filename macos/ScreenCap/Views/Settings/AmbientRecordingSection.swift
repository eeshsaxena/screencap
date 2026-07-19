import SwiftUI

/// SCR-214 U12 — the Ambient recording controls, hosted in the Privacy settings
/// pane (the natural home: always-on capture is a privacy concern and this pane
/// already owns the capture/keep-local/mask toggles). Mirrors that pane's row
/// conventions — `SettingsToggle`, inline errors, `.padding(.vertical, 16)` rows
/// with 1pt dividers — and its consent posture (the E2EE row gates an off→on
/// flip behind a disclosure; here the first enable gates behind a consent
/// sheet).
///
/// Self-contained: it owns its own `AmbientController` as a `@StateObject`
/// rather than an injected `@EnvironmentObject`, so wiring it in is a one-line,
/// additive change to the pane (no app-root environment plumbing). All state
/// reflects the daemon's CONFIRMED `ambient.status`; the enable toggle shows a
/// BLOCKED state (never a silent on-with-nothing-recording) when the daemon
/// reports a degraded reason.
struct AmbientRecordingSection: View {
    @StateObject private var ambient = AmbientController()

    /// Drives the first-run always-on-audio consent sheet (R1).
    @State private var showConsent = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            enableRow
            // The sub-controls only make sense once ambient is enabled AND not
            // blocked — a blocked enable shows why, not a set of dead toggles.
            if ambient.enabled, !ambient.isBlocked {
                rowDivider
                autostartRow
                rowDivider
                pauseRow
            }
        }
        .task { await ambient.load() }
        .sheet(isPresented: $showConsent) {
            AmbientAudioConsentSheet(
                onAccept: {
                    showConsent = false
                    Task { await ambient.acceptAudioConsentAndEnable() }
                },
                onDecline: { showConsent = false }
            )
        }
    }

    private var rowDivider: some View {
        Rectangle().fill(Color.scFillSubtle).frame(height: 1)
    }

    // MARK: - Enable row (consent-gated first enable, R1; blocked state, R2/U2)

    private var enableRow: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .center, spacing: 12) {
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 8) {
                        Text(AmbientSettingsCopy.title)
                            .font(SCTypography.sans(size: 14, weight: .semibold))
                            .foregroundStyle(Color.scInk)
                        if let stateChip {
                            chip(stateChip.text, color: stateChip.color)
                        }
                    }
                    Text(AmbientSettingsCopy.sub)
                        .font(SCTypography.sans(size: 12.5))
                        .foregroundStyle(Color.scInkMuted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 8)
                if ambient.pending {
                    ProgressView().controlSize(.small)
                }
                SettingsToggle(on: ambient.enabled, action: onToggleEnable)
                    .disabled(ambient.pending)
            }
            enableFooter
        }
        .padding(.vertical, 16)
    }

    /// Blocked state takes precedence (a degraded ambient must say WHY, R2/U2);
    /// otherwise a failed-write error surfaces inline.
    @ViewBuilder
    private var enableFooter: some View {
        if ambient.isBlocked, let reason = ambient.degraded {
            Text(AmbientSettingsCopy.blockedCaption(reason))
                .font(SCTypography.sans(size: 12))
                .foregroundStyle(Color.scAmberText)
                .fixedSize(horizontal: false, vertical: true)
        } else if let error = ambient.lastError {
            Text(AmbientSettingsCopy.writeError(error))
                .font(SCTypography.sans(size: 12))
                .foregroundStyle(Color.scRust)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// The small state chip beside the title, keyed to the CONFIRMED status.
    private var stateChip: (text: String, color: Color)? {
        if ambient.isBlocked { return ("BLOCKED", .scAmberText) }
        guard ambient.enabled else { return nil }
        if ambient.active, ambient.paused { return ("PAUSED", .scInkMuted) }
        if ambient.active { return ("RECORDING", .scTeal) }
        // Enabled but the detached spawn hasn't reported active yet.
        return ("STARTING", .scInkMuted)
    }

    private func onToggleEnable() {
        guard !ambient.pending else { return }
        if ambient.enabled {
            Task { await ambient.setEnabled(false) }
        } else {
            // Turning ON routes through the consent gate: the first enable is
            // blocked until the always-on-audio consent is accepted (R1). The
            // gate issues NO daemon write when consent is missing.
            Task {
                if await ambient.enableWithConsentGate() == .needsConsent {
                    showConsent = true
                }
            }
        }
    }

    // MARK: - Auto-start-on-login row (defaults on when enabled; independent, R2)

    private var autostartRow: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(AmbientSettingsCopy.autostartTitle)
                    .font(SCTypography.sans(size: 14, weight: .semibold))
                    .foregroundStyle(Color.scInk)
                Text(AmbientSettingsCopy.autostartSub)
                    .font(SCTypography.sans(size: 12.5))
                    .foregroundStyle(Color.scInkMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            SettingsToggle(on: ambient.autostart, action: onToggleAutostart)
                .disabled(ambient.pending)
        }
        .padding(.vertical, 16)
    }

    private func onToggleAutostart() {
        guard !ambient.pending else { return }
        let target = !ambient.autostart
        Task { await ambient.setAutostart(target) }
    }

    // MARK: - Pause / resume row (U4)

    private var pauseRow: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(AmbientSettingsCopy.pauseTitle)
                    .font(SCTypography.sans(size: 14, weight: .semibold))
                    .foregroundStyle(Color.scInk)
                Text(ambient.paused
                     ? AmbientSettingsCopy.pauseSubPaused
                     : AmbientSettingsCopy.pauseSubRecording)
                    .font(SCTypography.sans(size: 12.5))
                    .foregroundStyle(Color.scInkMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            pauseButton
        }
        .padding(.vertical, 16)
    }

    private var pauseButton: some View {
        // Disabled until a live ambient recording exists (nothing to pause during
        // the STARTING window) or while a request round-trips.
        let disabled = ambient.pending || !ambient.active
        return Button(action: onTogglePause) {
            Text(ambient.paused ? AmbientSettingsCopy.resumeAction : AmbientSettingsCopy.pauseAction)
                .font(SCTypography.sans(size: 12.5))
                .foregroundStyle(Color.scInkSecondary)
                .padding(.horizontal, 14)
                .padding(.vertical, 7)
                .overlay(Capsule().strokeBorder(Color.scBorderWarm, lineWidth: 1))
        }
        .buttonStyle(.plain)
        .disabled(disabled)
        .opacity(disabled ? 0.6 : 1)
    }

    private func onTogglePause() {
        guard !ambient.pending, ambient.active else { return }
        Task {
            if ambient.paused {
                await ambient.resume()
            } else {
                await ambient.pause()
            }
        }
    }

    // MARK: - Chip (mirrors the pane's private chip helper)

    private func chip(_ label: String, color: Color) -> some View {
        Text(label)
            .font(SCTypography.mono(size: 9.5))
            .foregroundStyle(color)
            .padding(.horizontal, 7)
            .padding(.vertical, 2)
            .overlay(Capsule().strokeBorder(color.opacity(0.35), lineWidth: 1))
    }
}

/// The first-run consent sheet (R1). It specifically names ALWAYS-ON AUDIO — that
/// ambient keeps the mic on continuously and can capture other people in meetings
/// — before ambient can start. Only the accept action enables ambient; declining
/// leaves it off. Presented once (the accept persists the consent flag).
struct AmbientAudioConsentSheet: View {
    let onAccept: () -> Void
    let onDecline: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(AmbientSettingsCopy.consentTitle)
                .font(SCTypography.paneHeading)
                .foregroundStyle(Color.scInk)

            // The load-bearing always-on-audio disclosure — headline first so it
            // can't be skimmed past.
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: "mic.fill")
                    .font(.system(size: 15))
                    .foregroundStyle(Color.scAmberText)
                    .accessibilityHidden(true)
                Text(AmbientSettingsCopy.audioHeadline)
                    .font(SCTypography.sans(size: 13.5, weight: .semibold))
                    .foregroundStyle(Color.scInk)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Text(AmbientSettingsCopy.audioBody)
                .font(SCTypography.sans(size: 12.5))
                .foregroundStyle(Color.scInkMuted)
                .fixedSize(horizontal: false, vertical: true)

            VStack(alignment: .leading, spacing: 8) {
                ForEach(AmbientSettingsCopy.consentPoints, id: \.self) { point in
                    HStack(alignment: .top, spacing: 8) {
                        Text("•")
                            .font(SCTypography.sans(size: 12.5))
                            .foregroundStyle(Color.scInkMuted)
                        Text(point)
                            .font(SCTypography.sans(size: 12.5))
                            .foregroundStyle(Color.scInkMuted)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }

            HStack(spacing: 10) {
                Spacer(minLength: 0)
                Button(AmbientSettingsCopy.declineButton, action: onDecline)
                    .buttonStyle(.bordered)
                    .keyboardShortcut(.cancelAction)
                Button(AmbientSettingsCopy.acceptButton, action: onAccept)
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
            }
            .padding(.top, 4)
        }
        .padding(24)
        .frame(width: 460)
    }
}

/// Copy constants for the Ambient section + consent sheet, kept here (mirroring
/// `SearchDisclosureCopy` / `PrivacySettingsCopy`) so the R1 always-on-audio
/// disclosure can be string-asserted without a render tree.
enum AmbientSettingsCopy {
    static let title = "Ambient recording"
    static let sub =
        "Capture your whole day automatically — screen, audio, and transcript — so your "
        + "days and timeline fill themselves. Off until you turn it on."

    // First-run always-on-audio consent (R1).
    static let consentTitle = "Turn on ambient recording"
    static let audioHeadline = "This keeps your microphone on continuously."
    static let audioBody =
        "While ambient recording is running it records audio the whole time — so in meetings "
        + "and calls it can capture other people's voices too, not just yours. You can pause it "
        + "or turn it off whenever you like."
    static let consentPoints = [
        "Everything is recorded and stored only on this Mac — nothing about ambient capture is uploaded.",
        "Blocked and private apps are still cut from capture, and pausing stops the screen and the microphone.",
        "Footage is kept for 30 days by default, then removed automatically — anything you keep as a task stays.",
    ]
    static let acceptButton = "Turn on ambient recording"
    static let declineButton = "Not now"

    static let autostartTitle = "Start at login"
    static let autostartSub = "Resume ambient recording automatically after you log in."

    static let pauseTitle = "Live recording"
    static let pauseSubRecording = "Ambient recording is capturing your screen and audio."
    static let pauseSubPaused = "Paused — nothing is captured until you resume."
    static let pauseAction = "Pause"
    static let resumeAction = "Resume"

    /// Shown when an enabled ambient is degraded (permission-denied /
    /// paywall-blocked / crash-ceiling): the reason is the daemon's
    /// human-readable `degraded` string.
    static func blockedCaption(_ reason: String) -> String {
        "Not recording: \(reason)"
    }

    static func writeError(_ detail: String) -> String {
        "Couldn't update ambient recording: \(detail)"
    }
}
