import { useEffect, useLayoutEffect, useRef, type Dispatch } from "react";
import type { Action, SessionRuntime } from "./reducer";
import { createHistoryBrowse, type HistoryBrowseProjection } from "./history-browse";
import { uuid } from "./util";

/** Navigation bookmarks contain geometry, never detached DOM nodes. */
export interface ChatReadingState {
  turnId: string;
  blockIndex: number;
  offset: number;
  turnOffset: number;
  scrollTop: number;
  sizes: Record<string, number>;
  disclosures: Record<string, boolean>;
  browsing: boolean;
}

interface SavedView {
  position: ChatReadingState;
  revision: string | null;
  generation: string | null;
  /** Explicit history, or a fallback if a live bookmark leaves the runtime. */
  browse: HistoryBrowseProjection | null;
}

/** App-owned, tab-lifetime memory. Thirty-two lightweight bookmarks and at
 * most four bounded paginated history windows;
 * machine + engine + space + session are all part of the caller's key. */
export class ChatReadingMemory {
  private views = new Map<string, SavedView>();

  clear(): void { this.views.clear(); }

  save(key: string, view: SavedView | null): void {
    this.views.delete(key);
    if (view) this.views.set(key, view);
    while (this.views.size > 32) this.views.delete(this.views.keys().next().value!);
    const paged = [...this.views].filter(([, saved]) => saved.browse);
    for (const [oldest] of paged.slice(0, -4)) this.views.delete(oldest);
  }

  read(key: string, runtime: SessionRuntime): SavedView | undefined {
    const view = this.views.get(key);
    if (!view || runtime.historyInvalidated
        || view.revision !== runtime.historyRevision
        || view.generation !== runtime.historyGeneration) return;
    if (view.position.browsing) return view;
    const matches = runtime.turns.filter(turn =>
      [turn.id, turn.clientMsgId, turn.historyTurnId].includes(view.position.turnId));
    if (matches.length !== 1) {
      return view.browse ? {
        ...view, position: { ...view.position, browsing: true },
      } : undefined;
    }
    // Pausing output is still a live reading position. Restore against the
    // current row so tools, completion and working marks keep updating.
    const position = { ...view.position, turnId: matches[0].id };
    const size = position.sizes[view.position.turnId];
    if (position.turnId !== view.position.turnId && size != null) {
      position.sizes = { ...position.sizes, [position.turnId]: size };
    }
    return { ...view, position, browse: null };
  }
}

export function useChatReadingMemory(
  scope: string, sid: string | null, runtime: SessionRuntime,
  browse: HistoryBrowseProjection | null, dispatch: Dispatch<Action>,
  enabled = true, authenticated = true,
  pageScopeKey = scope,
) {
  const memory = useRef(new ChatReadingMemory());
  useEffect(() => { if (!authenticated) memory.current.clear(); }, [authenticated]);
  const key = `${scope}\u0000${sid ?? ""}`;
  const available = enabled && authenticated;
  // Read once per navigation. In-place history/detail changes still belong to
  // ChatView's existing scroll transactions, not a second restore operation.
  const navigation = useRef<{
    key: string; available: boolean; saved: SavedView | undefined; restored: boolean;
  } | null>(null);
  if (navigation.current?.key !== key || navigation.current.available !== available) {
    navigation.current = { key, available, restored: false,
      saved: available ? memory.current.read(key, runtime) : undefined };
  }
  const entry = navigation.current;
  const saved = entry.saved;
  const valid = saved && !runtime.historyInvalidated
    && saved.revision === runtime.historyRevision
    && saved.generation === runtime.historyGeneration;
  useLayoutEffect(() => {
    if (entry.restored) return;
    entry.restored = true;
    if (!valid || !saved.browse) return;
    dispatch({ type: "restore_history_browse", projection: {
      ...saved.browse, viewId: uuid(), windowEpoch: saved.browse.windowEpoch + 1,
      latestDirty: true,
    } });
  }, [dispatch, entry, saved, valid]);
  return {
    initialReadingState: valid ? saved.position : undefined,
    onReadingStateSave: (position: ChatReadingState | null) => {
      if (!available || !sid || runtime.historyInvalidated) return;
      // Keep a bounded fallback if the newest page drops the bookmarked row
      // while away. Its existence does not change a live bookmark into browse.
      const window = position && runtime.historyRevision
        ? position.browsing && browse?.sid === sid ? browse : createHistoryBrowse({
          scopeKey: pageScopeKey, sid, revision: runtime.historyRevision,
          generation: runtime.historyGeneration, viewId: uuid(),
          baseTurns: runtime.turns, basePageKey: `reading:${uuid()}`,
          hasOlder: !!runtime.hasMore, olderCursor: runtime.oldestId ?? null,
        }).projection : null;
      memory.current.save(key, position ? {
        position,
        revision: runtime.historyRevision, generation: runtime.historyGeneration,
        browse: window,
      } : null);
    },
  };
}
