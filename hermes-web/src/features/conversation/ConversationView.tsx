import {
  CaretDown,
  CaretUp,
  Check,
  Circle,
  ClockCounterClockwise,
  Paperclip,
  PaperPlaneRight,
  Sparkle,
} from "@phosphor-icons/react";
import type { Dispatch } from "react";
import type {
  ConversationEvent,
  HermesWebAction,
  HermesWebState,
  TodoItem,
  ApprovalChoice,
  ClarificationAnswer,
} from "../../app/model";
import { ApprovalPanel } from "../../shared/ui/ApprovalPanel";
import { ClarificationPanel } from "../../shared/ui/ClarificationPanel";
import { StatusDot } from "../../shared/ui/StatusDot";

interface ConversationViewProps {
  state: HermesWebState;
  dispatch: Dispatch<HermesWebAction>;
  onSubmitPrompt: () => Promise<void>;
  onRespondApproval: (choice: ApprovalChoice) => Promise<void>;
  onRespondClarification: (answer: ClarificationAnswer) => Promise<void>;
  now: () => number;
  promptUnavailableReason: string | null;
  approvalUnavailableReason: string | null;
  clarificationUnavailableReason: string | null;
}

export function ConversationView({
  state,
  dispatch,
  onSubmitPrompt,
  onRespondApproval,
  onRespondClarification,
  now,
  promptUnavailableReason,
  approvalUnavailableReason,
  clarificationUnavailableReason,
}: ConversationViewProps) {
  const visibleConversation = state.conversation.filter((event) => event.kind !== "input");
  return (
    <section className="view-panel conversation-view" aria-label="Conversation">
      <div className="view-scroll conversation-scroll">
        {visibleConversation.length === 0 ? (
          <div className="empty-state">
            {state.runtimeSessionId === null
              ? "No authoritative session is connected."
              : "No conversation messages yet."}
          </div>
        ) : visibleConversation.map((event) => (
          <TranscriptEvent key={event.id} event={event} dispatch={dispatch} />
        ))}

        {state.queuedPrompts.length === 0 ? null : (
          <section className="queue-panel">
            <button
              type="button"
              className="queue-toggle"
              aria-expanded={state.queueExpanded}
              onClick={() => dispatch({ type: "queue.toggled" })}
            >
              <span className="queue-label">
                <ClockCounterClockwise size={15} aria-hidden="true" />
                {state.queuedPrompts.length} queued
              </span>
              {state.queueExpanded ? <CaretUp size={15} /> : <CaretDown size={15} />}
            </button>
            {state.queueExpanded ? (
              <ol className="queue-list">
                {state.queuedPrompts.map((promptText) => <li key={promptText}>{promptText}</li>)}
              </ol>
            ) : null}
          </section>
        )}

        {state.pendingApproval === null ? null : (
          <ApprovalPanel
            approval={state.pendingApproval}
            onRespond={onRespondApproval}
            now={now}
            disabledReason={approvalUnavailableReason}
            pending={state.commandLock?.key === `approval:${state.pendingApproval.requestId}`}
          />
        )}
        {state.pendingClarification === null ? null : (
          <ClarificationPanel
            clarification={state.pendingClarification}
            dispatch={dispatch}
            onRespond={onRespondClarification}
            now={now}
            disabledReason={clarificationUnavailableReason}
            pending={state.commandLock?.key === `clarify:${state.pendingClarification.requestId}`}
          />
        )}
      </div>

      {state.commandFeedback === null
        || (state.commandFeedback.kind === "approval" && state.commandFeedback.status !== "unknown") ? null : (
        <p className={`inline-feedback inline-feedback--${state.commandFeedback.status}`} role="status">
          {state.commandFeedback.message}
        </p>
      )}
      {promptUnavailableReason === null ? null : (
        <p className="control-unavailable" role="status">{promptUnavailableReason}</p>
      )}
      <form className="composer" aria-label="Message Hermes" onSubmit={(event) => {
        event.preventDefault();
        void onSubmitPrompt();
      }}>
        <label className="sr-only" htmlFor="hermes-message">Message Hermes</label>
        <textarea
          id="hermes-message"
          value={state.composerDraft}
          placeholder="Message Hermes"
          rows={1}
          onChange={(event) => dispatch({ type: "composer.draftChanged", value: event.target.value })}
        />
        <button type="button" className="composer-icon" aria-label="Attach file">
          <Paperclip size={19} />
        </button>
        <button
          type="submit"
          className="send-button"
          aria-label="Queue message"
          disabled={
            promptUnavailableReason !== null
            || state.composerDraft.trim().length === 0
            || state.commandLock?.key === "prompt"
          }
          title={promptUnavailableReason ?? undefined}
        >
          <PaperPlaneRight size={20} weight="fill" />
        </button>
      </form>
    </section>
  );
}

function TranscriptEvent({ event, dispatch }: ProcessEventProps) {
  if (event.kind === "user") {
    return (
      <article className="prompt-panel">
        <header>
          <strong>{event.label}</strong>
          <time>{event.time}</time>
        </header>
        <p>{event.body}</p>
      </article>
    );
  }
  if (event.kind === "assistant") {
    return (
      <article className="assistant-panel">
        <header>
          <div>
            <Sparkle className="assistant-spark" size={15} weight="fill" aria-hidden="true" />
            <strong>{event.label}</strong>
          </div>
          <time>{event.time}</time>
        </header>
        {(event.body ?? "").split("\n\n").map((paragraph, index) => (
          <p key={`${event.id}-${index}`}>{paragraph}</p>
        ))}
      </article>
    );
  }
  return (
    <div className="process-flow" aria-label={`${event.label} event`}>
      <ProcessEvent event={event} dispatch={dispatch} />
    </div>
  );
}

interface ProcessEventProps {
  event: ConversationEvent;
  dispatch: Dispatch<HermesWebAction>;
}

function ProcessEvent({ event, dispatch }: ProcessEventProps) {
  const isTool = event.kind === "tool" || event.kind === "terminal";
  const isTodo = event.kind === "todo";
  const isDisclosure = isTool || isTodo;
  const statusLabel = workStatusLabel(event.status ?? "waiting");
  return (
    <section className={`process-event process-event--${event.kind}`}>
      {isDisclosure ? (
        <button
          type="button"
          className="process-heading process-heading--button"
          aria-expanded={event.expanded}
          aria-label={isTool ? event.body ?? event.label : `${event.label} (${event.count ?? 0})`}
          onClick={() => dispatch({ type: "section.toggled", eventId: event.id })}
        >
          <span className="process-label">
            <StatusDot status={event.status ?? "waiting"} />
            {event.label} {event.count === undefined ? null : `(${event.count})`}
          </span>
          <span className="process-meta">
            <time>{event.time}</time>
            {event.expanded ? <CaretUp size={15} /> : <CaretDown size={15} />}
          </span>
        </button>
      ) : (
        <div className="process-heading">
          <span className="process-label">
            <StatusDot status={event.status ?? "waiting"} />
            {event.label} {event.count === undefined ? null : `(${event.count})`}
          </span>
          <time>{event.time}</time>
        </div>
      )}
      {!isDisclosure && event.body !== undefined ? <p className="process-body">{event.body}</p> : null}
      {isTodo && event.expanded && event.items !== undefined ? <TodoList items={event.items} /> : null}
      {isTool && event.expanded ? (
        <div className="tool-panel">
          <header>
            <strong>{event.body ?? event.label}</strong>
            <span>{statusLabel}</span>
          </header>
          <code>{event.details}</code>
        </div>
      ) : null}
    </section>
  );
}

function workStatusLabel(status: NonNullable<ConversationEvent["status"]>): string {
  switch (status) {
    case "active": return "Running";
    case "queued": return "Queued";
    case "waiting": return "Waiting";
    case "complete": return "Complete";
    case "stopped": return "Stopped";
  }
}

function TodoList({ items }: { items: readonly TodoItem[] }) {
  return (
    <ol className="todo-list">
      {items.map((item) => (
        <li key={item.id} className={`todo-item todo-item--${item.status}`}>
          {item.status === "completed" ? <Check size={13} weight="bold" /> : <Circle size={10} weight="fill" />}
          <span>{item.label}</span>
        </li>
      ))}
    </ol>
  );
}
