import type { ApprovalChoice, PendingApproval } from "../../app/model";

interface ApprovalPanelProps {
  approval: PendingApproval;
  onRespond: (choice: ApprovalChoice) => Promise<void>;
  now: () => number;
  compact?: boolean;
  disabledReason?: string | null;
  pending?: boolean;
}

const choiceLabels: Record<ApprovalChoice, string> = {
  allow_once: "Approve",
  allow_session: "Allow for session",
  allow_always: "Always allow",
  deny: "Deny",
};

export function ApprovalPanel({
  approval,
  onRespond,
  now = Date.now,
  compact = false,
  disabledReason = null,
  pending = false,
}: ApprovalPanelProps) {
  const resolutionLabel = approval.resolution === null
    ? null
    : approval.resolution === "deny" ? "Denied" : "Approved";
  const expired = approval.expiresAtEpochMs <= now();

  return (
    <section
      className={compact ? "approval-panel approval-panel--compact" : "approval-panel"}
      aria-labelledby={`${approval.requestId}-title`}
    >
      <div className="approval-heading">
        <strong id={`${approval.requestId}-title`}>{approval.title}</strong>
        {approval.time === undefined ? null : <time>{approval.time}</time>}
      </div>
      <p>{approval.description}</p>
      <code>{approval.command}</code>
      {resolutionLabel !== null ? (
        <p className={`approval-resolution approval-resolution--${approval.resolution}`} role="status">
          {resolutionLabel}
        </p>
      ) : expired ? (
        <p className="approval-resolution approval-resolution--deny" role="status">Approval expired</p>
      ) : approval.confirmationChoice === "allow_always" ? (
        <>
          {disabledReason === null ? null : (
            <p className="control-unavailable" role="status">{disabledReason}</p>
          )}
          <div className="approval-confirmation">
            <p>This grants ongoing permission for matching operations.</p>
            <button
              className="button button--approve"
              type="button"
              disabled={disabledReason !== null || pending}
              title={disabledReason ?? undefined}
              onClick={() => void onRespond("allow_always")}
            >
              Confirm always allow
            </button>
          </div>
        </>
      ) : (
        <>
          {disabledReason === null ? null : (
            <p className="control-unavailable" role="status">{disabledReason}</p>
          )}
          <div className="approval-actions">
            {approval.choices.map((choice) => (
              <button
                key={choice}
                className={`button ${choice === "deny" ? "button--deny" : "button--approve"}`}
                type="button"
                disabled={disabledReason !== null || pending}
                title={disabledReason ?? undefined}
                onClick={() => void onRespond(choice)}
              >
                {choiceLabels[choice]}
              </button>
            ))}
          </div>
        </>
      )}
    </section>
  );
}
