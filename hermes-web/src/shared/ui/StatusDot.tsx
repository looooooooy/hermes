import type { WorkStatus } from "../../app/model";

interface StatusDotProps {
  status: WorkStatus | "connected" | "disconnected";
  label?: string;
}

export function StatusDot({ status, label }: StatusDotProps) {
  return (
    <span className="status-dot-wrap">
      <span aria-hidden="true" className={`status-dot status-dot--${status}`} />
      {label === undefined ? null : <span>{label}</span>}
    </span>
  );
}
