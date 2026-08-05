import type {
  ApprovalCommand,
  ApprovalCommandResult,
  ClarificationCommand,
  ClarificationCommandResult,
  HermesRuntimePort,
  InterruptCommand,
  MutationCommandResult,
  PromptCommandResult,
  RuntimeCallbacks,
  SteerCommand,
  SubmitPromptCommand,
} from "../app/runtimePort";

/** Explicit fixture adapter. It is only dynamically imported by the dev entrypoint. */
export class PreviewRuntimeAdapter implements HermesRuntimePort {
  private callbacks: RuntimeCallbacks | null = null;

  start(callbacks: RuntimeCallbacks): () => void {
    this.callbacks = callbacks;
    return () => { this.callbacks = null; };
  }

  async submitPrompt(command: SubmitPromptCommand): Promise<PromptCommandResult> {
    return {
      status: "queued",
      clientRequestId: command.clientRequestId,
      clientTurnId: command.clientTurnId,
      serverTurnId: `preview-${command.clientTurnId}`,
    };
  }

  async steer(command: SteerCommand): Promise<MutationCommandResult> {
    return { status: "accepted", clientRequestId: command.clientRequestId };
  }

  async interrupt(command: InterruptCommand): Promise<MutationCommandResult> {
    queueMicrotask(() => this.callbacks?.onRunningChanged(false));
    return { status: "accepted", clientRequestId: command.clientRequestId };
  }

  async respondApproval(command: ApprovalCommand): Promise<ApprovalCommandResult> {
    return {
      status: "accepted",
      kind: "approval",
      requestId: command.requestId,
      clientRequestId: command.clientRequestId,
      controlRevision: command.controlRevision + 1,
    };
  }

  async respondClarification(command: ClarificationCommand): Promise<ClarificationCommandResult> {
    return {
      status: "accepted",
      kind: "clarify",
      requestId: command.requestId,
      clientRequestId: command.clientRequestId,
      controlRevision: command.controlRevision + 1,
    };
  }
}
