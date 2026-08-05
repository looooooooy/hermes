import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/browser",
  testMatch: "**/production-*.spec.ts",
  fullyParallel: false,
  reporter: "line",
  use: {
    baseURL: "http://127.0.0.1:4174",
    headless: false,
    screenshot: "only-on-failure",
    viewport: { width: 390, height: 844 },
  },
  webServer: {
    command: "npm run build && npm run preview -- --host 127.0.0.1 --port 4174 --strictPort",
    url: "http://127.0.0.1:4174",
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
