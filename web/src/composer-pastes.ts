export const MAX_PROMPT_CHARS = 2 * 1024 * 1024;
export const LONG_PASTE_THRESHOLD = 1000;
export const PASTE_PREVIEW_CHARS = 180;
const PASTE_PREVIEW_SOURCE_CHARS = 1024;

export interface ComposerPaste {
  id: string;
  text: string;
  chars: number;
  lines: number;
}

export type PasteText = Pick<ComposerPaste, "text">;

/** Count lines in one pass without allocating an array proportional to a
 * potentially 2 MiB paste. Keep the empty-string result compatible with
 * String.split(), which reports one editable line. */
export function countTextLines(text: string): number {
  let lines = 1;
  for (let index = 0; index < text.length; index += 1) {
    if (text.charCodeAt(index) === 10) lines += 1;
  }
  return lines;
}

/** Build a bounded card preview without normalizing or retaining the complete
 * paste as a second render-time string. */
export function composerPastePreview(text: string): string {
  return text.slice(0, PASTE_PREVIEW_SOURCE_CHARS)
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, PASTE_PREVIEW_CHARS);
}

export function makeComposerPaste(text: string, id: string): ComposerPaste {
  return { id, text, chars: text.length, lines: countTextLines(text) };
}

export type ComposedPastePrompt =
  | { ok: true; prompt: string }
  | { ok: false; chars: number; maxChars: number };

/**
 * Long-paste cards normally precede the input. A native command can reserve
 * the first line by putting its input before the cards. Size the joined prompt
 * before allocating it so an oversized private draft stays intact and never
 * reaches the reliable command outbox as a frame the protocol will reject.
 */
export function composePastePrompt(
  pastes: readonly PasteText[],
  input: string,
  inputFirst = false,
): ComposedPastePrompt {
  const texts = pastes.map(paste => paste.text);
  const parts = (inputFirst ? [input, ...texts] : [...texts, input]).filter(part => part.length > 0);
  const chars = parts.reduce((total, part) => total + part.length, 0)
    + Math.max(0, parts.length - 1) * 2;
  if (chars > MAX_PROMPT_CHARS) {
    return { ok: false, chars, maxChars: MAX_PROMPT_CHARS };
  }
  return { ok: true, prompt: parts.join("\n\n") };
}
