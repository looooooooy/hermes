import type { Dispatch, FormEvent } from "react";
import type {
  ClarificationAnswer,
  HermesWebAction,
  PendingClarification,
} from "../../app/model";

interface ClarificationPanelProps {
  clarification: PendingClarification;
  dispatch: Dispatch<HermesWebAction>;
  onRespond: (answer: ClarificationAnswer) => Promise<void>;
  now: () => number;
  disabledReason?: string | null;
  pending?: boolean;
  compact?: boolean;
}

export function ClarificationPanel({
  clarification,
  dispatch,
  onRespond,
  now,
  disabledReason = null,
  pending = false,
  compact = false,
}: ClarificationPanelProps) {
  const expired = clarification.expiresAtEpochMs <= now();
  const disabled = disabledReason !== null || pending;
  const submitOther = (event: FormEvent) => {
    event.preventDefault();
    const otherText = clarification.otherDraft.trim();
    if (!clarification.allowOther || otherText.length === 0 || disabled || expired) return;
    void onRespond({ otherText });
  };

  return (
    <section
      className={compact ? "approval-panel approval-panel--compact" : "approval-panel"}
      aria-labelledby={`${clarification.requestId}-title`}
    >
      <div className="approval-heading">
        <strong id={`${clarification.requestId}-title`}>Input required · Clarification</strong>
      </div>
      <p>{clarification.question}</p>
      {clarification.resolution !== null ? (
        <p className="approval-resolution" role="status">Clarification accepted</p>
      ) : expired ? (
        <p className="approval-resolution approval-resolution--deny" role="status">Clarification expired</p>
      ) : (
        <>
          {disabledReason === null ? null : (
            <p className="control-unavailable" role="status">{disabledReason}</p>
          )}
          <div className="approval-actions">
            {clarification.choices.map((choice) => (
              <button
                key={choice.id}
                type="button"
                className="button button--approve"
                disabled={disabled}
                onClick={() => void onRespond({ choiceId: choice.id })}
              >
                {choice.label}
              </button>
            ))}
          </div>
          {clarification.allowOther ? (
            <form className="clarification-other" onSubmit={submitOther}>
              <label htmlFor={`${clarification.requestId}-other`}>Other</label>
              <input
                id={`${clarification.requestId}-other`}
                aria-label="Other clarification answer"
                value={clarification.otherDraft}
                disabled={disabled}
                onChange={(event) => dispatch({
                  type: "clarification.draftChanged",
                  value: event.target.value,
                })}
              />
              <button
                type="submit"
                className="button button--approve"
                aria-label="Send other answer"
                disabled={disabled || clarification.otherDraft.trim().length === 0}
              >
                Send
              </button>
            </form>
          ) : null}
        </>
      )}
    </section>
  );
}
