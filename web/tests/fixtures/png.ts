import { crc32, deflateSync } from "node:zlib";

export function pngChunk(kind: string, payload: Uint8Array): Buffer {
  const chunk = Buffer.alloc(12 + payload.length);
  chunk.writeUInt32BE(payload.length, 0);
  chunk.write(kind, 4, 4, "ascii");
  chunk.set(payload, 8);
  chunk.writeUInt32BE(crc32(chunk.subarray(4, -4)), chunk.length - 4);
  return chunk;
}

/** A decodable PNG whose uncompressed IDAT contains the literal bytes acTL. */
export function staticPng(width = 2, height = 1, extraChunks: Uint8Array[] = []): Buffer {
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header[8] = 8;
  header[9] = 6; // RGBA
  const pixels = Buffer.alloc((1 + width * 4) * height);
  pixels.write("acTL", 1, "ascii");
  return Buffer.concat([
    Buffer.from("89504e470d0a1a0a", "hex"),
    pngChunk("IHDR", header), ...extraChunks,
    pngChunk("IDAT", deflateSync(pixels, { level: 0 })),
    pngChunk("IEND", Buffer.alloc(0)),
  ]);
}
