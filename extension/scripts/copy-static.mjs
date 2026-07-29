/**
 * Copies non-TypeScript assets into `dist/` so the built directory is a
 * loadable unpacked extension: `tsc` emits only JS, but Chrome also needs the
 * manifest at the root and the popup's HTML beside its compiled script.
 */
import { cp } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const assets = [
  ["manifest.json", "dist/manifest.json"],
  ["src/popup/popup.html", "dist/popup/popup.html"],
];

// `cp` creates missing destination directories itself, so no separate mkdir.
await Promise.all(assets.map(([from, to]) => cp(join(root, from), join(root, to))));
