#!/usr/bin/env node

import { randomUUID } from "node:crypto";
import { constants as filesystemConstants } from "node:fs";
import { lstat, open } from "node:fs/promises";
import { pathToFileURL } from "node:url";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const PROFILE_PATTERN = /^[A-Za-z0-9_.-]{1,128}$/;
const FORBIDDEN_CATALOG_IDENTITY = /(?:^|[^a-z0-9])(?:demo|fixture|test)(?:[^a-z0-9]|$)/iu;
const MAX_HTTP_BYTES = 256 * 1024;
const MAX_WS_FRAME_BYTES = 256 * 1024;
const MAX_PROMPT_BYTES = 128 * 1024;
const MAX_TOKEN_BYTES = 4 * 1024;
const DEFAULT_TIMEOUT_MS = 120_000;
const EVIDENCE_NAMES = new Set(["todo", "tool", "pending_input", "approval"]);
const REQUIRED_CONTROL_METHODS = [
  "session.control.acquire",
  "session.control.renew",
  "session.control.release",
  "session.control.status",
  "prompt.submit",
];
const CONTROL_METHODS = new Set([
  "session.control.acquire",
  "session.control.renew",
  "session.control.release",
  "session.control.status",
  "session.command.status",
  "prompt.submit",
  "session.interrupt",
  "session.steer",
  "approval.respond",
  "clarify.respond",
]);
const CONTROL_ERROR_CODES = Object.freeze({
  command_unknown: 4210,
  control_contract_unsupported: 4201,
  control_role_required: 4200,
  controller_conflict: 4203,
  deadline_exceeded_before_effect: 4306,
  effect_unknown: 4307,
  invalid_pending_response: 4213,
  lease_expired: 4205,
  lease_mismatch: 4206,
  lease_required: 4204,
  live_runtime_unavailable: 4202,
  method_not_allowed: 4209,
  owner_adapter_unavailable: 4214,
  pending_request_conflict: 4208,
  relay_overloaded: 4215,
  request_id_payload_conflict: 4207,
  revision_conflict: 4211,
  session_binding_mismatch: 4212,
});
const OBSERVER_EVENT_TYPES = new Set([
  "message.start",
  "message.delta",
  "message.complete",
  "agent.terminal.output",
  "reasoning.delta",
  "status.update",
  "thinking.delta",
  "tool.output.delta",
  "todo.update",
  "subagent.update",
  "tool.update",
  "terminal.update",
]);
const LIFECYCLE_STATUSES = new Set(["running", "completed", "failed", "interrupted", "unknown"]);
const TODO_STATUSES = new Set(["pending", "in_progress", "completed", "cancelled"]);
const SESSION_KEYS = [
  "id", "agent_id", "workspace_id", "_lineage_root_id", "parent_session_id", "title", "preview",
  "source", "model", "profile", "cwd", "git_branch", "started_at", "ended_at", "last_active",
  "message_count", "tool_call_count", "input_tokens", "output_tokens", "is_active", "archived",
  "directory_source", "availability", "runtime_generation", "surface", "authority_revision",
  "available_actions", "transcript_available",
];

export class GateConfigurationError extends Error {
  constructor(message = "Real full-chain gate configuration is invalid.") {
    super(message);
    this.name = "GateConfigurationError";
    this.code = "INVALID_CONFIGURATION";
    this.step = "configuration";
  }
}

export class GateVerificationError extends Error {
  constructor(code, step, message) {
    super(message);
    this.name = "GateVerificationError";
    this.code = code;
    this.step = step;
  }
}

export function parseGateArguments(argv, policy = {}) {
  if (!Array.isArray(argv)) throw new GateConfigurationError();
  const values = new Map();
  const requiredEvidence = new Set();
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    if (typeof flag !== "string" || !flag.startsWith("--") || typeof value !== "string") {
      throw new GateConfigurationError();
    }
    if (flag === "--require-evidence") {
      for (const name of value.split(",")) {
        if (!EVIDENCE_NAMES.has(name) || requiredEvidence.has(name)) throw new GateConfigurationError();
        requiredEvidence.add(name);
      }
      continue;
    }
    if (!new Set([
      "--cloud-url",
      "--access-token",
      "--access-token-file",
      "--agent-id",
      "--session-id",
      "--prompt",
      "--prompt-file",
      "--timeout-ms",
    ]).has(flag) || values.has(flag)) throw new GateConfigurationError();
    values.set(flag, value);
  }

  const cloudUrl = parseCloudUrl(values.get("--cloud-url"), policy.allowInsecureLoopback === true);
  const agentId = canonicalUuid(values.get("--agent-id"));
  const sessionId = canonicalUuid(values.get("--session-id"));
  const accessToken = exclusiveInput(values, "--access-token", "--access-token-file");
  const prompt = exclusiveInput(values, "--prompt", "--prompt-file");
  const timeoutValue = values.get("--timeout-ms");
  const timeoutMs = timeoutValue === undefined ? DEFAULT_TIMEOUT_MS : Number(timeoutValue);
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 5_000 || timeoutMs > 300_000) {
    throw new GateConfigurationError();
  }
  if (accessToken.kind === "inline") validateAccessToken(accessToken.value);
  if (prompt.kind === "inline") validatePrompt(prompt.value);
  return {
    cloudUrl,
    agentId,
    sessionId,
    accessToken,
    prompt,
    timeoutMs,
    requiredEvidence,
  };
}

export async function runRealFullChainGate(argv, policy = {}) {
  const config = parseGateArguments(argv, policy);
  const accessToken = await materializeInput(config.accessToken, MAX_TOKEN_BYTES, validateAccessToken);
  const prompt = await materializeInput(config.prompt, MAX_PROMPT_BYTES, validatePrompt);
  const deadline = Date.now() + config.timeoutMs;
  const authorization = `Bearer ${accessToken}`;
  let observer = null;
  let control = null;
  let lease = null;
  let released = false;
  try {
    await verifyCloudReady(config.cloudUrl, deadline);
    const { agent, session } = await verifyCatalog(config, authorization, deadline);
    const clientInstanceId = randomUUID();
    observer = await connectObserver({
      cloudUrl: config.cloudUrl,
      authorization,
      clientInstanceId,
      agentId: config.agentId,
      sessionId: config.sessionId,
      profile: session.profile,
      deadline,
    });
    control = await connectControl({
      cloudUrl: config.cloudUrl,
      authorization,
      clientInstanceId,
      agentId: config.agentId,
      sessionId: config.sessionId,
      deadline,
    });

    const evidence = createEvidence();
    collectSnapshotEvidence(observer.subscription, observer.subscription.eventSequence, evidence);
    const initialStatus = validateControllerStatus(
      await control.socket.call("session.control.status", { session_id: config.sessionId }, deadline),
      "control_status",
    );
    collectPendingEvidence(initialStatus.pendingInput, evidence);
    if (initialStatus.controllerKind !== "none") {
      throw new GateVerificationError(
        "CONTROLLER_ALREADY_HELD",
        "controller_lease",
        "The selected session already has an authoritative controller.",
      );
    }

    lease = validateLease(
      await control.socket.call("session.control.acquire", { session_id: config.sessionId }, deadline),
      deadline,
    );
    collectPendingEvidence(lease.pendingInput, evidence);
    const reconciled = validateControllerStatus(
      await control.socket.call("session.control.status", { session_id: config.sessionId }, deadline),
      "control_reconcile",
    );
    collectPendingEvidence(reconciled.pendingInput, evidence);
    if (!statusMatchesLease(reconciled, lease)) {
      throw new GateVerificationError(
        "CONTROLLER_LEASE_MISMATCH",
        "controller_lease",
        "The authoritative controller status did not match the acquired lease.",
      );
    }

    const clientRequestId = randomUUID();
    const clientTurnId = randomUUID();
    const promptResult = validatePromptResult(
      await control.socket.call("prompt.submit", {
        session_id: config.sessionId,
        lease_id: lease.leaseId,
        client_request_id: clientRequestId,
        client_turn_id: clientTurnId,
        text: prompt,
      }, deadline),
      clientRequestId,
      clientTurnId,
    );
    const observed = await observeAssistantCompletion(observer, evidence, deadline);

    const postPromptStatus = validateControllerStatus(
      await control.socket.call("session.control.status", { session_id: config.sessionId }, deadline),
      "post_prompt_status",
    );
    collectPendingEvidence(postPromptStatus.pendingInput, evidence);

    await observer.socket.close();
    const reconnected = await connectObserver({
      cloudUrl: config.cloudUrl,
      authorization,
      clientInstanceId,
      agentId: config.agentId,
      sessionId: config.sessionId,
      profile: session.profile,
      deadline,
    });
    observer = reconnected;
    verifyReconnectContinuity(observed, reconnected.subscription);
    verifyPromptTranscript(
      reconnected.subscription.messages,
      observed.initialMessageCount,
      prompt,
      observed.assistantText,
    );
    collectSnapshotEvidence(reconnected.subscription, observed.baselineSequence, evidence);
    enforceRequiredEvidence(config.requiredEvidence, evidence);

    await releaseLease(control, config.sessionId, lease.leaseId, deadline);
    released = true;
    return {
      schema_version: 1,
      gate: "hermes-real-full-chain",
      status: "passed",
      cloud_ready: true,
      authenticated: true,
      agent_id: agent.agentId,
      session_id: session.id,
      observer_contract: 2,
      control_contract: 1,
      prompt_status: promptResult.status,
      assistant_stream_ordered: true,
      assistant_terminal_event: "message.complete",
      reconnect_same_session: true,
      reconnect_sequence_continuous: true,
      evidence: evidenceReceipt(evidence),
    };
  } catch (error) {
    if (error instanceof GateConfigurationError || error instanceof GateVerificationError) throw error;
    throw new GateVerificationError(
      "GATE_EXECUTION_FAILED",
      "gate",
      "The real full-chain gate failed closed.",
    );
  } finally {
    if (!released && lease !== null && control !== null) {
      try {
        await releaseLease(control, config.sessionId, lease.leaseId, Math.max(deadline, Date.now() + 1_000));
      } catch {
        // Local authority is discarded below even when the best-effort release cannot be confirmed.
      }
    }
    await Promise.allSettled([observer?.socket.close(), control?.socket.close()]);
  }
}

async function verifyCloudReady(cloudUrl, deadline) {
  const value = await requestJson(endpoint(cloudUrl, "ready"), {
    method: "GET",
    headers: { Accept: "application/json" },
  }, deadline, "cloud_ready", "CLOUD_NOT_READY");
  if (
    !exactObject(value, ["component", "error", "live", "ready", "state"])
    || value.component !== "business-api"
    || value.error !== null
    || value.live !== true
    || value.ready !== true
    || value.state !== "READY"
  ) {
    throw new GateVerificationError(
      "CLOUD_NOT_READY",
      "cloud_ready",
      "Cloud did not return the exact ready receipt.",
    );
  }
}

async function verifyCatalog(config, authorization, deadline) {
  const agentsValue = await authenticatedJson(
    endpoint(config.cloudUrl, "api/v1/agents"),
    authorization,
    deadline,
    "authentication",
  );
  if (!exactObject(agentsValue, ["agents"]) || !Array.isArray(agentsValue.agents) || agentsValue.agents.length > 256) {
    throw invalidCatalog();
  }
  const agents = agentsValue.agents.map(parseAgent);
  if (new Set(agents.map((agent) => agent.agentId)).size !== agents.length) throw invalidCatalog();
  const agent = agents.find((candidate) => candidate.agentId === config.agentId);
  if (agent === undefined || agent.status !== "active") {
    throw new GateVerificationError(
      "AGENT_NOT_ACTIVE",
      "catalog",
      "The explicit Agent target is not active in the authenticated catalog.",
    );
  }
  rejectForbiddenCatalogIdentity(agent.agentKey);

  let offset = 0;
  let expectedTotal = null;
  let target = null;
  const seenIds = new Set();
  const seenKeys = new Set();
  do {
    const url = endpoint(config.cloudUrl, `api/v1/agents/${config.agentId}/sessions`);
    url.searchParams.set("min_messages", "0");
    url.searchParams.set("archived", "exclude");
    url.searchParams.set("order", "recent");
    url.searchParams.set("limit", "50");
    url.searchParams.set("offset", String(offset));
    const value = await authenticatedJson(url, authorization, deadline, "catalog");
    if (!exactObject(value, ["sessions", "total", "limit", "offset"]) || !Array.isArray(value.sessions)) {
      throw invalidCatalog();
    }
    const total = boundedInteger(value.total, 0, 1_000);
    const limit = boundedInteger(value.limit, 1, 50);
    const returnedOffset = boundedInteger(value.offset, 0, 1_000);
    if (limit !== 50 || returnedOffset !== offset || value.sessions.length > limit || offset + value.sessions.length > total) {
      throw invalidCatalog();
    }
    if (expectedTotal === null) expectedTotal = total;
    if (total !== expectedTotal) throw invalidCatalog();
    for (const raw of value.sessions) {
      const session = parseSession(raw, config.agentId);
      if (seenIds.has(session.id) || seenKeys.has(session.sessionKey)) throw invalidCatalog();
      seenIds.add(session.id);
      seenKeys.add(session.sessionKey);
      if (session.id === config.sessionId) target = session;
    }
    if (value.sessions.length === 0 && offset < total) throw invalidCatalog();
    offset += value.sessions.length;
  } while (expectedTotal !== null && offset < expectedTotal);

  if (target === null) {
    throw new GateVerificationError(
      "SESSION_NOT_IN_CATALOG",
      "catalog",
      "The explicit session target is not present in the Agent catalog.",
    );
  }
  rejectForbiddenCatalogIdentity(target.sessionKey);
  if (target.title !== null) rejectForbiddenCatalogIdentity(target.title);
  if (
    target.directorySource !== "host_catalog"
    || target.availability !== "live"
    || target.isActive !== true
    || target.runtimeGeneration === null
    || target.surface === null
    || target.authorityRevision === null
    || !target.availableActions.includes("prompt.submit")
  ) {
    throw new GateVerificationError(
      "SESSION_NOT_REALTIME_CAPABLE",
      "catalog",
      "The explicit session target is not a live authoritative host-catalog session.",
    );
  }
  return { agent, session: target };
}

async function authenticatedJson(url, authorization, deadline, step) {
  return requestJson(url, {
    method: "GET",
    headers: { Accept: "application/json", Authorization: authorization },
  }, deadline, step, step === "authentication" ? "AUTHENTICATION_FAILED" : "CATALOG_UNAVAILABLE");
}

function parseAgent(value) {
  if (!exactObject(value, ["agent_id", "workspace_id", "agent_key", "status", "last_seen_at"])) throw invalidCatalog();
  const status = value.status;
  if (!UUID_PATTERN.test(value.agent_id) || !UUID_PATTERN.test(value.workspace_id)) throw invalidCatalog();
  if (status !== "active" && status !== "offline" && status !== "disabled") throw invalidCatalog();
  safeText(value.agent_key, 1, 128);
  if (value.last_seen_at !== null && !validIsoTimestamp(value.last_seen_at)) throw invalidCatalog();
  return { agentId: value.agent_id, agentKey: value.agent_key, status };
}

function parseSession(value, expectedAgentId) {
  if (!exactObject(value, SESSION_KEYS)) throw invalidCatalog();
  if (!UUID_PATTERN.test(value.id) || value.agent_id !== expectedAgentId) throw invalidCatalog();
  const sessionKey = safeText(value._lineage_root_id, 1, 256);
  const profile = safeText(value.profile, 1, 128);
  if (!PROFILE_PATTERN.test(profile)) throw invalidCatalog();
  const title = value.title === null ? null : safeText(value.title, 1, 512);
  const workspaceId = value.workspace_id === null ? null : canonicalCatalogUuid(value.workspace_id);
  if (!Array.isArray(value.available_actions) || value.available_actions.length > CONTROL_METHODS.size) throw invalidCatalog();
  if (
    !value.available_actions.every((method) => typeof method === "string" && CONTROL_METHODS.has(method))
    || new Set(value.available_actions).size !== value.available_actions.length
  ) throw invalidCatalog();
  if (
    value.parent_session_id !== null
    || value.preview !== null
    || value.source !== null
    || value.model !== null
    || value.cwd !== null
    || value.git_branch !== null
    || typeof value.is_active !== "boolean"
    || value.archived !== false
    || (value.directory_source !== "host_catalog" && value.directory_source !== "transcript_projection")
    || (value.availability !== "live" && value.availability !== "offline")
    || typeof value.transcript_available !== "boolean"
  ) throw invalidCatalog();
  for (const field of ["message_count", "tool_call_count", "input_tokens", "output_tokens"]) {
    boundedInteger(value[field], 0, Number.MAX_SAFE_INTEGER);
  }
  for (const field of ["started_at", "ended_at", "last_active"]) {
    if (value[field] !== null && (!Number.isFinite(value[field]) || value[field] < 0)) throw invalidCatalog();
  }
  const runtimeGeneration = value.runtime_generation === null ? null : safeText(value.runtime_generation, 1, 128);
  const surface = value.surface === null ? null : safeText(value.surface, 1, 64);
  const authorityRevision = value.authority_revision === null
    ? null
    : boundedInteger(value.authority_revision, 1, Number.MAX_SAFE_INTEGER);
  if (value.directory_source === "host_catalog") {
    if (
      workspaceId !== null
      || title !== null
      || value.started_at !== null
      || value.ended_at !== null
      || value.last_active !== null
      || value.message_count !== 0
      || value.tool_call_count !== 0
      || value.input_tokens !== 0
      || value.output_tokens !== 0
      || runtimeGeneration === null
      || surface === null
      || authorityRevision === null
      || value.transcript_available !== false
    ) throw invalidCatalog();
  } else if (
    runtimeGeneration !== null
    || surface !== null
    || authorityRevision !== null
    || value.available_actions.length !== 0
    || value.transcript_available !== true
  ) throw invalidCatalog();
  return {
    id: value.id,
    sessionKey,
    profile,
    title,
    directorySource: value.directory_source,
    availability: value.availability,
    isActive: value.is_active,
    runtimeGeneration,
    surface,
    authorityRevision,
    availableActions: [...value.available_actions],
  };
}

async function connectObserver(options) {
  const ticket = await mintTicket(options, "observer");
  const socket = await JsonRpcSocket.connect(ticketWebSocketUrl(options.cloudUrl, ticket), "hermes.tui.v2", options.deadline);
  try {
    const ready = await socket.nextFrame(options.deadline, "OBSERVER_READY_INVALID", "observer_ready");
    if (!isObserverReady(ready)) {
      throw new GateVerificationError(
        "OBSERVER_READY_INVALID",
        "observer_ready",
        "Cloud did not advertise the exact observer-v2 ready contract.",
      );
    }
    const rawSubscription = await socket.call("session.observe.subscribe", {
      observer_contract: 2,
      session_id: options.sessionId,
      profile: options.profile,
      agent_id: options.agentId,
    }, options.deadline);
    const subscription = validateSubscription(rawSubscription, options.sessionId, options.profile);
    return { socket, subscription };
  } catch (error) {
    await socket.close();
    throw error;
  }
}

async function connectControl(options) {
  const ticket = await mintTicket(options, "control");
  const socket = await JsonRpcSocket.connect(ticketWebSocketUrl(options.cloudUrl, ticket), "hermes.tui.v1", options.deadline);
  try {
    const ready = await socket.nextFrame(options.deadline, "CONTROL_READY_INVALID", "control_ready");
    const methods = validateControlReady(ready);
    if (!REQUIRED_CONTROL_METHODS.every((method) => methods.includes(method))) {
      throw new GateVerificationError(
        "CONTROL_CAPABILITIES_INCOMPLETE",
        "control_ready",
        "Cloud did not advertise the complete controller and prompt contract.",
      );
    }
    return { socket, methods };
  } catch (error) {
    await socket.close();
    throw error;
  }
}

async function mintTicket(options, role) {
  const body = role === "observer"
    ? {
        connection_role: "observer",
        client_instance_id: options.clientInstanceId,
        agent_id: options.agentId,
        observer_contract: 2,
      }
    : {
        connection_role: "control",
        client_instance_id: options.clientInstanceId,
        agent_id: options.agentId,
        session_id: options.sessionId,
      };
  const value = await requestJson(endpoint(options.cloudUrl, "api/auth/ws-ticket"), {
    method: "POST",
    headers: {
      Accept: "application/json",
      Authorization: options.authorization,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  }, options.deadline, "ticket", "TICKET_MINT_FAILED");
  const expectedKeys = role === "observer"
    ? ["ticket", "ttl_seconds", "connection_role", "observer_contract"]
    : ["ticket", "ttl_seconds", "connection_role"];
  if (
    !exactObject(value, expectedKeys)
    || typeof value.ticket !== "string"
    || value.ticket.length < 32
    || value.ticket.length > 4_096
    || !Number.isSafeInteger(value.ttl_seconds)
    || value.ttl_seconds < 1
    || value.ttl_seconds > 60
    || value.connection_role !== role
    || (role === "observer" && value.observer_contract !== 2)
  ) {
    throw new GateVerificationError(
      "TICKET_RESPONSE_INVALID",
      "ticket",
      "Cloud returned an invalid single-use WebSocket ticket receipt.",
    );
  }
  return value.ticket;
}

function validateSubscription(value, expectedSessionId, expectedProfile) {
  const keys = [
    "observer_contract", "subscription_id", "profile", "runtime_generation", "session_id", "running", "status",
    "event_sequence", "snapshot_event_sequence", "messages", "inflight", "todo_sections", "subagents", "tools",
    "terminals", "replay_events",
  ];
  if (
    !exactObject(value, keys)
    || value.observer_contract !== 2
    || value.session_id !== expectedSessionId
    || value.profile !== expectedProfile
    || typeof value.subscription_id !== "string"
    || value.subscription_id.length === 0
    || typeof value.runtime_generation !== "string"
    || value.runtime_generation.length === 0
    || typeof value.running !== "boolean"
    || typeof value.status !== "string"
    || value.status.length === 0
    || !nonNegativeInteger(value.event_sequence)
    || !nonNegativeInteger(value.snapshot_event_sequence)
    || value.snapshot_event_sequence > value.event_sequence
    || !Array.isArray(value.messages)
    || !Array.isArray(value.todo_sections)
    || !Array.isArray(value.subagents)
    || !Array.isArray(value.tools)
    || !Array.isArray(value.terminals)
    || !Array.isArray(value.replay_events)
    || !validInflight(value.inflight)
  ) throw invalidObserverSubscription();
  const messages = value.messages.map(parseTranscriptMessage);
  const todoSections = value.todo_sections.map(validateTodoSnapshot);
  const tools = value.tools.map(validateToolSnapshot);
  if (!value.subagents.every(validSubagentSnapshot) || !value.terminals.every(validTerminalSnapshot)) {
    throw invalidObserverSubscription();
  }
  let lastSequence = value.snapshot_event_sequence;
  const replayEvents = [];
  for (const params of value.replay_events) {
    const event = validateObserverEventFrame({ jsonrpc: "2.0", method: "event", params }, {
      sessionId: expectedSessionId,
      profile: expectedProfile,
      runtimeGeneration: value.runtime_generation,
      lastSequence,
    });
    lastSequence = event.eventSequence;
    replayEvents.push(event);
  }
  if (lastSequence !== value.event_sequence) throw invalidObserverSubscription();
  return {
    sessionId: value.session_id,
    profile: value.profile,
    runtimeGeneration: value.runtime_generation,
    eventSequence: value.event_sequence,
    snapshotEventSequence: value.snapshot_event_sequence,
    messages,
    todoSections,
    tools,
    replayEvents,
  };
}

async function observeAssistantCompletion(observer, evidence, deadline) {
  const baselineSequence = observer.subscription.eventSequence;
  const initialMessageCount = observer.subscription.messages.length;
  let lastSequence = baselineSequence;
  let started = false;
  let completed = false;
  let assistantText = "";
  const digests = new Map();
  while (!completed) {
    const frame = await observer.socket.nextFrame(deadline, "ASSISTANT_TERMINAL_EVENT_MISSING", "assistant_stream");
    const event = validateObserverEventFrame(frame, {
      sessionId: observer.subscription.sessionId,
      profile: observer.subscription.profile,
      runtimeGeneration: observer.subscription.runtimeGeneration,
      lastSequence,
    });
    lastSequence = event.eventSequence;
    digests.set(event.eventSequence, event.digest);
    if (event.type === "todo.update") evidence.todo = true;
    if (event.type === "tool.update" || event.type === "tool.output.delta") evidence.tool = true;
    if (event.type === "message.start") {
      if (started || completed || event.payload.role !== "assistant") throw invalidAssistantOrder();
      started = true;
    } else if (event.type === "message.delta") {
      if (!started || completed || typeof event.payload.text !== "string") throw invalidAssistantOrder();
      assistantText += event.payload.text;
    } else if (event.type === "message.complete") {
      if (
        !started
        || completed
        || assistantText.length === 0
        || event.payload.status !== "complete"
        || ("error" in event.payload && event.payload.error !== null)
        || ("text" in event.payload && event.payload.text !== assistantText)
      ) throw invalidAssistantOrder();
      completed = true;
    }
  }
  return {
    baselineSequence,
    initialMessageCount,
    lastSequence,
    sessionId: observer.subscription.sessionId,
    profile: observer.subscription.profile,
    runtimeGeneration: observer.subscription.runtimeGeneration,
    assistantText,
    digests,
  };
}

function validateObserverEventFrame(frame, expected) {
  if (!exactObject(frame, ["jsonrpc", "method", "params"]) || frame.jsonrpc !== "2.0" || frame.method !== "event") {
    throw invalidObserverEvent();
  }
  const params = frame.params;
  const keys = ["observer_contract", "profile", "runtime_generation", "type", "session_id", "event_sequence", "payload"];
  if (isRecord(params) && "event_sequence_start" in params) keys.push("event_sequence_start");
  if (isRecord(params) && "extensions" in params) keys.push("extensions");
  if (
    !exactObject(params, keys)
    || params.observer_contract !== 2
    || params.session_id !== expected.sessionId
    || params.profile !== expected.profile
    || params.runtime_generation !== expected.runtimeGeneration
    || typeof params.type !== "string"
    || !OBSERVER_EVENT_TYPES.has(params.type)
    || !positiveInteger(params.event_sequence)
    || !isRecord(params.payload)
    || !validObserverPayload(params.type, params.payload)
  ) throw invalidObserverEvent();
  const start = "event_sequence_start" in params ? params.event_sequence_start : params.event_sequence;
  if (!positiveInteger(start) || start > params.event_sequence || start !== expected.lastSequence + 1) {
    throw new GateVerificationError(
      "OBSERVER_SEQUENCE_DISCONTINUITY",
      "observer_stream",
      "Observer event_sequence continuity was broken.",
    );
  }
  return {
    type: params.type,
    payload: params.payload,
    eventSequence: params.event_sequence,
    eventSequenceStart: start,
    digest: JSON.stringify(params),
  };
}

function verifyReconnectContinuity(observed, subscription) {
  if (
    subscription.sessionId !== observed.sessionId
    || subscription.profile !== observed.profile
    || subscription.runtimeGeneration !== observed.runtimeGeneration
  ) {
    throw new GateVerificationError(
      "RECONNECT_IDENTITY_CHANGED",
      "observer_reconnect",
      "Observer reconnect changed the authoritative session identity.",
    );
  }
  if (
    subscription.snapshotEventSequence < observed.baselineSequence
    || subscription.snapshotEventSequence > observed.lastSequence
    || subscription.eventSequence < observed.lastSequence
  ) {
    throw reconnectDiscontinuity();
  }
  let cursor = subscription.snapshotEventSequence;
  for (const event of subscription.replayEvents) {
    if (event.eventSequenceStart !== cursor + 1) throw reconnectDiscontinuity();
    if (event.eventSequenceStart <= observed.lastSequence && event.eventSequence > observed.lastSequence) {
      throw reconnectDiscontinuity();
    }
    if (event.eventSequence <= observed.lastSequence) {
      if (observed.digests.get(event.eventSequence) !== event.digest) throw reconnectDiscontinuity();
    }
    cursor = event.eventSequence;
  }
  if (cursor !== subscription.eventSequence || cursor < observed.lastSequence) throw reconnectDiscontinuity();
}

function verifyPromptTranscript(messages, initialMessageCount, prompt, assistantText) {
  let promptIndex = -1;
  for (let index = initialMessageCount; index < messages.length; index += 1) {
    if (messages[index].role === "user" && messages[index].content === prompt) promptIndex = index;
  }
  const assistant = promptIndex < 0 ? undefined : messages.slice(promptIndex + 1).find((message) => message.role === "assistant");
  if (assistant?.content !== assistantText) {
    throw new GateVerificationError(
      "PROMPT_TRANSCRIPT_NOT_CONFIRMED",
      "observer_reconnect",
      "The reconnected transcript did not confirm the submitted prompt and assistant response.",
    );
  }
}

function validateControlReady(value) {
  if (!exactObject(value, ["jsonrpc", "method", "params"]) || value.jsonrpc !== "2.0" || value.method !== "event") {
    throw invalidControlReady();
  }
  if (!exactObject(value.params, ["type", "payload"]) || value.params.type !== "gateway.ready") throw invalidControlReady();
  const payload = value.params.payload;
  if (
    !exactObject(payload, [
      "observer_contract", "control_contract", "connection_role", "control_available_methods", "control_error_codes",
    ])
    || payload.observer_contract !== 1
    || payload.control_contract !== 1
    || payload.connection_role !== "control"
    || !Array.isArray(payload.control_available_methods)
    || !payload.control_available_methods.every((method) => typeof method === "string" && CONTROL_METHODS.has(method))
    || new Set(payload.control_available_methods).size !== payload.control_available_methods.length
    || !exactObject(payload.control_error_codes, Object.keys(CONTROL_ERROR_CODES))
    || !Object.entries(CONTROL_ERROR_CODES).every(([name, code]) => payload.control_error_codes[name] === code)
  ) throw invalidControlReady();
  return [...payload.control_available_methods];
}

function validateControllerStatus(value, step) {
  if (!exactObject(value, [
    "controller_kind", "controller_label", "control_revision", "lease_expires_at_epoch_ms", "pending_input",
  ])) throw invalidControllerReceipt(step);
  const kind = value.controller_kind === "local" ? "desktop" : value.controller_kind;
  if (
    (kind !== "none" && kind !== "mobile" && kind !== "desktop")
    || (kind === "none" ? value.controller_label !== null : !safeControllerLabel(value.controller_label))
    || !nonNegativeInteger(value.control_revision)
    || !nonNegativeInteger(value.lease_expires_at_epoch_ms)
  ) throw invalidControllerReceipt(step);
  const pendingInput = validatePendingInput(value.pending_input, step);
  return {
    controllerKind: kind,
    controllerLabel: value.controller_label,
    controlRevision: value.control_revision,
    leaseExpiresAtEpochMs: value.lease_expires_at_epoch_ms,
    pendingInput,
  };
}

function validateLease(value, deadline) {
  if (!exactObject(value, [
    "lease_id", "expires_at_epoch_ms", "control_revision", "controller_kind", "controller_label", "pending_input",
  ])) throw invalidControllerReceipt("controller_acquire");
  if (
    typeof value.lease_id !== "string"
    || value.lease_id.length === 0
    || !nonNegativeInteger(value.expires_at_epoch_ms)
    || value.expires_at_epoch_ms <= Date.now()
    || value.expires_at_epoch_ms > deadline + 300_000
    || !nonNegativeInteger(value.control_revision)
    || value.controller_kind !== "mobile"
    || !safeControllerLabel(value.controller_label)
  ) throw invalidControllerReceipt("controller_acquire");
  return {
    leaseId: value.lease_id,
    expiresAtEpochMs: value.expires_at_epoch_ms,
    controlRevision: value.control_revision,
    controllerLabel: value.controller_label,
    pendingInput: validatePendingInput(value.pending_input, "controller_acquire"),
  };
}

function validatePendingInput(value, step) {
  if (value === null) return null;
  if (!isRecord(value) || (value.kind !== "approval" && value.kind !== "clarify")) throw invalidControllerReceipt(step);
  if (value.kind === "approval") {
    if (
      !exactObject(value, ["request_id", "kind", "title", "description", "command", "choices", "expires_at_epoch_ms"])
      || typeof value.request_id !== "string"
      || value.request_id.length === 0
      || typeof value.title !== "string"
      || value.title.length === 0
      || typeof value.description !== "string"
      || typeof value.command !== "string"
      || !Array.isArray(value.choices)
      || value.choices.length === 0
      || !value.choices.every((choice) => ["allow_once", "allow_session", "allow_always", "deny"].includes(choice))
      || !nonNegativeInteger(value.expires_at_epoch_ms)
    ) throw invalidControllerReceipt(step);
    return { kind: "approval" };
  }
  if (
    !exactObject(value, ["request_id", "kind", "question", "choices", "allow_other", "expires_at_epoch_ms"])
    || typeof value.request_id !== "string"
    || value.request_id.length === 0
    || typeof value.question !== "string"
    || value.question.length === 0
    || !Array.isArray(value.choices)
    || typeof value.allow_other !== "boolean"
    || !nonNegativeInteger(value.expires_at_epoch_ms)
  ) throw invalidControllerReceipt(step);
  return { kind: "clarify" };
}

function validatePromptResult(value, expectedRequestId, expectedTurnId) {
  const keys = ["status", "client_request_id"];
  if (isRecord(value) && "client_turn_id" in value) keys.push("client_turn_id");
  if (isRecord(value) && "server_turn_id" in value) keys.push("server_turn_id");
  if (
    !exactObject(value, keys)
    || (value.status !== "accepted" && value.status !== "queued")
    || value.client_request_id !== expectedRequestId
    || value.client_turn_id !== expectedTurnId
    || ("server_turn_id" in value && (typeof value.server_turn_id !== "string" || value.server_turn_id.length === 0))
  ) {
    throw new GateVerificationError(
      "PROMPT_ACK_INVALID",
      "prompt_submit",
      "Cloud returned an invalid prompt.submit receipt.",
    );
  }
  return { status: value.status };
}

async function releaseLease(control, sessionId, leaseId, deadline) {
  const value = await control.socket.call("session.control.release", {
    session_id: sessionId,
    lease_id: leaseId,
  }, deadline);
  if (!exactObject(value, ["released", "control_revision"]) || value.released !== true || !nonNegativeInteger(value.control_revision)) {
    throw new GateVerificationError(
      "CONTROLLER_RELEASE_INVALID",
      "controller_release",
      "Cloud did not confirm controller lease release.",
    );
  }
}

class JsonRpcSocket {
  constructor(websocket) {
    this.websocket = websocket;
    this.nextId = 1;
    this.frames = [];
    this.waiters = [];
    this.pending = new Map();
    this.closed = false;
    this.closePromise = new Promise((resolve) => { this.resolveClosed = resolve; });
    websocket.addEventListener("message", (event) => this.onMessage(event));
    websocket.addEventListener("close", () => this.onClose());
    websocket.addEventListener("error", () => this.onClose());
  }

  static async connect(url, protocol, deadline) {
    let websocket;
    try {
      websocket = new WebSocket(url, protocol);
    } catch {
      throw websocketUnavailable();
    }
    await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(timeoutFailure("websocket_connect")), remaining(deadline));
      websocket.addEventListener("open", () => {
        clearTimeout(timeout);
        if (websocket.protocol !== protocol) reject(websocketUnavailable());
        else resolve();
      }, { once: true });
      websocket.addEventListener("error", () => {
        clearTimeout(timeout);
        reject(websocketUnavailable());
      }, { once: true });
    });
    return new JsonRpcSocket(websocket);
  }

  call(method, params, deadline) {
    if (this.closed) return Promise.reject(websocketUnavailable());
    const id = this.nextId++;
    const promise = new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        this.pending.delete(id);
        reject(timeoutFailure("control_rpc"));
      }, remaining(deadline));
      this.pending.set(id, {
        resolve: (value) => {
          clearTimeout(timeout);
          resolve(value);
        },
        reject: (error) => {
          clearTimeout(timeout);
          reject(error);
        },
      });
    });
    this.websocket.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
    return promise;
  }

  nextFrame(deadline, code, step) {
    if (this.frames.length > 0) return Promise.resolve(this.frames.shift());
    if (this.closed) return Promise.reject(websocketUnavailable());
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        const index = this.waiters.findIndex((waiter) => waiter.resolve === resolve);
        if (index >= 0) this.waiters.splice(index, 1);
        reject(new GateVerificationError(code, step, "The expected Cloud realtime frame was not observed before the deadline."));
      }, remaining(deadline));
      this.waiters.push({
        resolve: (value) => {
          clearTimeout(timeout);
          resolve(value);
        },
        reject: (error) => {
          clearTimeout(timeout);
          reject(error);
        },
      });
    });
  }

  async close() {
    if (this.closed) return;
    try {
      this.websocket.close(1000, "gate_close");
    } catch {
      this.onClose();
      return;
    }
    await Promise.race([
      this.closePromise,
      new Promise((resolve) => setTimeout(resolve, 1_000)),
    ]);
  }

  onMessage(event) {
    if (typeof event.data !== "string" || Buffer.byteLength(event.data, "utf8") > MAX_WS_FRAME_BYTES) {
      this.fail(invalidObserverEvent());
      return;
    }
    let value;
    try {
      value = JSON.parse(event.data);
    } catch {
      this.fail(invalidObserverEvent());
      return;
    }
    if (isRecord(value) && positiveInteger(value.id) && ("result" in value || "error" in value)) {
      const pending = this.pending.get(value.id);
      if (pending === undefined) {
        this.fail(websocketUnavailable());
        return;
      }
      this.pending.delete(value.id);
      if (exactObject(value, ["jsonrpc", "id", "result"]) && value.jsonrpc === "2.0" && isRecord(value.result)) {
        pending.resolve(value.result);
      } else {
        pending.reject(new GateVerificationError(
          "CONTROL_RPC_REJECTED",
          "control_rpc",
          "Cloud rejected a required controller operation.",
        ));
      }
      return;
    }
    const waiter = this.waiters.shift();
    if (waiter === undefined) this.frames.push(value);
    else waiter.resolve(value);
  }

  onClose() {
    if (this.closed) return;
    this.closed = true;
    const error = websocketUnavailable();
    for (const pending of this.pending.values()) pending.reject(error);
    this.pending.clear();
    for (const waiter of this.waiters) waiter.reject(error);
    this.waiters.length = 0;
    this.resolveClosed();
  }

  fail(error) {
    for (const pending of this.pending.values()) pending.reject(error);
    this.pending.clear();
    for (const waiter of this.waiters) waiter.reject(error);
    this.waiters.length = 0;
    try {
      this.websocket.close(1002, "invalid_frame");
    } catch {
      this.onClose();
    }
  }
}

async function requestJson(url, init, deadline, step, code) {
  let response;
  try {
    response = await fetch(url, { ...init, signal: AbortSignal.timeout(remaining(deadline)) });
  } catch {
    throw new GateVerificationError(code, step, "The required Cloud HTTP operation failed closed.");
  }
  if (!response.ok) {
    await response.body?.cancel().catch(() => undefined);
    throw new GateVerificationError(code, step, "The required Cloud HTTP operation was rejected.");
  }
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.toLowerCase().includes("application/json")) {
    await response.body?.cancel().catch(() => undefined);
    throw new GateVerificationError(code, step, "Cloud returned a non-JSON protocol response.");
  }
  try {
    return JSON.parse(await boundedResponseText(response));
  } catch (error) {
    if (error instanceof GateVerificationError) throw error;
    throw new GateVerificationError(code, step, "Cloud returned an invalid JSON protocol response.");
  }
}

async function boundedResponseText(response) {
  const declared = response.headers.get("content-length");
  if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > MAX_HTTP_BYTES)) {
    await response.body?.cancel().catch(() => undefined);
    throw new Error("bounded response rejected");
  }
  if (response.body === null) throw new Error("empty response");
  const reader = response.body.getReader();
  const chunks = [];
  let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > MAX_HTTP_BYTES) {
      await reader.cancel().catch(() => undefined);
      throw new Error("bounded response rejected");
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
}

async function materializeInput(input, maximumBytes, validator) {
  if (input.kind === "inline") return validator(input.value);
  let handle;
  try {
    const initial = await lstat(input.value);
    if (!initial.isFile() || initial.isSymbolicLink()) throw new Error();
    handle = await open(input.value, filesystemConstants.O_RDONLY | filesystemConstants.O_NOFOLLOW);
    const stat = await handle.stat();
    if (
      !stat.isFile()
      || stat.size < 1
      || stat.size > maximumBytes + 2
      || (stat.mode & 0o077) !== 0
      || (typeof process.getuid === "function" && stat.uid !== process.getuid())
    ) throw new Error();
    const bytes = await handle.readFile();
    if (bytes.byteLength > maximumBytes + 2) throw new Error();
    const value = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    return validator(stripTerminalLineEnding(value));
  } catch {
    throw new GateConfigurationError("A secret-bearing input file is not an owner-private regular file.");
  } finally {
    await handle?.close().catch(() => undefined);
  }
}

function parseCloudUrl(value, allowInsecureLoopback) {
  if (typeof value !== "string") throw new GateConfigurationError();
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new GateConfigurationError();
  }
  const insecureAllowed = allowInsecureLoopback && url.protocol === "http:" && isLoopback(url.hostname);
  if (
    (url.protocol !== "https:" && !insecureAllowed)
    || url.username !== ""
    || url.password !== ""
    || url.search !== ""
    || url.hash !== ""
  ) throw new GateConfigurationError();
  if (!url.pathname.endsWith("/")) url.pathname += "/";
  return url;
}

function endpoint(base, relativePath) {
  return new URL(relativePath.replace(/^\/+/, ""), base);
}

function ticketWebSocketUrl(base, ticket) {
  const url = endpoint(base, "api/ws");
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.searchParams.set("ticket", ticket);
  return url.toString();
}

function exclusiveInput(values, inlineFlag, fileFlag) {
  const inline = values.get(inlineFlag);
  const file = values.get(fileFlag);
  if ((inline === undefined) === (file === undefined)) throw new GateConfigurationError();
  return inline === undefined ? { kind: "file", value: file } : { kind: "inline", value: inline };
}

function canonicalUuid(value) {
  if (typeof value !== "string" || !UUID_PATTERN.test(value)) throw new GateConfigurationError();
  return value;
}

function canonicalCatalogUuid(value) {
  if (typeof value !== "string" || !UUID_PATTERN.test(value)) throw invalidCatalog();
  return value;
}

function validateAccessToken(value) {
  if (
    typeof value !== "string"
    || Buffer.byteLength(value, "utf8") < 1
    || Buffer.byteLength(value, "utf8") > MAX_TOKEN_BYTES
    || /\s|\0/u.test(value)
  ) throw new GateConfigurationError();
  return value;
}

function validatePrompt(value) {
  if (
    typeof value !== "string"
    || value.trim().length === 0
    || Buffer.byteLength(value, "utf8") > MAX_PROMPT_BYTES
    || value.includes("\0")
  ) throw new GateConfigurationError();
  return value;
}

function stripTerminalLineEnding(value) {
  return value.endsWith("\r\n") ? value.slice(0, -2) : value.endsWith("\n") ? value.slice(0, -1) : value;
}

function isObserverReady(value) {
  return exactObject(value, ["jsonrpc", "method", "params"])
    && value.jsonrpc === "2.0"
    && value.method === "event"
    && exactObject(value.params, ["type", "payload"])
    && value.params.type === "gateway.ready"
    && exactObject(value.params.payload, ["observer_contract", "connection_role"])
    && value.params.payload.observer_contract === 2
    && value.params.payload.connection_role === "observer";
}

function parseTranscriptMessage(value) {
  if (!isRecord(value)) throw invalidObserverSubscription();
  const keys = ["role"];
  if ("content" in value) keys.push("content");
  if (
    !exactObject(value, keys)
    || typeof value.role !== "string"
    || value.role.length === 0
    || value.role.length > 64
    || ("content" in value && value.content !== null && typeof value.content !== "string")
  ) throw invalidObserverSubscription();
  return { role: value.role, content: "content" in value ? value.content : null };
}

function validateTodoSnapshot(value) {
  const allowed = ["turn_id", "section_id", "revision", "first_event_sequence", "status", "items"];
  if (!exactObject(value, allowed) || !validTodoCore(value) || !validTodoItems(value.items)) {
    throw invalidObserverSubscription();
  }
  return value;
}

function validateToolSnapshot(value) {
  const keys = ["turn_id", "tool_call_id", "revision", "first_event_sequence", "status", "name"];
  if (isRecord(value) && "call_label" in value) keys.push("call_label");
  if (isRecord(value) && "summary" in value) keys.push("summary");
  if (
    !exactObject(value, keys)
    || !validLifecycleCore(value, "tool_call_id")
    || !boundedProtocolText(value.name, 1, 160)
    || ("call_label" in value && !boundedProtocolText(value.call_label, 1, 4_096))
    || ("summary" in value && !boundedProtocolText(value.summary, 0, 4_096))
  ) throw invalidObserverSubscription();
  return value;
}

function validSubagentSnapshot(value) {
  if (!isRecord(value)) return false;
  const required = [
    "turn_id", "subagent_id", "revision", "first_event_sequence", "parent_subagent_id", "name", "goal", "summary", "status",
  ];
  return required.every((key) => key in value)
    && validIdentifier(value.turn_id)
    && validIdentifier(value.subagent_id)
    && positiveInteger(value.revision)
    && positiveInteger(value.first_event_sequence)
    && (value.parent_subagent_id === null || validIdentifier(value.parent_subagent_id))
    && boundedProtocolText(value.name, 1, 160)
    && boundedProtocolText(value.goal, 0, 4_096)
    && (value.summary === null || boundedProtocolText(value.summary, 0, 4_096))
    && new Set(["queued", "waiting", "running", "completed", "failed", "interrupted"]).has(value.status);
}

function validTerminalSnapshot(value) {
  return isRecord(value)
    && validLifecycleCore(value, "process_id")
    && (!("summary" in value) || boundedProtocolText(value.summary, 0, 4_096))
    && (!("exit_code" in value) || (Number.isSafeInteger(value.exit_code) && value.exit_code >= -2_147_483_648 && value.exit_code <= 2_147_483_647));
}

function validObserverPayload(type, payload) {
  if (type === "message.start") {
    const keys = [];
    if ("message_id" in payload) keys.push("message_id");
    if ("role" in payload) keys.push("role");
    return exactObject(payload, keys)
      && (!("message_id" in payload) || validIdentifier(payload.message_id))
      && (!("role" in payload) || payload.role === "assistant");
  }
  if (type === "message.delta" || type === "reasoning.delta" || type === "thinking.delta") {
    return exactObject(payload, ["text"]) && boundedProtocolText(payload.text, 0, 131_072);
  }
  if (type === "message.complete") {
    const keys = ["status"];
    if ("error" in payload) keys.push("error");
    if ("text" in payload) keys.push("text");
    return exactObject(payload, keys)
      && (payload.status === "complete" || payload.status === "error")
      && (!("error" in payload) || payload.error === null || boundedProtocolText(payload.error, 0, 4_096))
      && (!("text" in payload) || boundedProtocolText(payload.text, 0, 131_072));
  }
  if (type === "status.update") {
    const keys = ["status", "running"];
    if ("text" in payload) keys.push("text");
    return exactObject(payload, keys)
      && boundedProtocolText(payload.status, 1, 64)
      && typeof payload.running === "boolean"
      && (!("text" in payload) || boundedProtocolText(payload.text, 0, 131_072));
  }
  if (type === "tool.output.delta") return validToolOutput(payload);
  if (type === "agent.terminal.output") return validTerminalOutput(payload);
  if (type === "todo.update") return validTodoUpdate(payload);
  if (type === "tool.update") return validToolUpdate(payload);
  if (type === "subagent.update" || type === "terminal.update") {
    return validGenericLifecycleUpdate(payload, type === "subagent.update" ? "subagent_id" : "process_id");
  }
  return false;
}

function validTodoUpdate(value) {
  if (!isRecord(value) || (value.operation !== "upsert" && value.operation !== "delete")) return false;
  if (value.operation === "delete") {
    return exactObject(value, ["turn_id", "section_id", "revision", "first_event_sequence", "operation"])
      && validTodoIdentity(value);
  }
  return exactObject(value, [
    "turn_id", "section_id", "revision", "first_event_sequence", "operation", "status", "items",
  ]) && validTodoCore(value) && validTodoItems(value.items);
}

function validTodoCore(value) {
  return validTodoIdentity(value) && TODO_STATUSES.has(value.status);
}

function validTodoIdentity(value) {
  return validIdentifier(value.turn_id)
    && validIdentifier(value.section_id)
    && positiveInteger(value.revision)
    && positiveInteger(value.first_event_sequence);
}

function validTodoItems(value) {
  return Array.isArray(value)
    && value.length >= 1
    && value.length <= 256
    && value.every((item) => exactObject(item, ["id", "label", "status"])
      && validIdentifier(item.id)
      && boundedProtocolText(item.label, 1, 4_096)
      && TODO_STATUSES.has(item.status));
}

function validToolUpdate(value) {
  if (!isRecord(value) || (value.operation !== "upsert" && value.operation !== "delete")) return false;
  if (value.operation === "delete") {
    return exactObject(value, ["turn_id", "tool_call_id", "revision", "first_event_sequence", "operation"])
      && validLifecycleIdentity(value, "tool_call_id");
  }
  const keys = ["turn_id", "tool_call_id", "revision", "first_event_sequence", "operation", "status", "name"];
  if ("call_label" in value) keys.push("call_label");
  if ("duration_ms" in value) keys.push("duration_ms");
  if ("summary" in value) keys.push("summary");
  return exactObject(value, keys)
    && validLifecycleCore(value, "tool_call_id")
    && boundedProtocolText(value.name, 1, 160)
    && (!("call_label" in value) || boundedProtocolText(value.call_label, 1, 4_096))
    && (!("duration_ms" in value) || nonNegativeInteger(value.duration_ms))
    && (!("summary" in value) || boundedProtocolText(value.summary, 0, 4_096));
}

function validGenericLifecycleUpdate(value, identityKey) {
  if (!isRecord(value) || (value.operation !== "upsert" && value.operation !== "delete")) return false;
  return validLifecycleIdentity(value, identityKey);
}

function validLifecycleCore(value, identityKey) {
  return validLifecycleIdentity(value, identityKey) && LIFECYCLE_STATUSES.has(value.status);
}

function validLifecycleIdentity(value, identityKey) {
  return validIdentifier(value.turn_id)
    && validIdentifier(value[identityKey])
    && positiveInteger(value.revision)
    && positiveInteger(value.first_event_sequence);
}

function validToolOutput(value) {
  if (!isRecord(value)) return false;
  const keys = ["turn_id", "tool_call_id", "text"];
  if ("tool_name" in value) keys.push("tool_name");
  if ("sequence" in value) keys.push("sequence");
  return exactObject(value, keys)
    && validIdentifier(value.turn_id)
    && validIdentifier(value.tool_call_id)
    && boundedProtocolText(value.text, 0, 131_072)
    && (!("tool_name" in value) || boundedProtocolText(value.tool_name, 1, 256))
    && (!("sequence" in value) || nonNegativeInteger(value.sequence));
}

function validTerminalOutput(value) {
  return exactObject(value, ["turn_id", "process_id", "stream", "text"])
    && validIdentifier(value.turn_id)
    && validIdentifier(value.process_id)
    && (value.stream === "stdout" || value.stream === "stderr")
    && boundedProtocolText(value.text, 0, 131_072);
}

function validIdentifier(value) {
  return boundedProtocolText(value, 1, 256);
}

function boundedProtocolText(value, minimum, maximum) {
  return typeof value === "string"
    && [...value].length >= minimum
    && [...value].length <= maximum
    && !containsControl(value, true);
}

function validInflight(value) {
  return exactObject(value, ["user", "assistant", "streaming", "error"])
    && [value.user, value.assistant, value.error].every((item) => item === null || typeof item === "string")
    && typeof value.streaming === "boolean";
}

function statusMatchesLease(status, lease) {
  return status.controllerKind === "mobile"
    && status.controllerLabel === lease.controllerLabel
    && status.controlRevision === lease.controlRevision
    && status.leaseExpiresAtEpochMs === lease.expiresAtEpochMs;
}

function createEvidence() {
  return { todo: false, tool: false, pendingInput: false, approval: false };
}

function collectSnapshotEvidence(subscription, baselineSequence, evidence) {
  if (subscription.todoSections.some((item) => isRecord(item) && item.first_event_sequence > baselineSequence)) evidence.todo = true;
  if (subscription.tools.some((item) => isRecord(item) && item.first_event_sequence > baselineSequence)) evidence.tool = true;
}

function collectPendingEvidence(pendingInput, evidence) {
  if (pendingInput === null) return;
  evidence.pendingInput = true;
  if (pendingInput.kind === "approval") evidence.approval = true;
}

function enforceRequiredEvidence(required, evidence) {
  const observed = {
    todo: evidence.todo,
    tool: evidence.tool,
    pending_input: evidence.pendingInput,
    approval: evidence.approval,
  };
  if ([...required].some((name) => observed[name] !== true)) {
    throw new GateVerificationError(
      "REQUIRED_EVIDENCE_NOT_OBSERVED",
      "independent_evidence",
      "A required independent evidence class was not observed in this explicit scene.",
    );
  }
}

function evidenceReceipt(evidence) {
  return {
    todo: evidence.todo ? "confirmed" : "independent_gate_required",
    tool: evidence.tool ? "confirmed" : "independent_gate_required",
    pending_input: evidence.pendingInput ? "confirmed" : "independent_gate_required",
    approval: evidence.approval ? "confirmed" : "independent_gate_required",
  };
}

function rejectForbiddenCatalogIdentity(value) {
  if (FORBIDDEN_CATALOG_IDENTITY.test(value)) {
    throw new GateVerificationError(
      "FORBIDDEN_CATALOG_TARGET",
      "catalog",
      "The explicit catalog target is a forbidden demo, test, or fixture identity.",
    );
  }
}

function safeText(value, minimum, maximum) {
  if (
    typeof value !== "string"
    || [...value].length < minimum
    || [...value].length > maximum
    || containsControl(value, true)
  ) {
    throw invalidCatalog();
  }
  return value;
}

function safeControllerLabel(value) {
  return typeof value === "string" && value.length > 0 && value.length <= 160 && !containsControl(value, false);
}

function containsControl(value, allowLayoutWhitespace) {
  return [...value].some((character) => {
    const codePoint = character.codePointAt(0);
    if (codePoint === 0x7f) return true;
    if (codePoint === undefined || codePoint >= 0x20) return false;
    return !allowLayoutWhitespace || (codePoint !== 0x09 && codePoint !== 0x0a && codePoint !== 0x0d);
  });
}

function validIsoTimestamp(value) {
  return typeof value === "string" && value.length <= 64 && !Number.isNaN(Date.parse(value));
}

function boundedInteger(value, minimum, maximum) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) throw invalidCatalog();
  return value;
}

function positiveInteger(value) {
  return Number.isSafeInteger(value) && value > 0;
}

function nonNegativeInteger(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function isRecord(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function exactObject(value, keys) {
  if (!isRecord(value)) return false;
  const actual = Object.keys(value);
  return actual.length === keys.length && actual.every((key) => keys.includes(key));
}

function remaining(deadline) {
  const value = deadline - Date.now();
  if (value <= 0) throw timeoutFailure("gate");
  return value;
}

function isLoopback(hostname) {
  return hostname === "127.0.0.1" || hostname === "[::1]" || hostname === "::1";
}

function invalidCatalog() {
  return new GateVerificationError("CATALOG_RESPONSE_INVALID", "catalog", "Cloud returned an invalid Agent or session catalog.");
}

function invalidObserverSubscription() {
  return new GateVerificationError("OBSERVER_SUBSCRIPTION_INVALID", "observer_subscribe", "Cloud returned an invalid observer-v2 subscription.");
}

function invalidObserverEvent() {
  return new GateVerificationError("OBSERVER_EVENT_INVALID", "observer_stream", "Cloud returned an invalid observer-v2 event.");
}

function invalidAssistantOrder() {
  return new GateVerificationError("ASSISTANT_STREAM_ORDER_INVALID", "assistant_stream", "Assistant streaming and terminal events were not ordered.");
}

function invalidControlReady() {
  return new GateVerificationError("CONTROL_READY_INVALID", "control_ready", "Cloud returned an invalid control-v1 ready contract.");
}

function invalidControllerReceipt(step) {
  return new GateVerificationError("CONTROLLER_RECEIPT_INVALID", step, "Cloud returned an invalid controller lease receipt.");
}

function reconnectDiscontinuity() {
  return new GateVerificationError("RECONNECT_SEQUENCE_DISCONTINUITY", "observer_reconnect", "Observer reconnect did not preserve sequence continuity.");
}

function websocketUnavailable() {
  return new GateVerificationError("WEBSOCKET_UNAVAILABLE", "realtime", "A required Cloud WebSocket connection failed closed.");
}

function timeoutFailure(step) {
  return new GateVerificationError("GATE_TIMEOUT", step, "The real full-chain gate deadline expired.");
}

function helpText() {
  return [
    "Usage: node scripts/real-full-chain-gate.mjs",
    "  --cloud-url HTTPS_URL",
    "  (--access-token TOKEN | --access-token-file PRIVATE_FILE)",
    "  --agent-id UUID --session-id UUID",
    "  (--prompt TEXT | --prompt-file PRIVATE_FILE)",
    "  [--timeout-ms 5000..300000]",
    "  [--require-evidence todo,tool,pending_input,approval]",
  ].join("\n");
}

async function main() {
  if (process.argv.length === 3 && (process.argv[2] === "--help" || process.argv[2] === "-h")) {
    console.log(helpText());
    return;
  }
  try {
    const receipt = await runRealFullChainGate(process.argv.slice(2));
    console.log(JSON.stringify(receipt));
  } catch (error) {
    const known = error instanceof GateConfigurationError || error instanceof GateVerificationError;
    const failure = known
      ? error
      : new GateVerificationError("GATE_EXECUTION_FAILED", "gate", "The real full-chain gate failed closed.");
    console.error(JSON.stringify({
      schema_version: 1,
      gate: "hermes-real-full-chain",
      status: "failed",
      step: failure.step,
      code: failure.code,
      message: failure.message,
    }));
    process.exitCode = error instanceof GateConfigurationError ? 2 : 3;
  }
}

if (process.argv[1] !== undefined && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main();
}
