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
 * Whether the sender may drive auth.
 *
 * Only extension pages qualify — a `tab` on the sender means a content script,
 * which from U4 onward runs on allow-listed origins and must not be able to
 * start or end a session.
 */
export function isTrustedSender(sender: chrome.runtime.MessageSender): boolean {
  return sender.id === chrome.runtime.id && sender.tab === undefined;
}

export async function handle(request: AuthRequest): Promise<AuthResponse> {
  switch (request.type) {
    case "auth.whoami":
      return { ok: true, who: await session.whoami() };
    case "auth.signIn":
      return { ok: true, who: toWhoAmI(await session.signIn()) };
    case "auth.signOut":
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
