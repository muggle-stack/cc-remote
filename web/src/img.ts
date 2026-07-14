import type { QueryImg, QueryFile } from "./protocol";

// Browser-side mirror of cc_remote.attachments. Imports are processed
// sequentially and committed as one batch so Enter can never race a FileReader.
export const IMG_MAX_EDGE = 1568;
export const MAX_ATTACHMENT_COUNT = 8;
export const MAX_SINGLE_ATTACHMENT_BYTES = 6 * 1024 * 1024;
export const MAX_TOTAL_ATTACHMENT_BYTES = 8 * 1024 * 1024;
export const MAX_IMAGE_SOURCE_BYTES = 20 * 1024 * 1024;
export const MAX_IMAGE_DIMENSION = 8192;
export const MAX_IMAGE_PIXELS = 16_000_000;
export const MAX_FILENAME_BYTES = 240;

const ALLOWED_IMAGES = new Set(["image/png", "image/jpeg", "image/jpg", "image/webp"]);

export const decodedSize = (base64: string): number => {
  const clean = base64.replace(/\s/g, "");
  const padding = clean.endsWith("==") ? 2 : clean.endsWith("=") ? 1 : 0;
  return Math.max(0, Math.floor(clean.length * 3 / 4) - padding);
};

export function attachmentBytes(images: QueryImg[], files: QueryFile[]): number {
  return [...images, ...files].reduce((sum, item) => sum + decodedSize(item.data), 0);
}

function asciiAt(bytes: Uint8Array, offset: number, text: string): boolean {
  if (offset + text.length > bytes.length) return false;
  for (let i = 0; i < text.length; i++) {
    if (bytes[offset + i] !== text.charCodeAt(i)) return false;
  }
  return true;
}

function containsAscii(bytes: Uint8Array, text: string): boolean {
  for (let i = 0; i + text.length <= bytes.length; i++) {
    if (asciiAt(bytes, i, text)) return true;
  }
  return false;
}

export function imageDimensions(bytes: Uint8Array, mediaType: string): [number, number] | null {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const type = mediaType === "image/jpg" ? "image/jpeg" : mediaType;
  if (type === "image/png") {
    if (bytes.length < 24 || bytes[0] !== 0x89 || !asciiAt(bytes, 1, "PNG\r\n\x1a\n")
        || !asciiAt(bytes, 12, "IHDR") || containsAscii(bytes, "acTL")) return null;
    return [view.getUint32(16), view.getUint32(20)];
  }
  if (type === "image/jpeg") {
    if (bytes.length < 4 || bytes[0] !== 0xff || bytes[1] !== 0xd8) return null;
    const sof = new Set([0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7,
      0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf]);
    let pos = 2;
    while (pos + 4 <= bytes.length) {
      if (bytes[pos] !== 0xff) { pos++; continue; }
      while (pos < bytes.length && bytes[pos] === 0xff) pos++;
      if (pos >= bytes.length) break;
      const marker = bytes[pos++];
      if (marker === 0xd8 || marker === 0xd9) continue;
      if (pos + 2 > bytes.length) break;
      const length = view.getUint16(pos);
      if (length < 2 || pos + length > bytes.length) break;
      if (sof.has(marker) && length >= 7) {
        return [view.getUint16(pos + 5), view.getUint16(pos + 3)];
      }
      pos += length;
    }
    return null;
  }
  if (type === "image/webp") {
    if (bytes.length < 30 || !asciiAt(bytes, 0, "RIFF") || !asciiAt(bytes, 8, "WEBP")) return null;
    if (asciiAt(bytes, 12, "VP8X")) {
      if (bytes[20] & 0x02) return null; // animation
      const width = bytes[24] | (bytes[25] << 8) | (bytes[26] << 16);
      const height = bytes[27] | (bytes[28] << 8) | (bytes[29] << 16);
      return [width + 1, height + 1];
    }
    if (asciiAt(bytes, 12, "VP8 ") && bytes[23] === 0x9d
        && bytes[24] === 0x01 && bytes[25] === 0x2a) {
      return [view.getUint16(26, true) & 0x3fff, view.getUint16(28, true) & 0x3fff];
    }
    if (asciiAt(bytes, 12, "VP8L") && bytes[20] === 0x2f) {
      const bits = view.getUint32(21, true);
      return [(bits & 0x3fff) + 1, ((bits >>> 14) & 0x3fff) + 1];
    }
  }
  return null;
}

function readDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error(`${file.name || "附件"} 读取失败`));
    reader.readAsDataURL(file);
  });
}

export async function downscaleImage(file: File): Promise<QueryImg> {
  if (!ALLOWED_IMAGES.has(file.type)) throw new Error(`${file.name || "图片"} 格式不支持`);
  if (file.size > MAX_IMAGE_SOURCE_BYTES) throw new Error(`${file.name || "图片"} 原图超过 20 MiB`);
  const bytes = new Uint8Array(await file.arrayBuffer());
  const dimensions = imageDimensions(bytes, file.type);
  if (!dimensions) throw new Error(`${file.name || "图片"} 格式、动画或头信息不安全`);
  const [sourceWidth, sourceHeight] = dimensions;
  if (sourceWidth <= 0 || sourceHeight <= 0 || sourceWidth > MAX_IMAGE_DIMENSION
      || sourceHeight > MAX_IMAGE_DIMENSION || sourceWidth * sourceHeight > MAX_IMAGE_PIXELS) {
    throw new Error(`${file.name || "图片"} 像素尺寸过大`);
  }

  const dataUrl = await readDataUrl(file);
  const raw = (): QueryImg => ({
    // The allow-list check above narrows the runtime value; File.type itself is
    // declared as a broad string by lib.dom.d.ts.
    media_type: (file.type === "image/jpg" ? "image/jpeg" : file.type) as QueryImg["media_type"],
    data: dataUrl.split(",", 2)[1] || "",
  });
  if (Math.max(sourceWidth, sourceHeight) <= IMG_MAX_EDGE) return raw();

  return await new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => {
      const scale = IMG_MAX_EDGE / Math.max(image.width, image.height);
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(image.width * scale));
      canvas.height = Math.max(1, Math.round(image.height * scale));
      const context = canvas.getContext("2d");
      if (!context) { reject(new Error("图片处理失败")); return; }
      context.drawImage(image, 0, 0, canvas.width, canvas.height);
      const mediaType = file.type === "image/png" ? "image/png" : "image/jpeg";
      try {
        const output = canvas.toDataURL(mediaType, 0.85);
        resolve({ media_type: mediaType, data: output.split(",", 2)[1] || "" });
      } catch {
        reject(new Error("图片压缩失败"));
      }
    };
    image.onerror = () => reject(new Error(`${file.name || "图片"} 无法解码`));
    image.src = dataUrl;
  });
}

export interface AttachmentBatch {
  images: QueryImg[];
  files: QueryFile[];
  errors: string[];
}

/** Read one selection sequentially and return one atomic, limit-checked batch. */
export async function pickFiles(
  list: FileList | File[] | null,
  existingCount = 0,
  existingBytes = 0,
): Promise<AttachmentBatch> {
  const result: AttachmentBatch = { images: [], files: [], errors: [] };
  if (!list) return result;
  let count = existingCount;
  let totalBytes = existingBytes;

  // Do not materialize an arbitrarily large FileList before applying the
  // eight-item limit (drag/drop can contain far more entries than the picker).
  for (let index = 0; index < list.length; index++) {
    const file = list[index];
    if (count >= MAX_ATTACHMENT_COUNT) {
      result.errors.push(`一次消息最多 ${MAX_ATTACHMENT_COUNT} 个附件`);
      break;
    }
    try {
      let item: QueryImg | QueryFile;
      let image = false;
      if (file.type.startsWith("image/")) {
        item = await downscaleImage(file);
        image = true;
      } else {
        if (file.size > MAX_SINGLE_ATTACHMENT_BYTES) throw new Error(`${file.name} 超过 6 MiB`);
        if (!file.name || new TextEncoder().encode(file.name).byteLength > MAX_FILENAME_BYTES) {
          throw new Error("附件文件名为空或过长");
        }
        const dataUrl = await readDataUrl(file);
        item = { filename: file.name, data: dataUrl.split(",", 2)[1] || "" };
      }
      const size = decodedSize(item.data);
      if (size > MAX_SINGLE_ATTACHMENT_BYTES) throw new Error(`${file.name || "附件"} 超过 6 MiB`);
      if (totalBytes + size > MAX_TOTAL_ATTACHMENT_BYTES) throw new Error("附件解码后合计不能超过 8 MiB");
      if (image) result.images.push(item as QueryImg);
      else result.files.push(item as QueryFile);
      count++;
      totalBytes += size;
    } catch (error) {
      result.errors.push(error instanceof Error ? error.message : "附件读取失败");
    }
  }
  return result;
}
