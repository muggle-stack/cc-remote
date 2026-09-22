import { useEffect, useMemo, useState } from "react";
import { Icon } from "../icons";
import { deviceVersionNotice, type DeviceCompatibility } from "../device-version";

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
  const [error, setError] = useState<string | null>(null);
  const [pairCode, setPairCode] = useState<string | null>(null);
  const [pairExpires, setPairExpires] = useState<number | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [label, setLabel] = useState("");

  const refresh = async () => {
    setLoading(true);
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
  const pairCommand = useMemo(() => pairCode
    ? `python -m cc_remote.device pair ${window.location.origin} ${pairCode}`
    : "", [pairCode]);

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
        <div><b>设备中心</b><small>连接并管理运行 Claude / Codex 的机器</small></div>
        <div className="device-head-actions">
          <button className="iconbtn" disabled={loading} onClick={() => void refresh()}
            aria-label="刷新设备"><Icon name="refresh" /></button>
          <button className="iconbtn" onClick={onClose} aria-label="关闭"><Icon name="close" /></button>
        </div>
      </header>
      <div className="device-body">
        {error && <div className="device-error">{error}</div>}
        <section className="device-pairing">
          <div>
            <b>添加设备</b>
            <p>短时开启配对后，在 Mac 或 Linux 上执行一次命令即可接入。</p>
          </div>
          {!pairing.enabled && !pairCode
            ? <button disabled={loading} onClick={() => void startPairing()}>允许添加设备</button>
            : <button className="subtle" disabled={loading} onClick={() => void stopPairing()}>关闭配对</button>}
          {pairCode && <div className="device-code">
            <span>一次性配对码</span><strong>{pairCode}</strong>
            <code>{pairCommand}</code>
            <button onClick={() => void navigator.clipboard.writeText(pairCommand)}>复制命令</button>
            {expiresAt && <small>有效期至 {new Date(expiresAt * 1000).toLocaleTimeString()}</small>}
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
            <button className="device-main" onClick={() => onSelect(device.machine_id)}>
              <span className="device-glyph"><Icon name="devices" /></span>
              <span><b>{device.label}</b><small>{device.platform || "手工配置"}{device.hostname ? ` · ${device.hostname}` : ""}</small></span>
              <span className={`device-presence ${device.online ? "online" : "offline"}`}>
                {device.compatibility && device.compatibility.wrapper_protocol !== device.compatibility.relay_protocol
                  ? "需要更新" : device.online ? "在线" : "离线"}
              </span>
            </button>
            {deviceVersionNotice(device.compatibility) && <p className="device-error">
              {deviceVersionNotice(device.compatibility)}
            </p>}
            {editing === device.machine_id && <form className="device-rename"
              onSubmit={(event) => { event.preventDefault(); void saveLabel(device); }}>
              <input autoFocus maxLength={64} value={label}
                onChange={(event) => setLabel(event.target.value)} />
              <button type="submit">保存</button>
              <button type="button" onClick={() => setEditing(null)}>取消</button>
            </form>}
            {device.managed && editing !== device.machine_id && <div className="device-actions">
              <button onClick={() => { setEditing(device.machine_id); setLabel(device.label); }}>重命名</button>
              <button className="danger" onClick={() => void revoke(device)}>移除</button>
            </div>}
          </article>)}
        </section>
      </div>
    </section>
  </>;
}
