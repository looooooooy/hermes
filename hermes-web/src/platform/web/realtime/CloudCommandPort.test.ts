import { CloudCommandPort, CommandOutcomeUnknown } from "./CloudCommandPort";
import { CloudRpcFailure } from "./CloudRealtimeAdapter";

const SESSION_ID = "88888888-8888-4888-8888-888888888888";

describe("CloudCommandPort", () => {
  it("maps prompt, steer, interrupt, and approval commands to exact bound RPC frames", async () => {
    const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
    const client = {
      call: vi.fn(async (method: string, params: Record<string, unknown>) => {
        calls.push({ method, params });
        if (method === "prompt.submit") {
          return {
            status: "queued",
            client_request_id: params.client_request_id,
            client_turn_id: params.client_turn_id,
            server_turn_id: "server-turn-1",
          };
        }
        if (method === "approval.respond") {
          return {
            status: "accepted",
            kind: "approval",
            request_id: params.request_id,
            client_request_id: params.client_request_id,
            control_revision: 8,
          };
        }
        return { status: "accepted", client_request_id: params.client_request_id };
      }),
    };
    const port = new CloudCommandPort(client);
    const binding = {
      sessionId: SESSION_ID,
      leaseId: "lease-1",
      clientRequestId: "request-1",
    };

    await expect(port.submitPrompt({ ...binding, clientTurnId: "turn-1", text: "Run tests" })).resolves.toEqual({
      status: "queued",
      clientRequestId: "request-1",
      clientTurnId: "turn-1",
      serverTurnId: "server-turn-1",
    });
    await expect(port.steer({ ...binding, text: "Check failure" })).resolves.toEqual({
      status: "accepted",
      clientRequestId: "request-1",
    });
    await expect(port.interrupt(binding)).resolves.toEqual({ status: "accepted", clientRequestId: "request-1" });
    await expect(port.respondApproval({
      ...binding,
      requestId: "approval-42",
      choice: "allow_always",
      controlRevision: 7,
    })).resolves.toEqual({
      status: "accepted",
      kind: "approval",
      requestId: "approval-42",
      clientRequestId: "request-1",
      controlRevision: 8,
    });

    expect(calls).toEqual([
      {
        method: "prompt.submit",
        params: {
          session_id: SESSION_ID,
          lease_id: "lease-1",
          client_request_id: "request-1",
          client_turn_id: "turn-1",
          text: "Run tests",
        },
      },
      {
        method: "session.steer",
        params: {
          session_id: SESSION_ID,
          lease_id: "lease-1",
          client_request_id: "request-1",
          text: "Check failure",
        },
      },
      {
        method: "session.interrupt",
        params: {
          session_id: SESSION_ID,
          lease_id: "lease-1",
          client_request_id: "request-1",
        },
      },
      {
        method: "approval.respond",
        params: {
          session_id: SESSION_ID,
          lease_id: "lease-1",
          client_request_id: "request-1",
          request_id: "approval-42",
          choice: "allow_always",
        },
      },
    ]);
  });

  it("fails closed on malformed or mismatched RPC results", async () => {
    const port = new CloudCommandPort({
      call: vi.fn(async () => ({
        status: "accepted",
        client_request_id: "other-request",
        client_turn_id: "turn-1",
      })),
    });

    await expect(port.submitPrompt({
      sessionId: SESSION_ID,
      leaseId: "lease-1",
      clientRequestId: "request-1",
      clientTurnId: "turn-1",
      text: "Run tests",
    })).rejects.toThrow("invalid prompt.submit result");
  });

  it("maps both exact clarification answer forms and validates the response", async () => {
    const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
    const port = new CloudCommandPort({
      call: vi.fn(async (method, params) => {
        calls.push({ method, params });
        return {
          status: "accepted",
          kind: "clarify",
          request_id: params.request_id,
          client_request_id: params.client_request_id,
          control_revision: 9,
        };
      }),
    });
    const binding = {
      sessionId: SESSION_ID,
      leaseId: "lease-1",
      requestId: "clarify-42",
      controlRevision: 8,
    };

    await expect(port.respondClarification({
      ...binding,
      clientRequestId: "request-choice",
      choiceId: "choice-1",
    })).resolves.toMatchObject({ kind: "clarify", controlRevision: 9 });
    await expect(port.respondClarification({
      ...binding,
      clientRequestId: "request-other",
      otherText: "Use the staging tenant",
    })).resolves.toMatchObject({ kind: "clarify", controlRevision: 9 });

    expect(calls).toEqual([
      {
        method: "clarify.respond",
        params: {
          session_id: SESSION_ID,
          lease_id: "lease-1",
          client_request_id: "request-choice",
          request_id: "clarify-42",
          choice_id: "choice-1",
        },
      },
      {
        method: "clarify.respond",
        params: {
          session_id: SESSION_ID,
          lease_id: "lease-1",
          client_request_id: "request-other",
          request_id: "clarify-42",
          other_text: "Use the staging tenant",
        },
      },
    ]);
  });

  it("rejects a blank free-text clarification before creating a Cloud request", async () => {
    const client = { call: vi.fn() };
    const port = new CloudCommandPort(client);

    await expect(port.respondClarification({
      sessionId: SESSION_ID,
      leaseId: "lease-1",
      clientRequestId: "request-other",
      requestId: "clarify-42",
      controlRevision: 8,
      otherText: "   ",
    })).rejects.toThrow("clarification answer must not be blank");
    expect(client.call).not.toHaveBeenCalled();
  });

  it("treats 4306 as definitive before-effect failure without status lookup or resend", async () => {
    const client = {
      call: vi.fn(async () => {
        throw new CloudRpcFailure(4306, "deadline exceeded before effect");
      }),
    };
    const port = new CloudCommandPort(client);

    await expect(port.submitPrompt({
      sessionId: SESSION_ID,
      leaseId: "lease-1",
      clientRequestId: "request-1",
      clientTurnId: "turn-1",
      text: "Run tests",
    })).rejects.toMatchObject({ code: 4306 });
    expect(client.call).toHaveBeenCalledTimes(1);
  });

  it("reconciles 4307 prompt uncertainty through generic status without resending", async () => {
      const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
      const port = new CloudCommandPort({
        call: vi.fn(async (method, params) => {
          calls.push({ method, params });
          if (method === "prompt.submit") {
            throw new CloudRpcFailure(4307, "uncertain owner outcome");
          }
          return {
            status: "queued",
            client_request_id: "request-1",
            client_turn_id: "turn-1",
            server_turn_id: "server-turn-1",
          };
        }),
      });

      await expect(port.submitPrompt({
        sessionId: SESSION_ID,
        leaseId: "lease-1",
        clientRequestId: "request-1",
        clientTurnId: "turn-1",
        text: "Run tests",
      })).resolves.toEqual({
        status: "queued",
        clientRequestId: "request-1",
        clientTurnId: "turn-1",
        serverTurnId: "server-turn-1",
      });

      expect(calls.map(({ method }) => method)).toEqual([
        "prompt.submit",
        "session.command.status",
      ]);
      expect(calls[1].params).toEqual({
        session_id: SESSION_ID,
        method: "prompt.submit",
        client_request_id: "request-1",
      });
  });

  it.each(["approval", "clarify"] as const)(
    "adapts a generic accepted command status for %s without requiring mutation-only fields",
    async (kind) => {
      const method = kind === "approval" ? "approval.respond" : "clarify.respond";
      const client = {
        call: vi.fn(async (receivedMethod: string) => {
          if (receivedMethod === method) throw new CloudRpcFailure(4307, "effect unknown");
          return { status: "accepted", client_request_id: "request-1" };
        }),
      };
      const port = new CloudCommandPort(client);
      const binding = {
        sessionId: SESSION_ID,
        leaseId: "lease-1",
        clientRequestId: "request-1",
        requestId: "pending-1",
        controlRevision: 8,
      };

      const result = kind === "approval"
        ? await port.respondApproval({ ...binding, choice: "deny" })
        : await port.respondClarification({ ...binding, choiceId: "choice-1" });

      expect(result).toEqual({ status: "reconciled", clientRequestId: "request-1" });
      expect(client.call).toHaveBeenCalledTimes(2);
      expect(client.call).toHaveBeenNthCalledWith(2, "session.command.status", {
        session_id: SESSION_ID,
        method,
        client_request_id: "request-1",
      });
    },
  );

  it("surfaces an unknown 4307 outcome and never auto-resends", async () => {
    const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
    const port = new CloudCommandPort({
      call: vi.fn(async (method, params) => {
        calls.push({ method, params });
        if (method === "prompt.submit") throw new CloudRpcFailure(4307, "effect unknown");
        throw new CloudRpcFailure(4210, "command unknown");
      }),
    });

    const command = {
      sessionId: SESSION_ID,
      leaseId: "lease-1",
      clientRequestId: "request-1",
      clientTurnId: "turn-1",
      text: "Run tests",
    };
    await expect(port.submitPrompt(command)).rejects.toBeInstanceOf(CommandOutcomeUnknown);
    await expect(port.submitPrompt(command)).rejects.toBeInstanceOf(CommandOutcomeUnknown);
    expect(calls).toEqual([
      {
        method: "prompt.submit",
        params: {
          session_id: SESSION_ID,
          lease_id: "lease-1",
          client_request_id: "request-1",
          client_turn_id: "turn-1",
          text: "Run tests",
        },
      },
      {
        method: "session.command.status",
        params: {
          session_id: SESSION_ID,
          method: "prompt.submit",
          client_request_id: "request-1",
        },
      },
    ]);
  });

  it("keeps a bounded request ledger and rejects local payload conflicts", async () => {
    const client = {
      call: vi.fn(async (_method: string, params: Record<string, unknown>) => ({
        status: "accepted",
        client_request_id: params.client_request_id,
      })),
    };
    const port = new CloudCommandPort(client);
    const binding = {
      sessionId: SESSION_ID,
      leaseId: "lease-1",
      clientRequestId: "request-1",
    };

    await expect(port.steer({ ...binding, text: "Original payload" })).resolves.toEqual({
      status: "accepted",
      clientRequestId: "request-1",
    });
    await expect(port.steer({ ...binding, text: "Original payload" })).resolves.toEqual({
      status: "accepted",
      clientRequestId: "request-1",
    });
    await expect(port.steer({ ...binding, text: "Different payload" }))
      .rejects.toThrow("client request id was reused with a different payload");

    expect(client.call).toHaveBeenCalledTimes(1);
  });
});
