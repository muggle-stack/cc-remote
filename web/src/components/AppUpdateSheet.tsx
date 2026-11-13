import { useState } from "react";
import ReactMarkdown from "react-markdown";
import { Icon } from "../icons";
import { WEB_BUILD } from "../web-build";
import { newerVersion, preferServerUpdate, UPDATE_LABELS, UPDATE_TERMINAL, updateReason, releaseHighlights } from "../app-updates";
import type { AppUpdatesController } from "../use-app-updates";
import { CenteredSheet } from "./CenteredSheet";
import "./AppUpdateSheet.css";

export default function AppUpdateSheet({ updates: deviceUpdates, serverUpdates, deviceName, webUpdate, onClose }: {
  updates: AppUpdatesController; deviceName: string; onClose: () => void;
  serverUpdates?: AppUpdatesController;
  webUpdate: { version: string | null; updating: boolean; message: string | null; reload: () => Promise<void> };
}) {
  const [selected, setSelected] = useState<"device" | "server" | null>(null);
  const serverMode = !!serverUpdates && (selected === "server" || (selected === null
    && preferServerUpdate(deviceUpdates.data, serverUpdates.data, !!deviceUpdates.pending, !!serverUpdates.pending)));
  const updates = serverMode ? serverUpdates! : deviceUpdates;
  const targetName = serverMode ? "服务器" : deviceName;
  const { data, error, checking, changing, pending, pendingStatus } = updates;
  const job = data?.job;
  const active = !!job && !UPDATE_TERMINAL.has(job.phase);
  const recovering = updates.maintenance && !active;
  const uncertain = active && job.phase === "unknown";
  const available = newerVersion(data?.latest?.version, data?.current);
  const target = active ? job.version : pending?.version ?? data?.latest?.version ?? job?.version;
  const relayVersion = serverUpdates?.data?.relay_version ?? deviceUpdates.data?.relay_version;
  const needsRelay = !serverMode && newerVersion(target, relayVersion);
  const blocked = data?.reason || (needsRelay && !data?.allow_relay_update ? "relay_permission" : null);
  const canInstall = !!data?.enabled && available && !blocked && !active && !recovering && !checking && !changing;
  const canCancel = active && ["submitting", "preparing", "downloading", "validating", "waiting"].includes(job.phase);
  const readyPage = !!webUpdate.version && !active && !recovering && !pending;
  const succeeded = job?.phase === "succeeded";
  const retryPending = !!pending && pendingStatus !== "unconfirmed";
  const reason = updateReason(pending && pendingStatus === "missing" && error === "outcome_pending" ? null
    : error || (active ? job.reason : blocked || job?.reason));
  const title = recovering ? "正在确认更新进度" : active ? job.phase === "installing" ? `正在安装 ${targetName} 更新` : UPDATE_LABELS[job.phase]
    : pending ? pendingStatus === "missing" ? "更新尚未启动" : "更新请求待确认" : readyPage ? "刷新页面即可使用新版"
    : available ? `cc-remote v${target}` : succeeded ? "更新已完成"
    : checking ? "正在检查更新" : data?.latest && data.current ? "已是最新版本" : "检查应用更新";
  const eyebrow = recovering ? "正在更新" : active ? uncertain ? "需要确认" : "正在更新" : pending ? checking ? "正在核实" : retryPending ? "可以重试" : "需要确认"
    : readyPage && succeeded ? `${targetName}更新完成` : readyPage ? "新版页面已就绪"
    : available ? "发现新版本" : succeeded ? "更新完成" : "版本状态";
  const rows = [
    ...(!serverMode ? [{ label: deviceName, current: deviceUpdates.data?.current, icon: "devices" as const }] : []),
    { label: "服务器", current: relayVersion, icon: "globe" as const },
    { label: "当前页面", current: WEB_BUILD.version, icon: "code" as const },
  ];
  const step = !serverMode && active && ["installing", "verifying"].includes(job.phase) ? 1 : 0;
  const highlights = releaseHighlights(data?.latest?.notes ?? "");
  return <CenteredSheet open label="检查更新" onClose={onClose} header={false}
    className="app-update-sheet" maxWidth={510} maxHeight={760} minimumHeight={360}>
    <header className="app-update-heading">
      <h2>应用更新</h2><span className="app-update-device" title={targetName}>{targetName}</span>
      <button type="button" className="app-update-close" aria-label="收起更新窗口" onClick={onClose}>
        <Icon name="chevrons-right" size={20} /></button>
    </header>
    <div className="sheet-scroll app-update-body">
      {serverUpdates && <div className="app-update-targets" role="group" aria-label="更新目标">
        <button type="button" aria-pressed={!serverMode} onClick={() => setSelected("device")}>{deviceName}</button>
        <button type="button" aria-pressed={serverMode} onClick={() => setSelected("server")}>服务器</button>
      </div>}
      <div className="app-update-summary" role="status" aria-live="polite">
        <span className={`app-update-eyebrow${succeeded && !active && !available ? " success" : ""}`}>
          {succeeded && !active && !available && <Icon name="check" size={16} />}{eyebrow}</span>
        <h3>{title}</h3>
        <p>{recovering ? `${targetName}正在更新，正在获取最新进度。` : active ? `目标版本 v${job.version}`
          : pending ? pendingStatus === "missing" ? `${targetName}暂未查到这次更新请求。可以重试，不会重复安装。`
            : "尚未确认上次提交的结果，将保留原请求继续核实。"
          : readyPage ? "新版页面已就绪，可刷新后继续。"
          : available ? serverMode ? "更新服务器和网页，设备上的版本可随后单独更新。" : "查看本次更新内容，选择合适的时间安装。"
          : data?.current ? `${targetName} v${data.current}` : "正在读取设备与服务器版本…"}</p>
      </div>
      {active ? <section className="app-update-progress" aria-label="更新进度">
        {!uncertain && job.phase !== "waiting" && <div className="app-update-progress-track" aria-hidden="true"><i /></div>}
        <ol className="app-update-steps">
          {(serverMode ? ["服务器", "当前页面"] : ["服务器", deviceName, "当前页面"]).map((label, index, steps) => {
            const done = !uncertain && (index < step || (!serverMode && index === 0 && !needsRelay));
            const current = !uncertain && index === step && !done;
            return <li key={index} className={done ? "done" : current ? "current" : ""}>
              <span className="app-update-step-icon">{done && <Icon name="check" size={14} />}</span>
              <span>{label}</span><small>{index === steps.length - 1 ? "待更新后检查" : done ? "已就绪" : uncertain ? "待确认"
                : current ? serverMode || index === 1 ? job.phase === "verifying" ? "验证中" : job.phase === "installing" ? "安装中" : "准备中" : job.phase === "updating_relay" ? "更新中" : "准备中" : "等待中"}</small>
            </li>;
          })}
        </ol>
        <p className="app-update-progress-copy">{job.phase === "waiting" ? "仅等待会受更新影响的操作结束，例如排队消息或尚未完成的提交。可独立运行的任务会继续。"
          : uncertain ? "正在核实原更新任务，请勿重复安装。" : "短暂断线时更新仍会继续，完成后自动重连。"}</p>
        {job.cancel_requested && <p className="app-update-help">正在等待安全取消。</p>}
      </section> : <section aria-label="更新范围" className="app-update-versions">
        {rows.map(({ label, current, icon }) => <div key={label}>
          <span><Icon name={icon} size={18} />{label}</span>
          <span>{current || "待连接"}{newerVersion(target, current) && <><i>→</i><b>{target}</b></>}</span>
        </div>)}
      </section>}
      {available && !active && !recovering && !pending && highlights.length > 0 && <section className="app-update-highlights">
        <h4>更新亮点</h4><ul>{highlights.map((line, index) => <li key={index}>{line}</li>)}</ul>
      </section>}
      {reason && <p className="app-update-warning" role="status">{reason}</p>}
      {succeeded && job.cleanup_pending && <p className="app-update-warning" role="status">
        更新已安装，旧部署文件尚待清理。请检查安装目录中的 .retention-status.json，查看阻塞原因。
      </p>}
      {!serverMode && needsRelay && !data?.allow_relay_update && serverUpdates && <button type="button"
        className="app-update-later app-update-server-link" onClick={() => setSelected("server")}>前往服务器更新</button>}
      {webUpdate.message && <p className="app-update-warning" role="status">{webUpdate.message}</p>}
      <details className="app-update-details">
        <summary>{active || recovering || pending ? "更新详情" : available ? "查看完整更新说明" : "版本与更新记录"}<Icon name="chevron-right" size={18} /></summary>
        {active && <div className="app-update-record"><p>目标版本 v{job.version}</p><p>当前阶段：{serverMode && job.phase === "installing" ? "安装服务器更新" : UPDATE_LABELS[job.phase]}</p></div>}
        {pending && !active && <p>重试沿用原更新请求；查询进度不会自动启动安装。</p>}
        {data?.latest?.notes && <div className="app-update-notes"><ReactMarkdown skipHtml
          allowedElements={["p", "h1", "h2", "h3", "h4", "ul", "ol", "li", "strong", "em", "code", "pre", "a", "blockquote", "hr", "br"]}
          components={{ a: ({ children, href }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> }}
        >{data.latest.notes}</ReactMarkdown></div>}
      </details>
    </div>
    <footer className="app-update-footer">
      <p>{readyPage ? "有未保存内容时会先提醒。" : active || recovering ? "收起窗口不会中断更新。"
        : pending ? "重试会保留原请求，避免重复安装。"
        : canInstall ? data?.busy_count ? `${data.busy_count} 项操作需要先完成，其余任务可继续运行。` : "安装前会检查任务能否安全继续。" : ""}</p>
      <div className="app-update-actions">
        {canCancel && <button type="button" className="app-update-later" disabled={changing || job.cancel_requested} onClick={() => void updates.cancel()}>取消更新</button>}
        {uncertain && <button type="button" className="app-update-primary" disabled={changing} onClick={() => void updates.recover()}>{changing ? "核对中…" : "重新核对并恢复"}</button>}
        <button type="button" className="app-update-later" onClick={onClose}>{active || recovering ? "收起，后台继续" : available || readyPage || pending ? "稍后" : "完成"}</button>
        {readyPage ? <button type="button" className="app-update-primary" disabled={webUpdate.updating} onClick={() => void webUpdate.reload()}>{webUpdate.updating ? "检查中…" : "刷新页面"}</button>
          : !active && !recovering && (available || pending) && <button type="button" className="app-update-primary" disabled={pending ? changing || checking : !canInstall}
            onClick={() => pending && !retryPending ? void updates.check() : target && void updates.start(target)}>{changing ? "提交中…" : pending ? checking ? "核实中…" : retryPending ? "重试更新" : "重新确认" : data?.busy_count && canInstall ? "就绪后更新" : serverMode ? `更新服务器至 v${target}` : `更新到 v${target}`}</button>}
      </div>
    </footer>
  </CenteredSheet>;
}
