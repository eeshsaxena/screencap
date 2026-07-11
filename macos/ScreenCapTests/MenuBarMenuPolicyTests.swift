import XCTest
@testable import ScreenCap

/// Hide control — the menu-bar "Show recording controls" visibility gate.
final class MenuBarMenuPolicyTests: XCTestCase {

    func testVisibleOnlyWhileRecordingAndHidden() {
        XCTAssertTrue(MenuBarMenuPolicy.showRecordingControlsVisible(
            state: .recording(elapsed: 12), hudHidden: true
        ))
    }

    func testHiddenWhenPillIsShown() {
        XCTAssertFalse(MenuBarMenuPolicy.showRecordingControlsVisible(
            state: .recording(elapsed: 12), hudHidden: false
        ))
    }

    /// No restorable pill exists outside `.recording`, even if the flag is set.
    func testHiddenOutsideRecording() {
        let states: [RecordingState] = [
            .idle, .starting, .stopping(quitting: false), .stopping(quitting: true),
        ]
        for state in states {
            XCTAssertFalse(
                MenuBarMenuPolicy.showRecordingControlsVisible(state: state, hudHidden: true),
                "no restorable pill in \(state)"
            )
        }
    }

    // MARK: - Collapsed account section (account-sheet U5)

    /// The status line per persistent auth state while no sign-in flow is
    /// running: identity when signed in, an offline label when the stale token
    /// carries none, and honest signed-out / still-checking lines.
    func testAccountStatusLinePerAuthState() {
        XCTAssertEqual(
            MenuBarMenuPolicy.accountStatusLine(
                status: .signedIn(email: "user@example.com", uid: "u1", stale: false),
                signInFlow: .idle
            ),
            "Signed in: user@example.com"
        )
        XCTAssertEqual(
            MenuBarMenuPolicy.accountStatusLine(
                status: .signedIn(email: nil, uid: nil, stale: true),
                signInFlow: .idle
            ),
            "Signed in (offline)"
        )
        XCTAssertEqual(
            MenuBarMenuPolicy.accountStatusLine(status: .signedOut, signInFlow: .idle),
            "Not signed in"
        )
        XCTAssertEqual(
            MenuBarMenuPolicy.accountStatusLine(status: .unknown, signInFlow: .idle),
            "Checking sign-in…"
        )
    }

    /// An in-flight or failed browser round-trip outranks the persistent
    /// status; the failed line is static copy (no raw reason string — R10, the
    /// pane owns retry + details) and points at the Account entry.
    func testAccountStatusLineSignInFlowTakesPriority() {
        XCTAssertEqual(
            MenuBarMenuPolicy.accountStatusLine(
                status: .signedOut, signInFlow: .inProgress
            ),
            "Signing in… check your browser"
        )
        let failed = MenuBarMenuPolicy.accountStatusLine(
            status: .signedOut,
            signInFlow: .failed("raw envelope reason text")
        )
        XCTAssertEqual(failed, "Sign-in failed — open Account to retry")
        XCTAssertFalse(failed.contains("envelope"), "failed line must not leak the raw reason")
    }

    /// The single account menu item is the "Account…" entry into the shared
    /// pane — Sign In / Sign Out menu items are retired (they live in the
    /// Account & Plan sheet/pane now).
    func testAccountItemTitle() {
        XCTAssertEqual(MenuBarMenuPolicy.accountItemTitle, "Account…")
    }
}
