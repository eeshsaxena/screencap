/**
 * The recorder that lives in the offscreen document.
 *
 * MV3 service workers have no DOM and are suspended when idle, so `MediaRecorder`
 * cannot live in one. An offscreen document is the supported host: it keeps
 * recording while the worker sleeps, and no reason except `AUDIO_PLAYBACK` puts
 * a lifetime limit on it.
 *
 * ## Two source paths, because only one of them can cross contexts
 *
 * - **Tab capture** takes a stream id the service worker obtained and consumes
 *   it here through `getUserMedia`. This is the one cross-context path Chrome
 *   supports (since Chrome 116).
 * - **Whole-screen capture** calls the display picker *here*, inside the
 *   offscreen document. It cannot be acquired outside and passed in: a
 *   `chrome.desktopCapture` stream id is not usable across execution contexts
 *   and throws when consumed here. That is why the extension requests no
 *   `desktopCapture` permission at all.
 *
 * The two differ in when the offscreen document must exist, which is what makes
 * their cancellation paths different — see `../background/capture-controller.ts`.
 *
 * ## No policy here
 *
 * {@link CaptureRecorder.pause} exists so capture can be suppressed while a
 * non-allow-listed origin is in view, but this module has no idea that is why.
 * It decides *how* capture pauses; the allow-list decides *when*. Keeping the
 * two apart is what lets this be tested without any permission state.
 *
 * Every browser API is injected, so the orchestration below tests in Node —
 * which has neither `MediaRecorder` nor `MediaStream` — matching the
 * `AuthDeps` / `AllowlistDeps` split elsewhere in this extension.
 */

import type { ChunkStore } from "./chunk-store.js";

/** How often `MediaRecorder` hands over a slice. Shorter bounds what a crash
 * costs; longer costs fewer writes. */
export const TIMESLICE_MS = 3_000;

/** Tried in order. Recording into a container the assembly step cannot name
 * would produce a file nobody can play, so an empty intersection refuses the
 * recording rather than falling back to the browser's default. */
export const MIME_CANDIDATES = [
  "video/webm;codecs=vp9",
  "video/webm;codecs=vp8",
  "video/webm",
] as const;

/** Chrome rejects with these when the user dismisses the picker. Ordinary
 * outcomes, not failures — reporting them as errors would put a scary message
 * in front of someone who simply changed their mind. */
const CANCELLED_ERROR_NAMES = new Set(["NotAllowedError", "AbortError"]);

/** Only the members this module touches, so a test can supply a plain object
 * where the browser would supply a `MediaStream`. */
export interface CaptureTrack {
  stop(): void;
  /** What the browser calls this source. For whole-screen capture it is the
   * only way to learn which screen or window the user actually picked — the
   * picker runs inside this document and reports nothing else. */
  readonly label?: string;
  /**
   * Fires when the source ends without this extension asking — Chrome's own
   * "Stop sharing" control, a captured tab closing, a display disconnecting.
   *
   * Subscribed rather than ignored because these paths do not go through
   * {@link CaptureRecorder.stop}, so without them the worker would keep
   * reporting a recording nobody is making.
   */
  onEnded(handler: () => void): void;
}

export interface CaptureStream {
  getTracks(): CaptureTrack[];
}

export interface RecorderHandle {
  start(timesliceMs: number): void;
  pause(): void;
  resume(): void;
  stop(): void;
}

export interface RecorderEvents {
  onData(blob: Blob): void;
  onError(error: Error): void;
  onStop(): void;
}

export interface RecorderDeps {
  tabStream(streamId: string): Promise<CaptureStream>;
  displayStream(): Promise<CaptureStream>;
  isTypeSupported(mimeType: string): boolean;
  createRecorder(
    stream: CaptureStream,
    mimeType: string,
    events: RecorderEvents,
  ): RecorderHandle;
  chunks: ChunkStore;
  /** Called when capture dies on its own — a bad write or an encoder error.
   * The controller turns this into a failed session; nothing else notices
   * otherwise, because the worker is not told about slices. */
  onFailure(error: string): void;
  /** Called when the user ends the share from the browser's own control, or the
   * captured surface disappears. A normal ending, not a failure — but one the
   * extension did not initiate, so it still has to be announced. */
  onEnded(sessionId: string): void;
}

export type CaptureRequest =
  | { kind: "tab"; streamId: string }
  | { kind: "screen" };

export type StartResult =
  | {
      ok: true;
      mimeType: string;
      /** The track's own name for the source, when it has one. Null leaves the
       * controller's provisional label in place rather than replacing it with
       * an empty string. */
      sourceLabel: string | null;
    }
  | {
      ok: false;
      /** `cancelled`: the user dismissed the picker. `unsupported`: no usable
       * container. `failed`: the source could not be opened. */
      reason: "cancelled" | "unsupported" | "failed";
      error: string;
    };

export interface StopSummary {
  chunkCount: number;
  byteLength: number;
  complete: boolean;
}

export class CaptureRecorder {
  private handle: RecorderHandle | null = null;
  private stream: CaptureStream | null = null;
  private sessionId: string | null = null;
  private nextIndex = 0;
  private paused = false;
  private failed = false;
  /** Appends are chained rather than fired in parallel so slices cannot commit
   * out of order, and so {@link stop} has one thing to await. */
  private writeQueue: Promise<void> = Promise.resolve();
  private stopped: Promise<void> = Promise.resolve();

  constructor(private readonly deps: RecorderDeps) {}

  /**
   * Acquire the chosen source and begin recording.
   *
   * The container check runs before acquisition on purpose: asking someone to
   * pick a screen and then refusing to record it wastes the one interaction
   * this flow gets.
   */
  async start(sessionId: string, request: CaptureRequest): Promise<StartResult> {
    const mimeType = MIME_CANDIDATES.find((candidate) =>
      this.deps.isTypeSupported(candidate),
    );
    if (mimeType === undefined) {
      return {
        ok: false,
        reason: "unsupported",
        error: "This browser cannot record any container Screencap can read.",
      };
    }

    let stream: CaptureStream;
    try {
      stream =
        request.kind === "tab"
          ? await this.deps.tabStream(request.streamId)
          : await this.deps.displayStream();
    } catch (error) {
      const cancelled = error instanceof Error && CANCELLED_ERROR_NAMES.has(error.name);
      return {
        ok: false,
        reason: cancelled ? "cancelled" : "failed",
        error: describe(error),
      };
    }

    this.sessionId = sessionId;
    this.stream = stream;
    this.nextIndex = 0;
    this.paused = false;
    this.failed = false;

    let markStopped = (): void => {};
    this.stopped = new Promise<void>((resolve) => {
      markStopped = resolve;
    });

    this.handle = this.deps.createRecorder(stream, mimeType, {
      onData: (blob) => this.write(blob),
      onError: (error) => this.fail(describe(error)),
      onStop: () => markStopped(),
    });
    this.handle.start(TIMESLICE_MS);

    // Subscribed after the recorder exists so an immediate end still finds
    // something to stop.
    for (const track of stream.getTracks()) {
      track.onEnded(() => this.sourceEnded());
    }

    const label = stream.getTracks()[0]?.label;
    return { ok: true, mimeType, sourceLabel: label ? label : null };
  }

  /** Suspend capture. Returns whether this actually changed anything, so the
   * controller records a pause boundary only for a real transition — a repeated
   * signal would otherwise double-count the gap and shift every later event. */
  pause(): boolean {
    if (this.handle === null || this.paused || this.failed) return false;
    this.handle.pause();
    this.paused = true;
    return true;
  }

  resume(): boolean {
    if (this.handle === null || !this.paused || this.failed) return false;
    this.handle.resume();
    this.paused = false;
    return true;
  }

  /**
   * End capture and release the source.
   *
   * Waits for the write queue *after* the stop event, because `MediaRecorder`
   * flushes a final slice on the way out — resolving earlier would truncate
   * every recording by up to one timeslice.
   *
   * Releasing every track is what makes the browser's own sharing indicator go
   * away. A track left live tells the user they are still being recorded.
   */
  async stop(): Promise<StopSummary> {
    try {
      this.handle?.stop();
    } catch {
      // Already inactive — a failure stopped it moments ago and the user's stop
      // raced the failure message. `MediaRecorder.stop()` throws in that state,
      // and reporting it would replace a real cause with a confusing one.
    }
    await this.stopped;
    await this.writeQueue;

    for (const track of this.stream?.getTracks() ?? []) {
      track.stop();
    }
    this.stream = null;
    this.handle = null;
    this.paused = false;

    if (this.sessionId === null) {
      return { chunkCount: 0, byteLength: 0, complete: true };
    }
    const recording = await this.deps.chunks.read(this.sessionId);
    return {
      chunkCount: recording.chunks.length,
      byteLength: recording.byteLength,
      // The store can only see *holes*, and a failed write leaves no hole — it
      // stops the recording, so the surviving indices stay contiguous and a
      // truncated recording would otherwise pass as whole. The recorder is the
      // only party that knows a slice was lost, so its own view decides.
      complete:
        recording.complete && !this.failed && recording.chunks.length === this.nextIndex,
    };
  }

  /** Persist one slice. Empty slices are dropped rather than stored: they
   * occupy an index, which would make the sequence look gapless while
   * contributing no video. */
  private write(blob: Blob): void {
    const sessionId = this.sessionId;
    if (sessionId === null || this.failed || blob.size === 0) return;

    const index = this.nextIndex++;
    this.writeQueue = this.writeQueue
      .then(() => this.deps.chunks.append(sessionId, index, blob))
      .catch((error: unknown) => this.fail(describe(error)));
  }

  /** One failure ends the recording. Continuing past a bad write would produce
   * a video with a hole nobody can see. */
  private fail(error: string): void {
    if (this.failed) return;
    this.failed = true;
    this.release();
    this.deps.onFailure(error);
  }

  /**
   * The source ended without us asking — the user hit the browser's own stop
   * control, or the captured surface went away.
   *
   * Reported rather than treated as a failure: the recording is finished, and
   * the worker needs to know so its record and the badge stop describing a
   * capture that is over.
   */
  private sourceEnded(): void {
    if (this.failed || this.sessionId === null || this.handle === null) return;
    const sessionId = this.sessionId;
    this.release();
    this.deps.onEnded(sessionId);
  }

  /** Stop the encoder and let the source go. Releasing the tracks is what
   * makes the browser's own sharing indicator disappear; leaving one live
   * tells the user they are still being recorded. */
  private release(): void {
    try {
      this.handle?.stop();
    } catch {
      // Already inactive — the caller's report is what matters.
    }
    for (const track of this.stream?.getTracks() ?? []) {
      track.stop();
    }
  }
}

function describe(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * Adapter to the real browser APIs. Untested headlessly — Node has neither
 * `MediaRecorder` nor `getUserMedia` — so it stays as thin as the mapping
 * allows and is verified by recording in a real browser.
 */
/** Keeps the real stream reachable for `MediaRecorder` while the recorder above
 * sees only the narrow {@link CaptureStream} surface. */
interface BrowserCaptureStream extends CaptureStream {
  readonly native: MediaStream;
}

/**
 * Wrap a `MediaStream` in the injected surface.
 *
 * `MediaStreamTrack` signals its end through an `ended` *event*, which the
 * narrow interface exposes as a subscription so a test can fire it without a
 * DOM. Tracks are mapped once so repeated `getTracks()` calls return the same
 * wrappers rather than fresh ones that would lose their subscriptions.
 */
function asCaptureStream(native: MediaStream): BrowserCaptureStream {
  const tracks: CaptureTrack[] = native.getTracks().map((track) => ({
    label: track.label,
    stop: () => track.stop(),
    onEnded: (handler) => track.addEventListener("ended", handler, { once: true }),
  }));
  return { native, getTracks: () => tracks };
}

export function browserRecorderDeps(
  chunks: ChunkStore,
  onFailure: (error: string) => void,
  onEnded: (sessionId: string) => void,
): RecorderDeps {
  return {
    // The `mandatory` constraint shape is Chrome's own extension-capture
    // dialect, not standard `MediaTrackConstraints`, so it is cast rather than
    // typed.
    tabStream: async (streamId) =>
      asCaptureStream(
        await navigator.mediaDevices.getUserMedia({
          video: {
            mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: streamId },
          },
        } as unknown as MediaStreamConstraints),
      ),

    displayStream: async () =>
      asCaptureStream(await navigator.mediaDevices.getDisplayMedia({ video: true })),

    isTypeSupported: (mimeType) => MediaRecorder.isTypeSupported(mimeType),

    createRecorder: (stream, mimeType, events) => {
      const recorder = new MediaRecorder((stream as BrowserCaptureStream).native, {
        mimeType,
      });
      recorder.ondataavailable = (event) => events.onData(event.data);
      recorder.onerror = () => events.onError(new Error("Recording stopped unexpectedly"));
      recorder.onstop = () => events.onStop();
      return {
        start: (timesliceMs) => recorder.start(timesliceMs),
        pause: () => recorder.pause(),
        resume: () => recorder.resume(),
        stop: () => recorder.stop(),
      };
    },

    chunks,
    onFailure,
    onEnded,
  };
}
