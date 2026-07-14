export type SidebarSwipeAction = "open" | "close" | null;

const SWIPE_THRESHOLD_PX = 50;
const HORIZONTAL_INTENT_RATIO = 1.25;

export function resolveSidebarSwipe(
  startX: number,
  startY: number,
  endX: number,
  endY: number,
  viewportWidth: number,
  locked: boolean,
): SidebarSwipeAction {
  if (locked) return null;
  const dx = endX - startX;
  const dy = endY - startY;
  if (Math.abs(dx) <= SWIPE_THRESHOLD_PX
      || Math.abs(dx) <= Math.abs(dy) * HORIZONTAL_INTENT_RATIO) return null;
  if (dx > 0 && startX < viewportWidth / 3) return "open";
  return dx < 0 ? "close" : null;
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
