import { useEffect, useState } from "react";
import type { DshRead } from "../dsh-api";
import type { DshReadResult } from "../protocol";
import "./DshFeatures.css";

export default function DshSearch({ query, sid, read, onSelect }: {
  query: string; sid: string; read: DshRead; onSelect: (sid: string, query: string) => void;
}) {
  const [result, setResult] = useState<DshReadResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const abort = new AbortController();
    setResult(null); setError(null);
    const timer = setTimeout(() => {
      read(sid, "search", { query: query.trim(), signal: abort.signal }).then(value => {
        if (!abort.signal.aborted) { setResult(value); setError(value.error ?? null); }
      }).catch(err => { if (!abort.signal.aborted) setError(err.message); });
    }, 300);
    return () => { clearTimeout(timer); abort.abort(); };
  }, [query, sid, read, retry]);
  return <section className="dsh-search" aria-label="全文搜索结果">
    <div className="s-group">对话内容</div>
    {result?.items.map(item => <button key={item.id} onClick={() => item.sid && onSelect(item.sid, query.trim())}>
      <strong>{item.title}</strong><p>{item.detail}</p>
    </button>)}
    {error ? <p role="status">{error}<button onClick={() => setRetry(x => x + 1)}>重试</button></p>
      : !result ? <p role="status">搜索内容中…</p>
      : !result.items.length ? <p>内容中没有匹配</p>
      : result.has_more ? <p>显示前 20 条，请添加关键词缩小范围</p> : null}
  </section>;
}
