import { Icon } from "../icons";
import { WEB_BUILD } from "../web-build";
import { newerVersion, UPDATE_LABELS, UPDATE_TERMINAL, updateReason } from "../app-updates";
import type { AppUpdatesController } from "../use-app-updates";
import { CenteredSheet } from "./CenteredSheet";
import "./AppUpdateSheet.css";

export default function AppUpdateSheet({ updates, deviceName, webUpdate, onClose }: {
  updates: AppUpdatesController; deviceName: string; onClose: () => void;
  webUpdate: { version: string | null; updating: boolean; message: string | null; reload: () => Promise<void> };
}) {
  const { data, error, checking, changing, pending } = updates;
  const job = data?.job;
  const active = !!job && !UPDATE_TERMINAL.has(job.phase);
  const available = newerVersion(data?.latest?.version, data?.current);
  const target = active ? job.version : pending?.version ?? data?.latest?.version;
  const needsRelay = newerVersion(target, data?.relay_version);
  const blocked = data?.reason || (needsRelay && !data?.allow_relay_update ? "relay_permission" : null);
  const canInstall = !!data?.enabled && available && !blocked && !active && !checking && !changing;
  const canCancel = active && ["submitting", "preparing", "downloading", "validating", "waiting"].includes(job.phase);
  const readyPage = !!webUpdate.version && !active;
  const title = active ? UPDATE_LABELS[job.phase] : pending ? "确认更新状态"
    : available ? "发现新版本" : readyPage ? "新版页面已就绪"
    : job?.phase === "succeeded" ? "更新完成"
    : checking ? "正在检查更新" : data?.latest && data.current ? "已是最新版本" : "检查应用更新";
  const reason = updateReason(error || (active ? job.reason : blocked || job?.reason));
  const rows = [
    ["页面", WEB_BUILD.version], ["服务器", data?.relay_version], [`当前设备 · ${deviceName}`, data?.current],
  ];
  return <CenteredSheet open label="检查更新" onClose={onClose} header={false}
    className="app-update-sheet" maxWidth={560} maxHeight={780} minimumHeight={450}>
    <header className="app-update-heading">
      <h2>检查更新</h2>
      <button type="button" className="iconbtn" aria-label="重新检查更新" disabled={checking || changing}
        onClick={() => void updates.check()}><Icon name="refresh" size={18} /></button>
    </header>
    <div className="sheet-scroll app-update-body">
      <div className="app-update-summary" role="status" aria-live="polite">
        <span className="app-update-symbol"><Icon name="download" size={27} /></span>
        <div><b>{title}</b>{(target || webUpdate.version) && <strong>v{target || webUpdate.version}</strong>}
          <small>当前页面 v{WEB_BUILD.version}</small></div>
      </div>
      <section><h3>更新范围</h3><div className="app-update-versions">
        {rows.map(([name, current]) => <div key={name}><span>{name}</span><span>{current || "待连接"}
          {newerVersion(target, current) && <> <i>→</i> {target}</>}</span></div>)}
      </div></section>
      {data?.latest?.notes && <section><h3>更新内容</h3>
        <div className="app-update-notes">{data.latest.notes}</div>
      </section>}
      {active && <section aria-label="更新进度" className="app-update-progress">
        <div className={`app-update-progress-track${job.phase === "unknown" ? " uncertain" : ""}`}><i /></div>
        <p>{job.phase === "waiting" ? "等待正在运行的任务、后台任务和排队消息完成。"
          : job.phase === "unknown" ? "保留原任务编号，确认结果后再继续。"
          : "升级由设备独立执行，关闭这个窗口不会中断。"}</p>
        {job.cancel_requested && <p>取消请求已提交，正在等待安全停止。</p>}
      </section>}
      {reason && <p className="app-update-warning" role="status">{reason}</p>}
      {pending && !active && <p className="app-update-help">尚未确认原请求结果。继续检查会使用同一个更新编号。</p>}
      {job?.phase === "succeeded" && job.cleanup_pending && <p className="app-update-help">
        版本已更新，部分旧文件仍有引用或需要管理员检查，已保留。</p>}
      <p className="app-update-info">更新期间会短暂断开连接，完成后自动重连。</p>
      <p className="app-update-help">{data?.busy_count ? `${data.busy_count} 项活动仍在进行，安装前会等待它们结束。`
        : "安装前会检查运行中的任务。"}</p>
      {webUpdate.message && <p className="app-update-warning" role="status">{webUpdate.message}</p>}
    </div>
    <footer className="app-update-footer">
      {canCancel && <button type="button" className="app-update-cancel" disabled={changing || job.cancel_requested}
        onClick={() => void updates.cancel()}>取消更新</button>}
      <button type="button" className="app-update-later" onClick={onClose}>{active ? "收起" : "稍后"}</button>
      {readyPage ? <button type="button" className="app-update-primary" disabled={webUpdate.updating}
        onClick={() => void webUpdate.reload()}>{webUpdate.updating ? "检查中…" : "更新页面"}</button>
        : <button type="button" className="app-update-primary"
          disabled={pending ? changing || active : !canInstall}
          onClick={() => target && void updates.start(target)}>
          {changing ? "提交中…" : pending && !active ? "确认原更新" : active ? UPDATE_LABELS[job.phase]
            : data?.busy_count && canInstall ? "任务结束后更新" : "立即更新"}
        </button>}
    </footer>
  </CenteredSheet>;
}
