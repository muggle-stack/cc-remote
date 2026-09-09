import { MAX_ATTACHMENT_COUNT } from "./img";

export interface ClipboardImport {
  text: string;
  files: File[];
  images: string[];
  errors: string[];
}
const MAX_HTML_CHARS = 32 * 1024 * 1024;

/** Snapshot synchronously: DataTransfer is no longer readable after onPaste. */
export function readClipboardImport(data: DataTransfer): ClipboardImport {
  const result: ClipboardImport = {
    text: data.getData("text/plain"), files: [], images: [], errors: [],
  };
  for (const item of Array.from(data.items)) {
    if (item.kind !== "file") continue;
    const file = item.getAsFile();
    if (file) result.files.push(file);
  }
  const html = data.getData("text/html");
  if (!html) return result;
  if (html.length > MAX_HTML_CHARS) {
    result.errors.push("粘贴的富文本过大，图片未导入，请添加图片文件。");
    return result;
  }
  // Template contents stay inert, including image/iframe loads and handlers.
  // Never attach pasted markup to the live document.
  const template = document.createElement("template");
  template.innerHTML = html;
  template.content.querySelectorAll("script,style,iframe,object,template")
    .forEach((node) => node.remove());
  const images = Array.from(template.content.querySelectorAll("img"));
  result.images = images.slice(0, MAX_ATTACHMENT_COUNT).map((img) =>
    img.getAttribute("src") || img.getAttribute("data-lark-image-uri") || "");
  if (images.length > MAX_ATTACHMENT_COUNT) {
    result.errors.push(`一次最多导入 ${MAX_ATTACHMENT_COUNT} 张图片，其余图片未导入。`);
  }
  if (!result.text) {
    template.content.querySelectorAll("br").forEach((node) => node.replaceWith("\n"));
    template.content.querySelectorAll("div,p,li").forEach((node) => node.append("\n"));
    result.text = (template.content.textContent ?? "").replace(/\n+$/, "");
  }
  return result;
}

export async function resolveClipboardImport(snapshot: ClipboardImport) {
  const { resolveClipboardFiles } = await import("./clipboard-files");
  return resolveClipboardFiles(snapshot);
}

export function insertClipboardText(
  textarea: HTMLTextAreaElement, text: string, setText: (value: string) => void,
): void {
  // Preserve native textarea undo where supported. Controlled-state fallback
  // also covers synthetic paste events and browser environments without it.
  if (document.activeElement === textarea
      && typeof document.execCommand === "function"
      && document.execCommand("insertText", false, text)) return;
  const start = textarea.selectionStart;
  const end = textarea.selectionEnd;
  setText(textarea.value.slice(0, start) + text + textarea.value.slice(end));
  requestAnimationFrame(() => {
    if (document.activeElement === textarea) {
      textarea.setSelectionRange(start + text.length, start + text.length);
    }
  });
}
