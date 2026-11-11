import { useEffect, useMemo, useState } from "react";
import { Icon } from "../icons";
import { deviceVersionNotice, type DeviceCompatibility } from "../device-version";
import { PanelCloseButton, PanelRowMenu, RefreshButton } from "./PanelActions";

export interface RemoteDevice {
  machine_id: string;
  label: string;
  platform: string;
  hostname: string;
  created_at: number | null;
  last_seen: number | null;
  online: boolean;
  managed: boolean;
  compatibility?: DeviceCompatibility;
}

export interface PairingState {
  enabled: boolean;
  expires_at: number | null;
}

interface Props {
  open: boolean;
  currentId: string;
  devices: RemoteDevice[];
  pairing: PairingState;
  onDevices: (devices: RemoteDevice[], pairing: PairingState) => void;
  onSelect: (machineId: string) => void;
  onClose: () => void;
}

async function responseError(response: Response): Promise<string> {
  try {
    const payload = await response.json();
    return typeof payload?.error === "string" ? payload.error : `HTTP ${response.status}`;
  } catch {
    return `HTTP ${response.status}`;
  }
}

export function DeviceSheet({
  open, currentId, devices, pairing, onDevices, onSelect, onClose,
}: Props) {
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pairCode, setPairCode] = useState<string | null>(null);
  const [pairExpires, setPairExpires] = useState<number | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [label, setLabel] = useState("");
  const [now, setNow] = useState(Date.now);
  const [copied, setCopied] = useState<"command" | "code" | null>(null);

  const refresh = async () => {
    setLoading(true);
    setRefreshing(true);
    try {
      const response = await fetch("/api/devices", {
        credentials: "same-origin", cache: "no-store",
      });
      if (!response.ok) throw new Error(await responseError(response));
      const payload = await response.json();
      if (!Array.isArray(payload?.devices)) throw new Error("invalid_device_list");
      const nextPairing = payload.pairing ?? { enabled: false, expires_at: null };
      onDevices(payload.devices, nextPairing);
      if (!nextPairing.enabled) {
        setPairCode(null);
        setPairExpires(null);
      }
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "device_list_failed");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  };

  useEffect(() => {
    if (open) void refresh();
    else { setEditing(null); setError(null); }
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!open || (!pairing.enabled && !pairCode)) return;
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => window.clearInterval(timer);
  }, [open, pairing.enabled, pairCode]); // eslint-disable-line react-hooks/exhaustive-deps

  const expiresAt = pairExpires ?? pairing.expires_at;
  useEffect(() => {
    if (!open || !expiresAt) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [open, expiresAt]);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(null), 1500);
    return () => window.clearTimeout(timer);
  }, [copied]);
  const pairCommand = useMemo(() => pairCode
    ? `python -m cc_remote.device pair ${window.location.origin} ${pairCode}`
    : "", [pairCode]);
  const remaining = expiresAt == null ? null : Math.max(0, Math.ceil((expiresAt * 1000 - now) / 1000));
  const copy = async (kind: "command" | "code") => {
    try {
      await navigator.clipboard.writeText(kind === "command" ? pairCommand : pairCode ?? "");
      setCopied(kind);
    } catch {
      setError("复制失败，请选中命令手动复制。");
    }
  };

  if (!open) return null;

  const startPairing = async () => {
    setLoading(true);
    try {
      const response = await fetch("/api/devices/pairing", {
        method: "POST", credentials: "same-origin",
      });
      if (!response.ok) throw new Error(await responseError(response));
      const payload = await response.json();
      setPairCode(payload.code);
      setPairExpires(payload.expires_at);
      onDevices(devices, { enabled: true, expires_at: payload.expires_at });
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "pairing_failed");
    } finally {
      setLoading(false);
    }
  };

  const stopPairing = async () => {
    setLoading(true);
    try {
      const response = await fetch("/api/devices/pairing", {
        method: "DELETE", credentials: "same-origin",
      });
      if (!response.ok) throw new Error(await responseError(response));
      setPairCode(null);
      setPairExpires(null);
      onDevices(devices, { enabled: false, expires_at: null });
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "pairing_close_failed");
    } finally {
      setLoading(false);
    }
  };

  const saveLabel = async (device: RemoteDevice) => {
    const next = label.trim();
    if (!next || next.length > 64) return;
    setLoading(true);
    try {
      const response = await fetch(`/api/devices/${encodeURIComponent(device.machine_id)}`, {
        method: "PATCH", credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label: next }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      setEditing(null);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "rename_failed");
      setLoading(false);
    }
  };

  const revoke = async (device: RemoteDevice) => {
    if (!window.confirm(`移除设备「${device.label}」？该设备会立即断开，需要重新配对才能接入。`)) return;
    setLoading(true);
    try {
      const response = await fetch(`/api/devices/${encodeURIComponent(device.machine_id)}`, {
        method: "DELETE", credentials: "same-origin",
      });
      if (!response.ok) throw new Error(await responseError(response));
      if (currentId === device.machine_id) {
        const replacement = devices.find((item) => item.machine_id !== device.machine_id);
        if (replacement) onSelect(replacement.machine_id);
      }
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "revoke_failed");
      setLoading(false);
    }
  };

  return <>
    <div className="scrim show" onClick={onClose} />
    <section className="device-sheet" role="dialog" aria-modal="true" aria-label="设备中心">
      <header>
        <div><b><Icon name="devices" size={20} />设备中心</b><small>连接运行 Claude / Codex 的机器</small></div>
        <div className="device-head-actions">
          <RefreshButton busy={refreshing} failed={!!error} disabled={loading}
            onClick={() => void refresh()} label="刷新设备" />
          <PanelCloseButton onClick={onClose} label="关闭设备中心" />
        </div>
      </header>
      <div className="device-body">
        {error && <div className="device-error">{error}</div>}
        <section className="device-pairing">
          <div>
            <b>添加设备</b>
            <p>{pairCode ? "在目标设备的终端运行以下命令" : "开启配对，在 Mac 或 Linux 上连接新设备"}</p>
          </div>
          {!pairing.enabled && !pairCode
            ? <button disabled={loading} onClick={() => void startPairing()}><Icon name="plus" size={14} />添加设备</button>
            : <button className="subtle" disabled={loading} onClick={() => void stopPairing()}>结束配对</button>}
          {pairCode && <div className="device-code">
            <div className="device-command">
              <code>{pairCommand}</code>
              <button disabled={remaining === 0} onClick={() => void copy("command")}
                aria-label={copied === "command" ? "命令已复制" : "复制命令"}>
                <Icon name={copied === "command" ? "check" : "copy"} size={15} /><span>复制命令</span>
              </button>
            </div>
            <div className="device-pairing-meta">
              <span>配对码 <code>{pairCode}</code><button className="iconbtn panel-action"
                disabled={remaining === 0} aria-label={copied === "code" ? "配对码已复制" : "复制配对码"}
                onClick={() => void copy("code")}><Icon name={copied === "code" ? "check" : "copy"} size={14} /></button></span>
              {remaining !== null && <small title={`有效期至 ${new Date(expiresAt! * 1000).toLocaleTimeString()}`}>
                <Icon name="clock" size={14} />{remaining === 0 ? "已过期" : `剩余 ${String(Math.floor(remaining / 60)).padStart(2, "0")}:${String(remaining % 60).padStart(2, "0")}`}
              </small>}
            </div>
            <p className="device-pairing-status"><i aria-hidden="true" />{remaining === 0 ? "配对码已过期，请重新开启配对" : "等待设备连接…"}</p>
          </div>}
          {!pairCode && pairing.enabled && <p className="device-pairing-live">
            配对窗口已开启。为避免刷新后泄露，旧配对码不会再次显示；可关闭后重新生成。
          </p>}
        </section>
        <section className="device-list">
          <h3>我的设备 <span>{devices.length}</span></h3>
          {!devices.length && !loading && <div className="device-empty">尚未发现设备</div>}
          {devices.map((device) => <article key={device.machine_id}
            className={device.machine_id === currentId ? "current" : ""}>
            <div className="device-row"><button className="device-main" aria-current={device.machine_id === currentId ? "true" : undefined}
              onClick={() => onSelect(device.machine_id)}>
              <span className="device-glyph"><Icon name="devices" /></span>
              <span><b>{device.label}</b><small>{device.platform || "手工配置"}{device.hostname ? ` · ${device.hostname}` : ""}</small></span>
              <span className={`device-presence ${device.online ? "online" : "offline"}`}>
                {device.compatibility && device.compatibility.wrapper_protocol !== device.compatibility.relay_protocol
                  ? "需要更新" : device.online ? "在线" : "离线"}
              </span>
            </button>
            {device.managed && <PanelRowMenu label={`管理设备 ${device.label}`} disabled={loading} items={[
              { label: "重命名", onClick: () => { setEditing(device.machine_id); setLabel(device.label); } },
              { label: "移除设备", danger: true, onClick: () => void revoke(device) },
            ]} />}</div>
            {deviceVersionNotice(device.compatibility) && <p className="device-error">
              {deviceVersionNotice(device.compatibility)}
            </p>}
            {editing === device.machine_id && <form className="device-rename"
              onSubmit={(event) => { event.preventDefault(); void saveLabel(device); }}>
              <input autoFocus aria-label="设备名称" maxLength={64} value={label}
                onChange={(event) => setLabel(event.target.value)} />
              <button type="submit" disabled={loading}>保存</button>
              <button type="button" onClick={() => setEditing(null)}>取消</button>
            </form>}
          </article>)}
        </section>
      </div>
    </section>
  </>;
}
