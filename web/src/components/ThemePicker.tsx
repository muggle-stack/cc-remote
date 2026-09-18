import type { RefObject } from "react";
import { Icon } from "../icons";
import { THEME_PALETTES, themeLabel, type ThemeChoice, type ThemeEngine } from "../themes";
import { CenteredSheet } from "./CenteredSheet";
import "./ThemePicker.css";

export default function ThemePicker({ engine, choice, onSelect, onClose, returnFocusRef }: {
  engine: ThemeEngine;
  choice: ThemeChoice;
  onSelect: (choice: ThemeChoice) => void;
  onClose: () => void;
  returnFocusRef?: RefObject<HTMLElement | null>;
}) {
  return <CenteredSheet open label="主题" onClose={onClose} maxWidth={660} maxHeight={760} minimumHeight={660}
    className="theme-picker" returnFocusRef={returnFocusRef}>
    <div className="theme-picker-intro">
      <span>{({ claude: "Claude", codex: "Codex", dsh: "DSH" })[engine]}</span>
      <p>点击即切换，为当前引擎单独记住。</p>
    </div>
    <div className="sheet-scroll theme-picker-scroll">
      <div className="theme-basics" aria-label="基础主题">
        {(["system", "light", "dark"] as const).map(id => <button type="button" key={id}
          aria-pressed={choice === id} onClick={() => onSelect(id)}>
          <Icon name={id === "system" ? "devices" : id === "light" ? "sun" : "moon"} size={18} />
          <span>{themeLabel(id)}</span>
          {choice === id && <Icon name="check" size={14} />}
        </button>)}
      </div>
      <div className="theme-section-label">柔和配色 <span>低饱和 · 轻灰调</span></div>
      <div className="theme-grid" aria-label="柔和配色">
        {THEME_PALETTES.map(palette => <button type="button" key={palette.id}
          className="theme-card" aria-label={palette.name} aria-pressed={choice === palette.id}
          onClick={() => onSelect(palette.id)}>
          <span className="theme-miniature" aria-hidden="true" data-preview-palette={palette.id}>
            <span className="theme-mini-sidebar"><i /><b /><i /><i /></span>
            <span className="theme-mini-chat">
              <span className="theme-mini-head"><i /><b /></span>
              <span className="theme-mini-bubble" />
              <span className="theme-mini-lines"><i /><i /><i /></span>
              <span className="theme-mini-input"><i /><b /></span>
            </span>
            {choice === palette.id && <span className="theme-selected"><Icon name="check" size={13} /></span>}
          </span>
          <span className="theme-card-name">{palette.name}<span>{palette.mode === "light" ? "浅" : "深"}</span></span>
          <span className="theme-card-description">{palette.description}</span>
        </button>)}
      </div>
    </div>
    <footer className="theme-picker-footer">
      <span role="status">已选：{themeLabel(choice)}</span>
      <button type="button" onClick={onClose}>完成</button>
    </footer>
  </CenteredSheet>;
}
