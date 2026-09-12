import { useState } from "react";
import type { CodexContext } from "../protocol";
import { parseContextCapacity } from "../codex-context";
import { CODEX_CONTEXT_USAGE_NOTE } from "./ContextPopover";

export default function CodexContextControl({ state, onChange }: {
  state: CodexContext | null; onChange: (value: number | null) => boolean;
}) {
  const [input, setInput] = useState(state?.max_context_tokens?.toString() ?? "");
  const [error, setError] = useState<string | null>(null);
  const limit = state?.limit_tokens;
  const submit = (maxTokens: number | null) => {
    if (!onChange(maxTokens)) setError("设置暂未发送，请检查连接后重试");
    else setError(null);
  };
  return <div className="auto-compact-control codex-context-control">
    <strong>上下文上限 · 当前会话</strong>
    {!state ? <p role="status">读取模型上下文上限…</p> : <>
      <p>{state.model} · 可设置上限 {limit?.toLocaleString() ?? "未读取"} tokens</p>
      <p>设置当前会话的最大可用上下文，接近 95% 时自动压缩，以 Codex 原生限制为准。</p>
      <form onSubmit={(event) => {
        event.preventDefault();
        const maxTokens = parseContextCapacity(input);
        if (maxTokens === undefined || maxTokens === null || !limit || maxTokens > limit) {
          setError(`请输入 1–${limit?.toLocaleString() ?? "…"} tokens，可使用 200k、0.5m`);
          return;
        }
        submit(maxTokens);
      }}>
        <label htmlFor="codex-context-capacity">上下文上限（tokens）</label>
        <div className="codex-context-input">
          <input id="codex-context-capacity" value={input} onChange={(event) => setInput(event.target.value)}
            placeholder="例如 200k" autoComplete="off" disabled={!state.mutable || !limit} />
          <button type="submit" disabled={!state.mutable || !limit}>应用</button>
        </div>
      </form>
      <button className="codex-context-default" disabled={!state.mutable}
        onClick={() => { setInput(""); submit(null); }}>恢复 Codex 默认值</button>
      <p role="status">
        {state.pending
          ? `已保存上限 ${state.max_context_tokens?.toLocaleString() ?? "默认值"}，待确认生效`
          : `设定上限：${state.max_context_tokens?.toLocaleString() ?? "Codex 默认值"}`}
        <br />
        当前生效上限：{state.applied_max_context_tokens?.toLocaleString() ?? "待确认"}
        <br />
        当前生效阈值：{state.applied_threshold_tokens?.toLocaleString()
          ?? (state.max_context_tokens == null ? "Codex 默认值" : "待确认")}
      </p>
      <p>{CODEX_CONTEXT_USAGE_NOTE}</p>
      {(error || state.error) && <p role="alert">{error || state.error}</p>}
    </>}
  </div>;
}
