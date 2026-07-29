/**
 * Client-side Firebase auth for the extension: `signIn` / `signOut` /
 * `whoami` / `getIdToken`.
 *
 * Mirrors `src/screencap/auth.py` in shape, lifetime, and — importantly — in
 * its failure taxonomy. Sign-in yields a Firebase **ID token** (~1h, memory
 * only) and a **refresh token** (long-lived, the durable credential), and the
 * ID token is refreshed transparently a few minutes before expiry. The two
 * surfaces deliberately do NOT share a credential store: the CLI's refresh
 * token lives in the macOS Keychain behind a Team-signed entitlement group that
 * an extension cannot read. Matching the *account* is the requirement, not
 * sharing the credential.
 *
 * The OAuth flow differs from the CLI's. The CLI uses a Google **Desktop**
 * client over loopback + PKCE, which requires a bundled client secret at the
 * token endpoint. The extension uses a **Web** client through
 * `chrome.identity.launchWebAuthFlow` requesting an OIDC `id_token` directly,
 * so there is no code exchange and no secret to bundle. Per OIDC, the nonce
 * that binds the response to the request is a **claim inside the returned ID
 * token** — not a redirect parameter — so that is where it is verified.
 *
 * ## State invariant
 *
 * Three fields coordinate: `cached` (the live session), `inFlight` (the
 * single in-progress refresh), and `generation` (which session those belong
 * to). The rule is: **a network result may only be adopted if the generation
 * it started under is still current.** `signIn` and `signOut` each bump the
 * generation, so any result already in flight when they run is abandoned
 * rather than written. Storage is the rotation source of truth and is
 * preferred over `cached` when refreshing.
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

/** Bound every identity request, matching the CLI's `timeout=30`. Without it a
 * hung connection would latch `inFlight` forever and every later caller would
 * await a promise that never settles. */
export const FETCH_TIMEOUT_MS = 30_000;

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
  /** A credential is stored but could not be refreshed right now (offline, or
   * a transient upstream failure). Mirrors `whoami()`'s `stale` in
   * `src/screencap/auth.py`: signed in, identity currently unknown. */
  stale?: boolean;
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

/** No usable credential: nothing stored, or the stored one was rejected as
 * dead. The caller should show signed-out and offer sign-in. */
export class NotSignedInError extends Error {
  constructor(message = "Not signed in") {
    super(message);
    this.name = "NotSignedInError";
  }
}

/**
 * The credential could not be exercised right now — offline, timeout, rate
 * limit, or an upstream outage. **The stored credential is untouched and still
 * valid.** Distinguishing this from {@link NotSignedInError} is what stops a
 * dropped connection from signing a user out; `src/screencap/auth.py:723-731`
 * makes the same split and documents it as load-bearing.
 */
export class AuthUnavailableError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AuthUnavailableError";
  }
}

/** Sign-out could not be made durable, so the credential may still be on disk.
 * Never reported as a clean sign-out — `auth.py`'s `logout` takes the same
 * position: do not claim a success you cannot prove. */
export class SignOutIncompleteError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SignOutIncompleteError";
  }
}

export const SIGNED_OUT: WhoAmI = { signedIn: false, uid: null, email: null };

export function toWhoAmI(state: AuthState): WhoAmI {
  return { signedIn: true, uid: state.uid, email: state.email };
}

export function chromeAuthDeps(): AuthDeps {
  return {
    fetch: globalThis.fetch.bind(globalThis),
    now: () => Date.now() / 1000,
    launchWebAuthFlow: async (url) => {
      // Typed as optionally-undefined: Chrome resolves without a redirect on
      // some cancellation paths rather than rejecting, and reading the URL of
      // `undefined` would surface as an opaque TypeError.
      const redirect = await chrome.identity.launchWebAuthFlow({
        url,
        interactive: true,
      });
      if (!redirect) throw new Error("Sign-in was cancelled");
      return redirect;
    },
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
 * Decode a JWT payload as UTF-8 JSON.
 *
 * `atob` yields Latin-1, which mangles any non-ASCII claim (an accented name
 * or an internationalized email), so the bytes are re-decoded as UTF-8.
 * Signature is NOT verified — the cloud function verifies server-side on every
 * request. Callers must treat the result as display data, never as an
 * authorization decision.
 */
function decodeJwtPayload(token: string): Record<string, unknown> {
  const segments = token.split(".");
  if (segments.length < 2 || !segments[1]) {
    throw new Error("Malformed ID token");
  }
  const binary = atob(segments[1].replace(/-/g, "+").replace(/_/g, "/"));
  const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
  const parsed: unknown = JSON.parse(new TextDecoder().decode(bytes));
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("ID token payload is not an object");
  }
  return parsed as Record<string, unknown>;
}

function readIdentityClaims(idToken: string): { uid: string; email: string | null } {
  const claims = decodeJwtPayload(idToken);
  const uid = claims["user_id"] ?? claims["sub"];
  if (typeof uid !== "string" || uid === "") {
    throw new Error("ID token carries no user id");
  }
  const email = claims["email"];
  return { uid, email: typeof email === "string" ? email : null };
}

/**
 * Seconds until expiry, defaulting only when the field is genuinely absent.
 * Treating `"0"` as falsy and substituting an hour would cache a token the
 * server declared already dead.
 */
function expiresInSeconds(raw: string | undefined): number {
  if (raw === undefined || raw === "") return 3600;
  const parsed = Number(raw);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : 3600;
}

function toHex(bytes: Uint8Array): string {
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function isAbort(error: unknown): boolean {
  return error instanceof Error && (error.name === "AbortError" || error.name === "TimeoutError");
}

export class AuthSession {
  private cached: AuthState | null = null;
  /** Collapses concurrent refreshes so a popup and the service worker waking
   * at the same moment don't each burn a refresh round trip. */
  private inFlight: Promise<AuthState> | null = null;
  /** See the state invariant in the module docstring. */
  private generation = 0;

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
    const googleIdToken = extractVerifiedIdToken(redirect, nonce, oauthClientId);

    // The interactive flow can sit on the consent screen for minutes. Claim the
    // session only now, so a sign-out during that window invalidates this
    // sign-in rather than the other way round.
    const generation = ++this.generation;

    const body = await this.postJson<{
      idToken: string;
      refreshToken: string;
      expiresIn?: string;
    }>(
      `${SIGN_IN_WITH_IDP_URL}?key=${encodeURIComponent(firebaseApiKey)}`,
      {
        postBody: new URLSearchParams({
          id_token: googleIdToken,
          providerId: "google.com",
        }).toString(),
        requestUri: redirectUri,
        returnSecureToken: true,
      },
      "Sign-in",
    );

    return this.adopt(generation, body.idToken, body.refreshToken, body.expiresIn);
  }

  /**
   * Forgets the session on this device. The account itself is untouched.
   *
   * Durability first: in-memory state is cleared immediately, but a failure to
   * remove the stored credential is surfaced as
   * {@link SignOutIncompleteError} rather than swallowed — otherwise the popup
   * would render signed-out while the refresh token sat on disk, ready for the
   * next service-worker revival to restore.
   */
  async signOut(): Promise<void> {
    this.generation += 1;
    this.cached = null;
    this.inFlight = null;
    try {
      await this.deps.storage.remove(REFRESH_TOKEN_STORAGE_KEY);
    } catch {
      try {
        await this.deps.storage.remove(REFRESH_TOKEN_STORAGE_KEY);
      } catch (retryError) {
        throw new SignOutIncompleteError(
          `Signed out on this device, but the stored credential could not be removed: ${describe(retryError)}`,
        );
      }
    }
  }

  /**
   * A valid ID token, refreshing transparently when the cached one is within
   * {@link REFRESH_BUFFER_SECONDS} of expiry.
   *
   * Throws {@link NotSignedInError} when there is no usable credential, or
   * {@link AuthUnavailableError} when one exists but cannot be exercised now.
   */
  async getIdToken(): Promise<string> {
    const cached = this.cached;
    if (cached && cached.expiresAt - this.deps.now() > REFRESH_BUFFER_SECONDS) {
      return cached.idToken;
    }
    return (await this.refresh()).idToken;
  }

  /**
   * Who is signed in. Never throws. Reports three states, not two: signed out,
   * signed in, and signed in but `stale` — the last meaning a credential is
   * stored but unusable right now, so going offline does not read as being
   * logged out.
   */
  async whoami(): Promise<WhoAmI> {
    try {
      await this.getIdToken();
    } catch (error) {
      if (error instanceof AuthUnavailableError) {
        const stored = await this.deps.storage.get(REFRESH_TOKEN_STORAGE_KEY);
        if (stored) return { signedIn: true, uid: null, email: null, stale: true };
      }
      return SIGNED_OUT;
    }
    return this.cached ? toWhoAmI(this.cached) : SIGNED_OUT;
  }

  private async refresh(): Promise<AuthState> {
    if (this.inFlight) return this.inFlight;
    const pending = this.doRefresh().finally(() => {
      // Only release the latch if it is still ours: a sign-out (or a newer
      // refresh) may have replaced it, and clearing that would un-latch a
      // successor and let duplicate refreshes through.
      if (this.inFlight === pending) this.inFlight = null;
    });
    this.inFlight = pending;
    return pending;
  }

  private async doRefresh(): Promise<AuthState> {
    const generation = this.generation;
    // Storage is the rotation source of truth: another context may have
    // rotated the token while this one held a stale copy, and sending the
    // stale one would force a needless sign-out. Mirrors `_ensure_fresh`.
    const stored = await this.deps.storage.get(REFRESH_TOKEN_STORAGE_KEY);
    const refreshToken = stored ?? this.cached?.refreshToken ?? null;
    if (!refreshToken) {
      throw new NotSignedInError();
    }

    try {
      return await this.exchangeRefreshToken(generation, refreshToken);
    } catch (error) {
      if (!(error instanceof NotSignedInError)) throw error;
      // The token was rejected as dead. Another context may have rotated it
      // mid-flight; reload once and retry only if it actually changed, before
      // discarding what could be a freshly-rotated valid credential.
      const latest = await this.deps.storage.get(REFRESH_TOKEN_STORAGE_KEY);
      if (latest && latest !== refreshToken) {
        return this.exchangeRefreshToken(generation, latest);
      }
      // Genuinely dead — drop it, but only if this session is still current.
      if (generation === this.generation) {
        await this.signOut().catch(() => undefined);
      }
      throw error;
    }
  }

  private async exchangeRefreshToken(
    generation: number,
    refreshToken: string,
  ): Promise<AuthState> {
    const { firebaseApiKey } = this.deps.credentials();
    const url = `${SECURE_TOKEN_URL}?key=${encodeURIComponent(firebaseApiKey)}`;
    let response: Response;
    try {
      response = await this.deps.fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({
          grant_type: "refresh_token",
          refresh_token: refreshToken,
        }).toString(),
        signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
      });
    } catch (error) {
      throw new AuthUnavailableError(
        isAbort(error)
          ? "Token refresh timed out."
          : `Token refresh failed (network): ${describe(error)}`,
      );
    }

    if (response.status === 400 || response.status === 401) {
      // Only these mean the credential itself is dead.
      throw new NotSignedInError(
        `Your session has expired: ${await describeError(response)}`,
      );
    }
    if (!response.ok) {
      // 429 / 5xx are transient. The refresh token is still valid — surface a
      // retryable error and leave storage alone.
      throw new AuthUnavailableError(
        `Token refresh temporarily failed (HTTP ${response.status}).`,
      );
    }

    const body = (await response.json()) as {
      id_token: string;
      refresh_token?: string;
      expires_in?: string;
    };
    return this.adopt(
      generation,
      body.id_token,
      body.refresh_token ?? refreshToken,
      body.expires_in,
    );
  }

  private async postJson<T>(
    url: string,
    payload: unknown,
    label: string,
  ): Promise<T> {
    let response: Response;
    try {
      response = await this.deps.fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
      });
    } catch (error) {
      throw new AuthUnavailableError(
        isAbort(error)
          ? `${label} timed out.`
          : `${label} failed (network): ${describe(error)}`,
      );
    }
    if (!response.ok) {
      throw new Error(`${label} failed: ${await describeError(response)}`);
    }
    return (await response.json()) as T;
  }

  /**
   * Cache a fresh token pair in memory and persist only the refresh token.
   *
   * Refuses to write when `generation` no longer matches: the session was
   * deliberately changed while this token was in flight, so adopting it would
   * resurrect a session the user ended or overwrite a newer sign-in.
   */
  private async adopt(
    generation: number,
    idToken: string,
    refreshToken: string,
    expiresIn: string | undefined,
  ): Promise<AuthState> {
    if (generation !== this.generation) {
      throw new NotSignedInError("Session changed while signing in");
    }
    const { uid, email } = readIdentityClaims(idToken);
    const state: AuthState = {
      idToken,
      refreshToken,
      expiresAt: this.deps.now() + expiresInSeconds(expiresIn),
      uid,
      email,
    };
    this.cached = state;
    await this.deps.storage.set(REFRESH_TOKEN_STORAGE_KEY, refreshToken);
    return state;
  }
}

/**
 * Pull the ID token out of the OAuth redirect and verify it belongs to this
 * request.
 *
 * The nonce is read from the **signed token claim**, not from a redirect
 * parameter: OIDC defines the nonce as a claim, the provider does not echo it
 * as a fragment field, and a fragment value would in any case be supplied by
 * whoever supplied the token — checking it would prove nothing.
 */
export function extractVerifiedIdToken(
  redirectUrl: string,
  expectedNonce: string,
  expectedClientId: string,
): string {
  const fragment = new URLSearchParams(new URL(redirectUrl).hash.slice(1));

  // Read the provider's error first; otherwise every declined or misconfigured
  // flow reports the generic "no id_token" message.
  const error = fragment.get("error");
  if (error) {
    const description = fragment.get("error_description");
    throw new Error(`Sign-in failed: ${description ?? error}`);
  }

  const idToken = fragment.get("id_token");
  if (!idToken) {
    throw new Error("Sign-in returned no id_token");
  }

  const claims = decodeJwtPayload(idToken);
  if (claims["nonce"] !== expectedNonce) {
    throw new Error("Sign-in nonce did not match the request");
  }
  const audience = claims["aud"];
  if (audience !== expectedClientId) {
    throw new Error("Sign-in token was issued for a different client");
  }
  return idToken;
}

function describe(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

async function describeError(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { error?: { message?: string } };
    return body.error?.message ?? `HTTP ${response.status}`;
  } catch {
    return `HTTP ${response.status}`;
  }
}
