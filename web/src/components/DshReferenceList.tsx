import type { DshItem } from "../protocol";
import type { useDshReferences } from "./DshReferences";
import "./DshFeatures.css";
export default function DshReferences({ value, onPick }: { value: ReturnType<typeof useDshReferences>; onPick: (item: DshItem) => void }) {
  if (!value.open) return null;
  return <div className="dsh-references" aria-label="引用文件或会话">
    <div className="dsh-feature-caption">引用文件或会话</div>
    <div role="listbox" id="dsh-reference-list">
      {value.items.map((item, index) => <button key={item.id} type="button" role="option" id={`dsh-reference-${index}`}
        aria-selected={index === value.selected} onMouseDown={event => event.preventDefault()} onClick={() => onPick(item)}>
        <span>{item.state === "session" ? "会话" : item.state === "directory" ? "目录" : "文件"}</span><b>{item.title}</b>
        {item.detail && <small>{item.detail}</small>}
      </button>)}
    </div>
    {value.loading && <p role="status">查找中…</p>}
    {value.error && <p role="status">{value.error}<button type="button" onClick={value.retry}>重试</button></p>}
    {!value.loading && !value.error && !value.items.length && <p>没有匹配的文件或会话</p>}
  </div>;
}
