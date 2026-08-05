// @vitest-environment node

import { fileURLToPath } from "node:url";
import { resolveConfig } from "vite";
import {
  API_PROXY_CONTEXT,
  AUTH_PROXY_CONTEXT,
  HEALTH_PROXY_CONTEXT,
  WEBSOCKET_PROXY_CONTEXT,
} from "../../config/cloudProxy";

describe("Vite Cloud preview config resolution", () => {
  it("loads cloud mode environment into the ordered preview proxy helper", async () => {
    const previousCloudUrl = process.env.HERMES_WEB_CLOUD_URL;
    process.env.HERMES_WEB_CLOUD_URL = "https://cloud.test.example/configured-hermes///";
    try {
      const root = fileURLToPath(new URL("../../", import.meta.url));
      const configFile = fileURLToPath(new URL("../../vite.config.ts", import.meta.url));
      const config = await resolveConfig({
        root,
        configFile,
        logLevel: "silent",
      }, "serve", "cloud", "production", true);
      const proxy = config.preview.proxy;

      expect(config.mode).toBe("cloud");
      expect(Object.keys(proxy ?? {})).toEqual([
        HEALTH_PROXY_CONTEXT,
        AUTH_PROXY_CONTEXT,
        WEBSOCKET_PROXY_CONTEXT,
        API_PROXY_CONTEXT,
      ]);
      expect(proxy?.[AUTH_PROXY_CONTEXT]).toEqual(expect.objectContaining({
        target: "https://cloud.test.example",
        rewrite: expect.any(Function),
      }));
      const authProxy = proxy?.[AUTH_PROXY_CONTEXT];
      if (typeof authProxy === "object") {
        expect(authProxy.rewrite?.("/auth/password-login")).toBe(
          "/configured-hermes/auth/password-login",
        );
      }
    } finally {
      if (previousCloudUrl === undefined) delete process.env.HERMES_WEB_CLOUD_URL;
      else process.env.HERMES_WEB_CLOUD_URL = previousCloudUrl;
    }
  });

  it.each([
    { label: "fixture dev", command: "serve" as const, mode: "development", isPreview: false },
    { label: "ordinary build", command: "build" as const, mode: "production", isPreview: false },
    { label: "ordinary preview", command: "serve" as const, mode: "production", isPreview: true },
  ])("does not construct the Cloud proxy during $label", async ({ command, mode, isPreview }) => {
    const previousCloudUrl = process.env.HERMES_WEB_CLOUD_URL;
    process.env.HERMES_WEB_CLOUD_URL = "http://localhost:9119/hermes/";
    try {
      const root = fileURLToPath(new URL("../../", import.meta.url));
      const configFile = fileURLToPath(new URL("../../vite.config.ts", import.meta.url));
      const config = await resolveConfig({
        root,
        configFile,
        logLevel: "silent",
      }, command, mode, "production", isPreview);

      expect(config.preview.proxy).toBeUndefined();
    } finally {
      if (previousCloudUrl === undefined) delete process.env.HERMES_WEB_CLOUD_URL;
      else process.env.HERMES_WEB_CLOUD_URL = previousCloudUrl;
    }
  });
});
