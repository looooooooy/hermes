import { Minus } from "@phosphor-icons/react";
import type { Dispatch, KeyboardEvent } from "react";
import type {
  ApprovalChoice,
  ClarificationAnswer,
  HermesWebAction,
  HermesWebState,
  LongConversationEvent,
} from "../../app/model";
import { ApprovalPanel } from "../../shared/ui/ApprovalPanel";
import { ClarificationPanel } from "../../shared/ui/ClarificationPanel";

interface LongConversationViewProps {
  state: HermesWebState;
  dispatch: Dispatch<HermesWebAction>;
  onRespondApproval: (choice: ApprovalChoice) => Promise<void>;
  onRespondClarification: (answer: ClarificationAnswer) => Promise<void>;
  now: () => number;
  approvalUnavailableReason: string | null;
  clarificationUnavailableReason: string | null;
}

export function LongConversationView({
  state,
  dispatch,
  onRespondApproval,
  onRespondClarification,
  now,
  approvalUnavailableReason,
  clarificationUnavailableReason,
}: LongConversationViewProps) {
  const jumpTo = (event: LongConversationEvent) => {
    const target = document.getElementById(`long-event-${event.id}`);
    target?.scrollIntoView({ block: "center", behavior: "smooth" });
    target?.focus({ preventScroll: true });
  };

  const activateWithKeyboard = (
    keyboardEvent: KeyboardEvent<HTMLButtonElement>,
    event: LongConversationEvent,
  ) => {
    if (keyboardEvent.key === "Enter" || keyboardEvent.key === " ") {
      keyboardEvent.preventDefault();
      jumpTo(event);
    }
  };

  return (
    <section className="view-panel long-view" aria-label="Long conversation">
      <div className="long-layout">
        <div className="long-stream" aria-label={`${state.longEvents.length} conversation events`}>
          <header className="long-stream-heading">
            <span>Long conversation</span>
            <span>{state.longLineCount.toLocaleString()} lines</span>
          </header>
          <div className="long-event-list">
            {state.longEvents.map((event) => (
              <article
                id={`long-event-${event.id}`}
                key={event.id}
                className={`long-event long-event--${event.tone}`}
                tabIndex={-1}
              >
                <time>{event.time}</time>
                <strong>{event.actor}</strong>
                <span>{event.summary}</span>
              </article>
            ))}
          </div>
        </div>

        <nav className="event-minimap" aria-label="Long conversation navigation">
          {state.longEvents.map((event) => (
            <button
              key={event.id}
              type="button"
              className={`minimap-marker minimap-marker--${event.tone}`}
              aria-label={`Jump to ${event.summary}`}
              onClick={() => jumpTo(event)}
              onKeyDown={(keyboardEvent) => activateWithKeyboard(keyboardEvent, event)}
            >
              <Minus size={18} weight="bold" aria-hidden="true" />
            </button>
          ))}
        </nav>
      </div>

      {state.pendingApproval === null ? null : (
        <div className="long-approval">
          <ApprovalPanel
            approval={state.pendingApproval}
            onRespond={onRespondApproval}
            now={now}
            compact
            disabledReason={approvalUnavailableReason}
            pending={state.commandLock?.key === `approval:${state.pendingApproval.requestId}`}
          />
        </div>
      )}
      {state.pendingClarification === null ? null : (
        <div className="long-approval">
          <ClarificationPanel
            clarification={state.pendingClarification}
            dispatch={dispatch}
            onRespond={onRespondClarification}
            now={now}
            compact
            disabledReason={clarificationUnavailableReason}
            pending={state.commandLock?.key === `clarify:${state.pendingClarification.requestId}`}
          />
        </div>
      )}
    </section>
  );
}
