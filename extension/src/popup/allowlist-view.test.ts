import { describe, expect, it } from "vitest";
import { EMPTY_MESSAGE, addOutcome, allowlistView } from "./allowlist-view.js";

const entry = (label: string) => ({ pattern: `${label}/*`, label });

describe("allowlistView", () => {
  it("says what an empty allow-list means, not just that it is empty", () => {
    const view = allowlistView({ signedIn: true, entries: [] });

    expect(view.empty).toBe(true);
    expect(view.emptyMessage).toBe(EMPTY_MESSAGE);
    expect(view.emptyMessage).toMatch(/nothing can be recorded/i);
    // A fresh install with no allow-listed origins is the safe default, so the
    // empty state must not be rendered through the error channel.
    expect(view.error).toBeNull();
  });

  it("renders one row per entry", () => {
    const view = allowlistView({
      signedIn: true,
      entries: [entry("https://example.com"), entry("https://app.example.com")],
    });

    expect(view.empty).toBe(false);
    expect(view.entries.map((e) => e.label)).toEqual([
      "https://example.com",
      "https://app.example.com",
    ]);
  });

  it("hides the panel when signed out", () => {
    expect(allowlistView({ signedIn: false, entries: [] }).visible).toBe(false);
  });

  it("keeps the list visible alongside an error so a failed add does not blank it", () => {
    const view = allowlistView({
      signedIn: true,
      entries: [entry("https://example.com")],
      error: "Chrome didn't grant access, so nothing was added.",
    });

    expect(view.entries).toHaveLength(1);
    expect(view.error).not.toBeNull();
  });
});

describe("addOutcome", () => {
  it("names the input that could not be read as a site", () => {
    const outcome = addOutcome({ ok: false, reason: "invalid" }, "  not a site  ");

    expect(outcome.error).toContain('"not a site"');
    expect(outcome.notice).toBeNull();
  });

  it("reports a decline as a fact rather than a failure to explain", () => {
    const outcome = addOutcome({ ok: false, reason: "declined" }, "example.com");

    expect(outcome.error).toMatch(/didn't grant access/i);
  });

  it("warns that a dropped port widened the grant", () => {
    // The one case where a successful add did something broader than what was
    // typed. Chrome ignores ports in host permissions, so allowing
    // http://localhost:3000 allows every port on localhost.
    const outcome = addOutcome(
      {
        ok: true,
        entry: entry("http://localhost"),
        portDropped: "3000",
      },
      "http://localhost:3000",
    );

    expect(outcome.notice).toContain("every port on http://localhost");
    expect(outcome.error).toBeNull();
  });

  it("says nothing when an ordinary add succeeds", () => {
    const outcome = addOutcome(
      { ok: true, entry: entry("https://example.com"), portDropped: null },
      "example.com",
    );

    expect(outcome).toEqual({ error: null, notice: null });
  });
});
