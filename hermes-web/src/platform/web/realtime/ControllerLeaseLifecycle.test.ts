import { CloudRpcFailure } from "./CloudRealtimeAdapter";
import { ControllerLeaseLifecycle } from "./ControllerLeaseLifecycle";

const SESSION_ID = "88888888-8888-4888-8888-888888888888";

describe("ControllerLeaseLifecycle", () => {
  it("acquires a real lease, reconciles status, and publishes the authoritative pending approval", async () => {
    const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
    const states: unknown[] = [];
    const lifecycle = new ControllerLeaseLifecycle({
      sessionId: SESSION_ID,
      now: () => 1_000,
      call: async (method, params) => {
        calls.push({ method, params });
        if (method === "session.control.acquire") return leaseResult();
        if (method === "session.control.status") return statusResult({
          control_revision: 8,
          pending_input: pendingApproval(),
        });
        throw new Error(`unexpected ${method}`);
      },
      onStateChanged: (state) => states.push(state),
    });

    lifecycle.start();
    lifecycle.updateRuntimeSession("runtime-56f3");
    lifecycle.updateCapabilities(CONTROL_METHODS);
    await flushPromises();

    expect(calls).toEqual([
      {
        method: "session.control.status",
        params: { session_id: SESSION_ID },
      },
      {
        method: "session.control.acquire",
        params: { session_id: SESSION_ID },
      },
      {
        method: "session.control.status",
        params: { session_id: SESSION_ID },
      },
    ]);
    expect(states.at(-1)).toEqual({
      leaseId: "lease-1",
      runtimeSessionId: "runtime-56f3",
      leaseExpiresAtEpochMs: 11_000,
      controlRevision: 8,
      controllerKind: "mobile",
      controllerLabel: "Hermes Web",
      pendingInput: {
        kind: "approval",
        requestId: "approval-42",
        title: "Input required · Approval",
        description: "Always allow this operation?",
        command: "./gradlew test",
        choices: ["deny", "allow_once"],
        expiresAtEpochMs: 9_000,
      },
      unavailableReason: null,
    });
  });

  it("fails closed on denied renewal and performs one bounded reacquire while control stays live", async () => {
    const scheduled: Array<{ callback: () => void; delay: number; cleared: boolean }> = [];
    const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
    const states: unknown[] = [];
    let acquireCount = 0;
    const lifecycle = new ControllerLeaseLifecycle({
      sessionId: SESSION_ID,
      now: () => 1_000,
      call: async (method, params) => {
        calls.push({ method, params });
        if (method === "session.control.acquire") {
          acquireCount += 1;
          return leaseResult({ lease_id: acquireCount === 1 ? "lease-1" : "lease-2" });
        }
        if (method === "session.control.status") return statusResult();
        throw new Error("lease expired");
      },
      schedule: (callback, delay) => {
        const handle = { callback, delay, cleared: false };
        scheduled.push(handle);
        return handle;
      },
      cancel: (handle) => { (handle as (typeof scheduled)[number]).cleared = true; },
      onStateChanged: (state) => states.push(state),
    });

    lifecycle.start();
    lifecycle.updateRuntimeSession("runtime-56f3");
    lifecycle.updateCapabilities(CONTROL_METHODS);
    await flushPromises();

    expect(scheduled.at(-1)?.delay).toBe(5_000);
    scheduled.at(-1)!.callback();
    await flushPromises();

    expect(calls.at(-1)).toEqual({
      method: "session.control.renew",
      params: {
        session_id: SESSION_ID,
        lease_id: "lease-1",
      },
    });
    expect(states.at(-1)).toEqual({
      leaseId: null,
      runtimeSessionId: "runtime-56f3",
      leaseExpiresAtEpochMs: 0,
      controlRevision: 8,
      controllerKind: null,
      controllerLabel: null,
      pendingInput: null,
      unavailableReason: "Controller lease was lost. Reconnect to try again.",
    });
    expect(scheduled.at(-1)?.delay).toBe(500);
    scheduled.at(-1)!.callback();
    await flushPromises(8);
    expect(states.at(-1)).toMatchObject({
      leaseId: "lease-2",
      runtimeSessionId: "runtime-56f3",
      controllerKind: "mobile",
    });
  });

  it("releases on visibility suspension, stops renewal, and reacquires when resumed", async () => {
    const scheduled: Array<{ callback: () => void; delay: number; cleared: boolean }> = [];
    const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
    const lifecycle = new ControllerLeaseLifecycle({
      sessionId: SESSION_ID,
      now: () => 1_000,
      call: async (method, params) => {
        calls.push({ method, params });
        if (method === "session.control.acquire") return leaseResult();
        if (method === "session.control.status") return statusResult();
        if (method === "session.control.release") return { released: true, control_revision: 9 };
        throw new Error(`unexpected ${method}`);
      },
      schedule: (callback, delay) => {
        const handle = { callback, delay, cleared: false };
        scheduled.push(handle);
        return handle;
      },
      cancel: (handle) => { (handle as (typeof scheduled)[number]).cleared = true; },
      onStateChanged: () => undefined,
    });

    lifecycle.start();
    lifecycle.updateRuntimeSession("runtime-56f3");
    lifecycle.updateCapabilities(CONTROL_METHODS);
    await flushPromises();
    await lifecycle.suspend();

    expect(scheduled.at(-1)?.cleared).toBe(true);
    expect(calls.at(-1)).toEqual({
      method: "session.control.release",
      params: {
        session_id: SESSION_ID,
        lease_id: "lease-1",
      },
    });

    lifecycle.resume();
    await flushPromises();
    expect(calls.filter(({ method }) => method === "session.control.acquire")).toHaveLength(2);
  });

  it("bounds a best-effort release when suspension is aborted", async () => {
    const lifecycle = new ControllerLeaseLifecycle({
      sessionId: SESSION_ID,
      now: () => 1_000,
      call: async (method) => {
        if (method === "session.control.acquire") return leaseResult();
        if (method === "session.control.status") return statusResult();
        if (method === "session.control.release") return await new Promise<never>(() => undefined);
        throw new Error(`unexpected ${method}`);
      },
      schedule: () => 1,
      cancel: () => undefined,
      onStateChanged: () => undefined,
    });
    lifecycle.start();
    lifecycle.updateRuntimeSession("runtime-56f3");
    lifecycle.updateCapabilities(CONTROL_METHODS);
    await flushPromises();
    const controller = new AbortController();

    const suspension = lifecycle.suspend(controller.signal);
    controller.abort();

    await expect(Promise.race([
      suspension.then(() => "settled"),
      new Promise<string>((resolve) => setTimeout(() => resolve("timeout"), 0)),
    ])).resolves.toBe("settled");
  });

  it("rejects malformed or duplicated approval choices and never publishes a lease", async () => {
    const states: unknown[] = [];
    const lifecycle = new ControllerLeaseLifecycle({
      sessionId: SESSION_ID,
      call: async (method) => method === "session.control.acquire"
        ? leaseResult({ pending_input: { ...pendingApproval(), choices: ["deny", "deny"] } })
        : statusResult(),
      onStateChanged: (state) => states.push(state),
    });

    lifecycle.start();
    lifecycle.updateRuntimeSession("runtime-56f3");
    lifecycle.updateCapabilities(CONTROL_METHODS);
    await flushPromises();

    expect(states.at(-1)).toMatchObject({
      leaseId: null,
      unavailableReason: "Hermes returned an invalid controller lease.",
    });
  });

  it.each([
    ["approval title", { ...pendingApproval(), title: "password=hunter2" }, "password=hunter2"],
    [
      "approval command",
      { ...pendingApproval(), command: "Authorization: Basic dXNlcjpwYXNz" },
      "Authorization: Basic dXNlcjpwYXNz",
    ],
    ["approval description", {
      ...pendingApproval(),
      description: "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln",
    }, "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln"],
    ["clarification question", {
      request_id: "clarify-1",
      kind: "clarify",
      question: "token=provider-token-value",
      choices: [{ id: "continue", label: "Continue" }],
      allow_other: false,
      expires_at_epoch_ms: 9_000,
    }, "token=provider-token-value"],
  ])("rejects credential-bearing %s before publishing controller state", async (_label, pendingInput, credential) => {
    const states: unknown[] = [];
    const lifecycle = new ControllerLeaseLifecycle({
      sessionId: SESSION_ID,
      call: async (method) => method === "session.control.acquire"
        ? leaseResult()
        : statusResult({ pending_input: pendingInput }),
      onStateChanged: (state) => states.push(state),
    });

    lifecycle.start();
    lifecycle.updateRuntimeSession("runtime-56f3");
    lifecycle.updateCapabilities(CONTROL_METHODS);
    await flushPromises();

    expect(states.at(-1)).toMatchObject({
      leaseId: null,
      pendingInput: null,
      unavailableReason: "Hermes returned an invalid controller lease.",
    });
    expect(JSON.stringify(states)).not.toContain(credential);
  });

  it("drops lease A before switching runtime and keeps the Cloud binding on stable session id", async () => {
    const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
    const states: Array<{ leaseId: string | null; runtimeSessionId: string | null }> = [];
    let targetRuntime = "runtime-A";
    const lifecycle = new ControllerLeaseLifecycle({
      sessionId: SESSION_ID,
      now: () => 1_000,
      call: async (method, params) => {
        calls.push({ method, params });
        if (method === "session.control.acquire") return leaseResult({
          lease_id: targetRuntime === "runtime-A" ? "lease-A" : "lease-B",
          control_revision: targetRuntime === "runtime-A" ? 7 : 9,
        });
        if (method === "session.control.status") return statusResult({
          control_revision: targetRuntime === "runtime-A" ? 7 : 9,
        });
        if (method === "session.control.release") return { released: true, control_revision: 8 };
        throw new Error(`unexpected ${method}`);
      },
      schedule: () => 1,
      cancel: () => undefined,
      onStateChanged: (state) => states.push({
        leaseId: state.leaseId,
        runtimeSessionId: state.runtimeSessionId,
      }),
    });

    lifecycle.start();
    lifecycle.updateRuntimeSession("runtime-A");
    lifecycle.updateCapabilities(CONTROL_METHODS);
    await flushPromises();
    expect(states.at(-1)).toEqual({ leaseId: "lease-A", runtimeSessionId: "runtime-A" });

    targetRuntime = "runtime-B";
    lifecycle.updateRuntimeSession(targetRuntime);
    expect(states.at(-1)).toEqual({ leaseId: null, runtimeSessionId: "runtime-B" });
    await flushPromises(10);

    expect(calls).toContainEqual({
      method: "session.control.release",
      params: {
        session_id: SESSION_ID,
        lease_id: "lease-A",
      },
    });
    const releaseIndex = calls.findIndex(({ method }) => method === "session.control.release");
    const secondAcquireIndex = calls.map(({ method }) => method).lastIndexOf("session.control.acquire");
    expect(releaseIndex).toBeLessThan(secondAcquireIndex);
    expect(states.at(-1)).toEqual({ leaseId: "lease-B", runtimeSessionId: "runtime-B" });
  });

  it("prevents an old in-flight acquire response from filling a newer runtime epoch", async () => {
    const acquireA = deferred<Record<string, unknown>>();
    const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
    const states: Array<{ leaseId: string | null; runtimeSessionId: string | null }> = [];
    let acquireCount = 0;
    const lifecycle = new ControllerLeaseLifecycle({
      sessionId: SESSION_ID,
      call: async (method, params) => {
        calls.push({ method, params });
        if (method === "session.control.acquire") {
          acquireCount += 1;
          return acquireCount === 1 ? acquireA.promise : leaseResult({ lease_id: "lease-C" });
        }
        if (method === "session.control.status") return statusResult();
        if (method === "session.control.release") return { released: true, control_revision: 9 };
        throw new Error(`unexpected ${method}`);
      },
      schedule: () => 1,
      cancel: () => undefined,
      onStateChanged: (state) => states.push({
        leaseId: state.leaseId,
        runtimeSessionId: state.runtimeSessionId,
      }),
    });

    lifecycle.start();
    lifecycle.updateRuntimeSession("runtime-A");
    lifecycle.updateCapabilities(CONTROL_METHODS);
    await flushPromises();
    lifecycle.updateRuntimeSession("runtime-B");
    lifecycle.updateRuntimeSession("runtime-C");
    acquireA.resolve(leaseResult({ lease_id: "lease-A" }));
    await flushPromises(14);

    expect(calls).toContainEqual(expect.objectContaining({
      method: "session.control.release",
      params: { session_id: SESSION_ID, lease_id: "lease-A" },
    }));
    expect(calls.filter(({ method }) => method === "session.control.acquire")).toHaveLength(2);
    expect(states.at(-1)).toEqual({ leaseId: "lease-C", runtimeSessionId: "runtime-C" });
  });

  it("releases a stale in-flight acquire and reacquires after suspend-resume without creating two leases", async () => {
    const firstAcquire = deferred<Record<string, unknown>>();
    const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
    const publishedLeaseIds: Array<string | null> = [];
    let statusCount = 0;
    let acquireCount = 0;
    const lifecycle = new ControllerLeaseLifecycle({
      sessionId: SESSION_ID,
      call: async (method, params) => {
        calls.push({ method, params });
        if (method === "session.control.status") {
          statusCount += 1;
          return statusCount < 3
            ? statusResult({
                controller_kind: "none",
                controller_label: null,
                control_revision: statusCount,
                lease_expires_at_epoch_ms: 0,
              })
            : statusResult({
                lease_expires_at_epoch_ms: 21_000,
                control_revision: 10,
              });
        }
        if (method === "session.control.acquire") {
          acquireCount += 1;
          return acquireCount === 1
            ? firstAcquire.promise
            : leaseResult({ lease_id: "lease-B", expires_at_epoch_ms: 21_000, control_revision: 10 });
        }
        if (method === "session.control.release") return { released: true, control_revision: 9 };
        throw new Error(`unexpected ${method}`);
      },
      schedule: () => 1,
      cancel: () => undefined,
      onStateChanged: (state) => publishedLeaseIds.push(state.leaseId),
    });

    lifecycle.start();
    lifecycle.updateRuntimeSession("runtime-A");
    lifecycle.updateCapabilities(CONTROL_METHODS);
    await flushPromises();

    const suspension = lifecycle.suspend();
    lifecycle.resume();
    firstAcquire.resolve(leaseResult({ lease_id: "lease-A" }));
    await suspension;
    await flushPromises(20);

    expect(calls.filter(({ method }) => method === "session.control.acquire")).toHaveLength(2);
    expect(calls).toContainEqual(expect.objectContaining({
      method: "session.control.release",
      params: { session_id: SESSION_ID, lease_id: "lease-A" },
    }));
    expect(publishedLeaseIds).not.toContain("lease-A");
    expect(publishedLeaseIds.at(-1)).toBe("lease-B");
  });

  it("publishes an authoritative desktop conflict from status without attempting acquire", async () => {
    const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
    const states: unknown[] = [];
    const lifecycle = new ControllerLeaseLifecycle({
      sessionId: SESSION_ID,
      call: async (method, params) => {
        calls.push({ method, params });
        if (method === "session.control.status") return statusResult({
          controller_kind: "desktop",
          controller_label: "Hermes Desktop",
          control_revision: 8,
        });
        throw new Error(`unexpected ${method}`);
      },
      schedule: () => 1,
      cancel: () => undefined,
      onStateChanged: (state) => states.push(state),
    });

    lifecycle.start();
    lifecycle.updateRuntimeSession("runtime-A");
    lifecycle.updateCapabilities(CONTROL_METHODS);
    await flushPromises(10);

    expect(states.at(-1)).toMatchObject({
      leaseId: null,
      runtimeSessionId: "runtime-A",
      controllerKind: "desktop",
      controllerLabel: "Hermes Desktop",
      controlRevision: 8,
    });
    expect(calls.filter(({ method }) => method === "session.control.renew")).toHaveLength(0);
    expect(calls.filter(({ method }) => method === "session.control.acquire")).toHaveLength(0);
  });

  it("accepts a display-safe authoritative controller label at the 160-code-point boundary", async () => {
    const controllerLabel = "A".repeat(160);
    const states: unknown[] = [];
    const lifecycle = new ControllerLeaseLifecycle({
      sessionId: SESSION_ID,
      call: async (method) => method === "session.control.status"
        ? statusResult({
            controller_kind: "desktop",
            controller_label: controllerLabel,
            lease_expires_at_epoch_ms: 0,
          })
        : Promise.reject(new Error(`unexpected ${method}`)),
      onStateChanged: (state) => states.push(state),
    });

    lifecycle.start();
    lifecycle.updateRuntimeSession("runtime-A");
    lifecycle.updateCapabilities(CONTROL_METHODS);
    await flushPromises();

    expect(states.at(-1)).toMatchObject({
      leaseId: null,
      controllerKind: "desktop",
      controllerLabel,
      unavailableReason: `Controller is held by ${controllerLabel}.`,
    });
  });

  it.each([
    ["overlong", "A".repeat(161)],
    ["control-character", "Hermes\u0000Desktop"],
    ["credential-like", "password=hunter2"],
    ["surrounding-whitespace", " Hermes Desktop "],
    ["whitespace-only", "   "],
    ["ill-formed Unicode", "Hermes \ud800 Desktop"],
  ])("fails closed without echoing an invalid %s authoritative controller label", async (_case, controllerLabel) => {
    const states: unknown[] = [];
    const lifecycle = new ControllerLeaseLifecycle({
      sessionId: SESSION_ID,
      call: async (method) => method === "session.control.status"
        ? statusResult({
            controller_kind: "desktop",
            controller_label: controllerLabel,
            lease_expires_at_epoch_ms: 0,
          })
        : Promise.reject(new Error(`unexpected ${method}`)),
      onStateChanged: (state) => states.push(state),
    });

    lifecycle.start();
    lifecycle.updateRuntimeSession("runtime-A");
    lifecycle.updateCapabilities(CONTROL_METHODS);
    await flushPromises();

    expect(states.at(-1)).toMatchObject({
      leaseId: null,
      controllerKind: null,
      controllerLabel: null,
      pendingInput: null,
      unavailableReason: "Hermes returned an invalid controller lease.",
    });
    expect(JSON.stringify(states)).not.toContain(JSON.stringify(controllerLabel).slice(1, -1));
  });

  it("rejects an unsafe controller label from an acquired lease before it becomes actionable", async () => {
    const unsafeLabel = "Authorization: Bearer private-controller-token";
    const states: unknown[] = [];
    let statusCount = 0;
    const lifecycle = new ControllerLeaseLifecycle({
      sessionId: SESSION_ID,
      call: async (method) => {
        if (method === "session.control.status") {
          statusCount += 1;
          return statusCount === 1
            ? statusResult({
                controller_kind: "none",
                controller_label: null,
                lease_expires_at_epoch_ms: 0,
              })
            : statusResult({ controller_label: unsafeLabel });
        }
        if (method === "session.control.acquire") return leaseResult({ controller_label: unsafeLabel });
        throw new Error(`unexpected ${method}`);
      },
      onStateChanged: (state) => states.push(state),
    });

    lifecycle.start();
    lifecycle.updateRuntimeSession("runtime-A");
    lifecycle.updateCapabilities(CONTROL_METHODS);
    await flushPromises();

    expect(states.at(-1)).toMatchObject({
      leaseId: null,
      controllerKind: null,
      controllerLabel: null,
      unavailableReason: "Hermes returned an invalid controller lease.",
    });
    expect(JSON.stringify(states)).not.toContain(unsafeLabel);
  });

  it("normalizes a legacy local controller to canonical desktop at ingress", async () => {
    const states: Array<{ controllerKind: string | null }> = [];
    const lifecycle = new ControllerLeaseLifecycle({
      sessionId: SESSION_ID,
      call: async (method) => method === "session.control.status"
        ? statusResult({
              controller_kind: "local",
              controller_label: "Hermes Desktop",
              control_revision: 8,
            })
        : Promise.reject(new Error(`unexpected ${method}`)),
      schedule: () => 1,
      cancel: () => undefined,
      onStateChanged: (state) => states.push({ controllerKind: state.controllerKind }),
    });

    lifecycle.start();
    lifecycle.updateRuntimeSession("runtime-A");
    lifecycle.updateCapabilities(CONTROL_METHODS);
    await flushPromises(10);

    expect(states.at(-1)).toEqual({ controllerKind: "desktop" });
  });

  it("acquires only after a none status and then verifies the acquired lease", async () => {
    const calls: string[] = [];
    let statusCount = 0;
    const states: Array<{ leaseId: string | null }> = [];
    const lifecycle = new ControllerLeaseLifecycle({
      sessionId: SESSION_ID,
      now: () => 1_000,
      call: async (method) => {
        calls.push(method);
        if (method === "session.control.status") {
          statusCount += 1;
          return statusCount === 1
            ? statusResult({
                controller_kind: "none",
                controller_label: null,
                control_revision: 7,
                lease_expires_at_epoch_ms: 0,
              })
            : statusResult();
        }
        if (method === "session.control.acquire") return leaseResult();
        throw new Error(`unexpected ${method}`);
      },
      onStateChanged: (state) => states.push({ leaseId: state.leaseId }),
    });

    lifecycle.start();
    lifecycle.updateRuntimeSession("runtime-A");
    lifecycle.updateCapabilities(CONTROL_METHODS);
    await flushPromises(8);

    expect(calls).toEqual([
      "session.control.status",
      "session.control.acquire",
      "session.control.status",
    ]);
    expect(states.at(-1)).toEqual({ leaseId: "lease-1" });
  });

  it("does not treat controller conflict 4203 as an acquired lease", async () => {
    const calls: string[] = [];
    const states: Array<{ leaseId: string | null; controllerKind: string | null }> = [];
    let statusCount = 0;
    const lifecycle = new ControllerLeaseLifecycle({
      sessionId: SESSION_ID,
      call: async (method) => {
        calls.push(method);
        if (method === "session.control.status") {
          statusCount += 1;
          return statusCount === 1
            ? statusResult()
            : statusResult({
                controller_kind: "desktop",
                controller_label: "Hermes Desktop",
                control_revision: 9,
                lease_expires_at_epoch_ms: 0,
              });
        }
        if (method === "session.control.acquire") {
          throw new CloudRpcFailure(4203, "controller conflict");
        }
        throw new Error(`unexpected ${method}`);
      },
      onStateChanged: (state) => states.push({
        leaseId: state.leaseId,
        controllerKind: state.controllerKind,
      }),
    });

    lifecycle.start();
    lifecycle.updateRuntimeSession("runtime-A");
    lifecycle.updateCapabilities(CONTROL_METHODS);
    await flushPromises(10);

    expect(calls).toEqual([
      "session.control.status",
      "session.control.acquire",
      "session.control.status",
    ]);
    expect(states.at(-1)).toEqual({ leaseId: null, controllerKind: "desktop" });
  });

  it("rechecks an authoritative 4203 conflict within a bounded budget and reacquires only after status is none", async () => {
    const scheduled: Array<{ callback: () => void; delay: number; cleared: boolean }> = [];
    const calls: Array<{ method: string; oldReleased: boolean }> = [];
    const states: Array<{ leaseId: string | null; controllerKind: string | null }> = [];
    let oldReleased = false;
    let newLeaseAcquired = false;
    let acquireCount = 0;
    const lifecycle = new ControllerLeaseLifecycle({
      sessionId: SESSION_ID,
      now: () => 1_000,
      call: async (method) => {
        calls.push({ method, oldReleased });
        if (method === "session.control.status") {
          if (newLeaseAcquired) return statusResult({ control_revision: 10 });
          if (oldReleased) {
            return statusResult({
              controller_kind: "none",
              controller_label: null,
              control_revision: 9,
              lease_expires_at_epoch_ms: 0,
            });
          }
          return statusResult({ controller_label: "Hermes Web", control_revision: 8 });
        }
        if (method === "session.control.acquire") {
          acquireCount += 1;
          if (!oldReleased) throw new CloudRpcFailure(4203, "controller_conflict");
          newLeaseAcquired = true;
          return leaseResult({ lease_id: "lease-2", control_revision: 10 });
        }
        throw new Error(`unexpected ${method}`);
      },
      schedule: (callback, delay) => {
        const handle = { callback, delay, cleared: false };
        scheduled.push(handle);
        return handle;
      },
      cancel: (handle) => { (handle as (typeof scheduled)[number]).cleared = true; },
      onStateChanged: (state) => states.push({
        leaseId: state.leaseId,
        controllerKind: state.controllerKind,
      }),
    });

    lifecycle.start();
    lifecycle.updateRuntimeSession("runtime-A");
    lifecycle.updateCapabilities(CONTROL_METHODS);
    await flushPromises(10);

    expect(acquireCount).toBe(1);
    expect(states.at(-1)).toEqual({ leaseId: null, controllerKind: "mobile" });
    expect(scheduled).toHaveLength(1);
    expect(scheduled[0].delay).toBeGreaterThan(0);

    oldReleased = true;
    scheduled[0].callback();
    await flushPromises(14);

    expect(acquireCount).toBe(2);
    expect(calls.filter(({ method, oldReleased: released }) => (
      method === "session.control.acquire" && !released
    ))).toHaveLength(1);
    expect(states.at(-1)).toEqual({ leaseId: "lease-2", controllerKind: "mobile" });
  });

  it("cancels a scheduled controller-conflict retry when the lifecycle is suspended", async () => {
    const scheduled: Array<{ callback: () => void; delay: number; cleared: boolean }> = [];
    let acquireCount = 0;
    const lifecycle = new ControllerLeaseLifecycle({
      sessionId: SESSION_ID,
      call: async (method) => {
        if (method === "session.control.status") return statusResult();
        if (method === "session.control.acquire") {
          acquireCount += 1;
          throw new CloudRpcFailure(4203, "controller_conflict");
        }
        throw new Error(`unexpected ${method}`);
      },
      schedule: (callback, delay) => {
        const handle = { callback, delay, cleared: false };
        scheduled.push(handle);
        return handle;
      },
      cancel: (handle) => { (handle as (typeof scheduled)[number]).cleared = true; },
      onStateChanged: () => undefined,
    });

    lifecycle.start();
    lifecycle.updateRuntimeSession("runtime-A");
    lifecycle.updateCapabilities(CONTROL_METHODS);
    await flushPromises(10);
    expect(scheduled).toHaveLength(1);

    await lifecycle.suspend();
    expect(scheduled[0].cleared).toBe(true);
    scheduled[0].callback();
    await flushPromises(10);

    expect(acquireCount).toBe(1);
  });

  it("bounds persistent same-session mobile conflicts to three status-only retries", async () => {
    const scheduled: Array<{ callback: () => void; delay: number }> = [];
    let acquireCount = 0;
    let statusCount = 0;
    const lifecycle = new ControllerLeaseLifecycle({
      sessionId: SESSION_ID,
      call: async (method) => {
        if (method === "session.control.status") {
          statusCount += 1;
          return statusResult();
        }
        if (method === "session.control.acquire") {
          acquireCount += 1;
          throw new CloudRpcFailure(4203, "controller_conflict");
        }
        throw new Error(`unexpected ${method}`);
      },
      schedule: (callback, delay) => {
        const handle = { callback, delay };
        scheduled.push(handle);
        return handle;
      },
      cancel: () => undefined,
      onStateChanged: () => undefined,
    });

    lifecycle.start();
    lifecycle.updateRuntimeSession("runtime-A");
    lifecycle.updateCapabilities(CONTROL_METHODS);
    await flushPromises(10);
    for (let index = 0; index < scheduled.length; index += 1) {
      scheduled[index].callback();
      await flushPromises(10);
    }

    expect(scheduled.map(({ delay }) => delay)).toEqual([100, 250, 500]);
    expect(acquireCount).toBe(1);
    expect(statusCount).toBe(5);
  });

  it("cancels the old conflict retry when the runtime session epoch changes", async () => {
    const scheduled: Array<{ callback: () => void; delay: number; cleared: boolean }> = [];
    const calls: Array<{ method: string; runtime: string }> = [];
    let currentRuntime = "runtime-A";
    const lifecycle = new ControllerLeaseLifecycle({
      sessionId: SESSION_ID,
      call: async (method, params) => {
        void params;
        const runtime = currentRuntime;
        calls.push({ method, runtime });
        if (method === "session.control.status") {
          return runtime === "runtime-A"
            ? statusResult()
            : statusResult({
                controller_kind: "desktop",
                controller_label: "Hermes Desktop",
                lease_expires_at_epoch_ms: 0,
              });
        }
        if (method === "session.control.acquire") {
          throw new CloudRpcFailure(4203, "controller_conflict");
        }
        throw new Error(`unexpected ${method}`);
      },
      schedule: (callback, delay) => {
        const handle = { callback, delay, cleared: false };
        scheduled.push(handle);
        return handle;
      },
      cancel: (handle) => { (handle as (typeof scheduled)[number]).cleared = true; },
      onStateChanged: () => undefined,
    });

    lifecycle.start();
    lifecycle.updateRuntimeSession("runtime-A");
    lifecycle.updateCapabilities(CONTROL_METHODS);
    await flushPromises(10);
    expect(scheduled).toHaveLength(1);

    currentRuntime = "runtime-B";
    lifecycle.updateRuntimeSession(currentRuntime);
    await flushPromises(10);
    const callsBeforeStaleTimer = calls.length;
    expect(scheduled[0].cleared).toBe(true);
    scheduled[0].callback();
    await flushPromises(10);

    expect(calls).toHaveLength(callsBeforeStaleTimer);
    expect(calls.filter(({ runtime }) => runtime === "runtime-A")).toEqual([
      { method: "session.control.status", runtime: "runtime-A" },
      { method: "session.control.acquire", runtime: "runtime-A" },
      { method: "session.control.status", runtime: "runtime-A" },
    ]);
  });

  it("fails closed when a none controller carries a non-null label", async () => {
    const states: Array<{ leaseId: string | null; unavailableReason: string | null }> = [];
    const lifecycle = new ControllerLeaseLifecycle({
      sessionId: SESSION_ID,
      call: async (method) => method === "session.control.status"
        ? statusResult({
            controller_kind: "none",
            controller_label: "No controller",
            control_revision: 8,
            lease_expires_at_epoch_ms: 0,
          })
        : Promise.reject(new Error(`unexpected ${method}`)),
      onStateChanged: (state) => states.push({
        leaseId: state.leaseId,
        unavailableReason: state.unavailableReason,
      }),
    });

    lifecycle.start();
    lifecycle.updateRuntimeSession("runtime-A");
    lifecycle.updateCapabilities(CONTROL_METHODS);
    await flushPromises();

    expect(states.at(-1)).toEqual({
      leaseId: null,
      unavailableReason: "Hermes returned an invalid controller lease.",
    });
  });
});

const CONTROL_METHODS = [
  "session.control.acquire",
  "session.control.renew",
  "session.control.release",
  "session.control.status",
] as const;

function leaseResult(overrides: Record<string, unknown> = {}) {
  return {
    lease_id: "lease-1",
    expires_at_epoch_ms: 11_000,
    control_revision: 8,
    controller_kind: "mobile",
    controller_label: "Hermes Web",
    pending_input: null,
    ...overrides,
  };
}

function statusResult(overrides: Record<string, unknown> = {}) {
  return {
    controller_kind: "mobile",
    controller_label: "Hermes Web",
    control_revision: 8,
    lease_expires_at_epoch_ms: 11_000,
    pending_input: null,
    ...overrides,
  };
}

function pendingApproval() {
  return {
    request_id: "approval-42",
    kind: "approval",
    title: "Input required · Approval",
    description: "Always allow this operation?",
    command: "./gradlew test",
    choices: ["deny", "allow_once"],
    expires_at_epoch_ms: 9_000,
  };
}

async function flushPromises(count = 4): Promise<void> {
  for (let index = 0; index < count; index += 1) await Promise.resolve();
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}
