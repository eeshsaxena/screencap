import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";
import { describe, expect, it } from "vitest";

/**
 * The manifest's pinned `key` is what gives the extension one id across every
 * machine and install path. That id is not cosmetic: the OAuth client's
 * authorized redirect URI is derived from it, so changing or dropping the key
 * silently breaks sign-in with a `redirect_uri_mismatch` at Google rather than
 * anything that looks like a manifest problem.
 */
const manifest = JSON.parse(
  readFileSync(fileURLToPath(new URL("../../manifest.json", import.meta.url)), "utf8"),
) as Record<string, unknown>;

/**
 * Chrome's derivation: SHA-256 the DER public key, take the first 16 bytes, and
 * map each hex digit onto `a`-`p`. Recomputing it here is what lets the test
 * assert the *specific* provisioned id rather than merely that some key exists.
 */
function extensionIdFromKey(base64Key: string): string {
  const digest = createHash("sha256").update(Buffer.from(base64Key, "base64")).digest("hex");
  return [...digest.slice(0, 32)].map((c) => String.fromCharCode(parseInt(c, 16) + 97)).join("");
}

describe("manifest key", () => {
  it("pins a key so the extension id is stable across installs", () => {
    expect(typeof manifest.key).toBe("string");
    expect(manifest.key as string).not.toBe("");
  });

  it("still derives the id the OAuth client was provisioned against", () => {
    // If this fails, the key was regenerated or edited. The redirect URI in the
    // Google Cloud console no longer matches and sign-in will fail — provision
    // the new id before changing this expectation.
    expect(extensionIdFromKey(manifest.key as string)).toBe("bbbaflacfepheepgehcfjjmlmgooeajl");
  });

  it("leaves the permissions the auth path depends on intact", () => {
    // Pinning the key is a one-field addition; it must not disturb the
    // identity/storage grants sign-in and credential persistence rely on.
    //
    // Containment rather than equality: capture added its own permissions to
    // this same array, and two tests asserting the whole list would fight over
    // it every time either half grows. This one guards what auth needs; the
    // exhaustive pin — the one that catches a permission nothing exercises —
    // lives in `../permissions/allowlist.test.ts`.
    expect(manifest.permissions).toEqual(
      expect.arrayContaining(["identity", "storage"]),
    );
  });
});
