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
 */

import type { ResolvedSession } from "../capture/session.js";

export interface CaptureViewInput {
  signedIn: boolean;
  status: ResolvedSession;
  /** A verb is in flight; both actions are withheld so a second click cannot
   * race the first. */
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
  canStart: boolean;
  canStop: boolean;
  error: string | null;
}

const IDLE_MESSAGE = "Not recording.";

export function captureView({
  signedIn,
  status,
  busy = false,
  error = null,
}: CaptureViewInput): CaptureView {
  const label = status.record?.source.label ?? null;
  const base = { visible: signedIn, error };

  switch (status.kind) {
    case "recording":
      return {
        ...base,
        message: label === null ? "Recording." : `Recording ${label}.`,
        sourceLabel: label,
        canStart: false,
        canStop: !busy,
      };

    case "paused":
      return {
        ...base,
        // Named even while paused: the user needs to know what will resume,
        // and a paused recording is still holding that source.
        message: label === null ? "Paused." : `Paused — ${label}.`,
        sourceLabel: label,
        canStart: false,
        canStop: !busy,
      };

    case "starting":
      return {
        ...base,
        message: "Starting…",
        sourceLabel: label,
        canStart: false,
        canStop: false,
      };

    case "stopping":
      return {
        ...base,
        message: "Finishing…",
        sourceLabel: label,
        canStart: false,
        canStop: false,
      };

    case "interrupted":
      return {
        ...base,
        // Says what happened rather than reporting a clean stop. The bytes up
        // to the last slice are still on disk; recovering them is later work,
        // but claiming the recording finished would be false now.
        message: "Your last recording was interrupted before it finished.",
        sourceLabel: null,
        canStart: !busy,
        canStop: false,
      };

    case "failed":
      return {
        ...base,
        message:
          status.error === null
            ? "Your last recording stopped unexpectedly."
            : `Your last recording stopped: ${status.error}`,
        sourceLabel: null,
        canStart: !busy,
        canStop: false,
      };

    case "idle":
      return {
        ...base,
        message: IDLE_MESSAGE,
        sourceLabel: null,
        canStart: !busy,
        canStop: false,
      };
  }
}
