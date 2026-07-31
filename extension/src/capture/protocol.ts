/**
 * The messages that cross the service-worker/offscreen-document boundary.
 *
 * Kept in its own module because both sides need the same shapes and neither
 * can import the other: the controller would pull `MediaRecorder` into the
 * worker bundle, and the offscreen document would pull the auth session into a
 * context that must not hold one.
 *
 * Only small JSON crosses this line. A recorded `Blob` never does — extension
 * messaging is JSON-serialized by default, and structured-clone messaging needs
 * a Chrome version above this extension's floor. The bytes stay in the
 * offscreen document's IndexedDB and the worker gets a handle instead.
 *
 * Per-slice progress is deliberately *not* a message. Sending one per timeslice
 * would revive the suspended worker on every slice — a keepalive anti-pattern
 * that buys nothing, since the offscreen document persists slices without the
 * worker's help. The worker hears about start, stop, and failure only.
 */

import type { CaptureRequest, StartResult, StopSummary } from "./recorder.js";

export const OFFSCREEN_REQUEST_TYPES = [
  "offscreen.start",
  "offscreen.pause",
  "offscreen.resume",
  "offscreen.stop",
] as const;

export type OffscreenRequestType = (typeof OFFSCREEN_REQUEST_TYPES)[number];

export type OffscreenRequest =
  | { type: "offscreen.start"; sessionId: string; source: CaptureRequest }
  | { type: "offscreen.pause" }
  | { type: "offscreen.resume" }
  | { type: "offscreen.stop" };

export type OffscreenResponse =
  | { type: "offscreen.start"; result: StartResult }
  /** `changed` is false when the recorder was already in that state — the
   * controller records a pause boundary only for a real transition. */
  | { type: "offscreen.pause" | "offscreen.resume"; changed: boolean }
  | { type: "offscreen.stop"; summary: StopSummary }
  | { type: "offscreen.error"; error: string };

/** Sent unprompted when capture dies on its own. Nothing else would tell the
 * worker: it is not in the slice path. */
export const RECORDER_FAILED = "capture.recorderFailed";

export interface RecorderFailed {
  type: typeof RECORDER_FAILED;
  error: string;
}

/**
 * Whether this listener owns the message.
 *
 * Both contexts receive every `chrome.runtime.sendMessage`, including their own
 * siblings' traffic, so each listener checks the discriminant at runtime and
 * declines anything it does not recognize — the convention
 * `../background/service-worker.ts` established for the auth listener.
 */
export function isOffscreenRequest(request: unknown): request is OffscreenRequest {
  return (
    typeof request === "object" &&
    request !== null &&
    OFFSCREEN_REQUEST_TYPES.includes(
      (request as { type?: unknown }).type as OffscreenRequestType,
    )
  );
}

export function isRecorderFailed(request: unknown): request is RecorderFailed {
  return (
    typeof request === "object" &&
    request !== null &&
    (request as { type?: unknown }).type === RECORDER_FAILED
  );
}
