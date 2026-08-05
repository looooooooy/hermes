// @vitest-environment node

import {
  BoundedJsonResponseAborted,
  BoundedJsonResponseInvalid,
  BoundedJsonResponseTooLarge,
  readBoundedJsonResponse,
} from "./boundedJsonResponse";

describe("readBoundedJsonResponse", () => {
  it("parses valid chunked JSON without using Response.text or Response.json", async () => {
    const response = chunkedResponse(["{\"ok\":", "true}"]);
    response.text = vi.fn(async () => { throw new Error("must not buffer via text"); });
    response.json = vi.fn(async () => { throw new Error("must not buffer via json"); });

    await expect(readBoundedJsonResponse(response, { maximumBytes: 64 }))
      .resolves.toEqual({ ok: true });
    expect(response.text).not.toHaveBeenCalled();
    expect(response.json).not.toHaveBeenCalled();
  });

  it("cancels a chunked response immediately when incremental bytes exceed the bound", async () => {
    const cancel = vi.fn();
    let pullCount = 0;
    const chunks = [new Uint8Array(5), new Uint8Array(5), new Uint8Array(5)];
    const response = new Response(new ReadableStream<Uint8Array>({
      pull(controller) {
        const chunk = chunks[pullCount++];
        if (chunk === undefined) controller.close();
        else controller.enqueue(chunk);
      },
      cancel,
    }));

    await expect(readBoundedJsonResponse(response, { maximumBytes: 8 }))
      .rejects.toBeInstanceOf(BoundedJsonResponseTooLarge);
    expect(cancel).toHaveBeenCalledOnce();
    expect(pullCount).toBeLessThan(chunks.length + 1);
  });

  it("cancels and rejects an already-aborted read", async () => {
    const cancel = vi.fn();
    const response = new Response(new ReadableStream<Uint8Array>({ cancel }));
    const controller = new AbortController();
    controller.abort();

    await expect(readBoundedJsonResponse(response, {
      maximumBytes: 64,
      signal: controller.signal,
    })).rejects.toBeInstanceOf(BoundedJsonResponseAborted);
    expect(cancel).toHaveBeenCalledOnce();
  });

  it("preserves TooLarge when Content-Length cancellation rejects", async () => {
    const response = new Response(new ReadableStream<Uint8Array>({
      cancel: () => Promise.reject(new Error("cancel-failed")),
    }), { headers: { "Content-Length": "65" } });

    await expect(readBoundedJsonResponse(response, { maximumBytes: 64 }))
      .rejects.toBeInstanceOf(BoundedJsonResponseTooLarge);
    expect(response.body?.locked).toBe(false);
  });

  it("preserves TooLarge when chunked overflow cancellation rejects and releases the lock", async () => {
    const response = new Response(new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array(65));
      },
      cancel: () => Promise.reject(new Error("cancel-failed")),
    }));

    await expect(readBoundedJsonResponse(response, { maximumBytes: 64 }))
      .rejects.toBeInstanceOf(BoundedJsonResponseTooLarge);
    expect(response.body?.locked).toBe(false);
  });

  it("preserves Aborted when a pre-abort cancellation rejects and releases the lock", async () => {
    const response = new Response(new ReadableStream<Uint8Array>({
      cancel: () => Promise.reject(new Error("cancel-failed")),
    }));
    const controller = new AbortController();
    controller.abort();

    await expect(readBoundedJsonResponse(response, {
      maximumBytes: 64,
      signal: controller.signal,
    })).rejects.toBeInstanceOf(BoundedJsonResponseAborted);
    expect(response.body?.locked).toBe(false);
  });

  it("preserves Aborted when mid-read cancellation and the pending read reject", async () => {
    const response = new Response(new ReadableStream<Uint8Array>({
      cancel: () => Promise.reject(new Error("cancel-failed")),
    }));
    const controller = new AbortController();
    const reading = readBoundedJsonResponse(response, {
      maximumBytes: 64,
      signal: controller.signal,
    });

    controller.abort();

    await expect(reading).rejects.toBeInstanceOf(BoundedJsonResponseAborted);
    expect(response.body?.locked).toBe(false);
  });

  it("maps an ordinary stream read failure to Invalid and releases the lock", async () => {
    const response = new Response(new ReadableStream<Uint8Array>({
      start(controller) {
        controller.error(new Error("read-failed"));
      },
    }));

    await expect(readBoundedJsonResponse(response, { maximumBytes: 64 }))
      .rejects.toBeInstanceOf(BoundedJsonResponseInvalid);
    expect(response.body?.locked).toBe(false);
  });
});

function chunkedResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  return new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  }));
}
