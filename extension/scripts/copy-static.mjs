/**
 * Copies non-TypeScript assets into `dist/` so the built directory is a
 * loadable unpacked extension: `tsc` emits only JS, but Chrome also needs the
 * manifest at the root and every HTML entry point beside its compiled script.
 *
 * HTML is discovered rather than hand-listed — later units add their own entry
 * points (an offscreen document for recording, for one), and a hardcoded list
 * would silently omit them from the build.
 */
import { cp, glob } from "node:fs/promises";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

const htmlEntryPoints = [];
for await (const entry of glob("src/**/*.html", { cwd: root })) {
  htmlEntryPoints.push([entry, join("dist", relative("src", entry))]);
}

const assets = [["manifest.json", "dist/manifest.json"], ...htmlEntryPoints];

// `cp` creates missing destination directories itself, so no separate mkdir.
await Promise.all(assets.map(([from, to]) => cp(join(root, from), join(root, to))));

console.log(`copied ${assets.length} static asset(s) into dist/`);
