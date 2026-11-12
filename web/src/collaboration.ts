import type { CollaborationMember, CollaborationMemberSpec, CollaborationMessage } from "./protocol";
import { classifyPreviewTarget } from "./preview-path.ts";

export function resolveRoomFile(member: Pick<CollaborationMember, "cwd" | "spec">, path: string): string {
  if (path.startsWith("/") || path.startsWith("~/")) return path;
  const cwd = member.cwd || member.spec.cwd;
  if (!cwd) return path;
  const target = classifyPreviewTarget(`${cwd}/.cc-remote-message`, path);
  return target.kind === "local" ? target.value : path;
}

export function roomMemberIdentity(member: Pick<CollaborationMember, "spec">): string {
  const harness = { claude: "Claude", codex: "Codex", dsh: "DSH" }[member.spec.engine];
  return `${harness} · ${member.spec.profile_id ?? "default"}`;
}

export function shortRoomSessionId(sid: string | null | undefined): string | undefined {
  if (!sid) return undefined;
  const nativeId = sid.split("@").at(-1) ?? sid;
  return nativeId.replace(/^session-/, "").slice(0, 8);
}

export function roomMemberDescription(member: CollaborationMember): string {
  const cwd = member.cwd || member.spec.cwd;
  return [roomMemberIdentity(member), cwd?.split("/").filter(Boolean).slice(-2).join("/"),
    shortRoomSessionId(member.native_sid || member.sid)].filter(Boolean).join(" · ");
}

export function roomMentionMembers(members: CollaborationMember[], query: string): CollaborationMember[] {
  const search = query.trim().toLocaleLowerCase();
  return members.filter(member => !member.removed && [member.name,
    roomMemberIdentity(member), member.cwd || member.spec.cwd, member.native_sid, member.sid,
  ].join(" ").toLocaleLowerCase().includes(search));
}

export function mergeRoomMessages(
  current: CollaborationMessage[],
  incoming: CollaborationMessage[],
): CollaborationMessage[] {
  const byId = new Map(current.map((message) => [message.id, message]));
  for (const message of incoming) byId.set(message.id, message);
  return [...byId.values()].sort((left, right) => left.seq - right.seq);
}

export interface RoomMention {
  start: number;
  end: number;
  memberIds: string[];
}
export interface RoomDraft {
  text: string;
  mentions: RoomMention[];
  messageId?: string;
  attachmentCount?: number;
}

export function refreshRoomMentionNames(text: string, mentions: RoomMention[], members: CollaborationMember[]): RoomDraft {
  const names = new Map(members.map(member => [member.id, member.name]));
  let offset = 0;
  let nextText = text;
  const nextMentions = [...mentions].sort((left, right) => left.start - right.start).map(mention => {
    const token = text.slice(mention.start, mention.end);
    const name = mention.memberIds.length === 1 && token !== "@所有参与者"
      ? names.get(mention.memberIds[0]) : undefined;
    const replacement = name ? `@${name}` : token;
    const start = mention.start + offset;
    nextText = nextText.slice(0, start) + replacement + nextText.slice(mention.end + offset);
    offset += replacement.length - token.length;
    return { ...mention, start, end: start + replacement.length };
  });
  return { text: nextText, mentions: nextMentions };
}

export function readRoomDraft(value: string | null): RoomDraft {
  try {
    const saved = JSON.parse(value ?? "null");
    if (typeof saved?.text !== "string" || !Array.isArray(saved.mentions))
      return { text: "", mentions: [] };
    const text = saved.text.slice(0, 65536);
    const mentions = saved.mentions.filter(
      (mention: RoomMention) =>
        Number.isInteger(mention.start) &&
        Number.isInteger(mention.end) &&
        mention.start >= 0 &&
        mention.end <= text.length &&
        mention.end > mention.start &&
        text[mention.start] === "@" &&
        Array.isArray(mention.memberIds) &&
        mention.memberIds.every((id) => typeof id === "string"),
    );
    return {
      text,
      mentions,
      ...(typeof saved.messageId === "string" &&
      /^[a-zA-Z0-9-]{1,128}$/.test(saved.messageId)
        ? { messageId: saved.messageId }
        : {}),
      ...(Number.isInteger(saved.attachmentCount) && saved.attachmentCount > 0
        ? { attachmentCount: Math.min(saved.attachmentCount, 8) }
        : {}),
    };
  } catch {
    return { text: "", mentions: [] };
  }
}

/** Keep only mentions whose original text survives the user's edit. */
export function editRoomMentions(
  previous: string,
  next: string,
  mentions: RoomMention[],
): RoomMention[] {
  let prefix = 0;
  while (
    prefix < previous.length &&
    prefix < next.length &&
    previous[prefix] === next[prefix]
  )
    prefix++;
  let suffix = 0;
  while (
    suffix < previous.length - prefix &&
    suffix < next.length - prefix &&
    previous[previous.length - 1 - suffix] === next[next.length - 1 - suffix]
  )
    suffix++;
  const oldEnd = previous.length - suffix;
  const offset = next.length - previous.length;
  return mentions.flatMap((mention) => {
    if (mention.end <= prefix) return [mention];
    if (mention.start >= oldEnd)
      return [
        {
          ...mention,
          start: mention.start + offset,
          end: mention.end + offset,
        },
      ];
    return [];
  });
}

export function roomCatalogKey(spec: CollaborationMemberSpec): string {
  return JSON.stringify([
    spec.engine,
    spec.profile_id ?? null,
    spec.cwd ?? null,
  ]);
}

export const roomActiveStates = new Set([
  "preparing",
  "submitting",
  "accepted",
  "running",
  "stopping",
]);

export function roomDeliveryLabel(status: string): string {
  return (
    (
      {
        queued: "等待投递",
        preparing: "准备中",
        submitting: "正在投递",
        accepted: "已接收",
        running: "处理中",
        stopping: "停止中",
        succeeded: "已完成",
        cancelled: "已停止",
        failed: "处理失败",
        unknown: "结果待确认",
      } as Record<string, string>
    )[status] ?? status
  );
}
