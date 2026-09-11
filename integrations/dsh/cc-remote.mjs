/**
 * Optional DSH 0.1.5 Host plugin: one authenticated, read-only history cut.
 *
 * The ordinary Gateway page endpoint needs a known durable cursor. Its follow
 * endpoint acquires that cursor but then activates a cold Agent. This small
 * bridge obtains the cursor using the public Session Query lease and delegates
 * pagination to the official Gateway, without following or resuming an Agent.
 */
import { createRequire } from 'node:module';
export const name = 'cc-remote-history';
export const inject = ['connection', 'sessionQuery', 'typertGateway'];
export const SNAPSHOT_PATH = '/api/cc-remote.snapshot';
const MAX_RESPONSE_BYTES = 16 * 1024 * 1024;
const SESSION_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$/u;

export function apply(ctx) {
  ctx.connection.fetch.register({
    path: SNAPSHOT_PATH,
    methods: ['GET'],
    requestBody: 'buffered',
    fetch: request => snapshot(ctx, request),
  });
}

export async function snapshot(ctx, request) {
  const params = new URL(request.url).searchParams;
  const allowed = new Set(['sessionId', 'beforeSeq', 'throughSeq', 'maxMessages', 'query', 'deliverables']);
  if ([...params.keys()].some(key => !allowed.has(key) || params.getAll(key).length !== 1)) {
    return failure(400, 'invalid_request');
  }
  const sessionId = params.get('sessionId');
  if (!SESSION_ID.test(sessionId ?? '')) return failure(400, 'invalid_session');
  const query = params.get('query');
  if (query !== null && (!query.trim() || query.length > 1000)) return failure(400, 'invalid_query');
  if (params.has('deliverables') && params.get('deliverables') !== '1') return failure(400, 'invalid_request');
  let beforeSeq, throughSeq, maxMessages;
  try {
    beforeSeq = integer(params, 'beforeSeq', 0, Number.MAX_SAFE_INTEGER);
    throughSeq = integer(params, 'throughSeq', -1, Number.MAX_SAFE_INTEGER);
    maxMessages = integer(params, 'maxMessages', 1, 100) ?? 16;
  } catch {
    return failure(400, 'invalid_request');
  }
  let source;
  try {
    source = await ctx.sessionQuery.observeSession(sessionId, {
      signal: request.signal, projectionMode: 'all',
    });
    request.signal.throwIfAborted();
    if (source.header.version !== 3 || source.header.id !== sessionId || !source.header.cwd) {
      return failure(409, 'unsupported_session');
    }
    const cursor = throughSeq ?? source.cursor;
    if (cursor > source.cursor) return failure(409, 'stale_cursor');
    if (query && beforeSeq === undefined) {
      // Use the same native index and current surface as sidebar search. A
      // substring scan disagrees with native tokenization and retired surfaces.
      const matches = await ctx.sessionQuery.searchEvents({ sessionId, query,
        filters: [{ kind: 'type', values: ['user/message', 'assistant/message'] },
          { kind: 'surface', values: ['current'] }], limit: 20 });
      const match = matches.items.find(event => event.sessionId === sessionId
        && event.seq <= cursor && event.surface === 'current'
        && ['user/message', 'assistant/message'].includes(event.type));
      if (!match) return failure(409, 'search_match_changed');
      beforeSeq = match.seq + 1;
    }
    const page = await ctx.typertGateway.invoke({
      namespace: 'session', method: 'page',
      args: { request: {
        address: { kind: 'session', sessionId }, throughSeq: cursor, maxMessages,
        ...(beforeSeq === undefined ? {} : { beforeSeq }),
      } },
      signal: request.signal,
    });
    // Native page sizes count messages and can start in the middle of a turn.
    // Include its opening boundary so step/steer ownership is reconstructible.
    const first = page.records[0]?.event?.seq;
    if (first !== undefined && Array.isArray(source.events)) {
      const start = source.events.findLast(event => event.seq <= first && event.type === 'turn/start')?.seq;
      if (start !== undefined && start < first) {
        const prefix = source.events.filter(event => event.seq >= start && event.seq < first);
        page.records = [...prefix.map(event => ({ type: 'event', event })), ...page.records];
        page.hasMore = start > 0;
      }
    }
    const body = JSON.stringify({
      contract: 1, header: source.header, cursor, ...page,
      version: nativeVersion(),
      ...(params.has('deliverables') ? { deliverables: producedFiles(source.events, cursor) } : {}),
      projections: source.projections ?? { asOfSeq: source.cursor, values: {} },
    });
    if (Buffer.byteLength(body) > MAX_RESPONSE_BYTES) return failure(413, 'page_too_large');
    return new Response(body, { headers: {
      'content-type': 'application/json', 'cache-control': 'no-store',
    } });
  } catch {
    // Upstream errors may include paths or credential-bearing plugin messages.
    return failure(request.signal.aborted ? 499 : 503, 'history_unavailable');
  } finally {
    source?.[Symbol.dispose]();
  }
}

function nativeVersion() {
  try {
    const value = createRequire(process.argv[1])('@deepseek-ai/dsh/package.json').version;
    return typeof value === 'string' && value.length < 100 ? value : null;
  } catch { return null; }
}

export function producedFiles(events, cursor) {
  const calls = new Map();
  const files = new Map();
  for (const event of events) {
    if (event.seq > cursor || (event.surfaceOp && typeof event.surfaceOp === 'object')) continue;
    const data = event.data;
    if (event.type === 'tool/call') {
      let args;
      try { args = typeof data.arguments === 'string' ? JSON.parse(data.arguments) : data.arguments; } catch { continue; }
      if (!args || typeof args !== 'object') continue;
      const path = ['write', 'edit'].includes(data.name) ? args.file_path
        : data.name === 'str_replace_editor' && ['create', 'str_replace', 'insert'].includes(args.command) ? args.path : null;
      if (typeof path === 'string' && path.trim() && path.length <= 4096) calls.set(String(data.callId), path);
    }
    if (event.type === 'tool/result') {
      for (const block of data.message?.content ?? []) {
        const path = calls.get(String(block.toolCallId ?? data.message?.source?.callId));
        if (block.type === 'tool-result' && !block.isError && !data.error && path) {
          files.delete(path); files.set(path, { path, label: '创建或修改的文件' });
        }
      }
    }
    if (event.type === 'deliverables/presented') {
      for (const file of data.files ?? []) {
        if (typeof file.path === 'string' && file.path.trim() && file.path.length <= 4096) {
          files.delete(file.path); files.set(file.path, { path: file.path, label: String(file.description ?? '交付文件').slice(0, 4096) });
        }
      }
    }
  }
  return [...files.values()].slice(-256).reverse();
}

function integer(params, key, min, max) {
  const raw = params.get(key);
  if (raw === null) return undefined;
  if (!/^(?:0|[1-9][0-9]*|-1)$/u.test(raw)) throw new Error('invalid integer');
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < min || value > max) throw new Error('invalid integer');
  return value;
}

function failure(status, error) {
  return Response.json({ error }, { status, headers: { 'cache-control': 'no-store' } });
}
