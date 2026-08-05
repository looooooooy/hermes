import { getOrCreateBrowserClientInstanceId } from "./BrowserClientInstanceId";

describe("getOrCreateBrowserClientInstanceId", () => {
  it("reuses the same canonical client id across reloads and stores nothing else", () => {
    localStorage.clear();
    const createUuid = vi.fn(() => "11111111-1111-4111-8111-111111111111");

    expect(getOrCreateBrowserClientInstanceId(localStorage, createUuid))
      .toBe("11111111-1111-4111-8111-111111111111");
    expect(getOrCreateBrowserClientInstanceId(localStorage, createUuid))
      .toBe("11111111-1111-4111-8111-111111111111");
    expect(createUuid).toHaveBeenCalledTimes(1);
    expect(Object.keys(localStorage)).toEqual(["hermes.web.client_instance_id.v1"]);
  });

  it("replaces corrupt storage with a new canonical UUID without logging it", () => {
    localStorage.clear();
    localStorage.setItem("hermes.web.client_instance_id.v1", "corrupt");
    const log = vi.spyOn(console, "log").mockImplementation(() => undefined);
    const debug = vi.spyOn(console, "debug").mockImplementation(() => undefined);

    expect(getOrCreateBrowserClientInstanceId(
      localStorage,
      () => "22222222-2222-4222-8222-222222222222",
    )).toBe("22222222-2222-4222-8222-222222222222");
    expect(localStorage.getItem("hermes.web.client_instance_id.v1"))
      .toBe("22222222-2222-4222-8222-222222222222");
    expect(log).not.toHaveBeenCalled();
    expect(debug).not.toHaveBeenCalled();
  });
});
