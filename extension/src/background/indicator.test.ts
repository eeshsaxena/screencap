import { describe, expect, it } from "vitest";

import type { ResolvedSession, SessionRecord } from "../capture/session.js";
import { IDLE_BADGE, applyBadge, badgeFor, type BadgeDeps } from "./indicator.js";

function session(kind: ResolvedSession["kind"], label = "Looker — Revenue"): ResolvedSession {
  return {
    kind,
    record: kind === "idle" ? null : ({ source: { kind: "tab", label } } as SessionRecord),
    error: null,
  };
}

describe("badgeFor", () => {
  it("marks a running recording and names the source in the tooltip", () => {
    // The badge has room for a few characters; the source name only fits in
    // the tooltip, which is the only place it can be read without the popup.
    const badge = badgeFor(session("recording"));

    expect(badge.text).toBe("REC");
    expect(badge.title).toContain("Looker — Revenue");
  });

  it("distinguishes paused from recording", () => {
    expect(badgeFor(session("paused")).text).not.toBe(badgeFor(session("recording")).text);
  });

  it("flags an unfinished recording distinctly from idle", () => {
    // AE-U3-2 at a glance: idle and interrupted must not look the same, or a
    // stranded recording is invisible until the popup is opened.
    for (const kind of ["interrupted", "failed"] as const) {
      const badge = badgeFor(session(kind));
      expect(badge.text).not.toBe(IDLE_BADGE.text);
      expect(badge.title).toMatch(/didn't finish/i);
    }
  });

  it("stays quiet through brief transitions", () => {
    for (const kind of ["starting", "stopping", "idle"] as const) {
      expect(badgeFor(session(kind))).toEqual(IDLE_BADGE);
    }
  });

  it("still reports recording when the record carries no label", () => {
    const badge = badgeFor({ kind: "recording", record: null, error: null });

    expect(badge.text).toBe("REC");
    expect(badge.title).toBe("Screencap is recording");
  });
});

describe("applyBadge", () => {
  it("paints text, colour and tooltip together", async () => {
    const painted: Record<string, string> = {};
    const deps: BadgeDeps = {
      setText: async (text) => void (painted.text = text),
      setColor: async (color) => void (painted.color = color),
      setTitle: async (title) => void (painted.title = title),
    };

    await applyBadge(deps, badgeFor(session("recording")));

    expect(painted).toMatchObject({ text: "REC" });
    expect(painted.color).toBeTruthy();
    expect(painted.title).toContain("Looker");
  });

  it("does not let a failed repaint take down the caller", async () => {
    // The badge is an indicator. Losing it must never fail the stop that was
    // repainting it.
    const deps: BadgeDeps = {
      setText: async () => {
        throw new Error("action unavailable");
      },
      setColor: async () => {},
      setTitle: async () => {},
    };

    await expect(applyBadge(deps, IDLE_BADGE)).resolves.toBeUndefined();
  });
});
