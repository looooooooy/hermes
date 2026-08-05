import type {
  RuntimeControlState,
  RuntimeControllerKind,
  RuntimePendingApproval,
  RuntimePendingClarification,
  RuntimePendingInput,
} from "../../../app/runtimePort";
import { CloudRpcFailure } from "./CloudRealtimeAdapter";
import {
  HERMES_DISPLAY_NAME_MAX_CODE_POINTS,
  isDisplaySafeHermesText,
  isDisplaySafeHermesValue,
} from "../../../shared/contracts/cloudRealtimeV2";

const RENEW_LEAD_MS = 5_000;
const CONTROLLER_CONFLICT_RETRY_DELAYS_MS = [100, 250, 500] as const;
const REQUIRED_METHODS = [
  "session.control.acquire",
  "session.control.renew",
  "session.control.release",
  "session.control.status",
] as const;
const APPROVAL_CHOICES = new Set(["allow_once", "allow_session", "allow_always", "deny"]);

type TimeoutHandle = ReturnType<typeof globalThis.setTimeout>;

interface ControllerLeaseLifecycleOptions {
  sessionId: string;
  call(method: string, params: Record<string, unknown>): Promise<Record<string, unknown>>;
  onStateChanged(state: RuntimeControlState): void;
  now?: () => number;
  schedule?: (callback: () => void, delayMs: number) => unknown;
  cancel?: (handle: unknown) => void;
}

interface LeaseSnapshot {
  leaseId: string;
  runtimeSessionId: string;
  expiresAtEpochMs: number;
  controlRevision: number;
  controllerKind: "mobile";
  controllerLabel: string;
  pendingInput: RuntimePendingInput | null;
}

type DecodedLeaseSnapshot = Omit<LeaseSnapshot, "runtimeSessionId">;

interface StatusSnapshot {
  leaseExpiresAtEpochMs: number;
  controlRevision: number;
  controllerKind: RuntimeControllerKind;
  controllerLabel: string | null;
  pendingInput: RuntimePendingInput | null;
}

/** Owns the short-lived controller lease and never exposes it outside runtime state callbacks. */
export class ControllerLeaseLifecycle {
  private active = false;
  private runtimeSessionId: string | null = null;
  private availableMethods = new Set<string>();
  private lease: LeaseSnapshot | null = null;
  private renewTimer: unknown = null;
  private recoveryTimer: unknown = null;
  private generation = 0;
  private ensurePromise: Promise<void> | null = null;
  private ensureAfterFlight = false;
  private transitionPromise: Promise<void> | null = null;
  private readonly now: () => number;
  private readonly schedule: (callback: () => void, delayMs: number) => unknown;
  private readonly cancel: (handle: unknown) => void;

  constructor(private readonly options: ControllerLeaseLifecycleOptions) {
    this.now = options.now ?? Date.now;
    this.schedule = options.schedule ?? ((callback, delayMs) => globalThis.setTimeout(callback, delayMs));
    this.cancel = options.cancel ?? ((handle) => globalThis.clearTimeout(handle as TimeoutHandle));
  }

  start(): void {
    this.active = true;
    if (this.ensurePromise !== null) this.ensureAfterFlight = true;
    void this.ensureLease();
  }

  updateRuntimeSession(runtimeSessionId: string): void {
    if (this.runtimeSessionId === runtimeSessionId) return;
    const previousRuntimeSessionId = this.runtimeSessionId;
    const previousLease = this.lease;
    this.runtimeSessionId = runtimeSessionId;
    const transitionGeneration = ++this.generation;
    this.clearRenewal();
    this.clearRecovery();
    this.lease = null;
    this.publish(null, "Waiting for the new runtime controller lease.");

    if (previousLease === null && this.transitionPromise === null) {
      void this.ensureLease();
      return;
    }

    const priorTransition = this.transitionPromise ?? Promise.resolve();
    const transition = priorTransition
      .then(async () => {
        if (previousLease !== null && previousRuntimeSessionId !== null) {
          await this.releaseLease(previousLease.leaseId, previousRuntimeSessionId);
        }
      })
      .finally(() => {
        if (this.transitionPromise === transition) this.transitionPromise = null;
        if (this.isCurrent(transitionGeneration)) void this.ensureLease();
      });
    this.transitionPromise = transition;
  }

  updateCapabilities(methods: readonly string[]): void {
    this.availableMethods = new Set(methods);
    if (!REQUIRED_METHODS.every((method) => this.availableMethods.has(method))) {
      this.failClosed("Cloud does not advertise the complete controller lease contract.");
      return;
    }
    void this.ensureLease();
  }

  async suspend(signal?: AbortSignal): Promise<void> {
    this.active = false;
    const lease = this.lease;
    this.generation += 1;
    this.clearRenewal();
    this.clearRecovery();
    this.lease = null;
    this.publish(null, "Controller released while this tab is not active.");
    if (lease === null || !this.availableMethods.has("session.control.release")) return;
    try {
      const release = this.options.call(
        "session.control.release",
        this.leaseParams(lease.leaseId, lease.runtimeSessionId),
      );
      const result = await waitForRelease(release, signal);
      if (!decodeReleaseResult(result)) throw new Error("invalid release response");
    } catch {
      // Local authority is already removed. Release failures must remain fail closed.
    }
  }

  resume(): void {
    if (this.active) return;
    this.active = true;
    if (this.ensurePromise !== null) this.ensureAfterFlight = true;
    void this.ensureLease();
  }

  disconnect(reason = "Control connection was lost. Reconnect to try again."): void {
    this.active = false;
    this.availableMethods.clear();
    this.failClosed(reason);
  }

  private ensureLease(): Promise<void> {
    if (!this.active || this.runtimeSessionId === null || this.lease !== null) return Promise.resolve();
    if (!REQUIRED_METHODS.every((method) => this.availableMethods.has(method))) return Promise.resolve();
    if (this.transitionPromise !== null) return this.transitionPromise;
    if (this.ensurePromise !== null) return this.ensurePromise;
    const generation = this.generation;
    const runtimeSessionId = this.runtimeSessionId;
    const operation = this.acquireAndReconcile(generation, runtimeSessionId).finally(() => {
      if (this.ensurePromise !== operation) return;
      const retryAfterFlight = this.ensureAfterFlight || runtimeSessionId !== this.runtimeSessionId;
      this.ensurePromise = null;
      this.ensureAfterFlight = false;
      if (retryAfterFlight) void this.ensureLease();
    });
    this.ensurePromise = operation;
    return operation;
  }

  private async acquireAndReconcile(generation: number, runtimeSessionId: string): Promise<void> {
    let acquired: DecodedLeaseSnapshot | null = null;
    try {
      const initialStatus = decodeStatusResult(await this.options.call(
        "session.control.status",
        this.targetParams(),
      ));
      if (initialStatus === null) throw new InvalidLeaseResponse();
      if (!this.isCurrent(generation)) return;
      if (initialStatus.controllerKind === "desktop") {
        this.publishAuthoritativeStatus(runtimeSessionId, initialStatus);
        return;
      }

      acquired = decodeLeaseResult(await this.options.call(
        "session.control.acquire",
        this.targetParams(),
      ));
      if (acquired === null) throw new InvalidLeaseResponse();
      if (!this.isCurrent(generation)) {
        await this.releaseLease(acquired.leaseId, runtimeSessionId);
        return;
      }

      const status = decodeStatusResult(await this.options.call(
        "session.control.status",
        this.targetParams(),
      ));
      if (status === null) throw new InvalidLeaseResponse();
      if (!this.isCurrent(generation)) {
        await this.releaseLease(acquired.leaseId, runtimeSessionId);
        return;
      }
      if (!statusMatchesLease(status, acquired)) {
        this.publishAuthoritativeStatus(runtimeSessionId, status);
        await this.releaseLease(acquired.leaseId, runtimeSessionId);
        return;
      }
      const reconciled = {
        ...acquired,
        runtimeSessionId,
        pendingInput: status.pendingInput,
      };
      this.lease = reconciled;
      this.clearRecovery();
      this.publish(reconciled, pendingInputReason(reconciled.pendingInput));
      this.scheduleRenewal(generation);
    } catch (error) {
      if (!this.isCurrent(generation)) return;
      if (acquired !== null) await this.releaseLease(acquired.leaseId, runtimeSessionId);
      if (error instanceof CloudRpcFailure && error.code === 4203) {
        await this.reconcileControllerConflict(generation, runtimeSessionId);
        return;
      }
      this.failClosed(
        error instanceof InvalidLeaseResponse
          ? "Hermes returned an invalid controller lease."
          : "Controller lease could not be acquired. Try again after reconnecting.",
      );
    }
  }

  private async reconcileControllerConflict(
    generation: number,
    runtimeSessionId: string,
    retryIndex = 0,
  ): Promise<void> {
    try {
      const status = decodeStatusResult(await this.options.call(
        "session.control.status",
        this.targetParams(),
      ));
      if (status === null) throw new InvalidLeaseResponse();
      if (!this.isCurrent(generation)) return;
      this.publishAuthoritativeStatus(runtimeSessionId, status);
      if (status.controllerKind === "none") {
        this.requestEnsureLease();
        return;
      }
      if (status.controllerKind === "mobile") {
        this.scheduleControllerConflictRetry(generation, runtimeSessionId, retryIndex);
      }
    } catch {
      if (this.isCurrent(generation)) {
        this.failClosed("Another authoritative controller holds this Hermes session.");
      }
    }
  }

  private scheduleRenewal(generation: number): void {
    this.clearRenewal();
    const lease = this.lease;
    if (lease === null || !this.active) return;
    const delay = Math.max(0, lease.expiresAtEpochMs - RENEW_LEAD_MS - this.now());
    this.renewTimer = this.schedule(() => {
      this.renewTimer = null;
      void this.renew(generation);
    }, delay);
  }

  private async renew(generation: number): Promise<void> {
    const lease = this.lease;
    if (lease === null || !this.isCurrent(generation)) return;
    try {
      const renewed = decodeLeaseResult(await this.options.call(
        "session.control.renew",
        this.leaseParams(lease.leaseId, lease.runtimeSessionId),
      ));
      if (
        renewed === null
        || renewed.leaseId !== lease.leaseId
        || renewed.controllerLabel !== lease.controllerLabel
        || renewed.controlRevision < lease.controlRevision
        || renewed.expiresAtEpochMs <= this.now()
      ) {
        throw new InvalidLeaseResponse();
      }
      if (!this.isCurrent(generation) || this.lease?.leaseId !== lease.leaseId) return;
      const rebound = { ...renewed, runtimeSessionId: lease.runtimeSessionId };
      this.lease = rebound;
      this.publish(rebound, pendingInputReason(rebound.pendingInput));
      this.scheduleRenewal(generation);
    } catch {
      if (this.isCurrent(generation)) {
        this.failClosed("Controller lease was lost. Reconnect to try again.");
        this.scheduleRecovery();
      }
    }
  }

  private failClosed(reason: string): void {
    this.generation += 1;
    this.ensureAfterFlight = false;
    this.clearRenewal();
    this.clearRecovery();
    const controlRevision = this.lease?.controlRevision ?? 0;
    this.lease = null;
    this.publish(null, reason, controlRevision);
  }

  private publish(lease: LeaseSnapshot | null, unavailableReason: string | null, revision = 0): void {
    this.options.onStateChanged({
      leaseId: lease?.leaseId ?? null,
      runtimeSessionId: lease?.runtimeSessionId ?? this.runtimeSessionId,
      leaseExpiresAtEpochMs: lease?.expiresAtEpochMs ?? 0,
      controlRevision: lease?.controlRevision ?? revision,
      controllerKind: lease?.controllerKind ?? null,
      controllerLabel: lease?.controllerLabel ?? null,
      pendingInput: lease?.pendingInput ?? null,
      unavailableReason,
    });
  }

  private targetParams(): Record<string, unknown> {
    return { session_id: this.options.sessionId };
  }

  private leaseParams(leaseId: string, runtimeSessionId: string): Record<string, unknown> {
    void runtimeSessionId;
    return { ...this.targetParams(), lease_id: leaseId };
  }

  private isCurrent(generation: number): boolean {
    return this.active && generation === this.generation;
  }

  private clearRenewal(): void {
    if (this.renewTimer === null) return;
    this.cancel(this.renewTimer);
    this.renewTimer = null;
  }

  private scheduleRecovery(): void {
    if (
      this.recoveryTimer !== null
      || !this.active
      || this.runtimeSessionId === null
      || !REQUIRED_METHODS.every((method) => this.availableMethods.has(method))
    ) return;
    this.recoveryTimer = this.schedule(() => {
      this.recoveryTimer = null;
      void this.ensureLease();
    }, 500);
  }

  private scheduleControllerConflictRetry(
    generation: number,
    runtimeSessionId: string,
    retryIndex: number,
  ): void {
    const delay = CONTROLLER_CONFLICT_RETRY_DELAYS_MS[retryIndex];
    if (delay === undefined || this.recoveryTimer !== null || !this.isCurrent(generation)) return;
    this.recoveryTimer = this.schedule(() => {
      this.recoveryTimer = null;
      if (this.isCurrent(generation)) {
        void this.reconcileControllerConflict(generation, runtimeSessionId, retryIndex + 1);
      }
    }, delay);
  }

  private requestEnsureLease(): void {
    if (this.ensurePromise !== null) {
      this.ensureAfterFlight = true;
      return;
    }
    void this.ensureLease();
  }

  private clearRecovery(): void {
    if (this.recoveryTimer === null) return;
    this.cancel(this.recoveryTimer);
    this.recoveryTimer = null;
  }

  private publishAuthoritativeStatus(runtimeSessionId: string, status: StatusSnapshot): void {
    this.lease = null;
    this.clearRenewal();
    this.clearRecovery();
    const controller = status.controllerLabel ?? status.controllerKind;
    this.options.onStateChanged({
      leaseId: null,
      runtimeSessionId,
      leaseExpiresAtEpochMs: 0,
      controlRevision: status.controlRevision,
      controllerKind: status.controllerKind,
      controllerLabel: status.controllerLabel,
      pendingInput: status.pendingInput,
      unavailableReason: status.controllerKind === "none"
        ? "No authoritative controller is available."
        : `Controller is held by ${controller}.`,
    });
  }

  private async releaseLease(leaseId: string, runtimeSessionId: string): Promise<void> {
    if (!this.availableMethods.has("session.control.release")) return;
    try {
      const result = await this.options.call(
        "session.control.release",
        this.leaseParams(leaseId, runtimeSessionId),
      );
      if (!decodeReleaseResult(result)) throw new Error("invalid release response");
    } catch {
      // Best-effort release never restores local authority.
    }
  }
}

function waitForRelease<T>(release: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (signal === undefined) return release;
  if (signal.aborted) return Promise.reject(signal.reason);
  return new Promise<T>((resolve, reject) => {
    let settled = false;
    const finish = (callback: () => void) => {
      if (settled) return;
      settled = true;
      signal.removeEventListener("abort", abort);
      callback();
    };
    const abort = () => finish(() => reject(signal.reason));
    signal.addEventListener("abort", abort, { once: true });
    release.then(
      (value) => finish(() => resolve(value)),
      (error: unknown) => finish(() => reject(error)),
    );
  });
}

class InvalidLeaseResponse extends Error {}

function decodeLeaseResult(value: unknown): DecodedLeaseSnapshot | null {
  if (!isExactObject(value, [
    "lease_id",
    "expires_at_epoch_ms",
    "control_revision",
    "controller_kind",
    "controller_label",
    "pending_input",
  ])) return null;
  const pendingInput = decodePendingInput(value.pending_input);
  if (
    !isNonEmptyString(value.lease_id)
    || !isNonNegativeInteger(value.expires_at_epoch_ms)
    || !isNonNegativeInteger(value.control_revision)
    || value.controller_kind !== "mobile"
    || !isControllerLabel(value.controller_label)
    || pendingInput === undefined
  ) return null;
  return {
    leaseId: value.lease_id,
    expiresAtEpochMs: value.expires_at_epoch_ms,
    controlRevision: value.control_revision,
    controllerKind: "mobile",
    controllerLabel: value.controller_label,
    pendingInput,
  };
}

function decodeStatusResult(value: unknown): StatusSnapshot | null {
  if (!isExactObject(value, [
    "controller_kind",
    "controller_label",
    "control_revision",
    "lease_expires_at_epoch_ms",
    "pending_input",
  ])) return null;
  const pendingInput = decodePendingInput(value.pending_input);
  const controllerKind = value.controller_kind === "local"
    ? "desktop"
    : value.controller_kind;
  if (
    (
      controllerKind !== "desktop"
      && controllerKind !== "mobile"
      && controllerKind !== "none"
    )
    || (
      controllerKind === "none"
        ? value.controller_label !== null
        : !isControllerLabel(value.controller_label)
    )
    || !isNonNegativeInteger(value.control_revision)
    || !isNonNegativeInteger(value.lease_expires_at_epoch_ms)
    || pendingInput === undefined
  ) return null;
  return {
    leaseExpiresAtEpochMs: value.lease_expires_at_epoch_ms,
    controlRevision: value.control_revision,
    controllerKind,
    controllerLabel: value.controller_label as string | null,
    pendingInput,
  };
}

function statusMatchesLease(status: StatusSnapshot, lease: DecodedLeaseSnapshot): boolean {
  return status.controllerKind === "mobile"
    && status.controllerLabel === lease.controllerLabel
    && status.controlRevision === lease.controlRevision
    && status.leaseExpiresAtEpochMs === lease.expiresAtEpochMs;
}

function decodeReleaseResult(value: unknown): boolean {
  return isExactObject(value, ["released", "control_revision"])
    && value.released === true
    && isNonNegativeInteger(value.control_revision);
}

function decodePendingInput(value: unknown): RuntimePendingInput | null | undefined {
  if (value === null) return null;
  if (!isRecord(value) || typeof value.kind !== "string") return undefined;
  if (value.kind === "approval") return decodePendingApproval(value);
  if (value.kind === "clarify") return decodePendingClarification(value);
  return undefined;
}

function decodePendingApproval(value: Record<string, unknown>): RuntimePendingApproval | undefined {
  if (!isExactObject(value, [
    "request_id",
    "kind",
    "title",
    "description",
    "command",
    "choices",
    "expires_at_epoch_ms",
  ])) return undefined;
  if (
    value.kind !== "approval"
    || !isNonEmptyString(value.request_id)
    || !isNonEmptyString(value.title)
    || typeof value.description !== "string"
    || typeof value.command !== "string"
    || !Array.isArray(value.choices)
    || value.choices.length === 0
    || !value.choices.every((choice) => typeof choice === "string" && APPROVAL_CHOICES.has(choice))
    || new Set(value.choices).size !== value.choices.length
    || !isNonNegativeInteger(value.expires_at_epoch_ms)
    || !isDisplaySafeHermesValue(value)
  ) return undefined;
  return {
    kind: "approval",
    requestId: value.request_id,
    title: value.title,
    description: value.description,
    command: value.command,
    choices: value.choices as RuntimePendingApproval["choices"],
    expiresAtEpochMs: value.expires_at_epoch_ms,
  };
}

function decodePendingClarification(value: Record<string, unknown>): RuntimePendingClarification | undefined {
  if (!isExactObject(value, [
    "request_id",
    "kind",
    "question",
    "choices",
    "allow_other",
    "expires_at_epoch_ms",
  ])) return undefined;
  if (
    value.kind !== "clarify"
    || !isNonEmptyString(value.request_id)
    || !isNonEmptyString(value.question)
    || !Array.isArray(value.choices)
    || typeof value.allow_other !== "boolean"
    || !isNonNegativeInteger(value.expires_at_epoch_ms)
    || !isDisplaySafeHermesValue(value)
  ) return undefined;
  const choices: Array<{ id: string; label: string }> = [];
  for (const choice of value.choices) {
    if (!isExactObject(choice, ["id", "label"]) || !isNonEmptyString(choice.id) || !isNonEmptyString(choice.label)) {
      return undefined;
    }
    choices.push({ id: choice.id, label: choice.label });
  }
  if (
    (choices.length === 0 && !value.allow_other)
    || new Set(choices.map((choice) => choice.id)).size !== choices.length
    || new Set(choices.map((choice) => choice.label)).size !== choices.length
  ) return undefined;
  return {
    kind: "clarify",
    requestId: value.request_id,
    question: value.question,
    choices,
    allowOther: value.allow_other,
    expiresAtEpochMs: value.expires_at_epoch_ms,
  };
}

function pendingInputReason(pendingInput: RuntimePendingInput | null): string | null {
  return pendingInput?.kind === "clarify"
    ? "A clarification is waiting; use Hermes Mobile to answer it."
    : null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isExactObject(value: unknown, keys: readonly string[]): value is Record<string, unknown> {
  if (!isRecord(value)) return false;
  const actual = Object.keys(value);
  return actual.length === keys.length && actual.every((key) => keys.includes(key));
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function isControllerLabel(value: unknown): value is string {
  return isDisplaySafeHermesText(value, 1, HERMES_DISPLAY_NAME_MAX_CODE_POINTS);
}

function isNonNegativeInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) >= 0;
}
