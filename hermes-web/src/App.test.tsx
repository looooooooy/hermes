import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import { createPreviewFixture } from "./dev/fixtures";
import { createProductionState } from "./app/productionState";
import styleSheet from "./styles.css?inline";
import { CommandOutcomeUnknown, type RuntimeCallbacks } from "./app/runtimePort";

describe("Hermes Web mobile-parity slice", () => {
  it("renders process updates as flat conversation cards without a rail, nodes, or timeline pseudo-element", () => {
    const { container } = render(<App initialState={createPreviewFixture()} />);

    expect(screen.getByRole("region", { name: "Conversation" })).toBeVisible();
    expect(container.querySelector(".process-rail")).not.toBeInTheDocument();
    expect(container.querySelector(".process-node")).not.toBeInTheDocument();
    expect(styleSheet).not.toMatch(/\.process-rail\s*::before/);
    expect(styleSheet).not.toMatch(/\.process-node\b/);
  });

  it("keeps pending-input audit rows in Long conversation without duplicating the approval surface", () => {
    const fixture = createPreviewFixture();
    render(<App initialState={{
      ...fixture,
      conversation: [...fixture.conversation, {
        id: "runtime-input:approval:approval-42",
        kind: "input",
        label: "Input",
        time: "",
        body: "Input required · Approval",
        status: "waiting",
      }],
    }} />);

    expect(screen.getByRole("region", { name: "Input required · Approval" })).toBeVisible();
    expect(screen.queryByLabelText("Input event")).not.toBeInTheDocument();
  });

  it("shows no global tabs in Conversation or Long and exactly two peer tabs in Subagents", async () => {
    const user = userEvent.setup();
    render(<App initialState={createPreviewFixture()} />);

    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Open menu" }));
    await user.click(screen.getByRole("button", { name: "Open Subagents" }));
    expect(screen.getByRole("region", { name: "Subagent orchestration" })).toBeVisible();
    const tabs = within(screen.getByRole("tablist", { name: "Session views" })).getAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual(["Conversation", "Subagents"]);

    await user.click(screen.getByRole("button", { name: "Open menu" }));
    await user.click(screen.getByRole("button", { name: "Open Long conversation" }));
    expect(screen.getByRole("region", { name: "Long conversation" })).toBeVisible();
    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
  });

  it("expands queued work and tool details", async () => {
    const user = userEvent.setup();
    render(<App initialState={createPreviewFixture()} />);

    await user.click(screen.getByRole("button", { name: "1 queued" }));
    expect(screen.getByText("Review the focused test result")).toBeVisible();

    const tool = screen.getByRole("button", { name: /Terminal\(gradlew/ });
    expect(screen.getByText(":app:testDebugUnitTest")).toBeVisible();
    await user.click(tool);
    expect(screen.queryByText(":app:testDebugUnitTest")).not.toBeInTheDocument();
    await user.click(tool);
    expect(screen.getByText(":app:testDebugUnitTest")).toBeVisible();
  });

  it("renders the authoritative subagent parent-child structure as a nested tree", () => {
    render(<App initialState={{ ...createPreviewFixture(), activeView: "subagents" }} />);

    const tree = screen.getByRole("tree", { name: "Authoritative subagent hierarchy" });
    const coordinator = within(tree).getByRole("treeitem", { name: "Hermes (Coordinator)" });
    const childGroup = within(coordinator).getByRole("group");

    expect(within(childGroup).getByRole("treeitem", { name: "Test Runner" })).toBeVisible();
    expect(within(childGroup).getByRole("treeitem", { name: "Fixer" })).toBeVisible();
    expect(within(childGroup).getByRole("treeitem", { name: "Log Analyzer" })).toBeVisible();
    expect(within(childGroup).getByRole("treeitem", { name: "Committer" })).toBeVisible();
  });

  it("retains a historic authoritative tool result in a collapsed disclosure", async () => {
    let callbacks: RuntimeCallbacks | undefined;
    const runtime = recordingRuntime({
      start: vi.fn((received) => {
        callbacks = received as RuntimeCallbacks;
        return () => undefined;
      }),
    });
    render(<App initialState={createProductionState("mobile-56f3")} runtime={runtime} />);

    await act(async () => callbacks?.onSubscription({
      runtimeSessionId: "runtime-live",
      running: false,
      status: "idle",
      messages: [{ role: "tool", content: "Focused tests passed" }],
      inflight: { user: null, assistant: null, streaming: false, error: null },
    }));

    const disclosure = screen.getByRole("button", { name: "Historical tool output" });
    expect(disclosure).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Focused tests passed")).not.toBeInTheDocument();

    await userEvent.setup().click(disclosure);
    expect(screen.getByText("Focused tests passed")).toBeVisible();
    expect(screen.getByText("Complete")).toBeVisible();
  });

  it("keeps a queued prompt local until the authoritative command result confirms it", async () => {
    const confirmation = deferred<{
      status: "queued";
      clientRequestId: string;
      clientTurnId: string;
      serverTurnId: string;
    }>();
    const runtime = recordingRuntime({ submitPrompt: vi.fn(() => confirmation.promise) });
    const user = userEvent.setup();
    render(
      <App
        initialState={createPreviewFixture()}
        runtime={runtime}
        createId={(kind) => `${kind}-1`}
      />,
    );

    const composer = screen.getByRole("form", { name: "Message Hermes" });
    const textbox = within(composer).getByRole("textbox");
    await user.type(textbox, "Run the next focused test");
    await user.click(within(composer).getByRole("button", { name: "Queue message" }));

    expect(runtime.submitPrompt).toHaveBeenCalledWith({
      sessionId: "88888888-8888-4888-8888-888888888888",
      leaseId: "preview-lease",
      clientRequestId: "request-1",
      clientTurnId: "turn-1",
      text: "Run the next focused test",
    });
    expect(textbox).toHaveValue("Run the next focused test");
    expect(screen.queryByText("Queued by Hermes")).not.toBeInTheDocument();

    await act(async () => confirmation.resolve({
      status: "queued",
      clientRequestId: "request-1",
      clientTurnId: "turn-1",
      serverTurnId: "server-turn-1",
    }));
    expect(await screen.findByRole("status")).toHaveTextContent("Queued by Hermes");
    expect(textbox).toHaveValue("");
  });

  it("renders only server choices and confirms allow_always before sending an approval response", async () => {
    const confirmation = deferred<{
      status: "accepted";
      kind: "approval";
      requestId: string;
      clientRequestId: string;
      controlRevision: number;
    }>();
    const runtime = recordingRuntime({ respondApproval: vi.fn(() => confirmation.promise) });
    const user = userEvent.setup();
    render(
      <App
        initialState={{
          ...createPreviewFixture(),
          pendingApproval: {
            ...createPreviewFixture().pendingApproval!,
            choices: ["allow_once", "allow_session", "allow_always", "deny"],
          },
        }}
        runtime={runtime}
        createId={(kind) => `${kind}-1`}
        now={() => 1_700_000_000_000}
      />,
    );

    expect(screen.getByRole("button", { name: "Approve" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Allow for session" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Always allow" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Deny" })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Always allow" }));
    expect(runtime.respondApproval).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Confirm always allow" }));

    expect(runtime.respondApproval).toHaveBeenCalledWith({
      sessionId: "88888888-8888-4888-8888-888888888888",
      leaseId: "preview-lease",
      clientRequestId: "request-1",
      requestId: "approval-42",
      choice: "allow_always",
      controlRevision: 7,
    });
    expect(screen.queryByText("Approved")).not.toBeInTheDocument();

    await act(async () => confirmation.resolve({
      status: "accepted",
      kind: "approval",
      requestId: "approval-42",
      clientRequestId: "request-1",
      controlRevision: 8,
    }));
    expect(await screen.findByRole("status")).toHaveTextContent("Approved");
  });

  it("fails closed for expired approval state", async () => {
    const runtime = recordingRuntime();
    const expired = createPreviewFixture();
    render(
      <App
        initialState={{
          ...expired,
          pendingApproval: { ...expired.pendingApproval!, expiresAtEpochMs: 100 },
        }}
        runtime={runtime}
        now={() => 101}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Approval expired");
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(runtime.respondApproval).not.toHaveBeenCalled();
  });

  it("fails closed when an approval control revision is stale", async () => {
    const runtime = recordingRuntime();
    const stale = createPreviewFixture();
    const user = userEvent.setup();
    render(
      <App
        initialState={{
          ...stale,
          pendingApproval: { ...stale.pendingApproval!, controlRevision: 6 },
        }}
        runtime={runtime}
        now={() => 1_700_000_000_000}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(runtime.respondApproval).not.toHaveBeenCalled();
    expect(screen.queryByText("Approved")).not.toBeInTheDocument();
  });

  it("routes Guide, Send, and Stop through the command port without optimistic success", async () => {
    const steerConfirmation = deferred<{ status: "accepted"; clientRequestId: string }>();
    const promptConfirmation = deferred<{
      status: "accepted";
      clientRequestId: string;
      clientTurnId: string;
      serverTurnId: string;
    }>();
    const interruptConfirmation = deferred<{ status: "accepted"; clientRequestId: string }>();
    let callbacks: { onRunningChanged(running: boolean): void } | undefined;
    const runtime = recordingRuntime({
      start: vi.fn((received) => {
        callbacks = received as { onRunningChanged(running: boolean): void };
        return () => undefined;
      }),
      steer: vi.fn(() => steerConfirmation.promise),
      submitPrompt: vi.fn(() => promptConfirmation.promise),
      interrupt: vi.fn(() => interruptConfirmation.promise),
    });
    let nextId = 0;
    const user = userEvent.setup();
    render(
      <App
        initialState={{ ...createPreviewFixture(), activeView: "subagents" }}
        runtime={runtime}
        createId={(kind) => `${kind}-${++nextId}`}
      />,
    );

    const composer = screen.getByRole("form", { name: "Message subagents" });
    await user.type(within(composer).getByRole("textbox"), "Check the failure");
    await user.click(screen.getByRole("button", { name: "Guide" }));
    expect(runtime.steer).toHaveBeenCalledWith({
      sessionId: "88888888-8888-4888-8888-888888888888",
      leaseId: "preview-lease",
      clientRequestId: "request-1",
      text: "Check the failure",
    });
    expect(screen.queryByText("Guidance confirmed by Hermes")).not.toBeInTheDocument();
    await act(async () => steerConfirmation.resolve({ status: "accepted", clientRequestId: "request-1" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Guidance confirmed by Hermes");

    await user.type(within(composer).getByRole("textbox"), "Continue after the check");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(runtime.submitPrompt).toHaveBeenCalledWith({
      sessionId: "88888888-8888-4888-8888-888888888888",
      leaseId: "preview-lease",
      clientRequestId: "request-2",
      clientTurnId: "turn-3",
      text: "Continue after the check",
    });
    await act(async () => promptConfirmation.resolve({
      status: "accepted",
      clientRequestId: "request-2",
      clientTurnId: "turn-3",
      serverTurnId: "server-turn-2",
    }));

    await user.click(screen.getByRole("button", { name: "Stop" }));
    expect(runtime.interrupt).toHaveBeenCalledWith({
      sessionId: "88888888-8888-4888-8888-888888888888",
      leaseId: "preview-lease",
      clientRequestId: "request-4",
    });
    expect(screen.queryAllByText("Stopped", { selector: "[data-agent-status]" })).toHaveLength(0);
    await act(async () => interruptConfirmation.resolve({ status: "accepted", clientRequestId: "request-4" }));
    expect(screen.queryAllByText("Stopped", { selector: "[data-agent-status]" })).toHaveLength(0);
    await act(async () => callbacks?.onRunningChanged(false));
    expect(screen.getAllByText("Stopped", { selector: "[data-agent-status]" })).not.toHaveLength(0);
  });

  it("moves focus to a long-conversation event from click and keyboard", async () => {
    const user = userEvent.setup();
    render(<App initialState={{ ...createPreviewFixture(), activeView: "long" }} />);

    const marker = screen.getByRole("button", { name: "Jump to Approval required" });
    await user.click(marker);
    expect(document.activeElement).toHaveAttribute("id", "long-event-approval");

    const nextMarker = screen.getByRole("button", { name: "Jump to Change committed." });
    nextMarker.focus();
    await user.keyboard("{Enter}");
    expect(document.activeElement).toHaveAttribute("id", "long-event-complete");
  });

  it("dispatches authoritative subscription and stream callbacks into conversation state", async () => {
    let callbacks: RuntimeCallbacks | undefined;
    const runtime = recordingRuntime({
      start: vi.fn((received) => {
        callbacks = received as RuntimeCallbacks;
        return () => undefined;
      }),
    });
    render(<App initialState={createPreviewFixture()} runtime={runtime} />);

    await act(async () => callbacks?.onSubscription({
      runtimeSessionId: "runtime-live",
      running: true,
      status: "running",
      messages: [{ role: "user", content: "Live prompt" }],
      inflight: { user: null, assistant: "Live answer", streaming: true, error: null },
    }));
    expect(screen.getByText("Live prompt")).toBeVisible();
    expect(screen.getByText("Live answer")).toBeVisible();

    await act(async () => callbacks?.onEvent({
      type: "thinking.delta",
      eventSequence: 1,
      payload: { text: "Inspecting live state" },
    }));
    expect(screen.getByText("Inspecting live state")).toBeVisible();
  });

  it("installs the real controller lease and pending approval delivered by runtime callbacks", async () => {
    let callbacks: RuntimeCallbacks | undefined;
    const runtime = recordingRuntime({
      start: vi.fn((received) => {
        callbacks = received as RuntimeCallbacks;
        return () => undefined;
      }),
    });
    render(<App initialState={createProductionState("mobile-56f3")} runtime={runtime} now={() => 1_000} />);

    await act(async () => {
      callbacks?.onConnectionChanged("connected");
      callbacks?.onSubscription({
        runtimeSessionId: "runtime-live",
        running: true,
        status: "running",
        messages: [],
        inflight: { user: null, assistant: null, streaming: false, error: null },
      });
      callbacks?.onControlReady(["session.control.acquire", "approval.respond"]);
      callbacks?.onControlStateChanged({
        leaseId: "lease-live",
        runtimeSessionId: "runtime-live",
        leaseExpiresAtEpochMs: 11_000,
        controlRevision: 8,
        controllerKind: "mobile",
        controllerLabel: "Hermes Web",
        unavailableReason: null,
        pendingInput: {
          kind: "approval",
          requestId: "approval-live",
          title: "Approval required",
          description: "Allow this operation?",
          command: "./gradlew test",
          choices: ["deny", "allow_once"],
          expiresAtEpochMs: 9_000,
        },
      });
    });

    expect(screen.getByText("Controller")).toBeVisible();
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Deny" })).toBeEnabled();
    expect(screen.queryByText("9:42:28")).not.toBeInTheDocument();
  });

  it("shows the current Cloud lease-only limitation and keeps conversation actions disabled", async () => {
    let callbacks: RuntimeCallbacks | undefined;
    const runtime = recordingRuntime({
      start: vi.fn((received) => {
        callbacks = received as RuntimeCallbacks;
        return () => undefined;
      }),
    });
    const user = userEvent.setup();
    render(<App initialState={createProductionState("mobile-56f3")} runtime={runtime} now={() => 1_000} />);

    await act(async () => {
      callbacks?.onConnectionChanged("connected");
      callbacks?.onSubscription({
        runtimeSessionId: "runtime-live",
        running: false,
        status: "idle",
        messages: [],
        inflight: { user: null, assistant: null, streaming: false, error: null },
      });
      callbacks?.onControlReady([
        "session.control.acquire",
        "session.control.renew",
        "session.control.release",
        "session.control.status",
      ]);
      callbacks?.onControlStateChanged({
        leaseId: "lease-live",
        runtimeSessionId: "runtime-live",
        leaseExpiresAtEpochMs: 11_000,
        controlRevision: 8,
        controllerKind: "mobile",
        controllerLabel: "Hermes Web",
        unavailableReason: null,
        pendingInput: null,
      });
    });
    await user.type(screen.getByRole("textbox", { name: "Message Hermes" }), "hello");

    expect(screen.getAllByText(
      "Cloud currently exposes lease management only; conversation actions are unavailable.",
    ).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Queue message" })).toBeDisabled();
    expect(runtime.submitPrompt).not.toHaveBeenCalled();
  });

  it("never enables an action when the lease belongs to a previous runtime", async () => {
    const runtime = recordingRuntime();
    const user = userEvent.setup();
    const fixture = createPreviewFixture();
    render(<App
      initialState={{
        ...fixture,
        runtimeSessionId: "runtime-B",
        control: { ...fixture.control, runtimeSessionId: "runtime-A" },
      }}
      runtime={runtime}
      now={() => 1_000}
    />);

    await user.type(screen.getByRole("textbox", { name: "Message Hermes" }), "must not send");
    expect(screen.getByRole("button", { name: "Queue message" })).toBeDisabled();
    expect(screen.getAllByText("The controller lease belongs to a previous Hermes runtime.").length)
      .toBeGreaterThan(0);
    expect(screen.queryByText("Controller", { exact: true })).not.toBeInTheDocument();
    expect(runtime.submitPrompt).not.toHaveBeenCalled();
  });

  it("shows the authoritative controller label while local Web authority is false", async () => {
    let callbacks: RuntimeCallbacks | undefined;
    const runtime = recordingRuntime({
      start: vi.fn((received) => {
        callbacks = received as RuntimeCallbacks;
        return () => undefined;
      }),
    });
    render(<App initialState={createProductionState("mobile-56f3")} runtime={runtime} />);

    await act(async () => {
      callbacks?.onConnectionChanged("connected");
      callbacks?.onSubscription({
        runtimeSessionId: "runtime-A",
        running: false,
        status: "idle",
        messages: [],
        inflight: { user: null, assistant: null, streaming: false, error: null },
      });
      callbacks?.onControlStateChanged({
        leaseId: null,
        runtimeSessionId: "runtime-A",
        leaseExpiresAtEpochMs: 0,
        controlRevision: 8,
        controllerKind: "desktop",
        controllerLabel: "Hermes Desktop",
        pendingInput: null,
        unavailableReason: "Controller is held by Hermes Desktop.",
      });
    });

    expect(screen.getByText("Hermes Desktop", { exact: true })).toBeVisible();
    expect(screen.queryByText("Controller", { exact: true })).not.toBeInTheDocument();
  });

  it("offers an explicit realtime retry while Cloud is disconnected", async () => {
    const retryConnection = vi.fn();
    const runtime = recordingRuntime({ retryConnection });
    const user = userEvent.setup();
    render(<App initialState={createProductionState("mobile-56f3")} runtime={runtime} />);

    await user.click(screen.getByRole("button", { name: "Retry realtime connection" }));
    expect(retryConnection).toHaveBeenCalledTimes(1);
  });

  it("keeps v1 conversation readable while explicitly marking output parity unavailable", () => {
    render(<App initialState={{
      ...createProductionState("mobile-56f3"),
      connection: "connected",
      runtimeSessionId: "runtime-v1",
      observerContract: 1,
      outputParityAvailable: false,
      conversation: [{
        id: "snapshot-0",
        kind: "assistant",
        label: "Hermes",
        time: "",
        body: "V1 response remains readable",
        status: "complete",
      }],
    }} />);

    expect(screen.getByText("V1 response remains readable")).toBeVisible();
    expect(screen.getByText(
      "Todo, Subagent, tool, and terminal lifecycle parity is unavailable on observer v1.",
    )).toBeVisible();
  });

  it("submits an authoritative clarification choice and keeps it pending until confirmed", async () => {
    const confirmation = deferred<{
      status: "accepted";
      kind: "clarify";
      requestId: string;
      clientRequestId: string;
      controlRevision: number;
    }>();
    const runtime = recordingRuntime({ respondClarification: vi.fn(() => confirmation.promise) });
    const fixture = createPreviewFixture();
    const user = userEvent.setup();
    render(<App
      initialState={{
        ...fixture,
        pendingApproval: null,
        pendingClarification: {
          requestId: "clarify-1",
          question: "Which environment?",
          choices: [{ id: "staging", label: "Staging" }],
          allowOther: true,
          expiresAtEpochMs: 1_900_000_000_000,
          controlRevision: 7,
          otherDraft: "",
          resolution: null,
        },
        control: { ...fixture.control, availableMethods: [...fixture.control.availableMethods, "clarify.respond"] },
      }}
      runtime={runtime}
      createId={() => "request-clarify"}
      now={() => 1_000}
    />);

    await user.click(screen.getByRole("button", { name: "Staging" }));
    expect(runtime.respondClarification).toHaveBeenCalledWith({
      sessionId: "88888888-8888-4888-8888-888888888888",
      leaseId: "preview-lease",
      clientRequestId: "request-clarify",
      requestId: "clarify-1",
      controlRevision: 7,
      choiceId: "staging",
    });
    expect(screen.getByRole("button", { name: "Staging" })).toBeDisabled();

    await act(async () => confirmation.resolve({
      status: "accepted",
      kind: "clarify",
      requestId: "clarify-1",
      clientRequestId: "request-clarify",
      controlRevision: 8,
    }));
    expect((await screen.findAllByRole("status")).some((status) => (
      status.textContent?.includes("Clarification accepted")
    ))).toBe(true);
  });

  it("allows nonblank Other clarification text only when the server advertises it", async () => {
    const runtime = recordingRuntime({
      respondClarification: vi.fn(async (command) => ({
        status: "accepted",
        kind: "clarify",
        requestId: command.requestId,
        clientRequestId: command.clientRequestId,
        controlRevision: 8,
      })),
    });
    const fixture = createPreviewFixture();
    const user = userEvent.setup();
    render(<App
      initialState={{
        ...fixture,
        pendingApproval: null,
        pendingClarification: {
          requestId: "clarify-1",
          question: "Which environment?",
          choices: [],
          allowOther: true,
          expiresAtEpochMs: 1_900_000_000_000,
          controlRevision: 7,
          otherDraft: "",
          resolution: null,
        },
        control: { ...fixture.control, availableMethods: [...fixture.control.availableMethods, "clarify.respond"] },
      }}
      runtime={runtime}
      createId={() => "request-other"
      }
      now={() => 1_000}
    />);

    const other = screen.getByRole("textbox", { name: "Other clarification answer" });
    const send = screen.getByRole("button", { name: "Send other answer" });
    expect(send).toBeDisabled();
    await user.type(other, "  Use production  ");
    await user.click(send);
    expect(runtime.respondClarification).toHaveBeenCalledWith(expect.objectContaining({
      otherText: "Use production",
    }));
  });

  it("keeps prompt, approval, and clarification mutations single-flight", async () => {
    const prompt = deferred<never>();
    const approval = deferred<never>();
    const clarify = deferred<never>();
    const runtime = recordingRuntime({
      submitPrompt: vi.fn(() => prompt.promise),
      respondApproval: vi.fn(() => approval.promise),
      respondClarification: vi.fn(() => clarify.promise),
    });
    const fixture = createPreviewFixture();
    let idCount = 0;
    const user = userEvent.setup();
    const { rerender } = render(<App
      initialState={fixture}
      runtime={runtime}
      createId={(kind) => `${kind}-${++idCount}`}
      now={() => 1_000}
    />);

    const textbox = screen.getByRole("textbox", { name: "Message Hermes" });
    await user.type(textbox, "Run once");
    const queue = screen.getByRole("button", { name: "Queue message" });
    await user.dblClick(queue);
    expect(runtime.submitPrompt).toHaveBeenCalledTimes(1);
    expect(idCount).toBe(2);

    rerender(<App
      initialState={fixture}
      runtime={runtime}
      createId={(kind) => `${kind}-${++idCount}`}
      now={() => 1_000}
    />);
    const approve = screen.getByRole("button", { name: "Approve" });
    await user.dblClick(approve);
    expect(runtime.respondApproval).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["Guide", "steer", 1],
    ["Send", "submitPrompt", 2],
    ["Stop", "interrupt", 1],
  ] as const)(
    "keeps Subagents %s single-flight and disabled while pending",
    async (buttonName, runtimeMethod, expectedIdCount) => {
      const confirmation = deferred<never>();
      const command = vi.fn(() => confirmation.promise);
      const runtime = recordingRuntime(
        runtimeMethod === "steer"
          ? { steer: command }
          : runtimeMethod === "submitPrompt"
            ? { submitPrompt: command }
            : { interrupt: command },
      );
      let idCount = 0;
      const user = userEvent.setup();
      render(
        <App
          initialState={{ ...createPreviewFixture(), activeView: "subagents" }}
          runtime={runtime}
          createId={(kind) => `${kind}-${++idCount}`}
        />,
      );
      if (buttonName !== "Stop") {
        await user.type(
          screen.getByRole("textbox", { name: "Message subagents" }),
          "Run once",
        );
      }
      const button = screen.getByRole("button", { name: buttonName });

      await user.dblClick(button);

      expect(command).toHaveBeenCalledTimes(1);
      expect(idCount).toBe(expectedIdCount);
      expect(button).toBeDisabled();
      if (buttonName === "Guide") {
        expect(screen.getByRole("button", { name: "Send guidance" })).toBeDisabled();
      }
    },
  );

  it.each([
    "prompt.submit",
    "session.steer",
    "session.interrupt",
    "approval.respond",
    "clarify.respond",
  ] as const)(
    "keeps %s delivery unknown as a tombstone and retries only after a new user action",
    async (method) => {
      const runtimeMethod = method === "prompt.submit"
        ? "submitPrompt"
        : method === "session.steer"
          ? "steer"
          : method === "session.interrupt"
            ? "interrupt"
            : method === "approval.respond"
              ? "respondApproval"
              : "respondClarification";
      const command = vi.fn(async (receivedCommand: { clientRequestId: string }) => {
        void receivedCommand;
        throw new CommandOutcomeUnknown();
      });
      const runtime = recordingRuntime({ [runtimeMethod]: command });
      const fixture = createPreviewFixture();
      const initialState = method === "clarify.respond"
        ? {
            ...fixture,
            pendingApproval: null,
            pendingClarification: {
              requestId: "clarify-unknown",
              question: "Which environment?",
              choices: [{ id: "staging", label: "Staging" }],
              allowOther: false,
              expiresAtEpochMs: 1_900_000_000_000,
              controlRevision: fixture.control.controlRevision,
              otherDraft: "",
              resolution: null,
            },
          }
        : method === "session.steer" || method === "session.interrupt"
          ? { ...fixture, activeView: "subagents" as const }
          : fixture;
      let idCount = 0;
      const user = userEvent.setup();
      render(<App
        initialState={initialState}
        runtime={runtime}
        createId={(kind) => `${kind}-${++idCount}`}
        now={() => 1_000}
      />);

      if (method === "prompt.submit") {
        await user.type(screen.getByRole("textbox", { name: "Message Hermes" }), "Run once");
      } else if (method === "session.steer") {
        await user.type(screen.getByRole("textbox", { name: "Message subagents" }), "Guide once");
      }
      const buttonName = method === "prompt.submit"
        ? "Queue message"
        : method === "session.steer"
          ? "Guide"
          : method === "session.interrupt"
            ? "Stop"
            : method === "approval.respond"
              ? "Approve"
              : "Staging";
      const button = screen.getByRole("button", { name: buttonName });

      await user.click(button);

      expect(command).toHaveBeenCalledTimes(1);
      expect(screen.getByText("Delivery unknown; reconcile with Hermes before retrying.")).toBeVisible();
      expect(button).toBeEnabled();
      const firstClientRequestId = command.mock.calls[0]?.[0].clientRequestId;
      await act(async () => undefined);
      expect(command).toHaveBeenCalledTimes(1);

      await user.click(button);

      expect(command).toHaveBeenCalledTimes(2);
      expect(command.mock.calls[1]?.[0].clientRequestId).not.toBe(firstClientRequestId);
    },
  );

  it("does not create a second clarification request id while the first answer is pending", async () => {
    const confirmation = deferred<never>();
    const runtime = recordingRuntime({ respondClarification: vi.fn(() => confirmation.promise) });
    const fixture = createPreviewFixture();
    let idCount = 0;
    const user = userEvent.setup();
    render(<App
      initialState={{
        ...fixture,
        pendingApproval: null,
        pendingClarification: {
          requestId: "clarify-1",
          question: "Which environment?",
          choices: [{ id: "staging", label: "Staging" }],
          allowOther: false,
          expiresAtEpochMs: 1_900_000_000_000,
          controlRevision: 7,
          otherDraft: "",
          resolution: null,
        },
      }}
      runtime={runtime}
      createId={() => `request-${++idCount}`}
      now={() => 1_000}
    />);

    await user.dblClick(screen.getByRole("button", { name: "Staging" }));
    expect(runtime.respondClarification).toHaveBeenCalledTimes(1);
    expect(idCount).toBe(1);
  });
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function recordingRuntime(overrides: Record<string, unknown> = {}) {
  return {
    start: vi.fn(() => () => undefined),
    submitPrompt: vi.fn(),
    steer: vi.fn(),
    interrupt: vi.fn(),
    respondApproval: vi.fn(),
    respondClarification: vi.fn(),
    ...overrides,
  };
}
