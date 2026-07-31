import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    // The build scripts are plain `.mjs` (as `copy-static.mjs` already was), so
    // they sit outside the TypeScript surface `tsconfig.json` covers — its
    // `rootDir: "src"` cannot take a second root. Collecting their `.mjs` tests
    // here is what keeps them from being silently uncollected: a test the
    // runner never finds reports as a green suite, which for the release guard
    // is the worst possible failure.
    include: ["src/**/*.test.ts", "scripts/**/*.test.mjs"],
  },
});
