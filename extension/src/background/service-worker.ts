/**
 * The extension's single auth owner.
 *
 * The popup is short-lived and the service worker is suspended when idle, so
 * neither can hold the session by itself. Keeping `AuthSession` here means the
 * popup never talks to Firebase directly, and a suspended-then-revived worker
 * restores itself from the stored refresh token — the ID token cache is
 * expected to be lost, not preserved.
 */

import {
  AuthSession,
  chromeAuthDeps,
  SIGNED_OUT,
  toWhoAmI,
  type WhoAmI,
} from "../auth/firebase.js";
import {
  CaptureController,
  chromeCaptureDeps,
  registerCaptureListener,
} from "./capture-controller.js";
import { applyBadge, badgeFor, chromeBadgeDeps } from "./indicator.js";
import { isExtensionPageSender as isTrustedSender } from "./senders.js";

const session = new AuthSession(chromeAuthDeps());

export const AUTH_REQUEST_TYPES = [
  "auth.whoami",
  "auth.signIn",
  "auth.signOut",
] as const;

export type AuthRequestType = (typeof AUTH_REQUEST_TYPES)[number];
export type AuthRequest = { type: AuthRequestType };

export type AuthResponse =
  | { ok: true; who: WhoAmI }
  | { ok: false; error: string };

/**
 * Whether this listener owns the message.
 *
 * `onMessage` is typed `any`, and later units add their own listeners on the
 * same channel, so the discriminant is checked at runtime rather than trusted
 * from the cast. Anything unrecognized is declined so another listener can
 * answer it.
 */
export function isAuthRequest(request: unknown): request is AuthRequest {
  return (
    typeof request === "object" &&
    request !== null &&
    AUTH_REQUEST_TYPES.includes((request as { type?: unknown }).type as AuthRequestType)
  );
}

/**
 * Whether the sender may drive auth. The offscreen document applies the same
 * rule to capture, so the predicate itself lives in `./senders.ts`.
 */
export { isTrustedSender };

export async function handle(request: AuthRequest): Promise<AuthResponse> {
  switch (request.type) {
    case "auth.whoami":
      return { ok: true, who: await session.whoami() };
    case "auth.signIn":
      return { ok: true, who: toWhoAmI(await session.signIn()) };
    case "auth.signOut":
      // Stop capture before dropping the session. The recording panel is gated
      // on being signed in, so signing out mid-recording would take away the
      // only Stop control while the offscreen document kept recording.
      await capture.stop().catch(() => undefined);
      await session.signOut();
      return { ok: true, who: SIGNED_OUT };
  }
}

chrome.runtime.onMessage.addListener(
  (request: unknown, sender, sendResponse: (r: AuthResponse) => void) => {
    if (!isAuthRequest(request)) return false;
    if (!isTrustedSender(sender)) {
      sendResponse({ ok: false, error: "Unauthorized sender" });
      return false;
    }
    handle(request)
      .then(sendResponse)
      .catch((error: unknown) => {
        sendResponse({
          ok: false,
          error: error instanceof Error ? error.message : String(error),
        });
      });
    // Keeps the message channel open for the async response above.
    return true;
  },
);

// Registered after the auth listener, not instead of it: both see every message
// and decline what is not theirs.
const capture = new CaptureController(chromeCaptureDeps());

/**
 * Repaint the badge from the reconciled session.
 *
 * Called at module load as well as after each verb, because module load *is*
 * worker startup in MV3: a revived worker inherits whatever badge the browser
 * kept, which may describe a recording that has since ended.
 */
async function syncBadge(): Promise<void> {
  try {
    await applyBadge(chromeBadgeDeps(), badgeFor(await capture.status()));
  } catch {
    // A worker that cannot read its own state should still come up; the badge
    // is an indicator, and the popup asks for status directly anyway.
  }
}

registerCaptureListener(capture, () => void syncBadge());
void syncBadge();
