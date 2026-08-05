import { expect, test, type Page } from "@playwright/test";

test("production login reaches observer and controller sockets without fixture data or browser tokens", async ({ page }) => {
  const loginBodies: unknown[] = [];
  const logoutRequests: Array<{ method: string; authorization?: string; cookie?: string }> = [];
  const ticketBodies: unknown[] = [];
  const directoryRequests: Array<{ pathname: string; search: string; authorization?: string; cookie?: string }> = [];
  const consoleErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });

  await page.route("**/auth/password-login", async (route) => {
    loginBodies.push(route.request().postDataJSON());
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "Set-Cookie": "hermes_session_at=test; HttpOnly; SameSite=Strict; Path=/" },
      body: JSON.stringify({ ok: true }),
    });
  });
  await page.route("**/api/auth/ws-ticket", async (route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    ticketBodies.push(body);
    const role = body.connection_role === "control" ? "control" : "observer";
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ticket: `${role}-ticket-${"x".repeat(40)}`,
        ttl_seconds: 30,
        connection_role: role,
        ...(body.observer_contract === 2 ? { observer_contract: 2 } : {}),
      }),
    });
  });
  await page.route("**/auth/logout", async (route) => {
    const headers = route.request().headers();
    logoutRequests.push({
      method: route.request().method(),
      authorization: headers.authorization,
      cookie: headers.cookie,
    });
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ok: true }) });
  });
  await page.route("**/api/v1/agents", async (route) => {
    const requestUrl = new URL(route.request().url());
    const headers = route.request().headers();
    directoryRequests.push({
      pathname: requestUrl.pathname,
      search: requestUrl.search,
      authorization: headers.authorization,
      cookie: headers.cookie,
    });
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        agents: [{
          agent_id: "66666666-6666-4666-8666-666666666666",
          workspace_id: "77777777-7777-4777-8777-777777777777",
          agent_key: "macbook-pro",
          status: "active",
          last_seen_at: "2026-08-02T08:30:00+00:00",
        }],
      }),
    });
  });
  await page.route("**/api/v1/agents/*/sessions?**", async (route) => {
    const requestUrl = new URL(route.request().url());
    const headers = route.request().headers();
    directoryRequests.push({
      pathname: requestUrl.pathname,
      search: requestUrl.search,
      authorization: headers.authorization,
      cookie: headers.cookie,
    });
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        sessions: [{
          id: "88888888-8888-4888-8888-888888888888",
          agent_id: "66666666-6666-4666-8666-666666666666",
          workspace_id: "77777777-7777-4777-8777-777777777777",
          _lineage_root_id: "production-session",
          parent_session_id: null,
          title: "Production connector session",
          preview: null,
          source: null,
          model: null,
          profile: "work",
          cwd: null,
          git_branch: null,
          started_at: 1_785_630_600,
          ended_at: null,
          last_active: 1_785_634_200,
          message_count: 12,
          tool_call_count: 1,
          input_tokens: 120,
          output_tokens: 80,
          is_active: true,
          archived: false,
        }],
        total: 1,
        limit: 20,
        offset: 0,
      }),
    });
  });
  await page.addInitScript(installMockWebSocket);

  await page.goto("/");
  await page.screenshot({ path: "docs/qa/production-login.png", fullPage: true });
  await page.getByLabel("Username").fill("operator");
  await page.getByLabel("Password").fill("secret");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByLabel("Session key")).toHaveCount(0);
  await page.getByRole("button", { name: /macbook-pro/ }).click();
  await page.getByRole("button", { name: /Production connector session/ }).click();

  await expect(page.getByText("session · production-session")).toBeVisible();
  await expect(page.getByText("Controller", { exact: true })).toBeVisible();
  await expect(page.getByText(
    "Cloud currently exposes lease management only; conversation actions are unavailable.",
  ).first()).toBeVisible();
  await expect(page.getByText("Run the focused Android control tests and fix the first real failure.")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Queue message" })).toBeDisabled();
  await page.screenshot({ path: "docs/qa/production-controller-lease-only.png", fullPage: true });

  expect(loginBodies).toEqual([{
    provider: "basic",
    username: "operator",
    password: "secret",
    next: "",
  }]);
  expect(ticketBodies).toContainEqual(expect.objectContaining({
    connection_role: "control",
    session_key: "production-session",
    profile: "work",
  }));
  expect(directoryRequests).toHaveLength(2);
  expect(directoryRequests[0]).toMatchObject({ pathname: "/api/v1/agents", search: "", authorization: undefined });
  expect(directoryRequests[1]).toMatchObject({
    pathname: "/api/v1/agents/66666666-6666-4666-8666-666666666666/sessions",
    search: "?min_messages=1&archived=exclude&order=recent&limit=20&offset=0",
    authorization: undefined,
  });
  expect(directoryRequests.every((request) => request.cookie?.includes("hermes_session_at=test"))).toBe(true);
  expect(await page.evaluate(() => sessionStorage.getItem("hermes.access_token"))).toBeNull();
  const browserIdentity = await page.evaluate(() => ({
    value: localStorage.getItem("hermes.web.client_instance_id.v1"),
    keys: Object.keys(localStorage),
  }));
  expect(browserIdentity.value).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
  );
  expect(ticketBodies).toContainEqual({
    connection_role: "observer",
    client_instance_id: browserIdentity.value,
    observer_contract: 2,
  });
  expect(browserIdentity.keys).toEqual(["hermes.web.client_instance_id.v1"]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.getByRole("button", { name: "Open menu" }).click();
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("form", { name: "Sign in to Hermes Cloud" })).toBeVisible();
  expect(logoutRequests).toEqual([{
    method: "POST",
    authorization: undefined,
    cookie: expect.stringContaining("hermes_session_at=test"),
  }]);
  expect(consoleErrors).toEqual([]);
});

test("keeps a maximum-length safe authoritative controller label ellipsized inside the 390px topbar", async ({ page }) => {
  const controllerLabel = "D".repeat(160);
  await routeProductionEntry(page);
  await page.addInitScript(installMockWebSocket, { controllerKind: "desktop" as const, controllerLabel });
  await openProductionSession(page);

  const status = page.locator(".controller-status");
  const label = status.locator(".status-dot-wrap > span:last-child");
  await expect(label).toHaveText(controllerLabel);
  const geometry = await page.locator(".topbar").evaluate((topbar) => {
    const menu = topbar.children[0]!.getBoundingClientRect();
    const brand = topbar.children[1]!.getBoundingClientRect();
    const controller = topbar.children[2]!.getBoundingClientRect();
    const labelElement = topbar.querySelector<HTMLElement>(".controller-status .status-dot-wrap > span:last-child")!;
    const labelStyle = getComputedStyle(labelElement);
    return {
      menuRight: menu.right,
      brandLeft: brand.left,
      brandRight: brand.right,
      controllerLeft: controller.left,
      controllerRight: controller.right,
      labelClientWidth: labelElement.clientWidth,
      labelScrollWidth: labelElement.scrollWidth,
      overflow: labelStyle.overflow,
      textOverflow: labelStyle.textOverflow,
      whiteSpace: labelStyle.whiteSpace,
    };
  });
  expect(geometry.menuRight).toBeLessThanOrEqual(geometry.brandLeft);
  expect(geometry.brandRight).toBeLessThanOrEqual(geometry.controllerLeft);
  expect(geometry.controllerRight).toBeLessThanOrEqual(390);
  expect(geometry.labelScrollWidth).toBeGreaterThan(geometry.labelClientWidth);
  expect(geometry).toMatchObject({ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("never renders an unsafe authoritative controller label", async ({ page }) => {
  const unsafeLabel = "password=hunter2";
  await routeProductionEntry(page);
  await page.addInitScript(installMockWebSocket, { controllerKind: "desktop" as const, controllerLabel: unsafeLabel });
  await openProductionSession(page);

  await expect(page.getByText(unsafeLabel, { exact: false })).toHaveCount(0);
  await expect(page.getByText("Hermes returned an invalid controller lease.", { exact: true }).first()).toBeVisible();
  await expect(page.locator(".controller-status")).not.toContainText(unsafeLabel);
});

async function routeProductionEntry(page: Page): Promise<void> {
  await page.route("**/auth/password-login", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    headers: { "Set-Cookie": "hermes_session_at=test; HttpOnly; SameSite=Strict; Path=/" },
    body: JSON.stringify({ ok: true }),
  }));
  await page.route("**/api/auth/ws-ticket", async (route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    const role = body.connection_role === "control" ? "control" : "observer";
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ticket: `${role}-ticket-${"x".repeat(40)}`,
        ttl_seconds: 30,
        connection_role: role,
        ...(body.observer_contract === 2 ? { observer_contract: 2 } : {}),
      }),
    });
  });
  await page.route("**/api/v1/agents", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      agents: [{
        agent_id: "66666666-6666-4666-8666-666666666666",
        workspace_id: "77777777-7777-4777-8777-777777777777",
        agent_key: "macbook-pro",
        status: "active",
        last_seen_at: "2026-08-02T08:30:00+00:00",
      }],
    }),
  }));
  await page.route("**/api/v1/agents/*/sessions?**", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      sessions: [{
        id: "88888888-8888-4888-8888-888888888888",
        agent_id: "66666666-6666-4666-8666-666666666666",
        workspace_id: "77777777-7777-4777-8777-777777777777",
        _lineage_root_id: "production-session",
        parent_session_id: null,
        title: "Production connector session",
        preview: null,
        source: null,
        model: null,
        profile: "work",
        cwd: null,
        git_branch: null,
        started_at: 1_785_630_600,
        ended_at: null,
        last_active: 1_785_634_200,
        message_count: 12,
        tool_call_count: 1,
        input_tokens: 120,
        output_tokens: 80,
        is_active: true,
        archived: false,
      }],
      total: 1,
      limit: 20,
      offset: 0,
    }),
  }));
}

async function openProductionSession(page: Page): Promise<void> {
  await page.goto("/");
  await page.getByLabel("Username").fill("operator");
  await page.getByLabel("Password").fill("secret");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByRole("button", { name: /macbook-pro/ }).click();
  await page.getByRole("button", { name: /Production connector session/ }).click();
  await expect(page.getByText("session · production-session")).toBeVisible();
}

function installMockWebSocket(options?: { controllerKind?: "mobile" | "desktop"; controllerLabel?: string }) {
  const leaseExpiresAtEpochMs = Date.now() + 30_000;
  const controllerKind = options?.controllerKind ?? "mobile";
  const controllerLabel = options?.controllerLabel ?? "Hermes Web";
  const controlMethods = [
    "session.control.acquire",
    "session.control.renew",
    "session.control.release",
    "session.control.status",
  ];
  const errorCodes = {
    control_role_required: 4200,
    control_contract_unsupported: 4201,
    live_runtime_unavailable: 4202,
    controller_conflict: 4203,
    lease_required: 4204,
    lease_expired: 4205,
    lease_mismatch: 4206,
    request_id_payload_conflict: 4207,
    pending_request_conflict: 4208,
    method_not_allowed: 4209,
    command_unknown: 4210,
    revision_conflict: 4211,
    session_binding_mismatch: 4212,
    invalid_pending_response: 4213,
    owner_adapter_unavailable: 4214,
    relay_overloaded: 4215,
    deadline_exceeded_before_effect: 4306,
    effect_unknown: 4307,
  };

  class MockWebSocket extends EventTarget {
    static readonly CONNECTING = 0;
    static readonly OPEN = 1;
    static readonly CLOSING = 2;
    static readonly CLOSED = 3;
    readonly url: string;
    readonly protocol: string;
    readyState = MockWebSocket.CONNECTING;
    private readonly role: "observer" | "control";

    constructor(url: string | URL, protocols?: string | string[]) {
      super();
      this.url = String(url);
      this.protocol = Array.isArray(protocols) ? protocols[0] ?? "" : protocols ?? "";
      this.role = this.url.includes("control-ticket") ? "control" : "observer";
      window.setTimeout(() => {
        this.readyState = MockWebSocket.OPEN;
        this.dispatchEvent(new Event("open"));
        this.emit(this.role === "control"
          ? {
              jsonrpc: "2.0",
              method: "event",
              params: {
                type: "gateway.ready",
                payload: {
                  observer_contract: 1,
                  control_contract: 1,
                  connection_role: "control",
                  control_available_methods: controlMethods,
                  control_error_codes: errorCodes,
                },
              },
            }
          : {
              jsonrpc: "2.0",
              method: "event",
              params: {
                type: "gateway.ready",
                payload: { observer_contract: 2, connection_role: "observer" },
              },
            });
      }, 0);
    }

    send(raw: string): void {
      const request = JSON.parse(raw) as { id: number; method: string };
      if (request.method === "session.observe.subscribe") {
        this.emit({
          jsonrpc: "2.0",
          id: request.id,
          result: {
            observer_contract: 2,
            subscription_id: "subscription-production",
            profile: "work",
            runtime_generation: "generation-production",
            session_key: "production-session",
            runtime_session_id: "runtime-production",
            running: false,
            status: "idle",
            event_sequence: 0,
            snapshot_event_sequence: 0,
            messages: [],
            inflight: { user: null, assistant: null, streaming: false, error: null },
            todo_sections: [],
            subagents: [],
            tools: [],
            terminals: [],
            replay_events: [],
          },
        });
      } else if (request.method === "session.control.acquire") {
        this.emit({
          jsonrpc: "2.0",
          id: request.id,
          result: {
            lease_id: "production-lease",
            expires_at_epoch_ms: leaseExpiresAtEpochMs,
            control_revision: 7,
            controller_kind: "mobile",
            controller_label: controllerLabel,
            pending_input: null,
          },
        });
      } else if (request.method === "session.control.status") {
        this.emit({
          jsonrpc: "2.0",
          id: request.id,
          result: {
            controller_kind: controllerKind,
            controller_label: controllerLabel,
            control_revision: 7,
            lease_expires_at_epoch_ms: controllerKind === "mobile" ? leaseExpiresAtEpochMs : 0,
            pending_input: null,
          },
        });
      } else if (request.method === "session.control.renew") {
        this.emit({
          jsonrpc: "2.0",
          id: request.id,
          result: {
            lease_id: "production-lease",
            expires_at_epoch_ms: Date.now() + 30_000,
            control_revision: 8,
            controller_kind: "mobile",
            controller_label: controllerLabel,
            pending_input: null,
          },
        });
      } else if (request.method === "session.control.release") {
        this.emit({ jsonrpc: "2.0", id: request.id, result: { released: true, control_revision: 9 } });
      }
    }

    close(): void {
      if (this.readyState === MockWebSocket.CLOSED) return;
      this.readyState = MockWebSocket.CLOSED;
      this.dispatchEvent(new CloseEvent("close", { code: 1000, reason: "client_close" }));
    }

    private emit(value: unknown): void {
      window.setTimeout(() => this.dispatchEvent(new MessageEvent("message", {
        data: JSON.stringify(value),
      })), 0);
    }
  }

  Object.defineProperty(window, "WebSocket", { configurable: true, value: MockWebSocket });
}
