/**
 * The release guard: refuses to let an un-provisioned build be packaged.
 *
 * Mirrors `screencap _auth-config-check` in the Python layer, including the
 * decision that matters most — it checks the **built output**, not the source.
 * That catches both "forgot to run the generator" and "the generator ran but
 * its output did not reach the package", and it does so by calling the very
 * function a user's first sign-in calls, so the guard cannot drift from the
 * runtime behavior it is protecting.
 *
 * Enforcement is split by command rather than by an environment marker. The
 * Python CLI needs `SCREENCAP_RELEASE_BUILD` because the same binary is built
 * on PRs and on tags; here the ordinary build stays green with placeholders and
 * only packaging — which exists solely to produce something distributable —
 * runs this. There is no marker to forget.
 *
 * Usage (from `extension/`, after a build and the generator):
 *
 *     node scripts/verify-provisioned.mjs
 */
import { access } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL, fileURLToPath } from "node:url";

const EXTENSION_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");

/** Relative to the build output root; mirrors `src/auth/credentials.ts`. */
export const CREDENTIALS_RELATIVE_PATH = "auth/credentials.js";

/**
 * Check a built directory, without deciding what to do about the answer.
 *
 * Split from the CLI wrapper so the failure paths can be exercised against
 * fixture directories rather than by spawning a subprocess to read an exit
 * code.
 *
 * @param {string} distDir
 * @returns {Promise<{ ok: boolean, reason: string | null }>}
 */
export async function verifyProvisioned(distDir) {
  const credentialsPath = resolve(join(distDir, CREDENTIALS_RELATIVE_PATH));

  try {
    await access(credentialsPath);
  } catch {
    // An absent build is not a clean check. Reporting success here would mean a
    // packaging run that silently skipped the build passes the guard.
    return {
      ok: false,
      reason: `no built credentials module at ${credentialsPath} — run the build before the guard`,
    };
  }

  let bundledCredentials;
  try {
    ({ bundledCredentials } = await import(pathToFileURL(credentialsPath).href));
  } catch (error) {
    return { ok: false, reason: `could not load the built credentials module: ${describe(error)}` };
  }

  if (typeof bundledCredentials !== "function") {
    return { ok: false, reason: "the built credentials module exports no bundledCredentials()" };
  }

  try {
    bundledCredentials();
  } catch (error) {
    return { ok: false, reason: describe(error) };
  }

  return { ok: true, reason: null };
}

function describe(error) {
  return error instanceof Error ? error.message : String(error);
}

/** True when this module is being run as a script rather than imported. */
function isMain() {
  return process.argv[1] !== undefined && import.meta.url === `file://${process.argv[1]}`;
}

if (isMain()) {
  const { ok, reason } = await verifyProvisioned(join(EXTENSION_ROOT, "dist"));

  if (!ok) {
    console.log(
      `::error::verify-provisioned.mjs: refusing to package an un-provisioned build — ${reason}`,
    );
    console.error(
      `ERROR: release guard failed — ${reason}\n` +
        "Run the build, then scripts/generate-provisioned.mjs with " +
        "SCREENCAP_FIREBASE_API_KEY + SCREENCAP_EXTENSION_OAUTH_CLIENT_ID set " +
        "(see docs/runbooks/cloud-auth-setup.md).",
    );
    process.exit(1);
  }

  console.log("verify-provisioned: built credentials are provisioned.");
}
