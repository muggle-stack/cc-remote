import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  LEGACY_THEME_KEY, THEME_STORAGE_KEY, THEME_PALETTES,
  readThemePreferences, resolveThemeMode, saveThemeChoice,
} from "../src/themes.ts";

const values = new Map<string, string>();
const storage = { getItem: (key: string) => values.get(key) ?? null,
  setItem: (key: string, value: string) => { values.set(key, value); } };
assert.equal(readThemePreferences(storage).codex, "system");
values.set(LEGACY_THEME_KEY, "dark");
assert.deepEqual(readThemePreferences(storage), { claude: "dark", codex: "dark", dsh: "dark" });
values.set(THEME_STORAGE_KEY, '{"codex":"moss","claude":"invalid","dsh":"lagoon"}');
assert.deepEqual(readThemePreferences(storage), { claude: "dark", codex: "moss", dsh: "lagoon" });
const olderTab = readThemePreferences(storage);
saveThemeChoice(storage, olderTab, "dsh", "rose");
saveThemeChoice(storage, olderTab, "codex", "amber");
assert.deepEqual(readThemePreferences(storage), { claude: "dark", codex: "amber", dsh: "rose" },
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
    for (const ink of ["text", "dim", "accent-ink"] as const) {
      assert.ok(contrast(colors[ink], colors[surface]) >= 4.5,
        `${palette.id}: ${ink} remains readable on ${surface}`);
    }
  }
  assert.ok(contrast(colors["on-accent"], colors.accent) >= 4.5,
    `${palette.id}: muted primary buttons retain readable labels`);
}
console.log("themes: legacy migration, per-engine persistence, cross-tab merge, system mode and text contrast passed");
