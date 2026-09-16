import { MAX_ATTACHMENT_COUNT } from "./img";

const ATTACHMENT_LIMIT_NOTICE =
  `一次消息最多 ${MAX_ATTACHMENT_COUNT} 个附件，其余文件未导入`;

/** Snapshot before the picker is cleared or the drop event returns. Never
 * enumerate a whole selection just to enforce the attachment limit later. */
export function snapshotAttachmentFiles(
  list: FileList | File[] | null,
  existingCount = 0,
): { files: File[]; errors: string[] } {
  const files: File[] = [];
  if (!list) return { files, errors: [] };
  const length = list.length;
  const remaining = Math.max(0, MAX_ATTACHMENT_COUNT - existingCount);
  for (let index = 0; index < Math.min(length, remaining); index++) {
    files.push(list[index]);
  }
  return {
    files,
    errors: length > remaining ? [ATTACHMENT_LIMIT_NOTICE] : [],
  };
}
