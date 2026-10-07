import { useSyncExternalStore, type ComponentPropsWithoutRef, type ReactNode } from "react";
import type { SidebarController } from "../sidebar-state";

export function SidebarState({ controller, children }: {
  controller: SidebarController; children: (open: boolean) => ReactNode;
}) {
  const open = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
  return children(open);
}

export function SidebarShell({ controller, className = "", ...props }: {
  controller: SidebarController;
} & ComponentPropsWithoutRef<"div">) {
  const open = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
  return <div {...props} className={`shell${open ? " sidebar-open" : ""}${className ? ` ${className}` : ""}`} />;
}
