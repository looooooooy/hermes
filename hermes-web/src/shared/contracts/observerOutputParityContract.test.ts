import { CLOUD_OBSERVER_V2_EVENT_TYPES } from "./cloudRealtimeV2";

describe("observer output parity v2 contract capabilities", () => {
  it("advertises the authoritative Todo lifecycle event", () => {
    expect(CLOUD_OBSERVER_V2_EVENT_TYPES).toContain("todo.update");
  });

  it("advertises the authoritative Subagent lifecycle event", () => {
    expect(CLOUD_OBSERVER_V2_EVENT_TYPES).toContain("subagent.update");
  });
});
