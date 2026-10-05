export function sidebarDragIntent(dx: number, dy: number): "pending" | "horizontal" | "vertical" {
  if (Math.max(Math.abs(dx), Math.abs(dy)) < 10) return "pending";
  return Math.abs(dx) > Math.abs(dy) * 1.25 ? "horizontal" : "vertical";
}

export function sidebarReleaseOpen(offset: number, width: number, velocity: number): boolean {
  // A recent flick carries the drawer; a held/slow drag settles by distance.
  if (Math.abs(velocity) >= 0.45) return velocity > 0;
  return offset >= width / 2;
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
