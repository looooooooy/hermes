import { BrowserPasswordAuthClient } from "./BrowserPasswordAuthClient";

describe("BrowserPasswordAuthClient", () => {
  it("uses the exact Cloud password contract with secure cookie credentials", async () => {
    const requests: Array<{ url: string; init: RequestInit }> = [];
    const client = new BrowserPasswordAuthClient({
      loginEndpoint: "https://cloud.example/auth/password-login",
      logoutEndpoint: "https://cloud.example/auth/logout",
      fetcher: vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
        requests.push({ url: String(url), init: init ?? {} });
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      }),
    });

    await expect(client.login({ username: "operator", password: "secret" })).resolves.toEqual({ ok: true });
    expect(requests).toEqual([{
      url: "https://cloud.example/auth/password-login",
      init: {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider: "basic", username: "operator", password: "secret", next: "" }),
      },
    }]);
  });

  it("fails closed without exposing response details", async () => {
    const client = new BrowserPasswordAuthClient({
      loginEndpoint: "/auth/password-login",
      logoutEndpoint: "/auth/logout",
      fetcher: vi.fn(async () => new Response(JSON.stringify({ ok: true, token: "must-not-pass" }), { status: 200 })),
    });
    await expect(client.login({ username: "operator", password: "secret" }))
      .rejects.toThrow("Hermes returned an invalid login response");
  });

  it("forwards AbortSignal to login and logout requests", async () => {
    const signals: Array<AbortSignal | null | undefined> = [];
    const client = new BrowserPasswordAuthClient({
      loginEndpoint: "/auth/password-login",
      logoutEndpoint: "/auth/logout",
      fetcher: vi.fn(async (_url, init) => {
        signals.push(init?.signal);
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      }),
    });
    const login = new AbortController();
    const logout = new AbortController();

    await client.login({ username: "operator", password: "secret" }, login.signal);
    await client.logout(logout.signal);

    expect(signals).toEqual([login.signal, logout.signal]);
  });

  it("cancels a chunked login response once it exceeds the shared JSON bound", async () => {
    const cancel = vi.fn();
    const client = new BrowserPasswordAuthClient({
      loginEndpoint: "/auth/password-login",
      logoutEndpoint: "/auth/logout",
      fetcher: vi.fn(async () => oversizedChunkedResponse(cancel)),
    });

    await expect(client.login({ username: "operator", password: "secret" }))
      .rejects.toThrow("invalid login response");
    expect(cancel).toHaveBeenCalledOnce();
  });

  it("logs out through the exact same-origin cookie contract", async () => {
    const requests: Array<{ url: string; init: RequestInit }> = [];
    const client = new BrowserPasswordAuthClient({
      loginEndpoint: "/auth/password-login",
      logoutEndpoint: "/auth/logout",
      fetcher: vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
        requests.push({ url: String(url), init: init ?? {} });
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      }),
    });

    await expect(client.logout()).resolves.toEqual({ ok: true });
    expect(requests).toEqual([{
      url: "/auth/logout",
      init: {
        method: "POST",
        credentials: "include",
        headers: { Accept: "application/json" },
      },
    }]);
  });

  it.each([
    ["rejected response", vi.fn(async () => new Response(JSON.stringify({ ok: false }), { status: 401 }))],
    ["non-exact response", vi.fn(async () => new Response(JSON.stringify({ ok: true, token: "hidden" }), { status: 200 }))],
    ["network failure", vi.fn(async () => { throw new Error("private network detail"); })],
  ])("fails logout closed for %s without exposing response details", async (_case, fetcher) => {
    const client = new BrowserPasswordAuthClient({
      loginEndpoint: "/auth/password-login",
      logoutEndpoint: "/auth/logout",
      fetcher,
    });

    await expect(client.logout()).rejects.toThrow(/^Hermes .*sign-out/);
  });
});

function oversizedChunkedResponse(cancel: () => void): Response {
  let count = 0;
  return new Response(new ReadableStream<Uint8Array>({
    pull(controller) {
      count += 1;
      controller.enqueue(new Uint8Array(128 * 1024));
      if (count > 3) controller.close();
    },
    cancel,
  }));
}
