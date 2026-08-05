import { isDisplaySafeHermesText } from "../../../shared/contracts/cloudRealtimeV2";
import {
  BoundedJsonResponseAborted,
  BoundedJsonResponseTooLarge,
  readBoundedJsonResponse,
} from "../http/boundedJsonResponse";

const MAX_RESPONSE_BYTES = 256 * 1024;
const MAX_AGENTS = 256;
const MAX_SESSIONS = 1_000;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const CONTROL_ACTIONS = new Set([
  "approval.respond",
  "clarify.respond",
  "prompt.submit",
  "session.interrupt",
  "session.steer",
]);

export type SessionCatalogAgentStatus = "active" | "disabled" | "offline";

export interface SessionCatalogAgent {
  agentId: string;
  workspaceId: string;
  agentKey: string;
  status: SessionCatalogAgentStatus;
  lastSeenAt: string | null;
}

export interface SessionCatalogItem {
  id: string;
  agentId: string;
  workspaceId: string | null;
  sessionKey: string;
  profile: string;
  title: string | null;
  lastActive: number | null;
  messageCount: number;
  isActive: boolean;
  directorySource: "host_catalog" | "transcript_projection";
  availability: "live" | "offline";
  runtimeGeneration: string | null;
  surface: string | null;
  authorityRevision: number | null;
  availableActions: readonly string[];
  transcriptAvailable: boolean;
}

export interface SessionCatalogPage {
  sessions: SessionCatalogItem[];
  total: number;
  limit: number;
  offset: number;
  profile: string | null;
}

export interface SessionCatalogRequest {
  agentId: string;
  profile: string | null;
  limit: number;
  offset: number;
}

export interface SessionCatalogClient {
  listAgents(signal?: AbortSignal): Promise<SessionCatalogAgent[]>;
  listSessions(request: SessionCatalogRequest, signal?: AbortSignal): Promise<SessionCatalogPage>;
}

export class SessionCatalogAuthenticationRequired extends Error {
  constructor() {
    super("Hermes session directory authentication is required");
    this.name = "SessionCatalogAuthenticationRequired";
  }
}

export class SessionCatalogScopeAmbiguous extends Error {
  constructor(message = "Hermes session directory scope is ambiguous") {
    super(message);
    this.name = "SessionCatalogScopeAmbiguous";
  }
}

export class SessionCatalogRequestAborted extends Error {
  constructor() {
    super("Hermes session directory request was cancelled");
    this.name = "SessionCatalogRequestAborted";
  }
}

interface BrowserSessionCatalogClientOptions {
  agentsEndpoint: string;
  sessionsEndpoint: string;
  fetcher?: typeof fetch;
}

export class BrowserSessionCatalogClient implements SessionCatalogClient {
  private readonly agentsEndpoint: string;
  private readonly sessionsEndpoint: string;
  private readonly fetcher: typeof fetch;

  constructor({
    agentsEndpoint,
    sessionsEndpoint,
    fetcher = (input, init) => fetch(input, init),
  }: BrowserSessionCatalogClientOptions) {
    this.agentsEndpoint = agentsEndpoint;
    this.sessionsEndpoint = sessionsEndpoint;
    this.fetcher = fetcher;
  }

  async listAgents(signal?: AbortSignal): Promise<SessionCatalogAgent[]> {
    const payload = await this.requestJson(this.agentsEndpoint, "agents", signal);
    try {
      const root = exactObject(payload, ["agents"]);
      if (!Array.isArray(root.agents) || root.agents.length > MAX_AGENTS) throw new Error();
      const seen = new Set<string>();
      return root.agents.map((raw) => {
        const agent = exactObject(raw, ["agent_id", "workspace_id", "agent_key", "status", "last_seen_at"]);
        const agentId = strictUuid(agent.agent_id);
        if (seen.has(agentId)) throw new Error();
        seen.add(agentId);
        return {
          agentId,
          workspaceId: strictUuid(agent.workspace_id),
          agentKey: safeText(agent.agent_key, 1, 128),
          status: agentStatus(agent.status),
          lastSeenAt: nullableIsoTimestamp(agent.last_seen_at),
        };
      });
    } catch {
      throw new Error("Hermes returned an invalid Agent directory");
    }
  }

  async listSessions(request: SessionCatalogRequest, signal?: AbortSignal): Promise<SessionCatalogPage> {
    if (!UUID_PATTERN.test(request.agentId) || !isBoundedInteger(request.limit, 1, 50) || !isBoundedInteger(request.offset, 0, MAX_SESSIONS)) {
      throw new Error("Hermes returned an invalid session directory");
    }
    if (request.profile !== null) safeText(request.profile, 1, 128);
    const endpoint = appendQuery(appendAgentSessionsPath(
      this.sessionsEndpoint,
      request.agentId,
    ), [
      ...(request.profile === null ? [] : [["profile", request.profile]] as Array<[string, string]>),
      ["min_messages", "0"],
      ["archived", "exclude"],
      ["order", "recent"],
      ["limit", String(request.limit)],
      ["offset", String(request.offset)],
    ]);
    const payload = await this.requestJson(endpoint, "sessions", signal);
    try {
      const root = exactObject(payload, ["sessions", "total", "limit", "offset"]);
      if (!Array.isArray(root.sessions) || root.sessions.length > request.limit) throw new Error();
      const total = safeInteger(root.total, 0, MAX_SESSIONS);
      const limit = safeInteger(root.limit, 1, 50);
      const offset = safeInteger(root.offset, 0, MAX_SESSIONS);
      if (limit !== request.limit || offset !== request.offset || offset + root.sessions.length > total) throw new Error();

      const seenIds = new Set<string>();
      const seenKeys = new Set<string>();
      const sessions = root.sessions.map((raw) => {
        const session = exactObject(raw, [
          "id", "agent_id", "workspace_id", "_lineage_root_id", "parent_session_id", "title", "preview",
          "source", "model", "profile", "cwd", "git_branch", "started_at", "ended_at", "last_active",
          "message_count", "tool_call_count", "input_tokens", "output_tokens", "is_active", "archived",
          "directory_source", "availability", "runtime_generation", "surface", "authority_revision",
          "available_actions", "transcript_available",
        ]);
        const id = strictUuid(session.id);
        const agentId = strictUuid(session.agent_id);
        const sessionKey = safeText(session._lineage_root_id, 1, 256);
        const profile = safeText(session.profile, 1, 128);
        if (agentId !== request.agentId || (request.profile !== null && profile !== request.profile)) throw new Error();
        if (seenIds.has(id) || seenKeys.has(sessionKey)) throw new Error();
        seenIds.add(id);
        seenKeys.add(sessionKey);
        if (session.parent_session_id !== null || session.preview !== null || session.source !== null || session.model !== null || session.cwd !== null || session.git_branch !== null) throw new Error();
        if (session.directory_source !== "host_catalog" && session.directory_source !== "transcript_projection") throw new Error();
        if (session.availability !== "live" && session.availability !== "offline") throw new Error();
        const directorySource: SessionCatalogItem["directorySource"] = session.directory_source;
        const availability: SessionCatalogItem["availability"] = session.availability;
        const workspaceId = session.workspace_id === null ? null : strictUuid(session.workspace_id);
        const title = session.title === null ? null : safeText(session.title, 1, 512);
        const lastActive = session.last_active === null ? null : finiteNumber(session.last_active, 0);
        if (session.started_at !== null) finiteNumber(session.started_at, 0);
        if (session.ended_at !== null) finiteNumber(session.ended_at, 0);
        safeInteger(session.tool_call_count, 0, Number.MAX_SAFE_INTEGER);
        safeInteger(session.input_tokens, 0, Number.MAX_SAFE_INTEGER);
        safeInteger(session.output_tokens, 0, Number.MAX_SAFE_INTEGER);
        if (typeof session.is_active !== "boolean" || session.archived !== false) throw new Error();
        const runtimeGeneration = session.runtime_generation === null
          ? null
          : safeText(session.runtime_generation, 1, 128);
        const surface = session.surface === null ? null : safeText(session.surface, 1, 64);
        const authorityRevision = session.authority_revision === null
          ? null
          : safeInteger(session.authority_revision, 1, Number.MAX_SAFE_INTEGER);
        const availableActions = controlActions(session.available_actions);
        if (typeof session.transcript_available !== "boolean") throw new Error();
        if (directorySource === "host_catalog") {
          if (
            workspaceId !== null
            || title !== null
            || session.started_at !== null
            || lastActive !== null
            || session.ended_at !== null
            || session.message_count !== 0
            || session.tool_call_count !== 0
            || session.input_tokens !== 0
            || session.output_tokens !== 0
            || runtimeGeneration === null
            || surface === null
            || authorityRevision === null
            || session.transcript_available !== false
          ) throw new Error();
        } else if (
          runtimeGeneration !== null
          || surface !== null
          || authorityRevision !== null
          || availableActions.length !== 0
          || session.transcript_available !== true
        ) throw new Error();
        return {
          id,
          agentId,
          workspaceId,
          sessionKey,
          profile,
          title,
          lastActive,
          messageCount: safeInteger(session.message_count, 0, Number.MAX_SAFE_INTEGER),
          isActive: session.is_active,
          directorySource,
          availability,
          runtimeGeneration,
          surface,
          authorityRevision,
          availableActions,
          transcriptAvailable: session.transcript_available,
        };
      });

      const profiles = new Set(sessions.map((session) => session.profile));
      if (request.profile === null && profiles.size > 1) {
        throw new SessionCatalogScopeAmbiguous("Hermes session profile is ambiguous");
      }
      return {
        sessions,
        total,
        limit,
        offset,
        profile: request.profile ?? sessions[0]?.profile ?? null,
      };
    } catch (error) {
      if (error instanceof SessionCatalogScopeAmbiguous) throw error;
      throw new Error("Hermes returned an invalid session directory", { cause: error });
    }
  }

  private async requestJson(
    endpoint: string,
    scope: "agents" | "sessions",
    signal?: AbortSignal,
  ): Promise<unknown> {
    if (signal?.aborted) throw new SessionCatalogRequestAborted();
    let response: Response;
    try {
      response = await this.fetcher(endpoint, {
        method: "GET",
        credentials: "include",
        headers: { Accept: "application/json" },
        ...(signal === undefined ? {} : { signal }),
      });
    } catch (error) {
      if (signal?.aborted) throw new SessionCatalogRequestAborted();
      throw new Error("Hermes session directory is unavailable", { cause: error });
    }
    if (response.status === 401) throw new SessionCatalogAuthenticationRequired();
    if (scope === "sessions" && response.status === 409) throw new SessionCatalogScopeAmbiguous();
    if (!response.ok) throw new Error("Hermes session directory is unavailable");
    try {
      return await readBoundedJsonResponse(response, {
        maximumBytes: MAX_RESPONSE_BYTES,
        ...(signal === undefined ? {} : { signal }),
      });
    } catch (error) {
      if (error instanceof BoundedJsonResponseAborted || signal?.aborted) {
        throw new SessionCatalogRequestAborted();
      }
      if (error instanceof BoundedJsonResponseTooLarge) {
        throw new Error("Hermes returned an oversized session directory", { cause: error });
      }
      throw new Error("Hermes returned an invalid session directory", { cause: error });
    }
  }
}

function appendQuery(endpoint: string, entries: Array<[string, string]>): string {
  const absolute = /^[a-z][a-z\d+.-]*:\/\//i.test(endpoint);
  const url = new URL(endpoint, "https://hermes.invalid");
  for (const [key, value] of entries) url.searchParams.append(key, value);
  return absolute ? url.toString() : `${url.pathname}${url.search}`;
}

function appendAgentSessionsPath(endpoint: string, agentId: string): string {
  const absolute = /^[a-z][a-z\d+.-]*:\/\//i.test(endpoint);
  const url = new URL(endpoint, "https://hermes.invalid");
  const basePath = url.pathname.replace(/\/+$/, "");
  url.pathname = `${basePath}/${encodeURIComponent(agentId)}/sessions`;
  return absolute ? url.toString() : `${url.pathname}${url.search}`;
}

function exactObject(value: unknown, keys: readonly string[]): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new Error();
  const object = value as Record<string, unknown>;
  const actual = Object.keys(object).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) throw new Error();
  return object;
}

function strictUuid(value: unknown): string {
  if (typeof value !== "string" || !UUID_PATTERN.test(value)) throw new Error();
  return value;
}

function safeText(value: unknown, min: number, max: number): string {
  if (!isDisplaySafeHermesText(value, min, max)) throw new Error();
  return value;
}

function nullableIsoTimestamp(value: unknown): string | null {
  if (value === null) return null;
  if (typeof value !== "string" || value.length > 64 || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)$/.test(value) || !Number.isFinite(Date.parse(value))) throw new Error();
  return value;
}

function agentStatus(value: unknown): SessionCatalogAgentStatus {
  if (value !== "active" && value !== "disabled" && value !== "offline") throw new Error();
  return value;
}

function finiteNumber(value: unknown, min: number): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < min) throw new Error();
  return value;
}

function safeInteger(value: unknown, min: number, max: number): number {
  if (!isBoundedInteger(value, min, max)) throw new Error();
  return value;
}

function controlActions(value: unknown): string[] {
  if (!Array.isArray(value) || value.length > 5) throw new Error();
  const actions = value.map((action) => {
    if (typeof action !== "string" || !CONTROL_ACTIONS.has(action)) throw new Error();
    return action;
  });
  if (new Set(actions).size !== actions.length) throw new Error();
  return actions;
}

function isBoundedInteger(value: unknown, min: number, max: number): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= min && value <= max;
}
