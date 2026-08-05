import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SessionCatalogRequestAborted } from "../platform/web/catalog/BrowserSessionCatalogClient";
import { ProductionApp } from "./ProductionApp";

const AGENT_ID = "66666666-6666-4666-8666-666666666666";
const WORKSPACE_ID = "77777777-7777-4777-8777-777777777777";

describe("ProductionApp authentication composition", () => {
  it("logs in with username and password, then selects a catalog-returned Agent and service-returned session", async () => {
    window.sessionStorage.clear();
    const authClient = authStub();
    const catalogClient = catalogStub();
    const { runtime } = runtimeStub();
    const runtimeFactory = vi.fn(() => runtime);
    const user = userEvent.setup();
    render(<ProductionApp authClient={authClient} catalogClient={catalogClient} runtimeFactory={runtimeFactory} />);

    const form = screen.getByRole("form", { name: "Sign in to Hermes Cloud" });
    await user.type(within(form).getByLabelText("Username"), "operator");
    await user.type(within(form).getByLabelText("Password"), "secret");
    expect(within(form).queryByLabelText("Session key")).not.toBeInTheDocument();
    await user.click(within(form).getByRole("button", { name: "Sign in" }));

    expect(authClient.login).toHaveBeenCalledWith(
      { username: "operator", password: "secret" },
      expect.any(AbortSignal),
    );
    expect(await screen.findByRole("button", { name: /macbook-pro/ })).toBeVisible();
    expect(screen.getByText("Online")).toBeVisible();
    expect(screen.getByText(/Last seen/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: /macbook-pro/ }));

    expect(await screen.findByRole("button", { name: /Fix production connector/ })).toBeVisible();
    expect(screen.getByText("Hermes sessions")).toBeVisible();
    expect(screen.getByLabelText("Hermes sessions")).toBeVisible();
    expect(screen.queryByText("Real Hermes sessions")).not.toBeInTheDocument();
    expect(screen.getByRole("main").textContent).not.toMatch(/真实|\breal\b/i);
    expect(screen.getByText("12 messages")).toBeVisible();
    expect(screen.getByText("Active")).toBeVisible();
    await user.click(screen.getByRole("button", { name: /Fix production connector/ }));

    expect(catalogClient.listSessions).toHaveBeenCalledWith({
      agentId: AGENT_ID,
      profile: null,
      limit: 20,
      offset: 0,
    }, expect.any(AbortSignal));
    expect(runtimeFactory).toHaveBeenCalledWith({
      agentId: AGENT_ID,
      sessionId: "88888888-8888-4888-8888-888888888888",
      sessionKey: "session-real-1",
      profile: "work",
    });
    expect(screen.getByText("session · session-real-1")).toBeVisible();
    expect(window.sessionStorage.getItem("hermes.access_token")).toBeNull();
  });

  it("uses neutral loading copy until the Cloud directory resolves", async () => {
    const catalogClient = catalogStub({
      listSessions: vi.fn(() => new Promise(() => undefined)),
    });
    const user = userEvent.setup();
    render(<ProductionApp
      authClient={authStub()}
      catalogClient={catalogClient}
      runtimeFactory={vi.fn()}
    />);

    await signIn(user);
    await user.click(await screen.findByRole("button", { name: /macbook-pro/ }));

    expect(await screen.findByText("Loading sessions…")).toBeVisible();
    expect(screen.queryByText("Loading real sessions…")).not.toBeInTheDocument();
  });

  it("clears the password and keeps the form fail-closed after rejected login", async () => {
    const authClient = authStub({ login: vi.fn(async () => { throw new Error("Hermes sign-in was rejected"); }) });
    const user = userEvent.setup();
    render(<ProductionApp authClient={authClient} catalogClient={catalogStub()} runtimeFactory={vi.fn()} />);
    const form = screen.getByRole("form", { name: "Sign in to Hermes Cloud" });
    await user.type(within(form).getByLabelText("Username"), "operator");
    await user.type(within(form).getByLabelText("Password"), "wrong");
    await user.click(within(form).getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Hermes sign-in was rejected");
    expect(within(form).getByLabelText("Password")).toHaveValue("");
    expect(screen.queryByRole("button", { name: /macbook-pro/ })).not.toBeInTheDocument();
  });

  it("shows an honest empty state and retries the session directory without creating a runtime", async () => {
    const catalogClient = catalogStub({
      listSessions: vi.fn(async () => ({ sessions: [], total: 0, limit: 20, offset: 0, profile: null })),
    });
    const user = userEvent.setup();
    const runtimeFactory = vi.fn();
    render(<ProductionApp
      authClient={authStub()}
      catalogClient={catalogClient}
      runtimeFactory={runtimeFactory}
    />);

    await signIn(user);
    await user.click(await screen.findByRole("button", { name: /macbook-pro/ }));
    expect(await screen.findByText("该 Agent 暂无可用会话")).toBeVisible();
    expect(screen.getByRole("main").textContent).not.toMatch(/真实|\breal\b/i);
    expect(screen.queryByLabelText("Session key")).not.toBeInTheDocument();
    expect(runtimeFactory).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Refresh sessions" }));
    expect(catalogClient.listSessions).toHaveBeenCalledTimes(2);
  });

  it("uses ?session only after an exact paginated directory match", async () => {
    const pageOne = sessionPage({
      sessions: [sessionItem({ sessionKey: "session-other", id: "99999999-9999-4999-8999-999999999999" })],
      total: 2,
    });
    const pageTwo = sessionPage({ offset: 1, limit: 20, total: 2 });
    const catalogClient = catalogStub({
      listSessions: vi.fn(async ({ offset }: { offset: number }) => offset === 0 ? pageOne : pageTwo),
    });
    const runtimeFactory = vi.fn(() => runtimeStub().runtime);
    const user = userEvent.setup();
    render(<ProductionApp
      authClient={authStub()}
      catalogClient={catalogClient}
      runtimeFactory={runtimeFactory}
      initialSessionKey="session-real-1"
    />);

    expect(screen.queryByText("session · session-real-1")).not.toBeInTheDocument();
    await signIn(user);
    expect(runtimeFactory).not.toHaveBeenCalled();
    await user.click(await screen.findByRole("button", { name: /macbook-pro/ }));

    await waitFor(() => expect(runtimeFactory).toHaveBeenCalledWith({
      agentId: AGENT_ID,
      sessionId: "88888888-8888-4888-8888-888888888888",
      sessionKey: "session-real-1",
      profile: "work",
    }));
    expect(catalogClient.listSessions).toHaveBeenNthCalledWith(2, {
      agentId: AGENT_ID,
      profile: "work",
      limit: 20,
      offset: 1,
    }, expect.any(AbortSignal));
  });

  it("does not let an unmatched ?session bypass the catalog", async () => {
    const runtimeFactory = vi.fn();
    const user = userEvent.setup();
    render(<ProductionApp
      authClient={authStub()}
      catalogClient={catalogStub()}
      runtimeFactory={runtimeFactory}
      initialSessionKey="forged-session"
    />);

    await signIn(user);
    await user.click(await screen.findByRole("button", { name: /macbook-pro/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Requested session is not present in this Agent directory",
    );
    expect(runtimeFactory).not.toHaveBeenCalled();
  });

  it("stops the prior runtime before switching directory or signing out", async () => {
    const first = runtimeStub();
    const second = runtimeStub();
    const runtimes = [first.runtime, second.runtime];
    const runtimeFactory = vi.fn(() => runtimes.shift()!);
    const user = userEvent.setup();
    const authClient = authStub();
    render(<ProductionApp
      authClient={authClient}
      catalogClient={catalogStub()}
      runtimeFactory={runtimeFactory}
    />);

    await signIn(user);
    await user.click(await screen.findByRole("button", { name: /macbook-pro/ }));
    await user.click(await screen.findByRole("button", { name: /Fix production connector/ }));
    await waitFor(() => expect(first.start).toHaveBeenCalledOnce());
    expect(screen.queryByRole("button", { name: "Sessions" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open menu" }));
    await user.click(screen.getByRole("button", { name: "Sessions" }));
    await waitFor(() => expect(first.stop).toHaveBeenCalledOnce());

    await user.click(screen.getByRole("button", { name: /Fix production connector/ }));
    await waitFor(() => expect(second.start).toHaveBeenCalledOnce());
    await user.click(screen.getByRole("button", { name: "Open menu" }));
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    expect(authClient.logout).toHaveBeenCalledOnce();
    await waitFor(() => expect(second.stop).toHaveBeenCalledOnce());
    expect(screen.getByRole("form", { name: "Sign in to Hermes Cloud" })).toBeVisible();
  });

  it("does not enter the authenticated directory until listAgents succeeds", async () => {
    const catalogClient = catalogStub({
      listAgents: vi.fn(async () => { throw new Error("Hermes session directory authentication is required"); }),
    });
    const user = userEvent.setup();
    render(<ProductionApp authClient={authStub()} catalogClient={catalogClient} runtimeFactory={vi.fn()} />);

    await signIn(user);

    expect(await screen.findByRole("alert")).toHaveTextContent("Hermes session directory authentication is required");
    expect(screen.getByRole("form", { name: "Sign in to Hermes Cloud" })).toBeVisible();
    expect(screen.queryByText("Connected Agents")).not.toBeInTheDocument();
  });

  it("keeps the current runtime and authenticated state when server logout fails", async () => {
    const authClient = authStub({
      logout: vi.fn(async () => { throw new Error("Hermes sign-out service is unavailable"); }),
    });
    const current = runtimeStub();
    const user = userEvent.setup();
    render(<ProductionApp
      authClient={authClient}
      catalogClient={catalogStub()}
      runtimeFactory={() => current.runtime}
    />);

    await signIn(user);
    await user.click(await screen.findByRole("button", { name: /macbook-pro/ }));
    await user.click(await screen.findByRole("button", { name: /Fix production connector/ }));
    await waitFor(() => expect(current.start).toHaveBeenCalledOnce());
    await user.click(screen.getByRole("button", { name: "Open menu" }));
    await user.click(screen.getByRole("button", { name: "Sign out" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Hermes sign-out failed. The current session remains active.",
    );
    expect(screen.getByText("session · session-real-1")).toBeVisible();
    expect(current.stop).not.toHaveBeenCalled();
    expect(screen.queryByRole("form", { name: "Sign in to Hermes Cloud" })).not.toBeInTheDocument();
  });

  it("aborts the post-login Agent request on unmount without publishing an error", async () => {
    let signal: AbortSignal | undefined;
    const catalogClient = catalogStub({
      listAgents: vi.fn(async (requestSignal?: AbortSignal) => {
        signal = requestSignal;
        return await new Promise<never>((_resolve, reject) => {
          requestSignal?.addEventListener("abort", () => reject(new SessionCatalogRequestAborted()), { once: true });
        });
      }),
    });
    const user = userEvent.setup();
    const view = render(<ProductionApp authClient={authStub()} catalogClient={catalogClient} runtimeFactory={vi.fn()} />);

    await signIn(user);
    await waitFor(() => expect(catalogClient.listAgents).toHaveBeenCalledOnce());
    view.unmount();

    expect(signal).toBeInstanceOf(AbortSignal);
    expect(signal?.aborted).toBe(true);
  });

  it("aborts an in-flight login on unmount without publishing late authentication state", async () => {
    let loginSignal: AbortSignal | undefined;
    const authClient = authStub({
      login: vi.fn(async (_request, signal?: AbortSignal) => {
        loginSignal = signal;
        return await new Promise<never>((_resolve, reject) => {
          signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
        });
      }),
    });
    const catalogClient = catalogStub();
    const user = userEvent.setup();
    const view = render(<ProductionApp
      authClient={authClient}
      catalogClient={catalogClient}
      runtimeFactory={vi.fn()}
    />);

    await signIn(user);
    await waitFor(() => expect(authClient.login).toHaveBeenCalledOnce());
    view.unmount();

    expect(loginSignal?.aborted).toBe(true);
    expect(catalogClient.listAgents).not.toHaveBeenCalled();
  });

  it("aborts the prior login before starting a newer login epoch", async () => {
    let firstSignal: AbortSignal | undefined;
    let callCount = 0;
    const authClient = authStub({
      login: vi.fn(async (_request, signal?: AbortSignal) => {
        callCount += 1;
        if (callCount > 1) return { ok: true as const };
        firstSignal = signal;
        return await new Promise<never>((_resolve, reject) => {
          signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
        });
      }),
    });
    const catalogClient = catalogStub();
    const user = userEvent.setup();
    render(<ProductionApp
      authClient={authClient}
      catalogClient={catalogClient}
      runtimeFactory={vi.fn()}
    />);
    const form = screen.getByRole("form", { name: "Sign in to Hermes Cloud" });
    await user.type(within(form).getByLabelText("Username"), "operator");
    await user.type(within(form).getByLabelText("Password"), "secret");

    fireEvent.submit(form);
    await waitFor(() => expect(firstSignal).toBeInstanceOf(AbortSignal));
    fireEvent.submit(form);

    expect(firstSignal?.aborted).toBe(true);
    expect(await screen.findByRole("button", { name: /macbook-pro/ })).toBeVisible();
    expect(authClient.login).toHaveBeenCalledTimes(2);
  });

  it("aborts a stale Agent session request when switching Agents and does not render cancellation as an error", async () => {
    const secondAgentId = "99999999-9999-4999-8999-999999999999";
    let firstSignal: AbortSignal | undefined;
    const catalogClient = catalogStub({
      listAgents: vi.fn(async () => [
        agentItem(),
        agentItem({ agentId: secondAgentId, agentKey: "linux-workstation" }),
      ]),
      listSessions: vi.fn(async (
        { agentId }: { agentId: string },
        signal?: AbortSignal,
      ) => {
        if (agentId === secondAgentId) {
          return sessionPage({
            sessions: [sessionItem({ agentId: secondAgentId })],
          });
        }
        firstSignal = signal;
        return await new Promise<never>((_resolve, reject) => {
          signal?.addEventListener("abort", () => reject(new SessionCatalogRequestAborted()), { once: true });
        });
      }),
    });
    const user = userEvent.setup();
    render(<ProductionApp authClient={authStub()} catalogClient={catalogClient} runtimeFactory={vi.fn()} />);

    await signIn(user);
    await user.click(await screen.findByRole("button", { name: /macbook-pro/ }));
    await waitFor(() => expect(firstSignal).toBeInstanceOf(AbortSignal));
    await user.click(screen.getByRole("button", { name: "Agents" }));
    await user.click(screen.getByRole("button", { name: /linux-workstation/ }));

    expect(firstSignal?.aborted).toBe(true);
    expect(await screen.findByRole("button", { name: /Fix production connector/ })).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("keeps Agent identity when equal session key and profile exist under another Agent", async () => {
    const secondAgentId = "99999999-9999-4999-8999-999999999999";
    const catalogClient = catalogStub({
      listAgents: vi.fn(async () => [
        agentItem(),
        agentItem({ agentId: secondAgentId, agentKey: "linux-workstation" }),
      ]),
      listSessions: vi.fn(async ({ agentId }: { agentId: string }) => sessionPage({
        sessions: [sessionItem({ agentId, sessionKey: "shared-key", profile: "work" })],
      })),
    });
    const runtimeFactory = vi.fn(() => runtimeStub().runtime);
    const user = userEvent.setup();
    render(<ProductionApp
      authClient={authStub()}
      catalogClient={catalogClient}
      runtimeFactory={runtimeFactory}
    />);

    await signIn(user);
    await user.click(await screen.findByRole("button", { name: /linux-workstation/ }));
    await user.click(await screen.findByRole("button", { name: /Fix production connector/ }));

    expect(runtimeFactory).toHaveBeenCalledWith({
      agentId: secondAgentId,
      sessionId: "88888888-8888-4888-8888-888888888888",
      sessionKey: "shared-key",
      profile: "work",
    });
  });

  it("aborts an in-flight load-more request when returning to the Agent directory", async () => {
    let loadMoreSignal: AbortSignal | undefined;
    const catalogClient = catalogStub({
      listSessions: vi.fn(async (
        { offset }: { offset: number },
        signal?: AbortSignal,
      ) => {
        if (offset === 0) return sessionPage({ total: 2 });
        loadMoreSignal = signal;
        return await new Promise<never>((_resolve, reject) => {
          signal?.addEventListener("abort", () => reject(new SessionCatalogRequestAborted()), { once: true });
        });
      }),
    });
    const user = userEvent.setup();
    render(<ProductionApp authClient={authStub()} catalogClient={catalogClient} runtimeFactory={vi.fn()} />);

    await signIn(user);
    await user.click(await screen.findByRole("button", { name: /macbook-pro/ }));
    await user.click(await screen.findByRole("button", { name: "Load more" }));
    await waitFor(() => expect(loadMoreSignal).toBeInstanceOf(AbortSignal));
    await user.click(screen.getByRole("button", { name: "Agents" }));

    expect(loadMoreSignal?.aborted).toBe(true);
    expect(await screen.findByRole("button", { name: /macbook-pro/ })).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("aborts an in-flight session request only after server logout succeeds", async () => {
    let sessionSignal: AbortSignal | undefined;
    const authClient = authStub();
    const catalogClient = catalogStub({
      listSessions: vi.fn(async (_request: unknown, signal?: AbortSignal) => {
        sessionSignal = signal;
        return await new Promise<never>((_resolve, reject) => {
          signal?.addEventListener("abort", () => reject(new SessionCatalogRequestAborted()), { once: true });
        });
      }),
    });
    const user = userEvent.setup();
    render(<ProductionApp authClient={authClient} catalogClient={catalogClient} runtimeFactory={vi.fn()} />);

    await signIn(user);
    await user.click(await screen.findByRole("button", { name: /macbook-pro/ }));
    await waitFor(() => expect(sessionSignal).toBeInstanceOf(AbortSignal));
    await user.click(screen.getByRole("button", { name: "Sign out" }));

    await waitFor(() => expect(authClient.logout).toHaveBeenCalledOnce());
    expect(sessionSignal?.aborted).toBe(true);
    expect(screen.getByRole("form", { name: "Sign in to Hermes Cloud" })).toBeVisible();
  });

  it("fails closed when pagination repeats the same authoritative Agent/profile/session identity with another id", async () => {
    const pageOne = sessionPage({
      sessions: [sessionItem({ sessionKey: "duplicate-lineage" })],
      total: 2,
    });
    const pageTwo = sessionPage({
      sessions: [sessionItem({
        id: "99999999-9999-4999-8999-999999999999",
        sessionKey: "duplicate-lineage",
      })],
      total: 2,
      offset: 1,
    });
    const catalogClient = catalogStub({
      listSessions: vi.fn(async ({ offset }: { offset: number }) => offset === 0 ? pageOne : pageTwo),
    });
    const runtimeFactory = vi.fn();
    const user = userEvent.setup();
    render(<ProductionApp
      authClient={authStub()}
      catalogClient={catalogClient}
      runtimeFactory={runtimeFactory}
      initialSessionKey="session-target"
    />);

    await signIn(user);
    await user.click(await screen.findByRole("button", { name: /macbook-pro/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Hermes returned a duplicate session page");
    expect(runtimeFactory).not.toHaveBeenCalled();
  });

  it("caps automatic ?session discovery at twenty pages before issuing an unbounded search", async () => {
    const catalogClient = catalogStub({
      listSessions: vi.fn(async ({ offset }: { offset: number }) => {
        if (offset !== 0) throw new Error("automatic search issued an out-of-budget request");
        return sessionPage({ total: 401 });
      }),
    });
    const user = userEvent.setup();
    render(<ProductionApp
      authClient={authStub()}
      catalogClient={catalogClient}
      runtimeFactory={vi.fn()}
      initialSessionKey="session-not-present"
    />);

    await signIn(user);
    await user.click(await screen.findByRole("button", { name: /macbook-pro/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Hermes session directory exceeds the automatic search limit",
    );
    expect(catalogClient.listSessions).toHaveBeenCalledOnce();
  });
});

async function signIn(user: ReturnType<typeof userEvent.setup>) {
  const form = screen.getByRole("form", { name: "Sign in to Hermes Cloud" });
  await user.type(within(form).getByLabelText("Username"), "operator");
  await user.type(within(form).getByLabelText("Password"), "secret");
  await user.click(within(form).getByRole("button", { name: "Sign in" }));
}

function runtimeStub() {
  const stop = vi.fn();
  const start = vi.fn(() => stop);
  return { start, stop, runtime: {
    start,
    submitPrompt: vi.fn(),
    steer: vi.fn(),
    interrupt: vi.fn(),
    respondApproval: vi.fn(),
    respondClarification: vi.fn(),
  } };
}

function catalogStub(overrides: Record<string, unknown> = {}) {
  return {
    listAgents: vi.fn(async () => [{
      agentId: AGENT_ID,
      workspaceId: WORKSPACE_ID,
      agentKey: "macbook-pro",
      status: "active" as const,
      lastSeenAt: "2026-08-02T08:30:00+00:00",
    }]),
    listSessions: vi.fn(async () => sessionPage()),
    ...overrides,
  };
}

function authStub(overrides: Record<string, unknown> = {}) {
  return {
    login: vi.fn(async () => ({ ok: true as const })),
    logout: vi.fn(async () => ({ ok: true as const })),
    ...overrides,
  };
}

function agentItem(overrides: Record<string, unknown> = {}) {
  return {
    agentId: AGENT_ID,
    workspaceId: WORKSPACE_ID,
    agentKey: "macbook-pro",
    status: "active" as const,
    lastSeenAt: "2026-08-02T08:30:00+00:00",
    ...overrides,
  };
}

function sessionPage(overrides: Record<string, unknown> = {}) {
  return {
    sessions: [sessionItem()],
    total: 1,
    limit: 20,
    offset: 0,
    profile: "work",
    ...overrides,
  };
}

function sessionItem(overrides: Record<string, unknown> = {}) {
  return {
    id: "88888888-8888-4888-8888-888888888888",
    agentId: AGENT_ID,
    workspaceId: WORKSPACE_ID,
    sessionKey: "session-real-1",
    profile: "work",
    title: "Fix production connector",
    lastActive: 1_785_634_200,
    messageCount: 12,
    isActive: true,
    directorySource: "transcript_projection" as const,
    availability: "live" as const,
    runtimeGeneration: null,
    surface: null,
    authorityRevision: null,
    availableActions: [],
    transcriptAvailable: true,
    ...overrides,
  };
}
