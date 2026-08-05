import type { SubagentItem } from "../../app/model";
import { buildSubagentForest, MAX_SUBAGENT_TREE_DEPTH, MAX_SUBAGENT_TREE_NODES } from "./subagentTree";

describe("subagent tree normalization", () => {
  it("attaches out-of-order children and promotes orphans", () => {
    const forest = buildSubagentForest([
      agent("child", "parent"),
      agent("orphan", "missing"),
      agent("parent"),
    ]);

    expect(forest.nodes.map((node) => node.agent.id)).toEqual(["orphan", "parent"]);
    expect(forest.nodes[1].children.map((node) => node.agent.id)).toEqual(["child"]);
  });

  it("breaks self-links and multi-node cycles while rendering every accepted node once", () => {
    const forest = buildSubagentForest([
      agent("self", "self"),
      agent("a", "b"),
      agent("b", "c"),
      agent("c", "a"),
    ]);
    const ids = flatten(forest.nodes);

    expect(ids).toHaveLength(4);
    expect(new Set(ids)).toEqual(new Set(["self", "a", "b", "c"]));
  });

  it("bounds authoritative UI projection node count and nesting depth", () => {
    expect(MAX_SUBAGENT_TREE_DEPTH).toBe(8);
    expect(MAX_SUBAGENT_TREE_NODES).toBe(128);
    const agents = Array.from({ length: MAX_SUBAGENT_TREE_NODES + 20 }, (_, index) => (
      agent(`agent-${index}`, index === 0 ? undefined : `agent-${index - 1}`)
    ));
    const forest = buildSubagentForest(agents);

    expect(flatten(forest.nodes)).toHaveLength(MAX_SUBAGENT_TREE_NODES);
    expect(maxDepth(forest.nodes)).toBeLessThanOrEqual(MAX_SUBAGENT_TREE_DEPTH);
    expect(forest.truncated).toBe(true);
  });
});

function agent(id: string, parentId?: string): SubagentItem {
  return {
    id,
    parentId,
    name: id,
    role: parentId === undefined ? "coordinator" : "child",
    goal: "goal",
    summary: "summary",
    time: "",
    status: "active",
  };
}

function flatten(nodes: readonly { agent: SubagentItem; children: readonly unknown[] }[]): string[] {
  return nodes.flatMap((node) => [
    node.agent.id,
    ...flatten(node.children as readonly { agent: SubagentItem; children: readonly unknown[] }[]),
  ]);
}

function maxDepth(nodes: readonly { children: readonly unknown[] }[], depth = 1): number {
  return nodes.reduce((maximum, node) => Math.max(
    maximum,
    depth,
    maxDepth(node.children as readonly { children: readonly unknown[] }[], depth + 1),
  ), 0);
}
