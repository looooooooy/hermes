import { CookieTicketAuthenticationUnavailable, HttpTicketProvider } from "./HttpTicketProvider";

const SESSION_ID = "88888888-8888-4888-8888-888888888888";

describe("HttpTicketProvider", () => {
  it("uses the current Cloud observer/control ticket bodies and validates role and TTL", async () => {
    const requests: Array<{ url: string; init: RequestInit }> = [];
    const fetcher = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      requests.push({ url: String(url), init: init ?? {} });
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      const role = body.connection_role === "control" ? "control" : "observer";
      return new Response(JSON.stringify({
        ticket: `${role}-ticket-value-000000000000000000000000`,
        ttl_seconds: 60,
        connection_role: role,
        ...(body.observer_contract === 2 ? { observer_contract: 2 } : {}),
      }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    const provider = new HttpTicketProvider({
      endpoint: "https://cloud.example/api/auth/ws-ticket",
      clientInstanceId: "11111111-1111-4111-8111-111111111111",
      fetcher,
    });

    await expect(provider.mint({
      connectionRole: "observer",
      observerContract: 2,
      agentId: "66666666-6666-4666-8666-666666666666",
      sessionId: SESSION_ID,
    }))
      .resolves.toContain("observer-ticket");
    await expect(provider.mint({
      connectionRole: "control",
      agentId: "66666666-6666-4666-8666-666666666666",
      sessionId: SESSION_ID,
    }))
      .resolves.toContain("control-ticket");

    expect(requests.map((request) => ({
      url: request.url,
      method: request.init.method,
      credentials: request.init.credentials,
      authorization: (request.init.headers as Record<string, string>).Authorization,
      body: JSON.parse(String(request.init.body)),
    }))).toEqual([
      {
        url: "https://cloud.example/api/auth/ws-ticket",
        method: "POST",
        credentials: "include",
        authorization: undefined,
        body: {
          connection_role: "observer",
          client_instance_id: "11111111-1111-4111-8111-111111111111",
          observer_contract: 2,
          agent_id: "66666666-6666-4666-8666-666666666666",
        },
      },
      {
        url: "https://cloud.example/api/auth/ws-ticket",
        method: "POST",
        credentials: "include",
        authorization: undefined,
        body: {
          connection_role: "control",
          client_instance_id: "11111111-1111-4111-8111-111111111111",
          agent_id: "66666666-6666-4666-8666-666666666666",
          session_id: SESSION_ID,
        },
      },
    ]);
  });

  it.each([
    { ticket: "observer-ticket-value-00000000000000000000", ttl_seconds: 60, connection_role: "observer" },
    {
      ticket: "observer-ticket-value-00000000000000000000",
      ttl_seconds: 60,
      connection_role: "observer",
      observer_contract: 1,
    },
  ])("rejects a v2 observer ticket response that does not bind observer_contract=2", async (body) => {
    const provider = new HttpTicketProvider({
      endpoint: "https://cloud.example/api/auth/ws-ticket",
      clientInstanceId: "11111111-1111-4111-8111-111111111111",
      fetcher: vi.fn(async () => new Response(JSON.stringify(body), { status: 200 })),
    });

    await expect(provider.mint({
      connectionRole: "observer",
      observerContract: 2,
      agentId: "66666666-6666-4666-8666-666666666666",
      sessionId: SESSION_ID,
    })).rejects.toThrow("invalid");
  });

  it("reports the missing or expired cookie ticket bridge without falling back to a bearer token", async () => {
    const provider = new HttpTicketProvider({
      endpoint: "https://cloud.example/api/auth/ws-ticket",
      clientInstanceId: "11111111-1111-4111-8111-111111111111",
      fetcher: vi.fn(async () => new Response(null, { status: 401 })),
    });

    await expect(provider.mint({
      connectionRole: "observer",
      agentId: "66666666-6666-4666-8666-666666666666",
      sessionId: SESSION_ID,
    })).rejects.toBeInstanceOf(CookieTicketAuthenticationUnavailable);
  });

  it("forwards AbortSignal and cancels an oversized chunked ticket response", async () => {
    const cancel = vi.fn();
    let receivedSignal: AbortSignal | null | undefined;
    let count = 0;
    const provider = new HttpTicketProvider({
      endpoint: "/api/auth/ws-ticket",
      clientInstanceId: "11111111-1111-4111-8111-111111111111",
      fetcher: vi.fn(async (_url, init) => {
        receivedSignal = init?.signal;
        return new Response(new ReadableStream<Uint8Array>({
          pull(controller) {
            count += 1;
            controller.enqueue(new Uint8Array(128 * 1024));
            if (count > 3) controller.close();
          },
          cancel,
        }));
      }),
    });
    const controller = new AbortController();

    await expect(provider.mint({
      connectionRole: "observer",
      agentId: "66666666-6666-4666-8666-666666666666",
      sessionId: SESSION_ID,
    }, controller.signal)).rejects.toThrow("invalid");

    expect(receivedSignal).toBe(controller.signal);
    expect(cancel).toHaveBeenCalledOnce();
  });
});
