import parityPolicy from "./generated/observer-output-parity-v2.json";

type EntityKind = "todo" | "subagent" | "tool" | "terminal";

interface ProjectionEntity {
  kind: EntityKind;
  key: string;
  turnId: string;
  entityId: string;
  revision: number;
  firstEventSequence: number;
  status: string;
  digest: string;
  terminalCoreDigest: string;
  parentKey: string | null;
  deleted: boolean;
  value: Record<string, unknown>;
}

export interface ObserverV2SnapshotCollections {
  snapshotEventSequence: number;
  todoSections: readonly Record<string, unknown>[];
  subagents: readonly Record<string, unknown>[];
  tools: readonly Record<string, unknown>[];
  terminals: readonly Record<string, unknown>[];
}

export interface ObserverV2LifecycleEvent {
  type: string;
  eventSequence: number;
  payload: Record<string, unknown>;
}

export class ObserverV2ProjectionGuard {
  private readonly entities = new Map<string, ProjectionEntity>();

  installSnapshot(snapshot: ObserverV2SnapshotCollections): boolean {
    this.entities.clear();
    const collections: Array<[EntityKind, readonly Record<string, unknown>[]]> = [
      ["todo", snapshot.todoSections],
      ["subagent", snapshot.subagents],
      ["tool", snapshot.tools],
      ["terminal", snapshot.terminals],
    ];
    for (const [kind, values] of collections) {
      for (const value of values) {
        const entity = snapshotEntity(kind, value);
        if (
          entity === null
          || entity.firstEventSequence > snapshot.snapshotEventSequence
          || this.entities.has(entity.key)
          || (kind === "todo" && !hasUniqueTodoItems(value))
        ) {
          this.entities.clear();
          return false;
        }
        this.entities.set(entity.key, entity);
      }
    }
    if (!this.subagentGraphIsValid()) {
      this.entities.clear();
      return false;
    }
    return true;
  }

  apply(event: ObserverV2LifecycleEvent): boolean {
    const kind = lifecycleKind(event.type);
    if (kind === null) return true;
    const identity = eventIdentity(kind, event.payload);
    const operation = event.payload.operation;
    const revision = event.payload.revision;
    const firstEventSequence = event.payload.first_event_sequence;
    if (
      identity === null
      || (operation !== "upsert" && operation !== "delete")
      || !positiveInteger(revision)
      || !positiveInteger(firstEventSequence)
      || firstEventSequence > event.eventSequence
      || (kind === "todo" && operation === "upsert" && !hasUniqueTodoItems(event.payload))
    ) return false;

    const key = entityKey(kind, identity.turnId, identity.entityId);
    const current = this.entities.get(key);
    const digest = projectionDigest(event.payload);
    if (current === undefined) {
      if (operation !== "upsert" || revision !== 1) return false;
      const next = eventEntity(kind, key, identity.turnId, identity.entityId, event.payload, digest);
      if (next === null || !this.withinKindLimit(kind)) return false;
      this.entities.set(key, next);
      if (kind === "subagent" && !this.subagentGraphIsValid()) {
        this.entities.delete(key);
        return false;
      }
      return true;
    }
    if (current.deleted) return false;
    if (revision === current.revision) return digest === current.digest;
    if (revision !== current.revision + 1 || firstEventSequence !== current.firstEventSequence) return false;

    if (operation === "delete") {
      if (!isTerminalEntity(current)) return false;
      if (kind === "subagent" && this.hasLiveSubagentChild(current.key)) return false;
      this.entities.set(key, { ...current, revision, digest, deleted: true });
      return true;
    }

    const next = eventEntity(kind, key, identity.turnId, identity.entityId, event.payload, digest);
    if (next === null) return false;
    if (kind === "todo" && !todoTransitionIsValid(current.value, next.value)) return false;
    if (
      isTerminalStatus(kind, current.status)
      && (
        next.status !== current.status
        || next.terminalCoreDigest !== current.terminalCoreDigest
        || !safeMetadataIsEnrichment(kind, current.value, next.value)
      )
    ) return false;
    this.entities.set(key, next);
    if (kind === "subagent" && !this.subagentGraphIsValid()) {
      this.entities.set(key, current);
      return false;
    }
    return true;
  }

  private withinKindLimit(kind: EntityKind): boolean {
    const current = [...this.entities.values()].filter((entity) => entity.kind === kind && !entity.deleted).length;
    const limit = kind === "todo"
      ? parityPolicy.limits.max_todo_sections
      : kind === "subagent"
        ? parityPolicy.limits.max_subagents
        : kind === "tool"
          ? parityPolicy.limits.max_tools
          : parityPolicy.limits.max_terminals;
    return current < limit;
  }

  private hasLiveSubagentChild(parentKey: string): boolean {
    return [...this.entities.values()].some((entity) => (
      entity.kind === "subagent" && !entity.deleted && entity.parentKey === parentKey
    ));
  }

  private subagentGraphIsValid(): boolean {
    const subagents = new Map(
      [...this.entities.values()]
        .filter((entity) => entity.kind === "subagent" && !entity.deleted)
        .map((entity) => [entity.key, entity]),
    );
    for (const entity of subagents.values()) {
      if (entity.parentKey !== null && !subagents.has(entity.parentKey)) return false;
      const seen = new Set<string>();
      let cursor: ProjectionEntity | undefined = entity;
      let depth = 0;
      while (cursor !== undefined) {
        if (seen.has(cursor.key)) return false;
        seen.add(cursor.key);
        depth += 1;
        if (depth > parityPolicy.subagent_tree.max_depth) return false;
        cursor = cursor.parentKey === null ? undefined : subagents.get(cursor.parentKey);
      }
    }
    return true;
  }
}

function snapshotEntity(kind: EntityKind, value: Record<string, unknown>): ProjectionEntity | null {
  const identity = snapshotIdentity(kind, value);
  if (
    identity === null
    || !positiveInteger(value.revision)
    || !positiveInteger(value.first_event_sequence)
    || typeof value.status !== "string"
  ) return null;
  return {
    kind,
    key: entityKey(kind, identity.turnId, identity.entityId),
    turnId: identity.turnId,
    entityId: identity.entityId,
    revision: value.revision,
    firstEventSequence: value.first_event_sequence,
    status: value.status,
    digest: projectionDigest(value),
    terminalCoreDigest: terminalCoreDigest(kind, value),
    parentKey: subagentParentKey(identity.turnId, value.parent_subagent_id),
    deleted: false,
    value,
  };
}

function eventEntity(
  kind: EntityKind,
  key: string,
  turnId: string,
  entityId: string,
  payload: Record<string, unknown>,
  digest: string,
): ProjectionEntity | null {
  if (
    typeof payload.status !== "string"
    || !positiveInteger(payload.revision)
    || !positiveInteger(payload.first_event_sequence)
  ) return null;
  return {
    kind,
    key,
    turnId,
    entityId,
    revision: payload.revision,
    firstEventSequence: payload.first_event_sequence,
    status: payload.status,
    digest,
    terminalCoreDigest: terminalCoreDigest(kind, payload),
    parentKey: subagentParentKey(turnId, payload.parent_subagent_id),
    deleted: false,
    value: payload,
  };
}

function snapshotIdentity(kind: EntityKind, value: Record<string, unknown>) {
  const idField = kind === "todo"
    ? "section_id"
    : kind === "subagent"
      ? "subagent_id"
      : kind === "tool"
        ? "tool_call_id"
        : "process_id";
  return identity(value.turn_id, value[idField]);
}

function eventIdentity(kind: EntityKind, payload: Record<string, unknown>) {
  return snapshotIdentity(kind, payload);
}

function identity(turnId: unknown, entityId: unknown): { turnId: string; entityId: string } | null {
  return typeof turnId === "string" && turnId.length > 0 && typeof entityId === "string" && entityId.length > 0
    ? { turnId, entityId }
    : null;
}

function lifecycleKind(type: string): EntityKind | null {
  if (type === "todo.update") return "todo";
  if (type === "subagent.update") return "subagent";
  if (type === "tool.update") return "tool";
  if (type === "terminal.update") return "terminal";
  return null;
}

function entityKey(kind: EntityKind, turnId: string, entityId: string): string {
  return `${kind}:${turnId}:${entityId}`;
}

function subagentParentKey(turnId: string, value: unknown): string | null {
  return typeof value === "string" ? entityKey("subagent", turnId, value) : null;
}

function hasUniqueTodoItems(value: Record<string, unknown>): boolean {
  if (!Array.isArray(value.items)) return false;
  const ids = value.items.map((item) => isRecord(item) ? item.id : undefined);
  return ids.every((id) => typeof id === "string") && new Set(ids).size === ids.length;
}

function isTerminalStatus(kind: EntityKind, status: string): boolean {
  if (kind === "todo") return status === "completed" || status === "cancelled";
  return status === "completed" || status === "failed" || status === "interrupted";
}

function isTerminalEntity(entity: ProjectionEntity): boolean {
  if (entity.kind !== "todo") return isTerminalStatus(entity.kind, entity.status);
  if (!Array.isArray(entity.value.items)) return false;
  return entity.value.items.every((item) => (
    isRecord(item) && (item.status === "completed" || item.status === "cancelled")
  ));
}

function todoTransitionIsValid(
  currentValue: Record<string, unknown>,
  nextValue: Record<string, unknown>,
): boolean {
  if (!Array.isArray(currentValue.items) || !Array.isArray(nextValue.items)) return false;
  if (nextValue.items.length < currentValue.items.length) return false;
  for (let index = 0; index < currentValue.items.length; index += 1) {
    const current = currentValue.items[index];
    const next = nextValue.items[index];
    if (!isRecord(current) || !isRecord(next)) return false;
    if (next.id !== current.id || next.label !== current.label) return false;
    if (
      (current.status === "completed" || current.status === "cancelled")
      && next.status !== current.status
    ) return false;
  }
  return true;
}

function safeMetadataIsEnrichment(
  kind: EntityKind,
  currentValue: Record<string, unknown>,
  nextValue: Record<string, unknown>,
): boolean {
  if (kind === "todo") return true;
  const excluded = new Set([
    "operation",
    "revision",
    "first_event_sequence",
    "status",
    "turn_id",
    kind === "subagent" ? "subagent_id" : kind === "tool" ? "tool_call_id" : "process_id",
    ...(kind === "subagent"
      ? ["parent_subagent_id", "name", "goal"]
      : kind === "tool"
        ? ["name", "call_label"]
        : ["exit_code"]),
  ]);
  for (const [key, value] of Object.entries(currentValue)) {
    if (excluded.has(key) || value === null) continue;
    if (!(key in nextValue) || canonicalJson(nextValue[key]) !== canonicalJson(value)) return false;
  }
  return true;
}

function projectionDigest(value: Record<string, unknown>): string {
  const normalized = Object.fromEntries(Object.entries(value).filter(([key]) => key !== "operation"));
  return canonicalJson(normalized);
}

function terminalCoreDigest(kind: EntityKind, value: Record<string, unknown>): string {
  if (kind === "tool") {
    return canonicalJson({
      name: value.name,
      call_label: value.call_label ?? null,
    });
  }
  if (kind === "subagent") {
    return canonicalJson({
      parent_subagent_id: value.parent_subagent_id,
      name: value.name,
      goal: value.goal,
    });
  }
  if (kind === "terminal") return canonicalJson({ exit_code: value.exit_code ?? null });
  return canonicalJson({});
}

export function canonicalObserverV2Digest(value: unknown): string {
  return canonicalJson(value);
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (isRecord(value)) {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function positiveInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) >= 1;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
