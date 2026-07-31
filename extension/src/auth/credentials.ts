/**
 * Provisioned credentials, following the `_provisioned` pattern in
 * `src/screencap/auth.py`: values are meant to be injected at build time rather
 * than hardcoded per developer, and an un-provisioned build resolves to a
 * placeholder that `isPlaceholderCredential` recognizes.
 *
 * The injected values live in `./provisioned.ts`, whose compiled output
 * `scripts/generate-provisioned.mjs` overwrites during packaging. The
 * checked-in values there are placeholders and `bundledCredentials` throws on
 * them, so an un-provisioned build fails loudly rather than shipping broken —
 * at the release guard when packaging, or at first sign-in if one is ever
 * loaded unpacked.
 *
 * Neither value is secret: a Firebase web API key and an OAuth client id are
 * public identifiers. Unlike the CLI's Google **Desktop** OAuth client, the
 * extension uses a **Web** client through `chrome.identity.launchWebAuthFlow`
 * requesting an OIDC `id_token` directly, so there is no token-endpoint call
 * and no client secret to bundle at all.
 */

import {
  FIREBASE_API_KEY as INJECTED_FIREBASE_API_KEY,
  OAUTH_CLIENT_ID as INJECTED_OAUTH_CLIENT_ID,
} from "./provisioned.js";

/**
 * The sentinels {@link isPlaceholderCredential} compares against. Written out
 * literally rather than imported from `./provisioned.ts`: that module's
 * compiled output is replaced wholesale at build time, so it cannot be the
 * source of truth for what "un-provisioned" means. `credentials.test.ts` pins
 * the two copies together.
 */
export const DEFAULT_FIREBASE_API_KEY = "UNPROVISIONED_FIREBASE_API_KEY";
export const DEFAULT_OAUTH_CLIENT_ID = "UNPROVISIONED_OAUTH_CLIENT_ID";

/** Resolved from the build-time injection point. See `./provisioned.ts`. */
export const PROVISIONED = {
  firebaseApiKey: INJECTED_FIREBASE_API_KEY,
  oauthClientId: INJECTED_OAUTH_CLIENT_ID,
} as const;

/** True when a resolved credential is still an un-provisioned placeholder. */
export function isPlaceholderCredential(value: string): boolean {
  return value === DEFAULT_FIREBASE_API_KEY || value === DEFAULT_OAUTH_CLIENT_ID;
}

export interface Credentials {
  firebaseApiKey: string;
  oauthClientId: string;
}

/**
 * Resolve credentials as a shipped build will for an end user.
 *
 * Throws when either value is still a placeholder: signing in with one would
 * fail at Google or Firebase with an opaque error, and a clear failure here is
 * the difference between "this build was never provisioned" and "sign-in is
 * broken".
 */
export function bundledCredentials(): Credentials {
  const { firebaseApiKey, oauthClientId } = PROVISIONED;
  if (isPlaceholderCredential(firebaseApiKey) || isPlaceholderCredential(oauthClientId)) {
    throw new Error(
      "Screencap extension was built without provisioned credentials. " +
        "Inject FIREBASE_API_KEY and OAUTH_CLIENT_ID at build time.",
    );
  }
  return { firebaseApiKey, oauthClientId };
}
