import type { ApprovalChoice } from "./model";

export class CommandOutcomeUnknown extends Error {
  constructor() {
    super("Hermes command outcome is still unknown");
    this.name = "CommandOutcomeUnknown";
  }
}

export interface RuntimeBinding {
  sessionId: string;
  leaseId: string;
  clientRequestId: string;
}

export interface SubmitPromptCommand extends RuntimeBinding {
  clientTurnId: string;
  text: string;
}

export interface PromptCommandResult {
  status: "queued" | "accepted";
  clientRequestId: string;
  clientTurnId: string;
  serverTurnId?: string;
}

export interface SteerCommand extends RuntimeBinding {
  text: string;
}

export type InterruptCommand = RuntimeBinding;

export interface MutationCommandResult {
  status: "accepted";
  clientRequestId: string;
}

export interface ApprovalCommand extends RuntimeBinding {
  requestId: string;
  choice: ApprovalChoice;
  controlRevision: number;
}

export type ApprovalCommandResult = {
  status: "accepted";
  kind: "approval";
  requestId: string;
  clientRequestId: string;
  controlRevision: number;
} | ReconciledCommandResult;

export type ClarificationCommand = RuntimeBinding & {
  requestId: string;
  controlRevision: number;
} & (
  | { choiceId: string; otherText?: never }
  | { choiceId?: never; otherText: string }
);

export type ClarificationCommandResult = {
  status: "accepted";
  kind: "clarify";
  requestId: string;
  clientRequestId: string;
  controlRevision: number;
} | ReconciledCommandResult;

export interface ReconciledCommandResult {
  status: "reconciled";
  clientRequestId: string;
}

export interface RuntimePendingApproval {
  kind: "approval";
  requestId: string;
  title: string;
  description: string;
  command: string;
  choices: readonly ApprovalChoice[];
  expiresAtEpochMs: number;
}

export interface RuntimePendingClarification {
  kind: "clarify";
  requestId: string;
  question: string;
  choices: readonly { id: string; label: string }[];
  allowOther: boolean;
  expiresAtEpochMs: number;
}

export type RuntimePendingInput = RuntimePendingApproval | RuntimePendingClarification;
export type RuntimeControllerKind = "desktop" | "mobile" | "none";

export interface RuntimeControlState {
  leaseId: string | null;
  runtimeSessionId: string | null;
  leaseExpiresAtEpochMs: number;
  controlRevision: number;
  controllerKind: RuntimeControllerKind | null;
  controllerLabel: string | null;
  pendingInput: RuntimePendingInput | null;
  unavailableReason: string | null;
}

export interface RuntimeCallbacks {
  onConnectionChanged(connection: "connected" | "disconnected"): void;
  onSubscription(snapshot: RuntimeSubscriptionSnapshot): void;
  onEvent(event: RuntimeEvent): void;
  onControlReady(availableMethods: readonly string[]): void;
  onControlStateChanged(state: RuntimeControlState): void;
  onRunningChanged(running: boolean): void;
}

export interface RuntimeSubscriptionSnapshot {
  observerContract?: 1 | 2;
  runtimeGeneration?: string | null;
  runtimeSessionId: string;
  running: boolean;
  status: string;
  messages: readonly { role: string; content?: string | null }[];
  inflight: { user: string | null; assistant: string | null; streaming: boolean; error: string | null };
  todoSections?: readonly RuntimeTodoSection[];
  subagents?: readonly RuntimeSubagentProjection[];
  tools?: readonly RuntimeToolProjection[];
  terminals?: readonly RuntimeTerminalProjection[];
}

export interface RuntimeTodoSection {
  turnId: string;
  sectionId: string;
  revision: number;
  firstEventSequence: number;
  status: "pending" | "in_progress" | "completed" | "cancelled";
  items: readonly { id: string; label: string; status: "pending" | "in_progress" | "completed" | "cancelled" }[];
}

export interface RuntimeSubagentProjection {
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

export interface RuntimeToolProjection {
  turnId: string;
  toolCallId: string;
  revision: number;
  firstEventSequence: number;
  status: "running" | "completed" | "failed" | "interrupted" | "unknown";
  name: string;
  callLabel?: string;
  summary?: string;
}

export interface RuntimeTerminalProjection {
  turnId: string;
  processId: string;
  revision: number;
  firstEventSequence: number;
  status: "running" | "completed" | "failed" | "interrupted" | "unknown";
  summary?: string;
  exitCode?: number;
}

export interface RuntimeEvent {
  type:
    | "message.start"
    | "message.delta"
    | "message.complete"
    | "agent.terminal.output"
    | "reasoning.delta"
    | "status.update"
    | "thinking.delta"
    | "tool.output.delta"
    | "todo.update"
    | "subagent.update"
    | "tool.update"
    | "terminal.update";
  eventSequence: number;
  payload: Record<string, unknown>;
}

export interface HermesRuntimePort {
  start(callbacks: RuntimeCallbacks): () => void;
  retryConnection?(): void;
  submitPrompt(command: SubmitPromptCommand): Promise<PromptCommandResult>;
  steer(command: SteerCommand): Promise<MutationCommandResult>;
  interrupt(command: InterruptCommand): Promise<MutationCommandResult>;
  respondApproval(command: ApprovalCommand): Promise<ApprovalCommandResult>;
  respondClarification(command: ClarificationCommand): Promise<ClarificationCommandResult>;
}
