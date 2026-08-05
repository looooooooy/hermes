import type { TicketProvider, TicketRequest } from "./CloudRealtimeAdapter";
import {
  BoundedJsonResponseAborted,
  readBoundedJsonResponse,
} from "../http/boundedJsonResponse";

const MAX_TICKET_RESPONSE_BYTES = 256 * 1024;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

interface HttpTicketProviderOptions {
  endpoint: string;
  clientInstanceId: string;
  fetcher?: typeof fetch;
}

export class CookieTicketAuthenticationUnavailable extends Error {
  constructor() {
    super("Cloud does not yet accept the secure browser session for WebSocket tickets");
    this.name = "CookieTicketAuthenticationUnavailable";
  }
}

export class HttpTicketProvider implements TicketProvider {
  constructor(private readonly options: HttpTicketProviderOptions) {}

  async mint(request: TicketRequest, signal?: AbortSignal): Promise<string> {
    if (!UUID_PATTERN.test(request.agentId) || !UUID_PATTERN.test(request.sessionId)) {
      throw new Error("Cloud ticket request is invalid");
    }
    const body = request.connectionRole === "observer"
      ? {
          connection_role: "observer",
          client_instance_id: this.options.clientInstanceId,
          agent_id: request.agentId,
          ...(request.observerContract === 2 ? { observer_contract: 2 } : {}),
        }
      : {
          connection_role: "control",
          client_instance_id: this.options.clientInstanceId,
          agent_id: request.agentId,
          session_id: request.sessionId,
        };
    const response = await (this.options.fetcher ?? fetch)(this.options.endpoint, {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
      ...(signal === undefined ? {} : { signal }),
    });
    if (response.status === 401) throw new CookieTicketAuthenticationUnavailable();
    if (!response.ok) throw new Error(`Cloud ticket request failed with status ${response.status}`);
    let value: unknown;
    try {
      value = await readBoundedJsonResponse(response, {
        maximumBytes: MAX_TICKET_RESPONSE_BYTES,
        ...(signal === undefined ? {} : { signal }),
      });
    } catch (error) {
      if (error instanceof BoundedJsonResponseAborted) throw error;
      throw new Error("Cloud ticket response is invalid", { cause: error });
    }
    const expectedKeys = request.connectionRole === "observer" && request.observerContract === 2
      ? ["ticket", "ttl_seconds", "connection_role", "observer_contract"]
      : ["ticket", "ttl_seconds", "connection_role"];
    if (
      !isRecord(value)
      || !hasExactKeys(value, expectedKeys)
      || typeof value.ticket !== "string"
      || value.ticket.length < 32
      || value.ticket.length > 4096
      || !Number.isSafeInteger(value.ttl_seconds)
      || (value.ttl_seconds as number) < 1
      || (value.ttl_seconds as number) > 60
      || value.connection_role !== request.connectionRole
      || (request.connectionRole === "observer" && request.observerContract === 2 && value.observer_contract !== 2)
    ) throw new Error("Cloud ticket response is invalid");
    return value.ticket;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value);
  return actual.length === keys.length && actual.every((key) => keys.includes(key));
}
