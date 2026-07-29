/**
 * Client-side Firebase auth for the extension: `signIn` / `signOut` /
 * `whoami` / `getIdToken`.
 *
 * Mirrors `src/screencap/auth.py` in shape and lifetime — sign-in yields a
 * Firebase **ID token** (~1h, memory only) and a **refresh token** (long-lived,
 * the durable credential), and the ID token is refreshed transparently a few
 * minutes before expiry. The two surfaces deliberately do NOT share a
 * credential store: the CLI's refresh token lives in the macOS Keychain behind
 * a Team-signed entitlement group that an extension cannot read. Matching the
 * *account* is the requirement, not sharing the credential.
 *
 * The flow differs from the CLI's in one way. The CLI uses a Google **Desktop**
 * OAuth client over a loopback + PKCE code exchange, which requires a bundled
 * client secret at the token endpoint. The extension uses a **Web** client
 * through `chrome.identity.launchWebAuthFlow` requesting an OIDC `id_token`
 * directly, so there is no code exchange and no secret to bundle. A per-request
 * nonce binds the response to the request.
 *
 * Every external dependency is injected through {@link AuthDeps} so the token
 * lifecycle is testable without Chrome or the network.
 */

import { bundledCredentials, type Credentials } from "./credentials.js";

export const SIGN_IN_WITH_IDP_URL =
  "https://identitytoolkit.googleapis.com/v1/accounts:signInWithIdp";
export const SECURE_TOKEN_URL = "https://securetoken.googleapis.com/v1/token";
const GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth";

/** Refresh this many seconds before expiry, matching `_REFRESH_BUFFER_SECONDS`
 * in `src/screencap/auth.py`. */
export const REFRESH_BUFFER_SECONDS = 300;

/** The only key this module persists. The ID token is never written. */
export const REFRESH_TOKEN_STORAGE_KEY = "screencap.auth.refreshToken";

export interface AuthState {
  idToken: string;
  refreshToken: string;
  /** Epoch seconds. */
  expiresAt: number;
  uid: string;
  email: string | null;
}

export interface WhoAmI {
  signedIn: boolean;
  uid?: string | null;
  email?: string | null;
}

export interface AuthStorage {
  get(key: string): Promise<string | null>;
  set(key: string, value: string): Promise<void>;
  remove(key: string): Promise<void>;
}

export interface AuthDeps {
  fetch: typeof fetch;
  /** Epoch seconds. */
  now: () => number;
  /** Runs the interactive flow and resolves with the final redirect URL. */
  launchWebAuthFlow: (url: string) => Promise<string>;
  storage: AuthStorage;
  redirectUri: () => string;
  randomBytes: (byteLength: number) => Uint8Array;
  credentials: () => Credentials;
}

export class NotSignedInError extends Error {
  constructor(message = "Not signed in") {
    super(message);
    this.name = "NotSignedInError";
  }
}

export function chromeAuthDeps(): AuthDeps {
  return {
    fetch: globalThis.fetch.bind(globalThis),
    now: () => Date.now() / 1000,
    launchWebAuthFlow: (url) =>
      chrome.identity.launchWebAuthFlow({ url, interactive: true }).then((r) => {
        if (!r) throw new Error("Sign-in was cancelled");
        return r;
      }),
    storage: {
      get: async (key) => (await chrome.storage.local.get(key))[key] ?? null,
      set: async (key, value) => chrome.storage.local.set({ [key]: value }),
      remove: async (key) => chrome.storage.local.remove(key),
    },
    redirectUri: () => chrome.identity.getRedirectURL(),
    randomBytes: (n) => crypto.getRandomValues(new Uint8Array(n)),
    credentials: bundledCredentials,
  };
}

/**
 * Reads identity claims out of a Firebase ID token **for display only**.
 *
 * The signature is not verified here and must not be trusted for authorization
 * — the cloud function verifies the token server-side on every request. This
 * decode exists so the popup can show which account is signed in without an
 * extra round trip.
 */
function readIdentityClaims(idToken: string): { uid: string; email: string | null } {
  const payload = idToken.split(".")[1];
  if (!payload) throw new Error("Malformed ID token");
  const json = JSON.parse(
    atob(payload.replace(/-/g, "+").replace(/_/g, "/")),
  ) as Record<string, unknown>;
  const uid = json["user_id"] ?? json["sub"];
  if (typeof uid !== "string" || uid === "") {
    throw new Error("ID token carries no user id");
  }
  const email = json["email"];
  return { uid, email: typeof email === "string" ? email : null };
}

function toHex(bytes: Uint8Array): string {
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export class AuthSession {
  private cached: AuthState | null = null;
  /** Collapses concurrent refreshes so a popup and the service worker waking
   * at the same moment don't each burn a refresh round trip. */
  private inFlight: Promise<AuthState> | null = null;

  constructor(private readonly deps: AuthDeps) {}

  /** Runs the interactive Google flow and exchanges the result for a Firebase
   * session. Replaces any existing session. */
  async signIn(): Promise<AuthState> {
    const { firebaseApiKey, oauthClientId } = this.deps.credentials();
    const redirectUri = this.deps.redirectUri();
    const nonce = toHex(this.deps.randomBytes(16));

    const authUrl = new URL(GOOGLE_AUTH_URL);
    authUrl.searchParams.set("client_id", oauthClientId);
    authUrl.searchParams.set("response_type", "id_token");
    authUrl.searchParams.set("redirect_uri", redirectUri);
    authUrl.searchParams.set("scope", "openid email profile");
    authUrl.searchParams.set("nonce", nonce);

    const redirect = await this.deps.launchWebAuthFlow(authUrl.toString());
    const fragment = new URLSearchParams(new URL(redirect).hash.slice(1));
    const googleIdToken = fragment.get("id_token");
    if (!googleIdToken) {
      throw new Error("Sign-in returned no id_token");
    }
    if (fragment.get("nonce") !== nonce) {
      throw new Error("Sign-in nonce did not match the request");
    }

    const response = await this.deps.fetch(
      `${SIGN_IN_WITH_IDP_URL}?key=${encodeURIComponent(firebaseApiKey)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          postBody: `id_token=${googleIdToken}&providerId=google.com`,
          requestUri: redirectUri,
          returnSecureToken: true,
        }),
      },
    );
    if (!response.ok) {
      throw new Error(`Sign-in failed: ${await describeError(response)}`);
    }
    const body = (await response.json()) as {
      idToken: string;
      refreshToken: string;
      expiresIn: string;
    };
    return this.adopt(body.idToken, body.refreshToken, body.expiresIn);
  }

  /** Forgets the session on this device. The account itself is untouched. */
  async signOut(): Promise<void> {
    this.cached = null;
    this.inFlight = null;
    await this.deps.storage.remove(REFRESH_TOKEN_STORAGE_KEY);
  }

  /**
   * A valid ID token, refreshing transparently when the cached one is within
   * {@link REFRESH_BUFFER_SECONDS} of expiry.
   *
   * Throws {@link NotSignedInError} when there is no usable credential — the
   * caller's cue to show signed-out, not to retry.
   */
  async getIdToken(options: { forceRefresh?: boolean } = {}): Promise<string> {
    const cached = this.cached;
    if (
      !options.forceRefresh &&
      cached &&
      cached.expiresAt - this.deps.now() > REFRESH_BUFFER_SECONDS
    ) {
      return cached.idToken;
    }
    return (await this.refresh()).idToken;
  }

  /** Who is signed in, or `{ signedIn: false }`. Never throws: a rejected or
   * absent credential is a signed-out state, not an error to surface. */
  async whoami(): Promise<WhoAmI> {
    try {
      await this.getIdToken();
    } catch {
      return { signedIn: false, uid: null, email: null };
    }
    const state = this.cached;
    if (!state) return { signedIn: false, uid: null, email: null };
    return { signedIn: true, uid: state.uid, email: state.email };
  }

  private async refresh(): Promise<AuthState> {
    this.inFlight ??= this.doRefresh().finally(() => {
      this.inFlight = null;
    });
    return this.inFlight;
  }

  private async doRefresh(): Promise<AuthState> {
    const refreshToken =
      this.cached?.refreshToken ??
      (await this.deps.storage.get(REFRESH_TOKEN_STORAGE_KEY));
    if (!refreshToken) {
      throw new NotSignedInError();
    }

    const { firebaseApiKey } = this.deps.credentials();
    const response = await this.deps.fetch(
      `${SECURE_TOKEN_URL}?key=${encodeURIComponent(firebaseApiKey)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({
          grant_type: "refresh_token",
          refresh_token: refreshToken,
        }).toString(),
      },
    );

    if (!response.ok) {
      // A rejected refresh token is terminal — it will not start working
      // again. Drop it so the user sees signed-out and can sign in again,
      // rather than a session that silently fails every request.
      await this.signOut();
      throw new NotSignedInError(
        `Session expired, signed out: ${await describeError(response)}`,
      );
    }

    const body = (await response.json()) as {
      id_token: string;
      refresh_token: string;
      expires_in: string;
    };
    return this.adopt(body.id_token, body.refresh_token, body.expires_in);
  }

  /** Cache a fresh token pair in memory and persist only the refresh token. */
  private async adopt(
    idToken: string,
    refreshToken: string,
    expiresIn: string,
  ): Promise<AuthState> {
    const { uid, email } = readIdentityClaims(idToken);
    const state: AuthState = {
      idToken,
      refreshToken,
      expiresAt: this.deps.now() + (Number(expiresIn) || 3600),
      uid,
      email,
    };
    this.cached = state;
    await this.deps.storage.set(REFRESH_TOKEN_STORAGE_KEY, refreshToken);
    return state;
  }
}

async function describeError(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { error?: { message?: string } };
    return body.error?.message ?? `HTTP ${response.status}`;
  } catch {
    return `HTTP ${response.status}`;
  }
}
