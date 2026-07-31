import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { CREDENTIALS_RELATIVE_PATH, verifyProvisioned } from "./verify-provisioned.mjs";

let distDir;

beforeEach(async () => {
  distDir = await mkdtemp(join(tmpdir(), "screencap-guard-"));
});

afterEach(async () => {
  await rm(distDir, { recursive: true, force: true });
});

/**
 * Stand in for what the build emits. Kept behaviorally equivalent to
 * `src/auth/credentials.ts` rather than importing it: the guard's whole point
 * is that it reads whatever is actually on disk, so the fixture has to be able
 * to disagree with the source.
 *
 * `suffix` makes each fixture a distinct path — Node's ES module cache is
 * process-wide and keyed by URL, so two fixtures at the same path in one run
 * would return the first one's exports.
 */
async function writeBuiltCredentials({ apiKey, clientId, suffix = "" }) {
  const dir = join(distDir, suffix);
  await mkdir(join(dir, "auth"), { recursive: true });
  await writeFile(
    join(dir, CREDENTIALS_RELATIVE_PATH),
    `const PLACEHOLDERS = ["UNPROVISIONED_FIREBASE_API_KEY", "UNPROVISIONED_OAUTH_CLIENT_ID"];
export function bundledCredentials() {
  const firebaseApiKey = ${JSON.stringify(apiKey)};
  const oauthClientId = ${JSON.stringify(clientId)};
  if (PLACEHOLDERS.includes(firebaseApiKey) || PLACEHOLDERS.includes(oauthClientId)) {
    throw new Error("Screencap extension was built without provisioned credentials.");
  }
  return { firebaseApiKey, oauthClientId };
}
`,
    "utf8",
  );
  return dir;
}

describe("verifyProvisioned", () => {
  it("passes a build carrying real credentials", async () => {
    const dir = await writeBuiltCredentials({
      apiKey: "AIzaSyExampleRealLookingKey",
      clientId: "1234-example.apps.googleusercontent.com",
      suffix: "provisioned",
    });

    const result = await verifyProvisioned(dir);

    expect(result.ok).toBe(true);
    expect(result.reason).toBeNull();
  });

  it("rejects a build the generator never touched", async () => {
    const dir = await writeBuiltCredentials({
      apiKey: "UNPROVISIONED_FIREBASE_API_KEY",
      clientId: "UNPROVISIONED_OAUTH_CLIENT_ID",
      suffix: "placeholders",
    });

    const result = await verifyProvisioned(dir);

    expect(result.ok).toBe(false);
    expect(result.reason).toMatch(/provisioned credentials/i);
  });

  it("rejects a build provisioned on only one of the two values", async () => {
    const dir = await writeBuiltCredentials({
      apiKey: "AIzaSyExampleRealLookingKey",
      clientId: "UNPROVISIONED_OAUTH_CLIENT_ID",
      suffix: "half",
    });

    const result = await verifyProvisioned(dir);

    expect(result.ok).toBe(false);
  });

  it("fails rather than passes when there is no build at all", async () => {
    // A missing build must never read as a clean check — that would let a
    // packaging run that skipped the build sail past the guard.
    const result = await verifyProvisioned(join(distDir, "never-built"));

    expect(result.ok).toBe(false);
    expect(result.reason).toMatch(/run the build/i);
  });

  it("fails when the built module exports no bundledCredentials", async () => {
    const dir = join(distDir, "wrong-shape");
    await mkdir(join(dir, "auth"), { recursive: true });
    await writeFile(join(dir, CREDENTIALS_RELATIVE_PATH), "export const nothing = 1;\n", "utf8");

    const result = await verifyProvisioned(dir);

    expect(result.ok).toBe(false);
    expect(result.reason).toMatch(/bundledCredentials/);
  });
});
