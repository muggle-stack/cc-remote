import { common } from '@kit.AbilityKit';
import { webview } from '@kit.ArkWeb';
import { preferences } from '@kit.ArkData';
import { util } from '@kit.ArkTS';
import { notificationManager } from '@kit.NotificationKit';
import { pushService } from '@kit.PushKit';
import { BusinessError } from '@kit.BasicServicesKit';
import { vibrator } from '@kit.SensorServiceKit';
import { sameOrigin } from './UrlPolicy';

interface PushRequest { id: number; method: string }
interface PushResponse { id: number; enabled?: boolean; token?: string; error?: string }

export class PushBridge {
  private generation: number = 0;
  private port: webview.WebMessagePort | undefined = undefined;
  private queue: Promise<void> = Promise.resolve();
  private pending: number = 0;
  private lastRecordingHaptic: number = 0;

  private context: common.UIAbilityContext;
  private store: preferences.Preferences;

  constructor(context: common.UIAbilityContext, store: preferences.Preferences) {
    this.context = context;
    this.store = store;
  }

  detach(): void {
    this.generation++;
    try { this.port?.close(); } catch (_) {}
    this.port = undefined;
  }

  async attach(controller: webview.WebviewController, origin: string, script: string): Promise<void> {
    this.detach();
    const generation = this.generation;
    if (!sameOrigin(origin, controller.getUrl())) return;
    const nonce = util.generateRandomUUID();
    await controller.runJavaScript(script + '(' + JSON.stringify(origin) + ',' + JSON.stringify(nonce) + ');');
    if (generation !== this.generation || !sameOrigin(origin, controller.getUrl())) return;
    const ports = controller.createWebMessagePorts();
    this.port = ports[1];
    ports[1].onMessageEvent(value => {
      if (generation !== this.generation || typeof value !== 'string' || value.length > 256
        || !sameOrigin(origin, controller.getUrl())) return;
      if (value === 'recording-haptic') {
        const now = Date.now();
        if (now - this.lastRecordingHaptic < 500) return;
        this.lastRecordingHaptic = now;
        try {
          vibrator.startVibration({ type: 'time', duration: 25 }, { id: 0, usage: 'touch' }).catch(() => {});
        } catch (_) { /* Some devices do not have a vibrator. */ }
        return;
      }
      if (this.pending >= 8) return;
      let request: PushRequest;
      try { request = JSON.parse(value) as PushRequest; } catch (_) { return; }
      if (!request || typeof request !== 'object' || !Number.isSafeInteger(request.id) || request.id <= 0
        || !['status', 'enable', 'token', 'delete'].includes(request.method)) return;
      this.pending++;
      this.queue = this.queue.then(async () => {
        if (generation !== this.generation || !sameOrigin(origin, controller.getUrl())) return;
        const response: PushResponse = { id: request.id };
        try {
          if (request.method === 'delete') {
            await this.deleteIssuedToken();
          } else if (request.method === 'status') {
            response.enabled = await notificationManager.isNotificationEnabled();
          } else if (request.method === 'enable') {
            if (!await notificationManager.isNotificationEnabled()) {
              await notificationManager.requestEnableNotification(this.context);
            }
            response.enabled = await notificationManager.isNotificationEnabled();
            if (!response.enabled) response.error = 'permission_denied';
          } else {
            if (!await notificationManager.isNotificationEnabled()) throw new Error('permission_denied');
            if (this.store.getSync('pushReset', false) === true) await this.deleteIssuedToken();
            const token = await pushService.getToken();
            // Persist before exposing the token to Web/Relay. A failed entitlement
            // check must not leave an unissued token blocking server changes.
            await this.store.put('pushIssued', true);
            await this.store.flush();
            response.token = token;
          }
        } catch (error) {
          const code = (error as BusinessError).code;
          response.error = [1000900010, 1000900012].includes(code)
            ? 'push_not_configured' : (error as Error).message === 'permission_denied'
              ? 'permission_denied' : 'native_failed';
        }
        if (generation === this.generation && sameOrigin(origin, controller.getUrl())) {
          this.port?.postMessageEvent(JSON.stringify(response));
        }
      }).catch(() => {}).finally(() => { this.pending--; });
    });
    controller.postMessage(nonce, [ports[0]], origin);
  }

  private async deleteIssuedToken(): Promise<void> {
    if (this.store.getSync('pushIssued', false) !== true) return;
    await this.store.put('pushReset', true);
    await this.store.flush();
    await pushService.deleteToken();
    await this.store.put('pushIssued', false);
    await this.store.put('pushReset', false);
    await this.store.flush();
  }

  async resetServer(): Promise<void> {
    this.detach();
    // Serialize behind any already accepted token request, then invalidate it.
    await this.queue;
    await this.deleteIssuedToken();
  }
}
