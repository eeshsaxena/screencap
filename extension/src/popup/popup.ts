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

import type {
  CaptureMessage,
  CaptureMessageResponse,
  CaptureSourceRequest,
} from "../background/capture-controller.js";
import type { AuthRequest, AuthResponse } from "../background/service-worker.js";
import {
  Allowlist,
  chromeAllowlistDeps,
  type AllowlistEntry,
} from "../permissions/allowlist.js";
import { addOutcome, allowlistView, type AllowlistView } from "./allowlist-view.js";
import { captureView, type CaptureView } from "./capture-view.js";
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

/**
 * The auth state the last render computed.
 *
 * Add and remove finish asynchronously and then repaint. Hardcoding `true` for
 * that repaint meant a sign-out landing mid-flight was undone: the stale
 * refresh resolved afterwards and re-showed a live allow-list panel underneath
 * the signed-out UI.
 */
let lastSignedIn = false;

/**
 * Guards against an older refresh painting over a newer one.
 *
 * Refreshes are started from several places and are not awaited by their
 * callers, so a slow one can resolve after a fast one that superseded it.
 */
let refreshGeneration = 0;

function render(view: PopupView): void {
  const signedIn = view.status === "signed-in" || view.status === "stale";
  lastSignedIn = signedIn;

  el("signed-in").hidden = !signedIn;
  el("signed-out").hidden = view.status !== "signed-out";
  el("unknown").hidden = view.status !== "unknown";
  el("account").textContent = view.accountLabel ?? "";

  el("stale-note").hidden = view.status !== "stale";
  setText("error", view.error);

  void refreshAllowlist(signedIn);
  void refreshCapture(signedIn);
}

/**
 * The tab the popup was opened over, captured at load.
 *
 * Read once, up front, because the capture handler cannot afford to await:
 * `chrome.tabCapture.getMediaStreamId()` must run inside the user gesture, and
 * a `tabs.query()` first would spend it — the same constraint {@link addOrigin}
 * documents for permission requests.
 */
let activeTab: { id: number; title: string } | null = null;

/** A capture verb is in flight. Both buttons withhold so a second click cannot
 * race the first into a refused start. */
let captureBusy = false;

async function sendCapture(message: CaptureMessage): Promise<CaptureMessageResponse> {
  return chrome.runtime.sendMessage<CaptureMessage, CaptureMessageResponse>(message);
}

async function refreshCapture(
  signedIn: boolean,
  error: string | null = null,
): Promise<void> {
  if (!signedIn) {
    renderCapture(
      captureView({
        signedIn: false,
        status: { kind: "idle", record: null, error: null },
      }),
    );
    return;
  }

  try {
    const response = await sendCapture({ type: "capture.status" });
    renderCapture(
      captureView({
        signedIn: true,
        status:
          "status" in response && response.ok
            ? response.status
            : { kind: "idle", record: null, error: null },
        busy: captureBusy,
        error,
      }),
    );
  } catch (caught) {
    renderCapture(
      captureView({
        signedIn: true,
        status: { kind: "idle", record: null, error: null },
        busy: captureBusy,
        error: error ?? asError(caught),
      }),
    );
  }
}

function renderCapture(view: CaptureView): void {
  el("capture").hidden = !view.visible;
  el("capture-status").textContent = view.message;
  setText("capture-error", view.error);

  captureTab.hidden = !view.canStart;
  captureScreen.hidden = !view.canStart;
  captureStop.hidden = !view.canStop;
  captureTab.disabled = !view.canStart;
  captureScreen.disabled = !view.canStart;
  captureStop.disabled = !view.canStop;
}

/** Runs a capture verb and repaints from whatever state it left behind. */
async function runCapture(
  start: () => Promise<CaptureMessageResponse>,
): Promise<void> {
  captureBusy = true;
  try {
    const response = await start();
    await refreshCapture(lastSignedIn, captureProblem(response));
  } catch (error) {
    await refreshCapture(lastSignedIn, asError(error));
  } finally {
    captureBusy = false;
    await refreshCapture(lastSignedIn);
  }
}

/** Turn a refused or failed verb into something worth reading. A dismissed
 * picker is not one: the user closed it deliberately. */
function captureProblem(response: CaptureMessageResponse): string | null {
  if (!response.ok) return response.error;
  if ("start" in response && !response.start.ok) {
    if (response.start.reason === "already-recording") {
      return `Already recording ${response.start.runningSource.label}.`;
    }
    return response.start.reason === "cancelled" ? null : response.start.error;
  }
  if ("stop" in response && !response.stop.ok) {
    return response.stop.reason === "not-recording"
      ? "There was no recording to stop."
      : response.stop.error;
  }
  return null;
}

/**
 * Start recording the tab the popup was opened over.
 *
 * `getMediaStreamId` is called **before this handler awaits anything**. Chrome
 * requires it inside the user gesture, and it cannot be delegated to the
 * service worker: the popup is the extension page holding the gesture, and this
 * extension uses a popup, so `action.onClicked` never fires in the worker at
 * all.
 */
function startTabRecording(): void {
  if (captureBusy || activeTab === null) return;
  const { id, title } = activeTab;

  const streamId = tabStreamId(id);
  void runCapture(async () =>
    sendCapture({
      type: "capture.start",
      source: { kind: "tab", streamId: await streamId, label: title },
    }),
  );
}

/**
 * Promise-wrap the callback form of `getMediaStreamId`.
 *
 * The wrapper matters for more than ergonomics: a Promise executor runs
 * synchronously, so Chrome's API is still invoked inside the gesture. Checking
 * `lastError` is what turns a refusal — no `activeTab` grant, a tab that cannot
 * be captured — into a message instead of a silently unresolved promise.
 */
function tabStreamId(targetTabId: number): Promise<string> {
  return new Promise((resolve, reject) => {
    chrome.tabCapture.getMediaStreamId({ targetTabId }, (id) => {
      const failure = chrome.runtime.lastError;
      if (failure) reject(new Error(failure.message ?? "Chrome would not capture this tab."));
      else resolve(id);
    });
  });
}

/** The display picker opens inside the offscreen document, so no gesture is
 * spent here — unlike the tab path above. */
function startScreenRecording(): void {
  if (captureBusy) return;
  const source: CaptureSourceRequest = { kind: "screen" };
  void runCapture(() => sendCapture({ type: "capture.start", source }));
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
  const generation = ++refreshGeneration;
  const listing = signedIn
    ? await allowlist.list()
    : { entries: [], state: "ok" as const };

  // A newer refresh started while this one was awaiting; its paint is the
  // current one and must not be overwritten by this stale result.
  if (generation !== refreshGeneration) return;

  renderAllowlist(
    allowlistView({
      signedIn,
      entries: listing.entries,
      state: listing.state,
      error: message.error ?? null,
      notice: message.notice ?? null,
    }),
  );
}

function renderAllowlist(view: AllowlistView): void {
  el("allowlist").hidden = !view.visible;
  // `emptyMessage` is already null whenever a warning is showing, so the panel
  // cannot say "nothing can be recorded" beside a warning that says otherwise.
  setText("allowlist-empty", view.emptyMessage);
  setText("allowlist-warning", view.warning);
  setText("allowlist-notice", view.notice);
  setText("allowlist-error", view.error);

  el<HTMLUListElement>("allowlist-entries").replaceChildren(...view.entries.map(rowFor));
}

function rowFor(entry: AllowlistEntry): HTMLLIElement {
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
    await refreshAllowlist(lastSignedIn);
  } catch (error) {
    // The entry stays when the revoke did not take — a list that still shows a
    // live grant is honest, one that hides it is not.
    await refreshAllowlist(lastSignedIn, { error: asError(error) });
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
  // The Enter handler calls this directly, so it does not go through the
  // button and the browser's own disabled-button suppression never applies.
  // Without this check, click-then-Enter (or held-Enter auto-repeat) starts a
  // second permissions.request() for the same origin while the first prompt is
  // still open.
  if (addButton.disabled) return;

  const raw = addInput.value;
  if (raw.trim() === "") return;

  addButton.disabled = true;
  const pending = allowlist.add(raw);

  void pending
    .then(async (result) => {
      if (result.ok) addInput.value = "";
      await refreshAllowlist(lastSignedIn, addOutcome(result, raw));
    })
    .catch(async (error: unknown) => {
      await refreshAllowlist(lastSignedIn, { error: asError(error) });
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
const captureTab = el<HTMLButtonElement>("capture-tab");
const captureScreen = el<HTMLButtonElement>("capture-screen");
const captureStop = el<HTMLButtonElement>("capture-stop");

captureTab.addEventListener("click", startTabRecording);
captureScreen.addEventListener("click", startScreenRecording);
captureStop.addEventListener("click", () => {
  if (captureBusy) return;
  void runCapture(() => sendCapture({ type: "capture.stop" }));
});

// Resolved before any click can arrive, so the tab handler never has to await.
// The title needs activeTab, which Chrome grants for the duration of this
// popup being open.
void chrome.tabs
  .query({ active: true, currentWindow: true })
  .then(([tab]) => {
    if (tab?.id !== undefined) {
      activeTab = { id: tab.id, title: tab.title?.trim() || "this tab" };
    }
  })
  .catch(() => {
    // Leaves `activeTab` null, which disables tab capture rather than starting
    // a recording of a tab we cannot name.
  });

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
