import { describe, expect, it } from "vitest";
import { popupView, unreachableView } from "./view.js";

describe("popupView", () => {
  it("offers sign-in and shows no identity when signed out", () => {
    const view = popupView({ signedIn: false, uid: null, email: null });

    expect(view.status).toBe("signed-out");
    expect(view.accountLabel).toBeNull();
  });

  it("shows the account email once signed in", () => {
    const view = popupView({
      signedIn: true,
      uid: "uid-1",
      email: "a@example.com",
    });

    expect(view.status).toBe("signed-in");
    expect(view.accountLabel).toBe("a@example.com");
  });

  it("falls back to the account id when the token carries no email", () => {
    const view = popupView({ signedIn: true, uid: "uid-1", email: null });

    expect(view.status).toBe("signed-in");
    expect(view.accountLabel).toBe("uid-1");
  });

  it("keeps an offline session signed in rather than showing it as logged out", () => {
    // Being unable to reach the identity service is not the same as having no
    // account — offering a sign-in here would be wrong.
    const view = popupView({ signedIn: true, uid: null, email: null, stale: true });

    expect(view.status).toBe("stale");
    expect(view.accountLabel).toBe("your account");
  });

  it("surfaces an error alongside the signed-out view", () => {
    const view = popupView(
      { signedIn: false, uid: null, email: null },
      "Sign-in was cancelled",
    );

    expect(view.status).toBe("signed-out");
    expect(view.error).toBe("Sign-in was cancelled");
  });
});

describe("unreachableView", () => {
  it("reports unknown rather than signed out when the worker cannot be reached", () => {
    const view = unreachableView("Could not establish connection");

    expect(view.status).toBe("unknown");
    expect(view.accountLabel).toBeNull();
    expect(view.error).toBe("Could not establish connection");
  });
});
