/**
 * The toolbar badge — the only recording indicator visible with the popup shut.
 *
 * Derived from the reconciled session on every sync, never accumulated from
 * transitions. A revived service worker inherits whatever badge the browser
 * kept from before the suspension, and that badge may describe a recording that
 * has since ended, so re-deriving is what keeps it honest rather than merely
 * up to date.
 *
 * Chrome's own sharing indicator is left alone. It is the browser's, it appears
 * for whole-screen capture, and a second widget that could contradict it is
 * worse than deferring to the one the user already trusts.
 */

import type { ResolvedSession } from "../capture/session.js";

export interface Badge {
  /** Kept to a few characters — Chrome truncates the rest. Empty clears it. */
  text: string;
  color: string;
  /** The action's tooltip, which is where the source name fits; the badge
   * itself has nowhere near enough room for it. */
  title: string;
}

const RECORDING_RED = "#c5221f";
const ATTENTION_AMBER = "#8a5a00";

export const IDLE_BADGE: Badge = {
  text: "",
  color: RECORDING_RED,
  title: "Screencap",
};

export function badgeFor(status: ResolvedSession): Badge {
  const label = status.record?.source.label ?? null;

  switch (status.kind) {
    case "recording":
      return {
        text: "REC",
        color: RECORDING_RED,
        title: label === null ? "Screencap is recording" : `Screencap is recording ${label}`,
      };

    case "paused":
      return {
        text: "II",
        color: ATTENTION_AMBER,
        title: label === null ? "Screencap is paused" : `Screencap is paused — ${label}`,
      };

    case "interrupted":
    case "failed":
      // Distinct from idle: the user should be able to tell at a glance that
      // something ended badly, without opening the popup to find out.
      return {
        text: "!",
        color: ATTENTION_AMBER,
        title: "Your last Screencap recording didn't finish",
      };

    case "unknown":
      // Something is capturing and its record could not be read. A blank badge
      // would say "nothing is recording", which is the one thing this state
      // cannot support.
      return {
        text: "?",
        color: ATTENTION_AMBER,
        title: "Screencap can't tell whether a recording is running",
      };

    case "starting":
    case "stopping":
    case "idle":
      // Transitions are brief and self-resolving; flashing a badge for them
      // would read as noise rather than information.
      return IDLE_BADGE;
  }
}

export interface BadgeDeps {
  setText(text: string): Promise<void>;
  setColor(color: string): Promise<void>;
  setTitle(title: string): Promise<void>;
}

/**
 * Paint a badge. Failures are swallowed: the badge is an indicator, and losing
 * it must never take down the capture verb that triggered the repaint.
 */
export async function applyBadge(deps: BadgeDeps, badge: Badge): Promise<void> {
  try {
    await Promise.all([
      deps.setText(badge.text),
      deps.setColor(badge.color),
      deps.setTitle(badge.title),
    ]);
  } catch {
    // Intentionally swallowed — see the docstring.
  }
}

export function chromeBadgeDeps(): BadgeDeps {
  return {
    setText: (text) => chrome.action.setBadgeText({ text }),
    setColor: (color) => chrome.action.setBadgeBackgroundColor({ color }),
    setTitle: (title) => chrome.action.setTitle({ title }),
  };
}
