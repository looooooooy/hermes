// @vitest-environment node

import { createHash } from "node:crypto";
import { createServer, type RequestListener, type Server } from "node:http";
import { connect, type Socket } from "node:net";
import { preview, type PreviewServer, type ProxyOptions } from "vite";
import {
  createHermesCloudProxy,
  WEBSOCKET_PROXY_CONTEXT,
} from "../../config/cloudProxy";

describe("Hermes Cloud Vite preview integration", () => {
  it("executes REST, WebSocket, and cookie rewrites through preview.proxy", async () => {
    const restRequests: Array<{ url: string; host: string; origin: string }> = [];
    const websocketRequests: Array<{ url: string; origin: string }> = [];
    const restUpstream = createTrackedServer((request, response) => {
      restRequests.push({
        url: request.url ?? "",
        host: request.headers.host ?? "",
        origin: request.headers.origin ?? "",
      });
      if (request.url?.startsWith("/hermes/auth/")) {
        response.setHeader(
          "Set-Cookie",
          "hermes_session=opaque; Secure; HttpOnly; Domain=cloud.test.example; Path=/hermes",
        );
      }
      response.setHeader("Content-Type", "application/json");
      response.end(JSON.stringify({ ok: true }));
    });
    const websocketUpstream = createTrackedServer((_request, response) => {
      response.statusCode = 426;
      response.end();
    });
    websocketUpstream.server.on("upgrade", (request, socket) => {
      websocketRequests.push({
        url: request.url ?? "",
        origin: request.headers.origin ?? "",
      });
      const key = request.headers["sec-websocket-key"];
      if (typeof key !== "string") {
        socket.destroy();
        return;
      }
      const accept = createHash("sha1")
        .update(`${key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`)
        .digest("base64");
      socket.write([
        "HTTP/1.1 101 Switching Protocols",
        "Connection: Upgrade",
        "Upgrade: websocket",
        `Sec-WebSocket-Accept: ${accept}`,
        "",
        "",
      ].join("\r\n"));
    });

    let previewServer: PreviewServer | undefined;
    try {
      const restPort = await restUpstream.listen();
      const websocketPort = await websocketUpstream.listen();
      const proxy = localizeProxyTargets(
        createHermesCloudProxy({
          HERMES_WEB_CLOUD_URL: "https://cloud.test.example/hermes/",
        }),
        `http://127.0.0.1:${restPort}`,
        `http://127.0.0.1:${websocketPort}`,
      );
      previewServer = await preview({
        configFile: false,
        logLevel: "silent",
        preview: {
          host: "127.0.0.1",
          port: 0,
          strictPort: true,
          proxy,
        },
      });
      const address = previewServer.httpServer.address();
      if (address === null || typeof address === "string") {
        throw new Error("Vite preview did not expose a TCP port");
      }
      const previewOrigin = `http://127.0.0.1:${address.port}`;

      const loginResponse = await fetch(`${previewOrigin}/auth/password-login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider: "basic" }),
      });
      expect(loginResponse.status).toBe(200);
      const setCookie = loginResponse.headers.get("set-cookie") ?? "";
      const cookieAttributes = setCookie.split(";").map((attribute) => attribute.trim());
      expect(setCookie).toContain("Secure");
      expect(setCookie).toContain("HttpOnly");
      expect(cookieAttributes).toContain("Path=/");
      expect(cookieAttributes).not.toContain("Path=/hermes");
      expect(setCookie).not.toContain("Domain=");

      const ticketResponse = await fetch(`${previewOrigin}/api/auth/ws-ticket?role=observer`, {
        method: "POST",
      });
      expect(ticketResponse.status).toBe(200);
      const requestCountBeforeBoundaryProbe = restRequests.length;
      await fetch(`${previewOrigin}/authentic`);
      expect(restRequests).toHaveLength(requestCountBeforeBoundaryProbe);

      const websocketLookalikeResponse = await fetch(`${previewOrigin}/api/ws-extra`);
      expect(websocketLookalikeResponse.status).toBe(200);
      expect(websocketRequests).toEqual([]);
      expect(restRequests.map((request) => request.url)).toEqual([
        "/hermes/auth/password-login",
        "/hermes/api/auth/ws-ticket?role=observer",
        "/hermes/api/ws-extra",
      ]);
      expect(restRequests.every((request) => request.host === `127.0.0.1:${restPort}`)).toBe(true);
      expect(restRequests.every((request) => request.origin === "https://cloud.test.example")).toBe(true);

      const upgradeResponse = await requestWebSocketUpgrade(address.port);
      expect(upgradeResponse).toContain("101 Switching Protocols");
      expect(websocketRequests).toEqual([{
        url: "/hermes/api/ws?ticket=integration",
        origin: `http://127.0.0.1:${websocketPort}`,
      }]);
    } finally {
      await closeAllResources([
        () => previewServer?.close(),
        () => restUpstream.close(),
        () => websocketUpstream.close(),
      ]);
    }
  });

  it("attempts every cleanup and reports all failures", async () => {
    const calls: string[] = [];

    await expect(closeAllResources([
      () => { calls.push("preview"); },
      () => {
        calls.push("rest");
        throw new Error("rest close failed");
      },
      async () => {
        calls.push("websocket");
        throw new Error("websocket close failed");
      },
    ])).rejects.toThrow("Failed to close 2 test resources");
    expect(calls).toEqual(["preview", "rest", "websocket"]);
  });
});

function localizeProxyTargets(
  proxy: Record<string, ProxyOptions>,
  restTarget: string,
  websocketTarget: string,
): Record<string, ProxyOptions> {
  return Object.fromEntries(Object.entries(proxy).map(([route, options]) => [
    route,
    {
      ...options,
      target: route === WEBSOCKET_PROXY_CONTEXT ? websocketTarget : restTarget,
      secure: false,
    },
  ]));
}

function createTrackedServer(
  listener: RequestListener,
): {
  server: Server;
  listen: () => Promise<number>;
  close: () => Promise<void>;
} {
  const server = createServer(listener);
  const sockets = new Set<Socket>();
  server.on("connection", (socket) => {
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
  });
  return {
    server,
    listen: () => new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", () => {
        server.off("error", reject);
        const address = server.address();
        if (address === null || typeof address === "string") {
          reject(new Error("Test upstream did not expose a TCP port"));
          return;
        }
        resolve(address.port);
      });
    }),
    close: () => new Promise((resolve, reject) => {
      for (const socket of sockets) socket.destroy();
      if (!server.listening) {
        resolve();
        return;
      }
      server.close((error) => {
        if (error) reject(error);
        else resolve();
      });
    }),
  };
}

function requestWebSocketUpgrade(port: number): Promise<string> {
  return new Promise((resolve, reject) => {
    const socket = connect({ host: "127.0.0.1", port });
    let response = "";
    const timeout = setTimeout(() => {
      socket.destroy();
      reject(new Error("Timed out waiting for proxied WebSocket upgrade"));
    }, 3_000);
    socket.on("connect", () => {
      socket.write([
        "GET /api/ws?ticket=integration HTTP/1.1",
        `Host: 127.0.0.1:${port}`,
        "Connection: Upgrade",
        "Upgrade: websocket",
        "Sec-WebSocket-Version: 13",
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
        `Origin: http://127.0.0.1:${port}`,
        "",
        "",
      ].join("\r\n"));
    });
    socket.on("data", (chunk) => {
      response += chunk.toString("utf8");
      if (!response.includes("\r\n\r\n")) return;
      clearTimeout(timeout);
      socket.destroy();
      resolve(response);
    });
    socket.on("error", (error) => {
      clearTimeout(timeout);
      reject(error);
    });
  });
}

async function closeAllResources(
  closers: ReadonlyArray<() => void | Promise<void> | undefined>,
): Promise<void> {
  const results = await Promise.allSettled(
    closers.map(async (close) => close()),
  );
  const failures = results.flatMap((result) => result.status === "rejected" ? [result.reason] : []);
  if (failures.length > 0) {
    throw new AggregateError(failures, `Failed to close ${failures.length} test resources`);
  }
}
