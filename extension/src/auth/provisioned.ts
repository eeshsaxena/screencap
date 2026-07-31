/**
 * The build-time injection point, and nothing else.
 *
 * `scripts/generate-provisioned.mjs` overwrites this module's **compiled**
 * output in `dist/` after `tsc` runs. The checked-in values are the
 * un-provisioned placeholders, which is what makes a plain source checkout
 * typecheck, test, and build without any injection step — the CLI's equivalent
 * (`src/screencap/_provisioned.py`) can be absent because Python catches
 * `ImportError` at runtime, but a missing TypeScript import is a compile error,
 * so the module is committed and its output is replaced rather than created.
 *
 * Two consequences worth knowing before editing:
 *
 * - **The values must stay byte-identical to the `DEFAULT_*` sentinels in
 *   `credentials.ts`.** `isPlaceholderCredential` compares against those, so a
 *   typo here would make an un-provisioned build look provisioned and sail past
 *   the release guard. `credentials.test.ts` pins the equality.
 * - **This only works while `tsc` is the last thing to touch the file.** A
 *   bundler would inline these constants at build time, leaving nothing to
 *   overwrite; adopting one means moving injection into its define step.
 *
 * Neither value is secret — a Firebase web API key and an OAuth client id are
 * public identifiers — so this is configuration plumbing, not secret handling.
 */

export const FIREBASE_API_KEY = "UNPROVISIONED_FIREBASE_API_KEY";
export const OAUTH_CLIENT_ID = "UNPROVISIONED_OAUTH_CLIENT_ID";
