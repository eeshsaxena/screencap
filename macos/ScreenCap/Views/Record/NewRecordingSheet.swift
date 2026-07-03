import AVFoundation
import SwiftUI

// U6 — the pre-flight New-recording sheet (design 600–652). An in-window overlay
// (KTD-4), not a native `.sheet`: a dimmed scrim + a 540pt card with capture-mode
// cards, a microphone row with a live level meter, camera + MCP rows (stubbed),
// and Start. Start is wired to the real `RecorderController.start(audio:)`; a
// pre-spawn permission block renders inline (never a modal over the sheet). The
// Window/Area modes (SCR-215), camera (SCR-216), and MCP context (SCR-227) render
// per the design but disabled with their ticket tooltips (KTD-8).
struct NewRecordingSheet: View {
    @EnvironmentObject private var recorder: RecorderController
    @Binding var isPresented: Bool

    @StateObject private var meter = MicLevelMeter()
    @State private var audioOn = true
    @State private var micGranted = false
    @State private var uploadDefault: String?
    @State private var startError: String?

    var body: some View {
        ZStack(alignment: .top) {
            scrim
            panel
                .frame(width: 540)
                .padding(.top, 70)
        }
        .task { await loadPreflight() }
        .onDisappear { meter.stop() }
    }

    private var scrim: some View {
        Color.scDarkCanvas.opacity(0.35)
            .ignoresSafeArea()
            .contentShape(Rectangle())
            .onTapGesture { close() }
            .accessibilityHidden(true)
    }

    private var panel: some View {
        VStack(spacing: 0) {
            header
            body_
        }
        .background(Color.scPaper, in: RoundedRectangle(cornerRadius: SCMetrics.radiusCard))
        .overlay(
            RoundedRectangle(cornerRadius: SCMetrics.radiusCard)
                .strokeBorder(Color.scBorderWarm, lineWidth: 1)
        )
        .shadow(color: Color.scDarkCanvas.opacity(0.35), radius: 30, y: 24)
    }

    // MARK: - Header

    private var header: some View {
        HStack(spacing: 12) {
            Text("New recording")
                .font(SCTypography.sectionTitle)
                .foregroundStyle(Color.scInk)
            Spacer(minLength: 8)
            Text(NewRecordingSheetPolicy.headerCaption(uploadDefault: uploadDefault))
                .font(SCTypography.mono(size: 11))
                .foregroundStyle(Color.scInkMuted)
            Button {
                close()
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(Color.scInkMuted)
            }
            .buttonStyle(.plain)
            .keyboardShortcut(.cancelAction)
            .help("Close")
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 16)
        .background(Color.scCanvas)
        .overlay(alignment: .bottom) {
            Rectangle().fill(Color.scBorderWarm).frame(height: 1)
        }
    }

    // MARK: - Body

    private var body_: some View {
        VStack(spacing: 20) {
            modeCards
            optionRows
            startRow
        }
        .padding(.horizontal, 24)
        .padding(.top, 22)
        .padding(.bottom, 26)
    }

    private var modeCards: some View {
        HStack(spacing: 10) {
            ModeCard(title: "Full screen", icon: .fullScreen, active: true, ticket: nil)
            ModeCard(title: "Window", icon: .window, active: false, ticket: "SCR-215")
            ModeCard(title: "Area", icon: .area, active: false, ticket: "SCR-215")
        }
    }

    private var optionRows: some View {
        VStack(spacing: 10) {
            micRow
            cameraRow
            mcpRow
        }
    }

    private var micRow: some View {
        Button {
            audioOn.toggle()
        } label: {
            HStack(spacing: 10) {
                StatusDot(on: audioOn)
                Text(micLabel)
                    .font(SCTypography.sans(size: 13.5))
                    .foregroundStyle(audioOn ? Color.scInk : Color.scInkSecondary)
                Spacer(minLength: 8)
                MicLevelBars(level: audioOn ? meter.level : 0, active: audioOn)
            }
            .optionRowChrome()
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Microphone")
        .accessibilityValue(audioOn ? "on" : "off")
        .accessibilityAddTraits(.isButton)
    }

    private var micLabel: String {
        guard audioOn else { return "Microphone — off" }
        if micGranted, let name = AVCaptureDevice.default(for: .audio)?.localizedName {
            return "Microphone — \(name)"
        }
        return "Microphone — on"
    }

    // Stub: SCR-216 camera capture + camera bubble overlay
    private var cameraRow: some View {
        HStack(spacing: 10) {
            StatusDot(on: false)
            Text("Camera — off")
                .font(SCTypography.sans(size: 13.5))
                .foregroundStyle(Color.scInkSecondary)
            Spacer(minLength: 8)
            Text("⌘K to toggle")
                .font(SCTypography.mono(size: 11))
                .foregroundStyle(Color.scInkMuted)
        }
        .optionRowChrome()
        .opacity(0.7)
        .help("Coming soon — SCR-216")
    }

    // Stub: SCR-227 attach MCP context to recordings
    private var mcpRow: some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 10) {
                    StatusDot(on: true)
                    Text("Attach MCP context")
                        .font(SCTypography.sans(size: 13.5))
                        .foregroundStyle(Color.scInkSecondary)
                }
                Text("Captures app + doc references alongside the video")
                    .font(SCTypography.sans(size: 12))
                    .foregroundStyle(Color.scInkMuted)
                    .padding(.leading, 17)
            }
            Spacer(minLength: 8)
            // Decorative "on" state (soft teal, design line 643) — the shared
            // pill renders locked via `action: nil`.
            SettingsToggle(on: true, onFill: .scTealSoft, action: nil)
                .accessibilityHidden(true)
        }
        .optionRowChrome()
        .opacity(0.7)
        .help("Coming soon — SCR-227")
    }

    private var startRow: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let startError {
                InlineStartError(message: startError)
            }
            HStack(spacing: 14) {
                Button(action: startTapped) {
                    HStack(spacing: 9) {
                        Circle().fill(Color.scCanvas).frame(width: 9, height: 9)
                        Text("Start recording")
                            .font(SCTypography.sans(size: 14.5, weight: .semibold))
                    }
                    .foregroundStyle(Color.scCanvas)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 13)
                    .background(Color.scTeal, in: Capsule())
                    .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .keyboardShortcut("r", modifiers: [.command, .shift])

                Text("⌘⇧R")
                    .font(SCTypography.mono(size: 12))
                    .foregroundStyle(Color.scInkMuted)
            }
        }
    }

    // MARK: - Actions

    private func close() {
        meter.stop()
        isPresented = false
    }

    private func startTapped() {
        // Pre-spawn permission gate renders inline (KTD-8) rather than popping a
        // modal over the sheet. Only call start() once the gate passes.
        if let reason = recorder.newRecordingBlockReason() {
            startError = reason
            return
        }
        recorder.start(audio: audioOn)
        close()
    }

    /// Read `audio_default` / `upload_default`, resolve mic TCC (without
    /// prompting), and start the meter only when the mic is already granted.
    private func loadPreflight() async {
        let settings = try? await CLIClient.runJSONRaw(["settings", "--json"])
        if let settings, let env = try? JSONDecoder().decode(SettingsEnvelope.self, from: settings) {
            audioOn = NewRecordingSheetPolicy.initialAudioOn(audioDefault: env.settings.audioDefault)
            uploadDefault = env.settings.uploadDefault
        }
        // `authorizationStatus` never prompts — only `requestAccess` does.
        micGranted = AVCaptureDevice.authorizationStatus(for: .audio) == .authorized
        if NewRecordingSheetPolicy.shouldRunMeter(micGranted: micGranted) {
            meter.start()
        }
    }
}

// MARK: - Sub-views

/// A capture-mode card (design 610–621). The active mode gets a teal border +
/// tint; a stubbed mode renders disabled with its ticket tooltip (KTD-8).
private struct ModeCard: View {
    enum Icon { case fullScreen, window, area }
    let title: String
    let icon: Icon
    let active: Bool
    /// SCR ticket when this mode is a stub, else nil (the live Full-screen mode).
    let ticket: String?

    var body: some View {
        VStack(spacing: 8) {
            glyph
            Text(title)
                .font(SCTypography.sans(size: 13, weight: active ? .semibold : .medium))
                .foregroundStyle(active ? Color.scTeal : Color.scInkSecondary)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 13)
        .padding(.horizontal, 12)
        .background(active ? Color.scTeal.opacity(0.06) : Color.clear,
                    in: RoundedRectangle(cornerRadius: SCMetrics.radiusChip))
        .overlay(
            RoundedRectangle(cornerRadius: SCMetrics.radiusChip)
                .strokeBorder(active ? Color.scTeal : Color.scBorderWarm, lineWidth: active ? 2 : 1)
        )
        .opacity(ticket == nil ? 1 : 0.7)
        .help(ticket.map { "Coming soon — \($0)" } ?? "")
        .accessibilityAddTraits(active ? [.isSelected] : [])
    }

    private var glyph: some View {
        RoundedRectangle(cornerRadius: 4)
            .strokeBorder(active ? Color.scTeal : Color.scInkMuted, lineWidth: 2)
            .frame(width: 40, height: 26)
            .overlay {
                switch icon {
                case .window:
                    RoundedRectangle(cornerRadius: 2).fill(Color.scBorderWarm)
                        .frame(width: 18, height: 10)
                        .padding(.trailing, 4).padding(.top, 4)
                        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topTrailing)
                case .area, .fullScreen:
                    EmptyView()
                }
            }
    }
}

/// The leading state dot on an option row: filled teal when on, hollow when off.
private struct StatusDot: View {
    let on: Bool
    var body: some View {
        Circle()
            .fill(on ? Color.scTeal : Color.clear)
            .frame(width: 7, height: 7)
            .overlay(Circle().stroke(on ? Color.clear : Color.scInkMuted, lineWidth: 1.5))
    }
}

/// The 5-bar mic level meter (design 626–632). Bars light teal in proportion to
/// `level`; when inactive (mic off / not granted) they render flat and muted.
private struct MicLevelBars: View {
    let level: Double
    let active: Bool

    private let heights: [CGFloat] = [6, 11, 8, 13, 9]

    var body: some View {
        HStack(alignment: .bottom, spacing: 2) {
            ForEach(Array(heights.enumerated()), id: \.offset) { index, height in
                RoundedRectangle(cornerRadius: 2)
                    .fill(color(forBar: index))
                    .frame(width: 3, height: height)
            }
        }
        .animation(.easeOut(duration: 0.1), value: level)
        .accessibilityHidden(true)
    }

    private func color(forBar index: Int) -> Color {
        guard active else { return Color.scBorderWarm }
        return level * 5 > Double(index) ? Color.scTeal : Color.scBorderWarm
    }
}

private struct InlineStartError: View {
    let message: String
    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(Color.scErrorFg)
            Text(message)
                .font(SCTypography.sans(size: 12.5))
                .foregroundStyle(Color.scInk)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
        .background(Color.scErrorSurface, in: RoundedRectangle(cornerRadius: SCMetrics.radiusMd))
    }
}

private extension View {
    /// Shared bordered chrome for the sheet's option rows (design 624/634/638).
    func optionRowChrome() -> some View {
        self
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .overlay(
                RoundedRectangle(cornerRadius: SCMetrics.radiusChip)
                    .strokeBorder(Color.scBorderWarm, lineWidth: 1)
            )
            .contentShape(Rectangle())
    }
}
