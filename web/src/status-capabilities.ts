import type { StatusAccount } from "./protocol";

/** Explain why ChatGPT subscription statistics are absent without calling it a failure. */
export function accountStatsNote(account?: StatusAccount | null): string | null {
  if (account?.auth_type === "apiKey") {
    return "当前 API Key 模式不提供 ChatGPT 订阅限额和账户用量。";
  }
  if (account?.auth_type === "amazonBedrock") {
    return "当前 Amazon Bedrock 模式不提供 ChatGPT 订阅限额和账户用量。";
  }
  return null;
}
