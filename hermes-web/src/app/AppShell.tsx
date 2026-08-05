import { List } from "@phosphor-icons/react";
import { useState, type Dispatch, type ReactNode } from "react";
import type { HermesWebAction, HermesWebState, ViewId } from "./model";
import { StatusDot } from "../shared/ui/StatusDot";

interface AppShellProps {
  state: HermesWebState;
  dispatch: Dispatch<HermesWebAction>;
  children: ReactNode;
  onRetryConnection?: () => void;
  menuActions?: readonly { label: string; onSelect: () => void }[];
  sessionError?: string | null;
}

const SUBAGENT_TABS: readonly { id: ViewId; label: string }[] = [
  { id: "conversation", label: "Conversation" },
  { id: "subagents", label: "Subagents" },
];

const MENU_ITEMS: readonly { id: ViewId; label: string }[] = [
  { id: "conversation", label: "Conversation" },
  { id: "subagents", label: "Subagents" },
  { id: "long", label: "Long conversation" },
];

export function AppShell({
  state,
  dispatch,
  children,
  onRetryConnection,
  menuActions = [],
  sessionError = null,
}: AppShellProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  const holdsCurrentController = state.controller
    && state.control.leaseId !== null
    && state.control.runtimeSessionId === state.runtimeSessionId;
  const selectView = (view: ViewId) => {
    dispatch({ type: "view.selected", view });
    setMenuOpen(false);
  };

  return (
    <div className="app-stage">
      <section className="app-shell" aria-label="Hermes Web">
        <header className="topbar">
          <button
            className="icon-button"
            type="button"
            aria-label="Open menu"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((open) => !open)}
          >
            <List size={21} weight="bold" />
          </button>
          <strong className="brand">Hermes Web</strong>
          <div className="controller-status">
            <StatusDot
              status={state.connection}
              label={holdsCurrentController ? "Controller" : state.control.controllerLabel ?? "Observer"}
            />
          </div>
        </header>

        {menuOpen ? (
          <nav className="session-menu" aria-label="Hermes Web views">
            {MENU_ITEMS.map((item) => (
              <button
                key={item.id}
                type="button"
                aria-current={state.activeView === item.id ? "page" : undefined}
                onClick={() => selectView(item.id)}
              >
                Open {item.label}
              </button>
            ))}
            {menuActions.map((action) => (
              <button
                key={action.label}
                type="button"
                onClick={() => {
                  setMenuOpen(false);
                  action.onSelect();
                }}
              >
                {action.label}
              </button>
            ))}
          </nav>
        ) : null}

        {sessionError === null ? null : (
          <div className="connection-banner" role="alert">{sessionError}</div>
        )}

        <div className="session-strip">
          <span>session · {state.sessionId}</span>
          <StatusDot status={state.connection} label={state.networkLabel} />
        </div>

        {state.activeView === "subagents" ? (
          <nav className="view-tabs" role="tablist" aria-label="Session views">
            {SUBAGENT_TABS.map((tab) => (
              <button
                key={tab.id}
                type="button"
                role="tab"
                aria-selected={state.activeView === tab.id}
                className={state.activeView === tab.id ? "view-tab view-tab--active" : "view-tab"}
                onClick={() => selectView(tab.id)}
              >
                {tab.label}
              </button>
            ))}
          </nav>
        ) : null}

        {state.source === "cloud" && state.connection === "disconnected" ? (
          <div className="connection-banner" role="status">
            <span>{state.control.unavailableReason ?? "Cloud realtime is disconnected."}</span>
            {onRetryConnection === undefined ? null : (
              <button
                type="button"
                className="button button--neutral"
                aria-label="Retry realtime connection"
                onClick={onRetryConnection}
              >
                Retry
              </button>
            )}
          </div>
        ) : null}

        {state.source === "cloud" && state.connection === "connected" && state.observerContract === 1 ? (
          <div className="parity-banner" role="status">
            Todo, Subagent, tool, and terminal lifecycle parity is unavailable on observer v1.
          </div>
        ) : null}

        {children}
      </section>
    </div>
  );
}
