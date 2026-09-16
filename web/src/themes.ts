/** Appearance is local to each engine. Only the resolved light/dark mode is
 * passed to diff/preview renderers; palette names never enter the protocol. */
export type ThemeEngine = "claude" | "codex" | "dsh";
export type ThemeMode = "light" | "dark";
export type PaletteId = "amber" | "moss" | "rose" | "lagoon" | "bordeaux" | "heritage";
export type ThemeChoice = "system" | ThemeMode | PaletteId;
export type ThemePreferences = Partial<Record<ThemeEngine, ThemeChoice>>;
export const THEME_STORAGE_KEY = "cc_remote_themes_v1";
export const LEGACY_THEME_KEY = "cc_remote_theme";

export interface ThemePalette {
  id: PaletteId;
  name: string;
  description: string;
  mode: ThemeMode;
}

export const THEME_PALETTES: readonly ThemePalette[] = [
  {
    id: "amber", name: "奶油琥珀", description: "燕麦白 · 暖灰褐", mode: "light",
  },
  {
    id: "moss", name: "森林苔绿", description: "雾白 · 鼠尾草灰", mode: "light",
  },
  {
    id: "rose", name: "玫瑰雾", description: "柔白 · 玫瑰灰", mode: "light",
  },
  {
    id: "lagoon", name: "深海青", description: "炭灰 · 雾青", mode: "dark",
  },
  {
    id: "bordeaux", name: "酒红夜色", description: "暖炭灰 · 烟粉", mode: "dark",
  },
  {
    id: "heritage", name: "复古终端绿", description: "石墨灰 · 灰绿", mode: "dark",
  },
];

export function themePalette(choice: ThemeChoice): ThemePalette | undefined {
  return THEME_PALETTES.find(palette => palette.id === choice);
}

export function isThemeChoice(value: unknown): value is ThemeChoice {
  return value === "system" || value === "light" || value === "dark"
    || THEME_PALETTES.some(palette => palette.id === value);
}

export function themeLabel(choice: ThemeChoice): string {
  return themePalette(choice)?.name ?? (choice === "system" ? "跟随系统" : choice === "dark" ? "经典深色" : "经典浅色");
}

export function resolveThemeMode(choice: ThemeChoice, systemDark: boolean): ThemeMode {
  return themePalette(choice)?.mode ?? (choice === "dark" || (choice === "system" && systemDark) ? "dark" : "light");
}

export function readThemePreferences(storage?: Pick<Storage, "getItem">): ThemePreferences {
  try {
    const legacy = storage?.getItem(LEGACY_THEME_KEY);
    const fallback: ThemeChoice = legacy === "light" || legacy === "dark" ? legacy : "system";
    let saved: unknown;
    try { saved = JSON.parse(storage?.getItem(THEME_STORAGE_KEY) ?? "null"); }
    catch { /* Fall back to the previous light/dark preference. */ }
    const preferences: ThemePreferences = {};
    for (const engine of ["claude", "codex", "dsh"] as const) {
      const value = saved && typeof saved === "object" ? (saved as Record<string, unknown>)[engine] : undefined;
      preferences[engine] = isThemeChoice(value) ? value : fallback;
    }
    return preferences;
  } catch { return {}; /* Private browsing may disable storage entirely. */ }
}

/** Merge the latest persisted map so another tab's engine choice is retained. */
export function saveThemeChoice(storage: Pick<Storage, "getItem" | "setItem"> | undefined,
  current: ThemePreferences, engine: ThemeEngine, choice: ThemeChoice): ThemePreferences {
  let next = { ...current, [engine]: choice };
  try {
    if (storage) {
      next = { ...readThemePreferences(storage), ...current, [engine]: choice };
      // Only the selected engine is ours to overwrite across tabs.
      let saved: Partial<Record<ThemeEngine, unknown>> | null = null;
      try { saved = JSON.parse(storage.getItem(THEME_STORAGE_KEY) ?? "null"); }
      catch { /* Replace malformed preferences with this valid selection. */ }
      for (const other of ["claude", "codex", "dsh"] as const) {
        const value = saved?.[other];
        if (other !== engine && isThemeChoice(value)) next[other] = value;
      }
      storage.setItem(THEME_STORAGE_KEY, JSON.stringify(next));
    }
  } catch { /* Selection remains usable in memory when storage is unavailable. */ }
  return next;
}
