import { describe, expect, it } from "vitest";

import type { ResolvedSession, SessionRecord } from "../capture/session.js";
import { captureView } from "./capture-view.js";

function session(
  kind: ResolvedSession["kind"],
  label = "Looker — Revenue",
  error: string | null = null,
): ResolvedSession {
  const record = {
    id: "s1",
    source: { kind: "tab", label },
    startedAtEpochMs: 0,
    pausedIntervals: [],
    mimeType: "video/webm",
    state: "recording",
    error,
  } as SessionRecord;
  return { kind, record: kind === "idle" ? null : record, error };
}

describe("captureView", () => {
  it("stays hidden without an account to record under", () => {
    expect(captureView({ signedIn: false, status: session("idle") }).visible).toBe(false);
  });

  it("names the source while recording and offers only stop", () => {
    // AE-U3-1's user-visible half: the source must be nameable at any moment,
    // including right after the worker was revived from the stored record.
    const view = captureView({ signedIn: true, status: session("recording") });

    expect(view.message).toBe("Recording Looker — Revenue.");
    expect(view.sourceLabel).toBe("Looker — Revenue");
    expect(view).toMatchObject({ canStart: false, canStop: true });
  });

  it("keeps naming the source while paused", () => {
    // Paused still holds the source, and the user needs to know what resumes.
    const view = captureView({ signedIn: true, status: session("paused") });

    expect(view.message).toBe("Paused — Looker — Revenue.");
    expect(view).toMatchObject({ canStart: false, canStop: true });
  });

  it("reads an interrupted recording as unfinished, not as idle", () => {
    // AE-U3-2. Collapsing this into idle would tell someone their recording
    // completed while its bytes sit stranded.
    const view = captureView({ signedIn: true, status: session("interrupted") });

    expect(view.message).toMatch(/interrupted/i);
    expect(view.message).not.toBe(
      captureView({ signedIn: true, status: session("idle") }).message,
    );
    // Starting again must stay possible, or stranded bytes wedge the extension.
    expect(view).toMatchObject({ canStart: true, canStop: false });
  });

  it("carries the failure reason so the user has something to act on", () => {
    const view = captureView({
      signedIn: true,
      status: session("failed", "Looker", "quota exceeded"),
    });

    expect(view.message).toContain("quota exceeded");
    expect(view.canStart).toBe(true);
  });

  it("offers neither action mid-transition", () => {
    for (const kind of ["starting", "stopping"] as const) {
      expect(captureView({ signedIn: true, status: session(kind) })).toMatchObject({
        canStart: false,
        canStop: false,
      });
    }
  });

  it("withholds both actions while a verb is in flight", () => {
    // A second click during start would race the first into a refused start.
    expect(
      captureView({ signedIn: true, status: session("idle"), busy: true }).canStart,
    ).toBe(false);
    expect(
      captureView({ signedIn: true, status: session("recording"), busy: true }).canStop,
    ).toBe(false);
  });

  it("falls back to an unnamed recording rather than rendering a blank source", () => {
    const status = { kind: "recording", record: null, error: null } as ResolvedSession;

    expect(captureView({ signedIn: true, status }).message).toBe("Recording.");
  });
});
