import CoreGraphics

/// The main window's width contract.
///
/// The shell is a plain two-column `HStack` — a fixed-width sidebar plus a
/// flexible detail column (`MainWindow.shellContent`). An `HStack` that cannot
/// satisfy its children's *hard* minimums does not clip or truncate: it
/// overflows, and the overflow is distributed symmetrically about the centre.
/// So a window minimum below the widest route's hard minimum does not produce a
/// cramped layout — it produces content bleeding off **both** window edges.
///
/// That is exactly what shipped: the window declared an 880pt minimum while the
/// Day page's header chrome alone needs ~700pt on top of the 248pt sidebar. At
/// the 880pt floor the sidebar's leading edge and the playback pane's trailing
/// action bar were both drawn outside the window.
///
/// This namespace is the single place those numbers live, so the declared
/// window minimum and the layout's real requirement cannot drift apart
/// silently. `ShellWindowLayoutTests` pins the invariant
/// (`windowMinWidth >= minContentWidth`).
///
/// Only **hard** minimums belong in the arithmetic below — widths a view cannot
/// give back under compression (`.frame(width:)`, `.frame(minWidth:)`, an
/// AppKit control's intrinsic size). Text that truncates and `Spacer`s are
/// soft: they yield, so they cannot cause overflow and are deliberately
/// excluded (the two short button labels are counted only because they sit at
/// their natural width in every real layout).
enum ShellWindowLayout {

    // MARK: - Shell columns

    /// The fixed sidebar column (`MainWindow.sidebar`). Hard — it is a
    /// `.frame(width:)`, so it never compresses.
    static let sidebarWidth: CGFloat = 248

    // MARK: - Day page header (the widest route's chrome)

    /// `DayTimelineView.header` leading/trailing padding.
    static let dayHeaderHorizontalPadding: CGFloat = 24
    /// Spacing between the header's five slots (four gaps).
    static let dayHeaderSpacing: CGFloat = 16
    /// Natural width of the "← Back" label.
    static let dayHeaderBackButtonWidth: CGFloat = 48
    /// Natural width of the trailing day-actions menu button.
    static let dayHeaderActionsMenuWidth: CGFloat = 28

    /// Spacing inside `dateNavigator` (three gaps).
    static let dayDateNavigatorSpacing: CGFloat = 10
    /// Each chevron step button.
    static let dayDateStepperWidth: CGFloat = 14
    /// The date label's declared floor — a truncated date is not acceptable, so
    /// this stays hard.
    static let dayDateLabelMinWidth: CGFloat = 150
    /// `DatePicker(.field)`'s intrinsic width. An AppKit control: it does not
    /// compress below this, which is why it counts as hard.
    static let dayDatePickerMinWidth: CGFloat = 105

    /// The day-scoped search field's floor. Below its preferred width so the
    /// header has something to give back under compression — a fixed 300pt
    /// field was the single largest avoidable contributor to the overflow, and
    /// the field stays perfectly usable at this width.
    static let daySearchFieldMinWidth: CGFloat = 220
    /// The search field's preferred width, used when there is room for it.
    static let daySearchFieldMaxWidth: CGFloat = 300

    // MARK: - Playback pane

    /// Floor for the Day page's playback pane.
    ///
    /// The pane carries no aspect-ratio constraint — it takes whatever vertical
    /// space is left after the header, narrative and strip. At a 560pt-tall
    /// window that leftover was ~230pt against a ~690pt width, i.e. a ~3:1 box
    /// showing 16:10 footage, so the video occupied barely half the pane's
    /// width and the rest read as dead black space.
    static let playbackPaneMinHeight: CGFloat = 320

    /// Horizontal inset between the playback pane and the window chrome.
    ///
    /// The pane's other failure mode, at the opposite end of the size range: with
    /// no inset, a maximized window renders the video flush against the sidebar
    /// divider on one side and the window edge on the other, which reads as a
    /// slab wedged into the chrome rather than a player sitting in the page.
    ///
    /// 24pt is the day page's content gutter — the same value `header` and
    /// `narrativeSection` use — so the player's edges line up with the date
    /// navigator above it instead of introducing a third alignment.
    static let playbackPaneHorizontalInset: CGFloat = 24

    // MARK: - Window

    /// The window's declared minimum content width. Derived from
    /// `minContentWidth`, not chosen independently — see the test that pins the
    /// relationship. The headroom above the requirement is deliberate slack for
    /// font-metric variation (a longer localized weekday, an accessibility text
    /// size) rather than a number tuned to sit exactly on the boundary.
    static let windowMinWidth: CGFloat = 1000
    /// The window's declared minimum content height. Sized so the playback pane
    /// still clears `playbackPaneMinHeight` once the day page's header,
    /// narrative and strip have taken their share.
    static let windowMinHeight: CGFloat = 640
    /// The size the window opens at when it has no restored frame.
    static let windowDefaultWidth: CGFloat = 1180
    /// See `playbackPaneMinHeight` — the default height exists so the playback
    /// pane opens at a watchable size rather than at its floor.
    static let windowDefaultHeight: CGFloat = 780

    // MARK: - Derived requirements

    /// Hard minimum of `DayTimelineView.dateNavigator`.
    static var dayDateNavigatorMinWidth: CGFloat {
        dayDateStepperWidth * 2
            + dayDateNavigatorSpacing * 3
            + dayDateLabelMinWidth
            + dayDatePickerMinWidth
    }

    /// Hard minimum of `DayTimelineView.header` — the widest chrome any route
    /// puts in the detail column.
    static var dayHeaderMinWidth: CGFloat {
        dayHeaderHorizontalPadding * 2
            + dayHeaderBackButtonWidth
            + dayHeaderSpacing * 4
            + dayDateNavigatorMinWidth
            + daySearchFieldMinWidth
            + dayHeaderActionsMenuWidth
    }

    /// The narrowest content width the shell lays out without overflowing.
    static var minContentWidth: CGFloat {
        sidebarWidth + dayHeaderMinWidth
    }
}
