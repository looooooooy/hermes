export function createVitePreviewArguments(viteCli) {
  return [
    viteCli,
    "preview",
    "--mode",
    "cloud",
    "--host",
    "127.0.0.1",
    "--port",
    "0",
    "--strictPort",
  ];
}

export function extractPreviewOrigin(output) {
  return output.match(/http:\/\/127\.0\.0\.1:\d+/)?.[0];
}

export function waitForPreviewOrigin(child, { timeoutMs = 20_000 } = {}) {
  return new Promise((resolve, reject) => {
    let output = "";
    let settled = false;
    let observer;
    const onData = (chunk) => {
      output = `${output}${String(chunk)}`.slice(-4_000);
      const origin = extractPreviewOrigin(output);
      if (origin !== undefined) finish(() => resolve(origin));
    };
    const onError = (error) => finish(() => {
      reject(new Error(`Vite preview failed to start: ${error.message}`, { cause: error }));
    });
    const onTermination = (code, signal) => finish(() => {
      reject(new Error(`Vite preview exited before startup ${terminationDetail(child, code, signal)}`));
    });
    const timer = setTimeout(() => finish(() => {
      reject(new Error("Timed out waiting for Vite preview startup"));
    }), timeoutMs);
    child.stdout?.on("data", onData);
    child.stderr?.on("data", onData);
    observer = observeChildLifecycle(child, { onError, onTermination });
    observer.check();

    function finish(complete) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.stdout?.off("data", onData);
      child.stderr?.off("data", onData);
      observer?.dispose();
      complete();
    }
  });
}

export function waitForPreviewReady(
  origin,
  child,
  {
    fetchImpl = fetch,
    timeoutMs = 20_000,
    retryMs = 100,
    requestTimeoutMs = 1_000,
  } = {},
) {
  return new Promise((resolve, reject) => {
    let settled = false;
    let observer;
    let retryTimer;
    let requestTimer;
    let requestController;
    const timeoutTimer = setTimeout(() => finish(() => {
      reject(new Error("Timed out waiting for Vite preview readiness"));
    }), timeoutMs);
    const onError = (error) => finish(() => {
      reject(new Error(
        `Vite preview failed while waiting for readiness: ${error.message}`,
        { cause: error },
      ));
    });
    const onTermination = (code, signal) => finish(() => {
      reject(new Error(`Vite preview exited before readiness ${terminationDetail(child, code, signal)}`));
    });

    observer = observeChildLifecycle(child, { onError, onTermination });
    observer.check();
    if (!settled) attempt();

    function attempt() {
      requestController = new AbortController();
      const activeController = requestController;
      requestTimer = setTimeout(() => activeController.abort(), requestTimeoutMs);
      Promise.resolve()
        .then(() => fetchImpl(origin, { signal: activeController.signal }))
        .then((response) => {
          clearRequestTimeout(activeController);
          if (settled) return;
          if (response.ok) finish(resolve);
          else scheduleRetry();
        })
        .catch(() => {
          clearRequestTimeout(activeController);
          if (!settled) scheduleRetry();
        });
    }

    function scheduleRetry() {
      retryTimer = setTimeout(attempt, retryMs);
    }

    function clearRequestTimeout(controller) {
      if (requestController !== controller) return;
      clearTimeout(requestTimer);
      requestTimer = undefined;
      requestController = undefined;
    }

    function finish(complete) {
      if (settled) return;
      settled = true;
      clearTimeout(timeoutTimer);
      clearTimeout(retryTimer);
      clearTimeout(requestTimer);
      requestController?.abort();
      requestController = undefined;
      observer?.dispose();
      complete();
    }
  });
}

export async function stopProcess(
  child,
  { graceMs = 3_000, forceMs = 3_000 } = {},
) {
  if (hasExited(child)) return;
  if (await signalAndWait(child, "SIGTERM", graceMs)) return;
  if (await signalAndWait(child, "SIGKILL", forceMs)) return;
  throw new Error("Preview process did not exit after SIGKILL");
}

function signalAndWait(child, signal, timeoutMs) {
  return new Promise((resolve, reject) => {
    let settled = false;
    let observer;
    const onTermination = () => finish(() => resolve(true));
    const onError = (error) => finish(() => {
      if (hasExited(child)) resolve(true);
      else reject(error);
    });
    const timer = setTimeout(() => finish(() => resolve(hasExited(child))), timeoutMs);
    observer = observeChildLifecycle(child, { onError, onTermination });
    observer.check();
    if (settled) return;
    try {
      const signaled = child.kill(signal);
      if (hasExited(child)) observer.check();
      else if (!signaled) finish(() => resolve(false));
    } catch (error) {
      finish(() => reject(error));
    }

    function finish(complete) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      observer?.dispose();
      complete();
    }
  });
}

function observeChildLifecycle(child, { onError, onTermination }) {
  let active = true;
  const handleError = (error) => {
    if (active) onError(error);
  };
  const handleTermination = (code, signal) => {
    if (active) onTermination(code, signal);
  };
  child.once("error", handleError);
  child.once("exit", handleTermination);
  child.once("close", handleTermination);

  return {
    check() {
      if (active && hasExited(child)) {
        handleTermination(child.exitCode, child.signalCode);
      }
    },
    dispose() {
      if (!active) return;
      active = false;
      child.off("error", handleError);
      child.off("exit", handleTermination);
      child.off("close", handleTermination);
    },
  };
}

function hasExited(child) {
  return child.exitCode !== null || child.signalCode !== null;
}

function terminationDetail(child, code, signal) {
  const knownSignal = signal ?? child.signalCode;
  if (knownSignal !== null && knownSignal !== undefined) return `from signal ${knownSignal}`;
  return `with code ${code ?? child.exitCode ?? "unknown"}`;
}
