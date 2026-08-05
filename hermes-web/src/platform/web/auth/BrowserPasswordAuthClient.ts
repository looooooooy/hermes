import {
  BoundedJsonResponseAborted,
  readBoundedJsonResponse,
} from "../http/boundedJsonResponse";

const MAX_AUTH_RESPONSE_BYTES = 256 * 1024;

export interface PasswordLoginRequest {
  username: string;
  password: string;
}

export interface PasswordAuthClient {
  login(request: PasswordLoginRequest, signal?: AbortSignal): Promise<{ ok: true }>;
  logout(signal?: AbortSignal): Promise<{ ok: true }>;
}

interface BrowserPasswordAuthClientOptions {
  loginEndpoint: string;
  logoutEndpoint: string;
  fetcher?: typeof fetch;
}

export class BrowserPasswordAuthClient implements PasswordAuthClient {
  constructor(private readonly options: BrowserPasswordAuthClientOptions) {}

  async login(request: PasswordLoginRequest, signal?: AbortSignal): Promise<{ ok: true }> {
    if (request.username.trim().length === 0 || request.password.length === 0) {
      throw new Error("Username and password are required");
    }
    let response: Response;
    try {
      const fetcher = this.options.fetcher ?? ((input, init) => fetch(input, init));
      response = await fetcher(this.options.loginEndpoint, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider: "basic",
          username: request.username,
          password: request.password,
          next: "",
        }),
        ...(signal === undefined ? {} : { signal }),
      });
    } catch (error) {
      if (signal?.aborted) throw new BoundedJsonResponseAborted();
      throw new Error("Hermes sign-in service is unavailable", { cause: error });
    }
    if (!response.ok) throw new Error("Hermes sign-in was rejected");
    const value = await exactOkResponse(response, "login", signal);
    if (!isRecord(value) || Object.keys(value).length !== 1 || value.ok !== true) {
      throw new Error("Hermes returned an invalid login response");
    }
    return { ok: true };
  }

  async logout(signal?: AbortSignal): Promise<{ ok: true }> {
    let response: Response;
    try {
      const fetcher = this.options.fetcher ?? ((input, init) => fetch(input, init));
      response = await fetcher(this.options.logoutEndpoint, {
        method: "POST",
        credentials: "include",
        headers: { Accept: "application/json" },
        ...(signal === undefined ? {} : { signal }),
      });
    } catch (error) {
      if (signal?.aborted) throw new BoundedJsonResponseAborted();
      throw new Error("Hermes sign-out service is unavailable", { cause: error });
    }
    if (!response.ok) throw new Error("Hermes sign-out was rejected");
    const value = await exactOkResponse(response, "sign-out", signal);
    if (!isRecord(value) || Object.keys(value).length !== 1 || value.ok !== true) {
      throw new Error("Hermes returned an invalid sign-out response");
    }
    return { ok: true };
  }
}

async function exactOkResponse(
  response: Response,
  operation: "login" | "sign-out",
  signal?: AbortSignal,
): Promise<unknown> {
  try {
    return await readBoundedJsonResponse(response, {
      maximumBytes: MAX_AUTH_RESPONSE_BYTES,
      ...(signal === undefined ? {} : { signal }),
    });
  } catch (error) {
    if (error instanceof BoundedJsonResponseAborted) throw error;
    throw new Error(`Hermes returned an invalid ${operation} response`, { cause: error });
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
