import { describe, expect, it } from "vitest";
import { isAdoptablePattern, normalizeOrigin, originLabel } from "./origins.js";

describe("normalizeOrigin", () => {
  it("discards path and query, keeping scheme and host", () => {
    const origin = normalizeOrigin("https://example.com/reports/42?q=open#top");

    expect(origin).toEqual({
      pattern: "https://example.com/*",
      label: "https://example.com",
      portDropped: null,
    });
  });

  it("defaults a bare host to https, which is what someone types", () => {
    expect(normalizeOrigin("example.com")?.pattern).toBe("https://example.com/*");
  });

  it("preserves an explicit http host rather than upgrading it", () => {
    // Internal admin panels are the persona's habitat and plenty of them are
    // plain http. Silently upgrading would grant a host the user never named.
    expect(normalizeOrigin("http://internal.corp/admin")?.pattern).toBe(
      "http://internal.corp/*",
    );
  });

  it("lowercases the host so one origin cannot be added twice", () => {
    expect(normalizeOrigin("HTTPS://Example.COM/x")?.pattern).toBe(
      "https://example.com/*",
    );
  });

  it("trims surrounding whitespace", () => {
    expect(normalizeOrigin("  example.com  ")?.pattern).toBe("https://example.com/*");
  });

  it("keeps a subdomain distinct from its parent", () => {
    // KTD6: a grant covers the exact host. Collapsing these would silently
    // widen every allow-list entry to the whole registrable domain.
    expect(normalizeOrigin("https://app.example.com")?.pattern).toBe(
      "https://app.example.com/*",
    );
    expect(normalizeOrigin("https://example.com")?.pattern).toBe(
      "https://example.com/*",
    );
  });
});

describe("normalizeOrigin port handling", () => {
  // Chrome silently ignores ports in host-permission match patterns, and a
  // pattern carrying one can fail to grant at all (crbug 40517388). Dropping
  // the port is therefore the only shape that actually works — but it grants
  // every port on the host, so the caller is told rather than left guessing.
  it("drops the port and reports what it dropped", () => {
    expect(normalizeOrigin("http://localhost:3000")).toEqual({
      pattern: "http://localhost/*",
      label: "http://localhost",
      portDropped: "3000",
    });
  });

  it("labels the widened origin, not the string that was typed", () => {
    expect(normalizeOrigin("http://192.168.1.5:8080/admin")?.label).toBe(
      "http://192.168.1.5",
    );
  });

  it("does not report a dropped port for a default-port URL", () => {
    expect(normalizeOrigin("https://example.com:443")?.portDropped).toBeNull();
  });
});

describe("normalizeOrigin rejections", () => {
  it.each([
    ["an empty string", ""],
    ["whitespace only", "   "],
    ["a non-URL string", "not a url at all"],
    ["a file URL", "file:///Users/someone/secrets.txt"],
    ["a chrome page", "chrome://extensions"],
    ["another extension", "chrome-extension://abcdef/popup.html"],
    ["a data URL", "data:text/html,<h1>hi</h1>"],
    ["a bare wildcard host", "https://*"],
    ["a wildcard host with a path", "https://*/*"],
  ])("rejects %s", (_label, input) => {
    expect(normalizeOrigin(input)).toBeNull();
  });

  it("rejects rather than throwing, so the caller decides how to surface it", () => {
    expect(() => normalizeOrigin("://///")).not.toThrow();
    expect(normalizeOrigin("://///")).toBeNull();
  });
});

describe("originLabel", () => {
  it("renders a stored pattern as a readable origin", () => {
    expect(originLabel("https://example.com/*")).toBe("https://example.com");
  });

  it("renders a subdomain wildcard grant without mangling it", () => {
    // Chrome can widen a grant through its own prompt; the list has to be able
    // to display what was actually granted.
    expect(originLabel("https://*.example.com/*")).toBe("https://*.example.com");
  });
});

describe("isAdoptablePattern", () => {
  it("accepts a concrete host", () => {
    expect(isAdoptablePattern("https://example.com/*")).toBe(true);
  });

  it("accepts a subdomain wildcard Chrome may have granted", () => {
    // Refusing this would hide a real grant from the list — the same harm
    // adoption exists to prevent.
    expect(isAdoptablePattern("https://*.example.com/*")).toBe(true);
  });

  it.each([
    ["the https envelope", "https://*/*"],
    ["the http envelope", "http://*/*"],
    ["an all-schemes envelope", "*://*/*"],
    ["all urls", "<all_urls>"],
  ])("refuses to adopt %s", (_label, pattern) => {
    // The manifest must declare a broad optional envelope for arbitrary
    // user-chosen origins. If getAll() ever returns it, adopting it would make
    // the allow-list read back as everything-allowed — a silent fail-open of
    // the boundary this module exists to draw.
    expect(isAdoptablePattern(pattern)).toBe(false);
  });

  it("refuses a malformed pattern rather than assuming it is safe", () => {
    expect(isAdoptablePattern("garbage")).toBe(false);
    expect(isAdoptablePattern("")).toBe(false);
  });
});
