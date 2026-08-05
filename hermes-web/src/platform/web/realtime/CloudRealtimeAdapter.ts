import type { RuntimeConnection } from "../../../app/model";
import {
  decodeCloudRealtimeFrame,
  decodeObserverSubscription,
  eventStart,
  type CloudRealtimeEvent,
  type CloudGatewayReady,
  type CloudRpcError,
  type CloudRpcResult,
  type ControlGatewayReady,
  type ObserverSubscription,
} from "../../../shared/contracts/cloudRealtime";
import {
  CLOUD_OBSERVER_V2_SUBPROTOCOL,
  decodeCloudRealtimeV2Frame,
  decodeObserverSubscriptionV2,
  eventStartV2,
  type CloudRealtimeV2Event,
  type CloudRpcErrorV2,
  type CloudRpcResultV2,
  type ObserverGatewayReadyV2,
  type ObserverSubscriptionV2,
} from "../../../shared/contracts/cloudRealtimeV2";
import {
  canonicalObserverV2Digest,
  ObserverV2ProjectionGuard,
} from "../../../shared/contracts/observerV2ProjectionGuard";

const MAX_FRAME_BYTES = 262_144;
const MAX_V2_TRANSPORT_DIGESTS = 1_024;
const SUBPROTOCOL = "hermes.tui.v1";

export interface RealtimeAdapterSnapshot {
  connection: RuntimeConnection;
  reason:
    | "unconfigured"
    | "connecting"
    | "subscribing"
    | "live"
    | "closed"
    | "invalid_frame"
    | "resync_required"
    | "ticket_reused";
}

export interface TicketRequest {
  connectionRole: "observer" | "control";
  observerContract?: 1 | 2;
  agentId: string;
  sessionId: string;
}

export interface TicketProvider {
  mint(request: TicketRequest, signal?: AbortSignal): Promise<string>;
}

export interface WebSocketLike {
  readonly protocol: string;
  addEventListener(type: string, listener: (event: unknown) => void): void;
  send(data: string): void;
  close(code: number, reason: string): void;
}

export interface ControlReadySnapshot {
  availableMethods: readonly string[];
  errorCodes: Readonly<Record<string, number>>;
}

export interface CloudRealtimeAdapterOptions {
  role: "observer" | "control";
  observerContract?: 1 | 2;
  websocketUrl: string;
  agentId: string;
  sessionId: string;
  sessionKey: string;
  profile?: string;
  ticketProvider: TicketProvider;
  socketFactory?: (url: string, protocol: string) => WebSocketLike;
  onEvent: (event: CloudRealtimeEvent | CloudRealtimeV2Event) => void;
  onSnapshot: (snapshot: RealtimeAdapterSnapshot) => void;
  onSubscription?: (snapshot: ObserverSubscription | ObserverSubscriptionV2) => void;
  onControlReady?: (snapshot: ControlReadySnapshot) => void;
}

export class CloudRpcFailure extends Error {
  constructor(readonly code: number, message: string, readonly data?: Record<string, unknown> | null) {
    super(message);
    this.name = "CloudRpcFailure";
  }
}

/** Strict version-bound client for the public observer v1/v2 and control v1 Cloud boundaries. */
export class CloudRealtimeAdapter {
  private socket: WebSocketLike | null = null;
  private connectPromise: Promise<void> | null = null;
  private mintAbort: AbortController | null = null;
  private connectGeneration = 0;
  private phase: "idle" | "awaiting_ready" | "subscribing" | "live" = "idle";
  private readonly usedTickets = new Set<string>();
  private nextRpcId = 1;
  private subscriptionRpcId: number | null = null;
  private subscription: ObserverSubscription | ObserverSubscriptionV2 | null = null;
  private projectionGuardV2: ObserverV2ProjectionGuard | null = null;
  private readonly v2TransportDigests = new Map<number, string>();
  private controlReady: ControlReadySnapshot | null = null;
  private closeReason: RealtimeAdapterSnapshot["reason"] = "closed";
  private lastSnapshot: RealtimeAdapterSnapshot | null = null;
  private readonly pending = new Map<number, {
    resolve: (value: Record<string, unknown>) => void;
    reject: (reason: unknown) => void;
  }>();

  constructor(private readonly options: CloudRealtimeAdapterOptions) {}

  connect(): Promise<void> {
    if (this.socket !== null) return Promise.resolve();
    if (this.connectPromise !== null) return this.connectPromise;
    const generation = ++this.connectGeneration;
    const attempt = this.open(generation);
    const wrapped = attempt.finally(() => {
      if (this.connectPromise === wrapped) this.connectPromise = null;
    });
    this.connectPromise = wrapped;
    return wrapped;
  }

  private async open(generation: number): Promise<void> {
    this.publishSnapshot({ connection: "disconnected", reason: "connecting" });
    const mintAbort = new AbortController();
    this.mintAbort = mintAbort;
    let ticket: string;
    try {
      ticket = await this.options.ticketProvider.mint({
        connectionRole: this.options.role,
        agentId: this.options.agentId,
        sessionId: this.options.sessionId,
        ...(this.usesObserverV2() ? { observerContract: 2 as const } : {}),
      }, mintAbort.signal);
    } finally {
      if (this.mintAbort === mintAbort) this.mintAbort = null;
    }
    if (generation !== this.connectGeneration || this.socket !== null) return;
    if (ticket.length === 0 || this.usedTickets.has(ticket)) {
      this.publishSnapshot({ connection: "disconnected", reason: "ticket_reused" });
      throw new Error("single-use ticket was reused");
    }
    this.usedTickets.add(ticket);
    this.closeReason = "closed";
    this.subscription = null;
    this.controlReady = null;
    this.subscriptionRpcId = null;
    const url = new URL(this.options.websocketUrl);
    url.searchParams.set("ticket", ticket);
    const socketFactory = this.options.socketFactory
      ?? ((socketUrl: string, protocol: string) => new WebSocket(socketUrl, protocol) as unknown as WebSocketLike);
    const socket = socketFactory(url.toString(), this.usesObserverV2() ? CLOUD_OBSERVER_V2_SUBPROTOCOL : SUBPROTOCOL);
    this.socket = socket;
    this.phase = "awaiting_ready";
    socket.addEventListener("open", () => {
      if (socket !== this.socket) return;
      const expected = this.usesObserverV2() ? CLOUD_OBSERVER_V2_SUBPROTOCOL : SUBPROTOCOL;
      if (socket.protocol !== expected) this.protocolClose(socket, 1002, "subprotocol_mismatch");
    });
    socket.addEventListener("message", (rawEvent) => this.onMessage(socket, rawEvent));
    socket.addEventListener("close", () => this.onClose(socket));
  }

  call(method: string, params: Record<string, unknown>): Promise<Record<string, unknown>> {
    if (this.options.role !== "control" || this.socket === null || this.controlReady === null || this.phase !== "live") {
      return Promise.reject(new Error("control connection is not ready"));
    }
    if (!this.controlReady.availableMethods.includes(method)) {
      return Promise.reject(new Error(`control method ${method} was not advertised`));
    }
    const id = this.nextRpcId++;
    const promise = new Promise<Record<string, unknown>>((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
    });
    this.socket.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
    return promise;
  }

  disconnect(): void {
    this.connectGeneration += 1;
    this.mintAbort?.abort();
    this.mintAbort = null;
    this.connectPromise = null;
    const socket = this.socket;
    if (socket === null) return;
    this.closeReason = "closed";
    socket.close(1000, "client_close");
    if (this.socket === socket) this.onClose(socket);
  }

  private onMessage(socket: WebSocketLike, rawEvent: unknown): void {
    if (socket !== this.socket) return;
    const data = isRecord(rawEvent) ? rawEvent.data : undefined;
    if (typeof data !== "string") {
      this.protocolClose(socket, 1002, "invalid_frame");
      return;
    }
    if (new TextEncoder().encode(data).byteLength > MAX_FRAME_BYTES) {
      this.protocolClose(socket, 1009, "message_too_big", "invalid_frame");
      return;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(data) as unknown;
    } catch {
      this.protocolClose(socket, 1002, "invalid_frame");
      return;
    }
    if (this.usesObserverV2()) {
      this.onV2ParsedMessage(socket, parsed);
      return;
    }
    const decoded = decodeCloudRealtimeFrame(parsed);
    if (!decoded.ok) {
      this.protocolClose(socket, 1002, "invalid_frame");
      return;
    }
    const message = decoded.value;
    if ("method" in message) {
      if (message.params.type === "gateway.ready") {
        this.onReady(socket, message as CloudGatewayReady);
      } else {
        this.onObserverEvent(socket, message as CloudRealtimeEvent);
      }
      return;
    }
    if ("result" in message) this.onRpcResult(socket, message);
    else this.onRpcError(socket, message);
  }

  private onV2ParsedMessage(socket: WebSocketLike, parsed: unknown): void {
    const decoded = decodeCloudRealtimeV2Frame(parsed);
    if (!decoded.ok) {
      this.protocolClose(socket, 1002, "invalid_frame");
      return;
    }
    const message = decoded.value;
    if ("method" in message) {
      if (message.params.type === "gateway.ready") {
        this.onReadyV2(socket, message as ObserverGatewayReadyV2);
      } else {
        this.onObserverEvent(socket, message as CloudRealtimeV2Event);
      }
      return;
    }
    if ("result" in message) this.onRpcResultV2(socket, message);
    else this.onRpcError(socket, message);
  }

  private onReady(
    socket: WebSocketLike,
    ready: CloudGatewayReady,
  ): void {
    if (this.phase !== "awaiting_ready") {
      this.protocolClose(socket, 1002, "unexpected_ready");
      return;
    }
    if (ready.params.type !== "gateway.ready" || ready.params.payload.connection_role !== this.options.role) {
      this.protocolClose(socket, 1002, "invalid_role");
      return;
    }
    if (this.options.role === "observer") {
      const id = this.nextRpcId++;
      this.subscriptionRpcId = id;
      this.phase = "subscribing";
      socket.send(JSON.stringify({
        jsonrpc: "2.0",
        id,
        method: "session.observe.subscribe",
        params: {
          session_key: this.options.sessionKey,
          ...(this.options.profile === undefined ? {} : { profile: this.options.profile }),
        },
      }));
      this.publishSnapshot({ connection: "disconnected", reason: "subscribing" });
      return;
    }
    const controlPayload = (ready as ControlGatewayReady).params.payload;
    this.controlReady = {
      availableMethods: [...controlPayload.control_available_methods],
      errorCodes: { ...controlPayload.control_error_codes },
    };
    this.phase = "live";
    this.options.onControlReady?.(this.controlReady);
    this.publishSnapshot({ connection: "connected", reason: "live" });
  }

  private onReadyV2(socket: WebSocketLike, ready: ObserverGatewayReadyV2): void {
    if (this.phase !== "awaiting_ready" || ready.params.payload.observer_contract !== 2) {
      this.protocolClose(socket, 1002, "unexpected_ready");
      return;
    }
    const id = this.nextRpcId++;
    this.subscriptionRpcId = id;
    this.phase = "subscribing";
    socket.send(JSON.stringify({
      jsonrpc: "2.0",
      id,
      method: "session.observe.subscribe",
      params: {
        observer_contract: 2,
        session_id: this.options.sessionId,
        profile: this.options.profile ?? "default",
        agent_id: this.options.agentId,
      },
    }));
    this.publishSnapshot({ connection: "disconnected", reason: "subscribing" });
  }

  private onRpcResult(socket: WebSocketLike, message: CloudRpcResult): void {
    if (this.options.role === "observer" && this.phase === "subscribing" && message.id === this.subscriptionRpcId) {
      const subscription = decodeObserverSubscription(message, this.subscriptionRpcId, this.options.sessionKey);
      if (subscription === null) {
        this.protocolClose(socket, 1002, "invalid_subscription");
        return;
      }
      this.subscription = subscription;
      this.phase = "live";
      this.options.onSubscription?.(subscription);
      for (const event of subscription.replayEvents) this.options.onEvent(event);
      this.publishSnapshot({ connection: "connected", reason: "live" });
      return;
    }
    const pending = this.pending.get(message.id);
    if (pending === undefined) {
      this.protocolClose(socket, 1002, "unknown_rpc_result");
      return;
    }
    this.pending.delete(message.id);
    pending.resolve(message.result);
  }

  private onRpcResultV2(socket: WebSocketLike, message: CloudRpcResultV2): void {
    if (this.phase !== "subscribing" || message.id !== this.subscriptionRpcId) {
      this.protocolClose(socket, 1002, "unknown_rpc_result");
      return;
    }
    const subscription = decodeObserverSubscriptionV2(
      message,
      this.subscriptionRpcId,
      this.options.sessionId,
      this.options.profile ?? "default",
    );
    if (subscription === null) {
      this.protocolClose(socket, 1002, "invalid_subscription");
      return;
    }
    const projectionGuard = new ObserverV2ProjectionGuard();
    if (!projectionGuard.installSnapshot({
      snapshotEventSequence: subscription.snapshotEventSequence,
      todoSections: subscription.todoSections.map(todoToRaw),
      subagents: subscription.subagents.map(subagentToRaw),
      tools: subscription.tools.map(toolToRaw),
      terminals: subscription.terminals.map(terminalToRaw),
    })) {
      this.protocolClose(socket, 1002, "invalid_subscription");
      return;
    }
    this.v2TransportDigests.clear();
    for (const event of subscription.replayEvents) {
      if (!projectionGuard.apply({
        type: event.params.type,
        eventSequence: event.params.event_sequence,
        payload: event.params.payload,
      })) {
        this.protocolClose(socket, 1002, "invalid_subscription");
        return;
      }
      this.rememberV2TransportDigest(event);
    }
    this.subscription = subscription;
    this.projectionGuardV2 = projectionGuard;
    this.phase = "live";
    this.options.onSubscription?.(subscription);
    for (const event of subscription.replayEvents) this.options.onEvent(event);
    this.publishSnapshot({ connection: "connected", reason: "live" });
  }

  private onRpcError(socket: WebSocketLike, message: CloudRpcError | CloudRpcErrorV2): void {
    if (
      this.options.role === "control"
      && (this.controlReady === null || !Object.values(this.controlReady.errorCodes).includes(message.error.code))
    ) {
      this.protocolClose(socket, 1002, "unadvertised_error");
      return;
    }
    if (
      this.options.role === "observer"
      && message.id === this.subscriptionRpcId
      && message.error.code !== 4001
      && message.error.code !== 4091
    ) {
      this.protocolClose(socket, 1002, "unadvertised_error");
      return;
    }
    const pending = this.pending.get(message.id);
    if (pending !== undefined) {
      this.pending.delete(message.id);
      pending.reject(new CloudRpcFailure(message.error.code, message.error.message, message.error.data));
      return;
    }
    if (this.options.role === "observer" && message.id === this.subscriptionRpcId) {
      this.protocolClose(socket, 1002, "subscription_failed", "resync_required");
      return;
    }
    this.protocolClose(socket, 1002, "unknown_rpc_error");
  }

  private onObserverEvent(socket: WebSocketLike, event: CloudRealtimeEvent | CloudRealtimeV2Event): void {
    const subscription = this.subscription;
    const identityMatches = subscription?.observerContract === 2
      ? event.params.session_id === subscription.sessionId
      : subscription !== null
        && "session_key" in event.params
        && event.params.session_key === subscription.sessionKey
        && event.params.session_id === subscription.runtimeSessionId;
    if (
      this.options.role !== "observer"
      || this.phase !== "live"
      || subscription === null
      || !identityMatches
      || (subscription.observerContract === 2 && (
        !("observer_contract" in event.params)
        || event.params.observer_contract !== 2
        || event.params.profile !== subscription.profile
        || event.params.runtime_generation !== subscription.runtimeGeneration
      ))
    ) {
      this.protocolClose(socket, 1002, "resync_required", "resync_required");
      return;
    }
    const start = subscription.observerContract === 2
      ? eventStartV2(event as CloudRealtimeV2Event)
      : eventStart(event as CloudRealtimeEvent);
    if (event.params.event_sequence <= subscription.eventSequence) {
      if (subscription.observerContract === 1) return;
      const seenDigest = this.v2TransportDigests.get(event.params.event_sequence);
      const receivedDigest = canonicalObserverV2Digest(event.params);
      if (seenDigest === receivedDigest) return;
      this.protocolClose(socket, 1002, "resync_required", "resync_required");
      return;
    }
    if (start !== subscription.eventSequence + 1) {
      this.protocolClose(socket, 1002, "resync_required", "resync_required");
      return;
    }
    if (
      subscription.observerContract === 2
      && this.projectionGuardV2 !== null
      && !this.projectionGuardV2.apply({
        type: event.params.type,
        eventSequence: event.params.event_sequence,
        payload: event.params.payload,
      })
    ) {
      this.protocolClose(socket, 1002, "resync_required", "resync_required");
      return;
    }
    if (subscription.observerContract === 2) {
      this.rememberV2TransportDigest(event as CloudRealtimeV2Event);
    }
    this.subscription = { ...subscription, eventSequence: event.params.event_sequence };
    this.options.onEvent(event);
  }

  private rememberV2TransportDigest(event: CloudRealtimeV2Event): void {
    this.v2TransportDigests.set(event.params.event_sequence, canonicalObserverV2Digest(event.params));
    while (this.v2TransportDigests.size > MAX_V2_TRANSPORT_DIGESTS) {
      const oldestSequence = this.v2TransportDigests.keys().next().value as number | undefined;
      if (oldestSequence === undefined) break;
      this.v2TransportDigests.delete(oldestSequence);
    }
  }

  private protocolClose(
    socket: WebSocketLike,
    code: number,
    wireReason: string,
    reason: RealtimeAdapterSnapshot["reason"] = "invalid_frame",
  ): void {
    this.closeReason = reason;
    this.publishSnapshot({ connection: "disconnected", reason });
    socket.close(code, wireReason);
  }

  private onClose(socket: WebSocketLike): void {
    if (socket !== this.socket) return;
    this.socket = null;
    this.phase = "idle";
    this.subscription = null;
    this.projectionGuardV2 = null;
    this.controlReady = null;
    for (const pending of this.pending.values()) pending.reject(new Error("control connection closed"));
    this.pending.clear();
    this.publishSnapshot({ connection: "disconnected", reason: this.closeReason });
  }

  private publishSnapshot(snapshot: RealtimeAdapterSnapshot): void {
    if (
      this.lastSnapshot?.connection === snapshot.connection
      && this.lastSnapshot.reason === snapshot.reason
    ) return;
    this.lastSnapshot = snapshot;
    this.options.onSnapshot(snapshot);
  }

  private usesObserverV2(): boolean {
    return this.options.role === "observer" && this.options.observerContract === 2;
  }
}

function todoToRaw(value: ObserverSubscriptionV2["todoSections"][number]): Record<string, unknown> {
  return {
    turn_id: value.turnId,
    section_id: value.sectionId,
    revision: value.revision,
    first_event_sequence: value.firstEventSequence,
    status: value.status,
    items: value.items,
  };
}

function subagentToRaw(value: ObserverSubscriptionV2["subagents"][number]): Record<string, unknown> {
  return {
    turn_id: value.turnId,
    subagent_id: value.subagentId,
    revision: value.revision,
    first_event_sequence: value.firstEventSequence,
    parent_subagent_id: value.parentSubagentId,
    name: value.name,
    goal: value.goal,
    summary: value.summary,
    status: value.status,
  };
}

function toolToRaw(value: ObserverSubscriptionV2["tools"][number]): Record<string, unknown> {
  return {
    turn_id: value.turnId,
    tool_call_id: value.toolCallId,
    revision: value.revision,
    first_event_sequence: value.firstEventSequence,
    status: value.status,
    name: value.name,
    ...(value.callLabel === undefined ? {} : { call_label: value.callLabel }),
    ...(value.summary === undefined ? {} : { summary: value.summary }),
  };
}

function terminalToRaw(value: ObserverSubscriptionV2["terminals"][number]): Record<string, unknown> {
  return {
    turn_id: value.turnId,
    process_id: value.processId,
    revision: value.revision,
    first_event_sequence: value.firstEventSequence,
    status: value.status,
    ...(value.summary === undefined ? {} : { summary: value.summary }),
    ...(value.exitCode === undefined ? {} : { exit_code: value.exitCode }),
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
