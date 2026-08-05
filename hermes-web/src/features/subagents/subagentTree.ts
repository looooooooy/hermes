import type { SubagentItem } from "../../app/model";

export const MAX_SUBAGENT_TREE_NODES = 128;
export const MAX_SUBAGENT_TREE_DEPTH = 8;

export interface SubagentTreeNode {
  agent: SubagentItem;
  children: readonly SubagentTreeNode[];
}

export interface SubagentForest {
  nodes: readonly SubagentTreeNode[];
  truncated: boolean;
}

export function buildSubagentForest(agents: readonly SubagentItem[]): SubagentForest {
  const accepted: SubagentItem[] = [];
  const acceptedIds = new Set<string>();
  let truncated = false;
  for (const agent of agents) {
    if (acceptedIds.has(agent.id)) continue;
    if (accepted.length >= MAX_SUBAGENT_TREE_NODES) {
      truncated = true;
      continue;
    }
    accepted.push(agent);
    acceptedIds.add(agent.id);
  }

  const parentById = new Map<string, string | null>();
  for (const agent of accepted) {
    const parentId = agent.parentId;
    parentById.set(
      agent.id,
      parentId !== undefined && parentId !== agent.id && acceptedIds.has(parentId) ? parentId : null,
    );
  }

  breakParentCycles(accepted, parentById);
  boundTreeDepth(accepted, parentById);

  const childrenById = new Map<string, SubagentItem[]>();
  const roots: SubagentItem[] = [];
  for (const agent of accepted) {
    const parentId = parentById.get(agent.id) ?? null;
    if (parentId === null) {
      roots.push(agent);
    } else {
      const children = childrenById.get(parentId) ?? [];
      children.push(agent);
      childrenById.set(parentId, children);
    }
  }

  const buildNode = (agent: SubagentItem): SubagentTreeNode => ({
    agent,
    children: (childrenById.get(agent.id) ?? []).map(buildNode),
  });
  return { nodes: roots.map(buildNode), truncated };
}

function breakParentCycles(
  agents: readonly SubagentItem[],
  parentById: Map<string, string | null>,
): void {
  const state = new Map<string, "visiting" | "visited">();
  const visit = (id: string): void => {
    if (state.get(id) === "visited") return;
    state.set(id, "visiting");
    const parentId = parentById.get(id) ?? null;
    if (parentId !== null) {
      if (state.get(parentId) === "visiting") {
        parentById.set(id, null);
      } else {
        visit(parentId);
      }
    }
    state.set(id, "visited");
  };
  for (const agent of agents) visit(agent.id);
}

function boundTreeDepth(
  agents: readonly SubagentItem[],
  parentById: Map<string, string | null>,
): void {
  for (const agent of agents) {
    let depth = 1;
    let parentId = parentById.get(agent.id) ?? null;
    while (parentId !== null && depth <= MAX_SUBAGENT_TREE_DEPTH) {
      depth += 1;
      parentId = parentById.get(parentId) ?? null;
    }
    if (depth > MAX_SUBAGENT_TREE_DEPTH) parentById.set(agent.id, null);
  }
}
