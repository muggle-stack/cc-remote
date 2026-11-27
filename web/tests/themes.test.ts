import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { hexToHsv, hsvToHex } from "../src/color-picker.ts";
import {
  LEGACY_THEME_KEY, THEME_STORAGE_KEY, THEME_PALETTES,
  readThemePreferences, resolveThemeMode, saveThemeChoice,
} from "../src/themes.ts";

const values = new Map<string, string>();
const storage = { getItem: (key: string) => values.get(key) ?? null,
  setItem: (key: string, value: string) => { values.set(key, value); } };
assert.equal(readThemePreferences(storage).codex, "system");
values.set(LEGACY_THEME_KEY, "dark");
assert.deepEqual(readThemePreferences(storage), { claude: "dark", codex: "dark", dsh: "dark", antigravity: "dark" });
values.set(THEME_STORAGE_KEY, '{"codex":"moss","claude":"invalid","dsh":"lagoon"}');
assert.deepEqual(readThemePreferences(storage), { claude: "dark", codex: "moss", dsh: "lagoon", antigravity: "dark" });
const olderTab = readThemePreferences(storage);
saveThemeChoice(storage, olderTab, "dsh", "rose");
saveThemeChoice(storage, olderTab, "codex", "amber");
assert.deepEqual(readThemePreferences(storage), { claude: "dark", codex: "amber", dsh: "rose", antigravity: "dark" },
  "a stale tab must not overwrite another engine's latest preference");
assert.equal(values.get(LEGACY_THEME_KEY), "dark");
values.set(THEME_STORAGE_KEY, "{invalid");
assert.equal(readThemePreferences(storage).codex, "dark");
saveThemeChoice(storage, {}, "codex", "bordeaux");
assert.equal(readThemePreferences(storage).codex, "bordeaux", "malformed storage is recoverable");
const blocked = { getItem: () => { throw new Error("unavailable"); }, setItem: () => { throw new Error("unavailable"); } };
assert.deepEqual(readThemePreferences(blocked), {});
assert.deepEqual(saveThemeChoice(blocked, { claude: "rose" }, "codex", "moss"), { claude: "rose", codex: "moss" });
assert.equal(resolveThemeMode("system", true), "dark");
assert.equal(resolveThemeMode("system", false), "light");
assert.equal(resolveThemeMode("moss", true), "light");
assert.equal(resolveThemeMode("bordeaux", false), "dark");

function luminance(hex: string) {
  const rgb = hex.slice(1).match(/../g)!.map(value => {
    const channel = parseInt(value, 16) / 255;
    return channel <= .04045 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4;
  });
  return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
}
function contrast(a: string, b: string) {
  const x = luminance(a), y = luminance(b);
  return (Math.max(x, y) + .05) / (Math.min(x, y) + .05);
}
const css = readFileSync("src/themes.css", "utf8");
for (const palette of THEME_PALETTES) {
  const body = css.split(`[data-preview-palette="${palette.id}"] {`)[1]?.split("}")[0];
  assert.ok(body, `${palette.id}: palette has a stylesheet`);
  const colors = Object.fromEntries([...body.matchAll(/--([\w-]+):(#[\da-fA-F]+);/g)].map(match => [match[1], match[2]]));
  for (const surface of ["bg", "surface", "sidebar", "raised", "accent-weak"] as const) {
    for (const ink of ["text", "dim", "accent-ink", "emphasis-ink", "link-ink"] as const) {
      assert.ok(contrast(colors[ink], colors[surface]) >= 4.5,
        `${palette.id}: ${ink} remains readable on ${surface}`);
    }
  }
  assert.ok(contrast(colors["on-accent"], colors.accent) >= 4.5,
    `${palette.id}: muted primary buttons retain readable labels`);
  assert.ok(contrast(colors["inline-code-ink"], colors["inline-code-bg"]) >= 4.5,
    `${palette.id}: highlighted code remains readable on its own surface`);
  assert.ok(contrast(colors["selection-ink"], colors["selection-bg"]) >= 4.5,
    `${palette.id}: selected text remains readable`);
}
console.log("themes: legacy migration, per-engine persistence, cross-tab merge, system mode and text contrast passed");

// Custom appearance is independent for every engine and resolved mode.
const appearance = await import("../src/appearance.ts");
const { APPEARANCE_STORAGE_KEY, readAppearancePreferences, saveAppearanceChange, appearanceColors,
  appearanceVariables, normalizeHex, colorContrast } = appearance;
values.clear();
values.set(THEME_STORAGE_KEY, JSON.stringify({ claude: "rose", codex: "dark", dsh: "heritage" }));
const migrated = readAppearancePreferences(storage);
assert.deepEqual(migrated.claude, { mode: "light", light: { preset: "rose" }, dark: {} });
assert.deepEqual(migrated.codex, { mode: "dark", light: {}, dark: {} });
assert.deepEqual(migrated.dsh, { mode: "dark", light: {}, dark: { preset: "heritage" } });
assert.equal(migrated.codex?.sessionLayout, undefined, "legacy preferences leave the current compact layout as default");
assert.equal(values.has(APPEARANCE_STORAGE_KEY), false, "reading legacy preferences does not write storage");
let current = saveAppearanceChange(storage, migrated, "codex", { target: "light", patch: { accent: "#123456" } });
const stale = current;
current = saveAppearanceChange(storage, current, "codex", { target: "dark", patch: { accent: "#FEDCBA" } });
saveAppearanceChange(storage, current, "claude", { target: "light", patch: { font: "serif" } });
current = saveAppearanceChange(storage, stale, "codex", { target: "light", patch: { background: "#abcdef" } });
assert.deepEqual(current.codex?.light, { accent: "#123456", background: "#ABCDEF" });
assert.equal(current.codex?.dark.accent, "#FEDCBA", "other mode is not overwritten by a stale tab");
assert.equal(current.claude?.light.font, "serif", "other engine edits survive");
current = saveAppearanceChange(storage, current, "codex", { mode: "system" });
current = saveAppearanceChange(storage, current, "codex", { target: "light", replace: {} });
assert.deepEqual(current.codex?.light, {});
assert.equal(current.codex?.mode, "system", "reset does not change mode selection");
assert.equal(current.codex?.dark.accent, "#FEDCBA");
assert.equal(current.claude?.light.preset, "rose");
assert.equal(values.get(THEME_STORAGE_KEY), JSON.stringify({ claude: "rose", codex: "dark", dsh: "heritage" }));
const beforeLayout = current;
current = saveAppearanceChange(storage, current, "codex", { sessionLayout: "comfortable" });
saveAppearanceChange(storage, beforeLayout, "claude", { sessionLayout: "compact" });
current = saveAppearanceChange(storage, beforeLayout, "codex", { target: "dark", replace: {} });
assert.equal(current.codex?.sessionLayout, "comfortable", "a stale color reset must preserve the latest layout");
assert.equal(current.claude?.sessionLayout, "compact", "layout changes preserve other engines");
current = saveAppearanceChange(storage, current, "codex", { mode: "light" });
assert.equal(readAppearancePreferences(storage).codex?.sessionLayout, "comfortable", "layout survives mode changes and reloads");
assert.deepEqual(current.claude?.light, beforeLayout.claude?.light, "layout changes preserve existing colors and fonts");
values.set(APPEARANCE_STORAGE_KEY, JSON.stringify({ codex: { mode: "light", light: {
  accent: "red; background: url(https://example.com)", background: "#abc", foreground: "#12345G",
  preset: "__proto__", font: "constructor", evil: "ignored",
}, dark: { preset: "rose" }, sessionLayout: "invalid" } }));
assert.deepEqual(readAppearancePreferences(storage).codex, { mode: "light", light: { background: "#AABBCC" }, dark: {} });
values.set(APPEARANCE_STORAGE_KEY, "{");
assert.deepEqual(readAppearancePreferences(storage), migrated);
assert.equal(normalizeHex(" #aBc "), "#AABBCC");
assert.equal(normalizeHex("#12345"), undefined);
assert.equal(normalizeHex("#12345678"), undefined);
assert.equal(normalizeHex("var(--text)"), undefined);
const readOnly = { getItem: storage.getItem, setItem: () => { throw new Error("quota exceeded"); } };
values.set(APPEARANCE_STORAGE_KEY, JSON.stringify(migrated));
let inMemory = saveAppearanceChange(readOnly, migrated, "codex", { target: "light", patch: { accent: "#111111" } });
inMemory = saveAppearanceChange(readOnly, inMemory, "codex", { target: "light", patch: { background: "#EFEFEF" } });
assert.deepEqual(inMemory.codex?.light, { accent: "#111111", background: "#EFEFEF" });
inMemory = saveAppearanceChange(readOnly, inMemory, "codex", { sessionLayout: "comfortable" });
inMemory = saveAppearanceChange(readOnly, inMemory, "codex", { target: "dark", patch: { font: "serif" } });
assert.equal(inMemory.codex?.sessionLayout, "comfortable", "blocked storage still retains layout while editing colors");
assert.deepEqual(inMemory.codex?.light, { accent: "#111111", background: "#EFEFEF" });
assert.equal(readAppearancePreferences(blocked).codex?.mode, "system");
for (const engine of ["claude", "codex", "dsh"] as const) {
  for (const mode of ["light", "dark"] as const) {
    assert.deepEqual(appearanceVariables(engine, mode, {}), {}, "defaults keep their authored tokens");
    const colors = appearanceColors(engine, mode, {});
    const classic = readFileSync("src/index.css", "utf8");
    for (const color of Object.values(colors)) assert.ok(classic.includes(color));
    if (engine === "codex" && mode === "light") assert.ok(colorContrast(colors.accent, "#FFFFFF") >= 4.5);
    for (const accent of ["#FFFF00", "#000000", "#FFFFFF", "#F08080", "#6322FF"]) {
      const vars = appearanceVariables(engine, mode, { accent });
      for (const surface of ["--bg", "--surface", "--sidebar", "--raised", "--accent-weak"]) {
        for (const ink of ["--accent-ink", "--link-ink", "--emphasis-ink"]) {
          assert.ok(colorContrast(vars[ink], vars[surface]) >= 4.5, `${accent} ${ink} stays readable on ${surface}`);
        }
        assert.ok(colorContrast(vars["--dim"], vars[surface]) >= 4.5);
      }
      assert.ok(colorContrast(vars["--on-accent"], accent) >= 4.5, "button labels contrast with custom accent");
      for (const status of ["ok", "warn", "danger"]) {
        assert.ok(colorContrast(vars[`--${status}`], vars[`--${status}-weak`]) >= 4.5);
      }
    }
  }
}
for (const preset of THEME_PALETTES) {
  // A user can reverse the preset's luminance without changing its stored id.
  const background = preset.mode === "dark" ? "#FAFAFC" : "#1B1A17";
  const foreground = preset.mode === "dark" ? "#252334" : "#ECE9DF";
  const vars = appearanceVariables("codex", preset.mode, { preset: preset.id, background, foreground });
  assert.ok(colorContrast(vars["--emphasis-ink"], background) >= 4.5);
  assert.ok(colorContrast(vars["--link-ink"], background) >= 4.5);
  assert.ok(colorContrast(vars["--inline-code-ink"], vars["--inline-code-bg"]) >= 4.5);
  assert.ok(colorContrast(vars["--selection-ink"], vars["--selection-bg"]) >= 4.5);
}
assert.deepEqual(appearanceVariables("codex", "light", { font: "system" }), {});
assert.equal(appearanceVariables("codex", "light", { font: "serif" })["--mono"], undefined, "code keeps its monospace font");
console.log("appearance: migration, scoped edits/reset, storage failures, validation and derived contrast passed");

const { appearancePaletteLabel, isCustomPalette } = appearance;
assert.equal(appearancePaletteLabel("codex", "light", { font: "serif" }), "默认配色");
assert.equal(appearancePaletteLabel("codex", "dark", { preset: "lagoon", font: "sans" }), "深海青");
assert.equal(appearancePaletteLabel("codex", "dark", { preset: "lagoon", accent: "#8590FF" }), "自定义");
assert.equal(isCustomPalette("codex", "dark", { preset: "lagoon", accent: "#9AB1B3" }), false);
assert.deepEqual(appearanceVariables("codex", "dark", { preset: "lagoon", accent: "#9AB1B3" }), {},
  "returning to the preset colors restores its authored tokens");
values.set(APPEARANCE_STORAGE_KEY, JSON.stringify({ codex: { mode: "light", light: { custom: true, font: "serif" }, dark: {} } }));
assert.deepEqual(readAppearancePreferences(storage).codex?.light, { font: "serif" },
  "obsolete manual custom markers do not change the derived color label");

// The picker must preserve saved RGB values while moving between HEX and HSV.
for (const hex of ["#000000", "#FFFFFF", "#FF0000", "#00FF00", "#0000FF", "#6F69D2", "#FAFAFC", "#DB7C5E"]) {
  assert.equal(hsvToHex(hexToHsv(hex)), hex);
}
assert.equal(hsvToHex({ h: 360, s: 100, v: 100 }), "#FF0000");
assert.equal(hsvToHex({ h: 240, s: 0, v: 100 }), "#FFFFFF");
assert.equal(hsvToHex({ h: 240, s: 100, v: 0 }), "#000000");
console.log("appearance picker: RGB round trips and neutral colors passed");
