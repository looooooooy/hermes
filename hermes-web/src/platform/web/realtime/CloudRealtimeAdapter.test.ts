import { CloudRealtimeAdapter, type WebSocketLike } from "./CloudRealtimeAdapter";
import { CONTROL_ERROR_CODES } from "../../../shared/contracts/cloudRealtime";

const AGENT_ID = "66666666-6666-4666-8666-666666666666";
const SESSION_ID = "88888888-8888-4888-8888-888888888888";

describe("CloudRealtimeAdapter", () => {
  it("negotiates observer v2 across ticket, subprotocol, ready, and exact subscribe without downgrade", async () => {
    const harness = createHarness("observer", ["ticket-v2"], 2);
    await harness.adapter.connect();

    expect(harness.tickets).toEqual([{
      connectionRole: "observer",
      observerContract: 2,
      agentId: AGENT_ID,
      sessionId: SESSION_ID,
    }]);
    expect(harness.socket.protocol).toBe("hermes.tui.v2");
    harness.socket.open();
    harness.socket.message(observerReadyV2());
    expect(harness.socket.sent).toEqual([{
      jsonrpc: "2.0",
      id: 1,
      method: "session.observe.subscribe",
      params: {
        observer_contract: 2,
        session_id: SESSION_ID,
        profile: "default",
        agent_id: AGENT_ID,
      },
    }]);
    harness.socket.message(subscriptionResultV2(1));
    expect(harness.subscriptions).toHaveLength(1);
    expect(harness.snapshots.at(-1)).toEqual({ connection: "connected", reason: "live" });

    const downgraded = createHarness("observer", ["ticket-v2-other"], 2);
    await downgraded.adapter.connect();
    downgraded.socket.open();
    downgraded.socket.message(observerReady());
    expect(downgraded.socket.closes.at(-1)).toEqual([1002, "invalid_frame"]);
  });

  it("requires the server-selected subprotocol to match the version-bound socket role", async () => {
    const observer = createHarness("observer", ["ticket-v2"], 2, "hermes.tui.v1");
    await observer.adapter.connect();
    observer.socket.open();
    expect(observer.socket.closes.at(-1)).toEqual([1002, "subprotocol_mismatch"]);
    expect(observer.socket.sent).toHaveLength(0);

    const control = createHarness("control", ["control-ticket"], undefined, "hermes.tui.v2");
    await control.adapter.connect();
    control.socket.open();
    expect(control.socket.closes.at(-1)).toEqual([1002, "subprotocol_mismatch"]);
  });

  it("drops unsafe v2 extensions before they reach projection or rendering callbacks", async () => {
    const harness = createHarness("observer", ["ticket-v2"], 2);
    await harness.adapter.connect();
    harness.socket.open();
    harness.socket.message(observerReadyV2());
    harness.socket.message(subscriptionResultV2(1));

    const unsafe = todoUpdateV2(5, 2) as ReturnType<typeof todoUpdateV2> & {
      params: { extensions?: Record<string, unknown> };
    };
    unsafe.params.extensions = {
      "com.example.display": {
        nested: [{ approval_payload: { choice: "approve", command: "must-not-render" } }],
      },
    };
    harness.socket.message(unsafe);

    expect(harness.events).toHaveLength(0);
    expect(harness.socket.closes.at(-1)).toEqual([1002, "invalid_frame"]);
  });

  it("closes credential-bearing v2 snapshots before subscription callbacks", async () => {
    const harness = createHarness("observer", ["ticket-v2"], 2);
    await harness.adapter.connect();
    harness.socket.open();
    harness.socket.message(observerReadyV2());
    const credential = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln";
    harness.socket.message(subscriptionResultV2(1, {
      messages: [{ role: "assistant", content: credential }],
    }));

    expect(harness.subscriptions).toHaveLength(0);
    expect(harness.events).toHaveLength(0);
    expect(harness.socket.closes.at(-1)).toEqual([1002, "invalid_subscription"]);
    expect(JSON.stringify(harness.socket.closes)).not.toContain(credential);
  });

  it("closes credential-bearing v2 event text before event callbacks", async () => {
    const harness = createHarness("observer", ["ticket-v2"], 2);
    await harness.adapter.connect();
    harness.socket.open();
    harness.socket.message(observerReadyV2());
    harness.socket.message(subscriptionResultV2(1));
    const credential = "password=hunter2";
    harness.socket.message(messageDeltaV2(5, credential));

    expect(harness.subscriptions).toHaveLength(1);
    expect(harness.events).toHaveLength(0);
    expect(harness.socket.closes.at(-1)).toEqual([1002, "invalid_frame"]);
    expect(JSON.stringify(harness.socket.closes)).not.toContain(credential);
  });

  it("closes semantic Basic extensions before event callbacks without echoing credentials", async () => {
    const harness = createHarness("observer", ["ticket-v2"], 2);
    await harness.adapter.connect();
    harness.socket.open();
    harness.socket.message(observerReadyV2());
    harness.socket.message(subscriptionResultV2(1));
    const credential = "Basic dXNlcjpwYXNz";
    const event = todoUpdateV2(5, 2) as ReturnType<typeof todoUpdateV2> & {
      params: { extensions?: Record<string, unknown> };
    };
    event.params.extensions = {
      "com.example.display": { label: credential },
    };
    harness.socket.message(event);

    expect(harness.events).toHaveLength(0);
    expect(harness.socket.closes.at(-1)).toEqual([1002, "invalid_frame"]);
    expect(JSON.stringify(harness.socket.closes)).not.toContain(credential);
  });

  it("closes credential-bearing v2 replay before subscription or event callbacks", async () => {
    const harness = createHarness("observer", ["ticket-v2"], 2);
    await harness.adapter.connect();
    harness.socket.open();
    harness.socket.message(observerReadyV2());
    const credential = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln";
    harness.socket.message(subscriptionResultV2(1, {
      event_sequence: 5,
      snapshot_event_sequence: 4,
      replay_events: [messageDeltaV2(5, credential).params],
    }));

    expect(harness.subscriptions).toHaveLength(0);
    expect(harness.events).toHaveLength(0);
    expect(harness.socket.closes.at(-1)).toEqual([1002, "invalid_subscription"]);
    expect(JSON.stringify(harness.socket.closes)).not.toContain(credential);
  });

  it("validates full v2 replay events and fails closed on revision or runtime-generation conflicts", async () => {
    const replay = todoUpdateV2(5, 2);
    const harness = createHarness("observer", ["ticket-v2"], 2);
    await harness.adapter.connect();
    harness.socket.open();
    harness.socket.message(observerReadyV2());
    harness.socket.message(subscriptionResultV2(1, {
      event_sequence: 5,
      snapshot_event_sequence: 4,
      replay_events: [replay.params],
    }));
    expect(harness.events).toHaveLength(1);

    harness.socket.message(todoUpdateV2(6, 4));
    expect(harness.socket.closes.at(-1)).toEqual([1002, "resync_required"]);
    expect(harness.snapshots.at(-1)?.reason).toBe("resync_required");

    const rollover = createHarness("observer", ["ticket-v2-rollover"], 2);
    await rollover.adapter.connect();
    rollover.socket.open();
    rollover.socket.message(observerReadyV2());
    rollover.socket.message(subscriptionResultV2(1));
    const wrongGeneration = todoUpdateV2(5, 2);
    wrongGeneration.params.runtime_generation = "generation-2";
    rollover.socket.message(wrongGeneration);
    expect(rollover.socket.closes.at(-1)).toEqual([1002, "resync_required"]);
  });

  it("deduplicates seen v2 replay and live transport identities by canonical digest", async () => {
    const replay = todoUpdateV2(5, 2);
    const replayHarness = createHarness("observer", ["ticket-v2-replay"], 2);
    await replayHarness.adapter.connect();
    replayHarness.socket.open();
    replayHarness.socket.message(observerReadyV2());
    replayHarness.socket.message(subscriptionResultV2(1, {
      event_sequence: 5,
      snapshot_event_sequence: 4,
      replay_events: [replay.params],
    }));

    replayHarness.socket.message(replay);
    expect(replayHarness.events).toHaveLength(1);
    expect(replayHarness.socket.closes).toHaveLength(0);

    const conflictingReplay = todoUpdateV2(5, 2);
    conflictingReplay.params.payload.items[0]!.label = "Conflicting replay";
    replayHarness.socket.message(conflictingReplay);
    expect(replayHarness.socket.closes.at(-1)).toEqual([1002, "resync_required"]);

    const liveHarness = createHarness("observer", ["ticket-v2-live"], 2);
    await liveHarness.adapter.connect();
    liveHarness.socket.open();
    liveHarness.socket.message(observerReadyV2());
    liveHarness.socket.message(subscriptionResultV2(1));
    const live = todoUpdateV2(5, 2);
    liveHarness.socket.message(live);
    liveHarness.socket.message(live);
    expect(liveHarness.events).toHaveLength(1);
    expect(liveHarness.socket.closes).toHaveLength(0);

    const conflictingLive = todoUpdateV2(5, 2);
    conflictingLive.params.payload.items[0]!.label = "Conflicting live duplicate";
    liveHarness.socket.message(conflictingLive);
    expect(liveHarness.socket.closes.at(-1)).toEqual([1002, "resync_required"]);
  });

  it("becomes live only after observer ready and a contract-valid subscription snapshot", async () => {
    const harness = createHarness("observer");
    await harness.adapter.connect();

    expect(harness.tickets).toEqual([{
      connectionRole: "observer",
      agentId: AGENT_ID,
      sessionId: SESSION_ID,
    }]);
    expect(harness.socket.protocol).toBe("hermes.tui.v1");
    expect(new URL(harness.socket.url).searchParams.get("ticket")).toBe("ticket-1");
    expect(harness.snapshots.at(-1)).toEqual({ connection: "disconnected", reason: "connecting" });

    harness.socket.open();
    expect(harness.snapshots.at(-1)?.reason).toBe("connecting");
    harness.socket.message(observerReady());
    expect(harness.socket.sent).toEqual([{
      jsonrpc: "2.0",
      id: 1,
      method: "session.observe.subscribe",
      params: { session_key: "mobile-56f3", profile: "default" },
    }]);
    expect(harness.snapshots.at(-1)?.reason).toBe("subscribing");

    harness.socket.message(subscriptionResult(1));
    expect(harness.subscriptions).toHaveLength(1);
    expect(harness.snapshots.at(-1)).toEqual({ connection: "connected", reason: "live" });
  });

  it("enforces session binding and contiguous replay/live sequence with range starts", async () => {
    const harness = createHarness("observer");
    await harness.adapter.connect();
    harness.socket.open();
    harness.socket.message(observerReady());
    harness.socket.message(subscriptionResult(1, {
      event_sequence: 4,
      snapshot_event_sequence: 2,
      replay_events: [
        observerEvent(3, { event_sequence_start: 3 }).params,
        observerEvent(4).params,
      ],
    }));
    expect(harness.events.map((event) => event.params.event_sequence)).toEqual([3, 4]);

    harness.socket.message(observerEvent(5, { event_sequence_start: 5 }));
    harness.socket.message(observerEvent(5));
    expect(harness.events.map((event) => event.params.event_sequence)).toEqual([3, 4, 5]);

    harness.socket.message(observerEvent(7));
    expect(harness.socket.closes.at(-1)).toEqual([1002, "resync_required"]);
    expect(harness.snapshots.at(-1)?.reason).toBe("resync_required");
  });

  it("rotates single-use tickets on reconnect and rejects a reused ticket", async () => {
    const harness = createHarness("observer", ["ticket-1", "ticket-2", "ticket-2"]);
    await harness.adapter.connect();
    harness.socket.close(1006, "network");
    await harness.adapter.connect();
    expect(harness.sockets[1]?.url).toContain("ticket=ticket-2");
    harness.sockets[1]?.close(1006, "network");
    await expect(harness.adapter.connect()).rejects.toThrow("single-use ticket was reused");
    expect(harness.sockets).toHaveLength(2);
  });

  it("keeps concurrent reconnect attempts single-flight while a ticket is pending", async () => {
    const ticket = deferred<string>();
    const sockets: FakeSocket[] = [];
    const mint = vi.fn(() => ticket.promise);
    const adapter = new CloudRealtimeAdapter({
      role: "observer",
      websocketUrl: "wss://cloud.example/api/ws",
      agentId: AGENT_ID,
      sessionId: SESSION_ID,
      sessionKey: "mobile-56f3",
      profile: "default",
      ticketProvider: { mint },
      socketFactory: (url, protocol) => {
        const socket = new FakeSocket(url, protocol);
        sockets.push(socket);
        return socket;
      },
      onSnapshot: () => undefined,
      onEvent: () => undefined,
    });

    const first = adapter.connect();
    const second = adapter.connect();
    expect(mint).toHaveBeenCalledTimes(1);

    ticket.resolve("ticket-1");
    await Promise.all([first, second]);
    expect(sockets).toHaveLength(1);
  });

  it("aborts a pending ticket mint on disconnect without a late socket or ticket", async () => {
    let signal: AbortSignal | undefined;
    let issuedTickets = 0;
    const sockets: FakeSocket[] = [];
    const snapshots: Array<{ connection: string; reason: string }> = [];
    const adapter = new CloudRealtimeAdapter({
      role: "observer",
      websocketUrl: "wss://cloud.example/api/ws",
      agentId: AGENT_ID,
      sessionId: SESSION_ID,
      sessionKey: "mobile-56f3",
      profile: "default",
      ticketProvider: {
        mint: async (_request, requestSignal) => {
          signal = requestSignal;
          return await new Promise<string>((resolve, reject) => {
            const timer = setTimeout(() => {
              issuedTickets += 1;
              resolve("late-ticket");
            }, 1_000);
            requestSignal?.addEventListener("abort", () => {
              clearTimeout(timer);
              reject(new DOMException("aborted", "AbortError"));
            }, { once: true });
          });
        },
      },
      socketFactory: (url, protocol) => {
        const socket = new FakeSocket(url, protocol);
        sockets.push(socket);
        return socket;
      },
      onSnapshot: (snapshot) => snapshots.push(snapshot),
      onEvent: () => undefined,
    });

    const connecting = adapter.connect();
    adapter.disconnect();

    expect(signal?.aborted).toBe(true);
    await expect(connecting).rejects.toMatchObject({ name: "AbortError" });
    expect(issuedTickets).toBe(0);
    expect(sockets).toHaveLength(0);
    expect(snapshots).toEqual([{ connection: "disconnected", reason: "connecting" }]);
  });

  it("closes oversize raw frames before JSON parsing", async () => {
    const harness = createHarness("observer");
    await harness.adapter.connect();
    harness.socket.open();
    harness.socket.rawMessage("x".repeat(262_145));
    expect(harness.socket.closes.at(-1)).toEqual([1009, "message_too_big"]);
  });

  it("rejects a repeated ready frame after the immutable role handshake", async () => {
    const harness = createHarness("observer");
    await harness.adapter.connect();
    harness.socket.open();
    harness.socket.message(observerReady());
    harness.socket.message(subscriptionResult(1));
    harness.socket.message(observerReady());
    expect(harness.socket.closes.at(-1)).toEqual([1002, "unexpected_ready"]);
  });

  it("decodes dynamic control capabilities and correlates RPC result and error frames", async () => {
    const harness = createHarness("control");
    await harness.adapter.connect();
    harness.socket.open();
    harness.socket.message(controlReady());
    expect(harness.controlReady).toEqual([{
      availableMethods: ["prompt.submit", "session.control.acquire", "session.interrupt"],
      errorCodes: { ...CONTROL_ERROR_CODES },
    }]);
    expect(harness.snapshots.at(-1)?.reason).toBe("live");

    const call = harness.adapter.call("prompt.submit", { text: "hello" });
    expect(harness.socket.sent.at(-1)).toEqual({
      jsonrpc: "2.0",
      id: 1,
      method: "prompt.submit",
      params: { text: "hello" },
    });
    harness.socket.message({ jsonrpc: "2.0", id: 1, result: { status: "accepted" } });
    await expect(call).resolves.toEqual({ status: "accepted" });

    const failed = harness.adapter.call("session.interrupt", {});
    harness.socket.message({
      jsonrpc: "2.0",
      id: 2,
      error: { code: 4204, message: "lease required" },
    });
    await expect(failed).rejects.toMatchObject({ code: 4204, message: "lease required" });
    await expect(harness.adapter.call("session.steer", {})).rejects.toThrow("not advertised");
  });

  it("rejects control errors that were not advertised by the ready frame", async () => {
    const harness = createHarness("control");
    await harness.adapter.connect();
    harness.socket.open();
    harness.socket.message(controlReady());
    const call = harness.adapter.call("prompt.submit", { text: "hello" });
    harness.socket.message({ jsonrpc: "2.0", id: 1, error: { code: 4999, message: "unknown" } });
    await expect(call).rejects.toThrow("control connection closed");
    expect(harness.socket.closes.at(-1)).toEqual([1002, "unadvertised_error"]);
  });
});

function createHarness(
  role: "observer" | "control",
  ticketValues = ["ticket-1"],
  observerContract?: 1 | 2,
  selectedProtocol?: string,
) {
  const sockets: FakeSocket[] = [];
  const snapshots: Array<{ connection: string; reason: string }> = [];
  const subscriptions: unknown[] = [];
  const events: Array<ReturnType<typeof observerEvent>> = [];
  const controlReadyValues: unknown[] = [];
  const tickets: unknown[] = [];
  let ticketIndex = 0;
  const adapter = new CloudRealtimeAdapter({
    role,
    ...(observerContract === undefined ? {} : { observerContract }),
    websocketUrl: "wss://cloud.example/api/ws",
    agentId: AGENT_ID,
    sessionId: SESSION_ID,
    sessionKey: "mobile-56f3",
    profile: "default",
    ticketProvider: {
      mint: async (request) => {
        tickets.push(request);
        return ticketValues[ticketIndex++] ?? ticketValues.at(-1)!;
      },
    },
    socketFactory: (url, protocol) => {
      const socket = new FakeSocket(url, selectedProtocol ?? protocol);
      sockets.push(socket);
      return socket;
    },
    onSnapshot: (snapshot) => snapshots.push(snapshot),
    onSubscription: (snapshot) => subscriptions.push(snapshot),
    onEvent: (event) => events.push(event as ReturnType<typeof observerEvent>),
    onControlReady: (ready) => controlReadyValues.push(ready),
  });
  return {
    adapter,
    sockets,
    get socket() { return sockets.at(-1)!; },
    snapshots,
    subscriptions,
    events,
    get controlReady() { return controlReadyValues; },
    tickets,
  };
}

function observerReadyV2() {
  return {
    jsonrpc: "2.0",
    method: "event",
    params: { type: "gateway.ready", payload: { observer_contract: 2, connection_role: "observer" } },
  };
}

function subscriptionResultV2(id: number, overrides: Record<string, unknown> = {}) {
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
      event_sequence: 4,
      snapshot_event_sequence: 4,
      messages: [],
      inflight: { user: null, assistant: null, streaming: false, error: null },
      todo_sections: [{
        turn_id: "turn-1",
        section_id: "todo-1",
        revision: 1,
        first_event_sequence: 1,
        status: "in_progress",
        items: [{ id: "item-1", label: "Run tests", status: "in_progress" }],
      }],
      subagents: [],
      tools: [],
      terminals: [],
      replay_events: [],
      ...overrides,
    },
  };
}

function todoUpdateV2(eventSequence: number, revision: number) {
  return {
    jsonrpc: "2.0",
    method: "event",
    params: {
      observer_contract: 2,
      profile: "default",
      runtime_generation: "generation-1",
      type: "todo.update",
      session_id: SESSION_ID,
      event_sequence: eventSequence,
      payload: {
        turn_id: "turn-1",
        section_id: "todo-1",
        revision,
        first_event_sequence: 1,
        operation: "upsert",
        status: "completed",
        items: [{ id: "item-1", label: "Run tests", status: "completed" }],
      },
    },
  };
}

function messageDeltaV2(eventSequence: number, text: string) {
  return {
    jsonrpc: "2.0",
    method: "event",
    params: {
      observer_contract: 2,
      profile: "default",
      runtime_generation: "generation-1",
      type: "message.delta",
      session_id: SESSION_ID,
      event_sequence: eventSequence,
      payload: { text },
    },
  };
}

class FakeSocket implements WebSocketLike {
  readonly sent: unknown[] = [];
  readonly closes: Array<[number, string]> = [];
  private readonly listeners = new Map<string, Array<(event: unknown) => void>>();

  constructor(readonly url: string, readonly protocol: string) {}

  addEventListener(type: string, listener: (event: unknown) => void): void {
    const current = this.listeners.get(type) ?? [];
    current.push(listener);
    this.listeners.set(type, current);
  }

  send(data: string): void {
    this.sent.push(JSON.parse(data));
  }

  close(code: number, reason: string): void {
    this.closes.push([code, reason]);
    this.emit("close", { code, reason });
  }

  open(): void {
    this.emit("open", {});
  }

  message(value: unknown): void {
    this.rawMessage(JSON.stringify(value));
  }

  rawMessage(data: unknown): void {
    this.emit("message", { data });
  }

  private emit(type: string, event: unknown): void {
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
}

function observerReady() {
  return {
    jsonrpc: "2.0",
    method: "event",
    params: { type: "gateway.ready", payload: { observer_contract: 1, connection_role: "observer" } },
  };
}

function controlReady() {
  return {
    jsonrpc: "2.0",
    method: "event",
    params: {
      type: "gateway.ready",
      payload: {
        observer_contract: 1,
        control_contract: 1,
        connection_role: "control",
        control_available_methods: ["prompt.submit", "session.control.acquire", "session.interrupt"],
        control_error_codes: CONTROL_ERROR_CODES,
      },
    },
  };
}

function subscriptionResult(id: number, overrides: Record<string, unknown> = {}) {
  return {
    jsonrpc: "2.0",
    id,
    result: {
      subscription_id: "subscription-1",
      session_key: "mobile-56f3",
      runtime_session_id: "runtime-56f3",
      running: true,
      status: "running",
      event_sequence: 0,
      snapshot_event_sequence: 0,
      messages: [],
      inflight: { user: null, assistant: null, streaming: false, error: null },
      replay_events: [],
      ...overrides,
    },
  };
}

function observerEvent(sequence: number, overrides: Record<string, unknown> = {}) {
  return {
    jsonrpc: "2.0" as const,
    method: "event" as const,
    params: {
      type: "thinking.delta" as const,
      session_id: "runtime-56f3",
      session_key: "mobile-56f3",
      event_sequence: sequence,
      payload: { text: `event ${sequence}` },
      ...overrides,
    },
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}
