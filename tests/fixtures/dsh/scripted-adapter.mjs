// Offline native-runtime fixture. Copied next to the isolated DSH installation.
// No provider HTTP request is implemented or possible in this adapter.
import { LlmAdapter } from '@deepseek-ai/dsh-llm';
import { defineTool } from '@deepseek-ai/dsh-tools';
export const name = 'cc-remote-scripted-model';
export const inject = ['llm', 'tools', 'approval', 'connection', 'agents'];
const telemetry = { modelCalls: 0 };
class ScriptedAdapter extends LlmAdapter {
  async listModels(provider) { return [await this.resolveModel(provider, 'deepseek-flash')]; }
  async resolveModel(provider, id) {
    return { provider, id, name: 'Offline compatibility fixture',
      context: { contextWindow: 64000 }, inputModalities: ['text', 'image'],
      reasoning: { efforts: [{ id: 'off', name: 'Off' }, { id: 'low', name: 'Low' }], defaultEffort: 'low' } };
  }
  async *stream(options) {
    telemetry.modelCalls++;
    options.signal?.throwIfAborted();
    const input = options.messages.filter(m => m.role === 'user').flatMap(m => m.content)
      .filter(b => b.type === 'text' && b.text.startsWith('compatibility:')).at(-1)?.text ?? '';
    const userIndex = options.messages.findLastIndex(m => m.role === 'user' && m.content.some(b => b.type === 'text' && b.text.startsWith('compatibility:')));
    const answered = options.messages.slice(userIndex + 1).some(m => m.content.some(b => b.type === 'tool-result'));
    if (!answered && input === 'compatibility:plan') {
      yield { type: 'block-start', index: 0, blockType: 'tool-call' };
      yield { type: 'block-end', index: 0, block: { type: 'tool-call', id: 'fixture-plan-review',
        name: 'exit_plan_mode', arguments: JSON.stringify({ plan: '# Offline plan\n\n1. Validate the control channel.' }) } };
      yield { type: 'finish', reason: { kind: 'tool-calls' } };
      return;
    }
    if (!answered && (input === 'compatibility:ask' || input === 'compatibility:approval')) {
      const call = { type: 'tool-call', id: 'fixture-interaction',
        name: input.endsWith(':ask') ? 'ask_user_question' : 'cc_remote_test_approval',
        arguments: JSON.stringify(input.endsWith(':ask') ? { questions: [{ id: 'fixture-choice', question: '选择离线验证路径',
          options: [{ label: '继续', description: '验证答案回传' }, { label: '取消', description: '不继续' }] }] } : {}) };
      yield { type: 'block-start', index: 0, blockType: 'tool-call' };
      yield { type: 'block-end', index: 0, block: call };
      yield { type: 'finish', reason: { kind: 'tool-calls' } };
      return;
    }
    const text = input.includes('compatibility:') ? 'DSH compatibility response.' : 'Compatibility test';
    yield { type: 'block-start', index: 0, blockType: 'reasoning' };
    yield { type: 'reasoning-delta', index: 0, text: 'Offline fixture reasoning.' };
    yield { type: 'block-end', index: 0, block: { type: 'reasoning', text: 'Offline fixture reasoning.' } };
    yield { type: 'block-start', index: 1, blockType: 'text' };
    for (const word of text.split(' ')) {
      await new Promise(resolve => setTimeout(resolve, input.includes('compatibility:slow') ? 300 : 5));
      options.signal?.throwIfAborted();
      yield { type: 'text-delta', index: 1, text: word + ' ' };
    }
    yield { type: 'block-end', index: 1, block: { type: 'text', text } };
    yield { type: 'usage', usage: { inputTokens: 100, cacheReadTokens: 20, cacheWriteTokens: 10, outputTokens: 20 } };
    yield { type: 'finish', reason: { kind: 'stop' } };
  }
}
export function apply(ctx) {
  ctx.connection.fetch.register({ path: '/api/cc-remote.test-state', methods: ['GET'], requestBody: 'buffered',
    fetch: () => Response.json({ ...telemetry, agents: ctx.agents.roots().map(agent => agent.id) }),
  });
  ctx.llm.registerAdapter(['deepseek-official'], new ScriptedAdapter());
  ctx.tools.register(defineTool({ name: 'cc_remote_test_approval', description: 'Offline approval contract fixture', parameters: {},
    output: { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: value }] },
    async execute(_args, exec) {
      const outcome = await ctx.approval.request({ agent: exec.agent, toolName: 'cc_remote_test_approval',
        reason: 'Offline test; no file mutation or network request', signal: exec.signal });
      return outcome;
    },
  }));
}
