import "@testing-library/jest-dom/vitest";

class ResizeObserverStub implements ResizeObserver {
  disconnect(): void {}
  observe(): void {}
  unobserve(): void {}
}

globalThis.ResizeObserver = ResizeObserverStub;

if (typeof HTMLElement !== "undefined") {
  HTMLElement.prototype.scrollIntoView = vi.fn();
}
