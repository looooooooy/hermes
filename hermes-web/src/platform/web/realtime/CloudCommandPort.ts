import type {
  ApprovalCommand,
  ApprovalCommandResult,
  ClarificationCommand,
  ClarificationCommandResult,
  HermesRuntimePort,
  InterruptCommand,
  MutationCommandResult,
  PromptCommandResult,
  ReconciledCommandResult,
  RuntimeCallbacks,
  SteerCommand,
  SubmitPromptCommand,
} from "../../../app/runtimePort";
import { CommandOutcomeUnknown } from "../../../app/runtimePort";
import { CloudRpcFailure } from "./CloudRealtimeAdapter";

export { CommandOutcomeUnknown };

export interface ControlRpcClient {
  call(method: string, params: Record<string, unknown>): Promise<Record<string, unknown>>;
}

export class CloudCommandPort implements HermesRuntimePort {
  private readonly ledger = new Map<string, {
    fingerprint: string;
    promise: Promise<unknown>;
  }>();

  constructor(private readonly client: ControlRpcClient) {}

  start(callbacks: RuntimeCallbacks): () => void {
    void callbacks;
    return () => undefined;
  }

  async submitPrompt(command: SubmitPromptCommand): Promise<PromptCommandResult> {
    const params = {
      ...mutationScope(command),
      client_turn_id: command.clientTurnId,
      text: command.text,
    };
    const decode = (result: Record<string, unknown>, reconciled = false): PromptCommandResult => {
      const keys = ["status", "client_request_id"];
      if ("client_turn_id" in result) keys.push("client_turn_id");
      if ("server_turn_id" in result) keys.push("server_turn_id");
      if (
        !hasExactKeys(result, keys)
        || (result.status !== "queued" && result.status !== "accepted")
        || result.client_request_id !== command.clientRequestId
        || (!reconciled && result.client_turn_id !== command.clientTurnId)
        || ("client_turn_id" in result && result.client_turn_id !== command.clientTurnId)
        || ("server_turn_id" in result && !isNonEmptyString(result.server_turn_id))
      ) throw new Error(reconciled ? "invalid session.command.status result" : "invalid prompt.submit result");
      return {
        status: result.status,
        clientRequestId: command.clientRequestId,
        clientTurnId: command.clientTurnId,
        ...(typeof result.server_turn_id === "string" ? { serverTurnId: result.server_turn_id } : {}),
      };
    };
    return this.executeWithLedger("prompt.submit", params, decode, (result) => decode(result, true));
  }

  async steer(command: SteerCommand): Promise<MutationCommandResult> {
    const params = {
      ...mutationScope(command),
      text: command.text,
    };
    const decode = (result: Record<string, unknown>) => (
      decodeMutationResult(result, command.clientRequestId, "session.steer")
    );
    return this.executeWithLedger("session.steer", params, decode, decode);
  }

  async interrupt(command: InterruptCommand): Promise<MutationCommandResult> {
    const params = mutationScope(command);
    const decode = (result: Record<string, unknown>) => (
      decodeMutationResult(result, command.clientRequestId, "session.interrupt")
    );
    return this.executeWithLedger("session.interrupt", params, decode, decode);
  }

  async respondApproval(command: ApprovalCommand): Promise<ApprovalCommandResult> {
    const params = {
      ...mutationScope(command),
      request_id: command.requestId,
      choice: command.choice,
    };
    return this.executeWithLedger<ApprovalCommandResult>("approval.respond", params, (result) => {
      if (
        !hasExactKeys(result, ["status", "kind", "request_id", "client_request_id", "control_revision"])
        || result.status !== "accepted"
        || result.kind !== "approval"
        || result.request_id !== command.requestId
        || result.client_request_id !== command.clientRequestId
        || !isNonNegativeInteger(result.control_revision)
      ) throw new Error("invalid approval.respond result");
      return {
        status: "accepted",
        kind: "approval",
        requestId: result.request_id,
        clientRequestId: result.client_request_id,
        controlRevision: result.control_revision,
      };
    }, (result) => decodePendingReconciliation(result, command.clientRequestId));
  }

  respondClarification(command: ClarificationCommand): Promise<ClarificationCommandResult> {
    if (command.otherText !== undefined && command.otherText.trim().length === 0) {
      return Promise.reject(new Error("clarification answer must not be blank"));
    }
    const answer = "choiceId" in command && command.choiceId !== undefined
      ? { choice_id: command.choiceId }
      : { other_text: command.otherText };
    const params = {
      ...mutationScope(command),
      request_id: command.requestId,
      ...answer,
    };
    return this.executeWithLedger<ClarificationCommandResult>("clarify.respond", params, (result) => {
      if (
        !hasExactKeys(result, ["status", "kind", "request_id", "client_request_id", "control_revision"])
        || result.status !== "accepted"
        || result.kind !== "clarify"
        || result.request_id !== command.requestId
        || result.client_request_id !== command.clientRequestId
        || !isNonNegativeInteger(result.control_revision)
      ) throw new Error("invalid clarify.respond result");
      return {
        status: "accepted",
        kind: "clarify",
        requestId: result.request_id,
        clientRequestId: result.client_request_id,
        controlRevision: result.control_revision,
      };
    }, (result) => decodePendingReconciliation(result, command.clientRequestId));
  }

  private executeWithLedger<T>(
    method: string,
    params: Record<string, unknown>,
    decode: (result: Record<string, unknown>) => T,
    decodeReconciled: (result: Record<string, unknown>) => T = decode,
  ): Promise<T> {
    const key = `${method}\u0000${String(params.session_id)}\u0000${String(params.client_request_id)}`;
    const fingerprint = JSON.stringify(params);
    const existing = this.ledger.get(key);
    if (existing !== undefined) {
      if (existing.fingerprint !== fingerprint) {
        return Promise.reject(new Error("client request id was reused with a different payload"));
      }
      return existing.promise as Promise<T>;
    }
    const promise = this.callAndReconcile(method, params).then((outcome) => (
      outcome.reconciled ? decodeReconciled(outcome.result) : decode(outcome.result)
    ));
    this.ledger.set(key, { fingerprint, promise });
    if (this.ledger.size > 128) {
      const oldest = this.ledger.keys().next().value as string | undefined;
      if (oldest !== undefined) this.ledger.delete(oldest);
    }
    return promise;
  }

  private async callAndReconcile(
    method: string,
    params: Record<string, unknown>,
  ): Promise<{ result: Record<string, unknown>; reconciled: boolean }> {
    try {
      return { result: await this.client.call(method, params), reconciled: false };
    } catch (error) {
      if (!(error instanceof CloudRpcFailure) || error.code !== 4307) {
        throw error;
      }
      try {
        const result = await this.client.call("session.command.status", {
          session_id: params.session_id,
          method,
          client_request_id: params.client_request_id,
        });
        validateCommandStatus(result, String(params.client_request_id));
        return { result, reconciled: true };
      } catch (statusError) {
        if (statusError instanceof CloudRpcFailure && statusError.code === 4210) {
          throw new CommandOutcomeUnknown();
        }
        throw statusError;
      }
    }
  }
}

function mutationScope(command: InterruptCommand): Record<string, unknown> {
  return {
    session_id: command.sessionId,
    lease_id: command.leaseId,
    client_request_id: command.clientRequestId,
  };
}

function decodeMutationResult(
  result: Record<string, unknown>,
  clientRequestId: string,
  method: string,
): MutationCommandResult {
  if (
    !hasExactKeys(result, ["status", "client_request_id"])
    || result.status !== "accepted"
    || result.client_request_id !== clientRequestId
  ) throw new Error(`invalid ${method} result`);
  return { status: "accepted", clientRequestId };
}

function decodePendingReconciliation(
  result: Record<string, unknown>,
  clientRequestId: string,
): ReconciledCommandResult {
  validateCommandStatus(result, clientRequestId);
  if (result.status === "rejected") throw new Error("Hermes rejected the command");
  return { status: "reconciled", clientRequestId };
}

function validateCommandStatus(result: Record<string, unknown>, clientRequestId: string): void {
  const keys = ["status", "client_request_id"];
  if ("client_turn_id" in result) keys.push("client_turn_id");
  if ("server_turn_id" in result) keys.push("server_turn_id");
  if (
    !hasExactKeys(result, keys)
    || !["accepted", "queued", "rejected"].includes(String(result.status))
    || result.client_request_id !== clientRequestId
    || ("client_turn_id" in result && !isNonEmptyString(result.client_turn_id))
    || ("server_turn_id" in result && !isNonEmptyString(result.server_turn_id))
  ) throw new Error("invalid session.command.status result");
  if (result.status === "rejected") throw new Error("Hermes rejected the command");
}

function hasExactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value);
  return actual.length === keys.length && actual.every((key) => keys.includes(key));
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function isNonNegativeInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) >= 0;
}
