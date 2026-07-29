/**
 * Covers `chromeAuthDeps()` — the adapter between `AuthSession` and the real
 * Chrome APIs. Every other auth test injects fakes, so without this the
 * mapping from Chrome's shapes to `AuthDeps` would be the one part of the
 * module nothing exercises.
 */
import { beforeEach, describe, expect, it } from "vitest";
import { chromeAuthDeps } from "./firebase.js";

interface ChromeStub {
  storageArea: Map<string, string>;
  lastAuthFlowArgs: { url: string; interactive: boolean } | null;
  authFlowResult: string | undefined;
}

let stub: ChromeStub;

beforeEach(() => {
  stub = {
    storageArea: new Map(),
    lastAuthFlowArgs: null,
    authFlowResult: "https://abc.chromiumapp.org/#id_token=x",
  };

  (globalThis as unknown as { chrome: unknown }).chrome = {
    runtime: { id: "test-extension-id" },
    storage: {
      local: {
        get: async (key: string) =>
          stub.storageArea.has(key) ? { [key]: stub.storageArea.get(key) } : {},
        set: async (items: Record<string, string>) => {
          for (const [k, v] of Object.entries(items)) stub.storageArea.set(k, v);
        },
        remove: async (key: string) => void stub.storageArea.delete(key),
      },
    },
    identity: {
      launchWebAuthFlow: async (args: { url: string; interactive: boolean }) => {
        stub.lastAuthFlowArgs = args;
        return stub.authFlowResult;
      },
      getRedirectURL: () => "https://abc.chromiumapp.org/",
    },
  };
});

describe("chromeAuthDeps storage", () => {
  it("round-trips a value through chrome.storage.local", async () => {
    const { storage } = chromeAuthDeps();

    await storage.set("k", "v");
    expect(await storage.get("k")).toBe("v");
  });

  it("reports a missing key as null rather than undefined", async () => {
    // AuthSession branches on falsiness, but returning undefined here would
    // make the storage contract differ from the fakes every other test uses.
    expect(await chromeAuthDeps().storage.get("absent")).toBeNull();
  });

  it("removes a value", async () => {
    const { storage } = chromeAuthDeps();
    await storage.set("k", "v");

    await storage.remove("k");

    expect(await storage.get("k")).toBeNull();
  });
});

describe("chromeAuthDeps interactive flow", () => {
  it("requests an interactive flow and returns the redirect", async () => {
    const deps = chromeAuthDeps();

    const redirect = await deps.launchWebAuthFlow("https://accounts.google.com/x");

    expect(stub.lastAuthFlowArgs).toEqual({
      url: "https://accounts.google.com/x",
      interactive: true,
    });
    expect(redirect).toBe("https://abc.chromiumapp.org/#id_token=x");
  });

  it("turns a resolved-without-redirect cancellation into a clear error", async () => {
    // Chrome resolves undefined on some cancel paths instead of rejecting;
    // passing that through would surface as a TypeError reading .hash of
    // undefined rather than something a user can act on.
    stub.authFlowResult = undefined;

    await expect(
      chromeAuthDeps().launchWebAuthFlow("https://accounts.google.com/x"),
    ).rejects.toThrow(/cancelled/i);
  });

  it("uses Chrome's own redirect URL", () => {
    expect(chromeAuthDeps().redirectUri()).toBe("https://abc.chromiumapp.org/");
  });
});

describe("chromeAuthDeps randomness", () => {
  it("returns the requested number of bytes and does not return a constant", () => {
    const { randomBytes } = chromeAuthDeps();

    const a = randomBytes(16);
    const b = randomBytes(16);

    expect(a).toHaveLength(16);
    // A fixed nonce would defeat the replay binding it exists to provide.
    expect([...a].every((byte, i) => byte === b[i])).toBe(false);
  });
});
