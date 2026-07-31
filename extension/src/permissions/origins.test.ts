import { describe, expect, it } from "vitest";
import {
  isAdoptablePattern,
  isBroadHostPattern,
  normalizeOrigin,
  originLabel,
} from "./origins.js";

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

  it("accepts a bare host:port, which reads as a scheme but is not one", () => {
    // `localhost:` is a syntactically valid scheme, so a naive has-a-scheme
    // test treats `localhost:3000` as scheme-qualified, parses it with protocol
    // `localhost:`, and rejects it outright — while `http://localhost:3000`
    // works. A dev server on a port is the operator persona's likeliest input.
    expect(normalizeOrigin("localhost:3000")).toEqual({
      pattern: "https://localhost/*",
      label: "https://localhost",
      portDropped: "3000",
    });
  });

  it("defaults a bare host:port to https, same as any other bare host", () => {
    // Worth pinning rather than leaving implicit: someone typing a plain http
    // dev server gets an https grant, and the dropped-port notice is what shows
    // them the origin that was actually allowed.
    expect(normalizeOrigin("internal.corp:8080")?.pattern).toBe(
      "https://internal.corp/*",
    );
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

describe("isBroadHostPattern", () => {
  it.each([
    ["the https envelope", "https://*/*"],
    ["the http envelope", "http://*/*"],
  ])("recognizes %s", (_label, pattern) => {
    expect(isBroadHostPattern(pattern)).toBe(true);
  });

  it.each([
    ["a concrete host", "https://example.com/*"],
    ["a subdomain wildcard", "https://*.example.com/*"],
    ["a malformed pattern", "garbage"],
    ["an empty string", ""],
  ])("does not treat %s as all-sites", (_label, pattern) => {
    // Deliberately narrower than !isAdoptablePattern, which is also true for
    // malformed input. Only the genuine all-sites shape may trigger the warning
    // that tells a user their per-site list is not what limits recording.
    expect(isBroadHostPattern(pattern)).toBe(false);
  });
});
