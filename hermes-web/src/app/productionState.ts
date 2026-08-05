import type { HermesWebState } from "./model";

export function createProductionState(
  sessionId = "No active session",
  stableSessionId = sessionId,
): HermesWebState {
  return {
    activeView: "conversation",
    source: "cloud",
    connection: "disconnected",
    controller: false,
    sessionId,
    stableSessionId,
    runtimeSessionId: null,
    runtimeGeneration: null,
    observerContract: null,
    outputParityAvailable: false,
    running: false,
    control: {
      leaseId: null,
      runtimeSessionId: null,
      leaseExpiresAtEpochMs: 0,
      controlRevision: 0,
      availableMethods: [],
      controllerKind: null,
      controllerLabel: null,
      unavailableReason: "Waiting for a Cloud controller lease.",
    },
    networkLabel: "Offline",
    queueExpanded: false,
    queuedPrompts: [],
    conversation: [],
    subagents: [],
    subagentDraft: "",
    longEvents: [],
    longLineCount: 0,
    pendingApproval: null,
    pendingClarification: null,
    composerDraft: "",
    commandFeedback: null,
    commandLock: null,
  };
}
