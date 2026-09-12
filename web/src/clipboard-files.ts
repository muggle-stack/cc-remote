import { ATTACHMENT_LIMIT_NOTICE, MAX_IMAGE_SOURCE_BYTES } from "./img.ts";
import type { ClipboardImport } from "./clipboard-import.ts";
const IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/webp"]);

function embeddedImage(source: string, index: number): File | null {
  const match = /^data:(image\/(?:png|jpeg|webp));base64,([\s\S]+)$/i.exec(source);
  if (!match) return null;
  if (match[2].length > Math.ceil(MAX_IMAGE_SOURCE_BYTES * 4 / 3) + 1024) {
    throw new Error("粘贴的图片过大，请添加较小的图片。");
  }
  const decoded = atob(match[2].replace(/\s/g, ""));
  if (decoded.length > MAX_IMAGE_SOURCE_BYTES) throw new Error("粘贴的图片过大。");
  const bytes = Uint8Array.from(decoded, (char) => char.charCodeAt(0));
  return new File([bytes], `pasted-${index + 1}.${match[1].split("/")[1]}`,
    { type: match[1].toLowerCase() });
}

/** Import supplied bytes only. Private native/file/blob references belong to
 * the source app; never proxy them through the Wrapper or borrow its credentials. */
export async function resolveClipboardFiles(snapshot: ClipboardImport): Promise<{
  files: File[]; errors: string[];
}> {
  const limit = snapshot.remainingAttachments;
  const files = snapshot.files.slice(0, limit);
  const suppliedCount = files.length;
  const errors = [...snapshot.errors];
  const reportOverflow = () => {
    if (!errors.includes(ATTACHMENT_LIMIT_NOTICE)) errors.push(ATTACHMENT_LIMIT_NOTICE);
  };
  if (snapshot.files.length > limit || snapshot.images.length > limit) reportOverflow();
  let unavailable = 0;
  let native = false;
  for (let index = 0; index < Math.min(snapshot.images.length, limit); index++) {
    const source = snapshot.images[index];
    try {
      const file = embeddedImage(source, index);
      if (file) files.push(file);
      else {
        unavailable += 1;
        native ||= /^(?:native-resource|imkey):/i.test(source);
      }
    } catch {
      unavailable += 1;
    }
  }
  // Browsers commonly expose a File alongside an inaccessible <img src>.
  // That supplied file covers one reference; inline bytes are deduped below.
  unavailable = Math.max(0, unavailable
    - files.slice(0, suppliedCount).filter((file) => IMAGE_TYPES.has(file.type)).length);
  if (unavailable) errors.push(native
    ? `飞书的 ${unavailable} 张图片仅提供应用内引用，浏览器无法读取。请单独复制图片或添加图片文件。`
    : `${unavailable} 张图片未包含可读取的图片内容，请单独复制图片或添加图片文件。`);
  const unique: File[] = [];
  const contents: { bytes: Uint8Array; type: string; matched: boolean }[] = [];
  for (const [index, file] of files.entries()) {
    // Let the regular importer report unsupported/oversized files without
    // allocating their bodies just for clipboard deduplication.
    if (IMAGE_TYPES.has(file.type) && file.size <= MAX_IMAGE_SOURCE_BYTES) {
      let bytes: Uint8Array;
      try { bytes = new Uint8Array(await file.arrayBuffer()); }
      catch { errors.push("一张粘贴图片读取失败，请重新添加。"); continue; }
      const supplied = index < suppliedCount;
      const duplicate = !supplied && contents.find((prior) => !prior.matched
        && prior.type === file.type && prior.bytes.length === bytes.length
        && prior.bytes.every((byte, i) => byte === bytes[i]));
      if (duplicate) { duplicate.matched = true; continue; }
      if (supplied) contents.push({ bytes, type: file.type, matched: false });
    }
    if (unique.length >= limit) {
      reportOverflow();
      break;
    }
    unique.push(file);
  }
  return { files: unique, errors };
}
