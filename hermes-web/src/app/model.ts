export type ViewId = "conversation" | "subagents" | "long";

export type RuntimeConnection = "connected" | "disconnected";

export type WorkStatus = "active" | "queued" | "waiting" | "complete" | "stopped";

export type ConversationEventKind =
  | "user"
  | "thinking"
  | "todo"
  | "tool"
  | "terminal"
  | "subagent"
  | "status"
  | "input"
  | "assistant";

export interface TodoItem {
  id: string;
  label: string;
  status: "pending" | "in_progress" | "completed" | "cancelled";
}

export interface ConversationEvent {
  id: string;
  kind: ConversationEventKind;
  label: string;
  time: string;
  body?: string;
  count?: number;
  status?: WorkStatus;
  details?: string;
  expanded?: boolean;
  items?: readonly TodoItem[];
  eventSequence?: number;
  revision?: number;
  turnId?: string;
}

export interface SubagentItem {
  id: string;
  name: string;
  role: "coordinator" | "child";
  goal: string;
  summary: string;
  time: string;
  status: WorkStatus;
  parentId?: string;
  revision?: number;
  firstEventSequence?: number;
  turnId?: string;
}

export interface LongConversationEvent {
  id: string;
  time: string;
  actor: string;
  summary: string;
  tone: "neutral" | "cyan" | "green" | "amber" | "red";
}

export type ApprovalChoice = "allow_once" | "allow_session" | "allow_always" | "deny";

export interface PendingApproval {
  requestId: string;
  time?: string;
  title: string;
  description: string;
  command: string;
  choices: readonly ApprovalChoice[];
  expiresAtEpochMs: number;
  controlRevision: number;
  resolution: ApprovalChoice | null;
  confirmationChoice: ApprovalChoice | null;
}

export type ClarificationAnswer =
  | { choiceId: string; otherText?: never }
  | { choiceId?: never; otherText: string };

export interface PendingClarification {
  requestId: string;
  question: string;
  choices: readonly { id: string; label: string }[];
  allowOther: boolean;
  expiresAtEpochMs: number;
  controlRevision: number;
  otherDraft: string;
  resolution: ClarificationAnswer | null;
}

export interface ControlState {
  leaseId: string | null;
  runtimeSessionId: string | null;
  leaseExpiresAtEpochMs: number;
  controlRevision: number;
  availableMethods: readonly string[];
  controllerKind: "desktop" | "mobile" | "none" | null;
  controllerLabel: string | null;
  unavailableReason: string | null;
}

export interface CommandFeedback {
  kind: "prompt" | "approval" | "clarify" | "steer" | "interrupt";
  status: "pending" | "confirmed" | "unknown" | "failed";
  message: string;
  clientRequestId: string;
}

export interface CommandLock {
  key: string;
  clientRequestId: string;
  awaitingSnapshot: boolean;
}

export interface HermesWebState {
  activeView: ViewId;
  source: "cloud" | "preview";
  connection: RuntimeConnection;
  controller: boolean;
  sessionId: string;
  stableSessionId: string;
  runtimeSessionId: string | null;
  runtimeGeneration: string | null;
  observerContract: 1 | 2 | null;
  outputParityAvailable: boolean;
  running: boolean;
  control: ControlState;
  networkLabel: string;
  queueExpanded: boolean;
  queuedPrompts: readonly string[];
  conversation: readonly ConversationEvent[];
  subagents: readonly SubagentItem[];
  subagentDraft: string;
  longEvents: readonly LongConversationEvent[];
  longLineCount: number;
  pendingApproval: PendingApproval | null;
  pendingClarification: PendingClarification | null;
  composerDraft: string;
  commandFeedback: CommandFeedback | null;
  commandLock: CommandLock | null;
}

export type HermesWebAction =
  | { type: "view.selected"; view: ViewId }
  | { type: "queue.toggled" }
  | { type: "section.toggled"; eventId: string }
  | { type: "approval.confirmationRequested"; choice: ApprovalChoice }
  | { type: "approval.confirmed"; choice: ApprovalChoice; controlRevision: number; clientRequestId: string }
  | { type: "clarification.draftChanged"; value: string }
  | {
      type: "clarification.confirmed";
      answer: ClarificationAnswer;
      controlRevision: number;
      clientRequestId: string;
    }
  | { type: "prompt.confirmed"; clientRequestId: string; message: string }
  | { type: "subagents.commandConfirmed"; kind: "prompt" | "steer"; clientRequestId: string; message: string }
  | { type: "interrupt.confirmed"; clientRequestId: string }
  | { type: "command.started"; kind: CommandFeedback["kind"]; key: string; clientRequestId: string }
  | { type: "command.reconciled"; kind: "approval" | "clarify"; clientRequestId: string; message: string }
  | { type: "command.unknown"; kind: CommandFeedback["kind"]; clientRequestId: string; message: string }
  | { type: "command.failed"; kind: CommandFeedback["kind"]; clientRequestId: string; message: string }
  | { type: "runtime.runningChanged"; running: boolean }
  | { type: "runtime.connectionChanged"; connection: RuntimeConnection }
  | {
      type: "runtime.subscriptionInstalled";
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
  | { type: "runtime.eventReceived"; eventType: string; eventSequence: number; payload: Record<string, unknown> }
  | { type: "runtime.controlReady"; availableMethods: readonly string[] }
  | {
      type: "runtime.controlStateChanged";
      leaseId: string | null;
      runtimeSessionId: string | null;
      leaseExpiresAtEpochMs: number;
      controlRevision: number;
      controllerKind: "desktop" | "mobile" | "none" | null;
      controllerLabel: string | null;
      pendingInput:
        | {
            kind: "approval";
            requestId: string;
            title: string;
            description: string;
            command: string;
            choices: readonly ApprovalChoice[];
            expiresAtEpochMs: number;
          }
        | {
            kind: "clarify";
            requestId: string;
            question: string;
            choices: readonly { id: string; label: string }[];
            allowOther: boolean;
            expiresAtEpochMs: number;
          }
        | null;
      unavailableReason: string | null;
    }
  | { type: "subagents.draftChanged"; value: string }
  | { type: "composer.draftChanged"; value: string };
import type {
  RuntimeSubagentProjection,
  RuntimeTerminalProjection,
  RuntimeTodoSection,
  RuntimeToolProjection,
} from "./runtimePort";
