import {
  useCallback,
  useEffect,
  useRef,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";

import { clampPanelWidth } from "../responsive-layout";
import { DESKTOP_PANEL_QUERY, PANEL_WIDTH_KEY } from "../use-panel-width";

export function PanelResizer({ ariaLabel }: { ariaLabel: string }) {
  const handleRef = useRef<HTMLButtonElement>(null);
  const resizeRef = useRef<{
    pointerId: number;
    startX: number;
    startWidth: number;
  } | null>(null);
  const panelElement = useCallback(
    () => handleRef.current?.parentElement ?? null,
    [],
  );

  const applyPanelWidth = useCallback((requestedWidth: number,
    persist = false) => {
    const width = clampPanelWidth(requestedWidth, window.innerWidth);
    document.documentElement.style.setProperty("--panel-w", `${width}px`);
    if (persist) localStorage.setItem(PANEL_WIDTH_KEY, String(width));
    return width;
  }, []);

  useEffect(() => () => {
    resizeRef.current = null;
    document.documentElement.classList.remove("panel-resizing");
  }, []);

  const startResize = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const panel = panelElement();
    if (!window.matchMedia(DESKTOP_PANEL_QUERY).matches || !panel) return;
    resizeRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startWidth: panel.getBoundingClientRect().width,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
    document.documentElement.classList.add("panel-resizing");
    event.preventDefault();
  };
  const moveResize = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const resize = resizeRef.current;
    if (!resize || resize.pointerId !== event.pointerId) return;
    applyPanelWidth(resize.startWidth + resize.startX - event.clientX);
  };
  const finishResize = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const resize = resizeRef.current;
    if (!resize || resize.pointerId !== event.pointerId) return;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    resizeRef.current = null;
    document.documentElement.classList.remove("panel-resizing");
    const width = panelElement()?.getBoundingClientRect().width;
    if (width) applyPanelWidth(width, true);
  };
  const resizeWithKeyboard = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    const width = panelElement()?.getBoundingClientRect().width;
    if (!width) return;
    applyPanelWidth(width + (event.key === "ArrowLeft" ? 24 : -24), true);
    event.preventDefault();
  };

  return <button ref={handleRef} type="button" className="panel-resizer"
    aria-label={ariaLabel} title="左右拖动调整面板宽度"
    onPointerDown={startResize} onPointerMove={moveResize}
    onPointerUp={finishResize} onPointerCancel={finishResize}
    onKeyDown={resizeWithKeyboard} />;
}
