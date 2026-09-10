/**
 * Optional DSH 0.1.5 Host plugin: one authenticated, read-only history cut.
 *
 * The ordinary Gateway page endpoint needs a known durable cursor. Its follow
 * endpoint acquires that cursor but then activates a cold Agent. This small
 * bridge obtains the cursor using the public Session Query lease and delegates
 * pagination to the official Gateway, without following or resuming an Agent.
 */
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
  const allowed = new Set(['sessionId', 'beforeSeq', 'throughSeq', 'maxMessages']);
  if ([...params.keys()].some(key => !allowed.has(key) || params.getAll(key).length !== 1)) {
    return failure(400, 'invalid_request');
  }
  const sessionId = params.get('sessionId');
  if (!SESSION_ID.test(sessionId ?? '')) return failure(400, 'invalid_session');
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
