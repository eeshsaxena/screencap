import { describe, expect, it } from "vitest";
import {
  AuthSession,
  AuthUnavailableError,
  extractVerifiedIdToken,
  NotSignedInError,
  REFRESH_BUFFER_SECONDS,
  REFRESH_TOKEN_STORAGE_KEY,
  SECURE_TOKEN_URL,
  SIGN_IN_WITH_IDP_URL,
  SignOutIncompleteError,
  type AuthDeps,
} from "./firebase.js";

const CLIENT_ID = "test-client-id";

/** UTF-8 -> base64url, the encoding a real JWT segment uses. Deliberately not
 * Node's `Buffer`: this project targets the browser, and going through
 * `TextEncoder`/`btoa` exercises the same path the production decode reverses. */
function b64url(value: unknown): string {
  const bytes = new TextEncoder().encode(JSON.stringify(value));
  const binary = String.fromCharCode(...bytes);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** A syntactically valid unsigned JWT. Identity is read from the payload for
 * display only — the cloud function verifies the signature server-side. */
function fakeIdToken(payload: Record<string, unknown>): string {
  return `${b64url({ alg: "none" })}.${b64url(payload)}.sig`;
}

/**
 * A Google OIDC implicit redirect. The nonce lives ONLY in the token claim —
 * the provider does not echo it as a fragment parameter, and a harness that
 * invented one would let a nonce check that reads the fragment pass while
 * every real sign-in failed.
 */
function googleRedirect(claims: Record<string, unknown>): string {
  return `https://abc.chromiumapp.org/#id_token=${fakeIdToken(claims)}&token_type=Bearer`;
}

interface Call {
  url: string;
  body: string;
  method: string;
}

interface Harness {
  deps: AuthDeps;
  store: Map<string, string>;
  calls: Call[];
  names: () => string[];
  setNow: (seconds: number) => void;
  authFlowCount: () => number;
}

function harness(
  responses: Partial<
    Record<"idp" | "refresh", () => Response | Promise<Response>>
  > = {},
): Harness {
  const store = new Map<string, string>();
  const calls: Call[] = [];
  let now = 1_000_000;
  let authFlows = 0;

  const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });

  const deps: AuthDeps = {
    now: () => now,
    credentials: () => ({
      firebaseApiKey: "test-api-key",
      oauthClientId: CLIENT_ID,
    }),
    redirectUri: () => "https://abc.chromiumapp.org/",
    randomBytes: (n) => new Uint8Array(n).fill(7),
    launchWebAuthFlow: async (url) => {
      authFlows += 1;
      const nonce = new URL(url).searchParams.get("nonce");
      return googleRedirect({ nonce, aud: CLIENT_ID });
    },
    storage: {
      get: async (k) => store.get(k) ?? null,
      set: async (k, v) => void store.set(k, v),
      remove: async (k) => void store.delete(k),
    },
    fetch: (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({
        url,
        body: String(init?.body ?? ""),
        method: init?.method ?? "GET",
      });
      if (url.startsWith(SIGN_IN_WITH_IDP_URL)) {
        return await (responses.idp?.() ??
          ok({
            idToken: fakeIdToken({ user_id: "uid-1", email: "a@example.com" }),
            refreshToken: "refresh-1",
            expiresIn: "3600",
          }));
      }
      if (url.startsWith(SECURE_TOKEN_URL)) {
        return await (responses.refresh?.() ??
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
          }));
      }
      throw new Error(`unexpected fetch: ${url}`);
    }) as typeof fetch,
  };

  return {
    deps,
    store,
    calls,
    names: () =>
      calls.map((c) => (c.url.startsWith(SECURE_TOKEN_URL) ? "refresh" : "idp")),
    setNow: (s) => {
      now = s;
    },
    authFlowCount: () => authFlows,
  };
}

describe("extractVerifiedIdToken", () => {
  it("accepts a token whose nonce claim matches the request", () => {
    const redirect = googleRedirect({ nonce: "abc", aud: CLIENT_ID });
    expect(extractVerifiedIdToken(redirect, "abc", CLIENT_ID)).toContain(".");
  });

  it("rejects a token whose nonce claim was minted for another request", () => {
    const redirect = googleRedirect({ nonce: "attacker", aud: CLIENT_ID });
    expect(() => extractVerifiedIdToken(redirect, "abc", CLIENT_ID)).toThrow(/nonce/i);
  });

  it("ignores a nonce smuggled in as a fragment parameter", () => {
    // The provider never sends this; accepting it would let anyone supplying
    // the token also supply the value it is checked against.
    const token = fakeIdToken({ nonce: "attacker", aud: CLIENT_ID });
    const redirect = `https://abc.chromiumapp.org/#id_token=${token}&nonce=abc`;
    expect(() => extractVerifiedIdToken(redirect, "abc", CLIENT_ID)).toThrow(/nonce/i);
  });

  it("rejects a token issued for a different client", () => {
    const redirect = googleRedirect({ nonce: "abc", aud: "some-other-client" });
    expect(() => extractVerifiedIdToken(redirect, "abc", CLIENT_ID)).toThrow(/client/i);
  });

  it("surfaces the provider's error instead of a generic missing-token message", () => {
    const redirect =
      "https://abc.chromiumapp.org/#error=access_denied&error_description=User+declined";
    expect(() => extractVerifiedIdToken(redirect, "abc", CLIENT_ID)).toThrow(
      /User declined/,
    );
  });

  it("rejects a redirect carrying no token at all", () => {
    expect(() =>
      extractVerifiedIdToken("https://abc.chromiumapp.org/#", "abc", CLIENT_ID),
    ).toThrow(/no id_token/i);
  });

  it("rejects a token that is not a JWT", () => {
    const redirect = "https://abc.chromiumapp.org/#id_token=not-a-jwt";
    expect(() => extractVerifiedIdToken(redirect, "abc", CLIENT_ID)).toThrow(
      /malformed/i,
    );
  });
});

describe("AuthSession sign-in", () => {
  it("reports signed out when nothing is stored", async () => {
    const h = harness();
    const who = await new AuthSession(h.deps).whoami();

    expect(who.signedIn).toBe(false);
    expect(who.uid ?? null).toBeNull();
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

  it("sends the identity-provider token in a correctly encoded post body", async () => {
    const h = harness();
    await new AuthSession(h.deps).signIn();

    const idp = h.calls.find((c) => c.url.startsWith(SIGN_IN_WITH_IDP_URL));
    const payload = JSON.parse(idp?.body ?? "{}") as { postBody?: string };
    const fields = new URLSearchParams(payload.postBody ?? "");
    expect(fields.get("providerId")).toBe("google.com");
    expect(fields.get("id_token")).toMatch(/^[\w-]+\.[\w-]+\./);
  });

  it("never persists the ID token, only the refresh token", async () => {
    const h = harness();
    const state = await new AuthSession(h.deps).signIn();

    expect([...h.store.values()].join("|")).not.toContain(state.idToken);
  });
});

describe("AuthSession token refresh", () => {
  it("refreshes an expiring ID token without a second interactive sign-in", async () => {
    const h = harness();
    const session = new AuthSession(h.deps);
    const first = await session.signIn();

    h.setNow(first.expiresAt - REFRESH_BUFFER_SECONDS + 1);
    const token = await session.getIdToken();

    expect(token).not.toBe(first.idToken);
    expect(h.names()).toEqual(["idp", "refresh"]);
    expect(h.authFlowCount()).toBe(1);
    expect(h.store.get(REFRESH_TOKEN_STORAGE_KEY)).toBe("refresh-2");
  });

  it("sends the stored refresh token in a correctly encoded form body", async () => {
    const h = harness();
    h.store.set(REFRESH_TOKEN_STORAGE_KEY, "refresh-from-disk");
    await new AuthSession(h.deps).whoami();

    const call = h.calls.find((c) => c.url.startsWith(SECURE_TOKEN_URL));
    const fields = new URLSearchParams(call?.body ?? "");
    expect(call?.method).toBe("POST");
    expect(fields.get("grant_type")).toBe("refresh_token");
    expect(fields.get("refresh_token")).toBe("refresh-from-disk");
  });

  it("serves the cached ID token while it is comfortably valid", async () => {
    const h = harness();
    const session = new AuthSession(h.deps);
    const first = await session.signIn();

    h.setNow(first.expiresAt - REFRESH_BUFFER_SECONDS - 60);

    expect(await session.getIdToken()).toBe(first.idToken);
    expect(h.names()).toEqual(["idp"]);
  });

  it("prefers the stored refresh token over a stale cached one", async () => {
    const h = harness();
    const session = new AuthSession(h.deps);
    const first = await session.signIn();

    // Another context rotated the credential while this one held the old copy.
    h.store.set(REFRESH_TOKEN_STORAGE_KEY, "rotated-elsewhere");
    h.setNow(first.expiresAt);
    await session.getIdToken();

    const call = h.calls.find((c) => c.url.startsWith(SECURE_TOKEN_URL));
    expect(new URLSearchParams(call?.body ?? "").get("refresh_token")).toBe(
      "rotated-elsewhere",
    );
  });

  it("restores the session from a stored refresh token across restarts", async () => {
    const h = harness();
    h.store.set(REFRESH_TOKEN_STORAGE_KEY, "refresh-from-disk");

    const who = await new AuthSession(h.deps).whoami();

    expect(who).toMatchObject({ signedIn: true, uid: "uid-1" });
    expect(h.authFlowCount()).toBe(0);
  });

  it("signs the user out when the refresh token is rejected as dead", async () => {
    const h = harness({
      refresh: () =>
        new Response(JSON.stringify({ error: { message: "TOKEN_EXPIRED" } }), {
          status: 400,
        }),
    });
    h.store.set(REFRESH_TOKEN_STORAGE_KEY, "stale-refresh");
    const session = new AuthSession(h.deps);

    expect(await session.whoami()).toMatchObject({ signedIn: false });
    expect(h.store.has(REFRESH_TOKEN_STORAGE_KEY)).toBe(false);
    await expect(session.getIdToken()).rejects.toBeInstanceOf(NotSignedInError);
  });

  it("keeps the credential when the token service returns a transient error", async () => {
    for (const status of [429, 500, 503]) {
      const h = harness({ refresh: () => new Response("upstream", { status }) });
      h.store.set(REFRESH_TOKEN_STORAGE_KEY, "still-good");
      const session = new AuthSession(h.deps);

      await expect(session.getIdToken()).rejects.toBeInstanceOf(AuthUnavailableError);

      // A rate limit or an outage must not cost the user their session.
      expect(h.store.get(REFRESH_TOKEN_STORAGE_KEY)).toBe("still-good");
    }
  });

  it("keeps the credential and reports stale when the network is unreachable", async () => {
    const h = harness({
      refresh: () => Promise.reject(new TypeError("Failed to fetch")),
    });
    h.store.set(REFRESH_TOKEN_STORAGE_KEY, "still-good");
    const session = new AuthSession(h.deps);

    expect(await session.whoami()).toMatchObject({ signedIn: true, stale: true });
    expect(h.store.get(REFRESH_TOKEN_STORAGE_KEY)).toBe("still-good");
  });

  it("retries once against a credential rotated by another context", async () => {
    let attempt = 0;
    const h = harness({
      refresh: () => {
        attempt += 1;
        if (attempt === 1) {
          // First attempt loses the race and is rejected as dead...
          h.store.set(REFRESH_TOKEN_STORAGE_KEY, "rotated-mid-flight");
          return new Response(JSON.stringify({}), { status: 400 });
        }
        return new Response(
          JSON.stringify({
            id_token: fakeIdToken({ user_id: "uid-1", email: "a@example.com" }),
            refresh_token: "refresh-3",
            expires_in: "3600",
          }),
          { status: 200 },
        );
      },
    });
    h.store.set(REFRESH_TOKEN_STORAGE_KEY, "about-to-rotate");

    expect(await new AuthSession(h.deps).whoami()).toMatchObject({
      signedIn: true,
      uid: "uid-1",
    });
    expect(attempt).toBe(2);
  });

  it("collapses concurrent refreshes into a single round trip", async () => {
    const h = harness();
    h.store.set(REFRESH_TOKEN_STORAGE_KEY, "refresh-from-disk");
    const session = new AuthSession(h.deps);

    await Promise.all([session.getIdToken(), session.getIdToken(), session.whoami()]);

    expect(h.names().filter((n) => n === "refresh")).toHaveLength(1);
  });
});

describe("AuthSession sign-out", () => {
  it("clears the stored credential", async () => {
    const h = harness();
    const session = new AuthSession(h.deps);
    await session.signIn();

    await session.signOut();

    expect(h.store.has(REFRESH_TOKEN_STORAGE_KEY)).toBe(false);
    expect(await session.whoami()).toMatchObject({ signedIn: false });
  });

  it("keeps the user signed out when a refresh lands after sign-out", async () => {
    let releaseRefresh!: (r: Response) => void;
    const held = new Promise<Response>((resolve) => {
      releaseRefresh = resolve;
    });
    const h = harness({ refresh: () => held });
    h.store.set(REFRESH_TOKEN_STORAGE_KEY, "refresh-from-disk");
    const session = new AuthSession(h.deps);

    const pending = session.getIdToken();
    await session.signOut();
    releaseRefresh(
      new Response(
        JSON.stringify({
          id_token: fakeIdToken({ user_id: "uid-1", email: "a@example.com" }),
          refresh_token: "rotated-refresh",
          expires_in: "3600",
        }),
        { status: 200 },
      ),
    );
    await expect(pending).rejects.toThrow();

    expect(h.store.has(REFRESH_TOKEN_STORAGE_KEY)).toBe(false);
    expect(await session.whoami()).toMatchObject({ signedIn: false });
  });

  it("refuses to claim success when the credential cannot be removed", async () => {
    const h = harness();
    const session = new AuthSession(h.deps);
    await session.signIn();
    h.deps.storage.remove = () => Promise.reject(new Error("storage locked"));

    // Reporting a clean sign-out here would leave the token on disk while the
    // popup showed signed-out, ready for the next revival to restore it.
    await expect(session.signOut()).rejects.toBeInstanceOf(SignOutIncompleteError);
  });

  it("does not let a sign-in landing mid-refresh be discarded", async () => {
    let releaseRefresh!: (r: Response) => void;
    const held = new Promise<Response>((resolve) => {
      releaseRefresh = resolve;
    });
    const h = harness({ refresh: () => held });
    h.store.set(REFRESH_TOKEN_STORAGE_KEY, "old-session");
    const session = new AuthSession(h.deps);

    const stale = session.getIdToken();
    stale.catch(() => undefined);
    const signedIn = await session.signIn();

    // The old session's refresh now fails; it must not tear down the new one.
    releaseRefresh(new Response(JSON.stringify({}), { status: 400 }));
    await expect(stale).rejects.toThrow();

    expect(signedIn.uid).toBe("uid-1");
    expect(await session.whoami()).toMatchObject({ signedIn: true, uid: "uid-1" });
    expect(h.store.get(REFRESH_TOKEN_STORAGE_KEY)).toBe("refresh-1");
  });
});

describe("AuthSession token claim handling", () => {
  const signInReturning = (idToken: string) =>
    harness({
      idp: () =>
        new Response(
          JSON.stringify({ idToken, refreshToken: "r", expiresIn: "3600" }),
          { status: 200 },
        ),
    });

  it("reads a non-ASCII email without mangling it", async () => {
    const h = signInReturning(
      fakeIdToken({ user_id: "uid-1", email: "rené@example.com" }),
    );
    const state = await new AuthSession(h.deps).signIn();

    expect(state.email).toBe("rené@example.com");
  });

  it("falls back to the sub claim when user_id is absent", async () => {
    const h = signInReturning(fakeIdToken({ sub: "uid-from-sub" }));
    const state = await new AuthSession(h.deps).signIn();

    expect(state.uid).toBe("uid-from-sub");
    expect(state.email).toBeNull();
  });

  it("rejects a token carrying no account id", async () => {
    const h = signInReturning(fakeIdToken({ email: "a@example.com" }));
    await expect(new AuthSession(h.deps).signIn()).rejects.toThrow(/user id/i);
  });

  it("rejects a token whose payload is not an object", async () => {
    const h = signInReturning(`${b64url({ alg: "none" })}.${b64url("just-a-string")}.sig`);
    await expect(new AuthSession(h.deps).signIn()).rejects.toThrow(/not an object/i);
  });

  it("honours an immediate expiry instead of caching a dead token for an hour", async () => {
    const h = harness({
      idp: () =>
        new Response(
          JSON.stringify({
            idToken: fakeIdToken({ user_id: "uid-1" }),
            refreshToken: "r",
            expiresIn: "0",
          }),
          { status: 200 },
        ),
    });
    const session = new AuthSession(h.deps);
    const state = await session.signIn();

    expect(state.expiresAt).toBe(h.deps.now());
    // Already expired, so the next read must refresh rather than serve it.
    await session.getIdToken();
    expect(h.names()).toContain("refresh");
  });

  it("defaults to an hour when the expiry field is absent", async () => {
    const h = harness({
      idp: () =>
        new Response(
          JSON.stringify({
            idToken: fakeIdToken({ user_id: "uid-1" }),
            refreshToken: "r",
          }),
          { status: 200 },
        ),
    });
    const state = await new AuthSession(h.deps).signIn();

    expect(state.expiresAt).toBe(h.deps.now() + 3600);
  });
});
