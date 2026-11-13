import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  CollaborationCommand, CollaborationDelivery, CollaborationMember, CollaborationMessage,
  CollaborationRoom, CollaborationState, ServerEvent,
} from "../protocol";
import type { QueryAcceptanceResult } from "../outbox";
import { roomMemberIdentity, shortRoomSessionId } from "../collaboration";
import { EngineIcon, Icon } from "../icons";
import { AsyncQuestionDialog, type AsyncQuestionDraft } from "./AsyncQuestionDialog";
import { QuestionSheet, type QuestionDraft } from "./QuestionSheet";
import "./CollaborationQuestions.css";

type NativeQuestions = NonNullable<CollaborationState["data"]["questions"]>;
type QuestionStates = NonNullable<CollaborationState["data"]["question_states"]>;
type Command = (operation: CollaborationCommand["operation"], values?: Record<string, unknown>) => string | null;
type Submission = {
  roomId: string;
  memberId: string;
  messageId: string;
  prompt: string;
  lastAttempt: number;
  receipt: Promise<QueryAcceptanceResult>;
  finish: (result: QueryAcceptanceResult) => void;
};

export interface CollaborationQuestionCache {
  nativeDrafts: Map<string, QuestionDraft>;
  asyncDrafts: Map<string, AsyncQuestionDraft>;
  seen: Set<string>;
  deferredRooms: Set<string>;
  submissions: Map<string, Submission>;
  attempts: Map<string, string>;
  requests: Map<string, { key: string; asyncId?: string }>;
  errors: Map<string, string>;
}

const nativeKey = (memberId: string, askId: string) => JSON.stringify([memberId, askId]);
const asyncKey = (message: CollaborationMessage) => `async:${message.id}`;
const pending = (message: CollaborationMessage) => message.question?.kind === "async"
  && ["pending", "submitting"].includes(message.question.status);

function trackRequest(cache: CollaborationQuestionCache, requestId: string, key: string, asyncId?: string) {
  for (const [id, request] of cache.requests) {
    if (request.key === key) cache.requests.delete(id);
  }
  cache.requests.set(requestId, { key, asyncId });
}

function source(member: CollaborationMember): string {
  const identity = roomMemberIdentity(member);
  return [member.name, member.name !== identity && identity,
    shortRoomSessionId(member.native_sid || member.sid)].filter(Boolean).join(" · ");
}

export function CollaborationQuestions({ room, messages, deliveries, questions, pendingQuestions,
  questionStates, ready, command, cache, listen }: {
  room: CollaborationRoom;
  messages: CollaborationMessage[];
  deliveries: CollaborationDelivery[];
  questions: NativeQuestions;
  pendingQuestions: CollaborationMessage[] | null;
  questionStates: QuestionStates;
  ready: boolean;
  command: Command;
  cache: CollaborationQuestionCache;
  listen: (handler: ((event: ServerEvent) => boolean) | null) => void;
}) {
  const [active, setActive] = useState<string | null>(null);
  const [opening, setOpening] = useState(0);
  const [, redraw] = useState(0);
  const activeRef = useRef(active);
  activeRef.current = active;
  const asyncQuestions = useMemo(() => (pendingQuestions ?? messages).filter(pending), [messages, pendingQuestions]);
  const members = new Map(room.members?.map(member => [member.id, member]));

  const settle = useCallback((id: string, result: QueryAcceptanceResult) => {
    const submission = cache.submissions.get(id);
    if (!submission) return;
    cache.submissions.delete(id);
    for (const [requestId, request] of cache.requests) {
      if (request.asyncId === id) cache.requests.delete(requestId);
    }
    if (result.accepted) {
      cache.attempts.delete(id);
      cache.asyncDrafts.delete(id);
    }
    submission.finish(result);
    redraw(value => value + 1);
  }, [cache]);

  useEffect(() => {
    listen(event => {
      const requestId = (event.type === "error" || event.type === "collaboration_state") && event.request_id;
      const request = requestId && cache.requests.get(requestId);
      if (!request || !requestId) return false;
      const error = event.type === "error" ? event.message
        : event.type === "collaboration_state" ? event.error : undefined;
      cache.requests.delete(requestId);
      if (!error) return false;
      if (request.asyncId) settle(request.asyncId, { accepted: false,
        error: { code: "collaboration_answer_failed", message: error } });
      else {
        cache.errors.set(request.key, error);
        while (cache.errors.size > 64) cache.errors.delete(cache.errors.keys().next().value!);
      }
      redraw(value => value + 1);
      return true;
    });
    return () => listen(null);
  }, [listen, cache, settle]);

  useEffect(() => {
    // Reconstruct an in-flight answer after a reload or an answer from another
    // browser. Its existing delivery owns the input identity and receipt.
    for (const message of asyncQuestions) {
      const question = message.question!;
      const delivery = deliveries.find(delivery => delivery.id === question.answer_delivery);
      if (cache.submissions.has(message.id) || !message.member_id
          || question.status !== "submitting") continue;
      let finish!: Submission["finish"];
      const receipt = new Promise<QueryAcceptanceResult>(resolve => { finish = resolve; });
      cache.submissions.set(message.id, { roomId: room.id, memberId: message.member_id,
        messageId: delivery?.message_id ?? "", prompt: "", lastAttempt: Date.now(), receipt, finish });
      if (question.answers) cache.asyncDrafts.set(message.id, {
        choices: question.answers.map(() => null),
        texts: question.answers.map(answer => Array.isArray(answer) ? answer.join("\n") : answer),
      });
      redraw(value => value + 1);
    }
    for (const [id, submission] of cache.submissions) {
      if (submission.roomId !== room.id) continue;
      const state = asyncQuestions.find(message => message.id === id)?.question
        ?? questionStates[id] ?? messages.find(message => message.id === id)?.question;
      const delivery = deliveries.find(delivery => delivery.message_id === submission.messageId);
      const error = state?.error || (delivery && ["failed", "cancelled"].includes(delivery.status)
        ? delivery.error || "回答未送达，请重试。" : undefined);
      if (state?.status === "answered" && !state.error) settle(id, { accepted: true });
      else if (error) settle(id, { accepted: false, error: { code: "collaboration_answer_failed", message: error } });
      else if (ready && questionStates[id]?.status === "pending" && !delivery
          && submission.prompt && Date.now() - submission.lastAttempt >= 3500) {
        // A dropped control frame has no delivery. Reuse its original input
        // identity so an uncertain response can never enqueue a second answer.
        submission.lastAttempt = Date.now();
        const request = command("send", { message_id: submission.messageId, ask_id: id,
          targets: [submission.memberId], prompt: submission.prompt });
        if (request) trackRequest(cache, request, `async:${id}`, id);
      }
    }
  }, [messages, asyncQuestions, questionStates, deliveries, room.id, ready, command, cache, settle]);

  useEffect(() => {
    if (!ready) return;
    // A question may precede the current history page. Read its exact receipt
    // without moving the conversation cursor or replaying a model turn.
    const check = () => {
      for (const [id, submission] of cache.submissions) {
        if (submission.roomId === room.id) command("read", { room_id: room.id, ask_id: id });
      }
    };
    check();
    const timer = window.setInterval(check, 3500);
    return () => window.clearInterval(timer);
  }, [ready, room.id, command, cache]);

  useEffect(() => {
    const keys = [...questions.map(item => nativeKey(item.member_id, item.question.ask_id)),
      ...asyncQuestions.map(asyncKey)];
    for (const key of cache.seen) {
      if (cache.seen.size > 64 && !keys.includes(key)) cache.seen.delete(key);
    }
    if (active && keys.includes(active)) {
      if (cache.nativeDrafts.get(active)?.collapsed) cache.deferredRooms.add(room.id);
      return;
    }
    if (!keys.length && ready) cache.deferredRooms.delete(room.id);
    if (cache.deferredRooms.has(room.id)) {
      setActive(null);
      return;
    }
    const firstNative = questions[0];
    const firstAsync = asyncQuestions.find(message => !cache.seen.has(asyncKey(message)));
    const next = firstNative ? nativeKey(firstNative.member_id, firstNative.question.ask_id)
      : firstAsync ? asyncKey(firstAsync) : null;
    if (next) cache.seen.add(next);
    setActive(next);
  }, [questions, asyncQuestions, active, cache, room.id, ready]);

  const open = (key: string) => {
    cache.deferredRooms.add(room.id);
    const previous = activeRef.current;
    if (previous) {
      const draft = cache.nativeDrafts.get(previous);
      if (draft) cache.nativeDrafts.set(previous, { ...draft, collapsed: true });
    }
    const draft = cache.nativeDrafts.get(key);
    if (draft) cache.nativeDrafts.set(key, { ...draft, collapsed: false });
    cache.seen.add(key);
    setActive(key);
    setOpening(value => value + 1);
  };
  const closeAsync = (key: string) => {
    cache.deferredRooms.add(room.id);
    if (activeRef.current === key) setActive(null);
  };
  const answerNative = (key: string, values: Record<string, unknown>) => {
    cache.errors.delete(key);
    const request = ready && command("answer", values);
    if (request) trackRequest(cache, request, key);
    else cache.errors.set(key, "回答暂未提交，请检查设备连接后重试。");
    redraw(value => value + 1);
    return !!request;
  };
  const answerAsync = (message: CollaborationMessage, prompt: string) => {
    const existing = cache.submissions.get(message.id);
    if (existing) return existing.receipt;
    if (!ready || !message.member_id) return null;
    const attempt = cache.attempts.get(message.id);
    const delivery = attempt && deliveries.find(delivery => delivery.message_id === attempt);
    const retryable = delivery && ["failed", "cancelled"].includes(delivery.status);
    const messageId = attempt && !retryable ? attempt : crypto.randomUUID();
    const request = command("send", { message_id: messageId, ask_id: message.id,
      targets: [message.member_id], prompt });
    if (!request) return null;
    let finish!: Submission["finish"];
    const receipt = new Promise<QueryAcceptanceResult>(resolve => { finish = resolve; });
    cache.attempts.set(message.id, messageId);
    cache.submissions.set(message.id, { roomId: room.id, memberId: message.member_id,
      messageId, prompt, lastAttempt: Date.now(), receipt, finish });
    trackRequest(cache, request, asyncKey(message), message.id);
    return receipt;
  };

  if (!questions.length && !asyncQuestions.length) return null;
  return <div className="collab-questions" aria-label="待回答问题">
    {questions.map(item => {
      const member = members.get(item.member_id);
      if (!member) return null;
      const key = nativeKey(member.id, item.question.ask_id);
      if (active !== key) return <button className="collab-question-entry" key={key}
        type="button" onClick={() => open(key)}>
        <EngineIcon engine={member.spec.engine} size={20} />
        <span className="collab-question-identity">{member.name}<small>{source(member)}</small></span>
        <span className="collab-question-count">等待你回答</span>
        <span className="collab-question-action">继续回答<Icon name="chevron-right" size={14} /></span>
      </button>;
      return <QuestionSheet key={`${key}:${opening}`} retainDraftOnSubmit
        draft={{ key, store: cache.nativeDrafts }} engine={member.spec.engine}
        question={item.question.question} options={item.question.options}
        allowText={item.question.allow_text} multiSelect={item.question.multi_select}
        header={item.question.header} sourceLabel={source(member)}
        submissionError={cache.errors.get(key)} planDocument={item.question.plan_document}
        secret={item.question.secret} questions={item.question.questions}
        onAnswers={answers => answerNative(key, { member_id: member.id, ask_id: item.question.ask_id, answers })}
        onAnswer={answer => answerNative(key, { member_id: member.id, ask_id: item.question.ask_id, answer })} />;
    })}
    {asyncQuestions.map(message => {
      const member = message.member_id && members.get(message.member_id);
      const question = message.question!;
      if (!member) return null;
      const key = asyncKey(message);
      const submitting = question.status === "submitting" || cache.submissions.has(message.id);
      if (active !== key) return <button className="collab-question-entry" type="button" key={key}
        onClick={() => open(key)}>
        <EngineIcon engine={member.spec.engine} size={20} />
        <span className="collab-question-identity">{member.name}<small>{roomMemberIdentity(member)} · {shortRoomSessionId(member.native_sid || member.sid)}</small></span>
        <span className="collab-question-count">{submitting ? "回答确认中" : `${question.questions.length} 个问题待回答`}</span>
        <span className="collab-question-action">{submitting ? "查看回答" : "继续回答"}<Icon name="chevron-right" size={14} /></span>
      </button>;
      if (question.status === "submitting" && !cache.submissions.has(message.id)) return null;
      return <AsyncQuestionDialog key={`${key}:${opening}`} className="collab-async-question-dialog"
        engine={member.spec.engine} sourceLabel={source(member)} questionId={question.ask_id}
        questions={question.questions.map(page => ({ title: page.question,
          options: page.options.map(option => option.label) }))}
        initialDraft={cache.asyncDrafts.get(message.id) ?? (question.answers ? {
          choices: question.answers.map(() => null),
          texts: question.answers.map(answer => Array.isArray(answer) ? answer.join("\n") : answer),
        } : undefined)} answered={false}
        onDraftChange={draft => { cache.asyncDrafts.set(message.id, draft); }}
        pendingReply={cache.submissions.get(message.id)?.receipt}
        onReply={ready && (question.status !== "submitting" || cache.submissions.has(message.id))
          ? prompt => answerAsync(message, prompt) : undefined}
        replyMode={deliveries.some(delivery => delivery.member_id === member.id
          && ["running", "accepted", "stopping"].includes(delivery.status)) ? "steer" : "query"}
        onClose={() => closeAsync(key)} />;
    })}
  </div>;
}
