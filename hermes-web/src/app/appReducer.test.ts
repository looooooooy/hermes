import { createPreviewFixture } from "../dev/fixtures";
import { createProductionState } from "./productionState";
import { appReducer } from "./appReducer";

describe("Hermes Web interaction state", () => {
  it("switches between the three mobile-parity views", () => {
    const initial = createPreviewFixture();
    const subagents = appReducer(initial, { type: "view.selected", view: "subagents" });
    const long = appReducer(subagents, { type: "view.selected", view: "long" });

    expect(subagents.activeView).toBe("subagents");
    expect(long.activeView).toBe("long");
  });

  it("expands the queued work without changing canonical ordering", () => {
    const initial = createPreviewFixture();
    const next = appReducer(initial, { type: "queue.toggled" });

    expect(next.queueExpanded).toBe(true);
    expect(next.conversation.map((event) => event.id)).toEqual(
      initial.conversation.map((event) => event.id),
    );
  });

  it.each(["allow_once", "deny"] as const)(
    "accepts server-confirmed approval choice %s exactly once and rejects stale revisions",
    (choice) => {
      const initial = createPreviewFixture();
      const resolved = appReducer(initial, {
        type: "approval.confirmed",
        choice,
        clientRequestId: "request-1",
        controlRevision: 8,
      });
      const ignored = appReducer(resolved, {
        type: "approval.confirmed",
        choice: "deny",
        clientRequestId: "request-2",
        controlRevision: 8,
      });

      expect(resolved.pendingApproval?.resolution).toBe(choice);
      expect(ignored).toBe(resolved);
    },
  );

  it("appends authoritative stream events to an occurrence-stable runtime segment", () => {
    const initial = createPreviewFixture();
    const next = appReducer(initial, {
      type: "runtime.eventReceived",
      eventType: "message.delta",
      eventSequence: 12,
      payload: { text: "Validation is now green." },
    });

    expect(next.conversation.at(-1)?.body).toMatch(
      /Validation is now green\.$/,
    );
    expect(next.conversation).toHaveLength(initial.conversation.length);
  });

  it("preserves event_sequence order across assistant, thinking, tool, terminal, and status segments", () => {
    const subscribed = appReducer(createProductionState("mobile-56f3"), {
      type: "runtime.subscriptionInstalled",
      runtimeSessionId: "runtime-A",
      running: true,
      status: "running",
      messages: [
        { role: "user", content: "Start" },
        { role: "tool", content: "Historic tool result" },
      ],
      inflight: { user: null, assistant: null, streaming: false, error: null },
    });
    const events = [
      ["message.start", 1, { message_id: "assistant-1", role: "assistant" }],
      ["message.delta", 2, { text: "Before tool" }],
      ["thinking.delta", 3, { text: "Inspect" }],
      ["tool.output.delta", 4, { tool_call_id: "tool-1", tool_name: "Terminal", text: "A" }],
      ["message.delta", 5, { text: "After tool" }],
      ["tool.output.delta", 6, { tool_call_id: "tool-1", tool_name: "Terminal", text: "B" }],
      ["agent.terminal.output", 7, { process_id: "process-1", text: "stdout" }],
      ["status.update", 8, { status: "waiting", running: true, text: "Waiting for input" }],
    ] as const;
    const projected = events.reduce((state, [eventType, eventSequence, payload]) => appReducer(state, {
      type: "runtime.eventReceived",
      eventType,
      eventSequence,
      payload,
    }), subscribed);

    expect(projected.conversation.map((event) => [event.kind, event.details ?? event.body])).toEqual([
      ["user", "Start"],
      ["tool", "Historic tool result"],
      ["assistant", "Before tool"],
      ["thinking", "Inspect"],
      ["tool", "AB"],
      ["assistant", "After tool"],
      ["terminal", "stdout"],
      ["status", "Waiting for input"],
    ]);
    expect(projected.longEvents.map((event) => event.actor)).toEqual([
      "You",
      "Tool",
      "Hermes",
      "Thinking",
      "Terminal",
      "Hermes",
      "Terminal",
      "Status",
    ]);
  });

  it("keeps authoritative historic, live tool, and terminal output in stable collapsed disclosure nodes", () => {
    const subscribed = appReducer(createProductionState("mobile-56f3"), {
      type: "runtime.subscriptionInstalled",
      runtimeSessionId: "runtime-A",
      running: true,
      status: "running",
      messages: [{ role: "tool", content: "Historic tool result" }],
      inflight: { user: null, assistant: null, streaming: false, error: null },
    });
    const started = appReducer(subscribed, {
      type: "runtime.eventReceived",
      eventType: "tool.output.delta",
      eventSequence: 1,
      payload: { tool_call_id: "tool-1", tool_name: "Terminal", text: "first" },
    });
    const updated = appReducer(started, {
      type: "runtime.eventReceived",
      eventType: "tool.output.delta",
      eventSequence: 2,
      payload: { tool_call_id: "tool-1", tool_name: "Terminal", text: " second" },
    });
    const terminalStarted = appReducer(updated, {
      type: "runtime.eventReceived",
      eventType: "agent.terminal.output",
      eventSequence: 3,
      payload: { process_id: "process-1", text: "stdout one" },
    });
    const terminalUpdated = appReducer(terminalStarted, {
      type: "runtime.eventReceived",
      eventType: "agent.terminal.output",
      eventSequence: 4,
      payload: { process_id: "process-1", text: " stdout two" },
    });

    expect(terminalUpdated.conversation).toEqual([
      expect.objectContaining({
        kind: "tool",
        label: "Tool",
        details: "Historic tool result",
        expanded: false,
        status: "complete",
      }),
      expect.objectContaining({
        id: "runtime-tool:tool-1",
        kind: "tool",
        label: "Terminal",
        details: "first second",
        expanded: false,
        status: "active",
      }),
      expect.objectContaining({
        id: "runtime-terminal:process-1",
        kind: "terminal",
        label: "Terminal",
        details: "stdout one stdout two",
        expanded: false,
        status: "active",
      }),
    ]);
  });

  it("atomically installs and orders the observer v2 output-parity snapshot baseline", () => {
    const installed = appReducer(createProductionState("mobile-56f3"), {
      type: "runtime.subscriptionInstalled",
      observerContract: 2,
      runtimeGeneration: "generation-1",
      runtimeSessionId: "runtime-A",
      running: true,
      status: "running",
      messages: [{ role: "assistant", content: "Snapshot response" }],
      inflight: { user: null, assistant: null, streaming: false, error: null },
      todoSections: [{
        turnId: "turn-1",
        sectionId: "todo-1",
        revision: 1,
        firstEventSequence: 1,
        status: "in_progress",
        items: [{ id: "item-1", label: "Run tests", status: "in_progress" }],
      }],
      subagents: [{
        turnId: "turn-1",
        subagentId: "agent-1",
        revision: 1,
        firstEventSequence: 2,
        parentSubagentId: null,
        name: "Test runner",
        goal: "Run tests",
        summary: null,
        status: "running",
      }],
      tools: [{
        turnId: "turn-1",
        toolCallId: "tool-1",
        revision: 1,
        firstEventSequence: 3,
        status: "running",
        name: "Tests",
      }],
      terminals: [{
        turnId: "turn-1",
        processId: "process-1",
        revision: 1,
        firstEventSequence: 4,
        status: "running",
      }],
    });

    expect(installed.observerContract).toBe(2);
    expect(installed.outputParityAvailable).toBe(true);
    expect(installed.conversation.map((event) => [event.id, event.kind, event.status])).toEqual([
      ["snapshot-0", "assistant", "complete"],
      ["runtime-todo:turn-1:todo-1", "todo", "active"],
      ["runtime-subagent:turn-1:agent-1", "subagent", "active"],
      ["runtime-tool:turn-1:tool-1", "tool", "active"],
      ["runtime-terminal:turn-1:process-1", "terminal", "active"],
    ]);
    expect(installed.subagents).toEqual([
      expect.objectContaining({ id: "turn-1:agent-1", status: "active" }),
    ]);
    expect(installed.subagents[0]).not.toHaveProperty("parentId");
  });

  it("projects observer v2 subagent lifecycle into the flat conversation and long transcript", () => {
    const installed = appReducer(createProductionState("mobile-56f3"), {
      type: "runtime.subscriptionInstalled",
      observerContract: 2,
      runtimeGeneration: "generation-1",
      runtimeSessionId: "runtime-A",
      running: true,
      status: "running",
      messages: [],
      inflight: { user: null, assistant: null, streaming: false, error: null },
      todoSections: [],
      subagents: [{
        turnId: "turn-1",
        subagentId: "runner",
        revision: 1,
        firstEventSequence: 2,
        parentSubagentId: null,
        name: "Test runner",
        goal: "Run tests",
        summary: "Running focused tests.",
        status: "running",
      }],
      tools: [],
      terminals: [],
    });
    const queuedChild = appReducer(installed, {
      type: "runtime.eventReceived",
      eventType: "subagent.update",
      eventSequence: 5,
      payload: {
        turn_id: "turn-1",
        subagent_id: "analyzer",
        revision: 1,
        first_event_sequence: 5,
        operation: "upsert",
        parent_subagent_id: "runner",
        name: "Log analyzer",
        goal: "Inspect logs",
        summary: "Waiting for test results.",
        status: "queued",
      },
    });

    expect(installed.conversation).toContainEqual(expect.objectContaining({
      id: "runtime-subagent:turn-1:runner",
      kind: "subagent",
      label: "Subagent",
      body: "Test runner: Running focused tests.",
      status: "active",
      eventSequence: 2,
    }));
    expect(installed.longEvents).toContainEqual(expect.objectContaining({
      actor: "Subagent",
      summary: "Test runner: Running focused tests.",
      tone: "green",
    }));
    expect(queuedChild.conversation).toContainEqual(expect.objectContaining({
      id: "runtime-subagent:turn-1:analyzer",
      kind: "subagent",
      body: "Log analyzer: Waiting for test results.",
      status: "queued",
      eventSequence: 5,
    }));
    expect(queuedChild.longEvents).toContainEqual(expect.objectContaining({
      actor: "Subagent",
      summary: "Log analyzer: Waiting for test results.",
      tone: "amber",
    }));
  });

  it("stably replaces and deletes observer v2 lifecycle entities without moving their first occurrence", () => {
    const baseline = installV2Projection();
    const todoCompleted = appReducer(baseline, {
      type: "runtime.eventReceived",
      eventType: "todo.update",
      eventSequence: 5,
      payload: {
        turn_id: "turn-1",
        section_id: "todo-1",
        revision: 2,
        first_event_sequence: 1,
        operation: "upsert",
        status: "completed",
        items: [{ id: "item-1", label: "Run tests", status: "completed" }],
      },
    });
    const toolCompleted = appReducer(todoCompleted, {
      type: "runtime.eventReceived",
      eventType: "tool.update",
      eventSequence: 6,
      payload: {
        turn_id: "turn-1",
        tool_call_id: "tool-1",
        revision: 2,
        first_event_sequence: 3,
        operation: "upsert",
        status: "completed",
        name: "Tests",
        summary: "All tests passed",
      },
    });
    const todoDeleted = appReducer(toolCompleted, {
      type: "runtime.eventReceived",
      eventType: "todo.update",
      eventSequence: 7,
      payload: {
        turn_id: "turn-1",
        section_id: "todo-1",
        revision: 3,
        first_event_sequence: 1,
        operation: "delete",
      },
    });

    expect(todoCompleted.conversation.filter((event) => event.kind === "todo")).toHaveLength(1);
    expect(todoCompleted.conversation.find((event) => event.kind === "todo")).toMatchObject({
      id: "runtime-todo:turn-1:todo-1",
      status: "complete",
      revision: 2,
      eventSequence: 1,
    });
    expect(toolCompleted.conversation.find((event) => event.kind === "tool")).toMatchObject({
      id: "runtime-tool:turn-1:tool-1",
      status: "complete",
      revision: 2,
      details: "All tests passed",
      expanded: false,
    });
    expect(todoDeleted.conversation.some((event) => event.kind === "todo")).toBe(false);
  });

  it("retains streamed tool and terminal output when lifecycle completion adds a safe summary", () => {
    const baseline = installV2Projection();
    const toolOutput = appReducer(baseline, {
      type: "runtime.eventReceived",
      eventType: "tool.output.delta",
      eventSequence: 5,
      payload: { turn_id: "turn-1", tool_call_id: "tool-1", text: "test output" },
    });
    const terminalOutput = appReducer(toolOutput, {
      type: "runtime.eventReceived",
      eventType: "agent.terminal.output",
      eventSequence: 6,
      payload: { turn_id: "turn-1", process_id: "process-1", stream: "stdout", text: "process output" },
    });
    const completed = appReducer(terminalOutput, {
      type: "runtime.eventReceived",
      eventType: "tool.update",
      eventSequence: 7,
      payload: {
        turn_id: "turn-1",
        tool_call_id: "tool-1",
        revision: 2,
        first_event_sequence: 3,
        operation: "upsert",
        status: "completed",
        name: "Tests",
        summary: "All tests passed",
      },
    });
    const terminalCompleted = appReducer(completed, {
      type: "runtime.eventReceived",
      eventType: "terminal.update",
      eventSequence: 8,
      payload: {
        turn_id: "turn-1",
        process_id: "process-1",
        revision: 2,
        first_event_sequence: 4,
        operation: "upsert",
        status: "completed",
        exit_code: 0,
        summary: "Process completed",
      },
    });

    expect(completed.conversation.find((event) => event.kind === "tool")?.details).toBe(
      "test output\nAll tests passed",
    );
    expect(terminalCompleted.conversation.find((event) => event.kind === "terminal")?.details).toBe(
      "process output\nProcess completed",
    );
  });

  it("discards observer v2 projections on runtime rollover before installing the new baseline", () => {
    const baseline = installV2Projection();
    const rolled = appReducer(baseline, {
      type: "runtime.subscriptionInstalled",
      observerContract: 2,
      runtimeGeneration: "generation-2",
      runtimeSessionId: "runtime-B",
      running: false,
      status: "idle",
      messages: [],
      inflight: { user: null, assistant: null, streaming: false, error: null },
      todoSections: [],
      subagents: [],
      tools: [],
      terminals: [],
    });

    expect(rolled.runtimeGeneration).toBe("generation-2");
    expect(rolled.conversation).toEqual([]);
    expect(rolled.subagents).toEqual([]);
  });

  it("keeps v1 readable while marking output parity unavailable", () => {
    const installed = appReducer(createProductionState("mobile-56f3"), {
      type: "runtime.subscriptionInstalled",
      observerContract: 1,
      runtimeGeneration: null,
      runtimeSessionId: "runtime-A",
      running: false,
      status: "idle",
      messages: [{ role: "assistant", content: "V1 response" }],
      inflight: { user: null, assistant: null, streaming: false, error: null },
      todoSections: [],
      subagents: [],
      tools: [],
      terminals: [],
    });

    expect(installed.conversation[0].body).toBe("V1 response");
    expect(installed.observerContract).toBe(1);
    expect(installed.outputParityAvailable).toBe(false);
  });

  it("clears guidance only after command confirmation and stops only after runtime state", () => {
    const initial = createPreviewFixture();
    const drafted = appReducer(initial, { type: "subagents.draftChanged", value: "Check the failure" });
    const waiting = appReducer(drafted, {
      type: "command.started",
      kind: "steer",
      key: "steer",
      clientRequestId: "request-1",
    });
    const sent = appReducer(waiting, {
      type: "subagents.commandConfirmed",
      kind: "steer",
      clientRequestId: "request-1",
      message: "Guidance confirmed by Hermes",
    });
    const accepted = appReducer(sent, { type: "interrupt.confirmed", clientRequestId: "request-2" });
    const stopped = appReducer(accepted, { type: "runtime.runningChanged", running: false });

    expect(waiting.subagentDraft).toBe("Check the failure");
    expect(sent.subagentDraft).toBe("");
    expect(accepted.subagents.find((agent) => agent.status === "active")).toBeDefined();
    expect(stopped.subagents.find((agent) => agent.status === "active")).toBeUndefined();
  });

  it("keeps an unknown command tombstone while releasing its mutation lock", () => {
    const initial = createPreviewFixture();
    const waiting = appReducer(initial, {
      type: "command.started",
      kind: "prompt",
      key: "prompt",
      clientRequestId: "request-unknown",
    });

    const unknown = appReducer(waiting, {
      type: "command.unknown",
      kind: "prompt",
      clientRequestId: "request-unknown",
      message: "Delivery unknown; reconcile with Hermes before retrying.",
    });

    expect(unknown.commandFeedback).toEqual({
      kind: "prompt",
      status: "unknown",
      message: "Delivery unknown; reconcile with Hermes before retrying.",
      clientRequestId: "request-unknown",
    });
    expect(unknown.commandLock).toBeNull();
  });

  it("revokes the projected lease in the same reduction that installs a different runtime", () => {
    const initial = createPreviewFixture();
    const switched = appReducer(initial, {
      type: "runtime.subscriptionInstalled",
      runtimeSessionId: "runtime-B",
      running: false,
      status: "idle",
      messages: [],
      inflight: { user: null, assistant: null, streaming: false, error: null },
    });

    expect(switched.runtimeSessionId).toBe("runtime-B");
    expect(switched.controller).toBe(false);
    expect(switched.control).toMatchObject({
      leaseId: null,
      runtimeSessionId: "runtime-B",
      leaseExpiresAtEpochMs: 0,
      controlRevision: 0,
    });
    expect(switched.pendingApproval).toBeNull();
  });

  it("projects and clears clarification only from authoritative control snapshots", () => {
    const initial = { ...createPreviewFixture(), pendingApproval: null };
    const pending = appReducer(initial, {
      type: "runtime.controlStateChanged",
      leaseId: "preview-lease",
      runtimeSessionId: "runtime-56f3",
      leaseExpiresAtEpochMs: 1_900_000_000_000,
      controlRevision: 8,
      controllerKind: "mobile",
      controllerLabel: "Hermes Web",
      unavailableReason: null,
      pendingInput: {
        kind: "clarify",
        requestId: "clarify-1",
        question: "Which environment?",
        choices: [{ id: "staging", label: "Staging" }],
        allowOther: true,
        expiresAtEpochMs: 1_900_000_000_000,
      },
    });
    const cleared = appReducer(pending, {
      type: "runtime.controlStateChanged",
      leaseId: "preview-lease",
      runtimeSessionId: "runtime-56f3",
      leaseExpiresAtEpochMs: 1_900_000_000_000,
      controlRevision: 9,
      controllerKind: "mobile",
      controllerLabel: "Hermes Web",
      unavailableReason: null,
      pendingInput: null,
    });

    expect(pending.pendingClarification).toMatchObject({
      requestId: "clarify-1",
      controlRevision: 8,
      otherDraft: "",
    });
    expect(cleared.pendingClarification).toBeNull();
  });

  it("retains authoritative approval and clarification prompts in the long transcript", () => {
    const initial = appReducer(createProductionState("mobile-56f3"), {
      type: "runtime.subscriptionInstalled",
      observerContract: 2,
      runtimeGeneration: "generation-1",
      runtimeSessionId: "runtime-A",
      running: true,
      status: "running",
      messages: [],
      inflight: { user: null, assistant: null, streaming: false, error: null },
      todoSections: [],
      subagents: [],
      tools: [],
      terminals: [],
    });
    const pending = appReducer(initial, {
      type: "runtime.controlStateChanged",
      leaseId: "lease-1",
      runtimeSessionId: "runtime-A",
      leaseExpiresAtEpochMs: 1_900_000_000_000,
      controlRevision: 8,
      controllerKind: "mobile",
      controllerLabel: "Hermes Web",
      unavailableReason: null,
      pendingInput: {
        kind: "approval",
        requestId: "approval-1",
        title: "Input required · Approval",
        description: "Allow this operation?",
        command: "./gradlew test",
        choices: ["allow_once", "deny"],
        expiresAtEpochMs: 1_900_000_000_000,
      },
    });
    const cleared = appReducer(pending, {
      type: "runtime.controlStateChanged",
      leaseId: "lease-1",
      runtimeSessionId: "runtime-A",
      leaseExpiresAtEpochMs: 1_900_000_000_000,
      controlRevision: 9,
      controllerKind: "mobile",
      controllerLabel: "Hermes Web",
      unavailableReason: null,
      pendingInput: null,
    });

    expect(pending.conversation).toContainEqual(expect.objectContaining({
      id: "runtime-input:approval:approval-1",
      kind: "input",
      label: "Input",
      body: "Input required · Approval",
    }));
    expect(pending.longEvents).toContainEqual(expect.objectContaining({
      actor: "Input",
      summary: "Input required · Approval",
      tone: "cyan",
    }));
    expect(cleared.longEvents).toContainEqual(expect.objectContaining({
      id: "runtime-input:approval:approval-1",
      tone: "cyan",
    }));
  });
});

function installV2Projection() {
  return appReducer(createProductionState("mobile-56f3"), {
    type: "runtime.subscriptionInstalled",
    observerContract: 2,
    runtimeGeneration: "generation-1",
    runtimeSessionId: "runtime-A",
    running: true,
    status: "running",
    messages: [],
    inflight: { user: null, assistant: null, streaming: false, error: null },
    todoSections: [{
      turnId: "turn-1",
      sectionId: "todo-1",
      revision: 1,
      firstEventSequence: 1,
      status: "in_progress",
      items: [{ id: "item-1", label: "Run tests", status: "in_progress" }],
    }],
    subagents: [],
    tools: [{
      turnId: "turn-1",
      toolCallId: "tool-1",
      revision: 1,
      firstEventSequence: 3,
      status: "running",
      name: "Tests",
    }],
    terminals: [{
      turnId: "turn-1",
      processId: "process-1",
      revision: 1,
      firstEventSequence: 4,
      status: "running",
    }],
  });
}
