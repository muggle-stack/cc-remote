import { useEffect, useMemo, useState } from "react";
import "./SpreadsheetPreview.css";

type Cell = { r: number; c: number; v: string; f?: string };
type Sheet = { name: string; cells: Cell[]; truncated: boolean; range?: string };
function columnName(n: number): string {
  return n > 26 ? columnName(Math.floor((n - 1) / 26)) + String.fromCharCode(65 + (n - 1) % 26) : String.fromCharCode(64 + n);
}

export default function SpreadsheetPreview({ content, data, title }: { content: string; data?: string; title: string }) {
  const [selected, setSelected] = useState(0);
  const [url, setUrl] = useState<string>();
  const book = useMemo(() => {
    try {
      if (content.length > 512 * 1024) return null;
      const parsed = JSON.parse(content) as { sheets: Sheet[]; truncated: boolean };
      if (!Array.isArray(parsed.sheets) || !parsed.sheets.length || parsed.sheets.length > 64) return null;
      for (const sheet of parsed.sheets) {
        if (typeof sheet.name !== "string" || !Array.isArray(sheet.cells) || sheet.cells.length > 10000) return null;
        for (const cell of sheet.cells) if (!Number.isInteger(cell.r) || cell.r < 1 || cell.r > 500
          || !Number.isInteger(cell.c) || cell.c < 1 || cell.c > 100 || typeof cell.v !== "string") return null;
      }
      return parsed;
    } catch { return null; }
  }, [content]);
  useEffect(() => {
    if (!data || data.length > Math.ceil(8 * 1024 * 1024 / 3) * 4) return;
    let next: string | undefined;
    try {
      const bytes = Uint8Array.from(atob(data), value => value.charCodeAt(0));
      if (bytes[0] !== 0x50 || bytes[1] !== 0x4b) return;
      next = URL.createObjectURL(new Blob([bytes], { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" }));
      setUrl(next);
    } catch { /* Preview remains readable if downloading is unavailable. */ }
    return () => { if (next) URL.revokeObjectURL(next); };
  }, [data]);
  const sheet = book?.sheets[selected] ?? book?.sheets[0];
  if (!sheet || !book) return <p className="preview-error" role="alert">表格预览数据不完整，请刷新重试。</p>;
  const rows = [...new Set(sheet.cells.map(cell => cell.r))].sort((a, b) => a - b);
  const columns = Math.max(1, ...sheet.cells.map(cell => cell.c));
  const values = new Map(sheet.cells.map(cell => [`${cell.r}:${cell.c}`, cell]));
  return <section className="spreadsheet-preview" aria-label="表格预览">
    <div className="spreadsheet-toolbar"><span>单元格预览</span>{url && <a href={url} download={title}>下载原文件</a>}</div>
    <nav aria-label="工作表" role="tablist">{book.sheets.map((item, index) => <button type="button" key={index} role="tab"
      aria-selected={selected === index} onClick={() => setSelected(index)}>{item.name}</button>)}</nav>
    <div className="spreadsheet-scroll" role="tabpanel" aria-label={sheet.name} tabIndex={0}>
      {rows.length ? <table><thead><tr><th aria-label="行号" />{Array.from({ length: columns }, (_, index) => <th key={index} scope="col">{columnName(index + 1)}</th>)}</tr></thead>
        <tbody>{rows.map(row => <tr key={row}><th scope="row">{row}</th>{Array.from({ length: columns }, (_, index) => {
          const cell = values.get(`${row}:${index + 1}`);
          return <td key={index} title={cell?.f ? `=${cell.f}` : undefined}>{cell?.v}</td>;
        })}</tr>)}</tbody></table> : <p className="spreadsheet-empty">此工作表没有可显示的单元格</p>}
    </div>
    <p className="spreadsheet-note">显示已保存的单元格值；公式不重新计算，图表与复杂格式请下载查看。
      {(sheet.truncated || book.truncated) && <strong> 内容较多，当前仅显示部分数据。</strong>}</p>
  </section>;
}
