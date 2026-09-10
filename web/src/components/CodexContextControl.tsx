import { useState } from "react";
import type { CodexContext } from "../protocol";
import { parseContextThreshold } from "../codex-context";

export default function CodexContextControl({ state, onChange }: {
  state: CodexContext | null; onChange: (value: number | null) => boolean;
}) {
  const [input, setInput] = useState(state?.threshold_tokens?.toString() ?? "");
  const [error, setError] = useState<string | null>(null);
  const limit = state?.limit_tokens;
  const submit = (threshold: number | null) => {
    if (!onChange(threshold)) setError("设置暂未发送，请检查连接后重试");
    else setError(null);
  };
  return <div className="auto-compact-control codex-context-control">
    <strong>自动压缩 · 当前会话</strong>
    {!state ? <p role="status">读取模型上下文上限…</p> : <>
      <p>{state.model} · 可用上限 {limit?.toLocaleString() ?? "未读取"} tokens</p>
      <p>上下文达到设定值时，由 Codex 自动压缩历史。仅影响当前会话。</p>
      <form onSubmit={(event) => {
        event.preventDefault();
        const threshold = parseContextThreshold(input);
        if (threshold === undefined || threshold === null || !limit || threshold > limit) {
          setError(`请输入 1–${limit?.toLocaleString() ?? "…"} tokens，可使用 200k、0.5m`);
          return;
        }
        submit(threshold);
      }}>
        <label htmlFor="codex-context-threshold">压缩阈值（tokens）</label>
        <div className="codex-context-input">
          <input id="codex-context-threshold" value={input} onChange={(event) => setInput(event.target.value)}
            placeholder="例如 200k" autoComplete="off" disabled={!state.mutable || !limit} />
          <button type="submit" disabled={!state.mutable || !limit}>应用</button>
        </div>
      </form>
      <button className="codex-context-default" disabled={!state.mutable}
        onClick={() => { setInput(""); submit(null); }}>恢复 Codex 默认值</button>
      <p role="status">{state.pending
        ? `已保存 ${state.threshold_tokens?.toLocaleString() ?? "默认值"}，等待会话空闲并可重新加载`
        : `当前设置：${state.applied_threshold_tokens?.toLocaleString() ?? "跟随 Codex 默认值"}`}</p>
      {(error || state.error) && <p role="alert">{error || state.error}</p>}
    </>}
  </div>;
}
