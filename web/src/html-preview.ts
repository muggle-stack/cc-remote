import { installPreviewEscape } from "./preview-keyboard";
import { installViewerZoom } from "./viewer-zoom";

const STATIC_PREVIEW_CSP =
  "default-src 'none'; img-src data: blob:; style-src 'unsafe-inline'; "
  + "font-src data:; object-src 'none'; frame-src 'none'; form-action 'none'; "
  + "base-uri 'none'";
const INTERACTIVE_PREVIEW_CSP =
  STATIC_PREVIEW_CSP + "; script-src 'unsafe-inline'";

export type HtmlPreviewTheme = "light" | "dark";

const LIGHT_THEME = "color-scheme:light;--foreground:#111726;--background:#fff;"
  + "--card:#f7f9fc;--card-foreground:#111726;--primary:#2e6ff0;"
  + "--primary-foreground:#fff;--secondary:#eff3fa;--secondary-foreground:#33405a;"
  + "--muted:#eff3fa;--muted-foreground:#57627a;--accent:#e8efff;"
  + "--accent-foreground:#1b54d1;--destructive:#cf222e;--border:#d4ddec;"
  + "--input:#d4ddec;--ring:#2e6ff0;--viz-series-1:#2e6ff0;"
  + "--viz-series-2:#168a72;--viz-series-3:#c56b32;--viz-series-4:#8b5cc7;"
  + "--viz-series-5:#64748b";
const DARK_THEME = "color-scheme:dark;--foreground:#ecedf3;--background:#0d0e15;"
  + "--card:#16171f;--card-foreground:#ecedf3;--primary:#8590ff;"
  + "--primary-foreground:#0d0e15;--secondary:#1d1e28;--secondary-foreground:#c9cad5;"
  + "--muted:#1d1e28;--muted-foreground:#9a9bb0;--accent:#252746;"
  + "--accent-foreground:#a3acff;--destructive:#f0665d;--border:#31323f;"
  + "--input:#31323f;--ring:#8590ff;--viz-series-1:#8590ff;"
  + "--viz-series-2:#4cc9a4;--viz-series-3:#f2a65a;--viz-series-4:#c084fc;"
  + "--viz-series-5:#94a3b8";

const BASE_STYLE = "*{box-sizing:border-box}"
  + "body{margin:0;padding:18px;color:var(--foreground);background:var(--background);font:15px/1.65 "
  + "system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
  + "overflow-wrap:anywhere}img,svg,canvas,video{max-width:100%;height:auto}"
  + "table{display:block;max-width:100%;overflow-x:auto;border-collapse:collapse}"
  + "pre{max-width:100%;white-space:pre-wrap;overflow-wrap:anywhere}"
  + "a{color:var(--primary)}";

function previewThemeStyle(theme?: HtmlPreviewTheme): string {
  if (theme === "dark") return `:root{${DARK_THEME}}`;
  if (theme === "light") return `:root{${LIGHT_THEME}}`;
  return `:root{${LIGHT_THEME}}@media(prefers-color-scheme:dark){:root{${DARK_THEME}}}`;
}

function documentWithCsp(
  head: string,
  body: string,
  csp: string,
  theme?: HtmlPreviewTheme,
): string {
  const themeAttribute = theme ? ` data-theme="${theme}"` : "";
  return `<!doctype html><html${themeAttribute}><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${csp}"><meta name="viewport" content="width=device-width,initial-scale=1"><style>${previewThemeStyle(theme)}${BASE_STYLE}</style>${head}</head><body>${body}</body></html>`;
}

export function buildSandboxDocument(
  body: string,
  head = "",
  theme?: HtmlPreviewTheme,
): string {
  return documentWithCsp(head, body, STATIC_PREVIEW_CSP, theme);
}

export function buildInteractiveSandboxDocument(
  body: string,
  head = "",
  theme?: HtmlPreviewTheme,
): string {
  return documentWithCsp(`<script>(${installPreviewEscape.toString()})();(${installViewerZoom.toString()})();</script>${head}`,
    body, INTERACTIVE_PREVIEW_CSP, theme);
}
