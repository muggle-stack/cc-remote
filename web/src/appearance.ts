import { readThemePreferences, themePalette, type PaletteId, type ThemeEngine, type ThemeMode } from "./themes.ts";

export type AppearanceMode = "system" | ThemeMode;
export type AppearanceFont = "system" | "sans" | "serif";
export type SessionLayout = "comfortable" | "compact";
export interface AppearanceColors { accent: string; background: string; foreground: string }
export interface AppearanceLook extends Partial<AppearanceColors> {
  preset?: PaletteId;
  font?: AppearanceFont;
}
export interface EngineAppearance {
  mode: AppearanceMode;
  light: AppearanceLook;
  dark: AppearanceLook;
  sessionLayout?: SessionLayout;
}
export type AppearancePreferences = Partial<Record<ThemeEngine, EngineAppearance>>;
export type AppearanceChange = { mode: AppearanceMode }
  | { sessionLayout: SessionLayout }
  | { target: ThemeMode; patch: AppearanceLook }
  | { target: ThemeMode; replace: AppearanceLook };
export const APPEARANCE_STORAGE_KEY = "cc_remote_appearance_v2";
const ENGINES = ["claude", "codex", "dsh", "antigravity"] as const;
export const APPEARANCE_FONTS: Record<AppearanceFont, { label: string; stack?: string }> = {
  system: { label: "系统默认" },
  sans: { label: "无衬线", stack: 'Arial,"Helvetica Neue","PingFang SC","Microsoft YaHei",sans-serif' },
  serif: { label: "衬线", stack: 'ui-serif,"Iowan Old Style","Songti SC","Noto Serif CJK SC",Georgia,serif' },
};
const CLASSIC: Record<ThemeEngine, Record<ThemeMode, AppearanceColors>> = {
  claude: {
    light: { accent: "#C15F3C", background: "#F7F5EF", foreground: "#201E18" },
    dark: { accent: "#DB7C5E", background: "#1B1A17", foreground: "#ECE9DF" },
  },
  dsh: {
    light: { accent: "#2E6FF0", background: "#FBFCFE", foreground: "#111726" },
    dark: { accent: "#6BA5FF", background: "#0D0E15", foreground: "#ECEDF3" },
  },
  antigravity: {
    light: { accent: "#395FA8", background: "#F8FAFF", foreground: "#202535" },
    dark: { accent: "#A8C7FA", background: "#111216", foreground: "#ECEDF3" },
  },
  codex: {
    light: { accent: "#6F69D2", background: "#FAFAFC", foreground: "#252334" },
    dark: { accent: "#8590FF", background: "#0D0E15", foreground: "#ECEDF3" },
  },
};
// Existing presets remain readable after migration; their full tokens stay in themes.css.
const PRESETS: Record<PaletteId, AppearanceColors> = {
  amber: { accent: "#AA9982", background: "#F6F4EF", foreground: "#302D28" },
  moss: { accent: "#96A28B", background: "#F2F4EF", foreground: "#2C312A" },
  rose: { accent: "#B69BA5", background: "#F6F1F2", foreground: "#332D30" },
  lagoon: { accent: "#9AB1B3", background: "#242B2D", foreground: "#E2E6E5" },
  bordeaux: { accent: "#B7A0AA", background: "#2D282B", foreground: "#E8E2E5" },
  heritage: { accent: "#AAB79F", background: "#272D28", foreground: "#E1E6DE" },
};
export function normalizeHex(value: unknown): string | undefined {
  if (typeof value !== "string") return;
  const hex = value.trim();
  if (/^#[\da-f]{6}$/i.test(hex)) return hex.toUpperCase();
  if (/^#[\da-f]{3}$/i.test(hex)) return `#${[...hex.slice(1)].map(c => c + c).join("").toUpperCase()}`;
}
function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function validMode(value: unknown): value is AppearanceMode {
  return value === "system" || value === "light" || value === "dark";
}
function sanitizeLook(value: unknown, mode: ThemeMode): AppearanceLook {
  const raw = record(value), look: AppearanceLook = {};
  if (typeof raw.preset === "string" && Object.hasOwn(PRESETS, raw.preset)
      && themePalette(raw.preset as PaletteId)?.mode === mode) look.preset = raw.preset as PaletteId;
  for (const key of ["accent", "background", "foreground"] as const) {
    const color = normalizeHex(raw[key]);
    if (color) look[key] = color;
  }
  if (typeof raw.font === "string" && Object.hasOwn(APPEARANCE_FONTS, raw.font)) look.font = raw.font as AppearanceFont;
  return look;
}
export function readAppearancePreferences(storage?: Pick<Storage, "getItem">): AppearancePreferences {
  const legacy = readThemePreferences(storage);
  let saved: Record<string, unknown> = {};
  try { saved = record(JSON.parse(storage?.getItem(APPEARANCE_STORAGE_KEY) ?? "null")); }
  catch { /* Keep the previous theme if the new storage is unavailable or damaged. */ }
  const next: AppearancePreferences = {};
  for (const engine of ENGINES) {
    const choice = legacy[engine] ?? "system", palette = themePalette(choice);
    const fallback: EngineAppearance = { mode: palette?.mode ?? (validMode(choice) ? choice : "system"), light: {}, dark: {} };
    if (palette) fallback[palette.mode] = { preset: palette.id };
    const raw = record(saved[engine]);
    next[engine] = validMode(raw.mode)
      ? { mode: raw.mode, light: sanitizeLook(raw.light, "light"), dark: sanitizeLook(raw.dark, "dark") }
      : fallback;
    if (raw.sessionLayout === "comfortable" || raw.sessionLayout === "compact") {
      next[engine].sessionLayout = raw.sessionLayout;
    }
  }
  return next;
}
/** Write only the field/mode being edited, merging other tabs' latest values. */
export function saveAppearanceChange(storage: Pick<Storage, "getItem" | "setItem"> | undefined,
  current: AppearancePreferences, engine: ThemeEngine, change: AppearanceChange): AppearancePreferences {
  let latest = current;
  try {
    if (storage?.getItem(APPEARANCE_STORAGE_KEY) != null) latest = readAppearancePreferences(storage);
  } catch { /* Fall back to the in-memory selection. */ }
  const apply = (source: AppearancePreferences): AppearancePreferences => {
    const previous = source[engine] ?? { mode: "system", light: {}, dark: {} };
    const edited: EngineAppearance = "mode" in change ? { ...previous, mode: change.mode }
      : "sessionLayout" in change ? { ...previous, sessionLayout: change.sessionLayout }
      : { ...previous, [change.target]: sanitizeLook("replace" in change ? change.replace
        : { ...previous[change.target], ...change.patch }, change.target) };
    return { ...source, [engine]: edited };
  };
  const next = apply(latest);
  try { storage?.setItem(APPEARANCE_STORAGE_KEY, JSON.stringify(next)); }
  catch { return apply(current); /* Preserve earlier in-memory edits if writes are denied. */ }
  return next;
}
export function appearanceColors(engine: ThemeEngine, mode: ThemeMode, look: AppearanceLook): AppearanceColors {
  const base = look.preset ? PRESETS[look.preset] : CLASSIC[engine][mode];
  return { accent: look.accent ?? base.accent, background: look.background ?? base.background,
    foreground: look.foreground ?? base.foreground };
}
export function isCustomPalette(engine: ThemeEngine, mode: ThemeMode, look: AppearanceLook): boolean {
  const base = appearanceColors(engine, mode, { preset: look.preset });
  return (["accent", "background", "foreground"] as const).some(key => look[key] && look[key] !== base[key]);
}
export function appearancePaletteLabel(engine: ThemeEngine, mode: ThemeMode, look: AppearanceLook): string {
  return isCustomPalette(engine, mode, look) ? "自定义"
    : (look.preset && themePalette(look.preset)?.name) || "默认配色";
}
function rgb(hex: string): number[] {
  return [1, 3, 5].map(start => parseInt(hex.slice(start, start + 2), 16));
}
function mix(a: string, b: string, weight: number): string {
  const other = rgb(b);
  return `#${rgb(a).map((value, i) => Math.round(value + (other[i] - value) * weight).toString(16).padStart(2, "0")).join("").toUpperCase()}`;
}
export function colorContrast(a: string, b: string): number {
  const luminance = (hex: string) => rgb(hex).reduce((sum, value, i) => {
    const c = value / 255;
    return sum + (c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4) * [.2126, .7152, .0722][i];
  }, 0);
  const x = luminance(a), y = luminance(b);
  return (Math.max(x, y) + .05) / (Math.min(x, y) + .05);
}
function readable(color: string, surfaces: string[]): string {
  const contrast = (ink: string) => Math.min(...surfaces.map(surface => colorContrast(ink, surface)));
  const target = contrast("#000000") >= contrast("#FFFFFF") ? "#000000" : "#FFFFFF";
  for (let step = 0; step <= 20; step++) {
    const ink = mix(color, target, step / 20);
    if (contrast(ink) >= 4.5) return ink;
  }
  return target;
}
/** Only custom colors produce overrides. Untouched defaults keep their authored tokens. */
export function appearanceVariables(engine: ThemeEngine, mode: ThemeMode, look: AppearanceLook): Record<string, string> {
  const variables: Record<string, string> = {};
  if (isCustomPalette(engine, mode, look)) {
    const { accent, background: bg, foreground: text } = appearanceColors(engine, mode, look);
    const lightBackground = colorContrast("#000000", bg) > colorContrast("#FFFFFF", bg);
    const tone = mix(bg, accent, .025);
    const surface = mix(tone, "#FFFFFF", lightBackground ? .7 : .045);
    const sidebar = mix(tone, "#000000", .025);
    const raised = mix(tone, lightBackground ? "#000000" : "#FFFFFF", .045);
    const weak = mix(bg, accent, lightBackground ? .12 : .17);
    const surfaces = [bg, surface, sidebar, raised, weak];
    const ink = readable(accent, surfaces);
    Object.assign(variables, {
      "--bg": bg, "--surface": surface, "--sidebar": sidebar, "--raised": raised,
      "--text": text, "--dim": readable(mix(bg, text, .68), surfaces), "--faint": mix(bg, text, .52),
      "--border": mix(bg, text, .13), "--border-strong": mix(bg, text, .21), "--divider": mix(bg, text, .09),
      "--accent": accent, "--accent-ink": ink, "--accent-text": ink,
      "--accent-weak": weak, "--accent-line": mix(bg, accent, .34),
      "--on-accent": readable("#FFFFFF", [accent]), "--inline-code-ink": ink,
      // Custom edits must replace the preset's content colors as well as its
      // surfaces, even when the user switches to the opposite luminance.
      "--emphasis-ink": readable(mix(accent, text, .28), surfaces),
      "--link-ink": ink, "--inline-code-bg": weak, "--inline-code-line": mix(bg, accent, .34),
      "--inline-code-padding": ".08em .32em",
      "--selection-bg": accent, "--selection-ink": readable(text, [accent]),
    });
    for (const [name, color] of [["ok", "#3F8B6E"], ["warn", "#9A6700"], ["danger", "#BE4A3A"]]) {
      const tint = mix(bg, color, .12);
      variables[`--${name}-weak`] = tint;
      variables[`--${name}`] = readable(color, [...surfaces, tint]);
    }
  }
  const font = APPEARANCE_FONTS[look.font ?? "system"].stack;
  if (font) { variables["--sans"] = font; variables["--serif"] = font; }
  return variables;
}
