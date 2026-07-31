/**
 * What the recording panel shows, as a pure function of the reconciled session.
 *
 * Kept apart from the DOM wiring in `popup.ts` for the reason `view.ts` and
 * `allowlist-view.ts` are: the decisions — what the user is told, which buttons
 * make sense — are testable without a browser.
 *
 * The state list is deliberately not collapsed. `interrupted` in particular is
 * neither "recording" nor "not recording": the recording is over, but its bytes
 * are stranded rather than finished. Showing it as idle would tell someone
 * their recording completed when it did not, and showing it as running would
 * offer them a Stop button for something that is already gone.
 *
 * Visibility and availability are separate. Which buttons *belong* in a state
 * is a property of that state; whether they can be *clicked* also depends on a
 * verb being in flight. Folding the two together made the buttons vanish
 * mid-click rather than grey out.
 */

import type { CaptureMessageResponse } from "../background/capture-controller.js";
import type { ResolvedSession, ResolvedSessionKind } from "../capture/session.js";

export interface CaptureViewInput {
  signedIn: boolean;
  status: ResolvedSession;
  /** A verb is in flight; actions grey out so a second click cannot race it. */
  busy?: boolean;
  /**
   * Whether the service worker answered at all. False means the status below is
   * a placeholder, not an observation — see {@link captureView}.
   */
  reachable?: boolean;
  error?: string | null;
}

export interface CaptureView {
  /** There is nothing to record under without an account. */
  visible: boolean;
  message: string;
  /** The source being captured, when one is. Null in every state where naming
   * one would be a claim we cannot support. */
  sourceLabel: string | null;
  showStart: boolean;
  showStop: boolean;
  canStart: boolean;
  canStop: boolean;
  error: string | null;
}

/**
 * Which actions belong in each state.
 *
 * `starting` and `stopping` offer Stop rather than nothing. They are normally
 * momentary, but a worker that dies mid-verb leaves one of them persisted, and
 * a state with no action at all would strand the user until the browser
 * restarted. Stop is also the correct affordance on its own terms: it cancels a
 * start and retries a stop.
 */
const ACTIONS: Record<ResolvedSessionKind, { showStart: boolean; showStop: boolean }> = {
  idle: { showStart: true, showStop: false },
  recording: { showStart: false, showStop: true },
  paused: { showStart: false, showStop: true },
  starting: { showStart: false, showStop: true },
  stopping: { showStart: false, showStop: true },
  interrupted: { showStart: true, showStop: false },
  failed: { showStart: true, showStop: false },
  // Something is capturing and we cannot say what. Starting would open a second
  // document beside it; stopping is the one action that can only help.
  unknown: { showStart: false, showStop: true },
};

/** Named only while a source is genuinely held; after an interrupted or failed
 * recording, naming it would suggest something is still being captured. */
const NAMES_SOURCE = new Set<ResolvedSessionKind>([
  "recording",
  "paused",
  "starting",
  "stopping",
]);

/** Shown when the extension's own background service cannot be reached, so no
 * claim about recording state can be made at all. */
export const UNREACHABLE_MESSAGE =
  "Can't reach Screencap's background service, so it can't tell whether a recording is running.";

function messageFor(status: ResolvedSession, label: string | null): string {
  switch (status.kind) {
    case "recording":
      return label === null ? "Recording." : `Recording ${label}.`;
    case "paused":
      // Named even while paused: the user needs to know what will resume, and
      // a paused recording is still holding that source.
      return label === null ? "Paused." : `Paused — ${label}.`;
    case "starting":
      return "Starting…";
    case "stopping":
      return "Finishing…";
    case "interrupted":
      // Says what happened rather than reporting a clean stop. The bytes up to
      // the last slice are still on disk; recovering them is later work, but
      // claiming the recording finished would be false now.
      return "Your last recording was interrupted before it finished.";
    case "failed":
      return status.error === null
        ? "Your last recording stopped unexpectedly."
        : `Your last recording stopped: ${status.error}`;
    case "unknown":
      return "Something is being recorded, but Screencap can't tell what.";
    case "idle":
      return "Not recording.";
  }
}

/**
 * What the panel shows.
 *
 * An unreachable service worker is rendered as its own state rather than as
 * idle. The caller has no status to report in that case, and "Not recording"
 * would be an assertion made from an absence of information — the one claim a
 * screen recorder must never guess at. The auth half of this popup takes the
 * same position with its `unknown` status.
 */
export function captureView({
  signedIn,
  status,
  busy = false,
  reachable = true,
  error = null,
}: CaptureViewInput): CaptureView {
  if (signedIn && !reachable) {
    return {
      visible: true,
      message: UNREACHABLE_MESSAGE,
      sourceLabel: null,
      showStart: false,
      showStop: false,
      canStart: false,
      canStop: false,
      error,
    };
  }

  const label = status.record?.source.label ?? null;
  const { showStart, showStop } = ACTIONS[status.kind];

  return {
    visible: signedIn,
    message: messageFor(status, label),
    sourceLabel: NAMES_SOURCE.has(status.kind) ? label : null,
    showStart,
    showStop,
    canStart: showStart && !busy,
    canStop: showStop && !busy,
    error,
  };
}

/**
 * Turn the outcome of a capture verb into something worth reading.
 *
 * Lives here rather than beside the DOM wiring for the reason the rest of this
 * module does: it is a decision about what the user is told, and decisions are
 * testable while element assignment is not. A dismissed picker returns null —
 * the user closed it on purpose and does not need telling.
 */
export function captureProblem(response: CaptureMessageResponse): string | null {
  if (!response.ok) return response.error;
  if ("start" in response && !response.start.ok) {
    if (response.start.reason === "already-recording") {
      return `Already recording ${response.start.runningSource.label}.`;
    }
    return response.start.reason === "cancelled" ? null : response.start.error;
  }
  if ("stop" in response && !response.stop.ok) {
    return response.stop.reason === "not-recording"
      ? "There was no recording to stop."
      : response.stop.error;
  }
  return null;
}
