import "./SpeedPopover.css";
import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";
import { createPortal } from "react-dom";
import { normalizeSpeed, speedLabel, speedsFor } from "../codex-speed";
import { Icon } from "../icons";
import type { SpeedPickerProps } from "./SpeedPicker";

export default function SpeedPopover(p: SpeedPickerProps & {
  trigger: RefObject<HTMLButtonElement | null>; onClose: () => void;
}) {
  const { onClose, trigger } = p;
  const [position, setPosition] = useState({ top: 0, left: 0, width: 280, maxHeight: 420 });
  const popover = useRef<HTMLDivElement>(null);
  const { known, options, modelName } = speedsFor(p.model, p.catalog);
  const selected = p.value == null ? null : normalizeSpeed(p.value);
  const available = options.some((option) => option.id === selected);
  const label = speedLabel(selected, options);

  useLayoutEffect(() => {
    const positionPopup = () => {
      const rect = trigger.current?.getBoundingClientRect();
      if (!rect) return;
      const viewport = window.visualViewport;
      const leftEdge = viewport?.offsetLeft ?? 0;
      const topEdge = viewport?.offsetTop ?? 0;
      const width = Math.min(292, (viewport?.width ?? window.innerWidth) - 24);
      const bottomEdge = topEdge + (viewport?.height ?? window.innerHeight);
      const height = Math.min(popover.current?.scrollHeight ?? 260, bottomEdge - topEdge - 24);
      setPosition({
        width, maxHeight: bottomEdge - topEdge - 24,
        left: Math.max(leftEdge + 12, Math.min(rect.right - width,
          leftEdge + (viewport?.width ?? window.innerWidth) - width - 12)),
        top: Math.max(topEdge + 12, Math.min(rect.top - height - 10, bottomEdge - height - 12)),
      });
    };
    positionPopup();
    window.addEventListener("resize", positionPopup);
    window.addEventListener("scroll", positionPopup, true);
    window.visualViewport?.addEventListener("resize", positionPopup);
    window.visualViewport?.addEventListener("scroll", positionPopup);
    return () => {
      window.removeEventListener("resize", positionPopup);
      window.removeEventListener("scroll", positionPopup, true);
      window.visualViewport?.removeEventListener("resize", positionPopup);
      window.visualViewport?.removeEventListener("scroll", positionPopup);
    };
  }, [known, options.length, modelName, trigger]);

  useEffect(() => {
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !popover.current?.contains(event.target)
          && !trigger.current?.contains(event.target)) onClose();
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault(); onClose(); trigger.current?.focus();
      }
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    (popover.current?.querySelector<HTMLButtonElement>("[aria-checked=true]")
      ?? popover.current?.querySelector<HTMLButtonElement>("[role=menuitemradio]"))?.focus();
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [onClose, trigger]);

  return createPortal(<div ref={popover} className="speed-pop" style={position}
      role="menu" aria-label={`${modelName} 的速度`}
      onKeyDown={(event) => {
        if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const buttons = Array.from(popover.current?.querySelectorAll<HTMLButtonElement>("[role=menuitemradio]") ?? []);
        const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
        const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
          : (index + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
        buttons[next]?.focus();
      }}>
      <header><strong>速度</strong><span>{modelName}</span></header>
      {options.map((option) => <button type="button" key={option.id}
        role="menuitemradio" aria-checked={selected === option.id}
        onClick={() => {
          if (p.disabled) return;
          p.onChange(option.id); onClose(); trigger.current?.focus();
        }}>
        <span><b>{option.name}</b><small>{option.description}</small></span>
        {selected === option.id && <Icon name="check" size={16} />}
      </button>)}
      {!known && <p role="status">速度列表暂未读取，可先使用标准速度。</p>}
      {known && selected && !available && <p role="status">当前为 {label}，该模型已不再提供此档位，请重新选择。</p>}
      <footer>{p.newSession ? "首条消息生效" : "下条消息生效"}</footer>
    </div>, document.body);
}
