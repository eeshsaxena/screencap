import { beforeAll, describe, expect, it } from "vitest";

type Listener = (
  request: unknown,
  sender: chrome.runtime.MessageSender,
  sendResponse: (r: unknown) => void,
) => boolean;

const EXTENSION_ID = "test-extension-id";

let listener: Listener;
let isAuthRequest: (r: unknown) => boolean;
let isTrustedSender: (s: chrome.runtime.MessageSender) => boolean;

beforeAll(async () => {
  // The module registers its listener and builds its Chrome-backed deps at
  // import time, so the global has to exist first.
  const registered: Listener[] = [];
  (globalThis as unknown as { chrome: unknown }).chrome = {
    runtime: {
      id: EXTENSION_ID,
      onMessage: { addListener: (fn: Listener) => registered.push(fn) },
    },
    storage: { local: { get: async () => ({}), set: async () => {}, remove: async () => {} } },
    identity: { launchWebAuthFlow: async () => "", getRedirectURL: () => "" },
    // The worker paints the recording badge from its reconciled state at
    // startup, so importing it now touches the action and offscreen surfaces.
    action: {
      setBadgeText: async () => {},
      setBadgeBackgroundColor: async () => {},
      setTitle: async () => {},
    },
    offscreen: {
      Reason: { USER_MEDIA: "USER_MEDIA", DISPLAY_MEDIA: "DISPLAY_MEDIA" },
      closeDocument: async () => {},
    },
  };
  (
    globalThis as unknown as { chrome: { runtime: Record<string, unknown> } }
  ).chrome.runtime.getContexts = async () => [];
  (
    globalThis as unknown as { chrome: { runtime: Record<string, unknown> } }
  ).chrome.runtime.ContextType = { OFFSCREEN_DOCUMENT: "OFFSCREEN_DOCUMENT" };

  const mod = await import("./service-worker.js");
  isAuthRequest = mod.isAuthRequest;
  isTrustedSender = mod.isTrustedSender;
  listener = registered[0]!;
});

describe("isAuthRequest", () => {
  it("accepts the three auth message types", () => {
    for (const type of ["auth.whoami", "auth.signIn", "auth.signOut"]) {
      expect(isAuthRequest({ type })).toBe(true);
    }
  });

  it("declines anything else, including messages later units will add", () => {
    for (const request of [
      { type: "capture.start" },
      { type: "" },
      {},
      null,
      undefined,
      "auth.whoami",
      42,
    ]) {
      expect(isAuthRequest(request)).toBe(false);
    }
  });
});

describe("isTrustedSender", () => {
  it("accepts an extension page", () => {
    expect(isTrustedSender({ id: EXTENSION_ID } as chrome.runtime.MessageSender)).toBe(
      true,
    );
  });

  it("rejects a content script", () => {
    // From U4 a content script runs on allow-listed origins; it must not be
    // able to start or end a session.
    const sender = {
      id: EXTENSION_ID,
      tab: { id: 7 },
    } as chrome.runtime.MessageSender;

    expect(isTrustedSender(sender)).toBe(false);
  });

  it("rejects another extension", () => {
    expect(
      isTrustedSender({ id: "some-other-extension" } as chrome.runtime.MessageSender),
    ).toBe(false);
  });
});

describe("onMessage listener", () => {
  const page = { id: EXTENSION_ID } as chrome.runtime.MessageSender;

  it("declines an unrecognized message so another listener can answer it", () => {
    let answered = false;
    const kept = listener({ type: "capture.start" }, page, () => {
      answered = true;
    });

    expect(kept).toBe(false);
    expect(answered).toBe(false);
  });

  it("refuses an auth message from a content script", () => {
    let response: unknown;
    const kept = listener({ type: "auth.signOut" }, {
      id: EXTENSION_ID,
      tab: { id: 7 },
    } as chrome.runtime.MessageSender, (r) => {
      response = r;
    });

    expect(kept).toBe(false);
    expect(response).toMatchObject({ ok: false, error: "Unauthorized sender" });
  });

  it("keeps the channel open for a recognized message from an extension page", () => {
    expect(listener({ type: "auth.whoami" }, page, () => {})).toBe(true);
  });
});
