import type { SessionList } from "./protocol";

export function shouldAcceptSessionList(
  activeEngine: "claude" | "codex",
  event: SessionList,
): boolean {
  return event.engine === activeEngine;
}
