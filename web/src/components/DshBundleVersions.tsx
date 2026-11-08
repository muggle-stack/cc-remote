import { useEffect, useState } from "react";
import type { DshApiClient } from "../dsh-api";
import type { DshItem } from "../protocol";
import { ChoicePicker } from "./ChoicePicker";

export function DshBundleVersions({ sid, item, api, busy, onUpdate }: {
  sid: string; item: DshItem; api: DshApiClient; busy: boolean; onUpdate: (version: string) => Promise<boolean>;
}) {
  const [open, setOpen] = useState(false), [loading, setLoading] = useState(false);
  const [versions, setVersions] = useState<DshItem[]>([]), [version, setVersion] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    if (!open) return;
    const abort = new AbortController(); setLoading(true); setError("");
    api.read(sid, "bundles", { query: JSON.stringify({ versions: true, name: item.id }), signal: abort.signal }).then(result => {
      if (abort.signal.aborted) return;
      setVersions(result.items); setVersion((result.items.find(row => row.state === "compatible" && !row.data?.deprecated && !row.data?.prerelease)
        ?? result.items.find(row => row.state === "compatible" && !row.data?.deprecated))?.id ?? "");
      setError(result.error ?? "");
    }).catch(error => { if (!abort.signal.aborted) setError(error.message); })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [api, sid, item.id, open]);
  return <div className="dsh-bundle-versions">
    {!open ? <button disabled={busy} onClick={() => setOpen(true)}>检查更新</button> : <>
      {loading && <p role="status">正在读取版本…</p>}
      {error && <p role="alert">{error}</p>}
      {!loading && !error && <>
        {!versions.length ? <p>当前已是插件源中的最新版本。</p> : <>
          <ChoicePicker label="选择插件版本" disabled={busy || loading} value={version} options={versions.filter(row => row.state === "compatible").map(row => ({
            value: row.id, label: row.title, description: [row.data?.prerelease ? "预发布" : "稳定版", row.data?.deprecated ? "已弃用" : "", ...(row.data?.tags as string[] ?? [])].filter(Boolean).join(" · "),
          }))} onChange={setVersion}>{version || "没有兼容版本"}</ChoicePicker>
          {versions.some(row => row.state === "incompatible") && <p className="dsh-management-meta">不兼容的版本不可选择。</p>}
          <p className="dsh-management-meta">{String(item.data?.version ?? item.data?.installedVersion ?? "当前版本")} → {version || "待选择"}</p>
        </>}
      </>}
      <div className="dsh-management-actions">
        <button disabled={busy} onClick={() => setOpen(false)}>取消</button>
        <button className="dsh-primary" disabled={busy || loading || !!error || !version} onClick={async () => { if (await onUpdate(version)) setOpen(false); }}>{busy ? "正在更新…" : "更新到所选版本"}</button>
      </div>
    </>}
  </div>;
}
