import { PanelFrame, PanelExpandButton } from "./RightPanelPresentation";
import { useEffect, useId, useRef, useState } from "react";
import { Icon } from "../icons";
import { isLocalViewerUrl, matchingViewer, type ViewerScope, type ViewerSelection, type ViewerSite } from "../remote-viewer";
import { viewerRequest, ViewerRequestError } from "../viewer-request";
import { PanelResizer } from "./PanelResizer";
import { ViewerBridge, type BridgeGrant } from "../viewer-bridge";
import { useViewerPages } from "../viewer-pages-context";
import "./remote-viewer.css";

type Grant = { id: string; entry: string; expires_at: number } & (
  { mode: "isolated"; origin: string } | ({ mode: "bridge" } & BridgeGrant));

function BridgeFrame({ grant, title, onReady, onError }: {
  grant: BridgeGrant; title: string; onReady: () => void; onError: (message: string, clear: boolean) => void;
}) {
  const frame = useRef<HTMLIFrameElement>(null);
  const callbacks = useRef({ onReady, onError });
  callbacks.current = { onReady, onError };
  useEffect(() => {
    if (!frame.current) return;
    const bridge = new ViewerBridge(frame.current, grant, () => callbacks.current.onReady(),
      (message, clear) => callbacks.current.onError(message, clear));
    return () => bridge.dispose();
  }, [grant]);
  return <iframe ref={frame} title={title} referrerPolicy="no-referrer" sandbox="allow-scripts" />;
}
interface Props {
  active?: boolean;
  scope: ViewerScope;
  selection: ViewerSelection | null;
  requestedUrl?: string;
  devices: { machine_id: string; label: string }[];
  onSelect: (selection: ViewerSelection | null) => void;
  onClose: () => void;
}

export function RemoteViewerPanel({ active = true, scope, selection, requestedUrl, devices, onSelect, onClose }: Props) {
  const pageContext = useViewerPages();
  const [browseAll, setBrowseAll] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const pickerId = useId();
  const picker = useRef<HTMLDivElement>(null);
  const pickerTrigger = useRef<HTMLButtonElement>(null);
  const activeRef = useRef(active);
  activeRef.current = active;
  useEffect(() => { if (!active) setPickerOpen(false); }, [active]);
  const [sites, setSites] = useState<ViewerSite[]>([]);
  const [enabled, setEnabled] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [grant, setGrant] = useState<Grant | null>(null);
  const activeGrant = useRef<Grant | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [retry, setRetry] = useState(0);
  const frame = useRef<HTMLIFrameElement>(null);
  const invalidate = useRef<(() => void) | null>(null);
  const close = useRef(onClose);
  close.current = onClose;
  const select = useRef(onSelect);
  select.current = onSelect;
  const selectionRef = useRef(selection);
  selectionRef.current = selection;
  const handledUrl = useRef<string | null>(null);
  const site = sites.find((site) => site.machine_id === selection?.machine_id && site.id === selection?.site_id);
  const related = pageContext?.pages ?? [];
  const selected = related.find((page) => page.machine_id === selection?.machine_id
    && page.site_id === selection?.site_id && page.entry === (selection?.entry ?? site?.entry));
  const selectedEntry = selected?.entry;
  const selectedId = selected?.id;
  const selectionMachineId = selection?.machine_id;
  const selectionSiteId = selection?.site_id;
  const initialContent = useRef<string | undefined>(undefined);
  const [openedContent, setOpenedContent] = useState<string | undefined>(undefined);
  initialContent.current = selected?.content_revision;
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  // Restored metadata can paint before its lazy controller is ready. Keep
  // association clicks queued until live callbacks are available.
  const associatePage = pageContext?.commandsReady === false ? undefined : pageContext?.associate;
  const associate = useRef(associatePage);
  associate.current = associatePage;
  const choose = async (choice: ViewerSite) => {
    try {
      if (!associate.current) throw new Error("正在读取本会话页面，请稍后重试。");
      const page = await associate.current({ machine_id: choice.machine_id, site_id: choice.id, entry: choice.entry });
      if (page && alive.current) {
        setBrowseAll(false);
        setPickerOpen(false);
        select.current({ machine_id: page.machine_id, site_id: page.site_id, entry: page.entry });
      }
    } catch (reason) { if (alive.current) setError(reason instanceof Error ? reason.message : "无法关联页面。"); }
  };
  const chooseRef = useRef(choose);
  chooseRef.current = choose;
  const originalUrl = requestedUrl && isLocalViewerUrl(requestedUrl) ? requestedUrl
    : selected?.references.find(isLocalViewerUrl) ?? null;
  const label = (id: string) => devices.find((device) => device.machine_id === id)?.label ?? id;
  const heading = selected && !/^index(?:\.html?)?$/i.test(selected.label)
    ? selected.label : "页面预览";

  useEffect(() => {
    if (!pickerOpen || !active) return;
    const button = pickerTrigger.current;
    const panel = picker.current;
    panel?.focus({ preventScroll: true });
    const onOutsideClick = (event: MouseEvent) => {
      if (event.target instanceof Node && !panel?.contains(event.target) && !button?.contains(event.target))
        setPickerOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing) return;
      if (event.key === "Escape") {
        event.preventDefault(); event.stopPropagation();
        setPickerOpen(false);
      } else if (event.key === "Tab") {
        const controls = Array.from(panel?.querySelectorAll<HTMLElement>("button:not(:disabled), a[href]") ?? []);
        const first = controls[0], last = controls.at(-1);
        if (event.shiftKey && (document.activeElement === first || document.activeElement === panel)
            || !event.shiftKey && (document.activeElement === last || document.activeElement === panel)) {
          event.preventDefault();
          (event.shiftKey ? last : first)?.focus();
        }
        event.stopPropagation();
      }
    };
    document.addEventListener("click", onOutsideClick, true);
    window.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("click", onOutsideClick, true);
      window.removeEventListener("keydown", onKey, true);
      if (activeRef.current && (panel?.contains(document.activeElement) || document.activeElement === document.body))
        button?.focus({ preventScroll: true });
    };
  }, [pickerOpen, active]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    let reading = false;
    const readCatalog = (initial = false) => {
      if (reading) return;
      reading = true;
      void viewerRequest<{ enabled: boolean; sites: ViewerSite[] }>("/api/viewers", { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return;
        setEnabled(data.enabled);
        setSites(data.sites);
        if (!selectionRef.current) setError(null);
      }).catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "无法读取预览列表。");
      }).finally(() => {
        reading = false;
        if (!controller.signal.aborted && initial) setLoading(false);
      });
    };
    readCatalog(true);
    const timer = window.setInterval(() => { if (!selectionRef.current) readCatalog(); }, 5000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [retry, requestedUrl]);

  useEffect(() => {
    if (loading || !enabled || selection || !requestedUrl || handledUrl.current === requestedUrl) return;
    const match = matchingViewer(sites, requestedUrl);
    if (!match) { setBrowseAll(true); return; }
    // The catalog can arrive before the deferred session controller. Keep the
    // user's click pending until association is ready, not permanently failed.
    if (!associatePage) return;
    handledUrl.current = requestedUrl;
    void chooseRef.current(match);
  }, [loading, enabled, selection, requestedUrl, sites, associatePage]);

  // Migrate an explicitly selected pre-association panel once. Merely opening
  // the page picker is not permission to associate the complete catalog.
  const migrating = useRef(false);
  useEffect(() => {
    if (!selection || selection.entry || !site || selected || !pageContext || pageContext.loading || migrating.current) return;
    migrating.current = true;
    void chooseRef.current(site);
  }, [selection, site, selected, pageContext]);

  const refreshPages = pageContext?.refresh;
  useEffect(() => {
    if (selection?.entry && !selectedId) void refreshPages?.().catch(() => {});
  }, [selectionMachineId, selectionSiteId, selection?.entry, selectedId, refreshPages]);

  useEffect(() => {
    // A catalog refresh must retire the old frame before its replacement can
    // mount. Otherwise a revoked grant can briefly reconnect while open waits.
    activeGrant.current = null;
    setGrant(null);
    if (!selectionMachineId || !selectionSiteId || !enabled || loading || !selectedEntry || !selectedId) return;
    const controller = new AbortController();
    let disposed = false;
    let opened: Grant | null = null;
    let activating = false;
    let activated = false;
    setError(null);
    setLoaded(false);
    setOpenedContent(initialContent.current);
    const revoke = (id: string) => {
      void viewerRequest(`/api/viewers/${id}`, { method: "DELETE", keepalive: true }).catch(() => {});
    };
    invalidate.current = () => {
      activated = false;
      if (opened) revoke(opened.id);
    };
    const pageHide = () => {
      if (opened) {
        opened.expires_at = 0; // A BFCache restore must acquire a fresh lease too.
        revoke(opened.id);
      }
    };
    window.addEventListener("pagehide", pageHide);
    const listener = (event: MessageEvent) => {
      if (!opened || opened.mode !== "isolated" || disposed || event.source !== frame.current?.contentWindow
          || event.origin !== opened.origin || event.data?.id !== opened.id) return;
      if (event.data?.type === "cc-viewer-error" && !activated) {
        setError("预览连接失败，请检查隔离域名与浏览器 Cookie 设置后重试。");
        return;
      }
      if (event.data?.type !== "cc-viewer-ready" || activating || activated
          || !/^[a-f0-9]{32}$/.test(event.data.challenge)) return;
      activating = true;
      void viewerRequest(`/api/viewers/${opened.id}/${event.data.challenge}`, {
        method: "POST", signal: controller.signal,
      }).then(() => { activated = true; }).catch((reason: unknown) => {
        if (!disposed) setError(reason instanceof Error ? reason.message : "无法打开预览。");
      });
    };
    window.addEventListener("message", listener);
    // Don't abort the creation POST: if its response races unmount we must
    // still receive and revoke the created grant instead of leaking a slot.
    void viewerRequest<Grant>("/api/viewers/open", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ machine_id: selectionMachineId, site_id: selectionSiteId,
        parent_machine_id: scope.machineId, sid: scope.sid, entry: selectedEntry }),
    }).then((value) => {
      opened = value;
      if (disposed) { revoke(value.id); return; }
      let valid = /^[a-f0-9]{32}$/.test(value.id);
      if (value.mode === "bridge") {
        valid &&= value.runner === `/__cc_viewer/bridge/${value.id}` && Array.isArray(value.script_origins)
          && value.script_origins.every((origin) => /^https:\/\/[^/]+$/.test(origin));
        activated = valid;
      } else if (value.mode === "isolated") {
        const url = new URL(value.origin);
        valid &&= /^https?:$/.test(url.protocol) && url.origin === value.origin && url.origin !== location.origin;
      } else valid = false;
      if (!valid) {
        revoke(value.id);
        throw new Error("预览来源配置无效。");
      }
      activeGrant.current = value;
      setGrant(value);
    }).catch((reason: unknown) => {
      if (!disposed) setError(reason instanceof Error ? reason.message : "无法打开预览。");
    });
    const timeout = window.setTimeout(() => {
        if (!disposed && !activated) setError("预览连接超时，请检查网络后重试。");
    }, 30000);
    const health = window.setInterval(() => {
      if (!opened || !activated || disposed) return;
      void viewerRequest<{ expires_at: number }>(`/api/viewers/${opened.id}`, { signal: controller.signal })
        .then((value) => { if (opened) opened.expires_at = value.expires_at; })
        .catch((reason: unknown) => {
          if (disposed) return;
          setError(reason instanceof Error ? reason.message : "预览已断开。");
          if (reason instanceof ViewerRequestError && [401, 403, 404, 410].includes(reason.status)) {
            invalidate.current?.(); setGrant(null);
          }
        });
    }, 15000);
    return () => {
      disposed = true;
      activeGrant.current = null;
      invalidate.current = null;
      controller.abort();
      window.clearInterval(health);
      window.clearTimeout(timeout);
      window.removeEventListener("message", listener);
      window.removeEventListener("pagehide", pageHide);
      if (opened) revoke(opened.id);
    };
  }, [selectionMachineId, selectionSiteId, selectedEntry, selectedId, enabled, loading,
    scope.machineId, scope.sid, retry]);

  useEffect(() => {
    if (!active) return;
    const escape = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing || event.key !== "Escape") return;
      if (document.querySelector('[role="dialog"], dialog[open]')) return;
      close.current();
    };
    window.addEventListener("keydown", escape);
    return () => window.removeEventListener("keydown", escape);
  }, [active]);

  useEffect(() => {
    const resume = () => {
      if (document.visibilityState === "visible" && grant && Date.now() / 1000 >= grant.expires_at) {
        setGrant(null);
        setLoading(true);
        setRetry((value) => value + 1);
      }
    };
    window.addEventListener("pageshow", resume);
    document.addEventListener("visibilitychange", resume);
    return () => {
      window.removeEventListener("pageshow", resume);
      document.removeEventListener("visibilitychange", resume);
    };
  }, [grant]);

  const refresh = () => {
    setPickerOpen(false);
    handledUrl.current = null;
    migrating.current = false;
    setLoading(true); setGrant(null); setError(null); setRetry((value) => value + 1);
    void pageContext?.refresh().catch(() => {});
  };
  const catalog = <div className="viewer-catalog">
        <div className="viewer-catalog-actions"><span>{browseAll
          ? (requestedUrl ? "选择生成该页面的设备与预览" : "已登记的预览") : "本会话"}</span>
          <button onClick={() => setBrowseAll((value) => !value)}>{browseAll ? "返回本会话" : "+ 关联已有预览"}</button></div>
        {pageContext?.error && <p role="alert">{pageContext.error}</p>}
        {!browseAll && related.map((page) => <div className="viewer-page-row" key={page.id}>
          <button className="viewer-site" aria-current={selected?.id === page.id ? "page" : undefined}
            onClick={() => { setPickerOpen(false); onSelect({ machine_id: page.machine_id, site_id: page.site_id, entry: page.entry }); }}>
            <span className="viewer-site-icon"><Icon name="globe" /></span>
            <span><b>{page.label}</b><small>{label(page.machine_id)} · {page.entry}{!page.available && " · 暂不可用"}</small></span>
            <Icon name={selected?.id === page.id ? "check" : "chevron-right"} size={16} />
          </button>
          <button className="iconbtn" title="从本会话移除，不删除文件" aria-label={`移除关联 ${page.label}`}
            onClick={() => void pageContext?.remove(page.id).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "移除失败。"))}>
            <Icon name="close" size={14} /></button>
        </div>)}
        {browseAll && sites.map((site) => <button className="viewer-site" key={`${site.machine_id}:${site.id}`}
          onClick={() => void choose(site)}>
          <span className="viewer-site-icon"><Icon name="globe" /></span>
          <span><b>{site.label}</b><small>{label(site.machine_id)} · {site.entry}</small></span>
          <Icon name="chevron-right" size={16} />
        </button>)}
        {!(browseAll ? sites : related).length && <div className="viewer-empty"><Icon name="folder" size={28} />
          <b>{browseAll ? "暂无可关联的预览" : "本会话还没有页面"}</b>
          <p>{browseAll ? "在资源设备登记需要读取的范围，并保持 Wrapper 在线。"
            : "生成的本地页面会显示在这里，也可以关联已有预览。"}</p></div>}
      </div>;
  const originalLink = originalUrl && <div className="viewer-original">
    <span title={originalUrl}>{originalUrl}</span>
    <a href={originalUrl} target="_blank" rel="noopener noreferrer">打开原链接</a>
  </div>;
  return <PanelFrame role="complementary" className="artifact-panel remote-viewer-panel" label="远程预览" data-lock-horizontal-swipe>
    <PanelResizer ariaLabel="调整预览宽度" />
    {pickerOpen && <div className="viewer-picker-backdrop" aria-hidden="true" onClick={() => setPickerOpen(false)} />}
    <header className="viewer-header">
      <button className="iconbtn viewer-mobile-back" onClick={onClose} aria-label="返回对话"><Icon name="back" /></button>
      {selection ? <button ref={pickerTrigger} type="button" className="viewer-heading" aria-label="切换预览"
        aria-expanded={pickerOpen} aria-haspopup="dialog" aria-controls={pickerOpen ? pickerId : undefined}
        onClick={() => { setBrowseAll(false); setPickerOpen(value => !value); }}>
        <strong>{heading}</strong><Icon name="chev" size={14} />
      </button> : <div className="viewer-heading"><strong>页面预览</strong></div>}
      <button className="iconbtn" onClick={refresh} title="刷新预览" aria-label="刷新预览"><Icon name="refresh" size={18} /></button>
      <PanelExpandButton label="远程预览" />
      <button className="iconbtn viewer-desktop-close" onClick={onClose} aria-label="关闭预览"><Icon name="close" size={18} /></button>
      {pickerOpen && <div id={pickerId} ref={picker} className="viewer-picker" role="dialog" aria-label="页面详情与切换" tabIndex={-1}>
        {catalog}{originalLink}
      </div>}
    </header>
    {!selection && originalLink}
    {grant && openedContent && selected?.content_revision && selected.content_revision !== openedContent
      && <div className="viewer-update"><span>页面已更新，刷新后查看新版</span><button onClick={refresh}>刷新</button></div>}
    <div className="viewer-stage">
      {error && !grant ? <div className="viewer-empty" role="alert"><Icon name="globe" size={28} />
        <p>{error}</p><button className="viewer-action" onClick={refresh}>重新连接</button></div>
      : !enabled ? <div className="viewer-empty"><Icon name="globe" size={28} />
        <b>尚未启用远程预览</b><p>在 Relay 开启预览，并保持资源设备的 Wrapper 在线。</p></div>
      : !grant && (loading || pageContext?.loading) ? <div className="viewer-empty" role="status"><span className="spinner" /><p>正在查找预览…</p></div>
      : !selection ? catalog
      : !selected ? <div className="viewer-empty"><p>设备已离线，或该预览已移除。</p>
        <button className="viewer-action" onClick={() => onSelect(null)}>选择其他预览</button></div>
      : grant ? <>{grant.mode === "bridge"
        ? <BridgeFrame key={grant.id} grant={grant} title={selected.label}
            onReady={() => { if (activeGrant.current === grant) setLoaded(true); }}
            onError={(message, clear) => {
              // A retired frame must never invalidate its successor's grant.
              if (activeGrant.current !== grant) return;
              setError(message);
              if (clear) { invalidate.current?.(); setGrant(null); }
            }} />
        : <iframe ref={frame} key={grant.id} title={selected.label}
            src={`${grant.origin}/__cc_viewer/bootstrap`} referrerPolicy="no-referrer"
            sandbox="allow-scripts allow-same-origin" onLoad={() => setLoaded(true)} />}
        {error ? <div className="viewer-error-banner" role="alert"><span>{error}</span>
          <button className="viewer-action" onClick={refresh}>重新连接</button></div>
        : !loaded && <div className="viewer-loading" role="status"><span className="spinner" />正在加载预览…</div>}</>
      : <div className="viewer-empty" role="status"><span className="spinner" /><p>正在连接设备…</p></div>}
    </div>
  </PanelFrame>;
}
