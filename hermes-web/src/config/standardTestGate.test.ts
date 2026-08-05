// @vitest-environment node

import { readFileSync } from "node:fs";

describe("standard test gate", () => {
  it("runs every application, registry, bundle-budget, and Cloud runtime suite exactly once", () => {
    const packageJson = JSON.parse(
      readFileSync(new URL("../../package.json", import.meta.url), "utf8"),
    ) as { scripts: Record<string, string> };

    expect(packageJson.scripts.test).toBe(
      "npm run test:app && npm run test:iana-registry-parser && npm run test:bundle-budget && npm run test:cloud-preview-runtime && npm run test:real-full-chain-cli",
    );
    expect(packageJson.scripts["test:app"]).toBe(
      "vitest run --config vitest.config.ts",
    );
    expect(packageJson.scripts["test:cloud-preview-runtime"]).toBe(
      "vitest run --config vitest.runtime.config.mjs",
    );
    expect(packageJson.scripts["test:iana-registry-parser"]).toBe(
      "vitest run --config vitest.iana.config.mjs",
    );
    expect(packageJson.scripts["test:bundle-budget"]).toBe(
      "node --test tests/production-bundle-budget.test.mjs",
    );
    expect(packageJson.scripts["test:real-full-chain-cli"]).toBe(
      "node --test tests/real-full-chain-gate.test.mjs",
    );
  });

  it("uses the production ticket provider in the real Cloud integration gate", () => {
    const source = readFileSync(
      new URL("../integration/realCloudAuth.integration.test.ts", import.meta.url),
      "utf8",
    );

    expect(source).toContain("new HttpTicketProvider(");
    expect(source).toContain("ticketProvider.mint(");
    expect(source).not.toMatch(/jar\.fetch\([^\n]*api\/auth\/ws-ticket/);
    expect(source).not.toContain("Promise.allSettled([");
  });
});
