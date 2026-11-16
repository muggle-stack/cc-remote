import { useCallback, useContext, useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import type { DshApiClient } from "../dsh-api";
import type { DshItem, DshReadResult } from "../protocol";
import { uuid } from "../util";
import { Icon } from "../icons";
import { ChoicePicker } from "./ChoicePicker";
import { CenteredSheet } from "./CenteredSheet";
import { PanelRowMenu } from "./PanelActions";
import { DshTerminalClipboard, type TerminalClipboardTarget, type TerminalClipboardControls } from "./DshTerminalClipboard";
import { attachTerminalTouch, attachTerminalWheelScroll, type TerminalTouchFeedback } from "../dsh-terminal-touch";
import { attachTerminalKeyboard, terminalKey, modifyTerminalInput, noTerminalModifiers, type TerminalModifiers } from "../dsh-terminal-keyboard";
import { DshTerminalSelection } from "./DshTerminalSelection";
import { PanelVisible } from "../right-panel-presentation";
import "@xterm/xterm/css/xterm.css";

export default function DshTerminal({ sid, api }: { sid: string; api: DshApiClient }) {
  const visible = useContext(PanelVisible);
  const visibleRef = useRef(visible);
  visibleRef.current = visible;
  const [items, setItems] = useState<DshItem[]>([]);
  const [target, setTarget] = useState("");
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [connected, setConnected] = useState(false);
  const [stop, setStop] = useState("");
  const [exited, setExited] = useState(false);
  const [keyboardOpen, setKeyboardOpen] = useState(false);
  const [touchFeedback, setTouchFeedback] = useState<TerminalTouchFeedback | null>(null);
  const [selectionActive, setSelectionActive] = useState(false);
  const [view, setView] = useState<{ terminal: Terminal; host: HTMLElement } | null>(null);
  const [modifiers, setModifiers] = useState(noTerminalModifiers);
  const modifiersRef = useRef(modifiers);
  modifiersRef.current = modifiers;
  const keyboard = useRef<ReturnType<typeof attachTerminalKeyboard> | null>(null);
  const resetTouch = useRef<(() => void) | null>(null);
  const sendKey = useRef<((key: string) => void) | null>(null);
  const interrupt = useRef<(() => void) | null>(null);
  const suspend = useRef<(() => void) | null>(null);
  const clipboardTarget = useRef<TerminalClipboardTarget | null>(null);
  const clipboardControls = useRef<TerminalClipboardControls | null>(null);
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (visible) return;
    keyboard.current?.close();
    resetTouch.current?.();
    setModifiers(noTerminalModifiers);
  }, [visible]);
  const reload = useCallback(async () => {
    const result = await api.read(sid, "terminals");
    if (result.error) throw new Error(result.error);
    setItems(result.items);
  }, [api, sid]);
  useEffect(() => {
    const abort = new AbortController();
    api.read(sid, "terminals", { signal: abort.signal }).then(result => {
      if (!abort.signal.aborted) { setItems(result.items); setError(result.error ?? ""); }
    }).catch(error => { if (!abort.signal.aborted) setError(error.message); });
    return () => abort.abort();
  }, [api, sid]);
  const control = async (body: Record<string, unknown>) => {
    const result = await api.control(sid, "terminal", JSON.stringify(body));
    if (result.status !== "success") throw new Error(result.text || "终端操作结果未确认，请刷新列表");
    return result;
  };
  const create = async () => {
    setBusy(true); setError("");
    const id = uuid();
    try { await control({ action: "create", id }); await reload(); setTarget(id); }
    catch (error) { setError((error as Error).message); }
    finally { setBusy(false); }
  };
  const close = async () => {
    const id = stop;
    if (!id || busy) return;
    // Freeze the attachment before sending close: its final stream may end
    // before the command receipt, and that is not a connection failure.
    suspend.current?.();
    setBusy(true); setError("");
    try { await control({ action: "close", id }); setTarget(""); setStop(""); await reload(); }
    catch (error) { setError((error as Error).message); }
    finally { setBusy(false); }
  };
  useEffect(() => {
    if (!target || !host.current) return;
    let gone = false, writable = false, cursor = 0, pendingBytes = 0, inFlight = 0;
    const lease = uuid(), encoder = new TextEncoder();
    const queue: { value: Record<string, unknown>; bytes: number }[] = [];
    let rendering = false;
    const output: DshReadResult[] = [];
    const terminal = new Terminal({ cursorBlink: true, fontSize: 13, scrollback: 1000,
      theme: { background: "#10151e", foreground: "#e5eaf3", cursor: "#88afff", selectionBackground: "#496ba86b" } });
    const fit = new FitAddon(); terminal.loadAddon(fit); terminal.open(host.current);
    setView({ terminal, host: host.current });
    const inputKeyboard = attachTerminalKeyboard(host.current, terminal, setKeyboardOpen);
    keyboard.current = inputKeyboard;
    const clearSelection = () => { terminal.clearSelection(); setSelectionActive(false); };
    const onSelection = terminal.onSelectionChange(() => {
      if (!terminal.hasSelection()) setSelectionActive(false);
    });
    const detachWheelScroll = attachTerminalWheelScroll(host.current, terminal);
    setConnected(false); setExited(false); setError(""); setStop("");
    const freeze = () => {
      gone = true; writable = false; queue.length = 0; output.length = 0;
      resetTouch.current?.(); inputKeyboard.close(); setConnected(false);
    };
    suspend.current = freeze;
    const fail = (error: Error) => {
      if (gone) return;
      freeze(); setError(`${error.message} 输入不会自动重发。`);
    };
    const controlInput = async (body: Record<string, unknown>) => {
      const result = await api.control(sid, "terminal", JSON.stringify({ ...body, lease }));
      if (result.status !== "success") throw new Error(result.text || "终端操作结果未确认");
    };
    // A bounded pipeline keeps keystrokes ordered without a WAN round trip
    // between each key. Inputs never enter the reconnect outbox.
    const flush = () => {
      while (!gone && writable && inFlight < 8 && queue.length) {
        const { value, bytes } = queue.shift()!;
        inFlight++;
        void controlInput({ action: "input", value }).catch(fail).finally(() => {
          inFlight--; pendingBytes -= bytes; flush();
        });
      }
    };
    const input = (value: Record<string, unknown>) => {
      if (gone || !writable) return;
      const bytes = encoder.encode(JSON.stringify(value)).byteLength;
      if (pendingBytes + bytes > 65536) { fail(new Error("待发送输入过多，请重新连接")); return; }
      const previous = queue.at(-1);
      if (previous?.value.type === "write" && value.type === "write") {
        const data = String(previous.value.data) + String(value.data);
        if (encoder.encode(data).byteLength <= 2048) {
          const mergedBytes = encoder.encode(JSON.stringify({ type: "write", data })).byteLength;
          pendingBytes += mergedBytes - previous.bytes;
          previous.value = { type: "write", data }; previous.bytes = mergedBytes;
          flush();
          return;
        }
      }
      pendingBytes += bytes; queue.push({ value, bytes }); flush();
    };
    const onData = terminal.onData(data => {
      const keyInput = data[0] !== "\x1b" || data.length === 1 || data.length === 3 && data[0] === "\x1b"
        && (data[1] === "[" || data[1] === "O") && "ABCDHFZ".includes(data[2]);
      // Terminal protocol replies also arrive onData. A cursor/status reply
      // must not consume an armed modifier or clear a reading selection.
      if (keyInput && (modifiersRef.current.ctrl || modifiersRef.current.alt || modifiersRef.current.shift)) {
        data = modifyTerminalInput(data, modifiersRef.current);
        modifiersRef.current = noTerminalModifiers;
        setModifiers(noTerminalModifiers);
      }
      let part = "", size = 0;
      for (const char of data) {
        const n = encoder.encode(char).length;
        if (size + n > 2048) { input({ type: "write", data: part }); part = ""; size = 0; }
        part += char; size += n;
      }
      if (part) input({ type: "write", data: part });
    });
    clipboardTarget.current = { terminal, paste: text => {
      if (gone || !writable) return false;
      modifiersRef.current = noTerminalModifiers; setModifiers(noTerminalModifiers);
      terminal.paste(text); inputKeyboard.restore();
      return true;
    } };
    sendKey.current = key => {
      if (key === "Ctrl+C") {
        modifiersRef.current = noTerminalModifiers; setModifiers(noTerminalModifiers);
      }
      if (!gone && writable && visibleRef.current) terminal.input(terminalKey(terminal, key), true);
    };
    interrupt.current = () => sendKey.current?.("Ctrl+C");
    let columns = 0, rows = 0, resizeFrame = 0;
    const resize = () => {
      if (gone || !host.current?.clientWidth || !host.current.clientHeight) return;
      const viewport = terminal.buffer.active.viewportY;
      const wasReading = viewport < terminal.buffer.active.baseY;
      const previousCols = terminal.cols;
      fit.fit();
      if (wasReading && previousCols === terminal.cols) terminal.scrollToLine(viewport);
      if (writable && (columns !== terminal.cols || rows !== terminal.rows)) {
        columns = Math.min(500, Math.max(2, terminal.cols)); rows = Math.min(200, Math.max(1, terminal.rows));
        input({ type: "resize", cols: columns, rows });
      }
    };
    const detachTouch = attachTerminalTouch(host.current, terminal, {
      ready: () => !gone && writable && visibleRef.current,
      send: key => sendKey.current?.(key),
      activateTouch: inputKeyboard.activateTouch,
      openKeyboard: () => { if (!gone && writable && visibleRef.current) inputKeyboard.open(); },
      select: () => { inputKeyboard.close(); setSelectionActive(terminal.hasSelection()); },
      clearSelection,
      feedback: setTouchFeedback,
      resize,
    });
    resetTouch.current = detachTouch.cancel;
    const observer = new ResizeObserver(() => { cancelAnimationFrame(resizeFrame); resizeFrame = requestAnimationFrame(resize); });
    observer.observe(host.current);
    const write = (text: string) => new Promise<void>(resolve => terminal.write(text, resolve));
    const render = async () => {
      if (rendering || gone) return;
      rendering = true;
      try {
        while (!gone && output.length) {
          // Let xterm parse one coalesced write instead of adding a timer turn
          // for every small frame that arrived while the last write was pending.
          const batch = output.splice(0);
          let text = "", becameWritable = false, nextCursor = cursor;
          for (const result of batch) {
            if (result.error) {
              if (text) await write(text);
              throw new Error(result.error);
            }
            for (const item of result.items) {
              const frame = item.data ?? {};
              if (frame.type === "snapshot") {
                if (text) { await write(text); text = ""; }
                if (gone) return;
                clearSelection();
                terminal.reset(); text = String(frame.screen ?? "");
              } else if (frame.type === "output") text += String(frame.data ?? "");
              if ("writable" in frame) {
                becameWritable ||= !writable && frame.writable === true;
                writable = frame.writable === true; setConnected(writable);
              }
              const state = (frame.info as { state?: string } | undefined)?.state;
              if (state === "exited" || state === "failed") {
                writable = false; setConnected(false); setExited(true);
                if (state === "failed") setError("终端运行失败，可刷新列表或新建终端。");
                void reload().catch(() => {});
              }
            }
            nextCursor = result.next_seq ?? nextCursor;
          }
          if (text) await write(text);
          if (gone) return;
          cursor = nextCursor;
          // Acknowledge parsed output, not merely received network bytes.
          void controlInput({ action: "ack", cursor }).catch(fail);
          if (becameWritable) resize();
        }
      } catch (error) { fail(error as Error); }
      finally { rendering = false; }
    };
    const unwatch = api.watchTerminal(sid, lease, result => {
      if (gone) return;
      output.push(result);
      void render();
    }, () => fail(new Error("终端连接已断开，请重新连接")));
    const heartbeat = setInterval(() => {
      if (!gone) void controlInput({ action: "ack", cursor }).catch(fail);
    }, 10000);
    void api.control(sid, "terminal", JSON.stringify({ action: "attach", id: target, lease, stream: true })).then(result => {
      if (result.status !== "success") throw new Error(result.text || "终端连接未确认");
      if (gone) { void controlInput({ action: "detach" }).catch(() => {}); return; }
      if (visibleRef.current) inputKeyboard.restore();
    }).catch(fail);
    return () => {
      freeze(); unwatch(); clearInterval(heartbeat);
      if (clipboardTarget.current?.terminal === terminal) clipboardTarget.current = null;
      if (suspend.current === freeze) { suspend.current = null; interrupt.current = null; }
      keyboard.current = null; resetTouch.current = null; sendKey.current = null;
      setView(null); setSelectionActive(false); setModifiers(noTerminalModifiers);
      observer.disconnect(); cancelAnimationFrame(resizeFrame); detachTouch.dispose(); detachWheelScroll(); inputKeyboard.dispose(); onSelection.dispose(); onData.dispose(); terminal.dispose();
      void controlInput({ action: "detach" }).catch(() => {});
    };
  }, [sid, api, target, revision, reload]);
  const selected = items.find(item => item.id === target);
  const connectionStatus = exited ? "命令行已退出" : connected ? "已连接" : "只读或未连接";
  const reconnect = () => setRevision(value => value + 1);
  const toggleModifier = (name: keyof TerminalModifiers) => setModifiers(previous => ({ ...previous, [name]: !previous[name] }));
  return <div className="dsh-terminal" data-keyboard-open={keyboardOpen}>
    <div className="dsh-terminal-toolbar">
      <ChoicePicker label="选择终端" value={target} disabled={busy || !items.length} className="dsh-terminal-picker"
        options={items.map(item => ({ value: item.id, label: item.title, icon: "bash",
          description: `${item.state === "running" ? "运行中" : "已退出"} · ${item.detail}` }))}
        onChange={value => { setStop(""); setTarget(value); }}>
        <span className="dsh-terminal-connection" role="status" aria-label={connectionStatus} title={connectionStatus}>
          <i data-connected={connected} aria-hidden="true" /><span className="dsh-terminal-status-text">{connectionStatus}</span>
        </span><span>{selected?.title ?? "选择终端"}</span>
      </ChoicePicker>
      <span className="dsh-terminal-cwd" title={selected?.detail}>{selected?.detail}</span>
      <button className="iconbtn" aria-label="刷新列表" disabled={busy} onClick={() => void reload().catch(error => setError(error.message))}><Icon name="refresh" size={16} /></button>
      <button aria-label="新建终端" disabled={busy} onClick={() => void create()}><Icon name="plus" size={17} />新建</button>
    </div>
    {error && <p role="alert" className="dsh-feature-error">{error}</p>}
    {target && !connected && !error && <p role="status" className="dsh-terminal-notice">{connectionStatus}</p>}
    {target ? <div className="dsh-terminal-screen"><div className="dsh-terminal-viewport" ref={host} />
      {touchFeedback && <div className="dsh-terminal-direction" role="status" aria-label="方向键手势" data-direction={touchFeedback.direction}>
        <span data-active={touchFeedback.direction === "ArrowUp"}>↑</span><span data-active={touchFeedback.direction === "ArrowLeft"}>←</span>
        <span data-active={touchFeedback.direction === "ArrowRight"}>→</span><span data-active={touchFeedback.direction === "ArrowDown"}>↓</span>
        <small>{touchFeedback.speed ? `${touchFeedback.speed}×` : ""}</small>
      </div>}
      {visible && selectionActive && view && <DshTerminalSelection {...view}
        onCopy={() => clipboardControls.current?.copy()}
        onPaste={() => clipboardControls.current?.paste()}
        onClear={() => { view.terminal.clearSelection(); setSelectionActive(false); }} />}
    </div>
      : <div className="dsh-terminal-empty"><Icon name="bash" size={32} /><p>选择终端或新建一个终端</p><small>这里的命令由你直接运行。</small></div>}
    {target && <div className="dsh-terminal-actions">
      <div className="dsh-terminal-common-actions">
        <DshTerminalClipboard key={`${sid}:${target}:${revision}`} targetRef={clipboardTarget} controlsRef={clipboardControls} connected={connected && !busy} />
        <button aria-label="中断命令 Ctrl+C" disabled={busy || !connected}
          onPointerDown={event => event.preventDefault()} onClick={() => interrupt.current?.()}>Ctrl+C</button>
      </div>
      <div className="dsh-terminal-keys" role="toolbar" aria-label="终端快捷键">
        {["Escape", "Tab", "Shift+Tab"].map(key => <button key={key} disabled={busy || !connected}
          onPointerDown={event => event.preventDefault()} onClick={() => sendKey.current?.(key)}>{key === "Escape" ? "Esc" : key}</button>)}
        {(["ctrl", "alt", "shift"] as const).map(key => <button key={key} disabled={busy || !connected}
          aria-pressed={modifiers[key]} onPointerDown={event => event.preventDefault()}
          onClick={() => toggleModifier(key)}>{key === "ctrl" ? "Ctrl" : key === "alt" ? "Alt" : "Shift"}</button>)}
        {[["ArrowUp", "↑"], ["ArrowDown", "↓"], ["ArrowLeft", "←"], ["ArrowRight", "→"]].map(([key, label]) =>
          <button key={key} aria-label={key} disabled={busy || !connected} onPointerDown={event => event.preventDefault()} onClick={() => sendKey.current?.(key)}>{label}</button>)}
      </div>
      <button className="dsh-terminal-keyboard" aria-label={keyboardOpen ? "收起键盘" : "打开键盘"} aria-pressed={keyboardOpen}
        disabled={busy || !connected} onPointerDown={event => event.preventDefault()}
        onClick={() => keyboardOpen ? keyboard.current?.close() : keyboard.current?.open()}><Icon name="keyboard" size={21} /></button>
      <button className="dsh-terminal-reconnect" disabled={busy || exited} onClick={reconnect}><Icon name="refresh" size={15} />重新连接</button>
      <PanelRowMenu key={`${sid}:${target}:${revision}`} className="dsh-terminal-menu" label="终端更多操作" disabled={busy} items={[
        { label: "复制终端文字", icon: "copy", onClick: () => clipboardControls.current?.copy(), className: "dsh-terminal-menu-clipboard" },
        { label: "粘贴到终端", icon: "clipboard", onClick: () => clipboardControls.current?.paste(), disabled: !connected, className: "dsh-terminal-menu-clipboard" },
        { label: "重新连接", icon: "refresh", onClick: reconnect, disabled: exited, className: "dsh-terminal-menu-reconnect" },
        { label: "关闭终端", icon: "trash", onClick: () => setStop(target), disabled: exited, danger: true },
      ]} />
    </div>}
    {!!stop && <CenteredSheet open label="关闭终端？" maxWidth={360} maxHeight={240} minimumHeight={180} className="dsh-terminal-confirm" onClose={() => { if (!busy) setStop(""); }}>
      <p>结束 {items.find(item => item.id === stop)?.title ?? "这个终端"} 及其中运行的命令。</p>
      <div><button disabled={busy} onClick={() => setStop("")}>取消</button><button className="danger" disabled={busy} onClick={() => void close()}>{busy ? "正在关闭…" : "确认关闭"}</button></div>
    </CenteredSheet>}
  </div>;
}
