/**
 * What the allow-list panel shows, as a pure function of its inputs.
 *
 * Kept separate from the DOM wiring in `popup.ts` for the same reason
 * `view.ts` is: the decisions — whether the panel is visible, how an empty list
 * reads, what an add attempt has to say for itself — are testable without a
 * browser, leaving only mechanical element assignment untested.
 */

import type {
  AddResult,
  AllowlistEntry,
  AllowlistState,
} from "../permissions/allowlist.js";

export interface AllowlistViewInput {
  signedIn: boolean;
  entries: AllowlistEntry[];
  state?: AllowlistState;
  error?: string | null;
  notice?: string | null;
}

export interface AllowlistView {
  /** An allow-list is meaningless without an account to record under. */
  visible: boolean;
  entries: AllowlistEntry[];
  /** Distinct from `entries.length === 0` at the call site so the empty copy is
   * decided here rather than in the DOM layer. */
  empty: boolean;
  /** Shown in place of the list when there is nothing on it **and** the list can
   * be trusted. Says what the consequence is, not merely that the list is empty
   * — and reads as an ordinary starting state, because a fresh install having no
   * allow-listed origins is the safe default rather than something that went
   * wrong. `null` whenever {@link warning} is set: claiming nothing can be
   * recorded is exactly what must not be said in those states. */
  emptyMessage: string | null;
  /** Set when the list is not the whole truth — Chrome's grants could not be
   * read, or an all-sites grant is live. Never suppressed by the presence of
   * entries, because both states are about what is recordable *beyond* them. */
  warning: string | null;
  error: string | null;
  /** Something the user should know about an action that nonetheless
   * succeeded — currently only a grant that came out wider than requested. */
  notice: string | null;
}

export const EMPTY_MESSAGE =
  "No sites allowed yet. Nothing can be recorded until you add one.";

export const UNREADABLE_MESSAGE =
  "Screencap couldn't read Chrome's site permissions, so this list may be out of date.";

export const BROAD_GRANT_MESSAGE =
  "Chrome is currently granting Screencap access to every site, so this list is not what limits recording. Remove all-site access from Chrome's extension settings to enforce it.";

const WARNINGS: Record<AllowlistState, string | null> = {
  ok: null,
  "grants-unreadable": UNREADABLE_MESSAGE,
  "broad-grant": BROAD_GRANT_MESSAGE,
};

export function allowlistView({
  signedIn,
  entries,
  state = "ok",
  error = null,
  notice = null,
}: AllowlistViewInput): AllowlistView {
  const warning = WARNINGS[state];
  return {
    visible: signedIn,
    entries,
    empty: entries.length === 0,
    // Suppressed whenever a warning is showing. "Nothing can be recorded" is
    // true only when the empty list is also the whole truth.
    emptyMessage: entries.length === 0 && warning === null ? EMPTY_MESSAGE : null,
    warning,
    error,
    notice,
  };
}

/**
 * Turn the outcome of an add into what the user should read.
 *
 * A declined prompt is not an error the user needs explaining — they declined
 * it on purpose — so it states what followed rather than scolding. A dropped
 * port is the one case where a *successful* add did something wider than what
 * was typed, and saying so is the whole reason `portDropped` is carried out of
 * the store.
 */
export function addOutcome(
  result: AddResult,
  rawInput: string,
): { error: string | null; notice: string | null } {
  if (!result.ok) {
    return {
      error:
        result.reason === "invalid"
          ? `"${rawInput.trim()}" isn't a site address Screencap can allow.`
          : "Chrome didn't grant access, so nothing was added.",
      notice: null,
    };
  }
  if (result.portDropped !== null) {
    return {
      error: null,
      notice: `Chrome site permissions don't cover ports, so this allows every port on ${result.entry.label}.`,
    };
  }
  return { error: null, notice: null };
}
