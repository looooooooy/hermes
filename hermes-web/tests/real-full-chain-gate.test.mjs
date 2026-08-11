import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { chmod, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";

import {
  GateConfigurationError,
  GateVerificationError,
  parseGateArguments,
  runRealFullChainGate,
} from "../scripts/real-full-chain-gate.mjs";

const AGENT_ID = "66666666-6666-4666-8666-666666666666";
const SESSION_ID = "88888888-8888-4888-8888-888888888888";
const WORKSPACE_ID = "77777777-7777-4777-8777-777777777777";
const ACCESS_TOKEN = "release-account-access-material";
const PROMPT = "Return one short acceptance response after exercising observable work.";
const cleanup = [];

afterEach(async () => {
  await Promise.allSettled(cleanup.splice(0).map((operation) => operation()));
});

test("workflow materializes runner-temporary paths only after the runner starts", async () => {
  const workflow = await readFile(
    new URL("../../.github/workflows/real-full-chain.yml", import.meta.url),
    "utf8",
  );
  const jobEnvironment = workflow.match(/    env:\n([\s\S]*?)    steps:/)?.[1];

  assert.ok(jobEnvironment);
  assert.doesNotMatch(jobEnvironment, /\$\{\{\s*runner\.temp/);
  assert.match(workflow, /GITHUB_ENV/);
});

test("requires explicit authority, target, account token, and prompt without exposing their values", async () => {
  assert.throws(
    () => parseGateArguments([
      "--cloud-url", "https://private.example/hermes/",
      "--access-token", ACCESS_TOKEN,
      "--agent-id", AGENT_ID,
      "--session-id", SESSION_ID,
    ]),
    (error) => {
      assert.ok(error instanceof GateConfigurationError);
      assert.doesNotMatch(error.message, /private\.example|release-account|Return one short/);
      return true;
    },
  );

  assert.throws(
    () => parseGateArguments([
      "--cloud-url", "http://public.example/hermes/",
      "--access-token", ACCESS_TOKEN,
      "--agent-id", AGENT_ID,
      "--session-id", SESSION_ID,
      "--prompt", PROMPT,
    ]),
    GateConfigurationError,
  );
});

test("accepts secret-bearing files only when they are regular owner-private files", async () => {
  const directory = await privateDirectory();
  const tokenPath = join(directory, "account-token");
  const promptPath = join(directory, "acceptance-prompt");
  await writeFile(tokenPath, ACCESS_TOKEN, { mode: 0o600 });
  await writeFile(promptPath, PROMPT, { mode: 0o600 });
  await chmod(tokenPath, 0o644);

  await assert.rejects(
    () => runRealFullChainGate([
      "--cloud-url", "https://cloud.invalid/hermes/",
      "--access-token-file", tokenPath,
      "--agent-id", AGENT_ID,
      "--session-id", SESSION_ID,
      "--prompt-file", promptPath,
    ]),
    (error) => {
      assert.ok(error instanceof GateConfigurationError);
      assert.doesNotMatch(error.message, /release-account|Return one short/);
      return true;
    },
  );
});

test("the executable failure receipt never echoes inline endpoint, token, or prompt material", () => {
  const endpointSecret = "https://private-account-authority.example/hermes/";
  const promptSecret = "operator-secret-acceptance-prompt";
  const result = spawnSync(process.execPath, [
    new URL("../scripts/real-full-chain-gate.mjs", import.meta.url).pathname,
    "--cloud-url", endpointSecret,
    "--access-token", ACCESS_TOKEN,
    "--agent-id", AGENT_ID,
    "--session-id", "not-a-canonical-session-id",
    "--prompt", promptSecret,
  ], { encoding: "utf8" });

  assert.equal(result.status, 2);
  assert.match(result.stderr, /"code":"INVALID_CONFIGURATION"/);
  assert.doesNotMatch(`${result.stdout}${result.stderr}`, /private-account-authority|release-account-access|operator-secret/);
});

test("proves ready, bearer authentication, real catalog, control, prompt streaming, and reconnect continuity over real transports", async () => {
  const authority = await startAuthority();
  cleanup.push(authority.close);
  const { tokenPath, promptPath } = await privateInputs();

  const receipt = await runRealFullChainGate([
    "--cloud-url", authority.origin,
    "--access-token-file", tokenPath,
    "--agent-id", AGENT_ID,
    "--session-id", SESSION_ID,
    "--prompt-file", promptPath,
    "--timeout-ms", "5000",
    "--require-evidence", "todo,tool",
  ], { allowInsecureLoopback: true });

  assert.deepEqual(receipt, {
    schema_version: 1,
    gate: "hermes-real-full-chain",
    status: "passed",
    cloud_ready: true,
    authenticated: true,
    agent_id: AGENT_ID,
    session_id: SESSION_ID,
    observer_contract: 2,
    control_contract: 1,
    prompt_status: "accepted",
    assistant_stream_ordered: true,
    assistant_terminal_event: "message.complete",
    reconnect_same_session: true,
    reconnect_sequence_continuous: true,
    evidence: {
      todo: "confirmed",
      tool: "confirmed",
      pending_input: "independent_gate_required",
      approval: "independent_gate_required",
    },
  });
  assert.equal(authority.state.observerConnections, 2);
  assert.equal(authority.state.controlConnections, 1);
  assert.equal(authority.state.promptSubmissions, 1);
  assert.equal(authority.state.releases, 1);
  assert.equal(authority.state.authorizationFailures, 0);
});

test("explicitly rejects the known Cloud test session and all demo or fixture catalog targets", async () => {
  for (const forbiddenIdentity of [
    "Hermes Cloud Test Session",
    "demo operator session",
    "release fixture session",
  ]) {
    const authority = await startAuthority({ forbiddenIdentity });
    cleanup.push(authority.close);
    const { tokenPath, promptPath } = await privateInputs();

    await assert.rejects(
      () => runRealFullChainGate([
        "--cloud-url", authority.origin,
        "--access-token-file", tokenPath,
        "--agent-id", AGENT_ID,
        "--session-id", SESSION_ID,
        "--prompt-file", promptPath,
        "--timeout-ms", "5000",
      ], { allowInsecureLoopback: true }),
      (error) => {
        assert.ok(error instanceof GateVerificationError);
        assert.equal(error.code, "FORBIDDEN_CATALOG_TARGET");
        assert.doesNotMatch(error.message, new RegExp(forbiddenIdentity, "i"));
        return true;
      },
    );
  }
});

test("fails closed on an observer sequence gap and does not claim a prompt closed loop", async () => {
  const authority = await startAuthority({ gapAfterPrompt: true });
  cleanup.push(authority.close);
  const { tokenPath, promptPath } = await privateInputs();

  await assert.rejects(
    () => runRealFullChainGate([
      "--cloud-url", authority.origin,
      "--access-token-file", tokenPath,
      "--agent-id", AGENT_ID,
      "--session-id", SESSION_ID,
      "--prompt-file", promptPath,
      "--timeout-ms", "5000",
    ], { allowInsecureLoopback: true }),
    (error) => {
      assert.ok(error instanceof GateVerificationError);
      assert.equal(error.code, "OBSERVER_SEQUENCE_DISCONTINUITY");
      assert.doesNotMatch(error.message, /release-account|Return one short/);
      return true;
    },
  );
});

test("turns optional evidence into an exact independent fail-closed gate", async () => {
  const authority = await startAuthority();
  cleanup.push(authority.close);
  const { tokenPath, promptPath } = await privateInputs();

  await assert.rejects(
    () => runRealFullChainGate([
      "--cloud-url", authority.origin,
      "--access-token-file", tokenPath,
      "--agent-id", AGENT_ID,
      "--session-id", SESSION_ID,
      "--prompt-file", promptPath,
      "--timeout-ms", "5000",
      "--require-evidence", "approval",
    ], { allowInsecureLoopback: true }),
    (error) => {
      assert.ok(error instanceof GateVerificationError);
      assert.equal(error.code, "REQUIRED_EVIDENCE_NOT_OBSERVED");
      return true;
    },
  );
});

test("confirms authoritative pending-input and approval evidence without answering it", async () => {
  const authority = await startAuthority({ pendingApproval: true });
  cleanup.push(authority.close);
  const { tokenPath, promptPath } = await privateInputs();

  const receipt = await runRealFullChainGate([
    "--cloud-url", authority.origin,
    "--access-token-file", tokenPath,
    "--agent-id", AGENT_ID,
    "--session-id", SESSION_ID,
    "--prompt-file", promptPath,
    "--timeout-ms", "5000",
    "--require-evidence", "pending_input,approval",
  ], { allowInsecureLoopback: true });

  assert.equal(receipt.evidence.pending_input, "confirmed");
  assert.equal(receipt.evidence.approval, "confirmed");
  assert.equal(authority.state.approvalResponses, 0);
});

async function privateInputs() {
  const directory = await privateDirectory();
  const tokenPath = join(directory, randomUUID());
  const promptPath = join(directory, randomUUID());
  await writeFile(tokenPath, `${ACCESS_TOKEN}\n`, { mode: 0o600 });
  await writeFile(promptPath, `${PROMPT}\n`, { mode: 0o600 });
  return { tokenPath, promptPath };
}

async function privateDirectory() {
  const directory = await mkdtemp(join(tmpdir(), "hermes-real-gate-"));
  await chmod(directory, 0o700);
  cleanup.push(() => rm(directory, { recursive: true, force: true }));
  return directory;
}

async function startAuthority(options = {}) {
  const state = {
    observerConnections: 0,
    controlConnections: 0,
    promptSubmissions: 0,
    releases: 0,
    approvalResponses: 0,
    authorizationFailures: 0,
  };
  const tickets = new Map();
  const sockets = new Set();
  let activeObserver = null;
  let promptCompleted = false;
  let leaseExpiresAt = 0;

  const server = createServer(async (request, response) => {
    const url = new URL(request.url ?? "/", "http://127.0.0.1");
    if (url.pathname === "/ready" && request.method === "GET") {
      return json(response, 200, {
        component: "business-api",
        error: null,
        live: true,
        ready: true,
        state: "READY",
      });
    }
    if (request.headers.authorization !== `Bearer ${ACCESS_TOKEN}`) {
      state.authorizationFailures += 1;
      return json(response, 401, { code: "UNAUTHORIZED" });
    }
    if (url.pathname === "/api/v1/agents" && request.method === "GET") {
      return json(response, 200, {
        agents: [{
          agent_id: AGENT_ID,
          workspace_id: WORKSPACE_ID,
          agent_key: "release-operator-agent",
          status: "active",
          last_seen_at: "2026-08-03T00:00:00Z",
        }],
      });
    }
    if (url.pathname === `/api/v1/agents/${AGENT_ID}/sessions` && request.method === "GET") {
      return json(response, 200, sessionCatalog(options.forbiddenIdentity));
    }
    if (url.pathname === "/api/auth/ws-ticket" && request.method === "POST") {
      const body = JSON.parse(await bodyText(request));
      const role = body.connection_role;
      const ticket = `${role}-${randomUUID()}-single-use-ticket`;
      tickets.set(ticket, role);
      return json(response, 200, {
        ticket,
        ttl_seconds: 30,
        connection_role: role,
        ...(role === "observer" ? { observer_contract: 2 } : {}),
      });
    }
    return json(response, 404, { code: "NOT_FOUND" });
  });

  server.on("upgrade", (request, socket) => {
    const url = new URL(request.url ?? "/", "http://127.0.0.1");
    const ticket = url.searchParams.get("ticket");
    const role = ticket === null ? undefined : tickets.get(ticket);
    if (role === undefined) return socket.destroy();
    tickets.delete(ticket);
    const protocol = role === "observer" ? "hermes.tui.v2" : "hermes.tui.v1";
    if (request.headers["sec-websocket-protocol"] !== protocol) return socket.destroy();
    const accept = createHash("sha1")
      .update(`${request.headers["sec-websocket-key"]}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`)
      .digest("base64");
    socket.write([
      "HTTP/1.1 101 Switching Protocols",
      "Upgrade: websocket",
      "Connection: Upgrade",
      `Sec-WebSocket-Accept: ${accept}`,
      `Sec-WebSocket-Protocol: ${protocol}`,
      "\r\n",
    ].join("\r\n"));
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
    const peer = websocketPeer(socket, (message) => onRpc(role, peer, message));
    if (role === "observer") {
      state.observerConnections += 1;
      activeObserver = peer;
      peer.send(observerReady());
    } else {
      state.controlConnections += 1;
      peer.send(controlReady());
    }
  });

  function onRpc(role, peer, request) {
    if (role === "observer" && request.method === "session.observe.subscribe") {
      peer.send({
        jsonrpc: "2.0",
        id: request.id,
        result: subscription(promptCompleted),
      });
      return;
    }
    if (role !== "control") return;
    if (request.method === "session.control.status") {
      peer.send({
        jsonrpc: "2.0",
        id: request.id,
        result: state.promptSubmissions === 0
          ? controllerStatus("none", leaseExpiresAt)
          : controllerStatus(
              "mobile",
              leaseExpiresAt,
              options.pendingApproval === true && state.promptSubmissions > 0 ? approvalInput() : null,
            ),
      });
      return;
    }
    if (request.method === "approval.respond") {
      state.approvalResponses += 1;
      return;
    }
    if (request.method === "session.control.acquire") {
      state.promptSubmissions = -1;
      leaseExpiresAt = Date.now() + 30_000;
      peer.send({ jsonrpc: "2.0", id: request.id, result: leaseResult(leaseExpiresAt) });
      return;
    }
    if (request.method === "prompt.submit") {
      state.promptSubmissions = 1;
      peer.send({
        jsonrpc: "2.0",
        id: request.id,
        result: {
          status: "accepted",
          client_request_id: request.params.client_request_id,
          client_turn_id: request.params.client_turn_id,
          server_turn_id: "server-turn-release-1",
        },
      });
      const events = promptEvents(options.gapAfterPrompt === true);
      setTimeout(() => {
        for (const event of events) activeObserver?.send(event);
        promptCompleted = true;
      }, 10);
      return;
    }
    if (request.method === "session.control.release") {
      state.releases += 1;
      peer.send({
        jsonrpc: "2.0",
        id: request.id,
        result: { released: true, control_revision: 2 },
      });
    }
  }

  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  assert.ok(address && typeof address === "object");
  return {
    origin: `http://127.0.0.1:${address.port}/`,
    state,
    close: async () => {
      for (const socket of sockets) socket.destroy();
      await new Promise((resolve) => server.close(resolve));
    },
  };
}

function sessionCatalog(forbiddenIdentity) {
  const forbidden = typeof forbiddenIdentity === "string";
  return {
    sessions: [{
      id: SESSION_ID,
      agent_id: AGENT_ID,
      workspace_id: forbidden ? WORKSPACE_ID : null,
      _lineage_root_id: forbiddenIdentity ?? "operator-release-session",
      parent_session_id: null,
      title: forbiddenIdentity ?? null,
      preview: null,
      source: null,
      model: null,
      profile: "default",
      cwd: null,
      git_branch: null,
      started_at: forbidden ? 1_754_179_200 : null,
      ended_at: null,
      last_active: forbidden ? 1_754_179_200 : null,
      message_count: forbidden ? 2 : 0,
      tool_call_count: 0,
      input_tokens: 0,
      output_tokens: 0,
      is_active: true,
      archived: false,
      directory_source: forbidden ? "transcript_projection" : "host_catalog",
      availability: "live",
      runtime_generation: forbidden ? null : "runtime-generation-release-1",
      surface: forbidden ? null : "codex",
      authority_revision: forbidden ? null : 1,
      available_actions: forbidden ? [] : controlMethods(),
      transcript_available: forbidden,
    }],
    total: 1,
    limit: 50,
    offset: 0,
  };
}

function observerReady() {
  return {
    jsonrpc: "2.0",
    method: "event",
    params: {
      type: "gateway.ready",
      payload: { observer_contract: 2, connection_role: "observer" },
    },
  };
}

function controlReady() {
  return {
    jsonrpc: "2.0",
    method: "event",
    params: {
      type: "gateway.ready",
      payload: {
        observer_contract: 1,
        control_contract: 1,
        connection_role: "control",
        control_available_methods: controlMethods(),
        control_error_codes: {
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
        },
      },
    },
  };
}

function controlMethods() {
  return [
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
  ];
}

function controllerStatus(kind, leaseExpiresAt, pendingInput = null) {
  const mobile = kind === "mobile";
  return {
    controller_kind: kind,
    controller_label: mobile ? "Hermes Web Acceptance" : null,
    control_revision: mobile ? 1 : 0,
    lease_expires_at_epoch_ms: mobile ? leaseExpiresAt : 0,
    pending_input: pendingInput,
  };
}

function approvalInput() {
  return {
    request_id: "approval-release-1",
    kind: "approval",
    title: "Confirm acceptance operation",
    description: "The operation requires authoritative confirmation.",
    command: "acceptance-operation",
    choices: ["allow_once", "deny"],
    expires_at_epoch_ms: Date.now() + 30_000,
  };
}

function leaseResult(leaseExpiresAt) {
  return {
    lease_id: "acceptance-lease",
    expires_at_epoch_ms: leaseExpiresAt,
    control_revision: 1,
    controller_kind: "mobile",
    controller_label: "Hermes Web Acceptance",
    pending_input: null,
  };
}

function subscription(completed) {
  return {
    observer_contract: 2,
    subscription_id: completed ? "subscription-after" : "subscription-before",
    profile: "default",
    runtime_generation: "runtime-generation-release-1",
    session_id: SESSION_ID,
    running: false,
    status: "completed",
    event_sequence: completed ? 10 : 4,
    snapshot_event_sequence: completed ? 10 : 4,
    messages: completed
      ? [
          { role: "user", content: "Previous operator message" },
          { role: "assistant", content: "Previous operator response" },
          { role: "user", content: PROMPT },
          { role: "assistant", content: "Acceptance complete" },
        ]
      : [
          { role: "user", content: "Previous operator message" },
          { role: "assistant", content: "Previous operator response" },
        ],
    inflight: { user: null, assistant: null, streaming: false, error: null },
    todo_sections: completed ? [todoState()] : [],
    subagents: [],
    tools: completed ? [toolState()] : [],
    terminals: [],
    replay_events: [],
  };
}

function promptEvents(gap) {
  const sequence = gap ? 6 : 5;
  return [
    event(sequence, "message.start", { message_id: "assistant-release-1", role: "assistant" }),
    event(sequence + 1, "message.delta", { text: "Acceptance " }),
    event(sequence + 2, "todo.update", { ...todoState(), operation: "upsert" }),
    event(sequence + 3, "tool.update", { ...toolState(), operation: "upsert" }),
    event(sequence + 4, "message.delta", { text: "complete" }),
    event(sequence + 5, "message.complete", { status: "complete", text: "Acceptance complete", error: null }),
  ];
}

function event(eventSequence, type, payload) {
  return {
    jsonrpc: "2.0",
    method: "event",
    params: {
      observer_contract: 2,
      profile: "default",
      runtime_generation: "runtime-generation-release-1",
      type,
      session_id: SESSION_ID,
      event_sequence: eventSequence,
      payload,
    },
  };
}

function todoState() {
  return {
    turn_id: "server-turn-release-1",
    section_id: "acceptance-work",
    revision: 1,
    first_event_sequence: 7,
    status: "completed",
    items: [{ id: "verify", label: "Verify acceptance", status: "completed" }],
  };
}

function toolState() {
  return {
    turn_id: "server-turn-release-1",
    tool_call_id: "tool-release-1",
    revision: 1,
    first_event_sequence: 8,
    status: "completed",
    name: "Terminal",
    summary: "Acceptance command completed",
  };
}

function websocketPeer(socket, onMessage) {
  let buffer = Buffer.alloc(0);
  socket.on("data", (chunk) => {
    buffer = Buffer.concat([buffer, chunk]);
    while (buffer.length >= 2) {
      const opcode = buffer[0] & 0x0f;
      let length = buffer[1] & 0x7f;
      const masked = (buffer[1] & 0x80) !== 0;
      let offset = 2;
      if (length === 126) {
        if (buffer.length < 4) return;
        length = buffer.readUInt16BE(2);
        offset = 4;
      }
      if (!masked || buffer.length < offset + 4 + length) return;
      const mask = buffer.subarray(offset, offset + 4);
      offset += 4;
      const payload = Buffer.from(buffer.subarray(offset, offset + length));
      for (let index = 0; index < payload.length; index += 1) payload[index] ^= mask[index % 4];
      buffer = buffer.subarray(offset + length);
      if (opcode === 0x8) {
        socket.write(Buffer.from([0x88, 0x00]));
        socket.end();
      } else if (opcode === 0x1) {
        onMessage(JSON.parse(payload.toString("utf8")));
      }
    }
  });
  return {
    send(value) {
      const payload = Buffer.from(JSON.stringify(value));
      assert.ok(payload.length < 65_536);
      const header = payload.length < 126
        ? Buffer.from([0x81, payload.length])
        : Buffer.from([0x81, 126, payload.length >> 8, payload.length & 0xff]);
      socket.write(Buffer.concat([header, payload]));
    },
  };
}

async function bodyText(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8");
}

function json(response, status, value) {
  const body = JSON.stringify(value);
  response.writeHead(status, {
    "content-type": "application/json",
    "content-length": Buffer.byteLength(body),
  });
  response.end(body);
}
