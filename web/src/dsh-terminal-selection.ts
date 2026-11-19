import type { Terminal } from "@xterm/xterm";

export function terminalSelectedText(terminal: Terminal): string {
  const selection = window.getSelection();
  if (selection && !selection.isCollapsed && selection.anchorNode && selection.focusNode
    && terminal.element?.contains(selection.anchorNode) && terminal.element.contains(selection.focusNode)) {
    return selection.toString();
  }
  return terminal.getSelection();
}

export function terminalPoint(host: HTMLElement, terminal: Terminal, x: number, y: number, boundary = false) {
  const screen = host.querySelector(".xterm-screen")?.getBoundingClientRect();
  if (!screen?.width || !screen.height) return null;
  const columnPosition = (x - screen.left) / (screen.width / terminal.cols);
  const column = Math.max(0, Math.min(boundary ? terminal.cols : terminal.cols - 1,
    boundary ? Math.round(columnPosition) : Math.floor(columnPosition)));
  const row = Math.max(0, Math.min(terminal.rows - 1, Math.floor((y - screen.top) / (screen.height / terminal.rows))));
  return { column, row: terminal.buffer.active.viewportY + row };
}

/** Select cells, including wide characters, without making an editable DOM selection. */
export function selectTerminalWord(terminal: Terminal, point: { column: number; row: number }): void {
  const line = terminal.buffer.active.getLine(point.row);
  if (!line) return;
  let start = point.column;
  if (line.getCell(start)?.getWidth() === 0) start--;
  const separators = terminal.options.wordSeparator ?? " ()[]{}'\"";
  const word = (column: number) => {
    const cell = line.getCell(column);
    return cell?.getWidth() === 0 || !!cell?.getChars().trim() && !separators.includes(cell.getChars());
  };
  let end = start + Math.max(1, line.getCell(start)?.getWidth() ?? 1);
  if (word(start)) {
    while (start > 0 && word(start - 1)) start--;
    while (end < terminal.cols && word(end)) end++;
  }
  terminal.select(start, point.row, end - start);
}
