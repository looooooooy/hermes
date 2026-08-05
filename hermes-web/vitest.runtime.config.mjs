import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["scripts/cloud-preview-runtime.test.mjs"],
    environment: "node",
  },
});
