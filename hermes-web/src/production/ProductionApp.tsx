import { useEffect, useRef, useState, type FormEvent } from "react";
import { App } from "../App";
import { createProductionState } from "../app/productionState";
import type { HermesRuntimePort } from "../app/runtimePort";
import type { PasswordAuthClient } from "../platform/web/auth/BrowserPasswordAuthClient";
import type {
  SessionCatalogAgent,
  SessionCatalogClient,
  SessionCatalogItem,
} from "../platform/web/catalog/BrowserSessionCatalogClient";
import { SessionCatalogRequestAborted } from "../platform/web/catalog/BrowserSessionCatalogClient";

const PAGE_SIZE = 20;
const MAX_AUTO_SESSION_SEARCH_PAGES = 20;

interface ProductionRuntimeTarget {
  agentId: string;
  sessionId: string;
  sessionKey: string;
  profile: string;
}

interface ProductionAppProps {
  authClient: PasswordAuthClient;
  catalogClient: SessionCatalogClient;
  runtimeFactory: (target: ProductionRuntimeTarget) => HermesRuntimePort;
  initialSessionKey?: string;
}

interface ActiveSession {
  item: SessionCatalogItem;
  runtime: HermesRuntimePort;
}

export function ProductionApp({
  authClient,
  catalogClient,
  runtimeFactory,
  initialSessionKey = "",
}: ProductionAppProps) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [authenticated, setAuthenticated] = useState(false);
  const [agents, setAgents] = useState<SessionCatalogAgent[] | null>(null);
  const [selectedAgent, setSelectedAgent] = useState<SessionCatalogAgent | null>(null);
  const [sessions, setSessions] = useState<SessionCatalogItem[]>([]);
  const [sessionTotal, setSessionTotal] = useState(0);
  const [sessionProfile, setSessionProfile] = useState<string | null>(null);
  const [activeSession, setActiveSession] = useState<ActiveSession | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  const [loadingDirectory, setLoadingDirectory] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const operationEpoch = useRef(0);
  const directoryAbort = useRef<AbortController | null>(null);
  const authAbort = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const initialMatchConsumed = useRef(false);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      operationEpoch.current += 1;
      authAbort.current?.abort();
      authAbort.current = null;
      directoryAbort.current?.abort();
      directoryAbort.current = null;
    };
  }, []);

  const beginDirectoryOperation = () => {
    directoryAbort.current?.abort();
    const controller = new AbortController();
    directoryAbort.current = controller;
    return { epoch: ++operationEpoch.current, controller };
  };

  const finishDirectoryOperation = (epoch: number, controller: AbortController) => {
    if (operationEpoch.current !== epoch || directoryAbort.current !== controller) return false;
    directoryAbort.current = null;
    return true;
  };

  const cancelDirectoryOperation = () => {
    operationEpoch.current += 1;
    directoryAbort.current?.abort();
    directoryAbort.current = null;
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (username.trim().length === 0 || password.length === 0) {
      setError("Username and password are required");
      return;
    }
    cancelDirectoryOperation();
    const epoch = operationEpoch.current;
    authAbort.current?.abort();
    const loginController = new AbortController();
    authAbort.current = loginController;
    let directoryController: AbortController | null = null;
    setSubmitting(true);
    setError(null);
    try {
      await authClient.login({ username: username.trim(), password }, loginController.signal);
      if (operationEpoch.current !== epoch) return;
      directoryController = new AbortController();
      directoryAbort.current = directoryController;
      setLoadingDirectory(true);
      const directory = await catalogClient.listAgents(directoryController.signal);
      if (operationEpoch.current !== epoch) return;
      setAgents(directory);
      setAuthenticated(true);
    } catch (failure) {
      if (operationEpoch.current === epoch && !isDirectoryAbort(failure, directoryController)) {
        setError(messageFrom(failure, "Hermes sign-in failed"));
      }
    } finally {
      if (operationEpoch.current === epoch) {
        if (authAbort.current === loginController) authAbort.current = null;
        setPassword("");
        if (directoryController !== null && directoryAbort.current === directoryController) {
          directoryAbort.current = null;
        }
        setSubmitting(false);
        setLoadingDirectory(false);
      }
    }
  };

  const loadAgentSessions = async (agent: SessionCatalogAgent) => {
    const { epoch, controller } = beginDirectoryOperation();
    setActiveSession(null);
    setSelectedAgent(agent);
    setSessions([]);
    setSessionTotal(0);
    setSessionProfile(null);
    setLoadingDirectory(true);
    setError(null);
    try {
      const first = await catalogClient.listSessions({
        agentId: agent.agentId,
        profile: null,
        limit: PAGE_SIZE,
        offset: 0,
      }, controller.signal);
      if (operationEpoch.current !== epoch) return;
      const requested = initialMatchConsumed.current ? "" : initialSessionKey.trim();
      initialMatchConsumed.current = true;
      let combined = [...first.sessions];
      let page = first;
      if (requested.length > 0) {
        if (first.total > PAGE_SIZE * MAX_AUTO_SESSION_SEARCH_PAGES) {
          throw new Error("Hermes session directory exceeds the automatic search limit");
        }
        let requestCount = 1;
        while (combined.length < first.total) {
          if (requestCount >= MAX_AUTO_SESSION_SEARCH_PAGES) {
            throw new Error("Hermes session directory exceeds the automatic search limit");
          }
          if (page.sessions.length === 0 || page.profile === null) {
            throw new Error("Hermes returned an incomplete session directory");
          }
          page = await catalogClient.listSessions({
            agentId: agent.agentId,
            profile: page.profile,
            limit: PAGE_SIZE,
            offset: combined.length,
          }, controller.signal);
          requestCount += 1;
          if (operationEpoch.current !== epoch) return;
          if (page.total !== first.total || page.profile !== first.profile) {
            throw new Error("Hermes returned an inconsistent session directory");
          }
          combined = mergeSessions(combined, page.sessions);
        }
      }
      if (operationEpoch.current !== epoch) return;
      setSessions(combined);
      setSessionTotal(first.total);
      setSessionProfile(first.profile ?? page.profile);
      if (requested.length > 0) {
        const match = combined.find((session) => session.sessionKey === requested);
        if (match === undefined) {
          setError("Requested session is not present in this Agent directory");
        } else {
          openSession(match);
        }
      }
    } catch (failure) {
      if (operationEpoch.current === epoch && !isDirectoryAbort(failure, controller)) {
        setError(messageFrom(failure, "Hermes session directory is unavailable"));
      }
    } finally {
      if (finishDirectoryOperation(epoch, controller)) setLoadingDirectory(false);
    }
  };

  const loadMore = async () => {
    if (selectedAgent === null || sessionProfile === null || sessions.length >= sessionTotal) return;
    const { epoch, controller } = beginDirectoryOperation();
    setLoadingDirectory(true);
    setError(null);
    try {
      const page = await catalogClient.listSessions({
        agentId: selectedAgent.agentId,
        profile: sessionProfile,
        limit: PAGE_SIZE,
        offset: sessions.length,
      }, controller.signal);
      if (operationEpoch.current !== epoch) return;
      if (page.total !== sessionTotal || page.profile !== sessionProfile) {
        throw new Error("Hermes returned an inconsistent session directory");
      }
      setSessions(mergeSessions(sessions, page.sessions));
      setSessionTotal(page.total);
    } catch (failure) {
      if (operationEpoch.current === epoch && !isDirectoryAbort(failure, controller)) {
        setError(messageFrom(failure, "Hermes session directory is unavailable"));
      }
    } finally {
      if (finishDirectoryOperation(epoch, controller)) setLoadingDirectory(false);
    }
  };

  const openSession = (item: SessionCatalogItem) => {
    setError(null);
    setActiveSession({
      item,
      runtime: runtimeFactory({
        agentId: item.agentId,
        sessionId: item.id,
        sessionKey: item.sessionKey,
        profile: item.profile,
      }),
    });
  };

  const showSessions = () => {
    cancelDirectoryOperation();
    setActiveSession(null);
  };

  const showAgents = () => {
    cancelDirectoryOperation();
    setActiveSession(null);
    setSelectedAgent(null);
    setSessions([]);
    setSessionTotal(0);
    setSessionProfile(null);
    setLoadingDirectory(false);
    setError(null);
  };

  const signOut = async () => {
    if (signingOut) return;
    setSigningOut(true);
    setError(null);
    const logoutController = new AbortController();
    authAbort.current?.abort();
    authAbort.current = logoutController;
    try {
      await authClient.logout(logoutController.signal);
      if (!mounted.current) return;
      cancelDirectoryOperation();
      setActiveSession(null);
      setAuthenticated(false);
      setAgents(null);
      setSelectedAgent(null);
      setSessions([]);
      setSessionTotal(0);
      setSessionProfile(null);
      setUsername("");
      setPassword("");
      setSubmitting(false);
      setLoadingDirectory(false);
      setError(null);
      initialMatchConsumed.current = false;
    } catch {
      if (mounted.current) setError("Hermes sign-out failed. The current session remains active.");
    } finally {
      if (authAbort.current === logoutController) authAbort.current = null;
      if (mounted.current) setSigningOut(false);
    }
  };

  if (activeSession !== null) {
    return (
      <App
        initialState={createProductionState(activeSession.item.sessionKey, activeSession.item.id)}
        runtime={activeSession.runtime}
        sessionError={error}
        menuActions={[
          { label: "Sessions", onSelect: showSessions },
          { label: "Sign out", onSelect: () => void signOut() },
        ]}
      />
    );
  }

  if (authenticated) {
    return (
      <main className="catalog-stage">
        <section className="catalog-panel" aria-labelledby="catalog-title">
          <header className="catalog-header">
            <div>
              <strong id="catalog-title">Hermes Cloud</strong>
              <span>{selectedAgent === null ? "Connected Agents" : "Hermes sessions"}</span>
            </div>
            <button type="button" onClick={() => void signOut()} disabled={signingOut}>
              {signingOut ? "Signing out…" : "Sign out"}
            </button>
          </header>
          {error === null ? null : <p className="login-error" role="alert">{error}</p>}
          {selectedAgent === null ? (
            <AgentDirectory agents={agents ?? []} loading={loadingDirectory} onSelect={(agent) => void loadAgentSessions(agent)} />
          ) : (
            <SessionDirectory
              agent={selectedAgent}
              sessions={sessions}
              total={sessionTotal}
              loading={loadingDirectory}
              onBack={showAgents}
              onRefresh={() => void loadAgentSessions(selectedAgent)}
              onLoadMore={() => void loadMore()}
              onSelect={openSession}
            />
          )}
        </section>
      </main>
    );
  }

  return (
    <main className="login-stage">
      <section className="login-panel" aria-labelledby="login-title">
        <header>
          <strong id="login-title">Hermes Cloud</strong>
          <span>Web controller</span>
        </header>
        <form aria-label="Sign in to Hermes Cloud" onSubmit={(event) => void submit(event)}>
          <label htmlFor="login-username">Username</label>
          <input
            id="login-username"
            autoComplete="username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
          />
          <label htmlFor="login-password">Password</label>
          <input
            id="login-password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
          {error === null ? null : <p className="login-error" role="alert">{error}</p>}
          <button className="button button--approve" type="submit" disabled={submitting}>
            {submitting ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </section>
    </main>
  );
}

function AgentDirectory({
  agents,
  loading,
  onSelect,
}: {
  agents: SessionCatalogAgent[];
  loading: boolean;
  onSelect: (agent: SessionCatalogAgent) => void;
}) {
  if (loading) return <p className="catalog-status" role="status">Loading connected Agents…</p>;
  if (agents.length === 0) return <p className="catalog-empty">No connected Hermes Agents</p>;
  return (
    <div className="catalog-list" aria-label="Connected Agents">
      {agents.map((agent) => (
        <button className="catalog-card" type="button" key={agent.agentId} onClick={() => onSelect(agent)}>
          <span className="catalog-card__title">{agent.agentKey}</span>
          <span className={`catalog-status-pill catalog-status-pill--${agent.status}`}>
            {agent.status === "active" ? "Online" : agent.status === "offline" ? "Offline" : "Disabled"}
          </span>
          <span className="catalog-card__meta">Last seen {formatLastSeen(agent.lastSeenAt)}</span>
        </button>
      ))}
    </div>
  );
}

function SessionDirectory({
  agent,
  sessions,
  total,
  loading,
  onBack,
  onRefresh,
  onLoadMore,
  onSelect,
}: {
  agent: SessionCatalogAgent;
  sessions: SessionCatalogItem[];
  total: number;
  loading: boolean;
  onBack: () => void;
  onRefresh: () => void;
  onLoadMore: () => void;
  onSelect: (session: SessionCatalogItem) => void;
}) {
  return (
    <>
      <div className="catalog-actions">
        <button type="button" onClick={onBack}>Agents</button>
        <strong>{agent.agentKey}</strong>
        <button type="button" onClick={onRefresh} disabled={loading}>Refresh sessions</button>
      </div>
      {loading && sessions.length === 0 ? <p className="catalog-status" role="status">Loading sessions…</p> : null}
      {!loading && sessions.length === 0 ? <p className="catalog-empty">该 Agent 暂无可用会话</p> : null}
      <div className="catalog-list" aria-label="Hermes sessions">
        {sessions.map((session) => (
          <button className="catalog-card" type="button" key={session.id} onClick={() => onSelect(session)}>
            <span className="catalog-card__title">{session.title ?? session.sessionKey}</span>
            <span className={`catalog-status-pill${session.isActive ? " catalog-status-pill--active" : ""}`}>
              {session.isActive ? "Active" : "Inactive"}
            </span>
            <span className="catalog-card__meta">
              {session.transcriptAvailable ? `${session.messageCount} messages` : "Transcript unavailable"}
            </span>
            <span className="catalog-card__meta">
              {session.lastActive === null
                ? `${session.surface ?? "Host"} · ${session.availability}`
                : `Last active ${formatLastActive(session.lastActive)}`}
            </span>
          </button>
        ))}
      </div>
      {sessions.length < total ? (
        <button className="catalog-load-more" type="button" onClick={onLoadMore} disabled={loading}>
          {loading ? "Loading…" : "Load more"}
        </button>
      ) : null}
    </>
  );
}

function mergeSessions(current: SessionCatalogItem[], incoming: SessionCatalogItem[]): SessionCatalogItem[] {
  const seenIds = new Set(current.map((session) => session.id));
  const seenIdentities = new Set(current.map(sessionIdentity));
  for (const session of incoming) {
    const identity = sessionIdentity(session);
    if (seenIds.has(session.id) || seenIdentities.has(identity)) {
      throw new Error("Hermes returned a duplicate session page");
    }
    seenIds.add(session.id);
    seenIdentities.add(identity);
  }
  return [...current, ...incoming];
}

function sessionIdentity(session: SessionCatalogItem): string {
  return `${session.agentId}\u0000${session.profile}\u0000${session.sessionKey}`;
}

function isDirectoryAbort(failure: unknown, controller: AbortController | null): boolean {
  return controller?.signal.aborted === true || failure instanceof SessionCatalogRequestAborted;
}

function messageFrom(failure: unknown, fallback: string): string {
  return failure instanceof Error ? failure.message : fallback;
}

function formatLastSeen(value: string | null): string {
  return value === null ? "never" : new Date(value).toLocaleString();
}

function formatLastActive(epochSeconds: number): string {
  return new Date(epochSeconds * 1_000).toLocaleString();
}
