import assert from "node:assert/strict";
import { ClipboardPasteGuard, PASTE_REPLAY_MS } from "../src/clipboard-paste-guard.ts";
import type { ClipboardImport } from "../src/clipboard-import.ts";
import { LONG_PASTE_THRESHOLD } from "../src/composer-pastes.ts";

let now = 0;
const guard = new ClipboardPasteGuard(true, () => now);
const empty = { value: "", selectionStart: 0, selectionEnd: 0 };
const clipboard = (text: string): ClipboardImport => ({
  text, files: [], images: [], errors: [], remainingAttachments: 8,
});
const imageClipboard = (): ClipboardImport => ({
  ...clipboard(""), files: [new File(["source bytes"], "image.png", { type: "image/png" })],
});
try {
  const before = { value: "before REPLACE after", selectionStart: 7, selectionEnd: 14 };
  const first = guard.capture(before, clipboard("新文字"));
  assert.equal(first.acceptText, true);
  assert.equal(guard.blocksNativeInsert(before, "insertFromPaste", "新文字"), false);
  guard.observeInput(first.after, "insertFromPaste", "新文字");
  now += 10;
  assert.equal(guard.capture(first.after, clipboard("新文字")).acceptText, false,
    "a second delivery must not insert again at the advanced caret");
  for (const type of ["insertFromPaste", "insertText"]) {
    assert.equal(guard.blocksNativeInsert(first.after, type, "新文字"), true);
  }
  assert.equal(guard.blocksNativeInsert(first.after, "insertText", "other"), false);
  assert.equal(guard.blocksNativeInsert(first.after, "deleteContentBackward", null), false);
  assert.equal(guard.capture({ ...first.after, selectionStart: 0, selectionEnd: 0 },
    clipboard("新文字")).acceptText, true, "a different selection is a different operation");

  guard.clear();
  const one = guard.capture(empty, clipboard("repeat"));
  guard.clear(); // A key, touch, pointer, composition, focus or session boundary.
  assert.equal(guard.capture(one.after, clipboard("repeat")).acceptText, true,
    "two explicit actions may paste identical content even in the same millisecond");
  now += PASTE_REPLAY_MS + 1;
  assert.equal(guard.capture(one.after, clipboard("repeat")).acceptText, true);

  guard.clear();
  const long = clipboard("long ".repeat(LONG_PASTE_THRESHOLD));
  assert.equal(guard.capture(empty, long).acceptText, true);
  assert.equal(guard.capture(empty, long).acceptText, false, "long text cards are also single imports");
  guard.clear();
  assert.equal(guard.capture(empty, long).acceptText, true);

  guard.clear();
  const batch = { images: [{ media_type: "image/png" as const, data: "first pixels" }],
    files: [{ filename: "same.txt", data: "first bytes" }], errors: [] };
  const imagePaste = guard.capture(empty, imageClipboard());
  assert.equal(guard.acceptAttachments(imagePaste, batch), true);
  now += 5;
  const imageReplay = guard.capture(empty, imageClipboard());
  assert.equal(guard.acceptAttachments(imageReplay, structuredClone(batch)), false);
  assert.equal(guard.acceptAttachments(imageReplay, {
    ...batch, files: [{ filename: "same.txt", data: "other bytes" }],
  }), true, "a matching filename is not proof of duplicate bytes");
  assert.equal(guard.acceptAttachments(imageReplay, {
    ...batch, images: [{ media_type: "image/png", data: "other pixels" }],
  }), true);
  guard.clear();
  assert.equal(guard.acceptAttachments(guard.capture(empty, imageClipboard()), batch), true);

  guard.clear();
  const failed = guard.capture(empty, imageClipboard());
  const failedBatch = { images: [], files: [], errors: ["retry"] };
  assert.equal(guard.acceptAttachments(failed, failedBatch), true);
  assert.equal(guard.acceptAttachments(guard.capture(empty, imageClipboard()), batch), true,
    "an entirely failed import must not block retrying its attachments");

  for (const successful of [batch, { ...batch, images: [] }, { ...batch, files: [] }]) {
    guard.clear();
    const partial = { ...successful, errors: ["broken.png 图片格式无法识别", "large.txt 超过 6 MiB"] };
    const first = guard.capture(empty, imageClipboard());
    now += 10; // Complete the first asynchronous import before its native replay.
    assert.equal(guard.acceptAttachments(first, partial), true);
    now += 5;
    assert.equal(guard.acceptAttachments(guard.capture(empty, imageClipboard()), structuredClone(partial)),
      false, "successful attachments in a partial import must not be appended twice");
    assert.equal(guard.acceptAttachments(guard.capture(empty, imageClipboard()), successful),
      false, "replay deduplication compares successful bytes independently of import errors");
    assert.equal(guard.acceptAttachments(guard.capture(empty, imageClipboard()), failedBatch), true);
    assert.equal(guard.acceptAttachments(guard.capture(empty, imageClipboard()), partial),
      false, "an entirely failed replay must not erase the last successful receipt");
    guard.clear(); // A later explicit paste may deliberately repeat the same attachments.
    assert.equal(guard.acceptAttachments(guard.capture(empty, imageClipboard()), partial), true);
    now += PASTE_REPLAY_MS + 1;
    assert.equal(guard.acceptAttachments(guard.capture(empty, imageClipboard()), partial), true,
      "partial imports remain repeatable outside the native replay window");
  }

  guard.clear();
  const pending = guard.capture(empty, imageClipboard());
  guard.clear(); // Change session while the first import is still resolving.
  assert.equal(guard.acceptAttachments(pending, batch), true);
  assert.equal(guard.acceptAttachments(guard.capture(empty, imageClipboard()), batch), true,
    "an old import may finish but must not seed another draft's receipt");

  const desktop = new ClipboardPasteGuard(false, () => now);
  assert.equal(desktop.capture(empty, long).acceptText, true);
  assert.equal(desktop.capture(empty, long).acceptText, true);
  const receipt = desktop.capture(empty, imageClipboard());
  assert.equal(desktop.acceptAttachments(receipt, batch), true);
  assert.equal(desktop.acceptAttachments(receipt, batch), true);
  desktop.clear();
} finally { guard.clear(); }
console.log("clipboard paste guard tests passed");
