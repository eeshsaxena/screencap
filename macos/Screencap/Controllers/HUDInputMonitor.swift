import AppKit
import Carbon.HIToolbox

/// Identity of Screencap's single Carbon hotkey ('SCHP', id 1). File-level so both
/// the registration and the @convention(c) event handler (which cannot capture)
/// agree on it, and so the handler can filter out any *other* app-target hotkey.
private let hudHotKeySignature: OSType = 0x53434850  // 'SCHP'
private let hudHotKeyID: UInt32 = 1

// U2 + U3 — the global-input seam driven by `RecorderController` during a
// recording (KTD-4). It owns two focus-independent inputs that the shipped
// app-scoped shortcuts (KTD-13) cannot provide:
//
//   • the ⌘⇧H global hotkey (Carbon `RegisterEventHotKey` — KTD-1): needs no TCC
//     grant, consumes the combo system-wide, fires while the recorded app is
//     focused, and is registered only during a recording so ⌘⇧H is free when idle.
//   • the bottom-edge peek detector (polling `NSEvent.mouseLocation` — KTD-2):
//     needs no TCC grant either (public cursor position), so the whole feature
//     ships with no new permission prompt.
//
// `RecorderController` injects this like `windowLifecycle` and starts it on the
// recording-start edge / stops it on every teardown edge (including the imperative
// `transitionToIdle()` path, which bypasses the `.hideHUD` effect). The factory
// returns a no-op under the XCTest host so controller tests never register a real
// hotkey or spawn a poll timer.

@MainActor
protocol HUDInputMonitor: AnyObject {
    /// Begin monitoring for this recording: register ⌘⇧H and start the peek
    /// detector. Idempotent — a second call while already monitoring is a no-op.
    func startMonitoring(for recorder: RecorderController)
    /// Stop monitoring: unregister ⌘⇧H, stop the poll timer, hide the peek bar.
    /// Idempotent and safe to call when never started.
    func stopMonitoring()
}

enum HUDInputMonitorFactory {
    /// `@MainActor` because it constructs main-actor-isolated implementations; only
    /// ever evaluated as a default arg of the `@MainActor` `RecorderController.init`.
    @MainActor
    static func makeDefault() -> HUDInputMonitor {
        if ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil {
            return NoopHUDInputMonitor()
        }
        return LiveHUDInputMonitor()
    }
}

/// No-op implementation for tests (mirrors `NoopWindowLifecycle`).
@MainActor
final class NoopHUDInputMonitor: HUDInputMonitor {
    func startMonitoring(for recorder: RecorderController) {}
    func stopMonitoring() {}
}

/// Live AppKit + Carbon implementation.
@MainActor
final class LiveHUDInputMonitor: HUDInputMonitor {
    private weak var recorder: RecorderController?
    private var isMonitoring = false

    // ⌘⇧H (Carbon)
    private var hotKeyRef: EventHotKeyRef?
    private var eventHandlerRef: EventHandlerRef?

    // Bottom-edge peek (cursor polling)
    private let peek = HUDPeekPanelController()
    private var pollTimer: Timer?
    private var bandEnterTime: Date?

    /// Height of the reveal band above the visible-frame bottom, the dwell before
    /// reveal, and the poll cadence. Tuned by eye; kept short so the gesture feels
    /// responsive without flickering.
    private static let band: CGFloat = 6
    private static let dwell: TimeInterval = 0.35
    private static let pollInterval: TimeInterval = 0.08

    func startMonitoring(for recorder: RecorderController) {
        self.recorder = recorder
        guard !isMonitoring else { return }
        isMonitoring = true
        registerHotKey()
        startPeekDetector()
    }

    func stopMonitoring() {
        // Cleanup is idempotent — run it unconditionally so a stray hotkey, poll
        // timer, or peek panel can never be orphaned even if `isMonitoring` drifts
        // (e.g. the teardown reaching this twice via different end paths).
        isMonitoring = false
        unregisterHotKey()
        stopPeekDetector()
        peek.hide()
        bandEnterTime = nil
        recorder = nil
    }

    // MARK: - ⌘⇧H hotkey (KTD-1)

    private func registerHotKey() {
        guard hotKeyRef == nil, eventHandlerRef == nil else { return }
        var eventSpec = EventTypeSpec(
            eventClass: OSType(kEventClassKeyboard),
            eventKind: UInt32(kEventHotKeyPressed)
        )
        let selfPtr = Unmanaged.passUnretained(self).toOpaque()
        let installStatus = InstallEventHandler(
            GetApplicationEventTarget(),
            { (_, event, userData) -> OSStatus in
                // @convention(c): no captures. Confirm the fired hotkey is *our*
                // ⌘⇧H (not some other app-target hotkey a library might register),
                // then recover the instance and hop to the main actor to toggle.
                guard let userData else { return noErr }
                var firedID = EventHotKeyID()
                let status = GetEventParameter(
                    event,
                    EventParamName(kEventParamDirectObject),
                    EventParamType(typeEventHotKeyID),
                    nil,
                    MemoryLayout<EventHotKeyID>.size,
                    nil,
                    &firedID
                )
                guard status == noErr,
                      firedID.signature == hudHotKeySignature,
                      firedID.id == hudHotKeyID
                else { return noErr }
                let monitor = Unmanaged<LiveHUDInputMonitor>.fromOpaque(userData).takeUnretainedValue()
                Task { @MainActor in monitor.handleHotKey() }
                return noErr
            },
            1,
            &eventSpec,
            selfPtr,
            &eventHandlerRef
        )
        guard installStatus == noErr else {
            eventHandlerRef = nil
            NSLog("Screencap: HUD hotkey handler install failed (OSStatus \(installStatus)); menu-bar restore remains available.")
            return
        }
        // ⌘⇧H. Command+Shift avoids the macOS-15 modifier-only-hotkey bug.
        let hotKeyID = EventHotKeyID(signature: hudHotKeySignature, id: hudHotKeyID)
        let status = RegisterEventHotKey(
            UInt32(kVK_ANSI_H),
            UInt32(cmdKey | shiftKey),
            hotKeyID,
            GetApplicationEventTarget(),
            0,
            &hotKeyRef
        )
        if status != noErr {
            // ⌘⇧H already claimed by another consumer, or system-reserved. Degrade
            // gracefully: the shipped menu-bar "Show recording controls" item stays
            // authoritative rather than leaving a silent dead hotkey.
            hotKeyRef = nil
            NSLog("Screencap: ⌘⇧H registration failed (OSStatus \(status)); menu-bar 'Show recording controls' remains available.")
        }
    }

    private func unregisterHotKey() {
        if let hotKeyRef { UnregisterEventHotKey(hotKeyRef) }
        hotKeyRef = nil
        if let eventHandlerRef { RemoveEventHandler(eventHandlerRef) }
        eventHandlerRef = nil
    }

    private func handleHotKey() {
        recorder?.toggleRecordingHUD()
        // If the toggle just restored the pill, hide the peek bar immediately so the
        // bar and the pill are never on screen together (the peek-click path does
        // the same); otherwise the bar would linger until the next poll tick.
        if recorder?.hudHidden == false {
            peek.hide()
            bandEnterTime = nil
        }
    }

    // MARK: - Bottom-edge peek detector (KTD-2)

    private func startPeekDetector() {
        guard pollTimer == nil else { return }
        let timer = Timer(timeInterval: Self.pollInterval, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.peekTick() }
        }
        // `.common` so the peek still tracks while a menu/tracking loop is active.
        RunLoop.main.add(timer, forMode: .common)
        pollTimer = timer
    }

    private func stopPeekDetector() {
        pollTimer?.invalidate()
        pollTimer = nil
    }

    private func peekTick() {
        guard let recorder else { peek.hide(); bandEnterTime = nil; return }
        let isRecording: Bool = {
            if case .recording = recorder.state { return true } else { return false }
        }()
        let hidden = recorder.hudHidden

        let cursor = NSEvent.mouseLocation
        let visible = NSScreen.main?.visibleFrame ?? .zero
        let inBand = HUDPeekPolicy.cursorInBottomBand(
            cursor: cursor, visibleFrame: visible, band: Self.band
        )
        // Once revealed, the bar's own frame is part of the keep-alive region so
        // moving the cursor up off the edge band to click it doesn't dismiss it
        // first (the dead-end the design review flagged). A little padding gives
        // the pointer slack around the bar.
        let overBar = peek.frameOnScreen.map { $0.insetBy(dx: -8, dy: -8).contains(cursor) } ?? false

        let mayReveal = HUDPeekPolicy.shouldReveal(
            isRecording: isRecording, hudHidden: hidden, cursorInBand: inBand
        )
        let keepAlive = HUDPeekPolicy.shouldReveal(
            isRecording: isRecording, hudHidden: hidden, cursorInBand: inBand || overBar
        )

        if peek.isVisible {
            if !keepAlive { peek.hide(); bandEnterTime = nil }
            return
        }

        // Not yet shown: require a dwell in the band before revealing.
        guard mayReveal else { bandEnterTime = nil; return }
        if let entered = bandEnterTime {
            if Date().timeIntervalSince(entered) >= Self.dwell {
                showPeek(for: recorder)
            }
        } else {
            bandEnterTime = Date()
        }
    }

    private func showPeek(for recorder: RecorderController) {
        peek.show(recorder: recorder, onActivate: { [weak self] in
            self?.recorder?.showRecordingHUD()
            self?.peek.hide()
            self?.bandEnterTime = nil
        })
        bandEnterTime = nil
    }
}
