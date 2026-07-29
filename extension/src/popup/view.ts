/**
 * What the popup shows, as a pure function of auth state.
 *
 * Kept separate from the DOM wiring in `popup.ts` so the decision — which
 * section is visible and how the account reads — is testable without a browser,
 * leaving only mechanical element assignment untested.
 */

import type { WhoAmI } from "../auth/firebase.js";

export interface PopupView {
  showSignedIn: boolean;
  /** Identity to display; `null` when signed out. Falls back to the account id
   * when the token carries no email, so the popup never renders a blank name
   * for a session that is genuinely signed in. */
  accountLabel: string | null;
  error: string | null;
}

export function popupView(who: WhoAmI, error: string | null = null): PopupView {
  if (!who.signedIn) {
    return { showSignedIn: false, accountLabel: null, error };
  }
  return {
    showSignedIn: true,
    accountLabel: who.email ?? who.uid ?? "your account",
    error,
  };
}
