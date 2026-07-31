/**
 * Covers `chromeAllowlistDeps()` — the adapter between `Allowlist` and the real
 * Chrome APIs. Every other allow-list test injects fakes, so without this the
 * mapping from Chrome's shapes to `AllowlistDeps` would be the one part of the
 * module nothing exercises.
 */
import { beforeEach, describe, expect, it } from "vitest";
import { chromeAllowlistDeps } from "./allowlist.js";

interface ChromeStub {
  storageArea: Map<string, string>;
  granted: { permissions?: string[]; origins?: string[] };
  lastRequest: { origins?: string[] } | null;
  lastRemove: { origins?: string[] } | null;
  lastContains: { origins?: string[] } | null;
  containsResult: boolean;
  requestResult: boolean;
  removeResult: boolean;
}

let stub: ChromeStub;

beforeEach(() => {
  stub = {
    storageArea: new Map(),
    granted: { permissions: ["storage"], origins: ["https://example.com/*"] },
    lastRequest: null,
    lastRemove: null,
    lastContains: null,
    containsResult: true,
    requestResult: true,
    removeResult: true,
  };

  (globalThis as unknown as { chrome: unknown }).chrome = {
    permissions: {
      request: async (p: { origins?: string[] }) => {
        stub.lastRequest = p;
        return stub.requestResult;
      },
      remove: async (p: { origins?: string[] }) => {
        stub.lastRemove = p;
        return stub.removeResult;
      },
      contains: async (p: { origins?: string[] }) => {
        stub.lastContains = p;
        return stub.containsResult;
      },
      getAll: async () => stub.granted,
    },
    storage: {
      local: {
        get: async (key: string) =>
          stub.storageArea.has(key) ? { [key]: stub.storageArea.get(key) } : {},
        set: async (items: Record<string, string>) => {
          for (const [k, v] of Object.entries(items)) stub.storageArea.set(k, v);
        },
      },
    },
  };
});

describe("chromeAllowlistDeps permissions", () => {
  it("wraps origins in the shape chrome.permissions expects", async () => {
    await chromeAllowlistDeps().permissionsRequest(["https://a.example/*"]);

    expect(stub.lastRequest).toEqual({ origins: ["https://a.example/*"] });
  });

  it("returns Chrome's grant verdict unchanged", async () => {
    stub.requestResult = false;

    expect(
      await chromeAllowlistDeps().permissionsRequest(["https://a.example/*"]),
    ).toBe(false);
  });

  it("passes a revoke through with the same wrapping", async () => {
    stub.removeResult = true;

    expect(
      await chromeAllowlistDeps().permissionsRemove(["https://a.example/*"]),
    ).toBe(true);
    expect(stub.lastRemove).toEqual({ origins: ["https://a.example/*"] });
  });

  it("asks contains about exactly the origins it was given", async () => {
    stub.containsResult = false;

    expect(
      await chromeAllowlistDeps().permissionsContains(["https://a.example/*"]),
    ).toBe(false);
    expect(stub.lastContains).toEqual({ origins: ["https://a.example/*"] });
  });
});

describe("chromeAllowlistDeps getAll", () => {
  it("unwraps the origins array out of Chrome's Permissions object", async () => {
    expect(await chromeAllowlistDeps().permissionsGetAll()).toEqual([
      "https://example.com/*",
    ]);
  });

  it("reports no origins as an empty list rather than undefined", async () => {
    // Chrome omits `origins` entirely when nothing is granted. Passing that
    // through would make reconcile read `undefined.filter` and throw, turning
    // the ordinary empty-allow-list case into a crash.
    stub.granted = { permissions: ["storage"] };

    expect(await chromeAllowlistDeps().permissionsGetAll()).toEqual([]);
  });
});

describe("chromeAllowlistDeps storage", () => {
  it("round-trips a value through chrome.storage.local", async () => {
    const { storage } = chromeAllowlistDeps();

    await storage.set("k", "v");

    expect(await storage.get("k")).toBe("v");
  });

  it("reports a missing key as null rather than undefined", async () => {
    // The store branches on `=== null`; undefined would slip past it.
    expect(await chromeAllowlistDeps().storage.get("absent")).toBeNull();
  });
});
