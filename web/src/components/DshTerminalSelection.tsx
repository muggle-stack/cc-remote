import { useCallback, useEffect, useRef, useState, type PointerEvent } from "react";
import type { Terminal } from "@xterm/xterm";
import { terminalPoint } from "../dsh-terminal-selection";

export function DshTerminalSelection({ terminal, host, onClear, onCopy, onPaste }: {
  terminal: Terminal; host: HTMLElement; onClear: () => void; onCopy: () => void; onPaste: () => void;
}) {
  const [, update] = useState(0);
  const [dragging, setDragging] = useState(false);
  const drag = useRef<{ anchor: number; x: number; y: number; offsetY: number } | null>(null);
  const extend = useCallback((x: number, y: number) => {
    const point = terminalPoint(host, terminal, x, y - (drag.current?.offsetY ?? 0), true);
    if (!point || !drag.current) return;
    const offset = point.row * terminal.cols + point.column;
    const first = Math.min(drag.current.anchor, offset);
    terminal.select(first % terminal.cols, Math.floor(first / terminal.cols), Math.max(1, Math.abs(offset - drag.current.anchor)));
  }, [host, terminal]);
  useEffect(() => {
    const refresh = () => update(value => value + 1);
    const subscriptions = [terminal.onSelectionChange(refresh), terminal.onScroll(refresh), terminal.onRender(refresh)];
    return () => subscriptions.forEach(subscription => subscription.dispose());
  }, [terminal]);
  useEffect(() => {
    if (!dragging) return;
    // Keep extending at the edge even when the finger stops moving.
    const timer = setInterval(() => {
      if (!drag.current) return;
      const rect = host.getBoundingClientRect();
      const direction = drag.current.y < rect.top + 20 ? -1 : drag.current.y > rect.bottom - 20 ? 1 : 0;
      if (direction) { terminal.scrollLines(direction); extend(drag.current.x, drag.current.y); }
    }, 60);
    return () => clearInterval(timer);
  }, [dragging, host, terminal, extend]);
  const range = terminal.getSelectionPosition();
  if (!range) return null;
  const rect = host.querySelector(".xterm-screen")?.getBoundingClientRect();
  if (!rect) return null;
  const parent = host.getBoundingClientRect();
  const cellWidth = rect.width / terminal.cols, cellHeight = rect.height / terminal.rows;
  const position = (point: { x: number; y: number }) => ({
    left: rect.left - parent.left + point.x * cellWidth,
    top: rect.top - parent.top + (point.y - terminal.buffer.active.viewportY + 1) * cellHeight,
  });
  const start = position(range.start), end = position(range.end);
  const startDrag = (event: PointerEvent<HTMLButtonElement>, anchor: number) => {
    event.preventDefault(); event.stopPropagation();
    event.currentTarget.setPointerCapture(event.pointerId);
    // The round handle sits below the selected line. Keep that grip offset
    // while dragging so the selection does not jump to the following row.
    drag.current = { anchor, x: event.clientX, y: event.clientY, offsetY: cellHeight / 2 + 8 };
    setDragging(true);
  };
  const move = (event: PointerEvent<HTMLButtonElement>) => {
    if (!drag.current) return;
    event.preventDefault(); event.stopPropagation();
    drag.current.x = event.clientX; drag.current.y = event.clientY;
    extend(event.clientX, event.clientY);
  };
  const finish = () => { drag.current = null; setDragging(false); };
  return <div className="dsh-terminal-selection" aria-label="终端文字选择">
    {[{ label: "起点", pos: start, fixed: range.end }, { label: "终点", pos: end, fixed: range.start }].map(({ label, pos, fixed }) => {
      return pos.top >= 0 && pos.top <= parent.height + cellHeight && <button key={String(label)}
        className="dsh-terminal-selection-handle" aria-label={`拖动选区${label}`} style={pos}
        onPointerDown={event => startDrag(event, fixed.y * terminal.cols + fixed.x)}
        onPointerMove={move} onPointerUp={finish} onPointerCancel={finish} onLostPointerCapture={finish} />;
    })}
    {!dragging && <div className="dsh-terminal-selection-menu" style={{ top: Math.max(4, Math.min(parent.height - 50, start.top - cellHeight - 48)) }}>
      <button onPointerDown={event => event.preventDefault()} onClick={onCopy}>复制</button>
      <button onPointerDown={event => event.preventDefault()} onClick={onPaste}>粘贴</button>
      <button onPointerDown={event => event.preventDefault()} onClick={() => terminal.selectAll()}>全选</button>
      <button onPointerDown={event => event.preventDefault()} onClick={onClear}>取消选择</button>
    </div>}
  </div>;
}
