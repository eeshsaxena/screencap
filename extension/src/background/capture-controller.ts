/**
 * The service worker's half of recording: lifecycle, not media.
 *
 * The worker never touches a stream. It decides whether a recording may start,
 * owns the single offscreen document, and owns the session record — the three
 * things that must survive its own suspension.
 *
 * ## One document, and why presence alone cannot gate a start
 *
 * Chrome allows one offscreen document per extension, so opening a second is an
 * error rather than a replacement. But a document with no session record is an
 * *orphan* — a crash between opening it and writing the record — and refusing to
 * start on its presence alone would wedge the extension with no way back. Only
 * a live record means a real recording is running.
 *
 * ## Ordering, so a cancel leaves nothing behind
 *
 * The record is written only after the document exists. That gives the
 * invariant reconciliation depends on: a live record with no document means the
 * browser died, never that a start was halfway through.
 *
 * The two sources cancel differently. Tab capture has no picker — the stream id
 * was already obtained — so there is nothing to dismiss. Whole-screen capture
 * opens its picker *inside* the offscreen document, so the document exists
 * before the user has chosen and a dismissal must tear it down.
 */

import {
  RECORDER_FAILED,
  isRecorderFailed,
  type OffscreenRequest,
  type OffscreenResponse,
} from "../capture/protocol.js";
import type { CaptureRequest } from "../capture/recorder.js";
import {
  SessionStore,
  beginSession,
  chromeSessionStorage,
  markFailed,
  markPaused,
  markRecording,
  markResumed,
  markStopping,
  isLiveSessionKind,
  reconcile,
  type CaptureSource,
  type ResolvedSession,
  type SessionRecord,
} from "../capture/session.js";
import { isExtensionPageSender } from "./senders.js";

/** What the popup asks for. The stream id is transient and never persisted —
 * only the kind and the human-readable label belong in the record. */
export type CaptureSourceRequest =
  | { kind: "tab"; streamId: string; label: string }
  | { kind: "screen" };

export interface RecordingHandle {
  sessionId: string;
  mimeType: string | null;
  chunkCount: number;
  byteLength: number;
  /** False when a slice failed to write mid-recording, so assembly can tell a
   * damaged recording from a short one. */
  complete: boolean;
}

export type CaptureStartOutcome =
  | { ok: true; sessionId: string; source: CaptureSource }
  | { ok: false; reason: "already-recording"; runningSource: CaptureSource }
  | { ok: false; reason: "cancelled" | "unsupported" | "failed"; error: string };

export type CaptureStopOutcome =
  | { ok: true; handle: RecordingHandle }
  | { ok: false; reason: "not-recording" }
  | { ok: false; reason: "failed"; error: string };

export interface CaptureControllerDeps {
  session: SessionStore;
  hasOffscreenDocument(): Promise<boolean>;
  createOffscreenDocument(): Promise<void>;
  closeOffscreenDocument(): Promise<void>;
  sendToOffscreen(request: OffscreenRequest): Promise<OffscreenResponse>;
  /** Epoch milliseconds. */
  now(): number;
  newSessionId(): string;
}

export class CaptureController {
  constructor(private readonly deps: CaptureControllerDeps) {}

  /** The honest state, reconciling the stored record against whether the
   * document is actually alive. Every decision below starts here rather than
   * from memory, because the worker's memory does not survive suspension. */
  async status(): Promise<ResolvedSession> {
    const [record, offscreenExists] = await Promise.all([
      this.deps.session.read(),
      this.deps.hasOffscreenDocument(),
    ]);
    return reconcile(record, offscreenExists);
  }

  async start(source: CaptureSourceRequest): Promise<CaptureStartOutcome> {
    const current = await this.status();
    if (isLiveSessionKind(current.kind) && current.record !== null) {
      return {
        ok: false,
        reason: "already-recording",
        runningSource: current.record.source,
      };
    }

    // Anything open at this point is an orphan, not a recording — the guard
    // above already ruled out a live one.
    if (await this.deps.hasOffscreenDocument()) {
      await this.deps.closeOffscreenDocument();
    }

    const begun = beginSession(current, {
      id: this.deps.newSessionId(),
      source: toStoredSource(source),
      startedAtEpochMs: this.deps.now(),
    });
    if (!begun.ok) {
      // Unreachable in practice: the guard above already established this
      // session is not live, which is the only thing `beginSession` refuses on.
      return { ok: false, reason: "failed", error: "A recording is already running." };
    }

    await this.deps.createOffscreenDocument();
    await this.deps.session.write(begun.record);

    let response: OffscreenResponse;
    try {
      response = await this.deps.sendToOffscreen({
        type: "offscreen.start",
        sessionId: begun.record.id,
        source: toRecorderRequest(source),
      });
    } catch (error) {
      return this.abandonStart(describe(error));
    }

    if (response.type === "offscreen.error") {
      return this.abandonStart(response.error);
    }
    if (response.type !== "offscreen.start") {
      return this.abandonStart("The recorder answered the wrong question.");
    }
    if (!response.result.ok) {
      const { reason, error } = response.result;
      await this.teardown();
      return { ok: false, reason, error };
    }

    // The recorder is the only party that knows what the browser accepted and
    // what the source is actually called, and it cannot write the record itself
    // — an offscreen document reaches only chrome.runtime.
    const { mimeType, sourceLabel } = response.result;
    const named = sourceLabel
      ? { ...begun.record.source, label: sourceLabel }
      : begun.record.source;
    const recording = markRecording({ ...begun.record, source: named }, mimeType);
    await this.deps.session.write(recording);

    return { ok: true, sessionId: recording.id, source: recording.source };
  }

  async stop(): Promise<CaptureStopOutcome> {
    const current = await this.status();
    if (!isLiveSessionKind(current.kind) || current.record === null) {
      return { ok: false, reason: "not-recording" };
    }
    const record = current.record;
    await this.deps.session.write(markStopping(record));

    try {
      const response = await this.deps.sendToOffscreen({ type: "offscreen.stop" });
      if (response.type !== "offscreen.stop") {
        throw new Error(
          response.type === "offscreen.error" ? response.error : "The recorder answered the wrong question.",
        );
      }
      await this.teardown();
      return {
        ok: true,
        handle: { sessionId: record.id, mimeType: record.mimeType, ...response.summary },
      };
    } catch (error) {
      // Tear down anyway. Leaving the document open with a record claiming
      // "recording" would show the user a recording they can never stop; the
      // slices already written stay on disk either way.
      await this.teardown();
      return { ok: false, reason: "failed", error: describe(error) };
    }
  }

  /** Suspend capture. The boundary is recorded only when the recorder reports a
   * real state change — a repeated signal would double-count the gap and shift
   * every later event against the video. */
  async pause(): Promise<boolean> {
    return this.shift("offscreen.pause", "recording", markPaused);
  }

  async resume(): Promise<boolean> {
    return this.shift("offscreen.resume", "paused", markResumed);
  }

  /** Capture died on its own. The worker is not in the slice path, so this
   * message is the only way it finds out. */
  async recorderFailed(error: string): Promise<void> {
    const record = await this.deps.session.read();
    if (record !== null) {
      await this.deps.session.write(markFailed(record, error));
    }
    await this.deps.closeOffscreenDocument().catch(() => undefined);
  }

  private async shift(
    type: "offscreen.pause" | "offscreen.resume",
    from: ResolvedSession["kind"],
    apply: (record: SessionRecord, atEpochMs: number) => SessionRecord,
  ): Promise<boolean> {
    const current = await this.status();
    if (current.kind !== from || current.record === null) return false;

    const response = await this.deps.sendToOffscreen({ type });
    if (response.type !== type || !response.changed) return false;

    await this.deps.session.write(apply(current.record, this.deps.now()));
    return true;
  }

  /** Close the document and forget the session, in that order — a cleared
   * record with a live document reads as an orphan the next start can clean
   * up, whereas the reverse reads as a recording in progress. */
  private async teardown(): Promise<void> {
    await this.deps.closeOffscreenDocument().catch(() => undefined);
    await this.deps.session.clear();
  }

  private async abandonStart(error: string): Promise<CaptureStartOutcome> {
    await this.teardown();
    return { ok: false, reason: "failed", error };
  }
}

function toStoredSource(source: CaptureSourceRequest): CaptureSource {
  return source.kind === "tab"
    ? { kind: "tab", label: source.label }
    : // Refined once the recorder reports the track's own label; until then
      // this is the most specific truthful thing we can say.
      { kind: "screen", label: "Your screen" };
}

function toRecorderRequest(source: CaptureSourceRequest): CaptureRequest {
  return source.kind === "tab"
    ? { kind: "tab", streamId: source.streamId }
    : { kind: "screen" };
}

function describe(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export const CAPTURE_REQUEST_TYPES = [
  "capture.status",
  "capture.start",
  "capture.stop",
  "capture.pause",
  "capture.resume",
] as const;

export type CaptureRequestType = (typeof CAPTURE_REQUEST_TYPES)[number];

export type CaptureMessage =
  | { type: "capture.status" }
  | { type: "capture.start"; source: CaptureSourceRequest }
  | { type: "capture.stop" }
  | { type: "capture.pause" }
  | { type: "capture.resume" };

export type CaptureMessageResponse =
  | { ok: true; status: ResolvedSession }
  | { ok: true; start: CaptureStartOutcome }
  | { ok: true; stop: CaptureStopOutcome }
  | { ok: true; changed: boolean }
  | { ok: false; error: string };

export function isCaptureMessage(request: unknown): request is CaptureMessage {
  return (
    typeof request === "object" &&
    request !== null &&
    CAPTURE_REQUEST_TYPES.includes((request as { type?: unknown }).type as CaptureRequestType)
  );
}

async function dispatch(
  controller: CaptureController,
  message: CaptureMessage,
): Promise<CaptureMessageResponse> {
  switch (message.type) {
    case "capture.status":
      return { ok: true, status: await controller.status() };
    case "capture.start":
      return { ok: true, start: await controller.start(message.source) };
    case "capture.stop":
      return { ok: true, stop: await controller.stop() };
    case "capture.pause":
      return { ok: true, changed: await controller.pause() };
    case "capture.resume":
      return { ok: true, changed: await controller.resume() };
  }
}

/**
 * Register the capture verbs beside the auth listener.
 *
 * Both listeners see every message and decline what is not theirs, which is the
 * convention `./service-worker.ts` set. Content scripts are refused outright:
 * from the event-stream unit onward one runs on every allow-listed origin, and
 * a visited page must never be able to start or stop a recording.
 */
export function registerCaptureListener(
  controller: CaptureController,
  /** Run after any verb that could have changed state, so the toolbar badge is
   * repainted from the new truth. Injected rather than imported so the
   * controller stays free of presentation concerns. */
  onChanged: () => void = () => {},
): void {
  chrome.runtime.onMessage.addListener(
    (request: unknown, sender, sendResponse: (r: CaptureMessageResponse) => void) => {
      const failed = isRecorderFailed(request);
      if (!failed && !isCaptureMessage(request)) return false;
      if (!isExtensionPageSender(sender)) {
        sendResponse({ ok: false, error: "Unauthorized sender" });
        return false;
      }

      const work = failed
        ? controller.recorderFailed(request.error).then(() => ({ ok: true as const, changed: true }))
        : dispatch(controller, request);

      work
        .then((response) => {
          sendResponse(response);
          onChanged();
        })
        .catch((error: unknown) => {
          sendResponse({ ok: false, error: describe(error) });
          onChanged();
        });
      // Keeps the message channel open for the async response above.
      return true;
    },
  );
}

export function chromeCaptureDeps(): CaptureControllerDeps {
  return {
    session: new SessionStore(chromeSessionStorage()),

    hasOffscreenDocument: async () => {
      const contexts = await chrome.runtime.getContexts({
        contextTypes: [chrome.runtime.ContextType.OFFSCREEN_DOCUMENT],
      });
      return contexts.length > 0;
    },

    // Both reasons are declared because both source paths live in this one
    // document: the tab path consumes a stream id, the screen path opens the
    // display picker itself.
    createOffscreenDocument: () =>
      chrome.offscreen.createDocument({
        url: OFFSCREEN_DOCUMENT_URL,
        reasons: [
          chrome.offscreen.Reason.USER_MEDIA,
          chrome.offscreen.Reason.DISPLAY_MEDIA,
        ],
        justification:
          "Records the selected tab or screen. MediaRecorder cannot run in a service worker.",
      }),

    closeOffscreenDocument: () => chrome.offscreen.closeDocument(),

    sendToOffscreen: (request) =>
      chrome.runtime.sendMessage(request) as Promise<OffscreenResponse>,

    now: () => Date.now(),
    newSessionId: () => crypto.randomUUID(),
  };
}

export const OFFSCREEN_DOCUMENT_URL = "capture/offscreen.html";
export { RECORDER_FAILED };
