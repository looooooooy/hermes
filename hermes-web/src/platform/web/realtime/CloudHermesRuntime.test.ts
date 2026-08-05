import type { RuntimeCallbacks, RuntimeControlState } from "../../../app/runtimePort";
import { CONTROL_ERROR_CODES } from "../../../shared/contracts/cloudRealtime";
import { CloudHermesRuntime } from "./CloudHermesRuntime";
import { CookieTicketAuthenticationUnavailable } from "./HttpTicketProvider";
import type { WebSocketLike } from "./CloudRealtimeAdapter";

const AGENT_ID = "66666666-6666-4666-8666-666666666666";
const SESSION_ID = "88888888-8888-4888-8888-888888888888";

describe("CloudHermesRuntime", () => {
  it("composes observer and control sockets into a non-null lease using the current Cloud method tuple", async () => {
    const sockets: FakeSocket[] = [];
    const tickets: unknown[] = [];
    const controlStates: RuntimeControlState[] = [];
    const callbacks: RuntimeCallbacks = {
      onConnectionChanged: vi.fn(),
      onSubscription: vi.fn(),
      onEvent: vi.fn(),
      onControlReady: vi.fn(),
      onControlStateChanged: (state) => controlStates.push(state),
      onRunningChanged: vi.fn(),
    };
    const runtime = new CloudHermesRuntime({
      websocketUrl: "wss://cloud.example/api/ws",
      agentId: AGENT_ID,
      sessionId: SESSION_ID,
      sessionKey: "mobile-56f3",
      profile: "default",
      clientInstanceId: "client-1",
      ticketProvider: {
        mint: async (request) => {
          tickets.push(request);
          return `${request.connectionRole}-ticket`;
        },
      },
      socketFactory: (url, protocol) => {
        const socket = new FakeSocket(url, protocol);
        sockets.push(socket);
        return socket;
      },
      now: () => 1_000,
      scheduleLease: () => 1,
      cancelLease: () => undefined,
    });

    const stop = runtime.start(callbacks);
    await flushPromises();
    const observer = sockets.find((socket) => socket.url.includes("observer-ticket"))!;
    const control = sockets.find((socket) => socket.url.includes("control-ticket"))!;
    expect(tickets).toEqual([
      {
        connectionRole: "observer",
        observerContract: 2,
        agentId: AGENT_ID,
        sessionId: SESSION_ID,
      },
      {
        connectionRole: "control",
        agentId: AGENT_ID,
        sessionId: SESSION_ID,
      },
    ]);
    expect(observer.protocol).toBe("hermes.tui.v2");
    expect(control.protocol).toBe("hermes.tui.v1");
    observer.open();
    observer.message(observerReady());
    observer.message(subscriptionResult(1));
    control.open();
    control.message(controlReady());
    await flushPromises();

    expect(control.sent.at(-1)).toEqual({
      jsonrpc: "2.0",
      id: 1,
      method: "session.control.status",
      params: { session_id: SESSION_ID },
    });
    control.message({
      jsonrpc: "2.0",
      id: 1,
      result: statusResult({
        controller_kind: "none",
        controller_label: null,
        control_revision: 7,
        lease_expires_at_epoch_ms: 0,
      }),
    });
    await flushPromises();
    expect(control.sent.at(-1)?.method).toBe("session.control.acquire");
    control.message({ jsonrpc: "2.0", id: 2, result: leaseResult() });
    await flushPromises();
    expect(control.sent.at(-1)?.method).toBe("session.control.status");
    control.message({ jsonrpc: "2.0", id: 3, result: statusResult() });
    await flushPromises();

    expect(controlStates.at(-1)).toMatchObject({
      leaseId: "lease-1",
      leaseExpiresAtEpochMs: 11_000,
      controlRevision: 8,
    });
    await expect(runtime.submitPrompt({
      sessionId: SESSION_ID,
      leaseId: "lease-1",
      clientRequestId: "request-1",
      clientTurnId: "turn-1",
      text: "hello",
    })).rejects.toThrow("not advertised");

    stop();
  });

  it("aborts both pending ticket mints on stop without scheduling a late reconnect", async () => {
    const signals: AbortSignal[] = [];
    const scheduleReconnect = vi.fn(() => 1);
    const runtime = new CloudHermesRuntime({
      websocketUrl: "wss://cloud.example/api/ws",
      agentId: AGENT_ID,
      sessionId: SESSION_ID,
      sessionKey: "mobile-56f3",
      profile: "default",
      clientInstanceId: "client-1",
      ticketProvider: {
        mint: async (_request, signal) => {
          if (signal === undefined) throw new Error("missing ticket AbortSignal");
          signals.push(signal);
          return await new Promise<never>((_resolve, reject) => {
            signal.addEventListener("abort", () => {
              reject(new DOMException("aborted", "AbortError"));
            }, { once: true });
          });
        },
      },
      scheduleReconnect,
      cancelReconnect: vi.fn(),
    });
    const stop = runtime.start(callbacksStub());
    await flushPromises();

    stop();
    await flushPromises();
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(signals).toHaveLength(2);
    expect(signals.every((signal) => signal.aborted)).toBe(true);
    expect(scheduleReconnect).not.toHaveBeenCalled();
  });

  it("closes both sockets and rejects pending RPCs when lease release never responds", async () => {
    const sockets: FakeSocket[] = [];
    const scheduleReconnect = vi.fn();
    const callbacks = runtimeCallbacks();
    const runtime = new CloudHermesRuntime({
      websocketUrl: "wss://cloud.example/api/ws",
      agentId: AGENT_ID,
      sessionId: SESSION_ID,
      sessionKey: "mobile-56f3",
      profile: "default",
      clientInstanceId: "client-1",
      ticketProvider: { mint: async ({ connectionRole }) => `${connectionRole}-ticket` },
      socketFactory: (url, protocol) => {
        const socket = new FakeSocket(url, protocol);
        sockets.push(socket);
        return socket;
      },
      now: () => 1_000,
      scheduleLease: () => 1,
      cancelLease: () => undefined,
      scheduleReconnect,
    });
    runtime.start(callbacks);
    await flushPromises();
    const observer = sockets[0];
    const control = sockets[1];
    connectObserver(observer);
    control.open();
    control.message(controlReady(["prompt.submit"]));
    await flushPromises();
    control.message({
      jsonrpc: "2.0",
      id: 1,
      result: statusResult({
        controller_kind: "none",
        controller_label: null,
        control_revision: 7,
        lease_expires_at_epoch_ms: 0,
      }),
    });
    await flushPromises();
    control.message({ jsonrpc: "2.0", id: 2, result: leaseResult() });
    await flushPromises();
    control.message({ jsonrpc: "2.0", id: 3, result: statusResult() });
    await flushPromises();
    const pendingCommand = runtime.submitPrompt({
      sessionId: SESSION_ID,
      leaseId: "lease-1",
      clientRequestId: "request-1",
      clientTurnId: "turn-1",
      text: "hello",
    });

    const stopping = runtime.stop();

    expect(observer.closeCalls).toEqual([{ code: 1000, reason: "client_close" }]);
    expect(control.sent.some(({ method }) => method === "session.control.release")).toBe(true);
    expect(control.closeCalls).toEqual([{ code: 1000, reason: "client_close" }]);
    await expect(pendingCommand).rejects.toThrow("control connection closed");
    await expect(stopping).resolves.toBeUndefined();
    control.message({ jsonrpc: "2.0", id: 5, result: { released: true, control_revision: 9 } });
    await flushPromises();
    expect(scheduleReconnect).not.toHaveBeenCalled();
    expect(callbacks.onControlReady).not.toHaveBeenCalledWith([]);
  });

  it("bounds initial observer reconnect with exponential backoff and allows explicit retry", async () => {
    const scheduled: Array<{ callback: () => void; delay: number }> = [];
    const mint = vi.fn(async ({ connectionRole }: { connectionRole: "observer" | "control" }) => {
      if (connectionRole === "control") throw new CookieTicketAuthenticationUnavailable();
      throw new Error("network unavailable");
    });
    const runtime = new CloudHermesRuntime({
      websocketUrl: "wss://cloud.example/api/ws",
      agentId: AGENT_ID,
      sessionId: SESSION_ID,
      sessionKey: "mobile-56f3",
      profile: "default",
      clientInstanceId: "client-1",
      ticketProvider: { mint },
      scheduleReconnect: (callback, delay) => {
        scheduled.push({ callback, delay });
        return scheduled.length;
      },
      cancelReconnect: () => undefined,
    });
    const callbacks = runtimeCallbacks();

    runtime.start(callbacks);
    await flushPromises();
    expect(scheduled.map(({ delay }) => delay)).toEqual([500]);
    scheduled[0].callback();
    await flushPromises();
    scheduled[1].callback();
    await flushPromises();
    scheduled[2].callback();
    await flushPromises();
    expect(scheduled.map(({ delay }) => delay)).toEqual([500, 1_000, 2_000]);
    expect(mint.mock.calls.filter(([request]) => request.connectionRole === "observer")).toHaveLength(4);

    runtime.retryConnection();
    await flushPromises();
    expect(scheduled.at(-1)?.delay).toBe(500);
  });

  it("fails closed on authentication without automatic reconnect", async () => {
    const scheduleReconnect = vi.fn();
    const callbacks = runtimeCallbacks();
    const runtime = new CloudHermesRuntime({
      websocketUrl: "wss://cloud.example/api/ws",
      agentId: AGENT_ID,
      sessionId: SESSION_ID,
      sessionKey: "mobile-56f3",
      profile: "default",
      clientInstanceId: "client-1",
      ticketProvider: { mint: async () => { throw new CookieTicketAuthenticationUnavailable(); } },
      scheduleReconnect,
    });

    runtime.start(callbacks);
    await flushPromises();

    expect(scheduleReconnect).not.toHaveBeenCalled();
    expect(callbacks.onConnectionChanged).toHaveBeenCalledWith("disconnected");
    expect(callbacks.onControlStateChanged).toHaveBeenCalledWith(expect.objectContaining({
      leaseId: null,
      unavailableReason: expect.stringContaining("authentication"),
    }));
  });

  it("does not let a late old StrictMode release affect the sockets owned by a new start", async () => {
    const sockets: FakeSocket[] = [];
    let ticketSequence = 0;
    const runtime = new CloudHermesRuntime({
      websocketUrl: "wss://cloud.example/api/ws",
      agentId: AGENT_ID,
      sessionId: SESSION_ID,
      sessionKey: "mobile-56f3",
      profile: "default",
      clientInstanceId: "client-1",
      ticketProvider: {
        mint: async ({ connectionRole }) => `${connectionRole}-ticket-${++ticketSequence}`,
      },
      socketFactory: (url, protocol) => {
        const socket = new FakeSocket(url, protocol);
        sockets.push(socket);
        return socket;
      },
      now: () => 1_000,
      scheduleLease: () => 1,
      cancelLease: () => undefined,
    });
    const firstCallbacks = runtimeCallbacks();
    const stopFirst = runtime.start(firstCallbacks);
    await flushPromises();
    const observer = sockets.find((socket) => socket.url.includes("observer-ticket"))!;
    const control = sockets.find((socket) => socket.url.includes("control-ticket"))!;
    observer.open();
    observer.message(observerReady());
    observer.message(subscriptionResult(1));
    control.open();
    control.message(controlReady());
    await flushPromises();
    control.message({
      jsonrpc: "2.0",
      id: 1,
      result: statusResult({
        controller_kind: "none",
        controller_label: null,
        control_revision: 7,
        lease_expires_at_epoch_ms: 0,
      }),
    });
    await flushPromises();
    control.message({ jsonrpc: "2.0", id: 2, result: leaseResult() });
    await flushPromises();
    control.message({ jsonrpc: "2.0", id: 3, result: statusResult() });
    await flushPromises();

    stopFirst();
    expect(control.sent.at(-1)?.method).toBe("session.control.release");
    const releaseId = control.sent.at(-1)?.id as number;
    expect(observer.closeCalls).toEqual([{ code: 1000, reason: "client_close" }]);
    expect(control.closeCalls).toEqual([{ code: 1000, reason: "client_close" }]);
    const secondCallbacks = runtimeCallbacks();
    runtime.start(secondCallbacks);
    await flushPromises();
    const secondControl = sockets.at(-1)!;
    control.message({ jsonrpc: "2.0", id: releaseId, result: { released: true, control_revision: 9 } });
    await flushPromises();

    expect(secondControl.closeCalls).toEqual([]);
    expect(secondCallbacks.onControlReady).not.toHaveBeenCalledWith([]);
  });

  it("reacquires through a new runtime while the old best-effort release remains unanswered", async () => {
    const oldSockets: FakeSocket[] = [];
    const newSockets: FakeSocket[] = [];
    const scheduled: Array<{ callback: () => void; delay: number; cleared: boolean }> = [];
    const oldRuntime = runtimeWithSockets(oldSockets, "old", () => 1);
    const newRuntime = runtimeWithSockets(newSockets, "new", (callback, delay) => {
      const handle = { callback, delay, cleared: false };
      scheduled.push(handle);
      return handle;
    }, (handle) => { (handle as (typeof scheduled)[number]).cleared = true; });
    const oldStates: RuntimeControlState[] = [];
    const newStates: RuntimeControlState[] = [];
    const oldStop = oldRuntime.start(runtimeCallbacksWithStates(oldStates));
    await flushPromises();
    const oldObserver = oldSockets[0];
    const oldControl = oldSockets[1];
    connectObserver(oldObserver);
    oldControl.open();
    oldControl.message(controlReady());
    await flushPromises();
    oldControl.message({
      jsonrpc: "2.0",
      id: 1,
      result: statusResult({
        controller_kind: "none",
        controller_label: null,
        control_revision: 7,
        lease_expires_at_epoch_ms: 0,
      }),
    });
    await flushPromises();
    oldControl.message({ jsonrpc: "2.0", id: 2, result: leaseResult() });
    await flushPromises();
    oldControl.message({ jsonrpc: "2.0", id: 3, result: statusResult() });
    await flushPromises();
    expect(oldStates.at(-1)?.leaseId).toBe("lease-1");

    oldStop();
    const oldReleaseId = oldControl.sent.at(-1)?.id as number;
    expect(oldControl.sent.at(-1)?.method).toBe("session.control.release");
    expect(oldControl.closeCalls).toEqual([{ code: 1000, reason: "client_close" }]);

    newRuntime.start(runtimeCallbacksWithStates(newStates));
    await flushPromises();
    const newObserver = newSockets[0];
    const newControl = newSockets[1];
    connectObserver(newObserver);
    newControl.open();
    newControl.message(controlReady());
    await flushPromises();
    newControl.message({ jsonrpc: "2.0", id: 1, result: statusResult() });
    await flushPromises();
    expect(newControl.sent.at(-1)?.method).toBe("session.control.acquire");
    newControl.message({
      jsonrpc: "2.0",
      id: 2,
      error: { code: 4203, message: "controller_conflict" },
    });
    await flushPromises();
    expect(newControl.sent.at(-1)?.method).toBe("session.control.status");
    newControl.message({ jsonrpc: "2.0", id: 3, result: statusResult() });
    await flushPromises();

    expect(newStates.at(-1)).toMatchObject({ leaseId: null, controllerKind: "mobile" });
    expect(scheduled).toHaveLength(1);

    oldControl.message({
      jsonrpc: "2.0",
      id: oldReleaseId,
      result: { released: true, control_revision: 9 },
    });
    await flushPromises();
    expect(oldControl.closeCalls).toEqual([{ code: 1000, reason: "client_close" }]);
    expect(newControl.closeCalls).toEqual([]);

    scheduled[0].callback();
    await flushPromises();
    expect(newControl.sent.at(-1)?.method).toBe("session.control.status");
    newControl.message({
      jsonrpc: "2.0",
      id: 4,
      result: statusResult({
        controller_kind: "none",
        controller_label: null,
        control_revision: 9,
        lease_expires_at_epoch_ms: 0,
      }),
    });
    await flushPromises();
    expect(newControl.sent.at(-1)?.method).toBe("session.control.status");
    newControl.message({
      jsonrpc: "2.0",
      id: 5,
      result: statusResult({
        controller_kind: "none",
        controller_label: null,
        control_revision: 9,
        lease_expires_at_epoch_ms: 0,
      }),
    });
    await flushPromises();
    expect(newControl.sent.at(-1)?.method).toBe("session.control.acquire");
    newControl.message({
      jsonrpc: "2.0",
      id: 6,
      result: leaseResult({ lease_id: "lease-2", control_revision: 10 }),
    });
    await flushPromises();
    newControl.message({
      jsonrpc: "2.0",
      id: 7,
      result: statusResult({ control_revision: 10 }),
    });
    await flushPromises();

    expect(newStates.at(-1)).toMatchObject({
      leaseId: "lease-2",
      runtimeSessionId: SESSION_ID,
      controlRevision: 10,
    });
    expect(newControl.sent.filter(({ method }) => method === "session.control.acquire")).toHaveLength(2);
  });
});

function runtimeWithSockets(
  sockets: FakeSocket[],
  ticketPrefix: string,
  scheduleLease: NonNullable<ConstructorParameters<typeof CloudHermesRuntime>[0]["scheduleLease"]>,
  cancelLease: NonNullable<ConstructorParameters<typeof CloudHermesRuntime>[0]["cancelLease"]> = () => undefined,
): CloudHermesRuntime {
  return new CloudHermesRuntime({
    websocketUrl: "wss://cloud.example/api/ws",
    agentId: AGENT_ID,
    sessionId: SESSION_ID,
    sessionKey: "mobile-56f3",
    profile: "default",
    clientInstanceId: "client-1",
    ticketProvider: { mint: async ({ connectionRole }) => `${ticketPrefix}-${connectionRole}-ticket` },
    socketFactory: (url, protocol) => {
      const socket = new FakeSocket(url, protocol);
      sockets.push(socket);
      return socket;
    },
    now: () => 1_000,
    scheduleLease,
    cancelLease,
  });
}

function runtimeCallbacksWithStates(states: RuntimeControlState[]): RuntimeCallbacks {
  return {
    ...runtimeCallbacks(),
    onControlStateChanged: (state) => states.push(state),
  };
}

function connectObserver(socket: FakeSocket): void {
  socket.open();
  socket.message(observerReady());
  socket.message(subscriptionResult(1));
}

function runtimeCallbacks(): RuntimeCallbacks {
  return {
    onConnectionChanged: vi.fn(),
    onSubscription: vi.fn(),
    onEvent: vi.fn(),
    onControlReady: vi.fn(),
    onControlStateChanged: vi.fn(),
    onRunningChanged: vi.fn(),
  };
}

function callbacksStub(): RuntimeCallbacks {
  return {
    onConnectionChanged: vi.fn(),
    onSubscription: vi.fn(),
    onEvent: vi.fn(),
    onControlReady: vi.fn(),
    onControlStateChanged: vi.fn(),
    onRunningChanged: vi.fn(),
  };
}

class FakeSocket implements WebSocketLike {
  readonly sent: Array<Record<string, unknown>> = [];
  readonly closeCalls: Array<{ code: number; reason: string }> = [];
  private readonly listeners = new Map<string, Array<(event: unknown) => void>>();

  constructor(readonly url: string, readonly protocol: string) {}

  addEventListener(type: string, listener: (event: unknown) => void): void {
    const current = this.listeners.get(type) ?? [];
    current.push(listener);
    this.listeners.set(type, current);
  }

  send(data: string): void {
    this.sent.push(JSON.parse(data) as Record<string, unknown>);
  }

  close(code: number, reason: string): void {
    this.closeCalls.push({ code, reason });
    this.emit("close", { code, reason });
  }

  open(): void {
    this.emit("open", {});
  }

  message(value: unknown): void {
    this.emit("message", { data: JSON.stringify(value) });
  }

  private emit(type: string, event: unknown): void {
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
}

function observerReady() {
  return {
    jsonrpc: "2.0",
    method: "event",
    params: { type: "gateway.ready", payload: { observer_contract: 2, connection_role: "observer" } },
  };
}

function controlReady(additionalMethods: readonly string[] = []) {
  return {
    jsonrpc: "2.0",
    method: "event",
    params: {
      type: "gateway.ready",
      payload: {
        observer_contract: 1,
        control_contract: 1,
        connection_role: "control",
        control_available_methods: [
          "session.control.acquire",
          "session.control.renew",
          "session.control.release",
          "session.control.status",
          ...additionalMethods,
        ],
        control_error_codes: CONTROL_ERROR_CODES,
      },
    },
  };
}

function subscriptionResult(id: number) {
  return {
    jsonrpc: "2.0",
    id,
    result: {
      observer_contract: 2,
      subscription_id: "subscription-1",
      profile: "default",
      runtime_generation: "generation-1",
      session_id: SESSION_ID,
      running: true,
      status: "running",
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
  };
}

function leaseResult(overrides: Record<string, unknown> = {}) {
  return {
    lease_id: "lease-1",
    expires_at_epoch_ms: 11_000,
    control_revision: 8,
    controller_kind: "mobile",
    controller_label: "Hermes Web",
    pending_input: null,
    ...overrides,
  };
}

function statusResult(overrides: Record<string, unknown> = {}) {
  return {
    controller_kind: "mobile",
    controller_label: "Hermes Web",
    control_revision: 8,
    lease_expires_at_epoch_ms: 11_000,
    pending_input: null,
    ...overrides,
  };
}

async function flushPromises(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}
