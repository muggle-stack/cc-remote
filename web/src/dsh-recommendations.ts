import { useEffect, useState } from "react";
import type { DshApiClient } from "./dsh-api";
import type { DshItem } from "./protocol";
import type { Turn } from "./domain/conversation";

export function useDshRecommendations(api: DshApiClient | undefined, sid: string | null, turns: Turn[]) {
  const [items, setItems] = useState<DshItem[]>([]), [revision, setRevision] = useState(0);
  const [loadedSid, setLoadedSid] = useState<string | null>(null);
  const tail = turns.at(-1);
  const signature = `${tail?.id}:${tail?.done}:${tail?.forkPointId}:${turns.length}`;
  const running = !!tail && !tail.done;
  useEffect(() => {
    if (!api || !sid) return;
    const abort = new AbortController(); let timer: ReturnType<typeof setTimeout> | undefined;
    const read = async () => {
      try {
        const result = await api.read(sid, "plugins", { query: "recommendations", signal: abort.signal });
        if (!abort.signal.aborted && !result.error) { setItems(result.items); setLoadedSid(sid); }
      } catch { /* Optional native cards are absent when their plugin is disabled. */ }
      if (!abort.signal.aborted && running) timer = setTimeout(() => void read(), 5000);
    };
    void read();
    return () => { abort.abort(); clearTimeout(timer); };
  }, [api, sid, signature, running, revision]);
  return { items: api && loadedSid === sid ? items : [], refresh: () => setRevision(value => value + 1) };
}

export function recommendationTurn(item: DshItem, turn: Turn) {
  return typeof item.data?.owner === "string" && [turn.id, turn.historyTurnId, turn.forkPointId, turn.liveTaskId].includes(item.data.owner);
}
