import AppKit
import ApplicationServices
import AVFoundation
import CoreGraphics
import Foundation
import IOKit
import IOKit.hid

/// Custom entry point. When invoked with `--check-permission <name>` we treat
/// this binary as a single-shot CLI that prints `{"granted": true|false}` and
/// exits *before* SwiftUI/AppKit boots. That gives `PermissionController` a
/// fresh-process TCC query to work around the in-process cache on
/// `CGPreflightScreenCaptureAccess`, `AXIsProcessTrusted`,
/// `IOHIDCheckAccess`, and `AVCaptureDevice.authorizationStatus`.
///
/// Without this, the SwiftUI walkthrough keeps showing "denied" after the
/// user grants the permission in System Settings — TCC only refreshes on a
/// fresh process. The CLI uses the same trick (`recorder.py:_check_permission_fresh`).
private func runPermissionCheckIfRequested() {
    let args = CommandLine.arguments
    guard args.count >= 3, args[1] == "--check-permission" else { return }

    let granted: Bool
    switch args[2] {
    case "screen_recording":
        granted = CGPreflightScreenCaptureAccess()
    case "accessibility":
        let opts: NSDictionary = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: false]
        granted = AXIsProcessTrustedWithOptions(opts)
    case "input_monitoring":
        granted = IOHIDCheckAccess(kIOHIDRequestTypeListenEvent) == kIOHIDAccessTypeGranted
    case "microphone":
        granted = AVCaptureDevice.authorizationStatus(for: .audio) == .authorized
    default:
        FileHandle.standardError.write(Data("unknown permission: \(args[2])\n".utf8))
        exit(2)
    }
    print("{\"granted\": \(granted)}")
    exit(0)
}

runPermissionCheckIfRequested()
ScreenCapApp.main()
