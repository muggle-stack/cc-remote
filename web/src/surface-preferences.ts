import type { Engine, Space } from "./protocol";

export const LEGACY_SPACE_KEY = "cc_remote_space";
export const ENGINE_SPACES_KEY = "cc_remote_spaces_by_engine_v1";

export type EngineSpaces = Record<Engine, Space>;

interface ReadableStorage {
  getItem(key: string): string | null;
}

const normalizeSpace = (value: unknown): Space =>
  value === "work" ? "work" : "code";

export function readEngineSpaces(
  storage: ReadableStorage,
  currentEngine: Engine,
): EngineSpaces {
  const legacySpace = normalizeSpace(storage.getItem(LEGACY_SPACE_KEY));
  try {
    const saved = JSON.parse(storage.getItem(ENGINE_SPACES_KEY) ?? "null") as
      Partial<Record<Engine, unknown>> | null;
    if (saved) return {
      claude: normalizeSpace(saved.claude),
      codex: normalizeSpace(saved.codex),
      dsh: "code",
    };
  } catch { /* migrate malformed/legacy storage below */ }
  return {
    claude: currentEngine === "claude" ? legacySpace : "code",
    codex: currentEngine === "codex" ? legacySpace : "code",
    dsh: "code",
  };
}

export function rememberEngineSpace(
  spaces: EngineSpaces,
  engine: Engine,
  space: Space,
): EngineSpaces {
  return { ...spaces, [engine]: engine === "dsh" ? "code" : space };
}
