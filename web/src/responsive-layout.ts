export function sidebarDragIntent(dx: number, dy: number): "pending" | "horizontal" | "vertical" {
  const x = Math.abs(dx), y = Math.abs(dy);
  if (Math.max(x, y) < 10) return "pending";
  if (x > y * 1.25) return "horizontal";
  if (y > x * 1.25) return "vertical";
  // A diagonal first sample is not enough to abandon the entire gesture.
  return "pending";
}

const PANEL_MIN_WIDTH_PX = 360;
const CHAT_MIN_WIDTH_PX = 420;
const PANEL_MAX_VIEWPORT_RATIO = 0.72;

export function clampPanelWidth(width: number, viewportWidth: number): number {
  const maxWidth = Math.max(
    PANEL_MIN_WIDTH_PX,
    Math.min(viewportWidth - CHAT_MIN_WIDTH_PX, viewportWidth * PANEL_MAX_VIEWPORT_RATIO),
  );
  return Math.round(Math.min(Math.max(width, PANEL_MIN_WIDTH_PX), maxWidth));
}
