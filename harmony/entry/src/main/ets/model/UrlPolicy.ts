import { url } from '@kit.ArkTS';

export const SETTINGS_URL = 'cc-remote-app://settings';

function parse(value: string): url.URL | undefined {
  // Reject URL-parser repairs (backslashes, controls, whitespace, credentials).
  if (!/^https:\/\//i.test(value) || /[\\\s\u0000-\u001f\u007f]/.test(value)) return undefined;
  const authority = value.slice(value.indexOf('//') + 2).split(/[/?#]/, 1)[0];
  if (authority === '' || authority.includes('@')) return undefined;
  try {
    const parsed = new url.URL(value);
    if (parsed.protocol !== 'https:' || parsed.username !== '' || parsed.password !== ''
      || parsed.hostname === '' || parsed.port === '0') return undefined;
    return parsed;
  } catch (_) {
    return undefined;
  }
}

export function httpsOrigin(value: string): string {
  return parse(value)?.origin ?? '';
}

export function serverOrigin(input: string): string {
  const value = input.trim();
  const parsed = parse(value);
  if (!parsed || (parsed.pathname !== '' && parsed.pathname !== '/')
    || parsed.search !== '' || parsed.hash !== '' || value.includes('?') || value.includes('#')) return '';
  return parsed.origin;
}

export function sameOrigin(server: string, value: string): boolean {
  return server !== '' && server === httpsOrigin(value);
}

export function microphoneRequest(server: string, current: string, origin: string,
  resources: string[]): boolean {
  return sameOrigin(server, current) && sameOrigin(server, origin)
    && resources.length === 1 && resources[0] === 'TYPE_AUDIO_CAPTURE';
}

export function settingsRequest(value: string, server: string, current: string,
  mainFrame: boolean, gesture: boolean): boolean {
  return value === SETTINGS_URL && mainFrame && gesture && sameOrigin(server, current);
}

export type NavigationAction = 'allow' | 'external' | 'settings' | 'block';

export function newWindowAction(value: string, server: string,
  gesture: boolean): 'browser' | 'external' | 'block' {
  if (!gesture) return 'block';
  // A user-created blank window may resolve its destination after attachment.
  if (value === '' || value === 'about:blank') return 'browser';
  if (!httpsOrigin(value)) return 'block';
  return sameOrigin(server, value) ? 'browser' : 'external';
}

export function navigationAction(value: string, server: string, current: string,
  chat: boolean, mainFrame: boolean, gesture: boolean, pendingPopup: boolean = false): NavigationAction {
  // Opaque viewer frames and their own CSP/origin boundary remain untouched.
  // Local/command schemes still never get privileged native handling in a frame.
  if (!mainFrame) return value === SETTINGS_URL || /^file:|^intent:/i.test(value) ? 'block' : 'allow';
  if (chat && settingsRequest(value, server, current, true, gesture)) return 'settings';
  if (!chat && value === 'about:blank') return 'allow';
  if (!httpsOrigin(value)) return 'block';
  if (!sameOrigin(server, value)) {
    // Never launch another app for automatic redirects or an iframe load. Only
    // a click or the first destination of a user-created popup can leave ArkWeb.
    return gesture || (!chat && pendingPopup) ? 'external' : 'block';
  }
  return 'allow';
}
