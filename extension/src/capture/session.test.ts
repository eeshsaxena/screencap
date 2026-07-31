import { describe, expect, it } from "vitest";

import {
  SESSION_STORAGE_KEY,
  SessionStore,
  beginSession,
  markFailed,
  markPaused,
  markRecording,
  markResumed,
  reconcile,
  type SessionRecord,
  type SessionStorage,
} from "./session.js";

const START = {
  id: "session-1",
  source: { kind: "tab", label: "Looker — Revenue" },
  startedAtEpochMs: 1_000,
} as const;

function recording(overrides: Partial<SessionRecord> = {}): SessionRecord {
  const begun = beginSession({ kind: "idle" }, START);
  if (!begun.ok) throw new Error("fixture could not begin a session");
  return { ...markRecording(begun.record), ...overrides };
}

/** In-memory stand-in for `chrome.storage.local`, which the store never calls
 * directly. */
function fakeStorage(initial: Record<string, string> = {}) {
  const cells = new Map(Object.entries(initial));
  const storage: SessionStorage = {
    get: async (key) => cells.get(key) ?? null,
    set: async (key, value) => void cells.set(key, value),
    remove: async (key) => void cells.delete(key),
  };
  return { storage, cells };
}

describe("reconcile", () => {
  it("survives service-worker suspension: a reloaded record still names its source", async () => {
    // AE-U3-1. The worker's memory is gone; storage is all that is left.
    const { storage, cells } = fakeStorage();
    await new SessionStore(storage).write(recording());

    const revived = await new SessionStore(storage).read();
    const resolved = reconcile(revived, true);

    expect(cells.has(SESSION_STORAGE_KEY)).toBe(true);
    expect(resolved.kind).toBe("recording");
    expect(resolved.record?.source.label).toBe("Looker — Revenue");
  });

  it("resolves a running record with no offscreen document to interrupted", () => {
    // AE-U3-2. The browser crashed: the record outlived the document.
    expect(reconcile(recording(), false).kind).toBe("interrupted");
  });

  it("keeps a paused record honest when the document is gone", () => {
    expect(reconcile(markPaused(recording(), 2_000), false).kind).toBe("interrupted");
  });

  it("reads no record as idle whether or not a document exists", () => {
    expect(reconcile(null, false).kind).toBe("idle");
    // A document with no record is an orphan, not a recording — the controller
    // closes it. Reporting it as running would strand the user.
    expect(reconcile(null, true).kind).toBe("idle");
  });

  it("reports a failed record as failed rather than interrupted", () => {
    // Failure already closed the document, so absence is expected here and
    // must not be re-read as a crash.
    const resolved = reconcile(markFailed(recording(), "encoder died"), false);
    expect(resolved.kind).toBe("failed");
    expect(resolved.error).toBe("encoder died");
  });
});

describe("beginSession", () => {
  it("refuses a second recording and leaves the running one untouched", () => {
    // AE-U3-3.
    const running = reconcile(recording(), true);
    const result = beginSession(running, { ...START, id: "session-2" });

    expect(result).toEqual({ ok: false, reason: "already-recording" });
    expect(running.record?.id).toBe("session-1");
  });

  it("allows a new recording over an interrupted one", () => {
    // AE-U3-2's second half: stranded bytes must not wedge the extension.
    const result = beginSession(reconcile(recording(), false), {
      ...START,
      id: "session-2",
    });

    expect(result.ok).toBe(true);
    expect(result.ok && result.record.id).toBe("session-2");
  });
});

describe("pause accounting", () => {
  it("records an open interval on pause and closes it on resume", () => {
    const paused = markPaused(recording(), 3_000);
    expect(paused.state).toBe("paused");
    expect(paused.pausedIntervals).toEqual([{ fromOffsetMs: 2_000, toOffsetMs: null }]);

    const resumed = markResumed(paused, 5_000);
    expect(resumed.state).toBe("recording");
    expect(resumed.pausedIntervals).toEqual([
      { fromOffsetMs: 2_000, toOffsetMs: 4_000 },
    ]);
  });

  it("ignores a pause while already paused rather than nesting intervals", () => {
    // The allow-list can fire the same signal twice across a redirect chain; a
    // nested interval would double-count the gap and desynchronize the timeline.
    const paused = markPaused(recording(), 3_000);
    expect(markPaused(paused, 4_000)).toEqual(paused);
  });
});

describe("SessionStore", () => {
  it("reads a corrupt record as no session rather than throwing", async () => {
    // Storage never authorizes capture, so an unreadable cell costs display
    // only — the same posture `Allowlist.readStored` takes.
    const { storage } = fakeStorage({ [SESSION_STORAGE_KEY]: "{not json" });
    expect(await new SessionStore(storage).read()).toBeNull();
  });

  it("clears the record so a finished recording stops reading as running", async () => {
    const { storage, cells } = fakeStorage();
    const store = new SessionStore(storage);
    await store.write(recording());
    await store.clear();

    expect(cells.has(SESSION_STORAGE_KEY)).toBe(false);
    expect(await store.read()).toBeNull();
  });
});
