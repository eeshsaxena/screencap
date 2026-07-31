import { describe, expect, it } from "vitest";
import {
  bundledCredentials,
  DEFAULT_FIREBASE_API_KEY,
  DEFAULT_OAUTH_CLIENT_ID,
  isPlaceholderCredential,
} from "./credentials.js";
import { FIREBASE_API_KEY, OAUTH_CLIENT_ID } from "./provisioned.js";

describe("the injection point and the sentinels", () => {
  // The one failure mode splitting the injected values into their own module
  // introduces. `isPlaceholderCredential` compares against the sentinels here,
  // not against what `provisioned.ts` actually holds, so a typo there would
  // make an un-provisioned build read as provisioned and pass the release
  // guard — the exact thing the guard exists to catch.
  it("agree on what un-provisioned looks like", () => {
    expect(FIREBASE_API_KEY).toBe(DEFAULT_FIREBASE_API_KEY);
    expect(OAUTH_CLIENT_ID).toBe(DEFAULT_OAUTH_CLIENT_ID);
  });

  it("means the checked-in injection point reads as a placeholder", () => {
    expect(isPlaceholderCredential(FIREBASE_API_KEY)).toBe(true);
    expect(isPlaceholderCredential(OAUTH_CLIENT_ID)).toBe(true);
  });
});

describe("isPlaceholderCredential", () => {
  it("recognizes both un-provisioned sentinels", () => {
    expect(isPlaceholderCredential(DEFAULT_FIREBASE_API_KEY)).toBe(true);
    expect(isPlaceholderCredential(DEFAULT_OAUTH_CLIENT_ID)).toBe(true);
  });

  it("treats a real-looking value as provisioned", () => {
    expect(isPlaceholderCredential("AIzaSyExampleRealLookingKey")).toBe(false);
  });
});

describe("bundledCredentials", () => {
  it("refuses to hand back placeholder credentials", () => {
    // The checked-in values are placeholders, so this is what an un-provisioned
    // build does: fail loudly here rather than at an opaque Google error after
    // the user has already clicked sign in.
    expect(() => bundledCredentials()).toThrow(/provisioned credentials/i);
  });
});
