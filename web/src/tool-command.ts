/** Presentation only: never execute or evaluate the native shell envelope. */
export function displayCommand(command: unknown): string {
  if (Array.isArray(command) && command.every((part) => typeof part === "string")) {
    if (command.length === 3 && /(?:^|\/)(?:bash|zsh|sh|dash|fish)$/.test(command[0])
        && /^-[il]*c$/.test(command[1])) return command[2];
    return command.map((part) => /^[\w./=-]+$/.test(part)
      ? part : JSON.stringify(part)).join(" ");
  }
  if (typeof command !== "string") return "";
  const match = command.match(/^(?:\S*\/)?(?:bash|zsh|sh|dash|fish)\s+-[il]*c\s+([\s\S]+)$/);
  if (!match) return command;
  const argument = match[1];
  if (argument.startsWith("'") && argument.endsWith("'")) {
    const inner = argument.slice(1, -1);
    if (!inner.replaceAll("'\\''", "").includes("'")) {
      return inner.replaceAll("'\\''", "'");
    }
  }
  if (argument.startsWith('"')) {
    try {
      const decoded: unknown = JSON.parse(argument);
      if (typeof decoded === "string") return decoded;
    } catch { /* Keep ambiguous shell quoting verbatim. */ }
  }
  return command;
}
