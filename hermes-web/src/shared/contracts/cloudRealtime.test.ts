import { decodeCloudRealtimeFrame } from "./cloudRealtime";
import mobileControl from "./generated/mobile-control-v1.json";

describe("cloud realtime v1 decoder", () => {
  it.each([
    {
      jsonrpc: "2.0",
      method: "event",
      params: {
        type: "gateway.ready",
        payload: { observer_contract: 1, connection_role: "observer" },
      },
    },
    {
      jsonrpc: "2.0",
      method: "event",
      params: {
        type: "gateway.ready",
        payload: {
          observer_contract: 1,
          control_contract: 1,
          connection_role: "control",
          control_available_methods: [],
          control_error_codes: mobileControl.error_codes,
        },
      },
    },
  ])("accepts an exact role-aware gateway ready frame", (frame) => {
    expect(decodeCloudRealtimeFrame(frame)).toEqual({ ok: true, value: frame });
  });

  it("requires the complete canonical control error catalog", () => {
    const exact = {
      jsonrpc: "2.0",
      method: "event",
      params: {
        type: "gateway.ready",
        payload: {
          observer_contract: 1,
          control_contract: 1,
          connection_role: "control",
          control_available_methods: [],
          control_error_codes: mobileControl.error_codes,
        },
      },
    };
    const incomplete = structuredClone(exact);
    delete (incomplete.params.payload.control_error_codes as Partial<typeof mobileControl.error_codes>)
      .control_role_required;

    expect(decodeCloudRealtimeFrame(exact)).toEqual({ ok: true, value: exact });
    expect(decodeCloudRealtimeFrame(incomplete)).toEqual({ ok: false, reason: "invalid_frame" });
  });

  it("accepts a contract-valid observer event", () => {
    const decoded = decodeCloudRealtimeFrame({
      jsonrpc: "2.0",
      method: "event",
      params: {
        type: "thinking.delta",
        session_id: "runtime-session",
        session_key: "mobile-56f3",
        event_sequence: 12,
        payload: { text: "Analyzing request…" },
      },
    });

    expect(decoded.ok).toBe(true);
  });

  it("accepts the current Cloud lease-method tuple without imposing lexical order", () => {
    const frame = {
      jsonrpc: "2.0",
      method: "event",
      params: {
        type: "gateway.ready",
        payload: {
          observer_contract: 1,
          control_contract: 1,
          connection_role: "control",
          control_available_methods: [
            "session.control.acquire",
            "session.control.renew",
            "session.control.release",
            "session.control.status",
          ],
          control_error_codes: mobileControl.error_codes,
        },
      },
    };
    expect(decodeCloudRealtimeFrame(frame)).toEqual({ ok: true, value: frame });
  });

  it.each([
    ["rejects a reserved future method", [...mobileControl.available_methods.slice(0, 9), "session.redirect"]],
    ["rejects an eleventh advertised method", [...mobileControl.available_methods, "sudo.respond"]],
  ])("%s", (_label, methods) => {
    const frame = {
      jsonrpc: "2.0",
      method: "event",
      params: {
        type: "gateway.ready",
        payload: {
          observer_contract: 1,
          control_contract: 1,
          connection_role: "control",
          control_available_methods: methods,
          control_error_codes: mobileControl.error_codes,
        },
      },
    };

    expect(decodeCloudRealtimeFrame(frame)).toEqual({ ok: false, reason: "invalid_frame" });
  });

  it.each([
    {
      jsonrpc: "2.0",
      method: "event",
      params: {
        type: "private.reasoning",
        session_id: "runtime-session",
        session_key: "mobile-56f3",
        event_sequence: 12,
        payload: { text: "must not render" },
      },
    },
    {
      jsonrpc: "2.0",
      method: "event",
      params: {
        type: "thinking.delta",
        session_id: "runtime-session",
        session_key: "mobile-56f3",
        event_sequence: 12,
        payload: { text: "ok", token: "must not pass" },
      },
    },
    {
      jsonrpc: "2.0",
      method: "event",
      params: {
        type: "status.update",
        session_id: "runtime-session",
        session_key: "mobile-56f3",
        event_sequence: 12,
        payload: { status: "idle", running: true },
      },
    },
    {
      jsonrpc: "2.0",
      method: "event",
      params: {
        type: "gateway.ready",
        payload: { connection_role: "observer" },
      },
    },
    {
      jsonrpc: "2.0",
      method: "event",
      params: {
        type: "gateway.ready",
        payload: {
          observer_contract: 1,
          connection_role: "observer",
          extra: true,
        },
      },
    },
  ])("fails closed for unknown or malformed frames", (frame) => {
    expect(decodeCloudRealtimeFrame(frame)).toEqual({ ok: false, reason: "invalid_frame" });
  });
});
