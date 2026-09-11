import type { DshDownloadChunk } from "./protocol";
import type { RelayWs } from "./ws";
type Request = (sid: string, send: () => string | null, signal?: AbortSignal, timeout?: number) => Promise<DshDownloadChunk>;
export async function download(request: Request, transport: () => RelayWs | null, sid: string, progress: (value: number) => void, signal: AbortSignal): Promise<Blob> {
    const parts: Uint8Array<ArrayBuffer>[] = [];
    let offset = 0;
    let exportId: string | undefined;
    let total: number | undefined;
    try {
      while (true) {
        const result = await request(sid,
          () => {
            const id = transport()?.sendDownloadDsh(sid, exportId, offset) ?? null;
            // The first request id is also the private export identity, so a
            // cancel can stop packaging before the first chunk comes back.
            if (!exportId && id) exportId = id;
            return id;
          }, signal, 150000);
        if (result.error) throw new Error(result.error);
        if (!result.export_id || result.offset !== offset || result.total > 128 * 1024 * 1024
            || (exportId && result.export_id !== exportId) || (total !== undefined && result.total !== total)) {
          throw new Error("下载内容不一致，请重新导出");
        }
        exportId = result.export_id; total = result.total;
        const bytes = Uint8Array.from(atob(result.data), c => c.charCodeAt(0));
        if (!bytes.length && !result.done || offset + bytes.length > total) throw new Error("下载内容不完整");
        parts.push(bytes); offset += bytes.length;
        progress(total ? offset / total : 1);
        if (result.done) {
          if (offset !== total) throw new Error("下载内容不完整");
          return new Blob(parts, { type: "application/zip" });
        }
      }
    } finally {
      if (exportId) transport()?.sendDownloadDsh(sid, exportId, 0, true);
    }
}
