import SwiftUI

/// The permissions screen (design 87–151) — U11 wizard step 1 AND, since the
/// modal walkthrough sheet's retirement (U14), the app's only permission
/// surface: `PermissionSetupTakeover` presents this same screen for the
/// launch gate and the Privacy pane's "Finish setup" recovery. Left column:
/// heading + the three permission rows with live mono status; right column:
/// the design's drag-icon panel rendered as an instructional illustration
/// wired to the real "Open System Settings" flow, with the watchdog
/// "listening…" footer.
///
/// The daemon-install sub-states (install-needed / installing / failed) embed
/// above the rows — the helper must exist before its TCC rows can be granted.
/// Row semantics carry the retired sheet's hard-won rules: daemon-subject
/// registration before the pane opens (SCR-200 AE2), per-pane row labels
/// (SCR-201 — the Accessibility row is "ScreencapDaemon", not "ScreenCap"),
/// tri-state grants where indeterminate never blocks, the grant-state-timeout
/// heuristic (SCR-200 U6 / R7 — `daemonRowState`), the ad-hoc dev-build
/// advisory, and the CLI-fallback "Restart to apply permissions" escape hatch
/// (TCC per-process caching).
struct OnboardingPermissionsStep: View {
    @EnvironmentObject private var recorder: RecorderController
    @EnvironmentObject private var permissions: PermissionController
    @ObservedObject var daemonInstaller: DaemonInstallController
    let onContinue: () -> Void

    @State private var isPreparingRelaunch = false
    /// When the user last opened each daemon pane (= reached the toggle step).
    /// Drives the SCR-200 U6 grant-state-timeout heuristic: the row can only
    /// flip to "couldn't set this up" after the user has had a real chance to
    /// toggle (R7). Re-tapping the row restarts the budget (the retry).
    @State private var openedDaemonPaneAt: [PrivacyPane: Date] = [:]

    var body: some View {
        HStack(alignment: .top, spacing: 44) {
            leftColumn
                .frame(width: 360)
            illustrationPanel
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .padding(.horizontal, 90)
        .padding(.top, 24)
        .padding(.bottom, 12)
    }

    // MARK: - Left column

    private var leftColumn: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(OnboardingCopy.permissionsHeadline)
                .font(SCTypography.serif(size: 31))
                .foregroundStyle(Color.scInk)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, 10)
            Text(OnboardingCopy.permissionsSub)
                .font(SCTypography.sans(size: 13.5))
                .foregroundStyle(Color.scInkSecondary)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, 26)

            VStack(spacing: 10) {
                if showInstallRow {
                    HelperInstallCard(daemonInstaller: daemonInstaller)
                }
                permissionRow(
                    title: "Screen & system audio",
                    status: daemonStatusLine(for: .screenRecording),
                    granted: permissions.daemonGrant(for: .screenRecording) == .granted,
                    registering: permissions.isDaemonRegistering(.screenRecording)
                ) {
                    openDaemonPane(.screenRecording)
                }
                permissionRow(
                    title: "Accessibility",
                    status: daemonStatusLine(for: .accessibility),
                    granted: permissions.daemonGrant(for: .accessibility) == .granted,
                    registering: permissions.isDaemonRegistering(.accessibility)
                ) {
                    openDaemonPane(.accessibility)
                }
                permissionRow(
                    title: "Microphone",
                    status: microphoneStatusLine,
                    granted: permissions.microphone == .granted,
                    registering: false
                ) {
                    // App-owned (Phase 1c): the in-process request + pane open.
                    permissions.requestAndOpenSettings(for: .microphone, subject: .screenCapApp)
                }
            }

            if permissions.showAdHocDevBuildWarning {
                adHocDevBuildCallout
                    .padding(.top, 10)
            }

            Spacer(minLength: SCMetrics.space5)

            HStack(spacing: 16) {
                OnboardingPrimaryButton(title: "Open System Settings") {
                    openDaemonPane(.screenRecording)
                }
                OnboardingLinkButton(title: "Continue", action: onContinue)
            }

            // CLI-fallback only: TCC caches per-process, so a grant made after
            // launch needs a fresh process (the sheet's SCR-120 affordance).
            // On the daemon path the daemon is the TCC subject — no restart.
            if recorder.transport == .cliFallback {
                Button("Restart to apply permissions") {
                    isPreparingRelaunch = true
                    permissions.relaunchApplication()
                }
                .buttonStyle(.plain)
                .font(SCTypography.sans(size: 12))
                .underline()
                .foregroundStyle(Color.scInkMuted)
                .disabled(isPreparingRelaunch || permissions.isRelaunching || recorder.state.isRecording)
                .padding(.top, 12)
            }
        }
    }

    /// The helper-install card is shown until the daemon is confirmed: either
    /// this session's installer reached `installedAndRunning`, or the daemon
    /// transport is already live (pre-installed machine / replay).
    private var showInstallRow: Bool {
        daemonInstaller.state != .installedAndRunning && recorder.transport != .daemon
    }

    // MARK: - Permission rows (design 94–120; pill chrome + install card shared
    // with the walkthrough sheet via PermissionSetupUI)

    private func permissionRow(
        title: String,
        status: (text: String, color: Color),
        granted: Bool,
        registering: Bool,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(title)
                        .font(SCTypography.sans(size: 13.5, weight: .semibold))
                        .foregroundStyle(Color.scInk)
                    Text(status.text)
                        .font(SCTypography.mono(size: 10.5))
                        .foregroundStyle(status.color)
                }
                Spacer(minLength: 8)
                if registering {
                    ProgressView().controlSize(.small)
                } else {
                    GrantStateBadge(
                        granted: granted,
                        accessibilityLabel: granted ? "Granted" : "Not granted"
                    )
                }
            }
            .permissionPillChrome()
            .contentShape(RoundedRectangle(cornerRadius: SCMetrics.radiusChip))
        }
        .buttonStyle(.plain)
        .disabled(granted)
        .help(granted ? "" : "Open the matching System Settings pane")
    }

    /// Open a daemon pane: daemon-subject registration + System Settings
    /// (SCR-200 AE2). Every tap (re)starts the SCR-200 U6 heuristic budget —
    /// re-tapping a blocked row IS the retry.
    private func openDaemonPane(_ pane: PrivacyPane) {
        openedDaemonPaneAt[pane] = Date()
        permissions.requestAndOpenSettings(for: pane, subject: .daemon)
    }

    /// The design's mono status line, resolved from the daemon's tri-state
    /// grant (honest: indeterminate is "couldn't verify", never granted or
    /// waiting). While the helper isn't running yet, indeterminate is
    /// expected — say so, instead of a "couldn't verify" that reads like a
    /// failure when the real prerequisite is the approve-helper step above.
    /// A row the user opened that never became grantable within the budget
    /// flips to "couldn't set this up" with the row tap as the retry
    /// (SCR-200 U6 / R7 — never a manual "+" add, never a silent advance).
    private func daemonStatusLine(for pane: PrivacyPane) -> (text: String, color: Color) {
        let grant = permissions.daemonGrant(for: pane)
        let elapsed = openedDaemonPaneAt[pane].map { Date().timeIntervalSince($0) }
        let rowState = Self.daemonRowState(
            grant: grant,
            isRegistering: permissions.isDaemonRegistering(pane),
            elapsedSinceOpened: elapsed,
            budget: Self.daemonRowBlockBudget
        )
        if rowState == .blockedWithRetry {
            return ("couldn't set this up · click to retry", .scRust)
        }
        switch grant {
        case .granted:
            return ("granted", .scTeal)
        case .denied:
            return ("required · waiting", .scAmberText)
        case .indeterminate:
            if showInstallRow {
                return ("required · approve the helper above first", .scInkMuted)
            }
            return ("required · couldn't verify yet", .scInkMuted)
        }
    }

    /// The trailing state of a daemon permission row (SCR-200 U6 / R7),
    /// carried over from the retired walkthrough sheet.
    enum DaemonRowState: Equatable {
        case granted
        case registering       // a daemon round-trip is in flight
        case actionable        // normal open/grant — not (yet) blocked
        case blockedWithRetry  // budget exhausted with the row still not granted
    }

    /// Budget after the user opens a pane before a still-denied row is treated
    /// as "couldn't set this up" (R7). Generous so a slow-but-normal toggle
    /// never false-blocks; the exact value is on-device-tuned.
    static let daemonRowBlockBudget: TimeInterval = 25

    /// Resolve a daemon row's state from the registration-outcome +
    /// grant-state-timeout heuristic (R7). There is **no** public, non-SIP API
    /// for TCC row *presence* — an absent row and a present-but-OFF row both
    /// read `denied`, and TCC.db is SIP-protected — so "the row never
    /// appeared" is *inferred*, not read: the pane was opened (registration
    /// fired) AND the grant has not resolved to `granted` within `budget`
    /// after the user reached the toggle step. The block is gated on
    /// `elapsedSinceOpened` so it never fires before the user has had a real
    /// chance to toggle (no immediate post-install false-block).
    /// `indeterminate` ("couldn't verify") keeps the row actionable rather
    /// than blocking, so a transient probe hiccup can't false-block a user
    /// who is actually granted.
    static func daemonRowState(
        grant: DaemonGrantState,
        isRegistering: Bool,
        elapsedSinceOpened: TimeInterval?,
        budget: TimeInterval
    ) -> DaemonRowState {
        if grant == .granted { return .granted }
        if isRegistering { return .registering }
        // Not yet opened → the user hasn't reached the toggle step; never block.
        guard let elapsed = elapsedSinceOpened else { return .actionable }
        // Couldn't verify → keep the row actionable, don't hard-block.
        if grant == .indeterminate { return .actionable }
        // Opened + still denied past the budget → the row didn't take.
        if elapsed >= budget { return .blockedWithRetry }
        return .actionable
    }

    /// Developer-only hint: on an ad-hoc build, TCC grants are orphaned on
    /// every rebuild, so the rows can read "waiting" even though System
    /// Settings shows an earlier build as granted. Explains the cause and the
    /// fix so a developer doesn't chase a phantom permission bug. Never
    /// renders on a signed build (see
    /// `PermissionController.showAdHocDevBuildWarning`).
    private var adHocDevBuildCallout: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "hammer.fill")
                .font(SCTypography.sans(size: 12))
                // Developer advisory — de-colored to neutral (R7), not orange.
                .foregroundStyle(Color.scAdvisoryFg)
                .padding(.top, 2)
                // Decorative — the adjacent warning text conveys the full
                // meaning; keep VoiceOver from announcing the symbol as a
                // separate, content-free focus stop.
                .accessibilityHidden(true)
            Text(PermissionController.adHocDevBuildWarning)
                .font(SCTypography.sans(size: 11.5))
                .foregroundStyle(Color.scInkSecondary)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(12)
        .background(
            RoundedRectangle(cornerRadius: SCMetrics.radiusMd)
                .fill(Color.scAdvisorySurface)
        )
    }

    private var microphoneStatusLine: (text: String, color: Color) {
        switch permissions.microphone {
        case .granted: return ("granted", .scTeal)
        case .denied: return ("optional · off", .scInkMuted)
        case .notDetermined: return ("optional · records narration", .scInkMuted)
        }
    }

    // MARK: - Illustration panel (design 122–149)

    private var illustrationPanel: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("DRAG THE ICON INTO SYSTEM SETTINGS")
                .font(SCTypography.metaMonoSmall)
                .tracking(1.05)
                .foregroundStyle(Color.scInkMuted)
                .padding(.bottom, 20)

            HStack(spacing: 18) {
                appIconTile
                arrowGlyph
                miniSettingsWindow
            }
            .padding(.bottom, 14)

            // Accessibility attributes to the helper's own identity (SCR-201),
            // so that pane needs the *helper* bundle dropped in, not the app.
            helperDragChip
                .padding(.bottom, 18)

            // SCR-201: the row label differs per pane — Screen & System Audio
            // rolls up to the app ("ScreenCap"); Accessibility renders the
            // helper's own row ("ScreencapDaemon"), sometimes next to a stray
            // ScreenCap decoy. Spelling both out here points the user at the
            // right rows; the row may also take a moment to appear.
            Text(
                "Drag the big icon onto the Screen & System Audio Recording list, then switch on “ScreenCap”. "
                + "For Accessibility, drag the “ScreencapDaemon” chip instead — that pane lists the helper's own row. "
                + "Or click the pane preview to open System Settings; if a row hasn't appeared yet, give it a moment and reopen the pane."
            )
            .font(SCTypography.sans(size: 12.5))
            .foregroundStyle(Color.scInkMuted)
            .fixedSize(horizontal: false, vertical: true)

            Spacer(minLength: SCMetrics.space4)

            HStack(spacing: 8) {
                Circle()
                    .fill(listeningLine.color)
                    .frame(width: 7, height: 7)
                Text(listeningLine.text)
                    .font(SCTypography.metaMonoSmall)
                    .foregroundStyle(Color.scInkMuted)
            }
        }
        .padding(.horizontal, 28)
        .padding(.vertical, 26)
        .background(Color.scPaper, in: RoundedRectangle(cornerRadius: SCMetrics.radiusPanel))
        .overlay(
            RoundedRectangle(cornerRadius: SCMetrics.radiusPanel)
                .strokeBorder(Color.scBorderWarm, lineWidth: 1)
        )
    }

    /// The watchdog footer: teal once the required grants are confirmed, amber
    /// while the grant refresh loop is listening.
    private var listeningLine: (text: String, color: Color) {
        if permissions.daemonGrants.allRequiredGranted {
            return ("permission detected — you're set", .scTeal)
        }
        return ("listening for permission change…", .scAmberText)
    }

    /// The draggable app tile (design 124–127). The drag payload is the real
    /// app bundle URL — macOS Privacy panes accept an .app dropped onto their
    /// list, so the design's "drag the icon" interaction works for real for
    /// the app-attributed panes (Screen & System Audio rolls up to the host
    /// app). Accessibility needs the helper bundle instead — see
    /// `helperDragChip`.
    private var appIconTile: some View {
        RoundedRectangle(cornerRadius: 20)
            .fill(Color.scCanvas)
            .frame(width: 84, height: 84)
            .overlay(
                RoundedRectangle(cornerRadius: 20)
                    .strokeBorder(Color.scBorderWarm, lineWidth: 1)
            )
            .overlay(ShellLogoMark(size: 44))
            .shadow(color: Color.scInk.opacity(0.18), radius: 12, y: 10)
            .rotationEffect(.degrees(-4))
            .onDrag { NSItemProvider(object: Bundle.main.bundleURL as NSURL) }
            .help("Drag onto the Screen & System Audio Recording list in System Settings")
            .accessibilityLabel("ScreenCap app icon — drag into the System Settings permission list")
    }

    /// `Contents/Library/LoginItems/ScreencapDaemon.app` inside the app
    /// bundle — the TCC subject the Accessibility pane lists (SCR-201). nil
    /// when the dev build shipped without an embedded helper.
    private static var helperBundleURL: URL? {
        let url = Bundle.main.bundleURL.appendingPathComponent(
            "Contents/Library/LoginItems/ScreencapDaemon.app", isDirectory: true
        )
        return FileManager.default.fileExists(atPath: url.path) ? url : nil
    }

    /// A compact drag source for the helper bundle — the payload Accessibility
    /// actually needs. Hidden when no helper is embedded (some dev builds).
    @ViewBuilder
    private var helperDragChip: some View {
        if let helperURL = Self.helperBundleURL {
            HStack(spacing: 8) {
                ShellLogoMark(size: 14)
                Text("ScreencapDaemon.app — drag me for Accessibility")
                    .font(SCTypography.mono(size: 10.5))
                    .foregroundStyle(Color.scInkSecondary)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(Color.scCanvas, in: Capsule())
            .overlay(
                Capsule().strokeBorder(
                    Color.scBorderWarm, style: StrokeStyle(lineWidth: 1, dash: [4, 3])
                )
            )
            .onDrag { NSItemProvider(object: helperURL as NSURL) }
            .help("Drag onto the Accessibility list in System Settings — that pane lists the helper's own row")
            .accessibilityLabel("ScreencapDaemon helper — drag into the Accessibility permission list")
        }
    }

    private var arrowGlyph: some View {
        Image(systemName: "arrow.right")
            .font(.system(size: 18, weight: .medium))
            .foregroundStyle(Color.scInkMuted)
            .accessibilityHidden(true)
    }

    private var miniSettingsWindow: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 6) {
                Circle().fill(Color.scBorderWarm).frame(width: 8, height: 8)
                Circle().fill(Color.scBorderWarm).frame(width: 8, height: 8)
                Text("Privacy & Security")
                    .font(SCTypography.sans(size: 10.5, weight: .semibold))
                    .foregroundStyle(Color.scInkSecondary)
                    .padding(.leading, 6)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 7)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.scFillSubtle)

            VStack(alignment: .leading, spacing: 7) {
                Text("Screen & System Audio Recording")
                    .font(SCTypography.sans(size: 11))
                    .foregroundStyle(Color.scInkSecondary)
                Button {
                    openDaemonPane(.screenRecording)
                } label: {
                    HStack(spacing: 8) {
                        ShellLogoMark(size: 16).opacity(0.45)
                        Text("enable ScreenCap here")
                            .font(SCTypography.mono(size: 10))
                            .foregroundStyle(Color.scTeal)
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 9)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.scTeal.opacity(0.05))
                    .overlay(
                        RoundedRectangle(cornerRadius: SCMetrics.radiusInner)
                            .strokeBorder(
                                Color.scTeal,
                                style: StrokeStyle(lineWidth: 2, dash: [5, 4])
                            )
                    )
                    .contentShape(RoundedRectangle(cornerRadius: SCMetrics.radiusInner))
                }
                .buttonStyle(.plain)
                .help("Open the Screen Recording pane in System Settings")
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
        }
        .frame(maxWidth: .infinity)
        .overlay(
            RoundedRectangle(cornerRadius: SCMetrics.radiusChip)
                .strokeBorder(Color.scBorderWarm, lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: SCMetrics.radiusChip))
    }
}
