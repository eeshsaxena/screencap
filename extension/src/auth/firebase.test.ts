import { describe, expect, it } from "vitest";
import {
  AuthSession,
  REFRESH_BUFFER_SECONDS,
  REFRESH_TOKEN_STORAGE_KEY,
  SECURE_TOKEN_URL,
  SIGN_IN_WITH_IDP_URL,
  type AuthDeps,
} from "./firebase.js";

/** A syntactically valid unsigned JWT. Identity is read from the payload for
 * display only — the cloud function verifies the signature server-side. */
function fakeIdToken(payload: Record<string, unknown>): string {
  const b64 = (o: unknown) =>
    Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${b64({ alg: "none" })}.${b64(payload)}.sig`;
}

interface Harness {
  deps: AuthDeps;
  store: Map<string, string>;
  calls: string[];
  setNow: (seconds: number) => void;
  authFlowCount: () => number;
}

function harness(
  responses: Partial<Record<"idp" | "refresh", () => Response>> = {},
): Harness {
  const store = new Map<string, string>();
  const calls: string[] = [];
  let now = 1_000_000;
  let authFlows = 0;

  const ok = (body: unknown) =>
    new Response(JSON.stringify(body), { status: 200 });

  const deps: AuthDeps = {
    now: () => now,
    credentials: () => ({
      firebaseApiKey: "test-api-key",
      oauthClientId: "test-client-id",
    }),
    redirectUri: () => "https://abc.chromiumapp.org/",
    randomBytes: (n) => new Uint8Array(n).fill(7),
    launchWebAuthFlow: async (url) => {
      authFlows += 1;
      const nonce = new URL(url).searchParams.get("nonce");
      return `https://abc.chromiumapp.org/#id_token=${fakeIdToken({
        nonce,
      })}&nonce=${nonce}`;
    },
    storage: {
      get: async (k) => store.get(k) ?? null,
      set: async (k, v) => void store.set(k, v),
      remove: async (k) => void store.delete(k),
    },
    fetch: (async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith(SIGN_IN_WITH_IDP_URL)) {
        calls.push("idp");
        return (
          responses.idp?.() ??
          ok({
            idToken: fakeIdToken({ user_id: "uid-1", email: "a@example.com" }),
            refreshToken: "refresh-1",
            expiresIn: "3600",
          })
        );
      }
      if (url.startsWith(SECURE_TOKEN_URL)) {
        calls.push("refresh");
        return (
          responses.refresh?.() ??
          ok({
            // `refreshed` only distinguishes this token from the sign-in one so
            // tests can tell them apart; Firebase's real tokens differ by iat.
            id_token: fakeIdToken({
              user_id: "uid-1",
              email: "a@example.com",
              refreshed: true,
            }),
            refresh_token: "refresh-2",
            expires_in: "3600",
          })
        );
      }
      throw new Error(`unexpected fetch: ${url}`);
    }) as typeof fetch,
  };

  return {
    deps,
    store,
    calls,
    setNow: (s) => {
      now = s;
    },
    authFlowCount: () => authFlows,
  };
}

describe("AuthSession", () => {
  it("reports signed out when nothing is stored", async () => {
    const h = harness();
    const who = await new AuthSession(h.deps).whoami();

    expect(who.signedIn).toBe(false);
    expect(who.uid ?? null).toBeNull();
    expect(who.email ?? null).toBeNull();
  });

  it("surfaces the account identity after a successful sign-in", async () => {
    const h = harness();
    const session = new AuthSession(h.deps);

    const state = await session.signIn();

    expect(state.uid).toBe("uid-1");
    expect(state.email).toBe("a@example.com");
    expect(await session.whoami()).toMatchObject({
      signedIn: true,
      uid: "uid-1",
      email: "a@example.com",
    });
    expect(h.store.get(REFRESH_TOKEN_STORAGE_KEY)).toBe("refresh-1");
  });

  it("never persists the ID token, only the refresh token", async () => {
    const h = harness();
    const state = await new AuthSession(h.deps).signIn();

    const persisted = [...h.store.values()].join("|");
    expect(persisted).not.toContain(state.idToken);
  });

  it("refreshes an expiring ID token without a second interactive sign-in", async () => {
    const h = harness();
    const session = new AuthSession(h.deps);
    const first = await session.signIn();

    // Inside the refresh buffer: the cached token is still valid but too close
    // to expiry to hand out.
    h.setNow(first.expiresAt - REFRESH_BUFFER_SECONDS + 1);
    const token = await session.getIdToken();

    expect(token).not.toBe(first.idToken);
    expect(h.calls).toEqual(["idp", "refresh"]);
    expect(h.authFlowCount()).toBe(1);
    expect(h.store.get(REFRESH_TOKEN_STORAGE_KEY)).toBe("refresh-2");
  });

  it("serves the cached ID token while it is comfortably valid", async () => {
    const h = harness();
    const session = new AuthSession(h.deps);
    const first = await session.signIn();

    h.setNow(first.expiresAt - REFRESH_BUFFER_SECONDS - 60);

    expect(await session.getIdToken()).toBe(first.idToken);
    expect(h.calls).toEqual(["idp"]);
  });

  it("falls back to signed out when the refresh token is rejected", async () => {
    const h = harness({
      refresh: () =>
        new Response(JSON.stringify({ error: { message: "TOKEN_EXPIRED" } }), {
          status: 400,
        }),
    });
    h.store.set(REFRESH_TOKEN_STORAGE_KEY, "stale-refresh");
    const session = new AuthSession(h.deps);

    const who = await session.whoami();

    expect(who.signedIn).toBe(false);
    expect(h.store.has(REFRESH_TOKEN_STORAGE_KEY)).toBe(false);
    await expect(session.getIdToken()).rejects.toThrow(/signed out|not signed in/i);
  });

  it("restores the session from a stored refresh token across restarts", async () => {
    const h = harness();
    h.store.set(REFRESH_TOKEN_STORAGE_KEY, "refresh-from-disk");

    const who = await new AuthSession(h.deps).whoami();

    expect(who).toMatchObject({ signedIn: true, uid: "uid-1" });
    expect(h.calls).toEqual(["refresh"]);
    expect(h.authFlowCount()).toBe(0);
  });

  it("clears the stored credential on sign-out", async () => {
    const h = harness();
    const session = new AuthSession(h.deps);
    await session.signIn();

    await session.signOut();

    expect(h.store.has(REFRESH_TOKEN_STORAGE_KEY)).toBe(false);
    expect(await session.whoami()).toMatchObject({ signedIn: false });
  });

  it("rejects a redirect whose nonce does not match the request", async () => {
    const h = harness();
    h.deps.launchWebAuthFlow = async () =>
      `https://abc.chromiumapp.org/#id_token=${fakeIdToken({
        nonce: "attacker-chosen",
      })}&nonce=attacker-chosen`;

    await expect(new AuthSession(h.deps).signIn()).rejects.toThrow(/nonce/i);
  });
});
