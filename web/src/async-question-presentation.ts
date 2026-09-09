import type { AsyncQuestionSpec } from "./protocol";
import type { Turn } from "./domain/conversation";

export interface SupplementalAnswer { question: string; answer: string }

export function supplementalAnswerPrompt(answers: SupplementalAnswer[]): string {
  return `补充回答：\n\n${answers.map(({ question, answer }) =>
    `问题：${question}\n回答：${answer}`).join("\n\n")}`;
}

/** Display-only recognition of our exact reply envelope against a preceding
 * native async question. Never classify arbitrary prompts by a prefix alone;
 * unavailable or ambiguous history keeps its original, unabridged rendering. */
function matchReply(prompt: string, questions: AsyncQuestionSpec[]): SupplementalAnswer[] | null {
  let rest = prompt.slice("补充回答：\n\n".length);
  const answers: SupplementalAnswer[] = [];
  let next = 0;
  while (rest) {
    const index = questions.findIndex((q, i) =>
      i >= next && rest.startsWith(`问题：${q.title}\n回答：`));
    if (index < 0) return null;
    const question = questions[index].title;
    const body = rest.slice(`问题：${question}\n回答：`.length);
    const boundaries = questions.slice(index + 1).map(q =>
      body.indexOf(`\n\n问题：${q.title}\n回答：`)).filter(i => i >= 0);
    const end = boundaries.length ? Math.min(...boundaries) : body.length;
    const answer = body.slice(0, end);
    if (!answer || answer !== answer.trim()) return null;
    answers.push({ question, answer });
    rest = end === body.length ? "" : body.slice(end + 2);
    next = index + 1;
  }
  return answers.length && supplementalAnswerPrompt(answers) === prompt ? answers : null;
}

export function presentAsyncQuestionReplies(turns: readonly Turn[]) {
  // Compaction/replay can temporarily project one immutable native question in
  // more than one row. A row alias is not a new question or a new answer slot.
  const candidates = new Map<string, AsyncQuestionSpec[]>();
  const questionOwners = new Map<string, string>();
  const replies = new Map<string, SupplementalAnswer[]>();
  const answered = new Set<string>();
  for (const turn of turns) {
    if (turn.prompt.startsWith("补充回答：\n\n")) {
      const matches = [...candidates].flatMap(([messageId, questions]) => {
        const answers = matchReply(turn.prompt, questions);
        return answers ? [{ messageId, answers }] : [];
      });
      if (matches.length === 1) {
        replies.set(turn.id, matches[0].answers);
        if (!turn.error && !turn.interrupted) answered.add(matches[0].messageId);
      }
    }
    for (const block of turn.blocks) {
      if (block.kind === "text" && block.delivery === "async" && block.questions?.length
          && !candidates.has(block.message_id)) {
        candidates.set(block.message_id, block.questions);
        questionOwners.set(block.message_id, turn.id);
      }
    }
  }
  return { replies, answered, questionOwners };
}
