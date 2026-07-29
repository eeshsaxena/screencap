/**
 * What the popup shows, as a pure function of auth state.
 *
 * Kept separate from the DOM wiring in `popup.ts` so the decision — which
 * section is visible and how the account reads — is testable without a browser,
 * leaving only mechanical element assignment untested.
 */

import type { WhoAmI } from "../auth/firebase.js";

/**
 * Four states, not two. Collapsing `stale` or `unknown` into `signed-out`
 * would tell someone who is merely offline, or whose service worker is
 * unreachable, that they have no account — and offer them a sign-in they do
 * not need.
 */
export type PopupStatus = "signed-out" | "signed-in" | "stale" | "unknown";

export interface PopupView {
  status: PopupStatus;
  /** Identity to display; `null` unless signed in. Falls back to the account id
   * when the token carries no email, so the popup never renders a blank name
   * for a session that is genuinely signed in. */
  accountLabel: string | null;
  error: string | null;
}

export function popupView(who: WhoAmI, error: string | null = null): PopupView {
  if (!who.signedIn) {
    return { status: "signed-out", accountLabel: null, error };
  }
  return {
    status: who.stale === true ? "stale" : "signed-in",
    accountLabel: who.email ?? who.uid ?? "your account",
    error,
  };
}

/** The extension's own messaging failed, so auth state is unknown — distinct
 * from knowing the user is signed out. */
export function unreachableView(error: string): PopupView {
  return { status: "unknown", accountLabel: null, error };
}
