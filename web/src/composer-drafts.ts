import type { Engine, QueryFile, QueryImg, Space } from "./protocol";
import type { ComposerPaste } from "./composer-pastes";

export type { ComposerPaste } from "./composer-pastes";

export interface ComposerDraft {
  input: string;
  images: QueryImg[];
  files: QueryFile[];
  pastes: ComposerPaste[];
}

const MAX_DRAFTS = 32;
const MAX_RETAINED_BYTES = 64 * 1024 * 1024;

function emptyDraft(): ComposerDraft {
  return { input: "", images: [], files: [], pastes: [] };
}

function cloneDraft(draft: ComposerDraft): ComposerDraft {
  return {
    input: draft.input,
    images: draft.images.map((image) => ({ ...image })),
    files: draft.files.map((file) => ({ ...file })),
    pastes: draft.pastes.map((paste) => ({ ...paste })),
  };
}

function draftBytes(draft: ComposerDraft): number {
  return draft.input.length * 2
    + draft.images.reduce((total, image) => total + image.data.length, 0)
    + draft.files.reduce(
      (total, file) => total + file.filename.length * 2 + file.data.length,
      0,
    )
    + draft.pastes.reduce((total, paste) => total + paste.text.length * 2, 0);
}

function isEmptyDraft(draft: ComposerDraft): boolean {
  return !draft.input && draft.images.length === 0 && draft.files.length === 0
    && draft.pastes.length === 0;
}

export function composerDraftKey(
  machineId: string,
  space: Space,
  engine: Engine,
  sessionId: string,
): string {
  return JSON.stringify([machineId, space, engine, sessionId]);
}

/** In-memory, bounded drafts keep sensitive text and attachments out of
 * persistent browser storage while making navigation between sessions lossless. */
export class ComposerDraftStore {
  private drafts = new Map<string, { draft: ComposerDraft; bytes: number }>();
  private retainedBytes = 0;
  private imports = 0;

  get(key: string): ComposerDraft {
    const entry = this.drafts.get(key);
    if (!entry) return emptyDraft();
    this.drafts.delete(key);
    this.drafts.set(key, entry);
    return cloneDraft(entry.draft);
  }

  set(key: string, draft: ComposerDraft): void {
    const previous = this.drafts.get(key);
    if (previous) {
      this.retainedBytes -= previous.bytes;
      this.drafts.delete(key);
    }
    if (isEmptyDraft(draft)) return;
    const stored = cloneDraft(draft);
    const bytes = draftBytes(stored);
    this.drafts.set(key, { draft: stored, bytes });
    this.retainedBytes += bytes;
    while (this.drafts.size > MAX_DRAFTS
        || this.retainedBytes > MAX_RETAINED_BYTES) {
      const oldest = this.drafts.entries().next().value as
        | [string, { draft: ComposerDraft; bytes: number }]
        | undefined;
      if (!oldest) break;
      this.drafts.delete(oldest[0]);
      this.retainedBytes -= oldest[1].bytes;
    }
  }

  delete(key: string): void {
    const entry = this.drafts.get(key);
    if (!entry) return;
    this.retainedBytes -= entry.bytes;
    this.drafts.delete(key);
  }

  rekey(oldKey: string, newKey: string): void {
    if (oldKey === newKey) return;
    const entry = this.drafts.get(oldKey);
    if (!entry) return;
    const draft = cloneDraft(entry.draft);
    this.delete(oldKey);
    this.set(newKey, draft);
  }

  beginImport(): () => void {
    this.imports += 1;
    let finished = false;
    return () => {
      if (!finished) this.imports -= 1;
      finished = true;
    };
  }

  get hasDrafts(): boolean {
    return this.drafts.size > 0 || this.imports > 0;
  }

  clear(): void {
    this.drafts.clear();
    this.retainedBytes = 0;
  }
}
