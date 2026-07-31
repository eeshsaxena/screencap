import { mkdtemp, readFile, rm, writeFile, mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  FIREBASE_API_KEY_ENV,
  OAUTH_CLIENT_ID_ENV,
  OUTPUT_RELATIVE_PATH,
  generateProvisioned,
} from "./generate-provisioned.mjs";

const BOTH = {
  [FIREBASE_API_KEY_ENV]: "AIzaSyExampleRealLookingKey",
  [OAUTH_CLIENT_ID_ENV]: "1234-example.apps.googleusercontent.com",
};

let distDir;

beforeEach(async () => {
  distDir = await mkdtemp(join(tmpdir(), "screencap-provisioned-"));
});

afterEach(async () => {
  await rm(distDir, { recursive: true, force: true });
});

async function readOutput() {
  return readFile(join(distDir, OUTPUT_RELATIVE_PATH), "utf8");
}

/** Stand in for what `tsc` emitted, so "was it replaced?" is answerable. */
async function seedCompiledPlaceholder() {
  await mkdir(join(distDir, "auth"), { recursive: true });
  await writeFile(
    join(distDir, OUTPUT_RELATIVE_PATH),
    'export const FIREBASE_API_KEY = "UNPROVISIONED_FIREBASE_API_KEY";\n',
    "utf8",
  );
}

describe("generateProvisioned", () => {
  it("writes both values when given both", async () => {
    const result = await generateProvisioned({ env: BOTH, distDir });

    expect(result.ok).toBe(true);
    expect(result.missing).toEqual([]);
    const written = await readOutput();
    expect(written).toContain(`"${BOTH[FIREBASE_API_KEY_ENV]}"`);
    expect(written).toContain(`"${BOTH[OAUTH_CLIENT_ID_ENV]}"`);
  });

  it("replaces what the compiler emitted rather than writing alongside it", async () => {
    await seedCompiledPlaceholder();

    await generateProvisioned({ env: BOTH, distDir });

    const written = await readOutput();
    expect(written).not.toContain("UNPROVISIONED_FIREBASE_API_KEY");
  });

  it("fails and writes nothing when only the Firebase key is set", async () => {
    const result = await generateProvisioned({
      env: { [FIREBASE_API_KEY_ENV]: BOTH[FIREBASE_API_KEY_ENV] },
      distDir,
    });

    expect(result.ok).toBe(false);
    expect(result.missing).toEqual([OAUTH_CLIENT_ID_ENV]);
    await expect(readOutput()).rejects.toThrow();
  });

  it("fails and writes nothing when only the client id is set", async () => {
    const result = await generateProvisioned({
      env: { [OAUTH_CLIENT_ID_ENV]: BOTH[OAUTH_CLIENT_ID_ENV] },
      distDir,
    });

    expect(result.ok).toBe(false);
    expect(result.missing).toEqual([FIREBASE_API_KEY_ENV]);
    await expect(readOutput()).rejects.toThrow();
  });

  it("treats a whitespace-only value as absent", async () => {
    const result = await generateProvisioned({
      env: { ...BOTH, [OAUTH_CLIENT_ID_ENV]: "   " },
      distDir,
    });

    expect(result.ok).toBe(false);
    expect(result.missing).toEqual([OAUTH_CLIENT_ID_ENV]);
  });

  it("clears a previous run's values before failing", async () => {
    await generateProvisioned({ env: BOTH, distDir });

    const result = await generateProvisioned({ env: {}, distDir });

    // Without the delete-first ordering the prior run's real credentials would
    // survive in a gitignored directory and be packaged by the next run that
    // skipped the generator.
    expect(result.ok).toBe(false);
    await expect(readOutput()).rejects.toThrow();
  });

  it("succeeds when the build output directory does not exist yet", async () => {
    const absent = join(distDir, "never-built");

    const result = await generateProvisioned({ env: BOTH, distDir: absent });

    expect(result.ok).toBe(true);
  });
});
