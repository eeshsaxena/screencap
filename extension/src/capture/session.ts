/**
 * What is being recorded right now — the one answer the extension cannot keep
 * in memory.
 *
 * The service worker is suspended after roughly 30 seconds idle, so any
 * running-state flag it holds is gone on revival. The offscreen document keeps
 * recording through that suspension (it has no lifetime limit of its own), so
 * "the worker forgot" and "nothing is recording" are entirely different
 * situations and must not collapse into one.
 *
 * ## Neither store is sufficient alone
 *
 * - **The record alone lies after a crash.** A browser that dies mid-recording
 *   leaves a record saying `recording` with no document behind it.
 * - **The document alone carries no identity.** Its presence says something is
 *   recording, not *what* — and the user is owed the source name.
 *
 * So {@link reconcile} takes both and is the only honest reading. It runs on
 * every read rather than on an event, for the same reason `Allowlist.list`
 * reconciles instead of caching: the failure this exists to catch is precisely
 * the one that fires no event.
 *
 * ## Why `chrome.storage.local`, not `session`
 *
 * `storage.session` is cleared on browser restart, which is exactly when a
 * crashed recording's chunks are stranded on disk. Erasing the evidence would
 * make them unrecoverable and unreportable. The record outliving its recording
 * is the point; {@link reconcile} is what keeps that from being a lie.
 *
 * Transitions are pure functions over the record — the transition decides, the
 * caller persists — mirroring the decision/DOM split in `../popup/view.ts`.
 */

export type CaptureSourceKind = "tab" | "screen";

export interface CaptureSource {
  kind: CaptureSourceKind;
  /** Shown to the user, so it names the thing they picked ("Looker — Revenue",
   * "Entire screen") rather than an opaque id. */
  label: string;
}

/** A span the recorder was paused for, in recording-relative milliseconds.
 * `toOffsetMs` is null while the pause is still open. */
export interface PausedInterval {
  fromOffsetMs: number;
  toOffsetMs: number | null;
}

/** States a record can be persisted in. `idle` and `interrupted` are absent by
 * design: idle is the *absence* of a record, and interrupted is a conclusion
 * {@link reconcile} draws, never something written. */
export type StoredSessionState =
  | "starting"
  | "recording"
  | "paused"
  | "stopping"
  | "failed";

export interface SessionRecord {
  id: string;
  source: CaptureSource;
  /** Epoch milliseconds where the recording clock's zero sits. */
  startedAtEpochMs: number;
  pausedIntervals: PausedInterval[];
  /** Negotiated by the recorder and reported back; null until it starts. The
   * offscreen document cannot write this itself — only `chrome.runtime` is
   * available to it — so the controller records what the recorder reports. */
  mimeType: string | null;
  state: StoredSessionState;
  error: string | null;
}

export type ResolvedSessionKind =
  | "idle"
  | StoredSessionState
  | /** A live record with no document behind it: the browser died mid-recording
     * and this session's chunks are stranded. */ "interrupted";

export interface ResolvedSession {
  kind: ResolvedSessionKind;
  record: SessionRecord | null;
  error: string | null;
}

export interface SessionInit {
  id: string;
  source: CaptureSource;
  startedAtEpochMs: number;
}

export type StartOutcome =
  | { ok: true; record: SessionRecord }
  | { ok: false; reason: "already-recording" };

export const SESSION_STORAGE_KEY = "screencap.capture.session";

/** States that mean a recording is genuinely in flight, so a second start must
 * be refused and a missing document means a crash. */
const LIVE_STATES = new Set<string>(["starting", "recording", "paused", "stopping"]);

/**
 * Whether a recording is genuinely in flight.
 *
 * Exported because the controller gates starts and stops on the same question,
 * and two lists of "which states count as live" would drift — the failure being
 * a second recording admitted over a running one, or a stop refused for a real
 * recording.
 */
export function isLiveSessionKind(kind: ResolvedSessionKind): boolean {
  return LIVE_STATES.has(kind);
}

const IDLE: ResolvedSession = { kind: "idle", record: null, error: null };

/**
 * The honest current state, given the stored record and whether an offscreen
 * document is actually alive.
 *
 * A record in a live state with no document resolves to `interrupted` — see the
 * module docstring. A `failed` record resolves to `failed` whether or not a
 * document exists: failure closes the document, so its absence is expected
 * there and re-reading it as a crash would replace a specific cause with a
 * vague one.
 *
 * A document with no record is an *orphan*, not a recording — the controller
 * closes it before starting. Reporting it as running would leave the user
 * looking at a recording they cannot stop.
 */
export function reconcile(
  record: SessionRecord | null,
  offscreenExists: boolean,
): ResolvedSession {
  if (record === null) return IDLE;
  if (record.state === "failed") {
    return { kind: "failed", record, error: record.error };
  }
  if (isLiveSessionKind(record.state) && !offscreenExists) {
    return { kind: "interrupted", record, error: null };
  }
  return { kind: record.state, record, error: null };
}

/**
 * Claim the recording slot.
 *
 * Takes the *resolved* state rather than the raw record so that an interrupted
 * or failed session does not wedge the extension: those are finished, however
 * badly, and the user must be able to start again. Only a genuinely live
 * session refuses.
 */
export function beginSession(
  current: Pick<ResolvedSession, "kind">,
  init: SessionInit,
): StartOutcome {
  if (isLiveSessionKind(current.kind)) {
    return { ok: false, reason: "already-recording" };
  }
  return {
    ok: true,
    record: {
      id: init.id,
      source: init.source,
      startedAtEpochMs: init.startedAtEpochMs,
      pausedIntervals: [],
      mimeType: null,
      state: "starting",
      error: null,
    },
  };
}

/** The recorder confirmed capture is running. `mimeType` arrives here because
 * only the recorder knows what the browser actually accepted. */
export function markRecording(record: SessionRecord, mimeType?: string): SessionRecord {
  return { ...record, state: "recording", mimeType: mimeType ?? record.mimeType };
}

/**
 * Open a paused interval.
 *
 * A pause requested while already paused returns the record untouched rather
 * than nesting a second interval: the allow-list can fire the same signal twice
 * across a redirect chain, and double-counting the gap would desynchronize
 * every event timestamp after it.
 */
export function markPaused(record: SessionRecord, atEpochMs: number): SessionRecord {
  if (record.state !== "recording") return record;
  return {
    ...record,
    state: "paused",
    pausedIntervals: [
      ...record.pausedIntervals,
      { fromOffsetMs: offsetOf(record, atEpochMs), toOffsetMs: null },
    ],
  };
}

/** Close the open paused interval. Symmetrically to {@link markPaused}, a
 * resume that was not paused changes nothing. */
export function markResumed(record: SessionRecord, atEpochMs: number): SessionRecord {
  if (record.state !== "paused") return record;
  const closed = offsetOf(record, atEpochMs);
  return {
    ...record,
    state: "recording",
    pausedIntervals: record.pausedIntervals.map((interval) =>
      interval.toOffsetMs === null ? { ...interval, toOffsetMs: closed } : interval,
    ),
  };
}

export function markStopping(record: SessionRecord): SessionRecord {
  return { ...record, state: "stopping" };
}

/** Record why capture ended badly. The reason is kept because "your recording
 * failed" without it leaves the user nothing to act on. */
export function markFailed(record: SessionRecord, error: string): SessionRecord {
  return { ...record, state: "failed", error };
}

function offsetOf(record: SessionRecord, epochMs: number): number {
  return Math.max(0, epochMs - record.startedAtEpochMs);
}

export interface SessionStorage {
  get(key: string): Promise<string | null>;
  set(key: string, value: string): Promise<void>;
  remove(key: string): Promise<void>;
}

export class SessionStore {
  constructor(private readonly storage: SessionStorage) {}

  /**
   * The stored record, or null when there is none.
   *
   * An unreadable or malformed cell reads as null rather than throwing. The
   * record authorizes nothing — {@link reconcile} still needs a live document
   * to call anything running — so the cost of a bad read is a lost indicator,
   * not a lost guarantee. `Allowlist.readStored` takes the same position for
   * the same reason.
   */
  async read(): Promise<SessionRecord | null> {
    let raw: string | null;
    try {
      raw = await this.storage.get(SESSION_STORAGE_KEY);
    } catch {
      return null;
    }
    if (raw === null) return null;
    try {
      const parsed: unknown = JSON.parse(raw);
      return isSessionRecord(parsed) ? parsed : null;
    } catch {
      return null;
    }
  }

  async write(record: SessionRecord): Promise<void> {
    await this.storage.set(SESSION_STORAGE_KEY, JSON.stringify(record));
  }

  async clear(): Promise<void> {
    await this.storage.remove(SESSION_STORAGE_KEY);
  }
}

/** Structural check on the fields {@link reconcile} and the indicator actually
 * read. A record written by an older build with a since-removed shape should
 * read as "no session" rather than crash the worker on revival. */
function isSessionRecord(value: unknown): value is SessionRecord {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Partial<SessionRecord>;
  return (
    typeof candidate.id === "string" &&
    typeof candidate.startedAtEpochMs === "number" &&
    Array.isArray(candidate.pausedIntervals) &&
    typeof candidate.state === "string" &&
    typeof candidate.source === "object" &&
    candidate.source !== null &&
    typeof candidate.source.label === "string"
  );
}

/** Adapter to the real Chrome API. Every other test injects a fake, so this
 * mapping is covered by `chrome-deps.test.ts`. */
export function chromeSessionStorage(): SessionStorage {
  return {
    get: async (key) => (await chrome.storage.local.get(key))[key] ?? null,
    set: async (key, value) => chrome.storage.local.set({ [key]: value }),
    remove: async (key) => chrome.storage.local.remove(key),
  };
}
