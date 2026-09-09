import assert from "node:assert/strict";
import { imageDimensions, imageDimensionsFromBase64, inspectImageHeader } from "../src/img.ts";
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

console.log("image import tests passed");
