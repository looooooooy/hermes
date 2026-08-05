import type { ConversationEvent, HermesWebAction, HermesWebState, SubagentItem, WorkStatus } from "./model";
import type {
  RuntimeSubagentProjection,
  RuntimeTerminalProjection,
  RuntimeTodoSection,
  RuntimeToolProjection,
} from "./runtimePort";

export function appReducer(state: HermesWebState, action: HermesWebAction): HermesWebState {
  switch (action.type) {
    case "view.selected":
      return { ...state, activeView: action.view };
    case "queue.toggled":
      return { ...state, queueExpanded: !state.queueExpanded };
    case "section.toggled":
      return {
        ...state,
        conversation: state.conversation.map((event) =>
          event.id === action.eventId ? { ...event, expanded: !event.expanded } : event,
        ),
      };
    case "approval.confirmationRequested":
      if (state.pendingApproval === null || state.pendingApproval.resolution !== null) return state;
      return {
        ...state,
        pendingApproval: { ...state.pendingApproval, confirmationChoice: action.choice },
      };
    case "approval.confirmed":
      if (
        state.pendingApproval === null
        || state.pendingApproval.resolution !== null
        || action.controlRevision <= state.control.controlRevision
      ) return state;
      return {
        ...state,
        control: { ...state.control, controlRevision: action.controlRevision },
        pendingApproval: {
          ...state.pendingApproval,
          resolution: action.choice,
          confirmationChoice: null,
        },
        commandFeedback: {
          kind: "approval",
          status: "confirmed",
          message: action.choice === "deny" ? "Denied" : "Approved",
          clientRequestId: action.clientRequestId,
        },
        commandLock: null,
      };
    case "clarification.draftChanged":
      if (state.pendingClarification === null || state.pendingClarification.resolution !== null) return state;
      return {
        ...state,
        pendingClarification: { ...state.pendingClarification, otherDraft: action.value },
      };
    case "clarification.confirmed":
      if (
        state.pendingClarification === null
        || state.pendingClarification.resolution !== null
        || action.controlRevision <= state.control.controlRevision
      ) return state;
      return {
        ...state,
        control: { ...state.control, controlRevision: action.controlRevision },
        pendingClarification: { ...state.pendingClarification, resolution: action.answer },
        commandFeedback: {
          kind: "clarify",
          status: "confirmed",
          message: "Clarification accepted",
          clientRequestId: action.clientRequestId,
        },
        commandLock: null,
      };
    case "prompt.confirmed":
      return {
        ...state,
        composerDraft: "",
        commandFeedback: {
          kind: "prompt",
          status: "confirmed",
          message: action.message,
          clientRequestId: action.clientRequestId,
        },
        commandLock: null,
      };
    case "subagents.commandConfirmed":
      return {
        ...state,
        subagentDraft: "",
        commandFeedback: {
          kind: action.kind,
          status: "confirmed",
          message: action.message,
          clientRequestId: action.clientRequestId,
        },
        commandLock: null,
      };
    case "interrupt.confirmed":
      return {
        ...state,
        commandFeedback: {
          kind: "interrupt",
          status: "confirmed",
          message: "Stop accepted; waiting for runtime state",
          clientRequestId: action.clientRequestId,
        },
        commandLock: null,
      };
    case "command.started":
      return {
        ...state,
        commandFeedback: {
          kind: action.kind,
          status: "pending",
          message: "Waiting for Hermes",
          clientRequestId: action.clientRequestId,
        },
        commandLock: {
          key: action.key,
          clientRequestId: action.clientRequestId,
          awaitingSnapshot: false,
        },
      };
    case "command.reconciled":
      return {
        ...state,
        commandFeedback: {
          kind: action.kind,
          status: "confirmed",
          message: action.message,
          clientRequestId: action.clientRequestId,
        },
        commandLock: state.commandLock?.clientRequestId === action.clientRequestId
          ? { ...state.commandLock, awaitingSnapshot: true }
          : state.commandLock,
      };
    case "command.failed":
      return {
        ...state,
        commandFeedback: {
          kind: action.kind,
          status: "failed",
          message: action.message,
          clientRequestId: action.clientRequestId,
        },
        commandLock: state.commandLock?.clientRequestId === action.clientRequestId
          ? null
          : state.commandLock,
      };
    case "command.unknown":
      return {
        ...state,
        commandFeedback: {
          kind: action.kind,
          status: "unknown",
          message: action.message,
          clientRequestId: action.clientRequestId,
        },
        commandLock: state.commandLock?.clientRequestId === action.clientRequestId
          ? null
          : state.commandLock,
      };
    case "runtime.runningChanged":
      return {
        ...state,
        running: action.running,
        subagents: action.running || state.source !== "preview"
          ? state.subagents
          : state.subagents.map((agent) =>
            agent.status === "active" ? { ...agent, status: "stopped" } : agent,
          ),
      };
    case "runtime.connectionChanged":
      return {
        ...state,
        connection: action.connection,
        networkLabel: action.connection === "connected" ? "Cloud" : "Offline",
        commandLock: action.connection === "disconnected" ? null : state.commandLock,
      };
    case "runtime.controlReady":
      return { ...state, control: { ...state.control, availableMethods: action.availableMethods } };
    case "runtime.controlStateChanged": {
      if (
        action.leaseId !== null
        && action.controlRevision < state.control.controlRevision
      ) return state;
      const previousApproval = action.pendingInput?.kind === "approval"
        && state.pendingApproval?.requestId === action.pendingInput.requestId
        ? state.pendingApproval
        : null;
      const pendingApproval = action.pendingInput?.kind === "approval"
        ? {
            requestId: action.pendingInput.requestId,
            title: action.pendingInput.title,
            description: action.pendingInput.description,
            command: action.pendingInput.command,
            choices: action.pendingInput.choices,
            expiresAtEpochMs: action.pendingInput.expiresAtEpochMs,
            controlRevision: action.controlRevision,
            resolution: action.controlRevision === state.control.controlRevision
              ? previousApproval?.resolution ?? null
              : null,
            confirmationChoice: action.controlRevision === state.control.controlRevision
              ? previousApproval?.confirmationChoice ?? null
              : null,
          }
        : null;
      const previousClarification = action.pendingInput?.kind === "clarify"
        && state.pendingClarification?.requestId === action.pendingInput.requestId
        ? state.pendingClarification
        : null;
      const pendingClarification = action.pendingInput?.kind === "clarify"
        ? {
            requestId: action.pendingInput.requestId,
            question: action.pendingInput.question,
            choices: action.pendingInput.choices,
            allowOther: action.pendingInput.allowOther,
            expiresAtEpochMs: action.pendingInput.expiresAtEpochMs,
            controlRevision: action.controlRevision,
            otherDraft: previousClarification?.otherDraft ?? "",
            resolution: action.controlRevision === state.control.controlRevision
              ? previousClarification?.resolution ?? null
              : null,
          }
        : null;
      const conversation = action.pendingInput === null
        ? state.conversation
        : upsertStableEvent(state.conversation, {
            id: `runtime-input:${action.pendingInput.kind}:${action.pendingInput.requestId}`,
            kind: "input",
            label: "Input",
            time: "",
            body: action.pendingInput.kind === "approval"
              ? action.pendingInput.title
              : "Input required · Clarification",
            status: "waiting",
          });
      return {
        ...state,
        controller: action.leaseId !== null,
        control: {
          ...state.control,
          leaseId: action.leaseId,
          runtimeSessionId: action.runtimeSessionId,
          leaseExpiresAtEpochMs: action.leaseExpiresAtEpochMs,
          controlRevision: Math.max(state.control.controlRevision, action.controlRevision),
          controllerKind: action.controllerKind,
          controllerLabel: action.controllerLabel,
          unavailableReason: action.unavailableReason,
        },
        pendingApproval,
        pendingClarification,
        conversation,
        longEvents: projectLongEvents(conversation),
        commandLock: action.controlRevision > state.control.controlRevision ? null : state.commandLock,
      };
    }
    case "runtime.subscriptionInstalled": {
      const runtimeChanged = state.runtimeSessionId !== null
        && state.runtimeSessionId !== action.runtimeSessionId;
      const conversation: ConversationEvent[] = action.messages.map((message, index) =>
        projectSnapshotMessage(message, index),
      );
      const lifecycleEvents = projectSnapshotLifecycle(
        action.todoSections ?? [],
        action.subagents ?? [],
        action.tools ?? [],
        action.terminals ?? [],
      );
      conversation.push(...lifecycleEvents);
      if (action.inflight.user !== null) conversation.push({
        id: "inflight-user",
        kind: "user",
        label: "You",
        time: "",
        body: action.inflight.user,
        status: "active",
      });
      if (action.inflight.assistant !== null) conversation.push({
        id: "inflight-assistant",
        kind: "assistant",
        label: "Hermes",
        time: "",
        body: action.inflight.assistant,
        status: action.inflight.streaming ? "active" : "complete",
      });
      return {
        ...state,
        connection: "connected",
        runtimeSessionId: action.runtimeSessionId,
        runtimeGeneration: action.runtimeGeneration ?? null,
        observerContract: action.observerContract ?? 1,
        outputParityAvailable: action.observerContract === 2,
        running: action.running,
        controller: runtimeChanged ? false : state.controller,
        control: runtimeChanged
          ? {
              ...state.control,
              leaseId: null,
              runtimeSessionId: action.runtimeSessionId,
              leaseExpiresAtEpochMs: 0,
              controlRevision: 0,
              controllerKind: null,
              controllerLabel: null,
              unavailableReason: "Waiting for the new runtime controller lease.",
            }
          : state.control,
        pendingApproval: runtimeChanged ? null : state.pendingApproval,
        pendingClarification: runtimeChanged ? null : state.pendingClarification,
        commandLock: runtimeChanged ? null : state.commandLock,
        conversation,
        subagents: (action.subagents ?? [])
          .map(projectSnapshotSubagent)
          .sort((left, right) => (
            (left.firstEventSequence ?? 0) - (right.firstEventSequence ?? 0) || left.id.localeCompare(right.id)
          )),
        longEvents: projectLongEvents(conversation),
        longLineCount: countLines(conversation),
      };
    }
    case "runtime.eventReceived": {
      const conversation = projectRuntimeEvent(state.conversation, action);
      const subagents = action.eventType === "subagent.update"
        ? projectSubagentUpdate(state.subagents, action.payload)
        : state.subagents;
      const running = action.eventType === "status.update" && typeof action.payload.running === "boolean"
        ? action.payload.running
        : state.running;
      return {
        ...state,
        running,
        conversation,
        subagents,
        longEvents: projectLongEvents(conversation),
        longLineCount: countLines(conversation),
      };
    }
    case "subagents.draftChanged":
      return { ...state, subagentDraft: action.value };
    case "composer.draftChanged":
      return { ...state, composerDraft: action.value };
  }
}

function snapshotKind(role: string): ConversationEvent["kind"] {
  if (role === "user") return "user";
  if (role === "assistant") return "assistant";
  if (role === "tool") return "tool";
  return "status";
}

function projectSnapshotMessage(
  message: { role: string; content?: string | null },
  index: number,
): ConversationEvent {
  const kind = snapshotKind(message.role);
  const content = message.content ?? "";
  if (kind === "tool") {
    return {
      id: `snapshot-${index}`,
      kind,
      label: "Tool",
      time: "",
      body: "Historical tool output",
      details: content,
      expanded: false,
      status: "complete",
    };
  }
  return {
    id: `snapshot-${index}`,
    kind,
    label: snapshotLabel(message.role),
    time: "",
    body: content,
    status: "complete",
  };
}

function snapshotLabel(role: string): string {
  if (role === "user") return "You";
  if (role === "assistant") return "Hermes";
  if (role === "tool") return "Tool";
  return "Status";
}

function projectRuntimeEvent(
  conversation: readonly ConversationEvent[],
  action: Extract<HermesWebAction, { type: "runtime.eventReceived" }>,
): readonly ConversationEvent[] {
  const text = typeof action.payload.text === "string" ? action.payload.text : "";
  const sequence = action.eventSequence;
  if (action.eventType === "todo.update") {
    return projectTodoUpdate(conversation, action.payload, sequence);
  }
  if (action.eventType === "tool.update") {
    return projectToolUpdate(conversation, action.payload, sequence);
  }
  if (action.eventType === "terminal.update") {
    return projectTerminalUpdate(conversation, action.payload, sequence);
  }
  if (action.eventType === "subagent.update") {
    return projectSubagentConversationUpdate(conversation, action.payload, sequence);
  }
  if (action.eventType === "message.start") {
    const messageId = nonEmptyText(action.payload.message_id) ?? String(sequence);
    return appendEvent(conversation, {
      id: `runtime-message:${messageId}`,
      kind: "assistant",
      label: "Hermes",
      time: "",
      body: "",
      status: "active",
      eventSequence: sequence,
    });
  }
  if (action.eventType === "message.delta") {
    return appendContiguous(conversation, "assistant", "Hermes", text, sequence);
  }
  if (action.eventType === "message.complete") {
    const last = conversation.at(-1);
    if (last?.kind === "assistant" && last.status === "active") {
      const body = "text" in action.payload ? text : last.body ?? "";
      return replaceAt(conversation, conversation.length - 1, {
        ...last,
        body,
        status: action.payload.status === "error" ? "stopped" : "complete",
      });
    }
    return appendEvent(conversation, {
      id: `runtime-event:${sequence}`,
      kind: "assistant",
      label: "Hermes",
      time: "",
      body: text,
      status: action.payload.status === "error" ? "stopped" : "complete",
      eventSequence: sequence,
    });
  }
  if (action.eventType === "thinking.delta" || action.eventType === "reasoning.delta") {
    return appendContiguous(conversation, "thinking", "Thinking", text, sequence);
  }
  if (action.eventType === "tool.output.delta") {
    return appendStableOutput(
      conversation,
      "tool",
      nonEmptyText(action.payload.tool_call_id),
      nonEmptyText(action.payload.tool_name) ?? "Tool",
      text,
      sequence,
      nonEmptyText(action.payload.turn_id),
    );
  }
  if (action.eventType === "agent.terminal.output") {
    return appendStableOutput(
      conversation,
      "terminal",
      nonEmptyText(action.payload.process_id),
      "Terminal",
      text,
      sequence,
      nonEmptyText(action.payload.turn_id),
    );
  }
  if (action.eventType === "status.update") {
    const status = nonEmptyText(action.payload.status) ?? "Status update";
    return appendEvent(conversation, {
      id: `runtime-event:${sequence}`,
      kind: "status",
      label: "Status",
      time: "",
      body: text.length > 0 ? text : status,
      status: action.payload.running === true ? "active" : "complete",
      eventSequence: sequence,
    });
  }
  return conversation;
}

function appendContiguous(
  conversation: readonly ConversationEvent[],
  kind: ConversationEvent["kind"],
  label: string,
  text: string,
  sequence: number,
): readonly ConversationEvent[] {
  const last = conversation.at(-1);
  if (last?.kind === kind && last.status === "active") {
    return replaceAt(conversation, conversation.length - 1, {
      ...last,
      body: `${last.body ?? ""}${text}`,
    });
  }
  return appendEvent(conversation, {
    id: `runtime-event:${sequence}`,
    kind,
    label,
    time: "",
    body: text,
    status: "active",
    eventSequence: sequence,
  });
}

function appendStableOutput(
  conversation: readonly ConversationEvent[],
  kind: "tool" | "terminal",
  sourceId: string | null,
  label: string,
  text: string,
  sequence: number,
  turnId: string | null = null,
): readonly ConversationEvent[] {
  const id = sourceId === null
    ? `runtime-event:${sequence}`
    : turnId === null
      ? `runtime-${kind}:${sourceId}`
      : `runtime-${kind}:${turnId}:${sourceId}`;
  const index = conversation.findIndex((event) => event.id === id);
  if (index >= 0) {
    const current = conversation[index];
    return replaceAt(conversation, index, {
      ...current,
      details: `${current.details ?? ""}${text}`,
    });
  }
  return appendEvent(conversation, {
    id,
    kind,
    label,
    time: "",
    body: label,
    details: text,
    status: "active",
    expanded: false,
    eventSequence: sequence,
  });
}

function projectSnapshotLifecycle(
  todoSections: readonly RuntimeTodoSection[],
  subagents: readonly RuntimeSubagentProjection[],
  tools: readonly RuntimeToolProjection[],
  terminals: readonly RuntimeTerminalProjection[],
): ConversationEvent[] {
  return [
    ...todoSections.map(todoEvent),
    ...subagents.map(subagentEvent),
    ...tools.map(toolEvent),
    ...terminals.map(terminalEvent),
  ].sort((left, right) => (
    (left.eventSequence ?? 0) - (right.eventSequence ?? 0) || left.id.localeCompare(right.id)
  ));
}

function projectSnapshotSubagent(
  value: RuntimeSubagentProjection,
): SubagentItem {
  return {
    id: `${value.turnId}:${value.subagentId}`,
    name: value.name,
    role: value.parentSubagentId === null ? "coordinator" : "child",
    goal: value.goal,
    summary: value.summary ?? "",
    time: "",
    status: lifecycleStatus(value.status),
    ...(value.parentSubagentId === null ? {} : { parentId: `${value.turnId}:${value.parentSubagentId}` }),
    revision: value.revision,
    firstEventSequence: value.firstEventSequence,
    turnId: value.turnId,
  };
}

function projectSubagentUpdate(
  subagents: readonly SubagentItem[],
  payload: Record<string, unknown>,
): readonly SubagentItem[] {
  const turnId = nonEmptyText(payload.turn_id);
  const subagentId = nonEmptyText(payload.subagent_id);
  if (turnId === null || subagentId === null) return subagents;
  const id = `${turnId}:${subagentId}`;
  if (payload.operation === "delete") return subagents.filter((agent) => agent.id !== id);
  const name = nonEmptyText(payload.name);
  if (name === null || typeof payload.status !== "string") return subagents;
  const projected = projectSnapshotSubagent({
    turnId,
    subagentId,
    revision: numeric(payload.revision),
    firstEventSequence: numeric(payload.first_event_sequence),
    parentSubagentId: typeof payload.parent_subagent_id === "string" ? payload.parent_subagent_id : null,
    name,
    goal: typeof payload.goal === "string" ? payload.goal : "",
    summary: typeof payload.summary === "string" ? payload.summary : null,
    status: payload.status as RuntimeSubagentProjection["status"],
  });
  const index = subagents.findIndex((agent) => agent.id === id);
  return index < 0
    ? [...subagents, projected]
    : subagents.map((agent, currentIndex) => currentIndex === index ? projected : agent);
}

function todoEvent(value: RuntimeTodoSection): ConversationEvent {
  const status = todoStatus(value.status);
  return {
    id: `runtime-todo:${value.turnId}:${value.sectionId}`,
    kind: "todo",
    label: "Todo",
    time: "",
    count: value.items.length,
    status,
    items: value.items,
    expanded: status === "active" || status === "queued",
    eventSequence: value.firstEventSequence,
    revision: value.revision,
    turnId: value.turnId,
  };
}

function toolEvent(value: RuntimeToolProjection): ConversationEvent {
  return {
    id: `runtime-tool:${value.turnId}:${value.toolCallId}`,
    kind: "tool",
    label: value.name,
    time: "",
    body: value.callLabel ?? value.name,
    ...(value.summary === undefined ? {} : { details: value.summary }),
    status: lifecycleStatus(value.status),
    expanded: false,
    eventSequence: value.firstEventSequence,
    revision: value.revision,
    turnId: value.turnId,
  };
}

function terminalEvent(value: RuntimeTerminalProjection): ConversationEvent {
  return {
    id: `runtime-terminal:${value.turnId}:${value.processId}`,
    kind: "terminal",
    label: "Terminal",
    time: "",
    body: "Terminal",
    ...(value.summary === undefined ? {} : { details: value.summary }),
    status: lifecycleStatus(value.status),
    expanded: false,
    eventSequence: value.firstEventSequence,
    revision: value.revision,
    turnId: value.turnId,
  };
}

function subagentEvent(value: RuntimeSubagentProjection): ConversationEvent {
  return {
    id: `runtime-subagent:${value.turnId}:${value.subagentId}`,
    kind: "subagent",
    label: "Subagent",
    time: "",
    body: `${value.name}: ${value.summary ?? subagentStatusLabel(value.status)}`,
    status: lifecycleStatus(value.status),
    eventSequence: value.firstEventSequence,
    revision: value.revision,
    turnId: value.turnId,
  };
}

function projectSubagentConversationUpdate(
  conversation: readonly ConversationEvent[],
  payload: Record<string, unknown>,
  sequence: number,
): readonly ConversationEvent[] {
  const turnId = nonEmptyText(payload.turn_id);
  const subagentId = nonEmptyText(payload.subagent_id);
  if (turnId === null || subagentId === null) return conversation;
  const id = `runtime-subagent:${turnId}:${subagentId}`;
  if (payload.operation === "delete") return conversation.filter((event) => event.id !== id);
  const name = nonEmptyText(payload.name);
  if (name === null || typeof payload.status !== "string") return conversation;
  return upsertStableEvent(conversation, subagentEvent({
    turnId,
    subagentId,
    revision: numeric(payload.revision),
    firstEventSequence: numeric(payload.first_event_sequence, sequence),
    parentSubagentId: typeof payload.parent_subagent_id === "string" ? payload.parent_subagent_id : null,
    name,
    goal: typeof payload.goal === "string" ? payload.goal : "",
    summary: typeof payload.summary === "string" ? payload.summary : null,
    status: payload.status as RuntimeSubagentProjection["status"],
  }));
}

function projectTodoUpdate(
  conversation: readonly ConversationEvent[],
  payload: Record<string, unknown>,
  sequence: number,
): readonly ConversationEvent[] {
  const turnId = nonEmptyText(payload.turn_id);
  const sectionId = nonEmptyText(payload.section_id);
  if (turnId === null || sectionId === null) return conversation;
  const id = `runtime-todo:${turnId}:${sectionId}`;
  if (payload.operation === "delete") return conversation.filter((event) => event.id !== id);
  if (!Array.isArray(payload.items) || typeof payload.status !== "string") return conversation;
  const event = todoEvent({
    turnId,
    sectionId,
    revision: numeric(payload.revision),
    firstEventSequence: numeric(payload.first_event_sequence, sequence),
    status: payload.status as RuntimeTodoSection["status"],
    items: payload.items as RuntimeTodoSection["items"],
  });
  return upsertStableEvent(conversation, event);
}

function projectToolUpdate(
  conversation: readonly ConversationEvent[],
  payload: Record<string, unknown>,
  sequence: number,
): readonly ConversationEvent[] {
  const turnId = nonEmptyText(payload.turn_id);
  const toolCallId = nonEmptyText(payload.tool_call_id);
  if (turnId === null || toolCallId === null) return conversation;
  const id = `runtime-tool:${turnId}:${toolCallId}`;
  if (payload.operation === "delete") return conversation.filter((event) => event.id !== id);
  const name = nonEmptyText(payload.name);
  if (name === null || typeof payload.status !== "string") return conversation;
  const event = toolEvent({
    turnId,
    toolCallId,
    revision: numeric(payload.revision),
    firstEventSequence: numeric(payload.first_event_sequence, sequence),
    status: payload.status as RuntimeToolProjection["status"],
    name,
    ...(typeof payload.call_label === "string" ? { callLabel: payload.call_label } : {}),
    ...(typeof payload.summary === "string" ? { summary: payload.summary } : {}),
  });
  const current = conversation.find((candidate) => candidate.id === id);
  event.details = mergeLifecycleDetails(current?.details, event.details);
  return upsertStableEvent(conversation, event);
}

function projectTerminalUpdate(
  conversation: readonly ConversationEvent[],
  payload: Record<string, unknown>,
  sequence: number,
): readonly ConversationEvent[] {
  const turnId = nonEmptyText(payload.turn_id);
  const processId = nonEmptyText(payload.process_id);
  if (turnId === null || processId === null) return conversation;
  const id = `runtime-terminal:${turnId}:${processId}`;
  if (payload.operation === "delete") return conversation.filter((event) => event.id !== id);
  if (typeof payload.status !== "string") return conversation;
  const event = terminalEvent({
    turnId,
    processId,
    revision: numeric(payload.revision),
    firstEventSequence: numeric(payload.first_event_sequence, sequence),
    status: payload.status as RuntimeTerminalProjection["status"],
    ...(typeof payload.summary === "string" ? { summary: payload.summary } : {}),
    ...(typeof payload.exit_code === "number" ? { exitCode: payload.exit_code } : {}),
  });
  const current = conversation.find((candidate) => candidate.id === id);
  event.details = mergeLifecycleDetails(current?.details, event.details);
  return upsertStableEvent(conversation, event);
}

function upsertStableEvent(
  conversation: readonly ConversationEvent[],
  event: ConversationEvent,
): readonly ConversationEvent[] {
  const index = conversation.findIndex((candidate) => candidate.id === event.id);
  return index < 0 ? [...conversation, event] : replaceAt(conversation, index, event);
}

function todoStatus(status: RuntimeTodoSection["status"]): ConversationEvent["status"] {
  if (status === "in_progress") return "active";
  if (status === "pending") return "queued";
  if (status === "completed") return "complete";
  return "stopped";
}

function lifecycleStatus(status: string): WorkStatus {
  if (status === "running") return "active";
  if (status === "queued") return "queued";
  if (status === "waiting" || status === "unknown") return "waiting";
  if (status === "completed") return "complete";
  return "stopped";
}

function subagentStatusLabel(status: RuntimeSubagentProjection["status"]): string {
  if (status === "running") return "Running";
  if (status === "queued") return "Queued";
  if (status === "waiting") return "Waiting";
  if (status === "completed") return "Completed";
  if (status === "failed") return "Failed";
  return "Interrupted";
}

function numeric(value: unknown, fallback = 1): number {
  return Number.isSafeInteger(value) ? value as number : fallback;
}

function mergeLifecycleDetails(current: string | undefined, update: string | undefined): string | undefined {
  if (current === undefined || current.length === 0) return update;
  if (update === undefined || update.length === 0 || update === current) return current;
  return `${current}\n${update}`;
}

function appendEvent(
  conversation: readonly ConversationEvent[],
  event: ConversationEvent,
): readonly ConversationEvent[] {
  return [...conversation, event];
}

function replaceAt(
  conversation: readonly ConversationEvent[],
  index: number,
  event: ConversationEvent,
): readonly ConversationEvent[] {
  return conversation.map((current, currentIndex) => currentIndex === index ? event : current);
}

function nonEmptyText(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function projectLongEvents(conversation: readonly ConversationEvent[]) {
  return conversation.map((event) => ({
    id: event.id,
    time: event.time,
    actor: event.label,
    summary: event.body ?? event.label,
    tone: event.status === "stopped"
      ? "red" as const
      : event.kind === "subagent" && (event.status === "active" || event.status === "complete")
        ? "green" as const
        : event.kind === "subagent" && event.status === "queued"
          ? "amber" as const
      : event.kind === "input"
        ? "cyan" as const
        : event.kind === "tool" || event.kind === "terminal"
          ? "neutral" as const
          : "neutral" as const,
  }));
}

function countLines(conversation: readonly ConversationEvent[]): number {
  return conversation.reduce((count, event) => count + Math.max(1, (event.body ?? "").split("\n").length), 0);
}
