import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { DshApiClient } from "../dsh-api";
import type { DshItem } from "../protocol";

export function DshBundleDetail({ sid, api, item }: { sid: string; api: DshApiClient; item: DshItem }) {
  const [text, setText] = useState(""), [loading, setLoading] = useState(true), [error, setError] = useState("");
  const [revision, setRevision] = useState(0), [truncated, setTruncated] = useState(false);
  const source = item.data?.source, version = item.data?.version;
  useEffect(() => {
    const abort = new AbortController(); setLoading(true); setError(""); setText("");
    api.read(sid, "bundles", { query: JSON.stringify({ detail: true, name: item.id, source, version }), signal: abort.signal }).then(result => {
      if (abort.signal.aborted) return;
      if (result.error) throw new Error(result.error);
      setText(result.items[0]?.detail ?? ""); setTruncated(result.items[0]?.data?.truncated === true);
    }).catch(err => { if (!abort.signal.aborted) setError(err.message); })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [api, sid, item.id, source, version, revision]);
  return <div className="dsh-bundle-document">
    {loading && <p role="status">读取插件说明…</p>}
    {error && <p role="alert">{error}<button onClick={() => setRevision(value => value + 1)}>重试</button></p>}
    {!loading && !error && !text && <p className="dsh-management-meta">这个插件没有提供更多说明。</p>}
    <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml
      urlTransform={url => /^https?:\/\//i.test(url) ? url : ""}
      components={{ a: ({ href, children }) => href ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>,
        img: ({ alt }) => <span className="dsh-management-meta">[图片：{alt || "插件说明图片"}]</span> }}>{text}</ReactMarkdown>
    {truncated && <p className="dsh-management-meta">说明较长，当前显示前 65,536 个字符。</p>}
  </div>;
}
