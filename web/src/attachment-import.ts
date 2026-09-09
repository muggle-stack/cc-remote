import type { QueryImg, QueryFile } from "./protocol";
import {
  IMG_MAX_EDGE, MAX_ATTACHMENT_COUNT, MAX_SINGLE_ATTACHMENT_BYTES,
  MAX_TOTAL_ATTACHMENT_BYTES, MAX_IMAGE_SOURCE_BYTES, MAX_IMAGE_DIMENSION,
  MAX_IMAGE_PIXELS, MAX_FILENAME_BYTES, decodedSize, inspectImageHeader,
  rememberQueryImageDimensions,
} from "./img";

const ALLOWED_IMAGES = new Set(["image/png", "image/jpeg", "image/jpg", "image/webp"]);

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
  const header = inspectImageHeader(bytes, file.type);
  if (!Array.isArray(header)) {
    // Import fails before a server request exists. Keep a local diagnostic
    // without logging the filename, clipboard text, or image payload.
    console.warn("cc-remote image import rejected", {
      reason: header || "invalid_header", mediaType: file.type, bytes: bytes.byteLength,
    });
    throw new Error(header === "animated"
      ? `${file.name || "图片"} 是动画图片，暂不支持；请保存为静态图片后上传`
      : `${file.name || "图片"} 图片格式无法识别，或内容与文件类型不符`);
  }
  const dimensions = header;
  const [sourceWidth, sourceHeight] = dimensions;
  if (sourceWidth <= 0 || sourceHeight <= 0 || sourceWidth > MAX_IMAGE_DIMENSION
      || sourceHeight > MAX_IMAGE_DIMENSION || sourceWidth * sourceHeight > MAX_IMAGE_PIXELS) {
    throw new Error(`${file.name || "图片"} 像素尺寸过大`);
  }

  const dataUrl = await readDataUrl(file);
  const raw = (): QueryImg => rememberQueryImageDimensions({
    // The allow-list check above narrows the runtime value; File.type itself is
    // declared as a broad string by lib.dom.d.ts.
    media_type: (file.type === "image/jpg" ? "image/jpeg" : file.type) as QueryImg["media_type"],
    data: dataUrl.split(",", 2)[1] || "",
  }, dimensions);
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
        resolve(rememberQueryImageDimensions({
          media_type: mediaType, data: output.split(",", 2)[1] || "",
        }, [canvas.width, canvas.height]));
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
