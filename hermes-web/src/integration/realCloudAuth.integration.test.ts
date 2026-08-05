// @vitest-environment node

import { spawn, type ChildProcess } from "node:child_process";
import { EventEmitter } from "node:events";
import { createServer } from "node:http";
import { mkdtemp, rm } from "node:fs/promises";
import { connect, type Socket } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { PassThrough } from "node:stream";
import { preview, type PreviewServer, type ProxyOptions } from "vite";
import { createHermesCloudProxy } from "../../config/cloudProxy";
import { BrowserPasswordAuthClient } from "../platform/web/auth/BrowserPasswordAuthClient";
import {
  BrowserSessionCatalogClient,
  SessionCatalogAuthenticationRequired,
} from "../platform/web/catalog/BrowserSessionCatalogClient";
import { HttpTicketProvider } from "../platform/web/realtime/HttpTicketProvider";

const AGENT_ID = "66666666-6666-4666-8666-666666666666";
const SESSION_ID = "88888888-8888-4888-8888-888888888888";
const CLOUD_READINESS_TIMEOUT_MS = 15_000;
const CLOUD_READINESS_PROBE_TIMEOUT_MS = 250;

describe("current H5 clients against a real SQLite Cloud ASGI server", () => {
  it("allows a bounded fifteen-second readiness window for contended CI", () => {
    expect(CLOUD_READINESS_TIMEOUT_MS).toBe(15_000);
  });

  it("closes login, catalog, logout, old access, and old ticket authority", async () => {
    const tempRoot = await mkdtemp(join(tmpdir(), "hermes-h5-cloud-"));
    const cloudPort = await availablePort();
    const cloud = startCloud(cloudPort, join(tempRoot, "cloud.sqlite3"));
    let previewServer: PreviewServer | undefined;
    try {
      await waitForCloud(cloud, cloudPort);
      previewServer = await preview({
        configFile: false,
        logLevel: "silent",
        preview: {
          host: "127.0.0.1",
          port: 0,
          strictPort: true,
          proxy: localCloudProxy(cloudPort),
        },
      });
      const address = previewServer.httpServer.address();
      expect(address).not.toBeNull();
      expect(typeof address).not.toBe("string");
      if (address === null || typeof address === "string") throw new Error("Vite preview did not bind TCP");
      const origin = `http://127.0.0.1:${address.port}`;
      const jar = new CookieJarFetcher();
      const auth = new BrowserPasswordAuthClient({
        loginEndpoint: `${origin}/auth/password-login`,
        logoutEndpoint: `${origin}/auth/logout`,
        fetcher: jar.fetch,
      });
      const catalog = new BrowserSessionCatalogClient({
        agentsEndpoint: `${origin}/api/v1/agents`,
        sessionsEndpoint: `${origin}/api/v1/agents`,
        fetcher: jar.fetch,
      });
      const ticketProvider = new HttpTicketProvider({
        endpoint: `${origin}/api/auth/ws-ticket`,
        clientInstanceId: "77777777-7777-4777-8777-777777777777",
        fetcher: jar.fetch,
      });

      await expect(auth.login({
        username: "operator@example.test",
        password: "correct-password",
      })).resolves.toEqual({ ok: true });
      const oldAccess = jar.require("hermes_session_at");

      await expect(catalog.listAgents()).resolves.toEqual([
        expect.objectContaining({ agentId: AGENT_ID, agentKey: "integration-agent" }),
      ]);
      await expect(catalog.listSessions({
        agentId: AGENT_ID,
        profile: "default",
        limit: 20,
        offset: 0,
      })).resolves.toEqual(expect.objectContaining({
        profile: "default",
        total: 1,
        sessions: [expect.objectContaining({ id: SESSION_ID, sessionKey: SESSION_ID })],
      }));

      const oldTicket = await ticketProvider.mint({
        connectionRole: "control",
        agentId: AGENT_ID,
        sessionId: SESSION_ID,
      });

      await expect(auth.logout()).resolves.toEqual({ ok: true });
      expect(jar.has("hermes_session_at")).toBe(false);
      await expect(catalog.listAgents()).rejects.toBeInstanceOf(
        SessionCatalogAuthenticationRequired,
      );

      const replayedAccess = await fetch(`${origin}/api/v1/agents`, {
        headers: {
          Accept: "application/json",
          Cookie: `hermes_session_at=${oldAccess}`,
        },
      });
      expect(replayedAccess.status).toBe(401);

      const upgrade = await websocketUpgrade(address.port, oldTicket);
      expect(upgrade).not.toContain("101 Switching Protocols");
      expect(upgrade).toContain("403 Forbidden");
    } finally {
      await cleanupIntegration(previewServer, cloud, tempRoot);
    }
  }, 30_000);

  it("waits for process exit after escalating an ignored SIGTERM to SIGKILL", async () => {
    const child = spawn(process.execPath, [
      "-e",
      "process.on('SIGTERM', () => {}); console.log('ready'); setInterval(() => {}, 1000)",
    ], { stdio: ["ignore", "pipe", "pipe"] });
    await new Promise<void>((resolveReady, rejectReady) => {
      child.once("error", rejectReady);
      child.stdout?.once("data", () => resolveReady());
    });

    await stopCloud(child, 20, 2_000);

    expect(child.exitCode !== null || child.signalCode !== null).toBe(true);
  });

  it("reports a Cloud child that exits before readiness without waiting for the deadline", async () => {
    const child = spawn(process.execPath, ["-e", "console.error('startup-failed'); process.exit(17)"], {
      stdio: ["ignore", "pipe", "pipe"],
    });
    const port = await availablePort();

    await expect(waitForCloud(child, port, 15_000)).rejects.toThrow(
      /Real Cloud exited before readiness:.*startup-failed/,
    );
  });

  it.each(["ENOENT", "EACCES"])(
    "fails fast and removes listeners when Cloud spawn emits %s",
    async (code) => {
      const child = fakeChildProcess();
      const port = await availablePort();
      const waiting = waitForCloud(child, port, 1_000);

      child.emit("error", Object.assign(new Error("spawn /private/secret/cloud"), { code }));
      const failure = await waiting.catch((error: unknown) => error);

      expect(failure).toEqual(expect.objectContaining({
        message: `Real Cloud process failed to start (${code})`,
      }));
      expect(child.listenerCount("error")).toBe(0);
      expect(child.stderr?.listenerCount("data")).toBe(0);
    },
  );
});

function startCloud(port: number, database: string): ChildProcess {
  return spawn(
    resolve("../hermes-cloud/.venv/bin/python"),
    [resolve("tests/integration/real_cloud_server.py"), "--port", String(port), "--database", database],
    {
      cwd: resolve("."),
      env: {
        ...process.env,
        PYTHONDONTWRITEBYTECODE: "1",
        PYTHONPATH: resolve("../hermes-cloud/src"),
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
}

async function waitForCloud(
  process: ChildProcess,
  port: number,
  timeoutMs = CLOUD_READINESS_TIMEOUT_MS,
): Promise<void> {
  let diagnostic = "";
  let startFailure: Error | null = null;
  let wakeForProcessError!: () => void;
  const processError = new Promise<void>((resolveError) => { wakeForProcessError = resolveError; });
  const onStderr = (chunk: unknown) => { diagnostic += String(chunk); };
  const onProcessError = (error: Error) => {
    startFailure = new Error(`Real Cloud process failed to start (${safeProcessErrorCode(error)})`);
    wakeForProcessError();
  };
  process.stderr?.on("data", onStderr);
  process.on("error", onProcessError);
  try {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (startFailure !== null) throw startFailure;
      if (process.exitCode !== null || process.signalCode !== null) {
        throw new Error(`Real Cloud exited before readiness: ${diagnostic.slice(-1_000)}`);
      }
      const remainingMs = Math.max(1, deadline - Date.now());
      const probe = fetch(`http://127.0.0.1:${port}/ready`, {
        signal: AbortSignal.timeout(Math.min(CLOUD_READINESS_PROBE_TIMEOUT_MS, remainingMs)),
      }).catch(() => null);
      const response = await Promise.race([
        probe,
        processError.then(() => null),
      ]);
      if (startFailure !== null) throw startFailure;
      if (response?.status === 200) return;
      const delayMs = Math.min(50, Math.max(0, deadline - Date.now()));
      if (delayMs > 0) {
        await Promise.race([
          new Promise((resolve) => setTimeout(resolve, delayMs)),
          processError,
        ]);
      }
    }
    if (startFailure !== null) throw startFailure;
    if (process.exitCode !== null || process.signalCode !== null) {
      throw new Error(`Real Cloud exited before readiness: ${diagnostic.slice(-1_000)}`);
    }
    throw new Error(`Real Cloud did not become ready: ${diagnostic.slice(-1_000)}`);
  } finally {
    process.removeListener("error", onProcessError);
    process.stderr?.removeListener("data", onStderr);
  }
}

function safeProcessErrorCode(error: Error): "ENOENT" | "EACCES" | "UNKNOWN" {
  const code = (error as Error & { code?: unknown }).code;
  return code === "ENOENT" || code === "EACCES" ? code : "UNKNOWN";
}

async function stopCloud(
  process: ChildProcess,
  gracefulTimeoutMs = 2_000,
  forcedTimeoutMs = 2_000,
): Promise<void> {
  if (process.exitCode !== null) return;
  process.kill("SIGTERM");
  if (await waitForProcessExit(process, gracefulTimeoutMs)) return;
  process.kill("SIGKILL");
  if (!await waitForProcessExit(process, forcedTimeoutMs)) {
    throw new Error("Cloud integration process did not exit after SIGKILL");
  }
}

async function cleanupIntegration(
  previewServer: PreviewServer | undefined,
  cloud: ChildProcess,
  tempRoot: string,
): Promise<void> {
  const failures: unknown[] = [];
  try {
    await stopCloud(cloud);
  } catch (error) {
    failures.push(error);
  }
  try {
    await previewServer?.close();
  } catch (error) {
    failures.push(error);
  }
  try {
    await rm(tempRoot, { recursive: true, force: true });
  } catch (error) {
    failures.push(error);
  }
  if (failures.length > 0) throw new AggregateError(failures, "H5 integration cleanup failed");
}

function waitForProcessExit(process: ChildProcess, timeoutMs: number): Promise<boolean> {
  if (process.exitCode !== null || process.signalCode !== null) return Promise.resolve(true);
  return new Promise((resolveExit) => {
    const timeout = setTimeout(() => {
      process.removeListener("exit", exited);
      resolveExit(false);
    }, timeoutMs);
    const exited = () => {
      clearTimeout(timeout);
      resolveExit(true);
    };
    process.once("exit", exited);
  });
}

function fakeChildProcess(): ChildProcess {
  return Object.assign(new EventEmitter(), {
    exitCode: null,
    signalCode: null,
    stderr: new PassThrough(),
  }) as unknown as ChildProcess;
}

function localCloudProxy(port: number): Record<string, ProxyOptions> {
  const proxy = createHermesCloudProxy({
    HERMES_WEB_CLOUD_URL: "https://cloud.integration.test/",
  });
  return Object.fromEntries(Object.entries(proxy).map(([route, options]) => [
    route,
    {
      ...options,
      target: `http://127.0.0.1:${port}`,
      secure: false,
      xfwd: false,
      headers: {
        ...options.headers,
        origin: `https://127.0.0.1:${port}`,
        "x-forwarded-proto": "https",
      },
    },
  ]));
}

class CookieJarFetcher {
  private readonly cookies = new Map<string, string>();

  readonly fetch: typeof fetch = async (input, init) => {
    const headers = new Headers(init?.headers);
    if (this.cookies.size > 0) {
      headers.set("Cookie", [...this.cookies].map(([name, value]) => `${name}=${value}`).join("; "));
    }
    const response = await fetch(input, { ...init, headers });
    const cookieHeaders = (
      response.headers as Headers & { getSetCookie?: () => string[] }
    ).getSetCookie?.() ?? [];
    for (const header of cookieHeaders) this.capture(header);
    return response;
  };

  has(name: string): boolean {
    return this.cookies.has(name);
  }

  require(name: string): string {
    const value = this.cookies.get(name);
    if (value === undefined) throw new Error(`Expected cookie ${name}`);
    return value;
  }

  private capture(header: string): void {
    const [pair, ...attributes] = header.split(";").map((part) => part.trim());
    const separator = pair.indexOf("=");
    if (separator < 1) return;
    const name = pair.slice(0, separator);
    const value = pair.slice(separator + 1);
    const expired = value.length === 0 || attributes.some((attribute) => attribute.toLowerCase() === "max-age=0");
    if (expired) this.cookies.delete(name);
    else this.cookies.set(name, value);
  }
}

function availablePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (address === null || typeof address === "string") {
        reject(new Error("Could not reserve a Cloud test port"));
        return;
      }
      server.close((error) => error ? reject(error) : resolve(address.port));
    });
  });
}

function websocketUpgrade(port: number, ticket: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const socket: Socket = connect({ host: "127.0.0.1", port });
    let response = "";
    const timeout = setTimeout(() => {
      socket.destroy();
      reject(new Error("Timed out waiting for real Cloud WebSocket rejection"));
    }, 3_000);
    socket.on("connect", () => {
      socket.write([
        `GET /api/ws?ticket=${encodeURIComponent(ticket)} HTTP/1.1`,
        `Host: 127.0.0.1:${port}`,
        "Connection: Upgrade",
        "Upgrade: websocket",
        "Sec-WebSocket-Version: 13",
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
        "Origin: http://127.0.0.1",
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
