import { test, expect, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import {
  PROTOCOL_VERSION,
  type CollaborationMemberSpec,
} from "../src/protocol";

type NativeHistory = Record<string, any> | ((command: Record<string, any>) => Record<string, any>);

async function mockRooms(page: Page, nativeEngine: "claude" | "codex" = "codex") {
  const commands: Record<string, any>[] = [];
  let room: Record<string, any> | null = null;
  const messages: Record<string, any>[] = [];
  const deliveries: Record<string, any>[] = [];
  const pendingQuestions: Record<string, any>[] = [];
  const nativeAsyncAnswers: { sid: string; prompt: string; id: string }[] = [];
  let version = 0;
  let pauseSendState = false;
  let dropSend = false;
  let holdAsyncAnswers = false;
  const heldAsyncAnswers: Record<string, any>[] = [];
  let pauseRoomLoads = false;
  let catalogDelayMs = 0;
  let pauseHistory = false;
  let heldHistory: Record<string, any> | null = null;
  let rejectedOperation: { operation: string; message: string } | null = null;
  let directoryEntries = 0;
  let nativeHistory: NativeHistory = {};
  const png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aIfsAAAAASUVORK5CYII=";
  let emit = (_event: Record<string, any>) => {};
  const profiles = {
    claude: [{ id: "default", label: "Primary" }, { id: "gaia", label: "Gaia" }],
    codex: [{ id: "iris", label: "Iris" }, { id: "primary", label: "Primary" }],
    dsh: [{ id: "default", label: "default" }],
  };
  const member = (spec: CollaborationMemberSpec, index: number) => {
    const native = spec.session_id?.replace(/^dsh@/, "") || `native-${index}`;
    return {
      id: `member-${index}`,
      room_id: "room",
      name:
        spec.name ||
        `${spec.engine === "dsh" ? "DSH" : spec.engine === "claude" ? "Claude" : "Codex"} · ${spec.profile_id}`,
      spec,
      sid: spec.engine === "dsh" ? `dsh@${native}` : native,
      native_sid: native,
      cwd: spec.cwd || "/tmp/project",
      removed: false,
    };
  };
  const state = (request?: string, includeRoom = true, before?: number, askId?: string) => {
    const page = messages.filter(message => !before || message.seq < before).slice(-40);
    emit({
      type: "collaboration_state",
      request_id: request,
      room_id: includeRoom ? room?.id : undefined,
      data: {
        generation: "rooms-generation",
        version: ++version,
        profiles,
        rooms: room ? [room] : [],
        ...(room && includeRoom
          ? { room, messages: page, deliveries,
            question_states: Object.fromEntries(messages.filter(message => message.question
              && (message.id === askId || page.some(item => item.reply_to?.includes(message.id)))).map(message => [message.id, {
                member_id: message.member_id, kind: message.question.kind,
                ask_id: message.question.ask_id, status: message.question.status,
              }])),
            pending_questions: messages.filter(message => message.question?.kind === "async"
              && ["pending", "submitting"].includes(message.question.status)),
            questions: [...pendingQuestions].sort((left, right) =>
              room!.members.findIndex((member: Record<string, any>) => member.id === left.member_id)
              - room!.members.findIndex((member: Record<string, any>) => member.id === right.member_id)), before,
            has_more: !!page.length && page[0].seq > 1 }
          : {}),
      },
    });
  };
  const finishQuestion = (memberId: string, askId: string, answers: (string | string[])[]) => {
    const index = pendingQuestions.findIndex(item => item.member_id === memberId && item.question.ask_id === askId);
    if (index < 0) throw new Error("Question is not pending");
    const [item] = pendingQuestions.splice(index, 1);
    const message = messages.find(message => message.member_id === memberId && message.question?.ask_id === askId)!;
    message.question.status = "answered";
    message.question.answers = answers;
    messages.push({ id: `answer-${askId}`, seq: messages.length + 1, role: "user", member_id: null,
      text: "补充回答：\n\n" + message.question.questions.map((question: Record<string, any>, i: number) =>
        `问题：${question.question}\n回答：${Array.isArray(answers[i]) ? answers[i].join("\n") : answers[i]}`).join("\n\n"),
      created: 1, targets: [], reply_to: [message.id], source: `question-answer:${message.id}` });
    emit({ type: "ask_user_closed", sid: item.sid, ask_id: askId, reason: "answered" });
  };
  const finishAsync = (command: Record<string, any>) => {
    const questionMessage = messages.find(message => message.id === command.ask_id)!;
    questionMessage.question.status = "answered";
    delete questionMessage.question.error;
    const sid = room!.members.find((member: Record<string, any>) => member.id === questionMessage.member_id).sid;
    nativeAsyncAnswers.push({ sid, prompt: command.prompt, id: command.message_id });
    const delivery = deliveries.find(delivery => delivery.message_id === command.message_id);
    if (delivery) delivery.status = "running";
    emit({ type: "user_msg", sid, msg_id: command.message_id, client_msg_id: command.message_id, prompt: command.prompt });
    emit({ type: "turn_end", sid, turn_id: command.message_id,
      result: { subtype: "success", is_error: false } });
  };
  await page.addInitScript((engine) => {
    localStorage.setItem("cc_remote_engine", engine);
    localStorage.setItem("cc_remote_machine", "room-machine");
    localStorage.setItem("cc_remote_theme", "dark");
  }, nativeEngine);
  await page.route("**/api/**", (route) => route.fulfill({ status: 404 }));
  await page.route("**/api/session", (route) => route.fulfill({ json: {} }));
  await page.route("**/api/viewers/pages", (route) =>
    route.fulfill({ json: { pages: [] } }),
  );
  await page.route("**/api/devices", (route) =>
    route.fulfill({
      json: {
        devices: [{ machine_id: "room-machine", label: "mac2", online: true }],
      },
    }),
  );
  await page.routeWebSocket(/\/ws(?:\?|$)/, (socket) => {
    emit = (event) =>
      socket.send(
        JSON.stringify({
          v: PROTOCOL_VERSION,
          ts: Date.now() / 1000,
          ...event,
        }),
      );
    const snapshot = (sid: string) => {
      emit({
        type: "snapshot",
        sid,
        cc_session_id: sid,
        state: "idle",
        tail_text: "",
        cwd: "/tmp/project",
        generation: "native-generation",
      });
      emit({ type: "replay_end", sid, to_seq: 0, truncated: false });
      emit({ type: "ask_user_sync", sid });
      for (const item of pendingQuestions.filter(item => item.sid === sid)) emit({ ...item.question, sid });
    };
    socket.onMessage((raw) => {
      const command = JSON.parse(String(raw));
      commands.push(command);
      if (command.type === "hello") {
        emit({
          type: "btw_sync",
          generation: "native-generation",
          revision: 1,
          sessions: [],
        });
        snapshot("parent");
      } else if (command.type === "list_sessions") {
        emit({
          type: "session_list",
          request_id: command.cmd_id,
          engine: command.engine,
          space: command.space,
          sessions: [
            {
              session_id: "parent",
              engine: nativeEngine,
              space: "code",
              summary: "原生会话",
              cwd: "/tmp/project",
              state: "idle",
              last_modified: "100",
            },
            ...(room?.members ?? []).filter((member: Record<string, any>) => member.spec.engine === command.engine).map((member: Record<string, any>) => ({
              session_id: member.sid, native_session_id: member.native_sid, summary: member.name,
              engine: member.spec.engine, space: "code", cwd: member.cwd, state: "idle", last_modified: "101",
              claude_profile_id: member.spec.engine === "claude" ? member.spec.profile_id : undefined,
              codex_profile_id: member.spec.engine === "codex" ? member.spec.profile_id : undefined,
            })),
          ],
        });
      } else if (command.type === "switch_session") {
        emit({ type: "session_focus", session_id: command.session_id });
        snapshot(command.session_id);
      } else if (command.type === "answer_question") {
        const item = pendingQuestions.find(item => item.sid === command.sid && item.question.ask_id === command.ask_id)!;
        finishQuestion(item.member_id, command.ask_id, command.answers ?? [command.answer]);
        state();
      } else if ((command.type === "query" || command.type === "steer") && command.prompt.startsWith("补充回答：\n\n")) {
        const member = room?.members.find((member: Record<string, any>) => member.sid === command.sid);
        const message = messages.find(message => message.member_id === member?.id && message.question?.kind === "async")!;
        message.question.status = "answered";
        nativeAsyncAnswers.push({ sid: command.sid, prompt: command.prompt, id: command.msg_id });
        messages.push({ id: command.msg_id, seq: messages.length + 1, role: "user", member_id: null,
          text: command.prompt, created: 1, targets: [member.id], reply_to: [message.id],
          source: `native-question-answer:${member.id}:${command.msg_id}` });
        emit({ type: "user_msg", sid: command.sid, msg_id: command.msg_id, client_msg_id: command.msg_id, prompt: command.prompt });
        emit({ type: "turn_end", sid: command.sid, turn_id: command.msg_id,
          result: { subtype: "success", is_error: false } });
        state();
      } else if (command.type === "get_history") {
        emit({
          type: "history",
          session_id: command.session_id,
          sid: command.session_id,
          revision: "revision",
          generation: "native-generation",
          detail: "summary",
          events: [],
          turns: [],
          has_more: false,
          before: command.before,
          ...(typeof nativeHistory === "function" ? nativeHistory(command) : nativeHistory),
        });
      } else if (command.type === "get_turn_detail") {
        emit({ type: "turn_detail", session_id: command.session_id,
          turn_id: command.turn_id, revision: command.revision, before: command.before,
          authoritative: true, has_more: false, events: [
            { type: "user_msg", sid: command.session_id, msg_id: command.turn_id, prompt: "较早的原生问题", ts: 1 },
            { type: "assistant_msg_start", sid: command.session_id, message_id: "older-thinking", channel: "thinking", ts: 2 },
            { type: "delta", sid: command.session_id, message_id: "older-thinking", channel: "thinking", text: "更早的原生过程详情", ts: 3 },
            { type: "assistant_msg_end", sid: command.session_id, message_id: "older-thinking", channel: "thinking", ts: 4 },
            { type: "turn_end", sid: command.session_id, turn_id: command.turn_id, ts: 5,
              result: { subtype: "success", is_error: false } },
          ] });
      } else if (command.type === "get_file_preview") {
        const path = command.path as string;
        const format = path.endsWith(".wav") ? "audio" : path.endsWith(".mp4") ? "video"
          : path.endsWith(".png") ? "image" : path.endsWith(".html") ? "html"
            : path.endsWith(".md") ? "markdown" : "text";
        emit({ type: "file_preview", sid: command.sid, request_id: command.request_id, path, format,
          content: format === "html" ? "<h1>来自协作会话的 HTML</h1>" : `# 来自 ${command.sid}\n\n正文预览。`,
          media_type: format === "image" ? "image/png" : format === "audio" ? "audio/wav" : format === "video" ? "video/mp4" : null,
          data: format === "image" ? png : format === "audio" ? "UklGRiQAAABXQVZFZm10IBAAAAABAAEAgD4AAAB9AAACABAAZGF0YQAAAAA=" : null,
          media_id: format === "video" ? "a".repeat(32) : null,
          size: 44, truncated: false, mtime_ns: "1", revision: "file-version", writable: false });
      } else if (command.type === "get_preview_asset") {
        emit({ type: "preview_asset", sid: command.sid, path: command.path,
          request_id: command.request_id, preview_id: command.preview_id, media_type: "image/png", data: png });
      } else if (command.type === "list_dir") {
        emit({
          type: "dir_list",
          request_id: command.cmd_id,
          path: command.path || "/tmp",
          parent: "/",
          dirs: [{ name: "project", path: "/tmp/project" },
            ...Array.from({ length: directoryEntries }, (_, index) => ({
              name: `folder-${index}`, path: `/tmp/folder-${index}`,
            }))],
        });
      } else if (command.type === "collaboration") {
        if (rejectedOperation && command.operation === rejectedOperation.operation) {
          emit({ type: "collaboration_state", request_id: command.cmd_id,
            room_id: room?.id, error: rejectedOperation.message, data: {} });
          rejectedOperation = null;
          return;
        }
        if (command.operation === "create") {
          const members = command.members.map(member);
          room = {
            id: "room",
            name: command.name,
            members,
            default_member: members[0].id,
            revision: 1,
            updated: 1,
            archived: false,
          };
        } else if (command.operation === "add_member" && room) {
          room.members.push(member(command.member, room.members.length));
          room.revision++;
        } else if (command.operation === "update" && room) {
          room.default_member = command.default_member_id ?? room.default_member;
          room.name = command.name ?? room.name;
          room.archived = command.archived ?? room.archived;
          room.revision++;
        } else if (["remove_member", "restore_member", "rename_member"].includes(command.operation) && room) {
          const target = room.members.find((item: Record<string, any>) => item.id === command.member_id);
          if (command.operation === "rename_member") { target.name = command.name; target.spec.name = command.name; }
          else target.removed = command.operation === "remove_member";
          if (target.removed && room.default_member === target.id)
            room.default_member = room.members.find((item: Record<string, any>) => !item.removed)?.id ?? null;
          if (!room.default_member && !target.removed) room.default_member = target.id;
          room.revision++;
        } else if (command.operation === "sessions") {
          emit({ type: "collaboration_state", request_id: command.cmd_id, data: {
            sessions_spec: command.member,
            sessions: command.member.engine === "dsh" ? [
              { sid: "dsh@session-711d960d-3a17-491f-8b66-32b3a6e0a946", native_sid: "session-711d960d-3a17-491f-8b66-32b3a6e0a946", name: "资料核对", cwd: "/tmp/project", state: "idle" },
              { sid: "dsh@session-68a304ab-22b0-40d9-9edb-b553c3a97b77", native_sid: "session-68a304ab-22b0-40d9-9edb-b553c3a97b77", name: "交叉核对", cwd: "/tmp/review", state: "idle" },
            ] : [
              { sid: "01a0d871-implementation", native_sid: "01a0d871-implementation", name: "代码实现", cwd: "/tmp/project", state: "idle" },
              { sid: "01a0d15f-review", native_sid: "01a0d15f-review", name: "交叉审查", cwd: "/tmp/review", state: "running" },
            ],
          } });
        } else if (command.operation === "catalog") {
          const model = command.member.profile_id === "primary" ? "profile-model" : "native-model";
          const response = {
            type: "collaboration_state",
            request_id: command.cmd_id,
            data: {
              catalog_spec: command.member,
              catalog: {
                type: "models",
                engine: command.member.engine,
                models: [
                  {
                    id: model,
                    display_name: model === "profile-model" ? "Profile model" : "Native model",
                    description: "",
                    efforts: ["high", "max"],
                    service_tiers: [{ id: "priority", name: "快速" }],
                  },
                ],
                default_model: model,
                dsh_permissions: [{ value: "ask", name: "逐次确认", description: "" }],
                dsh_presets: [{ id: "native-agent", name: "Native Agent", available: true,
                  description: "", is_default: false },
                  { id: "unavailable-agent", name: "Unavailable Agent", available: false,
                    description: "", is_default: false }],
              },
              permission_profiles: {
                type: "permission_profiles",
                profiles: [{ id: "workspace-write", allowed: true }, { id: "read-only", allowed: false }],
              },
            },
          };
          if (catalogDelayMs) setTimeout(() => emit(response), catalogDelayMs);
          else emit(response);
          catalogDelayMs = 0;
        } else if (command.operation === "answer") {
          finishQuestion(command.member_id, command.ask_id, command.answers ?? [command.answer]);
        } else if (command.operation === "send" && room) {
          if (dropSend) {
            dropSend = false;
            return;
          }
          if (messages.some((message) => message.id === command.message_id)) {
            state(command.cmd_id);
            return;
          }
          const questionMessage = command.ask_id && messages.find(message => message.id === command.ask_id);
          if (questionMessage) {
            questionMessage.question.status = "submitting";
            delete questionMessage.question.error;
            if (holdAsyncAnswers) heldAsyncAnswers.push(command);
            else finishAsync(command);
          }
          messages.push({
            id: command.message_id,
            seq: messages.length + 1,
            role: "user",
            member_id: null,
            text: command.prompt,
            created: 1,
            targets: command.targets,
            reply_to: questionMessage ? [questionMessage.id] : [],
            source: questionMessage ? `async-answer:${questionMessage.member_id}:${command.message_id}` : null,
          });
          for (const target of command.targets.length ? command.targets : [room.default_member]) {
            const deliveryId = `delivery-${deliveries.length}`;
            deliveries.push({ id: deliveryId, message_id: command.message_id, member_id: target,
              status: questionMessage && holdAsyncAnswers ? "queued" : "running", mode: "query",
              error: null, created: 1, updated: 1 });
            if (questionMessage) {
              questionMessage.question.answer_delivery = deliveryId;
              questionMessage.question.answers = questionMessage.question.questions.map((page: Record<string, any>) =>
                command.prompt.split(`问题：${page.question}\n回答：`)[1]?.split("\n\n问题：")[0] ?? "");
            }
          }
        } else if (command.operation === "stop") {
          for (const delivery of deliveries.filter(
            (item) => item.member_id === command.member_id,
          ))
            delivery.status = "cancelled";
        }
        if (command.operation === "send" && pauseSendState)
          pauseSendState = false;
        else if (!["catalog", "sessions"].includes(command.operation)
          && !(pauseRoomLoads && ["list", "read"].includes(command.operation))) {
          if (command.operation === "read" && command.before && pauseHistory) heldHistory = command;
          else state(command.cmd_id, true, command.before, command.ask_id);
        }
      } else if (command.type === "ping") emit({ type: "pong", n: command.n });
      if (command.cmd_id)
        emit({
          type: "command_ack",
          client_id: command.client_id,
          cmd_id: command.cmd_id,
        });
    });
  });
  return {
    commands,
    ask(memberId: string, batch = false, askId = `ask-${memberId}`) {
      const target = room!.members.find((member: Record<string, any>) => member.id === memberId)!;
      const pages = [{ question: `${target.name} 选择工作目录？`, header: "工作目录",
        options: [{ label: "项目目录" }, { label: "临时目录" }], allow_text: true }];
      if (batch) pages.push({ ...pages[0], question: "使用哪种验证方式？", header: "验证方式",
        options: [{ label: "运行回归" }, { label: "只读检查" }] });
      const question = { type: "ask_user", ask_id: askId, ...pages[0], ...(batch ? { questions: pages } : {}) };
      pendingQuestions.push({ member_id: memberId, sid: target.sid, question });
      messages.push({ id: `question-${memberId}`, seq: messages.length + 1, role: "assistant", member_id: memberId,
        text: pages.map(page => page.question).join("\n"), created: 1, targets: [], reply_to: [],
        question: { kind: "native", ask_id: question.ask_id, status: "pending", questions: pages } });
      emit({ ...question, sid: target.sid });
      state();
    },
    askAsync(memberId: string, batch = false, id = "async-question", nativeId = `native-${id}`) {
      const title = "异步确认工作目录？";
      const questions = [{ title, options: ["项目目录", "临时目录"] }];
      if (batch) questions.push({ title: "验收需要截图吗？", options: ["需要", "暂时不用"] });
      messages.push({ id, seq: messages.length + 1, role: "assistant", member_id: memberId,
        text: title, created: 1, targets: [], reply_to: [], question: {
          kind: "async", ask_id: nativeId, status: "pending", questions: questions.map(question => ({
            question: question.title, options: question.options.map(label => ({ label })), allow_text: true,
          })),
        } });
      nativeHistory = command => ({ detail: "full", authoritative: true, in_progress: false,
        oldest_id: "question-turn", newest_id: "question-turn", events: [
          { type: "user_msg", sid: command.session_id, msg_id: "question-turn", prompt: "请检查项目", ts: 1 },
          { type: "assistant_msg_start", sid: command.session_id, message_id: "native-async-question", channel: "commentary", ts: 2 },
          { type: "delta", sid: command.session_id, message_id: "native-async-question", channel: "commentary", text: title, ts: 3 },
          { type: "assistant_msg_end", sid: command.session_id, message_id: "native-async-question", channel: "commentary",
            delivery: "async", questions, ts: 4 },
          { type: "turn_end", sid: command.session_id, turn_id: "question-turn", ts: 5,
            result: { subtype: "success", is_error: false } },
          ...nativeAsyncAnswers.filter(answer => answer.sid === command.session_id).flatMap(answer => [
            { type: "user_msg", sid: command.session_id, msg_id: answer.id, client_msg_id: answer.id, prompt: answer.prompt, ts: 6 },
            { type: "turn_end", sid: command.session_id, turn_id: answer.id, ts: 7,
              result: { subtype: "success", is_error: false } },
          ]),
        ] });
      state();
    },
    holdAsyncAnswers() { holdAsyncAnswers = true; },
    releaseAsyncAnswers() {
      holdAsyncAnswers = false;
      for (const command of heldAsyncAnswers.splice(0)) finishAsync(command);
      state();
    },
    failAsyncAnswer() {
      const command = heldAsyncAnswers.shift()!;
      const delivery = deliveries.find(delivery => delivery.message_id === command.message_id)!;
      delivery.status = "failed";
      delivery.error = "回答未送达，请重试。";
      messages.find(message => message.id === command.ask_id)!.question.status = "pending";
      state();
    },
    failAnsweredAsync(id = "async-question") {
      const message = messages.find(message => message.id === id)!;
      const delivery = deliveries.find(delivery => delivery.id === message.question.answer_delivery)!;
      delivery.status = "failed";
      delivery.error = "原生回答回合未完成";
      message.question.status = "pending";
      message.question.error = delivery.error;
      state();
    },
    queueAsyncFromAnotherBrowser() {
      const message = messages.find(message => message.id === "async-question")!;
      const prompt = '补充回答：\n\n问题 ID："native-async-question"\n\n问题：异步确认工作目录？\n回答：另一端提交的回答';
      message.question.status = "submitting";
      message.question.answer_delivery = "other-browser-delivery";
      message.question.answers = ["另一端提交的回答"];
      messages.push({ id: "other-browser-answer", seq: messages.length + 1, role: "user", member_id: null,
        text: prompt, source: `async-answer:${message.member_id}:other-browser-answer`,
        created: 1, targets: [message.member_id], reply_to: [message.id] });
      deliveries.push({ id: "other-browser-delivery", message_id: "other-browser-answer", member_id: message.member_id,
        status: "queued", mode: "steer", error: null, created: 1, updated: 1 });
      heldAsyncAnswers.push({ ask_id: message.id, message_id: "other-browser-answer", prompt });
      state();
    },
    setNativeHistory(value: NativeHistory) { nativeHistory = value; },
    renameFromHarness(memberId: string, name: string) {
      const target = room?.members.find((member: Record<string, any>) => member.id === memberId);
      if (target) { target.name = name; target.spec.name = name; }
      state();
    },
    reply(memberId: string, text: string) {
      messages.push({ id: `answer-${messages.length}`, seq: messages.length + 1,
        role: "assistant", member_id: memberId, text, created: 1, targets: [], reply_to: [] });
      state();
    },
    seedHistory(count: number) {
      messages.splice(0, messages.length, ...Array.from({ length: count }, (_, index) => ({
        id: `history-${index + 1}`, seq: index + 1, role: "user", member_id: null,
        text: `历史消息 ${index + 1}\n${"用于阅读位置校验的历史正文。".repeat(6)}`,
        created: 1, targets: [], reply_to: [],
      })));
      state();
    },
    holdHistory() { pauseHistory = true; },
    releaseHistory() {
      pauseHistory = false;
      if (heldHistory) state(heldHistory.cmd_id, true, heldHistory.before);
      heldHistory = null;
    },
    failHistory() {
      if (heldHistory) emit({ type: "collaboration_state", request_id: heldHistory.cmd_id,
        room_id: room?.id, error: "历史读取失败", data: {} });
      heldHistory = null;
    },
    emit: (event: Record<string, any>) => emit(event),
    rejectNextOperation(operation: string, message = "原生会话重命名暂未完成，请重试。") {
      rejectedOperation = { operation, message };
    },
    setDirectoryEntries(count: number) { directoryEntries = count; },
    state,
    pauseRoomLoads() {
      pauseRoomLoads = true;
    },
    resumeRoomLoads() {
      pauseRoomLoads = false;
      state(undefined, false);
      state();
    },
    pauseNextSendResponse() {
      pauseSendState = true;
    },
    dropNextSend() {
      dropSend = true;
    },
    delayNextCatalogResponse() {
      catalogDelayMs = 1000;
    },
    finish(memberId = "member-1") {
      for (const delivery of deliveries) delivery.status = "succeeded";
      messages.push({
        id: "answer",
        seq: messages.length + 1,
        role: "assistant",
        member_id: memberId,
        text: "## 已完成\n\n回归通过。",
        created: 1,
        targets: [],
        reply_to: messages
          .filter((item) => item.role === "user")
          .map((item) => item.id),
      });
      state();
    },
  };
}

async function addNewAgent(page: Page) {
  await page.getByRole("button", { name: "添加 Agent", exact: true }).click();
  await page.getByRole("menu", { name: "添加 Agent", exact: true })
    .getByRole("menuitem", { name: "新建 Agent", exact: true }).click();
}

async function createSimpleRoom(page: Page, name = "交互回归") {
  await page.goto("/");
  await page.getByRole("button", { name: "切换新会话引擎", exact: true }).click();
  await page.getByRole("menuitem", { name: "协作", exact: true }).click();
  const create = page.getByRole("region", { name: "新建聊天", exact: true });
  await create.getByLabel("聊天名称").fill(name);
  await create.getByRole("button", { name: "创建聊天", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "协作消息", exact: true })).toBeVisible();
}

async function selectChoice(page: Page, label: string, option: string) {
  await page.getByRole("button", { name: label, exact: true }).click();
  await page.getByRole("menu", { name: label, exact: true })
    .getByRole("menuitemradio", { name: option, exact: true }).click();
}

async function createQuestionRoom(page: Page, identity: string) {
  await page.goto("/");
  await page.getByRole("button", { name: "切换新会话引擎", exact: true }).click();
  await page.getByRole("menuitem", { name: "协作", exact: true }).click();
  await selectChoice(page, "选择参与者 1", identity);
  const create = page.getByRole("region", { name: "新建聊天", exact: true });
  await create.getByLabel("聊天名称").fill("问答状态回归");
  await create.getByRole("button", { name: "创建聊天", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "协作消息", exact: true })).toBeVisible();
}

async function openQuestionSession(page: Page) {
  await page.getByRole("button", { name: "管理成员", exact: true }).click();
  await page.getByRole("dialog", { name: "聊天成员", exact: true })
    .getByRole("button", { name: /^查看 .* 的原生会话$/ }).first().click();
}

for (const width of [320, 390, 1280]) {
  test(`collaboration keeps the Claude theme and three shared settings at ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: width < 980 ? 850 : 900 });
    const relay = await mockRooms(page);
    await createQuestionRoom(page, "Codex · iris");
    await expect(page.locator("html")).toHaveAttribute("data-engine", "claude");
    await expect(page.locator(".collab-pane")).toHaveCSS("background-color", "rgb(27, 26, 23)");
    expect(await page.evaluate(() => localStorage.getItem("cc_remote_engine"))).toBe("codex");
    const more = page.getByRole("button", { name: "更多设置", exact: true });
    const bounds = (await more.boundingBox())!;
    expect(bounds.x + bounds.width).toBeLessThanOrEqual(width);
    await more.click();
    const menu = page.getByRole("dialog", { name: "应用设置", exact: true });
    await expect(menu.locator(".header-menu-items b")).toHaveText(["通知", "外观", "退出登录"]);
    await expect(menu.getByRole("button")).toHaveCount(3);
    await page.screenshot({ path: info.outputPath(`collaboration-settings-${width}.png`), animations: "disabled" });
    await menu.getByRole("button", { name: /^通知/ }).click();
    await expect(menu.getByRole("radiogroup", { name: "通知隐私模式", exact: true })).toBeVisible();
    await menu.getByRole("radio", { name: /^关闭/ }).click();
    await expect(menu.getByRole("radio", { name: /^关闭/ })).toHaveAttribute("aria-checked", "true");
    await menu.getByRole("button", { name: "返回设置", exact: true }).click();
    await menu.getByRole("button", { name: /^外观/ }).click();
    const appearance = page.getByRole("dialog", { name: "外观", exact: true });
    await expect(appearance.locator(".theme-picker-intro span")).toHaveText("Claude");
    await appearance.getByRole("button", { name: "浅色", exact: true }).click();
    await expect(page.locator(".collab-pane")).toHaveCSS("background-color", "rgb(247, 245, 239)");
    await appearance.getByRole("button", { name: "完成", exact: true }).click();
    await page.getByRole("button", { name: "切换新会话引擎", exact: true }).click();
    await page.getByRole("menuitemradio", { name: "Codex", exact: true }).click();
    await expect(page.locator("html")).toHaveAttribute("data-engine", "codex");
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    await page.getByRole("button", { name: "切换新会话引擎", exact: true }).click();
    await page.getByRole("menuitem", { name: "协作", exact: true }).click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    await more.click();
    await expect(menu.locator(".header-menu-items b")).toHaveText(["通知", "外观", "退出登录"]);
    let logoutRequests = 0;
    await page.route("**/api/logout", route => { logoutRequests++; return route.fulfill({ json: {} }); });
    await menu.getByRole("button", { name: /^退出登录/ }).click();
    await expect(page.getByRole("textbox", { name: "协作消息", exact: true })).toHaveCount(0);
    await expect(page.locator(".login")).toBeVisible();
    expect(logoutRequests).toBe(1);
    expect(relay.commands.filter(command => ["query", "steer", "interrupt"].includes(command.type))).toEqual([]);
  });
}

for (const width of [390, 1280]) {
  test(`collaboration restores cached room history and reading position at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const relay = await mockRooms(page);
    await createSimpleRoom(page, "房间缓存回归");
    relay.seedHistory(80);
    const conversation = page.getByRole("region", { name: "协作对话记录", exact: true });
    await expect(page.locator('[data-message-id="history-80"]')).toBeAttached();
    await conversation.evaluate(node => { node.scrollTop = 0; node.dispatchEvent(new Event("scroll")); });
    await expect(page.locator('[data-message-id="history-1"]')).toBeAttached();
    const composer = page.getByRole("textbox", { name: "协作消息", exact: true });
    await composer.fill("保留切换前的房间草稿");
    const top = await conversation.evaluate(node => {
      node.scrollTop = 1200;
      node.dispatchEvent(new Event("scroll"));
      return node.scrollTop;
    });
    await page.getByRole("button", { name: "切换新会话引擎", exact: true }).click();
    await page.getByRole("menuitemradio", { name: "Codex", exact: true }).click();
    await expect(conversation).toHaveCount(0);
    relay.pauseRoomLoads();
    relay.reply("member-0", "切换期间的新回复");
    await page.getByRole("button", { name: "切换新会话引擎", exact: true }).click();
    await page.getByRole("menuitem", { name: "协作", exact: true }).click();
    await expect(page.locator('[data-message-id="history-1"]')).toBeAttached();
    await expect(page.getByText("加载会话历史…", { exact: true })).toHaveCount(0);
    await expect(conversation).toHaveAttribute("aria-busy", "false");
    await expect.poll(() => conversation.evaluate(node => node.scrollTop)).toBeCloseTo(top, 0);
    await expect(composer).toHaveValue("保留切换前的房间草稿");
    await expect(page.getByRole("button", { name: "发送消息", exact: true })).toBeDisabled();
    await composer.press("Enter");
    expect(relay.commands.filter(command => command.type === "collaboration" && command.operation === "send")).toEqual([]);
    await expect(page.getByText("切换期间的新回复", { exact: true })).toHaveCount(0);
    relay.resumeRoomLoads();
    await expect(page.getByText("切换期间的新回复", { exact: true })).toBeAttached();
    await expect(page.getByRole("button", { name: "发送消息", exact: true })).toBeEnabled();
    await expect(page.locator('[data-message-id="history-1"]')).toBeAttached();
    expect(relay.commands.filter(command => ["query", "steer", "interrupt"].includes(command.type))).toEqual([]);
  });
}

test("collaboration cache keeps a closed native question retired during background validation", async ({ page }) => {
  const relay = await mockRooms(page);
  await createQuestionRoom(page, "Codex · iris");
  relay.ask("member-0");
  let question = page.getByRole("dialog", { name: "操作确认", exact: true });
  await question.getByRole("button", { name: "收起问题", exact: true }).click();
  await openQuestionSession(page);
  question = page.getByRole("dialog", { name: "操作确认", exact: true });
  await question.getByRole("button", { name: "项目目录", exact: true }).click();
  await expect(question).toHaveCount(0);
  relay.pauseRoomLoads();
  await page.getByRole("button", { name: "切换新会话引擎", exact: true }).click();
  await page.getByRole("menuitem", { name: "协作", exact: true }).click();
  await expect(page.locator(".collab-questions")).toHaveCount(0);
  await expect(question).toHaveCount(0);
  relay.resumeRoomLoads();
  await expect(page.locator('[data-question-id="question-member-0"]')).toHaveCount(0);
  await expect(page.locator(".collab-thread")).not.toContainText("补充回答");
  await expect(question).toHaveCount(0);
});

for (const engine of ["claude", "codex", "dsh"] as const) {
  for (const surface of ["collaboration", "native"]) {
    test(`collaboration native ${engine} question stays shared when answered in ${surface}`, async ({ page }) => {
      const relay = await mockRooms(page);
      const label = { claude: "Claude", codex: "Codex", dsh: "DSH" }[engine];
      const identity = engine === "codex" ? "Codex · iris" : `${label} · default`;
      await createQuestionRoom(page, identity);
      relay.renameFromHarness("member-0", "问答会话甲");
      const batch = engine !== "codex";
      relay.ask("member-0", batch);
      let question = page.getByRole("dialog", { name: "操作确认", exact: true });
      await expect(question).toHaveCount(1);
      await expect(question).toContainText(`${label} 想确认一下`);
      await expect(question.locator(".qa-source")).toContainText(`问答会话甲 · ${identity} · native-0`);
      if (surface === "native") {
        await question.getByRole("button", { name: "收起问题", exact: true }).click();
        await openQuestionSession(page);
        question = page.getByRole("dialog", { name: "操作确认", exact: true });
        await expect(question).toHaveCount(1);
        await expect(question).toContainText("问答会话甲 选择工作目录？");
      }
      await question.getByRole("button", { name: "项目目录", exact: true }).click();
      if (batch) {
        await question.getByRole("button", { name: "下一题", exact: false }).click();
        await question.getByRole("button", { name: "运行回归", exact: true }).click();
        await question.getByRole("button", { name: "提交回答", exact: true }).click();
      }
      await expect(page.getByRole("dialog", { name: "操作确认", exact: true })).toHaveCount(0);
      const submitted = relay.commands.filter(command => surface === "native"
        ? command.type === "answer_question" : command.type === "collaboration" && command.operation === "answer");
      expect(submitted).toHaveLength(1);
      expect(submitted[0]).toMatchObject({ ask_id: "ask-member-0",
        ...(surface === "native" ? { sid: engine === "dsh" ? "dsh@native-0" : "native-0" } : { member_id: "member-0" }),
        ...(batch ? { answers: ["项目目录", "运行回归"] } : { answer: "项目目录" }),
      });
      if (surface === "collaboration") {
        await expect(page.locator('[data-question-id="question-member-0"]')).toHaveCount(0);
        await expect(page.locator(".collab-thread")).not.toContainText("补充回答");
        await openQuestionSession(page);
        await expect(page.getByRole("textbox", { name: "协作消息", exact: true })).toHaveCount(0);
        await expect(page.getByRole("dialog", { name: "操作确认", exact: true })).toHaveCount(0);
      }
      await page.getByRole("button", { name: "切换新会话引擎", exact: true }).click();
      await page.getByRole("menuitem", { name: "协作", exact: true }).click();
      await expect(page.locator('[data-question-id="question-member-0"]')).toHaveCount(0);
      await expect(page.locator(".collab-thread")).not.toContainText("补充回答");
      await expect(page.getByRole("dialog", { name: "操作确认", exact: true })).toHaveCount(0);
    });
  }
}

test("collaboration concurrent questions keep one dialog and identify every native session", async ({ page }, info) => {
  const relay = await mockRooms(page);
  await page.goto("/");
  await page.getByRole("button", { name: "切换新会话引擎", exact: true }).click();
  await page.getByRole("menuitem", { name: "协作", exact: true }).click();
  await addNewAgent(page);
  await selectChoice(page, "选择参与者 2", "Codex · iris");
  await addNewAgent(page);
  await selectChoice(page, "选择参与者 3", "DSH · default");
  const create = page.getByRole("region", { name: "新建聊天", exact: true });
  await create.getByLabel("聊天名称").fill("并发问答");
  await create.getByRole("button", { name: "创建聊天", exact: true }).click();
  relay.ask("member-1");
  const question = page.getByRole("dialog", { name: "操作确认", exact: true });
  await expect(question.locator(".qa-source")).toContainText("Codex · iris");
  relay.ask("member-0");
  relay.ask("member-2", true);
  await expect(page.locator(".question-dialog")).toHaveCount(1);
  await expect(question.locator(".qa-source")).toContainText("Codex · iris");
  await question.getByRole("button", { name: "项目目录", exact: true }).click();
  await expect(question.locator(".qa-source")).toContainText("Claude · default");
  await question.getByRole("button", { name: "临时目录", exact: true }).click();
  await expect(question.locator(".qa-source")).toHaveText("DSH · default · native-2");
  await expect(question).toContainText("DSH 想确认一下");
  await expect(page.locator(".question-dialog")).toHaveCount(1);
  const bounds = (await question.boundingBox())!;
  expect(bounds.x).toBeGreaterThanOrEqual(0);
  expect(bounds.y).toBeGreaterThanOrEqual(0);
  expect(bounds.x + bounds.width).toBeLessThanOrEqual(page.viewportSize()!.width + 1);
  expect(bounds.y + bounds.height).toBeLessThanOrEqual(page.viewportSize()!.height + 1);
  await page.screenshot({ path: info.outputPath("concurrent-dsh-question.png"), animations: "disabled" });
});

for (const batch of [false, true]) {
  test(`collaboration question keeps its ${batch ? "batch" : "single"} answer after a failed submission`, async ({ page }) => {
    const relay = await mockRooms(page);
    await createQuestionRoom(page, "DSH · default");
    relay.ask("member-0", batch);
    const question = page.getByRole("dialog", { name: "操作确认", exact: true });
    relay.rejectNextOperation("answer", "房间回答记录尚未确认，请重试提交。");
    await question.getByRole("button", { name: "项目目录", exact: true }).click();
    if (batch) {
      await question.getByRole("button", { name: "下一题", exact: false }).click();
      await question.getByRole("button", { name: "运行回归", exact: true }).click();
      await question.getByRole("button", { name: "提交回答", exact: true }).click();
    }
    await expect(question.getByRole("alert")).toHaveText("房间回答记录尚未确认，请重试提交。");
    await expect(question).toBeVisible();
    if (batch) {
      await expect(question.getByRole("button", { name: "运行回归", exact: true })).toHaveAttribute("aria-pressed", "true");
      await question.getByRole("button", { name: "提交回答", exact: true }).click();
    } else await question.getByRole("button", { name: "项目目录", exact: true }).click();
    await expect(question).toHaveCount(0);
    await expect(page.locator('[data-question-id="question-member-0"]')).toHaveCount(0);
    await expect(page.locator(".collab-thread")).not.toContainText("补充回答");
    const submitted = relay.commands.filter(command => command.type === "collaboration" && command.operation === "answer");
    expect(submitted).toHaveLength(2);
    expect(submitted[0].ask_id).toBe(submitted[1].ask_id);
  });
}

test("collaboration same-account questions with equal native IDs stay scoped to their sessions", async ({ page }) => {
  const relay = await mockRooms(page);
  await page.goto("/");
  await page.getByRole("button", { name: "切换新会话引擎", exact: true }).click();
  await page.getByRole("menuitem", { name: "协作", exact: true }).click();
  await selectChoice(page, "选择参与者 1", "DSH · default");
  await addNewAgent(page);
  await selectChoice(page, "选择参与者 2", "DSH · default");
  const create = page.getByRole("region", { name: "新建聊天", exact: true });
  await create.getByLabel("聊天名称").fill("同账号多会话提问");
  await create.getByRole("button", { name: "创建聊天", exact: true }).click();
  relay.renameFromHarness("member-0", "会话甲");
  relay.renameFromHarness("member-1", "会话乙");
  relay.ask("member-1", false, "same-native-question");
  const question = page.getByRole("dialog", { name: "操作确认", exact: true });
  await expect(question.locator(".qa-source")).toContainText("会话乙 · DSH · default · native-1");
  relay.ask("member-0", false, "same-native-question");
  await expect(question.locator(".qa-source")).toContainText("会话乙 · DSH · default · native-1");
  await question.getByRole("button", { name: "项目目录", exact: true }).click();
  await expect(page.locator('[data-question-id="question-member-1"]')).toHaveCount(0);
  await expect(page.locator('[data-question-id="question-member-0"]')).toHaveCount(0);
  await expect(question.locator(".qa-source")).toContainText("会话甲 · DSH · default · native-0");
});

for (const surface of ["collaboration", "native"]) {
  test(`collaboration async question answers synchronize with ${surface}`, async ({ page }) => {
    const relay = await mockRooms(page);
    await createQuestionRoom(page, "Codex · iris");
    relay.askAsync("member-0");
    let dialog = page.getByRole("dialog", { name: "助手询问", exact: true });
    await expect(dialog).toBeVisible();
    await expect(dialog.locator(".async-question-source")).toContainText("Codex · iris");
    if (surface === "collaboration") {
      await dialog.getByRole("radio", { name: "项目目录", exact: true }).check();
      await dialog.getByRole("button", { name: /发送回答/ }).click();
      await expect(dialog).toHaveCount(0);
      expect(relay.commands.findLast(command => command.type === "collaboration" && command.operation === "send"))
        .toMatchObject({ ask_id: "async-question", targets: ["member-0"],
          prompt: '补充回答：\n\n问题 ID："native-async-question"\n\n问题：异步确认工作目录？\n回答：项目目录' });
    } else await dialog.getByRole("button", { name: "稍后回答", exact: true }).click();
    await openQuestionSession(page);
    await expect(page.locator(".async-question-card")).toBeVisible();
    if (surface === "native") {
      await page.locator(".async-question-card").click();
      dialog = page.getByRole("dialog", { name: "助手询问", exact: true });
      await dialog.getByRole("radio", { name: "项目目录", exact: true }).check();
      await dialog.getByRole("button", { name: /发送回答/ }).click();
      await expect(dialog).toHaveCount(0);
    }
    await expect(page.locator(".async-question-entry-state")).toContainText("已回答");
    await page.getByRole("button", { name: "切换新会话引擎", exact: true }).click();
    await page.getByRole("menuitem", { name: "协作", exact: true }).click();
    await expect(page.locator('[data-question-id="async-question"]')).toHaveCount(0);
    await expect(page.locator(".collab-questions")).toHaveCount(0);
    await expect(page.locator(".collab-thread")).not.toContainText("补充回答");
  });
}

test("collaboration async question stays outside the transcript and resumes above the composer", async ({ page }) => {
  const relay = await mockRooms(page);
  await createQuestionRoom(page, "Codex · iris");
  relay.reply("member-0", "我继续检查项目，稍后补充结果。");
  relay.askAsync("member-0");
  const dialog = page.getByRole("dialog", { name: "助手询问", exact: true });
  await expect(dialog).toBeVisible();
  await expect(page.locator('.collab-thread [data-message-id="async-question"]')).toHaveCount(0);
  await dialog.getByRole("button", { name: "稍后回答", exact: true }).click();
  const resume = page.getByRole("button", { name: /Codex · iris.*继续回答/ });
  await expect(resume).toBeVisible();
  await expect(page.locator(".collab-thread .collab-question-entry")).toHaveCount(0);
  await page.getByRole("textbox", { name: "协作消息", exact: true }).fill("继续检查");
  await resume.click();
  await dialog.getByRole("radio", { name: "项目目录", exact: true }).check();
  await dialog.getByRole("button", { name: /发送回答/ }).click();
  await expect(dialog).toHaveCount(0);
  await expect(resume).toHaveCount(0);
  await expect(page.locator(".collab-thread")).not.toContainText("补充回答");
  await expect(page.locator(".collab-thread")).not.toContainText("异步确认工作目录");
  await expect(page.locator(".collab-thread")).toContainText("我继续检查项目");
});

for (const width of [390, 1280]) {
  test(`collaboration async question shows the complete native form at ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: width < 980 ? 844 : 900 });
    const relay = await mockRooms(page);
    await createQuestionRoom(page, "Codex · primary");
    const composer = page.getByRole("textbox", { name: "协作消息", exact: true });
    await composer.fill("先检查接口实现，范围和验收要求向我确认。");
    await page.getByRole("button", { name: "发送消息", exact: true }).click();
    relay.renameFromHarness("member-0", "接口检查");
    relay.askAsync("member-0", true);
    const dialog = page.getByRole("dialog", { name: "助手询问", exact: true });
    await expect(dialog.locator("fieldset")).toHaveCount(2);
    await expect(dialog.getByText("验收需要截图吗？", { exact: true })).toBeVisible();
    await expect(dialog.getByRole("button", { name: /下一题/ })).toHaveCount(0);
    await expect(dialog.locator(".async-question-source")).toHaveText("接口检查 · Codex · primary · native-0");
    expect(await page.evaluate(() => document.activeElement?.tagName)).not.toBe("TEXTAREA");
    const bounds = (await dialog.boundingBox())!;
    expect(bounds.x).toBeGreaterThanOrEqual(0);
    expect(bounds.y).toBeGreaterThanOrEqual(0);
    expect(bounds.x + bounds.width).toBeLessThanOrEqual(width);
    expect(bounds.y + bounds.height).toBeLessThanOrEqual(page.viewportSize()!.height + 1);
    await page.screenshot({ path: info.outputPath(`async-form-${width}.png`), animations: "disabled" });
    await dialog.getByRole("button", { name: "稍后回答", exact: true }).click();
    const resume = page.locator(".collab-question-entry");
    await expect(resume).toContainText("2 个问题待回答");
    await expect.poll(() => resume.locator(".engine-brand-icon").evaluate(node =>
      (node as SVGGraphicsElement).getBBox().width)).toBeGreaterThan(0);
    expect(await resume.evaluate(node => node.closest('[aria-label="协作对话记录"]') === null)).toBe(true);
    const entry = (await resume.boundingBox())!;
    const input = (await page.locator(".collab-input").boundingBox())!;
    expect(entry.y + entry.height).toBeLessThanOrEqual(input.y);
    expect(entry.height).toBeLessThanOrEqual(64);
    await page.screenshot({ path: info.outputPath(`async-later-${width}.png`), animations: "disabled" });
    await resume.click();
    await dialog.getByRole("radio", { name: "临时目录", exact: true }).check();
    await dialog.getByRole("radio", { name: "暂时不用", exact: true }).check();
    await dialog.getByRole("button", { name: /发送回答/ }).click();
    await expect(dialog).toHaveCount(0);
    await expect(resume).toHaveCount(0);
    relay.reply("member-0", "收到，我先在临时目录检查，完成后说明结果。");
    await expect(page.locator(".collab-thread")).not.toContainText("补充回答");
    await expect(page.locator(".collab-thread")).not.toContainText("异步确认工作目录");
    await page.screenshot({ path: info.outputPath(`async-answered-${width}.png`), animations: "disabled" });
  });
}

test("collaboration async question waits for native consumption beyond queue and command acknowledgements", async ({ page }) => {
  const relay = await mockRooms(page);
  await createQuestionRoom(page, "Codex · iris");
  relay.holdAsyncAnswers();
  relay.askAsync("member-0", true);
  const dialog = page.getByRole("dialog", { name: "助手询问", exact: true });
  await dialog.getByRole("textbox").first().fill("检查现有项目，保留工作目录。");
  await dialog.getByRole("button", { name: /发送回答/ }).click();
  await expect(dialog.getByRole("button", { name: /等待确认/ })).toBeDisabled();
  await expect(dialog).toContainText("正在确认回答是否送达");
  await dialog.getByRole("button", { name: "稍后回答", exact: true }).click();
  const resume = page.locator(".collab-question-entry");
  await expect(resume).toContainText("回答确认中");
  await resume.click();
  await expect(dialog.getByRole("textbox").first()).toHaveValue("检查现有项目，保留工作目录。");
  await expect(dialog.getByRole("button", { name: /等待确认/ })).toBeDisabled();
  expect(relay.commands.filter(command => command.operation === "send")).toHaveLength(1);
  relay.releaseAsyncAnswers();
  await expect(dialog).toHaveCount(0);
  await expect(resume).toHaveCount(0);
});

for (const failure of ["command", "delivery"]) {
  test(`collaboration async question retains its draft after ${failure} failure`, async ({ page }) => {
    const relay = await mockRooms(page);
    await createQuestionRoom(page, "Codex · iris");
    relay.askAsync("member-0", true);
    if (failure === "command") relay.rejectNextOperation("send", "回答未送达，请重试。");
    else relay.holdAsyncAnswers();
    const dialog = page.getByRole("dialog", { name: "助手询问", exact: true });
    await dialog.getByRole("textbox").first().fill("保留这份回答草稿");
    await dialog.getByRole("radio", { name: "暂时不用", exact: true }).check();
    await dialog.getByRole("button", { name: /发送回答/ }).click();
    if (failure === "delivery") {
      await expect(dialog.getByRole("button", { name: /等待确认/ })).toBeDisabled();
      relay.failAsyncAnswer();
    }
    await expect(dialog.getByRole("status")).toContainText("回答未送达");
    await expect(dialog.getByRole("textbox").first()).toHaveValue("保留这份回答草稿");
    await expect(dialog.getByRole("radio", { name: "暂时不用", exact: true })).toBeChecked();
    await dialog.getByRole("button", { name: /发送回答/ }).click();
    if (failure === "delivery") relay.releaseAsyncAnswers();
    await expect(dialog).toHaveCount(0);
    const sends = relay.commands.filter(command => command.operation === "send");
    expect(sends).toHaveLength(2);
    expect(sends[0].prompt).toBe(sends[1].prompt);
    expect(sends[0].message_id === sends[1].message_id).toBe(failure === "command");
    await expect(page.locator(".collab-thread")).not.toContainText("补充回答");
  });
}

test("collaboration async question keeps its draft and receipt when switching harnesses", async ({ page }) => {
  const relay = await mockRooms(page);
  await createQuestionRoom(page, "Codex · iris");
  relay.askAsync("member-0");
  const dialog = page.getByRole("dialog", { name: "助手询问", exact: true });
  await dialog.getByRole("textbox").fill("切换后继续回答");
  await dialog.getByRole("button", { name: "稍后回答", exact: true }).click();
  await page.getByRole("button", { name: "切换新会话引擎", exact: true }).click();
  await page.getByRole("menuitemradio", { name: "Codex", exact: true }).click();
  await page.getByRole("button", { name: "切换新会话引擎", exact: true }).click();
  await page.getByRole("menuitem", { name: "协作", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await page.locator(".collab-question-entry").click();
  await expect(dialog.getByRole("textbox")).toHaveValue("切换后继续回答");
  relay.holdAsyncAnswers();
  await dialog.getByRole("button", { name: /发送回答/ }).click();
  await expect(dialog.getByRole("button", { name: /等待确认/ })).toBeDisabled();
  await dialog.getByRole("button", { name: "稍后回答", exact: true }).click();
  await openQuestionSession(page);
  relay.releaseAsyncAnswers();
  await page.getByRole("button", { name: "切换新会话引擎", exact: true }).click();
  await page.getByRole("menuitem", { name: "协作", exact: true }).click();
  await expect(page.locator(".collab-questions")).toHaveCount(0);
  await expect(dialog).toHaveCount(0);
  expect(relay.commands.filter(command => command.operation === "send")).toHaveLength(1);
});

test("collaboration async question reload recovers an old pending tool and an in-flight answer", async ({ page }) => {
  const relay = await mockRooms(page);
  await createQuestionRoom(page, "Codex · iris");
  relay.holdAsyncAnswers();
  relay.askAsync("member-0");
  const dialog = page.getByRole("dialog", { name: "助手询问", exact: true });
  await dialog.getByRole("textbox").fill("刷新后保留原回答");
  await dialog.getByRole("button", { name: /发送回答/ }).click();
  await expect(dialog.getByRole("button", { name: /等待确认/ })).toBeDisabled();
  for (let index = 0; index < 45; index++) relay.reply("member-0", `后续公开消息 ${index + 1}`);
  await page.reload();
  await page.getByRole("button", { name: "切换新会话引擎", exact: true }).click();
  await page.getByRole("menuitem", { name: "协作", exact: true }).click();
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("textbox")).toHaveValue("刷新后保留原回答");
  await expect(dialog.getByRole("button", { name: /等待确认/ })).toBeDisabled();
  expect(relay.commands.filter(command => command.operation === "send")).toHaveLength(1);
  relay.releaseAsyncAnswers();
  await expect(dialog).toHaveCount(0);
  await expect(page.locator(".collab-questions")).toHaveCount(0);
});

test("collaboration async question retries a dropped frame using one input identity", async ({ page }) => {
  const relay = await mockRooms(page);
  await createQuestionRoom(page, "Codex · iris");
  relay.askAsync("member-0");
  relay.dropNextSend();
  const dialog = page.getByRole("dialog", { name: "助手询问", exact: true });
  await dialog.getByRole("button", { name: /发送回答/ }).click();
  await expect(dialog.getByRole("button", { name: /等待确认/ })).toBeDisabled();
  await expect(dialog).toHaveCount(0, { timeout: 12000 });
  const sends = relay.commands.filter(command => command.operation === "send");
  expect(sends.length).toBeGreaterThanOrEqual(2);
  expect(new Set(sends.map(command => command.message_id)).size).toBe(1);
  expect(new Set(sends.map(command => command.prompt)).size).toBe(1);
});

test("collaboration async question retry uses the current tool receipt after older answers leave the visible page", async ({ page }) => {
  const relay = await mockRooms(page);
  await createQuestionRoom(page, "Codex · iris");
  relay.askAsync("member-0");
  const dialog = page.getByRole("dialog", { name: "助手询问", exact: true });
  await dialog.getByRole("button", { name: /发送回答/ }).click();
  await expect(dialog).toHaveCount(0);
  for (let index = 0; index < 45; index++) relay.reply("member-0", `后续公开消息 ${index + 1}`);
  relay.failAnsweredAsync();
  await page.locator(".collab-question-entry").click();
  await dialog.getByRole("textbox").fill("重试时的新回答");
  relay.holdAsyncAnswers();
  await dialog.getByRole("button", { name: /发送回答/ }).click();
  await expect(dialog.getByRole("button", { name: /等待确认/ })).toBeDisabled();
  await dialog.getByRole("button", { name: "稍后回答", exact: true }).click();
  await expect(page.locator(".collab-question-entry")).toContainText("回答确认中");
  relay.releaseAsyncAnswers();
  await expect(page.locator(".collab-questions")).toHaveCount(0);
});

test("collaboration async question observes an answer queued from another browser while its form is open", async ({ page }) => {
  const relay = await mockRooms(page);
  await createQuestionRoom(page, "Codex · iris");
  relay.askAsync("member-0");
  const dialog = page.getByRole("dialog", { name: "助手询问", exact: true });
  await dialog.getByRole("textbox").fill("本地尚未提交的草稿");
  relay.queueAsyncFromAnotherBrowser();
  await expect(dialog.getByRole("button", { name: /等待确认/ })).toBeDisabled();
  await expect(dialog.getByRole("textbox")).toHaveValue("另一端提交的回答");
  expect(relay.commands.filter(command => command.operation === "send")).toHaveLength(0);
  relay.releaseAsyncAnswers();
  await expect(dialog).toHaveCount(0);
  await expect(page.locator(".collab-questions")).toHaveCount(0);
});

test("collaboration async question scopes identical native IDs and ignores a late receipt for another session", async ({ page }) => {
  const relay = await mockRooms(page);
  await page.goto("/");
  await page.getByRole("button", { name: "切换新会话引擎", exact: true }).click();
  await page.getByRole("menuitem", { name: "协作", exact: true }).click();
  await selectChoice(page, "选择参与者 1", "Codex · primary");
  await addNewAgent(page);
  await selectChoice(page, "选择参与者 2", "Codex · primary");
  const create = page.getByRole("region", { name: "新建聊天", exact: true });
  await create.getByLabel("聊天名称").fill("同账号异步问答");
  await create.getByRole("button", { name: "创建聊天", exact: true }).click();
  relay.renameFromHarness("member-0", "接口实现");
  relay.renameFromHarness("member-1", "交叉审查");
  relay.holdAsyncAnswers();
  relay.askAsync("member-1", false, "review-question", "same-native-id");
  const dialog = page.getByRole("dialog", { name: "助手询问", exact: true });
  await expect(dialog.locator(".async-question-source")).toContainText("交叉审查 · Codex · primary · native-1");
  relay.askAsync("member-0", false, "implementation-question", "same-native-id");
  await dialog.getByRole("textbox").fill("请检查现有改动");
  await dialog.getByRole("button", { name: /发送回答/ }).click();
  await expect(dialog.getByRole("button", { name: /等待确认/ })).toBeDisabled();
  await dialog.getByRole("button", { name: "稍后回答", exact: true }).click();
  await page.getByRole("button", { name: /接口实现.*继续回答/ }).click();
  await expect(dialog.locator(".async-question-source")).toContainText("接口实现 · Codex · primary · native-0");
  await dialog.getByRole("textbox").fill("只处理接口实现");
  relay.releaseAsyncAnswers();
  await expect(page.locator(".collab-question-entry")).toHaveCount(0);
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("textbox")).toHaveValue("只处理接口实现");
  await dialog.getByRole("button", { name: /发送回答/ }).click();
  await expect(dialog).toHaveCount(0);
  const sends = relay.commands.filter(command => command.operation === "send");
  expect(sends.map(command => command.targets)).toEqual([["member-1"], ["member-0"]]);
  expect(sends.map(command => command.ask_id)).toEqual(["review-question", "implementation-question"]);
  expect(sends[0].message_id).not.toBe(sends[1].message_id);
});

test("collaboration async question collapse keeps all three harnesses available for an explicit answer order", async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const relay = await mockRooms(page);
  await page.goto("/");
  await page.getByRole("button", { name: "切换新会话引擎", exact: true }).click();
  await page.getByRole("menuitem", { name: "协作", exact: true }).click();
  await addNewAgent(page);
  await selectChoice(page, "选择参与者 2", "Codex · primary");
  await addNewAgent(page);
  await selectChoice(page, "选择参与者 3", "DSH · default");
  const create = page.getByRole("region", { name: "新建聊天", exact: true });
  await create.getByLabel("聊天名称").fill("三个 Harness 同时提问");
  await create.getByRole("button", { name: "创建聊天", exact: true }).click();
  relay.askAsync("member-1", true);
  const asyncDialog = page.getByRole("dialog", { name: "助手询问", exact: true });
  await expect(asyncDialog).toBeVisible();
  await asyncDialog.getByRole("textbox").first().fill("稍后回答 Codex");
  relay.ask("member-0", true);
  relay.ask("member-2", true);
  await asyncDialog.getByRole("button", { name: "收起助手询问", exact: true }).click();
  await expect(page.locator(".question-dialog, .async-question-dialog")).toHaveCount(0);
  await expect(page.locator(".collab-question-entry")).toHaveCount(3);
  expect(relay.commands.filter(command => command.operation === "stop"
    || command.operation === "answer" || command.operation === "send" || command.type === "interrupt")).toEqual([]);
  await expect(page.getByRole("button", { name: /Codex · primary.*2 个问题待回答.*继续回答/ })).toBeVisible();
  await page.screenshot({ path: info.outputPath("three-harnesses-pending.png"), animations: "disabled" });
  await page.getByRole("button", { name: /DSH · default.*继续回答/ }).click();
  const nativeDialog = page.getByRole("dialog", { name: "操作确认", exact: true });
  await expect(nativeDialog.locator(".qa-source")).toContainText("DSH · default");
  await nativeDialog.getByRole("button", { name: "项目目录", exact: true }).click();
  await nativeDialog.getByRole("button", { name: /下一题/ }).click();
  await nativeDialog.getByRole("button", { name: "运行回归", exact: true }).click();
  await nativeDialog.getByRole("button", { name: "提交回答", exact: true }).click();
  await expect(nativeDialog).toHaveCount(0);
  await expect(page.locator(".collab-question-entry")).toHaveCount(2);
  await page.getByRole("button", { name: /Claude · default.*继续回答/ }).click();
  await nativeDialog.getByRole("button", { name: "项目目录", exact: true }).click();
  await nativeDialog.getByRole("button", { name: /下一题/ }).click();
  await nativeDialog.getByRole("button", { name: "运行回归", exact: true }).click();
  await nativeDialog.getByRole("button", { name: "提交回答", exact: true }).click();
  await expect(nativeDialog).toHaveCount(0);
  await page.getByRole("button", { name: /Codex · primary.*继续回答/ }).click();
  await expect(asyncDialog.getByRole("textbox").first()).toHaveValue("稍后回答 Codex");
  await asyncDialog.getByRole("button", { name: /发送回答/ }).click();
  await expect(asyncDialog).toHaveCount(0);
  await expect(page.locator(".collab-questions")).toHaveCount(0);
  expect(relay.commands.filter(command => command.operation === "answer" || command.operation === "send")
    .map(command => command.member_id ?? command.targets[0])).toEqual(["member-2", "member-0", "member-1"]);
  await expect(page.locator(".collab-thread")).not.toContainText("补充回答");
});

for (const kind of ["native", "async"]) {
  test(`collaboration ${kind} question receipt updates beyond the latest history page`, async ({ page }) => {
    const relay = await mockRooms(page);
    await createQuestionRoom(page, "Codex · iris");
    if (kind === "async") relay.askAsync("member-0");
    else relay.ask("member-0");
    const question = page.getByRole("dialog", { name: kind === "async" ? "助手询问" : "操作确认", exact: true });
    await expect(question).toBeVisible();
    for (let index = 0; index < 45; index++) relay.reply("member-0", `后续公开消息 ${index + 1}`);
    await expect(page.locator('[data-message-id="answer-45"]')).toBeAttached();
    if (kind === "async") {
      await question.getByRole("radio", { name: "项目目录", exact: true }).check();
      await question.getByRole("button", { name: /发送回答/ }).click();
    } else await question.getByRole("button", { name: "项目目录", exact: true }).click();
    await expect(question).toHaveCount(0);
    await expect(page.locator(".collab-questions")).toHaveCount(0);
    await expect(page.locator(".collab-thread")).not.toContainText("补充回答");
  });
}

test("collaboration file preview suppresses the dialog focus frame and restores its trigger", async ({ page, isMobile }, info) => {
  const relay = await mockRooms(page);
  await createSimpleRoom(page);
  relay.reply("member-0", "查看 [claude.txt](claude.txt)。");
  const conversation = page.getByRole("region", { name: "协作对话记录", exact: true });
  const file = conversation.getByRole("button", { name: "在 Remote 中打开 /tmp/project/claude.txt", exact: true });
  await expect(file).toBeVisible();
  const beforePreview = relay.commands.length;
  await conversation.focus();
  if (isMobile) await file.tap();
  else await file.click();
  const preview = page.getByRole("dialog", { name: "文件预览", exact: true });
  await expect(preview).toContainText("来自 native-0");
  // The loading fallback or a touch activation can leave focus on the modal
  // container. Keyboard modality must not draw a frame around the whole panel.
  await preview.focus();
  await page.keyboard.press("CapsLock");
  await expect.poll(() => preview.evaluate(node => node.matches(":focus-visible"))).toBe(true);
  await expect(preview).toHaveCSS("outline-style", "none");
  const close = preview.getByRole("button", { name: "收起", exact: true });
  await close.focus();
  await expect(close).toHaveCSS("outline-style", "solid");
  await expect(close).toHaveCSS("outline-width", "2px");
  await close.press("Enter");
  await expect(preview).toHaveCount(0);
  await expect.poll(() => file.evaluate(node => {
    const active = document.activeElement;
    return active === node || active === node.closest(".collab-conversation");
  })).toBe(true);
  await expect(conversation).toHaveCSS("outline-style", "none");
  await file.focus();
  await file.press("Enter");
  await expect(preview).toContainText("来自 native-0");
  await page.keyboard.press("Escape");
  await expect(preview).toHaveCount(0);
  await expect(file).toBeFocused();
  await expect(file).toHaveCSS("outline-style", "solid");
  await expect(file).toHaveCSS("outline-width", "2px");
  expect(relay.commands.slice(beforePreview).filter(command => ["query", "steer", "interrupt", "switch_session", "new_session"].includes(command.type))).toEqual([]);
  await page.screenshot({ path: info.outputPath("file-preview-focus-restored.png") });
});

test("collaboration transcript keyboard focus leaves the reading surface unframed", async ({ page }, info) => {
  const relay = await mockRooms(page);
  await createSimpleRoom(page);
  relay.reply("member-0", "协作正文保持可阅读。\n\n查看 [claude.txt](claude.txt)。");
  const conversation = page.getByRole("region", { name: "协作对话记录", exact: true });
  await page.getByText("协作正文保持可阅读。", { exact: true }).click();
  await expect(conversation).toBeFocused();
  await page.keyboard.press("CapsLock");
  await expect.poll(() => conversation.evaluate(node => node.matches(":focus-visible"))).toBe(true);
  await expect(conversation).toHaveCSS("outline-style", "none");
  const mention = page.getByRole("button", { name: "点名参与者", exact: true });
  await mention.focus();
  await expect(mention).toHaveCSS("outline-style", "solid");
  await expect(mention).toHaveCSS("outline-width", "2px");
  await page.screenshot({ path: info.outputPath("transcript-without-focus-frame.png") });
});

test("collaboration Command+4 preserves native and room drafts without sending inputs", async ({ page }) => {
  const relay = await mockRooms(page);
  await page.addInitScript(() => Object.defineProperty(navigator, "platform", { value: "MacIntel" }));
  await page.goto("/");
  const native = page.locator("textarea[data-chat-composer]");
  await expect(page.locator(".c-head .sub")).toHaveText("session parent");
  await native.fill("原生会话草稿");
  await page.keyboard.press("Meta+4");
  await expect(page.locator(".collab-workspace")).toBeVisible();
  await page.getByRole("button", { name: "切换新会话引擎" }).click();
  const collaboration = page.getByRole("menuitem", { name: "协作", exact: true });
  await expect(collaboration).toHaveAttribute("aria-keyshortcuts", "Meta+4 Control+4");
  await expect(collaboration.locator("kbd")).toHaveText("⌘4");
  await page.keyboard.press("Escape");
  const create = page.getByRole("region", { name: "新建聊天", exact: true });
  await create.getByLabel("聊天名称").fill("快捷切换讨论");
  await page.keyboard.press("Meta+4");
  await expect(create.getByLabel("聊天名称")).toHaveValue("快捷切换讨论");
  await create.getByRole("button", { name: "创建聊天", exact: true }).click();
  const composer = page.getByRole("textbox", { name: "协作消息", exact: true });
  await composer.fill("协作聊天草稿");
  await page.keyboard.press("Meta+2");
  await expect(native).toHaveValue("原生会话草稿");
  await native.focus();
  await page.keyboard.press("Control+4");
  await expect(composer).toHaveValue("协作聊天草稿");
  expect(relay.commands.filter(command => ["query", "steer", "interrupt", "new_session"].includes(command.type))).toEqual([]);
  expect(relay.commands.filter(command => command.operation === "send")).toEqual([]);
});

for (const engine of ["claude", "codex"] as const) {
  test(`collaboration shortcuts leave the hidden ${engine} session untouched`, async ({ page }) => {
    const relay = await mockRooms(page, engine);
    await page.addInitScript(() => Object.defineProperty(navigator, "platform", { value: "MacIntel" }));
    await page.goto("/");
    const native = page.locator("textarea[data-chat-composer]");
    await expect(native).toBeEnabled();
    await native.fill("保留原生草稿");
    await page.keyboard.press("Meta+4");
    const create = page.getByRole("region", { name: "新建聊天", exact: true });
    await create.getByLabel("聊天名称").fill("快捷键隔离回归");
    for (const key of ["Meta+Shift+E", "Control+Shift+K", "Meta+Shift+B", "Shift+Tab"]) {
      await page.keyboard.press(key);
    }
    await create.getByRole("button", { name: "配置参与者 1", exact: true }).click();
    const settings = page.getByRole("dialog", { name: "Claude · default", exact: true });
    await settings.getByLabel("会话名称").focus();
    await page.keyboard.press("Shift+Tab");
    await settings.getByRole("button", { name: "关闭", exact: true }).click();
    await create.getByRole("button", { name: "创建聊天", exact: true }).click();
    const composer = page.getByRole("textbox", { name: "协作消息", exact: true });
    await composer.fill("保留协作草稿");
    for (const key of ["Control+Shift+E", "Meta+Shift+K", "Control+Shift+B", "Shift+Tab"]) {
      await composer.focus();
      await page.keyboard.press(key);
    }
    const nativeOperations = ["open_btw", "close_btw", "get_diff", "set_perm", "set_collaboration_mode"];
    expect.soft(relay.commands.filter(command => nativeOperations.includes(command.type))).toEqual([]);
    await page.keyboard.press(engine === "claude" ? "Meta+1" : "Meta+2");
    await expect(native).toHaveValue("保留原生草稿");
    await expect(page.locator(".btw-panel")).toHaveCount(0);
    await expect(page.locator(".right-panel-frame")).toHaveCount(0);
    await native.focus();
    await page.keyboard.press("Shift+Tab");
    await expect.poll(() => relay.commands.filter(command => command.type === (
      engine === "claude" ? "set_perm" : "set_collaboration_mode"
    )).length).toBe(1);
    await page.keyboard.press("Meta+Shift+E");
    await expect.poll(() => relay.commands.filter(command => command.type === "open_btw").length).toBe(1);
    await expect(page.locator('.btw-panel[aria-modal="true"]')).toBeVisible();
    await page.keyboard.press("Control+4");
    await expect(composer).toHaveValue("保留协作草稿");
    const operationCount = relay.commands.filter(command => nativeOperations.includes(command.type)).length;
    for (const key of ["Meta+Shift+E", "Control+Shift+K", "Shift+Tab"]) {
      await composer.focus();
      await page.keyboard.press(key);
    }
    await page.keyboard.press(engine === "claude" ? "Meta+1" : "Meta+2");
    await expect(page.locator('.btw-panel[aria-modal="true"]')).toBeVisible();
    expect(relay.commands.filter(command => nativeOperations.includes(command.type))).toHaveLength(operationCount);
    expect(relay.commands.filter(command => ["query", "steer", "interrupt", "new_session"].includes(command.type))).toEqual([]);
  });
}

for (const width of [320, 390, 1280]) {
  test(`collaboration flat creation and responder pickers stay usable at ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 844 });
    const relay = await mockRooms(page);
    await page.goto("/");
    await page.getByRole("button", { name: "切换新会话引擎" }).click();
    await page.getByRole("menuitem", { name: "协作", exact: true }).click();
    const create = page.getByRole("region", { name: "新建聊天", exact: true });
    const name = create.getByLabel("聊天名称");
    await name.fill("紧凑成员列表");
    const nameStyle = await name.evaluate(node => {
      const style = getComputedStyle(node);
      return { fontSize: parseFloat(style.fontSize), background: style.backgroundColor,
        borderTop: style.borderTopWidth, borderLeft: style.borderLeftWidth,
        radius: parseFloat(style.borderRadius), height: node.getBoundingClientRect().height };
    });
    expect(nameStyle.fontSize).toBeGreaterThanOrEqual(14);
    expect(nameStyle.fontSize).toBeLessThanOrEqual(16);
    expect(nameStyle.background).not.toBe("rgba(0, 0, 0, 0)");
    expect(nameStyle.borderTop).toBe("1px");
    expect(nameStyle.borderLeft).toBe("1px");
    expect(nameStyle.radius).toBeGreaterThanOrEqual(16);
    expect(nameStyle.height).toBeLessThanOrEqual(48);
    await expect(create.getByText("账号默认配置", { exact: true })).toHaveCount(0);
    await expect(create.getByText("未设置项沿用账号默认配置", { exact: true })).toHaveCount(0);
    await selectChoice(page, "选择参与者 1", "Claude · gaia");
    for (const [index, name] of [[2, "Codex · iris"], [3, "DSH · default"]] as const) {
      await addNewAgent(page);
      await selectChoice(page, `选择参与者 ${index}`, name);
    }
    await expect(create.locator("select")).toHaveCount(0);
    await expect(create.locator(".collab-participant")).toHaveCount(3);
    const row = create.locator(".collab-participant").first();
    expect(await row.evaluate(node => getComputedStyle(node).borderLeftWidth)).toBe("0px");
    expect((await row.boundingBox())!.height).toBeLessThanOrEqual(72);
    const identitySpacing = await row.locator(".collab-participant-identity").evaluate(node =>
      node.getBoundingClientRect().width - node.querySelector("span")!.getBoundingClientRect().width,
    );
    expect.soft(identitySpacing).toBeLessThanOrEqual(36);
    expect((await create.getByRole("button", { name: "添加 Agent", exact: true }).boundingBox())!.width)
      .toBeLessThan(180);
    const createBounds = (await create.boundingBox())!;
    expect(createBounds.x).toBeGreaterThanOrEqual(12);
    expect(createBounds.x + createBounds.width).toBeLessThanOrEqual(width - 12);
    expect(createBounds.y).toBeGreaterThanOrEqual(0);
    expect(createBounds.y + createBounds.height).toBeLessThanOrEqual(844);
    const identity = create.getByRole("button", { name: "选择参与者 2", exact: true });
    await identity.press("ArrowDown");
    const picker = page.getByRole("menu", { name: "选择参与者 2", exact: true });
    await expect(picker.getByRole("menuitemradio", { name: "Codex · iris", exact: true })).toBeFocused();
    await expect(picker).toHaveAttribute("id", (await identity.getAttribute("aria-controls"))!);
    await expect(page.locator(".collab-select-popover")).toHaveCount(1);
    const pickerBounds = (await picker.boundingBox())!;
    expect(pickerBounds.x).toBeGreaterThanOrEqual(8);
    expect(pickerBounds.x + pickerBounds.width).toBeLessThanOrEqual(width - 8);
    expect(pickerBounds.y).toBeGreaterThanOrEqual(0);
    expect(pickerBounds.y + pickerBounds.height).toBeLessThanOrEqual(844);
    await page.keyboard.press("End");
    await expect(picker.getByRole("menuitemradio", { name: "DSH · default", exact: true })).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(picker).toHaveCount(0);
    await expect(identity).toBeFocused();
    await expect(create).toBeVisible();
    await identity.press("ArrowDown");
    await page.keyboard.press("Tab");
    await expect(picker).toHaveCount(0);
    await expect(create.getByRole("button", { name: "工作目录 2", exact: true })).toBeFocused();
    await identity.click();
    await page.mouse.click(4, 4);
    await expect(picker).toHaveCount(0);
    await expect(create.getByLabel("聊天名称")).toHaveValue("紧凑成员列表");
    await page.screenshot({ path: info.outputPath("creation.png") });
    await create.getByRole("button", { name: "创建聊天", exact: true }).click();
    await page.getByRole("button", { name: "管理成员", exact: true }).click();
    const members = page.getByRole("dialog", { name: "聊天成员", exact: true });
    await expect.poll(() => members.locator("svg.engine-brand-icon use").evaluateAll(nodes =>
      nodes.map(node => (node as SVGGraphicsElement).getBBox().width > 0),
    )).toEqual([true, true]);
    const positions = await members.locator(".collab-members > div").evaluateAll(rows =>
      rows.map(row => ({
        icon: row.querySelector(".engine-brand-icon")!.getBoundingClientRect().left,
        label: row.querySelector("strong")!.getBoundingClientRect().left,
      })),
    );
    expect(positions).toHaveLength(3);
    for (const position of positions) {
      expect.soft(position.icon).toBeCloseTo(positions[0].icon, 0);
      expect.soft(position.label).toBeCloseTo(positions[0].label, 0);
    }
    await page.screenshot({ path: info.outputPath("members.png") });
    await members.getByRole("button", { name: "关闭", exact: true }).click();
    await page.getByRole("button", { name: "默认回复者", exact: true }).click();
    const responder = page.getByRole("menu", { name: "默认回复者", exact: true });
    await expect(responder.getByRole("menuitemradio", { name: "Claude · gaia", exact: true })).toHaveAttribute("aria-checked", "true");
    const responderBounds = (await responder.boundingBox())!;
    expect(responderBounds.x).toBeGreaterThanOrEqual(8);
    expect(responderBounds.x + responderBounds.width).toBeLessThanOrEqual(width - 8);
    expect(responderBounds.y).toBeGreaterThanOrEqual(0);
    expect(responderBounds.y + responderBounds.height).toBeLessThanOrEqual(844);
    await expect.poll(() => responder.locator("svg.engine-brand-icon use").evaluateAll(nodes =>
      nodes.map(node => (node as SVGGraphicsElement).getBBox().width > 0),
    )).toEqual([true, true]);
    await page.screenshot({ path: info.outputPath("responder.png") });
    await responder.getByRole("menuitemradio", { name: "Codex · iris", exact: true }).click();
    const trigger = page.getByRole("button", { name: "默认回复者", exact: true });
    await expect(trigger).toHaveText("Codex · iris");
    await expect(trigger).toBeFocused();
    await expect(page.locator(".collab-composer select")).toHaveCount(0);
    const composer = page.getByRole("textbox", { name: "协作消息", exact: true });
    await composer.fill("默认给 Codex");
    await composer.press("Enter");
    await expect(page.locator('.collab-activity[data-engine="codex"]')).toHaveCount(1);
    await page.getByRole("button", { name: "点名参与者", exact: true }).click();
    await expect(composer).toHaveValue("@");
    await expect(composer).toBeFocused();
    await page.getByRole("option", { name: "DSH · default", exact: true }).click();
    await composer.press("End");
    await composer.type("这条给 DSH");
    await composer.press("Enter");
    await expect(trigger).toHaveText("Codex · iris");
    expect(relay.commands.findLast(command => command.operation === "send")!.targets).toEqual(["member-2"]);
    expect(relay.commands.filter(command => command.operation === "update")).toHaveLength(1);
  });
}

test("collaboration settings use account catalogs and clear overrides on identity changes", async ({ page }) => {
  const relay = await mockRooms(page);
  await page.goto("/");
  await page.getByRole("button", { name: "切换新会话引擎" }).click();
  await page.getByRole("menuitem", { name: "协作", exact: true }).click();
  const create = page.getByRole("region", { name: "新建聊天", exact: true });
  await create.getByLabel("聊天名称").fill("按账号配置");
  await create.getByRole("button", { name: "配置参与者 1", exact: true }).click();
  let settings = page.getByRole("dialog", { name: "Claude · default", exact: true });
  await selectChoice(page, "模型", "Native model");
  await selectChoice(page, "权限", "Plan");
  await settings.getByRole("button", { name: "完成", exact: true }).click();
  await addNewAgent(page);
  await create.getByRole("button", { name: "配置参与者 2", exact: true }).click();
  settings = page.getByRole("dialog", { name: "Claude · default", exact: true });
  await expect(settings.getByRole("button", { name: "模型", exact: true })).toHaveText("默认");
  await expect(settings.getByRole("button", { name: "权限", exact: true })).toHaveText("默认");
  await settings.getByRole("button", { name: "完成", exact: true }).click();
  await selectChoice(page, "选择参与者 1", "Codex · iris");
  await create.getByRole("button", { name: "配置参与者 1", exact: true }).click();
  settings = page.getByRole("dialog", { name: "Codex · iris", exact: true });
  await settings.getByLabel("会话名称").fill("代码检查");
  await settings.getByRole("textbox", { name: "工作目录", exact: true }).fill("/tmp/custom");
  const modelBeforeRead = (await settings.getByRole("button", { name: "模型", exact: true }).boundingBox())!;
  relay.delayNextCatalogResponse();
  await settings.getByLabel("会话名称").focus();
  await expect(settings.getByRole("status")).toHaveText("正在读取原生配置…");
  const modelDuringRead = (await settings.getByRole("button", { name: "模型", exact: true }).boundingBox())!;
  expect(modelDuringRead.y).toBeCloseTo(modelBeforeRead.y, 0);
  await selectChoice(page, "模型", "Native model");
  await selectChoice(page, "思考强度", "max");
  await settings.getByRole("button", { name: "执行环境", exact: true }).click();
  const permission = page.getByRole("menu", { name: "执行环境", exact: true });
  await expect(permission.getByRole("menuitemradio", { name: "read-only", exact: true })).toBeDisabled();
  await permission.getByRole("menuitemradio", { name: "workspace-write", exact: true }).click();
  await selectChoice(page, "速度", "快速");
  await settings.getByRole("button", { name: "完成", exact: true }).click();
  await selectChoice(page, "选择参与者 1", "Codex · primary");
  await create.getByRole("button", { name: "配置参与者 1", exact: true }).click();
  settings = page.getByRole("dialog", { name: "Codex · primary", exact: true });
  await expect(settings.getByLabel("会话名称")).toHaveValue("代码检查");
  await expect(settings.getByRole("textbox", { name: "工作目录", exact: true })).toHaveValue("/tmp/custom");
  for (const label of ["模型", "思考强度", "执行环境", "速度"])
    await expect(settings.getByRole("button", { name: label, exact: true })).toHaveText("默认");
  await settings.getByRole("button", { name: "模型", exact: true }).click();
  const model = page.getByRole("menu", { name: "模型", exact: true });
  await expect(model.getByRole("menuitemradio", { name: "Native model", exact: true })).toHaveCount(0);
  await model.getByRole("menuitemradio", { name: "Profile model", exact: true }).click();
  await settings.getByRole("button", { name: "恢复默认", exact: true }).click();
  await expect(settings.getByRole("button", { name: "模型", exact: true })).toHaveText("默认");
  await settings.getByRole("button", { name: "完成", exact: true }).click();
  await selectChoice(page, "选择参与者 2", "DSH · default");
  await create.getByRole("button", { name: "配置参与者 2", exact: true }).click();
  settings = page.getByRole("dialog", { name: "DSH · default", exact: true });
  await selectChoice(page, "权限", "逐次确认");
  await settings.getByRole("button", { name: "Agent Preset", exact: true }).click();
  const preset = page.getByRole("menu", { name: "Agent Preset", exact: true });
  await expect(preset.getByRole("menuitemradio", { name: "Unavailable Agent", exact: true })).toHaveCount(0);
  await preset.getByRole("menuitemradio", { name: "Native Agent", exact: true }).click();
  await expect(settings.locator("select")).toHaveCount(0);
  await settings.getByRole("button", { name: "完成", exact: true }).click();
  await create.getByRole("button", { name: "创建聊天", exact: true }).click();
  const created = relay.commands.find(command => command.operation === "create")!;
  expect(created.members[0]).toEqual({ engine: "codex", profile_id: "primary" });
  expect(created.members[1]).toMatchObject({ engine: "dsh", profile_id: "default", dsh_permission: "ask", dsh_agent_preset: "native-agent" });
  expect(relay.commands.filter(command => ["query", "steer", "interrupt", "new_session"].includes(command.type))).toEqual([]);
});

test("collaboration mention button preserves the caret, selection and existing targets", async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const relay = await mockRooms(page);
  await page.goto("/");
  await page.getByRole("button", { name: "切换新会话引擎" }).click();
  await page.getByRole("menuitem", { name: "协作", exact: true }).click();
  const create = page.getByRole("region", { name: "新建聊天", exact: true });
  await create.getByLabel("聊天名称").fill("点名按钮回归");
  await addNewAgent(page);
  await selectChoice(page, "选择参与者 2", "DSH · default");
  await create.getByRole("button", { name: "创建聊天", exact: true }).click();

  const composer = page.getByRole("textbox", { name: "协作消息", exact: true });
  const trigger = page.getByRole("button", { name: "点名参与者", exact: true });
  const choices = page.getByRole("listbox", { name: "点名会话", exact: true });
  await trigger.click();
  await expect(composer).toHaveValue("@");
  await expect(composer).toBeFocused();
  await expect(trigger).toHaveAttribute("aria-expanded", "true");
  await expect(choices).toHaveAttribute("id", (await trigger.getAttribute("aria-controls"))!);
  await page.screenshot({ path: info.outputPath("mention.png") });
  await trigger.click();
  await expect(composer).toHaveValue("@");
  await composer.press("Escape");
  await expect(choices).toHaveCount(0);
  await trigger.click();
  await expect(composer).toHaveValue("@");
  await choices.getByRole("option", { name: "Claude · default", exact: true }).click();
  await composer.press("End");
  await page.keyboard.insertText("检查结果");
  await composer.evaluate((node: HTMLTextAreaElement) => {
    const start = node.value.indexOf("检查");
    node.setSelectionRange(start, start + 2);
  });
  await trigger.click();
  await expect(composer).toHaveValue("@Claude · default @结果");
  await expect(composer).toBeFocused();
  await choices.getByRole("option", { name: "DSH · default", exact: true }).click();
  await expect(composer).toHaveValue("@Claude · default @DSH · default 结果");
  await composer.press("Enter");
  await expect.poll(() => relay.commands.findLast(command => command.operation === "send")?.targets)
    .toEqual(["member-0", "member-1"]);

  const fullDraft = "x".repeat(65536);
  await composer.fill(fullDraft);
  await trigger.click();
  await expect(composer).toHaveValue(fullDraft);
  await expect(choices).toHaveCount(0);
});

test("collaboration waits for confirmed room history before showing the empty state", async ({ page }) => {
  const relay = await mockRooms(page);
  relay.pauseRoomLoads();
  await page.goto("/");
  const selector = page.getByRole("button", { name: "切换新会话引擎", exact: true });
  await selector.click();
  await page.getByRole("menuitem", { name: "协作", exact: true }).click();
  const conversation = page.locator(".collab-conversation");
  await expect(conversation).toBeVisible();
  await expect(conversation.locator(".collab-primary")).toHaveCount(0);
  await expect(conversation.getByRole("status")).toHaveText("加载会话历史…");
  await expect(page.locator(".collab-pane .sub")).toHaveText("正在读取聊天…");
  await expect(conversation).toHaveAttribute("aria-busy", "true");
  relay.resumeRoomLoads();
  await expect(conversation.getByRole("region", { name: "新建聊天", exact: true })).toBeVisible();
  const create = page.getByRole("region", { name: "新建聊天", exact: true });
  await create.getByLabel("聊天名称").fill("已有聊天历史");
  await create.getByRole("button", { name: "创建聊天", exact: true }).click();
  await page.getByRole("textbox", { name: "协作消息", exact: true }).fill("已保存的问题");
  await page.getByRole("textbox", { name: "协作消息", exact: true }).press("Enter");
  await expect(conversation).toContainText("已保存的问题");
  relay.finish("member-0");
  await expect(conversation).toContainText("回归通过。");
  for (const remembered of [true, false]) {
    relay.pauseRoomLoads();
    await selector.click();
    await page.getByRole("menuitemradio", { name: "Codex", exact: true }).click();
    if (!remembered) await page.evaluate(() => localStorage.removeItem("cc_remote_collaboration:room-machine"));
    await selector.click();
    await page.getByRole("menuitem", { name: "协作", exact: true }).click();
    await expect(conversation).toBeVisible();
    await expect(conversation.locator(".collab-primary")).toHaveCount(0);
    if (remembered) {
      await expect(conversation.getByRole("status")).toHaveCount(0);
      await expect(conversation).toContainText("已保存的问题");
      await expect(conversation).toContainText("回归通过。");
      await expect(conversation).toHaveAttribute("aria-busy", "false");
    } else await expect(conversation.getByRole("status")).toHaveText("加载会话历史…");
    relay.resumeRoomLoads();
    await expect(page.locator(".collab-pane .sub")).toHaveText("已有聊天历史");
    await expect(conversation).toContainText("已保存的问题");
    await expect(conversation).toContainText("回归通过。");
    await expect(conversation.locator(".empty")).toHaveCount(0);
    await expect(conversation).toHaveAttribute("aria-busy", "false");
  }
});

for (const width of [390, 980, 1280]) {
  test(`collaboration navigation matches the native Harness sidebar at ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 });
    const relay = await mockRooms(page);
    await page.goto("/");
    const selector = page.getByRole("button", { name: "切换新会话引擎", exact: true });
    await expect(selector).toBeVisible();
    const geometry = () => page.evaluate(() => {
      const pane = document.querySelector<HTMLElement>(".shell > .pane")!;
      const sidebar = document.querySelector<HTMLElement>(".shell > .sessions")!;
      const style = getComputedStyle(pane);
      return { paneX: pane.getBoundingClientRect().x, paneWidth: pane.getBoundingClientRect().width,
        sidebarWidth: sidebar.getBoundingClientRect().width, radius: style.borderTopLeftRadius };
    });
    await page.locator(".surface-head-title").click();
    await expect(page.locator(".shell")).toHaveClass(/sidebar-open/);
    await expect(page.locator(".shell")).not.toHaveAttribute("data-sidebar-motion");
    const native = await geometry();
    await page.locator(".sessions").getByRole("button", { name: "收起", exact: true }).click();
    await selector.click();
    await page.getByRole("menuitem", { name: "协作", exact: true }).click();
    const workspace = page.locator(".collab-workspace");
    await expect(workspace).toBeVisible();
    await expect(workspace.locator(".collab-surface")).toHaveCount(0);
    await expect(selector).toHaveText("协作");
    await expect(page.getByRole("button", { name: "返回普通会话", exact: true })).toHaveCount(0);
    await selector.press("ArrowDown");
    await expect(page.getByRole("menuitem", { name: "协作", exact: true })).toBeFocused();
    await expect(page.getByRole("menuitem", { name: "协作", exact: true })).toHaveAttribute("aria-current", "page");
    await page.keyboard.press("Enter");
    await expect(workspace).toBeVisible();
    await expect(selector).toBeFocused();
    await selector.press("ArrowDown");
    await page.keyboard.press("Escape");
    await expect(selector).toBeFocused();
    await page.locator(".surface-head-title").click();
    await expect(workspace).toHaveClass(/sidebar-open/);
    await expect(workspace).not.toHaveAttribute("data-sidebar-motion");
    const collaboration = await geometry();
    expect(collaboration.paneX).toBeCloseTo(native.paneX, 1);
    expect(collaboration.paneWidth).toBeCloseTo(native.paneWidth, 1);
    expect(collaboration.sidebarWidth).toBeCloseTo(native.sidebarWidth, 1);
    expect(collaboration.radius).toBe(native.radius);
    await page.screenshot({ path: info.outputPath("collaboration-sidebar.png") });
    await workspace.locator(".brand").click();
    await expect(workspace).not.toHaveClass(/sidebar-open/);
    await expect(workspace).toBeVisible();
    if (width < 980) {
      await expect(workspace).not.toHaveAttribute("data-sidebar-motion");
      const touch = (type: string, x: number, time: number) =>
        workspace.locator(".collab-conversation").evaluate((target, point) => {
          const position = { identifier: 1, target, clientX: point.x, clientY: 250 };
          const event = new Event(point.type, { bubbles: true, cancelable: true });
          const touches = point.type === "touchend" ? [] : [position];
          Object.defineProperties(event, { touches: { value: touches }, targetTouches: { value: touches },
            changedTouches: { value: [position] }, timeStamp: { value: point.time } });
          target.dispatchEvent(event);
        }, { type, x, time });
      await touch("touchstart", 40, 1000);
      await touch("touchmove", 220, 1200);
      expect((await geometry()).paneX).toBeCloseTo(180, 1);
      await touch("touchend", 260, 1400);
      await expect(workspace).toHaveClass(/sidebar-open/);
      await expect(workspace).not.toHaveAttribute("data-sidebar-motion");
      await workspace.locator(".scrim-side").click({ position: { x: 20, y: 130 } });
      await expect(workspace).not.toHaveClass(/sidebar-open/);
    }
    const create = page.getByRole("region", { name: "新建聊天", exact: true });
    await create.getByLabel("聊天名称").fill("导航一致性验收");
    await create.getByRole("button", { name: "创建聊天", exact: true }).click();
    await expect(workspace.locator(".sub")).toHaveText("导航一致性验收");
    await page.getByRole("textbox", { name: "协作消息", exact: true }).fill("保留房间草稿");
    for (const harness of ["Codex", "Claude", "DSH"]) {
      await selector.click();
      await page.getByRole("menuitemradio", { name: harness, exact: true }).click();
      await expect(workspace).toHaveCount(0);
      await expect(selector).toHaveText(harness);
      await selector.click();
      await page.getByRole("menuitem", { name: "协作", exact: true }).click();
      await expect(workspace.locator(".sub")).toHaveText("导航一致性验收");
      await expect(page.getByRole("textbox", { name: "协作消息", exact: true })).toHaveValue("保留房间草稿");
    }
    const shortcut = await page.evaluate(() => navigator.platform.startsWith("Linux")
      && !/Android|CrOS/.test(navigator.userAgent) ? "Alt+3" : "Control+3");
    await page.keyboard.press(shortcut);
    await expect(workspace).toHaveCount(0);
    await expect(selector).toHaveText("DSH");
    expect(relay.commands.filter(command => ["query", "steer", "new_session", "interrupt"].includes(command.type))).toEqual([]);
    expect(relay.commands.filter(command => command.type === "collaboration"
      && ["remove_member", "stop", "send"].includes(command.operation))).toEqual([]);
  });
}

for (const width of [390, 1280]) {
  test(`collaboration settings keep compact editable rows and return from directory at ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 });
    await mockRooms(page);
    await page.goto("/");
    await page.getByRole("button", { name: "切换新会话引擎", exact: true }).click();
    await page.getByRole("menuitem", { name: "协作", exact: true }).click();
    const create = page.getByRole("region", { name: "新建聊天", exact: true });
    await create.getByRole("button", { name: "配置参与者 1", exact: true }).click();
    const settings = page.getByRole("dialog", { name: "Claude · default", exact: true });
    await settings.getByLabel("会话名称", { exact: true }).fill("架构梳理");
    await settings.getByLabel("工作目录", { exact: true }).fill("/tmp/custom");
    const rows = settings.locator(".collab-setting-row");
    await expect(rows).toHaveCount(2);
    for (const row of await rows.all()) expect((await row.boundingBox())!.height).toBeLessThanOrEqual(60);
    const directoryTrigger = settings.getByRole("button", { name: "选择工作目录", exact: true });
    // Keep the pointer down across a fast blur/catalog response. A native tap
    // must still open the picker if the centered settings layout changes.
    await directoryTrigger.click({ delay: 100 });
    const picker = page.getByRole("dialog", { name: "选择本地工作区", exact: true });
    await picker.getByRole("button", { name: "关闭", exact: true }).click();
    await expect(settings).toBeVisible();
    await expect(directoryTrigger).toBeFocused();
    await expect(settings.getByLabel("会话名称", { exact: true })).toHaveValue("架构梳理");
    await expect(settings.getByLabel("工作目录", { exact: true })).toHaveValue("/tmp/custom");
    if (info.project.use.hasTouch) await directoryTrigger.tap();
    else await directoryTrigger.click();
    await picker.getByRole("button", { name: "project", exact: true }).click();
    await picker.getByRole("button", { name: "使用此目录", exact: true }).click();
    await expect(settings.getByLabel("工作目录", { exact: true })).toHaveValue("/tmp/project");
    await page.screenshot({ path: info.outputPath("settings-compact.png") });
    await settings.getByRole("button", { name: "完成", exact: true }).click();
    await expect(page.locator("dialog[open]")).toHaveCount(0);
    await expect(create.getByRole("button", { name: "工作目录 1", exact: true })).toContainText("project");
  });

  test(`collaboration directory entry opens one picker and returns to creation at ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 });
    const relay = await mockRooms(page);
    relay.setDirectoryEntries(80);
    await page.goto("/");
    await page.getByRole("button", { name: "切换新会话引擎", exact: true }).click();
    await page.getByRole("menuitem", { name: "协作", exact: true }).click();
    const create = page.getByRole("region", { name: "新建聊天", exact: true });
    const trigger = create.getByRole("button", { name: "工作目录 1", exact: true });
    await trigger.focus();
    await trigger.press("Enter");
    const picker = page.getByRole("dialog", { name: "选择本地工作区", exact: true });
    await expect(picker.getByRole("button", { name: "project", exact: true })).toBeVisible();
    await expect(picker.getByRole("button", { name: "使用此目录", exact: true })).toBeEnabled();
    await page.screenshot({ path: info.outputPath("directory-direct.png") });
    await expect(page.locator("dialog[open]")).toHaveCount(1);
    await expect(page.locator(".collab-settings-dialog")).toHaveCount(0);
    const frame = await page.locator(".collab-directory-dialog").boundingBox();
    const content = await picker.locator(".dp").boundingBox();
    expect(frame!.height - content!.height).toBeLessThan(90);
    expect(frame!.height).toBeLessThanOrEqual(868);
    const footer = await picker.locator(".dp-foot").boundingBox();
    expect(footer!.y + footer!.height).toBeLessThanOrEqual(frame!.y + frame!.height);
    expect(await picker.evaluate(node => getComputedStyle(node).outlineStyle)).toBe("none");
    expect(relay.commands.filter(command => command.operation === "catalog")).toHaveLength(0);
    await picker.getByRole("button", { name: "关闭", exact: true }).click();
    await expect(page.locator("dialog[open]")).toHaveCount(0);
    await expect(trigger).toBeFocused();
    await trigger.click();
    await picker.getByRole("button", { name: "project", exact: true }).click();
    await picker.getByRole("button", { name: "使用此目录", exact: true }).click();
    await expect(page.locator("dialog[open]")).toHaveCount(0);
    await expect(trigger).toContainText("project");
    await trigger.click();
    await page.keyboard.press("Escape");
    await expect(page.locator("dialog[open]")).toHaveCount(0);
    await expect(trigger).toBeFocused();
    expect(relay.commands.filter(command => ["query", "steer", "interrupt", "new_session"].includes(command.type))).toEqual([]);
  });
}

for (const width of [390, 1280]) {
  test(`collaboration room creation, native settings and working marks at ${width}px`, async ({
    page,
  }, info) => {
    await page.setViewportSize({ width, height: 900 });
    const relay = await mockRooms(page);
    await page.goto("/");
    const selector = page.getByRole("button", { name: "切换新会话引擎" });
    await expect(selector).toBeVisible();
    await selector.press("ArrowDown");
    await expect(page.getByRole("menu")).toBeVisible();
    await page.keyboard.press("End");
    await page.keyboard.press("Enter");
    await expect(page.locator(".collab-workspace")).toBeVisible();
    const create = page.getByRole("region", { name: "新建聊天" });
    await create
      .getByRole("textbox", { name: "聊天名称" })
      .fill("接口重构讨论");
    for (const engine of ["codex", "dsh"]) {
      await addNewAgent(page);
      const index = engine === "codex" ? 2 : 3;
      await selectChoice(page, `选择参与者 ${index}`, engine === "codex" ? "Codex · iris" : "DSH · default");
    }
    await create.getByRole("button", { name: "配置参与者 2" }).click();
    const settings = page.getByRole("dialog", {
      name: "Codex · iris",
      exact: true,
    });
    await expect(settings.getByRole("button", { name: "模型", exact: true })).toHaveText("默认");
    await selectChoice(page, "模型", "Native model");
    await selectChoice(page, "思考强度", "max");
    await selectChoice(page, "速度", "快速");
    await settings.getByRole("button", { name: "选择工作目录" }).click();
    const directory = page.getByRole("dialog", {
      name: "选择本地工作区",
      exact: true,
    });
    await directory
      .getByRole("button", { name: "project", exact: true })
      .click();
    await directory.getByRole("button", { name: "使用此目录" }).click();
    await settings.getByRole("button", { name: "完成", exact: true }).click();
    await create.getByRole("button", { name: "创建聊天", exact: true }).click();
    await expect(page.locator(".collab-pane .sub")).toHaveText("接口重构讨论");
    const created = relay.commands.find(
      (command) => command.operation === "create",
    )!;
    expect(created.members[1]).toMatchObject({
      engine: "codex",
      profile_id: "iris",
      cwd: "/tmp/project",
      model: "native-model",
      effort: "max",
      service_tier: "priority",
    });
    expect(created.members[0].model).toBeUndefined();
    const composer = page.getByRole("textbox", { name: "协作消息" });
    await composer.fill("@");
    await page.getByRole("option", { name: "所有参与者" }).click();
    await composer.press("End");
    await composer.type("一起检查这份方案");
    await composer.press("Enter");
    await expect(page.locator(".collab-activity")).toHaveCount(3);
    for (const row of await page.locator(".collab-activity").all()) {
      const brand = row.locator("header > .engine-brand-icon");
      const title = (await row.locator("strong").boundingBox())!;
      const spark = (await row
        .locator(".collab-working > :first-child")
        .boundingBox())!;
      const word = (await row.locator(".working-word").boundingBox())!;
      expect(await brand.count()).toBe(1);
      expect(spark.x).toBeGreaterThan(title.x + title.width);
      expect(word.x).toBeGreaterThan(spark.x + spark.width - 1);
    }
    expect(
      relay.commands.findLast((command) => command.operation === "send")!
        .targets,
    ).toEqual(["member-0", "member-1", "member-2"]);
    const pane = (await page.locator(".collab-pane").boundingBox())!;
    expect(pane.x).toBe(0);
    expect(pane.width + pane.x).toBeLessThanOrEqual(width);
    await expect
      .poll(() =>
        page
          .locator(".working-word-layer")
          .first()
          .evaluate((node) => getComputedStyle(node).opacity),
      )
      .toBe("1");
    await page.screenshot({
      path: info.outputPath(`collaboration-${width}.png`),
    });
    const beforeProgress = relay.commands.length;
    await page
      .locator('.collab-activity[data-engine="codex"]')
      .getByRole("button", { name: "查看进度 ↗" })
      .click();
    const progress = page.getByRole("dialog", {
      name: "Codex · iris 的进度",
      exact: true,
    });
    await expect(progress).toBeVisible();
    relay.emit({
      type: "user_msg",
      sid: "native-1",
      msg_id: "native-input",
      prompt: "原生内部过程",
    });
    relay.emit({
      type: "assistant_msg_start",
      sid: "native-1",
      message_id: "native-response",
      channel: "final",
    });
    relay.emit({
      type: "delta",
      sid: "native-1",
      message_id: "native-response",
      channel: "final",
      text: "正在检查文件。",
    });
    await expect(progress).toContainText("正在检查文件。");
    await expect(progress.locator(".thread")).toBeVisible();
    expect(
      relay.commands
        .slice(beforeProgress)
        .some((command) =>
          ["query", "steer", "switch_session"].includes(command.type),
        ),
    ).toBe(false);
    await expect(progress.getByRole("button", { name: "刷新进度" })).toHaveCount(0);
    await expect(progress.getByRole("button", { name: "关闭进度" })).toHaveCount(0);
    await expect(progress.locator("footer")).toHaveCount(0);
    await expect(progress.getByRole("button", { name: "查看原生会话", exact: true })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.locator(".collab-thread")).not.toContainText(
      "原生内部过程",
    );
    relay.finish();
    await expect(page.locator(".collab-working.active")).toHaveCount(0);
    await expect(page.locator(".collab-activity")).toHaveCount(2);
    await expect(page.locator(".collab-answer")).toContainText("回归通过。");
    await page
      .locator(".collab-answer")
      .getByRole("button", { name: "查看原生会话 ↗", exact: true })
      .click();
    await expect(page.locator(".collab-workspace")).toHaveCount(0);
    await expect
      .poll(() =>
        relay.commands.some(
          (command) =>
            command.type === "switch_session" &&
            command.session_id === "native-1",
        ),
      )
      .toBe(true);
  });
}

test("collaboration restores mention targets across reload and supports adding an agent", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  const relay = await mockRooms(page);
  await page.goto("/");
  const enter = async () => {
    await page.getByRole("button", { name: "切换新会话引擎" }).click();
    await page.getByRole("menuitem", { name: "协作", exact: true }).click();
  };
  await enter();
  const create = page.getByRole("region", { name: "新建聊天" });
  await create.getByLabel("聊天名称").fill("讨论");
  await create.getByRole("button", { name: "创建聊天", exact: true }).click();
  await page.getByRole("button", { name: "管理成员" }).click();
  await addNewAgent(page);
  const add = page.getByRole("dialog", { name: "添加 Agent", exact: true });
  await selectChoice(page, "选择参与者 1", "Codex · iris");
  await add.getByRole("button", { name: "加入聊天", exact: true }).click();
  await expect(add).toHaveCount(0);
  const composer = page.getByRole("textbox", { name: "协作消息" });
  await composer.fill("@");
  await page
    .getByRole("listbox", { name: "点名会话" })
    .getByRole("option", { name: "Codex · iris", exact: true })
    .click();
  await composer.press("End");
  await composer.type("看看测试");
  await page.reload();
  await enter();
  await expect(composer).toHaveValue("@Codex · iris 看看测试");
  await composer.press("Enter");
  await expect
    .poll(
      () =>
        relay.commands.findLast((command) => command.operation === "send")
          ?.targets,
    )
    .toEqual(["member-1"]);
});

for (const received of [false, true]) {
  test(`collaboration recovers a pending send after reload when received=${received}`, async ({
    page,
  }) => {
    const relay = await mockRooms(page);
    const enter = async () => {
      await page.getByRole("button", { name: "切换新会话引擎" }).click();
      await page.getByRole("menuitem", { name: "协作", exact: true }).click();
    };
    await page.goto("/");
    await enter();
    const create = page.getByRole("region", { name: "新建聊天" });
    await create.getByLabel("聊天名称").fill("发送恢复");
    await create.getByRole("button", { name: "创建聊天", exact: true }).click();
    const composer = page.getByRole("textbox", { name: "协作消息" });
    await composer.fill("这条消息只执行一次");
    if (received) relay.pauseNextSendResponse();
    else relay.dropNextSend();
    await composer.press("Enter");
    await expect
      .poll(
        () => relay.commands.filter((item) => item.operation === "send").length,
      )
      .toBe(1);
    const original = relay.commands.find((item) => item.operation === "send")!;
    await page.reload();
    await enter();
    if (!received) {
      await expect(composer).toHaveValue(original.prompt);
      await composer.press("Enter");
      await expect
        .poll(
          () =>
            relay.commands.filter((item) => item.operation === "send").length,
        )
        .toBe(2);
      expect(
        relay.commands.findLast((item) => item.operation === "send")!
          .message_id,
      ).toBe(original.message_id);
    }
    await expect(composer).toHaveValue("");
    await expect(page.locator(".collab-user")).toHaveCount(1);
    expect(
      relay.commands.filter((item) => item.operation === "send"),
    ).toHaveLength(received ? 1 : 2);
  });
}

test("collaboration snapshot acknowledges an accepted send after its direct response was lost", async ({
  page,
}) => {
  const relay = await mockRooms(page);
  await page.goto("/");
  await page.getByRole("button", { name: "切换新会话引擎" }).click();
  await page.getByRole("menuitem", { name: "协作", exact: true }).click();
  const create = page.getByRole("region", { name: "新建聊天" });
  await create.getByLabel("聊天名称").fill("回执恢复");
  await create.getByRole("button", { name: "创建聊天", exact: true }).click();
  const composer = page.getByRole("textbox", { name: "协作消息" });
  await composer.fill("第一条");
  relay.pauseNextSendResponse();
  await composer.press("Enter");
  await expect
    .poll(() => relay.commands.some((item) => item.operation === "send"))
    .toBe(true);
  await expect(
    page.getByRole("button", { name: "发送消息", exact: true }),
  ).toBeDisabled();
  relay.state();
  await expect(composer).toHaveValue("");
  await composer.fill("继续引导");
  await expect(
    page.getByRole("button", { name: "发送消息", exact: true }),
  ).toBeEnabled();
  await composer.press("Enter");
  await expect(page.locator(".collab-user")).toHaveCount(2);
});

test("collaboration inline creation follows defaults without native execution", async ({ page }, info) => {
  const relay = await mockRooms(page);
  await page.goto("/");
  await page.getByRole("button", { name: "切换新会话引擎", exact: true }).click();
  await page.getByRole("menuitem", { name: "协作", exact: true }).click();
  const create = page.getByRole("region", { name: "新建聊天", exact: true });
  await expect(create).toBeVisible();
  await expect(page.getByRole("dialog", { name: "新建聊天", exact: true })).toHaveCount(0);
  await expect(create.locator(".collab-participant")).toHaveCount(1);
  await expect(create.getByRole("button", { name: "创建聊天", exact: true })).toBeDisabled();
  await page.screenshot({ path: info.outputPath("new-chat-initial.png") });
  await expect(page.getByRole("textbox", { name: "协作消息", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "切换新会话引擎", exact: true }).click();
  const harness = page.getByRole("menu", { name: "会话引擎", exact: true });
  await expect(harness.getByRole("menuitem", { name: /协作/ })).toHaveAttribute("aria-current", "page");
  await expect(harness.locator('svg use[href*="check"]')).toHaveCount(0);
  await page.keyboard.press("Escape");
  await create.getByLabel("聊天名称").fill("页面内新建");
  await page.screenshot({ path: info.outputPath("new-chat-inline.png") });
  await create.getByRole("button", { name: "创建聊天", exact: true }).click();
  await expect(page.locator(".collab-room-empty")).toHaveText("发一条消息开始讨论");
  const threadBox = (await page.locator(".collab-conversation").boundingBox())!;
  const emptyBox = (await page.locator(".collab-room-empty").boundingBox())!;
  expect(Math.abs(emptyBox.y + emptyBox.height / 2 - threadBox.y - threadBox.height / 2)).toBeLessThan(24);
  await page.screenshot({ path: info.outputPath("new-chat-created.png") });
  expect(relay.commands.filter(command => ["query", "steer", "interrupt", "new_session"].includes(command.type))).toEqual([]);
  expect(relay.commands.filter(command => command.operation === "send")).toEqual([]);
  for (const label of ["点名参与者", "默认回复者"]) {
    const button = page.getByRole("button", { name: label, exact: true });
    expect((await button.boundingBox())!.height).toBeLessThanOrEqual(34);
    expect(await button.evaluate(node => parseFloat(getComputedStyle(node).borderRadius))).toBeGreaterThanOrEqual(16);
  }
});

test("collaboration same-account sessions have distinct aliases and exact mention targets", async ({ page }, info) => {
  const relay = await mockRooms(page);
  await page.goto("/");
  await page.getByRole("button", { name: "切换新会话引擎", exact: true }).click();
  await page.getByRole("menuitem", { name: "协作", exact: true }).click();
  const create = page.getByRole("region", { name: "新建聊天", exact: true });
  await create.getByLabel("聊天名称").fill("同账号两会话");
  for (const alias of ["代码实现", "交叉审查"]) {
    await create.getByRole("button", { name: "添加 Agent", exact: true }).click();
    await page.getByRole("menuitem", { name: "关联已有会话", exact: true }).click();
    const picker = page.getByRole("dialog", { name: "关联已有会话", exact: true });
    await selectChoice(page, "选择 Harness 与账号", "Codex · primary");
    await picker.getByRole("button", { name: new RegExp(alias) }).click();
    await expect(picker).toHaveCount(0);
  }
  await create.getByRole("button", { name: "配置参与者 1", exact: true }).click();
  await page.getByRole("dialog", { name: "Claude · default", exact: true })
    .getByRole("button", { name: "移除参与者 1", exact: true }).click();
  await create.getByRole("button", { name: "创建聊天", exact: true }).click();
  const creation = relay.commands.find(command => command.operation === "create")!;
  expect(creation.members.map((member: CollaborationMemberSpec) => member.session_id))
    .toEqual(["01a0d871-implementation", "01a0d15f-review"]);
  expect(creation.members.map((member: CollaborationMemberSpec) => member.profile_id)).toEqual(["primary", "primary"]);
  const composer = page.getByRole("textbox", { name: "协作消息", exact: true });
  await composer.fill("@primary");
  await expect(page.getByRole("option", { name: "所有参与者", exact: true })).toHaveCount(0);
  await expect(page.getByRole("option", { name: "代码实现", exact: true })).toContainText("01a0d871");
  await expect(page.getByRole("option", { name: "交叉审查", exact: true })).toContainText("01a0d15f");
  await page.screenshot({ path: info.outputPath("same-account-mentions.png") });
  await page.getByRole("option", { name: "交叉审查", exact: true }).click();
  await composer.press("End");
  await composer.type("核对刚才的改动");
  await expect(composer).toHaveValue("@交叉审查 核对刚才的改动");
  await expect(page.getByRole("button", { name: "默认回复者", exact: true })).toContainText("代码实现");
  await composer.press("Enter");
  await expect.poll(() => relay.commands.find(command => command.operation === "send")?.targets).toEqual(["member-1"]);
  await expect(composer).toHaveValue("");
  await composer.fill("@交叉审查");
  await composer.press("Enter");
  await expect(composer).toHaveValue("@交叉审查 ");
  await composer.type("继续核对");
  await composer.press("Enter");
  await expect.poll(() => relay.commands.filter(command => command.operation === "send").length).toBe(2);
  expect(relay.commands.findLast(command => command.operation === "send")?.targets).toEqual(["member-1"]);
});

test("collaboration DSH identifiers stay distinct across selection, messages and mentions", async ({ page }, info) => {
  const relay = await mockRooms(page);
  await page.goto("/");
  await page.getByRole("button", { name: "切换新会话引擎", exact: true }).click();
  await page.getByRole("menuitem", { name: "协作", exact: true }).click();
  const create = page.getByRole("region", { name: "新建聊天", exact: true });
  await create.getByLabel("聊天名称").fill("DSH 会话标识回归");
  await create.getByRole("button", { name: "添加 Agent", exact: true }).click();
  await page.getByRole("menuitem", { name: "关联已有会话", exact: true }).click();
  const picker = page.getByRole("dialog", { name: "关联已有会话", exact: true });
  await selectChoice(page, "选择 Harness 与账号", "DSH · default");
  await expect(picker.locator(".collab-session-list")).toContainText("711d960d");
  await expect(picker.locator(".collab-session-list")).toContainText("68a304ab");
  await picker.getByRole("button", { name: /资料核对/ }).click();
  await expect(create.locator(".collab-participant").nth(1)).toContainText("711d960d");
  await create.getByRole("button", { name: "创建聊天", exact: true }).click();
  expect(relay.commands.find(command => command.operation === "create")!.members[1].session_id)
    .toBe("dsh@session-711d960d-3a17-491f-8b66-32b3a6e0a946");
  relay.finish("member-1");
  const metadata = page.locator('.collab-answer[data-engine="dsh"] header .collab-session-id');
  await expect(metadata).toContainText("711d960d");
  await expect(metadata).toHaveAttribute("title", "session-711d960d-3a17-491f-8b66-32b3a6e0a946");
  const name = (await page.locator('.collab-answer[data-engine="dsh"] header strong').boundingBox())!;
  const sid = (await metadata.boundingBox())!;
  expect(sid.x).toBeGreaterThan(name.x + name.width);
  expect(Math.abs(sid.y + sid.height / 2 - name.y - name.height / 2)).toBeLessThan(5);
  await page.getByRole("button", { name: "管理成员", exact: true }).click();
  const members = page.getByRole("dialog", { name: "聊天成员", exact: true });
  await expect(members.locator('[data-member-id="member-1"]')).toContainText("711d960d");
  await members.getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByRole("button", { name: "点名参与者", exact: true }).click();
  const mentions = page.getByRole("listbox", { name: "点名会话", exact: true });
  await expect(mentions).toContainText("711d960d");
  await page.screenshot({ path: info.outputPath("dsh-session-identity.png") });
  await mentions.getByRole("option", { name: "资料核对", exact: true }).click();
  const composer = page.getByRole("textbox", { name: "协作消息", exact: true });
  await composer.press("End");
  await composer.type("继续核对");
  await composer.press("Enter");
  await expect.poll(() => relay.commands.find(command => command.operation === "send")?.targets).toEqual(["member-1"]);
});

test("collaboration renamed members restore the original identity without creating sessions", async ({ page }) => {
  const relay = await mockRooms(page);
  await createSimpleRoom(page);
  await page.getByRole("button", { name: "管理成员", exact: true }).click();
  const members = page.getByRole("dialog", { name: "聊天成员", exact: true });
  await members.getByRole("button", { name: "Claude · default 的操作", exact: true }).click();
  await page.getByRole("menuitem", { name: "重命名", exact: true }).click();
  const rename = page.getByRole("dialog", { name: "重命名会话", exact: true });
  relay.rejectNextOperation("rename_member");
  await rename.getByLabel("会话名称").fill("已有会话名");
  await rename.getByRole("button", { name: "保存", exact: true }).click();
  await expect(rename.getByRole("alert")).toHaveText("原生会话重命名暂未完成，请重试。");
  await rename.getByLabel("会话名称").fill("架构梳理");
  await rename.getByRole("button", { name: "保存", exact: true }).click();
  await expect(rename).toHaveCount(0);
  await members.getByRole("button", { name: "架构梳理 的操作", exact: true }).click();
  await page.getByRole("menuitem", { name: "移出聊天", exact: true }).click();
  await expect(members.locator(".collab-members .collab-member")).toHaveCount(0);
  await members.locator("summary").click();
  await members.getByRole("button", { name: "恢复 架构梳理", exact: true }).click();
  await expect(members.locator('[data-member-id="member-0"]')).toContainText("架构梳理");
  const remove = relay.commands.find(command => command.operation === "remove_member")!;
  const restore = relay.commands.find(command => command.operation === "restore_member")!;
  expect(restore.member_id).toBe(remove.member_id);
  expect(relay.commands.filter(command => command.operation === "add_member")).toEqual([]);
  expect(relay.commands.filter(command => ["query", "steer", "interrupt", "new_session"].includes(command.type))).toEqual([]);
  await members.getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByRole("button", { name: "点名参与者", exact: true }).click();
  await page.getByRole("option", { name: "架构梳理", exact: true }).click();
  const composer = page.getByRole("textbox", { name: "协作消息", exact: true });
  await composer.press("End");
  await composer.type("继续核对");
  await composer.press("Enter");
  await expect.poll(() => relay.commands.find(command => command.operation === "send")?.targets).toEqual(["member-0"]);
});

test("collaboration automatic history paging keeps the reading row and deduplicates gestures", async ({ page, browserName, isMobile }, info) => {
  const relay = await mockRooms(page);
  await createSimpleRoom(page);
  relay.seedHistory(100);
  const thread = page.getByRole("region", { name: "协作对话记录", exact: true });
  await expect(thread.locator("[data-message-id]")).toHaveCount(40);
  relay.holdHistory();
  await thread.hover();
  if (browserName === "webkit" && isMobile) { await thread.focus(); await thread.press("Home"); }
  else await page.mouse.wheel(0, -100000);
  await expect.poll(() => relay.commands.filter(command => command.operation === "read" && command.before === 61).length).toBe(1);
  await expect(page.locator(".history-page-activity")).toHaveText("正在加载更早历史…");
  const anchor = thread.locator('[data-message-id="history-61"]');
  const offset = (await anchor.boundingBox())!.y;
  await thread.dispatchEvent("wheel", { deltaY: -1000 });
  relay.releaseHistory();
  await expect(thread.locator("[data-message-id]")).toHaveCount(80);
  await expect(page.locator(".history-page-activity")).toHaveCount(0);
  expect(Math.abs((await anchor.boundingBox())!.y - offset)).toBeLessThanOrEqual(2);
  expect(relay.commands.filter(command => command.operation === "read" && command.before === 61)).toHaveLength(1);
  await page.screenshot({ path: info.outputPath("history-anchor.png") });
});

test("collaboration history failures expose a retry without an automatic request loop", async ({ page, browserName, isMobile }) => {
  const relay = await mockRooms(page);
  await createSimpleRoom(page);
  relay.seedHistory(100);
  const thread = page.getByRole("region", { name: "协作对话记录", exact: true });
  await expect(thread.locator("[data-message-id]")).toHaveCount(40);
  relay.holdHistory();
  await thread.hover();
  if (browserName === "webkit" && isMobile) { await thread.focus(); await thread.press("Home"); }
  else await page.mouse.wheel(0, -100000);
  await expect.poll(() => relay.commands.filter(command => command.before === 61).length).toBe(1);
  relay.failHistory();
  await expect(thread.getByRole("status")).toHaveText("历史读取失败");
  await expect(page.locator(".history-page-activity")).toHaveCount(0);
  await thread.dispatchEvent("wheel", { deltaY: -1000 });
  expect(relay.commands.filter(command => command.before === 61)).toHaveLength(1);
  await thread.getByRole("button", { name: "重试加载历史", exact: true }).click();
  await expect.poll(() => relay.commands.filter(command => command.before === 61).length).toBe(2);
  relay.releaseHistory();
  await expect(thread.locator("[data-message-id]")).toHaveCount(80);
  await expect(thread.locator(".collab-history-error")).toHaveCount(0);
});

test("collaboration suppresses the installed Harmony settings overlay on authenticated surfaces", async ({ page }) => {
  await mockRooms(page);
  await createSimpleRoom(page);
  const adapter = readFileSync(new URL("../../harmony/entry/src/main/resources/rawfile/shell.js", import.meta.url), "utf8");
  await page.evaluate(`${adapter}(location.origin);`);
  const fallback = page.locator("#cc-remote-harmony-settings-fallback");
  await expect(fallback).toHaveCount(1);
  await expect(fallback).toBeHidden();
  await page.keyboard.press("Control+2");
  await expect(fallback).toBeHidden();
  await page.evaluate(() => document.querySelector("#root")!.replaceChildren());
  await expect(fallback).toBeVisible();
});

for (const recovery of ["truncated", "rebuild", "rollback", "rollback-without-sid"] as const) {
  test(`collaboration progress refreshes its inspected session after ${recovery}`, async ({ page, isMobile }) => {
    const relay = await mockRooms(page);
    await createSimpleRoom(page);
    relay.reply("member-0", "公开结论。");
    let recovered = false;
    let build = 0;
    const replayGap = recovery === "truncated" || recovery === "rebuild";
    const firstPageRequests = replayGap ? 3 : 2;
    const generation = recovery === "rebuild" ? "restarted-generation" : "native-generation";
    relay.setNativeHistory(command => {
      const latestId = recovered ? "native-refreshed" : "native-original";
      const id = command.before ? "native-oldest" : latestId;
      const latestText = recovered ? "断线后恢复的原生历史正文。\n\n".repeat(80) : "断线前的原生历史正文。";
      const text = command.before ? "恢复后仍可加载更早的原生结论。"
        : latestText;
      return {
        detail: "summary", authoritative: true,
        revision: recovered ? "refreshed-revision" : "revision",
        generation: recovered ? generation : "native-generation",
        build_seq: ++build, live_seq: recovered ? 20 : 5,
        oldest_id: id, newest_id: id, has_more: recovered && !command.before,
        in_progress: false, events: [], turns: [{
          id, prompt: id, done: true, detailLoaded: true,
          blocks: [{ kind: "text", message_id: `${id}-answer`, channel: "final", text, done: true }],
        }],
      };
    });
    const beforeOpen = relay.commands.length;
    await page.locator(".collab-answer").getByRole("button", { name: "查看进度 ↗", exact: true }).click();
    const progress = page.getByRole("dialog", { name: "Claude · default 的进度", exact: true });
    await expect(progress).toContainText("断线前的原生历史正文。");

    relay.emit({ type: "replay_start", sid: "another-session", from_seq: 1, to_seq: 5,
      truncated: true, generation: "native-generation" });
    relay.emit({ type: "history_invalidated", session_id: "another-session",
      revision: "other-revision", reason: "rollback" });
    relay.emit({ type: "replay_end", sid: "another-session", to_seq: 5, truncated: true });
    relay.emit({ type: "replay_start", sid: "native-0", from_seq: 6, to_seq: 5,
      truncated: false, generation: "native-generation" });
    relay.emit({ type: "replay_end", sid: "native-0", to_seq: 5, truncated: false });
    recovered = true;
    if (replayGap) {
      relay.emit({ type: "replay_start", sid: "native-0", from_seq: 16, to_seq: 20,
        truncated: recovery === "truncated", rebuild: recovery === "rebuild", generation });
    } else {
      relay.emit({ type: "replay_start", sid: "native-0", from_seq: 6, to_seq: 20,
        truncated: false, generation });
      relay.emit({ type: "history_invalidated", session_id: "native-0",
        ...(recovery === "rollback-without-sid" ? {} : { sid: "native-0" }),
        revision: "refreshed-revision", reason: "rollback" });
    }
    relay.emit({ type: "replay_end", sid: "native-0", to_seq: 20, truncated: recovery === "truncated" });

    await expect.poll(() => relay.commands.slice(beforeOpen).filter(command =>
      command.type === "get_history" && command.session_id === "native-0" && !command.before).length).toBe(firstPageRequests);
    await expect(progress).toContainText("断线后恢复的原生历史正文。");
    await expect(progress).not.toContainText("断线前的原生历史正文。");
    await expect(progress.getByText("正在恢复历史…", { exact: true })).toHaveCount(0);
    await expect(progress.getByText("加载会话历史…", { exact: true })).toHaveCount(0);
    const thread = progress.getByRole("region", { name: "对话记录", exact: true });
    await thread.evaluate(node => { node.scrollTop = 0; node.dispatchEvent(new Event("scroll")); });
    if (isMobile) {
      await thread.evaluate(node => {
        for (const [kind, y] of [["touchstart", 100], ["touchmove", 180], ["touchend", 180]] as const) {
          const event = new Event(kind, { bubbles: true, cancelable: true });
          const touch = { identifier: 1, clientX: 100, clientY: y };
          Object.defineProperty(event, "touches", { value: kind === "touchend" ? [] : [touch] });
          Object.defineProperty(event, "changedTouches", { value: [touch] });
          node.dispatchEvent(event);
        }
      });
    } else {
      await thread.hover();
      await page.mouse.wheel(0, -900);
    }
    await expect(progress).toContainText("恢复后仍可加载更早的原生结论。");
    const commands = relay.commands.slice(beforeOpen);
    expect(commands.filter(command => command.type === "get_history")).toEqual([
      ...Array.from({ length: firstPageRequests }, () =>
        expect.objectContaining({ session_id: "native-0", cwd: "/tmp/project" })),
      expect.objectContaining({ session_id: "native-0", before: "native-refreshed" }),
    ]);
    expect(commands.filter(command => ["query", "steer", "switch_session", "new_session"].includes(command.type))).toEqual([]);
  });
}

for (const interaction of ["touch", "keyboard"]) {
  test(`collaboration ${interaction} scrolling at the history edge loads automatically`, async ({ page }) => {
    const pageErrors: string[] = [];
    page.on("pageerror", error => pageErrors.push(error.message));
    const relay = await mockRooms(page);
    await createSimpleRoom(page);
    relay.seedHistory(100);
    const thread = page.getByRole("region", { name: "协作对话记录", exact: true });
    await expect(thread.locator("[data-message-id]")).toHaveCount(40);
    relay.holdHistory();
    if (interaction === "touch") {
      await thread.evaluate(node => {
        const fire = (kind: string, y: number) => {
          const event = new Event(kind, { bubbles: true, cancelable: true });
          const touch = { identifier: 1, clientX: 200, clientY: y };
          Object.defineProperty(event, "touches", { value: kind === "touchend" ? [] : [touch] });
          Object.defineProperty(event, "changedTouches", { value: [touch] });
          node.dispatchEvent(event);
        };
        fire("touchstart", 100);
        node.scrollTop = 0;
        fire("touchmove", 160);
        fire("touchmove", 180);
        fire("touchend", 180);
      });
    } else {
      await thread.focus();
      await thread.press("Home");
    }
    await expect.poll(() => relay.commands.filter(command => command.before === 61).length).toBe(1);
    relay.releaseHistory();
    await expect(thread.locator("[data-message-id]")).toHaveCount(80);
    expect(relay.commands.filter(command => command.before === 61)).toHaveLength(1);
    expect(pageErrors).toEqual([]);
  });
}

for (const width of [390, 1280]) {
  test(`collaboration aligned three-agent design stays complete at ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 });
    const relay = await mockRooms(page);
    await page.goto("/");
    await page.getByRole("button", { name: "切换新会话引擎", exact: true }).click();
    await page.getByRole("menuitem", { name: "协作", exact: true }).click();
    const create = page.getByRole("region", { name: "新建聊天", exact: true });
    await create.getByLabel("聊天名称").fill("接口重构讨论");
    const agents = [
      { identity: "Claude · gaia", alias: "架构梳理" },
      { identity: "Codex · primary", alias: "代码实现" },
      { identity: "DSH · default", alias: "资料核对" },
    ];
    for (const [index, agent] of agents.entries()) {
      if (index) await addNewAgent(page);
      await selectChoice(page, `选择参与者 ${index + 1}`, agent.identity);
      await create.getByRole("button", { name: `配置参与者 ${index + 1}`, exact: true }).click();
      const settings = page.getByRole("dialog", { name: agent.identity, exact: true });
      await settings.getByLabel("会话名称").fill(agent.alias);
      await settings.getByRole("textbox", { name: "工作目录", exact: true }).fill("/tmp/cc-remote");
      await settings.getByRole("button", { name: "完成", exact: true }).click();
      await expect(settings).toHaveCount(0);
    }
    const add = create.getByRole("button", { name: "添加 Agent", exact: true });
    expect((await add.boundingBox())!.height).toBeLessThanOrEqual(36);
    expect(await add.evaluate(node => parseFloat(getComputedStyle(node).borderRadius))).toBeGreaterThanOrEqual(16);
    await page.screenshot({ path: info.outputPath("aligned-new-chat.png") });
    await create.getByRole("button", { name: "创建聊天", exact: true }).click();
    await expect(page.locator(".collab-room-empty")).toBeVisible();
    await page.screenshot({ path: info.outputPath("aligned-room-empty.png") });
    const composer = page.getByRole("textbox", { name: "协作消息", exact: true });
    await page.getByRole("button", { name: "点名参与者", exact: true }).click();
    await page.getByRole("option", { name: "所有参与者", exact: true }).click();
    await composer.press("End");
    await composer.type("分别核对消息路由、终止状态与历史归属。");
    await composer.press("Enter");
    await expect(page.locator(".collab-activity")).toHaveCount(3);
    await expect(page.locator(".collab-working.active")).toHaveCount(3);
    for (const header of await page.locator(".collab-activity > header").all()) {
      const name = (await header.locator("strong").boundingBox())!;
      const sid = (await header.locator(".collab-session-id").boundingBox())!;
      expect(sid.x).toBeGreaterThan(name.x + name.width);
      expect(Math.abs(sid.y + sid.height / 2 - name.y - name.height / 2)).toBeLessThan(5);
    }
    await expect.poll(() => page.locator(".collab-working .working-word-layer.is-current").evaluateAll(nodes =>
      nodes.every(node => getComputedStyle(node).opacity === "1"),
    )).toBe(true);
    await page.screenshot({ path: info.outputPath("aligned-three-harness-working.png") });
    await page.getByRole("button", { name: "管理成员", exact: true }).click();
    const members = page.getByRole("dialog", { name: "聊天成员", exact: true });
    for (const agent of agents) await expect(members).toContainText(agent.alias);
    await page.screenshot({ path: info.outputPath("aligned-members.png") });
    await members.getByRole("button", { name: "架构梳理 的操作", exact: true }).click();
    await expect(page.getByRole("menuitem", { name: "移出聊天", exact: true })).toBeVisible();
    await page.screenshot({ path: info.outputPath("aligned-member-actions.png") });
    expect(relay.commands.filter(command => ["query", "steer", "interrupt", "new_session"].includes(command.type))).toEqual([]);
  });
}

for (const width of [390, 1280]) {
  test(`collaboration native progress automatically pages with the native cursor at ${width}px`, async ({ page, isMobile }) => {
    await page.setViewportSize({ width, height: 900 });
    const relay = await mockRooms(page);
    await createSimpleRoom(page);
    relay.reply("member-0", "公开结论。");
    relay.setNativeHistory(command => {
      const id = command.before ? "native-oldest" : "native-latest";
      if (command.before) return {
        detail: "summary", authoritative: true, oldest_id: id, newest_id: id,
        has_more: false, events: [], turns: [{
          id, prompt: "较早的原生问题", done: true, detailLoaded: false,
          detailEventCount: 1, processDetailState: "present",
          blocks: [{ kind: "text", message_id: `${id}-answer`, channel: "final",
            text: "最早一页原生结论", done: true }],
        }],
      };
      return {
        detail: "full", authoritative: true, oldest_id: id, newest_id: id,
        has_more: !command.before, in_progress: false,
        events: [
          { type: "user_msg", sid: "native-0", msg_id: id, prompt: id, ts: 1 },
          { type: "assistant_msg_start", sid: "native-0", message_id: `${id}-answer`, channel: "final", ts: 2 },
          { type: "delta", sid: "native-0", message_id: `${id}-answer`, channel: "final",
            text: command.before ? "最早一页原生结论" : "最新的原生历史正文。\n\n".repeat(80), ts: 3 },
          { type: "assistant_msg_end", sid: "native-0", message_id: `${id}-answer`, channel: "final", ts: 4 },
          { type: "turn_end", sid: "native-0", turn_id: id, ts: 5,
            result: { subtype: "success", is_error: false } },
        ],
      };
    });
    const beforeOpen = relay.commands.length;
    await page.locator(".collab-answer").getByRole("button", { name: "查看进度 ↗", exact: true }).click();
    const progress = page.getByRole("dialog", { name: "Claude · default 的进度", exact: true });
    await expect(progress).toContainText("最新的原生历史正文。");
    const thread = progress.getByRole("region", { name: "对话记录", exact: true });
    await thread.evaluate(node => { node.scrollTop = 0; node.dispatchEvent(new Event("scroll")); });
    if (isMobile) {
      await thread.evaluate(node => {
        for (const [kind, y] of [["touchstart", 100], ["touchmove", 180], ["touchend", 180]] as const) {
          const event = new Event(kind, { bubbles: true, cancelable: true });
          const touch = { identifier: 1, clientX: 100, clientY: y };
          Object.defineProperty(event, "touches", { value: kind === "touchend" ? [] : [touch] });
          Object.defineProperty(event, "changedTouches", { value: [touch] });
          node.dispatchEvent(event);
        }
      });
    } else {
      await thread.hover();
      await page.mouse.wheel(0, -900);
    }
    await expect(progress).toContainText("最早一页原生结论");
    await expect(progress.locator(".load-more-status, .history-page-activity")).toHaveCount(0);
    expect(relay.commands.slice(beforeOpen).filter(command => command.type === "get_history"))
      .toEqual([
        expect.objectContaining({ session_id: "native-0" }),
        expect.objectContaining({ session_id: "native-0", before: "native-latest" }),
      ]);
    const oldest = progress.locator('[data-turn-id="native-oldest"]');
    await oldest.locator(".turn-process-head").click();
    await expect(oldest.locator(".turn-process-head")).toHaveAttribute("aria-busy", "false");
    await oldest.locator(".process-reasoning summary").click();
    await expect(oldest).toContainText("更早的原生过程详情");
    expect(relay.commands.findLast(command => command.type === "get_turn_detail"))
      .toMatchObject({ session_id: "native-0", turn_id: "native-oldest", revision: "revision" });
    await page.keyboard.press("Escape");
    await expect(progress).toHaveCount(0);
  });

  test(`collaboration page preview keeps the source member and native Viewer sandbox at ${width}px`, async ({ page, context }) => {
    await page.setViewportSize({ width, height: 900 });
    const relay = await mockRooms(page);
    const metadata: Record<string, any>[] = [];
    const opens: Record<string, any>[] = [];
    const site = { id: "collab-preview", machine_id: "room-machine", entry: "/report/index.html",
      label: "协作报告", revision: "a".repeat(32), urls: [] };
    const previewPage = { id: "b".repeat(32), site_id: site.id, machine_id: site.machine_id,
      entry: site.entry, label: site.label, references: ["report/index.html"], turn_ids: [], available: true };
    const grantId = "c".repeat(32);
    let associated = false;
    await page.route("**/api/viewers", route => route.fulfill({ json: { enabled: true, sites: [site] } }));
    await page.route("**/api/viewers/pages", route => {
      const body = route.request().postDataJSON();
      metadata.push(body);
      if (body.scope.sid === "native-1" && body.action === "resolve") associated = true;
      return route.fulfill({ json: { pages: body.scope.sid === "native-1" && associated ? [previewPage] : [] } });
    });
    await page.route("**/api/viewers/open", route => {
      opens.push(route.request().postDataJSON());
      return route.fulfill({ json: { id: grantId, mode: "isolated", origin: "https://collab-preview.example",
        entry: site.entry, expires_at: Date.now() / 1000 + 3600 } });
    });
    await page.route(`**/api/viewers/${grantId}`, route => route.fulfill({ json: { expires_at: Date.now() / 1000 + 3600 } }));
    await context.route("https://collab-preview.example/**", route => route.fulfill({
      contentType: "text/html; charset=utf-8", body: '<meta charset="utf-8"><h1>来自第二个原生会话的报告</h1>',
    }));
    await page.goto("/");
    await page.getByRole("button", { name: "切换新会话引擎", exact: true }).click();
    await page.getByRole("menuitem", { name: "协作", exact: true }).click();
    const create = page.getByRole("region", { name: "新建聊天", exact: true });
    await create.getByLabel("聊天名称").fill("来源页面预览");
    await addNewAgent(page);
    await selectChoice(page, "选择参与者 2", "Codex · primary");
    await create.getByRole("button", { name: "创建聊天", exact: true }).click();
    const beforePreview = relay.commands.length;
    relay.reply("member-0", "没有页面的公开结论。");
    relay.reply("member-1", "报告已完成。[报告](report/index.html)");
    const answer = page.locator('.collab-answer[data-engine="codex"]');
    const open = answer.getByRole("button", { name: "查看页面", exact: true });
    await open.focus();
    await open.press("Enter");
    const preview = page.getByRole("dialog", { name: "页面预览", exact: true });
    const dialogBounds = (await preview.boundingBox())!;
    const panelBounds = (await preview.locator(".remote-viewer-panel").boundingBox())!;
    expect(panelBounds.x).toBeGreaterThanOrEqual(dialogBounds.x);
    expect(panelBounds.x + panelBounds.width).toBeLessThanOrEqual(dialogBounds.x + dialogBounds.width + 1);
    await expect(preview.frameLocator("iframe").getByRole("heading", { name: "来自第二个原生会话的报告" })).toBeVisible();
    await expect(preview.locator("iframe")).toHaveAttribute("sandbox", "allow-scripts allow-same-origin");
    expect(metadata.filter(request => request.action === "resolve")).toEqual([
      expect.objectContaining({ scope: { machine_id: "room-machine", sid: "native-1", engine: "codex", space: "code" },
        paths: ["report/index.html"] }),
    ]);
    expect(opens).toEqual([{ machine_id: "room-machine", site_id: site.id,
      parent_machine_id: "room-machine", sid: "native-1", entry: site.entry }]);
    await page.mouse.click(2, 2);
    await expect(preview).toHaveCount(0);
    await expect(open).toBeFocused();
    expect(relay.commands.slice(beforePreview).filter(command => ["query", "steer", "interrupt", "switch_session", "new_session"].includes(command.type))).toEqual([]);
  });

  for (const engine of ["claude", "codex", "dsh"] as const) {
    test(`collaboration progress reuses native ${engine} history and working marks at ${width}px`, async ({ page }, info) => {
      await page.setViewportSize({ width, height: 900 });
      const relay = await mockRooms(page);
      await page.goto("/");
      await page.getByRole("button", { name: "切换新会话引擎", exact: true }).click();
      await page.getByRole("menuitem", { name: "协作", exact: true }).click();
      const create = page.getByRole("region", { name: "新建聊天", exact: true });
      const identity = engine === "claude" ? "Claude · default" : engine === "codex" ? "Codex · iris" : "DSH · default";
      await selectChoice(page, "选择参与者 1", identity);
      await create.getByLabel("聊天名称").fill("原生进度窗口回归");
      await create.getByRole("button", { name: "创建聊天", exact: true }).click();
      relay.reply("member-0", "公开结论。");
      const sid = engine === "dsh" ? "dsh@native-0" : "native-0";
      relay.setNativeHistory({ detail: "full", authoritative: true, oldest_id: "native-turn", newest_id: "native-turn", has_more: false,
        live_seq: 5, in_progress: false,
        events: [
          { type: "user_msg", sid, msg_id: "native-turn", prompt: "原生内部问题", ts: 1 },
          { type: "assistant_msg_start", sid, message_id: "native-answer", channel: "final", ts: 2 },
          { type: "delta", sid, message_id: "native-answer", channel: "final", text: "原生过程结论。[文件](note.md)", ts: 3 },
          { type: "assistant_msg_end", sid, message_id: "native-answer", channel: "final", ts: 4 },
          { type: "turn_end", sid, turn_id: "native-turn", ts: 5,
            result: { subtype: "success", is_error: false, duration_ms: 31_000 } },
        ] });
      const entry = page.locator(".collab-answer").getByRole("button", { name: "查看进度 ↗", exact: true });
      await entry.click();
      const progress = page.getByRole("dialog", { name: `${identity} 的进度`, exact: true });
      await expect(progress).toContainText("原生过程结论。");
      await expect(progress.locator(".turn-done-mark")).toHaveCount(1);
      await expect(progress.locator(".turn-working")).toHaveCount(0);
      await expect(progress.getByText("正在恢复历史…", { exact: true })).toHaveCount(0);
      await expect(progress.getByText("加载会话历史…", { exact: true })).toHaveCount(0);
      await expect(progress.locator("header .collab-working, header .working-word")).toHaveCount(0);
      await expect(progress.locator("header .engine-brand-icon")).toHaveCount(1);
      await expect(progress.getByRole("button", { name: "刷新进度", exact: true })).toHaveCount(0);
      await expect(progress.getByRole("button", { name: "关闭进度", exact: true })).toHaveCount(0);
      await expect(progress.getByRole("button", { name: "查看原生会话", exact: true })).toBeVisible();
      const box = (await progress.boundingBox())!;
      expect(Math.abs(box.x + box.width / 2 - width / 2)).toBeLessThanOrEqual(1);
      expect(Math.abs(box.y + box.height / 2 - 450)).toBeLessThanOrEqual(1);
      const file = progress.getByRole("button", { name: "在 Remote 中打开 /tmp/project/note.md", exact: true });
      await file.hover();
      await expect(progress.getByRole("tooltip")).toHaveText("/tmp/project/note.md");
      await file.click();
      const preview = page.getByRole("dialog", { name: "文件预览", exact: true });
      await expect(preview).toContainText(`来自 ${sid}`);
      expect(relay.commands.findLast(command => command.type === "get_file_preview")).toMatchObject({ sid, path: "/tmp/project/note.md" });
      await page.keyboard.press("Escape");
      await expect(preview).toHaveCount(0);
      await expect(progress).toBeVisible();
      relay.emit({ type: "state", sid, state: "running", msg_id: "live-turn", seq: 6, ts: 6 });
      relay.emit({ type: "user_msg", sid, msg_id: "live-turn", prompt: "继续核对", seq: 7, ts: 7 });
      relay.emit({ type: "turn_binding", sid, msg_id: "live-turn", turn_id: "live-owner", seq: 8, ts: 8 });
      relay.emit({ type: "assistant_msg_start", sid, message_id: "live-answer", channel: "final", seq: 9, ts: 9 });
      relay.emit({ type: "delta", sid, message_id: "live-answer", channel: "final", text: "正在核对。", seq: 10, ts: 10 });
      await expect(progress.locator(".turn-working .working-word")).toBeVisible();
      await expect(progress.locator(".turn-working > :first-child")).toBeVisible();
      const color = await progress.locator(".turn-working").evaluate(node => getComputedStyle(node).color);
      expect(color).toBe({ claude: "rgb(219, 124, 94)", codex: "rgb(163, 172, 255)", dsh: "rgb(148, 189, 255)" }[engine]);
      if (engine === "claude") await expect(progress.locator(".turn-working .claude-working-mark.is-alternating")).toBeVisible();
      await progress.locator("header strong").hover();
      await expect(progress.locator(".working-word-layer.is-current")).toHaveCSS("opacity", "1");
      await page.screenshot({ path: info.outputPath(`native-${engine}-progress-${width}.png`) });
      const beforeDismiss = relay.commands.length;
      await page.mouse.click(2, 2);
      await expect(progress).toHaveCount(0);
      await expect(entry).toBeFocused();
      expect(relay.commands.slice(beforeDismiss).filter(command => ["query", "steer", "interrupt", "switch_session"].includes(command.type))).toEqual([]);
      await expect(page.locator(".collab-thread")).not.toContainText("原生内部问题");
      await entry.click();
      await expect(progress.locator(".turn-working .working-word")).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(progress).toHaveCount(0);
    });
  }

  test(`collaboration previews files and media from the reply's native session at ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 });
    const relay = await mockRooms(page);
    await page.goto("/");
    await page.getByRole("button", { name: "切换新会话引擎", exact: true }).click();
    await page.getByRole("menuitem", { name: "协作", exact: true }).click();
    const create = page.getByRole("region", { name: "新建聊天", exact: true });
    await create.getByLabel("聊天名称").fill("按来源预览");
    await addNewAgent(page);
    await selectChoice(page, "选择参与者 2", "Codex · primary");
    await create.getByRole("button", { name: "配置参与者 2", exact: true }).click();
    const settings = page.getByRole("dialog", { name: "Codex · primary", exact: true });
    await settings.getByRole("textbox", { name: "工作目录", exact: true }).fill("/tmp/other");
    await settings.getByRole("button", { name: "完成", exact: true }).click();
    await create.getByRole("button", { name: "创建聊天", exact: true }).click();
    const nativeBeforePreviews = relay.commands.length;
    relay.reply("member-0", "[同名文件](note.md)");
    relay.reply("member-1", "[同名文件](note.md) · [音频](sound.wav) · [视频](clip.mp4) · [图片](result.png) · [HTML](report.html)\n\n![内嵌图片](inline.png)");
    const first = page.locator('.collab-answer[data-engine="claude"]');
    const second = page.locator('.collab-answer[data-engine="codex"]');
    const preview = page.getByRole("dialog", { name: "文件预览", exact: true });
    await first.getByRole("button", { name: "在 Remote 中打开 /tmp/project/note.md", exact: true }).click();
    await expect(preview).toContainText("来自 native-0");
    await page.keyboard.press("Escape");
    for (const [path, selector] of [["note.md", ".markdown-preview"], ["sound.wav", "audio"], ["clip.mp4", "video"],
      ["result.png", ".artifact-image-stage img"], ["report.html", ".artifact-html-stage iframe"]]) {
      await second.getByRole("button", { name: `在 Remote 中打开 /tmp/other/${path}`, exact: true }).click();
      await expect(preview.locator(selector)).toBeVisible();
      const request = relay.commands.findLast(command => command.type === "get_file_preview")!;
      expect(request).toMatchObject({ sid: "native-1", path: `/tmp/other/${path}` });
      relay.emit({ type: "file_preview", sid: "native-0", path: "/wrong/file", request_id: request.request_id,
        format: "text", content: "错误来源内容", size: 0, mtime_ns: "1", truncated: false });
      await expect(preview).not.toContainText("错误来源内容");
      await page.keyboard.press("Escape");
      await expect(preview).toHaveCount(0);
    }
    const inlineImage = second.getByRole("button", { name: "预览图片：内嵌图片", exact: true });
    await expect(inlineImage).toBeVisible();
    expect(relay.commands.filter(command => command.type === "get_preview_asset" && command.path === "/tmp/other/inline.png"))
      .toEqual([expect.objectContaining({ sid: "native-1" })]);
    await inlineImage.click();
    const imagePreview = page.getByRole("dialog", { name: "内嵌图片", exact: true });
    await expect(imagePreview.getByRole("img", { name: "内嵌图片", exact: true })).toBeVisible();
    await expect.poll(() => imagePreview.locator("img").evaluate(node => (node as HTMLImageElement).naturalWidth)).toBe(1);
    const imageStage = (await imagePreview.locator(".artifact-image-stage").boundingBox())!;
    expect(imageStage.height).toBeGreaterThan(200);
    expect(imageStage.width).toBeGreaterThan(250);
    await imagePreview.getByRole("button", { name: "关闭预览", exact: true }).click();
    await expect(imagePreview).toHaveCount(0);
    const body = (await second.locator(".prose").boundingBox())!;
    const links = (await second.locator(".collab-native-links").boundingBox())!;
    expect(links.y).toBeGreaterThanOrEqual(body.y + body.height);
    await page.screenshot({ path: info.outputPath(`file-links-${width}.png`) });
    expect(relay.commands.slice(nativeBeforePreviews).filter(command => ["query", "steer", "interrupt", "switch_session", "new_session"].includes(command.type))).toEqual([]);
  });
}

test("collaboration follows native renames in reply headings, mentions, responder and an unsent mention", async ({ page }) => {
  const relay = await mockRooms(page);
  await createSimpleRoom(page);
  relay.reply("member-0", "公开结论。");
  await page.getByRole("button", { name: "点名参与者", exact: true }).click();
  await page.getByRole("option", { name: "Claude · default", exact: true }).click();
  const composer = page.getByRole("textbox", { name: "协作消息", exact: true });
  await composer.press("End");
  await composer.type("核对一下");
  relay.renameFromHarness("member-0", "原生会话新名称");
  await expect(page.locator(".collab-answer header strong")).toHaveText("原生会话新名称");
  await expect(composer).toHaveValue("@原生会话新名称 核对一下");
  await expect(page.getByRole("button", { name: "默认回复者", exact: true })).toContainText("原生会话新名称");
  await composer.press("Enter");
  await expect.poll(() => relay.commands.findLast(command => command.operation === "send")?.targets).toEqual(["member-0"]);
});
