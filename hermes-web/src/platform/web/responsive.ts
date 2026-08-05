export type LayoutMode = "compact" | "wide";

export const WIDE_LAYOUT_MIN_WIDTH = 768;

export function layoutModeForWidth(width: number): LayoutMode {
  return width < WIDE_LAYOUT_MIN_WIDTH ? "compact" : "wide";
}
