import XCTest
@testable import Screencap

/// Hide control — the menu-bar "Show recording controls" visibility gate.
final class MenuBarMenuPolicyTests: XCTestCase {

    // MARK: - Menu-bar icon tri-state (SCR-296)

    func testIconStateResolvesTheThreeCases() {
        XCTAssertEqual(
            MenuBarIconPresentation.state(isRecording: true, finalizing: false), .recording
        )
        XCTAssertEqual(
            MenuBarIconPresentation.state(isRecording: false, finalizing: true), .finalizing
        )
        XCTAssertEqual(
            MenuBarIconPresentation.state(isRecording: false, finalizing: false), .idle
        )
    }

    /// A recording started while a previous drain is still running must read as
    /// recording — the live capture is the more urgent fact.
    func testRecordingWinsOverAStillRunningDrain() {
        XCTAssertEqual(
            MenuBarIconPresentation.state(isRecording: true, finalizing: true), .recording
        )
    }

    /// KTD5: the finalizing state is distinguished by GLYPH, because this menu
    /// bar reserves colour for the recording state and the brand accent. If the
    /// three states ever share a symbol, the distinction has silently moved to
    /// colour and the scarcity rule is broken.
    func testEachIconStateHasItsOwnGlyph() {
        let names = [
            MenuBarIconPresentation.iconName(.idle),
            MenuBarIconPresentation.iconName(.recording),
            MenuBarIconPresentation.iconName(.finalizing),
        ]
        XCTAssertEqual(Set(names).count, 3, "the three states must not share a glyph")
    }

    /// The distinction must survive without sight of the glyph.
    func testEachIconStateHasItsOwnAccessibilityLabel() {
        let labels = [
            MenuBarIconPresentation.accessibilityLabel(.idle),
            MenuBarIconPresentation.accessibilityLabel(.recording),
            MenuBarIconPresentation.accessibilityLabel(.finalizing),
        ]
        XCTAssertEqual(Set(labels).count, 3, "VoiceOver must tell the three states apart")
    }

    /// R9: the drain has no honest ETA, so the line must not imply one. The
    /// Cmd+Q countdown beside it is the cautionary example — it counts down a
    /// timeout budget, so a recording that finishes in 40s still shows minutes
    /// "remaining".
    func testFinalizingStatusLineCarriesNoProgressClaim() {
        let line = MenuBarIconPresentation.finalizingStatusLine
        XCTAssertFalse(line.contains("%"), "no percentage")
        XCTAssertFalse(line.contains("remaining"), "no countdown")
        XCTAssertNil(
            line.rangeOfCharacter(from: CharacterSet.decimalDigits),
            "no elapsed or remaining figure"
        )
    }

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

    // MARK: - Mute mic item (SCR-254 U8)

    /// The menu-bar mute item appears only while a recording is live — there is no
    /// live mic to toggle in `.starting` / `.stopping` / `.idle`.
    func testMuteItemVisibleOnlyWhileRecording() {
        XCTAssertTrue(MenuBarMenuPolicy.muteItemVisible(state: .recording(elapsed: 3)))
        let hidden: [RecordingState] = [
            .idle, .starting, .stopping(quitting: false), .stopping(quitting: true),
        ]
        for state in hidden {
            XCTAssertFalse(
                MenuBarMenuPolicy.muteItemVisible(state: state),
                "no live mic to toggle in \(state)"
            )
        }
    }

    // MARK: - Shared mute-control grammar (SCR-254 U8)

    /// Effective-muted folds in the audio-off case so "Muted" always means "tap to
    /// turn the mic on" (the toggle and both surfaces read this).
    func testMuteControlEffectivelyMuted() {
        XCTAssertFalse(MuteControlPresentation.effectivelyMuted(muted: false, audioEnabled: true))
        XCTAssertTrue(MuteControlPresentation.effectivelyMuted(muted: true, audioEnabled: true))
        // Started audio-off, never unmuted → effectively muted.
        XCTAssertTrue(MuteControlPresentation.effectivelyMuted(muted: false, audioEnabled: false))
    }

    /// ONE grammar for both surfaces: status labels, transitional direction, and
    /// the slashed-vs-plain glyph.
    func testMuteControlLabelsAndIconPerState() {
        XCTAssertEqual(
            MuteControlPresentation.statusLabel(effectivelyMuted: false, inFlight: false), "Mic on"
        )
        XCTAssertEqual(
            MuteControlPresentation.statusLabel(effectivelyMuted: true, inFlight: false), "Muted"
        )
        XCTAssertEqual(
            MuteControlPresentation.statusLabel(effectivelyMuted: false, inFlight: true), "Muting…"
        )
        XCTAssertEqual(
            MuteControlPresentation.statusLabel(effectivelyMuted: true, inFlight: true), "Unmuting…"
        )
        XCTAssertEqual(MuteControlPresentation.iconName(effectivelyMuted: false), "mic.fill")
        XCTAssertEqual(MuteControlPresentation.iconName(effectivelyMuted: true), "mic.slash.fill")
    }

    /// VoiceOver labels spell out the action so "Muted" isn't read as a command.
    func testMuteControlAccessibilityLabels() {
        XCTAssertEqual(
            MuteControlPresentation.accessibilityLabel(effectivelyMuted: false, inFlight: false),
            "Microphone on, tap to mute"
        )
        XCTAssertEqual(
            MuteControlPresentation.accessibilityLabel(effectivelyMuted: true, inFlight: false),
            "Microphone muted, tap to unmute"
        )
        XCTAssertEqual(
            MuteControlPresentation.accessibilityLabel(effectivelyMuted: false, inFlight: true),
            "Muting microphone"
        )
        XCTAssertEqual(
            MuteControlPresentation.accessibilityLabel(effectivelyMuted: true, inFlight: true),
            "Unmuting microphone"
        )
    }
}
