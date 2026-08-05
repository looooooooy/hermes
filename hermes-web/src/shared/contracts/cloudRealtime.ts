import mobileControl from "./generated/mobile-control-v1.json";

export const CLOUD_OBSERVER_EVENT_TYPES = [
  "message.start",
  "message.delta",
  "message.complete",
  "agent.terminal.output",
  "reasoning.delta",
  "status.update",
  "thinking.delta",
  "tool.output.delta",
] as const;

const EVENT_TYPES = new Set<EventType>(CLOUD_OBSERVER_EVENT_TYPES);

const MERGEABLE_EVENT_TYPES = new Set<EventType>([
  "message.delta",
  "agent.terminal.output",
  "reasoning.delta",
  "status.update",
  "thinking.delta",
  "tool.output.delta",
]);

export const CONTROL_METHODS: readonly string[] = mobileControl.available_methods;

const CONTROL_METHOD_SET = new Set<string>(CONTROL_METHODS);

export const CONTROL_ERROR_CODES: Readonly<Record<string, number>> = Object.freeze({
  ...mobileControl.error_codes,
});

export type EventType =
  | "message.start"
  | "message.delta"
  | "message.complete"
  | "agent.terminal.output"
  | "reasoning.delta"
  | "status.update"
  | "thinking.delta"
  | "tool.output.delta";

export interface CloudRealtimeEvent {
  jsonrpc: "2.0";
  method: "event";
  params: {
    type: EventType;
    session_id: string;
    session_key: string;
    event_sequence_start?: number;
    event_sequence: number;
    payload: Record<string, unknown>;
  };
}

export interface ObserverGatewayReady {
  jsonrpc: "2.0";
  method: "event";
  params: {
    type: "gateway.ready";
    payload: { observer_contract: 1; connection_role: "observer" };
  };
}

export interface ControlGatewayReady {
  jsonrpc: "2.0";
  method: "event";
  params: {
    type: "gateway.ready";
    payload: {
      observer_contract: 1;
      control_contract: 1;
      connection_role: "control";
      control_available_methods: string[];
      control_error_codes: Record<string, number>;
    };
  };
}

export type CloudGatewayReady = ObserverGatewayReady | ControlGatewayReady;

export interface CloudRpcResult {
  jsonrpc: "2.0";
  id: number;
  result: Record<string, unknown>;
}

export interface CloudRpcError {
  jsonrpc: "2.0";
  id: number;
  error: { code: number; message: string; data?: Record<string, unknown> | null };
}

export interface ObserverSubscription {
  observerContract: 1;
  subscriptionId: string;
  sessionKey: string;
  runtimeSessionId: string;
  running: boolean;
  status: string;
  eventSequence: number;
  snapshotEventSequence: number;
  messages: readonly { role: string; content?: string | null }[];
  inflight: { user: string | null; assistant: string | null; streaming: boolean; error: string | null };
  replayEvents: readonly CloudRealtimeEvent[];
}

export type CloudRealtimeMessage = CloudRealtimeEvent | CloudGatewayReady | CloudRpcResult | CloudRpcError;

export type CloudRealtimeDecodeResult =
  | { ok: true; value: CloudRealtimeMessage }
  | { ok: false; reason: "invalid_frame" };

export function decodeCloudRealtimeFrame(value: unknown): CloudRealtimeDecodeResult {
  if (!withinJsonLimits(value)) return invalid();
  if (!isRecord(value) || value.jsonrpc !== "2.0") return invalid();
  if (isExactObject(value, ["jsonrpc", "method", "params"]) && value.method === "event") {
    if (isGatewayReadyParams(value.params)) {
      return { ok: true, value: value as unknown as CloudGatewayReady };
    }
    if (isObserverEventParams(value.params)) {
      return { ok: true, value: value as unknown as CloudRealtimeEvent };
    }
    return invalid();
  }
  if (
    isExactObject(value, ["jsonrpc", "id", "result"])
    && isPositiveInteger(value.id)
    && isRecord(value.result)
  ) return { ok: true, value: value as unknown as CloudRpcResult };
  if (
    isExactObject(value, ["jsonrpc", "id", "error"])
    && isPositiveInteger(value.id)
    && isRpcError(value.error)
  ) return { ok: true, value: value as unknown as CloudRpcError };
  return invalid();
}

export function decodeObserverSubscription(
  message: CloudRpcResult,
  expectedId: number,
  expectedSessionKey: string,
): ObserverSubscription | null {
  if (message.id !== expectedId) return null;
  const result = message.result;
  if (!isExactObject(result, [
    "subscription_id",
    "session_key",
    "runtime_session_id",
    "running",
    "status",
    "event_sequence",
    "snapshot_event_sequence",
    "messages",
    "inflight",
    "replay_events",
  ])) return null;
  if (
    !isBoundedString(result.subscription_id, 256)
    || result.session_key !== expectedSessionKey
    || !isBoundedString(result.runtime_session_id, 256)
    || typeof result.running !== "boolean"
    || !isBoundedString(result.status, 64)
    || result.running !== isRunningStatus(result.status)
    || !isNonNegativeInteger(result.event_sequence)
    || !isNonNegativeInteger(result.snapshot_event_sequence)
    || result.snapshot_event_sequence > result.event_sequence
    || !isMessages(result.messages)
    || !isInflight(result.inflight)
    || !Array.isArray(result.replay_events)
    || result.replay_events.length > 1024
  ) return null;

  const replayEvents: CloudRealtimeEvent[] = [];
  let last = result.snapshot_event_sequence;
  for (const rawEvent of result.replay_events) {
    const decoded = decodeReplayEvent(rawEvent);
    if (
      decoded === null
      || decoded.params.session_key !== expectedSessionKey
      || decoded.params.session_id !== result.runtime_session_id
      || eventStart(decoded) !== last + 1
      || decoded.params.event_sequence < eventStart(decoded)
    ) return null;
    last = decoded.params.event_sequence;
    replayEvents.push(decoded);
  }
  if (last !== result.event_sequence) return null;
  if (replayEvents.length === 0 && result.snapshot_event_sequence !== result.event_sequence) return null;

  return {
    observerContract: 1,
    subscriptionId: result.subscription_id,
    sessionKey: result.session_key,
    runtimeSessionId: result.runtime_session_id,
    running: result.running,
    status: result.status,
    eventSequence: result.event_sequence,
    snapshotEventSequence: result.snapshot_event_sequence,
    messages: result.messages as ObserverSubscription["messages"],
    inflight: result.inflight as ObserverSubscription["inflight"],
    replayEvents,
  };
}

export function eventStart(event: CloudRealtimeEvent): number {
  return event.params.event_sequence_start ?? event.params.event_sequence;
}

function decodeReplayEvent(value: unknown): CloudRealtimeEvent | null {
  if (!isRecord(value)) return null;
  const frame = { jsonrpc: "2.0", method: "event", params: value };
  const decoded = decodeCloudRealtimeFrame(frame);
  return decoded.ok && "method" in decoded.value && decoded.value.params.type !== "gateway.ready"
    ? decoded.value as CloudRealtimeEvent
    : null;
}

function isObserverEventParams(value: unknown): boolean {
  if (!isRecord(value)) return false;
  const keys = ["type", "session_id", "session_key", "event_sequence", "payload"];
  if ("event_sequence_start" in value) keys.push("event_sequence_start");
  if (
    !isExactObject(value, keys)
    || typeof value.type !== "string"
    || !EVENT_TYPES.has(value.type as EventType)
    || !isBoundedString(value.session_id, 256)
    || !isBoundedString(value.session_key, 256)
    || !isPositiveInteger(value.event_sequence)
    || !isRecord(value.payload)
    || !payloadIsValid(value.type as EventType, value.payload)
  ) return false;
  if (!("event_sequence_start" in value)) return true;
  return MERGEABLE_EVENT_TYPES.has(value.type as EventType)
    && isPositiveInteger(value.event_sequence_start)
    && value.event_sequence_start <= value.event_sequence;
}

function isGatewayReadyParams(value: unknown): boolean {
  if (!isExactObject(value, ["type", "payload"]) || value.type !== "gateway.ready") return false;
  const payload = value.payload;
  if (
    isExactObject(payload, ["observer_contract", "connection_role"])
    && payload.observer_contract === 1
    && payload.connection_role === "observer"
  ) return true;
  if (
    !isExactObject(payload, [
      "observer_contract",
      "control_contract",
      "connection_role",
      "control_available_methods",
      "control_error_codes",
    ])
    || payload.observer_contract !== 1
    || payload.control_contract !== 1
    || payload.connection_role !== "control"
    || !isControlMethods(payload.control_available_methods)
    || !isControlErrorCodes(payload.control_error_codes)
  ) return false;
  return true;
}

function isControlMethods(value: unknown): value is string[] {
  if (!Array.isArray(value) || value.length > CONTROL_METHODS.length) return false;
  if (!value.every((method) => typeof method === "string" && CONTROL_METHOD_SET.has(method))) return false;
  return new Set(value).size === value.length;
}

function isControlErrorCodes(value: unknown): value is Record<string, number> {
  if (!isRecord(value)) return false;
  const entries = Object.entries(value);
  return entries.length === Object.keys(CONTROL_ERROR_CODES).length
    && entries.every(([name, code]) => CONTROL_ERROR_CODES[name] === code);
}

function isRpcError(value: unknown): boolean {
  if (!isRecord(value)) return false;
  const keys = ["code", "message"];
  if ("data" in value) keys.push("data");
  return isExactObject(value, keys)
    && Number.isSafeInteger(value.code)
    && isBoundedString(value.message, 4096)
    && (!("data" in value) || value.data === null || (isRecord(value.data) && Object.keys(value.data).length <= 16));
}

function isMessages(value: unknown): boolean {
  return Array.isArray(value) && value.length <= 500 && value.every((message) => {
    if (!isRecord(message)) return false;
    const keys = ["role"];
    if ("content" in message) keys.push("content");
    return isExactObject(message, keys)
      && isBoundedString(message.role, 64)
      && (!("content" in message) || message.content === null || isString(message.content));
  });
}

function isInflight(value: unknown): boolean {
  return isExactObject(value, ["user", "assistant", "streaming", "error"])
    && nullableString(value.user)
    && nullableString(value.assistant)
    && typeof value.streaming === "boolean"
    && (value.error === null || isBoundedString(value.error, 4096));
}

function payloadIsValid(type: EventType, payload: Record<string, unknown>): boolean {
  switch (type) {
    case "message.start":
      return hasOnly(payload, ["message_id", "role"])
        && optionalBoundedString(payload.message_id, 256)
        && (payload.role === undefined || payload.role === "assistant");
    case "message.delta":
    case "reasoning.delta":
    case "thinking.delta":
      return hasOnly(payload, ["text"]) && isString(payload.text);
    case "message.complete":
      return hasOnly(payload, ["text", "status", "error"])
        && (payload.status === "complete" || payload.status === "error")
        && optionalString(payload.text)
        && (payload.error === undefined || payload.error === null || isBoundedString(payload.error, 4096));
    case "agent.terminal.output":
      return hasOnly(payload, ["process_id", "stream", "text", "sequence"])
        && isString(payload.text)
        && optionalBoundedString(payload.process_id, 256)
        && (payload.stream === undefined || payload.stream === "stdout" || payload.stream === "stderr")
        && optionalSequence(payload.sequence);
    case "status.update": {
      if (!hasOnly(payload, ["status", "running", "text"])) return false;
      if (!isBoundedString(payload.status, 64) || typeof payload.running !== "boolean") return false;
      return payload.running === isRunningStatus(payload.status) && optionalString(payload.text);
    }
    case "tool.output.delta":
      return hasOnly(payload, ["tool_call_id", "tool_name", "text", "sequence"])
        && isString(payload.text)
        && optionalBoundedString(payload.tool_call_id, 256)
        && optionalBoundedString(payload.tool_name, 256)
        && optionalSequence(payload.sequence);
  }
}

function isRunningStatus(status: string): boolean {
  return status === "running" || status === "working" || status === "streaming";
}

function withinJsonLimits(value: unknown, depth = 0): boolean {
  if (depth > 32) return false;
  if (typeof value === "string") return new TextEncoder().encode(value).byteLength <= 131_072;
  if (value === null || typeof value === "boolean" || typeof value === "number") return true;
  if (Array.isArray(value)) return value.length <= 1024 && value.every((item) => withinJsonLimits(item, depth + 1));
  if (isRecord(value)) {
    const entries = Object.entries(value);
    return entries.length <= 1024 && entries.every(([key, item]) => isString(key) && withinJsonLimits(item, depth + 1));
  }
  return false;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isExactObject<K extends string>(value: unknown, keys: readonly K[]): value is Record<K, unknown> {
  if (!isRecord(value)) return false;
  const actual = Object.keys(value);
  return actual.length === keys.length && actual.every((key) => keys.includes(key as K));
}

function hasOnly(value: Record<string, unknown>, keys: readonly string[]): boolean {
  return Object.keys(value).every((key) => keys.includes(key));
}

function isString(value: unknown): value is string {
  return typeof value === "string" && new TextEncoder().encode(value).byteLength <= 131_072;
}

function nullableString(value: unknown): boolean {
  return value === null || isString(value);
}

function isBoundedString(value: unknown, maxLength: number): value is string {
  return typeof value === "string" && value.length > 0 && new TextEncoder().encode(value).byteLength <= maxLength;
}

function optionalString(value: unknown): boolean {
  return value === undefined || isString(value);
}

function optionalBoundedString(value: unknown, maxLength: number): boolean {
  return value === undefined || isBoundedString(value, maxLength);
}

function isPositiveInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) >= 1;
}

function isNonNegativeInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) >= 0;
}

function optionalSequence(value: unknown): boolean {
  return value === undefined || isNonNegativeInteger(value);
}

function invalid(): CloudRealtimeDecodeResult {
  return { ok: false, reason: "invalid_frame" };
}
