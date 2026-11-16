import type { Terminal } from "@xterm/xterm";

// OSC 133/633 provide explicit prompt boundaries. Plain zsh/bash terminals
// also toggle bracketed paste around their line editor. Require a shell prompt
// on the cursor line for that fallback, rather than treating writable as idle.
export function observeTerminalPrompt(terminal: Terminal) {
  let shell = "";
  let prompt: boolean | undefined;
  let submitted = false;
  const osc = (data: string) => {
    const kind = data.split(";", 1)[0];
    if (kind === "A" || kind === "C" || kind === "D") prompt = false;
    if (kind === "B") { prompt = true; submitted = false; }
    return false;
  };
  const subscriptions = [
    terminal.parser.registerOscHandler(133, osc),
    terminal.parser.registerOscHandler(633, osc),
    terminal.parser.registerCsiHandler({ prefix: "?", final: "h" }, params => {
      if (params.includes(2004)) submitted = false;
      return false;
    }),
  ];
  return {
    reset(info: unknown) {
      const value = info as { shell?: { name?: string; path?: string } } | undefined;
      const name = value?.shell?.name ?? value?.shell?.path;
      shell = typeof name === "string" ? name.split(/[\\/]/).at(-1) ?? "" : "";
      prompt = undefined;
      submitted = false;
    },
    input(data: string) {
      if (["\r", "\n", "\u0003", "\u0004"].some(key => data.includes(key))) {
        submitted = true;
        if (prompt !== undefined) prompt = false;
      }
    },
    ready() {
      if (submitted) return false;
      if (prompt !== undefined) return prompt;
      if (!/^(?:zsh|bash|fish|sh|ksh|dash|ash)$/iu.test(shell)
        || !terminal.modes.bracketedPasteMode) return false;
      const buffer = terminal.buffer.active;
      const cursor = buffer.baseY + buffer.cursorY;
      let text = buffer.getLine(cursor)?.translateToString(false, 0, buffer.cursorX) ?? "";
      for (let row = cursor; row > Math.max(0, cursor - 4) && buffer.getLine(row)?.isWrapped; row--) {
        text = (buffer.getLine(row - 1)?.translateToString(false) ?? "") + text;
      }
      return /(?:[$#%]\s|(?:^|\s)[❯➜→❱λ>]\s)/u.test(text);
    },
    dispose() { subscriptions.forEach(subscription => subscription.dispose()); },
  };
}
