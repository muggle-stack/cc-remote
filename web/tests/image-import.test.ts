import assert from "node:assert/strict";
import {
  imageDimensions, imageDimensionsFromBase64, inspectImageHeader,
  ATTACHMENT_LIMIT_NOTICE, MAX_ATTACHMENT_COUNT, snapshotAttachmentFiles,
} from "../src/img.ts";
import { readClipboardImport, resolveClipboardImport } from "../src/clipboard-import.ts";
import { resolveClipboardFiles } from "../src/clipboard-files.ts";
import { pngChunk, staticPng } from "./fixtures/png.ts";

const pixels = staticPng();
assert.ok(pixels.includes(Buffer.from("acTL")));
assert.deepEqual(imageDimensions(pixels, "image/png"), [2, 1],
  "literal acTL in static PNG pixel data must not be mistaken for animation");

const metadata = staticPng(2, 1, [pngChunk("tEXt", Buffer.from("Comment\0static acTL"))]);
assert.deepEqual(imageDimensions(metadata, "image/png"), [2, 1],
  "PNG metadata may mention an animation chunk name without being animated");
assert.deepEqual(imageDimensionsFromBase64(metadata.toString("base64"), "image/png"), [2, 1]);

const animation = Buffer.alloc(8);
animation.writeUInt32BE(1, 0);
const animated = staticPng(2, 1, [pngChunk("acTL", animation)]);
assert.equal(imageDimensions(animated, "image/png"), null,
  "an actual animation control chunk must still be rejected");
assert.equal(inspectImageHeader(animated, "image/png"), "animated");
assert.deepEqual(imageDimensions(pixels.subarray(0, 24), "image/png"), [2, 1],
  "bounded history header reads still expose intrinsic dimensions");
assert.equal(imageDimensions(pixels, "image/jpeg"), null,
  "declared image types must still match the header");
assert.equal(inspectImageHeader(pixels, "image/jpeg"), null);

const truncated = Buffer.concat([pixels.subarray(0, 33), pngChunk("tEXt", Buffer.alloc(1))]);
truncated.writeUInt32BE(0xffffffff, 33);
assert.deepEqual(imageDimensions(truncated, "image/png"), [2, 1],
  "oversized/truncated chunk lengths must terminate a bounded header scan");

const selected = [new File(["one"], "one.txt"), new File(["two"], "two.txt")];
const reads: number[] = [];
const largeSelection = new Proxy({ length: 100_000 }, {
  get(target, key) {
    if (key === "length") return target.length;
    assert.notEqual(key, Symbol.iterator, "a FileList must not be enumerated");
    const index = Number(key);
    assert.ok(index < selected.length, "do not read beyond the remaining allowance");
    reads.push(index);
    return selected[index];
  },
}) as FileList;
const bounded = snapshotAttachmentFiles(largeSelection, MAX_ATTACHMENT_COUNT - 2);
assert.deepEqual(reads, [0, 1]);
assert.deepEqual(bounded.files, selected);
assert.match(bounded.errors.join(";"), /最多 8 个附件/);

reads.length = 0;
const full = snapshotAttachmentFiles(largeSelection, MAX_ATTACHMENT_COUNT);
assert.deepEqual(full.files, []);
assert.deepEqual(reads, [], "a full draft must not access any selected files");
assert.equal(full.errors.length, 1);
assert.deepEqual(snapshotAttachmentFiles(null), { files: [], errors: [] });
assert.deepEqual(snapshotAttachmentFiles([], MAX_ATTACHMENT_COUNT), { files: [], errors: [] });

const exact = snapshotAttachmentFiles(selected, MAX_ATTACHMENT_COUNT - 2);
assert.deepEqual(exact.errors, [], "filling the allowance exactly is not an overflow");
selected.length = 0;
assert.deepEqual(exact.files.map((file) => file.name), ["one.txt", "two.txt"],
  "clearing the original selection must not clear the asynchronous import's snapshot");

for (const existing of [0, 6, MAX_ATTACHMENT_COUNT]) {
  const remaining = MAX_ATTACHMENT_COUNT - existing;
  const reads: number[] = [];
  let expired = false;
  const file = new File(["clipboard"], "clipboard.txt", { type: "text/plain" });
  const clipboardFiles = new Proxy({ length: 100_000 }, {
    get(target, key) {
      assert.equal(expired, false, "capture the clipboard before the paste event returns");
      if (key === "length") return target.length;
      assert.notEqual(key, Symbol.iterator, "do not enumerate the whole clipboard");
      const index = Number(key);
      assert.ok(index >= 0 && index < remaining, "only read files within the remaining quota");
      reads.push(index);
      return file;
    },
  });
  const data = {
    files: clipboardFiles,
    getData: (type: string) => type === "text/plain" ? "keep clipboard text" : "",
    get items() { throw new Error("do not materialize the unbounded clipboard item list"); },
  } as unknown as DataTransfer;
  const snapshot = readClipboardImport(data, existing);
  expired = true;
  assert.equal(snapshot.text, "keep clipboard text");
  assert.equal(snapshot.remainingAttachments, remaining);
  assert.deepEqual(reads, Array.from({ length: remaining }, (_, index) => index));
  const resolved = await resolveClipboardImport(snapshot);
  assert.equal(resolved.files.length, remaining,
    "ordinary files must be bounded before the asynchronous importer");
  assert.deepEqual(resolved.errors, [ATTACHMENT_LIMIT_NOTICE]);
}

const suppliedImage = new File([Uint8Array.from(pixels)], "clipboard.png", { type: "image/png" });
const suppliedText = new File(["text"], "clipboard.txt", { type: "text/plain" });
const embedded = `data:image/png;base64,${pixels.toString("base64")}`;
const duplicate = await resolveClipboardFiles({
  text: "", files: [suppliedImage, suppliedText], images: [embedded], errors: [],
  remainingAttachments: 2,
});
assert.deepEqual(duplicate.files, [suppliedImage, suppliedText]);
assert.deepEqual(duplicate.errors, [], "duplicate rich images must not trigger a false overflow");

const mixedOverflow = await resolveClipboardFiles({
  text: "", files: [suppliedText], images: [embedded, embedded], errors: [],
  remainingAttachments: 2,
});
assert.equal(mixedOverflow.files.length, 2, "files and rich images share one allowance");
assert.equal(mixedOverflow.files[0], suppliedText);
assert.deepEqual(mixedOverflow.errors, [ATTACHMENT_LIMIT_NOTICE]);

const fullFiles = [suppliedImage];
const fullImages = [embedded];
for (const list of [fullFiles, fullImages]) {
  Object.defineProperty(list, "0", { get() {
    throw new Error("a full draft must not load or decode clipboard attachments");
  } });
}
const fullClipboard = await resolveClipboardFiles({
  text: "", files: fullFiles, images: fullImages, errors: [ATTACHMENT_LIMIT_NOTICE],
  remainingAttachments: 0,
});
assert.deepEqual(fullClipboard, { files: [], errors: [ATTACHMENT_LIMIT_NOTICE] });

console.log("image import tests passed");
