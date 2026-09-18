import { useCallback, useEffect, useLayoutEffect, useState } from "react";
import {
  LEGACY_THEME_KEY, THEME_STORAGE_KEY, readThemePreferences,
  resolveThemeMode, saveThemeChoice, themePalette,
  type ThemeChoice, type ThemeEngine,
} from "./themes";

function browserStorage(): Storage | undefined {
  try { return typeof window === "undefined" ? undefined : window.localStorage; }
  catch { return undefined; }
}

export function useTheme(engine: ThemeEngine) {
  const [preferences, setPreferences] = useState(() => readThemePreferences(browserStorage()));
  const [systemDark, setSystemDark] = useState(() => typeof window !== "undefined"
    && typeof window.matchMedia === "function" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  const choice = preferences[engine] ?? "system";
  const mode = resolveThemeMode(choice, systemDark);

  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const update = () => setSystemDark(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    const update = (event: StorageEvent) => {
      if (event.key === THEME_STORAGE_KEY || event.key === LEGACY_THEME_KEY || event.key === null) {
        setPreferences(readThemePreferences(browserStorage()));
      }
    };
    window.addEventListener("storage", update);
    return () => window.removeEventListener("storage", update);
  }, []);

  useLayoutEffect(() => {
    const root = document.documentElement;
    root.dataset.engine = engine;
    root.dataset.theme = mode;
    const palette = themePalette(choice);
    root.dataset.palette = palette?.id ?? "classic";
    root.style.colorScheme = mode;
    const background = getComputedStyle(root).getPropertyValue("--bg").trim();
    document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]').forEach(meta => {
      meta.content = background;
    });
  }, [engine, choice, mode]);

  const selectTheme = useCallback((next: ThemeChoice) => {
    // Write only on a user selection, never on mount or a system appearance change.
    setPreferences(current => saveThemeChoice(browserStorage(), current, engine, next));
  }, [engine]);
  return { choice, mode, selectTheme };
}
