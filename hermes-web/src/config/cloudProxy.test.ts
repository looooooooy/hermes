import {
  API_PROXY_CONTEXT,
  AUTH_PROXY_CONTEXT,
  createHermesCloudProxy,
  HEALTH_PROXY_CONTEXT,
  WEBSOCKET_PROXY_CONTEXT,
} from "../../config/cloudProxy";
import packageMetadata from "../../package.json";

describe("Hermes Cloud preview proxy", () => {
  it("provides an explicit production-build Cloud preview command", () => {
    expect(packageMetadata.scripts["dev:cloud"]).toBe(
      "npm run build && vite preview --mode cloud",
    );
    expect(packageMetadata.scripts["test:cloud-preview"]).toBe(
      "npm run build && node scripts/check-cloud-preview.mjs",
    );
    expect(packageMetadata.scripts.typecheck).toBe(
      "tsc -p tsconfig.browser.json && tsc -p tsconfig.node.json && tsc -p tsconfig.test.json",
    );
    expect(packageMetadata.scripts["check:iana-registry"]).toBe(
      "node scripts/check-iana-registry-drift.mjs",
    );
  });

  it("uses ordered boundary-aware route contexts", () => {
    const contexts = Object.keys(createHermesCloudProxy({}));

    expect(contexts).toEqual([
      "^/(?:live|ready)(?:\\?|$)",
      "^/auth(?:/|\\?|$)",
      "^/api/ws(?:\\?|$)",
      "^/api(?:/|\\?|$)",
    ]);
    const [, authContext, websocketContext, apiContext] = contexts.map((context) => new RegExp(context));
    expect(authContext.test("/auth/password-login")).toBe(true);
    expect(authContext.test("/authentic")).toBe(false);
    expect(websocketContext.test("/api/ws?ticket=opaque")).toBe(true);
    expect(websocketContext.test("/api/ws-extra")).toBe(false);
    expect(apiContext.test("/api/auth/ws-ticket")).toBe(true);
    expect(apiContext.test("/apian")).toBe(false);
  });

  it("proxies same-origin authentication requests to the Cloud Hermes prefix", () => {
    const proxy = createHermesCloudProxy({});

    expect(proxy[AUTH_PROXY_CONTEXT]).toMatchObject({
      target: "https://api.seaotter.wiki",
      changeOrigin: true,
      secure: true,
    });
    expect(proxy[AUTH_PROXY_CONTEXT]).toEqual(expect.objectContaining({
      rewrite: expect.any(Function),
    }));
    const authProxy = proxy[AUTH_PROXY_CONTEXT];
    if (typeof authProxy === "object" && typeof authProxy.rewrite === "function") {
      expect(authProxy.rewrite("/auth/password-login")).toBe("/hermes/auth/password-login");
    }
  });

  it("exposes only bounded no-secret Cloud health checks for the CLI smoke", () => {
    const proxy = createHermesCloudProxy({});
    const healthProxy = proxy[HEALTH_PROXY_CONTEXT];

    expect(healthProxy).toEqual(expect.objectContaining({ rewrite: expect.any(Function) }));
    expect(healthProxy.rewrite?.("/live")).toBe("/hermes/live");
    expect(healthProxy.rewrite?.("/ready?probe=1")).toBe("/hermes/ready?probe=1");
  });

  it("rewrites ticket REST requests through the Cloud Hermes prefix", () => {
    const proxy = createHermesCloudProxy({});
    const apiProxy = proxy[API_PROXY_CONTEXT];

    expect(apiProxy).toMatchObject({
      target: "https://api.seaotter.wiki",
      changeOrigin: true,
      secure: true,
    });
    expect(apiProxy.rewrite?.("/api/auth/ws-ticket")).toBe("/hermes/api/auth/ws-ticket");
  });

  it("enables the same-origin Cloud WebSocket route and rewrites its Origin", () => {
    const proxy = createHermesCloudProxy({});
    const websocketProxy = proxy[WEBSOCKET_PROXY_CONTEXT];

    expect(websocketProxy).toMatchObject({
      target: "https://api.seaotter.wiki",
      ws: true,
      changeOrigin: true,
      rewriteWsOrigin: true,
      headers: { origin: "https://api.seaotter.wiki" },
    });
    expect(websocketProxy.rewrite?.("/api/ws?ticket=short-lived")).toBe(
      "/hermes/api/ws?ticket=short-lived",
    );
  });

  it("rewrites Cloud cookie scope to the local same-origin root", () => {
    const proxy = createHermesCloudProxy({});

    expect(proxy[AUTH_PROXY_CONTEXT]).toMatchObject({
      cookieDomainRewrite: "",
      cookiePathRewrite: "/",
    });
  });

  it("allows the Cloud base URL and prefix to be overridden", () => {
    const proxy = createHermesCloudProxy({
      HERMES_WEB_CLOUD_URL: "https://cloud.test.example/custom-hermes///",
    });

    expect(proxy[AUTH_PROXY_CONTEXT].target).toBe("https://cloud.test.example");
    expect(proxy[AUTH_PROXY_CONTEXT].rewrite?.("/auth/password-login")).toBe(
      "/custom-hermes/auth/password-login",
    );
    expect(proxy[WEBSOCKET_PROXY_CONTEXT].headers).toEqual({ origin: "https://cloud.test.example" });
  });

  it("contains only the configured Cloud target and no local Agent endpoint", () => {
    const proxy = createHermesCloudProxy({});
    const targets = Object.values(proxy).map((options) => options.target);

    expect(targets).toEqual([
      "https://api.seaotter.wiki",
      "https://api.seaotter.wiki",
      "https://api.seaotter.wiki",
      "https://api.seaotter.wiki",
    ]);
    expect(JSON.stringify(proxy)).not.toContain("9119");
  });

  it("rejects an override that could connect the H5 directly to a local Agent", () => {
    expect(() => createHermesCloudProxy({
      HERMES_WEB_CLOUD_URL: "https://localhost:9119/hermes/",
    })).toThrow("must use an HTTPS Cloud hostname or global IP literal");
  });

  it("rejects the local Hermes port even when the hostname is not loopback", () => {
    expect(() => createHermesCloudProxy({
      HERMES_WEB_CLOUD_URL: "https://cloud.example:9119/hermes/",
    })).toThrow("must use an HTTPS Cloud hostname or global IP literal");
  });

  it.each([
    "https://localhost./hermes/",
    "https://agent.localhost/hermes/",
    "https://[::ffff:127.0.0.1]/hermes/",
    "https://[::ffff:7f00:1]/hermes/",
  ])("rejects normalized loopback Cloud override %s", (cloudUrl) => {
    expect(() => createHermesCloudProxy({
      HERMES_WEB_CLOUD_URL: cloudUrl,
    })).toThrow("must use an HTTPS Cloud hostname or global IP literal");
  });

  it.each([
    "https://0.0.0.0/hermes/",
    "https://10.0.0.1/hermes/",
    "https://100.64.0.1/hermes/",
    "https://169.254.169.254/hermes/",
    "https://172.16.0.1/hermes/",
    "https://192.168.0.1/hermes/",
    "https://192.0.2.1/hermes/",
    "https://192.0.0.8/hermes/",
    "https://192.0.0.11/hermes/",
    "https://192.88.99.1/hermes/",
    "https://198.18.0.1/hermes/",
    "https://198.51.100.1/hermes/",
    "https://203.0.113.1/hermes/",
    "https://224.0.0.1/hermes/",
    "https://240.0.0.1/hermes/",
    "https://[::]/hermes/",
    "https://[100::1]/hermes/",
    "https://[2001::1]/hermes/",
    "https://[2001:1::4]/hermes/",
    "https://[2001:2::1]/hermes/",
    "https://[2001:10::1]/hermes/",
    "https://[2001:db8::1]/hermes/",
    "https://[2002::1]/hermes/",
    "https://[3fff::1]/hermes/",
    "https://[fc00::1]/hermes/",
    "https://[fe80::1]/hermes/",
    "https://[ff02::1]/hermes/",
  ])("rejects non-global Cloud literal %s", (cloudUrl) => {
    expect(() => createHermesCloudProxy({
      HERMES_WEB_CLOUD_URL: cloudUrl,
    })).toThrow("must use an HTTPS Cloud hostname or global IP literal");
  });

  it.each([
    "https://1.1.1.1/hermes/",
    "https://192.0.0.9/hermes/",
    "https://192.0.0.10/hermes/",
    "https://[64:ff9b::1]/hermes/",
    "https://[2001:1::1]/hermes/",
    "https://[2001:1::2]/hermes/",
    "https://[2001:1::3]/hermes/",
    "https://[2001:3::1]/hermes/",
    "https://[2001:4:112::1]/hermes/",
    "https://[2001:20::1]/hermes/",
    "https://[2001:30::1]/hermes/",
    "https://[2620:4f:8000::1]/hermes/",
    "https://[2606:4700:4700::1111]/hermes/",
  ])("accepts global Cloud literal %s", (cloudUrl) => {
    expect(() => createHermesCloudProxy({
      HERMES_WEB_CLOUD_URL: cloudUrl,
    })).not.toThrow();
  });

});
