import XCTest
@testable import Screencap

/// Pins the static contract between `DaemonInstallController` and the committed
/// LaunchAgent plist (`Screencap/Resources/com.screencap.daemon.plist`).
///
/// The app registers its background daemon with
/// `SMAppService.agent(plistName: DaemonInstallController.plistName).register()`.
/// That call only succeeds if the shipped plist's filename, `Label`, and
/// `BundleProgram` line up with what the code and the launcher expect. A refactor
/// that renames the plist / label / launcher on ONE side and not the other
/// compiles cleanly but ships a bundle whose first-run onboarding dead-ends at
/// "macos rejected the helper signature" / "the helper plist was not found" for
/// every user. These assertions fail fast in CI instead.
///
/// This is the source-side twin of `script/verify_daemon_registration.sh`, which
/// enforces the same invariants on the *built + signed* artifact at release time
/// (and additionally checks team signing, which only exists on a real build).
final class DaemonRegistrationContractTests: XCTestCase {
    /// The committed plist, read from source (not the built bundle) so the test
    /// pins the checked-in contract regardless of build/copy-phase state.
    private func loadDaemonPlist(
        file: StaticString = #filePath, line: UInt = #line
    ) throws -> [String: Any] {
        // #filePath here is THIS file:
        //   .../macos/ScreencapTests/DaemonRegistrationContractTests.swift
        let plistURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // ScreencapTests/
            .deletingLastPathComponent()   // macos/
            .appendingPathComponent("Screencap/Resources/com.screencap.daemon.plist")
        XCTAssertTrue(
            FileManager.default.fileExists(atPath: plistURL.path),
            "committed LaunchAgent plist not found at \(plistURL.path) — was it renamed?",
            file: file, line: line
        )
        let data = try Data(contentsOf: plistURL)
        let parsed = try PropertyListSerialization.propertyList(
            from: data, options: [], format: nil
        )
        return try XCTUnwrap(parsed as? [String: Any])
    }

    /// The plist's `Label` must equal the filename base that
    /// `DaemonInstallController.plistName` resolves via `SMAppService.agent(plistName:)`.
    func testPlistLabelMatchesControllerPlistName() throws {
        let plist = try loadDaemonPlist()
        let label = try XCTUnwrap(plist["Label"] as? String)
        XCTAssertEqual(label, "com.screencap.daemon")
        // The controller looks the job up by filename; the Label must be that
        // filename minus ".plist".
        XCTAssertEqual("\(label).plist", DaemonInstallController.plistName)
    }

    /// `BundleProgram` must point at the launcher shim `sign_app.sh` /
    /// `verify_daemon_registration.sh` expect to exist in the shipped bundle, and
    /// the first program argument must invoke that same launcher.
    func testPlistBundleProgramPointsAtLauncher() throws {
        let plist = try loadDaemonPlist()
        let bundleProgram = try XCTUnwrap(plist["BundleProgram"] as? String)
        XCTAssertEqual(bundleProgram, "Contents/Resources/screencap-daemon-launcher")

        let programArgs = try XCTUnwrap(plist["ProgramArguments"] as? [String])
        let launcherArg = try XCTUnwrap(programArgs.first)
        XCTAssertEqual(
            (launcherArg as NSString).lastPathComponent, "screencap-daemon-launcher",
            "ProgramArguments[0] must invoke the same launcher named by BundleProgram"
        )
    }
}
