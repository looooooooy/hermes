import { CONTROL_ERROR_CODES } from "../../../shared/contracts/cloudRealtime";
import { CloudRealtimeAdapter, type WebSocketLike } from "./CloudRealtimeAdapter";
import { HttpTicketProvider } from "./HttpTicketProvider";

describe("Web production client against a strict Cloud v2 protocol harness", () => {
  it("claims exact tickets and keeps observer v2 snapshot/replay/live beside approval control v1", async () => {
    const cloud = new StrictCloudHarness();
    const subscriptions: unknown[] = [];
    const events: unknown[] = [];
    const observer = cloud.adapter("observer", {
      observerContract: 2,
      onSubscription: (value) => subscriptions.push(value),
      onEvent: (value) => events.push(value),
    });
    const control = cloud.adapter("control");

    await Promise.all([observer.connect(), control.connect()]);
    expect(cloud.ticketBodies).toEqual([
      {
        connection_role: "observer",
        client_instance_id: CLIENT_INSTANCE_ID,
        agent_id: AGENT_ID,
        observer_contract: 2,
      },
      {
        connection_role: "control",
        client_instance_id: CLIENT_INSTANCE_ID,
        agent_id: AGENT_ID,
        session_id: SESSION_ID,
      },
    ]);

    const observerSocket = cloud.socket("observer");
    const controlSocket = cloud.socket("control");
    expect(observerSocket.protocol).toBe("hermes.tui.v2");
    expect(controlSocket.protocol).toBe("hermes.tui.v1");
    observerSocket.open(observerReadyV2());
    controlSocket.open(controlReadyV1());
    expect(observerSocket.sent).toEqual([{
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

    observerSocket.message(subscriptionResultV2(1, {
      event_sequence: 5,
      snapshot_event_sequence: 4,
      replay_events: [todoUpdateV2(5, 2).params],
    }));
    observerSocket.message(subagentUpdateV2(6));
    expect(subscriptions).toHaveLength(1);
    expect(events).toHaveLength(2);

    const approval = control.call("approval.respond", {
      session_id: SESSION_ID,
      approval_id: "approval-1",
      choice: "approve",
    });
    expect(controlSocket.sent.at(-1)).toMatchObject({ method: "approval.respond" });
    controlSocket.message({ jsonrpc: "2.0", id: 1, result: { accepted: true, control_revision: 8 } });
    await expect(approval).resolves.toEqual({ accepted: true, control_revision: 8 });
    expect(cloud.claims).toEqual([
      { role: "observer", observerContract: 2, protocol: "hermes.tui.v2" },
      { role: "control", observerContract: 1, protocol: "hermes.tui.v1" },
    ]);
  });

  it("fails closed on gaps, runtime rollover, and a v1 ready frame without downgrade", async () => {
    const scenarios = [
      {
        name: "gap",
        live: () => todoUpdateV2(7, 2),
        wireReason: "resync_required",
      },
      {
        name: "runtime rollover",
        live: () => {
          const event = todoUpdateV2(5, 2);
          event.params.runtime_generation = "generation-2";
          return event;
        },
        wireReason: "resync_required",
      },
    ] as const;

    for (const scenario of scenarios) {
      const cloud = new StrictCloudHarness(scenario.name);
      const observer = cloud.adapter("observer", { observerContract: 2 });
      await observer.connect();
      const socket = cloud.socket("observer");
      socket.open(observerReadyV2());
      socket.message(subscriptionResultV2(1));
      socket.message(scenario.live());
      expect(socket.closes.at(-1), scenario.name).toEqual([1002, scenario.wireReason]);
    }

    const downgrade = new StrictCloudHarness("downgrade");
    const observer = downgrade.adapter("observer", { observerContract: 2 });
    await observer.connect();
    const socket = downgrade.socket("observer");
    socket.open(observerReadyV1());
    expect(socket.closes.at(-1)).toEqual([1002, "invalid_frame"]);
    expect(socket.sent).toHaveLength(0);
  });

  it("never presents the same single-use ticket to a second socket claim", async () => {
    const cloud = new StrictCloudHarness("reused", true);
    const observer = cloud.adapter("observer", { observerContract: 2 });
    await observer.connect();
    cloud.socket("observer").close(1006, "network");

    await expect(observer.connect()).rejects.toThrow("single-use ticket was reused");
    expect(cloud.claims).toHaveLength(1);
  });
});

const SESSION_KEY = "mobile-56f3";
const SESSION_ID = "88888888-8888-4888-8888-888888888888";
const CLIENT_INSTANCE_ID = "11111111-1111-4111-8111-111111111111";
const AGENT_ID = "66666666-6666-4666-8666-666666666666";

class StrictCloudHarness {
  readonly ticketBodies: Record<string, unknown>[] = [];
  readonly claims: Array<{ role: "observer" | "control"; observerContract: 1 | 2; protocol: string }> = [];
  private readonly tickets = new Map<string, { role: "observer" | "control"; observerContract: 1 | 2 }>();
  private readonly claimed = new Set<string>();
  private readonly sockets: StrictSocket[] = [];
  private nextTicket = 1;

  constructor(private readonly label = "default", private readonly repeatTicket = false) {}

  readonly fetcher = async (_input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
    this.ticketBodies.push(body);
    const role = body.connection_role === "control" ? "control" : "observer";
    const observerContract = body.observer_contract === 2 ? 2 : 1;
    const suffix = this.repeatTicket ? 1 : this.nextTicket++;
    const ticket = `${this.label}-${role}-${suffix}-ticket-value`.padEnd(48, "x");
    this.tickets.set(ticket, { role, observerContract });
    return new Response(JSON.stringify({
      ticket,
      ttl_seconds: 30,
      connection_role: role,
      ...(observerContract === 2 ? { observer_contract: 2 } : {}),
    }), { status: 200, headers: { "content-type": "application/json" } });
  };

  adapter(
    role: "observer" | "control",
    callbacks: {
      observerContract?: 1 | 2;
      onSubscription?: (value: unknown) => void;
      onEvent?: (value: unknown) => void;
    } = {},
  ): CloudRealtimeAdapter {
    const provider = new HttpTicketProvider({
      endpoint: "https://cloud.example/api/auth/ws-ticket",
      clientInstanceId: CLIENT_INSTANCE_ID,
      fetcher: this.fetcher as typeof fetch,
    });
    return new CloudRealtimeAdapter({
      role,
      ...(callbacks.observerContract === undefined ? {} : { observerContract: callbacks.observerContract }),
      websocketUrl: "wss://cloud.example/api/ws",
      agentId: AGENT_ID,
      sessionId: SESSION_ID,
      sessionKey: SESSION_KEY,
      profile: "default",
      ticketProvider: provider,
      socketFactory: (url, protocol) => this.claim(url, protocol),
      onSnapshot: () => undefined,
      onSubscription: callbacks.onSubscription,
      onEvent: callbacks.onEvent ?? (() => undefined),
    });
  }

  socket(role: "observer" | "control"): StrictSocket {
    const socket = this.sockets.find((candidate) => candidate.role === role);
    if (socket === undefined) throw new Error(`missing ${role} socket`);
    return socket;
  }

  private claim(url: string, protocol: string): StrictSocket {
    const ticket = new URL(url).searchParams.get("ticket");
    if (ticket === null || this.claimed.has(ticket)) throw new Error("ticket claim rejected");
    const binding = this.tickets.get(ticket);
    if (binding === undefined) throw new Error("unknown ticket");
    const expectedProtocol = binding.observerContract === 2 ? "hermes.tui.v2" : "hermes.tui.v1";
    if (protocol !== expectedProtocol) throw new Error("ticket protocol mismatch");
    this.claimed.add(ticket);
    this.claims.push({ role: binding.role, observerContract: binding.observerContract, protocol });
    const socket = new StrictSocket(binding.role, protocol);
    this.sockets.push(socket);
    return socket;
  }
}

class StrictSocket implements WebSocketLike {
  readonly sent: Array<Record<string, unknown>> = [];
  readonly closes: Array<[number, string]> = [];
  private readonly listeners = new Map<string, Array<(event: unknown) => void>>();

  constructor(readonly role: "observer" | "control", readonly protocol: string) {}

  addEventListener(type: string, listener: (event: unknown) => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  send(data: string): void {
    this.sent.push(JSON.parse(data) as Record<string, unknown>);
  }

  close(code: number, reason: string): void {
    this.closes.push([code, reason]);
    this.emit("close", { code, reason });
  }

  open(ready: unknown): void {
    this.emit("open", {});
    if (this.closes.length === 0) this.message(ready);
  }

  message(value: unknown): void {
    this.emit("message", { data: JSON.stringify(value) });
  }

  private emit(type: string, value: unknown): void {
    for (const listener of this.listeners.get(type) ?? []) listener(value);
  }
}

function observerReadyV2() {
  return {
    jsonrpc: "2.0",
    method: "event",
    params: { type: "gateway.ready", payload: { observer_contract: 2, connection_role: "observer" } },
  };
}

function observerReadyV1() {
  return {
    jsonrpc: "2.0",
    method: "event",
    params: { type: "gateway.ready", payload: { observer_contract: 1, connection_role: "observer" } },
  };
}

function controlReadyV1() {
  return {
    jsonrpc: "2.0",
    method: "event",
    params: {
      type: "gateway.ready",
      payload: {
        observer_contract: 1,
        control_contract: 1,
        connection_role: "control",
        control_available_methods: ["approval.respond"],
        control_error_codes: CONTROL_ERROR_CODES,
      },
    },
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
  return observerEventV2("todo.update", eventSequence, {
    turn_id: "turn-1",
    section_id: "todo-1",
    revision,
    first_event_sequence: 1,
    operation: "upsert",
    status: "completed",
    items: [{ id: "item-1", label: "Run tests", status: "completed" }],
  });
}

function subagentUpdateV2(eventSequence: number) {
  return observerEventV2("subagent.update", eventSequence, {
    turn_id: "turn-1",
    subagent_id: "agent-1",
    revision: 1,
    first_event_sequence: eventSequence,
    operation: "upsert",
    parent_subagent_id: null,
    name: "Test runner",
    goal: "Run focused tests",
    summary: null,
    status: "running",
  });
}

function observerEventV2(type: string, eventSequence: number, payload: Record<string, unknown>) {
  return {
    jsonrpc: "2.0",
    method: "event",
    params: {
      observer_contract: 2,
      profile: "default",
      runtime_generation: "generation-1",
      type,
      session_id: SESSION_ID,
      event_sequence: eventSequence,
      payload,
    },
  };
}
