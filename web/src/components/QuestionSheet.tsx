import { useState } from "react";
import type { AskOption } from "../protocol";
import { Icon } from "../icons";
import { useImeSubmit } from "../use-ime-submit";

interface Props {
  question: string;
  header?: string | null;
  options: AskOption[];
  allowText?: boolean;
  secret?: boolean;
  onAnswer: (answer: string) => void;
}

export function QuestionSheet({ question, header, options, allowText, secret, onAnswer }: Props) {
  const [text, setText] = useState("");
  const imeSubmit = useImeSubmit<HTMLInputElement>((value) => {
    if (value.trim()) onAnswer(value);
  });
  return (
    <>
      <div className="scrim show" />
      <div className="sheet show" role="dialog" aria-label="操作确认">
        <div className="sheet-grip" />
        <div className="sheet-title">
          <span className="qa-ic"><Icon name="spark" size={15} /></span>
          {header || "助手想确认一下"}
        </div>
        <div className="sheet-scroll">
          <div className="qa-question">{question}</div>
          <div className="qa-options">
            {options.map((o, i) => (
              <button key={i} className="qa-opt" onClick={() => onAnswer(o.label)}>
                <span className="qa-opt-label">{o.label}</span>
                {o.ds && <span className="qa-opt-ds">{o.ds}</span>}
              </button>
            ))}
          </div>
          {allowText && <div className="qa-text-answer">
            <input ref={imeSubmit.inputRef} type={secret ? "password" : "text"} value={text}
              autoFocus={options.length === 0} placeholder={secret ? "输入敏感内容" : "输入回答"}
              onChange={(e) => setText(e.target.value)}
              onCompositionStart={imeSubmit.startComposition}
              onCompositionEnd={(e) => {
                imeSubmit.endComposition();
                setText(e.currentTarget.value);
              }}
              onKeyDown={(e) => {
                if (!imeSubmit.shouldSubmitKey({
                  key: e.key,
                  shiftKey: e.shiftKey,
                  isComposing: e.nativeEvent.isComposing,
                  keyCode: e.nativeEvent.keyCode,
                })) return;
                e.preventDefault();
                imeSubmit.requestSubmit();
              }} />
            <button disabled={!text.trim()}
              onPointerDown={imeSubmit.commitCompositionBeforePointerSubmit}
              onClick={imeSubmit.requestSubmit}>确定</button>
          </div>}
        </div>
      </div>
    </>
  );
}
