import {
  CLOUD_OBSERVER_V2_EVENT_TYPES,
  decodeCloudRealtimeV2Frame,
  decodeObserverSubscriptionV2,
  isDisplaySafeHermesText,
} from "./cloudRealtimeV2";

export const STABLE_SESSION_ID = "88888888-8888-4888-8888-888888888888";

describe("cloud realtime observer v2 decoder", () => {
  it("provides one bounded well-formed display-text guard for public labels", () => {
    expect(isDisplaySafeHermesText("Hermes Desktop", 1, 160)).toBe(true);
    expect(isDisplaySafeHermesText("A".repeat(160), 1, 160)).toBe(true);
    expect(isDisplaySafeHermesText("A".repeat(161), 1, 160)).toBe(false);
    expect(isDisplaySafeHermesText(" password=hunter2 ", 1, 160)).toBe(false);
    expect(isDisplaySafeHermesText("Hermes\ud800Desktop", 1, 160)).toBe(false);
  });

  it("derives the twelve authoritative event types from the generated contract", () => {
    expect(CLOUD_OBSERVER_V2_EVENT_TYPES).toEqual([
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
  });

  it("accepts exact v2 ready and lifecycle events and rejects v1 or raw fields", () => {
    expect(decodeCloudRealtimeV2Frame(observerReadyV2()).ok).toBe(true);
    expect(decodeCloudRealtimeV2Frame({
      ...observerReadyV2(),
      params: { type: "gateway.ready", payload: { observer_contract: 1, connection_role: "observer" } },
    })).toEqual({ ok: false, reason: "invalid_frame" });

    for (const event of lifecycleEvents()) expect(decodeCloudRealtimeV2Frame(event).ok).toBe(true);
    const unsafe = lifecycleEvents()[2];
    unsafe.params.payload = { ...unsafe.params.payload, raw_args: { command: "must not cross" } };
    expect(decodeCloudRealtimeV2Frame(unsafe)).toEqual({ ok: false, reason: "invalid_frame" });

    const rangedLifecycle = lifecycleEvents()[0];
    (rangedLifecycle.params as Record<string, unknown>).event_sequence_start = 1;
    expect(decodeCloudRealtimeV2Frame(rangedLifecycle)).toEqual({ ok: false, reason: "invalid_frame" });
  });

  it.each([
    "client_secret",
    "api_token",
    "tool_args",
    "approval_payload",
  ])("rejects recursively nested private extension field %s", (privateField) => {
    const event = lifecycleEvents()[2];
    Object.assign(event.params, {
      extensions: {
        "com.example.display": {
          nested: [{ deeper: { [privateField]: "must-not-cross" } }],
        },
      },
    });

    expect(decodeCloudRealtimeV2Frame(event)).toEqual({ ok: false, reason: "invalid_frame" });
  });

  it.each([
    "Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature",
    "Authorization: Basic dXNlcjpwYXNzd29yZA==",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJl",
    "sk-secretcredential123456",
    "ASIAIOSFODNN7EXAMPLE",
    "AKIAIOSFODNN7EXAMPLE",
    "AIzaSyDexampleProviderCredential123456",
    "ghp_exampleProviderCredential123456",
    "xoxb-exampleProviderCredential123456",
    "hf_exampleProviderCredential123456",
    "glpat-exampleProviderCredential123456",
    "password=correct-horse-battery-staple",
    "api_key: abcdefghijklmnop",
    "token=provider-token-value",
  ])("rejects credential-like extension value %s", (privateValue) => {
    const event = lifecycleEvents()[2];
    Object.assign(event.params, {
      extensions: {
        "com.example.display": { label: privateValue },
      },
    });

    expect(decodeCloudRealtimeV2Frame(event)).toEqual({ ok: false, reason: "invalid_frame" });
  });

  it.each([
    "Authorization: Basic dXNlcjpwYXNzd29yZA==",
    "Basic dXNlcjpwYXNz",
    "password=correct-horse-battery-staple",
    "password=hunter2",
    "api_key: abcdefghijklmnop",
    "token=provider-token-value",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJl",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln",
    "AKIAIOSFODNN7EXAMPLE",
    "AIzaSyDexampleProviderCredential123456",
    "ghp_exampleProviderCredential123456",
  ])("rejects credential-like v2 event payload text %s", (privateValue) => {
    expect(decodeCloudRealtimeV2Frame(textEventV2(privateValue)))
      .toEqual({ ok: false, reason: "invalid_frame" });
  });

  it("rejects credential-like lifecycle summary before projection", () => {
    const event = lifecycleEvents()[2];
    event.params.payload = {
      ...event.params.payload,
      summary: "Authorization: Basic dXNlcjpwYXNzd29yZA==",
    };

    expect(decodeCloudRealtimeV2Frame(event)).toEqual({ ok: false, reason: "invalid_frame" });
  });

  it.each([
    "Basic authentication is disabled.",
    "Basic YWJjZA== is not a user-password credential.",
    "Basic OnBhc3M= has an empty user.",
    "Basic dXNlcjpwYXNz=== is malformed base64.",
    "a.b.c",
    "Version 1.2.3 is served by api.example.com.",
    "The tokenizer pathology notes are public.",
    "The API key rotation guide is ready for review.",
    "Token counts are aggregate only.",
    "Release marker 12345678.abcdefgh.ijklmnop is display metadata.",
    "AKIA is documented only as a provider prefix.",
  ])("allows benign display text without credential values: %s", (safeText) => {
    expect(decodeCloudRealtimeV2Frame(textEventV2(safeText)).ok).toBe(true);
  });

  it.each([
    "eyJhbGciOiJIUzI1NiJ9.bm90LWpzb24.c2lnbmF0dXJl",
    "eyJhbGciOiIifQ.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
    "eyJhbGciOiJIUzI1NiJ9.Imxvbmci.c2lnbmF0dXJl",
    "eyJhbGciOiJIUzI1NiJ9.W10.c2lnbmF0dXJl",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln.extra",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln=",
  ])("allows JWT-shaped text that lacks semantic JWT credentials: %s", (safeText) => {
    expect(decodeCloudRealtimeV2Frame(textEventV2(safeText)).ok).toBe(true);
  });

  it("applies semantic credentials to live events, extensions, snapshots, and replay", () => {
    const basic = "Basic dXNlcjpwYXNz";
    const jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln";
    const assignment = "password=hunter2";

    expect(decodeCloudRealtimeV2Frame(textEventV2(basic)))
      .toEqual({ ok: false, reason: "invalid_frame" });

    const extensionEvent = lifecycleEvents()[2];
    Object.assign(extensionEvent.params, {
      extensions: { "com.example.display": { label: jwt } },
    });
    expect(decodeCloudRealtimeV2Frame(extensionEvent))
      .toEqual({ ok: false, reason: "invalid_frame" });

    const snapshot = subscriptionResultV2(1, {
      messages: [{ role: "assistant", content: assignment }],
    });
    const decodedSnapshot = decodeCloudRealtimeV2Frame(snapshot);
    expect(decodedSnapshot.ok).toBe(true);
    if (!decodedSnapshot.ok || !("result" in decodedSnapshot.value)) throw new Error("expected RPC result");
    expect(decodeObserverSubscriptionV2(decodedSnapshot.value, 1, STABLE_SESSION_ID, "default")).toBeNull();

    const replay = subscriptionResultV2(1, {
      event_sequence: 5,
      snapshot_event_sequence: 4,
      replay_events: [textEventV2(jwt).params],
    });
    const decodedReplay = decodeCloudRealtimeV2Frame(replay);
    expect(decodedReplay.ok).toBe(true);
    if (!decodedReplay.ok || !("result" in decodedReplay.value)) throw new Error("expected RPC result");
    expect(decodeObserverSubscriptionV2(decodedReplay.value, 1, STABLE_SESSION_ID, "default")).toBeNull();
  });

  it("rejects extension control characters while allowing bounded display-safe metadata", () => {
    const unsafe = lifecycleEvents()[2];
    Object.assign(unsafe.params, {
      extensions: {
        "com.example.display": { label: "hidden\u0000value" },
      },
    });
    expect(decodeCloudRealtimeV2Frame(unsafe)).toEqual({ ok: false, reason: "invalid_frame" });

    const safe = lifecycleEvents()[2];
    Object.assign(safe.params, {
      extensions: {
        "com.example.display": {
          label: "Focused tests complete",
          metrics: {
            duration_ms: 120,
            token_counts: { input: 10, output: 2, reasoning: 1 },
          },
          badges: ["verified", "mobile-parity"],
        },
      },
    });
    expect(decodeCloudRealtimeV2Frame(safe).ok).toBe(true);
  });

  it("keeps extension depth, object fields, and arrays within generated v2 bounds", () => {
    const withExtensions = (value: unknown) => {
      const event = lifecycleEvents()[2];
      Object.assign(event.params, { extensions: { "com.example.display": value } });
      return event;
    };
    let nested: Record<string, unknown> = { label: "safe" };
    for (let index = 0; index < 33; index += 1) nested = { nested };

    expect(decodeCloudRealtimeV2Frame(withExtensions(nested))).toEqual({ ok: false, reason: "invalid_frame" });
    expect(decodeCloudRealtimeV2Frame(withExtensions(
      Object.fromEntries(Array.from({ length: 1_025 }, (_, index) => [`field_${index}`, index])),
    ))).toEqual({ ok: false, reason: "invalid_frame" });
    expect(decodeCloudRealtimeV2Frame(withExtensions({ items: Array.from({ length: 1_025 }, () => true) })))
      .toEqual({ ok: false, reason: "invalid_frame" });
  });

  it("keeps extension display text and integers within output-parity bounds", () => {
    const withExtensions = (value: unknown) => {
      const event = lifecycleEvents()[2];
      Object.assign(event.params, { extensions: { "com.example.display": value } });
      return event;
    };

    expect(decodeCloudRealtimeV2Frame(withExtensions({ label: "x".repeat(4_097) })))
      .toEqual({ ok: false, reason: "invalid_frame" });
    expect(decodeCloudRealtimeV2Frame(withExtensions({ count: 9_007_199_254_740_992 })))
      .toEqual({ ok: false, reason: "invalid_frame" });
  });

  it("keeps nested extension field names within the identifier bound", () => {
    const event = lifecycleEvents()[2];
    Object.assign(event.params, {
      extensions: {
        "com.example.display": { ["f".repeat(257)]: true },
      },
    });

    expect(decodeCloudRealtimeV2Frame(event)).toEqual({ ok: false, reason: "invalid_frame" });
  });

  it("decodes an exact snapshot baseline and validates every replay item with the full event decoder", () => {
    const result = subscriptionResultV2(1, {
      event_sequence: 5,
      snapshot_event_sequence: 4,
      replay_events: [lifecycleEvents()[0].params],
    });
    const decoded = decodeCloudRealtimeV2Frame(result);
    expect(decoded.ok).toBe(true);
    if (!decoded.ok || !("result" in decoded.value)) throw new Error("expected RPC result");
    expect(decodeObserverSubscriptionV2(decoded.value, 1, STABLE_SESSION_ID, "default")).toMatchObject({
      observerContract: 2,
      sessionId: STABLE_SESSION_ID,
      profile: "default",
      runtimeGeneration: "generation-1",
      eventSequence: 5,
      snapshotEventSequence: 4,
    });

    const unsafeReplay = subscriptionResultV2(1, {
      event_sequence: 5,
      snapshot_event_sequence: 4,
      replay_events: [{
        ...lifecycleEvents()[2].params,
        event_sequence: 5,
        payload: { ...lifecycleEvents()[2].params.payload, raw_args: { command: "must not cross" } },
      }],
    });
    const unsafeDecoded = decodeCloudRealtimeV2Frame(unsafeReplay);
    expect(unsafeDecoded.ok).toBe(true);
    if (!unsafeDecoded.ok || !("result" in unsafeDecoded.value)) throw new Error("expected RPC result");
    expect(decodeObserverSubscriptionV2(unsafeDecoded.value, 1, STABLE_SESSION_ID, "default")).toBeNull();
  });

  it.each([
    ["messages content", (result: ReturnType<typeof subscriptionResultV2>) => {
      (result.result.messages as Array<{ role: string; content: string }>).push(
        { role: "assistant", content: "Authorization: Basic dXNlcjpwYXNzd29yZA==" },
      );
    }],
    ["inflight user", (result: ReturnType<typeof subscriptionResultV2>) => {
      (result.result.inflight as { user: string | null }).user = "Authorization: Basic dXNlcjpwYXNzd29yZA==";
    }],
    ["todo label", (result: ReturnType<typeof subscriptionResultV2>) => {
      result.result.todo_sections[0]!.items[0]!.label = "Authorization: Basic dXNlcjpwYXNzd29yZA==";
    }],
    ["subagent summary", (result: ReturnType<typeof subscriptionResultV2>) => {
      (result.result.subagents[0] as { summary: string | null }).summary = "Authorization: Basic dXNlcjpwYXNzd29yZA==";
    }],
    ["tool summary", (result: ReturnType<typeof subscriptionResultV2>) => {
      (result.result.tools[0] as { summary?: string }).summary = "Authorization: Basic dXNlcjpwYXNzd29yZA==";
    }],
    ["terminal summary", (result: ReturnType<typeof subscriptionResultV2>) => {
      (result.result.terminals[0] as { summary?: string }).summary = "Authorization: Basic dXNlcjpwYXNzd29yZA==";
    }],
    ["replay text", (result: ReturnType<typeof subscriptionResultV2>) => {
      result.result.event_sequence = 5;
      (result.result.replay_events as Array<Record<string, unknown>>).push(
        textEventV2("Authorization: Basic dXNlcjpwYXNzd29yZA==").params,
      );
    }],
  ] as const)("rejects credential-like subscription snapshot %s", (_label, mutate) => {
    const result = subscriptionResultV2(1);
    mutate(result);
    const decoded = decodeCloudRealtimeV2Frame(result);
    expect(decoded.ok).toBe(true);
    if (!decoded.ok || !("result" in decoded.value)) throw new Error("expected RPC result");

    expect(decodeObserverSubscriptionV2(decoded.value, 1, STABLE_SESSION_ID, "default")).toBeNull();
  });

  it("allows safe snapshot display text and nonnegative aggregate token counts", () => {
    const result = subscriptionResultV2(1);
    (result.result.messages as Array<{ role: string; content: string }>).push(
      { role: "assistant", content: "Focused checks completed safely." },
    );
    const subagent = result.result.subagents[0] as {
      summary: string | null;
      token_counts?: { input: number; output: number; reasoning: number };
    };
    subagent.summary = "All checks passed.";
    subagent.token_counts = { input: 12, output: 7, reasoning: 3 };
    const decoded = decodeCloudRealtimeV2Frame(result);
    expect(decoded.ok).toBe(true);
    if (!decoded.ok || !("result" in decoded.value)) throw new Error("expected RPC result");

    expect(decodeObserverSubscriptionV2(decoded.value, 1, STABLE_SESSION_ID, "default")).not.toBeNull();
  });

  it.each([
    ["duplicate identity", (result: ReturnType<typeof subscriptionResultV2>) => {
      result.result.tools.push({ ...result.result.tools[0] });
    }],
    ["orphan subagent", (result: ReturnType<typeof subscriptionResultV2>) => {
      (result.result.subagents[0] as { parent_subagent_id: string | null }).parent_subagent_id = "missing";
    }],
    ["subagent cycle", (result: ReturnType<typeof subscriptionResultV2>) => {
      (result.result.subagents as Array<Record<string, unknown>>).push({
        ...result.result.subagents[0],
        subagent_id: "child",
        parent_subagent_id: "root",
        first_event_sequence: 2,
      });
      (result.result.subagents[0] as { subagent_id: string }).subagent_id = "root";
      (result.result.subagents[0] as { parent_subagent_id: string | null }).parent_subagent_id = "child";
    }],
    ["subagent depth over eight", (result: ReturnType<typeof subscriptionResultV2>) => {
      (result.result as { subagents: Array<Record<string, unknown>> }).subagents = Array.from(
        { length: 9 },
        (_, index) => ({
        ...result.result.subagents[0],
        subagent_id: `agent-${index}`,
        parent_subagent_id: index === 0 ? null : `agent-${index - 1}`,
        first_event_sequence: 2,
        }),
      );
    }],
    ["subagent node count over 128", (result: ReturnType<typeof subscriptionResultV2>) => {
      (result.result as { subagents: Array<Record<string, unknown>> }).subagents = Array.from(
        { length: 129 },
        (_, index) => ({
        ...result.result.subagents[0],
        subagent_id: `agent-${index}`,
        parent_subagent_id: null,
        first_event_sequence: 2,
        }),
      );
    }],
  ] as const)("rejects an invalid snapshot projection: %s", (_label, mutate) => {
    const result = subscriptionResultV2(1);
    mutate(result);
    const decoded = decodeCloudRealtimeV2Frame(result);
    expect(decoded.ok).toBe(true);
    if (!decoded.ok || !("result" in decoded.value)) throw new Error("expected RPC result");
    expect(decodeObserverSubscriptionV2(decoded.value, 1, STABLE_SESSION_ID, "default")).toBeNull();
  });
});

export function observerReadyV2() {
  return {
    jsonrpc: "2.0",
    method: "event",
    params: { type: "gateway.ready", payload: { observer_contract: 2, connection_role: "observer" } },
  };
}

export function subscriptionResultV2(id: number, overrides: Record<string, unknown> = {}) {
  return {
    jsonrpc: "2.0",
    id,
    result: {
      observer_contract: 2,
      subscription_id: "subscription-1",
      profile: "default",
      runtime_generation: "generation-1",
      session_id: STABLE_SESSION_ID,
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
      subagents: [{
        turn_id: "turn-1",
        subagent_id: "agent-1",
        revision: 1,
        first_event_sequence: 2,
        parent_subagent_id: null,
        name: "Test runner",
        goal: "Run tests",
        summary: null,
        status: "running",
      }],
      tools: [{
        turn_id: "turn-1",
        tool_call_id: "tool-1",
        revision: 1,
        first_event_sequence: 3,
        status: "running",
        name: "Tests",
      }],
      terminals: [{
        turn_id: "turn-1",
        process_id: "process-1",
        revision: 1,
        first_event_sequence: 4,
        status: "running",
      }],
      replay_events: [],
      ...overrides,
    },
  };
}

export function lifecycleEvents() {
  const envelope = (type: string, eventSequence: number, payload: Record<string, unknown>) => ({
    jsonrpc: "2.0",
    method: "event",
    params: {
      observer_contract: 2,
      profile: "default",
      runtime_generation: "generation-1",
      type,
      session_id: STABLE_SESSION_ID,
      event_sequence: eventSequence,
      payload,
    },
  });
  return [
    envelope("todo.update", 5, {
      turn_id: "turn-1",
      section_id: "todo-1",
      revision: 2,
      first_event_sequence: 1,
      operation: "upsert",
      status: "completed",
      items: [{ id: "item-1", label: "Run tests", status: "completed" }],
    }),
    envelope("subagent.update", 6, {
      turn_id: "turn-1",
      subagent_id: "agent-1",
      revision: 2,
      first_event_sequence: 2,
      operation: "upsert",
      parent_subagent_id: null,
      name: "Test runner",
      goal: "Run tests",
      summary: "Complete",
      status: "completed",
    }),
    envelope("tool.update", 7, {
      turn_id: "turn-1",
      tool_call_id: "tool-1",
      revision: 2,
      first_event_sequence: 3,
      operation: "upsert",
      status: "completed",
      name: "Tests",
      summary: "All tests passed",
    }),
    envelope("terminal.update", 8, {
      turn_id: "turn-1",
      process_id: "process-1",
      revision: 2,
      first_event_sequence: 4,
      operation: "upsert",
      status: "completed",
      exit_code: 0,
      summary: "Process completed",
    }),
  ];
}

function textEventV2(text: string) {
  return {
    jsonrpc: "2.0",
    method: "event",
    params: {
      observer_contract: 2,
      profile: "default",
      runtime_generation: "generation-1",
      type: "message.delta",
      session_id: STABLE_SESSION_ID,
      event_sequence: 5,
      payload: { text },
    },
  };
}
