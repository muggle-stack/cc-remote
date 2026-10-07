import { useState, type SetStateAction } from "react";

export interface SidebarController {
  getSnapshot: () => boolean;
  subscribe: (notify: () => void) => () => void;
  setOpen: (next: SetStateAction<boolean>) => void;
}

/** The shell and its controls subscribe; changing the drawer does not rerender
 * App's conversation, composer, panels, or streaming Markdown. Logical state
 * still changes immediately, including rapid reversals and external closes. */
export function useSidebarController(): SidebarController {
  const [controller] = useState((): SidebarController => {
    let open = false;
    const listeners = new Set<() => void>();
    return {
      getSnapshot: () => open,
      subscribe: notify => { listeners.add(notify); return () => { listeners.delete(notify); }; },
      setOpen: value => {
        const next = typeof value === "function" ? value(open) : value;
        if (next === open) return;
        open = next;
        for (const notify of [...listeners]) notify();
      },
    };
  });
  return controller;
}
