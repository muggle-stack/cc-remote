import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

import type { Notice, ServerEvent, StatusReport } from "../src/protocol.ts";
import {
  resetCreditConfirmation, resetCreditPresentation,
} from "../src/rate-limit-reset.ts";

const harness = await createServer({
  root: process.cwd(),
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, watch: null },
});

try {
  const {
    createRuntime, initialState, reduce, MAX_SESSION_NOTICES,
  } = await harness.ssrLoadModule("/src/reducer.ts");
  const { NoticeStack } = await harness.ssrLoadModule(
    "/src/components/NoticeStack.tsx");
  const { StatusSheet } = await harness.ssrLoadModule(
    "/src/components/StatusSheet.tsx");
  const { UsageMeter } = await harness.ssrLoadModule(
    "/src/components/UsageMeter.tsx");
  const {
    accountQuotaWindows, activeRateLimits, quotaTone, quotaWindowLabel,
    quotaWindowsForLimits, remainingPercent,
  } = await harness.ssrLoadModule("/src/rate-limit-usage.ts");
  const { statusNotices } = await harness.ssrLoadModule(
    "/src/notice-presentation.ts");
  const {
    ReconnectBanner, TRANSIENT_BANNER_TTL_MS,
  } = await harness.ssrLoadModule("/src/components/ReconnectBanner.tsx");
  const { ErrorBoundary } = await harness.ssrLoadModule(
    "/src/ErrorBoundary.tsx");
  const {
    presentCommandProblem, presentHistoricalTurnProblem, presentTurnProblem,
  } = await harness.ssrLoadModule("/src/problem-presentation.ts");
  const sid = "notice-session";
  const event = (body: Record<string, unknown>): ServerEvent => ({
    v: 10, ts: 10, sid, ...body,
  } as ServerEvent);
  let state = {
    ...initialState,
    banner: "machine reconnected — syncing…",
    focusedSid: sid,
    runtimes: { [sid]: createRuntime() },
  };

  // Per-session retention is bounded and duplicate ids replace/move instead of
  // growing the list.  Notice reduction must not mutate the reconnect banner.
  for (let index = 0; index < MAX_SESSION_NOTICES + 3; index += 1) {
    state = reduce(state, { type: "event", event: event({
      type: "notice",
      notice_id: `notice-${index}`,
      severity: "warning",
      category: "runtime",
      title: `warning ${index}`,
      message: "bounded message",
    }) });
  }
  assert.equal(state.runtimes[sid].notices.length, MAX_SESSION_NOTICES);
  assert.equal(state.runtimes[sid].notices[0].notice_id, "notice-3");
  state = reduce(state, { type: "event", event: event({
    type: "notice",
    notice_id: "notice-3",
    severity: "info",
    category: "deprecation",
    title: "updated",
    message: "same id",
  }) });
  assert.equal(state.runtimes[sid].notices.length, MAX_SESSION_NOTICES);
  assert.equal(state.runtimes[sid].notices.at(-1)?.title, "updated");
  assert.equal(state.banner, "machine reconnected — syncing…");

  state = reduce(state, {
    type: "dismiss_notice", sid, noticeId: "notice-3",
  });
  assert.equal(state.runtimes[sid].notices.some(
    (notice: Notice) => notice.notice_id === "notice-3"), false);

  state = reduce(state, {
    type: "command_error", detail: "详细过程已过期，请刷新会话后重试",
  });
  const staleDismiss = reduce(state, {
    type: "dismiss_banner", banner: "older warning",
  });
  assert.equal(staleDismiss.banner, "详细过程已过期，请刷新会话后重试",
    "an old timer must not dismiss a newer banner");
  state = reduce(state, {
    type: "dismiss_banner", banner: "详细过程已过期，请刷新会话后重试",
  });
  assert.equal(state.banner, undefined);

  const report = event({
    type: "status_report",
    thread: { thread_id: sid, status: "idle", active_flags: [] },
    runtime: {},
    context: {},
    account: null,
    rate_limits: [{
      limit_id: "codex", limit_name: "Codex", plan_type: "pro",
      primary: { used_percent: 40, resets_at: 900, window_duration_mins: 300 },
    }],
    usage: null,
    component_errors: [],
  }) as StatusReport;
  state = reduce(state, { type: "event", event: report });
  state = reduce(state, { type: "event", event: event({
    type: "rate_limit_update",
    limit_id: "codex",
    name: null,
    plan_type: null,
    reached_type: null,
    primary: { used_percent: 0, resets_at: 901, window_duration_mins: null },
    secondary: null,
  }) });
  let merged = state.runtimes[sid].statusReport?.rate_limits[0];
  assert.equal(merged?.primary?.used_percent, 40,
    "same-period live updates must not regress an authoritative percentage");
  assert.equal(merged?.primary?.resets_at, 901,
    "one-second provider reset jitter still belongs to the current period");
  state = reduce(state, { type: "event", event: event({
    type: "rate_limit_update",
    limit_id: "codex",
    name: null,
    plan_type: null,
    reached_type: null,
    primary: {
      used_percent: 0, resets_at: 18_900, window_duration_mins: 300,
    },
    secondary: null,
  }) });
  merged = state.runtimes[sid].statusReport?.rate_limits[0];
  assert.equal(merged?.primary?.used_percent, 0,
    "a confirmed new quota period may lower the used percentage");
  assert.equal(merged?.primary?.resets_at, 18_900);
  state = reduce(state, { type: "event", event: event({
    type: "rate_limit_update",
    limit_id: "codex",
    name: null,
    plan_type: null,
    reached_type: null,
    primary: { used_percent: 100, resets_at: 900, window_duration_mins: 300 },
    secondary: null,
  }) });
  merged = state.runtimes[sid].statusReport?.rate_limits[0];
  assert.equal(merged?.primary?.used_percent, 0,
    "a late snapshot from the previous period must be ignored");
  assert.equal(merged?.primary?.resets_at, 18_900);
  state = reduce(state, { type: "event", event: report });
  state = reduce(state, { type: "event", event: event({
    type: "rate_limit_update",
    limit_id: "codex",
    name: null,
    plan_type: null,
    reached_type: "rate_limit_reached",
    primary: { used_percent: 100, resets_at: null, window_duration_mins: null },
    secondary: null,
  }) });
  merged = state.runtimes[sid].statusReport?.rate_limits[0];
  assert.equal(merged?.limit_name, "Codex");
  assert.equal(merged?.plan_type, "pro");
  assert.equal(merged?.primary?.used_percent, 100);
  assert.equal(merged?.primary?.resets_at, 900);
  assert.equal(merged?.rate_limit_reached_type, "rate_limit_reached");
  assert.equal(Object.hasOwn(merged ?? {}, "credits"), false);
  assert.equal(Object.hasOwn(merged ?? {}, "individualLimit"), false);
  state = reduce(state, { type: "event", event: event({
    type: "rate_limit_update",
    limit_id: "codex",
    name: null,
    plan_type: null,
    reached_type: "",
    primary: { used_percent: 10, resets_at: null, window_duration_mins: 300 },
    secondary: null,
  }) });
  merged = state.runtimes[sid].statusReport?.rate_limits[0];
  assert.equal(merged?.rate_limit_reached_type, "");
  assert.equal(merged?.primary?.used_percent, 10,
    "an explicit allowed transition replaces exhausted usage without reset proof");
  assert.equal(merged?.primary?.resets_at, null,
    "an explicit allowed transition clears the exhausted period's old reset");

  // A delayed status response must not replace the newest in-flight read.
  state = reduce(state, {
    type: "begin_status_request", sid, requestId: "status-new",
  });
  const staleStatus = {
    ...report, request_id: "status-old",
    rate_limits: [{
      limit_id: "codex", primary: { used_percent: 100 },
    }],
  } as StatusReport;
  state = reduce(state, { type: "event", event: staleStatus });
  assert.equal(state.runtimes[sid].statusRequestId, "status-new");
  assert.equal(state.runtimes[sid].statusReport?.rate_limits[0]?.primary?.used_percent, 10,
    "stale status response must not replace the installed report");
  const uncorrelatedStatus = { ...staleStatus, request_id: undefined } as StatusReport;
  state = reduce(state, { type: "event", event: uncorrelatedStatus });
  assert.equal(state.runtimes[sid].statusRequestId, "status-new");
  assert.equal(state.runtimes[sid].statusReport?.rate_limits[0]?.primary?.used_percent, 10,
    "uncorrelated status response must not replace an in-flight request");
  const currentStatus = {
    ...report, request_id: "status-new",
    rate_limits: [{
      limit_id: "codex", primary: { used_percent: 0 },
    }],
  } as StatusReport;
  state = reduce(state, { type: "event", event: currentStatus });
  assert.equal(state.runtimes[sid].statusRequestId, null);
  assert.equal(state.runtimes[sid].statusReport?.rate_limits[0]?.primary?.used_percent, 0,
    "the matching status response installs and completes the request");

  const resetStatus = {
    ...currentStatus,
    request_id: "reset-status",
    reset_credits: {
      available_count: 2,
      credits: [{
        id: "credit/one",
        granted_at: 1_700_000_000,
        expires_at: 1_800_000_000,
        reset_type: "codexRateLimits" as const,
        status: "available" as const,
        title: "Launch reset",
        description: "Reset an eligible Codex limit window",
      }],
    },
  } satisfies StatusReport;
  state = reduce(state, { type: "event", event: resetStatus });
  state = reduce(state, {
    type: "begin_status_request", sid, requestId: "reset-current",
  });
  state = reduce(state, { type: "event", event: event({
    type: "rate_limit_reset_result",
    to: "browser",
    request_id: "reset-stale",
    outcome: "reset",
    credit_id: "credit/one",
  }) });
  assert.equal(state.runtimes[sid].statusRequestId, "reset-current");
  assert.equal(state.runtimes[sid].resetCreditResult, null,
    "a stale coupon result must not settle a newer click");
  state = reduce(state, { type: "event", event: event({
    type: "rate_limit_reset_result",
    to: "browser",
    request_id: "reset-current",
    outcome: "reset",
    credit_id: "credit/one",
  }) });
  assert.equal(state.runtimes[sid].statusRequestId, null);
  assert.equal(state.runtimes[sid].resetCreditResult?.outcome, "reset");
  assert.equal(state.runtimes[sid].statusReport?.reset_credits, null,
    "coupon inventory must stay hidden until the authoritative refresh arrives");
  state = reduce(state, { type: "event", event: {
    ...resetStatus,
    request_id: "reset-current",
    reset_credits: { available_count: 1, credits: null },
  } });
  assert.equal(state.runtimes[sid].statusReport?.reset_credits?.available_count, 1);

  state = reduce(state, {
    type: "begin_status_request", sid, requestId: "reset-error",
  });
  state = reduce(state, { type: "event", event: event({
    type: "error", code: "internal", message: "provider secret detail",
    request_id: "reset-error",
  }) });
  assert.equal(state.runtimes[sid].statusRequestId, null);
  assert.equal(state.runtimes[sid].resetCreditResult, null);
  assert.equal(state.runtimes[sid].statusError,
    "操作未完成，请稍后重试。");
  assert.equal(state.runtimes[sid].statusReport?.reset_credits, null,
    "an ambiguous status operation must hide stale coupon inventory");

  const claudeSid = "claude-rate-session";
  let claudeState = {
    ...initialState,
    focusedSid: claudeSid,
    runtimes: { [claudeSid]: createRuntime() },
  };
  for (const update of [{
    limit_id: "claude", name: "Claude", primary: {
      used_percent: 35, resets_at: 1_800_000_000,
      window_duration_mins: 300,
    },
  }, {
    limit_id: "claude", name: "Claude", secondary: {
      used_percent: 70, resets_at: 1_800_500_000,
      window_duration_mins: 10_080,
    },
  }, {
    limit_id: "claude-seven-day-opus", name: "Opus", primary: {
      used_percent: 80, resets_at: 1_800_500_000,
      window_duration_mins: 10_080,
    },
  }]) {
    claudeState = reduce(claudeState, { type: "event", event: event({
      type: "rate_limit_update", sid: claudeSid,
      plan_type: null, reached_type: null,
      primary: null, secondary: null, ...update,
    }) });
  }
  assert.equal(claudeState.runtimes[claudeSid].statusReport, null,
    "Claude quota must not fabricate a Codex StatusReport");
  assert.equal(claudeState.runtimes[claudeSid].rateLimits.length, 2);
  const claudeQuotas = quotaWindowsForLimits(
    claudeState.runtimes[claudeSid].rateLimits, "claude",
  );
  assert.equal(remainingPercent(claudeQuotas.fiveHour), 65);
  assert.equal(remainingPercent(claudeQuotas.weekly), 30);
  assert.equal(activeRateLimits([{
    limit_id: "claude", primary: {
      used_percent: 99, resets_at: 999, window_duration_mins: 300,
    },
  }], 1_000).length, 0,
  "elapsed SDK windows must disappear without waiting for another event");

  const officialDiagnostic = event({
    type: "notice", notice_id: "codex-notice-private-diagnostic",
    severity: "warning", category: "runtime",
    title: "Codex runtime warning",
    message: "provider crash at /private/token; see wrapper logs",
    detail: "Traceback: secret",
  }) as Notice;
  const officialConversationMarkup = renderToStaticMarkup(createElement(
    NoticeStack, { notices: [officialDiagnostic], onDismiss: () => {} }));
  assert.equal(officialConversationMarkup, "",
    "official app-server diagnostics must not interrupt the transcript");
  const safeStatusNotices = statusNotices([officialDiagnostic]);
  assert.equal(safeStatusNotices.length, 1);
  assert.doesNotMatch(safeStatusNotices.map((notice: Notice) =>
    [notice.title, notice.message, notice.detail ?? ""].join(" ")).join(" "),
    /crash|warning|wrapper|private|traceback|secret/i);

  const hiddenDiagnostic = "provider crash at /private/token; see wrapper logs";
  const policyFailure = "上游模型因安全策略拒绝了本次请求（cyber_policy）。"
    + "这不是本地权限或网络错误；请核实并说明任务背景与授权范围，"
    + "若属误判请向服务提供方反馈。";
  assert.equal(presentTurnProblem({ code: "cc_crash", message: policyFailure }),
    policyFailure);
  assert.equal(presentHistoricalTurnProblem(policyFailure), policyFailure);
  for (const message of [policyFailure + " secret-token", "cyber_policy private diagnostic"]) {
    assert.equal(presentTurnProblem({ code: "cc_crash", message }),
      "本次回复未完成，请重试。");
    assert.equal(presentHistoricalTurnProblem(message), "该轮未正常结束");
  }
  assert.equal(presentTurnProblem({ code: "cc_crash", message: hiddenDiagnostic }),
    "本次回复未完成，请重试。");
  for (const safeMessage of [
    "网络异常，连接失败，请重新尝试。",
    "网络连接异常，请检查网络后重试。",
    "模型服务认证已失效或当前账号无权限，请检查当前服务的凭据或账号权限后重试。",
    "请求过于频繁或当前额度受限，请稍后重试。",
    "请求超时，请重新尝试。",
    "Codex 上游服务暂时不可用，请稍后重试。",
  ]) {
    assert.equal(
      presentTurnProblem({ code: "cc_crash", message: safeMessage }),
      safeMessage,
    );
  }
  assert.equal(presentCommandProblem({ code: "internal", message: hiddenDiagnostic }),
    "操作未完成，请稍后重试。");
  assert.doesNotMatch(
    presentCommandProblem({ code: "protocol", message: hiddenDiagnostic }),
    /crash|wrapper|private|protocol/i);
  assert.equal(presentHistoricalTurnProblem("error"), "该轮未正常结束");
  assert.equal(
    presentHistoricalTurnProblem(
      "provider crash at /private/token; Authorization: Bearer secret",
    ),
    "该轮未正常结束",
  );
  assert.equal(
    presentHistoricalTurnProblem("网络连接异常，请检查网络后重试。"),
    "网络连接异常，请检查网络后重试。",
  );
  assert.equal(
    presentHistoricalTurnProblem(
      "Codex 登录已失效或当前账号无权限，请重新登录后重试。",
    ),
    "模型服务认证已失效或当前账号无权限，请检查当前服务的凭据或账号权限后重试。",
    "a cached pre-upgrade auth failure is migrated to provider-neutral copy",
  );

  const markup = renderToStaticMarkup(createElement(NoticeStack, {
    notices: state.runtimes[sid].notices,
    onDismiss: () => {},
  }));
  assert.match(markup, /notice-stack/);
  assert.equal((markup.match(/notice-dismiss/g) ?? []).length,
    state.runtimes[sid].notices.length);
  assert.doesNotMatch(markup, /\bwarning\b|crash|traceback/i,
    "conversation notices must use product copy instead of diagnostics");

  const statusMarkup = renderToStaticMarkup(createElement(StatusSheet, {
    open: true, report, notices: [officialDiagnostic], error: null,
    onClose: () => {}, onRefresh: () => {}, onDismissNotice: () => {},
  }));
  assert.match(statusMarkup, /需要关注/);
  assert.match(statusMarkup, /运行状态/);
  assert.match(statusMarkup, /剩余 60%/);
  assert.match(statusMarkup, /5 小时窗口/);
  assert.match(statusMarkup, /width:60%/);
  assert.doesNotMatch(statusMarkup, /40% 已用/);
  assert.doesNotMatch(statusMarkup,
    /crash|warning|wrapper|private|traceback|secret|rate_limit_reached/i,
    "the status sheet must not expose provider diagnostics or raw enums");

  const quotaReport = {
    ...report,
    account: {
      auth_type: "chatgpt", plan_type: "plus", requires_openai_auth: true,
    },
    rate_limits: [{
      limit_id: "codex", limit_name: "Codex", plan_type: "plus",
      primary: {
        used_percent: 40, resets_at: 1_800_000_000,
        window_duration_mins: 300,
      },
      secondary: {
        used_percent: 75, resets_at: 1_800_500_000,
        window_duration_mins: 10_080,
      },
    }],
  } satisfies StatusReport;
  const quotas = accountQuotaWindows(quotaReport);
  assert.equal(remainingPercent(quotas.fiveHour), 60);
  assert.equal(remainingPercent(quotas.weekly), 25);
  assert.equal(quotaTone(51), "good");
  assert.equal(quotaTone(50), "warn");
  assert.equal(quotaTone(21), "warn");
  assert.equal(quotaTone(20), "critical");
  assert.equal(quotaWindowLabel(300), "5 小时窗口");
  assert.equal(quotaWindowLabel(10_080), "一周窗口");
  assert.equal(quotaWindowLabel(1_440), "1440 分钟窗口");
  const separateQuotaLimits = accountQuotaWindows({
    ...quotaReport,
    rate_limits: [{
      limit_id: null, primary: {
        used_percent: 10, resets_at: 1_800_000_000,
        window_duration_mins: 300,
      }, secondary: null,
    }, {
      limit_id: null, primary: null, secondary: {
        used_percent: 90, resets_at: 1_800_500_000,
        window_duration_mins: 10_080,
      },
    }],
  });
  assert.equal(separateQuotaLimits.limit?.limit_id, null);
  assert.equal(remainingPercent(separateQuotaLimits.fiveHour), 90);
  assert.equal(separateQuotaLimits.weekly, null,
    "weekly quota from a different limit must not be combined");
  const pairedQuotaLimit = accountQuotaWindows({
    ...quotaReport,
    rate_limits: [{
      limit_id: null, primary: {
        used_percent: 10, resets_at: 1_800_000_000,
        window_duration_mins: 300,
      }, secondary: null,
    }, {
      limit_id: null, primary: {
        used_percent: 40, resets_at: 1_800_000_000,
        window_duration_mins: 300,
      }, secondary: {
        used_percent: 75, resets_at: 1_800_500_000,
        window_duration_mins: 10_080,
      },
    }],
  });
  assert.equal(pairedQuotaLimit.limit?.limit_id, null,
    "a complete quota pair takes precedence over an earlier partial limit");
  assert.equal(remainingPercent(pairedQuotaLimit.fiveHour), 60);
  assert.equal(remainingPercent(pairedQuotaLimit.weekly), 25);
  const competingQuotaBuckets = [{
    limit_id: "codex_bengalfox", limit_name: "GPT-5.3-Codex-Spark",
    plan_type: "pro", primary: {
      used_percent: 0, resets_at: 1_800_500_000,
      window_duration_mins: 10_080,
    }, secondary: null,
  }, {
    limit_id: "codex", limit_name: null, plan_type: "pro",
    primary: {
      used_percent: 2, resets_at: 1_800_500_000,
      window_duration_mins: 10_080,
    }, secondary: null,
  }];
  for (const rateLimits of [
    competingQuotaBuckets,
    [...competingQuotaBuckets].reverse(),
  ]) {
    const accountQuota = accountQuotaWindows({
      ...quotaReport, rate_limits: rateLimits,
    });
    assert.equal(accountQuota.limit?.limit_id, "codex",
      "model-specific bucket ordering must not replace the account quota");
    assert.equal(remainingPercent(accountQuota.weekly), 98,
      "cached updates and explicit reads must show the same account quota");
  }
  const completeStatusMarkup = renderToStaticMarkup(createElement(StatusSheet, {
    open: true,
    report: { ...quotaReport, rate_limits: competingQuotaBuckets },
    notices: [],
    error: null,
    onClose: () => {},
    onRefresh: () => {},
  }));
  assert.match(completeStatusMarkup, /GPT-5.3-Codex-Spark/,
    "complete status must render every model-specific bucket returned upstream");
  assert.match(completeStatusMarkup, /剩余 100%/);
  assert.match(completeStatusMarkup, /剩余 98%/);
  assert.match(completeStatusMarkup, /一周窗口/);
  assert.doesNotMatch(completeStatusMarkup, /10080 分钟窗口|% 已用/);
  const specializedQuotaOnly = accountQuotaWindows({
    ...quotaReport, rate_limits: [competingQuotaBuckets[0]],
  });
  assert.equal(specializedQuotaOnly.limit, null,
    "a model-specific quota must not masquerade as the account quota");
  assert.equal(specializedQuotaOnly.weekly, null);
  const freeQuotaReport = {
    ...quotaReport,
    account: {
      auth_type: "chatgpt", plan_type: "free", requires_openai_auth: true,
    },
    rate_limits: [{
      limit_id: "codex", limit_name: "codex", plan_type: "free",
      primary: {
        used_percent: 11, resets_at: 1_800_500_000,
        window_duration_mins: 43_200,
      },
      secondary: null,
    }],
  } satisfies StatusReport;
  const freeQuota = accountQuotaWindows(freeQuotaReport);
  assert.equal(freeQuota.limit?.limit_id, "codex",
    "the authoritative account bucket must survive non-Plus window shapes");
  assert.equal(remainingPercent(freeQuota.overall), 89);
  assert.equal(freeQuota.fiveHour, null);
  assert.equal(freeQuota.weekly, null);
  const unknownWindows = accountQuotaWindows({
    ...quotaReport,
    rate_limits: [{
      limit_id: "other", limit_name: "Other", plan_type: "plus",
      primary: {
        used_percent: 10, resets_at: null, window_duration_mins: null,
      },
      secondary: {
        used_percent: 20, resets_at: null, window_duration_mins: 1_440,
      },
    }],
  });
  assert.equal(unknownWindows.fiveHour, null,
    "unknown windows must not be relabeled as five-hour quota");
  assert.equal(unknownWindows.weekly, null,
    "unknown windows must not be relabeled as weekly quota");
  const usageMarkup = renderToStaticMarkup(createElement(UsageMeter, {
    open: true,
    report: quotaReport,
    error: null,
    loading: false,
    onToggle: () => {},
    onRefresh: () => {},
    onOpenStatus: () => {},
  }));
  assert.match(usageMarkup, /5 小时额度/);
  assert.match(usageMarkup, /每周额度/);
  assert.match(usageMarkup, /剩余 60%/);
  assert.match(usageMarkup, /剩余 25%/);
  assert.match(usageMarkup, /width:60%/);
  assert.match(usageMarkup, /width:25%/);
  assert.doesNotMatch(usageMarkup, /used_percent|rate_limit_reached/i);
  assert.match(usageMarkup, /aria-haspopup="true"/);
  assert.doesNotMatch(usageMarkup, /role="dialog"/);
  const resetStatusMarkup = renderToStaticMarkup(createElement(StatusSheet, {
    open: true,
    report: { ...quotaReport, reset_credits: resetStatus.reset_credits },
    notices: [],
    error: null,
    resetCreditLoading: false,
    resetCreditDisabled: false,
    resetCreditResult: null,
    onClose: () => {},
    onRefresh: () => {},
    onConsumeResetCredit: () => true,
  }));
  assert.match(resetStatusMarkup, /重置券/);
  assert.match(resetStatusMarkup, /<b>2<\/b> 张可用/);
  assert.match(resetStatusMarkup, /Launch reset/);
  assert.match(resetStatusMarkup, /Reset an eligible Codex limit window/);
  assert.match(resetStatusMarkup, /使用下一张/,
    "a capped detail list must retain the backend-selects-next action");
  assert.doesNotMatch(resetStatusMarkup, /SECRET|paidCredits|balance/);
  const localizedCredit = {
    ...resetStatus.reset_credits.credits[0],
    title: "Full reset",
    description: "Thanks for using Codex! You've been granted one free rate limit reset.",
  };
  const originalCredit = JSON.stringify(localizedCredit);
  assert.deepEqual(resetCreditPresentation(localizedCredit), {
    title: "完整额度重置券",
    description: "感谢使用 Codex！你已获赠一次免费的额度重置机会。",
  });
  const confirmation = resetCreditConfirmation(localizedCredit);
  assert.match(confirmation, /确定使用完整额度重置券吗/);
  assert.match(confirmation, /感谢使用 Codex！你已获赠一次免费的额度重置机会。/);
  assert.match(confirmation, /消耗一张重置券，并重置当前符合条件的额度窗口/);
  assert.doesNotMatch(confirmation, /Full reset|Thanks for using|You've been granted/);
  assert.deepEqual(resetCreditPresentation(), {
    title: "Codex 额度重置券", description: "",
  });
  assert.match(resetCreditConfirmation(), /确定使用一张 Codex 额度重置券吗/);
  assert.deepEqual(resetCreditPresentation({ title: " ", description: null }), {
    title: "Codex 额度重置券", description: "",
  });
  for (const copy of [
    { title: "中文券名", description: "仅用于符合条件的窗口。" },
    { title: "Future credit", description: "Only resets the weekly window." },
    { title: "constructor", description: "__proto__" },
  ]) {
    assert.deepEqual(resetCreditPresentation(copy), copy,
      "Chinese copy and unknown upstream conditions must not be overwritten");
  }
  const localizedMarkup = renderToStaticMarkup(createElement(StatusSheet, {
    open: true,
    report: { ...quotaReport, reset_credits: {
      available_count: 1, credits: [localizedCredit],
    } },
    onClose: () => {}, onRefresh: () => {}, onConsumeResetCredit: () => true,
  }));
  assert.match(localizedMarkup, /<b>完整额度重置券<\/b>/);
  assert.match(localizedMarkup, /感谢使用 Codex！你已获赠一次免费的额度重置机会。/);
  assert.match(localizedMarkup, /到期/);
  assert.match(localizedMarkup, />使用<\/button>/);
  assert.doesNotMatch(localizedMarkup, /Full reset|Thanks for using|You've been granted/);
  assert.equal(JSON.stringify(localizedCredit), originalCredit,
    "localization must not mutate upstream coupon data or its redemption id");
  const claudeUsageMarkup = renderToStaticMarkup(createElement(UsageMeter, {
    engine: "claude",
    open: true,
    report: null,
    rateLimits: claudeState.runtimes[claudeSid].rateLimits,
    error: null,
    loading: false,
    onToggle: () => {},
  }));
  assert.match(claudeUsageMarkup, /5 小时额度/);
  assert.match(claudeUsageMarkup, /每周额度/);
  assert.match(claudeUsageMarkup, /Opus 专项周额度/);
  assert.match(claudeUsageMarkup, /剩余 65%/);
  assert.match(claudeUsageMarkup, /剩余 30%/);
  assert.match(claudeUsageMarkup, /剩余 20%/);
  assert.match(claudeUsageMarkup, /原生额度事件自动同步/);
  assert.doesNotMatch(claudeUsageMarkup, />刷新<|完整状态/,
    "Claude SDK has no supported pull/status API, so the popover is push-only");
  const compactUsageMarkup = renderToStaticMarkup(createElement(UsageMeter, {
    open: false,
    report: quotaReport,
    error: null,
    loading: false,
    onToggle: () => {},
    onRefresh: () => {},
  }));
  assert.doesNotMatch(compactUsageMarkup, /<small>60%<\/small>|<small>25%<\/small>/,
    "the compact meter must keep percentages accessible but visually hidden");
  const failedUsageMarkup = renderToStaticMarkup(createElement(UsageMeter, {
    open: true,
    report: quotaReport,
    error: "账户额度暂不可用",
    loading: false,
    onToggle: () => {},
    onRefresh: () => {},
  }));
  assert.match(failedUsageMarkup, /账户额度暂不可用/);
  assert.doesNotMatch(failedUsageMarkup, /剩余 60%|剩余 25%|width:60%|width:25%/,
    "a failed account-switch refresh must quarantine the previous report");
  const authenticatedWithoutQuotaMarkup = renderToStaticMarkup(
    createElement(UsageMeter, {
      open: true,
      report: {
        ...quotaReport,
        account: {
          auth_type: "chatgpt",
          plan_type: "free",
          requires_openai_auth: true,
        },
        rate_limits: [],
      },
      error: null,
      loading: false,
      onToggle: () => {},
      onRefresh: () => {},
    }),
  );
  assert.match(
    authenticatedWithoutQuotaMarkup,
    /账户已登录；本次额度读取失败，请刷新重试。/,
  );
  assert.doesNotMatch(
    authenticatedWithoutQuotaMarkup,
    /暂未提供账户额度/,
    "an authenticated account must not be presented as missing",
  );
  const freeUsageMarkup = renderToStaticMarkup(createElement(UsageMeter, {
    open: true,
    report: freeQuotaReport,
    error: null,
    loading: false,
    onToggle: () => {},
    onRefresh: () => {},
    onOpenStatus: () => {},
  }));
  assert.match(freeUsageMarkup, /Codex 总额度/);
  assert.match(freeUsageMarkup, /剩余 89%/);
  assert.match(freeUsageMarkup, /43200 分钟窗口/);
  assert.doesNotMatch(
    freeUsageMarkup,
    /读取失败|5 小时额度|每周额度/,
    "a valid Free quota must not be rendered as a failed Plus-style pair",
  );

  assert.equal(TRANSIENT_BANNER_TTL_MS, 6_000);
  const transientBanner = renderToStaticMarkup(createElement(ReconnectBanner, {
    banner: "详细过程已过期，请刷新会话后重试",
    replaying: false,
    truncated: false,
    busy: false,
    onDismiss: () => {},
  }));
  assert.match(transientBanner, /banner-dismiss/);
  assert.match(transientBanner, /关闭提示/);
  const connectionBanner = renderToStaticMarkup(createElement(ReconnectBanner, {
    banner: "machine offline — waiting for reconnect",
    replaying: false,
    truncated: false,
    busy: true,
    onDismiss: () => {},
  }));
  assert.match(connectionBanner, /banner-dismiss/,
    "every persistent banner remains explicitly dismissible");
  const reconnectBannerSource = readFileSync(resolve(
    process.cwd(), "src/components/ReconnectBanner.tsx"), "utf8");
  assert.match(reconnectBannerSource,
    /window\.setTimeout\([\s\S]*TRANSIENT_BANNER_TTL_MS/,
    "transient banners must schedule their own dismissal");
  assert.match(reconnectBannerSource, /window\.clearTimeout\(timer\)/,
    "a changed or unmounted banner must cancel its stale timer");

  const appSource = readFileSync(resolve(process.cwd(), "src/App.tsx"), "utf8");
  assert.ok(appSource.indexOf("<ReconnectBanner") < appSource.indexOf("<NoticeStack"),
    "NoticeStack must remain below, not replace, ReconnectBanner");
  assert.match(appSource,
    /msg\.type === "state" && msg\.state === "idle"[\s\S]*sendGetStatusTo\(msg\.sid\)/,
    "Codex quota must refresh after the authoritative idle boundary");
  assert.match(appSource,
    /deferredStatusRefreshRef\.current\.add\(msg\.sid\)/,
    "an in-flight old-generation read must schedule an idle follow-up");
  assert.match(appSource,
    /focusedEngine !== "codex"[\s\S]*refreshStatus\(\)/,
    "focused Codex sessions must load quota without opening /status");
  assert.match(appSource, /statusReport=\{rt\.statusReport\}/);
  const statusSheetSource = readFileSync(resolve(
    process.cwd(), "src/components/StatusSheet.tsx"), "utf8");
  assert.match(statusSheetSource, /window\.confirm\(/,
    "redeeming an earned reset credit must keep an explicit user confirmation");
  assert.match(statusSheetSource, /resetLaunchRef\.current/,
    "the lazy reset-credit UI must reject double clicks before React rerenders");
  const composerSource = readFileSync(resolve(
    process.cwd(), "src/components/Composer.tsx"), "utf8");
  assert.match(composerSource, /<UsageMeter/);

  const boundary = new ErrorBoundary({ children: createElement("div") });
  boundary.state = { error: new Error(hiddenDiagnostic) };
  const boundaryMarkup = renderToStaticMarkup(boundary.render());
  assert.match(boundaryMarkup, /页面需要重新载入/);
  assert.match(boundaryMarkup, /重新载入/);
  assert.doesNotMatch(boundaryMarkup, /crash|wrapper|private|stack/i,
    "the recovery screen must not expose the render exception");
} finally {
  await harness.close();
}

console.log("notice and live rate-limit tests passed");
