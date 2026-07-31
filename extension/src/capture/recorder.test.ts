import { beforeEach, describe, expect, it } from "vitest";

import { ChunkStore, type ChunkRow, type ChunkStorage } from "./chunk-store.js";
import {
  CaptureRecorder,
  type CaptureStream,
  type RecorderDeps,
  type RecorderEvents,
  type RecorderHandle,
} from "./recorder.js";

/** Stands in for MediaRecorder. Nothing is emitted on its own — the test drives
 * `emit` so "was this slice written when it arrived, or held until stop?" is
 * observable rather than inferred. */
class FakeRecorder implements RecorderHandle {
  started: number | null = null;
  paused = false;
  stopped = false;
  constructor(private readonly events: RecorderEvents) {}

  start(timesliceMs: number): void {
    this.started = timesliceMs;
  }
  pause(): void {
    this.paused = true;
  }
  resume(): void {
    this.paused = false;
  }
  stop(): void {
    this.stopped = true;
    this.events.onStop();
  }
  emit(text: string): void {
    this.events.onData(new Blob([text], { type: "video/webm" }));
  }
  fail(message: string): void {
    this.events.onError(new Error(message));
  }
}

function fakeStream(
  trackCount = 2,
  label = "Screen 1",
): CaptureStream & { stoppedTracks: number; endTrack(): void } {
  let stoppedTracks = 0;
  const endHandlers: (() => void)[] = [];
  const tracks = Array.from({ length: trackCount }, () => ({
    label,
    stop: () => void (stoppedTracks += 1),
    onEnded: (handler: () => void) => void endHandlers.push(handler),
  }));
  return {
    getTracks: () => tracks,
    get stoppedTracks() {
      return stoppedTracks;
    },
    /** Simulates the browser's own stop-sharing control, or the captured
     * surface going away. */
    endTrack: () => endHandlers[0]?.(),
  };
}

function named(name: string): Error {
  const error = new Error(name);
  error.name = name;
  return error;
}

let rows: { sessionId: string; row: ChunkRow }[];
let failures: string[];
let ended: string[];
let recorder: FakeRecorder | null;
let stream: ReturnType<typeof fakeStream>;
let failNextPut: Error | null;

function harness(overrides: Partial<RecorderDeps> = {}) {
  const storage: ChunkStorage = {
    put: async (sessionId, row) => {
      if (failNextPut) {
        const error = failNextPut;
        failNextPut = null;
        throw error;
      }
      rows.push({ sessionId, row });
    },
    all: async (sessionId) =>
      rows.filter((r) => r.sessionId === sessionId).map((r) => r.row),
    removeSession: async () => {},
  };

  const deps: RecorderDeps = {
    tabStream: async () => stream,
    displayStream: async () => stream,
    isTypeSupported: () => true,
    createRecorder: (_stream, _mimeType, events) => {
      recorder = new FakeRecorder(events);
      return recorder;
    },
    chunks: new ChunkStore(storage),
    onFailure: (error) => void failures.push(error),
    onEnded: (sessionId) => void ended.push(sessionId),
    ...overrides,
  };

  return { recorder: new CaptureRecorder(deps), deps };
}

/** Lets the queued append promises settle; the recorder chains writes so slices
 * cannot land out of order. */
const settled = () => new Promise((resolve) => setTimeout(resolve, 0));

beforeEach(() => {
  rows = [];
  failures = [];
  ended = [];
  recorder = null;
  failNextPut = null;
  stream = fakeStream();
});

describe("start", () => {
  it("consumes a tab stream id and begins recording", async () => {
    const seen: string[] = [];
    const { recorder: capture } = harness({
      tabStream: async (streamId) => {
        seen.push(streamId);
        return stream;
      },
    });

    const result = await capture.start("s1", { kind: "tab", streamId: "stream-7" });

    expect(result).toMatchObject({ ok: true });
    expect(seen).toEqual(["stream-7"]);
    expect(recorder?.started).toBeGreaterThan(0);
  });

  it("reports the track's own name so the indicator can say what is being captured", async () => {
    // For whole-screen capture the picker runs in here and reports nothing
    // else, so the track label is the only description of what the user chose.
    stream = fakeStream(1, "Screen 1");
    const { recorder: capture } = harness();

    expect(await capture.start("s1", { kind: "screen" })).toMatchObject({
      sourceLabel: "Screen 1",
    });
  });

  it("reports no label rather than an empty one", async () => {
    // An empty string would overwrite the controller's provisional label with
    // nothing, leaving the user a recording indicator that names no source.
    stream = fakeStream(1, "");
    const { recorder: capture } = harness();

    expect(await capture.start("s1", { kind: "screen" })).toMatchObject({
      sourceLabel: null,
    });
  });

  it("reports a dismissed picker as cancelled and writes nothing", async () => {
    // AE-U3-4's recorder half. Chrome rejects with NotAllowedError when the
    // user closes the picker — an ordinary outcome, not a failure to report.
    const { recorder: capture } = harness({
      displayStream: async () => {
        throw named("NotAllowedError");
      },
    });

    const result = await capture.start("s1", { kind: "screen" });

    expect(result).toMatchObject({ ok: false, reason: "cancelled" });
    expect(rows).toEqual([]);
    expect(failures).toEqual([]);
  });

  it("distinguishes a real acquisition failure from a cancellation", async () => {
    const { recorder: capture } = harness({
      displayStream: async () => {
        throw named("NotReadableError");
      },
    });

    expect(await capture.start("s1", { kind: "screen" })).toMatchObject({
      ok: false,
      reason: "failed",
    });
  });

  it("refuses to start when no supported type is available", async () => {
    // Recording into a container the assembly step cannot name produces a file
    // nobody can play. Refusing is the honest outcome.
    let streamsAcquired = 0;
    const { recorder: capture } = harness({
      isTypeSupported: () => false,
      tabStream: async () => {
        streamsAcquired += 1;
        return stream;
      },
    });

    expect(await capture.start("s1", { kind: "tab", streamId: "x" })).toMatchObject({
      ok: false,
      reason: "unsupported",
    });
    // The check precedes acquisition, so a doomed start never asks the user for
    // a source it is about to throw away.
    expect(streamsAcquired).toBe(0);
  });
});

describe("slice persistence", () => {
  it("writes each slice as it arrives rather than holding them until stop", async () => {
    const { recorder: capture } = harness();
    await capture.start("s1", { kind: "tab", streamId: "x" });

    recorder?.emit("a");
    recorder?.emit("b");
    await settled();

    // Asserted before stop: holding slices in memory would leave this empty and
    // lose the whole recording on a crash.
    expect(rows.map((r) => r.row.index)).toEqual([0, 1]);
    expect(recorder?.stopped).toBe(false);
  });

  it("fails the recording when a slice cannot be written", async () => {
    // Continuing past a failed write produces a video with an invisible hole.
    const { recorder: capture } = harness();
    await capture.start("s1", { kind: "tab", streamId: "x" });

    failNextPut = new Error("quota exceeded");
    recorder?.emit("a");
    await settled();

    expect(failures).toEqual(["quota exceeded"]);
    expect(recorder?.stopped).toBe(true);
  });

  it("releases the source when the recording fails", async () => {
    // A live track keeps the browser telling the user they are being recorded
    // long after capture died.
    const { recorder: capture } = harness();
    await capture.start("s1", { kind: "tab", streamId: "x" });

    failNextPut = new Error("quota exceeded");
    recorder?.emit("a");
    await settled();

    expect(stream.stoppedTracks).toBe(2);
  });

  it("reports a recording truncated by a failed write as incomplete", async () => {
    // The store can only see holes, and a failed write leaves none — it stops
    // the recording, so the surviving indices stay contiguous. Passing that
    // through unchanged would let a truncated recording claim to be whole.
    const { recorder: capture } = harness();
    await capture.start("s1", { kind: "tab", streamId: "x" });
    recorder?.emit("a");
    await settled();

    failNextPut = new Error("quota exceeded");
    recorder?.emit("b");
    await settled();

    expect((await capture.stop()).complete).toBe(false);
  });

  it("surfaces a recorder error through the same failure path", async () => {
    const { recorder: capture } = harness();
    await capture.start("s1", { kind: "tab", streamId: "x" });

    recorder?.fail("encoder died");
    await settled();

    expect(failures).toEqual(["encoder died"]);
  });
});

describe("pause and resume", () => {
  it("pauses once and reports that a second pause changed nothing", async () => {
    // The allow-list can fire the same signal twice across a redirect chain.
    // Only a state change should be recorded as a pause boundary.
    const { recorder: capture } = harness();
    await capture.start("s1", { kind: "tab", streamId: "x" });

    expect(capture.pause()).toBe(true);
    expect(recorder?.paused).toBe(true);
    expect(capture.pause()).toBe(false);
  });

  it("resumes only from a paused state", async () => {
    const { recorder: capture } = harness();
    await capture.start("s1", { kind: "tab", streamId: "x" });

    expect(capture.resume()).toBe(false);
    capture.pause();
    expect(capture.resume()).toBe(true);
    expect(recorder?.paused).toBe(false);
  });
});

describe("the source ending on its own", () => {
  it("announces the session so the worker stops claiming a live recording", async () => {
    // Chrome gives the user its own stop-sharing control. Nothing about it goes
    // through this extension, so without this the badge stays on REC and the
    // popup keeps naming a source nobody is capturing.
    const { recorder: capture } = harness();
    await capture.start("s1", { kind: "tab", streamId: "x" });

    stream.endTrack();

    expect(ended).toEqual(["s1"]);
    expect(failures).toEqual([]);
  });

  it("releases the source rather than leaving tracks live", async () => {
    const { recorder: capture } = harness();
    await capture.start("s1", { kind: "tab", streamId: "x" });

    stream.endTrack();

    expect(stream.stoppedTracks).toBe(2);
  });

  it("stays quiet when the recording already failed", async () => {
    // A failure tears the stream down, which ends its tracks. Reporting that as
    // a user-initiated ending would race the failure the worker already has.
    const { recorder: capture } = harness();
    await capture.start("s1", { kind: "tab", streamId: "x" });

    failNextPut = new Error("quota exceeded");
    recorder?.emit("a");
    await settled();
    stream.endTrack();

    expect(ended).toEqual([]);
  });
});

describe("stop", () => {
  it("releases every track so the browser drops its sharing indicator", async () => {
    // A live track leaves Chrome telling the user they are still being
    // recorded after they stopped.
    const { recorder: capture } = harness();
    await capture.start("s1", { kind: "tab", streamId: "x" });
    recorder?.emit("a");

    const summary = await capture.stop();

    expect(stream.stoppedTracks).toBe(2);
    expect(summary).toMatchObject({ chunkCount: 1, complete: true });
  });

  it("releases tracks when stopped while paused", async () => {
    const { recorder: capture } = harness();
    await capture.start("s1", { kind: "tab", streamId: "x" });
    capture.pause();

    await capture.stop();

    expect(stream.stoppedTracks).toBe(2);
  });

  it("keeps a slice that arrives during shutdown", async () => {
    // MediaRecorder flushes a final dataavailable on stop; dropping it would
    // truncate every recording by up to one timeslice.
    const { recorder: capture } = harness();
    await capture.start("s1", { kind: "tab", streamId: "x" });

    const stopping = capture.stop();
    recorder?.emit("final");
    const summary = await stopping;

    expect(summary.chunkCount).toBe(1);
  });
});
