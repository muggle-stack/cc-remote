import { common } from '@kit.AbilityKit';
import { preferences } from '@kit.ArkData';
import { webview } from '@kit.ArkWeb';
import { util } from '@kit.ArkTS';
import { PushBridge } from '../model/PushBridge';
import { ExternalBrowser } from '../model/ExternalBrowser';
import { Downloads } from '../model/Downloads';
import { PushTarget } from '../model/PushRoute';
import { httpsOrigin, navigationAction, newWindowAction, sameOrigin, serverOrigin } from '../model/UrlPolicy';

@Entry
@Component
struct Index {
  @State private server: string = '';
  @State private address: string = '';
  @State private configuring: boolean = true;
  @State private saving: boolean = false;
  @State private ready: boolean = false;
  @State private setupError: string = '';
  @State private chatError: boolean = false;
  @State private browserOpen: boolean = false;
  @State private browserAddress: string = '';
  @State private browserError: boolean = false;
  @State private browserProgress: number = 0;
  private chat: webview.WebviewController = new webview.WebviewController();
  private browser: webview.WebviewController = new webview.WebviewController();
  private browserInitialUrl: string = '';
  private popupHandler: ControllerHandler | undefined = undefined;
  private popupTimeout: number = 0;
  private popupNavigationPending: boolean = false;
  private externalBrowser: ExternalBrowser = new ExternalBrowser();
  private downloads: Downloads | undefined = undefined;
  private store: preferences.Preferences | undefined = undefined;
  private shellScript: string = '';
  private settingsOpen: boolean = false;
  private push: PushBridge | undefined = undefined;
  private pushScript: string = '';
  private pushAttached: boolean = false;
  @StorageLink('pushTarget') @Watch('deliverNotification') private pushTarget: string = '';
  @StorageLink('pushRefresh') @Watch('refreshPush') private pushRefresh: number = 0;

  aboutToAppear(): void {
    try {
      const context = this.getUIContext().getHostContext() as common.UIAbilityContext | undefined;
      if (!context) throw new Error('Ability context unavailable');
      this.store = preferences.getPreferencesSync(context, { name: 'client' });
      const bytes = context.resourceManager.getRawFileContentSync('shell.js');
      this.shellScript = new util.TextDecoder().decodeToString(bytes);
      this.pushScript = new util.TextDecoder().decodeToString(
        context.resourceManager.getRawFileContentSync('push-bridge.js'));
      this.push = new PushBridge(context, this.store);
      this.downloads = new Downloads(context, () => this.server, message => {
        this.getUIContext().getPromptAction().showToast({ message: message });
      });
      this.server = serverOrigin(this.store.getSync('server', '') as string);
      this.address = this.server;
      this.configuring = this.server === '';
      this.ready = true;
    } catch (_) {
      this.setupError = '客户端初始化失败，请关闭后重新打开。';
    }
  }

  aboutToDisappear(): void {
    this.downloads?.cancel();
    this.push?.detach();
    this.cancelPendingPopup();
    webview.WebCookieManager.saveCookieAsync().catch(() => {});
  }

  onBackPress(): boolean {
    if (this.configuring && this.server !== '') {
      this.configuring = false;
      return true;
    }
    if (this.browserOpen) {
      try {
        if (this.browser.accessBackward()) this.browser.backward();
        else this.closeBrowser();
      } catch (_) { this.closeBrowser(); }
      return true;
    }
    try {
      if (!this.configuring && this.chat.accessBackward()) {
        this.chat.backward();
        return true;
      }
    } catch (_) { /* The controller may still be attaching on cold launch. */ }
    // A root back gesture backgrounds this ability; it does not kill the chat.
    const context = this.getUIContext().getHostContext() as common.UIAbilityContext | undefined;
    context?.moveAbilityToBackground().catch(() => {});
    return true;
  }

  private async connect(): Promise<void> {
    if (!this.ready || this.saving) return;
    const next = serverOrigin(this.address);
    if (next === '') {
      this.setupError = '请输入完整的 HTTPS 服务地址，不包含路径或账号信息。';
      return;
    }
    this.saving = true;
    try {
      if (this.server !== '' && this.server !== next) {
        this.downloads?.cancel();
        this.closeBrowser();
        await this.push?.resetServer();
        this.pushTarget = '';
        // Explicit server change clears only this app's control-link cookies.
        // Never read/export cookies, account files, or model credentials.
        await webview.WebCookieManager.clearAllCookies();
      }
      await this.store?.put('server', next);
      await this.store?.flush();
      this.server = next;
      this.address = next;
      this.chatError = false;
      this.setupError = '';
      this.configuring = false;
    } catch (_) {
      this.setupError = '保存失败，请检查网络后重试；旧通知绑定尚未解除时不会切换服务。';
      if (this.server !== '') this.pageFinished(this.chat.getUrl(), true);
    } finally {
      this.saving = false;
    }
  }

  private clientSettings(): void {
    if (this.settingsOpen || this.browserOpen) return;
    this.settingsOpen = true;
    this.getUIContext().getPromptAction().showActionMenu({
      title: '客户端设置',
      buttons: [
        { text: '刷新聊天页面', color: '#2962E8' },
        { text: '更换服务地址', color: '#182238' },
        { text: '关于 cc-remote', color: '#182238' }
      ]
    }).then(result => {
      if (result.index === 0) this.chat.refresh();
      if (result.index === 1) {
        this.address = this.server;
        this.setupError = '';
        this.configuring = true;
      }
      if (result.index === 2) this.getUIContext().getPromptAction().showDialog({
        title: 'cc-remote · 鸿蒙版',
        message: '0.1.0-preview.6\n直接使用鸿蒙 ArkWeb。聊天与任务仍在你的设备上运行。',
        buttons: [{ text: '知道了', color: '#2962E8' }]
      });
    }).catch(() => {}).finally(() => { this.settingsOpen = false; });
  }

  private intercept(request: WebResourceRequest, isChat: boolean): boolean {
    const value = request.getRequestUrl();
    const current = isChat ? this.chat.getUrl() : this.browser.getUrl();
    const action = navigationAction(value, this.server, current, isChat,
      request.isMainFrame(), request.isRequestGesture(), this.popupNavigationPending);
    if (action === 'settings') this.clientSettings();
    if (action === 'external') {
      if (!isChat) this.closeBrowser();
      this.openExternal(value);
    }
    if (action === 'block' && request.isMainFrame() && request.isRequestGesture()) {
      this.getUIContext().getPromptAction().showToast({ message: '此链接无法打开，仅支持 HTTPS 网页。' });
    }
    return action !== 'allow';
  }

  private async openExternal(value: string): Promise<void> {
    try {
      const context = this.getUIContext().getHostContext() as common.UIAbilityContext | undefined;
      if (!context) throw new Error('Ability context unavailable');
      await this.externalBrowser.open(value, context);
    } catch (_) {
      this.getUIContext().getPromptAction().showToast({
        message: '未能打开系统浏览器，请检查默认浏览器设置后再次点击链接。'
      });
    }
  }

  private newWindow(event: OnWindowNewEvent): void {
    const action = newWindowAction(event.targetUrl, this.server, event.isUserTrigger);
    if (action === 'external') {
      event.handler.setWebController(null);
      this.openExternal(event.targetUrl);
      return;
    }
    if (action === 'block' || this.browserOpen) {
      // Every path must settle the handler, otherwise ArkWeb blocks its renderer.
      event.handler.setWebController(null);
      if (action === 'browser' && this.browserOpen && sameOrigin(this.server, event.targetUrl)) {
        this.browser.loadUrl(event.targetUrl);
      }
      return;
    }
    this.browser = new webview.WebviewController();
    this.browserInitialUrl = '';
    this.browserAddress = httpsOrigin(event.targetUrl);
    this.browserError = false;
    this.popupHandler = event.handler;
    this.popupNavigationPending = true;
    this.browserOpen = true;
    this.popupTimeout = setTimeout(() => {
      if (this.popupHandler) this.closeBrowser();
    }, 5000);
  }

  private cancelPendingPopup(): void {
    clearTimeout(this.popupTimeout);
    if (this.popupHandler) {
      this.popupHandler.setWebController(null);
      this.popupHandler = undefined;
    }
  }

  private closeBrowser(): void {
    this.cancelPendingPopup();
    this.popupNavigationPending = false;
    this.browserOpen = false;
    this.browserError = false;
    this.browserProgress = 0;
  }

  private attached(isChat: boolean): void {
    const controller = isChat ? this.chat : this.browser;
    controller.setCustomUserAgent(webview.WebviewController.getDefaultUserAgent() + ' cc-remote-harmony/0.1.0');
    this.downloads?.attach(controller);
    if (!isChat && this.popupHandler) {
      const handler = this.popupHandler;
      this.popupHandler = undefined;
      clearTimeout(this.popupTimeout);
      handler.setWebController(controller);
    }
  }

  private pageFinished(value: string, isChat: boolean): void {
    if (isChat && sameOrigin(this.server, value)) {
      this.push?.attach(this.chat, this.server, this.pushScript).then(() => {
        this.pushAttached = true;
        this.deliverNotification();
      }).catch(() => {});
      this.chat.runJavaScript(this.shellScript + '(' + JSON.stringify(this.server) + ');').catch(() => {
        this.chatError = true;
      });
    }
    webview.WebCookieManager.saveCookieAsync().catch(() => {});
  }

  private refreshPush(): void {
    if (!this.pushAttached || !sameOrigin(this.server, this.chat.getUrl())) return;
    this.chat.runJavaScript("window.dispatchEvent(new Event('cc-remote-harmony-refresh'));").catch(() => {});
  }

  private deliverNotification(): void {
    if (!this.pushAttached || !this.pushTarget || !sameOrigin(this.server, this.chat.getUrl())) return;
    let target: PushTarget;
    try { target = JSON.parse(this.pushTarget) as PushTarget; } catch (_) { this.pushTarget = ''; return; }
    if (target.origin !== this.server) { this.pushTarget = ''; return; }
    const current = this.pushTarget;
    this.closeBrowser();
    this.configuring = false;
    // A warm notification never reloads the document or destroys an input draft.
    this.chat.runJavaScript("window.ccRemoteHarmonyNotification = " + JSON.stringify(target.route)
      + ";window.dispatchEvent(new Event('cc-remote-harmony-notification'));").then(() => {
      if (this.pushTarget === current) this.pushTarget = '';
    }).catch(() => {});
  }

  @Builder
  private webPage(isChat: boolean) {
    Web({ src: isChat ? this.server + '/' : this.browserInitialUrl,
      controller: isChat ? this.chat : this.browser, renderMode: RenderMode.ASYNC_RENDER })
      .width('100%').height('100%')
      .javaScriptAccess(true)
      .domStorageAccess(true)
      .fileAccess(false)
      .mixedMode(MixedMode.None)
      .geolocationAccess(false)
      .mediaPlayGestureAccess(true)
      .multiWindowAccess(true)
      .allowWindowOpenMethod(true)
      .overScrollMode(OverScrollMode.NEVER)
      .keyboardAvoidMode(WebKeyboardAvoidMode.RESIZE_CONTENT)
      .onControllerAttached(() => this.attached(isChat))
      .onLoadIntercept(event => event ? this.intercept(event.data, isChat) : true)
      .onOverrideUrlLoading(request => this.intercept(request, isChat))
      .onWindowNew(event => this.newWindow(event))
      .onWindowExit(() => { if (!isChat) this.closeBrowser(); })
      .onPageBegin(event => {
        if (isChat) {
          this.chatError = false;
          this.pushAttached = false;
          this.push?.detach();
        } else {
          if (event && sameOrigin(this.server, event.url)) this.popupNavigationPending = false;
          this.browserError = false;
          this.browserAddress = event ? httpsOrigin(event.url) : '';
        }
      })
      .onPageEnd(event => { if (event) this.pageFinished(event.url, isChat); })
      .onProgressChange(event => { if (!isChat && event) this.browserProgress = event.newProgress; })
      .onErrorReceive(event => {
        if (event?.request.isMainFrame()) {
          if (isChat) this.chatError = true;
          else this.browserError = true;
        }
      })
      .onHttpErrorReceive(event => {
        if (event?.request.isMainFrame() && event.response.getResponseCode() >= 400) {
          if (isChat) this.chatError = true;
          else this.browserError = true;
        }
      })
      .onSslErrorEventReceive(event => { event?.handler.handleCancel(); })
      .onRenderExited(() => {
        if (isChat) this.chatError = true;
        else this.browserError = true;
      })
      // ArkWeb's system file selector handles input[type=file], MIME filters,
      // multiple selection and cancellation without broad storage permission.
  }

  @Builder
  private setup() {
    Column({ space: 18 }) {
      Image($r('app.media.app_icon')).width(72).height(72).borderRadius(18)
      Text('cc-remote').fontSize(30).fontWeight(FontWeight.Bold).fontColor('#182238')
      Text('连接你的 Claude 与 Codex').fontSize(15).fontColor('#677188')
      TextInput({ placeholder: 'https://remote.example.com', text: this.address })
        .type(InputType.URL).height(52).borderRadius(14)
        .onChange(value => { this.address = value; this.setupError = ''; })
        .onSubmit(() => { this.connect(); })
      if (this.setupError !== '') {
        Text(this.setupError).fontColor('#BF3535').fontSize(13)
      }
      if (this.server !== '') {
        Text('切换到其他服务地址会退出当前网页登录。').fontSize(13).fontColor('#677188')
      }
      Button(this.saving ? '正在保存…' : '连接').width('100%').height(48)
        .enabled(this.ready && !this.saving).backgroundColor('#2962E8').onClick(() => { this.connect(); })
      if (this.server !== '') {
        Button('返回聊天').type(ButtonType.Normal).backgroundColor(Color.Transparent)
          .fontColor('#677188').onClick(() => { this.configuring = false; })
      }
    }.padding(28).width('100%').height('100%').justifyContent(FlexAlign.Center).backgroundColor('#F8FAFF')
  }

  build() {
    Stack() {
      if (this.server !== '') {
        // This subtree survives native menus, a link opened in another Web and
        // app background/foreground. Never replace it with a loading screen.
        Column() {
          if (this.chatError) {
            Row({ space: 12 }) {
              Text('页面未能加载').fontSize(13).fontColor('#677188').layoutWeight(1)
              Button('重试').onClick(() => this.chat.refresh())
              Button('设置').onClick(() => this.clientSettings())
            }.padding(10).width('100%')
          }
          Stack() { this.webPage(true) }.layoutWeight(1).width('100%')
        }.width('100%').height('100%')
          .visibility(this.browserOpen || this.configuring ? Visibility.Hidden : Visibility.Visible)
      }
      if (this.browserOpen) {
        Column() {
          Row({ space: 10 }) {
            Button('‹').fontSize(28).width(44).height(44)
              .backgroundColor(Color.Transparent).fontColor('#2962E8')
              .accessibilityText('网页后退').onClick(() => {
                if (this.browser.accessBackward()) this.browser.backward();
                else this.closeBrowser();
              })
            Text(this.browserAddress).fontSize(13).fontColor('#677188').maxLines(1)
              .textOverflow({ overflow: TextOverflow.Ellipsis }).layoutWeight(1)
            Button('返回聊天').fontSize(13).height(36).onClick(() => this.closeBrowser())
          }.padding({ left: 4, right: 12 }).height(48).width('100%')
          if (this.browserError) {
            Button('加载失败，点击重试').width('100%').onClick(() => this.browser.refresh())
          }
          // Fixed progress slot avoids resizing Web while the page loads.
          Progress({ value: this.browserProgress, total: 100, type: ProgressType.Linear })
            .width('100%').height(2).opacity(this.browserProgress < 100 ? 1 : 0)
          Stack() { this.webPage(false) }.layoutWeight(1).width('100%')
        }.width('100%').height('100%').backgroundColor('#F8FAFF')
      }
      if (this.configuring) { this.setup() }
    }.width('100%').height('100%').backgroundColor('#F8FAFF')
  }
}
