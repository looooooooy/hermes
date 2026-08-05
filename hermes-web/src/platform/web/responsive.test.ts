import { layoutModeForWidth } from "./responsive";

describe("responsive layout contract", () => {
  it("uses the compact phone layout below 768px", () => {
    expect(layoutModeForWidth(390)).toBe("compact");
    expect(layoutModeForWidth(767)).toBe("compact");
  });

  it("uses the wide shell at and above 768px", () => {
    expect(layoutModeForWidth(768)).toBe("wide");
    expect(layoutModeForWidth(1440)).toBe("wide");
  });
});
