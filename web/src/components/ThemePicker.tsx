import { type RefObject } from "react";
import { EngineIcon } from "../icons";
import type { ThemeEngine } from "../themes";
import { appearanceColors, colorContrast, isCustomPalette, type AppearanceColors } from "../appearance";
import type { AppearanceController } from "../use-theme";
import { CenteredSheet } from "./CenteredSheet";
import { AppearanceColor } from "./AppearanceColor";
import { AppearanceFont } from "./AppearanceFont";
import { AppearancePalette } from "./AppearancePalette";
import { AppearanceSessionLayout } from "./AppearanceSessionLayout";
import "./ThemePicker.css";

export default function ThemePicker({ engine, appearance, onClose, returnFocusRef, boldText, onBoldText }: {
  engine: ThemeEngine;
  appearance: AppearanceController;
  onClose: () => void;
  returnFocusRef?: RefObject<HTMLElement | null>;
  boldText?: boolean;
  onBoldText?: (enabled: boolean) => void;
}) {
  const { choice, mode, look, colors, sessionLayout, selectTheme, updateLook, replaceLook, selectSessionLayout } = appearance;
  const custom = isCustomPalette(engine, mode, look);
  const baseColors = appearanceColors(engine, mode, { preset: look.preset });
  const lowContrast = colorContrast(colors.background, colors.foreground) < 4.5;
  const modeLabel = mode === "light" ? "浅色" : "深色";
  return <CenteredSheet open label="外观" onClose={onClose} maxWidth={engine === "dsh" ? 520 : 620} maxHeight={940} minimumHeight={engine === "dsh" ? 700 : 600}
    className={`theme-picker${engine === "dsh" ? " theme-picker-dsh" : ""}`} returnFocusRef={returnFocusRef}>
    <div className="theme-picker-intro">
      <span><EngineIcon engine={engine} size={17} />{({ claude: "Claude", codex: "Codex", dsh: "DSH" })[engine]}</span>
      <p>配色与字体按当前引擎保存</p>
    </div>
    <div className="sheet-scroll theme-picker-scroll">
      <div className="appearance-mode-section">
        <span className="appearance-label">模式</span>
        <div className="theme-basics" aria-label="外观模式">
          {(["system", "light", "dark"] as const).map(id => <button type="button" key={id}
            aria-label={id === "system" ? "跟随系统" : id === "light" ? "浅色" : "深色"}
            aria-pressed={choice === id} onClick={() => selectTheme(id)}>
            <span className={`appearance-miniature ${id}`} aria-hidden="true">
              <span className="appearance-mini-sidebar"><i /><i /><i /></span>
              <span className="appearance-mini-chat"><b /><i /><i /><span><b /></span></span>
            </span>
            <span>{id === "system" ? "跟随系统" : id === "light" ? "浅色" : "深色"}</span>
          </button>)}
        </div>
      </div>
      <p className="appearance-mode-help">正在调整{modeLabel}外观 · 浅色与深色分别记住配色</p>
      <div className="appearance-fields">
        <AppearancePalette key={`palette:${engine}:${mode}`} engine={engine} mode={mode} look={look}
          onChange={preset => updateLook({ preset, accent: undefined, background: undefined, foreground: undefined })} />
        {([['accent', '强调色'], ['background', '背景色'], ['foreground', '文字色']] as const).map(([key, label]) =>
          <AppearanceColor key={`${engine}:${mode}:${look.preset ?? "classic"}:${key}`} label={label} value={colors[key]}
            onChange={value => {
              if (value !== colors[key]) updateLook({ [key]: value === baseColors[key] ? undefined : value } as Partial<AppearanceColors>);
            }} onReset={() => updateLook({ [key]: undefined })} />)}
        <AppearanceFont key={`font:${engine}:${mode}`} value={look.font ?? "system"}
          onChange={font => updateLook({ font })} />
      </div>
      <p className="appearance-help">{custom ? "手动调色后，自动标记为自定义。" : "点击色块选色，也可输入 HEX 色值。调整即时预览。"}</p>
      {lowContrast && <p className="appearance-contrast" role="status">文字与背景较接近，建议拉开明暗差距，便于阅读。</p>}
      <AppearanceSessionLayout value={sessionLayout} onChange={selectSessionLayout} />
      {onBoldText && <section className="appearance-reading-section" aria-label="阅读设置">
        <h3>阅读设置 <small>适用于所有引擎</small></h3>
        <button type="button" className="header-menu-item" role="switch" aria-label="加粗字体"
          aria-checked={!!boldText} onClick={() => onBoldText(!boldText)}>
          <span><b>加粗字体</b><small>整页文字加粗，更易阅读</small></span>
          <span className="header-menu-switch" aria-hidden="true" />
        </button>
      </section>}
    </div>
    <footer className="theme-picker-footer">
      <button type="button" className="appearance-reset" onClick={() => replaceLook({})}
        title={`恢复当前引擎的${modeLabel}默认颜色和字体`}>恢复默认</button>
      <span>仅重置当前{modeLabel}</span>
      <button type="button" className="appearance-done" onClick={onClose}>完成</button>
    </footer>
  </CenteredSheet>;
}
