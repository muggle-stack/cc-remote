import { useEffect, useRef, useState } from "react";
import type { DshApiClient } from "../dsh-api";
import type { DshItem } from "../protocol";
import { Icon } from "../icons";
import { DshSchedules } from "./DshSchedules";
import { DshBundleVersions } from "./DshBundleVersions";
import { ChoicePicker } from "./ChoicePicker";
import { DshBundleRow } from "./DshBundleRow";
import { bundlePresentation } from "../dsh-bundle-presentation";
import { DshBundleDetail } from "./DshBundleDetail";
import { DshPluginSources } from "./DshPluginSources";

type Kind = "schedules" | "presets" | "bundles";
const status: Record<string, string> = { active: "生效中", inactive: "已结束", enabled: "已启用", disabled: "未启用", failed: "配置异常", default: "默认", available: "可用", "not-installed": "可安装", installed: "已安装", provided: "内置", "update-available": "有新版本", "different-version": "版本不同" };
const captions = {
  presets: "为新会话选择起点。已创建的会话继续使用自己的预设。",
};

/** Management writes only happen on a user's explicit Save / Install action. */
export function DshManagement(props: { sid: string; kind: Kind; api: DshApiClient; initialSearch?: string; onCompose: (prompt: string, preset?: string) => void }) {
  return props.kind === "schedules" ? <DshSchedules {...props} /> : <Management {...props} kind={props.kind} />;
}
function Management({ sid, kind, api, onCompose, initialSearch = "" }: {
  sid: string; kind: Exclude<Kind, "schedules">; api: DshApiClient;
  initialSearch?: string;
  onCompose: (prompt: string, preset?: string) => void;
}) {
  const [items, setItems] = useState<DshItem[]>([]);
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [spec, setSpec] = useState("");
  const [installOpen, setInstallOpen] = useState(false);
  const [market, setMarket] = useState(false);
  const [search, setSearch] = useState(initialSearch);
  const [marketQuery, setMarketQuery] = useState("");
  const [sources, setSources] = useState<DshItem[]>([]), [source, setSource] = useState("");
  const showMarket = kind === "bundles" && market;
  const [offset, setOffset] = useState<number | undefined>();
  const [next, setNext] = useState<number | null>(null);
  const marketSnapshot = useRef<number | null>(null);
  const [remove, setRemove] = useState<DshItem | null>(null);
  useEffect(() => {
    if (!showMarket) return;
    const abort = new AbortController();
    api.read(sid, "bundles", { query: '{"sources":true}', signal: abort.signal }).then(result => {
      if (!abort.signal.aborted) setSources(result.items);
    }).catch(() => {}); // The source search below presents native availability errors.
    return () => abort.abort();
  }, [api, sid, showMarket, revision]);
  const configuredSources = sources.filter(row => row.state !== "empty");
  useEffect(() => {
    const abort = new AbortController();
    setLoading(true); setError(""); setNext(null);
    api.read(sid, kind, { signal: abort.signal, before_seq: offset, ...(showMarket ? { query: JSON.stringify({ market: true, text: marketQuery, source, ...(offset ? { fetchedAt: marketSnapshot.current } : {}) }) } : {}) }).then(result => {
      if (abort.signal.aborted) return;
      const fetchedAt = result.items[0]?.data?.fetchedAt;
      if (showMarket && typeof fetchedAt === "number") {
        if (offset && marketSnapshot.current !== null && marketSnapshot.current !== fetchedAt) {
          setOffset(undefined); setNotice("插件源内容已更新，正在重新读取列表。"); return;
        }
        marketSnapshot.current = fetchedAt;
      }
      setItems(previous => offset ? [...previous, ...result.items.filter(item => !previous.some(old => old.id === item.id))] : result.items);
      setNext(result.has_more ? result.next_seq ?? null : null); setError(result.error ?? "");
    }).catch(error => { if (!abort.signal.aborted) setError(error.message); })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [sid, kind, api, revision, showMarket, marketQuery, source, offset]);
  const mutate = async (body: Record<string, unknown>) => {
    setBusy(true); setNotice("");
    try {
      const result = await api.control(sid, "manage", JSON.stringify(body));
      setNotice(result.text);
      if (result.status === "success") { setRemove(null); setSpec(""); }
      // Never retry a possibly accepted mutation. Read authoritative state.
      setOffset(undefined); setRevision(value => value + 1);
      return result.status === "success";
    } catch (error) { setNotice(`${(error as Error).message}；请刷新核实结果，操作不会自动重发。`); return false; }
    finally { setBusy(false); }
  };
  const makeDefault = async (item: DshItem) => {
    setBusy(true); setNotice("");
    try {
      const result = await api.read(sid, "settings");
      const row = result.items.find(row => row.id === "agent-preset-registry" && row.controllable);
      if (!row) throw new Error(result.error || "当前运行时未开放默认预设设置");
      await mutate({ action: "preset_default", id: item.id, revision: row.data?.revision });
    } catch (error) { setNotice((error as Error).message); }
    finally { setBusy(false); }
  };
  const visible = showMarket ? items : items.filter(item => kind !== "bundles" || `${item.id} ${bundlePresentation(item).title} ${bundlePresentation(item).detail} ${item.detail}`.toLocaleLowerCase().includes(search.toLocaleLowerCase()));
  const catalogTabs = <div className="dsh-catalog-tabs">
    <button aria-pressed={!market} disabled={busy} onClick={() => { if (market) { setMarket(false); setItems([]); setOffset(undefined); } }}>本机插件</button>
    <button aria-pressed={market} disabled={busy} onClick={() => { if (!market) { setMarket(true); setItems([]); setOffset(undefined); } }}>插件源</button>
    {!market && <button className="iconbtn" aria-label="刷新插件" disabled={loading || busy} onClick={() => { setOffset(undefined); setRevision(value => value + 1); }}><Icon name="refresh" size={17} /></button>}
  </div>;
  return <div className={`dsh-management${kind === "bundles" ? " dsh-bundles" : ""}`}>
    {kind !== "bundles" && <div className="dsh-management-toolbar">
      <button disabled={loading || busy} onClick={() => { setOffset(undefined); setRevision(value => value + 1); }}>刷新</button>
      {kind === "presets" && <button className="dsh-primary" onClick={() => onCompose("我想创建一个 DSH 代理预设，请先和我确认用途及需要的能力。", "cordis")}>创建预设</button>}
    </div>}
    <div className="dsh-management-content">
      {kind !== "bundles" && <p className="dsh-management-caption">{captions[kind]}</p>}
      {notice && <p role="status" className="dsh-management-notice">{notice}</p>}
      {error && <p role="alert" className="dsh-feature-error">{error}</p>}
      {kind === "bundles" && <>
        {catalogTabs}
        {showMarket && <><p className="dsh-management-caption">来自这台设备在 DSH 中配置的插件源。</p>
          <DshPluginSources sources={sources} busy={busy} notice={notice} onSave={async body => { const ok = await mutate(body); if (ok) setSource(""); return ok; }} />
          {!loading && !configuredSources.length && <p className="dsh-management-meta">{error ? "插件市场暂不可用。" : "尚未添加插件源，可先添加索引地址。"}<button onClick={() => { setMarket(false); setSearch("@deepseek-ai/dsh-experimental-plugin-market-bundle"); setItems([]); setOffset(undefined); }}>查看市场插件</button></p>}</>}
        {showMarket && configuredSources.length > 1 && <ChoicePicker label="选择插件源" value={source || configuredSources[0].id}
          options={configuredSources.map(row => ({ value: row.id, label: row.title }))}
          onChange={value => { if (value !== source) { setSource(value); setItems([]); setOffset(undefined); } }}>{configuredSources.find(row => row.id === source)?.title ?? configuredSources[0].title}</ChoicePicker>}
        <form className="dsh-catalog-search" onSubmit={event => { event.preventDefault(); if (showMarket) { setMarketQuery(search); setOffset(undefined); setRevision(value => value + 1); } }}>
          <Icon name="search" size={18} /><input aria-label="搜索插件" placeholder="搜索名称或说明…" value={search} maxLength={200} onChange={event => setSearch(event.target.value)} />
          {showMarket && <button disabled={busy || loading}>搜索</button>}
          {!showMarket && <button type="button" className="iconbtn" aria-label="添加插件包" aria-expanded={installOpen} onClick={() => setInstallOpen(value => !value)}><Icon name="plus" size={20} /></button>}
        </form>
      </>}
      {kind === "bundles" && !market && installOpen && <form className="dsh-install" onSubmit={event => { event.preventDefault(); void mutate({ action: "bundle_install", spec }); }}>
        <label htmlFor="dsh-bundle-spec">从包名或路径安装</label><div><input id="dsh-bundle-spec" placeholder="包名或设备上的路径" value={spec} onChange={event => setSpec(event.target.value)} maxLength={2048} />
          <button className="dsh-primary" disabled={busy || !spec.trim()}>安装</button></div>
      </form>}
      {visible.map(item => <article className={kind === "bundles" ? "dsh-bundle-row" : "dsh-management-card"} key={item.id}>
        {kind === "bundles" ? <DshBundleRow item={item} busy={busy} detail={<DshBundleDetail sid={sid} api={api} item={item} />} onToggle={() => void mutate({ action: item.state === "enabled" ? "bundle_disable" : "bundle_enable", name: item.id })} onRemove={() => setRemove(item)} onInstall={() => void mutate({ action: item.data?.source ? "market_install" : "bundle_install", ...(item.data!.installTarget as Record<string, unknown>) })}>
          {item.data?.updatable === true && <DshBundleVersions sid={sid} item={item} api={api} busy={busy} onUpdate={version => mutate({ action: "bundle_update", name: item.id, version })} />}
        </DshBundleRow> : <>
        <div className="dsh-management-title"><strong>{item.title}</strong>{status[item.state] && <small>{status[item.state]}</small>}</div>
        {item.detail && <p>{item.detail}</p>}
        <div className="dsh-management-actions">
            {kind === "presets" && <><button disabled={busy || item.state === "failed"} onClick={() => onCompose("", item.id)}>用此预设新建会话</button><button disabled={busy || item.state !== "available"} onClick={() => void makeDefault(item)}>设为默认</button></>}
          </div></>}
        {remove?.id === item.id && <div className="dsh-inline-confirm"><p>卸载这个插件包？正在使用的插件由 DSH 决定是否允许卸载。</p><button disabled={busy} onClick={() => setRemove(null)}>取消</button><button disabled={busy} onClick={() => void mutate({ action: "bundle_remove", name: item.id })}>确认卸载</button></div>}
      </article>)}
      {kind === "bundles" && !market && !installOpen && <button className="dsh-install-link" onClick={() => setInstallOpen(true)}><Icon name="plus" size={17} />从包名或路径安装</button>}
      {next !== null && <button disabled={loading || busy} onClick={() => setOffset(next)}>加载更多插件</button>}
      {loading && <p role="status" className="dsh-feature-empty">读取中…</p>}
      {!loading && !error && !visible.length && <p className="dsh-feature-empty">{kind === "bundles" ? "没有匹配的插件" : "暂无记录"}</p>}
    </div>
  </div>;
}
