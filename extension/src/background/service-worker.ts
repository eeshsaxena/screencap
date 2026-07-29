/**
 * The extension's single auth owner.
 *
 * The popup is short-lived and the service worker is suspended when idle, so
 * neither can hold the session by itself. Keeping `AuthSession` here means the
 * popup never talks to Firebase directly, and a suspended-then-revived worker
 * restores itself from the stored refresh token — the ID token cache is
 * expected to be lost, not preserved.
 */

import { AuthSession, chromeAuthDeps, type WhoAmI } from "../auth/firebase.js";

const session = new AuthSession(chromeAuthDeps());

export type AuthRequest =
  | { type: "auth.whoami" }
  | { type: "auth.signIn" }
  | { type: "auth.signOut" };

export type AuthResponse =
  | { ok: true; who: WhoAmI }
  | { ok: false; error: string };

async function handle(request: AuthRequest): Promise<AuthResponse> {
  switch (request.type) {
    case "auth.whoami":
      return { ok: true, who: await session.whoami() };
    case "auth.signIn":
      await session.signIn();
      return { ok: true, who: await session.whoami() };
    case "auth.signOut":
      await session.signOut();
      return { ok: true, who: await session.whoami() };
  }
}

chrome.runtime.onMessage.addListener(
  (request: AuthRequest, _sender, sendResponse: (r: AuthResponse) => void) => {
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
