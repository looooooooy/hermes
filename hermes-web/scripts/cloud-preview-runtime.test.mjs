// @vitest-environment node

import { spawn } from "node:child_process";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { describe, expect, it, vi } from "vitest";
import {
  createVitePreviewArguments,
  extractPreviewOrigin,
  stopProcess,
  waitForPreviewOrigin,
  waitForPreviewReady,
} from "./cloud-preview-runtime.mjs";

describe("Cloud preview process runtime", () => {
  it("asks Vite for an ephemeral port and extracts the actual origin", () => {
    expect(createVitePreviewArguments("/vite.js")).toEqual([
      "/vite.js",
      "preview",
      "--mode",
      "cloud",
      "--host",
      "127.0.0.1",
      "--port",
      "0",
      "--strictPort",
    ]);
    expect(extractPreviewOrigin("\u001B[32m➜  Local:\u001B[0m http://127.0.0.1:54321/\n")).toBe(
      "http://127.0.0.1:54321",
    );
  });

  it("registers the exit listener before a synchronous SIGTERM exit", async () => {
    const child = new FakeChild({ exitOn: "SIGTERM" });

    await stopProcess(child, { graceMs: 20, forceMs: 20 });

    expect(child.listenerCountAtKill).toEqual([1]);
    expect(child.signals).toEqual(["SIGTERM"]);
    expect(child.listenerCount("exit")).toBe(0);
  });

  it("cleans the first listener before a synchronous forced exit", async () => {
    const child = new FakeChild({ exitOn: "SIGKILL" });

    await stopProcess(child, { graceMs: 5, forceMs: 20 });

    expect(child.listenerCountAtKill).toEqual([1, 1]);
    expect(child.signals).toEqual(["SIGTERM", "SIGKILL"]);
    expect(child.listenerCount("exit")).toBe(0);
  });

  it("reports an exit that happened before startup observation", async () => {
    const child = new FakeChild({ exitOn: undefined });
    child.exitCode = 1;

    await expect(waitForPreviewOrigin(child, { timeoutMs: 20 })).rejects.toThrow(
      "Vite preview exited before startup with code 1",
    );

    expect(child.stdout.listenerCount("data")).toBe(0);
    expect(child.stderr.listenerCount("data")).toBe(0);
    expect(child.listenerCount("exit")).toBe(0);
  });

  it("handles a startup child that emits only close and cleans every resource", async () => {
    vi.useFakeTimers();
    try {
      const child = new CloseOnlyChild();
      const startup = waitForPreviewOrigin(child, { timeoutMs: 20 });
      const startupAssertion = expect(startup).rejects.toThrow(
        "Vite preview exited before startup with code 7",
      );

      child.emit("close", 7, null);
      await vi.runAllTimersAsync();

      await startupAssertion;
      expect(child.stdout.listenerCount("data")).toBe(0);
      expect(child.stderr.listenerCount("data")).toBe(0);
      expect(child.listenerCount("error")).toBe(0);
      expect(child.listenerCount("exit")).toBe(0);
      expect(child.listenerCount("close")).toBe(0);
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("removes shutdown listeners after both signals time out", async () => {
    const child = new FakeChild({ exitOn: undefined });

    await expect(stopProcess(child, { graceMs: 1, forceMs: 1 })).rejects.toThrow(
      "Preview process did not exit after SIGKILL",
    );

    expect(child.signals).toEqual(["SIGTERM", "SIGKILL"]);
    expect(child.listenerCount("exit")).toBe(0);
  });

  it("stops on a close-only event and cleans every listener and timer", async () => {
    vi.useFakeTimers();
    try {
      const child = new CloseOnlyChild();
      const stopping = stopProcess(child, { graceMs: 20, forceMs: 20 });

      await vi.runAllTimersAsync();
      await expect(stopping).resolves.toBeUndefined();

      expect(child.signals).toEqual(["SIGTERM"]);
      expect(child.listenerCount("error")).toBe(0);
      expect(child.listenerCount("exit")).toBe(0);
      expect(child.listenerCount("close")).toBe(0);
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps startup and shutdown fail-safe timers referenced while pending", async () => {
      const timeoutSpy = vi.spyOn(globalThis, "setTimeout");
    try {
      const startupChild = new FakeChild({ exitOn: undefined });
      const startup = waitForPreviewOrigin(startupChild, { timeoutMs: 5 });
      const startupTimerIsReferenced = timeoutSpy.mock.results.at(-1).value.hasRef();
      await expect(startup).rejects.toThrow("Timed out waiting for Vite preview startup");

      const stopChild = new FakeChild({ exitOn: undefined });
      const stopping = stopProcess(stopChild, { graceMs: 5, forceMs: 5 });
      const stopTimerIsReferenced = timeoutSpy.mock.results.at(-1).value.hasRef();
      await expect(stopping).rejects.toThrow("Preview process did not exit after SIGKILL");
      expect(startupTimerIsReferenced).toBe(true);
      expect(stopTimerIsReferenced).toBe(true);
    } finally {
      timeoutSpy.mockRestore();
    }
  });

  it("stops a real child process with SIGTERM", async () => {
    const child = spawnReadyChild("setInterval(() => {}, 1_000);");
    await waitForPreviewOrigin(child);

    await stopProcess(child, { graceMs: 1_000, forceMs: 1_000 });

    expect(child.exitCode).toBeNull();
    expect(child.signalCode).toBe("SIGTERM");
  });

  it("escalates a real child process that ignores SIGTERM to SIGKILL", async () => {
    const child = spawnReadyChild(
      'process.on("SIGTERM", () => {}); setInterval(() => {}, 1_000);',
    );
    await waitForPreviewOrigin(child);

    await stopProcess(child, { graceMs: 30, forceMs: 1_000 });

    expect(child.exitCode).toBeNull();
    expect(child.signalCode).toBe("SIGKILL");
  });

  it("does not signal a real child again when signalCode already records termination", async () => {
    const child = spawnReadyChild("setInterval(() => {}, 1_000);");
    await waitForPreviewOrigin(child);
    const closed = new Promise((resolve) => child.once("close", resolve));
    child.kill("SIGTERM");
    await closed;
    let repeatedSignals = 0;
    const originalKill = child.kill.bind(child);
    child.kill = (...arguments_) => {
      repeatedSignals += 1;
      return originalKill(...arguments_);
    };

    await stopProcess(child, { graceMs: 5, forceMs: 5 });

    expect(child.signalCode).toBe("SIGTERM");
    expect(repeatedSignals).toBe(0);
    await expect(waitForPreviewOrigin(child, { timeoutMs: 5 })).rejects.toThrow(
      "signal SIGTERM",
    );
  });

  it("reports a real spawn error instead of waiting for the startup timeout", async () => {
    const child = spawn("/definitely-not-a-hermes-executable", [], {
      stdio: ["ignore", "pipe", "pipe"],
    });

    await expect(waitForPreviewOrigin(child, { timeoutMs: 1_000 })).rejects.toThrow(
      "Vite preview failed to start",
    );

    expect(child.listenerCount("error")).toBe(0);
    expect(child.listenerCount("exit")).toBe(0);
    expect(child.listenerCount("close")).toBe(0);
  });

  it("fails readiness immediately for a pre-existing signalCode", async () => {
    vi.useFakeTimers();
    try {
      const child = new FakeChild({ exitOn: undefined });
      child.signalCode = "SIGTERM";
      const fetchImpl = vi.fn();
      const readiness = waitForPreviewReady("http://127.0.0.1:54321", child, {
        fetchImpl,
        timeoutMs: 20,
      });
      const readinessAssertion = expect(readiness).rejects.toThrow("signal SIGTERM");

      await vi.runAllTimersAsync();

      await readinessAssertion;
      expect(fetchImpl).not.toHaveBeenCalled();
      expectLifecycleResourcesClean(child);
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("fails readiness on a close-only event and cleans every resource", async () => {
    vi.useFakeTimers();
    try {
      const child = new CloseOnlyChild();
      const readiness = waitForPreviewReady("http://127.0.0.1:54321", child, {
        fetchImpl: pendingFetch,
        timeoutMs: 20,
      });
      const readinessAssertion = expect(readiness).rejects.toThrow(
        "Vite preview exited before readiness with code 9",
      );

      child.emit("close", 9, null);
      await vi.runAllTimersAsync();

      await readinessAssertion;
      expectLifecycleResourcesClean(child);
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("fails readiness on child error and cleans every resource", async () => {
    vi.useFakeTimers();
    try {
      const child = new CloseOnlyChild();
      const readiness = waitForPreviewReady("http://127.0.0.1:54321", child, {
        fetchImpl: pendingFetch,
        timeoutMs: 20,
      });
      const readinessAssertion = expect(readiness).rejects.toThrow(
        "Vite preview failed while waiting for readiness: broken child",
      );

      child.emit("error", new Error("broken child"));
      await vi.runAllTimersAsync();

      await readinessAssertion;
      expectLifecycleResourcesClean(child);
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });
});

class FakeChild extends EventEmitter {
  exitCode = null;
  signalCode = null;
  listenerCountAtKill = [];
  signals = [];
  stdout = new PassThrough();
  stderr = new PassThrough();

  constructor({ exitOn }) {
    super();
    this.exitOn = exitOn;
  }

  kill(signal) {
    this.listenerCountAtKill.push(this.listenerCount("exit"));
    this.signals.push(signal);
    if (signal === this.exitOn) {
      this.signalCode = signal;
      this.emit("exit", null, signal);
    }
    return true;
  }
}

class CloseOnlyChild extends EventEmitter {
  exitCode = null;
  signalCode = null;
  signals = [];
  stdout = new PassThrough();
  stderr = new PassThrough();

  kill(signal) {
    this.signals.push(signal);
    this.emit("close", null, signal);
    return true;
  }
}

function spawnReadyChild(source) {
  return spawn(
    process.execPath,
    ["-e", `${source} process.stdout.write("http://127.0.0.1:54321\\n");`],
    { stdio: ["ignore", "pipe", "pipe"] },
  );
}

function pendingFetch(_url, { signal }) {
  return new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => reject(signal.reason), { once: true });
  });
}

function expectLifecycleResourcesClean(child) {
  expect(child.listenerCount("error")).toBe(0);
  expect(child.listenerCount("exit")).toBe(0);
  expect(child.listenerCount("close")).toBe(0);
}
