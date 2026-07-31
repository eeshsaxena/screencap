import { describe, expect, it } from "vitest";

import type { ResolvedSession, SessionRecord } from "../capture/session.js";
import { UNREACHABLE_MESSAGE, captureProblem, captureView } from "./capture-view.js";

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

  it("keeps stop reachable mid-transition so a dead worker cannot strand it", () => {
    // starting/stopping are normally momentary, but a worker that dies mid-verb
    // leaves one persisted. A state offering no action at all would wedge the
    // user until the browser restarted.
    for (const kind of ["starting", "stopping"] as const) {
      expect(captureView({ signedIn: true, status: session(kind) })).toMatchObject({
        canStart: false,
        canStop: true,
      });
    }
  });

  it("greys actions out while a verb is in flight rather than hiding them", () => {
    // A second click during start would race the first into a refused start.
    // Removing the button instead of disabling it makes it vanish under the
    // cursor mid-click.
    const starting = captureView({
      signedIn: true,
      status: session("idle"),
      busy: true,
    });
    expect(starting).toMatchObject({ showStart: true, canStart: false });

    const stopping = captureView({
      signedIn: true,
      status: session("recording"),
      busy: true,
    });
    expect(stopping).toMatchObject({ showStop: true, canStop: false });
  });

  it("falls back to an unnamed recording rather than rendering a blank source", () => {
    const status = { kind: "recording", record: null, error: null } as ResolvedSession;

    expect(captureView({ signedIn: true, status }).message).toBe("Recording.");
  });

  it("admits it cannot tell rather than claiming nothing is recording", () => {
    // The worker did not answer, so there is no status to report. Rendering
    // "Not recording." would assert the one thing that must never be guessed.
    const view = captureView({
      signedIn: true,
      status: session("idle"),
      reachable: false,
    });

    expect(view.message).toBe(UNREACHABLE_MESSAGE);
    expect(view.message).not.toBe(
      captureView({ signedIn: true, status: session("idle") }).message,
    );
    // Neither action can be trusted to reach anything.
    expect(view).toMatchObject({ showStart: false, showStop: false });
  });

  it("offers stop but not start when something is recording unidentifiably", () => {
    // A document is alive whose record could not be read. Starting would open a
    // second one beside it; stopping can only help.
    expect(captureView({ signedIn: true, status: session("unknown") })).toMatchObject({
      showStart: false,
      showStop: true,
    });
  });
});

describe("captureProblem", () => {
  it("names what is already running when a start is refused", () => {
    expect(
      captureProblem({
        ok: true,
        start: {
          ok: false,
          reason: "already-recording",
          runningSource: { kind: "tab", label: "Looker" },
        },
      }),
    ).toBe("Already recording Looker.");
  });

  it("stays silent when the user dismissed the picker", () => {
    // They closed it on purpose; an error message would scold them for it.
    expect(
      captureProblem({
        ok: true,
        start: { ok: false, reason: "cancelled", error: "NotAllowedError" },
      }),
    ).toBeNull();
  });

  it("surfaces a genuine start failure", () => {
    expect(
      captureProblem({
        ok: true,
        start: { ok: false, reason: "unsupported", error: "No usable container." },
      }),
    ).toBe("No usable container.");
  });

  it("explains a stop that had nothing to stop", () => {
    expect(
      captureProblem({ ok: true, stop: { ok: false, reason: "not-recording" } }),
    ).toBe("There was no recording to stop.");
  });

  it("passes a transport failure straight through", () => {
    expect(captureProblem({ ok: false, error: "Unauthorized sender" })).toBe(
      "Unauthorized sender",
    );
  });

  it("says nothing about a verb that worked", () => {
    expect(
      captureProblem({
        ok: true,
        start: { ok: true, sessionId: "s1", source: { kind: "tab", label: "Looker" } },
      }),
    ).toBeNull();
  });
});
