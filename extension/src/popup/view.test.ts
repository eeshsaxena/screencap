import { describe, expect, it } from "vitest";
import { popupView } from "./view.js";

describe("popupView", () => {
  it("offers sign-in and shows no identity when signed out", () => {
    const view = popupView({ signedIn: false, uid: null, email: null });

    expect(view.showSignedIn).toBe(false);
    expect(view.accountLabel).toBeNull();
  });

  it("shows the account email once signed in", () => {
    const view = popupView({
      signedIn: true,
      uid: "uid-1",
      email: "a@example.com",
    });

    expect(view.showSignedIn).toBe(true);
    expect(view.accountLabel).toBe("a@example.com");
  });

  it("falls back to the account id when the token carries no email", () => {
    const view = popupView({ signedIn: true, uid: "uid-1", email: null });

    expect(view.showSignedIn).toBe(true);
    expect(view.accountLabel).toBe("uid-1");
  });

  it("returns to the signed-out view after sign-out", () => {
    const view = popupView({ signedIn: false, uid: null, email: null });

    expect(view.showSignedIn).toBe(false);
  });

  it("surfaces an error alongside the signed-out view", () => {
    const view = popupView(
      { signedIn: false, uid: null, email: null },
      "Sign-in was cancelled",
    );

    expect(view.showSignedIn).toBe(false);
    expect(view.error).toBe("Sign-in was cancelled");
  });
});
