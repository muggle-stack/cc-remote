import { EngineIcon, Icon } from "../icons";
import type { DshItem } from "../protocol";

import { bundlePresentation } from "../dsh-bundle-presentation";
import type { ReactNode } from "react";
import { useState } from "react";

export function DshBundleRow({ item, busy, onToggle, onRemove, onInstall, children, detail }: {
  item: DshItem; busy: boolean; onToggle: () => void; onRemove: () => void; onInstall: () => void; children?: ReactNode; detail?: ReactNode;
}) {
  const [expanded, setExpanded] = useState(false);
  const display = bundlePresentation(item);
  const core = item.data?.readOnlyReason === "management-required";
  const enabled = item.state === "enabled";
  return <>
    <div className="dsh-bundle-summary">
      <span className="dsh-bundle-icon" aria-hidden="true">{item.id === "@deepseek-ai/dsh-base" ? <EngineIcon engine="dsh" size={25} /> : <Icon name={display.icon} size={23} />}</span>
      <div className="dsh-bundle-copy"><strong>{display.title}</strong><small>{String(item.data?.version ?? "")}{core ? " · 内置" : item.data?.availability === "installation" ? " · 可选扩展" : ""}</small></div>
      {enabled ? <span className="dsh-bundle-state"><i />已启用</span>
        : item.controllable ? <button disabled={busy} className="dsh-bundle-enable" onClick={onToggle}>启用</button>
        : item.state === "not-installed" && !!item.data?.installTarget ? <button disabled={busy} className="dsh-bundle-enable" onClick={onInstall}>安装</button>
        : <small className="dsh-management-meta">{({ installed: "已安装", provided: "内置", "update-available": "有更新", "different-version": "版本不同", "not-installed": "暂不可安装" } as Record<string, string>)[item.state] ?? "未启用"}</small>}
      {core && <span className="dsh-bundle-lock" title="核心组件不可停用或卸载"><Icon name="lock" size={16} /></span>}
    </div>
    {display.detail && <p className="dsh-bundle-description">{display.detail}</p>}
    <details className="dsh-bundle-details" onToggle={event => setExpanded(event.currentTarget.open)}><summary>详情{item.state === "update-available" ? " · 有新版本" : ""}</summary>
      {expanded && detail}
      <p className="dsh-management-meta">{item.id}</p>
      {core && <p>随 DSH 更新，为保留管理功能不可停用或卸载。</p>}
      {item.state === "update-available" && <p>已安装 {String(item.data?.installedVersion)}，可检查兼容版本。</p>}
      {item.data?.updatable === false && !core && <p>此插件的版本由 DSH 安装配置管理。</p>}
      {item.data?.readOnlyReason === "unaddressable" && <p>原生运行时未提供此插件的管理入口。</p>}
      {item.detail !== display.detail && item.detail && <p>{item.detail}</p>}
      <div className="dsh-management-actions">
        {item.controllable && enabled && <button disabled={busy} onClick={onToggle}>停用</button>}
        {item.controllable && item.data?.removable === true && <button disabled={busy} onClick={onRemove}>卸载</button>}
      </div>
      {children}
    </details>
  </>;
}
