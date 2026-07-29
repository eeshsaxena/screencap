/**
 * DOM wiring for the popup. All display decisions live in `popupView`; this
 * file only moves that result onto elements and forwards clicks to the service
 * worker, which owns the session.
 */

import type { AuthRequest, AuthResponse } from "../background/service-worker.js";
import { popupView, unreachableView, type PopupView } from "./view.js";

function el<T extends HTMLElement>(id: string): T {
  const found = document.getElementById(id);
  if (!found) throw new Error(`Missing element: ${id}`);
  return found as T;
}

async function send(request: AuthRequest): Promise<AuthResponse> {
  return chrome.runtime.sendMessage<AuthRequest, AuthResponse>(request);
}

function asError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function fromResponse(response: AuthResponse): PopupView {
  return response.ok
    ? popupView(response.who)
    : popupView({ signedIn: false }, response.error);
}

function render(view: PopupView): void {
  const signedIn = view.status === "signed-in" || view.status === "stale";

  el("signed-in").hidden = !signedIn;
  el("signed-out").hidden = view.status !== "signed-out";
  el("unknown").hidden = view.status !== "unknown";
  el("account").textContent = view.accountLabel ?? "";

  const staleNote = el("stale-note");
  staleNote.hidden = view.status !== "stale";

  const error = el("error");
  error.textContent = view.error ?? "";
  error.hidden = view.error === null;
}

/**
 * Disables the button for the duration so a double-click cannot start two
 * interactive sign-in flows.
 *
 * A transport failure renders as `unknown`, not signed-out — if the service
 * worker is unreachable the popup does not know the auth state, and showing a
 * sign-in call to action would be a guess.
 */
async function run(button: HTMLButtonElement, request: AuthRequest): Promise<void> {
  button.disabled = true;
  try {
    render(fromResponse(await send(request)));
  } catch (error) {
    render(unreachableView(asError(error)));
  } finally {
    button.disabled = false;
  }
}

const signIn = el<HTMLButtonElement>("sign-in");
const signOut = el<HTMLButtonElement>("sign-out");
const retry = el<HTMLButtonElement>("retry");

signIn.addEventListener("click", () => void run(signIn, { type: "auth.signIn" }));
signOut.addEventListener("click", () => void run(signOut, { type: "auth.signOut" }));
retry.addEventListener("click", () => void run(retry, { type: "auth.whoami" }));

void send({ type: "auth.whoami" })
  .then((response) => render(fromResponse(response)))
  .catch((error: unknown) => render(unreachableView(asError(error))));
