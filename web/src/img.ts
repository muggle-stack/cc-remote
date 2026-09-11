import type { QueryImg, QueryFile } from "./protocol";

// Browser-side mirror of cc_remote.attachments. Imports are processed
// sequentially and committed as one batch so Enter can never race a FileReader.
export const IMG_MAX_EDGE = 1568;
export const MAX_ATTACHMENT_COUNT = 8;
export const ATTACHMENT_LIMIT_NOTICE =
  `一次消息最多 ${MAX_ATTACHMENT_COUNT} 个附件，其余文件未导入`;
export const MAX_SINGLE_ATTACHMENT_BYTES = 6 * 1024 * 1024;
export const MAX_TOTAL_ATTACHMENT_BYTES = 8 * 1024 * 1024;
export const MAX_IMAGE_SOURCE_BYTES = 20 * 1024 * 1024;
export const MAX_IMAGE_DIMENSION = 8192;
export const MAX_IMAGE_PIXELS = 16_000_000;
export const MAX_FILENAME_BYTES = 240;

const BASE64_DIMENSION_SCAN_BYTES = 256 * 1024;
const queryImageDimensionCache = new WeakMap<QueryImg, readonly [number, number]>();

export const decodedSize = (base64: string): number => {
  const clean = base64.replace(/\s/g, "");
  const padding = clean.endsWith("==") ? 2 : clean.endsWith("=") ? 1 : 0;
  return Math.max(0, Math.floor(clean.length * 3 / 4) - padding);
};

export function attachmentBytes(images: QueryImg[], files: QueryFile[]): number {
  return [...images, ...files].reduce((sum, item) => sum + decodedSize(item.data), 0);
}

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

function asciiAt(bytes: Uint8Array, offset: number, text: string): boolean {
  if (offset + text.length > bytes.length) return false;
  for (let i = 0; i < text.length; i++) {
    if (bytes[offset + i] !== text.charCodeAt(i)) return false;
  }
  return true;
}

/** Intrinsic dimensions, an explicit animation marker, or an invalid header. */
export type ImageHeaderResult = [number, number] | "animated" | null;

export function inspectImageHeader(bytes: Uint8Array, mediaType: string): ImageHeaderResult {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const type = mediaType === "image/jpg" ? "image/jpeg" : mediaType;
  if (type === "image/png") {
    if (bytes.length < 24 || bytes[0] !== 0x89 || !asciiAt(bytes, 1, "PNG\r\n\x1a\n")
        || !asciiAt(bytes, 12, "IHDR")) return null;
    // Match cc_remote.attachments: only an actual acTL chunk marks an APNG.
    // Compressed pixels and metadata may contain the same four bytes. Stop at
    // incomplete chunks so bounded history/header-only reads keep working.
    let pos = 8;
    while (pos + 8 <= bytes.length) {
      const chunkEnd = pos + 12 + view.getUint32(pos);
      if (chunkEnd > bytes.length) break;
      if (asciiAt(bytes, pos + 4, "acTL")) return "animated";
      if (asciiAt(bytes, pos + 4, "IEND")) break;
      pos = chunkEnd;
    }
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
      if (bytes[20] & 0x02) return "animated";
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

export function imageDimensions(bytes: Uint8Array, mediaType: string): [number, number] | null {
  const header = inspectImageHeader(bytes, mediaType);
  return Array.isArray(header) ? header : null;
}

/** Read enough decoded bytes to determine the intrinsic size without creating
 * an Image element. QueryImg stays wire-compatible; the dimensions are a local
 * projection used to reserve layout before the browser decodes the image. */
export function imageDimensionsFromBase64(
  base64: string,
  mediaType: string,
): [number, number] | null {
  const scanChars = Math.ceil(BASE64_DIMENSION_SCAN_BYTES / 3) * 4;
  // Preview assets can be several MiB. Only normalize a bounded prefix; image
  // headers do not justify copying the complete base64 payload on the UI turn.
  let encoded = base64.slice(0, scanChars + 1024).replace(/\s/g, "");
  encoded = encoded.slice(0, scanChars);
  encoded = encoded.slice(0, encoded.length - (encoded.length % 4));
  if (!encoded) return null;
  try {
    const decoded = atob(encoded);
    const bytes = new Uint8Array(decoded.length);
    for (let index = 0; index < decoded.length; index++) {
      bytes[index] = decoded.charCodeAt(index);
    }
    const dimensions = inspectImageHeader(bytes, mediaType);
    if (!Array.isArray(dimensions)) return null;
    const [width, height] = dimensions;
    if (width <= 0 || height <= 0 || width > MAX_IMAGE_DIMENSION
        || height > MAX_IMAGE_DIMENSION || width * height > MAX_IMAGE_PIXELS) {
      return null;
    }
    return dimensions;
  } catch {
    return null;
  }
}

export function rememberQueryImageDimensions(
  image: QueryImg,
  dimensions: readonly [number, number],
): QueryImg {
  queryImageDimensionCache.set(image, dimensions);
  return image;
}

export function queryImageDimensions(image: QueryImg): [number, number] | null {
  const cached = queryImageDimensionCache.get(image);
  if (cached) return [cached[0], cached[1]];
  const dimensions = imageDimensionsFromBase64(image.data, image.media_type);
  if (!dimensions) return null;
  queryImageDimensionCache.set(image, dimensions);
  return dimensions;
}
