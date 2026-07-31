import { beforeEach, describe, expect, it } from "vitest";

import { SessionStore, type SessionStorage } from "../capture/session.js";
import type { OffscreenRequest, OffscreenResponse } from "../capture/protocol.js";
import {
  CaptureController,
  registerCaptureListener,
  type CaptureControllerDeps,
} from "./capture-controller.js";

const TAB = { kind: "tab", streamId: "stream-7", label: "Looker — Revenue" } as const;

interface Fake {
  cells: Map<string, string>;
  offscreenExists: boolean;
  /** Every offscreen lifecycle call, in order — teardown correctness is about
   * ordering, not just about the final state. */
  lifecycle: string[];
  sent: OffscreenRequest[];
  startResult: OffscreenResponse;
  stopSummary: { chunkCount: number; byteLength: number; complete: boolean };
  pauseChanged: boolean;
  clock: number;
}

let fake: Fake;

function deps(overrides: Partial<CaptureControllerDeps> = {}): CaptureControllerDeps {
  const storage: SessionStorage = {
    get: async (key) => fake.cells.get(key) ?? null,
    set: async (key, value) => void fake.cells.set(key, value),
    remove: async (key) => void fake.cells.delete(key),
  };

  return {
    session: new SessionStore(storage),
    hasOffscreenDocument: async () => fake.offscreenExists,
    createOffscreenDocument: async () => {
      fake.lifecycle.push("create");
      fake.offscreenExists = true;
    },
    closeOffscreenDocument: async () => {
      fake.lifecycle.push("close");
      fake.offscreenExists = false;
    },
    sendToOffscreen: async (request) => {
      fake.sent.push(request);
      switch (request.type) {
        case "offscreen.start":
          return fake.startResult;
        case "offscreen.stop":
          return { type: "offscreen.stop", summary: fake.stopSummary };
        default:
          return { type: request.type, changed: fake.pauseChanged };
      }
    },
    now: () => fake.clock,
    newSessionId: () => "session-1",
    ...overrides,
  };
}

/** Drives a controller to a running recording, which most cases start from. */
async function running(controller: CaptureController) {
  const result = await controller.start(TAB);
  expect(result.ok).toBe(true);
  fake.lifecycle = [];
  fake.sent = [];
  return result;
}

beforeEach(() => {
  fake = {
    cells: new Map(),
    offscreenExists: false,
    lifecycle: [],
    sent: [],
    startResult: {
      type: "offscreen.start",
      result: { ok: true, mimeType: "video/webm;codecs=vp9", sourceLabel: null },
    },
    stopSummary: { chunkCount: 4, byteLength: 2048, complete: true },
    pauseChanged: true,
    clock: 1_000,
  };
});

describe("start", () => {
  it("opens exactly one offscreen document and records the negotiated type", async () => {
    const controller = new CaptureController(deps());

    const result = await controller.start(TAB);

    expect(result).toMatchObject({ ok: true, sessionId: "session-1" });
    expect(fake.lifecycle).toEqual(["create"]);
    const status = await controller.status();
    expect(status.kind).toBe("recording");
    expect(status.record?.mimeType).toBe("video/webm;codecs=vp9");
    expect(status.record?.source.label).toBe("Looker — Revenue");
  });

  it("tears the document down and clears the record when the picker is dismissed", async () => {
    // AE-U3-4. For whole-screen capture the document must exist before the user
    // chooses — the picker lives inside it — so cancelling has something to
    // clean up, unlike the tab path.
    fake.startResult = {
      type: "offscreen.start",
      result: { ok: false, reason: "cancelled", error: "NotAllowedError" },
    };
    const controller = new CaptureController(deps());

    const result = await controller.start({ kind: "screen" });

    expect(result).toMatchObject({ ok: false, reason: "cancelled" });
    expect(fake.lifecycle).toEqual(["create", "close"]);
    expect(fake.offscreenExists).toBe(false);
    expect((await controller.status()).kind).toBe("idle");
  });

  it("refuses a second recording, naming the one already running", async () => {
    // AE-U3-3. Declining without saying what is running leaves the user with
    // no way to act on the refusal.
    const controller = new CaptureController(deps());
    await running(controller);

    const result = await controller.start({ kind: "screen" });

    expect(result).toEqual({
      ok: false,
      reason: "already-recording",
      runningSource: { kind: "tab", label: "Looker — Revenue" },
    });
    expect(fake.lifecycle).toEqual([]);
    expect((await controller.status()).record?.id).toBe("session-1");
  });

  it("closes an orphaned document rather than refusing forever", async () => {
    // A crash between opening the document and writing the record leaves a
    // document nothing tracks. Refusing on its presence alone would wedge the
    // extension with no way back; only a live record means a real recording.
    fake.offscreenExists = true;
    const controller = new CaptureController(deps());

    const result = await controller.start(TAB);

    expect(result.ok).toBe(true);
    expect(fake.lifecycle).toEqual(["close", "create"]);
  });

  it("adopts the track's own name for the source when the recorder reports one", async () => {
    // Whole-screen capture starts with a provisional label because the picker
    // runs inside the offscreen document; the track is what says which screen.
    fake.startResult = {
      type: "offscreen.start",
      result: { ok: true, mimeType: "video/webm", sourceLabel: "Screen 1" },
    };
    const controller = new CaptureController(deps());

    await controller.start({ kind: "screen" });

    expect((await controller.status()).record?.source.label).toBe("Screen 1");
  });

  it("keeps the provisional label when the track has no name", async () => {
    const controller = new CaptureController(deps());

    await controller.start({ kind: "screen" });

    expect((await controller.status()).record?.source.label).toBe("Your screen");
  });

  it("starts over an interrupted recording", async () => {
    // AE-U3-2's second half: stranded bytes must not block a new recording.
    const controller = new CaptureController(deps());
    await running(controller);
    fake.offscreenExists = false; // the browser died; the record outlived it

    expect((await controller.status()).kind).toBe("interrupted");
    expect((await controller.start({ kind: "screen" })).ok).toBe(true);
  });
});

describe("stop", () => {
  it("closes the document, clears the record, and hands back the recording", async () => {
    const controller = new CaptureController(deps());
    await running(controller);

    const result = await controller.stop();

    expect(result).toEqual({
      ok: true,
      handle: {
        sessionId: "session-1",
        mimeType: "video/webm;codecs=vp9",
        chunkCount: 4,
        byteLength: 2048,
        complete: true,
      },
    });
    expect(fake.lifecycle).toEqual(["close"]);
    expect((await controller.status()).kind).toBe("idle");
  });

  it("refuses when nothing is recording", async () => {
    expect(await new CaptureController(deps()).stop()).toEqual({
      ok: false,
      reason: "not-recording",
    });
  });

  it("still tears down when the offscreen document cannot be reached", async () => {
    // Leaving the document open and the record claiming "recording" would show
    // the user a recording they can never stop.
    const base = deps();
    const controller = new CaptureController({
      ...base,
      // Only the stop is unreachable — the recording itself started fine, which
      // is the situation this covers.
      sendToOffscreen: async (request) => {
        if (request.type === "offscreen.stop") {
          throw new Error("Could not establish connection");
        }
        return base.sendToOffscreen(request);
      },
    });
    await running(controller);

    const result = await controller.stop();

    expect(result.ok).toBe(false);
    expect(fake.lifecycle).toEqual(["close"]);
    expect((await controller.status()).kind).toBe("idle");
  });
});

describe("pause", () => {
  it("records a pause boundary only when the recorder actually changed state", async () => {
    const controller = new CaptureController(deps());
    await running(controller);
    fake.clock = 4_000;

    await controller.pause();

    const record = (await controller.status()).record;
    expect(record?.state).toBe("paused");
    expect(record?.pausedIntervals).toEqual([{ fromOffsetMs: 3_000, toOffsetMs: null }]);
  });

  it("leaves the record untouched when the recorder was already paused", async () => {
    // The allow-list can fire the same signal twice across a redirect chain;
    // a second interval would double-count the gap.
    const controller = new CaptureController(deps());
    await running(controller);
    await controller.pause();
    fake.pauseChanged = false;
    fake.clock = 9_000;

    await controller.pause();

    expect((await controller.status()).record?.pausedIntervals).toHaveLength(1);
  });
});

describe("message listener", () => {
  type Listener = (
    request: unknown,
    sender: chrome.runtime.MessageSender,
    sendResponse: (r: unknown) => void,
  ) => boolean;

  const EXTENSION_ID = "test-extension-id";

  function register() {
    const registered: Listener[] = [];
    (globalThis as unknown as { chrome: unknown }).chrome = {
      runtime: {
        id: EXTENSION_ID,
        onMessage: { addListener: (fn: Listener) => registered.push(fn) },
      },
    };
    registerCaptureListener(new CaptureController(deps()));
    return registered[0]!;
  }

  it("refuses a capture message from a content script before opening anything", async () => {
    // From the event-stream unit onward a content script runs on every
    // allow-listed origin. A visited page must never be able to start a
    // recording of it.
    const listener = register();
    let response: unknown;

    listener(
      { type: "capture.start", source: TAB },
      { id: EXTENSION_ID, tab: { id: 7 } } as chrome.runtime.MessageSender,
      (r) => void (response = r),
    );

    expect(response).toEqual({ ok: false, error: "Unauthorized sender" });
    expect(fake.lifecycle).toEqual([]);
  });

  it("declines messages belonging to another listener", () => {
    // The auth listener answers these; claiming them here would break sign-in.
    const listener = register();

    expect(
      listener({ type: "auth.whoami" }, { id: EXTENSION_ID } as chrome.runtime.MessageSender, () => {}),
    ).toBe(false);
  });

  it("keeps the channel open for a capture message from an extension page", () => {
    const listener = register();

    expect(
      listener({ type: "capture.status" }, { id: EXTENSION_ID } as chrome.runtime.MessageSender, () => {}),
    ).toBe(true);
  });
});

describe("the source ending on its own", () => {
  it("tears down so the indicator stops claiming a live recording", async () => {
    // Chrome's own stop-sharing control does not go through this extension.
    const controller = new CaptureController(deps());
    const started = await running(controller);
    const sessionId = started.ok ? started.sessionId : "";

    await controller.recorderEnded(sessionId);

    expect((await controller.status()).kind).toBe("idle");
    expect(fake.lifecycle).toEqual(["close"]);
  });

  it("ignores a message naming a session that is no longer the live one", async () => {
    // A late message must not end a recording the user has since started.
    const controller = new CaptureController(deps());
    await running(controller);

    await controller.recorderEnded("some-older-session");

    expect((await controller.status()).kind).toBe("recording");
    expect(fake.lifecycle).toEqual([]);
  });
});

describe("unreadable session state", () => {
  it("refuses to start rather than closing a document it cannot identify", async () => {
    // The record could not be read but a capture document is alive. Starting
    // would run the orphan cleanup against a recording that may still be going.
    fake.cells.set("screencap.capture.session", "{not json");
    fake.offscreenExists = true;
    const controller = new CaptureController(deps());

    expect((await controller.status()).kind).toBe("unknown");

    const result = await controller.start(TAB);

    expect(result.ok).toBe(false);
    expect(fake.lifecycle).toEqual([]);
  });
});

describe("recorder failure", () => {
  it("marks the session failed and closes the document", async () => {
    const controller = new CaptureController(deps());
    await running(controller);

    await controller.recorderFailed("encoder died");

    const status = await controller.status();
    expect(status.kind).toBe("failed");
    expect(status.error).toBe("encoder died");
    expect(fake.lifecycle).toEqual(["close"]);
  });
});
