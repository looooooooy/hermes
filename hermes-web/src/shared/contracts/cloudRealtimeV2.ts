import realtimeV2 from "./generated/cloud-realtime-v2.json";
import parityPolicy from "./generated/observer-output-parity-v2.json";
import cloudSessionEventPayloadV2 from "./generated/schemas/cloud/payloads/session-event-v2.schema.json";
import publicSessionEventV2 from "./generated/schemas/public/session-event-v2.schema.json";
import { validatesGeneratedSchema } from "./generatedJsonSchema";
import { ObserverV2ProjectionGuard } from "./observerV2ProjectionGuard";

const EXTERNAL_SCHEMAS = {
  [cloudSessionEventPayloadV2.$id]: cloudSessionEventPayloadV2,
  [publicSessionEventV2.$id]: publicSessionEventV2,
} as const;
const EXPECTED_DEPENDENCIES = [
  "schemas/cloud/payloads/session-event-v2.schema.json",
  "schemas/public/session-event-v2.schema.json",
] as const;

export const CLOUD_OBSERVER_V2_EVENT_TYPES = Object.freeze(
  [...publicSessionEventV2.properties.type.enum],
);
export const HERMES_DISPLAY_NAME_MAX_CODE_POINTS = parityPolicy.limits.max_name_code_points;

const EVENT_TYPE_SET = new Set<string>(CLOUD_OBSERVER_V2_EVENT_TYPES);
const MERGEABLE_EVENT_TYPES = new Set<string>(realtimeV2.sequence.mergeable_event_types);
const AUTHORITY_IS_BOUND = realtimeV2.observer_contract === 2
  && realtimeV2.websocket_subprotocol === "hermes.tui.v2"
  && realtimeV2.schema_dependencies.length === EXPECTED_DEPENDENCIES.length
  && EXPECTED_DEPENDENCIES.every((dependency, index) => (
    realtimeV2.schema_dependencies[index] === dependency
  ));
const SENSITIVE_EXTENSION_TOKENS = new Set([
  "approval", "arg", "args", "argument", "arguments", "argv", "auth", "authorization",
  "bearer", "cookie", "credential", "credentials", "env", "environment", "output",
  "passphrase", "password", "path", "private", "raw", "reasoning", "secret", "secrets",
  "stderr", "stdin", "stdout", "token", "tokens",
]);
const CREDENTIAL_KEY_QUALIFIERS = new Set(["access", "api", "client", "private", "secret", "signing"]);
const TOKEN_COUNT_FIELDS = new Set(["input", "output", "reasoning"]);
const CREDENTIAL_VALUE = /(?:\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bAIza[A-Za-z0-9_-]{20,}\b|\b(?:sk|ghp|xox[baprs]|hf|npm)[-_][A-Za-z0-9_-]{8,}\b|\bglpat-[A-Za-z0-9_-]{8,}\b)/u;
const CREDENTIAL_VALUE_CASE_INSENSITIVE = /(?:\bBearer[\t ]+\S+|-----BEGIN[\t ]+[A-Z ]*PRIVATE[\t ]+KEY-----|\b(?:password|passwd|secret|token|api[\s_-]?key|client[\s_-]?secret)\b[\t ]*(?:=|:)[\t ]*["']?[^\s"',;]+)/iu;
const JWT_SHAPE = /(?<![A-Za-z0-9_=-])(?<![A-Za-z0-9_-]\.)([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]+)(?![A-Za-z0-9_=-])(?!\.[A-Za-z0-9_-])/gu;

export type CloudRealtimeV2EventType = typeof CLOUD_OBSERVER_V2_EVENT_TYPES[number];

export interface CloudRealtimeV2Event {
  jsonrpc: "2.0";
  method: "event";
  params: {
    observer_contract: 2;
    profile: string;
    runtime_generation: string;
    type: CloudRealtimeV2EventType;
    session_id: string;
    event_sequence_start?: number;
    event_sequence: number;
    payload: Record<string, unknown>;
    extensions?: Readonly<Record<string, Record<string, unknown>>>;
  };
}

export interface ObserverGatewayReadyV2 {
  jsonrpc: "2.0";
  method: "event";
  params: {
    type: "gateway.ready";
    payload: { observer_contract: 2; connection_role: "observer" };
  };
}

export interface CloudRpcResultV2 {
  jsonrpc: "2.0";
  id: number;
  result: Record<string, unknown>;
}

export interface CloudRpcErrorV2 {
  jsonrpc: "2.0";
  id: number;
  error: { code: number; message: string; data?: Record<string, unknown> | null };
}

export type CloudRealtimeV2Message =
  | CloudRealtimeV2Event
  | ObserverGatewayReadyV2
  | CloudRpcResultV2
  | CloudRpcErrorV2;

export type CloudRealtimeV2DecodeResult =
  | { ok: true; value: CloudRealtimeV2Message }
  | { ok: false; reason: "invalid_frame" };

export interface TodoSectionV2 {
  turnId: string;
  sectionId: string;
  revision: number;
  firstEventSequence: number;
  status: "pending" | "in_progress" | "completed" | "cancelled";
  items: readonly { id: string; label: string; status: "pending" | "in_progress" | "completed" | "cancelled" }[];
}

export interface SubagentProjectionV2 {
  turnId: string;
  subagentId: string;
  revision: number;
  firstEventSequence: number;
  parentSubagentId: string | null;
  name: string;
  goal: string;
  summary: string | null;
  status: "queued" | "waiting" | "running" | "completed" | "failed" | "interrupted";
}

export interface ToolProjectionV2 {
  turnId: string;
  toolCallId: string;
  revision: number;
  firstEventSequence: number;
  status: "running" | "completed" | "failed" | "interrupted" | "unknown";
  name: string;
  callLabel?: string;
  summary?: string;
}

export interface TerminalProjectionV2 {
  turnId: string;
  processId: string;
  revision: number;
  firstEventSequence: number;
  status: "running" | "completed" | "failed" | "interrupted" | "unknown";
  summary?: string;
  exitCode?: number;
}

export interface ObserverSubscriptionV2 {
  observerContract: 2;
  subscriptionId: string;
  profile: string;
  runtimeGeneration: string;
  sessionId: string;
  runtimeSessionId: string;
  running: boolean;
  status: string;
  eventSequence: number;
  snapshotEventSequence: number;
  messages: readonly { role: string; content?: string | null }[];
  inflight: { user: string | null; assistant: string | null; streaming: boolean; error: string | null };
  todoSections: readonly TodoSectionV2[];
  subagents: readonly SubagentProjectionV2[];
  tools: readonly ToolProjectionV2[];
  terminals: readonly TerminalProjectionV2[];
  replayEvents: readonly CloudRealtimeV2Event[];
}

export function decodeCloudRealtimeV2Frame(value: unknown): CloudRealtimeV2DecodeResult {
  if (!AUTHORITY_IS_BOUND || !withinJsonLimits(value)) return invalid();
  if (!isRecord(value) || value.jsonrpc !== "2.0") return invalid();
  if (isExactObject(value, ["jsonrpc", "method", "params"]) && value.method === "event") {
    if (validatesGeneratedSchema(realtimeV2.schemas.gateway_ready, value, EXTERNAL_SCHEMAS)) {
      return { ok: true, value: value as unknown as ObserverGatewayReadyV2 };
    }
    if (validatesGeneratedSchema(realtimeV2.schemas.session_event, value, EXTERNAL_SCHEMAS)) {
      if (!hasDisplaySafeEvent(value)) return invalid();
      return { ok: true, value: value as unknown as CloudRealtimeV2Event };
    }
    return invalid();
  }
  if (
    isExactObject(value, ["jsonrpc", "id", "result"])
    && positiveInteger(value.id)
    && isRecord(value.result)
  ) return { ok: true, value: value as unknown as CloudRpcResultV2 };
  if (
    isExactObject(value, ["jsonrpc", "id", "error"])
    && positiveInteger(value.id)
    && validRpcError(value.error)
  ) return { ok: true, value: value as unknown as CloudRpcErrorV2 };
  return invalid();
}

export function decodeObserverSubscriptionV2(
  message: CloudRpcResultV2,
  expectedId: number,
  expectedSessionId: string,
  expectedProfile: string,
): ObserverSubscriptionV2 | null {
  if (
    message.id !== expectedId
    || !validatesGeneratedSchema(realtimeV2.schemas.observe_subscribe_result, message, EXTERNAL_SCHEMAS)
  ) return null;

  const result = message.result;
  if (
    result.observer_contract !== 2
    || result.session_id !== expectedSessionId
    || result.profile !== expectedProfile
    || typeof result.running !== "boolean"
    || typeof result.status !== "string"
    || result.running !== isRunningStatus(result.status)
    || !nonNegativeInteger(result.event_sequence)
    || !nonNegativeInteger(result.snapshot_event_sequence)
    || result.snapshot_event_sequence > result.event_sequence
    || !Array.isArray(result.messages)
    || !isRecord(result.inflight)
    || !Array.isArray(result.todo_sections)
    || !Array.isArray(result.subagents)
    || !Array.isArray(result.tools)
    || !Array.isArray(result.terminals)
    || !Array.isArray(result.replay_events)
    || typeof result.runtime_generation !== "string"
    || typeof result.session_id !== "string"
    || typeof result.subscription_id !== "string"
  ) return null;
  if (!displaySafeContentValue({
    messages: result.messages,
    inflight: result.inflight,
    todo_sections: result.todo_sections,
    subagents: result.subagents,
    tools: result.tools,
    terminals: result.terminals,
  }, 0)) return null;

  const rawCollections = {
    snapshotEventSequence: result.snapshot_event_sequence,
    todoSections: result.todo_sections as Record<string, unknown>[],
    subagents: result.subagents as Record<string, unknown>[],
    tools: result.tools as Record<string, unknown>[],
    terminals: result.terminals as Record<string, unknown>[],
  };
  const projectionGuard = new ObserverV2ProjectionGuard();
  if (!projectionGuard.installSnapshot(rawCollections)) return null;

  const replayEvents: CloudRealtimeV2Event[] = [];
  let lastSequence = result.snapshot_event_sequence;
  for (const rawParams of result.replay_events) {
    const decoded = decodeCloudRealtimeV2Frame({ jsonrpc: "2.0", method: "event", params: rawParams });
    if (!decoded.ok || !("method" in decoded.value) || decoded.value.params.type === "gateway.ready") return null;
    const event = decoded.value as CloudRealtimeV2Event;
    if (
      event.params.session_id !== expectedSessionId
      || event.params.profile !== expectedProfile
      || event.params.runtime_generation !== result.runtime_generation
      || eventStartV2(event) !== lastSequence + 1
      || event.params.event_sequence < eventStartV2(event)
      || !projectionGuard.apply({
        type: event.params.type,
        eventSequence: event.params.event_sequence,
        payload: event.params.payload,
      })
    ) return null;
    lastSequence = event.params.event_sequence;
    replayEvents.push(event);
  }
  if (lastSequence !== result.event_sequence) return null;

  return {
    observerContract: 2,
    subscriptionId: result.subscription_id,
    profile: result.profile,
    runtimeGeneration: result.runtime_generation,
    sessionId: result.session_id as string,
    runtimeSessionId: result.session_id as string,
    running: result.running,
    status: result.status,
    eventSequence: result.event_sequence,
    snapshotEventSequence: result.snapshot_event_sequence,
    messages: result.messages as ObserverSubscriptionV2["messages"],
    inflight: result.inflight as unknown as ObserverSubscriptionV2["inflight"],
    todoSections: rawCollections.todoSections.map(mapTodo),
    subagents: rawCollections.subagents.map(mapSubagent),
    tools: rawCollections.tools.map(mapTool),
    terminals: rawCollections.terminals.map(mapTerminal),
    replayEvents,
  };
}

export function eventStartV2(event: CloudRealtimeV2Event): number {
  return event.params.event_sequence_start ?? event.params.event_sequence;
}

export function isV2LifecycleEvent(type: string): boolean {
  return type === "todo.update" || type === "subagent.update" || type === "tool.update" || type === "terminal.update";
}

export function isDisplaySafeHermesValue(value: unknown): boolean {
  return withinJsonLimits(value) && displaySafeContentValue(value, 0);
}

export function isDisplaySafeHermesText(
  value: unknown,
  minCodePoints: number,
  maxCodePoints: number,
): value is string {
  if (
    typeof value !== "string"
    || !Number.isSafeInteger(minCodePoints)
    || !Number.isSafeInteger(maxCodePoints)
    || minCodePoints < 0
    || maxCodePoints < minCodePoints
  ) return false;
  const codePoints = Array.from(value).length;
  return codePoints >= minCodePoints
    && codePoints <= maxCodePoints
    && value.trim() === value
    && hasWellFormedUnicode(value)
    && isDisplaySafeHermesValue(value);
}

function mapTodo(value: Record<string, unknown>): TodoSectionV2 {
  return {
    turnId: value.turn_id as string,
    sectionId: value.section_id as string,
    revision: value.revision as number,
    firstEventSequence: value.first_event_sequence as number,
    status: value.status as TodoSectionV2["status"],
    items: value.items as TodoSectionV2["items"],
  };
}

function mapSubagent(value: Record<string, unknown>): SubagentProjectionV2 {
  return {
    turnId: value.turn_id as string,
    subagentId: value.subagent_id as string,
    revision: value.revision as number,
    firstEventSequence: value.first_event_sequence as number,
    parentSubagentId: value.parent_subagent_id as string | null,
    name: value.name as string,
    goal: value.goal as string,
    summary: value.summary as string | null,
    status: value.status as SubagentProjectionV2["status"],
  };
}

function mapTool(value: Record<string, unknown>): ToolProjectionV2 {
  return {
    turnId: value.turn_id as string,
    toolCallId: value.tool_call_id as string,
    revision: value.revision as number,
    firstEventSequence: value.first_event_sequence as number,
    status: value.status as ToolProjectionV2["status"],
    name: value.name as string,
    ...(typeof value.call_label === "string" ? { callLabel: value.call_label } : {}),
    ...(typeof value.summary === "string" ? { summary: value.summary } : {}),
  };
}

function mapTerminal(value: Record<string, unknown>): TerminalProjectionV2 {
  return {
    turnId: value.turn_id as string,
    processId: value.process_id as string,
    revision: value.revision as number,
    firstEventSequence: value.first_event_sequence as number,
    status: value.status as TerminalProjectionV2["status"],
    ...(typeof value.summary === "string" ? { summary: value.summary } : {}),
    ...(typeof value.exit_code === "number" ? { exitCode: value.exit_code } : {}),
  };
}

function validRpcError(value: unknown): boolean {
  if (!isRecord(value)) return false;
  const keys = ["code", "message"];
  if ("data" in value) keys.push("data");
  return isExactObject(value, keys)
    && Number.isSafeInteger(value.code)
    && typeof value.message === "string"
    && Array.from(value.message).length <= 4096
    && (!("data" in value) || value.data === null || isRecord(value.data));
}

function isRunningStatus(status: string): boolean {
  return status === "running" || status === "working" || status === "streaming";
}

function withinJsonLimits(value: unknown, depth = 0): boolean {
  if (depth > realtimeV2.limits.max_nesting_depth) return false;
  if (typeof value === "string") {
    return new TextEncoder().encode(value).byteLength <= realtimeV2.limits.max_string_bytes;
  }
  if (value === null || typeof value === "boolean" || typeof value === "number") return true;
  if (Array.isArray(value)) {
    return value.length <= realtimeV2.limits.max_array_items
      && value.every((item) => withinJsonLimits(item, depth + 1));
  }
  if (isRecord(value)) {
    const entries = Object.entries(value);
    return entries.length <= realtimeV2.limits.max_object_fields
      && entries.every(([, item]) => withinJsonLimits(item, depth + 1));
  }
  return false;
}

function hasDisplaySafeEvent(value: Record<string, unknown>): boolean {
  const params = value.params;
  if (!isRecord(params) || !displaySafeContentValue(params.payload, 0)) return false;
  if (!("extensions" in params)) return true;
  if (!isRecord(params.extensions)) return false;
  return displaySafeExtensionValue(params.extensions, 0);
}

function displaySafeContentValue(value: unknown, depth: number): boolean {
  if (depth > realtimeV2.limits.max_nesting_depth) return false;
  if (typeof value === "string") {
    return !hasControlCharacter(value) && !containsCredentialValue(value);
  }
  if (value === null || typeof value === "boolean") return true;
  if (typeof value === "number") return Number.isFinite(value) && Number.isSafeInteger(value);
  if (Array.isArray(value)) {
    return value.length <= realtimeV2.limits.max_array_items
      && value.every((item) => displaySafeContentValue(item, depth + 1));
  }
  if (!isRecord(value)) return false;
  const entries = Object.entries(value);
  return entries.length <= realtimeV2.limits.max_object_fields
    && entries.every(([, item]) => displaySafeContentValue(item, depth + 1));
}

function displaySafeExtensionValue(value: unknown, depth: number): boolean {
  if (depth > realtimeV2.limits.max_nesting_depth) return false;
  if (typeof value === "string") {
    return Array.from(value).length <= parityPolicy.limits.max_display_text_code_points
      && !hasControlCharacter(value)
      && !containsCredentialValue(value);
  }
  if (value === null || typeof value === "boolean") return true;
  if (typeof value === "number") {
    return Number.isFinite(value)
      && Math.abs(value) <= parityPolicy.limits.max_safe_integer
      && (!Number.isInteger(value) || Number.isSafeInteger(value));
  }
  if (Array.isArray(value)) {
    return value.length <= realtimeV2.limits.max_array_items
      && value.every((item) => displaySafeExtensionValue(item, depth + 1));
  }
  if (!isRecord(value)) return false;
  const entries = Object.entries(value);
  if (entries.length > realtimeV2.limits.max_object_fields) return false;
  for (const [key, item] of entries) {
    if (Array.from(key).length > parityPolicy.limits.max_id_code_points || hasControlCharacter(key)) return false;
    const normalized = normalizeExtensionKey(key);
    if (normalized === "token_counts") {
      if (!isAggregateTokenCounts(item)) return false;
      continue;
    }
    const tokens = normalized.split("_").filter(Boolean);
    const credentialKey = tokens.includes("key")
      && tokens.some((token) => CREDENTIAL_KEY_QUALIFIERS.has(token));
    if (credentialKey || tokens.some((token) => SENSITIVE_EXTENSION_TOKENS.has(token))) return false;
    if (!displaySafeExtensionValue(item, depth + 1)) return false;
  }
  return true;
}

function hasControlCharacter(value: string): boolean {
  return Array.from(value).some((character) => {
    const codePoint = character.codePointAt(0) ?? 0;
    return codePoint <= 0x1f || (codePoint >= 0x7f && codePoint <= 0x9f);
  });
}

function containsCredentialValue(value: string): boolean {
  if (CREDENTIAL_VALUE.test(value) || CREDENTIAL_VALUE_CASE_INSENSITIVE.test(value)) return true;
  for (const match of value.matchAll(JWT_SHAPE)) {
    if (isSemanticJwt(match[1]!, match[2]!, match[3]!)) return true;
  }
  for (const match of value.matchAll(/\bBasic[\t ]+([A-Za-z0-9+/]+={0,2})(?![A-Za-z0-9+/=])/giu)) {
    const decoded = decodeStrictBase64(match[1]!);
    if (decoded !== null && decoded.indexOf(":") > 0) return true;
  }
  return false;
}

function hasWellFormedUnicode(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    const codeUnit = value.charCodeAt(index);
    if (codeUnit >= 0xd800 && codeUnit <= 0xdbff) {
      const next = value.charCodeAt(index + 1);
      if (next < 0xdc00 || next > 0xdfff) return false;
      index += 1;
    } else if (codeUnit >= 0xdc00 && codeUnit <= 0xdfff) {
      return false;
    }
  }
  return true;
}

function isSemanticJwt(headerValue: string, payloadValue: string, signatureValue: string): boolean {
  const header = decodeBase64UrlJsonObject(headerValue);
  const payload = decodeBase64UrlJsonObject(payloadValue);
  const signature = decodeStrictBase64Url(signatureValue);
  return header !== null
    && payload !== null
    && typeof header.alg === "string"
    && header.alg.trim().length > 0
    && signature !== null
    && signature.length > 0;
}

function decodeBase64UrlJsonObject(value: string): Record<string, unknown> | null {
  const decoded = decodeStrictBase64Url(value);
  if (decoded === null) return null;
  try {
    const parsed: unknown = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(decoded));
    return isRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function decodeStrictBase64Url(value: string): Uint8Array | null {
  if (!/^[A-Za-z0-9_-]+$/u.test(value) || value.length % 4 === 1) return null;
  const standard = value.replace(/-/gu, "+").replace(/_/gu, "/");
  const decoded = decodeStrictBase64(standard);
  if (decoded === null) return null;
  const canonical = btoa(decoded)
    .replace(/\+/gu, "-")
    .replace(/\//gu, "_")
    .replace(/=+$/u, "");
  return canonical === value ? Uint8Array.from(decoded, (character) => character.charCodeAt(0)) : null;
}

function decodeStrictBase64(value: string): string | null {
  if (!/^[A-Za-z0-9+/]+={0,2}$/u.test(value) || value.length % 4 === 1) return null;
  const unpadded = value.replace(/=+$/u, "");
  const padded = unpadded.padEnd(unpadded.length + ((4 - (unpadded.length % 4)) % 4), "=");
  try {
    const decoded = atob(padded);
    return btoa(decoded).replace(/=+$/u, "") === unpadded ? decoded : null;
  } catch {
    return null;
  }
}

function normalizeExtensionKey(value: string): string {
  return value
    .replace(/(?<=[a-z0-9])(?=[A-Z])/g, "_")
    .replace(/[^A-Za-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .toLowerCase();
}

function isAggregateTokenCounts(value: unknown): boolean {
  if (!isRecord(value)) return false;
  const entries = Object.entries(value);
  return entries.length > 0
    && entries.every(([key, item]) => (
      TOKEN_COUNT_FIELDS.has(key)
      && Number.isSafeInteger(item)
      && (item as number) >= 0
    ));
}

function isExactObject<K extends string>(value: unknown, keys: readonly K[]): value is Record<K, unknown> {
  if (!isRecord(value)) return false;
  const actual = Object.keys(value);
  return actual.length === keys.length && actual.every((key) => keys.includes(key as K));
}

function positiveInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) >= 1;
}

function nonNegativeInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) >= 0;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function invalid(): CloudRealtimeV2DecodeResult {
  return { ok: false, reason: "invalid_frame" };
}

export const CLOUD_OBSERVER_V2_SUBPROTOCOL = realtimeV2.websocket_subprotocol;
export const CLOUD_OBSERVER_V2_MERGEABLE_EVENT_TYPES = Object.freeze([...MERGEABLE_EVENT_TYPES]);
export const CLOUD_OBSERVER_V2_EVENT_TYPE_SET = EVENT_TYPE_SET;
