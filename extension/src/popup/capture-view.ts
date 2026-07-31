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

import type { ResolvedSession, ResolvedSessionKind } from "../capture/session.js";

export interface CaptureViewInput {
  signedIn: boolean;
  status: ResolvedSession;
  /** A verb is in flight; actions grey out so a second click cannot race it. */
  busy?: boolean;
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
};

/** Named only while a source is genuinely held; after an interrupted or failed
 * recording, naming it would suggest something is still being captured. */
const NAMES_SOURCE = new Set<ResolvedSessionKind>([
  "recording",
  "paused",
  "starting",
  "stopping",
]);

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
    case "idle":
      return "Not recording.";
  }
}

export function captureView({
  signedIn,
  status,
  busy = false,
  error = null,
}: CaptureViewInput): CaptureView {
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
