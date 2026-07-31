/**
 * Archives the built extension into a loadable, distributable zip.
 *
 * Runs last in `npm run package`, after the build, the credential injection,
 * and the release guard — so reaching this script at all means the output has
 * already been proven provisioned. Nothing here re-checks that; the guard is
 * the single place that decision is made.
 *
 * Uses the platform `zip` rather than a dependency: the extension's toolchain
 * is deliberately thin (three devDependencies), and adding an archiver to
 * produce one file at release time would not earn its keep.
 */
import { execFile } from "node:child_process";
import { readFile, rm, stat } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const EXTENSION_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");

const distDir = join(EXTENSION_ROOT, "dist");

try {
  const stats = await stat(distDir);
  if (!stats.isDirectory()) throw new Error("not a directory");
} catch {
  console.error(`ERROR: no build output at ${distDir} — run the build first.`);
  process.exit(1);
}

const { version } = JSON.parse(await readFile(join(EXTENSION_ROOT, "package.json"), "utf8"));
const archiveName = `screencap-extension-${version}.zip`;
const archivePath = join(EXTENSION_ROOT, archiveName);

// Replace rather than update: `zip` merges into an existing archive by default,
// which would keep files a later build no longer emits.
await rm(archivePath, { force: true });

// `-r .` from inside dist/ so the manifest lands at the archive root, which is
// where Chrome and the Web Store expect it.
await execFileAsync("zip", ["-q", "-r", archivePath, "."], { cwd: distDir });

console.log(`Wrote ${archivePath}`);
