import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["scripts/iana-registry-drift.test.mjs"],
    environment: "node",
  },
});
