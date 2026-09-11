import type { DshState } from "./protocol";
import { clientSlashesFor, type Cmd } from "./data";

const controls: Record<string, Pick<Cmd, "name" | "ic">> = {
  goal: { name: "目标", ic: "verify" },
  plan: { name: "计划模式", ic: "plan" },
};

/** Native discovery owns availability; common missing controls explain why. */
export function dshCommandMatches(token: string, state?: DshState): Cmd[] {
  const commands = state?.commands ?? [];
  const local = clientSlashesFor("dsh");
  const matches: Cmd[] = commands.filter(c => c.name.startsWith(token)
    && !local.has(c.name)).map(c => ({
    slash: c.name, name: controls[c.name]?.name ?? c.name,
    ic: controls[c.name]?.ic ?? "term", ds: c.description,
  }));
  for (const slash of ["goal", "plan"]) {
    if (!slash.startsWith(token) || commands.some(c => c.name === slash)) continue;
    const unavailableReason = commands.length
      ? `${state?.agent_preset ?? "DSH"} 未启用；可新建标准或代码编排会话`
      : "命令列表读取中";
    matches.push({ slash, ...controls[slash], ds: unavailableReason, unavailable: true });
  }
  return matches;
}
