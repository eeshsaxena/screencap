import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { beforeEach, describe, expect, it } from "vitest";
import {
  ALLOWLIST_STORAGE_KEY,
  Allowlist,
  type AllowlistDeps,
} from "./allowlist.js";

/**
 * A stand-in for Chrome's two stores. `granted` is Chrome's permission state;
 * `storageArea` is `chrome.storage.local`. Tests drive them independently,
 * because every interesting case in this module is the two disagreeing.
 */
interface Fake {
  granted: string[];
  storageArea: Map<string, string>;
  requestResult: boolean;
  removeResult: boolean;
  containsResult: boolean;
  containsCalls: string[][];
  requestCalls: string[][];
  removeCalls: string[][];
  failGetAll: boolean;
  failStorageSet: boolean;
  failContains: boolean;
}

let fake: Fake;

function deps(): AllowlistDeps {
  return {
    permissionsRequest: async (origins) => {
      fake.requestCalls.push(origins);
      if (fake.requestResult) fake.granted.push(...origins);
      return fake.requestResult;
    },
    permissionsRemove: async (origins) => {
      fake.removeCalls.push(origins);
      if (fake.removeResult) {
        fake.granted = fake.granted.filter((g) => !origins.includes(g));
      }
      return fake.removeResult;
    },
    permissionsContains: async (origins) => {
      fake.containsCalls.push(origins);
      if (fake.failContains) throw new Error("permissions unavailable");
      return fake.containsResult;
    },
    permissionsGetAll: async () => {
      if (fake.failGetAll) throw new Error("permissions unavailable");
      return [...fake.granted];
    },
    storage: {
      get: async (key) => fake.storageArea.get(key) ?? null,
      set: async (key, value) => {
        if (fake.failStorageSet) throw new Error("storage full");
        fake.storageArea.set(key, value);
      },
    },
  };
}

/** Seed the stored list directly, bypassing `add`, so drift can be constructed. */
function seedStored(...patterns: string[]): void {
  fake.storageArea.set(ALLOWLIST_STORAGE_KEY, JSON.stringify(patterns));
}

function storedPatterns(): string[] {
  const raw = fake.storageArea.get(ALLOWLIST_STORAGE_KEY);
  return raw === undefined ? [] : (JSON.parse(raw) as string[]);
}

beforeEach(() => {
  fake = {
    granted: [],
    storageArea: new Map(),
    requestResult: true,
    removeResult: true,
    containsResult: true,
    containsCalls: [],
    requestCalls: [],
    removeCalls: [],
    failGetAll: false,
    failStorageSet: false,
    failContains: false,
  };
});

describe("reconcile: drift between the stored list and Chrome's grants", () => {
  // This is the case chrome.permissions.onRemoved does not fire for, which is
  // why the design reads grant state instead of listening for changes.
  it("drops an entry whose grant was revoked outside the extension", async () => {
    seedStored("https://example.com/*");
    fake.granted = [];

    expect((await new Allowlist(deps()).list()).entries).toEqual([]);
  });

  it("persists the prune so the stored list stops disagreeing", async () => {
    seedStored("https://example.com/*");
    fake.granted = [];

    await new Allowlist(deps()).list();

    expect(storedPatterns()).toEqual([]);
  });

  it("adopts a grant that has no stored entry", async () => {
    // Granted from chrome://extensions, or a storage write that failed after
    // the grant succeeded. Either way the origin is recordable right now, and
    // an origin the user cannot see is one they cannot revoke.
    fake.granted = ["https://adopted.example/*"];

    expect((await new Allowlist(deps()).list()).entries).toEqual([
      { pattern: "https://adopted.example/*", label: "https://adopted.example" },
    ]);
    expect(storedPatterns()).toEqual(["https://adopted.example/*"]);
  });

  it("returns the corrected list even when the repairing write fails", async () => {
    // The answer is fail-closed regardless of whether the repair persists.
    seedStored("https://example.com/*");
    fake.granted = [];
    fake.failStorageSet = true;

    expect((await new Allowlist(deps()).list()).entries).toEqual([]);
    expect(storedPatterns()).toEqual(["https://example.com/*"]);
  });

  it("flags an unreadable grant state instead of calling it empty", async () => {
    // Reporting [] here would tell the user nothing can be recorded while the
    // real grants — and therefore isAllowed — are untouched and still live.
    seedStored("https://example.com/*");
    fake.failGetAll = true;

    const listing = await new Allowlist(deps()).list();

    expect(listing.state).toBe("grants-unreadable");
    expect(listing.entries).toEqual([
      { pattern: "https://example.com/*", label: "https://example.com" },
    ]);
  });

  it("does not overwrite the stored list when grants cannot be read", async () => {
    // A transient getAll() failure must not destroy the ordering that is the
    // entire reason a stored list exists alongside Chrome's grants.
    seedStored("https://a.example/*", "https://b.example/*");
    fake.failGetAll = true;

    await new Allowlist(deps()).list();

    expect(storedPatterns()).toEqual(["https://a.example/*", "https://b.example/*"]);
  });

  it("leaves the stored list alone when it already agrees with Chrome", async () => {
    seedStored("https://example.com/*");
    fake.granted = ["https://example.com/*"];
    fake.failStorageSet = true; // would throw if a write were attempted

    expect((await new Allowlist(deps()).list()).entries).toHaveLength(1);
  });

  it("shows the widened pattern when Chrome granted more than was asked for", async () => {
    seedStored("https://example.com/*");
    fake.granted = ["https://*.example.com/*"];

    expect((await new Allowlist(deps()).list()).entries).toEqual([
      { pattern: "https://*.example.com/*", label: "https://*.example.com" },
    ]);
  });

  it("collapses a duplicated entry into one row", async () => {
    // Two popups reconciling at once, or an add that raced another context's
    // write, can store the same pattern twice. Two Remove buttons for one grant
    // is a confusing way to present that.
    seedStored("https://example.com/*", "https://example.com/*");
    fake.granted = ["https://example.com/*"];

    expect((await new Allowlist(deps()).list()).entries).toEqual([
      { pattern: "https://example.com/*", label: "https://example.com" },
    ]);
    expect(storedPatterns()).toEqual(["https://example.com/*"]);
  });

  it("survives a corrupt stored value instead of throwing", async () => {
    fake.storageArea.set(ALLOWLIST_STORAGE_KEY, "{not json");
    fake.granted = ["https://example.com/*"];

    expect((await new Allowlist(deps()).list()).entries).toHaveLength(1);
  });
});

describe("reconcile: the adoption guard", () => {
  it.each([
    ["the https envelope", "https://*/*"],
    ["the http envelope", "http://*/*"],
  ])("never adopts %s", async (_label, envelope) => {
    // The manifest must declare this envelope to request arbitrary origins at
    // runtime. Adopting it would make the allow-list read back as
    // everything-allowed — a silent fail-open of the whole boundary.
    fake.granted = [envelope];

    expect((await new Allowlist(deps()).list()).entries).toEqual([]);
  });

  it("adopts real grants while refusing the envelope beside them", async () => {
    fake.granted = ["https://*/*", "https://real.example/*"];

    expect((await new Allowlist(deps()).list()).entries).toEqual([
      { pattern: "https://real.example/*", label: "https://real.example" },
    ]);
  });

  it("reports a live all-sites grant rather than showing an empty list", async () => {
    // The whole point. Refusing to adopt the envelope keeps it out of the rows
    // — there would be no way to revoke it from one — but an empty list here
    // would read as "nothing can be recorded" while isAllowed answers true for
    // every URL by subsumption. The state is what stops that claim being made.
    fake.granted = ["https://*/*"];
    fake.containsResult = true; // Chrome confirms the probe host is covered

    const allowlist = new Allowlist(deps());
    const listing = await allowlist.list();

    expect(listing.state).toBe("broad-grant");
    expect(listing.entries).toEqual([]);
    // ...and the gate genuinely does allow everything in this state, which is
    // exactly why the list must not claim otherwise.
    expect(await allowlist.isAllowed("https://anything.example/page")).toBe(true);
  });

  it("stays quiet when the envelope is declared but not actually granted", async () => {
    // Chrome's docs leave open whether getAll() reports the manifest's declared
    // optional envelope. If it does, warning on its mere presence would cry
    // wolf on every popup open, so the probe decides instead.
    fake.granted = ["https://*/*"];
    fake.containsResult = false; // no live grant covers the probe host

    const listing = await new Allowlist(deps()).list();

    expect(listing.state).toBe("ok");
    expect(listing.entries).toEqual([]);
  });

  it("does not probe at all when no envelope is present", async () => {
    fake.granted = ["https://real.example/*"];

    expect((await new Allowlist(deps()).list()).state).toBe("ok");
    expect(fake.containsCalls).toEqual([]);
  });
});

describe("add", () => {
  it("requests the matching host permission and records only on success", async () => {
    const result = await new Allowlist(deps()).add("example.com");

    expect(fake.requestCalls).toEqual([["https://example.com/*"]]);
    expect(result).toEqual({
      ok: true,
      entry: { pattern: "https://example.com/*", label: "https://example.com" },
      portDropped: null,
    });
    expect(storedPatterns()).toEqual(["https://example.com/*"]);
  });

  it("reaches the permission request before yielding, so the gesture survives", () => {
    // Chrome only honours permissions.request() inside a user gesture, and an
    // await before it spends the gesture. Nothing is awaited here on purpose:
    // if add() ever grows a storage read or a message round-trip ahead of the
    // request, requestCalls is still empty at this point and this fails.
    void new Allowlist(deps()).add("example.com");

    expect(fake.requestCalls).toEqual([["https://example.com/*"]]);
  });

  it("leaves the allow-list unchanged when the prompt is declined", async () => {
    fake.requestResult = false;

    expect(await new Allowlist(deps()).add("example.com")).toEqual({
      ok: false,
      reason: "declined",
    });
    expect(storedPatterns()).toEqual([]);
  });

  it("rejects input that cannot be expressed as an origin", async () => {
    expect(await new Allowlist(deps()).add("chrome://extensions")).toEqual({
      ok: false,
      reason: "invalid",
    });
    expect(fake.requestCalls).toEqual([]);
  });

  it("surfaces a failed request and stores nothing", async () => {
    const failing = { ...deps(), permissionsRequest: async () => {
      throw new Error("request blew up");
    } };

    await expect(new Allowlist(failing).add("example.com")).rejects.toThrow(
      "request blew up",
    );
    expect(storedPatterns()).toEqual([]);
  });

  it("reports a dropped port so the caller can say the grant widened", async () => {
    const result = await new Allowlist(deps()).add("http://localhost:3000");

    expect(fake.requestCalls).toEqual([["http://localhost/*"]]);
    expect(result).toMatchObject({ ok: true, portDropped: "3000" });
  });

  it("does not add a duplicate entry for an origin already allowed", async () => {
    const allowlist = new Allowlist(deps());
    await allowlist.add("example.com");
    await allowlist.add("https://example.com/somewhere/else");

    expect(storedPatterns()).toEqual(["https://example.com/*"]);
  });
});

describe("remove", () => {
  it("revokes the host permission as well as the list entry", async () => {
    const allowlist = new Allowlist(deps());
    await allowlist.add("example.com");

    await allowlist.remove("https://example.com/*");

    expect(fake.removeCalls).toEqual([["https://example.com/*"]]);
    expect(fake.granted).toEqual([]);
    expect(storedPatterns()).toEqual([]);
  });

  it("keeps the entry when Chrome refuses to revoke", async () => {
    // A list that still shows a live grant is honest; one that hides it is not.
    const allowlist = new Allowlist(deps());
    await allowlist.add("example.com");
    fake.removeResult = false;

    await expect(allowlist.remove("https://example.com/*")).rejects.toThrow();
    expect(storedPatterns()).toEqual(["https://example.com/*"]);
  });
});

describe("isAllowed", () => {
  it("asks Chrome about the page's own origin pattern", async () => {
    // Subsumption is Chrome's job: a broader grant like https://*.example.com/*
    // must answer true for this page, and that is what contains() decides.
    fake.containsResult = true;

    expect(await new Allowlist(deps()).isAllowed("https://app.example.com/x?y=1")).toBe(
      true,
    );
    expect(fake.containsCalls).toEqual([["https://app.example.com/*"]]);
  });

  it("returns Chrome's negative verbatim", async () => {
    fake.containsResult = false;

    expect(await new Allowlist(deps()).isAllowed("https://app.example.com/x")).toBe(
      false,
    );
  });

  it("refuses when the permission check throws", async () => {
    fake.failContains = true;

    expect(await new Allowlist(deps()).isAllowed("https://example.com/")).toBe(false);
  });

  it.each([
    ["a chrome page", "chrome://newtab"],
    ["an unparseable URL", "://///"],
    ["an empty string", ""],
  ])("refuses %s without asking Chrome", async (_label, url) => {
    expect(await new Allowlist(deps()).isAllowed(url)).toBe(false);
    expect(fake.containsCalls).toEqual([]);
  });

  it("never consults storage, so a stale entry cannot authorize capture", async () => {
    seedStored("https://example.com/*");
    fake.granted = [];
    fake.containsResult = false;

    expect(await new Allowlist(deps()).isAllowed("https://example.com/")).toBe(false);
  });
});

describe("manifest", () => {
  const manifest = JSON.parse(
    readFileSync(
      fileURLToPath(new URL("../../manifest.json", import.meta.url)),
      "utf8",
    ),
  ) as Record<string, unknown>;

  it("declares the optional envelope needed to request arbitrary origins", () => {
    expect(manifest.optional_host_permissions).toEqual([
      "https://*/*",
      "http://*/*",
    ]);
  });

  it("declares no required host permissions", () => {
    // Nothing is granted at install, so every concrete host in getAll() is a
    // runtime grant the user made — the invariant reconcile depends on.
    expect(manifest.host_permissions).toBeUndefined();
  });

  it("does not carry permissions no code exercises yet", () => {
    expect(manifest.permissions).toEqual([
      "identity",
      "storage",
      "activeTab",
      "tabCapture",
      "offscreen",
      "unlimitedStorage",
    ]);
  });

  it("does not turn activeTab into standing access to every tab", () => {
    // activeTab is what lets the popup read the current tab's title and capture
    // it, scoped to the moment the user opens the popup. The `tabs` permission
    // would grant the same reads across every tab, permanently, and warn the
    // user accordingly — a much wider ask for the same feature.
    expect(manifest.permissions).not.toContain("tabs");
  });

  it("does not request desktopCapture", () => {
    // A desktopCapture stream id cannot be consumed inside an offscreen
    // document, so whole-screen capture uses the web-standard display picker
    // instead and this permission would buy nothing — while costing a
    // sensitive-permission line in Chrome Web Store review.
    expect(manifest.permissions).not.toContain("desktopCapture");
  });

  it("keeps recorded bytes exempt from quota eviction", () => {
    // Slices live in IndexedDB until upload. Without unlimitedStorage the
    // browser may evict a recording that has not been uploaded yet.
    expect(manifest.permissions).toContain("unlimitedStorage");
  });
});
