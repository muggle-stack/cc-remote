import { useMemo, useState } from "react";
import { readableToolInput, readableToolOutput } from "../tool-details";

function RawToolData({ label, value }: { label: string; value: unknown }) {
  const [open, setOpen] = useState(false);
  return <details className="tool-raw" onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{label}</summary>
    {open && <pre className="tool-pre">{typeof value === "string"
      ? value : JSON.stringify(value, null, 2)}</pre>}
  </details>;
}

export function ToolInput({ input, omit = [] }: {
  input: Record<string, unknown>; omit?: string[];
}) {
  return <>
    {readableToolInput(input, omit).map(({ label, text }) => <div key={label}>
      <div className="tool-lbl">{label}</div>
      <pre className="tool-pre">{text}</pre>
    </div>)}
    <RawToolData label="原始参数" value={input} />
  </>;
}

export function ToolOutput({ output, truncated, label = "输出" }: {
  output: string; truncated?: boolean | null; label?: string;
}) {
  const projection = useMemo(() => readableToolOutput(output), [output]);
  return <>
    <div className="tool-lbl">{label}</div>
    <pre className="tool-pre">{projection.text}{truncated ? "\n…（输出已截断）" : ""}</pre>
    {projection.unwrapped && <RawToolData label="原始结果" value={output} />}
  </>;
}
