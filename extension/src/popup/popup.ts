/**
 * DOM wiring for the popup. All display decisions live in `popupView` and
 * `allowlistView`; this file only moves those results onto elements and
 * forwards clicks.
 *
 * Note the split in who owns what. The session lives in the service worker, so
 * the auth buttons message it. The allow-list is driven from **here** instead:
 * `chrome.permissions.request()` must run inside a user gesture on an extension
 * page, and forwarding it to the service worker over `sendMessage` spends the
 * gesture and throws. See {@link addOrigin}.
 */

import type { AuthRequest, AuthResponse } from "../background/service-worker.js";
import { Allowlist, chromeAllowlistDeps } from "../permissions/allowlist.js";
import { addOutcome, allowlistView, type AllowlistView } from "./allowlist-view.js";
import { popupView, unreachableView, type PopupView } from "./view.js";

const allowlist = new Allowlist(chromeAllowlistDeps());

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

/** Assign text and hide the node when there is nothing to say. */
function setText(id: string, value: string | null): void {
  const node = el(id);
  node.textContent = value ?? "";
  node.hidden = value === null;
}

function render(view: PopupView): void {
  const signedIn = view.status === "signed-in" || view.status === "stale";

  el("signed-in").hidden = !signedIn;
  el("signed-out").hidden = view.status !== "signed-out";
  el("unknown").hidden = view.status !== "unknown";
  el("account").textContent = view.accountLabel ?? "";

  el("stale-note").hidden = view.status !== "stale";
  setText("error", view.error);

  void refreshAllowlist(signedIn);
}

/**
 * Re-read the allow-list and paint it.
 *
 * Every read reconciles against Chrome's grant state, so this is also what
 * makes a revoke performed on `chrome://extensions` show up — no extension
 * reload, and no permission event to subscribe to (Chrome does not fire one for
 * that case).
 */
async function refreshAllowlist(
  signedIn: boolean,
  message: { error?: string | null; notice?: string | null } = {},
): Promise<void> {
  const entries = signedIn ? await allowlist.list() : [];
  renderAllowlist(
    allowlistView({
      signedIn,
      entries,
      error: message.error ?? null,
      notice: message.notice ?? null,
    }),
  );
}

function renderAllowlist(view: AllowlistView): void {
  el("allowlist").hidden = !view.visible;
  setText("allowlist-empty", view.empty ? view.emptyMessage : null);
  setText("allowlist-notice", view.notice);
  setText("allowlist-error", view.error);

  el<HTMLUListElement>("allowlist-entries").replaceChildren(...view.entries.map(rowFor));
}

function rowFor(entry: { pattern: string; label: string }): HTMLLIElement {
  const row = document.createElement("li");

  const origin = document.createElement("span");
  origin.className = "origin";
  // textContent, never innerHTML — a granted pattern is data, not markup.
  origin.textContent = entry.label;

  const revoke = document.createElement("button");
  revoke.type = "button";
  revoke.textContent = "Remove";
  revoke.setAttribute("aria-label", `Remove ${entry.label}`);
  revoke.addEventListener("click", () => void removeOrigin(revoke, entry.pattern));

  row.append(origin, revoke);
  return row;
}

async function removeOrigin(button: HTMLButtonElement, pattern: string): Promise<void> {
  button.disabled = true;
  try {
    await allowlist.remove(pattern);
    await refreshAllowlist(true);
  } catch (error) {
    // The entry stays when the revoke did not take — a list that still shows a
    // live grant is honest, one that hides it is not.
    await refreshAllowlist(true, { error: asError(error) });
  } finally {
    button.disabled = false;
  }
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

/**
 * Add the typed origin to the allow-list.
 *
 * `allowlist.add()` is called **before this handler awaits anything**, and it
 * reaches `chrome.permissions.request()` before its own first await. Chrome
 * requires that call to happen inside the user gesture; awaiting first — a list
 * refresh, a message round-trip — spends the gesture and the request fails with
 * "This function must be called during a user gesture". This is why the panel
 * does not follow the service-worker delegation the auth buttons above use.
 */
function addOrigin(): void {
  const raw = addInput.value;
  if (raw.trim() === "") return;

  addButton.disabled = true;
  const pending = allowlist.add(raw);

  void pending
    .then(async (result) => {
      if (result.ok) addInput.value = "";
      await refreshAllowlist(true, addOutcome(result, raw));
    })
    .catch(async (error: unknown) => {
      await refreshAllowlist(true, { error: asError(error) });
    })
    .finally(() => {
      addButton.disabled = false;
    });
}

const signIn = el<HTMLButtonElement>("sign-in");
const signOut = el<HTMLButtonElement>("sign-out");
const retry = el<HTMLButtonElement>("retry");
const addButton = el<HTMLButtonElement>("allowlist-add");
const addInput = el<HTMLInputElement>("allowlist-input");

signIn.addEventListener("click", () => void run(signIn, { type: "auth.signIn" }));
signOut.addEventListener("click", () => void run(signOut, { type: "auth.signOut" }));
retry.addEventListener("click", () => void run(retry, { type: "auth.whoami" }));

addButton.addEventListener("click", addOrigin);
addInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") addOrigin();
});

void send({ type: "auth.whoami" })
  .then((response) => render(fromResponse(response)))
  .catch((error: unknown) => render(unreachableView(asError(error))));
