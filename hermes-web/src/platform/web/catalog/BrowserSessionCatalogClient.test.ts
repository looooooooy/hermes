import {
  BrowserSessionCatalogClient,
  SessionCatalogAuthenticationRequired,
  SessionCatalogRequestAborted,
  SessionCatalogScopeAmbiguous,
} from "./BrowserSessionCatalogClient";

const AGENT_ID = "66666666-6666-4666-8666-666666666666";
const WORKSPACE_ID = "77777777-7777-4777-8777-777777777777";
const SESSION_ID = "88888888-8888-4888-8888-888888888888";

describe("BrowserSessionCatalogClient", () => {
  it("reads the authenticated Agent and scoped session catalogs with browser cookies only", async () => {
    const requests: Array<{ url: string; init: RequestInit }> = [];
    const client = new BrowserSessionCatalogClient({
      agentsEndpoint: "https://cloud.example/api/v1/agents",
      sessionsEndpoint: "https://cloud.example/api/v1/agents",
      fetcher: vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        requests.push({ url, init: init ?? {} });
        return url === "https://cloud.example/api/v1/agents"
          ? jsonResponse({ agents: [agentPayload()] })
          : jsonResponse(sessionPagePayload());
      }),
    });

    await expect(client.listAgents()).resolves.toEqual([{
      agentId: AGENT_ID,
      workspaceId: WORKSPACE_ID,
      agentKey: "macbook-pro",
      status: "active",
      lastSeenAt: "2026-08-02T08:30:00+00:00",
    }]);
    await expect(client.listSessions({
      agentId: AGENT_ID,
      profile: "work",
      limit: 20,
      offset: 0,
    })).resolves.toEqual({
      sessions: [{
        id: SESSION_ID,
        agentId: AGENT_ID,
        workspaceId: WORKSPACE_ID,
        sessionKey: "session-real-1",
        profile: "work",
        title: "Fix production connector",
        lastActive: 1_785_634_200,
        messageCount: 12,
        isActive: true,
        directorySource: "transcript_projection",
        availability: "live",
        runtimeGeneration: null,
        surface: null,
        authorityRevision: null,
        availableActions: [],
        transcriptAvailable: true,
      }],
      total: 1,
      limit: 20,
      offset: 0,
      profile: "work",
    });

    expect(requests).toEqual([
      {
        url: "https://cloud.example/api/v1/agents",
        init: {
          method: "GET",
          credentials: "include",
          headers: { Accept: "application/json" },
        },
      },
      {
        url: `https://cloud.example/api/v1/agents/${AGENT_ID}/sessions?profile=work&min_messages=0&archived=exclude&order=recent&limit=20&offset=0`,
        init: {
          method: "GET",
          credentials: "include",
          headers: { Accept: "application/json" },
        },
      },
    ]);
    expect(JSON.stringify(requests)).not.toContain("Authorization");
  });

  it("discovers one service-returned profile before profile-bound pagination", async () => {
    const client = clientReturning(sessionPagePayload());

    await expect(client.listSessions({
      agentId: AGENT_ID,
      profile: null,
      limit: 20,
      offset: 0,
    })).resolves.toMatchObject({ profile: "work" });
  });

  it("rejects a non-canonical Agent id before constructing the scoped path", async () => {
    const fetcher = vi.fn(async () => jsonResponse(sessionPagePayload()));
    const client = new BrowserSessionCatalogClient({
      agentsEndpoint: "/api/v1/agents",
      sessionsEndpoint: "/api/v1/agents/",
      fetcher,
    });

    await expect(client.listSessions({
      agentId: "../../api/auth/ws-ticket",
      profile: "work",
      limit: 20,
      offset: 0,
    })).rejects.toThrow("Hermes returned an invalid session directory");
    expect(fetcher).not.toHaveBeenCalled();
  });

  it.each([
    ["extra Agent field", { agents: [{ ...agentPayload(), access_token: "not-accepted" }] }],
    ["invalid UUID", { agents: [{ ...agentPayload(), agent_id: "agent-a" }] }],
    ["unsafe display text", { agents: [{ ...agentPayload(), agent_key: "password=hunter2" }] }],
    ["duplicate Agent", { agents: [agentPayload(), agentPayload()] }],
  ])("rejects %s", async (_label, payload) => {
    await expect(clientReturning(payload).listAgents())
      .rejects.toThrow("Hermes returned an invalid Agent directory");
  });

  it.each([
    ["extra page field", { ...sessionPagePayload(), cursor: "secret" }],
    ["wrong Agent binding", sessionPagePayload({ agent_id: "99999999-9999-4999-8999-999999999999" })],
    ["unsafe title", sessionPagePayload({ title: "Authorization: Bearer hidden" })],
    ["duplicate session", { ...sessionPagePayload(), sessions: [sessionPayload(), sessionPayload()], total: 2 }],
  ])("rejects %s", async (_label, payload) => {
    await expect(clientReturning(payload).listSessions({
      agentId: AGENT_ID,
      profile: "work",
      limit: 20,
      offset: 0,
    })).rejects.toThrow("Hermes returned an invalid session directory");
  });

  it("rejects mixed profiles during profile discovery", async () => {
    const second = { ...sessionPayload(), id: "99999999-9999-4999-8999-999999999999", _lineage_root_id: "session-real-2", profile: "personal" };
    const payload = { ...sessionPagePayload(), sessions: [sessionPayload(), second], total: 2 };

    await expect(clientReturning(payload).listSessions({
      agentId: AGENT_ID,
      profile: null,
      limit: 20,
      offset: 0,
    })).rejects.toThrow("Hermes session profile is ambiguous");
  });

  it("fails closed for authentication, ambiguous scope, network, and oversized responses", async () => {
    const authentication = clientReturning({}, 401);
    await expect(authentication.listAgents()).rejects.toBeInstanceOf(SessionCatalogAuthenticationRequired);

    const ambiguous = clientReturning({}, 409);
    await expect(ambiguous.listSessions({
      agentId: AGENT_ID,
      profile: null,
      limit: 20,
      offset: 0,
    })).rejects.toBeInstanceOf(SessionCatalogScopeAmbiguous);

    const unavailable = new BrowserSessionCatalogClient({
      agentsEndpoint: "/api/v1/agents",
      sessionsEndpoint: "/api/v1/agents",
      fetcher: vi.fn(async () => { throw new Error("private network detail"); }),
    });
    await expect(unavailable.listAgents()).rejects.toThrow("Hermes session directory is unavailable");

    const oversized = clientReturning({ agents: [] }, 200, { "Content-Length": String(256 * 1024 + 1) });
    await expect(oversized.listAgents()).rejects.toThrow("Hermes returned an oversized session directory");
  });

  it("cancels a chunked directory response as soon as the shared byte bound is exceeded", async () => {
    const cancel = vi.fn();
    let count = 0;
    const client = new BrowserSessionCatalogClient({
      agentsEndpoint: "/api/v1/agents",
      sessionsEndpoint: "/api/v1/agents",
      fetcher: vi.fn(async () => new Response(new ReadableStream<Uint8Array>({
        pull(controller) {
          count += 1;
          controller.enqueue(new Uint8Array(128 * 1024));
          if (count > 3) controller.close();
        },
        cancel,
      }))),
    });

    await expect(client.listAgents()).rejects.toThrow("oversized session directory");
    expect(cancel).toHaveBeenCalledOnce();
  });

  it("forwards AbortSignal to Agent and session fetches and preserves cancellation as a non-network outcome", async () => {
    const observedSignals: AbortSignal[] = [];
    const fetcher = vi.fn(async (_input: string | URL | Request, init?: RequestInit) => {
      const signal = init?.signal;
      if (!(signal instanceof AbortSignal)) throw new Error("missing AbortSignal");
      observedSignals.push(signal);
      return await new Promise<Response>((_resolve, reject) => {
        signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
      });
    });
    const client = new BrowserSessionCatalogClient({
      agentsEndpoint: "/api/v1/agents",
      sessionsEndpoint: "/api/v1/agents",
      fetcher,
    });
    const agentsAbort = new AbortController();
    const agents = client.listAgents(agentsAbort.signal);
    agentsAbort.abort();
    await expect(agents).rejects.toBeInstanceOf(SessionCatalogRequestAborted);

    const sessionsAbort = new AbortController();
    const sessions = client.listSessions({
      agentId: AGENT_ID,
      profile: "work",
      limit: 20,
      offset: 0,
    }, sessionsAbort.signal);
    sessionsAbort.abort();
    await expect(sessions).rejects.toBeInstanceOf(SessionCatalogRequestAborted);
    expect(observedSignals).toEqual([agentsAbort.signal, sessionsAbort.signal]);
  });
});

function clientReturning(payload: unknown, status = 200, headers?: HeadersInit) {
  return new BrowserSessionCatalogClient({
    agentsEndpoint: "/api/v1/agents",
    sessionsEndpoint: "/api/v1/agents",
    fetcher: vi.fn(async () => jsonResponse(payload, status, headers)),
  });
}

function jsonResponse(payload: unknown, status = 200, headers?: HeadersInit): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function agentPayload() {
  return {
    agent_id: AGENT_ID,
    workspace_id: WORKSPACE_ID,
    agent_key: "macbook-pro",
    status: "active",
    last_seen_at: "2026-08-02T08:30:00+00:00",
  };
}

function sessionPagePayload(overrides: Record<string, unknown> = {}) {
  return {
    sessions: [{ ...sessionPayload(), ...overrides }],
    total: 1,
    limit: 20,
    offset: 0,
  };
}

function sessionPayload() {
  return {
    id: SESSION_ID,
    agent_id: AGENT_ID,
    workspace_id: WORKSPACE_ID,
    _lineage_root_id: "session-real-1",
    parent_session_id: null,
    title: "Fix production connector",
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
    directory_source: "transcript_projection",
    availability: "live",
    runtime_generation: null,
    surface: null,
    authority_revision: null,
    available_actions: [],
    transcript_available: true,
  };
}
