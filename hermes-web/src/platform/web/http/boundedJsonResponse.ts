export class BoundedJsonResponseTooLarge extends Error {
  constructor() {
    super("JSON response exceeds the byte limit");
    this.name = "BoundedJsonResponseTooLarge";
  }
}

export class BoundedJsonResponseInvalid extends Error {
  constructor(cause?: unknown) {
    super("JSON response is invalid", cause === undefined ? undefined : { cause });
    this.name = "BoundedJsonResponseInvalid";
  }
}

export class BoundedJsonResponseAborted extends Error {
  constructor() {
    super("JSON response read was aborted");
    this.name = "AbortError";
  }
}

interface BoundedJsonResponseOptions {
  maximumBytes: number;
  signal?: AbortSignal;
}

export async function readBoundedJsonResponse(
  response: Response,
  { maximumBytes, signal }: BoundedJsonResponseOptions,
): Promise<unknown> {
  if (!Number.isSafeInteger(maximumBytes) || maximumBytes < 1) {
    throw new TypeError("maximumBytes must be a positive safe integer");
  }
  const advertisedLength = response.headers.get("Content-Length");
  if (advertisedLength !== null) {
    const length = Number(advertisedLength);
    if (!Number.isSafeInteger(length) || length < 0 || length > maximumBytes) {
      await safeCancelUnlockedBody(response);
      throw new BoundedJsonResponseTooLarge();
    }
  }
  if (response.body === null) throw new BoundedJsonResponseInvalid();
  const reader = response.body.getReader();
  let aborted = signal?.aborted ?? false;
  const abort = () => {
    aborted = true;
    void safeCancelReader(reader, signal?.reason);
  };
  signal?.addEventListener("abort", abort, { once: true });
  try {
    if (aborted) {
      await safeCancelReader(reader, signal?.reason);
      throw new BoundedJsonResponseAborted();
    }
    const chunks: Uint8Array[] = [];
    let byteLength = 0;
    while (true) {
      let result: ReadableStreamReadResult<Uint8Array>;
      try {
        result = await reader.read();
      } catch (error) {
        if (aborted || signal?.aborted) throw new BoundedJsonResponseAborted();
        throw new BoundedJsonResponseInvalid(error);
      }
      if (aborted) throw new BoundedJsonResponseAborted();
      const { done, value } = result;
      if (done) break;
      byteLength += value.byteLength;
      if (byteLength > maximumBytes) {
        await safeCancelReader(reader);
        throw new BoundedJsonResponseTooLarge();
      }
      chunks.push(value);
    }
    const bytes = new Uint8Array(byteLength);
    let offset = 0;
    for (const chunk of chunks) {
      bytes.set(chunk, offset);
      offset += chunk.byteLength;
    }
    try {
      return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)) as unknown;
    } catch (error) {
      throw new BoundedJsonResponseInvalid(error);
    }
  } finally {
    signal?.removeEventListener("abort", abort);
    safeReleaseReaderLock(reader);
  }
}

async function safeCancelUnlockedBody(response: Response): Promise<void> {
  if (response.body === null || response.body.locked) return;
  try {
    await response.body.cancel();
  } catch {
    // Cancellation is cleanup and must not replace the primary classification.
  }
}

async function safeCancelReader(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  reason?: unknown,
): Promise<void> {
  try {
    await reader.cancel(reason);
  } catch {
    // Cancellation is cleanup and must not replace the primary classification.
  }
}

function safeReleaseReaderLock(reader: ReadableStreamDefaultReader<Uint8Array>): void {
  try {
    reader.releaseLock();
  } catch {
    // The primary parse/read classification remains authoritative.
  }
}
