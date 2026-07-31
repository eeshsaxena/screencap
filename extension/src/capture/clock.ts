/**
 * The recording clock — the seam the event stream aligns to.
 *
 * Two timelines exist and they are not the same one:
 *
 * - **Recording time** is wall-clock elapsed since the recording began. It runs
 *   through pauses. Every captured event is stamped against it, because an
 *   event's "when" should not depend on whether the recorder happened to be
 *   paused.
 * - **Video time** is the position in the produced file. `MediaRecorder.pause()`
 *   *elides* the paused span rather than freezing a frame, so the file is
 *   shorter than the recording by the total time paused.
 *
 * Conflating them means an event captured after a pause seeks to the wrong
 * frame — off by the accumulated pause, growing with every pause. So this
 * module owns the mapping in both directions and is the only place that
 * arithmetic lives.
 *
 * The offsets are plain numbers rather than a class so the event stream can
 * stamp events without holding a live object across a service-worker
 * suspension: the record carries the origin, this module carries the maths.
 */

import type { SessionRecord } from "./session.js";

/** Only the fields the clock reads. Narrower than the whole record on purpose —
 * a caller with just the origin and pauses can do this arithmetic. */
export type RecordingClock = Pick<
  SessionRecord,
  "startedAtEpochMs" | "pausedIntervals"
>;

/**
 * Recording-relative position of a wall-clock instant.
 *
 * Clamped at zero: an instant before the origin means clocks moved (a system
 * time change, or an event queued just before capture began), and a negative
 * offset would sort ahead of the recording's own start.
 */
export function offsetMsAt(clock: RecordingClock, epochMs: number): number {
  return Math.max(0, epochMs - clock.startedAtEpochMs);
}

/**
 * Where a recording-relative offset lands in the produced video.
 *
 * An offset that falls *inside* a pause has no frame of its own. It maps to the
 * pause's leading boundary — the last moment actually captured — rather than
 * being interpolated forward, which would point at a frame from after the
 * resume and label it with a time it does not depict.
 *
 * The result is non-decreasing in `offsetMs`, which is what lets a timeline
 * seek from it without reordering events.
 */
export function videoTimeMsFor(clock: RecordingClock, offsetMs: number): number {
  let elided = 0;
  for (const interval of clock.pausedIntervals) {
    if (offsetMs <= interval.fromOffsetMs) break;
    const closedAt = interval.toOffsetMs;
    // A null end is a pause still open, so everything at or after its start is
    // inside it.
    if (closedAt === null || offsetMs < closedAt) {
      return interval.fromOffsetMs - elided;
    }
    elided += closedAt - interval.fromOffsetMs;
  }
  return offsetMs - elided;
}
