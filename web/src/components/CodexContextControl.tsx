import { useEffect, useState } from "react";
import type { CodexContext, ContextReport } from "../protocol";
import {
  codexContextPresentation,
  codexContextPresets,
  formatContextTokens,
  parseContextCapacity,
} from "../codex-context";

export default function CodexContextControl({ state, report, onChange }: {
  state: CodexContext | null; onChange: (value: number | null) => boolean;
  report?: ContextReport | null;
}) {
  const limit = state?.limit_tokens;
  const presets = codexContextPresets(limit);
  const selected = state?.max_context_tokens ?? null;
  const isCustom = selected != null && selected !== limit && !presets.includes(selected);
  const [customOpen, setCustomOpen] = useState(isCustom);
  const [input, setInput] = useState(selected?.toString() ?? "");
  const [error, setError] = useState<string | null>(null);
  const context = codexContextPresentation(state, report);
  const locked = !state?.mutable;

  useEffect(() => {
    setInput(selected?.toString() ?? "");
    setCustomOpen(isCustom);
    setError(null);
  }, [selected, isCustom]);

  const choose = (maxTokens: number | null) => {
    if (!onChange(maxTokens)) {
      setError("设置暂未发送，请检查连接后重试");
      return;
    }
    setError(null);
    setCustomOpen(false);
  };

  return <div className="auto-compact-control codex-context-control">
    <div className="auto-compact-head">
      <span>上下文上限</span>
      <b>{selected == null ? "跟随 Codex" : formatContextTokens(selected)}</b>
    </div>
    {!state ? <div className="ctx-pop-loading" role="status">读取模型上下文上限…</div> : <>
      <div className="auto-compact-options" role="group" aria-label="Codex 上下文上限预设">
        <button type="button" className={!customOpen && selected == null ? "selected" : ""}
          aria-pressed={!customOpen && selected == null} disabled={locked}
          onClick={() => choose(null)}>跟随 Codex</button>
        {presets.map(value => <button type="button" key={value}
          className={!customOpen && selected === value ? "selected" : ""}
          aria-pressed={!customOpen && selected === value} disabled={locked}
          onClick={() => choose(value)}>{formatContextTokens(value)}</button>)}
        <button type="button"
          className={!customOpen && selected != null && selected === limit ? "selected" : ""}
          aria-pressed={!customOpen && selected != null && selected === limit}
          disabled={locked || !limit} title={limit ? `${limit.toLocaleString()} tokens` : undefined}
          onClick={() => { if (limit) choose(limit); }}>最大值</button>
        <button type="button" className={customOpen ? "selected" : ""}
          aria-pressed={customOpen} disabled={locked || !limit}
          onClick={() => setCustomOpen(true)}>自定义</button>
      </div>
      {customOpen && <form className="auto-compact-custom" onSubmit={event => {
        event.preventDefault();
        const value = parseContextCapacity(input);
        if (value == null || !limit || value > limit) {
          setError(`请输入 1–${limit?.toLocaleString() ?? "…"} tokens，可使用 200k、0.5m`);
          return;
        }
        choose(value);
      }}>
        <label>
          <input aria-label="上下文上限（tokens）" value={input}
            onChange={event => setInput(event.target.value)}
            placeholder="例如 200k" autoComplete="off" disabled={locked || !limit} />
          <span>tokens</span>
        </label>
        <button type="submit" disabled={locked || !limit}>应用</button>
      </form>}
      <div className="codex-context-limits">
        <div className="ctx-pop-row"><span>模型最大窗口</span>
          <span className="ctx-pop-nums">{state.model_max_tokens?.toLocaleString() ?? "读取中"}</span></div>
        <div className="ctx-pop-row"><span>可设置上限</span>
          <span className="ctx-pop-nums">{limit?.toLocaleString() ?? "读取中"}</span></div>
      </div>
      <div className="auto-compact-meta">
        <span>生效上限 {context.capacity != null ? formatContextTokens(context.capacity) : "待确认"}
          {context.threshold != null ? ` · 实际阈值 ${formatContextTokens(context.threshold)}` : ""}</span>
        {context.status && <span role="status">{context.status}</span>}
        {state.pending && <span>官方 App 或 CLI 仍连接时可能无法切换，释放后可重新选择。</span>}
        {!state.mutable && <span>当前会话的上下文设置为只读。</span>}
        {(error || state.error) && <span className="error" role="alert">{error || state.error}</span>}
      </div>
    </>}
  </div>;
}
