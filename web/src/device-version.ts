export interface DeviceCompatibility {
  wrapper_version: string | null;
  relay_version: string;
  wrapper_protocol: number;
  relay_protocol: number;
}

export function deviceVersionNotice(info: DeviceCompatibility | undefined): string | null {
  if (!info || !Number.isSafeInteger(info.wrapper_protocol)
      || !Number.isSafeInteger(info.relay_protocol)) return null;
  if (info.wrapper_protocol !== info.relay_protocol) {
    const target = info.wrapper_protocol < info.relay_protocol ? "这台设备" : "VPS 服务端";
    return `版本不兼容：设备协议 v${info.wrapper_protocol}，服务端协议 v${info.relay_protocol}。请在${target}执行 cc-remote update。`;
  }
  if (info.wrapper_version && info.wrapper_version !== info.relay_version) {
    return `设备 v${info.wrapper_version}，服务端 v${info.relay_version}：版本不同，当前仍可连接。可执行 cc-remote update 更新。`;
  }
  return null;
}
