import {
  ATTACHMENT_LIMIT_NOTICE, MAX_ATTACHMENT_COUNT, snapshotAttachmentFiles,
} from "./img.ts";

export interface ClipboardImport {
  text: string;
  files: File[];
  images: string[];
  errors: string[];
  /** Capacity of the originating draft at paste time. */
  remainingAttachments: number;
}
const MAX_HTML_CHARS = 32 * 1024 * 1024;

/** Snapshot synchronously: DataTransfer is no longer readable after onPaste. */
export function readClipboardImport(data: DataTransfer, existingCount = 0): ClipboardImport {
  const remainingAttachments = Math.max(0, MAX_ATTACHMENT_COUNT - existingCount);
  // The browser's file-only view avoids enumerating every clipboard MIME item.
  const selected = snapshotAttachmentFiles(data.files, existingCount);
  const result: ClipboardImport = {
    text: data.getData("text/plain"), ...selected, images: [], remainingAttachments,
  };
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
  const images = document.createTreeWalker(template.content, NodeFilter.SHOW_ELEMENT, {
    acceptNode: (node) => node.nodeName === "IMG"
      ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP,
  });
  while (images.nextNode()) {
    if (result.images.length >= remainingAttachments) {
      if (!result.errors.includes(ATTACHMENT_LIMIT_NOTICE)) {
        result.errors.push(ATTACHMENT_LIMIT_NOTICE);
      }
      break;
    }
    const img = images.currentNode as Element;
    result.images.push(img.getAttribute("src") || img.getAttribute("data-lark-image-uri") || "");
  }
  if (!result.text) {
    template.content.querySelectorAll("br").forEach((node) => node.replaceWith("\n"));
    template.content.querySelectorAll("div,p,li").forEach((node) => node.append("\n"));
    result.text = (template.content.textContent ?? "").replace(/\n+$/, "");
  }
  return result;
}

export async function resolveClipboardImport(snapshot: ClipboardImport) {
  const { resolveClipboardFiles } = await import("./clipboard-files.ts");
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
