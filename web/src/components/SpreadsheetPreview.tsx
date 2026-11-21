import { useMemo, useRef, useState } from "react";
import "./SpreadsheetPreview.css";

type Cell = { r: number; c: number; v: string; f?: string };
type Sheet = { name: string; cells: Cell[]; truncated: boolean; range?: string };
const ROWS_PER_PAGE = 25;
function columnName(n: number): string {
  return n > 26 ? columnName(Math.floor((n - 1) / 26)) + String.fromCharCode(65 + (n - 1) % 26) : String.fromCharCode(64 + n);
}

export default function SpreadsheetPreview({ content }: { content: string }) {
  const [selected, setSelected] = useState(0);
  const [page, setPage] = useState(0);
  const scroll = useRef<HTMLDivElement>(null);
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
  const sheet = book?.sheets[selected] ?? book?.sheets[0];
  if (!sheet || !book) return <p className="preview-error" role="alert">表格预览数据不完整，请刷新重试。</p>;
  const rows = [...new Set(sheet.cells.map(cell => cell.r))].sort((a, b) => a - b);
  const columns = Math.max(1, ...sheet.cells.map(cell => cell.c));
  const values = new Map(sheet.cells.map(cell => [`${cell.r}:${cell.c}`, cell]));
  const pageCount = Math.max(1, Math.ceil(rows.length / ROWS_PER_PAGE));
  const current = Math.min(page, pageCount - 1);
  const visibleRows = rows.slice(current * ROWS_PER_PAGE, (current + 1) * ROWS_PER_PAGE);
  const go = (next: number) => {
    setPage(next);
    if (scroll.current) scroll.current.scrollTop = 0;
  };
  return <section className="spreadsheet-preview" aria-label="表格预览">
    {book.sheets.length > 1 && <nav aria-label="工作表" role="tablist">{book.sheets.map((item, index) => <button type="button" key={index} role="tab"
      aria-selected={sheet === item} onClick={() => { setSelected(index); go(0); }}>{item.name}</button>)}</nav>}
    {pageCount > 1 && <div className="spreadsheet-toolbar" role="group" aria-label="表格分页">
      <button type="button" disabled={current === 0} onClick={() => go(current - 1)}>上一组</button>
      <span aria-live="polite">第 {visibleRows[0]}–{visibleRows.at(-1)} 行 · {current + 1} / {pageCount}</span>
      <button type="button" disabled={current + 1 >= pageCount} onClick={() => go(current + 1)}>下一组</button>
    </div>}
    <div ref={scroll} className="spreadsheet-scroll" role={book.sheets.length > 1 ? "tabpanel" : "region"} aria-label={sheet.name} tabIndex={0}>
      {rows.length ? <table><thead><tr><th aria-label="行号" />{Array.from({ length: columns }, (_, index) => <th key={index} scope="col">{columnName(index + 1)}</th>)}</tr></thead>
        <tbody>{visibleRows.map(row => <tr key={row}><th scope="row">{row}</th>{Array.from({ length: columns }, (_, index) => {
          const cell = values.get(`${row}:${index + 1}`);
          return <td key={index} title={cell?.f ? `=${cell.f}` : undefined}>{cell?.v}</td>;
        })}</tr>)}</tbody></table> : <p className="spreadsheet-empty">此工作表没有可显示的单元格</p>}
    </div>
    {(sheet.truncated || book.truncated) && <p className="spreadsheet-note" role="status">内容较多，当前仅显示部分数据。</p>}
  </section>;
}
