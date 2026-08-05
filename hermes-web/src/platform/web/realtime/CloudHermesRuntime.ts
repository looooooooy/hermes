import type {
  ApprovalCommand,
  ApprovalCommandResult,
  ClarificationCommand,
  ClarificationCommandResult,
  HermesRuntimePort,
  InterruptCommand,
  MutationCommandResult,
  PromptCommandResult,
  RuntimeCallbacks,
  SteerCommand,
  SubmitPromptCommand,
} from "../../../app/runtimePort";
import { CloudCommandPort } from "./CloudCommandPort";
import {
  CloudRealtimeAdapter,
  type CloudRealtimeAdapterOptions,
  type TicketProvider,
} from "./CloudRealtimeAdapter";
import { ControllerLeaseLifecycle } from "./ControllerLeaseLifecycle";
import { CookieTicketAuthenticationUnavailable } from "./HttpTicketProvider";

const STOP_RELEASE_DEADLINE_MS = 250;

export interface CloudHermesRuntimeOptions {
  websocketUrl: string;
  agentId: string;
  sessionId: string;
  sessionKey: string;
  profile: string;
  clientInstanceId: string;
  ticketProvider: TicketProvider;
  socketFactory?: CloudRealtimeAdapterOptions["socketFactory"];
  now?: () => number;
  scheduleLease?: (callback: () => void, delayMs: number) => unknown;
  cancelLease?: (handle: unknown) => void;
  scheduleReconnect?: (callback: () => void, delayMs: number) => unknown;
  cancelReconnect?: (handle: unknown) => void;
}

export class CloudHermesRuntime implements HermesRuntimePort {
  private callbacks: RuntimeCallbacks | null = null;
  private stopped = true;
  private readonly observer: CloudRealtimeAdapter;
  private readonly control: CloudRealtimeAdapter;
  private readonly commands: CloudCommandPort;
  private readonly leaseLifecycle: ControllerLeaseLifecycle;
  private readonly reconnectTimers = new Map<CloudRealtimeAdapter, unknown>();
  private readonly reconnectAttempts = new Map<CloudRealtimeAdapter, number>();
  private readonly scheduleReconnectCallback: (callback: () => void, delayMs: number) => unknown;
  private readonly cancelReconnectCallback: (handle: unknown) => void;
  private visibilityHandler: (() => void) | null = null;
  private lifecycleEpoch = 0;
  private stopPromise: Promise<void> | null = null;

  constructor(options: CloudHermesRuntimeOptions) {
    this.observer = new CloudRealtimeAdapter({
      role: "observer",
      observerContract: 2,
      websocketUrl: options.websocketUrl,
      agentId: options.agentId,
      sessionId: options.sessionId,
      sessionKey: options.sessionKey,
      profile: options.profile,
      ticketProvider: options.ticketProvider,
      socketFactory: options.socketFactory,
      onSnapshot: (snapshot) => {
        this.callbacks?.onConnectionChanged(snapshot.connection);
        if (snapshot.connection === "connected") this.reconnectAttempts.set(this.observer, 0);
        if (!this.stopped && (snapshot.reason === "closed" || snapshot.reason === "resync_required")) {
          this.scheduleReconnect(this.observer);
        }
      },
      onSubscription: (snapshot) => {
        this.callbacks?.onSubscription({
          observerContract: snapshot.observerContract,
          runtimeGeneration: snapshot.observerContract === 2 ? snapshot.runtimeGeneration : null,
          runtimeSessionId: snapshot.runtimeSessionId,
          running: snapshot.running,
          status: snapshot.status,
          messages: snapshot.messages,
          inflight: snapshot.inflight,
          todoSections: snapshot.observerContract === 2 ? snapshot.todoSections : [],
          subagents: snapshot.observerContract === 2 ? snapshot.subagents : [],
          tools: snapshot.observerContract === 2 ? snapshot.tools : [],
          terminals: snapshot.observerContract === 2 ? snapshot.terminals : [],
        });
        this.leaseLifecycle.updateRuntimeSession(snapshot.runtimeSessionId);
      },
      onEvent: (event) => {
        this.callbacks?.onEvent({
          type: event.params.type as Parameters<RuntimeCallbacks["onEvent"]>[0]["type"],
          eventSequence: event.params.event_sequence,
          payload: event.params.payload,
        });
        if (event.params.type === "status.update" && typeof event.params.payload.running === "boolean") {
          this.callbacks?.onRunningChanged(event.params.payload.running);
        }
      },
    });
    this.control = new CloudRealtimeAdapter({
      role: "control",
      websocketUrl: options.websocketUrl,
      agentId: options.agentId,
      sessionId: options.sessionId,
      sessionKey: options.sessionKey,
      profile: options.profile,
      ticketProvider: options.ticketProvider,
      socketFactory: options.socketFactory,
      onSnapshot: (snapshot) => {
        if (!this.stopped && (snapshot.reason === "closed" || snapshot.reason === "resync_required")) {
          this.callbacks?.onControlReady([]);
          this.leaseLifecycle.disconnect();
          this.scheduleReconnect(this.control);
        }
      },
      onEvent: () => undefined,
      onControlReady: (ready) => {
        this.reconnectAttempts.set(this.control, 0);
        this.callbacks?.onControlReady(ready.availableMethods);
        this.leaseLifecycle.updateCapabilities(ready.availableMethods);
        this.leaseLifecycle.resume();
      },
    });
    this.commands = new CloudCommandPort(this.control);
    this.leaseLifecycle = new ControllerLeaseLifecycle({
      sessionId: options.sessionId,
      call: (method, params) => this.control.call(method, params),
      onStateChanged: (state) => this.callbacks?.onControlStateChanged(state),
      now: options.now,
      schedule: options.scheduleLease,
      cancel: options.cancelLease,
    });
    this.scheduleReconnectCallback = options.scheduleReconnect
      ?? ((callback, delayMs) => globalThis.setTimeout(callback, delayMs));
    this.cancelReconnectCallback = options.cancelReconnect
      ?? ((handle) => globalThis.clearTimeout(handle as ReturnType<typeof globalThis.setTimeout>));
  }

  start(callbacks: RuntimeCallbacks): () => void {
    const lifecycleEpoch = ++this.lifecycleEpoch;
    this.callbacks = callbacks;
    this.stopped = false;
    this.stopPromise = null;
    this.leaseLifecycle.start();
    this.installVisibilityLifecycle();
    this.connectAdapter(this.observer);
    this.connectAdapter(this.control);
    return () => {
      void this.stopEpoch(lifecycleEpoch);
    };
  }

  stop(): Promise<void> {
    return this.stopEpoch(this.lifecycleEpoch);
  }

  submitPrompt(command: SubmitPromptCommand): Promise<PromptCommandResult> {
    return this.commands.submitPrompt(command);
  }

  steer(command: SteerCommand): Promise<MutationCommandResult> {
    return this.commands.steer(command);
  }

  interrupt(command: InterruptCommand): Promise<MutationCommandResult> {
    return this.commands.interrupt(command);
  }

  respondApproval(command: ApprovalCommand): Promise<ApprovalCommandResult> {
    return this.commands.respondApproval(command);
  }

  respondClarification(command: ClarificationCommand): Promise<ClarificationCommandResult> {
    return this.commands.respondClarification(command);
  }

  retryConnection(): void {
    if (this.stopped) return;
    for (const timer of this.reconnectTimers.values()) this.cancelReconnectCallback(timer);
    this.reconnectTimers.clear();
    this.reconnectAttempts.clear();
    this.connectAdapter(this.observer);
    this.connectAdapter(this.control);
  }

  private connectAdapter(adapter: CloudRealtimeAdapter): void {
    const lifecycleEpoch = this.lifecycleEpoch;
    void adapter.connect().catch((error) => {
      if (this.stopped || lifecycleEpoch !== this.lifecycleEpoch) return;
      const authenticationFailure = error instanceof CookieTicketAuthenticationUnavailable;
      if (adapter === this.observer) {
        this.callbacks?.onConnectionChanged("disconnected");
        this.callbacks?.onControlStateChanged(emptyControlState(ticketFailureReason(error)));
      } else {
        this.callbacks?.onControlReady([]);
        this.leaseLifecycle.disconnect(ticketFailureReason(error));
      }
      if (!authenticationFailure) this.scheduleReconnect(adapter);
    });
  }

  private stopEpoch(lifecycleEpoch: number): Promise<void> {
    if (lifecycleEpoch !== this.lifecycleEpoch) return Promise.resolve();
    if (this.stopPromise !== null) return this.stopPromise;
    this.stopped = true;
    this.removeVisibilityLifecycle();
    for (const timer of this.reconnectTimers.values()) this.cancelReconnectCallback(timer);
    this.reconnectTimers.clear();
    this.reconnectAttempts.clear();
    const releaseAbort = new AbortController();
    const deadline = globalThis.setTimeout(() => releaseAbort.abort(), STOP_RELEASE_DEADLINE_MS);
    const release = this.leaseLifecycle.suspend(releaseAbort.signal);
    this.callbacks = null;
    this.observer.disconnect();
    this.control.disconnect();
    const stopping = release.finally(() => globalThis.clearTimeout(deadline));
    this.stopPromise = stopping;
    return stopping;
  }

  private scheduleReconnect(adapter: CloudRealtimeAdapter): void {
    if (this.reconnectTimers.has(adapter)) return;
    const attempt = this.reconnectAttempts.get(adapter) ?? 0;
    if (attempt >= 3) return;
    this.reconnectAttempts.set(adapter, attempt + 1);
    const timer = this.scheduleReconnectCallback(() => {
      this.reconnectTimers.delete(adapter);
      if (!this.stopped) this.connectAdapter(adapter);
    }, 500 * (2 ** attempt));
    this.reconnectTimers.set(adapter, timer);
  }

  private installVisibilityLifecycle(): void {
    if (typeof document === "undefined" || this.visibilityHandler !== null) return;
    this.visibilityHandler = () => {
      if (document.visibilityState === "hidden") void this.leaseLifecycle.suspend();
      else this.leaseLifecycle.resume();
    };
    document.addEventListener("visibilitychange", this.visibilityHandler);
    if (document.visibilityState === "hidden") void this.leaseLifecycle.suspend();
  }

  private removeVisibilityLifecycle(): void {
    if (typeof document === "undefined" || this.visibilityHandler === null) return;
    document.removeEventListener("visibilitychange", this.visibilityHandler);
    this.visibilityHandler = null;
  }
}

function emptyControlState(unavailableReason: string) {
  return {
    leaseId: null,
    runtimeSessionId: null,
    leaseExpiresAtEpochMs: 0,
    controlRevision: 0,
    controllerKind: null,
    controllerLabel: null,
    pendingInput: null,
    unavailableReason,
  } as const;
}

function ticketFailureReason(error: unknown): string {
  return error instanceof CookieTicketAuthenticationUnavailable
    ? "Cloud login succeeded, but browser WebSocket ticket authentication is unavailable."
    : "Cloud realtime connection is unavailable. Reconnect to try again.";
}
