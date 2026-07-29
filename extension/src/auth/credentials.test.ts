import { describe, expect, it } from "vitest";
import {
  bundledCredentials,
  DEFAULT_FIREBASE_API_KEY,
  DEFAULT_OAUTH_CLIENT_ID,
  isPlaceholderCredential,
} from "./credentials.js";

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
