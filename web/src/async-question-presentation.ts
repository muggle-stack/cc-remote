import type { AsyncQuestionSpec } from "./protocol";
import type { Turn } from "./domain/conversation";

export interface SupplementalAnswer { question: string; answer: string }

export function supplementalAnswerPrompt(answers: SupplementalAnswer[], questionId?: string): string {
  const reference = questionId ? `问题 ID：${JSON.stringify(questionId)}\n\n` : "";
  return `补充回答：\n\n${reference}${answers.map(({ question, answer }) =>
    `问题：${question}\n回答：${answer}`).join("\n\n")}`;
}

function answerBody(prompt: string): { body: string; questionId?: string } | null {
  const prefix = "补充回答：\n\n";
  if (!prompt.startsWith(prefix)) return null;
  const body = prompt.slice(prefix.length);
  if (!body.startsWith("问题 ID：")) return { body };
  const end = body.indexOf("\n\n");
  if (end < 0) return null;
  try {
    const questionId: unknown = JSON.parse(body.slice("问题 ID：".length, end));
    if (typeof questionId !== "string" || !questionId || questionId.length > 128) return null;
    return { body: body.slice(end + 2), questionId };
  } catch {
    return null;
  }
}

/** The envelope already contains the question context needed by the bubble.
 * Parse the complete format, without requiring an earlier history page. This
 * is only formatting: it does not prove which native question was answered. */
function replyEnvelope(prompt: string) {
  const envelope = answerBody(prompt);
  if (!envelope?.body.startsWith("问题：")) return null;
  const parts = envelope.body.slice("问题：".length).split("\n\n问题：");
  if (parts.length > 16) return null;
  const answers: SupplementalAnswer[] = [];
  for (const part of parts) {
    const separator = part.indexOf("\n回答：");
    if (separator < 0) return null;
    const question = part.slice(0, separator);
    const answer = part.slice(separator + "\n回答：".length);
    if (!question.trim() || !answer || answer !== answer.trim()) return null;
    answers.push({ question, answer });
  }
  return supplementalAnswerPrompt(answers, envelope.questionId) === prompt
    ? { ...envelope, answers } : null;
}

/** Only an exact, unambiguous preceding native question can receive an
 * answered receipt. Matching must remain independent of bubble formatting. */
function matchReply(prompt: string, questions: AsyncQuestionSpec[], questionId?: string): SupplementalAnswer[] | null {
  let rest = answerBody(prompt)?.body ?? "";
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
  return answers.length && supplementalAnswerPrompt(answers, questionId) === prompt ? answers : null;
}

export function presentAsyncQuestionReplies(turns: readonly Turn[], pendingReplyId?: string | null) {
  // Compaction/replay can temporarily project one immutable native question in
  // more than one row. A row alias is not a new question or a new answer slot.
  const candidates = new Map<string, AsyncQuestionSpec[]>();
  const questionOwners = new Map<string, string>();
  const replies = new Map<string, SupplementalAnswer[]>();
  const answered = new Set<string>();
  for (const turn of turns) {
    const envelope = !turn.detailReasons?.includes("prompt_truncated")
      ? replyEnvelope(turn.prompt) : null;
    if (envelope) replies.set(turn.id, envelope.answers);
    if (turn.prompt.startsWith("补充回答：\n\n")) {
      const matches = [...candidates].flatMap(([messageId, questions]) => {
        if (envelope?.questionId && envelope.questionId !== messageId) return [];
        const answers = matchReply(turn.prompt, questions, envelope?.questionId);
        return answers ? [{ messageId, answers }] : [];
      });
      if (matches.length === 1) {
        if (!turn.error && !turn.interrupted && (!pendingReplyId
            || (turn.id !== pendingReplyId && turn.clientMsgId !== pendingReplyId))) {
          answered.add(matches[0].messageId);
        }
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
