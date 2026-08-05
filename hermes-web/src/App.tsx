import { useEffect, useReducer, useRef } from "react";
import { AppShell } from "./app/AppShell";
import { appReducer } from "./app/appReducer";
import type { HermesWebState } from "./app/model";
import { createProductionState } from "./app/productionState";
import { ConversationView } from "./features/conversation/ConversationView";
import { LongConversationView } from "./features/long-conversation/LongConversationView";
import { SubagentsView } from "./features/subagents/SubagentsView";
import type { ApprovalChoice, ClarificationAnswer } from "./app/model";
import { CommandOutcomeUnknown, type HermesRuntimePort } from "./app/runtimePort";

interface AppProps {
  initialState?: HermesWebState;
  runtime?: HermesRuntimePort;
  createId?: (kind: "request" | "turn") => string;
  now?: () => number;
  menuActions?: readonly { label: string; onSelect: () => void }[];
  sessionError?: string | null;
}

const defaultCreateId = (kind: "request" | "turn") => `${kind}-${crypto.randomUUID()}`;

export function App({
  initialState = createProductionState(),
  runtime,
  createId = defaultCreateId,
  now = Date.now,
  menuActions,
  sessionError,
}: AppProps) {
  const [state, dispatch] = useReducer(appReducer, initialState);
  const mutationLocks = useRef(new Map<string, { clientRequestId: string; controlRevision: number }>());

  useEffect(() => runtime?.start({
    onConnectionChanged: (connection) => {
      if (connection === "disconnected") mutationLocks.current.clear();
      dispatch({ type: "runtime.connectionChanged", connection });
    },
    onSubscription: (snapshot) => dispatch({ type: "runtime.subscriptionInstalled", ...snapshot }),
    onEvent: (event) => dispatch({
      type: "runtime.eventReceived",
      eventType: event.type,
      eventSequence: event.eventSequence,
      payload: event.payload,
    }),
    onControlReady: (availableMethods) => dispatch({ type: "runtime.controlReady", availableMethods }),
    onControlStateChanged: (controlState) => {
      for (const [key, lock] of mutationLocks.current) {
        if (controlState.controlRevision > lock.controlRevision) mutationLocks.current.delete(key);
      }
      dispatch({ type: "runtime.controlStateChanged", ...controlState });
    },
    onRunningChanged: (running) => dispatch({ type: "runtime.runningChanged", running }),
  }), [runtime]);

  const beginMutation = (key: string): string | null => {
    if (mutationLocks.current.has(key)) return null;
    const clientRequestId = createId("request");
    mutationLocks.current.set(key, {
      clientRequestId,
      controlRevision: state.control.controlRevision,
    });
    return clientRequestId;
  };

  const finishMutation = (key: string, clientRequestId: string): void => {
    if (mutationLocks.current.get(key)?.clientRequestId === clientRequestId) {
      mutationLocks.current.delete(key);
    }
  };

  const finishRejectedMutation = (
    error: unknown,
    key: string,
    kind: "prompt" | "approval" | "clarify" | "steer" | "interrupt",
    clientRequestId: string,
    failureMessage: string,
  ): void => {
    finishMutation(key, clientRequestId);
    dispatch(error instanceof CommandOutcomeUnknown
      ? {
          type: "command.unknown",
          kind,
          clientRequestId,
          message: "Delivery unknown; reconcile with Hermes before retrying.",
        }
      : { type: "command.failed", kind, clientRequestId, message: failureMessage });
  };

  const commandBinding = (clientRequestId: string) => {
    if (
      runtime === undefined
      || state.runtimeSessionId === null
      || state.control.leaseId === null
      || state.control.runtimeSessionId !== state.runtimeSessionId
      || state.control.leaseExpiresAtEpochMs <= now()
    ) return null;
    return {
      sessionId: state.stableSessionId,
      leaseId: state.control.leaseId,
      clientRequestId,
    };
  };

  const unavailableReason = (method: string): string | null => {
    if (runtime === undefined) return "Hermes control is unavailable.";
    if (state.connection === "disconnected") {
      return state.control.unavailableReason ?? "Cloud realtime is disconnected.";
    }
    if (state.runtimeSessionId === null) return "Waiting for the authoritative Hermes session.";
    if (state.control.leaseId === null) {
      return state.control.unavailableReason ?? "Waiting for a controller lease.";
    }
    if (state.control.runtimeSessionId !== state.runtimeSessionId) {
      return "The controller lease belongs to a previous Hermes runtime.";
    }
    if (state.control.leaseExpiresAtEpochMs <= now()) return "The controller lease has expired.";
    if (!state.control.availableMethods.includes(method)) {
      const actionMethods = [
        "prompt.submit",
        "session.steer",
        "session.interrupt",
        "approval.respond",
        "clarify.respond",
      ];
      if (!actionMethods.some((candidate) => state.control.availableMethods.includes(candidate))) {
        return "Cloud currently exposes lease management only; conversation actions are unavailable.";
      }
      return `Cloud does not advertise ${method}.`;
    }
    return null;
  };

  const submitPrompt = async () => {
    const text = state.composerDraft.trim();
    if (text.length === 0 || !state.control.availableMethods.includes("prompt.submit")) return;
    const mutationKey = "prompt";
    const clientRequestId = beginMutation(mutationKey);
    if (clientRequestId === null) return;
    const binding = commandBinding(clientRequestId);
    if (binding === null) {
      finishMutation(mutationKey, clientRequestId);
      return;
    }
    const clientTurnId = createId("turn");
    dispatch({ type: "command.started", kind: "prompt", key: mutationKey, clientRequestId });
    try {
      const result = await runtime!.submitPrompt({ ...binding, clientTurnId, text });
      if (
        result.clientRequestId !== clientRequestId
        || result.clientTurnId !== clientTurnId
        || (result.status !== "queued" && result.status !== "accepted")
      ) throw new Error("Hermes returned an invalid prompt confirmation");
      finishMutation(mutationKey, clientRequestId);
      dispatch({ type: "prompt.confirmed", clientRequestId, message: "Queued by Hermes" });
    } catch (error) {
      finishRejectedMutation(error, mutationKey, "prompt", clientRequestId, "Hermes did not confirm the message");
    }
  };

  const respondApproval = async (choice: ApprovalChoice) => {
    const approval = state.pendingApproval;
    if (approval === null || approval.resolution !== null || !approval.choices.includes(choice)) return;
    if (choice === "allow_always" && approval.confirmationChoice !== "allow_always") {
      dispatch({ type: "approval.confirmationRequested", choice });
      return;
    }
    const mutationKey = `approval:${approval.requestId}`;
    const clientRequestId = beginMutation(mutationKey);
    if (clientRequestId === null) return;
    const binding = commandBinding(clientRequestId);
    if (
      binding === null
      || approval.expiresAtEpochMs <= now()
      || approval.controlRevision !== state.control.controlRevision
      || !state.control.availableMethods.includes("approval.respond")
    ) {
      finishMutation(mutationKey, clientRequestId);
      return;
    }
    dispatch({ type: "command.started", kind: "approval", key: mutationKey, clientRequestId });
    try {
      const result = await runtime!.respondApproval({
        ...binding,
        requestId: approval.requestId,
        choice,
        controlRevision: approval.controlRevision,
      });
      if (result.status === "reconciled") {
        dispatch({
          type: "command.reconciled",
          kind: "approval",
          clientRequestId,
          message: "Approval accepted; waiting for the authoritative snapshot",
        });
        return;
      }
      if (
        result.status !== "accepted"
        || result.kind !== "approval"
        || result.requestId !== approval.requestId
        || result.clientRequestId !== clientRequestId
        || result.controlRevision <= state.control.controlRevision
      ) throw new Error("Hermes returned an invalid approval confirmation");
      finishMutation(mutationKey, clientRequestId);
      dispatch({ type: "approval.confirmed", choice, controlRevision: result.controlRevision, clientRequestId });
    } catch (error) {
      finishRejectedMutation(error, mutationKey, "approval", clientRequestId, "Hermes did not confirm the approval");
    }
  };

  const respondClarification = async (answer: ClarificationAnswer) => {
    const clarification = state.pendingClarification;
    if (clarification === null || clarification.resolution !== null) return;
    const normalizedAnswer: ClarificationAnswer | null = "choiceId" in answer && answer.choiceId !== undefined
      ? clarification.choices.some((choice) => choice.id === answer.choiceId)
        ? { choiceId: answer.choiceId }
        : null
      : clarification.allowOther && answer.otherText !== undefined && answer.otherText.trim().length > 0
        ? { otherText: answer.otherText.trim() }
        : null;
    if (
      normalizedAnswer === null
      || clarification.expiresAtEpochMs <= now()
      || clarification.controlRevision !== state.control.controlRevision
      || !state.control.availableMethods.includes("clarify.respond")
    ) return;
    const mutationKey = `clarify:${clarification.requestId}`;
    const clientRequestId = beginMutation(mutationKey);
    if (clientRequestId === null) return;
    const binding = commandBinding(clientRequestId);
    if (binding === null) {
      finishMutation(mutationKey, clientRequestId);
      return;
    }
    dispatch({ type: "command.started", kind: "clarify", key: mutationKey, clientRequestId });
    try {
      const result = await runtime!.respondClarification({
        ...binding,
        requestId: clarification.requestId,
        controlRevision: clarification.controlRevision,
        ...normalizedAnswer,
      });
      if (result.status === "reconciled") {
        dispatch({
          type: "command.reconciled",
          kind: "clarify",
          clientRequestId,
          message: "Clarification accepted; waiting for the authoritative snapshot",
        });
        return;
      }
      if (
        result.kind !== "clarify"
        || result.requestId !== clarification.requestId
        || result.clientRequestId !== clientRequestId
        || result.controlRevision <= state.control.controlRevision
      ) throw new Error("Hermes returned an invalid clarification confirmation");
      finishMutation(mutationKey, clientRequestId);
      dispatch({
        type: "clarification.confirmed",
        answer: normalizedAnswer,
        controlRevision: result.controlRevision,
        clientRequestId,
      });
    } catch (error) {
      finishRejectedMutation(
        error,
        mutationKey,
        "clarify",
        clientRequestId,
        "Hermes did not confirm the clarification",
      );
    }
  };

  const steerSubagents = async () => {
    const text = state.subagentDraft.trim();
    if (text.length === 0 || !state.control.availableMethods.includes("session.steer")) return;
    const mutationKey = "steer";
    const clientRequestId = beginMutation(mutationKey);
    if (clientRequestId === null) return;
    const binding = commandBinding(clientRequestId);
    if (binding === null) {
      finishMutation(mutationKey, clientRequestId);
      return;
    }
    dispatch({ type: "command.started", kind: "steer", key: mutationKey, clientRequestId });
    try {
      const result = await runtime!.steer({ ...binding, text });
      if (result.status !== "accepted" || result.clientRequestId !== clientRequestId) {
        throw new Error("Hermes returned an invalid steer confirmation");
      }
      finishMutation(mutationKey, clientRequestId);
      dispatch({
        type: "subagents.commandConfirmed",
        kind: "steer",
        clientRequestId,
        message: "Guidance confirmed by Hermes",
      });
    } catch (error) {
      finishRejectedMutation(error, mutationKey, "steer", clientRequestId, "Hermes did not confirm the guidance");
    }
  };

  const sendSubagentPrompt = async () => {
    const text = state.subagentDraft.trim();
    if (text.length === 0 || !state.control.availableMethods.includes("prompt.submit")) return;
    const mutationKey = "subagent-prompt";
    const clientRequestId = beginMutation(mutationKey);
    if (clientRequestId === null) return;
    const binding = commandBinding(clientRequestId);
    if (binding === null) {
      finishMutation(mutationKey, clientRequestId);
      return;
    }
    const clientTurnId = createId("turn");
    dispatch({ type: "command.started", kind: "prompt", key: mutationKey, clientRequestId });
    try {
      const result = await runtime!.submitPrompt({ ...binding, clientTurnId, text });
      if (
        result.clientRequestId !== clientRequestId
        || result.clientTurnId !== clientTurnId
        || (result.status !== "queued" && result.status !== "accepted")
      ) throw new Error("Hermes returned an invalid prompt confirmation");
      finishMutation(mutationKey, clientRequestId);
      dispatch({
        type: "subagents.commandConfirmed",
        kind: "prompt",
        clientRequestId,
        message: "Message confirmed by Hermes",
      });
    } catch (error) {
      finishRejectedMutation(error, mutationKey, "prompt", clientRequestId, "Hermes did not confirm the message");
    }
  };

  const interrupt = async () => {
    if (!state.control.availableMethods.includes("session.interrupt")) return;
    const mutationKey = "interrupt";
    const clientRequestId = beginMutation(mutationKey);
    if (clientRequestId === null) return;
    const binding = commandBinding(clientRequestId);
    if (binding === null) {
      finishMutation(mutationKey, clientRequestId);
      return;
    }
    dispatch({ type: "command.started", kind: "interrupt", key: mutationKey, clientRequestId });
    try {
      const result = await runtime!.interrupt(binding);
      if (result.status !== "accepted" || result.clientRequestId !== clientRequestId) {
        throw new Error("Hermes returned an invalid interrupt confirmation");
      }
      finishMutation(mutationKey, clientRequestId);
      dispatch({ type: "interrupt.confirmed", clientRequestId });
    } catch (error) {
      finishRejectedMutation(error, mutationKey, "interrupt", clientRequestId, "Hermes did not confirm the stop request");
    }
  };

  return (
    <AppShell
      state={state}
      dispatch={dispatch}
      menuActions={menuActions}
      sessionError={sessionError}
      onRetryConnection={runtime?.retryConnection === undefined
        ? undefined
        : () => runtime.retryConnection?.()}
    >
      {state.activeView === "conversation" ? (
        <ConversationView
          state={state}
          dispatch={dispatch}
          onSubmitPrompt={submitPrompt}
          onRespondApproval={respondApproval}
          onRespondClarification={respondClarification}
          now={now}
          promptUnavailableReason={unavailableReason("prompt.submit")}
          approvalUnavailableReason={unavailableReason("approval.respond")}
          clarificationUnavailableReason={unavailableReason("clarify.respond")}
        />
      ) : null}
      {state.activeView === "subagents" ? (
        <SubagentsView
          state={state}
          dispatch={dispatch}
          onGuide={steerSubagents}
          onSend={sendSubagentPrompt}
          onStop={interrupt}
          guidePending={mutationLocks.current.has("steer")}
          sendPending={mutationLocks.current.has("subagent-prompt")}
          stopPending={mutationLocks.current.has("interrupt")}
          guideUnavailableReason={unavailableReason("session.steer")}
          sendUnavailableReason={unavailableReason("prompt.submit")}
          stopUnavailableReason={unavailableReason("session.interrupt")}
        />
      ) : null}
      {state.activeView === "long" ? (
        <LongConversationView
          state={state}
          dispatch={dispatch}
          onRespondApproval={respondApproval}
          onRespondClarification={respondClarification}
          now={now}
          approvalUnavailableReason={unavailableReason("approval.respond")}
          clarificationUnavailableReason={unavailableReason("clarify.respond")}
        />
      ) : null}
    </AppShell>
  );
}
