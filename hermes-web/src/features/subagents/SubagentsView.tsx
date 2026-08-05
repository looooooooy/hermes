import {
  BookOpen,
  Paperclip,
  PaperPlaneRight,
  Stop,
} from "@phosphor-icons/react";
import type { Dispatch, FormEvent } from "react";
import type { HermesWebAction, HermesWebState, SubagentItem } from "../../app/model";
import { StatusDot } from "../../shared/ui/StatusDot";
import { buildSubagentForest, type SubagentTreeNode } from "./subagentTree";

interface SubagentsViewProps {
  state: HermesWebState;
  dispatch: Dispatch<HermesWebAction>;
  onGuide: () => Promise<void>;
  onSend: () => Promise<void>;
  onStop: () => Promise<void>;
  guidePending: boolean;
  sendPending: boolean;
  stopPending: boolean;
  guideUnavailableReason: string | null;
  sendUnavailableReason: string | null;
  stopUnavailableReason: string | null;
}

export function SubagentsView({
  state,
  dispatch,
  onGuide,
  onSend,
  onStop,
  guidePending,
  sendPending,
  stopPending,
  guideUnavailableReason,
  sendUnavailableReason,
  stopUnavailableReason,
}: SubagentsViewProps) {
  const submit = (event: FormEvent) => {
    event.preventDefault();
    void onGuide();
  };

  return (
    <section className="view-panel subagents-view" aria-label="Subagent orchestration">
      <div className="view-scroll subagent-scroll">
        <h1>Subagent orchestration</h1>
        {state.subagents.length === 0 ? (
          <p className="empty-state">No authoritative subagent events are available.</p>
        ) : (
          <div className="agent-tree" role="tree" aria-label="Authoritative subagent hierarchy">
            <AgentTree agents={state.subagents} />
          </div>
        )}
      </div>

      {state.commandFeedback === null ? null : (
        <p className={`inline-feedback inline-feedback--${state.commandFeedback.status}`} role="status">
          {state.commandFeedback.message}
        </p>
      )}
      {[guideUnavailableReason, sendUnavailableReason, stopUnavailableReason].every((reason) => reason === null)
        ? null
        : (
            <p className="control-unavailable" role="status">
              {guideUnavailableReason ?? sendUnavailableReason ?? stopUnavailableReason}
            </p>
          )}

      <form className="subagent-composer" aria-label="Message subagents" onSubmit={submit}>
        <label className="sr-only" htmlFor="subagent-message">Message subagents</label>
        <input
          id="subagent-message"
          value={state.subagentDraft}
          placeholder="Message subagents"
          onChange={(event) => dispatch({ type: "subagents.draftChanged", value: event.target.value })}
        />
        <button type="button" className="composer-icon" aria-label="Attach file">
          <Paperclip size={19} />
        </button>
        <button
          type="submit"
          className="send-button"
          aria-label="Send guidance"
          disabled={guidePending || guideUnavailableReason !== null || state.subagentDraft.trim().length === 0}
          title={guideUnavailableReason ?? undefined}
        >
          <PaperPlaneRight size={20} weight="fill" />
        </button>
      </form>

      <div className="orchestration-actions">
        <button
          type="button"
          className="button button--neutral"
          onClick={() => void onGuide()}
          disabled={guidePending || guideUnavailableReason !== null || state.subagentDraft.trim().length === 0}
          title={guideUnavailableReason ?? undefined}
        >
          <BookOpen size={19} /> Guide
        </button>
        <button
          type="button"
          className="button button--deny"
          onClick={() => void onStop()}
          disabled={stopPending || stopUnavailableReason !== null}
          title={stopUnavailableReason ?? undefined}
        >
          <Stop size={17} /> Stop
        </button>
        <button
          type="button"
          className="button button--approve"
          onClick={() => void onSend()}
          disabled={sendPending || sendUnavailableReason !== null || state.subagentDraft.trim().length === 0}
          title={sendUnavailableReason ?? undefined}
        >
          <PaperPlaneRight size={18} /> Send
        </button>
      </div>
    </section>
  );
}

function AgentTree({ agents }: { agents: readonly SubagentItem[] }) {
  const forest = buildSubagentForest(agents);
  return (
    <>
      {forest.nodes.map((node) => <AgentTreeItem key={node.agent.id} node={node} />)}
      {forest.truncated ? (
        <p className="projection-limit" role="status">Subagent projection limited to 128 nodes.</p>
      ) : null}
    </>
  );
}

function AgentTreeItem({ node }: { node: SubagentTreeNode }) {
  return (
    <div className="agent-tree-item" role="treeitem" aria-label={node.agent.name}>
      <AgentCard agent={node.agent} />
      {node.children.length === 0 ? null : (
        <div className="agent-tree-group" role="group">
          {node.children.map((child) => (
            <AgentTreeItem key={child.agent.id} node={child} />
          ))}
        </div>
      )}
    </div>
  );
}

function AgentCard({ agent }: { agent: SubagentItem }) {
  const statusLabel = agent.status[0].toUpperCase() + agent.status.slice(1);
  return (
    <article className={`agent-card agent-card--${agent.role} agent-card--${agent.status}`}>
      <header>
        <div>
          <StatusDot status={agent.status} />
          <strong>{agent.name}</strong>
        </div>
        <span className={`agent-status agent-status--${agent.status}`} data-agent-status>
          {statusLabel}
        </span>
      </header>
      <p className="agent-role">{agent.goal}</p>
      <p>{agent.summary}</p>
      <time>{agent.time}</time>
    </article>
  );
}
