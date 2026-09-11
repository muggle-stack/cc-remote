import { useEffect, useState } from "react";
import type { DshRead } from "../dsh-api";
import type { DshItem } from "../protocol";

// Match the official file-reference grammar, including open quotes, but do not
// reinterpret a completed structured session mention as another completion.
export function dshReferenceToken(input: string, caret: number) {
  const text = input.slice(0, caret);
  const match = /(?:^|\s)(@"([^"]*))$/u.exec(text) ?? /(?:^|\s)(@([^\s]*))$/u.exec(text);
  if (!match || match[2].startsWith("[") || match[2].includes('"')) return null;
  return { start: caret - match[1].length, end: caret, query: match[2] };
}

export function useDshReferences(read: DshRead | undefined, sid: string | undefined, input: string, caret: number, enabled: boolean) {
  const token = enabled ? dshReferenceToken(input, caret) : null;
  const query = token?.query;
  const key = `${sid ?? ""}:${query ?? ""}`;
  const [result, setResult] = useState<{ key: string; items: DshItem[]; error?: string | null } | null>(null);
  const [selected, setSelected] = useState(0);
  const [retry, setRetry] = useState(0);
  const [dismissed, setDismissed] = useState<string | null>(null);
  useEffect(() => {
    if (!read || !sid || query === undefined) return;
    const abort = new AbortController();
    setSelected(0); setResult(null);
    const timer = setTimeout(() => {
      read(sid, "references", { query, signal: abort.signal }).then(value => {
        if (!abort.signal.aborted) setResult({ key, items: value.items.filter(item => item.mention), error: value.error });
      }).catch(error => { if (!abort.signal.aborted) setResult({ key, items: [], error: error.message }); });
    }, 200);
    return () => { clearTimeout(timer); abort.abort(); };
  }, [read, sid, query, key, retry]);
  const open = !!token && !!read && !!sid && dismissed !== `${key}:${input}:${caret}`;
  return { token, open, selected, setSelected, items: result?.key === key ? result.items : [],
    error: result?.key === key ? result.error : null, loading: result?.key !== key,
    retry: () => setRetry(value => value + 1), dismiss: () => setDismissed(`${key}:${input}:${caret}`) };
}
