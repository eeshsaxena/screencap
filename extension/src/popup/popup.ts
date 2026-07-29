/**
 * DOM wiring for the popup. All display decisions live in `popupView`; this
 * file only moves that result onto elements and forwards clicks to the service
 * worker, which owns the session.
 */

import type { AuthRequest, AuthResponse } from "../background/service-worker.js";
import { popupView } from "./view.js";

function el<T extends HTMLElement>(id: string): T {
  const found = document.getElementById(id);
  if (!found) throw new Error(`Missing element: ${id}`);
  return found as T;
}

async function send(request: AuthRequest): Promise<AuthResponse> {
  return chrome.runtime.sendMessage<AuthRequest, AuthResponse>(request);
}

function render(response: AuthResponse): void {
  const view = response.ok
    ? popupView(response.who)
    : popupView({ signedIn: false }, response.error);

  el("signed-in").hidden = !view.showSignedIn;
  el("signed-out").hidden = view.showSignedIn;
  el("account").textContent = view.accountLabel ?? "";

  const error = el("error");
  error.textContent = view.error ?? "";
  error.hidden = view.error === null;
}

function asError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * Disables the button for the duration so a double-click cannot start two
 * interactive sign-in flows.
 *
 * Catches transport failures too — if the service worker is gone the
 * `sendMessage` promise rejects rather than returning `{ ok: false }`, and an
 * uncaught rejection would leave the popup showing no error at all.
 */
async function run(button: HTMLButtonElement, request: AuthRequest): Promise<void> {
  button.disabled = true;
  try {
    render(await send(request));
  } catch (error) {
    render({ ok: false, error: asError(error) });
  } finally {
    button.disabled = false;
  }
}

const signIn = el<HTMLButtonElement>("sign-in");
const signOut = el<HTMLButtonElement>("sign-out");

signIn.addEventListener("click", () => void run(signIn, { type: "auth.signIn" }));
signOut.addEventListener("click", () => void run(signOut, { type: "auth.signOut" }));

void send({ type: "auth.whoami" })
  .then(render)
  .catch((error: unknown) => render({ ok: false, error: asError(error) }));
