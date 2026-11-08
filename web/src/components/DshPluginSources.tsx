import { useState } from "react";
import type { DshItem } from "../protocol";
import { DshConfirm } from "./DshConfirm";

export function DshPluginSources({ sources, busy, notice, onSave }: {
  sources: DshItem[]; busy: boolean; notice: string; onSave: (body: Record<string, unknown>) => Promise<boolean>;
}) {
  const [open, setOpen] = useState(false), [url, setUrl] = useState("");
  const revision = sources[0]?.data?.revision;
  const rows = sources.filter(row => row.state !== "empty");
  return <>
    <button className="dsh-feature-link" disabled={busy || !revision} onClick={() => setOpen(true)}>管理插件源{rows.length ? ` · ${rows.length}` : ""}</button>
    {open && <DshConfirm title="管理插件源" busy={busy} onClose={() => { if (!busy) setOpen(false); }}>
      <p className="dsh-management-meta">使用这台设备的 DSH 插件索引。移除来源会保留已安装插件。</p>
      {notice && <p role="status">{notice}</p>}
      {rows.map(row => <div className="dsh-source-row" key={row.id}><span>{row.title}<small>{String(row.data?.url || "地址由原生 DSH 管理")}</small></span>
        <button disabled={busy} onClick={() => void onSave({ action: "market_source_remove", source: row.id, revision })}>移除</button></div>)}
      <form onSubmit={async event => { event.preventDefault(); if (await onSave({ action: "market_source_add", url: url.trim(), revision })) setUrl(""); }}>
        <label>插件索引地址<input aria-label="插件索引地址" type="url" required placeholder="https://example.com/plugins.json" value={url} onChange={event => setUrl(event.target.value)} maxLength={2048} /></label>
        <footer><button type="button" disabled={busy} onClick={() => setOpen(false)}>关闭</button><button disabled={busy || !url.trim()}>添加来源</button></footer>
      </form>
    </DshConfirm>}
  </>;
}
