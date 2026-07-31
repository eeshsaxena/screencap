/**
 * Covers the capture module's adapters to the real Chrome APIs. Every other
 * capture test injects fakes, so without this the mapping from Chrome's shapes
 * would be the one part nothing exercises — matching the `chrome-deps.test.ts`
 * files in `auth/` and `permissions/`.
 *
 * The IndexedDB and MediaRecorder adapters are deliberately absent: Node has
 * neither, and both modules say so in their own docstrings rather than claiming
 * a coverage they do not have.
 */
import { beforeEach, describe, expect, it } from "vitest";

import { SESSION_STORAGE_KEY, chromeSessionStorage } from "./session.js";

let area: Map<string, string>;

beforeEach(() => {
  area = new Map();
  (globalThis as unknown as { chrome: unknown }).chrome = {
    storage: {
      local: {
        get: async (key: string) =>
          area.has(key) ? { [key]: area.get(key) } : {},
        set: async (items: Record<string, string>) => {
          for (const [k, v] of Object.entries(items)) area.set(k, v);
        },
        remove: async (key: string) => void area.delete(key),
      },
    },
  };
});

describe("chromeSessionStorage", () => {
  it("round-trips a value through chrome.storage.local", async () => {
    const storage = chromeSessionStorage();

    await storage.set(SESSION_STORAGE_KEY, "{}");

    expect(await storage.get(SESSION_STORAGE_KEY)).toBe("{}");
  });

  it("reports a missing key as null rather than undefined", async () => {
    // SessionStore.read branches on `=== null`; undefined would slip past it
    // and reach JSON.parse.
    expect(await chromeSessionStorage().get(SESSION_STORAGE_KEY)).toBeNull();
  });

  it("removes the key so a cleared session does not read back", async () => {
    const storage = chromeSessionStorage();
    await storage.set(SESSION_STORAGE_KEY, "{}");

    await storage.remove(SESSION_STORAGE_KEY);

    expect(area.has(SESSION_STORAGE_KEY)).toBe(false);
  });
});
