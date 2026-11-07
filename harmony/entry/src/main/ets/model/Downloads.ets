import { common } from '@kit.AbilityKit';
import { webview } from '@kit.ArkWeb';
import { fileIo, picker } from '@kit.CoreFileKit';
import { util } from '@kit.ArkTS';
import { sameOrigin } from './UrlPolicy';

export function downloadAllowed(origin: string, document: string, source: string): boolean {
  if (!sameOrigin(origin, document)) return false;
  // Blob downloads stay inside their creating WebView. Never fetch them through
  // a native HTTP client, export cookies, or privilege opaque preview frames.
  return sameOrigin(origin, source.startsWith('blob:') ? source.slice(5) : source);
}

export function downloadName(suggested: string): string {
  const name = suggested.replace(/[\\/\u0000-\u001f\u007f:*?"<>|\u202a-\u202e\u2066-\u2069]/g, '_')
    .replace(/^[. ]+|[. ]+$/g, '');
  // Stay below the filesystem's UTF-8 name limit while retaining the extension.
  if (name.length <= 64) return name || 'download';
  const dot = name.lastIndexOf('.');
  const suffix = dot > 0 && name.length - dot <= 12 ? name.slice(dot) : '';
  return name.slice(0, 64 - suffix.length) + suffix;
}

class DownloadTask {
  item: webview.WebDownloadItem;
  path: string;
  name: string;
  current: () => boolean;
  saving: boolean = false;
  cancelled: boolean = false;
  timer: number = 0;

  constructor(item: webview.WebDownloadItem, path: string, name: string, current: () => boolean) {
    this.item = item;
    this.path = path;
    this.name = name;
    this.current = current;
  }
}

/** Native ArkWeb download -> private temporary file -> user-selected document URI. */
export class Downloads {
  private context: common.UIAbilityContext;
  private origin: () => string;
  private notify: (message: string) => void;
  private directory: string;
  private prepared: boolean = false;
  private active: DownloadTask | undefined = undefined;

  constructor(context: common.UIAbilityContext, origin: () => string, notify: (message: string) => void) {
    this.context = context;
    this.origin = origin;
    this.notify = notify;
    this.directory = context.cacheDir + '/cc-remote-downloads';
  }

  private prepare(): void {
    if (this.prepared) return;
    if (!fileIo.accessSync(this.directory)) fileIo.mkdirSync(this.directory, true);
    // Only our UUID temporary files; recover space after a killed process.
    for (const name of fileIo.listFileSync(this.directory)) {
      if (/^[0-9a-f-]{36}\.download$/.test(name)) {
        try { fileIo.unlinkSync(this.directory + '/' + name); } catch (_) { /* Already removed. */ }
      }
    }
    this.prepared = true;
  }

  attach(controller: webview.WebviewController): void {
    const delegate = new webview.WebDownloadDelegate();
    delegate.onBeforeDownload(item => this.before(controller, item));
    delegate.onDownloadFinish(item => {
      const task = this.match(item);
      if (task && !task.saving && !task.cancelled) this.save(task);
    });
    delegate.onDownloadFailed(item => {
      const task = this.match(item);
      if (task && !task.saving && !task.cancelled) {
        this.fail(task, '下载失败，请检查网络后重试。');
      }
    });
    controller.setDownloadDelegate(delegate);
  }

  cancel(): void {
    const task = this.active;
    if (!task) return;
    task.cancelled = true;
    clearTimeout(task.timer);
    try { task.item.cancel(); } catch (_) { /* Completed or renderer already gone. */ }
    // A pending picker/copy owns its file until the async save finally exits.
    if (!task.saving) this.clean(task);
  }

  private match(item: webview.WebDownloadItem): DownloadTask | undefined {
    return this.active?.item.getGuid() === item.getGuid() ? this.active : undefined;
  }

  private before(controller: webview.WebviewController, item: webview.WebDownloadItem): void {
    const origin = this.origin();
    try {
      if (!downloadAllowed(origin, controller.getUrl(), item.getUrl())) {
        item.cancel();
        this.notify('此下载来源不受支持，请从聊天中的下载按钮保存。');
        return;
      }
      if (this.active) {
        item.cancel();
        this.notify('请先完成当前文件的下载或保存。');
        return;
      }
      this.prepare();
      const task = new DownloadTask(item, this.directory + '/' + util.generateRandomUUID() + '.download',
        downloadName(item.getSuggestedFileName()),
        () => this.origin() === origin && sameOrigin(origin, controller.getUrl()));
      this.active = task;
      task.timer = setTimeout(() => this.fail(task, '下载超时，请重试。'), 10 * 60 * 1000);
      item.start(task.path);
      this.notify('正在下载，完成后请选择保存位置。');
    } catch (_) {
      const task = this.match(item);
      if (task) this.fail(task, '无法开始下载，请重试。');
      else {
        try { item.cancel(); } catch (_) { /* Best effort. */ }
        this.notify('无法开始下载，请重试。');
      }
    }
  }

  private fail(task: DownloadTask, message: string): void {
    if (this.active !== task || task.cancelled || task.saving) return;
    task.cancelled = true;
    clearTimeout(task.timer);
    try { task.item.cancel(); } catch (_) { /* Best effort. */ }
    this.notify(message);
    this.clean(task);
  }

  private async save(task: DownloadTask): Promise<void> {
    task.saving = true;
    clearTimeout(task.timer);
    let output: fileIo.File | undefined = undefined;
    try {
      if (task.cancelled || !task.current()) return;
      const options = new picker.DocumentSaveOptions();
      options.newFileNames = [task.name];
      const uris = await new picker.DocumentViewPicker(this.context).save(options);
      if (task.cancelled || !task.current()) return;
      if (uris.length === 0) {
        this.notify('已取消保存。');
        return;
      }
      // Do not give a picker URI to item.start(): ArkWeb renames its temporary
      // file and cannot write through the picker grant. Copy via the granted FD.
      output = await fileIo.open(uris[0], fileIo.OpenMode.WRITE_ONLY);
      await fileIo.copyFile(task.path, output.fd);
      await fileIo.close(output);
      output = undefined;
      this.notify('文件已保存。');
    } catch (_) {
      if (!task.cancelled) this.notify('保存未完成，请重新下载并选择保存位置。');
    } finally {
      if (output) {
        try { await fileIo.close(output); } catch (_) { /* Best effort. */ }
      }
      this.clean(task);
    }
  }

  private clean(task: DownloadTask): void {
    clearTimeout(task.timer);
    try { fileIo.unlinkSync(task.path); } catch (_) { /* No partial file remains. */ }
    if (this.active === task) this.active = undefined;
  }
}
