import { ObserverV2ProjectionGuard } from "./observerV2ProjectionGuard";

describe("observer v2 projection revision guard", () => {
  it("requires terminal state before delete and retains tombstones until the next snapshot", () => {
    const guard = guardWith({ todoStatus: "in_progress" });
    expect(guard.apply(todoDelete(5, 2))).toBe(false);

    expect(guard.apply(todoUpsert(5, 2, "completed"))).toBe(true);
    expect(guard.apply(todoDelete(6, 3))).toBe(true);
    expect(guard.apply(todoUpsert(7, 4, "completed"))).toBe(false);
  });

  it("allows terminal metadata enrichment but rejects identity semantics changes", () => {
    const guard = guardWith({ toolStatus: "completed" });
    expect(guard.apply(toolUpsert(5, 2, { name: "Different name" }))).toBe(false);
    expect(guard.apply(toolUpsert(5, 2, { name: "Tests", duration_ms: 120 }))).toBe(true);
  });

  it("rejects deleting a terminal subagent while it still owns a live child", () => {
    const guard = new ObserverV2ProjectionGuard();
    expect(guard.installSnapshot({
      snapshotEventSequence: 2,
      todoSections: [],
      tools: [],
      terminals: [],
      subagents: [
        subagent("root", null, "completed", 1),
        subagent("child", "root", "running", 2),
      ],
    })).toBe(true);

    expect(guard.apply({
      type: "subagent.update",
      eventSequence: 3,
      payload: {
        turn_id: "turn-1",
        subagent_id: "root",
        revision: 2,
        first_event_sequence: 1,
        operation: "delete",
      },
    })).toBe(false);
  });

  it("accepts an initial revision whose first event sequence precedes its upsert", () => {
    const guard = new ObserverV2ProjectionGuard();
    expect(guard.installSnapshot({
      snapshotEventSequence: 4,
      todoSections: [],
      subagents: [],
      tools: [],
      terminals: [],
    })).toBe(true);

    expect(guard.apply({
      type: "tool.update",
      eventSequence: 5,
      payload: {
        turn_id: "turn-1",
        tool_call_id: "tool-late",
        revision: 1,
        first_event_sequence: 2,
        operation: "upsert",
        status: "running",
        name: "Late projection",
      },
    })).toBe(true);
  });

  it("keeps terminal exit codes immutable while allowing safe metadata enrichment", () => {
    const guard = new ObserverV2ProjectionGuard();
    expect(guard.installSnapshot({
      snapshotEventSequence: 4,
      todoSections: [],
      subagents: [],
      tools: [],
      terminals: [{
        turn_id: "turn-1",
        process_id: "process-1",
        revision: 1,
        first_event_sequence: 2,
        status: "failed",
        exit_code: 1,
      }],
    })).toBe(true);

    expect(guard.apply(terminalUpsert(5, 2, 2, { summary: "Still failed" }))).toBe(false);
    expect(guard.apply(terminalUpsert(5, 2, 1, { summary: "Still failed", duration_ms: 120 }))).toBe(true);
  });

  it("retains todo item identity and order, absorbs terminal items, and only appends new items", () => {
    const guard = new ObserverV2ProjectionGuard();
    expect(guard.installSnapshot({
      snapshotEventSequence: 4,
      subagents: [],
      tools: [],
      terminals: [],
      todoSections: [{
        turn_id: "turn-1",
        section_id: "todo-1",
        revision: 1,
        first_event_sequence: 1,
        status: "in_progress",
        items: [
          { id: "item-1", label: "Finished", status: "completed" },
          { id: "item-2", label: "Pending", status: "pending" },
        ],
      }],
    })).toBe(true);

    expect(guard.apply(todoItemsUpsert(5, 2, [
      { id: "item-1", label: "Finished", status: "pending" },
      { id: "item-2", label: "Pending", status: "in_progress" },
    ]))).toBe(false);
    expect(guard.apply(todoItemsUpsert(5, 2, [
      { id: "replacement", label: "Finished", status: "completed" },
      { id: "item-2", label: "Pending", status: "in_progress" },
    ]))).toBe(false);
    expect(guard.apply(todoItemsUpsert(5, 2, [
      { id: "item-1", label: "Finished", status: "completed" },
    ]))).toBe(false);
    expect(guard.apply(todoItemsUpsert(5, 2, [
      { id: "item-1", label: "Finished", status: "completed" },
      { id: "item-2", label: "Pending", status: "in_progress" },
      { id: "item-3", label: "Appended", status: "pending" },
    ]))).toBe(true);
  });
});

function guardWith(options: { todoStatus?: string; toolStatus?: string }) {
  const guard = new ObserverV2ProjectionGuard();
  expect(guard.installSnapshot({
    snapshotEventSequence: 4,
    todoSections: [{
      turn_id: "turn-1",
      section_id: "todo-1",
      revision: 1,
      first_event_sequence: 1,
      status: options.todoStatus ?? "completed",
      items: [{ id: "item-1", label: "Run tests", status: options.todoStatus ?? "completed" }],
    }],
    subagents: [],
    tools: [{
      turn_id: "turn-1",
      tool_call_id: "tool-1",
      revision: 1,
      first_event_sequence: 3,
      status: options.toolStatus ?? "running",
      name: "Tests",
      summary: "Done",
    }],
    terminals: [],
  })).toBe(true);
  return guard;
}

function todoUpsert(eventSequence: number, revision: number, status: string) {
  return {
    type: "todo.update",
    eventSequence,
    payload: {
      turn_id: "turn-1",
      section_id: "todo-1",
      revision,
      first_event_sequence: 1,
      operation: "upsert",
      status,
      items: [{ id: "item-1", label: "Run tests", status }],
    },
  };
}

function todoDelete(eventSequence: number, revision: number) {
  return {
    type: "todo.update",
    eventSequence,
    payload: {
      turn_id: "turn-1",
      section_id: "todo-1",
      revision,
      first_event_sequence: 1,
      operation: "delete",
    },
  };
}

function toolUpsert(
  eventSequence: number,
  revision: number,
  metadata: { name: string; duration_ms?: number },
) {
  return {
    type: "tool.update",
    eventSequence,
    payload: {
      turn_id: "turn-1",
      tool_call_id: "tool-1",
      revision,
      first_event_sequence: 3,
      operation: "upsert",
      status: "completed",
      summary: "Done",
      ...metadata,
    },
  };
}

function subagent(id: string, parent: string | null, status: string, sequence: number) {
  return {
    turn_id: "turn-1",
    subagent_id: id,
    revision: 1,
    first_event_sequence: sequence,
    parent_subagent_id: parent,
    name: id,
    goal: "Run tests",
    summary: null,
    status,
  };
}

function terminalUpsert(
  eventSequence: number,
  revision: number,
  exitCode: number,
  metadata: { summary?: string; duration_ms?: number },
) {
  return {
    type: "terminal.update",
    eventSequence,
    payload: {
      turn_id: "turn-1",
      process_id: "process-1",
      revision,
      first_event_sequence: 2,
      operation: "upsert",
      status: "failed",
      exit_code: exitCode,
      ...metadata,
    },
  };
}

function todoItemsUpsert(
  eventSequence: number,
  revision: number,
  items: Array<{ id: string; label: string; status: string }>,
) {
  return {
    type: "todo.update",
    eventSequence,
    payload: {
      turn_id: "turn-1",
      section_id: "todo-1",
      revision,
      first_event_sequence: 1,
      operation: "upsert",
      status: "in_progress",
      items,
    },
  };
}
