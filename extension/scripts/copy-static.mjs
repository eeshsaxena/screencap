/**
 * Copies non-TypeScript assets into `dist/` so the built directory is a
 * loadable unpacked extension: `tsc` emits only JS, but Chrome also needs the
 * manifest at the root and the popup's HTML beside its compiled script.
 */
import { copyFile, mkdir } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const assets = [
  ["manifest.json", "dist/manifest.json"],
  ["src/popup/popup.html", "dist/popup/popup.html"],
];

for (const [from, to] of assets) {
  const target = join(root, to);
  await mkdir(dirname(target), { recursive: true });
  await copyFile(join(root, from), target);
}
