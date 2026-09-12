import assert from "node:assert/strict";
import { createServer } from "vite";

import type { ServerEvent } from "../src/protocol.ts";


const harness = await createServer({
  root: process.cwd(),
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, watch: null },
});

try {
  const { createRuntime, initialState, reduce } =
    await harness.ssrLoadModule("/src/reducer.ts");
  const event = (body: Record<string, unknown>): ServerEvent => ({
    v: 37, ts: 10, ...body,
  } as ServerEvent);
  for (const includeError of [true, false]) {
    const sid = "capacity-terminal-order";
    let state = { ...initialState, focusedSid: sid,
      sessions: [{ session_id: sid, engine: "codex", space: "code" }],
      runtimes: { [sid]: { ...createRuntime(), state: "running" } } };
    const apply = (body: Record<string, unknown>) => {
      state = reduce(state, { type: "event", event: event({ sid, ...body }) });
    };
    apply({ type: "user_msg", msg_id: "failed-message", prompt: "deploy", seq: 1 });
    apply({ type: "turn_binding", msg_id: "failed-message", turn_id: "failed-native", seq: 2 });
    if (includeError) apply({ type: "error", code: "cc_crash", msg_id: "failed-message",
      message: "当前模型繁忙，请稍后重试或切换模型。", seq: 3 });
    apply({ type: "turn_end", turn_id: "failed-native", seq: 4,
      result: { subtype: "error", is_error: true, duration_ms: 337204 } });
    const failed = state.runtimes[sid].turns[0];
    assert.equal(failed.done, true);
    assert.equal(failed.error, includeError ? "当前模型繁忙，请稍后重试或切换模型。" : "本次回复未完成，请重试。",
      "TurnEnd keeps the preceding provider error; a lost Error still cannot become success");
    assert.equal(failed.terminalSource, "failed");
    apply({ type: "user_msg", msg_id: "next-message", prompt: "", seq: 5 });
    apply({ type: "turn_binding", msg_id: "next-message", turn_id: "next-native", seq: 6 });
    apply({ type: "process", item_id: "next-tool", kind: "command", phase: "start",
      status: "running", title: "verify", turn_id: "next-native", seq: 7 });
    apply({ type: "turn_end", turn_id: "failed-native", seq: 8,
      result: { subtype: "error", is_error: true, duration_ms: 337204 } });
    assert.equal(state.runtimes[sid].turns[1].done, false,
      "a repeated old failure cannot stop the next native turn");
    assert.equal(state.runtimes[sid].turns[0].error, failed.error);
  }
  const history = (
    sid: string,
    revision: string,
    generation: string,
    terminalFences: Array<Record<string, unknown>>,
    extra: Record<string, unknown> = {},
  ): ServerEvent => event({
    type: "history",
    sid,
    session_id: sid,
    revision,
    generation,
    build_seq: 1,
    authoritative: false,
    events: [],
    turns: [],
    detail: "summary",
    has_more: false,
    terminal_fences: terminalFences,
    ...extra,
  });

  const staleSid = "default@stale-terminal-fence";
  const staleNativeId = "stale-native-turn";
  const completion = { id: "already-notified", unread: false, revision: 7 };
  let staleState = {
    ...initialState,
    focusedSid: staleSid,
    runtimes: {
      [staleSid]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
        historyRevision: "stale-r1",
        historyGeneration: "stale-g1",
        historyBuildSeq: 5,
        completion,
        turns: [{
          id: "stale-browser-turn",
          forkPointId: staleNativeId,
          prompt: "finish despite stale history",
          blocks: [{
            kind: "process" as const,
            item_id: "stale-process",
            processKind: "command" as const,
            phase: "start" as const,
            status: "running" as const,
            turn_id: staleNativeId,
            title: "running",
            done: false,
          }],
          done: false,
        }],
      },
    },
  };
  staleState = reduce(staleState, {
    type: "event",
    event: history(staleSid, "stale-r1", "stale-g1", [{
      turn_id: staleNativeId,
      status: "completed",
      duration_ms: 3210,
      completed_at: 20,
    }], { build_seq: 4, live_seq: 2, in_progress: true }),
  });
  assert.deepEqual({
    done: staleState.runtimes[staleSid].turns[0].done,
    processDone: staleState.runtimes[staleSid].turns[0].blocks[0].done,
    durationMs: staleState.runtimes[staleSid].turns[0].durationMs,
    doneTs: staleState.runtimes[staleSid].turns[0].doneTs,
    state: staleState.runtimes[staleSid].state,
    completion: staleState.runtimes[staleSid].completion,
  }, {
    done: true,
    processDone: true,
    durationMs: 3210,
    doneTs: 20_000,
    state: "running",
    completion,
  }, "a stale History fence repairs only its exact turn and no receipt/state");
  assert.equal(
    staleState.runtimes[staleSid].pendingTerminalFences
      ?.fences[0].turn_id,
    staleNativeId,
    "a stale page retains an applied fence for a possibly delayed steer segment",
  );

  const settledSid = "default@settled-terminal-fence";
  const settledState = reduce({
    ...initialState,
    focusedSid: settledSid,
    runtimes: {
      [settledSid]: {
        ...createRuntime(),
        historyRevision: "settled-r1",
        historyGeneration: "settled-g1",
        turns: [{
          id: "settled-browser-turn",
          forkPointId: "settled-native-turn",
          prompt: "already failed",
          blocks: [],
          done: true,
          error: "authoritative live failure",
        }],
      },
    },
  }, {
    type: "event",
    event: history(settledSid, "settled-r1", "settled-g1", [{
      turn_id: "settled-native-turn", status: "completed",
    }]),
  });
  assert.equal(
    settledState.runtimes[settledSid].turns[0].error,
    "authoritative live failure",
    "a recovery fence cannot rewrite an already terminal narrative outcome",
  );

  const liveWinsSid = "default@live-wins-terminal-fence";
  let liveWinsState = {
    ...initialState,
    focusedSid: liveWinsSid,
    runtimes: {
      [liveWinsSid]: {
        ...createRuntime(),
        historyRevision: "live-wins-r1",
        historyGeneration: "live-wins-g1",
      },
    },
  };
  liveWinsState = reduce(liveWinsState, {
    type: "event",
    event: history(liveWinsSid, "live-wins-r1", "live-wins-g1", [{
      turn_id: "live-wins-native", status: "completed",
    }]),
  });
  liveWinsState = reduce(liveWinsState, {
    type: "event",
    event: event({
      type: "user_msg", sid: liveWinsSid, seq: 2,
      msg_id: "live-wins-browser", prompt: "live outcome wins",
    }),
  });
  liveWinsState = reduce(liveWinsState, {
    type: "event",
    event: event({
      type: "turn_end", sid: liveWinsSid, seq: 3,
      turn_id: "live-wins-native",
      result: {
        subtype: "error_during_execution",
        duration_ms: 5,
        is_error: true,
      },
    }),
  });
  assert.equal(liveWinsState.runtimes[liveWinsSid].turns[0].interrupted, true);
  assert.equal(
    liveWinsState.runtimes[liveWinsSid].pendingTerminalFences,
    null,
    "a later live TurnEnd consumes but is never rewritten by a pending fence",
  );

  const pendingSid = "default@pending-terminal-fence";
  const pendingNativeId = "pending-native-turn";
  let pendingState = {
    ...initialState,
    focusedSid: pendingSid,
    runtimes: {
      [pendingSid]: {
        ...createRuntime(),
        historyRevision: "pending-r1",
        historyGeneration: "pending-g1",
        historyBuildSeq: 5,
      },
    },
  };
  pendingState = reduce(pendingState, {
    type: "event",
    event: history(pendingSid, "pending-r1", "pending-g1", [{
      turn_id: pendingNativeId, status: "completed",
    }]),
  });
  assert.equal(
    pendingState.runtimes[pendingSid].pendingTerminalFences
      ?.fences[0].turn_id,
    pendingNativeId,
    "a fence can arrive before its exact narrative identity",
  );
  pendingState = reduce(pendingState, {
    type: "event",
    event: history(pendingSid, "pending-r1", "pending-g1", [], {
      build_seq: 0,
    }),
  });
  assert.equal(
    pendingState.runtimes[pendingSid].pendingTerminalFences
      ?.fences[0].turn_id,
    pendingNativeId,
    "an older empty snapshot cannot erase a newer pending fence",
  );
  pendingState = reduce(pendingState, {
    type: "event",
    event: event({
      type: "user_msg", sid: pendingSid, seq: 2,
      msg_id: "pending-browser-turn", prompt: "late binding",
    }),
  });
  pendingState = reduce(pendingState, {
    type: "event",
    event: event({
      type: "turn_binding", sid: pendingSid, seq: 3,
      msg_id: "pending-browser-turn", turn_id: pendingNativeId,
    }),
  });
  assert.equal(pendingState.runtimes[pendingSid].turns[0].done, true);
  assert.equal(
    pendingState.runtimes[pendingSid].pendingTerminalFences
      ?.fences[0].turn_id,
    pendingNativeId,
    "an exact binding applies but cannot retire a fence before settled History",
  );
  pendingState = reduce(pendingState, {
    type: "event",
    event: history(pendingSid, "pending-r1", "pending-g1", [{
      turn_id: pendingNativeId, status: "completed",
    }], {
      authoritative: true,
      build_seq: 6,
      live_seq: 3,
      in_progress: false,
      turns: [{
        id: "pending-browser-turn",
        forkPointId: pendingNativeId,
        prompt: "late binding",
        blocks: [],
        done: true,
        detailEventCount: 0,
        detailLoaded: false,
      }],
    }),
  });
  assert.equal(
    pendingState.runtimes[pendingSid].pendingTerminalFences,
    null,
    "an idle unraced authoritative page retires the applied fence",
  );

  const steeredSid = "default@steered-terminal-fence";
  const steeredNativeId = "shared-steered-native-turn";
  let steeredState = {
    ...initialState,
    focusedSid: steeredSid,
    runtimes: {
      [steeredSid]: {
        ...createRuntime(),
        historyRevision: "steered-r1",
        historyGeneration: "steered-g1",
        historyBuildSeq: 5,
        turns: [{
          id: "completed-predecessor",
          forkPointId: steeredNativeId,
          prompt: "before steer",
          blocks: [],
          done: true,
        }],
      },
    },
  };
  steeredState = reduce(steeredState, {
    type: "event",
    event: history(steeredSid, "steered-r1", "steered-g1", [{
      turn_id: steeredNativeId, status: "completed",
    }], { build_seq: 4 }),
  });
  assert.equal(
    steeredState.runtimes[steeredSid].pendingTerminalFences
      ?.fences[0].turn_id,
    steeredNativeId,
    "a completed predecessor cannot consume a shared native turn fence",
  );
  steeredState = reduce(steeredState, {
    type: "event",
    event: event({
      type: "turn_steered", sid: steeredSid, seq: 6,
      msg_id: "late-steer-segment", turn_id: steeredNativeId,
      prompt: "late steer narrative",
    }),
  });
  assert.equal(
    steeredState.runtimes[steeredSid].turns
      .find((turn: { id: string }) => turn.id === "late-steer-segment")
      ?.done,
    true,
    "the retained fence closes the exact later steer segment",
  );
  assert.equal(
    steeredState.runtimes[steeredSid].pendingTerminalFences
      ?.fences[0].turn_id,
    steeredNativeId,
    "the late steer consumes the outcome but retains it until settled History",
  );
  steeredState = reduce(steeredState, {
    type: "event",
    event: history(steeredSid, "steered-r1", "steered-g1", [{
      turn_id: steeredNativeId, status: "completed",
    }], {
      authoritative: true,
      build_seq: 6,
      live_seq: 6,
      in_progress: false,
      turns: [{
        id: "completed-predecessor",
        forkPointId: steeredNativeId,
        prompt: "before steer",
        blocks: [],
        done: true,
        detailEventCount: 0,
        detailLoaded: false,
      }, {
        id: "late-steer-segment",
        forkPointId: steeredNativeId,
        prompt: "late steer narrative",
        blocks: [],
        done: true,
        detailEventCount: 0,
        detailLoaded: false,
      }],
    }),
  });
  assert.equal(steeredState.runtimes[steeredSid].pendingTerminalFences, null);

  const runningSteerSid = "default@running-steer-terminal-fence";
  const runningSteerNativeId = "running-shared-native-turn";
  let runningSteerState = {
    ...initialState,
    focusedSid: runningSteerSid,
    runtimes: {
      [runningSteerSid]: {
        ...createRuntime(),
        state: "running" as const,
        historyRevision: "running-steer-r1",
        historyGeneration: "running-steer-g1",
      },
    },
  };
  runningSteerState = reduce(runningSteerState, {
    type: "event",
    event: history(
      runningSteerSid,
      "running-steer-r1",
      "running-steer-g1",
      [{ turn_id: runningSteerNativeId, status: "completed" }],
      {
        authoritative: true,
        in_progress: true,
        live_seq: 0,
        turns: [{
          id: "running-completed-predecessor",
          forkPointId: runningSteerNativeId,
          prompt: "completed segment before steer",
          blocks: [],
          done: true,
          detailEventCount: 0,
          detailLoaded: false,
        }],
      },
    ),
  });
  assert.equal(
    runningSteerState.runtimes[runningSteerSid].pendingTerminalFences
      ?.fences[0].turn_id,
    runningSteerNativeId,
    "a running authoritative page cannot consume a future steer fence",
  );
  runningSteerState = reduce(runningSteerState, {
    type: "event",
    event: event({
      type: "turn_steered", sid: runningSteerSid, seq: 2,
      msg_id: "running-late-steer", turn_id: runningSteerNativeId,
      prompt: "late steer after the running page",
    }),
  });
  assert.equal(
    runningSteerState.runtimes[runningSteerSid].turns
      .find((turn: { id: string }) => turn.id === "running-late-steer")
      ?.done,
    true,
  );
  assert.equal(
    runningSteerState.runtimes[runningSteerSid].pendingTerminalFences
      ?.fences[0].turn_id,
    runningSteerNativeId,
  );

  const cacheSid = "default@cache-terminal-fence";
  let cacheState = {
    ...initialState,
    focusedSid: cacheSid,
    runtimes: {
      [cacheSid]: {
        ...createRuntime(),
        historyRevision: "cache-r1",
        historyGeneration: "cache-g1",
      },
    },
  };
  cacheState = reduce(cacheState, {
    type: "event",
    event: history(cacheSid, "cache-r1", "cache-g1", [{
      turn_id: "cache-native-turn", status: "completed",
    }], { authoritative: true }),
  });
  cacheState = reduce(cacheState, {
    type: "hydrate_cache",
    sid: cacheSid,
    revision: "cache-r1",
    generation: "cache-g1",
    turns: [{
      id: "cache-browser-turn",
      codexTurnId: "cache-native-turn",
      prompt: "cache won the race",
      blocks: [],
      done: false,
    }],
  });
  assert.equal(
    cacheState.runtimes[cacheSid].turns[0].done,
    true,
    "cache hydration applies a terminal which arrived before its identity",
  );
  assert.equal(
    cacheState.runtimes[cacheSid].pendingTerminalFences
      ?.fences[0].turn_id,
    "cache-native-turn",
    "cache hydration cannot prove that no later shared-id segment exists",
  );

  const previewSid = "default@preview-terminal-fence";
  const previewState = reduce({
    ...initialState,
    focusedSid: previewSid,
    runtimes: { [previewSid]: createRuntime() },
  }, {
    type: "event",
    event: history(previewSid, "preview-r1", "preview-g1", [{
      turn_id: "preview-native-turn", status: "completed",
    }], {
      turns: [{
        id: "preview-browser-turn",
        forkPointId: "preview-native-turn",
        prompt: "sampled preview",
        blocks: [],
        done: false,
        detailEventCount: 0,
        detailLoaded: false,
      }],
    }),
  });
  assert.equal(
    previewState.historyRecovery?.turns[0].done,
    true,
    "a provisional cold preview applies its independently delivered terminal",
  );

  const clearedSid = "default@cleared-terminal-fence";
  let clearedState = {
    ...initialState,
    focusedSid: clearedSid,
    runtimes: {
      [clearedSid]: {
        ...createRuntime(),
        historyRevision: "cleared-r1",
        historyGeneration: "cleared-g1",
      },
    },
  };
  clearedState = reduce(clearedState, {
    type: "event",
    event: history(clearedSid, "cleared-r1", "cleared-g1", [{
      turn_id: "vanished-native-turn", status: "completed",
    }]),
  });
  assert.ok(clearedState.runtimes[clearedSid].pendingTerminalFences);
  clearedState = reduce(clearedState, {
    type: "event",
    event: history(clearedSid, "cleared-r1", "cleared-g1", [], {
      build_seq: 2,
    }),
  });
  assert.equal(
    clearedState.runtimes[clearedSid].pendingTerminalFences,
    null,
    "a current empty snapshot invalidates an unmatched old fence",
  );

  const rebuildSid = "default@rebuild-terminal-fence";
  const rebuildState = reduce({
    ...initialState,
    focusedSid: rebuildSid,
    runtimes: {
      [rebuildSid]: {
        ...createRuntime(),
        historyRevision: "rebuild-r1",
        historyGeneration: "rebuild-g1",
        pendingTerminalFences: {
          revision: "rebuild-r1",
          generation: "rebuild-g1",
          fences: [{ turn_id: "old-native-turn", status: "completed" }],
        },
      },
    },
  }, {
    type: "event",
    event: event({
      type: "replay_start", sid: rebuildSid,
      generation: "rebuild-g1", from_seq: 0, truncated: false,
      rebuild: true,
    }),
  });
  assert.equal(
    rebuildState.runtimes[rebuildSid].pendingTerminalFences,
    null,
    "a same-generation rebuild invalidates prior narrative fences",
  );

  const invalidatedSid = "default@invalidated-terminal-fence";
  let invalidatedState = {
    ...initialState,
    focusedSid: invalidatedSid,
    runtimes: {
      [invalidatedSid]: {
        ...createRuntime(),
        historyRevision: "invalidated-r1",
        historyGeneration: "invalidated-g1",
        historyBuildSeq: 9,
      },
    },
  };
  invalidatedState = reduce(invalidatedState, {
    type: "event",
    event: event({
      type: "history_invalidated",
      session_id: invalidatedSid,
      revision: "invalidated-r2",
      sid: invalidatedSid,
      seq: 10,
    }),
  });
  invalidatedState = reduce(invalidatedState, {
    type: "event",
    event: history(
      invalidatedSid,
      "invalidated-r1",
      "invalidated-g1",
      [{ turn_id: "pre-rollback-terminal", status: "completed" }],
      { authoritative: true, build_seq: 10 },
    ),
  });
  assert.equal(
    invalidatedState.runtimes[invalidatedSid].pendingTerminalFences,
    null,
    "a pre-invalidation History cannot reinstall an old terminal fence",
  );

  const unrelatedSid = "default@unrelated-terminal-fence";
  let unrelatedState = {
    ...initialState,
    focusedSid: unrelatedSid,
    runtimes: {
      [unrelatedSid]: {
        ...createRuntime(),
        historyRevision: "unrelated-r1",
        historyGeneration: "unrelated-g1",
      },
    },
  };
  unrelatedState = reduce(unrelatedState, {
    type: "event",
    event: history(unrelatedSid, "unrelated-r1", "unrelated-g1", [{
      turn_id: "old-native-turn", status: "completed",
    }]),
  });
  unrelatedState = reduce(unrelatedState, {
    type: "event",
    event: event({
      type: "user_msg", sid: unrelatedSid, seq: 2,
      msg_id: "new-browser-turn", prompt: "new turn",
    }),
  });
  unrelatedState = reduce(unrelatedState, {
    type: "event",
    event: event({
      type: "turn_binding", sid: unrelatedSid, seq: 3,
      msg_id: "new-browser-turn", turn_id: "new-native-turn",
    }),
  });
  assert.equal(
    unrelatedState.runtimes[unrelatedSid].turns[0].done,
    false,
    "an old fence cannot close a different newer native turn",
  );

  const continuationSid = "default@compact-terminal-fence";
  const continuationId = "compact-interrupted-native";
  const continuationState = reduce({
    ...initialState,
    focusedSid: continuationSid,
    runtimes: {
      [continuationSid]: {
        ...createRuntime(),
        state: "running" as const,
        historyRevision: "compact-r1",
        historyGeneration: "compact-g1",
        turns: [{
          id: "compact-browser-turn",
          forkPointId: continuationId,
          prompt: "continue after compact",
          blocks: [],
          done: false,
        }],
      },
    },
  }, {
    type: "event",
    event: history(continuationSid, "compact-r1", "compact-g1", [{
      turn_id: continuationId, status: "interrupted",
    }], {
      in_progress: true,
      compaction_continuation_turn_ids: [continuationId],
    }),
  });
  assert.equal(continuationState.runtimes[continuationSid].turns[0].done, false);
  assert.equal(
    continuationState.runtimes[continuationSid].pendingTerminalFences,
    null,
    "a proven compaction continuation discards its internal interrupt",
  );

  const nativeId = "same-native-across-profiles";
  const defaultSid = "default@profile-terminal";
  const irisSid = "iris@profile-terminal";
  let profileState = {
    ...initialState,
    focusedSid: defaultSid,
    runtimes: {
      [defaultSid]: {
        ...createRuntime(),
        historyRevision: "default-r1",
        historyGeneration: "profile-g1",
        turns: [{
          id: "default-turn", forkPointId: nativeId,
          prompt: "default", blocks: [], done: false,
        }],
      },
      [irisSid]: {
        ...createRuntime(),
        historyRevision: "iris-r1",
        historyGeneration: "profile-g1",
        turns: [{
          id: "iris-turn", forkPointId: nativeId,
          prompt: "iris", blocks: [], done: false,
        }],
      },
    },
  };
  profileState = reduce(profileState, {
    type: "event",
    event: history(defaultSid, "default-r1", "profile-g1", [{
      turn_id: nativeId, status: "failed",
    }]),
  });
  assert.deepEqual({
    defaultDone: profileState.runtimes[defaultSid].turns[0].done,
    defaultError: profileState.runtimes[defaultSid].turns[0].error,
    irisDone: profileState.runtimes[irisSid].turns[0].done,
  }, {
    defaultDone: true,
    defaultError: "本次回复未完成，请重试。",
    irisDone: false,
  }, "routed profile sids isolate identical native ids");
  profileState = reduce(profileState, {
    type: "event",
    event: history(irisSid, "iris-r1", "profile-g1", [{
      turn_id: nativeId, status: "interrupted",
    }]),
  });
  assert.deepEqual({
    done: profileState.runtimes[irisSid].turns[0].done,
    interrupted: profileState.runtimes[irisSid].turns[0].interrupted,
    error: profileState.runtimes[irisSid].turns[0].error,
  }, { done: true, interrupted: true, error: undefined },
  "interrupted remains distinct from failed");
} finally {
  await harness.close();
}

console.log("codex terminal fence tests passed");
