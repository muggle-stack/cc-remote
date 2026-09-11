import type { GoalStatus } from "./protocol";

export const goalStatusName: Record<GoalStatus, string> = {
  active: "进行中", paused: "已暂停", blocked: "受阻",
  usageLimited: "用量受限", budgetLimited: "预算已满", complete: "已完成",
};

export function goalTokens(value: number): string {
  return value >= 1000 ? `${Number((value / 1000).toFixed(1))}k` : String(value);
}

export function validGoalLimit(value: string, optional = false): boolean {
  return value === "" ? optional : /^[0-9]+$/.test(value) && Number.isSafeInteger(Number(value)) && Number(value) > 0;
}
