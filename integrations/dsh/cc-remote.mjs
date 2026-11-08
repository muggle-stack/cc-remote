/** Bounded authenticated integration endpoints over native DSH services. */
import { createRequire } from 'node:module';
import { isAbsolute, normalize, relative, resolve } from 'node:path';
export const name = 'cc-remote-metadata';
export const inject = ['connection', 'sessionQuery', 'agents', 'commands', 'agentPresets', 'skills'];
const SID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,123}$/u;
export function apply(ctx) {
  ctx.connection.fetch.register({ path: '/api/cc-remote.catalog', methods: ['GET'], requestBody: 'buffered', fetch: request => catalog(ctx, request) });
  ctx.connection.fetch.register({ path: '/api/cc-remote.metadata', methods: ['GET'], requestBody: 'buffered', fetch: request => metadata(ctx, request) });
}
export async function metadata(ctx, request) {
  const params = new URL(request.url).searchParams;
  const sid = params.get('sessionId'), query = params.get('query');
  if (!SID.test(sid ?? '') || [...params.keys()].some(k => !['sessionId','query'].includes(k) || params.getAll(k).length !== 1) || (query && query.length > 1000)) return Response.json({}, {status:400});
  let observation, lease;
  try {
    observation = await ctx.sessionQuery.observeSession(sid, {signal: request.signal, projectionMode: 'all'});
    if (observation.header.version !== 4) return Response.json({}, {status:409});
    const live = ctx.agents.get(sid);
    if (!live) lease = await ctx.agentPresets.acquireScope(observation.projections?.values?.agentPreset ?? observation.header.agentPreset);
    let matchSeq;
    if (query) {
      const matches = await ctx.sessionQuery.searchEvents({sessionId:sid,query,
        filters:[{kind:'type',values:['user/message','assistant/message']},{kind:'surface',values:['current']}],limit:20});
      matchSeq = matches.items.find(e => e.sessionId === sid && e.surface === 'current')?.seq;
    }
    const body = JSON.stringify({contract:3,version:createRequire(process.argv[1])('@deepseek-ai/dsh/package.json').version,
      sessionId:sid,commands:ctx.commands.list(live ?? lease.key),deliverables:producedFiles(observation.events, observation.header.cwd),matchSeq});
    if (Buffer.byteLength(body) > 1024 * 1024) return Response.json({}, {status:413});
    return new Response(body,{headers:{'content-type':'application/json','cache-control':'no-store'}});
  } catch { return Response.json({}, {status:503}); }
  finally { await lease?.[Symbol.asyncDispose](); observation?.[Symbol.dispose](); }
}
export function producedFiles(events, cwd) {
  const calls = new Map(), patches = new Map(), files = new Map();
  for (const event of events) {
    if (event.surfaceOp && typeof event.surfaceOp === 'object') continue;
    const data = event.data;
    if (event.type === 'tool/call') {
      let args; try {args = JSON.parse(data.arguments);} catch {continue;}
      if (data.name === 'apply_patch' && typeof args?.input === 'string') {
        const paths = new Set();
        for (const line of args.input.split('\n')) {
          const match = /^\*\*\* (?:Add File|Update File|Delete File|Move to): ([^\0\r\n]{1,4096})$/u.exec(line);
          if (!match) continue;
          paths.add(normalize(match[1]));
          if (typeof cwd === 'string' && isAbsolute(cwd)) {
            const absolute = resolve(cwd, match[1]);
            paths.add(absolute);
            paths.add(relative(cwd, absolute));
          }
        }
        patches.set(data.callId, paths);
      }
      const path = ['write','edit'].includes(data.name) ? args?.file_path
        : data.name === 'str_replace_editor' && ['create','str_replace','insert'].includes(args?.command) ? args.path : null;
      if (typeof path === 'string' && path.trim() && path.length <= 4096) calls.set(data.callId,path);
    }
    if (event.type === 'tool/result' && !data.error && !data.message?.isError) {
      const path = calls.get(data.message?.toolCallId);
      if (path) files.set(path,{path,label:'创建或修改的文件'});
      if (patches.has(data.message?.toolCallId)) {
        // 0.2.2's canonical renderer names only completed operations in these
        // sections. Never infer successful output paths from the patch input:
        // a commit may stop partway through and leave later operations undone.
        const text = (data.message.content ?? []).filter(b => b.type === 'text').map(b => b.text).join('\n');
        const completed = text.startsWith('Success. Updated the following files:\n') ? text.split('\n').slice(1)
          : /^Patch stopped after \d+ of \d+ operations\.\nCompleted:\n/u.test(text)
            ? text.split('\n').slice(2) : [];
        for (const line of completed) {
          if (/^(Failed|Outcome unknown|Not executed):$/u.test(line)) break;
          const match = /^([AMD]) ([^\0\r\n]{1,4096})$/u.exec(line);
          if (!match || !patches.get(data.message.toolCallId).has(normalize(match[2]))) continue;
          if (match[1] === 'D') files.delete(match[2]);
          else files.set(match[2], {path:match[2],label:'创建或修改的文件'});
        }
      }
    }
    if (event.type === 'deliverables/presented') for (const file of data.files ?? []) {
      if (typeof file.path === 'string' && file.path.trim() && file.path.length <= 4096) files.set(file.path,{path:file.path,label:String(file.description ?? '交付文件').slice(0,4096)});
    }
  }
  return [...files.values()].slice(-256).reverse();
}

export async function catalog(ctx, request) {
  const params = new URL(request.url).searchParams;
  const cwd = params.get('cwd'), preset = params.get('preset') || undefined;
  if (!cwd || !isAbsolute(cwd) || cwd.length > 4096 || (preset && preset.length > 256)
    || [...params.keys()].some(k => !['cwd','preset'].includes(k) || params.getAll(k).length !== 1)) return Response.json({}, {status:400});
  let lease;
  try {
    lease = await ctx.agentPresets.acquireScope(preset);
    const require = createRequire(process.argv[1]);
    const { isUserInvocable } = require('@deepseek-ai/dsh-skill');
    const skills = (await ctx.skills.list({cwd, scope: lease.key})).filter(isUserInvocable)
      .slice(0,500).map(({name,description}) => ({name, description}));
    const body = JSON.stringify({contract:3,version:require('@deepseek-ai/dsh/package.json').version,
      skills,commands:ctx.commands.list(lease.key)});
    if (Buffer.byteLength(body) > 1024*1024) return Response.json({}, {status:413});
    return new Response(body,{headers:{'content-type':'application/json','cache-control':'no-store'}});
  } catch { return Response.json({}, {status:503}); }
  finally { await lease?.[Symbol.asyncDispose](); }
}
