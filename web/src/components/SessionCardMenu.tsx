import { useLayoutEffect, useRef, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";

/** Keep session actions outside the sidebar's scrolling/clipping ancestors. */
export function SessionCardMenu({ anchor, onClose, children }: {
  anchor: RefObject<HTMLButtonElement | null>;
  onClose: () => void;
  children: ReactNode;
}) {
  const menuRef = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const trigger = anchor.current;
    const menu = menuRef.current;
    const sidebar = trigger?.closest<HTMLElement>(".sessions");
    const scroll = trigger?.closest<HTMLElement>(".s-scroll");
    const footer = sidebar?.querySelector<HTMLElement>(".s-foot");
    if (!trigger || !menu || !sidebar || !scroll || !footer) return;
    const viewport = window.visualViewport;
    const position = () => {
      const button = trigger.getBoundingClientRect();
      const list = scroll.getBoundingClientRect();
      if (!trigger.isConnected || button.bottom <= list.top || button.top >= list.bottom) {
        onClose();
        return;
      }
      const panel = sidebar.getBoundingClientRect();
      const viewLeft = viewport?.offsetLeft ?? 0;
      const viewTop = viewport?.offsetTop ?? 0;
      const left = Math.max(panel.left, viewLeft) + 8;
      const right = Math.min(panel.right, viewLeft + (viewport?.width ?? window.innerWidth)) - 8;
      const top = Math.max(panel.top, viewTop) + 8;
      const bottom = Math.min(footer.getBoundingClientRect().top,
        viewTop + (viewport?.height ?? window.innerHeight)) - 8;
      const width = Math.max(0, right - left);
      menu.style.minWidth = `${Math.min(152, width)}px`;
      menu.style.maxWidth = `${width}px`;
      const height = menu.scrollHeight + menu.offsetHeight - menu.clientHeight;
      const above = Math.max(0, Math.min(button.top - 6, bottom) - top);
      const below = Math.max(0, bottom - Math.max(button.bottom + 6, top));
      const upward = height > below && above > below;
      const available = upward ? above : below;
      menu.style.maxHeight = `${available}px`;
      const box = menu.getBoundingClientRect();
      menu.style.left = `${Math.max(left, Math.min(button.right - box.width, right - box.width))}px`;
      menu.style.top = `${Math.max(top, Math.min(
        upward ? button.top - 6 - box.height : button.bottom + 6, bottom - box.height,
      ))}px`;
      menu.dataset.placement = upward ? "above" : "below";
      menu.style.visibility = "visible";
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      onClose();
      trigger.focus({ preventScroll: true });
    };
    position();
    if (document.activeElement === trigger && trigger.matches(":focus-visible")) {
      menu.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus({ preventScroll: true });
    }
    const observer = new ResizeObserver(position);
    for (const element of [menu, trigger, sidebar, scroll, footer]) observer.observe(element);
    window.addEventListener("resize", position);
    window.addEventListener("scroll", position, true);
    viewport?.addEventListener("resize", position);
    viewport?.addEventListener("scroll", position);
    sidebar.addEventListener("transitionend", position);
    document.addEventListener("keydown", escape, true);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", position);
      window.removeEventListener("scroll", position, true);
      viewport?.removeEventListener("resize", position);
      viewport?.removeEventListener("scroll", position);
      sidebar.removeEventListener("transitionend", position);
      document.removeEventListener("keydown", escape, true);
    };
  }, [anchor, onClose]);

  return typeof document === "undefined" ? null : createPortal(
    <div ref={menuRef} className="card-menu" role="group" aria-label="会话操作"
      onClick={event => event.stopPropagation()}
      onTouchStart={event => event.stopPropagation()}
      onKeyDown={event => {
        if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        event.stopPropagation();
        const items = [...event.currentTarget.querySelectorAll<HTMLButtonElement>("button:not(:disabled)")];
        const index = items.indexOf(document.activeElement as HTMLButtonElement);
        const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1
          : (index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
        items[next]?.focus();
      }}>
      {children}
    </div>, document.body,
  );
}
