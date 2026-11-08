import { useState } from "react";
import type { DshApiClient } from "../dsh-api";
import type { DshItem } from "../protocol";
import { Icon } from "../icons";
import "./DshRecommendations.css";

export function DshRecommendation({ item, sid, api, onOpen, onRefresh }: {
  item: DshItem; sid: string; api: DshApiClient; onOpen: (name: string) => void; onRefresh: () => void;
}) {
  const [busy, setBusy] = useState(false), [notice, setNotice] = useState("");
  const [hidden, setHidden] = useState(false);
  const act = async (verb: "enable" | "dismiss") => {
    if (busy) return;
    setBusy(true); setNotice("");
    try {
      const result = await api.control(sid, "manage", JSON.stringify({ action: "recommendation", call: item.id, verb }));
      setNotice(result.text);
      if (result.status === "success") { if (verb === "dismiss") setHidden(true); onRefresh(); }
    } catch (err) { setNotice((err as Error).message); }
    finally { setBusy(false); }
  };
  if (hidden) return null;
  const settled = item.state === "enabled" || item.state === "already-enabled";
  return <aside className="dsh-recommendation" aria-label="插件推荐">
    <div className="dsh-recommendation-heading"><small>可选建议{settled ? " · 已启用" : ""}</small><button className="iconbtn" aria-label="收起这条推荐" disabled={busy} onClick={() => setHidden(true)}><Icon name="close" size={15} /></button></div>
    <div className="dsh-recommendation-body"><Icon name="archive" size={22} /><div><strong>{item.title}</strong><p>{item.detail}</p></div></div>
    <div className="dsh-recommendation-actions"><button disabled={busy} onClick={() => onOpen(String(item.data?.name ?? ""))}>查看插件</button>
      {item.controllable && !settled && <><button disabled={busy || item.data?.enabled === true} onClick={() => void act("enable")}>{busy ? "处理中…" : item.data?.enabled ? "已启用" : "启用"}</button><button disabled={busy} onClick={() => void act("dismiss")}>本会话不再提示</button></>}
    </div>
    {!item.controllable && !settled && <small>原生会话当前不可操作，可先查看插件。</small>}
    {item.data?.application === "restart-required" && <small>已保存，重启 DSH 后生效。</small>}
    {notice && <p role="status">{notice}</p>}
  </aside>;
}
