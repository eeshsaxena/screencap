/**
 * Provisioned credentials, mirroring the `_provisioned` pattern in
 * `src/screencap/auth.py`: values are injected at build time, never hardcoded
 * per developer, and an un-provisioned build resolves to a placeholder that
 * `isPlaceholderCredential` recognizes so a release guard can reject it.
 *
 * Unlike the CLI's Google **Desktop** OAuth client, the extension uses a
 * **Web** client through `chrome.identity.launchWebAuthFlow` requesting an
 * OIDC `id_token` directly. That flow has no token-endpoint call, so no client
 * secret is bundled — one less provisioned secret than the CLI carries.
 */

export const DEFAULT_FIREBASE_API_KEY = "UNPROVISIONED_FIREBASE_API_KEY";
export const DEFAULT_OAUTH_CLIENT_ID = "UNPROVISIONED_OAUTH_CLIENT_ID";

/**
 * Build-time injection point. The release build rewrites this object; the
 * checked-in values are placeholders so an un-provisioned build fails loudly
 * at the release guard rather than silently at a user's first sign-in.
 */
export const PROVISIONED = {
  firebaseApiKey: DEFAULT_FIREBASE_API_KEY,
  oauthClientId: DEFAULT_OAUTH_CLIENT_ID,
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
