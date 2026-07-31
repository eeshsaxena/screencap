import { describe, expect, it } from "vitest";

import { offsetMsAt, videoTimeMsFor } from "./clock.js";
import type { SessionRecord } from "./session.js";

/** Only the two fields the clock reads; the rest of the record is irrelevant
 * here and stubbing it whole would hide that narrowness. */
function clockOf(
  pausedIntervals: SessionRecord["pausedIntervals"],
): Pick<SessionRecord, "startedAtEpochMs" | "pausedIntervals"> {
  return { startedAtEpochMs: 1_000, pausedIntervals };
}

describe("offsetMsAt", () => {
  it("is monotonic across a pause and resume", () => {
    // Wall-clock offsets keep running while paused — that is what makes them
    // usable as the event stream's clock regardless of what the video does.
    const clock = clockOf([{ fromOffsetMs: 2_000, toOffsetMs: 4_000 }]);
    const offsets = [1_500, 3_000, 5_000, 6_000].map((at) => offsetMsAt(clock, at));

    expect(offsets).toEqual([500, 2_000, 4_000, 5_000]);
    expect(offsets).toEqual([...offsets].sort((a, b) => a - b));
  });

  it("clamps an instant before the clock origin to zero", () => {
    expect(offsetMsAt(clockOf([]), 0)).toBe(0);
  });
});

describe("videoTimeMsFor", () => {
  it("equals the recording offset when nothing was ever paused", () => {
    expect(videoTimeMsFor(clockOf([]), 5_000)).toBe(5_000);
  });

  it("subtracts the accumulated paused duration after a pause", () => {
    // MediaRecorder.pause() elides the span, so video time runs behind
    // recording time by exactly the pause.
    const clock = clockOf([{ fromOffsetMs: 2_000, toOffsetMs: 4_000 }]);
    expect(videoTimeMsFor(clock, 5_000)).toBe(3_000);
  });

  it("subtracts every prior pause, not just the last", () => {
    const clock = clockOf([
      { fromOffsetMs: 1_000, toOffsetMs: 2_000 },
      { fromOffsetMs: 4_000, toOffsetMs: 6_000 },
    ]);
    expect(videoTimeMsFor(clock, 8_000)).toBe(5_000);
  });

  it("maps an instant inside a paused interval to the boundary", () => {
    // There is no frame for that moment. Seeking to the pause boundary shows
    // the last thing actually captured; interpolating would point at a frame
    // from after the resume and mislabel it.
    const clock = clockOf([{ fromOffsetMs: 2_000, toOffsetMs: 4_000 }]);
    expect(videoTimeMsFor(clock, 3_000)).toBe(2_000);
  });

  it("maps an instant during an open pause to the boundary", () => {
    const clock = clockOf([{ fromOffsetMs: 2_000, toOffsetMs: null }]);
    expect(videoTimeMsFor(clock, 9_000)).toBe(2_000);
  });

  it("never runs backwards across a pause boundary", () => {
    const clock = clockOf([{ fromOffsetMs: 2_000, toOffsetMs: 4_000 }]);
    const times = [1_000, 2_000, 3_000, 4_000, 5_000].map((o) =>
      videoTimeMsFor(clock, o),
    );
    expect(times).toEqual([...times].sort((a, b) => a - b));
  });
});
