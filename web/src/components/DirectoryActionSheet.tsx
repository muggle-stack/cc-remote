import { useCallback, useEffect, useRef, useState } from "react";
import type { DirectoryActionPlan, DirectoryActionProgress, DirectoryActionScope, ErrorMsg, ServerEvent } from "../protocol";
import { CenteredSheet } from "./CenteredSheet";
import { Icon } from "../icons";
import "./DirectoryActionSheet.css";

export type DirectoryActionEvent = DirectoryActionPlan | DirectoryActionProgress | ErrorMsg;
interface Props {
  scope: DirectoryActionScope;
  machineLabel: string;
  listen: (listener: ((event: ServerEvent) => boolean) | null) => void;
  prepare: (scope: DirectoryActionScope) => string | null;
  run: (planId: string, engine: "claude" | "codex") => string | null;
  onClose: () => void;
}

export default function DirectoryActionSheet({ scope, machineLabel, listen, prepare, run, onClose }: Props) {
  const [plan, setPlan] = useState<DirectoryActionPlan | null>(null);
  const [progress, setProgress] = useState<DirectoryActionProgress | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pending = useRef<{ prepare: string | null; run: string | null; plan: string | null }>({ prepare: null, run: null, plan: null });
  const callbacks = useRef({ prepare, run, onClose });
  callbacks.current = { prepare, run, onClose };
  const refresh = useCallback(() => {
    setError(null); setPlan(null); setProgress(null);
    pending.current = { prepare: callbacks.current.prepare(scope), run: null, plan: null };
    if (!pending.current.prepare) setError("请求未发送，请检查连接后重试。");
  }, [scope]);
  useEffect(() => {
    listen(event => {
      if (event.type === "directory_action_plan" && event.request_id === pending.current.prepare
          && event.engine === scope.engine && event.cwd === scope.cwd && event.action === scope.action) {
        pending.current.plan = event.plan_id;
        setPlan(event); setError(null); return true;
      }
      if (event.type === "directory_action_progress" && event.request_id === pending.current.run
          && event.plan_id === pending.current.plan && event.engine === scope.engine) {
        setProgress(event); setRunning(!event.done); return true;
      }
      if (event.type === "error" && event.request_id
          && [pending.current.prepare, pending.current.run].includes(event.request_id)) {
        setError(event.message); setRunning(false); return true;
      }
      return false;
    });
    refresh();
    return () => listen(null);
  // One confirmation snapshot per mounted directory/scope, never a render-driven mutation.
  }, [scope, listen, refresh]);
  const confirm = () => {
    if (!plan || running || progress || pending.current.run) return;
    if (Date.now() / 1000 >= plan.expires_at) { setError("清单已过期，请重新读取后确认。"); return; }
    setRunning(true); setError(null);
    pending.current.run = callbacks.current.run(plan.plan_id, plan.engine);
    if (!pending.current.run) { setRunning(false); setError("操作未发送，请检查连接。"); }
  };
  const close = () => { if (!running) callbacks.current.onClose(); };
  const deleting = scope.action === "delete";
  const verb = deleting ? "删除" : "归档";
  const done = progress?.done === true;
  const basename = scope.cwd.split("/").filter(Boolean).at(-1) || scope.cwd;
  const settled = progress ? progress.succeeded + progress.skipped + progress.failed : 0;
  return <CenteredSheet open label={done ? `${verb}结果` : `${deleting ? "永久删除" : "归档"}此目录的会话？`}
    onClose={close} maxWidth={440} minimumHeight={320} className="directory-action-sheet">
    <div className="directory-action-body">
      <p>{deleting ? "删除后无法恢复。" : "归档后可在“已归档”中恢复。"}</p>
      <div className="directory-action-scope"><Icon name="folder" size={19} /><div>
        <b>{basename}</b><small>{machineLabel} · {plan?.profiles.join("、") || (scope.profile_id ? "当前账号" : "全部账号")}</small>
        <small className="directory-action-path">{scope.cwd}</small>
      </div></div>
      {!plan && !error && <div role="status" className="directory-action-loading"><span className="spinner" />正在读取完整会话清单…</div>}
      {plan && <>
        {!progress && <div className="directory-action-count"><span>将{deleting ? "永久删除" : "归档"}的会话</span><b>{plan.eligible} 个</b></div>}
        {!progress && deleting && <small className="directory-action-muted">未归档 {plan.eligible - plan.archived} 个 · 已归档 {plan.archived} 个</small>}
        {!progress && plan.skipped > 0 && <p className="directory-action-note">{plan.skipped} 个运行中或暂不可操作的会话将保留。</p>}
        <p className="directory-action-note">{deleting ? "包含子代理记录。" : "运行中、后台任务未结束或有排队消息的会话将保留。"}项目目录和文件保留。</p>
      </>}
      {progress && <div className="directory-action-progress" role="status" aria-live="polite">
        <b>{done ? "处理完成" : `正在${verb} ${settled} / ${progress.total}`}</b>
        {!done && <progress value={settled} max={progress.total || 1} />}
        <span>{progress.succeeded} 个成功 · {progress.skipped + (plan?.skipped ?? 0)} 个保留 · {progress.failed} 个未完成</span>
        {progress.errors.map((detail, index) => <small key={index}>{detail}</small>)}
      </div>}
      {error && <p role="alert" className="directory-action-error">{error}</p>}
      <div className="directory-action-buttons">
        {!done && <button disabled={running} onClick={close}>取消</button>}
        {error && !pending.current.run && <button onClick={refresh}>重新读取</button>}
        {done ? <button className="directory-action-primary" onClick={close}>完成</button>
          : <button className={deleting ? "directory-action-danger" : "directory-action-primary"}
            disabled={!plan?.eligible || running || !!error || !!pending.current.run} onClick={confirm}>
            {running ? "正在处理…" : `${verb}${plan ? ` ${plan.eligible} 个` : ""}会话`}
          </button>}
      </div>
    </div>
  </CenteredSheet>;
}
