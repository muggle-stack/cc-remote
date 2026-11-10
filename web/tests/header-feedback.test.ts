import assert from "node:assert/strict";
import { createServer } from "vite";
import type { Notice } from "../src/protocol.ts";

const harness = await createServer({
  root: process.cwd(), appType: "custom", logLevel: "silent",
  server: { middlewareMode: true, watch: null },
});
try {
  const { advanceConnectionFeedback: advance, connectionFeedbackItem: item,
    headerConnectionPhase: phase, isHeaderNotice, headerBannerKind,
    headerNoticeItem } = await harness.ssrLoadModule("/src/header-feedback.ts");
  const { initialState, reduce } = await harness.ssrLoadModule("/src/reducer.ts");

  let connection = advance(null, "device-a", "connecting");
  connection = advance(connection, "device-a", "syncing");
  connection = advance(connection, "device-a", null);
  assert.equal(item(connection), null, "the first connection is not a reconnection receipt");
  connection = advance(connection, "device-a", "offline");
  assert.equal(item(connection).label, "设备离线");
  const episode = connection.episode;
  connection = { ...connection, dismissed: true };
  for (const next of ["offline", "reconnecting", "syncing", "syncing", null]) {
    connection = advance(connection, "device-a", next);
    assert.equal(item(connection), null, "hiding one outage must cover its entire recovery");
  }
  connection = advance(connection, "device-a", "offline");
  assert.ok(connection.episode > episode);
  assert.equal(item(connection).label, "设备离线", "a new outage is a new event");
  connection = advance(connection, "device-a", "syncing");
  connection = advance(connection, "device-a", null);
  assert.equal(item(connection).label, "已重连");
  connection = advance(connection, "device-b", null);
  assert.equal(item(connection), null, "reconnection success cannot cross devices");
  connection = advance(connection, "device-b", "syncing");
  connection = advance(connection, "device-b", null);
  assert.equal(item(connection), null, "history recovery alone is not a reconnection");

  assert.equal(phase("connected", true, false, "machine reconnected — syncing…"), null,
    "a stale connection banner must not keep the header syncing after readiness");
  assert.equal(phase("connected", false, false, "machine offline — waiting for reconnect"), "offline");
  assert.equal(phase("connected", false, false, "machine reconnected — syncing…"), "syncing");
  assert.equal(phase("connected", true, true), "syncing");
  assert.equal(phase("reconnecting", true, false), "reconnecting");
  assert.equal(phase("disconnected", false, false), "disconnected",
    "an exhausted protocol recovery must not claim to still be reconnecting");
  assert.equal(headerBannerKind("操作未完成，请稍后重试。"), "failure");
  assert.equal(headerBannerKind("通知权限未开启，设置没有更改。"), "failure");
  assert.equal(headerBannerKind("客户端与服务器版本不一致，请刷新页面。"), "failure");
  assert.equal(headerBannerKind("当前操作不适用于这个会话。"), "failure");

  const base = { type: "notice", v: 85, ts: 1, severity: "warning", category: "runtime",
    title: "操作需要处理", message: "稍后重试" } as Notice;
  assert.equal(isHeaderNotice({ ...base, notice_id: "codex-notice-1" }), false);
  for (const prefix of ["schedule-", "compact-"]) {
    assert.equal(isHeaderNotice({ ...base, notice_id: prefix + "1" }), true);
  }
  assert.equal(isHeaderNotice({ ...base, notice_id: "compact-done", severity: "info" }), false);
  assert.equal(isHeaderNotice({ ...base, notice_id: "security-1", category: "security" }), true);
  const rollback = { ...base, notice_id: "rollback-1", severity: "info",
    message: "对话：已恢复 · 代码：已恢复" } as Notice;
  assert.equal(isHeaderNotice(rollback), true);
  assert.equal(headerNoticeItem(rollback, "a").label, "已撤销");
  assert.equal(headerNoticeItem(rollback, "a").message, rollback.message);
  assert.notEqual(headerNoticeItem(rollback, "a").id, headerNoticeItem(rollback, "b").id);
  assert.equal(headerNoticeItem({ ...rollback, severity: "warning" }, "a").label, "操作未完成");
  const checkpoint = { ...base, notice_id: "checkpoint-1", title: "本轮代码回滚不可用",
    message: "本轮修改了 Git 暂存区或包含当前无法安全记录的文件；对话仍可正常回滚。" } as Notice;
  assert.equal(isHeaderNotice(checkpoint), true, "file rollback limitations use the compact status entry");
  const limited = headerNoticeItem(checkpoint, "a");
  assert.equal(limited.label, "回滚受限");
  assert.equal(limited.tone, "warning", "a limitation is not a successful undo receipt");
  assert.equal(limited.message, checkpoint.message, "the popover preserves the reason and conversation rollback availability");
  assert.equal(limited.noticeId, checkpoint.notice_id);
  assert.notEqual(limited.id, headerNoticeItem(checkpoint, "b").id);

  let state = reduce(initialState, { type: "command_error", detail: "操作未完成，请稍后重试。" });
  const first = state.bannerRevision;
  state = reduce(state, { type: "command_error", detail: state.banner });
  assert.ok(state.bannerRevision > first, "two failed commands with identical copy are separate events");
  const later = state;
  state = reduce(state, { type: "dismiss_banner", banner: state.banner, revision: first });
  assert.equal(state, later, "a delayed dismissal cannot hide a newer failure");
  state = reduce(state, { type: "dismiss_banner", banner: state.banner, revision: state.bannerRevision });
  assert.equal(state.banner, undefined);
  state = reduce(state, { type: "conn", connState: "connected" });
  state = reduce(state, { type: "event", event: { type: "wrapper_disconnected" } });
  state = reduce(state, { type: "command_error", detail: "操作未完成，请稍后重试。" });
  assert.equal(phase(state.connState, state.wrapperOnline, false, state.banner, state.wrapperConnection), "offline",
    "command feedback must not replace authoritative device offline state with syncing");
  console.log("header feedback lifecycle tests passed");
} finally {
  await harness.close();
}
