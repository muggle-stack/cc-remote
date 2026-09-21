import { LONG_PASTE_THRESHOLD } from "./composer-pastes.ts";
import type { ClipboardImport } from "./clipboard-import.ts";
import type { QueryFile, QueryImg } from "./protocol.ts";

// A compatibility window for immediately repeated native paste deliveries,
// not a debounce of the draft. Explicit gestures always start a new action.
export const PASTE_REPLAY_MS = 120;
type Selection = { value: string; selectionStart: number; selectionEnd: number };
type Batch = { images: QueryImg[]; files: QueryFile[]; errors: string[] };
export interface PasteReceipt {
  at: number;
  epoch: number;
  before: Selection;
  after: Selection;
  acceptText: boolean;
}

const selection = (input: Selection): Selection => ({
  value: input.value, selectionStart: input.selectionStart, selectionEnd: input.selectionEnd,
});
const sameSelection = (a: Selection, b: Selection) => a.value === b.value
  && a.selectionStart === b.selectionStart && a.selectionEnd === b.selectionEnd;

/** Per-composer receipts, never shared across sessions or persisted. The text
 * receipt fences both a second paste event and a second native insertion.
 * Attachment receipts compare imported bytes, not filenames or file sizes. */
export class ClipboardPasteGuard {
  private epoch = 0;
  private text: { receipt: PasteReceipt; value: string } | null = null;
  private attachments: { receipt: PasteReceipt; batch: Batch } | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private readonly enabled: boolean;
  private readonly now: () => number;

  constructor(enabled: boolean, now = () => performance.now()) {
    this.enabled = enabled;
    this.now = now;
  }

  clear = (): void => {
    this.epoch++;
    this.text = null;
    this.attachments = null;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
  };

  private recent(receipt: PasteReceipt, at = this.now()): boolean {
    return this.enabled && receipt.epoch === this.epoch
      && at >= receipt.at && at - receipt.at <= PASTE_REPLAY_MS;
  }

  capture(input: Selection, clipboard: ClipboardImport): PasteReceipt {
    const before = selection(input);
    if (!this.enabled) return {
      at: 0, epoch: this.epoch, before, after: before, acceptText: true,
    };
    if (!clipboard.text && !clipboard.files.length && !clipboard.images.length
        && !clipboard.errors.length) this.clear();
    const text = clipboard.text.replace(/\r\n?/g, "\n");
    const at = this.now();
    const prior = this.text;
    const replay = !!text && !!prior && this.recent(prior.receipt, at)
      && prior.value === text && sameSelection(before, prior.receipt.after);
    const inline = clipboard.text.length <= LONG_PASTE_THRESHOLD;
    const caret = before.selectionStart + text.length;
    const receipt: PasteReceipt = {
      at, epoch: this.epoch, before, acceptText: !replay,
      after: !text || !inline || replay ? before : {
        value: before.value.slice(0, before.selectionStart) + text
          + before.value.slice(before.selectionEnd),
        selectionStart: caret, selectionEnd: caret,
      },
    };
    if (text && !replay) this.text = { receipt, value: text };
    // Do not retain clipboard/draft content beyond the compatibility window.
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = setTimeout(this.clear, PASTE_REPLAY_MS);
    return receipt;
  }

  blocksNativeInsert(input: Selection, type: string, data: string | null): boolean {
    const prior = this.text;
    if (!prior || !this.recent(prior.receipt)
        || !sameSelection(input, prior.receipt.after)) return false;
    if (type === "insertFromPaste") return data === null || data === prior.value;
    // Keyboard suggestions may deliver their extra insertion as insertText.
    // A key/pointer/composition boundary clears the receipt before real typing.
    return type === "insertText" && data === prior.value;
  }

  observeInput(input: Selection, type: string, data: string | null): void {
    const prior = this.text;
    if (prior && this.recent(prior.receipt)
        && (type === "insertFromPaste" || (type === "insertText" && data === prior.value))
        && sameSelection(input, prior.receipt.after)) return;
    this.clear();
  }

  acceptAttachments(receipt: PasteReceipt, batch: Batch): boolean {
    // Partial imports still append their successful attachments. Compare those
    // bytes even when other items failed; an explicit retry clears the receipt.
    if (!this.recent(receipt)
        || (!batch.images.length && !batch.files.length)) return true;
    const prior = this.attachments;
    if (prior && this.recent(prior.receipt, receipt.at)
        && sameSelection(receipt.before, prior.receipt.after)
        && batch.images.length === prior.batch.images.length
        && batch.files.length === prior.batch.files.length
        && batch.images.every((image, i) => image.media_type === prior.batch.images[i].media_type
          && image.data === prior.batch.images[i].data)
        && batch.files.every((file, i) => file.filename === prior.batch.files[i].filename
          && file.data === prior.batch.files[i].data)) return false;
    this.attachments = { receipt, batch };
    return true;
  }
}
